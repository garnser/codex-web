from __future__ import annotations

import json
import uuid
from typing import Any

from codex_web.action_intents import ActionIntentCreate
from codex_web.action_providers import ActionRequest
from codex_web.agent_teams import TeamDelegationRequest, TeamExecutionRequest
from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRun, AutomationRunStatus
from codex_web.identity import TenantScope
from codex_web.models import TurnCreate
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.services.agent_team_execution import AgentTeamExecutionService
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.identity import IdentityService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TASK_SOURCE_CREATE_ACTION_ID,
)
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.canonical_events import CanonicalEventStore


class AutomationExecutionError(RuntimeError):
    pass


class AutomationExecutionService:
    """Launch admitted Automation runs through canonical Agent/Team execution."""

    def __init__(
        self,
        runs: AutomationRunService,
        *,
        identity: IdentityService,
        threads: ThreadService,
        turns: TurnService,
        teams: AgentTeamExecutionService,
        events: CanonicalEventStore,
        work_items: WorkItemService | None = None,
        action_intents: ActionIntentService | None = None,
        action_providers: ActionProviderRegistry | None = None,
    ) -> None:
        self.runs = runs
        self.identity = identity
        self.threads = threads
        self.turns = turns
        self.teams = teams
        self.events = events
        self.work_items = work_items
        self.action_intents = action_intents
        self.action_providers = action_providers

    def _run(self, run_id: str, *, organization_id: str, workspace_id: str) -> AutomationRun:
        return self.runs.store.get(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _thread_id(response: dict[str, Any]) -> str:
        payload = response.get("thread")
        if not isinstance(payload, dict):
            payload = response
        value = payload.get("id") if isinstance(payload, dict) else None
        if not value:
            raise AutomationExecutionError(
                "profile-bound Automation thread creation returned no thread id"
            )
        return str(value)

    def _trigger_context(self, run: AutomationRun) -> dict[str, Any] | None:
        event_id = run.trigger.event_id
        if not event_id:
            return None
        event = self.events.event(event_id)
        if event is None:
            raise AutomationExecutionError(
                "Automation trigger canonical event is no longer available"
            )
        if (
            event.tenant_id != run.organization_id
            or event.workspace_id != run.workspace_id
        ):
            raise AutomationExecutionError(
                "Automation trigger event tenant/workspace does not match run"
            )
        return {
            "eventId": event.event_id,
            "eventType": event.event_type,
            "source": event.source,
            "occurredAt": event.occurred_at,
            "payload": event.payload,
        }

    @staticmethod
    def _message(instructions: str, context: dict[str, Any] | None) -> str:
        if context is None:
            return instructions
        return (
            instructions
            + "\n\nCanonical trigger context (read-only provenance):\n"
            + json.dumps(context, sort_keys=True, ensure_ascii=False)
        )[:50000]

    def _actor(self, run: AutomationRun, owner_identity_id: str | None):
        owner = str(owner_identity_id or "").strip()
        if not owner:
            raise AutomationExecutionError(
                "enabled Automation execution requires owner_identity_id"
            )
        return self.identity.actor_for_identity(
            owner,
            scope=TenantScope(
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
            ),
        )

    @staticmethod
    def _event_work_item_ref(context: dict[str, Any] | None) -> str | None:
        if not context:
            return None
        payload = context.get("payload")
        if not isinstance(payload, dict):
            return None
        value = str(
            payload.get("work_item_ref")
            or payload.get("workItemRef")
            or ""
        ).strip()
        return value or None

    def _validated_work_item_ref(
        self,
        run: AutomationRun,
        value: str | None,
    ) -> str | None:
        ref = str(value or "").strip()
        if not ref:
            return None
        if self.work_items is None:
            return ref
        state = self.work_items.state_machine._work_item_state(ref)
        if (
            state.organization_id != run.organization_id
            or state.workspace_id != run.workspace_id
        ):
            raise AutomationExecutionError(
                "Automation Work Item is outside the run tenant/workspace"
            )
        if state.project_id != run.project_id:
            raise AutomationExecutionError(
                "Automation Work Item project does not match the run project"
            )
        return ref

    def _resolve_work_item_ref(
        self,
        run: AutomationRun,
        *,
        policy: str,
        explicit_ref: str | None,
        event_ref: str | None,
    ) -> str | None:
        if policy == "always_create":
            return None
        candidate = self._validated_work_item_ref(
            run,
            str(explicit_ref or "").strip() or event_ref,
        )
        if policy == "reuse_only" and candidate is None:
            raise AutomationExecutionError(
                "Automation work_item_policy=reuse_only requires an existing "
                "canonical Work Item"
            )
        return candidate

    def _task_source_binding(self, project_id: str, actor):
        if self.action_providers is None:
            raise AutomationExecutionError(
                "Automation Work Item creation requires canonical ActionProvider registry"
            )
        candidates = [
            item
            for item in self.action_providers.list_bindings(actor)
            if item.enabled
            and item.provider_type == TASK_SOURCE_ACTION_PROVIDER_TYPE
            and item.provider_instance == TASK_SOURCE_ACTION_PROVIDER_INSTANCE
            and item.project_id in {None, project_id}
        ]
        exact = [item for item in candidates if item.project_id == project_id]
        eligible = exact if exact else [
            item for item in candidates if item.project_id is None
        ]
        if not eligible:
            raise AutomationExecutionError(
                "no enabled authoritative task-source ActionProvider binding "
                f"permits project {project_id}"
            )
        if len(eligible) != 1:
            raise AutomationExecutionError(
                "multiple authoritative task-source ActionProvider bindings "
                f"match project {project_id}"
            )
        return eligible[0]

    @staticmethod
    def _work_item_body(
        run: AutomationRun,
        automation,
        context: dict[str, Any] | None,
    ) -> str:
        parts = [
            automation.description or automation.instructions,
            "",
            f"Canonical Automation: {run.automation_id}",
            f"Automation run: {run.id}",
        ]
        if context is not None:
            parts.extend(
                (
                    "",
                    "Canonical trigger provenance:",
                    json.dumps(context, sort_keys=True, ensure_ascii=False),
                )
            )
        return "\n".join(parts)[:12000]

    def _wait_for_work_item_creation(
        self,
        run: AutomationRun,
        automation,
        *,
        actor,
        context: dict[str, Any] | None,
    ) -> AutomationRun:
        if self.action_intents is None:
            raise AutomationExecutionError(
                "Automation Work Item creation requires canonical ActionIntent service"
            )
        binding = self._task_source_binding(run.project_id, actor)
        request = ActionRequest(
            action_id=TASK_SOURCE_CREATE_ACTION_ID,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            parameters={
                "title": automation.name,
                "body": self._work_item_body(run, automation, context),
                "owners": (
                    [automation.owner_identity_id]
                    if automation.owner_identity_id
                    else []
                ),
                "labels": ["automation"],
            },
            idempotency_key=f"automation:{run.id}:work-item-create",
            correlation_id=run.trigger.correlation_id or run.id,
            requested_by=actor.identity_id,
        )
        intent = self.action_intents.create(
            ActionIntentCreate(
                binding_id=binding.id,
                request=request,
                verification_required=False,
            ),
            actor=actor,
        )
        return self.runs.wait_for_work_item(
            run.id,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            action_intent_id=intent.id,
        )

    def _block(self, run: AutomationRun, code: str, reason: str) -> AutomationRun:
        return self.runs.block(
            run.id,
            organization_id=run.organization_id,
            workspace_id=run.workspace_id,
            code=code,
            reason=reason,
        )

    async def launch(
        self,
        run_id: str,
        *,
        organization_id: str,
        workspace_id: str,
        work_item_ref: str | None = None,
        repository_resource_id: str | None = None,
        read_only_repository_resource_ids: tuple[str, ...] = (),
    ) -> AutomationRun:
        run = self._run(
            run_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
        )
        if run.status != AutomationRunStatus.ADMITTED:
            return run

        try:
            automation, _reference = self.runs.definitions.resolve_reference(
                run.definition_ref,
                organization_id=run.organization_id,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
            )
            actor = self._actor(run, automation.owner_identity_id)
            if not run.project_id:
                raise AutomationExecutionError(
                    "Automation execution requires an explicit canonical project id"
                )
            context = self._trigger_context(run)
            selected_work_item_ref = self._resolve_work_item_ref(
                run,
                policy=automation.work_item_policy,
                explicit_ref=work_item_ref,
                event_ref=self._event_work_item_ref(context),
            )
            if (
                selected_work_item_ref is None
                and automation.work_item_policy in {"reuse_or_create", "always_create"}
            ):
                return self._wait_for_work_item_creation(
                    run,
                    automation,
                    actor=actor,
                    context=context,
                )
            message = self._message(automation.instructions, context)

            if automation.target.kind == AutomationTargetKind.AGENT_PROFILE:
                profile, _decision = (
                    self.threads.agent_profiles.resolve_for_execution(
                        automation.target.id,
                        actor=actor,
                        project_id=run.project_id,
                    )
                    if self.threads.agent_profiles is not None
                    else (None, None)
                )
                if profile is None:
                    raise AutomationExecutionError(
                        "Agent Profile service is unavailable for Automation execution"
                    )
                execution_profile_id = (
                    automation.execution_profile_ref.definition_id
                    if automation.execution_profile_ref is not None
                    else None
                )
                thread = await self.threads.create(
                    project_id=run.project_id,
                    repository_resource_id=repository_resource_id,
                    read_only_repository_resource_ids=read_only_repository_resource_ids,
                    execution_profile_id=execution_profile_id,
                    actor=actor,
                    agent_profile_id=profile.profile_id,
                    agent_profile_revision=profile.revision,
                )
                thread_id = self._thread_id(thread)
                execution_id = f"automation-turn-{uuid.uuid4().hex}"
                await self.turns.start(
                    thread_id,
                    TurnCreate(
                        message=message,
                        project_id=run.project_id,
                        repository_resource_id=repository_resource_id,
                        read_only_repository_resource_ids=(
                            read_only_repository_resource_ids
                        ),
                        execution_profile_id=execution_profile_id,
                        agent_profile_id=profile.profile_id,
                        agent_profile_revision=profile.revision,
                    ),
                    actor=actor,
                    execution_id=execution_id,
                    work_item_ref=selected_work_item_ref,
                )
                return self.runs.mark_running(
                    run.id,
                    organization_id=run.organization_id,
                    workspace_id=run.workspace_id,
                    work_item_ref=selected_work_item_ref,
                    execution_ids=(execution_id,),
                )

            if automation.target.kind == AutomationTargetKind.TEAM:
                if not selected_work_item_ref:
                    raise AutomationExecutionError(
                        "Team Automation requires a canonical Work Item; "
                        "work_item_policy creation/reuse must resolve before launch"
                    )
                result = await self.teams.execute(
                    automation.target.id,
                    TeamExecutionRequest(
                        delegation=TeamDelegationRequest(
                            work_item_id=selected_work_item_ref,
                            project_id=run.project_id,
                            trigger_id=run.id,
                        ),
                        message=message,
                        repository_resource_id=repository_resource_id,
                        read_only_repository_resource_ids=(
                            read_only_repository_resource_ids
                        ),
                    ),
                    actor=actor,
                )
                if result.plan.blocked:
                    return self._block(
                        run,
                        "automation_team_launch_blocked",
                        result.plan.reason,
                    )
                execution_ids = tuple(
                    [
                        *(
                            (result.coordinator.execution_id,)
                            if result.coordinator is not None
                            else ()
                        ),
                        *(item.execution_id for item in result.members),
                    ]
                )
                if not execution_ids:
                    raise AutomationExecutionError(
                        "Team Automation produced no canonical executions"
                    )
                return self.runs.mark_running(
                    run.id,
                    organization_id=run.organization_id,
                    workspace_id=run.workspace_id,
                    work_item_ref=selected_work_item_ref,
                    execution_ids=execution_ids,
                )

            raise AutomationExecutionError(
                f"unsupported Automation target kind: {automation.target.kind}"
            )
        except Exception as exc:
            if isinstance(exc, AutomationExecutionError):
                reason = str(exc)
            else:
                reason = f"{type(exc).__name__}: {exc}"
            return self._block(
                run,
                "automation_launch_blocked",
                reason,
            )
