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
    max_retained_events = 10000

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
        def apply(payload: Any) -> dict[str, Any]:
            if payload is None:
                payload = ControlPlaneBrokerAuditState().model_dump(mode="json")
            if not isinstance(payload, dict):
                raise ValueError("control-plane broker audit state must be an object")
            CONTROL_PLANE_BROKER_AUDIT_CONTRACT.require(
                str(payload.get("schema_version") or "")
            )
            events = payload.get("events")
            if not isinstance(events, list):
                raise ValueError("control-plane broker audit events must be an array")
            return {
                **payload,
                "events": [
                    *events,
                    event.model_dump(mode="json"),
                ][-self.max_retained_events :],
            }

        # The event is already validated. Avoid reconstructing every retained
        # Pydantic event twice merely to append one audit record; large audit
        # histories otherwise block the async broker request path.
        self.store.update(
            self.namespace,
            apply,
            default=ControlPlaneBrokerAuditState().model_dump(mode="json"),
        )
        return event
