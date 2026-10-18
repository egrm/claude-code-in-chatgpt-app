import os
import plistlib
import subprocess
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

import services


class Runner:
    def __init__(self, results=None):
        self.calls, self.results = [], results or {}

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        key = ' '.join(map(str, command))
        for prefix, (code, out) in self.results.items():
            if key.startswith(prefix):
                return subprocess.CompletedProcess(command, code, out, '')
        return subprocess.CompletedProcess(command, 0, '', '')


def windows_task(tmp_path, existing):
    ops = []
    task = services.WindowsTask(tmp_path, run=Runner(), sid='S-1-5-21-1')
    def fake(op, **values):
        ops.append((op, values))
        if op == 'query':
            return existing
        return {}
    task._powershell = fake
    return task, ops


def action(root, exe='pythonw.exe'):
    return {'exists': True, 'execute': str(root/'.venv'/'Scripts'/exe), 'arguments': '"' + str(root/'daemon.py') + '"',
            'working_directory': str(root), 'state': 'Running'}


def test_windows_owned_pythonw_task_is_never_modified(tmp_path):
    task, ops = windows_task(tmp_path, action(tmp_path))
    assert task.name == 'ClaudeCodeSupervisor-S-1-5-21-1'
    assert task.register() == 'unchanged'
    assert [op for op, _ in ops] == ['query']
    assert task.record() == {'kind': 'windows-task', 'name': task.name, 'root': str(tmp_path)}


def test_windows_legacy_python_exe_task_is_updated_only_action(tmp_path):
    task, ops = windows_task(tmp_path, action(tmp_path, 'python.exe'))
    assert task.register() == 'updated'
    assert [op for op, _ in ops] == ['query', 'update']
    assert ops[1][1]['CCS_WORKER'] == str(tmp_path/'.venv'/'Scripts'/'pythonw.exe')


def test_windows_absent_task_created_with_on_demand_definition(tmp_path):
    task, ops = windows_task(tmp_path, {'exists': False})
    assert task.register() == 'created'
    op, values = ops[1]
    assert op == 'create'
    assert values['CCS_ARGS'] == '"' + str(tmp_path/'daemon.py') + '"' and values['CCS_SID'] == 'S-1-5-21-1'
    script = services.WINDOWS_SCRIPTS['create']
    assert 'LogonType Interactive' in script and 'MultipleInstances IgnoreNew' in script and 'Trigger' not in script


def test_windows_foreign_task_is_refused_and_not_removed(tmp_path):
    other = tmp_path/'other'
    task, ops = windows_task(tmp_path, action(other))
    with pytest.raises(ValueError, match='another installation'):
        task.register()
    with pytest.raises(ValueError, match='another installation'):
        task.remove()
    assert [op for op, _ in ops] == ['query', 'query']


def test_windows_start_uses_schtasks_run_and_reports_status(tmp_path):
    runner = Runner({str(Path(os.environ.get('SystemRoot', 'C:/Windows'))/'System32'/'schtasks.exe'): (1, 'ERROR: denied')})
    task = services.WindowsTask(tmp_path, run=runner, sid='S-1')
    with pytest.raises(RuntimeError, match='exit 1'):
        task.start()
    assert runner.calls[0][1:] == ['/Run', '/TN', 'ClaudeCodeSupervisor-S-1']


def launch_agent(tmp_path, results=None):
    runner = Runner(results)
    agent = services.LaunchAgent(tmp_path/'proj', run=runner, home=tmp_path/'home', uid=501, path_env='/usr/bin:/bin')
    return agent, runner


