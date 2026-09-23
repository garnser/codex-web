from __future__ import annotations

import time
from typing import Any

from codex_web.action_intents import (
    ActionIntentStatus,
    TERMINAL_ACTION_INTENT_STATUSES,
)
from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.attention import TERMINAL_ATTENTION_STATUSES
from codex_web.company_operations import (
    CompanyFactDiagnostic,
    CompanyOperationsExplainStage,
    CompanyOperationsCounts,
    CompanyOperationsExplain,
    CompanyOperationsHealth,
    CompanyOperationsOverview,
    CompanySourceDiagnostic,
)
from codex_web.executive_roles import ExecutiveActivationStatus
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.attention import AttentionService
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_data_sources import BusinessDataSourceService
from codex_web.services.business_kpis import BusinessKPIService
from codex_web.services.decisions import DecisionService
from codex_web.services.executive_management import ExecutiveManagementService
from codex_web.services.extensions import ExtensionService
from codex_web.services.goals import GoalService
from codex_web.services.provider_capacity import ProviderCapacityService


class CompanyOperationsError(RuntimeError):
    pass


class CompanyOperationsNotFoundError(CompanyOperationsError):
    pass


class CompanyOperationsValidationError(CompanyOperationsError):
    pass


class CompanyOperationsService:
    MAX_ENTITIES = 500
    MAX_FACT_DIAGNOSTICS = 500
    MAX_EXTERNAL_RECORDS = 500
    MAX_TIMELINE_ITEMS = 500

    def __init__(
        self,
        business_context: BusinessContextService,
        business_data_sources: BusinessDataSourceService,
        business_kpis: BusinessKPIService,
        goals: GoalService,
        decisions: DecisionService,
        executives: ExecutiveManagementService,
        attention: AttentionService,
        provider_capacity: ProviderCapacityService,
        approvals: ApprovalRequestService,
        action_intents: ActionIntentService,
        artifact_evidence: ArtifactEvidenceService,
        extension_service: ExtensionService | None = None,
        *,
        clock=time.time,
    ) -> None:
        self.business_context = business_context
        self.business_data_sources = business_data_sources
        self.business_kpis = business_kpis
        self.goals = goals
        self.decisions = decisions
        self.executives = executives
        self.attention = attention
        self.provider_capacity = provider_capacity
        self.approvals = approvals
        self.action_intents = action_intents
        self.artifact_evidence = artifact_evidence
        self.extension_service = extension_service
        self.clock = clock

    @staticmethod
    def _serialize(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return item
        raise CompanyOperationsValidationError(
            f"unsupported Company Operations object: {type(item).__name__}"
        )

    def _fact_diagnostics(
        self,
        *,
        actor: AuthenticationActor,
        entities,
        at: float,
    ) -> tuple[CompanyFactDiagnostic, ...]:
        by_entity = {item.id: item for item in entities}
        facts = self.business_context.list_facts(
            actor=actor,
            include_inactive=True,
            limit=self.MAX_FACT_DIAGNOSTICS,
        )
        pairs = tuple(
            dict.fromkeys(
                (item.business_entity_id, item.key.casefold())
                for item in facts
                if item.business_entity_id in by_entity
            )
        )
        rows: list[CompanyFactDiagnostic] = []
        for entity_id, key in pairs[: self.MAX_FACT_DIAGNOSTICS]:
            entity = by_entity[entity_id]
            resolution = self.business_context.resolve_fact(
                entity_id,
                key,
                actor=actor,
                at=at,
            )
            selected = resolution.selected
            rows.append(
                CompanyFactDiagnostic(
                    business_entity_id=entity_id,
                    entity_name=entity.name,
                    entity_type=entity.entity_type.value,
                    fact_key=key,
                    freshness=resolution.freshness.value,
                    conflict=resolution.conflict,
                    selected_fact_id=selected.id if selected else None,
                    selected_value=selected.value if selected else None,
                    unit=selected.unit if selected else None,
                    provider=(
                        selected.source.provider if selected else None
                    ),
                    external_record_ref_id=(
                        selected.source.external_record_ref_id
                        if selected
                        else None
                    ),
                    classification=(
                        selected.classification.value if selected else None
                    ),
                    candidate_fact_ids=resolution.candidate_fact_ids,
                    conflict_fact_ids=resolution.conflict_fact_ids,
                    stale_fact_ids=resolution.stale_fact_ids,
                    revoked_source_fact_ids=resolution.revoked_source_fact_ids,
                    reason=resolution.reason,
                )
            )
        rows.sort(
            key=lambda item: (
                item.freshness == "fresh" and not item.conflict,
                item.entity_name.casefold(),
                item.fact_key,
            )
        )
        return tuple(rows)

    def _source_diagnostics(
        self,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CompanySourceDiagnostic, ...]:
        rows: list[CompanySourceDiagnostic] = []
        for source in self.business_data_sources.list(actor=actor):
            extension = None
            extension_grants = ()
            if source.extension_installation_id:
                if self.extension_service is None:
                    extension_issue = "extension installation is linked but ExtensionService is unavailable"
                else:
                    try:
                        extension = self.extension_service.get(
                            source.extension_installation_id,
                            actor,
                        )
                        extension_grants = tuple(
                            grant
                            for grant in self.extension_service.grants(
                                source.extension_installation_id,
                                actor,
                            )
                            if grant.active
                        )
                        extension_issue = None
                    except Exception as exc:
                        extension_issue = (
                            "linked extension installation unavailable: "
                            + str(exc)
                        )
            else:
                extension_issue = None

            capacity = self.provider_capacity.get(
                source.provider_id,
                source.id,
                actor=actor,
            )
            drift = self.business_data_sources.drift(
                source.id,
                actor=actor,
            )
            conflict_count = sum(
                len(item.get("conflicts") or ())
                for item in drift
            )
            issues: list[str] = []
            if source.status.value != "active":
                issues.append(f"source status is {source.status.value}")
            if source.last_error:
                issues.append(source.last_error)
            if (
                getattr(source, "credential_required", True)
                and source.credential_ref is None
            ):
                issues.append("credential reference is not configured")
            if extension_issue:
                issues.append(extension_issue)
            if extension is not None and extension.lifecycle.value != "enabled":
                issues.append(
                    f"linked extension lifecycle is {extension.lifecycle.value}"
                )
            if extension is not None and extension.incompatible_reason:
                issues.append(
                    "linked extension is incompatible: "
                    + extension.incompatible_reason
                )
            if capacity is not None and capacity.status.value != "available":
                detail = capacity.status.value
                if capacity.reason:
                    detail += f": {capacity.reason}"
                issues.append(f"provider capacity {detail}")
            if conflict_count:
                issues.append(
                    f"{conflict_count} reconciliation conflict(s) require inspection"
                )
            if source.stale_events:
                issues.append(
                    f"{source.stale_events} stale/out-of-order event(s) observed"
                )
            if (
                source.status.value in {"quarantined", "paused"}
                or (
                    extension is not None
                    and extension.lifecycle.value in {
                        "quarantined",
                        "incompatible",
                        "disabled",
                        "removed",
                    }
                )
            ):
                health = CompanyOperationsHealth.BLOCKED
            elif source.last_error or (
                capacity is not None
                and capacity.status.value != "available"
            ) or conflict_count:
                health = CompanyOperationsHealth.DEGRADED
            else:
                health = CompanyOperationsHealth.HEALTHY
            rows.append(
                CompanySourceDiagnostic(
                    source_id=source.id,
                    name=source.name,
                    source_type=source.source_type,
                    provider_id=source.provider_id,
                    provider_instance=source.source_instance,
                    extension_installation_id=source.extension_installation_id,
                    extension_id=(
                        extension.manifest.id if extension is not None else None
                    ),
                    extension_version=(
                        extension.manifest.version if extension is not None else None
                    ),
                    extension_lifecycle=(
                        extension.lifecycle.value if extension is not None else None
                    ),
                    extension_health=(
                        extension.health_status.value if extension is not None else None
                    ),
                    extension_requested_capabilities=(
                        tuple(extension.manifest.capabilities.requested)
                        if extension is not None
                        else ()
                    ),
                    extension_granted_capabilities=tuple(
                        grant.capability for grant in extension_grants
                    ),
                    extension_configuration_record_ids=(
                        tuple(extension.configuration_record_ids)
                        if extension is not None
                        else ()
                    ),
                    object_type=source.object_type,
                    entity_type=source.entity_type.value,
                    status=source.status.value,
                    capabilities=tuple(
                        item.value for item in source.capabilities
                    ),
                    credential_ref=source.credential_ref,
                    cursor=source.cursor,
                    checkpoint=source.checkpoint,
                    last_success_at=source.last_success_at,
                    last_error=source.last_error,
                    last_error_at=source.last_error_at,
                    projected_records=source.projected_records,
                    stale_events=source.stale_events,
                    duplicate_events=source.duplicate_events,
                    capacity_status=(
                        capacity.status.value if capacity else None
                    ),
                    capacity_reason=(
                        capacity.reason if capacity else None
                    ),
                    retry_at=capacity.retry_at if capacity else None,
                    consecutive_failures=(
                        capacity.consecutive_failures if capacity else 0
                    ),
                    drift_count=len(drift),
                    conflict_count=conflict_count,
                    health=health,
                    issues=tuple(dict.fromkeys(issues)),
                )
            )
        rows.sort(key=lambda item: (item.health.value, item.name.casefold()))
        return tuple(rows)

    def overview(
        self,
        *,
        actor: AuthenticationActor,
    ) -> CompanyOperationsOverview:
        now = float(self.clock())
        entities = self.business_context.list_entities(
            actor=actor,
            include_inactive=True,
            limit=self.MAX_ENTITIES,
        )
        external_records = self.business_context.list_external_records(
            actor=actor,
            include_inactive=True,
            limit=self.MAX_EXTERNAL_RECORDS,
        )
        fact_diagnostics = self._fact_diagnostics(
            actor=actor,
            entities=entities,
            at=now,
        )
        sources = self._source_diagnostics(actor=actor)
        kpi_view = self.business_kpis.operating_view(
            actor=actor,
            at=now,
        )

        goals = tuple(
            self._serialize(item)
            for item in self.goals.list(scope=actor.tenant)
        )
        decisions = tuple(
            self._serialize(item)
            for item in self.decisions.list(actor=actor)
        )
        executive_rows = tuple(
            item
            for item in self.executives.list(actor=actor)
            if item.status in {
                ExecutiveActivationStatus.PLANNED,
                ExecutiveActivationStatus.CONSULTING,
                ExecutiveActivationStatus.ESCALATED,
            }
        )
        attention_rows = tuple(
            item
            for item in self.attention.list(actor)
            if item.status not in TERMINAL_ATTENTION_STATUSES
        )
        approval_rows = tuple(
            item
            for item in self.approvals.list(actor)
            if item.status in {
                ApprovalRequestStatus.PENDING,
                ApprovalRequestStatus.PARTIALLY_APPROVED,
                ApprovalRequestStatus.APPROVED,
            }
        )
        action_rows = tuple(
            item
            for item in self.action_intents.list(actor)
            if item.status not in TERMINAL_ACTION_INTENT_STATUSES
        )

        domains = {
            item.domain.value
            for item in self.business_kpis.list(actor=actor)
        }
        for activation in executive_rows:
            domains.update(activation.business_domains)

        blockers: list[str] = []
        blockers.extend(kpi_view.blockers)
        for source in sources:
            if source.health != CompanyOperationsHealth.HEALTHY:
                blockers.append(
                    f"source {source.name}: {source.health.value}"
                )
        for fact in fact_diagnostics:
            if fact.freshness != "fresh" or fact.conflict:
                state = (
                    "conflict"
                    if fact.conflict
                    else fact.freshness
                )
                blockers.append(
                    f"fact {fact.entity_name}/{fact.fact_key}: {state}"
                )
        for intent in action_rows:
            if intent.status in {
                ActionIntentStatus.UNCERTAIN,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
            }:
                blockers.append(
                    f"action {intent.id}: {intent.status.value}"
                )

        if any(
            source.health == CompanyOperationsHealth.BLOCKED
            for source in sources
        ):
            overall = CompanyOperationsHealth.BLOCKED
        elif blockers:
            overall = CompanyOperationsHealth.DEGRADED
        elif entities or sources or kpi_view.items:
            overall = CompanyOperationsHealth.HEALTHY
        else:
            overall = CompanyOperationsHealth.UNKNOWN

        return CompanyOperationsOverview(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            evaluated_at=now,
            overall_health=overall,
            counts=CompanyOperationsCounts(
                business_entities=len(entities),
                external_records=len(external_records),
                fact_diagnostics=len(fact_diagnostics),
                sources=len(sources),
                kpis=len(kpi_view.items),
                goals=len(goals),
                decisions=len(decisions),
                executive_activations=len(executive_rows),
                attention_items=len(attention_rows),
                pending_approvals=len(approval_rows),
                action_intents=len(action_rows),
            ),
            business_domains=tuple(sorted(domains)),
            business_entities=tuple(
                self._serialize(item) for item in entities
            ),
            fact_diagnostics=fact_diagnostics,
            external_records=tuple(
                self._serialize(item) for item in external_records
            ),
            sources=sources,
            kpi_view=kpi_view.model_dump(mode="json"),
            goals=goals,
            decisions=decisions,
            executive_activations=tuple(
                self._serialize(item) for item in executive_rows
            ),
            attention_items=tuple(
                self._serialize(item) for item in attention_rows
            ),
            approval_requests=tuple(
                self._serialize(item) for item in approval_rows
            ),
            action_intents=tuple(
                self._serialize(item) for item in action_rows
            ),
            blockers=tuple(dict.fromkeys(blockers)),
        )

    @staticmethod
    def _proposal_refs(activation) -> tuple[str, ...]:
        refs: list[str] = []
        refs.extend(activation.goal_ids)
        refs.extend(activation.decision_ids)
        refs.extend(activation.work_item_refs)
        refs.extend(activation.evidence_ids)
        for proposal in activation.proposals:
            if proposal.resulting_ref:
                refs.append(proposal.resulting_ref)
                if ":" in proposal.resulting_ref:
                    refs.append(proposal.resulting_ref.split(":", 1)[1])
        return tuple(dict.fromkeys(refs))

    def _related_intents(
        self,
        refs: tuple[str, ...],
        *,
        actor: AuthenticationActor,
    ):
        refset = set(refs)
        rows = []
        for item in self.action_intents.list(actor):
            if any(
                value and value in refset
                for value in (
                    item.id,
                    item.goal_id,
                    item.decision_id,
                    item.work_item_ref,
                    item.execution_id,
                    item.correlation_id,
                    item.causation_id,
                )
            ):
                rows.append(item)
        return tuple(rows)

    def _related_approvals(
        self,
        refs: tuple[str, ...],
        *,
        actor: AuthenticationActor,
    ):
        refset = set(refs)
        rows = []
        for item in self.approvals.list(actor):
            if (
                item.id in refset
                or item.target.object_id in refset
                or (
                    item.resulting_operation_reference
                    and item.resulting_operation_reference in refset
                )
            ):
                rows.append(item)
        return tuple(rows)

    def _intent_stages(
        self,
        intent,
        *,
        actor: AuthenticationActor,
    ) -> list[CompanyOperationsExplainStage]:
        history = self.action_intents.history(intent.id, actor)
        stages = [
            CompanyOperationsExplainStage(
                kind="action_intent",
                label="Canonical external action intent",
                object_id=intent.id,
                status=intent.status.value,
                summary=intent.action_definition.title,
                refs=tuple(
                    value
                    for value in (
                        intent.goal_id,
                        intent.decision_id,
                        intent.work_item_ref,
                        intent.execution_id,
                    )
                    if value
                ),
                details={
                    "provider_type": intent.provider_type,
                    "provider_instance": intent.provider_instance,
                    "action_id": intent.action_id,
                    "risk_class": intent.action_definition.risk_class.value,
                    "resource_ids": list(intent.resource_ids),
                    "required_authority": list(
                        intent.action_definition.required_authority
                    ),
                    "required_authority_level": (
                        intent.action_definition.required_authority_level
                    ),
                    "reversible": intent.action_definition.reversible,
                    "verification_required": intent.verification_required,
                    "authority_decision": (
                        intent.authority_decision.model_dump(mode="json")
                    ),
                    "authority_recheck": (
                        intent.authority_recheck.model_dump(mode="json")
                        if intent.authority_recheck
                        else None
                    ),
                    "policy_decision": (
                        intent.policy_decision.model_dump(mode="json")
                    ),
                    "security_decision": (
                        intent.security_decision.model_dump(mode="json")
                    ),
                },
            )
        ]
        for receipt in history["receipts"]:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="provider_receipt",
                    label="Provider receipt",
                    object_id=receipt.get("id"),
                    status=str(receipt.get("outcome") or "unknown"),
                    summary=(
                        receipt.get("result", {}).get("status")
                        if isinstance(receipt.get("result"), dict)
                        else None
                    ),
                    refs=tuple(
                        value
                        for value in (
                            receipt.get("provider_external_id"),
                            receipt.get("correlation_id"),
                        )
                        if value
                    ),
                    details=receipt,
                )
            )
        for verification in history["verifications"]:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="action_verification",
                    label="Action verification",
                    object_id=verification.get("id"),
                    status=str(
                        verification.get("verification", {}).get("verified")
                        if isinstance(verification.get("verification"), dict)
                        else "recorded"
                    ),
                    details=verification,
                )
            )

        evidence = [
            item
            for item in self.artifact_evidence.list_evidence(actor)
            if (
                (intent.work_item_ref and item.work_item_ref == intent.work_item_ref)
                or (intent.execution_id and item.execution_id == intent.execution_id)
            )
        ]
        for item in evidence[: self.MAX_TIMELINE_ITEMS]:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="evidence",
                    label="Evidence / result",
                    object_id=item.id,
                    status=item.result.value,
                    summary=item.summary,
                    refs=tuple(
                        value
                        for value in (
                            item.external_id,
                            item.deep_link,
                            item.execution_id,
                            item.work_item_ref,
                        )
                        if value
                    ),
                    details=item.model_dump(mode="json"),
                )
            )
        return stages

    def explain_action_intent(
        self,
        intent_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CompanyOperationsExplain:
        try:
            intent = self.action_intents.get(intent_id, actor)
        except Exception as exc:
            raise CompanyOperationsNotFoundError(
                "action intent not found"
            ) from exc
        refs = tuple(
            value
            for value in (
                intent.id,
                intent.goal_id,
                intent.decision_id,
                intent.work_item_ref,
                intent.execution_id,
            )
            if value
        )
        approvals = self._related_approvals(refs, actor=actor)
        stages: list[CompanyOperationsExplainStage] = []
        for approval in approvals:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="approval",
                    label="Approval / authority gate",
                    object_id=approval.id,
                    status=approval.status.value,
                    summary=approval.reason,
                    refs=tuple(approval.target.resource_ids),
                    details=approval.model_dump(mode="json"),
                )
            )
        stages.extend(self._intent_stages(intent, actor=actor))
        unresolved = []
        if intent.status in {
            ActionIntentStatus.UNCERTAIN,
            ActionIntentStatus.REQUIRES_RECONCILIATION,
        }:
            unresolved.append(
                f"action outcome is {intent.status.value}; reconciliation required"
            )
        if intent.last_error:
            unresolved.append(intent.last_error)
        return CompanyOperationsExplain(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            subject_type="action_intent",
            subject_id=intent.id,
            stages=tuple(stages),
            unresolved=tuple(dict.fromkeys(unresolved)),
            evaluated_at=float(self.clock()),
        )

    def explain_executive_activation(
        self,
        activation_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CompanyOperationsExplain:
        try:
            activation = self.executives.get(
                activation_id,
                actor=actor,
            )
        except Exception as exc:
            raise CompanyOperationsNotFoundError(
                "Executive activation not found"
            ) from exc

        stages: list[CompanyOperationsExplainStage] = [
            CompanyOperationsExplainStage(
                kind="trigger",
                label="Source request / event",
                object_id=activation.trigger_ref,
                status=activation.trigger_kind.value,
                summary=activation.request,
                refs=tuple(
                    value
                    for value in (
                        activation.event_type,
                        activation.project_id,
                    )
                    if value
                ),
                details={
                    "subject": activation.subject,
                    "trigger_kind": activation.trigger_kind.value,
                    "event_type": activation.event_type,
                },
            )
        ]

        for row in activation.context.business_entities:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="business_entity",
                    label="Governed business entity",
                    object_id=str(row.get("id") or ""),
                    status=str(row.get("lifecycle") or "unknown"),
                    summary=str(row.get("name") or ""),
                    refs=tuple(
                        value
                        for value in (
                            row.get("citation"),
                            row.get("governance_record_id"),
                        )
                        if value
                    ),
                    details=row,
                )
            )
        for row in activation.context.business_facts:
            selected = row.get("selected")
            stages.append(
                CompanyOperationsExplainStage(
                    kind="company_fact",
                    label="Governed CompanyFact",
                    object_id=(
                        str(selected.get("id"))
                        if isinstance(selected, dict) and selected.get("id")
                        else str(row.get("business_entity_id") or "")
                    ),
                    status=str(row.get("freshness") or "unknown"),
                    summary=(
                        f"{row.get('key')}: "
                        + (
                            str(selected.get("value"))
                            if isinstance(selected, dict)
                            else "no selected value"
                        )
                    ),
                    refs=tuple(
                        value
                        for value in (
                            row.get("citation"),
                            (
                                selected.get("source", {}).get(
                                    "external_record_ref_id"
                                )
                                if isinstance(selected, dict)
                                and isinstance(selected.get("source"), dict)
                                else None
                            ),
                        )
                        if value
                    ),
                    details=row,
                )
            )
        for row in activation.context.business_kpis:
            current = row.get("current") or {}
            stages.append(
                CompanyOperationsExplainStage(
                    kind="business_kpi",
                    label="Canonical business KPI / Metric",
                    object_id=str(row.get("id") or ""),
                    status=str(current.get("readiness") or "unknown"),
                    summary=f"{row.get('name')}: {current.get('value')}",
                    refs=tuple(
                        value
                        for value in (
                            row.get("citation"),
                            row.get("metric_id"),
                        )
                        if value
                    ),
                    details=row,
                )
            )

        for selection in activation.selections:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="executive_role",
                    label="Executive role selection",
                    object_id=selection.role_id,
                    status="selected",
                    summary="; ".join(selection.reasons),
                    details=selection.model_dump(mode="json"),
                )
            )
        for consultation in activation.consultations:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="executive_recommendation",
                    label="Executive recommendation",
                    object_id=consultation.id,
                    status="advisory",
                    summary=consultation.output.recommendation,
                    refs=consultation.output.context_refs,
                    details=consultation.model_dump(mode="json"),
                )
            )

        refs = list(self._proposal_refs(activation))
        for proposal in activation.proposals:
            stages.append(
                CompanyOperationsExplainStage(
                    kind="executive_proposal",
                    label="Advisory proposal / authority result",
                    object_id=proposal.id,
                    status=proposal.status.value,
                    summary=proposal.title,
                    refs=tuple(
                        value
                        for value in (
                            proposal.resulting_ref,
                            proposal.authority_capability,
                        )
                        if value
                    ),
                    details=proposal.model_dump(mode="json"),
                )
            )
            if proposal.resulting_ref:
                refs.append(proposal.resulting_ref)
                if ":" in proposal.resulting_ref:
                    refs.append(proposal.resulting_ref.split(":", 1)[1])

        related_refs = tuple(dict.fromkeys(refs))
        for approval in self._related_approvals(
            related_refs,
            actor=actor,
        ):
            stages.append(
                CompanyOperationsExplainStage(
                    kind="approval",
                    label="Approval / authority gate",
                    object_id=approval.id,
                    status=approval.status.value,
                    summary=approval.reason,
                    refs=tuple(approval.target.resource_ids),
                    details=approval.model_dump(mode="json"),
                )
            )

        intents = self._related_intents(related_refs, actor=actor)
        for intent in intents:
            stages.extend(self._intent_stages(intent, actor=actor))

        unresolved = list(activation.context.business_context_denials)
        unresolved_text = [
            str(item.get("reason") or item)
            for item in unresolved
        ]
        if activation.failure_reason:
            unresolved_text.append(activation.failure_reason)
        for intent in intents:
            if intent.status in {
                ActionIntentStatus.UNCERTAIN,
                ActionIntentStatus.REQUIRES_RECONCILIATION,
            }:
                unresolved_text.append(
                    f"{intent.id}: {intent.status.value}"
                )
        return CompanyOperationsExplain(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            subject_type="executive_activation",
            subject_id=activation.id,
            stages=tuple(stages[: self.MAX_TIMELINE_ITEMS]),
            unresolved=tuple(dict.fromkeys(unresolved_text)),
            evaluated_at=float(self.clock()),
        )
