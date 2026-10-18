"""Private local transport identity, single worker ownership and hidden startup."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'


def private_runtime():
    RUNTIME.mkdir(exist_ok=True)
    if os.name == 'nt':
        import win32api
        import win32security
        import ntsecuritycon
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        flags = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
        system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, ntsecuritycon.FILE_ALL_ACCESS, system)
        win32security.SetNamedSecurityInfo(str(RUNTIME), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    else:
        RUNTIME.chmod(0o700)
    return RUNTIME


@contextmanager
def exclusive_lock(path, timeout=10):
    import msvcrt
    with open(path, 'a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'\0')
            f.flush()
        end = time.monotonic()+timeout
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= end:
                    raise TimeoutError('Another supervisor owns this local worker lock')
                time.sleep(.1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def identity():
    path = RUNTIME/'transport.json'
    return json.loads(path.read_text())


def healthy(info):
    import httpx2
    try:
        r = httpx2.post(f"http://127.0.0.1:{info['port']}/rpc", headers={'Authorization':'Bearer '+info['secret']},
                        json={'method':'health','args':{}}, timeout=1, trust_env=False)
        return r.status_code == 200 and r.json().get('result', {}).get('service') == 'claude-code-supervisor-v1'
    except Exception:
        return False


def ensure_worker():
    private_runtime()
    with exclusive_lock(RUNTIME/'launcher.lock'):
        path = RUNTIME/'transport.json'
        if not path.exists():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            path.write_text(json.dumps({'port':port, 'secret':secrets.token_urlsafe(32)}))
        info = identity()
        if healthy(info):
            return info
        from policy import clean_environment
        log = RUNTIME/'daemon.log'
        if log.exists() and log.stat().st_size > 2_000_000:
            log.write_text('')
        scheduler = RUNTIME/'service-task.json'
        process = None
        if scheduler.exists():
            task = json.loads(scheduler.read_text(encoding='utf-8-sig'))
            if Path(task['root']).resolve() != ROOT:
                raise ValueError('Scheduled worker belongs to another installation; rerun setup')
            result = subprocess.run([str(Path(os.environ['SystemRoot'])/'System32'/'schtasks.exe'),
                '/Run', '/TN', task['name']], capture_output=True, timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError('Could not start the on-demand worker task; rerun setup.ps1')
        else:
            with log.open('ab') as output:
                process = subprocess.Popen([sys.executable, str(ROOT/'daemon.py')], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=clean_environment(os.environ),
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        end = time.monotonic()+15
        while time.monotonic() < end:
            if healthy(info):
                return info
            if process is not None and process.poll() is not None:
                raise RuntimeError('Local Claude worker exited during startup; see .runtime/daemon.log')
            time.sleep(.2)
        raise TimeoutError('Local Claude worker did not start within 15 seconds')
