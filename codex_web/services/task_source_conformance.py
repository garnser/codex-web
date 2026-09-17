from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_web.models import TaskSourceIdentity
from codex_web.services.task_source_reconciliation import same_task_source_identity
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCanonicalProjection,
    TaskSourceCapabilities,
    TaskSourceEvent,
    TaskSourceSnapshot,
)


class TaskSourceConformanceError(ValueError):
    """Raised when an adapter or one of its normalized outputs breaks contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TaskSourceConformanceSuite:
    """Reusable provider-neutral assertions for every authoritative adapter.

    The suite validates the adapter declaration and objects after the adapter
    has normalized provider data. It deliberately does not know provider API
    payloads, credentials, routes, or state vocabularies.
    """

    def validate_adapter(self, source: Any) -> TaskSource:
        if not isinstance(source, TaskSource):
            raise TaskSourceConformanceError(
                "protocol_mismatch",
                "Task-source adapter does not implement the complete TaskSource protocol.",
            )
        if not str(source.source_type or "").strip():
            raise TaskSourceConformanceError(
                "missing_source_type",
                "Task-source adapter source_type must not be empty.",
            )
        if not str(source.source_instance or "").strip():
            raise TaskSourceConformanceError(
                "missing_source_instance",
                "Task-source adapter source_instance must not be empty.",
            )
        if not isinstance(source.capabilities, TaskSourceCapabilities):
            raise TaskSourceConformanceError(
                "invalid_capabilities",
                "Task-source adapter must declare TaskSourceCapabilities.",
            )
        return source

    def validate_identity(
        self,
        source: TaskSource,
        identity: TaskSourceIdentity,
    ) -> TaskSourceIdentity:
        self.validate_adapter(source)
        expected_type = str(source.source_type).strip().casefold()
        actual_type = identity.source_type.strip().casefold()
        if actual_type != expected_type:
            raise TaskSourceConformanceError(
                "source_type_mismatch",
                f"Normalized identity source_type={identity.source_type!r} does not match adapter source_type={source.source_type!r}.",
            )

        expected_instance = str(source.source_instance).strip().rstrip("/")
        actual_instance = identity.source_instance.strip().rstrip("/")
        if actual_instance != expected_instance:
            raise TaskSourceConformanceError(
                "source_instance_mismatch",
                "Normalized identity source_instance does not match the adapter instance.",
            )
        return identity

    def validate_snapshot(
        self,
        source: TaskSource,
        snapshot: TaskSourceSnapshot,
    ) -> TaskSourceSnapshot:
        self.validate_identity(source, snapshot.identity)
        return snapshot

    def validate_event(
        self,
        source: TaskSource,
        event: TaskSourceEvent,
    ) -> TaskSourceEvent:
        self.validate_identity(source, event.identity)
        if event.snapshot is not None:
            self.validate_snapshot(source, event.snapshot)
            if not same_task_source_identity(event.identity, event.snapshot.identity):
                raise TaskSourceConformanceError(
                    "event_snapshot_identity_mismatch",
                    "Normalized event and its snapshot must identify the same authoritative item.",
                )
        return event

    def validate_projection(
        self,
        source: TaskSource,
        snapshot: TaskSourceSnapshot,
        projection: TaskSourceCanonicalProjection,
    ) -> TaskSourceCanonicalProjection:
        self.validate_snapshot(source, snapshot)
        self.validate_identity(source, projection.identity)
        if not same_task_source_identity(snapshot.identity, projection.identity):
            raise TaskSourceConformanceError(
                "projection_identity_mismatch",
                "Canonical projection must retain the normalized snapshot identity.",
            )
        return projection
