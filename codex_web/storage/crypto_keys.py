from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.crypto import CRYPTO_STATE_CONTRACT, CryptoKeyState
from codex_web.storage.sqlite_state import SQLiteStateStore


CRYPTO_KEY_MIGRATIONS = MigrationRegistry("crypto-key-state")
CRYPTO_KEY_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": CRYPTO_STATE_CONTRACT.current,
        "keys": list(payload.get("keys", [])),
        "events": list(payload.get("events", [])),
    },
)


class CryptoKeyStore:
    namespace = "crypto_keys"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> CryptoKeyState:
        if payload is None:
            return CryptoKeyState()
        if not isinstance(payload, dict):
            raise ValueError("crypto key state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != CRYPTO_STATE_CONTRACT.current:
            payload = CRYPTO_KEY_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=CRYPTO_STATE_CONTRACT.current,
            )
        CRYPTO_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return CryptoKeyState.model_validate(payload)

    @staticmethod
    def _encode(state: CryptoKeyState) -> dict[str, Any]:
        return state.model_dump(mode="json")

    def load(self) -> CryptoKeyState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[CryptoKeyState], CryptoKeyState],
    ) -> CryptoKeyState:
        def apply(raw: Any) -> dict[str, Any]:
            return self._encode(updater(self._decode(raw)))

        payload = self.store.update(
            self.namespace,
            apply,
            default=CryptoKeyState().model_dump(mode="json"),
        )
        return self._decode(payload)
