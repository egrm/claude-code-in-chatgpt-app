import sys
from pathlib import Path
import tomllib
sys.path.insert(0,str(Path(__file__).parents[1]))


def test_timeout_configuration_preserves_other_servers_and_settings():
    from registration import add_timeouts
    source = '# keep this\nmodel = "example"\n[mcp_servers.other]\ncommand = "other"\ntool_timeout_sec = 45\n[mcp_servers.claude_supervisor]\ncommand = "python"\nargs = ["server.py"]\n'
    result = add_timeouts(source)
    before, after = tomllib.loads(source), tomllib.loads(result)
    target = after['mcp_servers'].pop('claude_supervisor')
    before['mcp_servers'].pop('claude_supervisor')
    assert after == before
    assert target['startup_timeout_sec'] == 30
    assert target['tool_timeout_sec'] == 60
    assert '# keep this' in result
    assert add_timeouts(result) == result


def test_registration_refuses_another_installation_before_overwrite(tmp_path):
    import pytest
    from registration import check_existing
    check_existing('', tmp_path)
    with pytest.raises(ValueError, match='another installation'):
        check_existing('[mcp_servers.claude_supervisor]\ncommand="elsewhere"\nargs=["other.py"]\n', tmp_path)


def test_preserves_empty_args_normalized_away_by_codex_cli(tmp_path):
    import json
    from registration import merge_registration
    original = '# original comment\n[mcp_servers.other]\ncommand="node"\nargs=[]\n'
    target = '\n[mcp_servers.claude_supervisor]\ncommand='+json.dumps(str(tmp_path/'.venv/Scripts/python.exe'))+'\nargs='+json.dumps([str(tmp_path/'server.py')])+'\n'
    registered = original.replace('args=[]\n','') + target
    result = merge_registration(original, registered, tmp_path)
    parsed = tomllib.loads(result)
    assert parsed['mcp_servers']['other']['args'] == []
    assert parsed['mcp_servers']['claude_supervisor']['startup_timeout_sec'] == 30
    assert result.startswith(original)
