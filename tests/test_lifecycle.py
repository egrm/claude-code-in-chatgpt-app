import asyncio
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

import lifecycle
from lifecycle import Lifecycle


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(**kw):
    clock = Clock()
    return Lifecycle(ttl=6, grace=10, startup_grace=30, clock=clock, **kw), clock


def test_declared_constants_bound_abrupt_loss_to_16_seconds():
    assert (lifecycle.HEARTBEAT_S, lifecycle.LEASE_TTL_S, lifecycle.IDLE_GRACE_S) == (2, 6, 10)
    assert lifecycle.LEASE_TTL_S + lifecycle.IDLE_GRACE_S == 16
    assert lifecycle.HEARTBEAT_S * 3 <= lifecycle.LEASE_TTL_S


def test_multiple_clients_graceful_release_starts_grace_after_last():
    life, clock = make()
    a, b = life.acquire('a')['lease_id'], life.acquire('b')['lease_id']
    clock.now = 1
    life.release(a)
    clock.now = 5
    life.heartbeat(b)
    life.release(b)
    clock.now = 14.9
    assert not life.idle_expired()
    clock.now = 15
    assert life.idle_expired()


def test_abrupt_client_loss_expires_ttl_plus_grace_after_last_heartbeat():
    life, clock = make()
    lease = life.acquire('a')['lease_id']
    clock.now = 2
    life.heartbeat(lease)
    clock.now = 17.9
    assert not life.idle_expired()
    clock.now = 18
    assert life.idle_expired()
    with pytest.raises(ValueError):
        life.heartbeat(lease)


def test_reconnect_within_grace_cancels_shutdown():
    life, clock = make()
    life.release(life.acquire('a')['lease_id'])
    clock.now = 9
    lease = life.acquire('b')['lease_id']
    for t in range(10, 60, 2):
        clock.now = t
        life.heartbeat(lease)
        assert not life.idle_expired()


def test_startup_grace_when_nobody_connects():
    life, clock = make()
    clock.now = 29.9
    assert not life.idle_expired()
    clock.now = 30
    assert life.idle_expired()


def test_deadline_rejects_acquire_and_heartbeat_before_watchdog_tick():
    life, clock = make()
    lease = life.acquire('a')['lease_id']
    life.release(lease)
    clock.now = 10
    with pytest.raises(ValueError, match='shutting down'):
        life.acquire('late')
    assert life.state == 'draining'


def test_work_requires_live_lease_and_requests_never_extend_deadline():
    life, clock = make()
    lease = life.acquire('a')['lease_id']
    life.validate(lease)
    with pytest.raises(ValueError, match='reload'):
        life.validate(None)
    clock.now = 5
    life.validate(lease)          # validation is not a heartbeat
    clock.now = 6
    with pytest.raises(ValueError):
        life.validate(lease)
    clock.now = 16
    assert life.idle_expired()


def test_environment_override_can_only_shorten(monkeypatch):
    monkeypatch.setenv('CCS_LIFECYCLE', '1,1,2,1')
    assert lifecycle.configured() == {'ttl': 1, 'grace': 1, 'startup_grace': 2, 'close_timeout': 1}
    for bad in ('100,1,1,1', 'nan,1,1,1', '0,1,1,1', '-1,1,1,1', 'inf,1,1,1', '1,2', 'x'):
        monkeypatch.setenv('CCS_LIFECYCLE', bad)
        assert lifecycle.configured() == {'ttl': 6, 'grace': 10, 'startup_grace': 30, 'close_timeout': 8}


# --- daemon RPC and registry drain ---

class SlowBackend:
    def __init__(self, record, emit, permission, hook):
        self.emit, self.permission = emit, permission
        self.decision = None

    async def connect(self):
        await self.emit('init', {'session_id': 's', 'model': 'claude-haiku-5-5'})

    async def send(self, prompt):
        if prompt == 'ask':
            asyncio.get_running_loop().create_task(self._ask())

    async def _ask(self):
        self.decision = await self.permission('Write', {'file_path': 'x.txt', 'content': ''})

    async def interrupt(self):
        await self.emit('result', {'result': '', 'interrupted': True})

    async def close(self):
        await asyncio.sleep(3600)   # hangs: shutdown must stay bounded


