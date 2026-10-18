"""Private local transport identity, single worker ownership and hidden startup."""
import json
import os
import secrets
import socket
import time

from platform_support import ROOT, RUNTIME, exclusive_lock, load_service_record, private_write

__all__ = ['ROOT', 'RUNTIME', 'private_runtime', 'exclusive_lock', 'identity', 'healthy', 'ensure_worker']


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


def identity():
    path = RUNTIME/'transport.json'
    return json.loads(path.read_text())


def probe(info):
    """Authenticated health result of this installation's worker, or None if unreachable."""
    import httpx2
    try:
        r = httpx2.post(f"http://127.0.0.1:{info['port']}/rpc", headers={'Authorization':'Bearer '+info['secret']},
                        json={'method':'health','args':{}}, timeout=1, trust_env=False)
        result = r.json().get('result', {}) if r.status_code == 200 else {}
        return result if result.get('service') == 'claude-code-supervisor-v1' else None
    except Exception:
        return None


def healthy(info):
    """Reachable and accepting work (pre-lease workers report no state)."""
    status = probe(info)
    return status is not None and status.get('state', 'running') == 'running'


def ensure_worker():
    private_runtime()
    # Longer than a draining predecessor's bounded exit (30 s) plus worker startup (15 s).
    with exclusive_lock(RUNTIME/'launcher.lock', timeout=60):
        path = RUNTIME/'transport.json'
        if not path.exists():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            private_write(path, json.dumps({'port':port, 'secret':secrets.token_urlsafe(32)}))
        info = identity()
        if healthy(info):
            return info
        # A draining worker still holds the port and worker lock: wait for its bounded exit.
        end = time.monotonic()+30
        while (status := probe(info)) is not None:
            if status.get('state', 'running') == 'running':
                return info
            if time.monotonic() >= end:
                raise TimeoutError('Previous supervisor worker is still shutting down; retry shortly')
            time.sleep(.2)
        # The listener closes before a predecessor's bounded teardown ends; launching while it still
        # holds worker.lock would be ignored (IgnoreNew/active unit) or exit at once (detached).
        try:
            with exclusive_lock(RUNTIME/'worker.lock', timeout=max(1, end-time.monotonic())):
                pass
        except TimeoutError:
            raise TimeoutError('Previous supervisor worker is still exiting; retry shortly') from None
        import services
        log = RUNTIME/'daemon.log'
        if log.exists() and log.stat().st_size > 2_000_000:
            log.write_text('')
        record = load_service_record(RUNTIME)
        if record is not None:
            # Registered launcher (Windows task outside the MCP host's job, launchd, systemd, detached).
            process = services.from_record(record, ROOT).start()
        else:
            # Development fallback without setup.
            process = services.spawn_worker(ROOT, log)
        end = time.monotonic()+15
        while time.monotonic() < end:
            if healthy(info):
                return info
            if process is not None and process.poll() is not None:
                raise RuntimeError('Local Claude worker exited during startup; see .runtime/daemon.log')
            time.sleep(.2)
        raise TimeoutError('Local Claude worker did not start within 15 seconds; run: python setup.py doctor')
