from __future__ import annotations

from typing import Any, Callable

from codex_web.storage.state_store import StateStore
from codex_web.upgrades import UpgradePlan, UpgradeState, UPGRADE_STATE_CONTRACT
from codex_web.compatibility import MigrationRegistry

UPGRADE_STATE_MIGRATIONS = MigrationRegistry("upgrade-state")
UPGRADE_STATE_MIGRATIONS.register("1.0", "1.1", lambda raw: {**raw, "schema_version": "1.1"})


class UpgradeNotFoundError(KeyError):
    pass


class UpgradeStore:
    namespace = "upgrades"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> UpgradeState:
        if raw is None:
            return UpgradeState()
        if not isinstance(raw, dict):
            raise ValueError("upgrade state must be an object")
        version = str(raw.get("schema_version") or "1.0")
        UPGRADE_STATE_CONTRACT.require(version)
        if version != UPGRADE_STATE_CONTRACT.current:
            raw = UPGRADE_STATE_MIGRATIONS.migrate(raw, from_version=version,
                to_version=UPGRADE_STATE_CONTRACT.current)
        return UpgradeState.model_validate(raw)

    def load(self) -> UpgradeState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[UpgradeState], UpgradeState],
    ) -> UpgradeState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=UpgradeState().model_dump(mode="json"),
        )
        return self._decode(raw)

    def get(self, plan_id: str) -> UpgradePlan:
        item = self.load().plans.get(plan_id)
        if item is None:
            raise UpgradeNotFoundError(plan_id)
        return item

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> list[UpgradePlan]:
        rows = [
            item
            for item in self.load().plans.values()
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        return sorted(rows, key=lambda item: (item.created_at, item.id), reverse=True)
