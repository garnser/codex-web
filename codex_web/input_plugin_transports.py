from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from codex_web.input_plugins import (
    InputContextBlock,
    InputEnvelope,
    InputMessage,
    InputPatch,
    InputPhase,
    InputPluginBudgetError,
    InputPluginExecutionError,
    InputPluginSecurityError,
)


DEFAULT_EXTERNAL_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_EXTERNAL_REQUEST_BYTES = 128 * 1024
DEFAULT_MAX_EXTERNAL_RESPONSE_BYTES = 32 * 1024
_SENSITIVE_SETTING_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|credential|private[_-]?key)",
    re.IGNORECASE,
)


class ExternalInputTransportError(InputPluginExecutionError):
    pass


class ExternalInputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=64)
    content: str


class ExternalInputContextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=256)
    content: str
    classification: str = Field(min_length=1, max_length=64)


class ExternalInputProjection(BaseModel):
    """Minimum provider-neutral projection allowed to cross an external plugin transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: str = Field(min_length=1, max_length=256)
    model_class: str = Field(min_length=1, max_length=256)
    system_prompt: str = ""
    messages: tuple[ExternalInputMessage, ...] = ()
    context_blocks: tuple[ExternalInputContextBlock, ...] = ()
    text_verbosity: str | None = Field(default=None, max_length=64)
    reasoning_effort: str | None = Field(default=None, max_length=64)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    max_output_tokens: int = Field(ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0.0)
    preferred_provider_ids: tuple[str, ...] = ()


class ExternalInputPluginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(min_length=1, max_length=256)
    plugin_version: str = Field(min_length=1, max_length=128)
    phase: InputPhase
    input: ExternalInputProjection
    settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


ExternalInputInvoker = Callable[
    [ExternalInputPluginRequest],
    Mapping[str, Any] | InputPatch | Awaitable[Mapping[str, Any] | InputPatch],
]


class ExternalInputInvokerProtocol(Protocol):
    def __call__(
        self,
        request: ExternalInputPluginRequest,
    ) -> Mapping[str, Any] | InputPatch | Awaitable[Mapping[str, Any] | InputPatch]: ...


def _json_size(value: BaseModel | Mapping[str, Any]) -> int:
    payload: Any
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = dict(value)
    return len(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


def _safe_settings(
    settings: Mapping[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool | None]:
    for key in settings:
        if _SENSITIVE_SETTING_RE.search(str(key)):
            raise InputPluginSecurityError(
                "external input plugin settings cannot contain secret/credential-like keys"
            )
    return dict(settings)


class BoundedExternalInputPlugin:
    """Transport-neutral wrapper around a bounded injected external invoker.

    This class performs no process/network I/O itself. The injected invoker owns
    transport mechanics and must be composed through the appropriate worker,
    extension, network, resource and credential boundaries.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        plugin_version: str,
        phase: InputPhase,
        transport: str,
        invoker: ExternalInputInvoker,
        timeout_seconds: float = DEFAULT_EXTERNAL_TIMEOUT_SECONDS,
        max_request_bytes: int = DEFAULT_MAX_EXTERNAL_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_EXTERNAL_RESPONSE_BYTES,
        allowed_context_classifications: tuple[str, ...] = ("public",),
        include_system_prompt: bool = True,
        include_messages: bool = True,
    ) -> None:
        if not plugin_id.strip() or not plugin_version.strip() or not transport.strip():
            raise ValueError("external input plugin identity/version/transport are required")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("external input plugin timeout must be > 0 and <= 120 seconds")
        if max_request_bytes < 256 or max_response_bytes < 256:
            raise ValueError("external input plugin request/response budgets must be >= 256 bytes")
        if not callable(invoker):
            raise ValueError("external input plugin invoker must be callable")

        self.id = plugin_id
        self.version = plugin_version
        self.phase = phase
        self.transport = transport
        self.invoker = invoker
        self.timeout_seconds = timeout_seconds
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.allowed_context_classifications = tuple(
            dict.fromkeys(value.strip() for value in allowed_context_classifications if value.strip())
        )
        self.include_system_prompt = include_system_prompt
        self.include_messages = include_messages

    def _projection(self, envelope: InputEnvelope) -> ExternalInputProjection:
        allowed = set(self.allowed_context_classifications)
        blocks = tuple(
            ExternalInputContextBlock(
                id=block.id,
                source=block.source,
                content=block.content,
                classification=block.classification,
            )
            for block in envelope.context_blocks
            if block.classification in allowed
        )
        messages = (
            tuple(
                ExternalInputMessage(role=item.role, content=item.content)
                for item in envelope.messages
            )
            if self.include_messages
            else ()
        )
        return ExternalInputProjection(
            purpose=envelope.purpose,
            model_class=envelope.model_class,
            system_prompt=envelope.system_prompt if self.include_system_prompt else "",
            messages=messages,
            context_blocks=blocks,
            text_verbosity=envelope.text_verbosity,
            reasoning_effort=envelope.reasoning_effort,
            output_contract=dict(envelope.output_contract),
            max_output_tokens=envelope.max_output_tokens,
            max_cost_usd=envelope.max_cost_usd,
            preferred_provider_ids=envelope.preferred_provider_ids,
        )

    async def transform(
        self,
        envelope: InputEnvelope,
        context,
    ) -> InputPatch:
        request = ExternalInputPluginRequest(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=self.phase,
            input=self._projection(envelope),
            settings=_safe_settings(context.settings),
        )
        request_size = _json_size(request)
        if request_size > self.max_request_bytes:
            raise InputPluginBudgetError(
                f"external input plugin request exceeds {self.max_request_bytes} byte budget"
            )

        async def call() -> Mapping[str, Any] | InputPatch:
            result = self.invoker(request)
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            raw = await asyncio.wait_for(call(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise ExternalInputTransportError(
                f"external input plugin transport timed out after {self.timeout_seconds:g}s"
            ) from exc
        except InputPluginSecurityError:
            raise
        except Exception as exc:
            raise ExternalInputTransportError(
                f"external input plugin transport failed with {type(exc).__name__}"
            ) from exc

        if not isinstance(raw, (InputPatch, Mapping)):
            raise ExternalInputTransportError(
                "external input plugin transport returned unsupported response type"
            )
        if _json_size(raw) > self.max_response_bytes:
            raise InputPluginBudgetError(
                f"external input plugin response exceeds {self.max_response_bytes} byte budget"
            )

        patch = raw if isinstance(raw, InputPatch) else InputPatch.model_validate(raw)
        if patch.plugin_id != self.id or patch.plugin_version != self.version:
            raise InputPluginSecurityError(
                "external input plugin response identity/version mismatch"
            )
        if patch.phase != self.phase:
            raise InputPluginSecurityError(
                "external input plugin response phase mismatch"
            )
        return patch


class CommandInputPlugin(BoundedExternalInputPlugin):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(transport="command", **kwargs)


class HttpInputPlugin(BoundedExternalInputPlugin):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(transport="http", **kwargs)


class McpInputPlugin(BoundedExternalInputPlugin):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(transport="mcp", **kwargs)