def test_launchd_agent_is_on_demand_and_owned(tmp_path):
    agent, runner = launch_agent(tmp_path, {'launchctl print': (113, '')})
    assert agent.register() == 'created'
    data = plistlib.loads(agent.plist_path.read_bytes())
    assert data['ProgramArguments'] == [str(tmp_path/'proj'/'.venv'/'bin'/'python'), str(tmp_path/'proj'/'daemon.py')]
    assert data['RunAtLoad'] is False and 'KeepAlive' not in data and 'StartInterval' not in data
    assert data['EnvironmentVariables'] == {'PATH': '/usr/bin:/bin'}
    assert ['launchctl', 'bootstrap', 'gui/501', str(agent.plist_path)] in runner.calls
    runner.results = {}
    assert agent.register() == 'unchanged'


def test_launchd_refuses_foreign_plist(tmp_path):
    agent, _ = launch_agent(tmp_path)
    agent.plist_path.parent.mkdir(parents=True)
    agent.plist_path.write_bytes(plistlib.dumps({'Label': agent.label, 'ProgramArguments': ['/x/python', '/x/daemon.py'], 'WorkingDirectory': '/x'}))
    with pytest.raises(ValueError, match='another installation'):
        agent.register()
    with pytest.raises(ValueError, match='another installation'):
        agent.remove()
    assert agent.plist_path.exists()


def test_launchd_start_bootstraps_when_not_loaded(tmp_path):
    agent, runner = launch_agent(tmp_path, {'launchctl print': (113, '')})
    agent.register()
    runner.calls.clear()
    agent.start()
    assert runner.calls[-1] == ['launchctl', 'kickstart', 'gui/501/' + agent.label]
    assert ['launchctl', 'bootstrap', 'gui/501', str(agent.plist_path)] in runner.calls


def systemd(tmp_path, results=None):
    runner = Runner(results)
    unit = services.SystemdUserUnit(tmp_path/'my proj', run=runner, config_home=tmp_path/'cfg', path_env='/usr/bin:/a%b')
    return unit, runner


def test_systemd_unit_has_no_install_section_and_quotes(tmp_path):
    unit, runner = systemd(tmp_path)
    assert unit.register() == 'created'
    text = unit.unit_path.read_text()
    assert '[Install]' not in text and 'Restart=no' in text
    assert 'ExecStart="' + str(tmp_path/'my proj'/'.venv'/'bin'/'python').replace('\\', '\\\\') in text
    assert 'a%%b' in text
    assert ['systemctl', '--user', 'daemon-reload'] in runner.calls
    assert unit.register() == 'unchanged'
    unit.start()
    assert runner.calls[-1] == ['systemctl', '--user', 'start', unit.unit]


def test_systemd_foreign_unit_refused(tmp_path):
    unit, _ = systemd(tmp_path)
    unit.unit_path.parent.mkdir(parents=True)
    unit.unit_path.write_text('[Service]\nExecStart=/somewhere/else\n')
    with pytest.raises(ValueError, match='another installation'):
        unit.register()
    with pytest.raises(ValueError, match='another installation'):
        unit.remove()
    assert unit.unit_path.read_text() == '[Service]\nExecStart=/somewhere/else\n'


def test_systemd_availability_and_detached_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(services, 'WINDOWS', False)
    monkeypatch.setattr(services, 'MACOS', False)
    monkeypatch.setattr(services.shutil, 'which', lambda name: '/usr/bin/systemctl')
    offline = Runner({'systemctl --user is-system-running': (1, 'offline\n')})
    assert isinstance(services.for_platform(tmp_path, run=offline), services.Detached)
    degraded = Runner({'systemctl --user is-system-running': (1, 'degraded\n')})
    assert isinstance(services.for_platform(tmp_path, run=degraded), services.SystemdUserUnit)
    monkeypatch.setattr(services.shutil, 'which', lambda name: None)
    assert isinstance(services.for_platform(tmp_path, run=degraded), services.Detached)


def test_from_record_rejects_other_root(tmp_path):
    with pytest.raises(ValueError, match='another installation'):
        services.from_record({'kind': 'systemd', 'name': 'x', 'root': str(tmp_path/'other')}, tmp_path)
    assert isinstance(services.from_record({'kind': 'detached', 'root': str(tmp_path)}, tmp_path), services.Detached)
