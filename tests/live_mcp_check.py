"""Opt-in real stdio MCP check; defaults to no inference, --infer enables one tiny call."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


async def connection(callback):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'server.py')], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            return await callback(session, init)


def data(result):
    assert not result.is_error, str(result)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


async def main():
    (ROOT/'collaboration').mkdir(exist_ok=True)
    saved = {}
    if not (ROOT/'.runtime'/'service-task.json').exists():
        # Development only: bootstrap outside the MCP client's kill-on-close job.
        from runtime import ensure_worker
        await asyncio.to_thread(ensure_worker)
    async def first(session, init):
        tools = await session.list_tools()
        names = [t.name for t in tools.tools]
        assert set(names) == {'task_list','task_status','task_start','task_followup','task_events','task_wait',
                              'task_request','task_answer','task_interrupt','task_resume','task_stop','task_result'}
        saved.update(protocol=init.protocol_version, tools=names)
        assert 'tasks' in data(await session.call_tool('task_list', {}))
        if '--infer' in sys.argv:
            fixture = Path(tempfile.mkdtemp(prefix='mcp-live-', dir=ROOT/'collaboration'))
            status = data(await session.call_tool('task_start', {'prompt':'Reply exactly MCP_CLAUDE_OK.',
                'workspace':str(fixture), 'model':'claude-haiku-5-5','effort':'low','tools':[], 'max_turns':1,'max_budget_usd':.03}))
            saved['task_id'] = status['task_id']
    await connection(first)
    async def second(session, init):
        tasks = data(await session.call_tool('task_list', {}))['tasks']
        saved['reconnect_registry_available'] = True
        if 'task_id' in saved:
            status = data(await session.call_tool('task_status', {'task_id':saved['task_id']}))
            while status['state'] not in ('completed','failed','interrupted'):
                status = data(await session.call_tool('task_wait', {'task_id':saved['task_id'],'after':status['cursor'],'timeout':10}))
            result = data(await session.call_tool('task_result', {'task_id':saved['task_id']}))
            assert result['state'] == 'completed', result
            assert result['result'].strip() == 'MCP_CLAUDE_OK', result
            assert result['billing']['isUsingOverage'] is False
            saved.update(state=status['state'], model=status['actual_model'], response=result['result'], isUsingOverage=False)
            await session.call_tool('task_stop', {'task_id':saved['task_id']})
    async with asyncio.timeout(100):
        await connection(second)
    (ROOT/'collaboration'/'live-mcp-evidence.json').write_text(json.dumps(saved, indent=2))
    print(json.dumps(saved))


if __name__ == '__main__':
    asyncio.run(main())
