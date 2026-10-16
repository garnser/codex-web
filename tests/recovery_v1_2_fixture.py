"""Frozen model and store from public5db for real older-reader/writer refusal.
Only unchanged nested domain types are imported from current code.
"""
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from codex_web.compatibility import ContractSpec
from codex_web.recovery import (RecoveryPolicyChange,RecoveryPolicy,BackupManifest,
    RestoreVerification,RecoveryFiring,RecoveryFiringWatermark)
RECOVERY_STATE_CONTRACT = ContractSpec("recovery-state", "1.2", ("1.2",))
class RecoveryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = RECOVERY_STATE_CONTRACT.current
    policy_organization_id: str | None = None
    policy_workspace_id: str | None = None
    policy_changes: tuple[RecoveryPolicyChange, ...] = ()
    policy: RecoveryPolicy | None = None
    backups: dict[str, BackupManifest] = Field(default_factory=dict)
    verifications: dict[str, RestoreVerification] = Field(default_factory=dict)
    backup_schedule_id: str | None = None
    verification_schedule_id: str | None = None
    scheduled_firings: dict[str, RecoveryFiring] = Field(default_factory=dict, max_length=128)
    scheduled_watermarks: dict[str, RecoveryFiringWatermark] = Field(default_factory=dict, max_length=2)


    def model_post_init(self, __context: Any) -> None:
        RECOVERY_STATE_CONTRACT.require(self.schema_version)



from typing import Any, Callable


from codex_web.compatibility import MigrationRegistry


RECOVERY_STATE_MIGRATIONS = MigrationRegistry("recovery-state")
RECOVERY_STATE_MIGRATIONS.register("1.0", "1.1", lambda payload: {**payload, "schema_version": "1.1"})
RECOVERY_STATE_MIGRATIONS.register("1.1", "1.2", lambda payload: {**payload, "schema_version": "1.2"})
from codex_web.storage.state_store import StateStore


class RecoveryStore:
    namespace = "recovery"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> RecoveryState:
        if raw is None:
            return RecoveryState()
        if not isinstance(raw, dict):
            raise ValueError("recovery state must be an object")
        version = str(raw.get("schema_version") or "1.0")
        if version != RECOVERY_STATE_CONTRACT.current:
            raw = RECOVERY_STATE_MIGRATIONS.migrate(raw, from_version=version, to_version=RECOVERY_STATE_CONTRACT.current)
        return RecoveryState.model_validate(raw)

    def load(self) -> RecoveryState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[RecoveryState], RecoveryState],
    ) -> RecoveryState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=RecoveryState().model_dump(mode="json"),
        )
        return self._decode(raw)
