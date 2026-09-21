from __future__ import annotations

from typing import Any

from codex_web.identity import AuthenticationActor
from codex_web.services.canonical_materialization import (
    CanonicalMaterializationService,
)
from codex_web.services.project_readiness import ProjectReadinessService


class FreshProjectBootstrapService:
    """Materialize canonical topology for a newly configured Project.

    This service deliberately reuses canonical materialization rather than
    implementing a second repository discovery/binding path.
    """

    def __init__(
        self,
        *,
        materialization: CanonicalMaterializationService,
        readiness: ProjectReadinessService,
    ) -> None:
        self.materialization = materialization
        self.readiness = readiness

    def bootstrap(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        confirm_generic = (
            actor.organization_id == "local"
            and actor.workspace_id == "default"
        )
        plan = self.materialization.plan(
            project_id,
            actor=actor,
            confirm_generic_target=confirm_generic,
        )
        execution = None
        if not plan.blockers:
            execution = self.materialization.apply(
                plan,
                actor=actor,
            )

        readiness = self.readiness.evaluate(
            project_id,
            actor=actor,
            record=True,
        )
        return {
            "status": (
                "ready"
                if readiness.execution_ready
                else "blocked"
            ),
            "materializationPlanId": plan.id,
            "materializationExecutionId": (
                execution.id if execution is not None else None
            ),
            "materializationVersion": plan.version,
            "materializationCounts": plan.counts(),
            "materializationBlockers": [
                {
                    "id": item.id,
                    "domain": item.domain,
                    "code": item.reason_code,
                    "message": item.message,
                    "operatorAction": item.operator_action,
                }
                for item in plan.blockers
            ],
            "readiness": readiness.model_dump(mode="json"),
        }
