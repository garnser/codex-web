"""Code-owned reductions for an explicitly configured native reasoning runtime.

This policy cannot grant a model, credentials, worker or execution authority.
The canonical gateway/profile/bootstrap owners must select those separately.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from codex_web.runtime.codex import CodexRuntime

NATIVE_REASONING_CONTRACT = 'codex-reasoning/1.0'
QUALIFIED_CODEX_VERSION = '0.160.0'
REASONING_CODEX_HOME = Path('/tmp/codex-reasoning-home')
REASONING_CATALOG = Path('/opt/codex-reasoning-model.json')
DISABLED_FEATURES = (
    'shell_tool', 'apps', 'plugins', 'hooks', 'multi_agent', 'multi_agent_v2',
    'code_mode', 'code_mode_host', 'computer_use', 'browser_use',
    'browser_use_external', 'browser_use_full_cdp_access', 'image_generation',
    'view_image', 'goals', 'memories', 'skill_search', 'sleep_tool',
    'tool_suggest', 'workspace_dependencies', 'remote_plugin',
    'skill_mcp_dependency_install', 'daemon_auto_start', 'unbounded_connection_retries',
)


class NativeReasoningPolicyError(RuntimeError):
    pass


def restricted_model_catalog(catalog: dict, concrete_model: str) -> dict:
    """Reduce one actual bundled row; never synthesize model access or pricing."""
    rows = catalog.get('models')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise NativeReasoningPolicyError('Native bundled catalog unavailable')
    matches = [row for row in rows if isinstance(row, dict) and row.get('slug') == concrete_model]
    if len(matches) != 1:
        raise NativeReasoningPolicyError('Native model must have one exact bundled record')
    row = copy.deepcopy(matches[0])
    row.update(shell_type='disabled', apply_patch_tool_type=None,
               experimental_supported_tools=[], tool_mode=None,
               supports_search_tool=False, include_skills_usage_instructions=False,
               include_plugin_usage_instructions=False, include_apps_usage_instructions=False)
    return {'models': [row]}


def reasoning_config_arguments() -> tuple[str, ...]:
    values = [*(f'features.{name}=false' for name in DISABLED_FEATURES),
        'web_search="disabled"', 'forced_login_method="chatgpt"', 'model_provider="openai"',
        'model_catalog_json=' + json.dumps(str(REASONING_CATALOG)),
        'project_doc_max_bytes=0']
    return tuple(part for value in values for part in ('--config', value))


def verify_reasoning_config(config: dict) -> None:
    """Verify the native process's actual effective config before thread creation."""
    if not isinstance(config, dict):
        raise NativeReasoningPolicyError('Native reasoning config unavailable')
    features = config.get('features')
    if not isinstance(features, dict) or any(features.get(name) is not False for name in DISABLED_FEATURES):
        raise NativeReasoningPolicyError('Native reasoning feature restriction missing')
    servers = config.get('mcp_servers')
    if not isinstance(servers, dict) or any(not isinstance(value, dict) or value.get('enabled') is not False
                                           for value in servers.values()):
        raise NativeReasoningPolicyError('Native reasoning MCP startup is not disabled')
    if (config.get('web_search') != 'disabled'
            or config.get('model_catalog_json') != str(REASONING_CATALOG)
            or config.get('forced_login_method') != 'chatgpt'
            or config.get('project_doc_max_bytes') != 0
            or config.get('model_provider') != 'openai'):
        raise NativeReasoningPolicyError('Native reasoning effective config changed')


class ReasoningOnlyCodexRuntime(CodexRuntime):
    """Existing transport with explicit request refusal, not a second execution engine."""

    def __init__(self, *args, **kwargs):
        kwargs['restart_on_timeout'] = False
        super().__init__(*args, **kwargs)
        self.reasoning_refusal = None

    async def _handle_message(self, message):
        if message.get('id') is not None and 'method' in message:
            self.reasoning_refusal = ('native_auth_refresh_owner_required'
                if message['method'] == 'account/chatgptAuthTokens/refresh'
                else 'native_reasoning_server_request_refused')
            # No auto-approval, tool, permission, credential-refresh or other
            # server request can borrow a human or operational runtime actor.
            await self._send({'id': message['id'], 'error': {
                'code': -32601, 'message': 'Server requests are disabled for canonical reasoning'}})
            return
        await super()._handle_message(message)

    async def _send(self, message, *, expected_generation=None, start_guard=None):
        if message.get('method') in {'thread/start', 'thread/resume', 'turn/start'}:
            from codex_web.services.native_model_provider import current_native_model_invocation
            context = current_native_model_invocation()
            previous = start_guard
            def reasoning_guard():
                context.validate_current()
                if previous is not None:
                    previous()
                context.validate_current()
            start_guard = reasoning_guard
        return await super()._send(message, expected_generation=expected_generation,
                                   start_guard=start_guard)

    async def request(self, method, params=None, *, start_capture=None):
        if method in {'thread/start', 'thread/resume', 'turn/start'}:
            from codex_web.services.native_model_provider import current_native_model_invocation
            from codex_web.services.post_turn_readiness import PostTurnReadinessService
            import asyncio
            context = current_native_model_invocation()
            await PostTurnReadinessService._run_owned(
                lambda: asyncio.to_thread(context.validate_current),
                seconds=5., name='native-model-final-admission')
            if not context.concrete_model:
                raise NativeReasoningPolicyError('Exact native inference model unavailable')
        if method in {'thread/start', 'thread/resume'}:
            if self.native_runtime_version != QUALIFIED_CODEX_VERSION or self.restart_on_timeout:
                raise NativeReasoningPolicyError('Native reasoning transport is not qualified')
            response = await super().request('config/read', {'includeLayers': False})
            verify_reasoning_config(response.get('config'))
            params = {**(params or {}), 'baseInstructions': context.system_prompt,
                      'developerInstructions': None, 'model': context.concrete_model,
                      'modelProvider': 'openai'}
            if method == 'thread/start':
                params.update(dynamicTools=[], environments=[], allowProviderModelFallback=False)
        if method == 'turn/start':
            params = {**(params or {}), 'model': context.concrete_model}
        return await super().request(method, params, start_capture=start_capture)


from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.agent_providers import AgentProviderCapability


class ReasoningOnlyCodexAdapter(CodexAgentRuntimeAdapter):
    runtime_id = 'codex-reasoning'
    capabilities = (AgentProviderCapability.AGENT_EXECUTION,
                    AgentProviderCapability.PERSISTENT_SESSIONS,
                    AgentProviderCapability.STREAMING,
                    AgentProviderCapability.INTERRUPT_CANCEL,
                    AgentProviderCapability.USAGE_PARTIAL)
