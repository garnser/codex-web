from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.definitions import DefinitionReference


logger = logging.getLogger(__name__)


class InputPhase(StrEnum):
    NORMALIZE = "normalize"
    ENRICH = "enrich"
    COMPOSE = "compose"
    OPTIMIZE = "optimize"
    PROVIDER_DECORATE = "provider_decorate"


INPUT_PHASE_ORDER: tuple[InputPhase, ...] = tuple(InputPhase)


class InputFailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"
    SKIP = "skip"


class InputFieldClass(StrEnum):
    COMPOSABLE = "composable"
    GATED = "gated"
    PROTECTED = "protected"


COMPOSABLE_INPUT_FIELDS = frozenset(
    {
        "system_prompt",
        "messages",
        "context_blocks",
        "text_verbosity",
        "output_contract",
    }
)
GATED_INPUT_FIELDS = frozenset(
    {
        "model_class",
        "reasoning_effort",
        "max_output_tokens",
        "max_cost_usd",
        "preferred_provider_ids",
    }
)
PROTECTED_INPUT_FIELDS = frozenset(
    {
        "organization_id",
        "workspace_id",
        "actor_id",
        "prompt_template_id",
        "prompt_template_version",
        "required_capabilities",
        "required_residency_tags",
        "required_compliance_tags",
        "timeout_seconds",
        "allow_fallback",
        "work_item_ref",
        "goal_id",
        "decision_id",
        "execution_id",
        "purpose",
        "authority_refs",
        "policy_refs",
        "approval_refs",
        "sandbox",
        "secret_refs",
        "service_scopes",
    }
)
KNOWN_INPUT_FIELDS = (
    COMPOSABLE_INPUT_FIELDS | GATED_INPUT_FIELDS | PROTECTED_INPUT_FIELDS
)

DEFAULT_MAX_PATCH_BYTES = 32 * 1024
DEFAULT_MAX_ADDED_CHARACTERS = 16_000
MAX_CONTEXT_BLOCKS = 64
MAX_MESSAGES = 128
MAX_OUTPUT_CONTRACT_KEYS = 64


class InputPluginError(RuntimeError):
    pass


class InputPluginSecurityError(InputPluginError):
    pass


class InputPluginExecutionError(InputPluginError):
    pass


class InputPluginBudgetError(InputPluginError):
    pass


class InputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=64)
    content: str


class InputContextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=256)
    content: str
    classification: str = Field(default="internal", min_length=1, max_length=64)
    source_ref: str | None = Field(default=None, max_length=512)


class InputEnvelope(BaseModel):
    """Provider-neutral input state; protected fields remain core-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=256)
    organization_id: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1, max_length=256)

    model_class: str = Field(min_length=1, max_length=256)
    messages: tuple[InputMessage, ...] = ()
    system_prompt: str = ""
    context_blocks: tuple[InputContextBlock, ...] = ()
    text_verbosity: str | None = Field(default=None, max_length=64)
    reasoning_effort: str | None = Field(default=None, max_length=64)
    output_contract: dict[str, Any] = Field(default_factory=dict)

    prompt_template_id: str = Field(default="generic.system", min_length=1, max_length=256)
    prompt_template_version: str | None = Field(default=None, max_length=128)
    required_capabilities: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    preferred_provider_ids: tuple[str, ...] = ()
    max_output_tokens: int = Field(default=2048, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    max_cost_usd: float | None = Field(default=None, gt=0.0)
    allow_fallback: bool = True

    work_item_ref: str | None = Field(default=None, max_length=512)
    goal_id: str | None = Field(default=None, max_length=256)
    decision_id: str | None = Field(default=None, max_length=256)
    execution_id: str | None = Field(default=None, max_length=256)
    purpose: str = Field(default="general", min_length=1, max_length=256)

    authority_refs: tuple[str, ...] = ()
    policy_refs: tuple[str, ...] = ()
    approval_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_bounds(self) -> "InputEnvelope":
        if len(self.messages) > MAX_MESSAGES:
            raise ValueError(f"messages cannot exceed {MAX_MESSAGES}")
        if len(self.context_blocks) > MAX_CONTEXT_BLOCKS:
            raise ValueError(f"context_blocks cannot exceed {MAX_CONTEXT_BLOCKS}")
        if len(self.output_contract) > MAX_OUTPUT_CONTRACT_KEYS:
            raise ValueError(
                f"output_contract cannot exceed {MAX_OUTPUT_CONTRACT_KEYS} keys"
            )
        return self


class InputPatch(BaseModel):
    """Untrusted plugin proposal. Field authority is enforced by the pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str = Field(min_length=1, max_length=256)
    plugin_version: str = Field(min_length=1, max_length=128)
    phase: InputPhase
    changes: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class InputPluginContext(BaseModel):
    """Minimum core-owned context exposed to an input plugin; never secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    actor_id: str
    request_id: str
    work_item_ref: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    purpose: str
    max_patch_bytes: int
    max_added_characters: int
    settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class InputPlugin(Protocol):
    id: str
    version: str
    transport: str

    async def transform(
        self,
        envelope: InputEnvelope,
        context: InputPluginContext,
    ) -> InputPatch: ...


class InputPluginRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    plugin: Any
    phase: InputPhase
    order: int = Field(default=100, ge=0, le=1_000_000)
    failure_policy: InputFailurePolicy = InputFailurePolicy.FAIL_CLOSED
    max_patch_bytes: int = Field(
        default=DEFAULT_MAX_PATCH_BYTES,
        ge=256,
        le=1024 * 1024,
    )
    max_added_characters: int = Field(
        default=DEFAULT_MAX_ADDED_CHARACTERS,
        ge=0,
        le=1_000_000,
    )
    definition_ref: DefinitionReference | None = None
    settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_plugin(self) -> "InputPluginRegistration":
        for attribute in ("id", "version", "transport", "transform"):
            if not getattr(self.plugin, attribute, None):
                raise ValueError(f"input plugin requires {attribute}")
        return self


class InputGatedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str
    plugin_version: str
    phase: InputPhase
    field: str
    value_sha256: str = Field(min_length=64, max_length=64)


class InputPluginAuditEvent(BaseModel):
    """Metadata-only audit event emitted before composition failure escapes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    actor_id: str
    request_id: str
    work_item_ref: str | None = None
    execution_id: str | None = None
    plugin_id: str
    plugin_version: str
    transport: str
    phase: InputPhase
    failure_policy: InputFailurePolicy
    event_type: str
    outcome: str
    rejected_fields: tuple[str, ...] = ()
    exception_type: str | None = None
    started_at: float
    completed_at: float
    duration_seconds: float = Field(ge=0.0)


