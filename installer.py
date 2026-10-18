"""install | configure | doctor | remove for this checkout. Stdlib only (runs before the venv exists).

Steps needing dependencies (Windows ACLs, worker start, MCP discovery) run in the venv interpreter.
No step performs model inference, restarts the desktop app or displays credentials.
"""
import argparse
from dataclasses import dataclass
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import platform_support as ps
import registration
import services

ACTIVE = {'running', 'waiting_for_permission', 'waiting_for_answer'}
TOOLS = {'task_list', 'task_status', 'task_start', 'task_followup', 'task_events', 'task_wait',
         'task_request', 'task_answer', 'task_interrupt', 'task_resume', 'task_stop', 'task_result'}
CLAUDE_SETUP = 'https://code.claude.com/docs/en/setup'


class SetupError(RuntimeError):
    pass


@dataclass
class Context:
    root: Path = ps.ROOT
    config: Path = None

    def __post_init__(self):
        self.root = Path(self.root)
        self.runtime = self.root / '.runtime'
        self.config = Path(self.config) if self.config else ps.codex_config_path()

    @property
    def python(self):
        return ps.venv_python(self.root)


def say(status, label, detail=''):
    print(f'[{status}] {label}' + (f': {detail}' if detail else ''), flush=True)


# --- steps (module-level so tests can substitute them) ---

def ensure_venv(ctx):
    if not ctx.python.exists():
        if (ctx.root / '.venv').exists():
            raise SetupError('.venv exists but has no interpreter; inspect or move it aside, then rerun')
        say('..', 'Creating virtual environment', str(ctx.root / '.venv'))
        result = subprocess.run([sys.executable, '-m', 'venv', str(ctx.root / '.venv')], cwd=ctx.root)
        if result.returncode and not ctx.python.exists():
            # e.g. Debian without ensurepip: create without pip; pip_command() recovers or explains.
            result = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(ctx.root / '.venv')], cwd=ctx.root)
        if not ctx.python.exists():
            raise SetupError('Virtual environment creation failed (Debian/Ubuntu: sudo apt install python3-venv)')
    version = venv_run(ctx, 'import sys; print("%d.%d.%d" % sys.version_info[:3])').strip()
    if tuple(int(x) for x in version.split('.')[:2]) < ps.MIN_PYTHON:
        raise SetupError(f'.venv uses Python {version}; 3.11+ is required. Move .venv aside and rerun with a newer Python')
    say('ok', 'Virtual environment', f'Python {version}')


