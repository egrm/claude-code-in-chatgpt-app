"""Explicit opt-in, bounded subscription coding test through the actual MCP path."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT/'tests'))
from live_mcp_check import connection, data


async def main():
    (ROOT/'collaboration').mkdir(exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='coding-', dir=ROOT/'collaboration'))
    (fixture/'addition.py').write_text('def add(a, b):\n    return a - b\n')
    (fixture/'check.py').write_text('from addition import add\nassert add(2, 1) == 3\nprint("FIXTURE_TEST_PASS")\n')
    evidence = {'question_approval_interrupt_resume': 'covered by separate live_sdk_check', 'fixture':str(fixture)}
    async def run(session, init):
        status = data(await session.call_tool('task_start', {
            'prompt':'Read addition.py and check.py. Correct the arithmetic implementation using Edit, then run exactly "python check.py" with Bash. Do not run any other shell command. Report the actual test exit status and changed file.',
            'workspace':str(fixture),'model':'claude-haiku-5-5','effort':'low',
            'tools':['Read','Edit','Bash'],'max_turns':10,'max_budget_usd':.12}))
        tid = status['task_id']
        edit_seen = shell_seen = False
        try:
            while status['state'] not in ('completed','failed','interrupted'):
                status = data(await session.call_tool('task_wait', {'task_id':tid,'after':status['cursor'],'timeout':10}))
                for summary in status['pending_requests']:
                    request = data(await session.call_tool('task_request', {'task_id':tid,'request_id':summary['request_id']}))
                    tool, inputs = request['tool'], request['input']
                    allow = False
                    if tool == 'Edit':
                        allow = Path(inputs['file_path']).resolve() == (fixture/'addition.py').resolve() and '+' in inputs.get('new_string','')
                        edit_seen = allow
                    elif tool == 'Bash':
                        allow = inputs.get('command','').strip() == 'python check.py'
                        shell_seen = allow
                    await session.call_tool('task_answer', {'task_id':tid,'request_id':request['request_id'],'allow':allow,'message':'Only the exact fixture edit and python check.py are authorized'})
            result = data(await session.call_tool('task_result', {'task_id':tid}))
            assert status['state'] == 'completed', result
            assert edit_seen and shell_seen
            check = subprocess.run([sys.executable,'check.py'], cwd=fixture, capture_output=True, text=True, timeout=10,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            assert check.returncode == 0, check.stderr
            events = []
            cursor = 0
            while True:
                page = data(await session.call_tool('task_events', {'task_id':tid,'after':cursor,'limit':20}))
                events += page['events']
                cursor = page['next_cursor']
                if not page['has_more']:
                    break
            shell_results = [e for e in events if e['kind']=='tool_result' and isinstance(e['data'],dict) and e['data'].get('tool_name')=='Bash']
            assert any('FIXTURE_TEST_PASS' in json.dumps(e['data']) for e in shell_results)
            assert result['billing']['isUsingOverage'] is False
            evidence.update(task_id=tid,state=result['state'],model=status['actual_model'],
                edit_approved=True,shell_approved=True,independent_test_exit_code=check.returncode,
                independent_test_stdout=check.stdout.strip(),tool_result_observed=True,isUsingOverage=False)
        finally:
            await session.call_tool('task_stop', {'task_id':tid})
    async with asyncio.timeout(120):
        await connection(run)
    (ROOT/'collaboration'/'live-coding-evidence.json').write_text(json.dumps(evidence,indent=2))
    print(json.dumps(evidence))


if __name__ == '__main__':
    asyncio.run(main())
