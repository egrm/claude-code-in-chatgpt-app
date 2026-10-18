import json
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

import platform_support as ps

ELF = b'\x7fELF\x02\x01\x01' + b'\0'*32
MACHO = b'\xcf\xfa\xed\xfe' + b'\0'*32
PE = b'MZ\x90\x00' + b'\0'*32


def make(path, data, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if executable and os.name != 'nt':
        path.chmod(0o755)
    return path


def test_venv_paths_per_platform(tmp_path):
    assert ps.venv_python(tmp_path, windows=True) == tmp_path/'.venv'/'Scripts'/'python.exe'
    assert ps.venv_python(tmp_path, windowless=True, windows=True) == tmp_path/'.venv'/'Scripts'/'pythonw.exe'
    assert ps.venv_python(tmp_path, windows=False) == tmp_path/'.venv'/'bin'/'python'
    assert ps.venv_python(tmp_path, windowless=True, windows=False) == tmp_path/'.venv'/'bin'/'python'


def test_native_cli_requires_binary_header(tmp_path):
    shell_shim = make(tmp_path/'bin'/'claude', b'#!/bin/sh\nexec node "$basedir/../lib/node_modules/cli.js" "$@"\n')
    assert 'shim' in ps.native_cli_problem(shell_shim, windows=False)
    assert ps.native_cli_problem(make(tmp_path/'elf'/'claude', ELF), windows=False) is None
    assert ps.native_cli_problem(make(tmp_path/'mac'/'claude', MACHO), windows=False) is None
    assert ps.native_cli_problem(make(tmp_path/'w'/'claude.exe', PE), windows=True) is None
    assert ps.native_cli_problem(make(tmp_path/'w'/'claude.cmd', b'@ECHO off\r\nnode cli.js\r\n'), windows=True)
    assert ps.native_cli_problem(make(tmp_path/'w'/'fake.exe', b'#!/bin/sh\n'), windows=True)
    assert ps.native_cli_problem(make(tmp_path/'node_modules'/'x'/'claude.exe', PE), windows=True)
    assert ps.native_cli_problem(tmp_path/'missing', windows=False) == 'not found'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX execute bit')
def test_native_cli_requires_execute_bit(tmp_path):
    path = make(tmp_path/'claude', ELF, executable=False)
    assert ps.native_cli_problem(path, windows=False) == 'not executable'


def test_discovery_prefers_native_default_then_record_then_path(tmp_path, monkeypatch):
    home = tmp_path/'home'
    monkeypatch.setattr(ps.Path, 'home', classmethod(lambda cls: home))
    exe = 'claude.exe' if os.name == 'nt' else 'claude'
    native = ELF if os.name != 'nt' else PE
    shim_dir = tmp_path/'npm'
    make(shim_dir/('claude.cmd' if os.name == 'nt' else 'claude'), b'#!/bin/sh\nnode cli.js\n')
    found, notes = ps.find_claude_cli(search_path=str(shim_dir), recorded='')
    assert found is None
    assert notes and 'shim' in notes[0]
    recorded = make(tmp_path/'opt'/exe, native)
    found, _ = ps.find_claude_cli(search_path=str(shim_dir), recorded=str(recorded))
    assert found == recorded
    default = make(home/'.local'/'bin'/exe, native)
    found, _ = ps.find_claude_cli(search_path=str(shim_dir), recorded=str(recorded))
    assert found == default


def test_exclusive_lock_excludes_second_owner(tmp_path):
    lock = tmp_path/'worker.lock'
    with ps.exclusive_lock(lock, timeout=0):
        with pytest.raises(TimeoutError):
            with ps.exclusive_lock(lock, timeout=0):
                pass
    with ps.exclusive_lock(lock, timeout=0):
        pass


def test_private_write_is_atomic_and_private(tmp_path):
    target = tmp_path/'transport.json'
    ps.private_write(target, '{"a": 1}')
    ps.private_write(target, '{"a": 2}')
    assert json.loads(target.read_text()) == {'a': 2}
    assert [p.name for p in tmp_path.iterdir()] == ['transport.json']
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600


def test_legacy_windows_service_record_compatibility(tmp_path):
    (tmp_path/'service-task.json').write_bytes('\ufeff{"name": "ClaudeCodeSupervisor-S-1", "root": "C:\\\\x"}'.encode('utf-8'))
    assert ps.load_service_record(tmp_path) == {'kind': 'windows-task', 'name': 'ClaudeCodeSupervisor-S-1', 'root': 'C:\\x'}
    ps.save_service_record({'kind': 'windows-task', 'name': 'N', 'root': 'R'}, tmp_path)
    assert json.loads((tmp_path/'service-task.json').read_text()) == {'name': 'N', 'root': 'R'}
    ps.save_service_record({'kind': 'systemd', 'name': 'u.service', 'root': 'R'}, tmp_path)
    assert ps.load_service_record(tmp_path)['kind'] == 'systemd'
    ps.delete_service_record(tmp_path)
    assert ps.load_service_record(tmp_path) is None


def test_same_path_does_not_resolve_venv_symlinks(tmp_path):
    assert ps.same_path(tmp_path/'a'/'..'/'b', tmp_path/'b')
    assert not ps.same_path(tmp_path/'a'/'.venv'/'bin'/'python', tmp_path/'b'/'.venv'/'bin'/'python')