InputPluginAuditSink = Callable[[InputPluginAuditEvent], None]


class InputPluginProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: str
    plugin_version: str
    transport: str
    phase: InputPhase
    definition_ref: DefinitionReference | None = None
    input_sha256: str = Field(min_length=64, max_length=64)
    patch_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    output_sha256: str = Field(min_length=64, max_length=64)
    outcome: str
    applied_fields: tuple[str, ...] = ()
    proposed_gated_fields: tuple[str, ...] = ()
    rejected_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    input_characters_before: int = Field(ge=0)
    input_characters_after: int = Field(ge=0)
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    started_at: float
    completed_at: float
    duration_seconds: float = Field(ge=0.0)


class InputPipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope: InputEnvelope
    provenance: tuple[InputPluginProvenance, ...] = ()
    gated_proposals: tuple[InputGatedProposal, ...] = ()


GatedValidator = Callable[
    [str, Any, InputEnvelope, InputPluginContext],
    bool | Awaitable[bool],
]


def input_field_class(field: str) -> InputFieldClass:
    if field in COMPOSABLE_INPUT_FIELDS:
        return InputFieldClass.COMPOSABLE
    if field in GATED_INPUT_FIELDS:
        return InputFieldClass.GATED
    if field in PROTECTED_INPUT_FIELDS:
        return InputFieldClass.PROTECTED
    raise InputPluginSecurityError(f"input plugin attempted unknown field mutation: {field}")


def _canonical_hash(value: BaseModel | Mapping[str, Any]) -> str:
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = dict(value)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _warning_marker(value: str) -> str:
    return "plugin_warning_sha256:" + hashlib.sha256(
        value.encode("utf-8", errors="replace")
    ).hexdigest()


def _serialized_size(value: BaseModel | Mapping[str, Any]) -> int:
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
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


