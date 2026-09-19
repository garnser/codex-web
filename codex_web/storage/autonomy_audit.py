from __future__ import annotations

from typing import Any, Callable

from codex_web.autonomy_audit import (
    AUTONOMY_AUDIT_CONTRACT,
    GENESIS_HASH,
    AutonomyAuditCheckpoint,
    AutonomyAuditPayload,
    AutonomyAuditRecord,
    AutonomyAuditState,
    AutonomyReliabilityPolicy,
    AutonomySafetySignal,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class AutonomyAuditStore:
    namespace = "autonomy_audit"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> AutonomyAuditState:
        if payload is None:
            return AutonomyAuditState()
        if not isinstance(payload, dict):
            raise ValueError("autonomy audit state must be an object")
        AUTONOMY_AUDIT_CONTRACT.require(str(payload.get("schema_version") or ""))
        return AutonomyAuditState.model_validate(payload)

    def load(self) -> AutonomyAuditState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AutonomyAuditState], AutonomyAuditState],
    ) -> AutonomyAuditState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=AutonomyAuditState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def append(
        self,
        payload: AutonomyAuditPayload,
    ) -> AutonomyAuditRecord:
        appended: list[AutonomyAuditRecord] = []

        def apply(state: AutonomyAuditState) -> AutonomyAuditState:
            partition_id = AutonomyAuditRecord.partition_for(
                payload.organization_id,
                payload.workspace_id,
            )
            rows = [
                item for item in state.records if item.partition_id == partition_id
            ]
            if rows:
                previous = max(rows, key=lambda item: item.sequence)
                sequence = previous.sequence + 1
                previous_hash = previous.record_hash
            else:
                sequence = 1
                previous_hash = GENESIS_HASH
            record = AutonomyAuditRecord.build(
                payload,
                sequence=sequence,
                previous_hash=previous_hash,
            )
            state.records.append(record)
            appended.append(record)
            return state

        self.update(apply)
        return appended[0]

    def add_checkpoint(
        self,
        checkpoint: AutonomyAuditCheckpoint,
    ) -> AutonomyAuditCheckpoint:
        def apply(state: AutonomyAuditState) -> AutonomyAuditState:
            if any(item.id == checkpoint.id for item in state.checkpoints):
                return state
            state.checkpoints.append(checkpoint)
            return state

        self.update(apply)
        return checkpoint

    def set_reliability_policy(
        self,
        policy: AutonomyReliabilityPolicy,
    ) -> AutonomyReliabilityPolicy:
        self.update(
            lambda state: state.model_copy(
                update={"reliability_policy": policy}
            )
        )
        return policy

    def add_signal(
        self,
        signal: AutonomySafetySignal,
    ) -> AutonomySafetySignal:
        def apply(state: AutonomyAuditState) -> AutonomyAuditState:
            state.signals.append(signal)
            return state

        self.update(apply)
        return signal

    def clear_signal(
        self,
        signal_id: str,
        *,
        cleared_at: float,
    ) -> AutonomySafetySignal:
        updated: list[AutonomySafetySignal] = []

        def apply(state: AutonomyAuditState) -> AutonomyAuditState:
            rows = []
            found = False
            for item in state.signals:
                if item.id != signal_id:
                    rows.append(item)
                    continue
                found = True
                replacement = item.model_copy(
                    update={
                        "active": False,
                        "cleared_at": cleared_at,
                    }
                )
                rows.append(replacement)
                updated.append(replacement)
            if not found:
                raise KeyError(signal_id)
            state.signals = rows
            return state

        self.update(apply)
        return updated[0]
