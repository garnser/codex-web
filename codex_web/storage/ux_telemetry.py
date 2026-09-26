from __future__ import annotations

from typing import Any, Callable

from codex_web.storage.state_store import StateStore
from codex_web.ux_telemetry import UX_TELEMETRY_CONTRACT, UxTelemetryState


class UxTelemetryStore:
    namespace = "ux_telemetry"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(payload: Any) -> UxTelemetryState:
        if payload is None:
            return UxTelemetryState()
        if not isinstance(payload, dict):
            raise ValueError("UX telemetry state must be an object")
        UX_TELEMETRY_CONTRACT.require(str(payload.get("schema_version") or ""))
        return UxTelemetryState.model_validate(payload)

    def load(self) -> UxTelemetryState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[UxTelemetryState], UxTelemetryState],
    ) -> UxTelemetryState:
        payload = self.store.update(
            self.namespace,
            lambda raw: updater(self._decode(raw)).model_dump(mode="json"),
            default=UxTelemetryState().model_dump(mode="json"),
        )
        return self._decode(payload)
