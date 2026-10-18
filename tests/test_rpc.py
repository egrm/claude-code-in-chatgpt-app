import asyncio
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

HEADERS = {'Authorization': 'Bearer local-test-capability'}


async def lease(c):
    return (await c.post('/rpc', headers=HEADERS, json={'method':'lease_acquire','args':{'client':'test'}})).json()['result']['lease_id']


@pytest.mark.asyncio
async def test_rpc_refuses_unauthenticated_and_unknown_methods(tmp_path):
    import httpx2
    from registry import Registry
    from test_registry import Backend
    from daemon import make_app
    r = Registry(tmp_path/'db', backend_factory=Backend)
    app = make_app(r, 'local-test-capability')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://test') as c:
        assert (await c.post('/rpc', json={'method':'list','args':{}})).status_code == 401
        held = await lease(c)
        assert (await c.post('/rpc', headers=HEADERS, json={'method':'__dict__','args':{},'lease':held})).status_code == 400
        response = await c.post('/rpc', headers=HEADERS, json={'method':'list','args':{},'lease':held})
        assert response.json()['result'] == []
    await r.close()


@pytest.mark.asyncio
async def test_rpc_poll_returns_compact_status_without_event_payloads(tmp_path):
    import httpx2
    from registry import Registry
    from test_registry import Backend
    from daemon import make_app
    r = Registry(tmp_path/'db', backend_factory=Backend)
    app = make_app(r, 'local-test-capability')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://test') as c:
        held = await lease(c)
        response = await c.post('/rpc', headers=HEADERS, json={'method':'start','lease':held,'args':{'prompt':'complete','workspace':str(tmp_path),'model':'claude-haiku-5-5'}})
        tid = response.json()['result']['task_id']
        await asyncio.sleep(.02)
        response = await c.post('/rpc', headers=HEADERS, json={'method':'status','lease':held,'args':{'task_id':tid}})
        result = response.json()['result']
        assert result['state'] == 'completed'
        assert 'events' not in result and 'result' not in result
    await r.close()
