import asyncio
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))


class Backend:
    def __init__(self, record, emit, permission, hook):
        self.emit, self.permission = emit, permission
    async def connect(self):
        await self.emit('init', {'session_id': 'test-session', 'model': 'claude-haiku-5-5'})
    async def send(self, prompt):
        if prompt == 'complete':
            await self.emit('result', {'result': 'done', 'is_error': False})
    async def interrupt(self):
        await self.emit('result', {'result': '', 'interrupted': True})
    async def close(self):
        pass


async def ready(registry, task_id):
    for _ in range(100):
        if task_id in registry.backends:
            await asyncio.sleep(.001)
            return
        await asyncio.sleep(.001)
    pytest.fail('backend did not connect')


@pytest.mark.asyncio
async def test_lifecycle_context_resume_and_restart_recovery(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=Backend)
    task = await r.start('complete', str(tmp_path), model='claude-haiku-5-5')
    tid = task['task_id']
    await ready(r, tid)
    assert r.status(tid)['state'] == 'completed'
    assert r.result(tid)['result'] == 'done'
    await r.followup(tid, 'keep running')
    assert r.status(tid)['state'] == 'running'
    assert r.status(tid)['session_id'] == 'test-session'
    recovered = Registry(tmp_path/'db', backend_factory=Backend)
    assert recovered.status(tid)['state'] == 'interrupted'
    assert recovered.status(tid)['session_id'] == 'test-session'
    await r.interrupt(tid)
    assert r.status(tid)['state'] == 'interrupted'
    await r.close()
    await recovered.resume(tid, 'complete')
    await ready(recovered, tid)
    assert recovered.status(tid)['state'] == 'completed'
    await recovered.close()


@pytest.mark.asyncio
async def test_permission_question_roundtrip_and_stale_answer(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=Backend)
    tid = (await r.start('work', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await ready(r, tid)
    permission = asyncio.create_task(r.request(tid, 'Write', {'file_path': str(tmp_path/'a')}))
    await asyncio.sleep(.01)
    status = r.status(tid)
    assert status['state'] == 'waiting_for_permission'
    request_id = status['pending_requests'][0]['request_id']
    assert 'input' not in status['pending_requests'][0]
    assert r.request_detail(tid, request_id)['input']['file_path'] == str(tmp_path/'a')
    await r.answer(tid, request_id, allow=True)
    assert (await permission)['allow'] is True
    assert r.status(tid)['state'] == 'running'
    with pytest.raises(ValueError):
        await r.answer(tid, request_id, allow=True)
    question = asyncio.create_task(r.request(tid, 'AskUserQuestion', {'questions': [{'question':'Format?','options':[{'label':'short'},{'label':'long'}]}]}))
    await asyncio.sleep(.01)
    assert r.status(tid)['state'] == 'waiting_for_answer'
    qid = r.status(tid)['pending_requests'][0]['request_id']
    await r.answer(tid, qid, answers={'Format?': 'short'})
    assert (await question)['answers'] == {'Format?': 'short'}
    blocked = asyncio.create_task(r.request(tid, 'Write', {'file_path': str(tmp_path/'b')}))
    await asyncio.sleep(.01)
    stale = r.status(tid)['pending_requests'][0]['request_id']
    await r.interrupt(tid)
    assert (await blocked)['allow'] is False
    with pytest.raises(ValueError):
        await r.answer(tid, stale, allow=True)
    await r.close()


@pytest.mark.asyncio
async def test_silence_and_wait_do_not_invent_failure(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=Backend)
    tid = (await r.start('work', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await ready(r, tid)
    current = r.status(tid)
    assert current['state'] == 'running'
    after = current['cursor']
    result = await r.wait(tid, after, timeout=.02)
    assert result['state'] == 'running'
    assert result['changed'] is False
    assert result['stalled'] is False
    await r.close()


@pytest.mark.asyncio
async def test_backend_failure_is_visible(tmp_path):
    from registry import Registry
    class Broken(Backend):
        async def connect(self):
            raise RuntimeError('connection failed')
    r = Registry(tmp_path/'db', backend_factory=Broken)
    tid = (await r.start('work', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await asyncio.sleep(.02)
    assert r.status(tid)['state'] == 'failed'
    assert 'connection failed' in r.result(tid)['error']
    await r.close()


@pytest.mark.asyncio
async def test_failed_followup_does_not_leave_a_running_ghost(tmp_path):
    from registry import Registry
    class FailsOnFollowup(Backend):
        async def send(self, prompt):
            if prompt == 'fail':
                raise RuntimeError('stream gone')
            await super().send(prompt)
    r = Registry(tmp_path/'db', backend_factory=FailsOnFollowup)
    tid = (await r.start('complete', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await ready(r, tid)
    with pytest.raises(RuntimeError):
        await r.followup(tid, 'fail')
    assert r.status(tid)['state'] == 'failed'
    assert 'stream gone' in r.result(tid)['error']
    await r.close()


@pytest.mark.asyncio
async def test_unexpected_stream_failure_reclaims_connection_slots(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=Backend)
    for _ in range(5):
        tid = (await r.start('work', str(tmp_path), model='claude-haiku-5-5'))['task_id']
        await ready(r, tid)
        await r.emit(tid, 'failed', {'error':'unexpected EOF'})
        await asyncio.sleep(.01)
        assert r.status(tid)['state'] == 'failed'
        assert r.status(tid)['process_connected'] is False
    await r.close()


@pytest.mark.asyncio
async def test_resume_cannot_exceed_connection_limit(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=Backend)
    saved = (await r.start('complete', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await ready(r, saved)
    await r.stop(saved)
    for _ in range(4):
        tid = (await r.start('work', str(tmp_path), model='claude-haiku-5-5'))['task_id']
        await ready(r, tid)
    with pytest.raises(ValueError, match='Four'):
        await r.resume(saved, 'work')
    assert r.status(saved)['state'] == 'completed'
    await r.close()
