from __future__ import annotations

from codex_web.skill_security import (
    SkillScanResult,
    SkillSecurityAuditEvent,
    SkillSecurityBaseline,
    SkillSecurityOverride,
)
from codex_web.storage.state_store import StateStore


class SkillSecurityStore:
    SCANS = "skill_security_scans"
    OVERRIDES = "skill_security_overrides"
    BASELINES = "skill_security_baselines"
    EVENTS = "skill_security_events"

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _prefix(organization_id: str, workspace_id: str, record_id: str = "") -> str:
        return f"{organization_id}:{workspace_id}:{record_id}"

    def put_scan(self, item: SkillScanResult) -> SkillScanResult:
        key = f"{self._prefix(item.organization_id, item.workspace_id, item.record_id)}:{item.scanned_at:020.6f}:{item.id}"
        self.state.record_apply(self.SCANS, upserts={key: item.model_dump(mode="json")})
        return item

    def scans(self, organization_id: str, workspace_id: str, *, record_id: str | None = None) -> list[SkillScanResult]:
        prefix = self._prefix(organization_id, workspace_id, record_id or "")
        if record_id is not None:
            prefix += ":"
        return sorted(
            (
                SkillScanResult.model_validate(value)
                for key, value in self.state.record_items(self.SCANS).items()
                if key.startswith(prefix)
            ),
            key=lambda item: (item.scanned_at, item.id),
            reverse=True,
        )

    def put_override(self, item: SkillSecurityOverride) -> SkillSecurityOverride:
        key = f"{self._prefix(item.organization_id, item.workspace_id, item.record_id)}:{item.id}"
        self.state.record_apply(self.OVERRIDES, upserts={key: item.model_dump(mode="json")})
        return item

    def overrides(self, organization_id: str, workspace_id: str, record_id: str) -> list[SkillSecurityOverride]:
        prefix = self._prefix(organization_id, workspace_id, record_id) + ":"
        return [
            SkillSecurityOverride.model_validate(value)
            for key, value in self.state.record_items(self.OVERRIDES).items()
            if key.startswith(prefix)
        ]

    def put_baseline(self, item: SkillSecurityBaseline) -> SkillSecurityBaseline:
        key = self._prefix(item.organization_id, item.workspace_id, item.record_id)
        self.state.record_apply(self.BASELINES, upserts={key: item.model_dump(mode="json")})
        return item

    def baseline(self, organization_id: str, workspace_id: str, record_id: str) -> SkillSecurityBaseline | None:
        value = self.state.record_get(self.BASELINES, self._prefix(organization_id, workspace_id, record_id))
        return SkillSecurityBaseline.model_validate(value) if value is not None else None

    def add_event(self, item: SkillSecurityAuditEvent) -> SkillSecurityAuditEvent:
        key = f"{self._prefix(item.organization_id, item.workspace_id, item.record_id)}:{item.occurred_at:020.6f}:{item.id}"
        self.state.record_apply(self.EVENTS, upserts={key: item.model_dump(mode="json")})
        return item

    def events(self, organization_id: str, workspace_id: str, record_id: str) -> list[SkillSecurityAuditEvent]:
        prefix = self._prefix(organization_id, workspace_id, record_id) + ":"
        return sorted(
            (
                SkillSecurityAuditEvent.model_validate(value)
                for key, value in self.state.record_items(self.EVENTS).items()
                if key.startswith(prefix)
            ),
            key=lambda item: (item.occurred_at, item.id),
            reverse=True,
        )
