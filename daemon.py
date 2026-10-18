"""Single authenticated loopback worker, alive only while leased by MCP clients (see lifecycle.py)."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import inspect
import os
import subprocess
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lifecycle import PROTOCOL, Lifecycle, configured
from runtime import RUNTIME, ROOT, private_runtime, exclusive_lock, identity

METHODS = {'list','status','start','followup','events','wait','answer','request_detail','interrupt','resume','stop','result'}
ACTIVE = {'running', 'waiting_for_permission', 'waiting_for_answer'}


def prepare_background_streams(log_path):
    import sys
    if sys.stdout is None or sys.stderr is None:
        log = open(log_path, 'a', encoding='utf-8', buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def make_app(registry, secret, lifecycle=None):
    life = lifecycle or Lifecycle(**configured())

    def drain(reason):
        """Idempotent: block everything now, then interrupt/close tasks and stop the server."""
        life.state = 'draining'
        if app.state.drain_task is None:
            if app.state.on_drain:
                app.state.on_drain()
            async def run():
                try:
                    await registry.shutdown(reason, timeout=life.close_timeout)
                finally:
                    if app.state.shutdown:
                        app.state.shutdown()
            app.state.drain_task = asyncio.get_running_loop().create_task(run())

    async def watchdog():
        while True:
            await asyncio.sleep(.25)
            if life.idle_expired():
                drain('All ChatGPT/MCP clients disconnected')
                return

    @asynccontextmanager
    async def lifespan(app):
        guard = asyncio.create_task(watchdog())
        yield
        guard.cancel()
        if app.state.on_drain:
            app.state.on_drain()   # external SIGTERM/should_exit teardown is bounded too
        await registry.shutdown('Supervisor worker stopped', timeout=min(1.0, life.close_timeout))

    def refuse(message, status, **extra):
        return JSONResponse({'error': message, **extra}, status_code=status)

    async def rpc(request: Request):
        if not hmac.compare_digest(request.headers.get('authorization',''), 'Bearer '+secret):
            return refuse('Local transport authorization required', 401)
        # No browser access: JSON + capability authorization, no CORS.
        if request.headers.get('origin'):
            return refuse('Browser origins refused', 403)
        body = await request.body()
        if len(body) > 100_000:
            return refuse('Request exceeds size limit', 413)
        try:
            import json
            payload = json.loads(body)
            method, args = payload['method'], payload.get('args', {})
            if life.idle_expired():
                drain('All ChatGPT/MCP clients disconnected')
            if method == 'health':
                return JSONResponse({'result':{'service':'claude-code-supervisor-v1','pid':os.getpid(),
                    'protocol':PROTOCOL,'state':life.state,'clients':life.clients()}})
            if life.state != 'running':
                return refuse('Supervisor worker is shutting down; reconnect and resume tasks explicitly', 503, draining=True)
            if method == 'shutdown':
                # Authenticated administrative stop of this installation's worker (setup/remove).
                drain('Worker shutdown requested')
                return JSONResponse({'result':{'stopping':True}})
            if method == 'admin_tasks':
                states = [r['state'] for r in registry.records.values()]
                return JSONResponse({'result':{'active':sum(s in ACTIVE for s in states), 'total':len(states)}})
            if method in ('lease_acquire', 'lease_heartbeat', 'lease_release'):
                try:
                    if method == 'lease_acquire':
                        result = life.acquire(str(args.get('client', ''))[:200])
                    elif method == 'lease_heartbeat':
                        result = life.heartbeat(args.get('lease_id'))
                    else:
                        result = life.release(args.get('lease_id'))
                except ValueError as exc:
                    if life.state != 'running':
                        drain('All ChatGPT/MCP clients disconnected')
                        return refuse(str(exc), 503, draining=True)
                    return refuse(str(exc), 409)
                return JSONResponse({'result':result})
            if method not in METHODS:
                raise ValueError('Unknown supervisor operation')
            try:
                life.validate(payload.get('lease'))
            except ValueError as exc:
                return refuse(str(exc), 409, lease_required=True)
            result = getattr(registry, method)(**args)
            if inspect.isawaitable(result):
                result = await result
            return JSONResponse({'result':result})
        except (ValueError, TypeError, KeyError, TimeoutError) as exc:
            return refuse(str(exc)[:2000], 400)
        except Exception as exc:
            return refuse(f'Supervisor operation failed: {type(exc).__name__}', 500)

    app = Starlette(routes=[Route('/rpc', rpc, methods=['POST'])], lifespan=lifespan)
    app.state.lifecycle = life
    app.state.drain_task = None
    app.state.shutdown = None
    app.state.on_drain = None
    return app


def child_job():
    # Closing the worker also terminates its Claude/shell descendants on Windows.
    import win32api
    import win32job
    import uuid
    job = win32job.CreateJobObject(None, 'ClaudeSupervisor-' + str(uuid.uuid4()))
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    return job


def posix_exit_on_sigterm():
    # uvicorn restores this handler and re-raises SIGTERM after shutting down; exit
    # through SystemExit so the final group cleanup still runs.
    import signal
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)


def _group_members(pgid):
    """Other live processes in our group (reaping our own zombies first), or None if unknown."""
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass
    me = os.getpid()
    try:
        out = subprocess.run(['pgrep', '-g', str(pgid)], capture_output=True, text=True, timeout=5).stdout
        return [int(p) for p in out.split() if int(p) != me]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if os.path.isdir('/proc'):
        members = []
        for entry in os.listdir('/proc'):
            if entry.isdigit() and int(entry) != me:
                try:
                    with open(f'/proc/{entry}/stat') as f:
                        fields = f.read().rsplit(')', 1)[1].split()
                    if int(fields[2]) == pgid and fields[0] != 'Z':
                        members.append(int(entry))
                except (OSError, IndexError, ValueError):
                    continue
        return members
    return None


def posix_group_cleanup(term_grace=2.0):
    """Terminate remaining Claude/shell descendants, only when this worker leads its own group
    (systemd/launchd service, or the detached session started by runtime.ensure_worker):
    SIGTERM, bounded wait, then SIGKILL for members that ignored it."""
    import signal
    if os.name == 'nt' or os.getpgrp() != os.getpid():
        return False
    pgid = os.getpgrp()
    # TERM members individually: a group-wide SIGTERM would hit this leader's (restored) handler
    # and abort cleanup from any thread. Thread-safe; no signal dispositions change.
    members = _group_members(pgid)
    for pid in members or []:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.monotonic() + term_grace
    while members and time.monotonic() < end:
        time.sleep(.1)
        members = _group_members(pgid)
    if members == []:
        return True
    members = _group_members(pgid)
    if members is None:
        os.killpg(pgid, signal.SIGKILL)   # last resort; includes this exiting leader
    for pid in members:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _group_members(pgid)
    return True


HARD_EXIT_MARGIN_S = 4.0


def hard_exit():
    """Owned-process end: POSIX own-group TERM/KILL, then exit; on Windows exit closes the kill-on-close job."""
    try:
        posix_group_cleanup()
    finally:
        os._exit(0)


def serve(registry, info, lifecycle=None, hard_exit_margin=HARD_EXIT_MARGIN_S):
    """Run the worker. From the moment a drain or server shutdown begins, a timer ends this process
    within close_timeout + margin even if a Claude close resists cancellation or loop teardown hangs."""
    import threading
    import uvicorn
    job = child_job() if os.name == 'nt' else None
    if job is None:
        posix_exit_on_sigterm()
    app = make_app(registry, info['secret'], lifecycle)
    life = app.state.lifecycle
    service = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=info['port'], log_level='warning',
                                            access_log=False, timeout_graceful_shutdown=2))
    app.state.shutdown = lambda: setattr(service, 'should_exit', True)
    armed = []
    def arm():
        if armed:
            return
        timer = threading.Timer(life.close_timeout + hard_exit_margin, hard_exit)
        timer.daemon = True
        timer.start()
        armed.append(timer)
    app.state.on_drain = arm
    try:
        service.run()
    finally:
        # uvicorn re-raises SIGTERM after lifespan shutdown (SystemExit here); always clean up and exit.
        hard_exit()


if __name__ == '__main__':
    private_runtime()
    prepare_background_streams(RUNTIME/'daemon.log')
    from policy import sanitize_process_environment
    from registry import Registry
    from windows_process import hide_background_children
    sanitize_process_environment()
    hide_background_children()
    private_runtime()
    with exclusive_lock(RUNTIME/'worker.lock', timeout=0):
        serve(Registry(RUNTIME/'tasks.sqlite'), identity())
