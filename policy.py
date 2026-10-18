"""Subscription routing and file-tool boundaries. Never opens credential stores."""
import json
import os
from pathlib import Path
import subprocess

CLI = Path.home() / '.local' / 'bin' / 'claude.exe'
MODELS = {'claude-opus-5-5', 'claude-haiku-5-5'}


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


def preflight_auth():
    command = [str(CLI), '--safe-mode', '--setting-sources', '', '--settings',
               '{"forceLoginMethod":"claudeai","fallbackModel":[],"fastMode":false}', 'auth', 'status', '--json']
    r = subprocess.run(command, capture_output=True, text=True, timeout=20,
                       env=clean_environment(os.environ), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode:
        raise ValueError('Claude subscription authentication status unavailable; no inference started')
    data = json.loads(r.stdout)
    validate_auth(data)
    return {k: data.get(k) for k in ('loggedIn', 'authMethod', 'apiProvider', 'subscriptionType')}


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
