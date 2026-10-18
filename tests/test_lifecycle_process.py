"""Real worker/facade processes on an isolated copied checkout (never this checkout's .runtime). No inference."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import secrets
import subprocess
import sys
import time
import pytest
import httpx2

SOURCE = Path(__file__).parents[1]
FAST = '1,1,3,1'   # ttl, grace, startup grace, close timeout (may only shorten defaults)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path/'checkout'
    root.mkdir()
    for path in SOURCE.glob('*.py'):
        if path.name != 'setup.py':
            shutil.copy2(path, root/path.name)
    (root/'.runtime').mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    info = {'port': port, 'secret': secrets.token_urlsafe(16)}
    (root/'.runtime'/'transport.json').write_text(json.dumps(info))
    return root, info


def rpc(info, method, args=None, lease=None, timeout=2):
    body = {'method': method, 'args': args or {}}
    if lease:
        body['lease'] = lease
    return httpx2.post(f"http://127.0.0.1:{info['port']}/rpc", headers={'Authorization': 'Bearer ' + info['secret']},
                       json=body, timeout=timeout, trust_env=False)


def health(info):
    try:
        return rpc(info, 'health', timeout=1).json()['result']
    except Exception:
        return None


def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.1)
    return False


def worker(root):
    env = {**os.environ, 'CCS_LIFECYCLE': FAST}
    return subprocess.Popen([sys.executable, str(root/'daemon.py')], cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                            start_new_session=os.name != 'nt')


def test_worker_exits_after_abrupt_client_loss_and_without_any_client(checkout):
    root, info = checkout
    process = worker(root)
    try:
        assert wait_for(lambda: health(info), 20), 'worker did not start'
        lease = rpc(info, 'lease_acquire', {'client': 'fixture'}).json()['result']['lease_id']
        for _ in range(4):
            time.sleep(.4)
            assert rpc(info, 'lease_heartbeat', {'lease_id': lease}).status_code == 200
        stopped_heartbeat = time.monotonic()
        process.wait(timeout=15)
        assert time.monotonic() - stopped_heartbeat >= 1.5   # ttl + grace, not immediately
    finally:
        if process.poll() is None:
            process.kill()
    second = worker(root)
    try:
        assert wait_for(lambda: health(info), 20)
        second.wait(timeout=15)   # startup grace with nobody connecting
    finally:
        if second.poll() is None:
            second.kill()


def test_facade_holds_lease_and_graceful_close_stops_owned_worker(checkout):
    root, info = checkout
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client, get_default_environment
    seen = {}

    async def session():
        params = StdioServerParameters(command=sys.executable, args=[str(root/'server.py')], cwd=str(root),
                                       env={**get_default_environment(), 'CCS_LIFECYCLE': FAST})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.call_tool('task_list', {})
                assert not listed.isError, listed
                await asyncio.sleep(3)    # beyond ttl+grace: heartbeats must keep the worker alive
                seen['health'] = health(info)
    asyncio.run(asyncio.wait_for(session(), 60))
    assert seen['health']['clients'] >= 1 and seen['health']['protocol'] == 2
    assert wait_for(lambda: health(info) is None, 15), 'owned worker kept running after last client left'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group cleanup (Windows uses the kill-on-close job)')
def test_posix_group_cleanup_kills_sigterm_ignoring_child(tmp_path):
    program = '''import subprocess, sys
sys.path.insert(0, sys.argv[1])
import daemon
child = subprocess.Popen([sys.executable, '-c', "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
child.stdout.readline()
print(child.pid, flush=True)
print(daemon.posix_group_cleanup(term_grace=.5), flush=True)
'''
    result = subprocess.run([sys.executable, '-c', program, str(SOURCE)], capture_output=True, text=True,
                            timeout=30, start_new_session=True)
    pid, cleaned = result.stdout.split()[:2]
    assert cleaned == 'True', result.stderr
    assert wait_for(lambda: not _alive(int(pid)), 5)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split()[2] != 'Z'
    except OSError:
        return True
