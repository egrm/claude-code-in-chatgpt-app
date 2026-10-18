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


def test_windows_start_verifies_ownership_then_runs_and_reports_status(tmp_path, monkeypatch):
    monkeypatch.setenv('SystemRoot', str(tmp_path/'Windows'))
    runner = Runner({str(tmp_path/'Windows'/'System32'/'schtasks.exe'): (1, 'ERROR: denied')})
    task = services.WindowsTask(tmp_path, run=runner, sid='S-1')
    for other in (action(tmp_path/'other'), {'exists': False}):
        task._powershell = lambda op, **v: other
        with pytest.raises(ValueError):
            task.start()
    assert runner.calls == []
    task._powershell = lambda op, **v: action(tmp_path)
    with pytest.raises(RuntimeError, match='exit 1'):
        task.start()
    assert runner.calls[0][1:] == ['/Run', '/TN', 'ClaudeCodeSupervisor-S-1']


LIVE_SETTINGS = {'actions': 1, 'triggers': 0, 'time_limit': 'PT0S', 'restart_count': 0, 'multiple': 'IgnoreNew',
                 'logon': 'Interactive', 'run_level': 'Limited'}


def test_windows_correct_running_task_untouched_but_drift_is_repaired_only_when_idle(tmp_path):
    task, ops = windows_task(tmp_path, {**action(tmp_path), **LIVE_SETTINGS})
    assert task.register() == 'unchanged' and [op for op, _ in ops] == ['query']
    for drift in ({'triggers': 1}, {'restart_count': 3}, {'time_limit': 'PT72H'}, {'multiple': 'Parallel'}):
        task, ops = windows_task(tmp_path, {**action(tmp_path), **LIVE_SETTINGS, **drift})
        with pytest.raises(RuntimeError, match='running'):
            task.register()
        assert [op for op, _ in ops] == ['query']
        task, ops = windows_task(tmp_path, {**action(tmp_path), **LIVE_SETTINGS, **drift, 'state': 'Ready'})
        assert task.register() == 'repaired' and [op for op, _ in ops] == ['query', 'recreate']
    assert 'Trigger' not in services.WINDOWS_SCRIPTS['recreate'] and 'IgnoreNew' in services.WINDOWS_SCRIPTS['recreate']


def launch_agent(tmp_path, results=None):
    runner = Runner(results)
    agent = services.LaunchAgent(tmp_path/'proj', run=runner, home=tmp_path/'home', uid=501, path_env='/usr/bin:/bin')
    return agent, runner


def launchd_print(agent, python=None, daemon=None, workdir=None):
    python = python or agent.definition()['ProgramArguments'][0]
    return (f"{agent.target} = {{\n\tstate = running\n\tprogram = {python}\n\targuments = {{\n\t\t{python}\n"
            f"\t\t{daemon or agent.root/'daemon.py'}\n\t}}\n\tworking directory = {workdir or agent.root}\n}}\n")


def test_launchd_and_systemd_refuse_foreign_loaded_job_before_any_mutation(tmp_path):
    agent, runner = launch_agent(tmp_path, {'launchctl print': (0, launchd_print(launch_agent(tmp_path)[0], python='/other/python'))})
    with pytest.raises(ValueError, match='another installation'):
        agent.register()          # absent on disk, foreign loaded
    assert not agent.plist_path.exists() and not any(c[1] in ('bootout', 'bootstrap') for c in runner.calls)
    runner.results = {'launchctl print': (113, '')}
    agent.register()
    runner.results = {'launchctl print': (0, launchd_print(agent, daemon='/other/daemon.py'))}
    runner.calls.clear()
    with pytest.raises(ValueError, match='another installation'):
        agent.remove()            # owned on disk, foreign loaded
    assert agent.plist_path.exists() and not any(c[1] == 'bootout' for c in runner.calls)
    assert agent.inspect()['ownership'] == 'foreign'
    unit, srunner = systemd(tmp_path)
    foreign = 'LoadState=loaded\nExecStart={ path=/other/python ; argv[]=/other/python /other/daemon.py ; }\nWorkingDirectory=/other\n'
    srunner.results = {'systemctl --user show': (0, foreign)}
    with pytest.raises(ValueError, match='another installation'):
        unit.register()
    assert not unit.unit_path.exists() and ['systemctl', '--user', 'daemon-reload'] not in srunner.calls
    srunner.results = {}
    unit.register()
    srunner.results = {'systemctl --user show': (0, foreign)}
    srunner.calls.clear()
    with pytest.raises(ValueError, match='another installation'):
        unit.remove()
    assert unit.unit_path.exists() and not any('stop' in c for c in srunner.calls)
    assert unit.inspect()['ownership'] == 'foreign'


