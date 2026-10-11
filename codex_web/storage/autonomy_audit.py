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
from codex_web.compatibility import ContractSpec
from codex_web.storage.state_store import StateStore


AUDIT_RECORDS_CONTRACT = ContractSpec("autonomy-audit-records", "1.0", ("1.0",))


class AutonomyAuditStore:
    namespace = "autonomy_audit"
    meta_key = "_audit_records_meta"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> AutonomyAuditState:
        if payload is None:
            return AutonomyAuditState()
        if not isinstance(payload, dict):
            raise ValueError("autonomy audit state must be an object")
        AUTONOMY_AUDIT_CONTRACT.require(str(payload.get("schema_version") or ""))
        if AutonomyAuditStore.meta_key in payload:
            AutonomyAuditStore._validate_meta(payload[AutonomyAuditStore.meta_key])
            payload = {
                **{key: payload[key] for key in AutonomyAuditState.model_fields
                   if key != "records" and key in payload},
                "records": [value for key, value in sorted(payload.items()) if key.startswith("record:")],
            }
        return AutonomyAuditState.model_validate(payload)

    @staticmethod
    def _record_key(sequence: int) -> str:
        return f"record:{sequence:020d}"

    @staticmethod
    def _tail_key(partition_id: str) -> str:
        return f"tail:{partition_id}"

    @classmethod
    def _encode(cls, state: AutonomyAuditState) -> dict[str, Any]:
        raw = state.model_dump(mode="json")
        rows = raw.pop("records")
        raw[cls.meta_key] = {
            "schema_version": AUDIT_RECORDS_CONTRACT.current,
            "next_sequence": len(rows),
        }
        for sequence, record in enumerate(rows):
            raw[cls._record_key(sequence)] = record
            tail_key = cls._tail_key(record["partition_id"])
            if tail_key not in raw or raw[tail_key]["sequence"] < record["sequence"]:
                raw[tail_key] = record
        return raw

    def _ensure_records(self) -> None:
        meta = self.store.record_get(self.namespace, self.meta_key)
        if meta is not None:
            self._validate_meta(meta)
            return

        def migrate(current):
            if current[self.meta_key] is not None:
                self._validate_meta(current[self.meta_key])
                return {}
            legacy = {key: value for key, value in current.items()
                      if key != self.meta_key and value is not None}
            state = self._decode(legacy or None)
            # Remove the legacy array only after it has validated completely.
            return {**self._encode(state), "records": None}

        self.store.record_mutate(
            self.namespace, (self.meta_key, *AutonomyAuditState.model_fields), migrate,
        )

    @staticmethod
    def _validate_meta(meta: Any) -> None:
        if not isinstance(meta, dict):
            raise ValueError("audit records metadata must be an object")
        AUDIT_RECORDS_CONTRACT.require(str(meta.get("schema_version") or ""))
        if not isinstance(meta.get("next_sequence"), int) or meta["next_sequence"] < 0:
            raise ValueError("invalid audit records sequence")

    def load(self) -> AutonomyAuditState:
        self._ensure_records()
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[AutonomyAuditState], AutonomyAuditState],
    ) -> AutonomyAuditState:
        return self._decode(self._persist_update(updater))

    def _persist_update(
        self,
        updater: Callable[[AutonomyAuditState], AutonomyAuditState],
    ) -> Any:
        self._ensure_records()
        return self.store.update(
            self.namespace,
            lambda raw: self._encode(updater(self._decode(raw))),
            default=AutonomyAuditState().model_dump(mode="json"),
        )

    def append(
        self,
        payload: AutonomyAuditPayload,
    ) -> AutonomyAuditRecord:
        self._ensure_records()
        appended: list[AutonomyAuditRecord] = []
        partition_id = AutonomyAuditRecord.partition_for(
            payload.organization_id, payload.workspace_id,
        )
        tail_key = self._tail_key(partition_id)

        def apply(current):
            meta = dict(current[self.meta_key])
            self._validate_meta(meta)
            previous = (AutonomyAuditRecord.model_validate(current[tail_key])
                        if current[tail_key] is not None else None)
            if previous is not None and previous.partition_id != partition_id:
                raise ValueError("audit tail belongs to another partition")
            record = AutonomyAuditRecord.build(
                payload, sequence=previous.sequence + 1 if previous else 1,
                previous_hash=previous.record_hash if previous else GENESIS_HASH,
            )
            row = record.model_dump(mode="json")
            sequence = meta["next_sequence"]
            meta["next_sequence"] = sequence + 1
            appended.append(record)
            return {self.meta_key: meta, self._record_key(sequence): row, tail_key: row}

        # Sequence assignment, immutable row and partition tail commit together.
        # Full integrity verification remains an explicit history operation.
        self.store.record_mutate(self.namespace, (self.meta_key, tail_key), apply)
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
