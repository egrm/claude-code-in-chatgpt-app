"""Explicit opt-in subscription integration check; never collected by pytest."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parents[1]))
from registry import Registry
from policy import sanitize_process_environment


async def main():
    sanitize_process_environment()
    root = Path(__file__).parents[1]
    (root/'collaboration').mkdir(exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix='live-', dir=root/'collaboration'))
    registry = Registry(fixture/'registry.sqlite')
    evidence = []
    try:
        async with asyncio.timeout(180):
            status = await registry.start(
                'First use AskUserQuestion to ask exactly "What word should I write?" with options ALPHA and BETA. '
                'After the answer, use Write to create answer.txt in the current workspace containing only the selected word. '
                'Then reply DONE. Do not use shell commands.',
                str(fixture), model='claude-haiku-5-5', effort='low',
                tools=['Read', 'Write', 'AskUserQuestion'], max_turns=8, max_budget_usd=.08)
            tid = status['task_id']
            question_seen = write_seen = False
            while True:
                status = await registry.wait(tid, status['cursor'], 10)
                if status['state'] in ('waiting_for_answer', 'waiting_for_permission'):
                    for summary in status['pending_requests']:
                        request = registry.request_detail(tid, summary['request_id'])
                        if request['tool'] == 'AskUserQuestion':
                            question_seen = True
                            answers = {q['question']: 'ALPHA' for q in request['input']['questions']}
                            await registry.answer(tid, request['request_id'], answers=answers)
                        elif request['tool'] == 'Write':
                            write_seen = True
                            assert not (fixture/'answer.txt').exists(), 'Write occurred before approval'
                            await registry.answer(tid, request['request_id'], allow=True)
                if status['state'] in ('completed', 'failed', 'interrupted'):
                    break
            assert status['state'] == 'completed', registry.result(tid)
            assert question_seen and write_seen
            assert (fixture/'answer.txt').read_text().strip() == 'ALPHA'
            session_id = registry.status(tid)['session_id']
            evidence.append({'stage': 'question_and_scoped_write', 'state': 'completed', 'session_id': session_id, 'billing': registry.result(tid)['billing']})
            status = await registry.followup(tid, 'Use Write to create blocked.txt containing BLOCKED. Do not use another tool.')
            while status['state'] != 'waiting_for_permission':
                assert status['state'] not in ('failed', 'completed'), registry.result(tid)
                status = await registry.wait(tid, status['cursor'], 10)
            stale = status['pending_requests'][0]['request_id']
            await registry.interrupt(tid)
            assert registry.status(tid)['state'] == 'interrupted'
            assert not (fixture/'blocked.txt').exists()
            try:
                await registry.answer(tid, stale, allow=True)
                raise AssertionError('stale request accepted')
            except ValueError:
                pass
            evidence.append({'stage': 'interrupt_pending_write', 'state': registry.status(tid)['state'], 'file_absent': True})
            await registry.stop(tid)
            status = await registry.resume(tid, 'Do not write anything. Read answer.txt and reply with its word and the exact question you previously asked. No questions.')
            while status['state'] not in ('completed', 'failed'):
                status = await registry.wait(tid, status['cursor'], 10)
            result = registry.result(tid)
            assert status['state'] == 'completed', result
            assert 'ALPHA' in result['result']
            assert 'What word should I write?' in result['result']
            assert result['session_id'] == session_id
            evidence.append({'stage': 'disconnect_and_resume', 'state': 'completed', 'same_session_id': True, 'context_retained': True})
            kinds = {e['kind'] for e in registry.events(tid, limit=100)['events']}
            assert 'tool_result' in kinds
            evidence.append({'stage': 'tool_observability', 'kinds': sorted(kinds)})
    finally:
        await registry.close()
        (root/'collaboration'/'live-sdk-evidence.json').write_text(json.dumps({'fixture': str(fixture), 'evidence': evidence}, indent=2))
        print(json.dumps({'fixture': str(fixture), 'evidence': evidence}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
