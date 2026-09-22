from __future__ import annotations

from typing import Any, Callable

from codex_web.artifact_evidence import ArtifactEvidenceState
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


ARTIFACT_EVIDENCE_CONTRACT = ContractSpec(
    "artifact-evidence-state",
    "1.1",
    ("1.0", "1.1"),
)
ARTIFACT_EVIDENCE_MIGRATIONS = MigrationRegistry("artifact-evidence-state")
ARTIFACT_EVIDENCE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        **{key: value for key, value in payload.items() if key != "schema_version"},
    },
)
ARTIFACT_EVIDENCE_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": "1.1",
        "evidence": [
            {
                **dict(item),
                "resource_ids": dict(item).get("resource_ids") or [],
            }
            for item in payload.get("evidence", [])
        ],
    },
)


class ArtifactEvidenceStore:
    namespace = "artifact_evidence"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ArtifactEvidenceState:
        if payload is None:
            return ArtifactEvidenceState()
        if not isinstance(payload, dict):
            raise ValueError("artifact/evidence state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != ARTIFACT_EVIDENCE_CONTRACT.current:
            payload = ARTIFACT_EVIDENCE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=ARTIFACT_EVIDENCE_CONTRACT.current,
            )
        ARTIFACT_EVIDENCE_CONTRACT.require(payload.get("schema_version", ""))
        return ArtifactEvidenceState.model_validate(payload)

    def load(self) -> ArtifactEvidenceState:
        return self._decode(self.store.get(self.namespace))

    def update(self, updater: Callable[[ArtifactEvidenceState], ArtifactEvidenceState]) -> ArtifactEvidenceState:
        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            return updater(state).model_dump(mode="json")

        payload = self.store.update(
            self.namespace,
            apply,
            default=ArtifactEvidenceState().model_dump(mode="json"),
        )
        return self._decode(payload)
