from __future__ import annotations

from typing import Any, Callable

from codex_web.storage.state_store import StateStore
from codex_web.upgrades import UpgradePlan, UpgradeState


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
