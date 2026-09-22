from __future__ import annotations

from collections.abc import Mapping

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.anthropic_worker_configuration import (
    ANTHROPIC_WORKER_API_KEY_CONFIG,
)
from codex_web.services.codex_worker_configuration import (
    CODEX_WORKER_ACCESS_TOKEN_CONFIG,
)


DEFAULT_RUNTIME_CREDENTIAL_CONFIGS: dict[tuple[str, str], str] = {
    ("openai", "codex"): CODEX_WORKER_ACCESS_TOKEN_CONFIG,
    ("anthropic", "claude-code"): ANTHROPIC_WORKER_API_KEY_CONFIG,
}


def runtime_credential_config_key(
    runtime_binding: ExecutionRuntimeBinding | None,
    mapping: Mapping[tuple[str, str], str] | None = None,
) -> str | None:
    if runtime_binding is None:
        return None
    table = mapping or DEFAULT_RUNTIME_CREDENTIAL_CONFIGS
    return table.get((runtime_binding.provider_id, runtime_binding.runtime_id))
