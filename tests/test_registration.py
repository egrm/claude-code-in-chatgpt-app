import os
import sys
from pathlib import Path
import tomllib
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))

import registration as reg
from functools import partial

ROOT = Path('C:/inst' if os.name == 'nt' else '/opt/inst')
configure = partial(reg.configure_text, root=ROOT)
remove = partial(reg.remove_text, root=ROOT)


def entry(root=ROOT):
    return reg.desired_entry(root)


def owned_table(root=ROOT, extra=''):
    e = entry(root)
    return f"[mcp_servers.claude_supervisor]\ncommand = '{e['command']}'\nargs = ['{e['args'][0]}']\n{extra}"


def test_configure_appends_exact_table_and_is_idempotent():
    source = '# keep this\nmodel = "example"\n\n[mcp_servers.other]\ncommand = "other"\nargs = []\n'
    result = configure(source)
    assert result.startswith(source)
    parsed = tomllib.loads(result)
    assert parsed['mcp_servers']['other'] == {'command': 'other', 'args': []}
    assert parsed['mcp_servers']['claude_supervisor'] == {**entry(), 'startup_timeout_sec': 30, 'tool_timeout_sec': 60}
    assert configure(result) == result


def test_configure_creates_from_empty():
    result = configure('')
    assert tomllib.loads(result) == {'mcp_servers': {'claude_supervisor': {**entry(), 'startup_timeout_sec': 30, 'tool_timeout_sec': 60}}}


def test_owned_table_gets_only_missing_timeouts_and_neighbours_keep_comments():
    source = '[a]\nx = 1\n' + owned_table(extra='enabled = false\ntool_timeout_sec = 90\n') + '\n# comment for next\n[mcp_servers.next]\ncommand = "n"\n'
    result = configure(source)
    parsed = tomllib.loads(result)['mcp_servers']['claude_supervisor']
    assert parsed['enabled'] is False and parsed['tool_timeout_sec'] == 90 and parsed['startup_timeout_sec'] == 30
    assert '\n# comment for next\n[mcp_servers.next]' in result
    assert result.replace('startup_timeout_sec = 30\n', '', 1) == source
    assert configure(result) == result


def test_codex_cli_style_double_quoted_entry_is_owned():
    e = entry()
    import json
    source = '[mcp_servers.claude_supervisor]\ncommand = ' + json.dumps(e['command']) + '\nargs = ' + json.dumps(e['args']) + '\nstartup_timeout_sec = 30\ntool_timeout_sec = 60\n'
    assert reg.owned_state(source, ROOT) == 'owned'
    assert configure(source) == source


def test_refuses_other_installation_without_echoing_values():
    secret = 'sk-ant-do-not-print'
    source = f'[mcp_servers.claude_supervisor]\ncommand = "elsewhere"\nargs = ["other.py"]\nenv = {{ ANTHROPIC_API_KEY = "{secret}" }}\n'
    with pytest.raises(ValueError, match='another installation') as error:
        configure(source)
    assert secret not in str(error.value) and 'elsewhere' not in str(error.value)
    with pytest.raises(ValueError, match='another installation'):
        remove(source)


@pytest.mark.parametrize('source', [
    'mcp_servers = { claude_supervisor = { command = "x" } }\n',
    '[mcp_servers]\nclaude_supervisor.command = "x"\n',
])
def test_refuses_non_table_forms(source):
    with pytest.raises(ValueError):
        configure(source)


def test_inline_parent_without_target_is_refused_not_corrupted():
    with pytest.raises(ValueError):
        configure('mcp_servers = { other = { command = "x" } }\n')


def test_remove_restores_original_bytes_and_preserves_unrelated():
    original = '# top\n[mcp_servers.other]\ncommand = "o"\nargs = []\n'
    configured = configure(original)
    assert remove(configured) == original
    assert remove(original) == original


def test_remove_owned_table_with_subtable_keeps_following_comment():
    source = owned_table(extra='\n[mcp_servers.claude_supervisor.env]\nX = "1"\n') + '\n# next\n[z]\nk = 1\n'
    result = remove(source)
    assert tomllib.loads(result) == {'z': {'k': 1}}
    assert result.endswith('# next\n[z]\nk = 1\n')


def test_apply_preserves_bom_crlf_backs_up_and_reports_change(tmp_path):
    config = tmp_path/'codex'/'config.toml'
    config.parent.mkdir()
    original = '\ufeff# c\r\n[mcp_servers.other]\r\ncommand = "o"\r\n'.encode('utf-8')
    config.write_bytes(original)
    backups = tmp_path/'backups'
    backups.mkdir()
    assert reg.apply(config, configure, backups) is True
    data = config.read_bytes()
    assert data.startswith(original)
    assert b'\n' not in data.replace(b'\r\n', b'')
    assert [p.read_bytes() for p in backups.iterdir()] == [original]
    assert reg.apply(config, configure, backups) is False
    assert len(list(backups.iterdir())) == 1
    assert reg.apply(config, remove, backups) is True
    assert config.read_bytes() == original


def test_apply_creates_missing_config_without_backup(tmp_path):
    config = tmp_path/'new'/'config.toml'
    backups = tmp_path/'b'
    backups.mkdir()
    assert reg.apply(config, configure, backups) is True
    assert 'claude_supervisor' in tomllib.loads(config.read_text())['mcp_servers']
    assert list(backups.iterdir()) == []
    assert reg.apply(tmp_path/'absent'/'config.toml', remove, backups) is False
    assert not (tmp_path/'absent').exists()


def test_apply_refuses_concurrent_modification(tmp_path, monkeypatch):
    config = tmp_path/'config.toml'
    config.write_text('a = 1\n')
    def racing(text):
        config.write_text('a = 2\n')
        return configure(text)
    with pytest.raises(ValueError, match='changed'):
        reg.apply(config, racing, tmp_path)
    assert config.read_text() == 'a = 2\n'


def test_apply_refuses_invalid_toml_untouched(tmp_path):
    config = tmp_path/'config.toml'
    config.write_bytes(b'[broken\n')
    with pytest.raises(ValueError):
        reg.apply(config, reg.configure_text, tmp_path)
    assert config.read_bytes() == b'[broken\n'


def test_entry_status_reports_without_values():
    assert reg.entry_status('', ROOT) == {'state': 'absent'}
    status = reg.entry_status(owned_table(), ROOT)
    assert status == {'state': 'owned', 'startup_timeout_sec': None, 'tool_timeout_sec': None, 'enabled': True}
    assert reg.entry_status('[mcp_servers.claude_supervisor]\ncommand = "x"\n', ROOT) == {'state': 'foreign'}
