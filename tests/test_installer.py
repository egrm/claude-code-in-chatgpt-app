import sys
from pathlib import Path
import tomllib
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

import installer
import registration


class FakeService:
    kind = 'fake'

    def __init__(self, ownership='owned'):
        self.ownership, self.calls = ownership, []

    def inspect(self):
        return {'exists': self.ownership != 'absent', 'ownership': self.ownership, 'state': 'ready'}

    def register(self):
        self.calls.append('register')
        return 'unchanged'

    def remove(self):
        self.calls.append('remove')
        if self.ownership == 'foreign':
            raise ValueError('Launcher belongs to another installation; left unchanged')
        return 'removed'

    def record(self):
        return {'kind': 'fake', 'root': 'x'}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    root = tmp_path/'proj'
    (root/'.runtime').mkdir(parents=True)
    context = installer.Context(root=root, config=tmp_path/'codex'/'config.toml')
    service = FakeService()
    monkeypatch.setattr(installer, 'service_for', lambda c: service)
    monkeypatch.setattr(installer, 'worker_rpc', lambda c, method, args=None, **kw: None)
    context.service = service
    return context


def good_doctor(monkeypatch):
    monkeypatch.setattr(installer, 'venv_report', lambda c: (True, 'Python 3.14.4; pinned dependencies match'))
    monkeypatch.setattr(installer, 'find_claude', lambda c: (Path('/claude'), []))
    monkeypatch.setattr(installer, 'claude_version', lambda c, cli: '2.1.302 (Claude Code)')
    monkeypatch.setattr(installer, 'auth_fields', lambda c, cli: {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'})
    monkeypatch.setattr(installer, 'mcp_discovery', lambda c: (True, '12 tools discovered'))
    monkeypatch.setattr(installer.ps, 'git_bash', lambda: Path('/git/bin/bash'))


def configure(ctx):
    ctx.config.parent.mkdir(parents=True, exist_ok=True)
    ctx.config.write_text(registration.configure_text('', ctx.root))


def test_version_gate_runs_before_imports():
    import setup
    assert setup.version_error((3, 10, 12))
    assert setup.version_error((3, 11, 0)) is None


def test_doctor_all_ok(ctx, monkeypatch, capsys):
    good_doctor(monkeypatch)
    configure(ctx)
    assert installer.doctor(ctx) == 0
    out = capsys.readouterr().out
    assert 'FAIL' not in out and '12 tools' in out


def test_doctor_missing_venv_fails_and_skips_discovery(ctx, monkeypatch, capsys):
    good_doctor(monkeypatch)
    monkeypatch.setattr(installer, 'venv_report', lambda c: (False, 'missing; run setup.py install'))
    monkeypatch.setattr(installer, 'mcp_discovery', lambda c: pytest.fail('must not probe without venv'))
    configure(ctx)
    assert installer.doctor(ctx) == 1
    assert 'FAIL' in capsys.readouterr().out


def test_doctor_shows_only_nonsecret_auth_fields_and_rejects_non_max(ctx, monkeypatch, capsys):
    good_doctor(monkeypatch)
    monkeypatch.setattr(installer, 'auth_fields', lambda c, cli: {'loggedIn': True, 'authMethod': 'console',
        'apiProvider': 'firstParty', 'subscriptionType': 'pro', 'email': 'person@example.com', 'orgId': 'org-secret'})
    configure(ctx)
    assert installer.doctor(ctx) == 1
    out = capsys.readouterr().out
    assert 'person@example.com' not in out and 'org-secret' not in out
    assert 'subscriptionType=pro' in out


def test_doctor_reports_foreign_registration_and_missing_cli(ctx, monkeypatch, capsys):
    good_doctor(monkeypatch)
    monkeypatch.setattr(installer, 'find_claude', lambda c: (None, ['/npm/claude: npm/script shim, not the native executable']))
    ctx.config.parent.mkdir(parents=True)
    ctx.config.write_text('[mcp_servers.claude_supervisor]\ncommand = "x"\nargs = []\n')
    assert installer.doctor(ctx) == 1
    out = capsys.readouterr().out
    assert 'another installation' in out and 'shim' in out


def test_remove_refuses_with_active_tasks(ctx, monkeypatch):
    configure(ctx)
    before = ctx.config.read_bytes()
    monkeypatch.setattr(installer, 'worker_rpc', lambda c, method, args=None, **kw:
        {'active': 1, 'total': 3} if method == 'admin_tasks' else pytest.fail('no shutdown'))
    assert installer.remove(ctx) == 1
    assert ctx.config.read_bytes() == before
    assert ctx.service.calls == []


def test_remove_owned_entry_and_service_only(ctx, monkeypatch):
    ctx.config.parent.mkdir(parents=True)
    original = '# mine\n[mcp_servers.other]\ncommand = "o"\n'
    ctx.config.write_text(registration.configure_text(original, ctx.root))
    (ctx.root/'.venv').mkdir()
    (ctx.root/'.runtime'/'tasks.sqlite').write_text('db')
    (ctx.root/'.runtime'/'service.json').write_text('{"kind": "fake", "root": "x"}')
    calls = []
    def rpc(c, method, args=None, **kw):
        calls.append(method)
        return {'active': 0, 'total': 1} if method == 'admin_tasks' else {'stopping': True}
    monkeypatch.setattr(installer, 'worker_rpc', rpc)
    monkeypatch.setattr(installer, 'wait_worker_stopped', lambda c, timeout=15: True)
    assert installer.remove(ctx) == 0
    assert ctx.config.read_text() == original
    assert calls == ['admin_tasks', 'shutdown']
    assert ctx.service.calls == ['remove']
    assert (ctx.root/'.venv').exists() and (ctx.root/'.runtime'/'tasks.sqlite').exists()
    assert not (ctx.root/'.runtime'/'service.json').exists()


def test_remove_aborts_before_shutdown_when_config_update_fails(ctx, monkeypatch):
    configure(ctx)
    monkeypatch.setattr(installer, 'worker_rpc', lambda c, method, args=None, **kw:
        {'active': 0, 'total': 0} if method == 'admin_tasks' else pytest.fail('must not shut down'))
    def racing(*a, **k):
        raise ValueError('Host configuration changed during update; nothing written, rerun the command')
    monkeypatch.setattr(installer.registration, 'apply', racing)
    assert installer.remove(ctx) == 1
    assert ctx.service.calls == []


def test_doctor_fails_disabled_entry(ctx, monkeypatch, capsys):
    good_doctor(monkeypatch)
    ctx.config.parent.mkdir(parents=True)
    ctx.config.write_text(registration.configure_text('', ctx.root).replace('tool_timeout_sec = 60', 'tool_timeout_sec = 60\nenabled = false'))
    assert installer.doctor(ctx) == 1
    assert 'disabled' in capsys.readouterr().out


def test_upgrade_never_stops_older_worker_with_active_or_unknown_tasks(ctx, monkeypatch):
    monkeypatch.setattr(installer, 'worker_healthy', lambda c: {'service': 'claude-code-supervisor-v1'})
    def legacy(c, method, args=None, **kw):
        if method == 'admin_tasks':
            raise installer.SetupError('Worker refused admin_tasks: Unknown supervisor operation')
        if method == 'list':
            return [{'state': args['state']}] if args['state'] == 'waiting_for_permission' else []
        pytest.fail('must not shut down')
    monkeypatch.setattr(installer, 'worker_rpc', legacy)
    assert installer.upgrade_worker(ctx) is False


def test_remove_leaves_foreign_entry_and_service(ctx, capsys):
    ctx.config.parent.mkdir(parents=True)
    foreign = '[mcp_servers.claude_supervisor]\ncommand = "x"\nargs = ["y"]\n'
    ctx.config.write_text(foreign)
    ctx.service.ownership = 'foreign'
    (ctx.root/'.runtime'/'service.json').write_text('{}')
    assert installer.remove(ctx) == 1
    assert ctx.config.read_text() == foreign
    assert (ctx.root/'.runtime'/'service.json').exists()
    assert 'another installation' in capsys.readouterr().out


def test_install_stops_before_launcher_when_native_claude_missing(ctx, monkeypatch):
    monkeypatch.setattr(installer, 'ensure_venv', lambda c: None)
    monkeypatch.setattr(installer, 'install_requirements', lambda c, dev=False: None)
    monkeypatch.setattr(installer, 'venv_run', lambda c, code, **kw: None)
    monkeypatch.setattr(installer, 'find_claude', lambda c: (None, []))
    assert installer.install(ctx) == 2
    assert ctx.service.calls == []
    assert not ctx.config.exists()


def test_install_happy_path_registers_and_configures(ctx, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(installer, 'ensure_venv', lambda c: ran.append('venv'))
    monkeypatch.setattr(installer, 'install_requirements', lambda c, dev=False: ran.append('deps'))
    monkeypatch.setattr(installer, 'venv_run', lambda c, code, **kw: ran.append(code))
    monkeypatch.setattr(installer, 'find_claude', lambda c: (Path('/native/claude'), []))
    monkeypatch.setattr(installer, 'auth_fields', lambda c, cli: {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'})
    monkeypatch.setattr(installer, 'mcp_discovery', lambda c: (True, '12 tools discovered'))
    monkeypatch.setattr(installer.ps, 'git_bash', lambda: Path('/git/bin/bash'))
    assert installer.install(ctx) == 0
    assert ctx.service.calls == ['register']
    assert tomllib.loads(ctx.config.read_text())['mcp_servers']['claude_supervisor']['args'] == [str(ctx.root/'server.py')]
    assert any('ensure_worker' in str(step) for step in ran)
    out = capsys.readouterr().out
    assert 'task_list' in out and '/mcp' in out
