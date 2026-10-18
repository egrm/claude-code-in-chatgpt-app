import ast
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def test_auth_status_returns_only_routing_fields_from_clean_environment(monkeypatch):
    import policy
    seen = {}
    def fake_run(command, **kwargs):
        seen.update(command=command, env=kwargs['env'])
        payload = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max',
                   'email': 'person@example.com', 'orgId': 'secret-org'}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    monkeypatch.setattr(policy.subprocess, 'run', fake_run)
    assert policy.preflight_auth('claude') == {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}
    assert 'ANTHROPIC_API_KEY' not in seen['env']
    assert seen['command'][-3:] == ['auth', 'status', '--json'] and '--safe-mode' in seen['command']


def test_failed_auth_status_is_never_accepted(monkeypatch):
    import policy
    payload = {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'}
    monkeypatch.setattr(policy.subprocess, 'run', lambda c, **k: subprocess.CompletedProcess(c, 1, json.dumps(payload), ''))
    with pytest.raises(ValueError, match='unavailable'):
        policy.preflight_auth('claude')
    monkeypatch.setattr(policy.subprocess, 'run', lambda c, **k: subprocess.CompletedProcess(c, 1, '{"loggedIn": false}', ''))
    assert policy.auth_status('claude')['loggedIn'] is False
    with pytest.raises(ValueError, match='Max'):
        policy.preflight_auth('claude')


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    import runtime
    monkeypatch.setattr(runtime, 'RUNTIME', tmp_path)
    monkeypatch.setattr(runtime, 'private_runtime', lambda: tmp_path)
    states = iter([False, True])
    monkeypatch.setattr(runtime, 'healthy', lambda info: next(states))
    return runtime, tmp_path


def test_ensure_worker_starts_legacy_windows_task_record(isolated_runtime, monkeypatch):
    runtime, folder = isolated_runtime
    import services
    started = []
    class Task:
        def start(self):
            started.append('task')
    monkeypatch.setattr(services, 'from_record', lambda record, root: started.append(record) or Task())
    monkeypatch.setattr(services, 'spawn_worker', lambda *a: pytest.fail('must use registered launcher'))
    (folder/'service-task.json').write_text(json.dumps({'name': 'ClaudeCodeSupervisor-S-1', 'root': str(runtime.ROOT)}))
    info = runtime.ensure_worker()
    assert set(info) == {'port', 'secret'}
    assert started == [{'kind': 'windows-task', 'name': 'ClaudeCodeSupervisor-S-1', 'root': str(runtime.ROOT)}, 'task']
    if os.name != 'nt':
        assert (folder/'transport.json').stat().st_mode & 0o777 == 0o600


def test_ensure_worker_without_record_uses_development_spawn(isolated_runtime, monkeypatch):
    runtime, folder = isolated_runtime
    import services
    class Process:
        def poll(self):
            return None
    monkeypatch.setattr(services, 'spawn_worker', lambda root, log: Process())
    assert runtime.ensure_worker()['port']


def test_posix_group_cleanup_only_for_group_leader(monkeypatch):
    import daemon
    if os.name == 'nt':
        assert daemon.posix_group_cleanup() is False
        return
    killed = []
    monkeypatch.setattr(daemon.os, 'getpgrp', lambda: 4242)
    monkeypatch.setattr(daemon.os, 'getpid', lambda: 1)
    monkeypatch.setattr(daemon.os, 'killpg', lambda *a: killed.append(a))
    assert daemon.posix_group_cleanup() is False and killed == []


def test_sources_parse_as_python_3_11_and_bootstrap_older():
    for path in ROOT.glob('*.py'):
        ast.parse(path.read_text(encoding='utf-8'), str(path), feature_version=(3, 11))
    ast.parse((ROOT/'setup.py').read_text(encoding='utf-8'), 'setup.py', feature_version=(3, 7))


def test_setup_cli_rejects_old_python_message():
    result = subprocess.run([sys.executable, '-c', 'import sys; sys.version_info=(3,10,0); import runpy; runpy.run_path("setup.py", run_name="__main__")'],
                            cwd=ROOT, capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 1 and '3.11+' in result.stderr
