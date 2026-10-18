"""Hide every background child created inside this dedicated worker process.

The SDK currently exposes no Windows creationflags option. Install a scoped
Popen initializer policy in this worker only; no SDK or application files change.
This also covers asyncio.windows_utils.Popen, which subclasses subprocess.Popen.
"""
import inspect
import os
import subprocess


def hide_background_children():
    if os.name != 'nt' or getattr(subprocess.Popen.__init__, '_claude_hidden', False):
        return
    original = subprocess.Popen.__init__
    signature = inspect.signature(original)

    def hidden_init(self, *args, **kwargs):
        call = signature.bind(self, *args, **kwargs)
        call.arguments['creationflags'] = call.arguments.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        startup = call.arguments.get('startupinfo') or subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        call.arguments['startupinfo'] = startup
        original(*call.args, **call.kwargs)

    hidden_init._claude_hidden = True
    subprocess.Popen.__init__ = hidden_init
