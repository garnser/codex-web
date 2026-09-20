from __future__ import annotations

from typing import Any, Callable

from codex_web.control_plane_broker import (
    CONTROL_PLANE_BROKER_AUDIT_CONTRACT,
    ControlPlaneBrokerAuditEvent,
    ControlPlaneBrokerAuditState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class ControlPlaneBrokerAuditStore:
    namespace = "control_plane_broker_audit"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> ControlPlaneBrokerAuditState:
        if payload is None:
            return ControlPlaneBrokerAuditState()
        if not isinstance(payload, dict):
            raise ValueError("control-plane broker audit state must be an object")
        CONTROL_PLANE_BROKER_AUDIT_CONTRACT.require(
            str(payload.get("schema_version") or "")
        )
        return ControlPlaneBrokerAuditState.model_validate(payload)

    def load(self) -> ControlPlaneBrokerAuditState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[
            [ControlPlaneBrokerAuditState],
            ControlPlaneBrokerAuditState,
        ],
    ) -> ControlPlaneBrokerAuditState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=ControlPlaneBrokerAuditState().model_dump(mode="json"),
        )
        return self._decode(payload)

    def append(
        self,
        event: ControlPlaneBrokerAuditEvent,
    ) -> ControlPlaneBrokerAuditEvent:
        def apply(
            state: ControlPlaneBrokerAuditState,
        ) -> ControlPlaneBrokerAuditState:
            state.events.append(event)
            state.events = state.events[-10000:]
            return state

        self.update(apply)
        return event
