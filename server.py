"""STDIO MCP facade. Holds a heartbeat lease on the shared worker while this client is connected;
when the last client's lease ends the worker interrupts its tasks and exits (see lifecycle.py)."""
import asyncio
from contextlib import asynccontextmanager, suppress
import os
import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from lifecycle import HEARTBEAT_S
from runtime import ensure_worker
from policy import sanitize_process_environment

UPGRADE = ('The running supervisor worker predates client leases. Finish or stop its active tasks, '
           'then run: python setup.py install (it upgrades an idle older worker)')
_transport = None
_lease = None
_heartbeat_s = HEARTBEAT_S
_connecting = None


async def _post(method, args, timeout=50, lease=None):
    body = {'method': method, 'args': args}
    if lease:
        body['lease'] = lease
    async with httpx2.AsyncClient(trust_env=False, timeout=timeout) as client:
        response = await client.post(f"http://127.0.0.1:{_transport['port']}/rpc",
            headers={'Authorization': 'Bearer '+_transport['secret']}, json=body)
    return response.status_code, response.json()


async def _connect():
    """(Re)start or reuse the worker and acquire a fresh client lease."""
    global _transport, _lease, _heartbeat_s, _connecting
    if _connecting is None:
        _connecting = asyncio.Lock()
    async with _connecting:
        _lease = None
        _transport = await asyncio.to_thread(ensure_worker)
        code, body = await _post('lease_acquire', {'client': f'mcp-facade pid {os.getpid()}'}, timeout=10)
        if code == 400:
            raise ValueError(UPGRADE)
        if code != 200:
            raise ValueError(body.get('error', f'Worker lease refused ({code})'))
        _lease = body['result']['lease_id']
        _heartbeat_s = body['result'].get('heartbeat_s', HEARTBEAT_S)


async def _heartbeat():
    while True:
        await asyncio.sleep(_heartbeat_s)
        try:
            code, _ = await _post('lease_heartbeat', {'lease_id': _lease}, timeout=5)
            if code != 200:
                await _connect()
        except (httpx2.HTTPError, ValueError, OSError, TypeError):
            with suppress(Exception):
                await _connect()


@asynccontextmanager
async def lifespan(server):
    await _connect()
    beat = asyncio.create_task(_heartbeat())
    try:
        yield
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await beat
        if _lease:
            with suppress(Exception):
                await _post('lease_release', {'lease_id': _lease}, timeout=3)


server = MCPServer('claude_supervisor', version='0.1.0', lifespan=lifespan, instructions=(
    'Supervise external Claude tasks using task_list/status. This is separate from native agents. '
    'Start defaults to Opus 5.5/high; use explicit Haiku 5.5/low for cheap checks. '
    'Do not inject all activity: status/wait are compact; events are cursor-based on demand. '
    'If waiting_for_permission/answer, inspect task_request then answer within user authorization. '
    'Keep bounded task_wait polling active while responsible for a running task; there is no automatic chat wakeup. '
    'Followup interrupts/drains a running turn before redirecting. Stop disconnects; resume retains context.'))


async def call(method, args):
    if _transport is None:
        await _connect()
    for attempt in (0, 1):
        try:
            code, result = await _post(method, args, lease=_lease)
        except httpx2.ConnectError:
            if attempt:
                raise
            await _connect()
            continue
        # Draining worker or lapsed lease: reconnect once (a drained worker's tasks need explicit resume).
        if not attempt and (result.get('draining') or result.get('lease_required')):
            await _connect()
            continue
        break
    if 'error' in result:
        raise ValueError(result['error'])
    return result['result']


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


@server.tool(annotations=READ)
async def task_list(state: str | None = None, limit: int = 20) -> dict:
    """List the external Claude roster in compact form; native agents are separate."""
    return {'tasks': await call('list', {'state':state,'limit':limit})}


@server.tool(annotations=READ)
async def task_status(task_id: str) -> dict:
    """Compact lifecycle, activity age and pending IDs; silence alone does not mean stalled."""
    return await call('status', {'task_id':task_id})


@server.tool(annotations=WRITE)
async def task_start(prompt: str, workspace: str, model: str = 'claude-opus-5-5', effort: str = 'high',
                     tools: list[str] | None = None, max_turns: int = 30, max_budget_usd: float = 1.0) -> dict:
    """Start a persistent Max-subscription task. Explicit workspace; mutations require scoped approvals. Budget is a list-price estimate cap, not permission to spend API credits."""
    return await call('start', locals())


@server.tool(annotations=WRITE)
async def task_followup(task_id: str, text: str) -> dict:
    """Give Claude NEW instructions/context or redirect it (the only way to steer). Active turn is interrupted and drained first; same session."""
    return await call('followup', {'tid':task_id,'text':text})


@server.tool(annotations=READ)
async def task_events(task_id: str, after: int = 0, limit: int = 10) -> dict:
    """Inspect bounded recent activity on demand. Pass returned next_cursor; gap means old events expired. Max 20 events."""
    if not 1 <= limit <= 20:
        raise ValueError('limit must be 1..20')
    return await call('events', {'tid':task_id,'after':after,'limit':limit})


@server.tool(annotations=READ)
async def task_wait(task_id: str, after: int = 0, timeout: float = 15) -> dict:
    """Wait at most 20s for a status/activity change. Returns compact status, never the full stream. Poll while supervising."""
    return await call('wait', {'tid':task_id,'after':after,'timeout':timeout})


@server.tool(annotations=READ)
async def task_request(task_id: str, request_id: str) -> dict:
    """Inspect exact pending tool input/question before answering its scoped request ID."""
    return await call('request_detail', {'tid':task_id,'request_id':request_id})


@server.tool(annotations=WRITE)
async def task_answer(task_id: str, request_id: str, allow: bool = False,
                      answers: dict[str,str] | None = None, message: str = '') -> dict:
    """Resolve ONE specific pending permission/question by request_id; not for new instructions (use task_followup). answers maps exact question text to label/free text. message reaches Claude only as a denial reason; with allow=true it is not delivered. Rejects stale IDs."""
    return await call('answer', {'tid':task_id,'request_id':request_id,'allow':allow,'answers':answers,'message':message})


@server.tool(annotations=WRITE)
async def task_interrupt(task_id: str) -> dict:
    """Interrupt and drain current turn, cancel pending approvals; retain session for followup/resume."""
    return await call('interrupt', {'tid':task_id})


@server.tool(annotations=WRITE)
async def task_resume(task_id: str, prompt: str) -> dict:
    """Reconnect using the exact saved session ID or continue its current process; no new-model substitution."""
    return await call('resume', {'tid':task_id,'prompt':prompt})


@server.tool(annotations=WRITE)
async def task_stop(task_id: str) -> dict:
    """Disconnect this Claude process, keeping durable task/session state for later resume."""
    return await call('stop', {'tid':task_id})


@server.tool(annotations=READ)
async def task_result(task_id: str) -> dict:
    """Read final result/error and subscription evidence; verify code/tests independently in the workspace."""
    return await call('result', {'tid':task_id})


if __name__ == '__main__':
    sanitize_process_environment()
    import sys
    if '--shutdown-worker' in sys.argv:
        from runtime import identity, healthy
        info = identity()
        if healthy(info):
            httpx2.post(f"http://127.0.0.1:{info['port']}/rpc", headers={'Authorization':'Bearer '+info['secret']},
                        json={'method':'shutdown','args':{}}, timeout=5, trust_env=False)
            print('Worker shutdown requested')
    else:
        server.run()
