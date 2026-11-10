"""Typed references to existing canonical owners; content cannot register or grant operations."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.authority import AUTHORITY_LEVEL_RANK, AuthorityLevel
from codex_web.skill_procedures import ProcedureValueContract
from codex_web.timeout_values import normalize_timeout


class CanonicalOperationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CanonicalOperationResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    disposition: Literal['succeeded', 'waiting', 'reconciliation_required', 'refused']
    outputs: dict[str, Any] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)
    original_references: tuple[str, ...] = Field(default=(), max_length=16)
    code: str | None = Field(default=None, max_length=160)


@dataclass(frozen=True)
class CanonicalPreviousStep:
    status: str
    failure_code: str | None
    diagnostic_evidence_ids: tuple[str, ...] = ()
    operation_code: str | None = None


@dataclass(frozen=True)
class CanonicalProcedureContext:
    assignment: Any
    checkpoint: Any
    step: Any
    requester_actor: Any
    worker_actor: Any
    resource_ids: tuple[str, ...]
    validate_current: Callable[[], None] = field(repr=False, compare=False)
    _original_scope: Callable = field(repr=False, compare=False)
    previous_step: CanonicalPreviousStep | None = None

    def original_intent_scope(self, original_id: str, request_sha256: str):
        return self._original_scope(self, original_id, request_sha256)


@dataclass(frozen=True)
class CanonicalOperationContract:
    operation_id: str
    contract_version: str
    resource_types: tuple[str, ...]
    required_capability: str
    authority_level: str
    input_model: type[BaseModel]
    output_contracts: tuple[ProcedureValueContract, ...]
    effect: Literal['read', 'mutation']
    timeout_seconds: int
    max_attempts: int = 1
    allow_original_intent: bool = False


@dataclass(frozen=True)
class CanonicalOperationRegistration:
    contract: CanonicalOperationContract
    adapter: Any
    input_fields: tuple[tuple[str, frozenset[str], bool], ...]


# Installed only while an actual interpreter invokes its registered recovery owner.
# Neither stored procedure/input JSON nor a request can install this capability.
_current_original = ContextVar('canonical_procedure_original_intent', default=None)


@contextmanager
def original_intent_scope(engine, registration, context, original_id, request_sha256):
    contract = registration.contract
    if (not contract.allow_original_intent or contract.effect != 'mutation'
            or not isinstance(original_id, str) or not original_id
            or not isinstance(request_sha256, str) or len(request_sha256) != 64
            or any(c not in '0123456789abcdef' for c in request_sha256)
            or not callable(getattr(registration.adapter, 'admit_original', None))):
        raise CanonicalOperationError('procedure_original_intent_scope_unavailable')
    context.validate_current()
    token = _current_original.set((engine, registration, context, original_id, request_sha256))
    try:
        yield
    finally:
        _current_original.reset(token)


def current_original_intent(engine, intent):
    value = _current_original.get()
    if value is None or value[0] is not engine:
        return None
    _, registration, context, original_id, digest = value
    if intent.id != original_id:
        raise CanonicalOperationError('procedure_original_intent_changed')
    return registration, context, digest


class CanonicalOperationRegistry:
    """Code-injected metadata and owners, not a mutable operation or authority catalog."""
    def __init__(self):
        self._operations = {}

    def register(self, contract: CanonicalOperationContract, adapter):
        key = (contract.operation_id, contract.contract_version)
        if (key in self._operations or not callable(getattr(adapter, 'execute', None))
                or contract.input_model.model_config.get('extra') != 'forbid'
                or contract.effect not in {'read', 'mutation'}
                or contract.timeout_seconds not in (0, -1) and not 1 <= contract.timeout_seconds <= 3600
                or contract.max_attempts != 1
                or (contract.effect == 'mutation' and contract.authority_level in AUTHORITY_LEVEL_RANK
                    and AUTHORITY_LEVEL_RANK[AuthorityLevel(contract.authority_level)] < AUTHORITY_LEVEL_RANK[AuthorityLevel.EXECUTE])
                or contract.authority_level not in AUTHORITY_LEVEL_RANK
                or not contract.required_capability or not contract.resource_types
                or any(type(v) is not ProcedureValueContract for v in contract.output_contracts)
                or (contract.effect == 'mutation' and not callable(getattr(adapter, 'reconcile', None)))
                or (contract.allow_original_intent and (contract.effect != 'mutation'
                    or not callable(getattr(adapter, 'admit_original', None))))):
            raise CanonicalOperationError('procedure_canonical_registration_invalid')
        properties = contract.input_model.model_json_schema().get('properties', {})
        fields = []
        for name, value in contract.input_model.model_fields.items():
            schema = properties.get(name, {})
            kinds = {schema.get('type')} | {part.get('type') for part in schema.get('anyOf', ())}
            fields.append((name, frozenset(k for k in kinds if isinstance(k, str)), value.is_required()))
        self._operations[key] = CanonicalOperationRegistration(contract, adapter, tuple(fields))

    def resolve(self, operation_id, contract_version):
        try:
            return self._operations[(operation_id, contract_version)]
        except KeyError as exc:
            raise CanonicalOperationError('procedure_canonical_operation_unavailable') from exc

    def validate_step(self, procedure, step):
        ref = step.control_plane
        if procedure.schema_version not in {'1.4', '1.5'} or ref is None:
            raise CanonicalOperationError('procedure_canonical_schema_required')
        registration = self.resolve(ref.operation_id, ref.contract_version)
        contract = registration.contract
        selector = next((s for s in procedure.resource_selectors if s.id == ref.resource_selector), None)
        if (selector is None or set(contract.resource_types) != set(selector.resource_types)
                or (procedure.schema_version != '1.5' and normalize_timeout(contract.timeout_seconds) is not None
                    and (normalize_timeout(step.timeout_seconds) is None
                         or normalize_timeout(step.timeout_seconds) > normalize_timeout(contract.timeout_seconds)))
                or step.max_attempts != 1
                or step.retry_backoff_seconds != 0
                or (contract.effect == 'read' and step.kind not in {'diagnostic', 'verification'})
                or (contract.effect == 'mutation' and step.kind not in {'mutation', 'recovery'})
                or step.recovery is not None
                or (step.impact is not None and step.impact.reversible)
                or tuple(step.outputs) != contract.output_contracts
                or any(e.kind == 'provider_receipt' for e in step.expected_evidence)
                or not any(p.capability == contract.required_capability
                    and p.resource_selector == ref.resource_selector
                    and AUTHORITY_LEVEL_RANK[p.level] >= AUTHORITY_LEVEL_RANK[AuthorityLevel(contract.authority_level)]
                    for p in step.permissions)):
            raise CanonicalOperationError('procedure_canonical_contract_mismatch')
        # Parameter names/types come from a trusted registered Pydantic contract;
        # generic bags/URLs/actors cannot be introduced by a Definition.
        fields = {name:(kinds, required) for name,kinds,required in registration.input_fields}
        if {v.name for v in step.inputs} != set(fields):
            raise CanonicalOperationError('procedure_canonical_inputs_mismatch')
        for value in step.inputs:
            kinds, required = fields[value.name]
            expected = 'string' if value.type in {'resource_reference', 'evidence_reference', 'secret_reference'} else value.type
            if expected not in kinds or value.required != required:
                raise CanonicalOperationError('procedure_canonical_input_type_mismatch')
        return registration


def canonical_metadata_registry(value):
    return (type(value) is CanonicalOperationRegistry and all(
        getattr(getattr(value, name, None), '__self__', None) is value
        and getattr(getattr(value, name, None), '__func__', None) is original
        for name, original in _METADATA_METHODS.items()))


_METADATA_METHODS = {name:getattr(CanonicalOperationRegistry, name) for name in ('resolve', 'validate_step')}
