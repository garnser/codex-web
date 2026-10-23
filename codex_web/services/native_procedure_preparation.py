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

    def permits(self, selected):
        if not self.selected or set(self.selected) != set(selected):
            return False
        self.validate()
        return True


_current = ContextVar("native_provider_procedure_preparation", default=None)


def current_preparation():
    return _current.get()


@contextmanager
def native_provider_preparation(proof):
    token = _current.set(proof)
    try:
        yield
    finally:
        _current.reset(token)
