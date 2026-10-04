from __future__ import annotations

import time
from typing import Any, Callable

from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionScope,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.definitions import DefinitionKindSchema, DefinitionNotFoundError, DefinitionRegistryService
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.skill_security import (
    SEVERITY_RANK,
    SKILL_SECURITY_POLICY_ID,
    SKILL_SECURITY_POLICY_KIND,
    SKILL_SECURITY_POLICY_SCHEMA_VERSION,
    SkillFindingSeverity,
    SkillScanRequest,
    SkillScanResult,
    SkillScanStatus,
    SkillSecurityAuditEvent,
    SkillSecurityBaseline,
    SkillSecurityBaselineRequest,
    SkillSecurityOverride,
    SkillSecurityOverrideRequest,
    SkillSecurityPolicy,
    SkillSecurityPolicyUpdate,
)
from codex_web.skills import SkillOrigin
from codex_web.storage.skill_security import SkillSecurityStore


class SkillSecurityError(RuntimeError):
    pass


class SkillSecurityBlocked(SkillSecurityError):
    pass


def validate_skill_security_policy(payload: dict[str, Any]) -> dict[str, Any]:
    return SkillSecurityPolicy.model_validate(payload).model_dump(mode="json")


class SkillSecurityService:
    def __init__(
        self,
        store: SkillSecurityStore,
        definitions: DefinitionRegistryService,
        providers: SkillScannerRegistry,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.providers = providers
        self.clock = clock
        try:
            definitions.schemas.get(SKILL_SECURITY_POLICY_KIND, SKILL_SECURITY_POLICY_SCHEMA_VERSION)
        except Exception:
            definitions.register_schema(
                DefinitionKindSchema(
                    kind=SKILL_SECURITY_POLICY_KIND,
                    schema_version=SKILL_SECURITY_POLICY_SCHEMA_VERSION,
                    validate=validate_skill_security_policy,
                )
            )

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        allowed = (
            "skills:admin" in actor.service_scopes
            if actor.principal_kind == PrincipalKind.SERVICE
            else actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)
        )
        if not allowed:
            raise AuthorizationError("Skill security administrator required")
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)

    def policy(self, actor: AuthenticationActor) -> SkillSecurityPolicy:
        try:
            record = self.definitions.resolve(
                definition_id=SKILL_SECURITY_POLICY_ID,
                kind=SKILL_SECURITY_POLICY_KIND,
                context=DefinitionContext(
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                ),
                now=float(self.clock()),
            )
        except DefinitionNotFoundError:
            return SkillSecurityPolicy()
        return SkillSecurityPolicy.model_validate(record.payload)

    def update_policy(self, payload: SkillSecurityPolicyUpdate, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        policy = SkillSecurityPolicy.model_validate(payload.model_dump(mode="python", exclude={"reason"}))
        records = [
            item
            for item in self.definitions.list_records(
                kind=SKILL_SECURITY_POLICY_KIND,
                definition_id=SKILL_SECURITY_POLICY_ID,
            )
            if item.scope_type == DefinitionScope.WORKSPACE and item.scope_id == actor.workspace_id
        ]
        active = next((item for item in records if item.lifecycle == DefinitionLifecycle.PUBLISHED), None)
        latest = max(records, key=lambda item: item.revision) if records else None
        draft = self.definitions.create_draft(
            DefinitionDraftCreate(
                definition_id=SKILL_SECURITY_POLICY_ID,
                kind=SKILL_SECURITY_POLICY_KIND,
                definition_schema_version=SKILL_SECURITY_POLICY_SCHEMA_VERSION,
                scope_type=DefinitionScope.WORKSPACE,
                scope_id=actor.workspace_id,
                payload=policy.model_dump(mode="json"),
                actor=actor.identity_id,
                reason=payload.reason,
                derived_from_record_id=(latest.record_id if latest else None),
            )
        )
        published = self.definitions.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=actor.identity_id,
                reason=payload.reason,
                expected_active_revision=(active.revision if active else None),
            ),
        )
        return {
            "policy": policy.model_dump(mode="json"),
            "definitionReference": {
                "definition_id": published.definition_id,
                "record_id": published.record_id,
                "revision": published.revision,
                "checksum": published.checksum,
                "definition_schema_version": published.definition_schema_version,
            },
        }

    @staticmethod
    def _content(view: dict[str, Any]) -> str:
        skill = view["skill"]
        assets = "\n\n".join(
            f"## Asset: {item['path']}\n{item['content']}"
            for item in skill.get("assets", [])
        )
        return f"# {skill['name']}\n\n{skill.get('description', '')}\n\n{skill['instructions']}\n\n{assets}".strip()

    @staticmethod
    def _highest(findings) -> SkillFindingSeverity:
        return max(
            (item.severity for item in findings),
            key=lambda value: SEVERITY_RANK[value],
            default=SkillFindingSeverity.INFO,
        )

    def _event(self, view: dict[str, Any], actor: AuthenticationActor, event_type: str, details: dict[str, Any], now: float) -> None:
        self.store.add_event(
            SkillSecurityAuditEvent(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                record_id=view["recordId"],
                event_type=event_type,
                actor_id=actor.identity_id,
                details=details,
                occurred_at=now,
            )
        )

    def scan(self, view: dict[str, Any], request: SkillScanRequest, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        policy = self.policy(actor)
        provider_id = policy.provider_id
        now = float(self.clock())
        previous = self._latest(view, actor)
        try:
            provider = self.providers.get(provider_id, tenant_scope=actor.tenant)
            provider_result = provider.scan(content=self._content(view), mode=request.mode)
            baseline = self.store.baseline(actor.organization_id, actor.workspace_id, view["recordId"])
            suppressed = set(baseline.finding_fingerprints if baseline else ())
            active_findings = tuple(item for item in provider_result.findings if item.fingerprint not in suppressed)
            severity = self._highest(active_findings)
            blocked = (
                provider_result.risk_score > policy.max_risk_score
                or any(item.severity in policy.blocked_severities for item in active_findings)
                or any(item.category.casefold() in policy.blocked_categories for item in active_findings)
            )
            if blocked:
                status, policy_result = SkillScanStatus.QUARANTINED, "blocked"
            elif active_findings:
                status, policy_result = SkillScanStatus.WARNING, "allowed_with_findings"
            else:
                status, policy_result = SkillScanStatus.PASSED, "allowed"
            result = SkillScanResult(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                skill_id=view["skillId"],
                record_id=view["recordId"],
                checksum=view["checksum"],
                provider_id=provider_id,
                scanner_version=provider_result.scanner_version,
                mode=request.mode,
                status=status,
                risk_score=provider_result.risk_score,
                severity=severity,
                findings=provider_result.findings,
                policy_result=policy_result,
                report_artifact_id=provider_result.report_artifact_id,
                scanned_at=now,
            )
        except Exception as exc:
            result = SkillScanResult(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                skill_id=view["skillId"],
                record_id=view["recordId"],
                checksum=view["checksum"],
                provider_id=provider_id,
                scanner_version="unavailable",
                mode=request.mode,
                status=SkillScanStatus.SCAN_ERROR,
                risk_score=100 if policy.fail_closed_on_scanner_error else 0,
                severity=(SkillFindingSeverity.CRITICAL if policy.fail_closed_on_scanner_error else SkillFindingSeverity.INFO),
                findings=(),
                policy_result="blocked" if policy.fail_closed_on_scanner_error else "allowed_after_error",
                scanned_at=now,
                error=str(exc)[:1000],
            )
        self.store.put_scan(result)
        self._event(
            view,
            actor,
            "skill_scan_completed",
            {
                "scan_id": result.id,
                "status": result.status.value,
                "provider_id": result.provider_id,
                "risk_score": result.risk_score,
                "finding_count": len(result.findings),
                "policy_result": result.policy_result,
            },
            now,
        )
        self._event(view, actor, "skill_first_scan" if previous is None else "skill_rescanned", {"scan_id": result.id}, now)
        if previous is not None and previous.scanner_version != result.scanner_version:
            self._event(view, actor, "skill_scanner_version_changed", {"from": previous.scanner_version, "to": result.scanner_version}, now)
        if previous is not None:
            before = {item.fingerprint for item in previous.findings}
            after = {item.fingerprint for item in result.findings}
            if before != after:
                self._event(view, actor, "skill_findings_changed", {"new_count": len(after - before), "resolved_count": len(before - after)}, now)
        if result.status == SkillScanStatus.QUARANTINED:
            self._event(view, actor, "skill_quarantined", {"scan_id": result.id}, now)
        return self.summary(view, actor=actor)

    def _latest(self, view: dict[str, Any], actor: AuthenticationActor) -> SkillScanResult | None:
        rows = self.store.scans(actor.organization_id, actor.workspace_id, record_id=view["recordId"])
        return rows[0] if rows else None

    def _active_override(self, view: dict[str, Any], actor: AuthenticationActor) -> SkillSecurityOverride | None:
        now = float(self.clock())
        rows = [
            item
            for item in self.store.overrides(actor.organization_id, actor.workspace_id, view["recordId"])
            if item.expires_at is None or item.expires_at > now
        ]
        return max(rows, key=lambda item: item.created_at) if rows else None

    def decision(self, view: dict[str, Any], *, actor: AuthenticationActor, phase: str) -> dict[str, Any]:
        policy = self.policy(actor)
        imported = view["skill"].get("provenance", {}).get("origin") != SkillOrigin.LOCAL.value
        required = imported or policy.scan_locally_authored
        if phase == "publish":
            required = required and policy.require_scan_before_publish
        elif phase == "assignment":
            required = required and policy.require_scan_before_assignment
        if not required:
            return {"allowed": True, "reason": "scan_not_required", "overridden": False}
        latest = self._latest(view, actor)
        if latest is not None and latest.checksum == view["checksum"] and latest.policy_result.startswith("allowed"):
            return {"allowed": True, "reason": latest.policy_result, "overridden": False}
        override = self._active_override(view, actor)
        if policy.allow_audited_override and override is not None:
            return {"allowed": True, "reason": "audited_override", "overridden": True, "overrideId": override.id}
        reason = "current_revision_not_scanned" if latest is None else f"security_{latest.status.value}"
        return {"allowed": False, "reason": reason, "overridden": False}

    def assert_allowed(self, view: dict[str, Any], *, actor: AuthenticationActor, phase: str) -> None:
        decision = self.decision(view, actor=actor, phase=phase)
        if not decision["allowed"]:
            raise SkillSecurityBlocked(f"Skill security policy blocks {phase}: {decision['reason']}")

    def summary(self, view: dict[str, Any], *, actor: AuthenticationActor) -> dict[str, Any]:
        latest = self._latest(view, actor)
        any_skill_scan = next(
            (item for item in self.store.scans(actor.organization_id, actor.workspace_id) if item.skill_id == view["skillId"]),
            None,
        )
        if latest is None:
            status = SkillScanStatus.STALE if any_skill_scan is not None else SkillScanStatus.NOT_SCANNED
            return {
                "status": status.value,
                "currentRevision": False,
                "scan": None,
                "assignmentDecision": self.decision(view, actor=actor, phase="assignment"),
            }
        return {
            "status": latest.status.value,
            "currentRevision": latest.checksum == view["checksum"],
            "scan": latest.model_dump(mode="json"),
            "assignmentDecision": self.decision(view, actor=actor, phase="assignment"),
        }

    def report(self, view: dict[str, Any], *, actor: AuthenticationActor) -> dict[str, Any]:
        baseline = self.store.baseline(actor.organization_id, actor.workspace_id, view["recordId"])
        return {
            **self.summary(view, actor=actor),
            "history": [item.model_dump(mode="json") for item in self.store.scans(actor.organization_id, actor.workspace_id, record_id=view["recordId"])],
            "baseline": baseline.model_dump(mode="json") if baseline else None,
            "audit": [item.model_dump(mode="json") for item in self.store.events(actor.organization_id, actor.workspace_id, view["recordId"])],
        }

    def create_override(self, view: dict[str, Any], payload: SkillSecurityOverrideRequest, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        item = self.store.put_override(
            SkillSecurityOverride(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                record_id=view["recordId"],
                actor_id=actor.identity_id,
                reason=payload.reason,
                created_at=float(self.clock()),
                expires_at=payload.expires_at,
            )
        )
        self._event(view, actor, "skill_security_override_created", {"override_id": item.id, "reason": item.reason}, float(self.clock()))
        return item.model_dump(mode="json")

    def set_baseline(self, view: dict[str, Any], payload: SkillSecurityBaselineRequest, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        item = self.store.put_baseline(
            SkillSecurityBaseline(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                record_id=view["recordId"],
                finding_fingerprints=payload.finding_fingerprints,
                actor_id=actor.identity_id,
                reason=payload.reason,
                created_at=float(self.clock()),
            )
        )
        self._event(view, actor, "skill_security_baseline_changed", {"baseline_id": item.id, "suppressed_count": len(item.finding_fingerprints), "reason": item.reason}, float(self.clock()))
        return item.model_dump(mode="json")
