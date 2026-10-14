from __future__ import annotations

from typing import Any, Callable
import hashlib
import json

from codex_web.models import (
    ExecutionPreflightAttempt,
    ExecutionPreflightBlocker,
    ExecutionPreflightBlockerSnapshot,
)
from codex_web.storage.state_store import StateStore


class ExecutionPreflightStore:
    """Keyed durable store for blocked/retried execution attempts.

    Attempt identity is authoritative under an `a/` key. A duplicate
    thread/time key lives in the same record collection so one record_apply()
    updates both views atomically.
    """

    NAMESPACE = "execution_preflight_attempts"
    MAX_TIMESTAMP_MICROS = 9_999_999_999_999_999

    def __init__(self, store: StateStore) -> None:
        self.store = store
        # Queue duplicate checks read this collection inside the queue's atomic
        # update. Materialize its keyed layout before that transaction: lazy
        # initialization would need a second SQLite writer and deadlock it.
        self.store.record_page(self.NAMESPACE, limit=1)

    @classmethod
    def _inverse_timestamp(cls, value: float) -> int:
        micros = max(0, int(float(value) * 1_000_000))
        return cls.MAX_TIMESTAMP_MICROS - min(
            micros,
            cls.MAX_TIMESTAMP_MICROS,
        )

    @staticmethod
    def _attempt_key(attempt_id: str) -> str:
        return f"a/{attempt_id}"

    @staticmethod
    def queued_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {key:value for key,value in snapshot.items()
            if key not in {'id','execution_id','created_at','attempts'}}

    @classmethod
    def _queued_identity_key(cls, snapshot: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(cls.queued_identity(snapshot), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        return f"q/{snapshot['thread_id']}/{digest}/"

    def matching_queued(self, snapshot: dict[str, Any]) -> ExecutionPreflightAttempt | None:
        cursor = None
        while True:
            rows, cursor = self.store.record_page(self.NAMESPACE,
                key_prefix=self._queued_identity_key(snapshot), after=cursor, limit=200)
            for row in rows.values():
                attempt = self.get(row['attemptId'])
                if attempt is not None and attempt.status != 'started':
                    return attempt
            if cursor is None:
                return None

    @classmethod
    def _thread_key(cls, attempt: ExecutionPreflightAttempt) -> str:
        return (
            f"t/{attempt.thread_id}/"
            f"{cls._inverse_timestamp(attempt.created_at):016d}/"
            f"{attempt.id}"
        )

    def get(self, attempt_id: str) -> ExecutionPreflightAttempt | None:
        payload = self.store.record_get(
            self.NAMESPACE,
            self._attempt_key(attempt_id),
        )
        return (
            ExecutionPreflightAttempt.model_validate(payload)
            if isinstance(payload, dict)
            else None
        )

    def put(self, attempt: ExecutionPreflightAttempt) -> None:
        payload = attempt.model_dump(mode="json")
        key = self._attempt_key(attempt.id)
        def write(values):
            if values[key] is not None:
                current = ExecutionPreflightAttempt.model_validate(values[key])
                if current.notification_batch_id is not None and current != attempt:
                    raise ValueError("notification batch member requires batch-scoped recovery")
            return {
                self._attempt_key(attempt.id): payload,
                self._thread_key(attempt): {
                    "attemptId": attempt.id,
                },
            }
        self.store.record_mutate(self.NAMESPACE, (key,), write)

    def get_notification_batch(self, batch_id: str):
        from codex_web.notification_batches import NotificationBatchAdmission
        value = self.store.record_get(self.NAMESPACE, "b/" + batch_id)
        return NotificationBatchAdmission.model_validate(value) if value is not None else None

    def claim_notification_batch(self, plan, *, claim_id: str, now: float, queue_order=()):
        """All member claims and the descriptor commit in one keyed transaction.

        This is retention/ownership only. No queue is removed and no native
        outcome or permission is inferred. Caller must recheck authority/fencing.
        """
        from codex_web.notification_batches import NotificationBatchAdmission, NotificationBatchPlan, _eligible
        from codex_web.services.turn_queue_policy import TurnQueuePolicy
        plan = NotificationBatchPlan.model_validate(plan.model_dump(mode="python"))
        admission = NotificationBatchAdmission(id=NotificationBatchAdmission.identity(plan.id, claim_id),
                                              plan=plan, claim_id=claim_id, claimed_at=now, queue_order=queue_order)
        if queue_order and (len(set(queue_order)) != len(queue_order)
                            or any(item.id not in queue_order for item in plan.members)):
            raise ValueError("notification batch original order is invalid")
        batch_key = "b/" + admission.id
        keys = tuple(self._attempt_key(value) for value in admission.attempt_ids)
        selected = (*keys, batch_key)
        outcome = None

        def claim(values):
            nonlocal outcome
            previous = values[batch_key]
            if previous is not None:
                existing = NotificationBatchAdmission.model_validate(previous)
                if (existing.claim_id != claim_id or existing.plan != plan or existing.queue_order != admission.queue_order
                        or existing.phase != "prepared"):
                    raise ValueError("notification batch is already claimed or requires reconciliation")
                for key, member, original in zip(keys, plan.members, plan.original_snapshots_json, strict=True):
                    current = ExecutionPreflightAttempt.model_validate(values[key])
                    if (current.notification_batch_id != admission.id or current.status != "admitting"
                            or current.queued_admission_claim_id != claim_id):
                        raise ValueError("notification batch member claim changed")
                    checked = current.model_copy(update={"status":"queued", "notification_batch_id":None})
                    if _eligible(member, checked, organization_id=plan.organization_id, workspace_id=plan.workspace_id,
                                 notification_source_origin=TurnQueuePolicy.source_origin(member.source)) != original:
                        raise ValueError("notification batch original changed")
                outcome = existing
                return {}
            changes = {}
            for key, member, original in zip(keys, plan.members, plan.original_snapshots_json, strict=True):
                if values[key] is None:
                    raise ValueError("notification batch original is not retained")
                current = ExecutionPreflightAttempt.model_validate(values[key])
                snapshot = _eligible(member, current, organization_id=plan.organization_id,
                    workspace_id=plan.workspace_id, notification_source_origin=TurnQueuePolicy.source_origin(member.source))
                if snapshot != original:
                    raise ValueError("notification batch original changed or is not eligible")
                changes[key] = current.model_copy(update={"status": "admitting",
                    "notification_batch_id": admission.id, "queued_admission_claim_id": claim_id,
                    "updated_at": now}).model_dump(mode="json")
            changes[batch_key] = admission.model_dump(mode="json")
            outcome = admission
            return changes

        self.store.record_mutate(self.NAMESPACE, selected, claim)
        return outcome

    def transition_notification_batch(self, batch_id: str, *, claim_id: str, phase: str):
        """Fence all members before native I/O, or release only a prepared claim."""
        from codex_web.notification_batches import NotificationBatchAdmission, _eligible
        from codex_web.services.turn_queue_policy import TurnQueuePolicy
        observed = self.get_notification_batch(batch_id)
        if observed is None:
            raise KeyError(batch_id)
        batch_key = "b/" + batch_id
        keys = tuple(self._attempt_key(value) for value in observed.attempt_ids)
        result = None

        def change(values):
            nonlocal result
            current = NotificationBatchAdmission.model_validate(values[batch_key])
            if current != observed or current.claim_id != claim_id:
                raise ValueError("notification batch claim changed")
            if phase not in {"native_outcome_unknown", "released"} or current.phase != "prepared":
                raise ValueError("notification batch needs authoritative native reconciliation")
            changes = {}
            for key, queued, original in zip(keys, current.plan.members, current.plan.original_snapshots_json, strict=True):
                member = ExecutionPreflightAttempt.model_validate(values[key])
                if (member.notification_batch_id != batch_id or member.status != "admitting"
                        or member.queued_admission_claim_id != claim_id or member.queued_start_receipt is not None):
                    raise ValueError("notification batch member was fenced or changed")
                checked = member.model_copy(update={"status":"queued", "notification_batch_id":None})
                if _eligible(queued, checked, organization_id=current.plan.organization_id,
                             workspace_id=current.plan.workspace_id,
                             notification_source_origin=TurnQueuePolicy.source_origin(queued.source)) != original:
                    raise ValueError("notification batch original changed")
                updates = {"status": "reconciliation_required" if phase == "native_outcome_unknown" else "queued"}
                if phase == "released":
                    updates.update(notification_batch_id=None, queued_admission_claim_id=None)
                changes[key] = member.model_copy(update=updates).model_dump(mode="json")
            result = current.model_copy(update={"phase": phase})
            changes[batch_key] = result.model_dump(mode="json")
            return changes

        self.store.record_mutate(self.NAMESPACE, (*keys, batch_key), change)
        return result

    def record_notification_native_start(self, batch_id, receipt):
        from codex_web.notification_batches import NotificationBatchAdmission, _eligible
        from codex_web.models import QueuedNativeStartReceipt
        from codex_web.services.turn_queue_policy import TurnQueuePolicy
        receipt = QueuedNativeStartReceipt.model_validate(receipt.model_dump(mode="python"))
        observed = self.get_notification_batch(batch_id)
        if observed is None:
            raise KeyError(batch_id)
        batch_key = "b/" + batch_id
        keys = tuple(self._attempt_key(value) for value in observed.attempt_ids)
        result = None
        def record(values):
            nonlocal result
            current = NotificationBatchAdmission.model_validate(values[batch_key])
            if current.native_receipt is not None:
                if current.native_receipt != receipt:
                    raise ValueError("notification native receipt changed")
                result = current
                return {}
            if current != observed or current.phase != "native_outcome_unknown":
                raise ValueError("notification native start was not durably fenced")
            result = NotificationBatchAdmission.model_validate({**current.model_dump(mode="python"),
                "phase":"native_input_started", "native_receipt":receipt})
            changes = {batch_key:result.model_dump(mode="json")}
            for key, queued, original in zip(keys,current.plan.members,current.plan.original_snapshots_json,strict=True):
                member = ExecutionPreflightAttempt.model_validate(values[key])
                if (member.notification_batch_id != batch_id or member.status != "reconciliation_required"
                        or member.queued_admission_claim_id != current.claim_id or member.queued_start_receipt is not None):
                    raise ValueError("notification batch member was fenced or changed")
                checked=member.model_copy(update={"status":"queued", "notification_batch_id":None})
                if _eligible(queued,checked,organization_id=current.plan.organization_id,
                             workspace_id=current.plan.workspace_id,
                             notification_source_origin=TurnQueuePolicy.source_origin(queued.source)) != original:
                    raise ValueError("notification batch original changed")
                # Input delivery is known; per-original completion remains held.
                changes[key]=member.model_copy(update={"updated_at":receipt.observed_at}).model_dump(mode="json")
            return changes
        self.store.record_mutate(self.NAMESPACE,(*keys,batch_key),record)
        return result

    def consume_notification_native_start(self, batch_id, *, claim_id, requested_execution_id, thread_id, message):
        observed = self.get_notification_batch(batch_id)
        if (observed is None or observed.claim_id != claim_id or observed.id != requested_execution_id
                or observed.plan.members[0].thread_id != thread_id or observed.native_input() != message):
            raise ValueError("notification native submission does not match retained admission")
        return self.transition_notification_batch(batch_id,claim_id=claim_id,phase="native_outcome_unknown")

    def retain_queued(self, attempt: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
        """Commit the full request before queue removal; retries preserve its state."""
        def retain(raw):
            if raw is not None:
                current = ExecutionPreflightAttempt.model_validate(raw)
                expected = current.queued_turn_snapshot
                if expected is not None and current.replacement_thread_id:
                    expected = dict(expected)
                    original_thread = expected.get('thread_id')
                    expected['thread_id'] = current.replacement_thread_id
                    reply = expected.get('reply_target')
                    if reply and reply.get('thread_id') == original_thread:
                        expected['reply_target'] = dict(reply, thread_id=current.replacement_thread_id)
                if (
                    current.organization_id != attempt.organization_id
                    or current.workspace_id != attempt.workspace_id
                    or current.project_id != attempt.project_id
                    or expected != attempt.queued_turn_snapshot
                ):
                    raise ValueError("retained queued request identity changed")
                return raw
            return attempt.model_dump(mode="json")

        raw = self.store.record_update(self.NAMESPACE, self._attempt_key(attempt.id),
            retain, default=None)
        retained = ExecutionPreflightAttempt.model_validate(raw)
        # Both writes complete before the runtime may remove the queue entry.
        # A crash between them leaves the original queue intact; calling this
        # again repairs the existing domain's inspection index idempotently.
        self.store.record_apply(self.NAMESPACE, upserts={
            self._thread_key(retained): {"attemptId":retained.id},
            self._queued_identity_key(attempt.queued_turn_snapshot)+retained.id:
                {"attemptId":retained.id}})
        return retained

    def update(
        self,
        attempt_id: str,
        updater: Callable[
            [ExecutionPreflightAttempt],
            ExecutionPreflightAttempt,
        ],
    ) -> ExecutionPreflightAttempt:
        key = self._attempt_key(attempt_id)

        def update_raw(payload: Any) -> dict[str, Any]:
            if not isinstance(payload, dict):
                raise KeyError(attempt_id)
            current = ExecutionPreflightAttempt.model_validate(payload)
            if current.notification_batch_id is not None:
                raise ValueError("notification batch member requires batch-scoped recovery")
            updated = updater(current.model_copy(deep=True))
            if (
                updated.id != current.id
                or updated.thread_id != current.thread_id
            ):
                raise ValueError(
                    "execution preflight attempt identity is immutable"
                )
            return updated.model_dump(mode="json")

        payload = self.store.record_update(
            self.NAMESPACE,
            key,
            update_raw,
            default=None,
        )
        updated = ExecutionPreflightAttempt.model_validate(payload)
        if updated.replacement_thread_id:
            target = updated.model_copy(update={"thread_id": updated.replacement_thread_id})
            indexes = {
                self._thread_key(target): {"attemptId": updated.id},
            }
            if updated.queued_turn_snapshot is not None:
                projected = dict(updated.queued_turn_snapshot, thread_id=updated.replacement_thread_id)
                reply = projected.get('reply_target')
                if reply and reply.get('thread_id') == updated.thread_id:
                    projected['reply_target'] = dict(reply, thread_id=updated.replacement_thread_id)
                indexes[self._queued_identity_key(projected)+updated.id] = {'attemptId':updated.id}
            self.store.record_apply(self.NAMESPACE, upserts=indexes)
        return updated


    def for_thread(
        self,
        thread_id: str,
        *,
        limit: int = 50,
    ) -> list[ExecutionPreflightAttempt]:
        page_size = max(1, min(int(limit), 200))
        prefix = f"t/{thread_id}/"
        rows, _cursor = self.store.record_page(
            self.NAMESPACE,
            key_prefix=prefix,
            limit=page_size,
        )
        result: list[ExecutionPreflightAttempt] = []
        for payload in rows.values():
            if not isinstance(payload, dict):
                continue
            attempt_id = str(payload.get("attemptId") or "")
            if not attempt_id:
                continue
            attempt = self.get(attempt_id)
            if attempt is not None and thread_id in {attempt.thread_id, attempt.replacement_thread_id}:
                result.append(attempt)
        return result

    def claim_retry(
        self,
        attempt_id: str,
        *,
        claim_id: str,
        now: float,
        stale_after_seconds: float = 60.0,
    ) -> tuple[ExecutionPreflightAttempt, bool]:
        key = self._attempt_key(attempt_id)
        acquired = False

        def mutate(payload):
            nonlocal acquired
            acquired = False
            if not isinstance(payload, dict):
                raise KeyError(attempt_id)
            current = ExecutionPreflightAttempt.model_validate(payload)
            if current.bootstrap_recovery is not None and current.bootstrap_recovery.state != 'created':
                return current.model_dump(mode="json")
            if current.notification_batch_id is not None:
                raise ValueError("notification batch member requires batch-scoped recovery")
            if current.status in {"queued", "reconciliation_required"}:
                return current.model_dump(mode="json")
            if current.status == "started":
                return current.model_dump(mode="json")
            if current.status == "retrying":
                if current.retry_claim_id == claim_id:
                    acquired = True
                    return current.model_dump(mode="json")
                retry_started_at = current.retry_started_at or current.updated_at
                if now - retry_started_at < max(1.0, stale_after_seconds):
                    return current.model_dump(mode="json")
                # Retained queued recovery never calls the native runtime in
                # retrying state. Only a later queued -> admitting -> unknown
                # claim can pass the native gate, so interrupted queue recovery
                # may acquire a new token without inferring a start outcome.
            acquired = True
            return current.model_copy(
                update={
                    "status": "retrying",
                    "attempt_number": current.attempt_number + 1,
                    "retry_claim_id": claim_id,
                    "retry_started_at": now,
                    "last_error": None,
                    **({'queued_admission_claim_id':None}
                       if current.queued_turn_snapshot is not None else {}),
                    "updated_at": now,
                }
            ).model_dump(mode="json")

        payload = self.store.record_update(
            self.NAMESPACE,
            key,
            mutate,
            default=None,
        )
        attempt = ExecutionPreflightAttempt.model_validate(payload)
        self.store.record_apply(
            self.NAMESPACE,
            upserts={self._thread_key(attempt): {"attemptId":attempt.id}},
        )
        return attempt, acquired

    def mark_blocked(
        self,
        attempt_id: str,
        *,
        blockers,
        now: float,
        last_error: str | None = None,
    ) -> ExecutionPreflightAttempt:
        normalized = tuple(
            item
            if isinstance(item, ExecutionPreflightBlocker)
            else ExecutionPreflightBlocker.model_validate(item)
            for item in blockers
        )

        def apply(current: ExecutionPreflightAttempt):
            snapshot = ExecutionPreflightBlockerSnapshot(
                blockers=normalized,
                recorded_at=now,
                attempt_number=current.attempt_number,
            )
            history = tuple(
                (*current.blocker_history, snapshot)[-20:]
            )
            return current.model_copy(
                update={
                    "status": "blocked",
                    "blockers": normalized,
                    "blocker_history": history,
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": last_error,
                    "updated_at": now,
                }
            )

        return self.update(attempt_id, apply)

    def mark_started(
        self,
        attempt_id: str,
        *,
        now: float,
    ) -> ExecutionPreflightAttempt:
        return self.update(
            attempt_id,
            lambda current: current.model_copy(
                update={
                    "status": "started",
                    "blockers": (),
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "started_at": now,
                    "last_error": None,
                    "updated_at": now,
                }
            ),
        )

    def mark_failed(
        self,
        attempt_id: str,
        *,
        now: float,
        error: str,
    ) -> ExecutionPreflightAttempt:
        return self.update(
            attempt_id,
            lambda current: current.model_copy(
                update={
                    "status": "failed",
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": error,
                    "updated_at": now,
                }
            ),
        )

    def status(self) -> dict[str, Any]:
        return {
            "namespace": self.NAMESPACE,
            "revision": self.store.namespace_revision(self.NAMESPACE),
        }
