from __future__ import annotations

import re
from urllib.parse import urlsplit

from codex_web.artifact_evidence import EvidenceLifecycle, EvidenceResult, EvidenceType
from codex_web.services.artifact_evidence import ArtifactEvidenceError
from codex_web.upgrades import UpgradeStatus


class LocalInstallationInspector:
    """Project canonical verification and explicitly untrusted installer reports."""

    PHASES = {"objects_verified", "installed", "failed"}
    FAILURES = {"origin_mismatch", "fetch_failed", "incomplete_objects", "checkout_changed",
                "qualification_denied", "installation_failed"}

    def __init__(self, upgrades, *, process_reader=None):
        self.upgrades = upgrades
        self.process_reader = process_reader

    def inspect(self, plan_id, *, actor):
        plan = self.upgrades.get(plan_id, actor=actor)
        release = self.upgrades.releases.get(plan.release_id, actor=actor)
        verification = None
        if plan.post_upgrade_evidence_id:
            try:
                evidence = self.upgrades.evidence.get_evidence(plan.post_upgrade_evidence_id, actor=actor)
            except ArtifactEvidenceError:
                evidence = None
            metadata = evidence.metadata if evidence else {}
            valid = bool(evidence and evidence.lifecycle == EvidenceLifecycle.VALID
                     and evidence.evidence_type == EvidenceType.POLICY_EVALUATION
                     and evidence.result == EvidenceResult.PASS
                     and evidence.provider == "codex-web"
                     and evidence.source == "upgrade-post-verification"
                     and metadata.get("upgrade_plan_id") == plan.id
                     and metadata.get("source_app_version") == plan.current_app_version
                     and metadata.get("target_app_version") == plan.target_app_version)
            verification = {"id": plan.post_upgrade_evidence_id, "valid": valid,
                            "href": f"/api/evidence/{plan.post_upgrade_evidence_id}"}
        reports = []
        evidence_items = self.upgrades.evidence.list_evidence(actor, include_inactive=False)
        cancellation = next(({"href": f"/api/evidence/{e.id}",
            "reason": str(e.metadata.get("reason", ""))[:500]}
            for e in evidence_items if e.provider == "codex-web"
            and e.source == "upgrade-unexecuted-cancellation"
            and e.evidence_type == EvidenceType.POLICY_EVALUATION and e.result == EvidenceResult.FAIL
            and e.metadata.get("upgrade_plan_id") == plan.id
            and e.metadata.get("source_app_version") == plan.current_app_version
            and e.metadata.get("target_app_version") == plan.target_app_version
            and e.metadata.get("admission_epoch") == plan.admission_epoch - 1
            and plan.status == UpgradeStatus.FAILED and not plan.maintenance_mode), None)
        for evidence in evidence_items:
            meta = evidence.metadata
            if (evidence.provider != "local-installer" or evidence.source != plan.id
                    or meta.get("upgrade_plan_id") != plan.id
                    or type(meta.get("installer_report_version")) is not int
                    or meta.get("installer_report_version") != 1
                    or meta.get("phase") not in self.PHASES
                    or meta.get("source_revision") != plan.current_app_version
                    or meta.get("target_revision") != plan.target_app_version
                    or release.build.artifact_id not in evidence.artifact_ids):
                continue
            report = {"id": evidence.id, "href": f"/api/evidence/{evidence.id}",
                      "producer_identity_id": evidence.producer_identity_id,
                      "observed_at": evidence.observed_at, "phase": meta["phase"],
                      "reported_only": True,
                      "stale_instance": meta.get("service_instance_id") != plan.service_instance_id}
            digest = meta.get("archive_sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                report["archive_sha256"] = digest
            if type(meta.get("fetched")) is bool:
                report["fetched"] = meta["fetched"]
            if meta.get("failure_code") in self.FAILURES:
                report["failure_code"] = meta["failure_code"]
            origin = meta.get("repository_source")
            if isinstance(origin, str) and len(origin) <= 512:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    parsed = urlsplit("")
                if (parsed.scheme in {"http", "https"} and parsed.hostname
                        and parsed.username is None and parsed.password is None
                        and not parsed.query and not parsed.fragment):
                    report["repository_source"] = origin
            reports.append(report)
            if len(reports) == 8:
                break
        process = ({"status": "unknown", "reason": "No live tracked installer handle"}
                   if self.process_reader is None else self.process_reader(plan))
        verified = bool(plan.status == UpgradeStatus.COMPLETED and verification and verification["valid"])
        return {"plan_id": plan.id, "plan_href": f"/api/upgrades/{plan.id}",
                "release_id": release.id, "release_href": f"/api/releases/{release.id}",
                "source_revision": plan.current_app_version, "target_revision": plan.target_app_version,
                "service_instance_id": plan.service_instance_id, "plan_status": plan.status.value,
                "maintenance_mode": plan.maintenance_mode,
                "admission_epoch": plan.admission_epoch,
                "current_service_instance_id": self.upgrades.service_instance_id,
                "cancellation": cancellation,
                "cancellation_request_available": bool(
                    plan.compatibility.deployment_mode.value == "local"
                    and plan.completed_at is None and not plan.irreversible_boundary_crossed
                    and not plan.definition_migrations
                    and plan.status in {UpgradeStatus.DRAFT, UpgradeStatus.PREFLIGHT_BLOCKED,
                        UpgradeStatus.READY, UpgradeStatus.DRAINING, UpgradeStatus.FAILED}
                    and not any(step.attempts for step in plan.steps)),
                "canonical_verification": verification, "installation_verified": verified,
                "process": process, "reported_diagnostics": reports,
                "preflight_evidence_href": (f"/api/evidence/{plan.preflight.evidence_id}"
                                            if plan.preflight and plan.preflight.evidence_id else None),
                "diagnostics_grant_authority": False}
