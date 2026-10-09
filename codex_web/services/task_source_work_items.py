from __future__ import annotations

import contextlib
import threading
import time
from datetime import datetime, timezone
from typing import Any

from codex_web.models import WorkItemArtifactRelation, WorkItemEvent, WorkItemState
from codex_web.services.task_source_conformance import TaskSourceConformanceSuite
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.task_source_reconciliation import same_task_source_identity
from codex_web.services.task_sources import TaskSource, TaskSourceSnapshot
from codex_web.services.work_item_state import WorkItemStateMachine


class TaskSourceWorkItemProjector:
    """Project normalized authoritative task snapshots into canonical work state.

    Provider adapters own transport and native-to-canonical mapping. This class
    owns the shared persistence/update semantics so canonical WorkItemState code
    never needs provider payload objects. Projection is deliberately synchronous:
    it performs only deterministic in-process state transformation/persistence.
    """

    def __init__(
        self,
        host: Any | None,
        state_machine: WorkItemStateMachine,
        *,
        dependencies: WorkItemRuntimeDependencies | None = None,
    ) -> None:
        if dependencies is None:
            if host is None:
                raise TypeError(
                    "TaskSourceWorkItemProjector requires work-item dependencies"
                )
            dependencies = WorkItemRuntimeDependencies.from_host(host)
        self.dependencies = dependencies
        self.state_machine = state_machine
        self.conformance = TaskSourceConformanceSuite()
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "keyed_gets": 0,
            "source_identity_index_lookups": 0,
            "compatibility_full_scans": 0,
            "keyed_saves": 0,
            "compatibility_bulk_saves": 0,
        }

    def _metric(self, name: str) -> None:
        with self._metrics_lock:
            self._metrics[name] = int(self._metrics.get(name, 0)) + 1

    def metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return dict(self._metrics)

    @staticmethod
    def _revision_timestamp(snapshot: TaskSourceSnapshot) -> float | None:
        return TaskSourceWorkItemProjector._parse_timestamp(
            snapshot.identity.revision
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        return None

    @classmethod
    def _reconcile_artifact_relations(
        cls,
        snapshot: TaskSourceSnapshot,
        state: WorkItemState | None,
    ) -> list[WorkItemArtifactRelation] | None:
        if snapshot.artifact_relations is None:
            return (
                list(state.verified_artifact_relations)
                if state is not None
                and state.verified_artifact_relations is not None
                else None
            )
        project_path = snapshot.identity.external_id.partition("#")[0].strip()
        source_type = snapshot.identity.source_type.strip().casefold()
        source_instance = snapshot.identity.source_instance.strip().rstrip("/")
        previous = {
            item.ref: item
            for item in (
                state.verified_artifact_relations
                if state is not None and state.verified_artifact_relations
                else []
            )
        }
        reconciled: list[WorkItemArtifactRelation] = []
        for relation in snapshot.artifact_relations:
            relation_project, separator, relation_iid = relation.ref.partition("!")
            if (
                relation.source_type.strip().casefold() != source_type
                or relation.source_instance.strip().rstrip("/") != source_instance
                or relation_project.strip() != project_path
                or not separator
                or not relation_iid.isdigit()
            ):
                continue
            prior = previous.get(relation.ref)
            prior_timestamp = (
                cls._parse_timestamp(prior.source_revision) if prior else None
            )
            incoming_timestamp = cls._parse_timestamp(relation.source_revision)
            if (
                prior is not None
                and prior_timestamp is not None
                and incoming_timestamp is not None
                and incoming_timestamp < prior_timestamp
            ):
                reconciled.append(prior)
            else:
                reconciled.append(relation)
        return sorted(reconciled, key=lambda item: item.ref)

    @staticmethod
    def _open_verified_mr_refs(
        relations: list[WorkItemArtifactRelation] | None,
    ) -> list[str]:
        return sorted(
            relation.ref
            for relation in relations or []
            if relation.kind == "merge_request" and relation.state == "opened"
        )

    @staticmethod
    def _first_prefixed(labels: tuple[str, ...], prefix: str) -> str | None:
        prefix_cf = prefix.casefold()
        for label in labels:
            value = str(label).strip()
            if value.casefold().startswith(prefix_cf):
                return value
        return None

    @staticmethod
    def _find_existing_state(
        states: dict[str, WorkItemState],
        snapshot: TaskSourceSnapshot,
    ) -> WorkItemState | None:
        direct = states.get(snapshot.identity.external_id.strip())
        if direct is not None:
            return direct
        for candidate in states.values():
            if candidate.source_identity is None:
                continue
            if same_task_source_identity(candidate.source_identity, snapshot.identity):
                return candidate
        return None

    def _project_tenant(self, project_id: str) -> tuple[str, str]:
        for project in self.dependencies.load_projects():
            if getattr(project, "id", None) == project_id:
                return (
                    str(
                        getattr(project, "organization_id", "local")
                        or "local"
                    ),
                    str(
                        getattr(project, "workspace_id", "default")
                        or "default"
                    ),
                )
        return ("local", "default")

    def _project_resource_ids(
        self,
        project_id: str,
        *,
        source_type: str,
        project_path: str | None,
    ) -> list[str]:
        try:
            if source_type.strip().casefold() == "gitlab" and project_path:
                return list(
                    dict.fromkeys(
                        str(item)
                        for item in self.dependencies.resource_ids_for_project(
                            project_id,
                            alias_value=project_path,
                            provider="gitlab",
                        )
                        if str(item).strip()
                    )
                )
            return list(
                dict.fromkeys(
                    str(item)
                    for item in self.dependencies.resource_ids_for_project(
                        project_id
                    )
                    if str(item).strip()
                )
            )
        except Exception:
            return []

    def _persist(
        self,
        state: WorkItemState,
        states: dict[str, WorkItemState] | None,
    ) -> None:
        if self.dependencies.save_state is not None:
            self._metric("keyed_saves")
            self.dependencies.save_state(state)
            return
        self._metric("compatibility_bulk_saves")
        if states is None:
            states = self.dependencies.load_states()
        states[state.ref] = state
        self.dependencies.save_states(states)

    def upsert(
        self,
        source: TaskSource,
        snapshot: TaskSourceSnapshot,
        *,
        project_id: str,
        reconcile_implementation_owner: bool = False,
        reconciliation_actor: str | None = None,
        reconciliation_reason: str | None = None,
    ) -> WorkItemState:
        self.conformance.validate_snapshot(source, snapshot)
        external_ref = snapshot.identity.external_id.strip()
        if not external_ref:
            raise ValueError("Task-source snapshot external identity must not be empty")

        states: dict[str, WorkItemState] | None = None
        state: WorkItemState | None = None

        if self.dependencies.get_state is not None:
            self._metric("keyed_gets")
            state = self.dependencies.get_state(external_ref)

        if (
            state is None
            and self.dependencies.get_state_by_source_identity is not None
        ):
            self._metric("source_identity_index_lookups")
            state = self.dependencies.get_state_by_source_identity(
                snapshot.identity
            )

        if state is None and (
            self.dependencies.get_state is None
            or self.dependencies.get_state_by_source_identity is None
        ):
            self._metric("compatibility_full_scans")
            states = self.dependencies.load_states()
            state = self._find_existing_state(states, snapshot)

        ref = state.ref if state is not None else external_ref
        projection = source.project(
            snapshot,
            current_stage=state.current_stage if state is not None else None,
        )
        self.conformance.validate_projection(source, snapshot, projection)

        now = time.time()
        organization_id, workspace_id = self._project_tenant(project_id)
        source_timestamp = self._revision_timestamp(snapshot)
        labels = sorted(dict.fromkeys(str(label).strip() for label in snapshot.labels if str(label).strip()))
        status_label = self._first_prefixed(tuple(labels), "status::")
        priority = self._first_prefixed(tuple(labels), "priority::")
        project_path = external_ref.split("#", 1)[0] if "#" in external_ref else None
        resource_ids = self._project_resource_ids(
            project_id,
            source_type=snapshot.identity.source_type,
            project_path=project_path,
        )
        projected_stage = projection.stage or (state.current_stage if state is not None else "implementation_active")
        projected_owner = None if projected_stage == "closed" else projection.owner
        projected_status_label = None if projected_stage == "closed" else status_label
        artifact_relations = self._reconcile_artifact_relations(snapshot, state)
        verified_mr_refs = self._open_verified_mr_refs(artifact_relations)
        if (
            state is not None
            and state.current_stage == "ready_for_validation"
            and projected_stage == "implementation_active"
            and verified_mr_refs
            and (snapshot.source_state or "").strip().casefold()
            not in {"closed", "merged"}
        ):
            projected_stage = "ready_for_validation"

        if state is None:
            state = WorkItemState(
                ref=ref,
                organization_id=organization_id,
                workspace_id=workspace_id,
                project_id=project_id,
                resource_ids=resource_ids,
                project_path=project_path,
                source_identity=snapshot.identity,
                title=snapshot.title,
                url=snapshot.identity.external_url,
                kind="issue",
                priority=priority,
                current_owner=projected_owner,
                current_stage=projected_stage,
                implementation_owner=(
                    projected_owner
                    if projected_owner and projected_owner not in self.dependencies.non_implementation_owners
                    else None
                ),
                validation_owner=self.dependencies.default_validation_owner,
                release_owner=self.dependencies.default_release_owner,
                artifact_state="branch",
                last_meaningful_update_at=now,
                last_owner_activity_at=now,
                # Transitional persisted field retained until lifecycle metadata
                # migration replaces the provider-specific name.
                last_gitlab_event_at=source_timestamp or now,
                blocker=None,
                next_action=None,
                next_owner=None,
                release_gate=priority == "priority::P1",
                status_label=projected_status_label,
                labels=labels,
                mr_refs=verified_mr_refs,
                verified_artifact_relations=artifact_relations,
                created_at=now,
                updated_at=now,
                closed_at=(source_timestamp or now) if projected_stage == "closed" else None,
            )
            self.state_machine._append_work_item_event(
                self.state_machine._work_item_event(
                    ref,
                    "task_source_snapshot_backfilled",
                    payload={
                        "project_id": project_id,
                        "source_type": snapshot.identity.source_type,
                        "current_owner": state.current_owner,
                        "current_stage": state.current_stage,
                        "priority": state.priority,
                    },
                )
            )
            if state.current_stage == "failed_with_action_owner":
                state = self.state_machine._reconcile_blocked_work_item_state(
                    state,
                    projected_owner=projected_owner,
                    previous_owner=None,
                    now=now,
                )
        else:
            implementation_owner_reconciled = False
            previous_stage = state.current_stage
            previous_owner = state.current_owner
            previous_status_label = state.status_label
            previous_priority = state.priority
            was_closed = bool(state.closed_at)
            routing_metadata_changed = (
                state.organization_id != organization_id
                or state.workspace_id != workspace_id
                or state.project_id != project_id
                or state.resource_ids != resource_ids
                or (project_path and state.project_path != project_path)
            )
            state.organization_id = organization_id
            state.workspace_id = workspace_id
            state.project_id = project_id
            state.resource_ids = resource_ids
            state.project_path = project_path or state.project_path
            # Artifact events historically used the same ``project#iid`` key
            # as issues. After artifact refs became kind-qualified, an
            # authoritative issue snapshot must be allowed to repair such a
            # collided record even when the later MR event has a newer
            # timestamp.
            identity_kind_mismatch = state.kind != "issue"

            if (
                source_timestamp is not None
                and state.last_gitlab_event_at is not None
                and source_timestamp < state.last_gitlab_event_at
                and not identity_kind_mismatch
            ):
                self.state_machine._append_work_item_event(
                    self.state_machine._work_item_event(
                        ref,
                        "task_source_snapshot_stale_ignored",
                        payload={
                            "source_type": snapshot.identity.source_type,
                            "projected_owner": projected_owner,
                            "projected_stage": projected_stage,
                            "source_timestamp": source_timestamp,
                        },
                    )
                )
                if routing_metadata_changed:
                    state.updated_at = now
                    self._persist(state, states)
                return state

            if (
                reconcile_implementation_owner
                and projection.owner_known
                and projected_owner
                and self.state_machine._coerce_owner(projected_owner)
                not in self.dependencies.non_implementation_owners
            ):
                previous_implementation_owner = self.state_machine._coerce_owner(
                    state.implementation_owner
                )
                reconciled_implementation_owner = self.state_machine._coerce_owner(
                    projected_owner
                )
                if (
                    reconciled_implementation_owner
                    and reconciled_implementation_owner
                    != previous_implementation_owner
                ):
                    state.implementation_owner = reconciled_implementation_owner
                    state.updated_at = now
                    implementation_owner_reconciled = True
                    self.state_machine._append_work_item_event(
                        WorkItemEvent(
                            ref=ref,
                            event_type="implementation_owner_reconciled",
                            created_at=now,
                            actor=reconciliation_actor,
                            source="operator-ui",
                            reason=(
                                reconciliation_reason
                                or "operator reconcile from authoritative assignment"
                            ),
                            payload={
                                "previous_implementation_owner": (
                                    previous_implementation_owner
                                ),
                                "implementation_owner": (
                                    reconciled_implementation_owner
                                ),
                                "project_id": project_id,
                                "resource_ids": resource_ids,
                                "source_type": snapshot.identity.source_type,
                                "source_instance": (
                                    snapshot.identity.source_instance
                                ),
                                "external_id": snapshot.identity.external_id,
                                "source_revision": snapshot.identity.revision,
                            },
                        )
                    )

            if self.state_machine._preserve_accepted_handoff_recipient(
                state,
                incoming_owner=projected_owner,
                incoming_stage=projected_stage,
                incoming_status_label=projected_status_label,
            ):
                self.state_machine._append_work_item_event(
                    self.state_machine._work_item_event(
                        ref,
                        "task_source_snapshot_handoff_projection_ignored",
                        payload={
                            "source_type": snapshot.identity.source_type,
                            "projected_owner": projected_owner,
                            "projected_stage": projected_stage,
                        },
                    )
                )
                if routing_metadata_changed or implementation_owner_reconciled:
                    state.updated_at = now
                    self._persist(state, states)
                return state

            state.source_identity = snapshot.identity
            state.kind = "issue"
            state.title = snapshot.title or state.title
            state.url = snapshot.identity.external_url or state.url
            state.priority = priority or state.priority
            state.labels = labels
            state.status_label = projected_status_label
            state.release_gate = priority == "priority::P1" or state.release_gate
            if snapshot.artifact_relations is not None:
                previous_relations = state.verified_artifact_relations
                state.verified_artifact_relations = artifact_relations
                state.mr_refs = verified_mr_refs
                if previous_relations != artifact_relations:
                    self.state_machine._append_work_item_event(
                        self.state_machine._work_item_event(
                            ref,
                            "task_source_artifact_relations_reconciled",
                            payload={
                                "source_type": snapshot.identity.source_type,
                                "mr_refs": verified_mr_refs,
                                "relation_states": {
                                    item.ref: item.state
                                    for item in artifact_relations or []
                                },
                            },
                        )
                    )
            state = self.state_machine._transition_work_item_stage(
                state,
                projected_stage,
                source="task-source-snapshot-projection",
                external_projection=True,
            )
            if projection.owner_known and projected_owner and not (state.handoff and state.handoff.status == "pending"):
                state.current_owner = projected_owner
            state.updated_at = now
            state.last_gitlab_event_at = source_timestamp or now

            if projected_stage == "closed":
                state = self.state_machine._normalize_closed_work_item_state(
                    state,
                    now=now,
                    reason_code="task_source_closed",
                    closed_at=source_timestamp or now,
                )
            else:
                state.closed_at = None
                if projected_stage == "implementation_active" and not (
                    state.handoff and state.handoff.status == "pending"
                ):
                    state.blocker = None
                    if self.state_machine._coerce_owner(state.next_owner) != self.state_machine._coerce_owner(state.current_owner):
                        state.next_owner = None

            if (
                previous_stage != state.current_stage
                or previous_owner != state.current_owner
                or previous_status_label != state.status_label
                or previous_priority != state.priority
                or was_closed != bool(state.closed_at)
            ):
                state.last_meaningful_update_at = now
                state.last_owner_activity_at = now

            if state.current_stage == "failed_with_action_owner":
                state = self.state_machine._reconcile_blocked_work_item_state(
                    state,
                    projected_owner=projected_owner,
                    previous_owner=previous_owner,
                    now=now,
                )

        state = self.state_machine._ensure_work_item_lane_defaults(state)
        state.artifact_state = self.state_machine._infer_artifact_state_from_state(state)
        self._persist(state, states)
        return state
