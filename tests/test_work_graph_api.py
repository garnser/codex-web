from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.work_graph import build_work_graph_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import WorkItemState
from codex_web.services.projects import ProjectNotFoundError
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_graph import WorkGraphStore


def item(
    ref: str,
    *,
    project_id: str = "project-a",
    organization_id: str = "org-a",
    workspace_id: str = "ws-a",
) -> WorkItemState:
    now = time.time()
    return WorkItemState(
        ref=ref,
        organization_id=organization_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title=ref,
        last_meaningful_update_at=now,
        updated_at=now,
        created_at=now,
    )


class _Projects:
    def get(self, project_id: str, scope: TenantScope):
        if (
            project_id == "project-a"
            and scope.organization_id == "org-a"
            and scope.workspace_id == "ws-a"
        ):
            return object()
        raise ProjectNotFoundError("Project not found")


class WorkGraphApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.items = {
            "A": item("A"),
            "B": item("B"),
            "C": item("C"),
            "OTHER": item(
                "OTHER",
                organization_id="org-b",
                workspace_id="ws-b",
            ),
        }
        self.service = WorkGraphService(
            WorkGraphStore(sqlite),
            lambda: self.items,
        )
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )

        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            request.state.tenant_scope = self.actor.tenant
            return await call_next(request)

        app.include_router(build_work_graph_router(self.service, _Projects()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def _edge(self, source: str = "A", target: str = "B", **overrides):
        payload = {
            "relation": "blocks",
            "source_ref": source,
            "target_ref": target,
            "failure_behavior": "pause",
            "reason": "dependency",
        }
        payload.update(overrides)
        return payload

    def test_reads_are_tenant_scoped_without_step_up(self) -> None:
        snapshot = self.client.get("/api/work-graph/projects/project-a")
        self.assertEqual(snapshot.status_code, 200)
        refs = {node["ref"] for node in snapshot.json()["graph"]["nodes"]}
        self.assertEqual(refs, {"A", "B", "C"})
        self.assertNotIn("OTHER", refs)

        readiness = self.client.get(
            "/api/work-graph/readiness",
            params={"ref": "A"},
        )
        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(
            readiness.json()["readiness"]["status"],
            "runnable",
        )

        hidden = self.client.get(
            "/api/work-graph/readiness",
            params={"ref": "OTHER"},
        )
        self.assertEqual(hidden.status_code, 404)

        missing_project = self.client.get(
            "/api/work-graph/projects/project-b"
        )
        self.assertEqual(missing_project.status_code, 404)

    def test_low_assurance_admin_cannot_mutate_graph(self) -> None:
        response = self.client.post(
            "/api/work-graph/edges",
            json=self._edge(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("mfa", response.json()["detail"].lower())

    def test_mfa_admin_can_edit_and_cycle_conflicts_are_deterministic(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        first = self.client.post(
            "/api/work-graph/edges",
            json=self._edge("A", "B"),
        )
        second = self.client.post(
            "/api/work-graph/edges",
            json=self._edge("B", "C", failure_behavior="replan"),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        snapshot = self.client.get("/api/work-graph/projects/project-a")
        self.assertEqual(snapshot.status_code, 200)
        graph = snapshot.json()["graph"]
        self.assertEqual(graph["critical_path"]["refs"], ["A", "B", "C"])
        self.assertEqual(graph["progress"]["blocked"], 2)
        self.assertEqual(graph["runnable_refs"], ["A"])

        traversal = self.client.get(
            "/api/work-graph/traverse",
            params={
                "ref": "A",
                "relation": "blocks",
                "direction": "downstream",
            },
        )
        self.assertEqual(traversal.status_code, 200)
        self.assertEqual(traversal.json()["refs"], ["B", "C"])

        cycle = self.client.post(
            "/api/work-graph/edges",
            json=self._edge("C", "A"),
        )
        self.assertEqual(cycle.status_code, 409)
        self.assertIn("cycle", cycle.json()["detail"].lower())

        events = self.client.get(
            "/api/work-graph/events",
            params={"project_id": "project-a"},
        )
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["count"], 2)

        edge_id = first.json()["edge"]["id"]
        removed = self.client.delete(f"/api/work-graph/edges/{edge_id}")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["edge"]["id"], edge_id)

    def test_cross_tenant_edge_target_fails_closed(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )

        response = self.client.post(
            "/api/work-graph/edges",
            json=self._edge("A", "OTHER"),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "work item not found")

    def test_service_requires_explicit_work_graph_admin_scope_for_mutation(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="graph-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=(),
        )

        denied = self.client.post(
            "/api/work-graph/edges",
            json=self._edge(),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIn("work-graph:admin", denied.json()["detail"])

        self.actor = self.actor.model_copy(
            update={"service_scopes": ("work-graph:admin",)}
        )
        allowed = self.client.post(
            "/api/work-graph/edges",
            json=self._edge(),
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.json()["edge"]["created_by"],
            "graph-service",
        )

    def test_failed_dependency_explains_downstream_behavior_without_llm(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        self.client.post(
            "/api/work-graph/edges",
            json=self._edge(
                "A",
                "B",
                failure_behavior="escalate",
            ),
        )
        self.items["A"].terminal_outcome = "failed"

        response = self.client.get(
            "/api/work-graph/readiness",
            params={"ref": "B"},
        )

        self.assertEqual(response.status_code, 200)
        readiness = response.json()["readiness"]
        self.assertEqual(readiness["status"], "blocked")
        self.assertEqual(
            readiness["failure_impacts"][0]["behavior"],
            "escalate",
        )
        self.assertIn(
            "downstream behavior is escalate",
            readiness["failure_impacts"][0]["reason"],
        )


if __name__ == "__main__":
    unittest.main()
