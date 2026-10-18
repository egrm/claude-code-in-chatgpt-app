"""Durable external task roster, independent of any individual MCP call."""
import asyncio
import json
from pathlib import Path
import time
import uuid

from event_store import EventStore
from policy import MODELS, check_file_scope

TERMINAL = {'completed', 'failed', 'interrupted'}


class Registry:
    def __init__(self, path, backend_factory=None):
        self.store = EventStore(path)
        self.records = {r['task_id']: r for r in self.store.tasks()}
        self.backends, self.workers, self.commands, self.pending = {}, {}, {}, {}
        self.changed = asyncio.Event()
        self.closing = False
        if backend_factory is None:
            from sdk_backend import ClaudeBackend
            backend_factory = ClaudeBackend
        self.factory = backend_factory
        for r in self.records.values():
            if r['state'] not in TERMINAL:
                r['state'] = 'interrupted'
                r['error'] = 'Supervisor restarted; resume the saved session explicitly'
                r['current_operation'] = None
                r['pending_requests'] = []
                self._event(r, 'recovered', {'state': 'interrupted'})

    def _open(self):
        if self.closing:
            raise ValueError('Supervisor worker is shutting down; reconnect and resume tasks explicitly')

    def _record(self, task_id):
        if task_id not in self.records:
            raise ValueError('Unknown external Claude task ID')
        return self.records[task_id]

    def _event(self, r, kind, data):
        r['cursor'] = self.store.append(r['task_id'], kind, data)
        r['last_activity_at'] = time.time()
        self.store.save_task(r)
        self.changed.set()

    def status(self, task_id):
        r = self._record(task_id)
        fields = ('task_id', 'backend', 'state', 'workspace', 'session_id', 'requested_model',
                  'actual_model', 'effort', 'current_operation', 'cursor', 'pending_requests', 'error', 'billing')
        status = {k: r.get(k) for k in fields}
        status['pending_requests'] = [{'request_id':p['request_id'], 'tool':p['tool'],
            'summary': str(p['input'].get('file_path') or p['input'].get('command') or 'Clarification question')[:200]}
            for p in r['pending_requests']]
        status['process_connected'] = task_id in self.backends
        status['activity_age_s'] = round(max(0, time.time()-r['last_activity_at']), 1)
        status['stalled'] = False
        status['activity_note'] = 'No recent events; inspect operation before diagnosing a stall' if status['activity_age_s'] > 60 and r['state'] == 'running' else None
        return status

    def list(self, state=None, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        rows = sorted(self.records.values(), key=lambda r: r['created_at'], reverse=True)
        return [{k: self.status(r['task_id'])[k] for k in ('task_id', 'state', 'requested_model', 'current_operation', 'cursor', 'activity_age_s', 'process_connected')}
                for r in rows if state is None or r['state'] == state][:limit]

    async def start(self, prompt, workspace, model='claude-opus-5-5', effort='high', tools=None,
                    max_turns=30, max_budget_usd=1.0):
        self._open()
        if not prompt.strip() or len(prompt) > 64000:
            raise ValueError('Provide a nonempty prompt of at most 64000 characters')
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('workspace must be an existing directory')
        if model not in MODELS or effort not in ('low', 'medium', 'high'):
            raise ValueError('Use exact claude-opus-5-5 or claude-haiku-5-5 and low/medium/high effort')
        permitted = {'Read', 'Glob', 'Grep', 'Write', 'Edit', 'Bash', 'AskUserQuestion'}
        tools = list(permitted) if tools is None else tools
        if not set(tools).issubset(permitted):
            raise ValueError('Unsupported tool; only local coding tools and AskUserQuestion are available')
        if not 1 <= max_turns <= 100 or not 0 < max_budget_usd <= 10:
            raise ValueError('max_turns must be 1..100 and estimate budget >0 and <=10')
        self._check_capacity()
        tid = str(uuid.uuid4())
        r = dict(task_id=tid, backend='claude', state='running', workspace=str(root), session_id=None,
                 requested_model=model, actual_model=None, effort=effort, tools=tools,
                 max_turns=max_turns, max_budget_usd=max_budget_usd, current_operation='connecting',
                 created_at=time.time(), last_activity_at=time.time(), pending_requests=[], cursor=0,
                 result=None, error=None, billing=None)
        self.records[tid] = r
        self._event(r, 'started', {'model': model, 'effort': effort})
        self._launch(r, prompt)
        return self.status(tid)

    def _check_capacity(self):
        if sum(not w.done() for w in self.workers.values()) >= 4:
            raise ValueError('Four connected Claude tasks already exist; stop one first')

    def _launch(self, r, prompt):
        tid = r['task_id']
        self.commands[tid] = asyncio.Queue()
        self.workers[tid] = asyncio.create_task(self._worker(r, prompt))

    async def _worker(self, r, prompt):
        tid = r['task_id']
        backend = self.factory(r, lambda kind, data: self.emit(tid, kind, data),
                               lambda tool, data: self.request(tid, tool, data),
                               lambda kind, data: self.hook(tid, kind, data))
        try:
            await backend.connect()
            if self.closing:
                return   # shutdown began during connect (even if cancellation was swallowed): never send
            self.backends[tid] = backend
            r['current_operation'] = 'model'
            await backend.send(prompt)
            while True:
                operation, text, reply = await self.commands[tid].get()
                if self.closing:
                    reply.set_exception(ValueError('Supervisor worker is shutting down'))
                    break
                try:
                    if operation in ('interrupt', 'followup') and r['state'] not in TERMINAL:
                        self._cancel_requests(tid)
                        await backend.interrupt()
                        if self.closing:
                            # Interrupt swallowed shutdown's cancellation: keep the persisted state, send nothing.
                            if not reply.done():
                                reply.set_exception(ValueError('Supervisor worker is shutting down'))
                            break
                        r['state'] = 'interrupted'
                        r['current_operation'] = None
                        self._event(r, 'interrupted', {})
                    if operation == 'followup':
                        r.update(state='running', error=None, result=None, current_operation='model')
                        self._event(r, 'followup', {'prompt_chars': len(text)})
                        await backend.send(text)
                    if operation == 'stop':
                        self._cancel_requests(tid)
                        r['state'] = 'interrupted' if r['state'] not in TERMINAL else r['state']
                        self._event(r, 'disconnected', {})
                        reply.set_result(True)
                        break
                    reply.set_result(True)
                except Exception as exc:
                    if not self.closing:
                        r.update(state='failed', error=str(exc)[:2000], current_operation=None)
                        self._event(r, 'failed', {'error': r['error']})
                    self._cancel_requests(tid)
                    if not reply.done():
                        reply.set_exception(exc)
        except asyncio.CancelledError:
            if r['state'] not in TERMINAL:
                r['state'] = 'interrupted'
                self._event(r, 'interrupted', {'reason': 'supervisor shutdown'})
            raise
        except Exception as exc:
            if not self.closing:   # drain already persisted the interrupted state and reason
                r.update(state='failed', error=str(exc)[:2000], current_operation=None)
                self._event(r, 'failed', {'error': r['error']})
        finally:
            self._cancel_requests(tid)
            self.backends.pop(tid, None)
            try:
                await backend.close()
            except Exception:
                pass
            while not self.commands[tid].empty():
                _, _, reply = self.commands[tid].get_nowait()
                if not reply.done():
                    reply.set_exception(ValueError('Claude backend disconnected'))

    async def emit(self, tid, kind, data):
        r = self._record(tid)
        if self.closing and kind in ('result', 'failed', 'disconnected', 'init'):
            # Drain already persisted the final interrupted state; record late output only.
            self._event(r, 'late_' + kind, {k: v for k, v in data.items() if k in ('error', 'is_error', 'interrupted')})
            return
        if kind == 'init':
            r['session_id'] = data.get('session_id', r['session_id'])
            r['actual_model'] = data.get('model')
            if r['actual_model'] != r['requested_model']:
                raise ValueError('Actual model differs from requested model; refusing substitution')
        elif kind == 'billing':
            r['billing'] = data
        elif kind == 'result':
            r['result'] = str(data.get('result', ''))[:32000]
            r['state'] = 'interrupted' if data.get('interrupted') else ('failed' if data.get('is_error') else 'completed')
            r['error'] = str(data.get('error') or data.get('result') or 'Claude task failed')[:2000] if r['state'] == 'failed' else None
            r['current_operation'] = None
            self._cancel_requests(tid)
        elif kind in ('failed', 'disconnected'):
            if kind == 'failed' or r['state'] not in TERMINAL:
                r.update(state='failed', error=str(data.get('error', 'backend stream ended'))[:2000], current_operation=None)
            self._cancel_requests(tid)
        self._event(r, kind, data)
        if kind in ('failed', 'disconnected') and tid in self.commands:
            self.commands[tid].put_nowait(('stop', None, asyncio.get_running_loop().create_future()))

    async def hook(self, tid, kind, data):
        r = self._record(tid)
        if self.closing and kind == 'tool_started':
            return 'Supervisor worker is shutting down; no new tool actions'
        tool, inputs = data.get('tool_name'), data.get('tool_input', {})
        reason = check_file_scope(r['workspace'], tool, inputs) if kind == 'tool_started' else None
        if reason:
            self._event(r, 'tool_denied', {'tool': tool, 'reason': reason})
            return reason
        r['current_operation'] = tool if kind == 'tool_started' else 'model'
        self._event(r, kind, data)
        return None

    async def request(self, tid, tool, data):
        r = self._record(tid)
        if self.closing:
            return {'allow': False, 'message': 'Supervisor worker is shutting down'}
        if len(json.dumps(data)) > 64000:
            return {'allow': False, 'message': 'Tool input exceeds 64000 characters; split the operation'}
        reason = check_file_scope(r['workspace'], tool, data)
        if reason:
            return {'allow': False, 'message': reason}
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (tid, future)
        request = {'request_id': request_id, 'tool': tool, 'input': data}
        r['pending_requests'].append(request)
        r['state'] = 'waiting_for_answer' if tool == 'AskUserQuestion' else 'waiting_for_permission'
        r['current_operation'] = tool
        self._event(r, 'request', request)
        try:
            return await future
        finally:
            self.pending.pop(request_id, None)
            r['pending_requests'] = [p for p in r['pending_requests'] if p['request_id'] != request_id]
            if r['state'] in ('waiting_for_answer', 'waiting_for_permission'):
                r['state'] = 'waiting_for_answer' if any(p['tool'] == 'AskUserQuestion' for p in r['pending_requests']) else ('waiting_for_permission' if r['pending_requests'] else 'running')
            self.store.save_task(r)

    def request_detail(self, tid, request_id):
        r = self._record(tid)
        pending = self.pending.get(request_id)
        if not pending or pending[0] != tid or pending[1].done():
            raise ValueError('Unknown, stale or already answered request ID')
        return next(p for p in r['pending_requests'] if p['request_id'] == request_id)

    async def answer(self, tid, request_id, allow=False, answers=None, message=''):
        self._open()
        r = self._record(tid)
        pending = self.pending.get(request_id)
        if not pending or pending[0] != tid or pending[1].done():
            raise ValueError('Unknown, stale or already answered request ID')
        request = next(p for p in r['pending_requests'] if p['request_id'] == request_id)
        if request['tool'] == 'AskUserQuestion' and answers is not None:
            questions = request['input'].get('questions', [])
            if set(answers) != {q['question'] for q in questions} or not all(isinstance(v, str) and v for v in answers.values()):
                raise ValueError('answers must map each exact question text to a nonempty selected label or free text')
            result = {'allow': True, 'answers': answers}
        else:
            result = {'allow': bool(allow), 'message': message[:2000]}
        pending[1].set_result(result)
        self._event(r, 'answered', {'request_id': request_id, 'allow': result['allow']})
        await asyncio.sleep(0)
        status = self.status(tid)
        if result['allow'] and message.strip() and request['tool'] != 'AskUserQuestion':
            # The SDK delivers a message only with a denial; approvals do not steer Claude.
            status['warning'] = 'message was not delivered to Claude on approval; use task_followup for new instructions'
        return status

    def _cancel_requests(self, tid):
        for request_id, (owner, future) in list(self.pending.items()):
            if owner == tid:
                if not future.done():
                    future.set_result({'allow': False, 'message': 'Task interrupted or disconnected'})
                self.pending.pop(request_id, None)
        self._record(tid)['pending_requests'] = []

    async def _command(self, tid, operation, text=None):
        if operation != 'stop':
            self._open()
        self._record(tid)
        if tid not in self.workers or self.workers[tid].done():
            raise ValueError('Backend is disconnected; use task_resume')
        reply = asyncio.get_running_loop().create_future()
        self.commands[tid].put_nowait((operation, text, reply))
        await asyncio.wait_for(asyncio.shield(reply), timeout=25)
        return self.status(tid)

    async def followup(self, tid, text):
        if not text.strip() or len(text) > 64000:
            raise ValueError('Followup must contain 1..64000 characters')
        return await self._command(tid, 'followup', text)

    async def interrupt(self, tid):
        return await self._command(tid, 'interrupt')

    async def stop(self, tid):
        if tid in self.workers and not self.workers[tid].done():
            await self._command(tid, 'stop')
            await asyncio.wait_for(asyncio.shield(self.workers[tid]), 15)
        return self.status(tid)

    async def resume(self, tid, prompt):
        self._open()
        r = self._record(tid)
        if tid in self.workers and not self.workers[tid].done():
            if r['state'] != 'failed':
                return await self.followup(tid, prompt)
            await self.stop(tid)
        if not r['session_id']:
            raise ValueError('No saved Claude session ID; start a new task')
        if not prompt.strip():
            raise ValueError('Resume requires a nonempty instruction')
        self._check_capacity()
        r.update(state='running', error=None, current_operation='reconnecting')
        self._event(r, 'resuming', {})
        self._launch(r, prompt)
        return self.status(tid)

    def events(self, tid, after=0, limit=20):
        self._record(tid)
        return self.store.events(tid, after, limit)

    async def wait(self, tid, after=0, timeout=15):
        if not 0 <= timeout <= 20 or after < 0:
            raise ValueError('timeout must be 0..20 seconds and cursor nonnegative')
        deadline = time.monotonic() + timeout
        while True:
            self.changed.clear()
            status = self.status(tid)
            if status['cursor'] > after or time.monotonic() >= deadline:
                return {**status, 'changed': status['cursor'] > after}
            try:
                await asyncio.wait_for(self.changed.wait(), deadline-time.monotonic())
            except TimeoutError:
                return {**self.status(tid), 'changed': False}

    def result(self, tid):
        r = self._record(tid)
        return {'task_id': tid, 'state': r['state'], 'result': r['result'], 'error': r['error'], 'session_id': r['session_id'], 'billing': r['billing']}

    async def shutdown(self, reason='Supervisor worker stopped', timeout=8.0):
        """Refuse new work, persist active tasks as interrupted, then close every Claude
        connection concurrently within `timeout`. The daemon's process exit (Windows job /
        POSIX group cleanup) terminates anything still unclosed."""
        first = not self.closing
        self.closing = True
        interrupted = 0
        if first:
            for tid, r in self.records.items():
                self._cancel_requests(tid)
                if r['state'] not in TERMINAL:
                    r.update(state='interrupted', current_operation=None,
                             error=f'{reason}; resume the saved session explicitly')
                    self._event(r, 'interrupted', {'reason': reason})
                    interrupted += 1
        live = [w for w in self.workers.values() if not w.done()]
        for w in live:
            w.cancel()
        pending = set()
        if live:
            _, pending = await asyncio.wait(live, timeout=timeout)
        return {'interrupted': interrupted, 'unclosed': len(pending)}

    async def close(self):
        await self.shutdown()
