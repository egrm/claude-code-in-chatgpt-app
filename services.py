"""Owned on-demand worker launchers: Windows Task Scheduler, macOS launchd, Linux systemd --user.

Stdlib only. Every mutation is preceded by an ownership check; definitions have no
timed/login trigger and start no model work by themselves.
"""
import base64
import csv
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from platform_support import MACOS, NO_WINDOW, ROOT, WINDOWS, private_write, same_path, venv_python

DESCRIPTION = 'On-demand persistent worker for the local Claude Code supervisor MCP bridge'
FOREIGN = 'Worker launcher {} belongs to another installation; left unchanged'


def run_command(command, timeout=60, env=None):
    return subprocess.run([str(c) for c in command], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, env=env, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def _failure(what, result):
    detail = ' '.join(((result.stderr or '') + ' ' + (result.stdout or '')).split())[:300]
    return RuntimeError(f'{what} failed (exit {result.returncode}){": " + detail if detail else ""}')


def spawn_worker(root=ROOT, log_path=None):
    """Directly spawned worker: POSIX detached session; on Windows a development-only fallback
    (an MCP host's kill-on-close job would still own it, hence the scheduled task)."""
    from policy import clean_environment
    root = Path(root)
    log_path = Path(log_path or root / '.runtime' / 'daemon.log')
    kwargs = {'creationflags': NO_WINDOW} if WINDOWS else {'start_new_session': True}
    with log_path.open('ab') as output:
        return subprocess.Popen([sys.executable, str(root / 'daemon.py')], cwd=root, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, env=clean_environment(os.environ), close_fds=True, **kwargs)


WINDOWS_SCRIPTS = {
    'query': r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$task = Get-ScheduledTask -TaskName $env:CCS_TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) { '{"exists":false}'; exit 0 }
$actions = @($task.Actions)
$first = if ($actions.Count -eq 1) { $actions[0] } else { $null }
@{exists=$true; execute=[string]$first.Execute; arguments=[string]$first.Arguments; actions=$actions.Count;
  working_directory=[string]$first.WorkingDirectory; state=[string]$task.State;
  triggers=@($task.Triggers | Where-Object { $_ }).Count; time_limit=[string]$task.Settings.ExecutionTimeLimit;
  restart_count=[int]$task.Settings.RestartCount; multiple=[string]$task.Settings.MultipleInstances;
  logon=[string]$task.Principal.LogonType; run_level=[string]$task.Principal.RunLevel} | ConvertTo-Json -Compress
''',
    # Same definition the original setup.ps1 registered: current user, interactive, limited,
    # no trigger (on demand only), no time limit, single instance.
    'create': r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$action = New-ScheduledTaskAction -Execute $env:CCS_WORKER -Argument $env:CCS_ARGS -WorkingDirectory $env:CCS_ROOT
$principal = New-ScheduledTaskPrincipal -UserId $env:CCS_SID -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $env:CCS_TASK_NAME -Action $action -Principal $principal -Settings $settings -Description $env:CCS_DESCRIPTION | Out-Null
''',
    'recreate': r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Unregister-ScheduledTask -TaskName $env:CCS_TASK_NAME -Confirm:$false
$action = New-ScheduledTaskAction -Execute $env:CCS_WORKER -Argument $env:CCS_ARGS -WorkingDirectory $env:CCS_ROOT
$principal = New-ScheduledTaskPrincipal -UserId $env:CCS_SID -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $env:CCS_TASK_NAME -Action $action -Principal $principal -Settings $settings -Description $env:CCS_DESCRIPTION | Out-Null
''',
    'update': r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$action = New-ScheduledTaskAction -Execute $env:CCS_WORKER -Argument $env:CCS_ARGS -WorkingDirectory $env:CCS_ROOT
Set-ScheduledTask -TaskName $env:CCS_TASK_NAME -Action $action | Out-Null
''',
    'remove': r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Unregister-ScheduledTask -TaskName $env:CCS_TASK_NAME -Confirm:$false
''',
}


class WindowsTask:
    kind = 'windows-task'

    def __init__(self, root=ROOT, run=run_command, sid=None, name=None):
        self.root, self.run, self._sid, self._name = Path(root), run, sid, name

    @property
    def sid(self):
        if self._sid is None:
            whoami = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'whoami.exe'
            result = self.run([whoami, '/user', '/fo', 'csv', '/nh'], timeout=15)
            if result.returncode:
                raise _failure('Reading the current Windows user SID', result)
            self._sid = next(csv.reader([result.stdout.strip()]))[1]
        return self._sid

    @property
    def name(self):
        return self._name or 'ClaudeCodeSupervisor-' + self.sid

    def _powershell(self, op, **values):
        exe = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        encoded = base64.b64encode(WINDOWS_SCRIPTS[op].encode('utf-16-le')).decode('ascii')
        env = {**os.environ, 'CCS_TASK_NAME': self.name, **values}
        result = self.run([exe, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], env=env, timeout=90)
        if result.returncode:
            raise _failure(f'Task Scheduler {op} for {self.name}', result)
        text = result.stdout.strip()
        return json.loads(text) if text else {}

    def _action_values(self):
        return {'CCS_WORKER': str(venv_python(self.root, windowless=True, windows=True)),
                'CCS_ARGS': '"' + str(self.root / 'daemon.py') + '"', 'CCS_ROOT': str(self.root)}

    def classify(self, info):
        if not info.get('exists'):
            return 'absent'
        argument = (info.get('arguments') or '').strip()
        if len(argument) >= 2 and argument[0] == argument[-1] == '"':
            argument = argument[1:-1]
        execute = info.get('execute') or ''
        if not argument or not same_path(argument, self.root / 'daemon.py'):
            return 'foreign'
        if same_path(execute, venv_python(self.root, windowless=True, windows=True)):
            return 'owned'
        if same_path(execute, venv_python(self.root, windows=True)):
            return 'legacy'
        return 'foreign'

    @staticmethod
    def drift(info):
        """Departures from the on-demand definition: no trigger, no restart, no time limit, one instance."""
        expected = {'actions': 1, 'triggers': 0, 'time_limit': 'PT0S', 'restart_count': 0, 'multiple': 'IgnoreNew',
                    'logon': 'Interactive', 'run_level': 'Limited'}
        return sorted(key for key, value in expected.items() if key in info and info[key] != value)

    def inspect(self):
        info = self._powershell('query')
        return {'exists': bool(info.get('exists')), 'ownership': self.classify(info), 'state': info.get('state'),
                'name': self.name, 'drift': self.drift(info) if info.get('exists') else []}

    def register(self):
        info = self._powershell('query')
        ownership = self.classify(info)
        if ownership == 'foreign':
            raise ValueError(FOREIGN.format(self.name))
        drift = self.drift(info) if ownership != 'absent' else []
        if drift:
            if info.get('state') == 'Running':
                raise RuntimeError(f'Worker task {self.name} differs from the on-demand definition ({", ".join(drift)}) '
                                   'but is running; stop its tasks and the worker, then rerun setup.py install')
            self._powershell('recreate', CCS_SID=self.sid, CCS_DESCRIPTION=DESCRIPTION, **self._action_values())
            return 'repaired'
        if ownership == 'owned':
            return 'unchanged'
        if ownership == 'legacy':
            # Verified older python.exe launcher of this folder: switch only its action to windowless pythonw.
            self._powershell('update', **self._action_values())
            return 'updated'
        self._powershell('create', CCS_SID=self.sid, CCS_DESCRIPTION=DESCRIPTION, **self._action_values())
        return 'created'

    def start(self):
        ownership = self.classify(self._powershell('query'))
        if ownership not in ('owned', 'legacy'):
            raise ValueError(FOREIGN.format(self.name) if ownership == 'foreign'
                             else f'Worker task {self.name} is not registered; rerun setup.py install')
        schtasks = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'schtasks.exe'
        result = self.run([schtasks, '/Run', '/TN', self.name], timeout=15)
        if result.returncode:
            raise _failure('Starting the on-demand worker task; rerun setup.py install', result)

    def remove(self):
        ownership = self.classify(self._powershell('query'))
        if ownership == 'absent':
            return 'absent'
        if ownership == 'foreign':
            raise ValueError(FOREIGN.format(self.name))
        self._powershell('remove')
        return 'removed'

    def record(self):
        return {'kind': self.kind, 'name': self.name, 'root': str(self.root)}


def default_path_env():
    entries = os.environ.get('PATH', '').split(os.pathsep) + ['/usr/local/bin', '/opt/homebrew/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    seen = []
    for entry in entries:
        if entry and entry not in seen:
            seen.append(entry)
    return os.pathsep.join(seen)


class LaunchAgent:
    kind = 'launchd'
    label = 'com.github.egrm.claude-code-supervisor'

    def __init__(self, root=ROOT, run=run_command, home=None, uid=None, path_env=None):
        self.root, self.run = Path(root), run
        self.home = Path(home) if home else Path.home()
        self.uid = os.getuid() if uid is None else uid
        self.path_env = path_env or default_path_env()
        self.plist_path = self.home / 'Library' / 'LaunchAgents' / (self.label + '.plist')
        self.domain = f'gui/{self.uid}'
        self.target = f'{self.domain}/{self.label}'
        self.note = None

    def definition(self):
        log = str(self.root / '.runtime' / 'daemon.log')
        # No RunAtLoad/KeepAlive/StartInterval: loaded at login, started only by kickstart.
        return {'Label': self.label, 'ProgramArguments': [str(venv_python(self.root, windows=False)), str(self.root / 'daemon.py')],
                'WorkingDirectory': str(self.root), 'EnvironmentVariables': {'PATH': self.path_env},
                'RunAtLoad': False, 'StandardOutPath': log, 'StandardErrorPath': log}

    def _existing(self):
        if not self.plist_path.exists():
            return None
        try:
            return plistlib.loads(self.plist_path.read_bytes())
        except Exception:
            return {}

    def classify(self, data):
        if data is None:
            return 'absent'
        args = data.get('ProgramArguments') if isinstance(data, dict) else None
        if (isinstance(args, list) and len(args) == 2 and data.get('Label') == self.label
                and same_path(args[0], venv_python(self.root, windows=False)) and same_path(args[1], self.root / 'daemon.py')
                and same_path(data.get('WorkingDirectory', ''), self.root)):
            return 'owned'
        return 'foreign'

    def loaded(self):
        return self.loaded_ownership() != 'absent'

    def loaded_ownership(self):
        """Ownership of whatever launchd currently has loaded under this label (independent of disk)."""
        result = self.run(['launchctl', 'print', self.target], timeout=15)
        if result.returncode:
            return 'absent'
        shown = result.stdout or ''
        paths = (venv_python(self.root, windows=False), self.root / 'daemon.py', self.root)
        return 'owned' if all(f'{p}\n' in shown or f'{p} ' in shown or f'{p}"' in shown for p in paths) else 'foreign'

    def _refuse_foreign(self):
        if self.classify(self._existing()) == 'foreign' or self.loaded_ownership() == 'foreign':
            raise ValueError(FOREIGN.format(self.target))

    def _bootstrap(self):
        result = self.run(['launchctl', 'bootstrap', self.domain, str(self.plist_path)], timeout=30)
        if result.returncode:
            raise _failure('launchctl bootstrap of the worker agent', result)

    def inspect(self):
        data = self._existing()
        result = self.run(['launchctl', 'print', self.target], timeout=15)
        loaded = self.loaded_ownership()
        ownership = 'foreign' if loaded == 'foreign' else self.classify(data)
        state = 'not loaded' if loaded == 'absent' else 'running' if 'state = running' in (result.stdout or '') else 'loaded'
        return {'exists': data is not None or loaded != 'absent', 'ownership': ownership, 'state': state, 'name': self.label}

    def register(self):
        existing = self._existing()
        self._refuse_foreign()   # before any write/bootout: disk AND loaded job
        data = plistlib.dumps(self.definition())
        changed = existing is None or self.plist_path.read_bytes() != data
        if changed:
            self.plist_path.parent.mkdir(parents=True, exist_ok=True)
            private_write(self.plist_path, data, mode=0o644)
        if not self.loaded():
            self._bootstrap()
        elif changed:
            if self.inspect()['state'] == 'running':
                self.note = 'Updated agent definition applies after the worker next stops'
            else:
                self.run(['launchctl', 'bootout', self.target], timeout=30)
                self._bootstrap()
        return 'created' if existing is None else 'updated' if changed else 'unchanged'

    def start(self):
        if self.classify(self._existing()) != 'owned':
            raise ValueError(FOREIGN.format(self.plist_path) + '; rerun setup.py install')
        loaded = self.loaded_ownership()
        if loaded == 'foreign':
            raise ValueError(f'Loaded launchd job {self.label} does not run this checkout; rerun setup.py install')
        if loaded == 'absent':
            self._bootstrap()
        result = self.run(['launchctl', 'kickstart', self.target], timeout=30)
        if result.returncode:
            raise _failure('launchctl kickstart of the worker agent; rerun setup.py install', result)

    def remove(self):
        existing = self._existing()
        self._refuse_foreign()
        if existing is None and not self.loaded():
            return 'absent'
        if self.loaded():
            result = self.run(['launchctl', 'bootout', self.target], timeout=30)
            if result.returncode:
                raise _failure('launchctl bootout of the worker agent', result)
        if existing is not None:
            self.plist_path.unlink()
        return 'removed'

    def record(self):
        return {'kind': self.kind, 'name': self.label, 'root': str(self.root)}


def _systemd_quote(value, expands_variables=True):
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (value.replace('$', '$$') if expands_variables else value) + '"'


def systemd_available(run=run_command):
    if not shutil.which('systemctl'):
        return False
    try:
        result = run(['systemctl', '--user', 'is-system-running'], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    # Non-zero for "degraded" too; the state word shows whether a user manager answered.
    return (result.stdout or '').strip() in ('running', 'degraded', 'starting', 'initializing', 'maintenance')


class SystemdUserUnit:
    kind = 'systemd'
    unit = 'claude-code-supervisor.service'
    marker = '# claude-code-supervisor root: '

    def __init__(self, root=ROOT, run=run_command, config_home=None, path_env=None):
        self.root, self.run = Path(root), run
        config_home = config_home or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
        self.unit_path = Path(config_home) / 'systemd' / 'user' / self.unit
        self.path_env = path_env or default_path_env()
        self.note = None

    def definition(self):
        python = str(venv_python(self.root, windows=False))
        # No [Install] section: cannot be enabled, so it only ever starts on demand.
        return '\n'.join([
            self.marker + str(self.root), '[Unit]', 'Description=' + DESCRIPTION, '', '[Service]', 'Type=simple',
            'WorkingDirectory=' + str(self.root).replace('%', '%%'),
            'ExecStart=' + _systemd_quote(python) + ' ' + _systemd_quote(str(self.root / 'daemon.py')),
            'Environment=' + _systemd_quote('PATH=' + self.path_env, expands_variables=False),
            'KillMode=control-group', 'Restart=no', ''])

    def _existing(self):
        return self.unit_path.read_text(encoding='utf-8', errors='replace') if self.unit_path.exists() else None

    def classify(self, text):
        if text is None:
            return 'absent'
        keys = ('ExecStart=', 'WorkingDirectory=')
        wanted = {line for line in self.definition().splitlines() if line.startswith(keys)}
        found = {line.strip() for line in text.splitlines() if line.strip().startswith(keys)}
        return 'owned' if found == wanted else 'foreign'

    def _systemctl(self, *args, check=True):
        result = self.run(['systemctl', '--user', *args], timeout=30)
        if check and result.returncode:
            raise _failure('systemctl --user ' + ' '.join(args), result)
        return result

    def loaded_ownership(self):
        """Ownership of the unit systemd has loaded (may differ from disk until daemon-reload)."""
        shown = self._systemctl('show', self.unit, '-p', 'LoadState', '-p', 'ExecStart', '-p', 'WorkingDirectory',
                                check=False).stdout or ''
        if not shown.strip() or 'LoadState=not-found' in shown.splitlines():
            return 'absent'
        return 'owned' if self.loaded_matches(shown) else 'foreign'

    def _refuse_foreign(self):
        if self.classify(self._existing()) == 'foreign' or self.loaded_ownership() == 'foreign':
            raise ValueError(FOREIGN.format(self.unit_path))

    def inspect(self):
        text = self._existing()
        loaded = self.loaded_ownership()
        state = None
        if text is not None or loaded != 'absent':
            state = (self._systemctl('is-active', self.unit, check=False).stdout or '').strip() or 'unknown'
        ownership = 'foreign' if loaded == 'foreign' else self.classify(text)
        return {'exists': text is not None or loaded != 'absent', 'ownership': ownership, 'state': state, 'name': self.unit}

    def register(self):
        existing = self._existing()
        self._refuse_foreign()   # before any write/daemon-reload: disk AND loaded unit
        text = self.definition()
        if existing == text:
            return 'unchanged'
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        private_write(self.unit_path, text, mode=0o644)
        self._systemctl('daemon-reload')
        return 'created' if existing is None else 'updated'

    def start(self):
        if self.classify(self._existing()) != 'owned':
            raise ValueError(FOREIGN.format(self.unit_path) + '; rerun setup.py install')
        if self.loaded_ownership() != 'owned':
            raise ValueError(f'Loaded unit {self.unit} does not run this checkout; rerun setup.py install')
        self._systemctl('start', self.unit)

    def loaded_matches(self, shown):
        """`systemctl show` lines: exact WorkingDirectory, ExecStart path= interpreter and argv[] daemon."""
        lines = shown.splitlines()
        execs = [line for line in lines if line.startswith('ExecStart=')]
        python, daemon = str(venv_python(self.root, windows=False)), str(self.root / 'daemon.py')
        argv = (f'argv[]={python} {daemon} ;', f'argv[]="{python}" "{daemon}" ;')
        return (f'WorkingDirectory={self.root}' in lines and len(execs) == 1 and f'path={python} ;' in execs[0]
                and any(a in execs[0] for a in argv))

    def remove(self):
        existing = self._existing()
        self._refuse_foreign()   # before stop: never stop a foreign loaded unit
        loaded = self.loaded_ownership()
        if existing is None and loaded == 'absent':
            return 'absent'
        if loaded == 'owned':
            self._systemctl('stop', self.unit, check=False)
        if existing is not None:
            self.unit_path.unlink()
        self._systemctl('daemon-reload', check=False)
        return 'removed'

    def record(self):
        return {'kind': self.kind, 'name': self.unit, 'root': str(self.root)}


class Detached:
    """POSIX fallback when no systemd user manager is reachable."""
    kind = 'detached'
    note = None

    def __init__(self, root=ROOT, run=None):
        self.root = Path(root)

    def inspect(self):
        return {'exists': True, 'ownership': 'owned', 'state': 'detached session (no service manager)', 'name': 'detached'}

    def register(self):
        return 'unchanged'

    def start(self):
        return spawn_worker(self.root)

    def remove(self):
        return 'removed'

    def record(self):
        return {'kind': self.kind, 'root': str(self.root)}


def for_platform(root=ROOT, run=run_command):
    if WINDOWS:
        return WindowsTask(root, run)
    if MACOS:
        return LaunchAgent(root, run)
    if systemd_available(run):
        return SystemdUserUnit(root, run)
    return Detached(root)


def from_record(record, root=ROOT, run=run_command):
    if Path(record.get('root', '')).resolve() != Path(root).resolve():
        raise ValueError('Worker launcher belongs to another installation; rerun setup.py install')
    kind = record.get('kind')
    if kind == 'windows-task':
        return WindowsTask(root, run, name=record['name'])
    if kind == 'launchd':
        return LaunchAgent(root, run)
    if kind == 'systemd':
        return SystemdUserUnit(root, run)
    if kind == 'detached':
        return Detached(root)
    raise ValueError('Unknown worker launcher record; rerun setup.py install')
