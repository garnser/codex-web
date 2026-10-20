"""Adapt the existing Project reconciliation decision to bot runtime consumers."""
from __future__ import annotations

from collections.abc import Callable
from codex_web.identity import AuthenticationActor


class BotRuntimeGateClosed(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def project_bot_runtime_gate(gates, connections) -> Callable[..., str | None]:
    def reason(project_id: str, actor: AuthenticationActor | None = None) -> str | None:
        try:
            if actor is None:
                actor = connections.runtime_actor(project_id)
            else:
                # Conversation ingestion already authenticated this actor. Check
                # its real Project tenant rather than borrowing bot credentials.
                project = connections.projects.get(project_id)
                if (project.organization_id, project.workspace_id) != (
                        actor.organization_id, actor.workspace_id):
                    return "bot_runtime_project_scope_denied"
            decision = gates.decision("bot-runtime", project_id, actor=actor)
            return None if decision.eligible else decision.reason_code
        except Exception:
            return "bot_runtime_gate_unavailable"
    return reason