def test_launchd_agent_is_on_demand_and_owned(tmp_path):
    agent, runner = launch_agent(tmp_path, {'launchctl print': (113, '')})
    assert agent.register() == 'created'
    data = plistlib.loads(agent.plist_path.read_bytes())
    assert data['ProgramArguments'] == [str(tmp_path/'proj'/'.venv'/'bin'/'python'), str(tmp_path/'proj'/'daemon.py')]
    assert data['RunAtLoad'] is False and 'KeepAlive' not in data and 'StartInterval' not in data
    assert data['EnvironmentVariables'] == {'PATH': '/usr/bin:/bin'}
    assert ['launchctl', 'bootstrap', 'gui/501', str(agent.plist_path)] in runner.calls
    runner.results = {'launchctl print': (0, launchd_print(agent))}
    assert agent.register() == 'unchanged'


@pytest.mark.parametrize('change', [
    {'ProgramArguments': ['/x/python', '/x/daemon.py'], 'WorkingDirectory': '/x'},
    {'Label': 'com.other.label'},
    {'ProgramArguments': ['/usr/bin/python3', 'DAEMON']},
])
def test_launchd_refuses_foreign_plist(tmp_path, change):
    agent, _ = launch_agent(tmp_path)
    data = agent.definition()
    data.update(change)
    if data['ProgramArguments'][-1] == 'DAEMON':
        data['ProgramArguments'][-1] = str(agent.root/'daemon.py')
    agent.plist_path.parent.mkdir(parents=True)
    agent.plist_path.write_bytes(plistlib.dumps(data))
    for operation in (agent.register, agent.remove, agent.start):
        with pytest.raises(ValueError, match='another installation'):
            operation()
    assert agent.plist_path.exists()


def test_launchd_start_bootstraps_when_not_loaded_and_checks_loaded_job(tmp_path):
    agent, runner = launch_agent(tmp_path, {'launchctl print': (113, '')})
    agent.register()
    runner.calls.clear()
    agent.start()
    assert runner.calls[-1] == ['launchctl', 'kickstart', 'gui/501/' + agent.label]
    assert ['launchctl', 'bootstrap', 'gui/501', str(agent.plist_path)] in runner.calls
    runner.results = {'launchctl print': (0, 'program = /other/python\narguments = { /other/daemon.py }')}
    runner.calls.clear()
    with pytest.raises(ValueError, match='does not run this checkout'):
        agent.start()
    assert not any('kickstart' in call for call in runner.calls)
    runner.results = {'launchctl print': (0, launchd_print(agent))}
    agent.start()
    assert runner.calls[-1][1] == 'kickstart'


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
    python, daemon = unit.root/'.venv'/'bin'/'python', unit.root/'daemon.py'
    def shown(interpreter=python, script=daemon, workdir=unit.root):
        return (f'ExecStart={{ path={interpreter} ; argv[]={interpreter} {script} ; ignore_errors=no ; start_time=[n/a] ; '
                f'stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}\nWorkingDirectory={workdir}\n')
    for bad in (shown(interpreter='/other/python'), shown(script=str(daemon) + '.bak'),
                shown(workdir=str(unit.root) + '-sibling'), ''):
        runner.results = {'systemctl --user show': (0, bad)}
        with pytest.raises(ValueError, match='does not run this checkout'):
            unit.start()
        assert runner.calls[-1][2] == 'show'
    runner.results = {'systemctl --user show': (0, shown())}
    unit.start()
    assert runner.calls[-1] == ['systemctl', '--user', 'start', unit.unit]


def test_systemd_foreign_unit_refused_even_with_copied_marker(tmp_path):
    unit, _ = systemd(tmp_path)
    unit.unit_path.parent.mkdir(parents=True)
    forged = unit.marker + str(unit.root) + '\n[Service]\nExecStart=/somewhere/else\n'
    unit.unit_path.write_text(forged)
    for operation in (unit.register, unit.remove, unit.start):
        with pytest.raises(ValueError, match='another installation'):
            operation()
    assert unit.unit_path.read_text() == forged


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
