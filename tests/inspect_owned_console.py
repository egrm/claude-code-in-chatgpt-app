"""Inspect only this installation's authenticated worker console, without screen access."""
import ctypes
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
import httpx2
from runtime import identity

info = identity()
response = httpx2.post('http://127.0.0.1:%s/rpc' % info['port'],
    headers={'Authorization':'Bearer '+info['secret']}, json={'method':'health','args':{}}, trust_env=False)
pid = response.json()['result']['pid']
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.FreeConsole()
attached = bool(k.AttachConsole(pid))
result = {'owned_daemon_pid':pid, 'console_attached':attached}
if attached:
    buffer = ctypes.create_unicode_buffer(1024)
    k.GetConsoleTitleW(buffer, len(buffer))
    result['console_title'] = buffer.value
    processes = (ctypes.c_uint32 * 64)()
    count = k.GetConsoleProcessList(processes, 64)
    result['console_process_ids'] = list(processes)[:count]
    k.FreeConsole()
print(json.dumps(result))
