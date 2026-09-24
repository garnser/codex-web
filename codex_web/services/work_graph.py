from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable, Iterable

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import (
    DependencyFailureBehavior,
    WorkDependencyImpact,
    WorkGraphCriticalPath,
    WorkGraphEdge,
    WorkGraphEdgeCreate,
    WorkGraphEvent,
    WorkGraphNode,
    WorkGraphProgress,
    WorkGraphRelation,
    WorkGraphSnapshot,
    WorkGraphState,
    WorkReadiness,
    WorkReadinessStatus,
)


class WorkGraphError(RuntimeError):
    pass


class WorkGraphNotFoundError(WorkGraphError):
    pass


class WorkGraphConflictError(WorkGraphError):
    pass


class WorkGraphCycleError(WorkGraphConflictError):
    pass


class WorkGraphScopeError(WorkGraphError):
    pass


class WorkGraphService:
    """Deterministic project work graph over canonical Work Item lifecycle state."""

    def __init__(
        self,
        store: WorkGraphStore,
        work_items: Callable[[], dict[str, WorkItemState] | Iterable[WorkItemState]],
    ) -> None:
        self.store = store
        self._work_items = work_items

    def _all_items(self) -> dict[str, WorkItemState]:
        raw = self._work_items()
        if isinstance(raw, dict):
            return dict(raw)
        return {item.ref: item for item in raw}

    @staticmethod
    def _scope_matches(item: WorkItemState, scope: TenantScope) -> bool:
        return (
            item.organization_id == scope.organization_id
            and item.workspace_id == scope.workspace_id
        )

    def _item(self, ref: str, scope: TenantScope) -> WorkItemState:
        item = self._all_items().get(ref)
        if item is None or not self._scope_matches(item, scope):
            raise WorkGraphNotFoundError("work item not found")
        return item

    def _project_items(
        self,
        project_id: str,
        scope: TenantScope,
    ) -> dict[str, WorkItemState]:
        return {
            ref: item
            for ref, item in self._all_items().items()
            if self._scope_matches(item, scope) and item.project_id == project_id
        }

    def _visible_edges(
        self,
        *,
        project_id: str,
        scope: TenantScope,
    ) -> list[WorkGraphEdge]:
        return [
            edge
            for edge in self.store.load().edges
            if edge.organization_id == scope.organization_id
            and edge.workspace_id == scope.workspace_id
            and edge.project_id == project_id
        ]

    @staticmethod
    def _adjacency(
        edges: Iterable[WorkGraphEdge],
        relation: WorkGraphRelation,
    ) -> dict[str, set[str]]:
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            if edge.relation != relation:
                continue
            adjacency.setdefault(edge.source_ref, set()).add(edge.target_ref)
            adjacency.setdefault(edge.target_ref, set())
        return adjacency

    @classmethod
    def _path_exists(
        cls,
        edges: Iterable[WorkGraphEdge],
        relation: WorkGraphRelation,
        start: str,
        target: str,
    ) -> bool:
        adjacency = cls._adjacency(edges, relation)
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(sorted(adjacency.get(current, ()), reverse=True))
        return False

    def add_edge(
        self,
        payload: WorkGraphEdgeCreate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> WorkGraphEdge:
        source = self._item(payload.source_ref, scope)
        target = self._item(payload.target_ref, scope)
        if not source.project_id or source.project_id != target.project_id:
            raise WorkGraphScopeError(
                "work graph edges require Work Items in the same project"
            )
        project_id = source.project_id

        result: WorkGraphEdge | None = None

        def apply(state: WorkGraphState) -> WorkGraphState:
            nonlocal result
            visible = [
                edge
                for edge in state.edges
                if edge.organization_id == scope.organization_id
                and edge.workspace_id == scope.workspace_id
                and edge.project_id == project_id
            ]
            existing = next(
                (
                    edge
                    for edge in visible
                    if edge.relation == payload.relation
                    and edge.source_ref == payload.source_ref
                    and edge.target_ref == payload.target_ref
                ),
                None,
            )
            if existing is not None:
                if (
                    payload.relation == WorkGraphRelation.BLOCKS
                    and existing.failure_behavior != payload.failure_behavior
                ):
                    raise WorkGraphConflictError(
                        "dependency already exists with different failure behavior"
                    )
                result = existing
                return state

            if payload.relation == WorkGraphRelation.PARENT:
                current_parent = next(
                    (
                        edge
                        for edge in visible
                        if edge.relation == WorkGraphRelation.PARENT
                        and edge.target_ref == payload.target_ref
                    ),
                    None,
                )
                if current_parent is not None:
                    raise WorkGraphConflictError(
                        "work item already has a parent relationship"
                    )

            candidate = WorkGraphEdge(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                project_id=project_id,
                relation=payload.relation,
                source_ref=payload.source_ref,
                target_ref=payload.target_ref,
                failure_behavior=payload.failure_behavior,
                created_by=actor_id,
                reason=payload.reason,
            )
            if self._path_exists(
                [*visible, candidate],
                payload.relation,
                payload.target_ref,
                payload.source_ref,
            ):
                raise WorkGraphCycleError(
                    f"{payload.relation.value} relationship would introduce a cycle"
                )
            state.edges.append(candidate)
            state.events.append(
                WorkGraphEvent(
                    event_type="work_graph_edge_added",
                    edge=candidate,
                    actor_id=actor_id,
                )
            )
            result = candidate
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def remove_edge(
        self,
        edge_id: str,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> WorkGraphEdge:
        removed: WorkGraphEdge | None = None

        def apply(state: WorkGraphState) -> WorkGraphState:
            nonlocal removed
            for index, edge in enumerate(state.edges):
                if edge.id != edge_id:
                    continue
                if (
                    edge.organization_id != scope.organization_id
                    or edge.workspace_id != scope.workspace_id
                ):
                    raise WorkGraphNotFoundError("work graph edge not found")
                removed = edge
                state.edges.pop(index)
                state.events.append(
                    WorkGraphEvent(
                        event_type="work_graph_edge_removed",
                        edge=edge,
                        actor_id=actor_id,
                    )
                )
                return state
            raise WorkGraphNotFoundError("work graph edge not found")

        self.store.update(apply)
        assert removed is not None
        return removed

    @staticmethod
    def _terminal_outcome(item: WorkItemState) -> str | None:
        if item.terminal_outcome is not None:
            return str(item.terminal_outcome)
        if item.current_stage == "closed":
            # Legacy closed rows without an explicit terminal outcome are treated
            # as successfully satisfied dependencies.
            return "completed"
        return None

    @classmethod
    def _readiness_from_loaded(
        cls,
        ref: str,
        item: WorkItemState,
        items: dict[str, WorkItemState],
        blocking_edges: Iterable[WorkGraphEdge],
    ) -> WorkReadiness:
        """Resolve readiness from one preloaded project snapshot.

        Callers that already loaded canonical Work Items and graph edges can
        avoid re-reading the full project for every node.
        """
        outcome = cls._terminal_outcome(item)
        if outcome is not None:
            return WorkReadiness(
                ref=ref,
                status=WorkReadinessStatus.TERMINAL,
                reasons=(f"work item is terminal: {outcome}",),
            )

        reasons: list[str] = []
        blocking_refs: list[str] = []
        impacts: list[WorkDependencyImpact] = []

        if item.blocker:
            reasons.append(f"canonical work item blocker: {item.blocker}")
        if item.blocking_findings:
            reasons.append(
                f"canonical blocking findings: {len(item.blocking_findings)}"
            )

        for edge in blocking_edges:
            blocker = items.get(edge.source_ref)
            if blocker is None:
                blocking_refs.append(edge.source_ref)
                reasons.append(
                    f"dependency {edge.source_ref} is missing from the project graph"
                )
                continue
            blocker_outcome = cls._terminal_outcome(blocker)
            if blocker_outcome == "completed":
                continue
            blocking_refs.append(blocker.ref)
            if blocker_outcome in {"failed", "cancelled"}:
                reason = (
                    f"dependency {blocker.ref} ended {blocker_outcome}; "
                    f"downstream behavior is {edge.failure_behavior.value}"
                )
                reasons.append(reason)
                impacts.append(
                    WorkDependencyImpact(
                        blocker_ref=blocker.ref,
                        blocked_ref=ref,
                        blocker_outcome=blocker_outcome,
                        behavior=edge.failure_behavior,
                        reason=reason,
                    )
                )
            else:
                reasons.append(
                    f"dependency {blocker.ref} has not completed successfully"
                )

        if reasons:
            return WorkReadiness(
                ref=ref,
                status=WorkReadinessStatus.BLOCKED,
                reasons=tuple(reasons),
                blocking_refs=tuple(dict.fromkeys(blocking_refs)),
                failure_impacts=tuple(impacts),
            )
        return WorkReadiness(ref=ref, status=WorkReadinessStatus.RUNNABLE)

    def readiness(
        self,
        ref: str,
        *,
        scope: TenantScope,
    ) -> WorkReadiness:
        all_items = self._all_items()
        item = all_items.get(ref)
        if item is None or not self._scope_matches(item, scope):
            raise WorkGraphNotFoundError("work item not found")
        if not item.project_id:
            return self._readiness_from_loaded(ref, item, {ref: item}, ())
        items = {
            candidate_ref: candidate
            for candidate_ref, candidate in all_items.items()
            if self._scope_matches(candidate, scope)
            and candidate.project_id == item.project_id
        }
        blocking_edges = [
            edge
            for edge in self._visible_edges(project_id=item.project_id, scope=scope)
            if edge.relation == WorkGraphRelation.BLOCKS
            and edge.target_ref == ref
        ]
        return self._readiness_from_loaded(
            ref,
            item,
            items,
            blocking_edges,
        )

    def traverse(
        self,
        ref: str,
        *,
        scope: TenantScope,
        relation: WorkGraphRelation | None = None,
        direction: str = "downstream",
    ) -> tuple[str, ...]:
        item = self._item(ref, scope)
        if not item.project_id:
            return ()
        if direction not in {"downstream", "upstream"}:
            raise ValueError("direction must be downstream or upstream")
        edges = self._visible_edges(project_id=item.project_id, scope=scope)
        adjacency: dict[str, set[str]] = {}
        for edge in edges:
            if relation is not None and edge.relation != relation:
                continue
            source = edge.source_ref if direction == "downstream" else edge.target_ref
            target = edge.target_ref if direction == "downstream" else edge.source_ref
            adjacency.setdefault(source, set()).add(target)

        pending = deque([ref])
        seen = {ref}
        result: list[str] = []
        while pending:
            current = pending.popleft()
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                result.append(neighbor)
                pending.append(neighbor)
        return tuple(result)

    @staticmethod
    def _critical_path(
        refs: Iterable[str],
        edges: Iterable[WorkGraphEdge],
    ) -> WorkGraphCriticalPath:
        ref_set = set(refs)
        adjacency: dict[str, set[str]] = {ref: set() for ref in ref_set}
        indegree: dict[str, int] = {ref: 0 for ref in ref_set}
        for edge in edges:
            if (
                edge.relation != WorkGraphRelation.BLOCKS
                or edge.source_ref not in ref_set
                or edge.target_ref not in ref_set
            ):
                continue
            if edge.target_ref in adjacency[edge.source_ref]:
                continue
            adjacency[edge.source_ref].add(edge.target_ref)
            indegree[edge.target_ref] += 1

        queue = [ref for ref in ref_set if indegree[ref] == 0]
        heapq.heapify(queue)
        best_path: dict[str, tuple[str, ...]] = {
            ref: (ref,)
            for ref in ref_set
        }
        processed = 0

        while queue:
            current = heapq.heappop(queue)
            processed += 1
            for child in sorted(adjacency[current]):
                candidate = (*best_path[current], child)
                existing = best_path[child]
                if (
                    len(candidate) > len(existing)
                    or (
                        len(candidate) == len(existing)
                        and candidate < existing
                    )
                ):
                    best_path[child] = candidate
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(queue, child)

        if processed != len(ref_set):
            raise WorkGraphCycleError(
                "stored dependency graph contains a cycle"
            )

        path = min(
            best_path.values(),
            key=lambda item: (-len(item), item),
        ) if best_path else ()
        return WorkGraphCriticalPath(
            refs=path,
            node_count=len(path),
            edge_count=max(0, len(path) - 1),
        )

    def snapshot(
        self,
        project_id: str,
        *,
        scope: TenantScope,
    ) -> WorkGraphSnapshot:
        items = self._project_items(project_id, scope)
        edges = self._visible_edges(project_id=project_id, scope=scope)
        nodes: list[WorkGraphNode] = []
        impacts: list[WorkDependencyImpact] = []
        runnable: list[str] = []
        completed = failed = cancelled = blocked = 0
        blocking_by_target: dict[str, list[WorkGraphEdge]] = {}
        for edge in edges:
            if edge.relation == WorkGraphRelation.BLOCKS:
                blocking_by_target.setdefault(edge.target_ref, []).append(edge)

        for ref in sorted(items):
            item = items[ref]
            readiness = self._readiness_from_loaded(
                ref,
                item,
                items,
                blocking_by_target.get(ref, ()),
            )
            outcome = self._terminal_outcome(item)
            if outcome == "completed":
                completed += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "cancelled":
                cancelled += 1
            elif readiness.status == WorkReadinessStatus.BLOCKED:
                blocked += 1
            if readiness.status == WorkReadinessStatus.RUNNABLE:
                runnable.append(ref)
            impacts.extend(readiness.failure_impacts)
            nodes.append(
                WorkGraphNode(
                    ref=ref,
                    title=item.title,
                    project_id=project_id,
                    stage=item.current_stage,
                    terminal_outcome=outcome,
                    readiness=readiness,
                )
            )

        total = len(items)
        active = total - completed - failed - cancelled
        progress = WorkGraphProgress(
            total=total,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            active=active,
            runnable=len(runnable),
            blocked=blocked,
            completion_fraction=(completed / total if total else 0.0),
        )
        return WorkGraphSnapshot(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            project_id=project_id,
            nodes=tuple(nodes),
            edges=tuple(sorted(edges, key=lambda item: (item.relation, item.source_ref, item.target_ref))),
            runnable_refs=tuple(runnable),
            failure_impacts=tuple(impacts),
            critical_path=self._critical_path(items, edges),
            progress=progress,
        )

    def events(
        self,
        *,
        scope: TenantScope,
        project_id: str | None = None,
        limit: int = 100,
    ) -> tuple[WorkGraphEvent, ...]:
        bounded = max(1, min(int(limit), 500))
        rows = [
            event
            for event in self.store.load().events
            if event.edge.organization_id == scope.organization_id
            and event.edge.workspace_id == scope.workspace_id
            and (project_id is None or event.edge.project_id == project_id)
        ]
        rows.sort(key=lambda item: item.occurred_at, reverse=True)
        return tuple(rows[:bounded])
