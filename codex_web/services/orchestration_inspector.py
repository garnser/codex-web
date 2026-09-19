from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.attention import AttentionStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.evaluations import EvaluationStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.scheduler import SchedulerStore


class OrchestrationInspectorService:
    """Read-only projection over canonical orchestration and execution state.

    Refresh paths only read durable stores and apply deterministic correlation.
    No model, provider, worker, scheduler tick, evaluator run, or ActionIntent
    mutation is reachable from this service.
    """

    def __init__(
        self,
        events: CanonicalEventStore,
        autonomy: AutonomyController,
        *,
        scheduler: SchedulerStore | None = None,
        evaluations: EvaluationStore | None = None,
        attention: AttentionStore | None = None,
        approvals: ApprovalRequestStore | None = None,
        action_intents: ActionIntentStore | None = None,
        agent_sessions: AgentSessionStore | None = None,
        runtime_usage: AgentRuntimeUsageStore | None = None,
        model_gateway: ModelGatewayStore | None = None,
        artifact_evidence: ArtifactEvidenceStore | None = None,
    ) -> None:
        self.events = events
        self.autonomy = autonomy
        self.scheduler = scheduler
        self.evaluations = evaluations
        self.attention = attention
        self.approvals = approvals
        self.action_intents = action_intents
        self.agent_sessions = agent_sessions
        self.runtime_usage = runtime_usage
        self.model_gateway = model_gateway
        self.artifact_evidence = artifact_evidence

    @staticmethod
    def _same_scope(
        item: Any,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> bool:
        if organization_id is None and workspace_id is None:
            return True
        item_org = getattr(item, "organization_id", None)
        item_workspace = getattr(item, "workspace_id", None)
        return (
            (item_org is None or item_org == organization_id)
            and (item_workspace is None or item_workspace == workspace_id)
        )

    @staticmethod
    def _event_same_scope(
        event: Any,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> bool:
        if organization_id is None and workspace_id is None:
            return True
        tenant_id = getattr(event, "tenant_id", None)
        event_workspace = getattr(event, "workspace_id", None)
        return (
            (tenant_id is None or tenant_id == organization_id)
            and (event_workspace is None or event_workspace == workspace_id)
        )

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)

    @staticmethod
    def _contains_ref(value: str | None, refs: Iterable[str]) -> bool:
        if not value:
            return False
        return any(ref and ref in value for ref in refs)

    @staticmethod
    def _event_refs(event: Any) -> set[str]:
        refs = {
            str(getattr(event, "event_id", "") or ""),
            str(getattr(event, "correlation_id", "") or ""),
            str(getattr(event, "causation_id", "") or ""),
        }
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            for key in (
                "execution_id",
                "work_item_ref",
                "ref",
                "goal_id",
                "decision_id",
                "action_intent_id",
                "approval_request_id",
                "attention_item_id",
                "agent_session_id",
            ):
                value = payload.get(key)
                if value:
                    refs.add(str(value))
        return {item for item in refs if item}

    @staticmethod
    def _compact_intent(item: Any) -> dict[str, Any]:
        return {
            "id": item.id,
            "status": OrchestrationInspectorService._value(item.status),
            "provider_type": item.provider_type,
            "provider_instance": item.provider_instance,
            "action_id": item.action_id,
            "binding_id": item.binding_id,
            "project_id": item.project_id,
            "work_item_ref": item.work_item_ref,
            "goal_id": item.goal_id,
            "decision_id": item.decision_id,
            "execution_id": item.execution_id,
            "authority_decision": item.authority_decision.model_dump(mode="json"),
            "authority_recheck": (
                item.authority_recheck.model_dump(mode="json")
                if item.authority_recheck is not None
                else None
            ),
            "policy_decision": item.policy_decision.model_dump(mode="json"),
            "security_decision": item.security_decision.model_dump(mode="json"),
            "verification_required": item.verification_required,
            "expected_evidence": [
                requirement.model_dump(mode="json")
                for requirement in item.expected_evidence
            ],
            "attempt": item.attempt,
            "retry_policy": item.retry_policy.model_dump(mode="json"),
            "not_before": item.not_before,
            "last_error": item.last_error,
            "last_receipt_id": item.last_receipt_id,
            "last_verification_id": item.last_verification_id,
            "correlation_id": item.correlation_id,
            "causation_id": item.causation_id,
            "created_at": item.created_at,
            "completed_at": item.completed_at,
        }

    def _intent_projection(
        self,
        intent_ids: Iterable[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        if self.action_intents is None:
            return [], set()
        wanted = {str(item) for item in intent_ids if item}
        state = self.action_intents.load()
        intents = [
            item
            for item in state.intents
            if item.id in wanted
            and self._same_scope(item, organization_id, workspace_id)
        ]
        intent_refs = set(wanted)
        for item in intents:
            for value in (
                item.execution_id,
                item.work_item_ref,
                item.goal_id,
                item.decision_id,
                item.correlation_id,
                item.causation_id,
            ):
                if value:
                    intent_refs.add(str(value))

        receipts_by_intent: dict[str, list[dict[str, Any]]] = {}
        for receipt in state.receipts:
            if receipt.intent_id in wanted:
                receipts_by_intent.setdefault(receipt.intent_id, []).append(
                    receipt.model_dump(mode="json")
                )
        verifications_by_intent: dict[str, list[dict[str, Any]]] = {}
        for verification in state.verifications:
            if verification.intent_id in wanted:
                verifications_by_intent.setdefault(
                    verification.intent_id, []
                ).append(verification.model_dump(mode="json"))

        result = []
        for item in intents:
            compact = self._compact_intent(item)
            compact["receipts"] = receipts_by_intent.get(item.id, [])
            compact["verifications"] = verifications_by_intent.get(item.id, [])
            result.append(compact)
        return result, intent_refs

    def _approval_projection(
        self,
        refs: set[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.approvals is None:
            return []
        rows = self.approvals.list(
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        matches = []
        for item in rows:
            target = item.target
            if (
                target.object_id in refs
                or self._contains_ref(item.resulting_operation_reference, refs)
                or any(resource_id in refs for resource_id in target.resource_ids)
            ):
                matches.append(item.model_dump(mode="json"))
        return matches

    def _attention_projection(
        self,
        refs: set[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.attention is None:
            return []
        rows = [
            item
            for item in self.attention.list()
            if self._same_scope(item, organization_id, workspace_id)
        ]
        return [
            item.model_dump(mode="json")
            for item in rows
            if (
                item.source.object_id in refs
                or (item.source.event_id and item.source.event_id in refs)
                or self._contains_ref(item.deep_link, refs)
            )
        ]

    def _runtime_projection(
        self,
        refs: set[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        usage_rows = []
        if self.runtime_usage is not None:
            for item in self.runtime_usage.list():
                if not self._same_scope(item, organization_id, workspace_id):
                    continue
                related = {
                    item.execution_id,
                    item.work_item_ref,
                    item.goal_id,
                    item.decision_id,
                    item.agent_session_id,
                }
                if refs.intersection({str(value) for value in related if value}):
                    usage_rows.append(item.model_dump(mode="json"))

        session_ids = {
            item.get("agent_session_id")
            for item in usage_rows
            if item.get("agent_session_id")
        }
        sessions = []
        if self.agent_sessions is not None:
            for item in self.agent_sessions.list():
                if not self._same_scope(item, organization_id, workspace_id):
                    continue
                related = {
                    item.id,
                    item.execution_id,
                    item.assignment_id,
                    item.execution_workspace_id,
                    item.project_id,
                }
                if (
                    item.id in session_ids
                    or refs.intersection(
                        {str(value) for value in related if value}
                    )
                ):
                    sessions.append(item.model_dump(mode="json"))
        return sessions, usage_rows

    def _model_projection(
        self,
        refs: set[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.model_gateway is None:
            return []
        state = self.model_gateway.load()
        rows = []
        for item in reversed(state.invocations):
            if not self._same_scope(item, organization_id, workspace_id):
                continue
            related = {
                item.execution_id,
                item.work_item_ref,
                item.goal_id,
                item.decision_id,
            }
            if not refs.intersection({str(value) for value in related if value}):
                continue
            rows.append(
                {
                    "id": item.id,
                    "status": item.status,
                    "purpose": item.purpose,
                    "model_class": item.model_class,
                    "route_reason": item.route_reason,
                    "policy_fingerprint_sha256": item.policy_fingerprint_sha256,
                    "prompt_template_id": item.prompt_template_id,
                    "prompt_template_version": item.prompt_template_version,
                    "prompt_template_checksum_sha256": (
                        item.prompt_template_checksum_sha256
                    ),
                    "selected_provider_id": item.selected_provider_id,
                    "selected_model_id": item.selected_model_id,
                    "selected_concrete_model": item.selected_concrete_model,
                    "selected_model_version": item.selected_model_version,
                    "attempts": [
                        attempt.model_dump(mode="json")
                        for attempt in item.attempts
                    ],
                    "created_at": item.created_at,
                    "completed_at": item.completed_at,
                }
            )
        return rows[:20]

    def _evidence_projection(
        self,
        refs: set[str],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.artifact_evidence is None:
            return []
        state = self.artifact_evidence.load()
        rows = []
        for item in reversed(state.evidence):
            if not self._same_scope(item, organization_id, workspace_id):
                continue
            related = {
                item.id,
                item.execution_id,
                item.work_item_ref,
                item.project_id,
                item.external_id,
            }
            if refs.intersection({str(value) for value in related if value}):
                rows.append(
                    {
                        "id": item.id,
                        "evidence_type": self._value(item.evidence_type),
                        "result": self._value(item.result),
                        "summary": item.summary,
                        "provider": item.provider,
                        "source": item.source,
                        "deep_link": item.deep_link,
                        "observed_at": item.observed_at,
                    }
                )
        return rows[:50]

    def _schedule_projection(
        self,
        all_events: list[Any],
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.scheduler is None:
            return []
        rows = []
        for item in self.scheduler.list():
            if organization_id is not None and item.tenant_id != organization_id:
                continue
            if (
                workspace_id is not None
                and item.workspace_id is not None
                and item.workspace_id != workspace_id
            ):
                continue
            source = f"scheduler:{item.id}"
            firings = [
                {
                    "event_id": event.event_id,
                    "occurred_at": event.occurred_at,
                    "correlation_id": event.correlation_id,
                    "causation_id": event.causation_id,
                    "scheduled_for": (
                        event.payload.get("scheduled_for")
                        if isinstance(event.payload, dict)
                        else None
                    ),
                }
                for event in all_events
                if event.source == source
            ]
            rows.append(
                {
                    **item.model_dump(mode="json"),
                    "canonical_event_source": source,
                    "recent_firings": firings[:20],
                }
            )
        return rows

    def _evaluation_projection(
        self,
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> dict[str, list[dict[str, Any]]]:
        if self.evaluations is None:
            return {"runs": [], "comparisons": [], "suite_runs": []}
        if organization_id is None or workspace_id is None:
            state = self.evaluations.load()
            runs = tuple(reversed(state.runs[-100:]))
            comparisons = tuple(reversed(state.comparisons[-100:]))
            suites = tuple(reversed(state.suite_runs[-100:]))
        else:
            runs = self.evaluations.list_runs(
                organization_id,
                workspace_id,
            )[:100]
            comparisons = self.evaluations.list_comparisons(
                organization_id,
                workspace_id,
            )[:100]
            suites = self.evaluations.list_suite_runs(
                organization_id,
                workspace_id,
            )[:100]
        return {
            "runs": [item.model_dump(mode="json") for item in runs],
            "comparisons": [
                item.model_dump(mode="json") for item in comparisons
            ],
            "suite_runs": [item.model_dump(mode="json") for item in suites],
        }

    def _attention_summary(
        self,
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.attention is None:
            return []
        return [
            item.model_dump(mode="json")
            for item in self.attention.list()
            if self._same_scope(item, organization_id, workspace_id)
        ][:100]

    def _approval_summary(
        self,
        *,
        organization_id: str | None,
        workspace_id: str | None,
    ) -> list[dict[str, Any]]:
        if self.approvals is None:
            return []
        return [
            item.model_dump(mode="json")
            for item in self.approvals.list(
                organization_id=organization_id,
                workspace_id=workspace_id,
            )[:100]
        ]

    @staticmethod
    def _pipeline(
        *,
        event: Any,
        cycles: list[dict[str, Any]],
        intents: list[dict[str, Any]],
        approvals: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
        usage: list[dict[str, Any]],
        models: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        attention: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        first_cycle = cycles[0] if cycles else None
        return [
            {
                "stage": "event",
                "status": "observed",
                "detail": {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "source": event.source,
                    "correlation_id": event.correlation_id,
                    "causation_id": event.causation_id,
                },
            },
            {
                "stage": "deterministic_filter",
                "status": (
                    first_cycle.get("outcome")
                    if first_cycle is not None
                    else "no_autonomy_cycle"
                ),
                "detail": {
                    "reason": (
                        first_cycle.get("reason")
                        if first_cycle is not None
                        else "event has no recorded bounded-autonomy cycle"
                    )
                },
            },
            {
                "stage": "reasoning_gate",
                "status": (
                    "invoked"
                    if any(bool(item.get("reasoning_invoked")) for item in cycles)
                    else "skipped"
                ),
                "detail": {
                    "reasoning_score": (
                        first_cycle.get("reasoning_score")
                        if first_cycle is not None
                        else None
                    ),
                    "attempts": max(
                        (
                            int(item.get("reasoning_attempts") or 0)
                            for item in cycles
                        ),
                        default=0,
                    ),
                    "reasons": [
                        item.get("reason")
                        for item in cycles
                        if item.get("reason")
                    ],
                },
            },
            {
                "stage": "routing",
                "status": "selected" if sessions or models else "not_recorded",
                "detail": {
                    "agent_sessions": sessions,
                    "runtime_usage": usage,
                    "model_invocations": models,
                },
            },
            {
                "stage": "authority_approval",
                "status": (
                    "recorded" if intents or approvals else "not_applicable"
                ),
                "detail": {
                    "action_intents": intents,
                    "approval_requests": approvals,
                },
            },
            {
                "stage": "execution",
                "status": (
                    "recorded" if intents else "no_external_action"
                ),
                "detail": {
                    "actions": [
                        {
                            "id": item["id"],
                            "status": item["status"],
                            "attempt": item["attempt"],
                            "receipts": item.get("receipts", []),
                        }
                        for item in intents
                    ]
                },
            },
            {
                "stage": "verification",
                "status": "recorded" if evidence or intents else "none",
                "detail": {
                    "verifications": [
                        {
                            "intent_id": item["id"],
                            "receipts": item.get("verifications", []),
                        }
                        for item in intents
                        if item.get("verifications")
                    ],
                    "evidence": evidence,
                },
            },
            {
                "stage": "human_attention",
                "status": "pending" if attention else "none",
                "detail": {"attention_items": attention},
            },
        ]

    def snapshot(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        source: str | None = None,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        count = max(1, min(int(limit), 500))
        wanted_type = str(event_type or "").strip()
        wanted_source = str(source or "").strip().casefold()

        all_events = [
            item
            for item in self.events.recent(limit=500)
            if self._event_same_scope(item, organization_id, workspace_id)
        ]
        events = list(all_events)
        if wanted_type:
            events = [item for item in events if item.event_type == wanted_type]
        if wanted_source:
            events = [
                item
                for item in events
                if wanted_source in item.source.casefold()
            ]
        events = events[:count]

        autonomy = self.autonomy.status()
        cycles = autonomy["recent_cycles"]
        cycles_by_event: dict[str, list[dict[str, Any]]] = {}
        for cycle in cycles:
            cycle_org = cycle.get("organization_id")
            cycle_workspace = cycle.get("workspace_id")
            if (
                organization_id is not None
                and cycle_org is not None
                and cycle_org != organization_id
            ):
                continue
            if (
                workspace_id is not None
                and cycle_workspace is not None
                and cycle_workspace != workspace_id
            ):
                continue
            cycles_by_event.setdefault(str(cycle["event_id"]), []).append(cycle)

        timeline = []
        for event in events:
            event_cycles = cycles_by_event.get(event.event_id, [])
            action_intent_ids = []
            refs = self._event_refs(event)
            for cycle in event_cycles:
                refs.add(str(cycle.get("id") or ""))
                for intent_id in cycle.get("action_intent_ids") or ():
                    if intent_id not in action_intent_ids:
                        action_intent_ids.append(intent_id)
                        refs.add(str(intent_id))
            refs.discard("")

            intents, intent_refs = self._intent_projection(
                action_intent_ids,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            refs.update(intent_refs)
            approvals = self._approval_projection(
                refs,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            attention = self._attention_projection(
                refs,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            sessions, usage = self._runtime_projection(
                refs,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            models = self._model_projection(
                refs,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            evidence = self._evidence_projection(
                refs,
                organization_id=organization_id,
                workspace_id=workspace_id,
            )
            timeline.append(
                {
                    "event": event.model_dump(mode="json"),
                    "cycles": event_cycles,
                    "filtering_result": (
                        event_cycles[0]["outcome"]
                        if event_cycles
                        else "no_autonomy_cycle"
                    ),
                    "reasoning": {
                        "invoked": any(
                            bool(cycle.get("reasoning_invoked"))
                            for cycle in event_cycles
                        ),
                        "reasons": [
                            str(cycle.get("reason") or "")
                            for cycle in event_cycles
                            if cycle.get("reason")
                        ],
                    },
                    "resulting_action_intent_ids": action_intent_ids,
                    "action_intents": intents,
                    "approval_requests": approvals,
                    "attention_items": attention,
                    "agent_sessions": sessions,
                    "runtime_usage": usage,
                    "model_invocations": models,
                    "evidence": evidence,
                    "pipeline": self._pipeline(
                        event=event,
                        cycles=event_cycles,
                        intents=intents,
                        approvals=approvals,
                        sessions=sessions,
                        usage=usage,
                        models=models,
                        evidence=evidence,
                        attention=attention,
                    ),
                }
            )

        dependencies = {
            "scheduler": {
                "available": self.scheduler is not None,
                "issue": 158,
                "reason": (
                    "canonical durable scheduler is projected read-only"
                    if self.scheduler is not None
                    else "scheduler store is not composed"
                ),
            },
            "evaluation_replay": {
                "available": self.evaluations is not None,
                "issue": 159,
                "reason": (
                    "canonical evaluation/replay state is projected read-only"
                    if self.evaluations is not None
                    else "evaluation store is not composed"
                ),
            },
            "attention_queue": {
                "available": self.attention is not None,
                "issue": 160,
                "reason": (
                    "canonical Attention state is projected read-only"
                    if self.attention is not None
                    else "Attention store is not composed"
                ),
            },
        }

        return {
            "control": autonomy["control"],
            "timeline": timeline,
            "dead_letters": autonomy["dead_letters"],
            "cycle_count": len(cycles),
            "event_count": len(timeline),
            "schedules": self._schedule_projection(
                all_events,
                organization_id=organization_id,
                workspace_id=workspace_id,
            ),
            "evaluations": self._evaluation_projection(
                organization_id=organization_id,
                workspace_id=workspace_id,
            ),
            "attention_items": self._attention_summary(
                organization_id=organization_id,
                workspace_id=workspace_id,
            ),
            "approval_requests": self._approval_summary(
                organization_id=organization_id,
                workspace_id=workspace_id,
            ),
            "dependencies": dependencies,
        }
