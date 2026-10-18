"""Official SDK adapter. Auth and refresh remain entirely inside Claude Code."""
import asyncio
from dataclasses import asdict
import json
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import SystemMessage, ResultMessage, AssistantMessage, StreamEvent, RateLimitEvent, TextBlock
from policy import CLI, check_billing_event, preflight_auth


class ClaudeBackend:
    def __init__(self, record, emit, permission, hook):
        self.record, self.emit, self.permission, self.hook = record, emit, permission, hook
        self.client = None
        self.reader = None
        self.result_seen = asyncio.Event()
        self.closing = False
        self.last_progress = 0

    async def connect(self):
        from windows_process import hide_background_children
        hide_background_children()
        auth = await asyncio.to_thread(preflight_auth)
        await self.emit('auth', auth)
        settings = json.dumps({'forceLoginMethod': 'claudeai', 'fallbackModel': [], 'fastMode': False,
                               'availableModels': [self.record['requested_model']],
                               'permissions': {'ask': ['Write', 'Edit', 'Bash', 'AskUserQuestion']}})
        options = ClaudeAgentOptions(
            cli_path=CLI, cwd=self.record['workspace'], model=self.record['requested_model'],
            effort=self.record['effort'], tools=self.record['tools'], setting_sources=[],
            settings=settings, strict_mcp_config=True, mcp_servers={},
            permission_mode='default', can_use_tool=self._permission,
            resume=self.record.get('session_id'), max_turns=self.record['max_turns'],
            max_budget_usd=self.record['max_budget_usd'], include_partial_messages=True,
            system_prompt={'type': 'preset', 'preset': 'claude_code', 'append':
                'You are supervised by a coordinator. Use AskUserQuestion for unresolved choices. '
                'Work only in the task workspace. Report actual test exit status and changed files. '
                'Use Glob to find files then Read or Grep specific files; recursive Grep is disabled to protect credentials. '
                'Respect declined tools. Do not attempt alternate auth, billing routes, or model substitution.'},
            extra_args={'safe-mode': None, 'restricted': None},
            env={'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_MAX_RETRIES': '0',
                 'API_TIMEOUT_MS': '60000', 'ENABLE_CLAUDEAI_MCP_SERVERS': 'false'},
            stderr=lambda line: None,
            hooks={
                'PreToolUse': [HookMatcher(hooks=[self._pre], timeout=30)],
                'PostToolUse': [HookMatcher(hooks=[self._post], timeout=30)],
                'PostToolUseFailure': [HookMatcher(hooks=[self._failure], timeout=30)],
            })
        self.client = ClaudeSDKClient(options)
        await self.client.connect()
        self.reader = asyncio.create_task(self._receive())

    async def _permission(self, tool, data, context):
        response = await self.permission(tool, data)
        if not response['allow']:
            return PermissionResultDeny(message=response.get('message', 'Coordinator declined'))
        updated = dict(data)
        if tool == 'AskUserQuestion':
            updated['answers'] = response.get('answers', {})
        return PermissionResultAllow(updated_input=updated)

    async def _pre(self, data, tool_use_id, context):
        reason = await self.hook('tool_started', {**data, 'tool_use_id': tool_use_id})
        if reason:
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
        return {}

    async def _post(self, data, tool_use_id, context):
        await self.hook('tool_result', {**data, 'tool_use_id': tool_use_id})
        return {}

    async def _failure(self, data, tool_use_id, context):
        await self.hook('tool_failed', {**data, 'tool_use_id': tool_use_id})
        return {}

    async def _receive(self):
        try:
            async for message in self.client.receive_messages():
                if isinstance(message, SystemMessage) and message.subtype == 'init':
                    await self.emit('init', {'session_id': message.data.get('session_id'), 'model': message.data.get('model')})
                elif isinstance(message, AssistantMessage):
                    if message.model not in (self.record['requested_model'], '<synthetic>'):
                        raise ValueError('Unexpected model substitution refused')
                    text = ''.join(b.text for b in message.content if isinstance(b, TextBlock))
                    if text:
                        await self.emit('assistant', {'text': text[:16000], 'model': message.model})
                elif isinstance(message, StreamEvent):
                    delta = message.event.get('delta', {})
                    if delta.get('type') == 'text_delta' and time.monotonic()-self.last_progress > .5:
                        self.last_progress = time.monotonic()
                        await self.emit('output_progress', {'text': delta.get('text', '')[:1000]})
                    # Thinking blocks and private/internal data are deliberately not retained.
                elif isinstance(message, RateLimitEvent):
                    info = dict(message.rate_limit_info.raw)
                    info.update(status=message.rate_limit_info.status, overageStatus=message.rate_limit_info.overage_status)
                    await self.emit('billing', info)
                    reason = check_billing_event(info)
                    if reason:
                        raise ValueError(reason)
                elif isinstance(message, ResultMessage):
                    interrupted = message.terminal_reason in ('aborted_streaming', 'aborted_tools')
                    await self.emit('result', {'result': message.result or '', 'is_error': message.is_error,
                        'interrupted': interrupted, 'error': '; '.join(message.errors or []),
                        'session_id': message.session_id, 'num_turns': message.num_turns,
                        'list_price_estimate_usd': message.total_cost_usd})
                    self.result_seen.set()
            if not self.closing:
                await self.emit('disconnected', {'error': 'Claude stream disconnected; use task_resume'})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.emit('failed', {'error': str(exc)[:2000]})
            self.result_seen.set()
            if self.client:
                await self.client.disconnect()

    async def send(self, prompt):
        if self.reader.done():
            raise ValueError('Claude stream disconnected; use task_resume')
        self.result_seen.clear()
        await self.client.query(prompt)

    async def interrupt(self):
        self.result_seen.clear()
        await self.client.interrupt()
        await asyncio.wait_for(self.result_seen.wait(), 15)

    async def close(self):
        self.closing = True
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.client:
            await self.client.disconnect()
