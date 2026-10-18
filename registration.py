"""Adjust only the owned MCP table's supported timeout keys, preserving all else."""
import os
import copy
import json
from pathlib import Path
import re
import tempfile
import tomllib


def check_existing(source, root):
    target = tomllib.loads(source).get('mcp_servers', {}).get('claude_supervisor')
    if target is None:
        return
    if Path(target.get('command','')).resolve() != (root/'.venv/Scripts/python.exe').resolve() or target.get('args') != [str(root/'server.py')]:
        raise ValueError('MCP name belongs to another installation; refusing to change it')


def add_timeouts(source):
    original = tomllib.loads(source)
    match = re.search(r'(?m)^\[mcp_servers\.claude_supervisor\][ \t]*\r?$', source)
    if not match:
        raise ValueError('Expected claude_supervisor MCP table was not found')
    start = source.find('\n', match.end())
    start = len(source) if start < 0 else start+1
    following = re.search(r'(?m)^\[', source[start:])
    end = start+following.start() if following else len(source)
    body = source[start:end]
    body = re.sub(r'(?m)^(startup_timeout_sec|tool_timeout_sec)[ \t]*=.*\n?', '', body)
    newline = '\r\n' if '\r\n' in source else '\n'
    addition = 'startup_timeout_sec = 30'+newline+'tool_timeout_sec = 60'+newline
    if body and not body.endswith('\n'):
        body += newline
    result = source[:start] + body + addition + source[end:]
    updated = tomllib.loads(result)
    old_target = original['mcp_servers'].pop('claude_supervisor')
    new_target = updated['mcp_servers'].pop('claude_supervisor')
    expected = {**old_target, 'startup_timeout_sec':30, 'tool_timeout_sec':60}
    if original != updated or new_target != expected:
        raise ValueError('Refusing unexpected configuration changes')
    return result


def merge_registration(original, registered, root):
    before, current = tomllib.loads(original), tomllib.loads(registered)
    check_existing(original, root)
    check_existing(registered, root)
    # The native CLI omits other servers' empty args arrays when rewriting TOML.
    # Restore original text only after proving no other semantic edits occurred.
    def normalized(value):
        value = copy.deepcopy(value)
        servers = value.setdefault('mcp_servers', {})
        servers.pop('claude_supervisor', None)
        for server in servers.values():
            if server.get('args') == []:
                server.pop('args')
        return value
    if normalized(before) != normalized(current):
        raise ValueError('Concurrent or unexpected unrelated configuration changes; refusing overwrite')
    if 'claude_supervisor' in before.get('mcp_servers', {}):
        return add_timeouts(original)
    target = current['mcp_servers']['claude_supervisor']
    if not set(target).issubset({'command','args','startup_timeout_sec','tool_timeout_sec'}):
        raise ValueError('Unexpected fields in newly registered MCP table')
    block = '\n[mcp_servers.claude_supervisor]\ncommand = '+json.dumps(target['command'])+'\nargs = '+json.dumps(target['args'])+'\nstartup_timeout_sec = 30\ntool_timeout_sec = 60\n'
    result = original + ('' if original.endswith('\n') else '\n') + block
    expected = copy.deepcopy(before)
    expected.setdefault('mcp_servers', {})['claude_supervisor'] = {**target, 'startup_timeout_sec':30, 'tool_timeout_sec':60}
    if tomllib.loads(result) != expected:
        raise ValueError('Registration preservation check failed')
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    config = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'config.toml'
    import sys
    if '--check-existing' in sys.argv:
        check_existing(config.read_text(encoding='utf-8') if config.exists() else '', root)
        sys.exit(0)
    source = config.read_text(encoding='utf-8')
    check_existing(source, root)
    if '--preserve-from' in sys.argv:
        backup = Path(sys.argv[sys.argv.index('--preserve-from')+1])
        result = merge_registration(backup.read_text(encoding='utf-8'), source, root)
    else:
        result = add_timeouts(source)
    # Backup is created by setup.ps1 before the supported CLI registration.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config.parent, delete=False, suffix='.tmp') as f:
        f.write(result)
        temporary = f.name
    os.replace(temporary, config)
    print('Owned MCP startup/tool timeouts configured: 30/60 seconds')