def _input_character_count(envelope: InputEnvelope) -> int:
    return (
        len(envelope.system_prompt)
        + sum(len(message.content) for message in envelope.messages)
        + sum(len(block.content) for block in envelope.context_blocks)
        + len(
            json.dumps(
                envelope.output_contract,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
    )


def _estimated_tokens(envelope: InputEnvelope) -> int:
    characters = _input_character_count(envelope)
    return 0 if characters == 0 else max(1, (characters + 3) // 4)


async def _accepted(
    validator: GatedValidator | None,
    field: str,
    value: Any,
    envelope: InputEnvelope,
    context: InputPluginContext,
) -> bool:
    if validator is None:
        return False
    result = validator(field, value, envelope, context)
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


class InputPluginPipeline:
    """Deterministic ordered input composition with code-owned security gates."""

    def __init__(
        self,
        registrations: tuple[InputPluginRegistration, ...] | list[InputPluginRegistration] = (),
        *,
        gated_validator: GatedValidator | None = None,
        audit_sink: InputPluginAuditSink | None = None,
    ) -> None:
        self.registrations = tuple(
            sorted(
                registrations,
                key=lambda item: (
                    INPUT_PHASE_ORDER.index(item.phase),
                    item.order,
                    str(item.plugin.id),
                    str(item.plugin.version),
                ),
            )
        )
        self.gated_validator = gated_validator
        self.audit_sink = audit_sink

    def _audit(
        self,
        *,
        context: InputPluginContext,
        registration: InputPluginRegistration,
        event_type: str,
        outcome: str,
        started_at: float,
        rejected_fields: tuple[str, ...] = (),
        exception_type: str | None = None,
    ) -> None:
        if self.audit_sink is None:
            return
        completed = time.time()
        event = InputPluginAuditEvent(
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            actor_id=context.actor_id,
            request_id=context.request_id,
            work_item_ref=context.work_item_ref,
            execution_id=context.execution_id,
            plugin_id=str(registration.plugin.id),
            plugin_version=str(registration.plugin.version),
            transport=str(registration.plugin.transport),
            phase=registration.phase,
            failure_policy=registration.failure_policy,
            event_type=event_type,
            outcome=outcome,
            rejected_fields=rejected_fields,
            exception_type=exception_type,
            started_at=started_at,
            completed_at=completed,
            duration_seconds=max(0.0, completed - started_at),
        )
        try:
            self.audit_sink(event)
        except Exception:
            logger.exception(
                "input plugin audit sink failed",
                extra={
                    "plugin_id": event.plugin_id,
                    "plugin_version": event.plugin_version,
                    "event_type": event.event_type,
                },
            )

    async def execute(self, envelope: InputEnvelope) -> InputPipelineResult:
        current = envelope
        provenance: list[InputPluginProvenance] = []
        gated_proposals: list[InputGatedProposal] = []

        for registration in self.registrations:
            plugin = registration.plugin
            context = InputPluginContext(
                organization_id=current.organization_id,
                workspace_id=current.workspace_id,
                actor_id=current.actor_id,
                request_id=current.request_id,
                work_item_ref=current.work_item_ref,
                goal_id=current.goal_id,
                decision_id=current.decision_id,
                execution_id=current.execution_id,
                purpose=current.purpose,
                max_patch_bytes=registration.max_patch_bytes,
                max_added_characters=registration.max_added_characters,
                settings=dict(registration.settings),
            )
            before = current
            before_hash = _canonical_hash(before)
            before_chars = _input_character_count(before)
            started = time.time()
            security_audited = False

            try:
                raw_patch = await plugin.transform(before, context)
                patch = (
                    raw_patch
                    if isinstance(raw_patch, InputPatch)
                    else InputPatch.model_validate(raw_patch)
                )
                if patch.plugin_id != str(plugin.id):
                    raise InputPluginSecurityError(
                        "input patch plugin_id does not match registered plugin"
                    )
                if patch.plugin_version != str(plugin.version):
                    raise InputPluginSecurityError(
                        "input patch plugin_version does not match registered plugin"
                    )
                if patch.phase != registration.phase:
                    raise InputPluginSecurityError(
                        "input patch phase does not match registered phase"
                    )
                if _serialized_size(patch) > registration.max_patch_bytes:
                    raise InputPluginBudgetError(
                        f"input patch exceeds {registration.max_patch_bytes} byte budget"
                    )

                composable: dict[str, Any] = {}
                gated: dict[str, Any] = {}
                protected: list[str] = []
                unknown: list[str] = []
                for field, value in patch.changes.items():
                    try:
                        classification = input_field_class(field)
                    except InputPluginSecurityError:
                        unknown.append(field)
                        continue
                    if classification == InputFieldClass.PROTECTED:
                        protected.append(field)
                    elif classification == InputFieldClass.GATED:
                        gated[field] = value
                    else:
                        composable[field] = value

                if protected or unknown:
                    rejected = tuple(sorted({*protected, *unknown}))
                    self._audit(
                        context=context,
                        registration=registration,
                        event_type="protected_mutation_rejected",
                        outcome="denied",
                        started_at=started,
                        rejected_fields=rejected,
                        exception_type="InputPluginSecurityError",
                    )
                    security_audited = True
                    raise InputPluginSecurityError(
                        "input plugin attempted protected/unknown mutations: "
                        + ", ".join(rejected)
                    )

                accepted_gated: dict[str, Any] = {}
                rejected_gated: list[str] = []
                for field, value in gated.items():
                    gated_proposals.append(
                        InputGatedProposal(
                            plugin_id=str(plugin.id),
                            plugin_version=str(plugin.version),
                            phase=registration.phase,
                            field=field,
                            value_sha256=_canonical_hash({"value": value}),
                        )
                    )
                    if await _accepted(
                        self.gated_validator,
                        field,
                        value,
                        before,
                        context,
                    ):
                        accepted_gated[field] = value
                    else:
                        rejected_gated.append(field)

                candidate_payload = before.model_dump(mode="python")
                candidate_payload.update(composable)
                candidate_payload.update(accepted_gated)
                candidate = InputEnvelope.model_validate(candidate_payload)
                after_chars = _input_character_count(candidate)
                added = max(0, after_chars - before_chars)
                if added > registration.max_added_characters:
                    raise InputPluginBudgetError(
                        "input plugin added "
                        f"{added} characters; budget is "
                        f"{registration.max_added_characters}"
                    )

                current = candidate
                completed = time.time()
                warnings = tuple(
                    _warning_marker(value) for value in patch.warnings
                ) + tuple(
                    f"gated field rejected by core validator: {field}"
                    for field in sorted(rejected_gated)
                )
                provenance.append(
                    InputPluginProvenance(
                        plugin_id=str(plugin.id),
                        plugin_version=str(plugin.version),
                        transport=str(plugin.transport),
                        phase=registration.phase,
                        definition_ref=registration.definition_ref,
                        input_sha256=before_hash,
                        patch_sha256=_canonical_hash(patch),
                        output_sha256=_canonical_hash(current),
                        outcome="applied",
                        applied_fields=tuple(
                            sorted({*composable, *accepted_gated})
                        ),
                        proposed_gated_fields=tuple(sorted(gated)),
                        rejected_fields=tuple(sorted(rejected_gated)),
                        warnings=warnings,
                        input_characters_before=before_chars,
                        input_characters_after=after_chars,
                        estimated_tokens_before=_estimated_tokens(before),
                        estimated_tokens_after=_estimated_tokens(current),
                        started_at=started,
                        completed_at=completed,
                        duration_seconds=max(0.0, completed - started),
                    )
                )
            except InputPluginSecurityError as exc:
                # Security classification is structural and never fail-open.
                if not security_audited:
                    self._audit(
                        context=context,
                        registration=registration,
                        event_type="plugin_security_violation",
                        outcome="denied",
                        started_at=started,
                        exception_type=type(exc).__name__,
                    )
                raise
            except Exception as exc:
                completed = time.time()
                exception_type = type(exc).__name__
                if exception_type == "ExternalInputTransportError":
                    event_type = "external_transport_failure"
                elif isinstance(exc, InputPluginBudgetError):
                    event_type = "plugin_budget_failure"
                else:
                    event_type = "plugin_execution_failure"
                self._audit(
                    context=context,
                    registration=registration,
                    event_type=event_type,
                    outcome=(
                        "denied"
                        if registration.failure_policy == InputFailurePolicy.FAIL_CLOSED
                        else "continued"
                    ),
                    started_at=started,
                    exception_type=exception_type,
                )
                if registration.failure_policy == InputFailurePolicy.FAIL_CLOSED:
                    raise InputPluginExecutionError(
                        f"input plugin {plugin.id} failed with {type(exc).__name__}"
                    ) from exc
                outcome = (
                    "failed_open"
                    if registration.failure_policy == InputFailurePolicy.FAIL_OPEN
                    else "skipped_after_error"
                )
                provenance.append(
                    InputPluginProvenance(
                        plugin_id=str(plugin.id),
                        plugin_version=str(plugin.version),
                        transport=str(plugin.transport),
                        phase=registration.phase,
                        definition_ref=registration.definition_ref,
                        input_sha256=before_hash,
                        output_sha256=before_hash,
                        outcome=outcome,
                        warnings=(f"plugin failed with {type(exc).__name__}",),
                        input_characters_before=before_chars,
                        input_characters_after=before_chars,
                        estimated_tokens_before=_estimated_tokens(before),
                        estimated_tokens_after=_estimated_tokens(before),
                        started_at=started,
                        completed_at=completed,
                        duration_seconds=max(0.0, completed - started),
                    )
                )

        return InputPipelineResult(
            envelope=current,
            provenance=tuple(provenance),
            gated_proposals=tuple(gated_proposals),
        )


class NormalizeWhitespaceInputPlugin:
    """Small deterministic built-in reference plugin used by the core contract tests."""

    id = "builtin.normalize-whitespace"
    version = "1.0.0"
    transport = "builtin"

    async def transform(
        self,
        envelope: InputEnvelope,
        context: InputPluginContext,
    ) -> InputPatch:
        del context
        messages = tuple(
            message.model_copy(update={"content": message.content.strip()})
            for message in envelope.messages
        )
        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=InputPhase.NORMALIZE,
            changes={
                "system_prompt": envelope.system_prompt.strip(),
                "messages": messages,
            },
            metadata={"strategy": "strip-leading-trailing-whitespace"},
        )
