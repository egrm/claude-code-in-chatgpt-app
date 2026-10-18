"""Stdlib-only platform adapters shared by the installer, facade and worker."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
WINDOWS = os.name == 'nt'
MACOS = sys.platform == 'darwin'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
MIN_PYTHON = (3, 11)
SHIM_SUFFIXES = ('.cmd', '.bat', '.ps1', '.js', '.mjs', '.cjs')
# ELF; Mach-O 32/64 both byte orders; universal (fat) binaries.
POSIX_MAGIC = (b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe',
               b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf')


def venv_python(root=ROOT, windowless=False, windows=WINDOWS):
    """MCP stdio uses python(.exe); the Windows worker uses pythonw.exe."""
    if windows:
        return Path(root) / '.venv' / 'Scripts' / ('pythonw.exe' if windowless else 'python.exe')
    return Path(root) / '.venv' / 'bin' / 'python'


def same_path(a, b):
    # Deliberately no symlink resolution: POSIX venv interpreters all link to one base Python.
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def default_claude_cli(windows=WINDOWS):
    return Path.home() / '.local' / 'bin' / ('claude.exe' if windows else 'claude')


def native_cli_problem(path, windows=WINDOWS):
    """Return why a path is not the native Claude Code executable, or None."""
    path = Path(path)
    if not path.is_file():
        return 'not found'
    real = Path(os.path.realpath(path))
    names = {p.suffix.lower() for p in (path, real)}
    if names & set(SHIM_SUFFIXES) or 'node_modules' in [part.lower() for part in real.parts]:
        return 'npm/script shim, not the native executable'
    try:
        with open(real, 'rb') as f:
            head = f.read(4)
    except OSError:
        return 'not readable'
    if windows:
        if path.suffix.lower() != '.exe' or head[:2] != b'MZ':
            return 'npm/script shim, not the native executable'
    else:
        if head not in POSIX_MAGIC:
            return 'npm/script shim, not the native executable'
        if not os.access(real, os.X_OK):
            return 'not executable'
    return None


def install_record(runtime=RUNTIME):
    try:
        return json.loads((Path(runtime) / 'install.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def find_claude_cli(search_path=None, recorded=None):
    """Native ~/.local/bin first, then the setup-recorded path, then PATH. Returns (path|None, notes)."""
    notes = []
    candidates = [default_claude_cli()]
    if recorded is None:
        recorded = install_record().get('claude_cli')
    if recorded:
        candidates.append(Path(recorded))
    found = shutil.which('claude', path=search_path)
    if found:
        candidates.append(Path(found))
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        problem = native_cli_problem(candidate)
        if problem is None:
            return candidate, notes
        if problem != 'not found':
            notes.append(f'{candidate}: {problem}')
    return None, notes


def claude_cli():
    path, _ = find_claude_cli()
    return path or default_claude_cli()


def git_bash(windows=WINDOWS):
    """Claude's Bash tool on Windows needs Git for Windows' bash.exe (never WSL's System32 bash)."""
    if not windows:
        found = shutil.which('bash')
        return Path(found) if found else None
    candidates = []
    git = shutil.which('git')
    if git:
        candidates += [parent / 'bin' / 'bash.exe' for parent in list(Path(os.path.realpath(git)).parents)[:3]]
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramW6432'), os.environ.get('LOCALAPPDATA')):
        if base:
            candidates += [Path(base) / 'Git' / 'bin' / 'bash.exe', Path(base) / 'Programs' / 'Git' / 'bin' / 'bash.exe']
    system = os.path.normcase(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32'))
    for candidate in candidates:
        if candidate.is_file() and not os.path.normcase(str(candidate)).startswith(system):
            return candidate
    return None


def codex_config_path(environ=None):
    environ = os.environ if environ is None else environ
    home = environ.get('CODEX_HOME')
    return (Path(home) if home else Path.home() / '.codex') / 'config.toml'


def private_write(path, data, mode=0o600):
    """Atomically replace a small private file; never briefly world-readable on POSIX."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode('utf-8')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp')
    try:
        if not WINDOWS:
            os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextmanager
def exclusive_lock(path, timeout=10):
    with open(path, 'a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'\0')
            f.flush()
        end = time.monotonic()+timeout
        while True:
            try:
                _lock(f)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise TimeoutError('Another supervisor owns this local worker lock')
                time.sleep(.1)
        try:
            yield
        finally:
            _unlock(f)


if WINDOWS:
    def _lock(f):
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f):
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    def _lock(f):
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f):
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_service_record(runtime=RUNTIME):
    """service.json (POSIX kinds) or the original Windows service-task.json {name, root}."""
    current = Path(runtime) / 'service.json'
    legacy = Path(runtime) / 'service-task.json'
    if current.exists():
        return json.loads(current.read_text(encoding='utf-8-sig'))
    if legacy.exists():
        return {'kind': 'windows-task', **json.loads(legacy.read_text(encoding='utf-8-sig'))}
    return None


def save_service_record(record, runtime=RUNTIME):
    runtime = Path(runtime)
    if record['kind'] == 'windows-task':
        # Same {name, root} shape older checkouts read.
        private_write(runtime / 'service-task.json', json.dumps({'name': record['name'], 'root': record['root']}, indent=2))
    else:
        private_write(runtime / 'service.json', json.dumps(record, indent=2))


def delete_service_record(runtime=RUNTIME):
    for name in ('service.json', 'service-task.json'):
        try:
            (Path(runtime) / name).unlink()
        except FileNotFoundError:
            pass
