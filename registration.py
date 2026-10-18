"""Exact-table editing of the owned [mcp_servers.claude_supervisor] host entry.

Stdlib only so it runs before the venv exists. Edits splice text, leaving every
other byte unchanged, then prove the parsed result differs only as intended.
Error messages never echo configuration values.
"""
import codecs
import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import tomllib

from platform_support import ROOT, WINDOWS, private_write, same_path, venv_python

NAME = 'claude_supervisor'
TIMEOUTS = {'startup_timeout_sec': 30, 'tool_timeout_sec': 60}
FOREIGN = 'MCP name claude_supervisor belongs to another installation; refusing to change it'
NOT_TABLE = ('claude_supervisor is not a plain [mcp_servers.claude_supervisor] table; '
             'edit it manually or via the app MCP settings')
HEADER = re.compile(r'^[ \t]*\[[ \t]*mcp_servers[ \t]*\.[ \t]*(?:claude_supervisor|"claude_supervisor"|\'claude_supervisor\')'
                    r'[ \t]*(\.[^\]\r\n]*)?\][ \t]*(?:#[^\r\n]*)?\r?$', re.M)
ANY_HEADER = re.compile(r'^[ \t]*\[', re.M)


def desired_entry(root=ROOT):
    return {'command': str(venv_python(root)), 'args': [str(Path(root) / 'server.py')]}


def _parse(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError('Host configuration is not valid TOML; left unchanged') from None


def _target(parsed):
    servers = parsed.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('mcp_servers is not a table; refusing to edit host configuration')
    return servers.get(NAME)


def owned_state(text, root=ROOT):
    target = _target(_parse(text) if isinstance(text, str) else text)
    if target is None:
        return 'absent'
    want = desired_entry(root)
    args = target.get('args') if isinstance(target, dict) else None
    if (isinstance(target, dict) and isinstance(target.get('command'), str) and same_path(target['command'], want['command'])
            and isinstance(args, list) and len(args) == 1 and isinstance(args[0], str) and same_path(args[0], want['args'][0])):
        return 'owned'
    return 'foreign'


def entry_status(text, root=ROOT):
    parsed = _parse(text)
    state = owned_state(parsed, root)
    if state != 'owned':
        return {'state': state}
    target = _target(parsed)
    return {'state': 'owned', **{k: target.get(k) for k in TIMEOUTS}, 'enabled': target.get('enabled', True) is not False}


def _quote(value):
    if "'" not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "'" + value + "'"
    return json.dumps(value, ensure_ascii=False)


def _newline(text):
    return '\r\n' if '\r\n' in text else '\n'


def _blocks(text):
    """(header_start, body_start, content_end, is_main) for the target table and its subtables.

    content_end excludes trailing blank/comment lines, which belong to the next table.
    """
    spans = []
    for match in HEADER.finditer(text):
        body = text.find('\n', match.end())
        body = len(text) if body < 0 else body+1
        following = ANY_HEADER.search(text, body)
        limit = following.start() if following else len(text)
        end = position = body
        for line in text[body:limit].splitlines(keepends=True):
            position += len(line)
            if line.strip() and not line.lstrip().startswith('#'):
                end = position
        spans.append((match.start(), body, end, match.group(1) is None))
    return spans


def _without_target(parsed):
    value = copy.deepcopy(parsed)
    servers = value.get('mcp_servers')
    if isinstance(servers, dict):
        servers.pop(NAME, None)
        if not servers:
            value.pop('mcp_servers')
    return value


def _verify(result, expected):
    if _parse(result) != expected:
        raise ValueError('Refusing unexpected configuration changes; left unchanged')
    return result


def configure_text(text, root=ROOT):
    parsed = _parse(text)
    state = owned_state(parsed, root)
    if state == 'foreign':
        raise ValueError(FOREIGN)
    newline = _newline(text)
    expected = copy.deepcopy(parsed)
    if state == 'absent':
        entry = desired_entry(root)
        block = newline.join(['[mcp_servers.claude_supervisor]', 'command = ' + _quote(entry['command']),
            'args = [' + ', '.join(_quote(a) for a in entry['args']) + ']',
            *(f'{k} = {v}' for k, v in TIMEOUTS.items())]) + newline
        separator = ('' if not text or text.endswith('\n') else newline) + (newline if text.strip() else '')
        if not isinstance(expected.setdefault('mcp_servers', {}), dict):
            raise ValueError('mcp_servers is not a table; refusing to edit host configuration')
        expected['mcp_servers'][NAME] = {**entry, **TIMEOUTS}
        try:
            return _verify(text + separator + block, expected)
        except ValueError:
            # e.g. mcp_servers defined inline cannot be extended by a table header.
            raise ValueError(NOT_TABLE) from None
    target = expected['mcp_servers'][NAME]
    missing = {k: v for k, v in TIMEOUTS.items() if k not in target}
    if not missing:
        return text
    main = [span for span in _blocks(text) if span[3]]
    if len(main) != 1:
        raise ValueError(NOT_TABLE)
    _, body, end, _ = main[0]
    insertion = ''.join(f'{k} = {v}{newline}' for k, v in missing.items())
    prefix = text[:end]
    if end > body and not prefix.endswith('\n'):
        insertion = newline + insertion
    target.update(missing)
    return _verify(prefix + insertion + text[end:], expected)


def remove_text(text, root=ROOT):
    parsed = _parse(text)
    state = owned_state(parsed, root)
    if state == 'absent':
        return text
    if state == 'foreign':
        raise ValueError(FOREIGN)
    spans = _blocks(text)
    if [s[3] for s in spans].count(True) != 1:
        raise ValueError(NOT_TABLE)
    for start, _, end, _ in reversed(spans):
        before, after = text[:start], text[end:]
        if not after.strip():
            # Undo the blank separator configure_text added when appending.
            after = ''
            if re.search(r'\n[ \t]*\r?\n\Z', before):
                before = re.sub(r'\r?\n\Z', '', before)
        elif not before.strip() or re.search(r'\n[ \t]*\r?\n\Z', before):
            after = re.sub(r'\A(?:[ \t]*\r?\n)+', '', after)
        text = before + after
    return _verify(text, _without_target(parsed))


def apply(config, transform, backup_dir):
    """Read, transform, back up and atomically replace. Returns whether anything changed."""
    config = Path(config)
    existed = config.exists()
    raw = config.read_bytes() if existed else b''
    bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b''
    try:
        text = raw[len(bom):].decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Host configuration is not UTF-8; left unchanged') from None
    _parse(text)
    result = transform(text)
    if result == text:
        return False
    data = bom + result.encode('utf-8')
    current = config.read_bytes() if config.exists() else None
    if current != (raw if existed else None):
        raise ValueError('Host configuration changed during update; nothing written, rerun the command')
    if existed:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = Path(backup_dir) / f'config.toml.backup-{stamp}'
        counter = 1
        while backup.exists():
            backup = Path(backup_dir) / f'config.toml.backup-{stamp}-{counter}'
            counter += 1
        private_write(backup, raw)
        mode = stat.S_IMODE(config.stat().st_mode)
    else:
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = 0o600
    fd, temporary = tempfile.mkstemp(dir=config.parent, prefix='.config.toml.', suffix='.tmp')
    try:
        if not WINDOWS:
            os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, config)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True
