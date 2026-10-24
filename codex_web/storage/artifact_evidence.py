from __future__ import annotations

from typing import Any, Callable

from codex_web.artifact_evidence import ArtifactEvidenceState
from codex_web.compatibility import ContractSpec, MigrationRegistry
from codex_web.storage.sqlite_state import SQLiteStateStore


ARTIFACT_EVIDENCE_CONTRACT = ContractSpec(
    "artifact-evidence-state",
    "1.3",
    ("1.0", "1.1", "1.2", "1.3"),
)
ARTIFACT_EVIDENCE_MIGRATIONS = MigrationRegistry("artifact-evidence-state")
ARTIFACT_EVIDENCE_MIGRATIONS.register(
    "1.2", "1.3", lambda payload: {**payload, "schema_version": "1.3"},
)
ARTIFACT_EVIDENCE_MIGRATIONS.register(
    "1.1", "1.2", lambda payload: {**payload, "schema_version": "1.2"},
)
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

    def artifact_state(self, artifact_id: str, *, organization_id: str, workspace_id: str) -> ArtifactEvidenceState:
        reader = getattr(self.store, 'document_array_item_envelope', None)
        if not callable(reader):
            return self.load()
        return self._decode(reader(self.namespace, 'artifacts', artifact_id,
            organization_id=organization_id, workspace_id=workspace_id,
            collection_names=('artifacts','evidence','verifications')))

    def artifact_states(self, artifact_ids: tuple[str, ...], *, organization_id: str,
                        workspace_id: str) -> dict[str, ArtifactEvidenceState]:
        """One snapshot, with the same first scoped member as each point read.

        Ask the backend for each first scoped raw member, matching POINT's
        LIMIT 1 before domain decoding. Always decode its complete result:
        nonstandard/legacy full fallback must retain full-state validation.
        """
        ids = tuple(dict.fromkeys(artifact_ids))
        if not ids:
            return {}
        reader = getattr(self.store, 'document_array_items_envelope', None)
        if not callable(reader):
            state = self.load()
            return dict.fromkeys(ids, state)
        # Selection happens in the existing backend so its full fallback remains
        # distinguishable without guessing from the returned document's fields.
        state = self._decode(reader(self.namespace, 'artifacts', ids,
            organization_id=organization_id, workspace_id=workspace_id,
            collection_names=('artifacts', 'evidence', 'verifications'), first_per_id=True))
        return dict.fromkeys(ids, state)

    def evidence_state(self, evidence_id: str, *, organization_id: str, workspace_id: str) -> ArtifactEvidenceState:
        """Validate the canonical envelope and complete selected Evidence.

        Explicit full-state/transactional callers retain load(). Older backends
        keep the full decoder path; no alternate authority or persisted index is
        introduced. Unrelated historical catalog bodies are not point-read data.
        """
        reader = getattr(self.store, "document_array_item_envelope", None)
        if not callable(reader):
            return self.load()
        return self._decode(reader(self.namespace, "evidence", evidence_id,
            organization_id=organization_id, workspace_id=workspace_id,
            collection_names=("artifacts", "evidence", "verifications")))

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
