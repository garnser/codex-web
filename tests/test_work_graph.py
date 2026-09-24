from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.services.work_graph import (
    WorkGraphConflictError,
    WorkGraphCycleError,
    WorkGraphNotFoundError,
    WorkGraphScopeError,
    WorkGraphService,
)
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import (
    DependencyFailureBehavior,
    WorkGraphEdge,
    WorkGraphEdgeCreate,
    WorkGraphRelation,
    WorkReadinessStatus,
)


def item(
    ref: str,
    *,
    project_id: str = "project-a",
    organization_id: str = "org-a",
    workspace_id: str = "ws-a",
    title: str | None = None,
) -> WorkItemState:
    now = time.time()
    return WorkItemState(
        ref=ref,
        organization_id=organization_id,
        workspace_id=workspace_id,
        project_id=project_id,
        title=title or ref,
        last_meaningful_update_at=now,
        updated_at=now,
        created_at=now,
    )


class WorkGraphServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.store = WorkGraphStore(sqlite)
        self.items = {
            ref: item(ref)
            for ref in ("A", "B", "C", "D", "E")
        }
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.service = WorkGraphService(self.store, lambda: self.items)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add(
        self,
        source: str,
        target: str,
        *,
        relation: WorkGraphRelation = WorkGraphRelation.BLOCKS,
        failure_behavior: DependencyFailureBehavior = DependencyFailureBehavior.PAUSE,
    ):
        return self.service.add_edge(
            WorkGraphEdgeCreate(
                relation=relation,
                source_ref=source,
                target_ref=target,
                failure_behavior=failure_behavior,
            ),
            scope=self.scope,
            actor_id="operator",
        )

    def test_fan_in_becomes_runnable_only_after_every_dependency_completes(self) -> None:
        self.add("A", "C")
        self.add("B", "C")

        blocked = self.service.readiness("C", scope=self.scope)
        self.assertEqual(blocked.status, WorkReadinessStatus.BLOCKED)
        self.assertEqual(blocked.blocking_refs, ("A", "B"))

        self.items["A"].terminal_outcome = "completed"
        still_blocked = self.service.readiness("C", scope=self.scope)
        self.assertEqual(still_blocked.blocking_refs, ("B",))

        self.items["B"].terminal_outcome = "completed"
        ready = self.service.readiness("C", scope=self.scope)
        self.assertEqual(ready.status, WorkReadinessStatus.RUNNABLE)
        self.assertEqual(ready.reasons, ())

    def test_fan_out_exposes_parallel_runnable_work(self) -> None:
        self.add("A", "B")
        self.add("A", "C")
        self.items["A"].terminal_outcome = "completed"

        graph = self.service.snapshot("project-a", scope=self.scope)

        self.assertIn("B", graph.runnable_refs)
        self.assertIn("C", graph.runnable_refs)
        self.assertIn("D", graph.runnable_refs)
        self.assertNotIn("A", graph.runnable_refs)

    def test_block_dependency_cycles_are_rejected(self) -> None:
        self.add("A", "B")
        self.add("B", "C")

        with self.assertRaisesRegex(WorkGraphCycleError, "cycle"):
            self.add("C", "A")

    def test_parent_hierarchy_is_single_parent_and_acyclic(self) -> None:
        self.add("A", "B", relation=WorkGraphRelation.PARENT)
        self.add("B", "C", relation=WorkGraphRelation.PARENT)

        with self.assertRaisesRegex(WorkGraphConflictError, "already has a parent"):
            self.add("D", "C", relation=WorkGraphRelation.PARENT)

        with self.assertRaisesRegex(WorkGraphCycleError, "cycle"):
            self.add("C", "A", relation=WorkGraphRelation.PARENT)

        self.assertEqual(
            self.service.traverse(
                "A",
                scope=self.scope,
                relation=WorkGraphRelation.PARENT,
                direction="downstream",
            ),
            ("B", "C"),
        )
        self.assertEqual(
            self.service.traverse(
                "C",
                scope=self.scope,
                relation=WorkGraphRelation.PARENT,
                direction="upstream",
            ),
            ("B", "A"),
        )

    def test_failed_dependency_produces_deterministic_replan_impact(self) -> None:
        self.add(
            "A",
            "C",
            failure_behavior=DependencyFailureBehavior.REPLAN,
        )
        self.items["A"].terminal_outcome = "failed"

        readiness = self.service.readiness("C", scope=self.scope)

        self.assertEqual(readiness.status, WorkReadinessStatus.BLOCKED)
        self.assertEqual(len(readiness.failure_impacts), 1)
        impact = readiness.failure_impacts[0]
        self.assertEqual(impact.blocker_ref, "A")
        self.assertEqual(impact.blocked_ref, "C")
        self.assertEqual(impact.blocker_outcome, "failed")
        self.assertEqual(impact.behavior, DependencyFailureBehavior.REPLAN)
        self.assertIn("downstream behavior is replan", impact.reason)

    def test_canonical_work_item_blockers_are_part_of_readiness(self) -> None:
        self.items["D"].blocker = "waiting for operator"
        self.items["E"].blocking_findings = ["validation failed"]

        d = self.service.readiness("D", scope=self.scope)
        e = self.service.readiness("E", scope=self.scope)

        self.assertEqual(d.status, WorkReadinessStatus.BLOCKED)
        self.assertIn("canonical work item blocker", d.reasons[0])
        self.assertEqual(e.status, WorkReadinessStatus.BLOCKED)
        self.assertIn("canonical blocking findings", e.reasons[0])

    def test_critical_path_and_progress_are_deterministic(self) -> None:
        self.add("A", "B")
        self.add("B", "C")
        self.add("A", "D")
        self.items["A"].terminal_outcome = "completed"
        self.items["E"].terminal_outcome = "failed"

        graph = self.service.snapshot("project-a", scope=self.scope)

        self.assertEqual(graph.critical_path.refs, ("A", "B", "C"))
        self.assertEqual(graph.critical_path.node_count, 3)
        self.assertEqual(graph.critical_path.edge_count, 2)
        self.assertEqual(graph.progress.total, 5)
        self.assertEqual(graph.progress.completed, 1)
        self.assertEqual(graph.progress.failed, 1)
        self.assertEqual(graph.progress.active, 3)
        self.assertAlmostEqual(graph.progress.completion_fraction, 0.2)

    def test_cross_project_relationships_fail_closed(self) -> None:
        self.items["E"] = item("E", project_id="project-b")

        with self.assertRaisesRegex(WorkGraphScopeError, "same project"):
            self.add("A", "E")

    def test_cross_tenant_work_item_is_not_disclosed(self) -> None:
        self.items["E"] = item(
            "E",
            organization_id="org-b",
            workspace_id="ws-b",
        )

        with self.assertRaisesRegex(
            WorkGraphNotFoundError,
            "work item not found",
        ):
            self.add("A", "E")

    def test_large_snapshot_loads_items_and_edges_once(self) -> None:
        self.items = {
            f"N{index:04d}": item(f"N{index:04d}")
            for index in range(500)
        }
        item_loads = 0
        edge_loads = 0
        original_load = self.store.load

        def load_items():
            nonlocal item_loads
            item_loads += 1
            return self.items

        def load_edges():
            nonlocal edge_loads
            edge_loads += 1
            return original_load()

        service = WorkGraphService(self.store, load_items)
        self.store.load = load_edges

        graph = service.snapshot("project-a", scope=self.scope)

        self.assertEqual(graph.progress.total, 500)
        self.assertEqual(item_loads, 1)
        self.assertEqual(edge_loads, 1)

    def test_deep_dependency_chain_uses_iterative_critical_path(self) -> None:
        refs = tuple(f"N{index:04d}" for index in range(1200))
        edges = tuple(
            WorkGraphEdge(
                organization_id="org-a",
                workspace_id="ws-a",
                project_id="project-a",
                relation=WorkGraphRelation.BLOCKS,
                source_ref=refs[index],
                target_ref=refs[index + 1],
                created_by="test",
            )
            for index in range(len(refs) - 1)
        )

        critical = self.service._critical_path(refs, edges)

        self.assertEqual(critical.node_count, 1200)
        self.assertEqual(critical.edge_count, 1199)
        self.assertEqual(critical.refs[0], "N0000")
        self.assertEqual(critical.refs[-1], "N1199")

    def test_all_failure_behaviors_are_reported_without_llm_reasoning(self) -> None:
        behaviors = (
            DependencyFailureBehavior.PAUSE,
            DependencyFailureBehavior.FAIL,
            DependencyFailureBehavior.REPLAN,
            DependencyFailureBehavior.ESCALATE,
        )
        for index, behavior in enumerate(behaviors):
            blocker = f"F{index}"
            blocked = f"T{index}"
            self.items[blocker] = item(blocker)
            self.items[blocked] = item(blocked)
            self.add(blocker, blocked, failure_behavior=behavior)
            self.items[blocker].terminal_outcome = "failed"

            readiness = self.service.readiness(blocked, scope=self.scope)

            self.assertEqual(readiness.status, WorkReadinessStatus.BLOCKED)
            self.assertEqual(readiness.failure_impacts[0].behavior, behavior)
            self.assertIn(
                f"downstream behavior is {behavior.value}",
                readiness.failure_impacts[0].reason,
            )

    def test_edge_mutation_is_persistent_and_audited(self) -> None:
        edge = self.add("A", "B")
        reloaded = WorkGraphService(self.store, lambda: self.items)

        graph = reloaded.snapshot("project-a", scope=self.scope)
        self.assertEqual([item.id for item in graph.edges], [edge.id])

        removed = reloaded.remove_edge(
            edge.id,
            scope=self.scope,
            actor_id="operator-2",
        )
        self.assertEqual(removed.id, edge.id)
        self.assertEqual(
            [event.event_type for event in reloaded.events(scope=self.scope)],
            ["work_graph_edge_removed", "work_graph_edge_added"],
        )

    def test_duplicate_dependency_is_idempotent_but_behavior_change_is_explicit(self) -> None:
        first = self.add("A", "B")
        second = self.add("A", "B")
        self.assertEqual(first.id, second.id)

        with self.assertRaisesRegex(
            WorkGraphConflictError,
            "different failure behavior",
        ):
            self.add(
                "A",
                "B",
                failure_behavior=DependencyFailureBehavior.ESCALATE,
            )


if __name__ == "__main__":
    unittest.main()
