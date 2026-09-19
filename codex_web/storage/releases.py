from __future__ import annotations

from typing import Any, Callable

from codex_web.releases import ReleaseRecord, ReleaseState
from codex_web.storage.state_store import StateStore


class ReleaseNotFoundError(KeyError):
    pass


class ReleaseStore:
    namespace = "releases"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _decode(raw: Any) -> ReleaseState:
        if raw is None:
            return ReleaseState()
        return ReleaseState.model_validate(raw)

    def load(self) -> ReleaseState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ReleaseState], ReleaseState],
    ) -> ReleaseState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(self._decode(current)).model_dump(mode="json"),
            default=ReleaseState().model_dump(mode="json"),
        )
        return self._decode(raw)

    def get(self, release_id: str) -> ReleaseRecord:
        item = self.load().releases.get(release_id)
        if item is None:
            raise ReleaseNotFoundError(release_id)
        return item

    def list(
        self,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> list[ReleaseRecord]:
        rows = [
            item
            for item in self.load().releases.values()
            if item.organization_id == organization_id
            and item.workspace_id == workspace_id
        ]
        return sorted(rows, key=lambda item: (item.created_at, item.id), reverse=True)

    def create(self, release: ReleaseRecord) -> ReleaseRecord:
        def apply(state: ReleaseState) -> ReleaseState:
            if release.id in state.releases:
                raise RuntimeError(f"release already exists: {release.id}")
            state.releases[release.id] = release
            return state

        self.update(apply)
        return release
