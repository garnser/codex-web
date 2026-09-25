from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.agent_providers import AgentProviderCapability
from codex_web.api.goal_execution_bindings import build_goal_execution_bindings_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)


class _Dumpable:
    def __init__(self, **payload):
        self.payload = payload

    def model_dump(self, mode="json"):
        return self.payload


class _BindingServiceStub:
    def __init__(self) -> None:
        self.bindings = []
        self.created = []

    def list_all(self, *, scope):
        return tuple(self.bindings)

    def create(self, goal_id, payload, *, scope, actor_id):
        self.created.append((goal_id, payload, scope, actor_id))
        return _Dumpable(
            id="binding-1",
            goal_id=goal_id,
            agent_session_id=payload.agent_session_id,
            project_id=payload.project_id,
        )


class _AgentSessionsStub:
    def __init__(self) -> None:
        self.sessions = [
            SimpleNamespace(
                id="session-a",
                provider_id="openai",
                runtime_id="codex",
                project_id="project-a",
                provider_native_session_id="thread-a",
                capability_snapshot=(
                    AgentProviderCapability.NATIVE_EXECUTION_OBJECTIVES,
                ),
            ),
            SimpleNamespace(
                id="session-unsupported",
                provider_id="anthropic",
                runtime_id="claude",
                project_id="project-a",
                provider_native_session_id="thread-b",
                capability_snapshot=(),
            ),
        ]
        self.objectives = {
            "session-a": {
                "id": "native-goal-a",
                "objective": "Validate the bounded release",
                "status": "active",
            }
        }

    def list(self, actor):
        return list(self.sessions)

    def get(self, session_id, actor):
        return next(item for item in self.sessions if item.id == session_id)

    async def read_objective(self, session_id, *, actor):
        return SimpleNamespace(payload=self.objectives.get(session_id, {}))


class GoalExecutionBindingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bindings = _BindingServiceStub()
        self.sessions = _AgentSessionsStub()
        self.actor = AuthenticationActor(
            identity_id="member",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(
            build_goal_execution_bindings_router(
                self.bindings,
                self.sessions,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_unbound_discovery_is_read_only_capability_gated_and_filters_bound_sessions(self) -> None:
        response = self.client.get("/api/goals/runtime-objectives/unbound")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        item = response.json()["items"][0]
        self.assertEqual(item["agent_session_id"], "session-a")
        self.assertEqual(item["objective"], "Validate the bounded release")
        self.assertEqual(item["status"], "active")

        self.bindings.bindings = [
            SimpleNamespace(agent_session_id="session-a")
        ]
        response = self.client.get("/api/goals/runtime-objectives/unbound")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "count": 0})

    def test_attach_requires_mfa_admin_and_uses_canonical_binding_service(self) -> None:
        denied = self.client.post(
            "/api/goals/runtime-objectives/session-a/attach/goal-a",
            json={
                "work_item_refs": ["root-a"],
                "reason": "attach validated runtime objective",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.bindings.created, [])

        self.actor = self.actor.model_copy(
            update={
                "roles": (MembershipRole.ADMIN,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )
        response = self.client.post(
            "/api/goals/runtime-objectives/session-a/attach/goal-a",
            json={
                "work_item_refs": ["root-a"],
                "reason": "attach validated runtime objective",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["goal_id"], "goal-a")
        self.assertEqual(len(self.bindings.created), 1)
        goal_id, payload, scope, actor_id = self.bindings.created[0]
        self.assertEqual(goal_id, "goal-a")
        self.assertEqual(payload.agent_session_id, "session-a")
        self.assertEqual(payload.provider_native_objective_id, "native-goal-a")
        self.assertTrue(payload.native_objective_supported)
        self.assertEqual(payload.work_item_refs, ("root-a",))
        self.assertEqual(actor_id, "member")
        self.assertEqual(scope.organization_id, "org-a")
        self.assertEqual(scope.workspace_id, "ws-a")


if __name__ == "__main__":
    unittest.main()
