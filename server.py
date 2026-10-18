"""STDIO MCP facade. Worker state survives facade/client disconnects."""
import asyncio
from contextlib import asynccontextmanager
import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from runtime import ensure_worker
from policy import sanitize_process_environment

_transport = None


@asynccontextmanager
async def lifespan(server):
    global _transport
    _transport = await asyncio.to_thread(ensure_worker)
    yield
    # Deliberately leave the independently owned worker and Claude sessions running.


server = MCPServer('claude_supervisor', version='0.1.0', lifespan=lifespan, instructions=(
    'Supervise external Claude tasks using task_list/status. This is separate from native agents. '
    'Start defaults to Opus 5.5/high; use explicit Haiku 5.5/low for cheap checks. '
    'Do not inject all activity: status/wait are compact; events are cursor-based on demand. '
    'If waiting_for_permission/answer, inspect task_request then answer within user authorization. '
    'Keep bounded task_wait polling active while responsible for a running task; there is no automatic chat wakeup. '
    'Followup interrupts/drains a running turn before redirecting. Stop disconnects; resume retains context.'))


async def call(method, args):
    global _transport
    if _transport is None:
        _transport = await asyncio.to_thread(ensure_worker)
    async with httpx2.AsyncClient(trust_env=False, timeout=50) as client:
        try:
            response = await client.post(f"http://127.0.0.1:{_transport['port']}/rpc",
                headers={'Authorization':'Bearer '+_transport['secret']}, json={'method':method, 'args':args})
        except httpx2.ConnectError:
            _transport = await asyncio.to_thread(ensure_worker)
            response = await client.post(f"http://127.0.0.1:{_transport['port']}/rpc",
                headers={'Authorization':'Bearer '+_transport['secret']}, json={'method':method, 'args':args})
    result = response.json()
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
    """Send context or redirect; active turn is interrupted and drained first. Same Claude session."""
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
    """Resolve one pending permission or question. answers maps exact question text to selected label/free text. Rejects stale IDs."""
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
