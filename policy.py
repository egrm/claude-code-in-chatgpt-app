"""Subscription routing and file-tool boundaries. Never opens credential stores."""
import json
import os
from pathlib import Path
import subprocess

from platform_support import NO_WINDOW, claude_cli

# Native Claude Code only: ~/.local/bin/claude(.exe), else the setup-recorded/PATH native binary.
CLI = claude_cli()
MODELS = {'claude-opus-5-5', 'claude-haiku-5-5'}
AUTH_FIELDS = ('loggedIn', 'authMethod', 'apiProvider', 'subscriptionType')


def clean_environment(source):
    prefixes = ('ANTHROPIC_', 'CLAUDE_', 'AWS_', 'AZURE_', 'GOOGLE_', 'CLOUD_ML_', 'OTEL_')
    return {k: v for k, v in source.items() if not k.upper().startswith(prefixes) and k.upper() != 'CLAUDECODE'}


def sanitize_process_environment():
    clean = clean_environment(os.environ)
    os.environ.clear()
    os.environ.update(clean)


def validate_auth(status):
    if not (status.get('loggedIn') is True and status.get('authMethod') == 'claude.ai'
            and status.get('apiProvider') == 'firstParty' and status.get('subscriptionType') == 'max'):
        raise ValueError('Claude Max claude.ai subscription authentication is required; API/provider routes refused')


def auth_status(cli=None):
    """Clean-environment `auth status`; returns only the four non-secret routing fields."""
    command = [str(cli or CLI), '--safe-mode', '--setting-sources', '', '--settings',
               '{"forceLoginMethod":"claudeai","fallbackModel":[],"fastMode":false}', 'auth', 'status', '--json']
    r = subprocess.run(command, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                       env=clean_environment(os.environ), creationflags=NO_WINDOW)
    try:
        data = json.loads(r.stdout)
    except ValueError:
        data = None
    # A failed status call is only reported (never accepted) when it says logged out.
    if not isinstance(data, dict) or (r.returncode and data.get('loggedIn') is not False):
        raise ValueError('Claude subscription authentication status unavailable; no inference started')
    return {k: data.get(k) for k in AUTH_FIELDS}


def preflight_auth(cli=None):
    data = auth_status(cli)
    validate_auth(data)
    return data


def check_billing_event(info):
    if info.get('isUsingOverage') is True or info.get('overageStatus') in ('allowed', 'allowed_warning'):
        return 'Usage credits/overage detected or enabled; supervisor stopped this session'
    if info.get('status') in ('rejected', 'blocked'):
        return 'Included subscription limit reached; no paid fallback permitted'
    return None


def check_file_scope(workspace, tool, data):
    if tool not in ('Read', 'Write', 'Edit', 'Glob', 'Grep'):
        return None
    raw = data.get('file_path') if tool in ('Read', 'Write', 'Edit') else data.get('path', str(workspace))
    if not raw:
        return 'File path is required'
    root = Path(workspace).resolve()
    path = Path(raw)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(root):
        return 'File access outside the task workspace is refused'
    relative = path.relative_to(root)
    if tool == 'Grep' and not path.is_file():
        return 'Grep must target one explicit file; use Glob then Read/Grep selected files to protect credential descendants'
    if tool == 'Glob':
        pattern = str(data.get('pattern', '')).replace('\\', '/')
        if '..' in pattern.split('/') or pattern.startswith('/') or ':' in pattern:
            return 'Glob patterns must stay relative to the workspace'
    protected = {'.git', '.claude', '.codex', '.aws', '.ssh', '.azure', '.gnupg'}
    if any(part.lower() in protected for part in relative.parts):
        return 'Agent configuration, repository internals and credentials are protected'
    if any(part.lower() == '.env' or part.lower().startswith('.env.') or part.lower() in ('.credentials.json', 'credentials', 'credentials.json') for part in relative.parts):
        return 'Credential files are protected'
    return None
