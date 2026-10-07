from __future__ import annotations

from codex_web.skill_catalog import SkillSource, UiSkillsDiscoveryRequest
from codex_web.storage.state_store import StateStore


class SkillSourceStore:
    NAMESPACE = "skill_sources"

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _key(organization_id: str, workspace_id: str, source_id: str) -> str:
        return f"{organization_id}:{workspace_id}:{source_id}"

    def list(self, organization_id: str, workspace_id: str) -> list[SkillSource]:
        prefix = f"{organization_id}:{workspace_id}:"
        return sorted(
            (
                SkillSource.model_validate(value)
                for key, value in self.state.record_items(self.NAMESPACE).items()
                if key.startswith(prefix)
            ),
            key=lambda item: (item.name.casefold(), item.source_id),
        )

    def get(
        self, organization_id: str, workspace_id: str, source_id: str
    ) -> SkillSource | None:
        payload = self.state.record_get(
            self.NAMESPACE, self._key(organization_id, workspace_id, source_id)
        )
        return SkillSource.model_validate(payload) if payload is not None else None

    def put(self, item: SkillSource) -> SkillSource:
        self.state.record_apply(
            self.NAMESPACE,
            upserts={
                self._key(
                    item.organization_id, item.workspace_id, item.source_id
                ): item.model_dump(mode="json")
            },
        )
        return item

    def get_discovery(
        self, organization_id: str, workspace_id: str, source_id: str
    ) -> UiSkillsDiscoveryRequest | None:
        payload = self.state.record_get(
            "skill_source_discoveries",
            self._key(organization_id, workspace_id, source_id),
        )
        return (
            UiSkillsDiscoveryRequest.model_validate(payload)
            if payload is not None
            else None
        )

    def put_discovery(
        self,
        organization_id: str,
        workspace_id: str,
        source_id: str,
        discovery: UiSkillsDiscoveryRequest,
    ) -> UiSkillsDiscoveryRequest:
        self.state.record_apply(
            "skill_source_discoveries",
            upserts={
                self._key(
                    organization_id, workspace_id, source_id
                ): discovery.model_dump(mode="json")
            },
        )
        return discovery
