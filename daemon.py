"""Single authenticated loopback worker, shared across MCP clients/reconnects."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import inspect
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from runtime import RUNTIME, ROOT, private_runtime, exclusive_lock, identity

METHODS = {'list','status','start','followup','events','wait','answer','request_detail','interrupt','resume','stop','result'}


def prepare_background_streams(log_path):
    import sys
    if sys.stdout is None or sys.stderr is None:
        log = open(log_path, 'a', encoding='utf-8', buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


def make_app(registry, secret):
    @asynccontextmanager
    async def lifespan(app):
        yield
        await registry.close()

    async def rpc(request: Request):
        if not hmac.compare_digest(request.headers.get('authorization',''), 'Bearer '+secret):
            return JSONResponse({'error':'Local transport authorization required'}, status_code=401)
        # No browser access: JSON + capability authorization, no CORS.
        if request.headers.get('origin'):
            return JSONResponse({'error':'Browser origins refused'}, status_code=403)
        body = await request.body()
        if len(body) > 100_000:
            return JSONResponse({'error':'Request exceeds size limit'}, status_code=413)
        try:
            import json
            payload = json.loads(body)
            method, args = payload['method'], payload.get('args', {})
            if method == 'health':
                return JSONResponse({'result':{'service':'claude-code-supervisor-v1','pid':os.getpid()}})
            if method == 'shutdown' and getattr(app.state, 'shutdown', None):
                asyncio.get_running_loop().call_later(.2, app.state.shutdown)
                return JSONResponse({'result':{'stopping':True}})
            if method not in METHODS:
                raise ValueError('Unknown supervisor operation')
            result = getattr(registry, method)(**args)
            if inspect.isawaitable(result):
                result = await result
            return JSONResponse({'result':result})
        except (ValueError, TypeError, KeyError, TimeoutError) as exc:
            return JSONResponse({'error':str(exc)[:2000]}, status_code=400)
        except Exception as exc:
            return JSONResponse({'error':f'Supervisor operation failed: {type(exc).__name__}'}, status_code=500)

    app = Starlette(routes=[Route('/rpc', rpc, methods=['POST'])], lifespan=lifespan)
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
    # through SystemExit so group cleanup below still runs.
    import signal
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)


def posix_group_cleanup():
    """Terminate remaining Claude/shell descendants, only when this worker leads its own group
    (systemd/launchd service, or the detached session started by runtime.ensure_worker)."""
    import signal
    if os.name == 'nt' or os.getpgrp() != os.getpid():
        return False
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        return False
    return True


if __name__ == '__main__':
    private_runtime()
    prepare_background_streams(RUNTIME/'daemon.log')
    import uvicorn
    from policy import sanitize_process_environment
    from registry import Registry
    from windows_process import hide_background_children
    sanitize_process_environment()
    hide_background_children()
    private_runtime()
    with exclusive_lock(RUNTIME/'worker.lock', timeout=0):
        if os.name == 'nt':
            job = child_job()
        else:
            posix_exit_on_sigterm()
        info = identity()
        app = make_app(Registry(RUNTIME/'tasks.sqlite'), info['secret'])
        service = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=info['port'], log_level='warning', access_log=False))
        app.state.shutdown = lambda: setattr(service, 'should_exit', True)
        try:
            service.run()
        finally:
            posix_group_cleanup()
