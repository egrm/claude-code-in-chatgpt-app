import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))


def test_subscription_env_removes_paid_routes_without_reading_credentials():
    from policy import clean_environment, validate_auth
    clean = clean_environment({'PATH': 'path', 'USERPROFILE': 'home', 'ANTHROPIC_API_KEY': 'secret', 'anthropic_base_url': 'proxy', 'CLAUDE_CODE_USE_BEDROCK': '1', 'AWS_SECRET_ACCESS_KEY': 'secret', 'CLAUDE_CONFIG_DIR': 'other'})
    assert clean == {'PATH': 'path', 'USERPROFILE': 'home'}
    validate_auth({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'max'})
    for auth in ({}, {'loggedIn': True, 'authMethod': 'api_key'}, {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty', 'subscriptionType': 'console'}):
        with pytest.raises(ValueError):
            validate_auth(auth)


def test_file_tools_confine_resolved_paths_and_deny_credentials(tmp_path):
    from policy import check_file_scope
    assert check_file_scope(tmp_path, 'Write', {'file_path': str(tmp_path/'a.txt')}) is None
    assert check_file_scope(tmp_path, 'Read', {'file_path': str(tmp_path.parent/'outside')})
    assert check_file_scope(tmp_path, 'Read', {'file_path': str(tmp_path/'.env')})
    assert check_file_scope(tmp_path, 'Write', {'file_path': str(tmp_path/'.claude'/'settings.json')})
    assert check_file_scope(tmp_path, 'Read', {'file_path': ''})


def test_unsafe_shell_is_never_automatically_approved(tmp_path):
    from policy import check_file_scope
    assert check_file_scope(tmp_path, 'Bash', {'command': 'echo test'}) is None
    # Scope function permits it to reach the explicit approval gate, not execute.


def test_overage_evidence_must_reject_paid_usage():
    from policy import check_billing_event
    assert check_billing_event({'status': 'allowed','isUsingOverage': False,'overageStatus': 'rejected','overageDisabledReason': 'org_level_disabled'}) is None
    assert check_billing_event({'status': 'allowed','isUsingOverage': True})
    assert check_billing_event({'status': 'allowed','overageStatus': 'allowed'})
    assert check_billing_event({'status': 'allowed','overageStatus': 'allowed_warning'})


def test_search_cannot_read_protected_descendants(tmp_path):
    from policy import check_file_scope
    (tmp_path/'.env').write_text('SECRET=fixture')
    (tmp_path/'safe.py').write_text('print(1)')
    assert check_file_scope(tmp_path, 'Grep', {'path':str(tmp_path), 'glob':'.env', 'pattern':'.'})
    assert check_file_scope(tmp_path, 'Grep', {'path':str(tmp_path/'safe.py'), 'pattern':'print'}) is None
    assert check_file_scope(tmp_path, 'Glob', {'path':str(tmp_path), 'pattern':'../*'})
