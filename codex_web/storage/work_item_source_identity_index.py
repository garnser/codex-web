from __future__ import annotations

import hashlib
import threading
from typing import Any

from codex_web.models import TaskSourceIdentity, WorkItemState
from codex_web.storage.state_store import StateStore


class WorkItemSourceIdentityConflict(RuntimeError):
    pass


def canonical_task_source_identity(
    identity: TaskSourceIdentity,
) -> tuple[str, str, str]:
    return (
        identity.source_type.strip().casefold(),
        identity.source_instance.strip().rstrip("/"),
        identity.external_id.strip(),
    )


class WorkItemSourceIdentityIndex:
    """Canonical indexed lookup from TaskSource identity to Work Item ref."""

    NAMESPACE = "work_item_source_identity_lookup"
    REF_NAMESPACE = "work_item_source_identity_ref_lookup"

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._metrics = {
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "upserts": 0,
            "removes": 0,
            "conflicts": 0,
            "rebuilds": 0,
        }

    @staticmethod
    def _identity_key(identity: TaskSourceIdentity) -> str:
        canonical = "\x00".join(canonical_task_source_identity(identity))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _identity_payload(identity: TaskSourceIdentity) -> dict[str, str]:
        source_type, source_instance, external_id = (
            canonical_task_source_identity(identity)
        )
        return {
            "source_type": source_type,
            "source_instance": source_instance,
            "external_id": external_id,
        }

    def _metric(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._metrics[key] = int(self._metrics.get(key, 0)) + amount

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def ref_for_identity(
        self,
        identity: TaskSourceIdentity,
    ) -> str | None:
        self._metric("lookups")
        key = self._identity_key(identity)
        row = self.store.record_get(self.NAMESPACE, key)
        if not isinstance(row, dict):
            self._metric("misses")
            return None
        expected = self._identity_payload(identity)
        if any(row.get(name) != value for name, value in expected.items()):
            self._metric("conflicts")
            raise WorkItemSourceIdentityConflict(
                "TaskSource identity index hash collision or corrupt entry"
            )
        ref = str(row.get("ref") or "").strip()
        if not ref:
            self._metric("misses")
            return None
        self._metric("hits")
        return ref

    def upsert(self, state: WorkItemState) -> None:
        identity = state.source_identity
        previous = self.store.record_get(self.REF_NAMESPACE, state.ref)
        previous_key = (
            str(previous.get("identity_key") or "")
            if isinstance(previous, dict)
            else ""
        )

        if identity is None:
            if previous_key:
                self.store.record_apply(
                    self.NAMESPACE,
                    upserts={},
                    deletes=(previous_key,),
                )
                self.store.record_apply(
                    self.REF_NAMESPACE,
                    upserts={},
                    deletes=(state.ref,),
                )
                self._metric("removes")
            return

        key = self._identity_key(identity)
        current = self.store.record_get(self.NAMESPACE, key)
        if isinstance(current, dict):
            current_ref = str(current.get("ref") or "").strip()
            if current_ref and current_ref != state.ref:
                self._metric("conflicts")
                raise WorkItemSourceIdentityConflict(
                    "duplicate canonical TaskSource identity is already "
                    f"indexed by Work Item {current_ref!r}"
                )

        if previous_key and previous_key != key:
            self.store.record_apply(
                self.NAMESPACE,
                upserts={},
                deletes=(previous_key,),
            )

        payload = {
            **self._identity_payload(identity),
            "ref": state.ref,
        }
        self.store.record_apply(
            self.NAMESPACE,
            upserts={key: payload},
        )
        self.store.record_apply(
            self.REF_NAMESPACE,
            upserts={
                state.ref: {
                    "identity_key": key,
                }
            },
        )
        self._metric("upserts")

    def remove(self, ref: str) -> None:
        previous = self.store.record_get(self.REF_NAMESPACE, ref)
        if not isinstance(previous, dict):
            return
        key = str(previous.get("identity_key") or "")
        if key:
            self.store.record_apply(
                self.NAMESPACE,
                upserts={},
                deletes=(key,),
            )
        self.store.record_apply(
            self.REF_NAMESPACE,
            upserts={},
            deletes=(ref,),
        )
        self._metric("removes")

    def rebuild(self, states: dict[str, WorkItemState]) -> None:
        """Explicitly reconcile existing canonical Work Items into the index."""

        desired_by_key: dict[str, dict[str, str]] = {}
        desired_ref_lookup: dict[str, dict[str, str]] = {}
        for state in states.values():
            identity = state.source_identity
            if identity is None:
                continue
            key = self._identity_key(identity)
            existing = desired_by_key.get(key)
            if existing is not None and existing["ref"] != state.ref:
                self._metric("conflicts")
                raise WorkItemSourceIdentityConflict(
                    "duplicate canonical TaskSource identity detected during "
                    f"index rebuild: {existing['ref']!r}, {state.ref!r}"
                )
            desired_by_key[key] = {
                **self._identity_payload(identity),
                "ref": state.ref,
            }
            desired_ref_lookup[state.ref] = {"identity_key": key}

        self.store.record_replace(self.NAMESPACE, desired_by_key)
        self.store.record_replace(self.REF_NAMESPACE, desired_ref_lookup)
        self._metric("rebuilds")
