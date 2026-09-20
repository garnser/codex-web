from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.storage.state_store import StateStore


class WorkItemListIndex:
    """Project-scoped time index for bounded Work Item collection reads."""

    LOOKUP_NAMESPACE = "work_item_list_lookup"
    MAX_TIMESTAMP_MICROS = 9_999_999_999_999_999
    DEFAULT_SCAN_BUDGET = 5000

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _scope_key(scope: TenantScope, project_id: str) -> str:
        raw = (
            f"{scope.organization_id}\x00"
            f"{scope.workspace_id}\x00"
            f"{project_id}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @classmethod
    def namespace(cls, scope: TenantScope, project_id: str) -> str:
        return f"work_item_list:{cls._scope_key(scope, project_id)}"

    @classmethod
    def _inverse_timestamp(cls, value: float | None) -> int:
        micros = max(0, int(float(value or 0.0) * 1_000_000))
        return cls.MAX_TIMESTAMP_MICROS - min(
            micros,
            cls.MAX_TIMESTAMP_MICROS,
        )

    @classmethod
    def _key(cls, state: WorkItemState) -> str:
        return (
            f"{cls._inverse_timestamp(state.updated_at):016d}/"
            f"{state.ref}"
        )

    @staticmethod
    def _scope_for_state(state: WorkItemState) -> TenantScope:
        return TenantScope(
            organization_id=state.organization_id,
            workspace_id=state.workspace_id,
        )

    def revision(
        self,
        *,
        scope: TenantScope,
        project_id: str,
    ) -> float | None:
        return self.store.namespace_revision(
            self.namespace(scope, project_id)
        )

    def upsert(self, state: WorkItemState) -> None:
        if not state.project_id:
            self.remove(state.ref)
            return
        scope = self._scope_for_state(state)
        namespace = self.namespace(scope, state.project_id)
        key = self._key(state)
        previous = self.store.record_get(
            self.LOOKUP_NAMESPACE,
            state.ref,
        )
        previous_namespace = (
            str(previous.get("namespace") or "")
            if isinstance(previous, dict)
            else ""
        )
        previous_key = (
            str(previous.get("key") or "")
            if isinstance(previous, dict)
            else ""
        )

        if previous_namespace and previous_key and (
            previous_namespace != namespace
            or previous_key != key
        ):
            self.store.record_apply(
                previous_namespace,
                upserts={},
                deletes=(previous_key,),
            )

        self.store.record_apply(
            namespace,
            upserts={
                key: {
                    "ref": state.ref,
                    "updatedAt": state.updated_at,
                }
            },
        )
        self.store.record_apply(
            self.LOOKUP_NAMESPACE,
            upserts={
                state.ref: {
                    "namespace": namespace,
                    "key": key,
                }
            },
        )

    def remove(self, ref: str) -> None:
        previous = self.store.record_get(
            self.LOOKUP_NAMESPACE,
            ref,
        )
        if not isinstance(previous, dict):
            return
        namespace = str(previous.get("namespace") or "")
        key = str(previous.get("key") or "")
        if namespace and key:
            self.store.record_apply(
                namespace,
                upserts={},
                deletes=(key,),
            )
        self.store.record_apply(
            self.LOOKUP_NAMESPACE,
            upserts={},
            deletes=(ref,),
        )

    def rebuild(self, states: dict[str, WorkItemState]) -> None:
        """Reconcile the index during startup/compatibility checkpoints."""

        desired_refs = set(states)
        current = self.store.record_items(self.LOOKUP_NAMESPACE)
        for ref in set(current) - desired_refs:
            self.remove(ref)
        for state in states.values():
            self.upsert(state)

    def page(
        self,
        *,
        scope: TenantScope,
        project_id: str,
        after: str | None,
        limit: int,
        get_state: Callable[[str], WorkItemState | None],
        predicate: Callable[[WorkItemState], bool],
        scan_budget: int | None = None,
    ) -> tuple[list[WorkItemState], str | None, bool]:
        page_size = max(1, min(int(limit), 100))
        budget = max(
            page_size,
            min(
                int(scan_budget or self.DEFAULT_SCAN_BUDGET),
                20_000,
            ),
        )
        namespace = self.namespace(scope, project_id)
        cursor = after
        selected: list[WorkItemState] = []
        scanned = 0
        batch_size = min(1000, max(100, page_size * 4))

        while scanned < budget:
            rows, backend_next = self.store.record_page(
                namespace,
                after=cursor,
                limit=min(batch_size, budget - scanned),
            )
            if not rows:
                return selected, None, False

            for key, payload in rows.items():
                cursor = key
                scanned += 1
                if not isinstance(payload, dict):
                    continue
                ref = str(payload.get("ref") or "")
                if not ref:
                    continue
                state = get_state(ref)
                if state is None:
                    self.remove(ref)
                    continue
                if (
                    state.organization_id != scope.organization_id
                    or state.workspace_id != scope.workspace_id
                    or state.project_id != project_id
                ):
                    self.upsert(state)
                    continue
                if not predicate(state):
                    if scanned >= budget:
                        break
                    continue
                selected.append(state)
                if len(selected) >= page_size:
                    return selected, cursor, False
                if scanned >= budget:
                    break

            if scanned >= budget:
                return selected, cursor, bool(backend_next)
            if not backend_next:
                return selected, None, False
            cursor = backend_next

        return selected, cursor, True
