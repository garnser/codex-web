from __future__ import annotations

import threading
from typing import Any, Callable

from codex_web.control_plane_broker import (
    CONTROL_PLANE_BROKER_AUDIT_CONTRACT,
    ControlPlaneBrokerAuditEvent,
    ControlPlaneBrokerAuditState,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class ControlPlaneBrokerAuditStore:
    namespace = "control_plane_broker_audit"
    records_namespace = "control_plane_broker_audit_events"
    max_retained_events = 10000

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store
        self._migration_lock = threading.Lock()

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
        legacy = self._decode(self.store.get(self.namespace))
        events = {event.id: event for event in legacy.events}
        for payload in self.store.record_items(self.records_namespace).values():
            event = ControlPlaneBrokerAuditEvent.model_validate(payload)
            events[event.id] = event
        retained = sorted(
            events.values(),
            key=lambda event: (event.occurred_at, event.id),
        )[-self.max_retained_events :]
        return ControlPlaneBrokerAuditState(events=retained)

    @staticmethod
    def _record_key(event: ControlPlaneBrokerAuditEvent) -> str:
        return f"{event.occurred_at:020.6f}:{event.id}"

    def _migrate_legacy_events(self) -> None:
        with self._migration_lock:
            payload = self.store.get(self.namespace)
            if not isinstance(payload, dict):
                return
            raw_events = payload.get("events")
            if not isinstance(raw_events, list) or not raw_events:
                return
            events = [
                ControlPlaneBrokerAuditEvent.model_validate(item)
                for item in raw_events
            ]
            self.store.record_apply(
                self.records_namespace,
                upserts={
                    self._record_key(event): event.model_dump(mode="json")
                    for event in events
                },
            )
            self.store.update(
                self.namespace,
                lambda current: {
                    **(
                        current
                        if isinstance(current, dict)
                        else ControlPlaneBrokerAuditState().model_dump(mode="json")
                    ),
                    "events": [],
                },
                default=ControlPlaneBrokerAuditState().model_dump(mode="json"),
            )

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
        # Keep the audit history as keyed rows. Appending to one giant JSON
        # document made every broker request decode, validate, and rewrite the
        # complete retained history on the event loop.
        self._migrate_legacy_events()
        self.store.record_apply(
            self.records_namespace,
            upserts={
                self._record_key(event): event.model_dump(mode="json"),
            },
        )
        excess = self.store.record_count(self.records_namespace) - self.max_retained_events
        if excess > 0:
            oldest, _ = self.store.record_page(
                self.records_namespace,
                limit=min(excess, 1000),
            )
            self.store.record_apply(
                self.records_namespace,
                upserts={},
                deletes=tuple(oldest),
            )
        return event