async def connected(registry, tid):
    for _ in range(200):
        if tid in registry.backends:
            return
        await asyncio.sleep(.005)
    pytest.fail('backend did not connect')


@pytest.mark.asyncio
async def test_registry_shutdown_is_bounded_concurrent_and_persists_interrupted(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=SlowBackend)
    ids = [(await r.start('ask', str(tmp_path), model='claude-haiku-5-5'))['task_id'] for _ in range(3)]
    for tid in ids:
        await connected(r, tid)
    for _ in range(100):
        if all(r.status(t)['state'] == 'waiting_for_permission' for t in ids):
            break
        await asyncio.sleep(.005)
    backend = r.backends[ids[0]]
    started = asyncio.get_running_loop().time()
    summary = await r.shutdown('All ChatGPT/MCP clients disconnected', timeout=.3)
    assert asyncio.get_running_loop().time() - started < 2
    assert summary['interrupted'] == 3
    assert backend.decision == {'allow': False, 'message': 'Task interrupted or disconnected'}
    with pytest.raises(ValueError, match='shutting down'):
        await r.start('x', str(tmp_path), model='claude-haiku-5-5')
    with pytest.raises(ValueError, match='shutting down'):
        await r.resume(ids[0], 'x')
    recovered = Registry(tmp_path/'db', backend_factory=SlowBackend)
    for tid in ids:
        status = recovered.status(tid)
        assert status['state'] == 'interrupted' and 'clients disconnected' in status['error']
        assert status['pending_requests'] == []


@pytest.mark.asyncio
async def test_approval_note_is_flagged_as_not_delivered(tmp_path):
    from registry import Registry
    r = Registry(tmp_path/'db', backend_factory=SlowBackend)
    tid = (await r.start('ask', str(tmp_path), model='claude-haiku-5-5'))['task_id']
    await connected(r, tid)
    for _ in range(100):
        if r.status(tid)['pending_requests']:
            break
        await asyncio.sleep(.005)
    request_id = r.status(tid)['pending_requests'][0]['request_id']
    result = await r.answer(tid, request_id, allow=True, message='also add tests')
    assert 'task_followup' in result['warning']
    await r.shutdown('test', timeout=.1)


def rpc_app(tmp_path, life):
    from registry import Registry
    from test_registry import Backend
    from daemon import make_app
    registry = Registry(tmp_path/'db', backend_factory=Backend)
    return make_app(registry, 'cap', life), registry


@pytest.mark.asyncio
async def test_rpc_work_requires_lease_admin_does_not_and_deadline_drains(tmp_path):
    import httpx2
    life, clock = make()
    app, registry = rpc_app(tmp_path, life)
    headers = {'Authorization': 'Bearer cap'}
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://t') as c:
        async def post(method, args=None, lease=None):
            body = {'method': method, 'args': args or {}}
            if lease:
                body['lease'] = lease
            return await c.post('/rpc', headers=headers, json=body)
        health = (await post('health')).json()['result']
        assert health['protocol'] == 2 and health['state'] == 'running' and health['clients'] == 0
        refused = await post('list')
        assert refused.status_code == 409 and 'reload' in refused.json()['error']
        lease = (await post('lease_acquire', {'client': 'test'})).json()['result']['lease_id']
        assert (await post('list', lease=lease)).json()['result'] == []
        assert (await post('admin_tasks')).json()['result'] == {'active': 0, 'total': 0}
        assert (await post('lease_heartbeat', {'lease_id': lease})).status_code == 200
        assert (await post('lease_release', {'lease_id': lease})).status_code == 200
        clock.now = 10
        late = await post('lease_acquire', {'client': 'late'})
        assert late.status_code == 503 and late.json()['draining'] is True
        assert (await post('list', lease=lease)).status_code == 503
        assert (await post('health')).json()['result']['state'] == 'draining'
        await asyncio.sleep(.05)
        assert registry.closing
