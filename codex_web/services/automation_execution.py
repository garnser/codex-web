from __future__ import annotations

import json
import uuid
from typing import Any

from codex_web.agent_teams import TeamDelegationRequest, TeamExecutionRequest
from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRun, AutomationRunStatus
from codex_web.identity import TenantScope
from codex_web.models import TurnCreate
from codex_web.services.agent_team_execution import AgentTeamExecutionService
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.identity import IdentityService
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService
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
    ) -> None:
        self.runs = runs
        self.identity = identity
        self.threads = threads
        self.turns = turns
        self.teams = teams
        self.events = events

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
            selected_work_item_ref = (
                str(work_item_ref or "").strip()
                or self._event_work_item_ref(context)
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
