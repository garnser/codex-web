from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.home_overview import HomeOverviewService
from codex_web.services.project_readiness import ReadinessCheckStatus


class _Projects:
    def get(self, project_id, scope):
        return SimpleNamespace(id=project_id, name="Large Project", path="/workspace/large")


class _WorkItems:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, int]] = []

    async def list(
        self,
        *,
        project_id,
        owner,
        stage,
        release_gate,
        scope,
        limit,
        cursor,
    ):
        self.calls.append((stage, limit))
        count = 500 if stage == "closed" else 1300
        rows = []
        for index in range(count):
            rows.append(
                {
                    "ref": f"group/project#{index}",
                    "title": f"Work Item {index}",
                    "current_stage": "closed" if stage == "closed" else "implementation_active",
                    "current_owner": "agent-a",
                    "next_owner": None,
                    "blocker": "blocked" if stage is None and index % 2 else None,
                    "updated_at": 10000 - index,
                }
            )
        # Deliberately ignore the requested limit. The Home projection must still
        # remain bounded if an adapter/source violates its page-size contract.
        return {"items": rows, "nextCursor": None, "hasMore": False}


class _EmptyList:
    def list(self, *args, **kwargs):
        return []


class _Readiness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def evaluate(self, project_id, *, actor, record=False):
        self.calls.append((project_id, record))
        check = SimpleNamespace(
            id="worker",
            domain="execution_worker",
            status=ReadinessCheckStatus.BLOCKED,
            code="worker_missing",
            message="A qualified worker is required.",
            remediation="Enroll a qualified worker.",
            remediation_route="#workspace/operations",
        )
        return SimpleNamespace(
            semantic_ready=False,
            execution_ready=False,
            status=ReadinessCheckStatus.BLOCKED,
            checks=[check],
        )


class _EmptyGoals:
    def list(self, *, scope):
        return []

    def snapshot(self, goal_id, *, scope):
        raise AssertionError("snapshot must not be called when there are no goals")


class HomeOverviewPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_source_state_stays_bounded_and_requests_small_pages(self) -> None:
        work_items = _WorkItems()
        readiness = _Readiness()
        actor = AuthenticationActor(
            identity_id="human-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        service = HomeOverviewService(
            projects=_Projects(),
            work_items=work_items,
            attention=_EmptyList(),
            approvals=_EmptyList(),
            incidents=_EmptyList(),
            agent_sessions=_EmptyList(),
            goals=_EmptyGoals(),
            schedules=None,
            readiness=readiness,
            clock=lambda: 123.0,
            section_limit=5,
        )

        payload = await service.overview(actor=actor, project_id="project-large")

        self.assertEqual(work_items.calls, [(None, 10), ("closed", 5)])
        self.assertEqual(len(payload["sections"]["active_work"]["items"]), 5)
        self.assertEqual(len(payload["sections"]["blocked_work"]["items"]), 5)
        self.assertEqual(len(payload["sections"]["recently_completed"]["items"]), 5)
        self.assertEqual(payload["sections"]["active_work"]["count"], 1300)
        self.assertEqual(payload["sections"]["blocked_work"]["count"], 650)
        self.assertEqual(payload["sections"]["recently_completed"]["count"], 500)
        self.assertEqual(readiness.calls, [("project-large", False)])
        self.assertEqual(payload["sections"]["project_readiness"]["items"][0]["checks"][0]["message"], "A qualified worker is required.")

    def test_section_limit_has_a_hard_upper_bound(self) -> None:
        service = HomeOverviewService(
            projects=_Projects(),
            work_items=_WorkItems(),
            attention=_EmptyList(),
            approvals=_EmptyList(),
            incidents=_EmptyList(),
            agent_sessions=_EmptyList(),
            goals=_EmptyGoals(),
            schedules=None,
            section_limit=10_000,
        )

        self.assertEqual(service.section_limit, 10)
        section = service._section([{"id": index} for index in range(1300)], total=1300)
        self.assertEqual(len(section["items"]), 10)
        self.assertEqual(section["count"], 1300)


if __name__ == "__main__":
    unittest.main()