def _works(command):
    try:
        return subprocess.run(command, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                              creationflags=ps.NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pip_command(ctx):
    """venv pip; else recover it with ensurepip; else the setup Python's pip targeting the venv."""
    own = [str(ctx.python), '-m', 'pip']
    if _works(own + ['--version']):
        return own
    if _works([str(ctx.python), '-m', 'ensurepip', '--upgrade', '--default-pip']) and _works(own + ['--version']):
        return own
    if _works([sys.executable, '-m', 'pip', '--version']):
        return [sys.executable, '-m', 'pip', '--python', str(ctx.python)]
    raise SetupError('pip is missing from .venv and ensurepip is unavailable. Install your Python\'s venv/pip support '
                     '(Debian/Ubuntu: sudo apt install python3-venv python3-pip) or use a python.org/Homebrew Python, '
                     'then move .venv aside and rerun')


def install_requirements(ctx, dev=False):
    requirements = ctx.root / ('requirements-dev.txt' if dev else 'requirements.txt')
    say('..', 'Installing pinned dependencies', requirements.name)
    result = subprocess.run(pip_command(ctx) + ['install', '--disable-pip-version-check', '-q', '-r',
                             str(requirements)], cwd=ctx.root)
    if result.returncode:
        raise SetupError('Dependency installation failed; see pip output above')


def venv_run(ctx, code, timeout=120):
    result = subprocess.run([str(ctx.python), '-c', code], cwd=ctx.root, capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL, creationflags=ps.NO_WINDOW)
    if result.returncode:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise SetupError(lines[-1][:500] if lines else f'venv step failed (exit {result.returncode})')
    return result.stdout


def find_claude(ctx):
    return ps.find_claude_cli()


def claude_version(ctx, cli):
    from policy import clean_environment
    try:
        result = subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=20,
                                stdin=subprocess.DEVNULL, env=clean_environment(os.environ), creationflags=ps.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else None


def auth_fields(ctx, cli):
    from policy import auth_status
    try:
        return auth_status(cli)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def auth_problem(fields):
    from policy import validate_auth
    if fields is None:
        return 'status unavailable'
    try:
        validate_auth(fields)
    except ValueError as exc:
        return str(exc)
    return None


def show_auth(fields):
    from policy import AUTH_FIELDS
    return ', '.join(f'{k}={fields.get(k)}' for k in AUTH_FIELDS) if fields else 'unavailable'


def service_for(ctx):
    record = ps.load_service_record(ctx.runtime)
    if record is not None:
        try:
            return services.from_record(record, ctx.root)
        except ValueError:
            pass
    return services.for_platform(ctx.root)


def worker_rpc(ctx, method, args=None, timeout=5):
    """Authenticated loopback call to this installation's worker; None when unreachable."""
    try:
        info = json.loads((ctx.runtime / 'transport.json').read_text())
    except (OSError, ValueError):
        return None
    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/rpc",
        data=json.dumps({'method': method, 'args': args or {}}).encode(),
        headers={'Authorization': 'Bearer ' + info['secret'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read()).get('result')
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get('error')
        except ValueError:
            error = None
        raise SetupError(f'Worker refused {method}: {error or exc.code}') from None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def active_tasks(ctx):
    """Confirmed active-task count (lower bound for pre-lease workers); None only if no worker answers."""
    try:
        counts = worker_rpc(ctx, 'admin_tasks')
    except SetupError as exc:
        if 'Unknown supervisor operation' not in str(exc):
            raise
        total = 0
        for state in sorted(ACTIVE):   # pre-lease worker: Registry.list filters by state before limiting
            rows = worker_rpc(ctx, 'list', {'state': state, 'limit': 100})
            if not isinstance(rows, list):
                raise SetupError('Older worker gave no task list; refusing to stop it')
            total += len(rows)
        return total
    if counts is None:
        return None
    if not isinstance(counts, dict) or not isinstance(counts.get('active'), int):
        raise SetupError('Worker task count unavailable; refusing to stop it')
    return counts['active']


def upgrade_worker(ctx):
    """Stop an idle pre-lease worker so the current one starts; never interrupts active tasks."""
    from lifecycle import PROTOCOL
    health = worker_healthy(ctx)
    if not health or health.get('protocol', 1) >= PROTOCOL:
        return True
    active = active_tasks(ctx)
    if active is None or active > 0:
        say('FAIL', 'Older worker', ('unreachable' if active is None else f'{active} active task(s)')
            + '; left running. Finish/stop its tasks, then rerun install')
        return False
    worker_rpc(ctx, 'shutdown')
    stopped = wait_worker_stopped(ctx, 30)
    say('ok' if stopped else 'FAIL', 'Older idle worker', 'stopped for upgrade' if stopped else 'did not stop')
    return stopped


def worker_healthy(ctx):
    try:
        result = worker_rpc(ctx, 'health', timeout=2)
    except SetupError:
        return None
    return result if isinstance(result, dict) and result.get('service') == 'claude-code-supervisor-v1' else None


def wait_worker_stopped(ctx, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not worker_healthy(ctx):
            return True
        time.sleep(.3)
    return False


VENV_CHECK = r'''
import importlib.metadata as metadata, json, re, sys
problems = []
if sys.version_info < (3, 11):
    problems.append('Python %s < 3.11' % sys.version.split()[0])
for line in open('requirements.txt', encoding='utf-8'):
    spec, _, marker = line.split('#')[0].strip().partition(';')
    if not spec:
        continue
    condition = re.search(r"sys_platform\s*(==|!=)\s*['\"]([^'\"]+)", marker)
    if condition and (sys.platform == condition.group(2)) != (condition.group(1) == '=='):
        continue
    name, _, pin = (part.strip() for part in spec.partition('=='))
    try:
        found = metadata.version(name)
    except metadata.PackageNotFoundError:
        problems.append(name + ' missing')
        continue
    if pin and found != pin:
        problems.append('%s %s (pinned %s)' % (name, found, pin))
for module in ['claude_agent_sdk', 'mcp', 'httpx2', 'starlette', 'uvicorn'] + (['win32job', 'win32security'] if sys.platform == 'win32' else []):
    try:
        __import__(module)
    except Exception as exc:
        problems.append('import %s: %s' % (module, type(exc).__name__))
print(json.dumps({'python': sys.version.split()[0], 'problems': problems}))
'''


def venv_report(ctx):
    if not ctx.python.exists():
        return False, f'missing {ctx.python}; run: python setup.py install'
    try:
        report = json.loads(venv_run(ctx, VENV_CHECK, timeout=60))
    except (SetupError, ValueError, subprocess.SubprocessError) as exc:
        return False, f'venv check failed: {exc}'
    if report['problems']:
        return False, f"Python {report['python']}; " + '; '.join(report['problems']) + ' (rerun setup.py install)'
    return True, f"Python {report['python']}; pinned dependencies match"


def mcp_discovery(ctx):
    try:
        result = subprocess.run([str(ctx.python), str(ctx.root / 'mcp_probe.py')], cwd=ctx.root, capture_output=True,
                                text=True, timeout=90, stdin=subprocess.DEVNULL, creationflags=ps.NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, 'MCP discovery timed out'
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        lines = result.stderr.strip().splitlines()
        return False, 'MCP discovery failed: ' + (lines[-1][:300] if lines else f'exit {result.returncode}')
    tools = set(data.get('tools', []))
    if tools != TOOLS:
        return False, f'unexpected tool set ({len(tools)} tools)'
    return True, f"{len(tools)} tools discovered over stdio (protocol {data.get('protocol')})"


def read_config(ctx):
    if not ctx.config.exists():
        return ''
    return ctx.config.read_bytes().decode('utf-8-sig')


# --- commands ---

def activation_instructions():
    print('\nNext steps (nothing was restarted):\n'
          '  1. In the ChatGPT/Codex desktop app use the MCP servers Restart/refresh control, or fully quit and\n'
          '     reopen the app when convenient (not during a voice call you want to keep).\n'
          '  2. In a chat type /mcp and confirm claude_supervisor is enabled and connected.\n'
          '  3. Ask: "Use the claude_supervisor tools to call task_list. Do not start an agent yet."\n'
          '  Diagnostics: python setup.py doctor')


def install(ctx, dev=False, configure=True):
    print(f'Claude Code Supervisor setup for {ctx.root}')
    ensure_venv(ctx)
    install_requirements(ctx, dev)
    venv_run(ctx, 'from runtime import private_runtime; private_runtime()')
    say('ok', 'Private runtime directory', str(ctx.runtime))
    cli, notes = find_claude(ctx)
    if cli is None:
        say('FAIL', 'Native Claude Code not found', '; '.join(notes) or str(ps.default_claude_cli()))
        print(f'  Install native Claude Code ({CLAUDE_SETUP}); npm/.cmd shims are not used. Then rerun this command.')
        return 2
    say('ok', 'Native Claude Code', str(cli))
    ps.private_write(ctx.runtime / 'install.json', json.dumps({'root': str(ctx.root), 'claude_cli': str(cli)}, indent=2))
    fields = auth_fields(ctx, cli)
    problem = auth_problem(fields)
    if problem:
        say('warn', 'Claude authentication', show_auth(fields))
        print(f'  Sign in with your Claude Max subscription: "{cli}" auth login --claudeai\n'
              '  Max via claude.ai/firstParty only; API keys, providers and usage credits are not used.')
    else:
        say('ok', 'Claude authentication', show_auth(fields))
    if ps.WINDOWS and ps.git_bash() is None:
        say('warn', 'Git Bash', 'not found; Claude\'s Bash tool needs Git for Windows (https://git-scm.com/downloads/win)')
    service = preflight(ctx) if configure else service_for(ctx)
    if not configure and service.inspect()['ownership'] == 'foreign':
        raise ValueError(f'Worker launcher {service.kind} belongs to another installation')
    if not upgrade_worker(ctx):
        return 4
    outcome = service.register()
    ps.save_service_record(service.record(), ctx.runtime)
    say('ok', f'Worker launcher ({service.kind})', outcome + (f'; {service.note}' if getattr(service, 'note', None) else ''))
    venv_run(ctx, 'from runtime import ensure_worker; ensure_worker()', timeout=90)
    say('ok', 'Persistent worker is ready', 'on demand; it exits after the last MCP client leaves')
    if configure:
        changed = registration.apply(ctx.config, partial(registration.configure_text, root=ctx.root), ctx.runtime)
        say('ok', 'MCP entry claude_supervisor', ('registered' if changed else 'already registered') + f' in {ctx.config}')
    else:
        say('skip', 'MCP entry', 'not configured (--no-configure); run: python setup.py configure')
    ok, detail = mcp_discovery(ctx)
    say('ok' if ok else 'FAIL', 'MCP discovery (no inference)', detail)
    activation_instructions()
    if not ok:
        return 1
    if problem:
        print('\nSetup finished, but Claude Max authentication still needs attention (see above).')
        return 3
    return 0


def configure(ctx):
    if not ctx.python.exists():
        raise SetupError('Virtual environment missing; run: python setup.py install')
    venv_run(ctx, 'from runtime import private_runtime; private_runtime()')
    changed = registration.apply(ctx.config, partial(registration.configure_text, root=ctx.root), ctx.runtime)
    say('ok', 'MCP entry claude_supervisor', ('registered' if changed else 'already registered') + f' in {ctx.config}')
    if changed:
        activation_instructions()
    return 0


def doctor(ctx, mcp=True):
    failed = []
    def check(status, label, detail=''):
        if status == 'FAIL':
            failed.append(label)
        say(status, label, detail)

    version = '.'.join(map(str, sys.version_info[:3]))
    check('ok' if sys.version_info[:2] >= ps.MIN_PYTHON else 'FAIL', 'Setup Python', version)
    venv_ok, detail = venv_report(ctx)
    check('ok' if venv_ok else 'FAIL', 'Virtual environment', detail)

    cli, notes = find_claude(ctx)
    if cli is None:
        check('FAIL', 'Native Claude Code', 'not found' + (' (' + '; '.join(notes) + ')' if notes else '') + f'; see {CLAUDE_SETUP}')
    else:
        version = claude_version(ctx, cli)
        check('ok' if version else 'FAIL', 'Native Claude Code', f'{cli} ({version or "--version failed"})')
        fields = auth_fields(ctx, cli)
        problem = auth_problem(fields)
        check('FAIL' if problem else 'ok', 'Claude Max authentication', show_auth(fields)
              + (f'; sign in: "{cli}" auth login --claudeai' if problem else ''))

    if ps.WINDOWS:
        bash = ps.git_bash()
        check('ok' if bash else 'FAIL', 'Git Bash (Claude Bash tool)', str(bash) if bash else
              'not found; install Git for Windows: https://git-scm.com/downloads/win')

    registered = False
    try:
        status = registration.entry_status(read_config(ctx), ctx.root)
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        check('FAIL', 'MCP entry', str(exc) if isinstance(exc, ValueError) else 'host configuration unreadable')
    else:
        if status['state'] == 'absent':
            check('FAIL', 'MCP entry', f'claude_supervisor not in {ctx.config}; run: python setup.py configure')
        elif status['state'] == 'foreign':
            check('FAIL', 'MCP entry', registration.FOREIGN)
        elif not status['enabled']:
            check('FAIL', 'MCP entry', 'owned but disabled in host settings; enable it in the app')
        else:
            registered = True
            timeouts = ', '.join(f'{k}={status[k]}' for k in registration.TIMEOUTS)
            check('ok', 'MCP entry', f'owned by this checkout ({timeouts})')

    launcher_ok = False
    try:
        service = service_for(ctx)
        info = service.inspect()
    except Exception as exc:
        check('FAIL', 'Worker launcher', str(exc)[:300])
    else:
        name = f"{service.kind} {info.get('name') or ''}".strip()
        if info['ownership'] == 'absent':
            check('FAIL', 'Worker launcher', f'{name} not registered; run: python setup.py install')
        elif info['ownership'] == 'foreign':
            check('FAIL', 'Worker launcher', f'{name} belongs to another installation')
        elif info['ownership'] == 'legacy':
            check('warn', 'Worker launcher', f'{name} uses legacy python.exe; rerun: python setup.py install')
            launcher_ok = True
        else:
            check('ok', 'Worker launcher', f"{name} ({info.get('state')})")
            launcher_ok = True

    health = worker_healthy(ctx)
    check('ok', 'Worker', f"running (pid {health.get('pid')})" if health else 'not running; starts on demand when an MCP client connects')

    if not mcp:
        check('skip', 'MCP discovery', '--quick')
    elif venv_ok and registered and launcher_ok:
        ok, detail = mcp_discovery(ctx)
        check('ok' if ok else 'FAIL', 'MCP discovery', detail)
    else:
        check('skip', 'MCP discovery', 'fix the failures above first')
    print('\nAll checks passed.' if not failed else f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
    return 1 if failed else 0


def preflight(ctx):
    """Ownership of the MCP entry and launcher, checked before any mutation or worker stop."""
    if registration.entry_status(read_config(ctx), ctx.root)['state'] == 'foreign':
        raise ValueError(registration.FOREIGN)
    service = service_for(ctx)
    if service.inspect()['ownership'] == 'foreign':
        raise ValueError(f'Worker launcher {service.kind} belongs to another installation')
    return service


def remove(ctx, force=False):
    try:
        service = preflight(ctx)
        active = active_tasks(ctx)
    except (ValueError, RuntimeError, OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        say('FAIL', 'Remove preflight', (str(exc) if not isinstance(exc, (OSError, UnicodeDecodeError)) else
            'host configuration unreadable')[:300] + '; nothing was changed')
        return 1
    if active and not force:
        say('FAIL', 'Active Claude tasks', f'{active} task(s) running or waiting; stop them first or rerun with --force')
        return 1
    failures = 0
    try:
        changed = registration.apply(ctx.config, partial(registration.remove_text, root=ctx.root), ctx.runtime)
        say('ok', 'MCP entry claude_supervisor', 'removed' if changed else 'not present')
    except (ValueError, OSError) as exc:
        say('FAIL', 'MCP entry', (str(exc) if isinstance(exc, ValueError) else 'host configuration unreadable')
            + '; worker and launcher left unchanged')
        return 1
    if active is not None:
        worker_rpc(ctx, 'shutdown')
        stopped = wait_worker_stopped(ctx)
        say('ok' if stopped else 'warn', 'Worker', 'stopped' if stopped else 'shutdown requested; still responding')
    try:
        outcome = service.remove()
        ps.delete_service_record(ctx.runtime)
        say('ok', f'Worker launcher ({service.kind})', outcome)
    except (ValueError, RuntimeError, OSError) as exc:
        failures += 1
        say('FAIL', 'Worker launcher', str(exc)[:300])
    print('\nThe project folder, .venv, .runtime task history/backups and Claude Code account data were kept.\n'
          'Delete the project folder manually if you no longer need it. Refresh MCP servers in the desktop app.')
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='setup.py', description='Claude Code Supervisor bootstrap (not a packaging script)')
    commands = parser.add_subparsers(dest='command', required=True)
    install_parser = commands.add_parser('install', help='venv, dependencies, worker launcher, MCP entry')
    install_parser.add_argument('--dev', action='store_true', help='also install test dependencies')
    install_parser.add_argument('--no-configure', action='store_true', help='do not edit the host MCP configuration')
    commands.add_parser('configure', help='register only the owned MCP entry')
    doctor_parser = commands.add_parser('doctor', help='diagnose without inference')
    doctor_parser.add_argument('--quick', action='store_true', help='skip the real MCP discovery probe')
    remove_parser = commands.add_parser('remove', help='remove the owned MCP entry and worker launcher')
    remove_parser.add_argument('--force', action='store_true', help='remove even while tasks are active')
    args = parser.parse_args(argv)
    ctx = Context()
    try:
        if args.command == 'install':
            return install(ctx, dev=args.dev, configure=not args.no_configure)
        if args.command == 'configure':
            return configure(ctx)
        if args.command == 'doctor':
            return doctor(ctx, mcp=not args.quick)
        return remove(ctx, force=args.force)
    except (SetupError, ValueError, RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
        say('FAIL', args.command, str(exc)[:500] or type(exc).__name__)
        return 1
