import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows background window policy')
def test_sdk_style_children_have_no_console_window():
    result = subprocess.run([sys.executable, '-c',
        "import subprocess,sys; from windows_process import hide_background_children; hide_background_children(); "
        "r=subprocess.run([sys.executable,'-c','import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())'],capture_output=True,text=True); print(r.stdout.strip())"],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0'


@pytest.mark.skipif(os.name != 'nt', reason='Windows async SDK subprocess policy')
def test_asyncio_sdk_children_have_no_console_window():
    program = '''import asyncio,sys
from windows_process import hide_background_children
hide_background_children()
async def main():
    p=await asyncio.create_subprocess_exec(sys.executable,'-c','import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())',stdout=asyncio.subprocess.PIPE)
    out,_=await p.communicate()
    print(out.decode().strip())
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=Path(__file__).parents[1],
        capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0'


def test_background_logging_handles_pythonw_missing_streams(tmp_path):
    program = '''import sys
from daemon import prepare_background_streams
sys.stdout=None
sys.stderr=None
prepare_background_streams(sys.argv[1])
import uvicorn
uvicorn.Config('daemon:app')
print('LOGGING_READY',flush=True)
'''
    log = tmp_path/'worker.log'
    result = subprocess.run([sys.executable, '-c', program, str(log)], cwd=Path(__file__).parents[1],
        capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode == 0, result.stderr
    assert 'LOGGING_READY' in log.read_text()
