from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from codex_web.models import QueuedTurn
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import OperationTimingMetrics, StateStore


class TurnQueueRepository:
    """Keyed per-thread turn queues with rollback-compatible JSON checkpoints."""

    namespace = "turn_queues"

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._mirror_metrics = OperationTimingMetrics()

    def _legacy_payload(self) -> dict[str, Any]:
        if not self.legacy_path.exists():
            return {}
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _ensure_records(self) -> None:
        if self.store.record_collection_exists(self.namespace):
            return
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
        self.store.record_replace(
            self.namespace,
            payload if isinstance(payload, dict) else {},
        )

    @staticmethod
    def _validate(values: Any) -> list[QueuedTurn]:
        if not isinstance(values, list):
            return []
        return [
            QueuedTurn.model_validate(item)
            for item in values
            if isinstance(item, dict)
        ]

    def load(self) -> dict[str, list[QueuedTurn]]:
        self._ensure_records()
        raw = self.store.record_items(self.namespace)
        result = {
            thread_id: self._validate(values)
            for thread_id, values in raw.items()
            if isinstance(thread_id, str)
        }
        with self._lock:
            self._snapshots[id(result)] = copy.deepcopy(raw)
        return result

    def list_queues(self) -> dict[str, list[QueuedTurn]]:
        """Read canonical queues without registering mutable save snapshots."""
        self._ensure_records()
        return {
            thread_id: self._validate(values)
            for thread_id, values in self.store.record_items(self.namespace).items()
            if isinstance(thread_id, str)
        }

    def get(self, thread_id: str) -> list[QueuedTurn]:
        self._ensure_records()
        return self._validate(
            self.store.record_get(self.namespace, str(thread_id))
        )

    def update(
        self,
        thread_id: str,
        updater,
    ) -> list[QueuedTurn]:
        self._ensure_records()
        key = str(thread_id)

        def apply(raw: Any):
            current = self._validate(raw)
            updated = list(updater(current))
            if not updated:
                return None
            return [
                item.model_dump(mode="json")
                for item in updated
            ]

        raw = self.store.record_update(
            self.namespace,
            key,
            apply,
            default=[],
        )
        return self._validate(raw)

    def update_with_ingress(self, thread_id, ingress_id, updater):
        """Commit the exact thread row and existing ingress document together.

        Conversation state remains one document; only queue access is bounded
        to a single thread. Unrelated queue rows are not read or rewritten.
        """
        from codex_web.storage.conversation_channels import ConversationChannelStore
        from codex_web.conversation_channels import ConversationChannelState, ConversationProjectionOutcome
        self._ensure_records()
        conversations = ConversationChannelStore(self.store)
        def apply(documents):
            state = conversations._decode(documents[conversations.namespace][None])
            receipt = state.receipts.get(ingress_id)
            if receipt is None or receipt.outcome != ConversationProjectionOutcome.ROUTING:
                raise ValueError("ingress has no exclusive routing claim")
            updated = list(updater(self._validate(documents[self.namespace][thread_id])))
            admitted = [item for item in updated if item.canonical_ingress_id == ingress_id]
            if len(admitted) != 1 or admitted[0].project_id != receipt.project_id:
                raise ValueError("canonical ingress queue admission scope mismatch")
            item = admitted[0]
            state.receipts[ingress_id] = receipt.model_copy(update={
                "outcome": ConversationProjectionOutcome.QUEUE_ADMITTED,
                "thread_id": thread_id, "queued_id": item.id,
                "routing_result": {"ok": True, "queued": True, "threadId": thread_id, "queuedId": item.id},
                "reason": "canonical queue admission committed; ingress projection pending",
            })
            return {self.namespace: {thread_id: [row.model_dump(mode="json") for row in updated]},
                conversations.namespace: {None: state.model_dump(mode="json")}}
        result = self.store.record_mutate_many({self.namespace: (thread_id,),
            conversations.namespace: (None,)}, apply)
        return self._validate(result[self.namespace].get(thread_id))

    def put(self, thread_id: str, items: list[QueuedTurn]) -> None:
        self._ensure_records()
        key = str(thread_id)
        if not items:
            self.store.record_apply(
                self.namespace,
                upserts={},
                deletes=(key,),
            )
            return
        self.store.record_apply(
            self.namespace,
            upserts={
                key: [item.model_dump(mode="json") for item in items]
            },
        )

    def delete(self, thread_id: str) -> bool:
        self._ensure_records()
        key = str(thread_id)
        existed = self.store.record_get(self.namespace, key) is not None
        if existed:
            self.store.record_apply(
                self.namespace,
                upserts={},
                deletes=(key,),
            )
        return existed

    def remove_exact(
        self,
        thread_id: str,
        expected: QueuedTurn,
    ) -> str:
        """Remove one byte-for-byte-equivalent queue item, preserving order.

        Returns ``removed``, ``missing`` or ``changed``.  The comparison and
        removal share the keyed queue transaction, so a concurrent mutation
        cannot be mistaken for the administratively inspected request.
        """
        outcome = "missing"

        def update(current: list[QueuedTurn]) -> list[QueuedTurn]:
            nonlocal outcome
            matches = [item for item in current if item.id == expected.id]
            if not matches:
                outcome = "missing"
                return current
            if len(matches) != 1 or matches[0] != expected:
                outcome = "changed"
                return current
            outcome = "removed"
            removed = False
            retained: list[QueuedTurn] = []
            for item in current:
                if not removed and item.id == expected.id:
                    removed = True
                    continue
                retained.append(item)
            return retained

        self.update(thread_id, update)
        return outcome

    def remove_many_exact(self, thread_id: str, expected: tuple[QueuedTurn, ...], *, guard) -> str:
        """All-or-none scheduling removal under the existing keyed transaction."""
        if not 2 <= len(expected) <= 16 or len({item.id for item in expected}) != len(expected):
            raise ValueError("invalid notification batch members")
        if any(item.thread_id != thread_id for item in expected):
            raise ValueError("notification batch queue scope differs")
        outcome = "missing"
        def remove(current):
            nonlocal outcome
            if not guard():
                raise ValueError("notification batch authority/ownership changed")
            for item in expected:
                matches = [value for value in current if value.id == item.id]
                if not matches:
                    return current
                if len(matches) != 1 or matches[0] != item:
                    outcome = "changed"
                    return current
            outcome = "removed"
            ids = {item.id for item in expected}
            return [item for item in current if item.id not in ids]
        self.update(thread_id, remove)
        return outcome

    def restore_many_exact(self, thread_id: str, originals: tuple[QueuedTurn, ...], *, order, guard,
                           priority_ids=frozenset()):
        """Restore missing originals without overwriting peers or changed copies."""
        if (not 2 <= len(originals) <= 16 or len(set(order)) != len(order)
                or len({item.id for item in originals}) != len(originals)
                or any(item.thread_id != thread_id or item.id not in order for item in originals)):
            raise ValueError("invalid notification batch restoration")
        def restore(current):
            if not guard():
                raise ValueError("notification batch authority/ownership changed")
            for original in originals:
                matches = [item for item in current if item.id == original.id]
                if matches and (len(matches) != 1 or matches[0] != original):
                    raise ValueError("notification batch queue copy changed")
            for original in sorted(originals, key=lambda item:order.index(item.id)):
                if any(item.id == original.id for item in current):
                    continue
                position = order.index(original.id)
                before, after = set(order[:position]), set(order[position+1:])
                predecessor = max((index for index,item in enumerate(current) if item.id in before), default=-1)
                successor = next((index for index,item in enumerate(current) if item.id in after), None)
                index = predecessor + 1 if predecessor >= 0 else successor
                if index is None:
                    index = max((index for index,item in enumerate(current) if item.id in priority_ids), default=-1) + 1
                current.insert(index, original.model_copy(deep=True))
            return current
        return self.update(thread_id, restore)

    def remove_present_originals_exact(self, thread_id, originals, *, guard):
        """Remove only exact surviving copies during reserved route recovery."""
        def remove(current):
            if not guard():
                raise ValueError('notification recovery ownership changed')
            expected={item.id:item for item in originals}
            for item_id,item in expected.items():
                copies=[value for value in current if value.id==item_id]
                if copies and (len(copies)!=1 or copies[0]!=item):
                    raise ValueError('notification recovery queue copy changed')
            return [item for item in current if item.id not in expected]
        return self.update(thread_id,remove)

    def flush_legacy_mirror(self) -> None:
        started = __import__("time").perf_counter()
        success = False
        try:
            self._ensure_records()
            atomic_write_text(
                self.legacy_path,
                json.dumps(
                    self.store.record_items(self.namespace),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                private=True,
            )
            success = True
        finally:
            self._mirror_metrics.observe(
                __import__("time").perf_counter() - started,
                success=success,
            )

    def compatibility_metrics(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "legacyPath": str(self.legacy_path),
            "checkpoint": self._mirror_metrics.snapshot(),
        }

    @staticmethod
    def _merge_queue(
        base_items: list[Any],
        desired_items: list[Any],
        latest_items: list[Any],
    ) -> list[Any]:
        base_by_id = {
            str(item.get("id")): item
            for item in base_items
            if isinstance(item, dict) and item.get("id")
        }
        desired_by_id = {
            str(item.get("id")): item
            for item in desired_items
            if isinstance(item, dict) and item.get("id")
        }
        changed_by_id = {
            item_id: item
            for item_id, item in desired_by_id.items()
            if base_by_id.get(item_id) != item
        }
        deleted_ids = set(base_by_id) - set(desired_by_id)
        result: list[Any] = []
        seen_ids: set[str] = set()
        for item in latest_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id or item_id in deleted_ids:
                continue
            result.append(changed_by_id.get(item_id, item))
            seen_ids.add(item_id)
        for item in desired_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if (
                item_id
                and item_id in changed_by_id
                and item_id not in seen_ids
            ):
                result.append(changed_by_id[item_id])
                seen_ids.add(item_id)

        deduped: list[Any] = []
        seen_semantics: set[tuple[str, str]] = set()
        for item in result:
            if item.get("provider_notification_pin") is not None or item.get("owner_routing_pin") is not None:
                # Distinct canonical provider events remain distinct originals,
                # even when their projected message text happens to match.
                deduped.append(item)
                continue
            semantic = (
                str(item.get("source") or ""),
                str(item.get("message") or ""),
            )
            if semantic in seen_semantics:
                continue
            seen_semantics.add(semantic)
            deduped.append(item)
        return deduped

    def save(self, values: dict[str, list[QueuedTurn]]) -> None:
        payload = {
            str(thread_id): [
                item.model_dump(mode="json") for item in items
            ]
            for thread_id, items in sorted(values.items())
            if items
        }
        with self._lock:
            base = self._snapshots.pop(id(values), None)
        self._ensure_records()
        if base is None:
            current = self.store.record_items(self.namespace)
            base = copy.deepcopy(current)

        changed_keys = {
            key
            for key in set(base) | set(payload)
            if base.get(key) != payload.get(key)
        }
        for key in sorted(changed_keys):
            base_items = base.get(key)
            desired_items = payload.get(key)
            if not isinstance(base_items, list):
                base_items = []
            if not isinstance(desired_items, list):
                desired_items = []

            def merge(latest: Any, *, _base=base_items, _desired=desired_items):
                latest_items = latest if isinstance(latest, list) else []
                merged = self._merge_queue(
                    _base,
                    _desired,
                    latest_items,
                )
                return merged or None

            self.store.record_update(
                self.namespace,
                key,
                merge,
                default=[],
            )
        self.flush_legacy_mirror()
