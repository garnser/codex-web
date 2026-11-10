"""Process-local proof owned by an actual session and canonical interpreter.

Serialized provenance is evidence only. No request, stored digest or Definition
can install this scope; the live validation callable remains authoritative.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable

from codex_web.definitions import DefinitionReference


@dataclass(frozen=True)
class NativeProviderPreparation:
    selected: tuple[DefinitionReference, ...]
    source: str
    validate: Callable[[], None] = field(repr=False, compare=False)
    context: object | None = field(default=None, repr=False, compare=False)
    exclude_empty: Callable[[DefinitionReference], bool] | None = field(default=None, repr=False, compare=False)

    def excludes(self, reference, context):
        if self.context is not context or self.selected or self.exclude_empty is None:
            return False
        self.validate()
        return self.exclude_empty(reference)

    def permits(self, selected, context):
        if self.context is not context or not self.selected or set(self.selected) != set(selected):
            return False
        self.validate()
        return True


_current = ContextVar("native_provider_procedure_preparation", default=None)
_session_observation = ContextVar('native_session_preparation_observation', default=None)


@dataclass(frozen=True)
class NativeSessionPreparationObservation:
    """Ephemeral owning-session proof; never serialized or accepted from input."""
    assignment_id: str
    validate: Callable[[object, object], None] = field(repr=False, compare=False)


@contextmanager
def native_session_observation(proof, *, attestor, procedures):
    if type(proof) is not NativeSessionPreparationObservation:
        raise ValueError('Actual native session preparation observation required')
    token = _session_observation.set((proof, attestor, procedures))
    try:
        yield
    finally:
        _session_observation.reset(token)


def attest_prepared_session(assignment, procedures, attestor):
    observed = _session_observation.get()
    if observed is None:
        return attestor(assignment, procedures)
    proof, original_attestor, original_procedures = observed
    if (original_attestor is not attestor or original_procedures is not procedures
            or proof.assignment_id != assignment.id):
        raise ValueError('Native session preparation owner changed')
    return proof.validate(assignment, procedures)


def current_preparation():
    return _current.get()


@contextmanager
def native_provider_preparation(proof):
    token = _current.set(proof)
    try:
        yield
    finally:
        _current.reset(token)
