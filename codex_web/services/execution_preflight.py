from __future__ import annotations

import time
import uuid
import hashlib
import json
import contextlib
from typing import Any, Callable

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.models import (
    ExecutionPreflightAttempt,
    ExecutionPreflightBlocker,
    ExecutionPreflightBlockerSnapshot,
    TurnCreate,
    QueuedTurn,
    QueuedNativeStartReceipt,
    QueuedSchedulingRetirement,
    QueuedBootstrapRecovery,
    QueuedBootstrapPrestartRefusal,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.execution_preflight import ExecutionPreflightStore


class ExecutionPreflightService:
    """Persist and retry execution attempts blocked before active execution."""

    RETRY_CLAIM_TTL_SECONDS = 30.0
    MAX_HISTORY = 50

    def __init__(
        self,
        store: ExecutionPreflightStore,
        *,
        clock=time.time,
        replacement_resolver: Callable[..., str | None] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.replacement_resolver = replacement_resolver
        self.event_sink = event_sink
        self.queued_start_observer = None

    def observe_queued_start(self, attempt):
        """Project a committed native fact; metadata failure never permits replay."""
        if self.queued_start_observer is None:
            return
        try:
            self.queued_start_observer(attempt)
        except Exception:
            if self.event_sink is not None:
                with contextlib.suppress(Exception):
                    self.event_sink({'type': 'queued_native_start_projection_failed', 'attempt_id': attempt.id})

    @staticmethod
    def _attempt_id(execution_id: str) -> str:
        return f"preflight-{execution_id}"

    def retain_queued(
        self, queued: QueuedTurn, *, organization_id: str, workspace_id: str,
    ) -> ExecutionPreflightAttempt:
        """Retain admission intent, without claiming a native start or delivery."""
        correlation = queued.execution_id or f"reserved-queue-{queued.id}"
        now = float(self.clock())
        attempt = ExecutionPreflightAttempt(
            id=f'preflight-queued-{queued.id}', correlation_id=correlation,
            execution_id=correlation, organization_id=organization_id,
            workspace_id=workspace_id, thread_id=queued.thread_id,
            project_id=queued.project_id, message=queued.message,
            queued_turn_snapshot=queued.model_dump(mode="json"),
            sandbox=queued.sandbox, approval_policy=queued.approval_policy,
            model=queued.model, reasoning_effort=queued.reasoning_effort,
            repository_resource_id=queued.repository_resource_id,
            writable_repository_resource_ids=queued.writable_repository_resource_ids,
            read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
            **({"resource_context":queued.resource_context} if queued.resource_context else {}),
            execution_profile_id=queued.execution_profile_id,
            agent_profile_id=queued.agent_profile_id,
            agent_profile_revision=queued.agent_profile_revision,
            agent_profile_actor_id=queued.agent_profile_actor_id,
            source=queued.source, status="queued", created_at=now, updated_at=now,
        )
        current = self.store.get(attempt.id)
        if current is not None and (
            current.replacement_thread_id or current.thread_id
        ) != queued.thread_id:
            self._restore_queued_route(current, attempt)
        return self.store.retain_queued(attempt)

    def _restore_queued_route(
        self, current: ExecutionPreflightAttempt, incoming: ExecutionPreflightAttempt,
    ) -> None:
        """Repair scheduling provenance; never infer or retry a native outcome."""
        if (current.organization_id != incoming.organization_id
                or current.workspace_id != incoming.workspace_id
                or current.project_id != incoming.project_id
                or self.replacement_resolver is None):
            raise ValueError("retained queued request identity changed")
        target = self.replacement_resolver(
            current.thread_id, project_id=current.project_id,
            organization_id=current.organization_id, workspace_id=current.workspace_id,
        )
        if target != incoming.thread_id:
            raise ValueError("retained queued request replacement is unverified")
        prior_target = current.replacement_thread_id or current.thread_id
        changed = False
        def update(value: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            nonlocal changed
            changed = False
            expected = dict(value.queued_turn_snapshot or {})
            original = expected.get("thread_id")
            expected["thread_id"] = target
            reply = expected.get("reply_target")
            if reply and reply.get("thread_id") == original:
                expected["reply_target"] = dict(reply, thread_id=target)
            if (value.organization_id != incoming.organization_id
                    or value.workspace_id != incoming.workspace_id
                    or value.project_id != incoming.project_id
                    or value.queued_turn_snapshot is None
                    or not self.store.queued_snapshots_equal(expected, incoming.queued_turn_snapshot)
                    or (value.replacement_thread_id or value.thread_id)
                       not in {prior_target, target}
                    or value.status in {"admitting", "retrying"}):
                raise ValueError("retained queued request identity changed")
            if value.replacement_thread_id == target:
                return value
            changed = True
            return value.model_copy(update={"replacement_thread_id":target,
                                           "updated_at":float(self.clock())})
        self.store.update(current.id, update)
        if changed and self.event_sink is not None:
            self.event_sink({"type":"queued_preflight_replacement_route_restored",
                "attempt_id":current.id, "original_thread_id":current.thread_id,
                "previous_thread_id":prior_target, "replacement_thread_id":target,
                "project_id":current.project_id,
                "native_outcome":current.status})

    def claim_queued_admission(self, attempt_id: str) -> tuple[ExecutionPreflightAttempt, str | None]:
        claim_id = f'queued-admission-{uuid.uuid4().hex}'
        acquired = False
        def update(current):
            nonlocal acquired
            acquired = False
            if (current.status != 'queued' or current.queued_turn_snapshot is None
                    or current.scheduling_retirement is not None):
                return current
            acquired = True
            return current.model_copy(update={'status':'admitting',
                'queued_admission_claim_id':claim_id, 'updated_at':float(self.clock())})
        updated = self.store.update(attempt_id, update)
        return updated, claim_id if acquired else None

    def begin_queued_start(self, attempt_id: str, *, claim_id: str) -> bool:
        acquired = False
        def update(current):
            nonlocal acquired
            acquired = False
            if (current.status != 'admitting'
                    or current.queued_admission_claim_id != claim_id
                    or current.scheduling_retirement is not None):
                return current
            acquired = True
            return current.model_copy(update={'status':'reconciliation_required',
                'blockers':(), 'last_error':None, 'updated_at':float(self.clock())})
        self.store.update(attempt_id, update)
        return acquired

    def record_queued_native_start(
        self, attempt_id: str, receipt: QueuedNativeStartReceipt,
    ) -> ExecutionPreflightAttempt:
        """Only the native runtime response may supply this internal receipt."""
        def update(current):
            if current.queued_start_correlation is not None:
                # The reader owns exact response attribution. Never downgrade
                # its result or bypass missing/malformed positive evidence.
                from codex_web.services.queued_native_start import validate_attribution
                validate_attribution(self, current, current.queued_start_correlation)
                recorded = current.queued_start_receipt
                if (recorded is None or current.queued_start_evidence is None
                        or recorded.model_dump(exclude={'observed_at'})
                        != receipt.model_dump(exclude={'observed_at'})):
                    raise ValueError('exact native response evidence is unavailable or conflicting')
                return current
            if current.status == 'started' and current.queued_start_receipt == receipt:
                return current
            if (current.queued_turn_snapshot is None
                    or current.status != 'reconciliation_required'
                    or current.queued_admission_claim_id != receipt.admission_claim_id
                    or current.execution_id != receipt.requested_execution_id
                    or receipt.thread_id not in {current.thread_id, current.replacement_thread_id}):
                raise ValueError('native start receipt does not match the retained admission')
            return current.model_copy(update={'status':'started',
                'queued_start_receipt':receipt, 'started_at':receipt.observed_at,
                'updated_at':receipt.observed_at, 'last_error':None, 'blockers':()})
        return self.store.update(attempt_id, update)

    def queued_disposition(self, attempt_id: str, *, status: str,
                           detail: dict[str, Any] | None = None,
                           claim_id: str | None = None,
                           preinput_refusal: bool = False) -> ExecutionPreflightAttempt:
        """Internal runtime outcome; unknown native starts remain non-retryable.

        preinput_refusal is supplied only by the controller's typed local
        pre-write exception boundary, never an HTTP/provider detail code.
        """
        if status not in {'queued', 'blocked', 'started', 'reconciliation_required'}:
            raise ValueError('invalid queued admission disposition')
        now = float(self.clock())
        def update(current):
            if current.queued_turn_snapshot is None:
                raise ValueError('queued disposition requires the original request')
            if current.scheduling_retirement is not None:
                return current
            if current.status == 'started':
                return current
            if claim_id is not None and current.queued_admission_claim_id != claim_id:
                return current
            if (preinput_refusal and claim_id is not None
                    and current.status == 'reconciliation_required' and status in {'queued', 'blocked'}
                    and current.queued_resume_correlation is not None
                    and current.queued_start_correlation is None
                    and current.queued_start_evidence is None and current.queued_start_receipt is None
                    and current.started_at is None and len(current.queued_resume_history) < 8):
                from codex_web.services.queued_native_resume import validate_resume_observation
                from codex_web.models import QueuedNativeResumeObservation
                try:
                    correlation, observed = validate_resume_observation(self, current)
                except ValueError:
                    pass  # Invalid/partial observation cannot release custody.
                else:
                    if observed.outcome == 'resumed':
                        # A local controller witness proves no subsequent input
                        # was sent. Preserve successful resume as history; it is
                        # not itself a nonstart proof or permission to replay.
                        history = QueuedNativeResumeObservation(correlation=correlation,evidence=observed)
                        current = current.model_copy(update={'queued_resume_correlation':None,
                            'queued_resume_evidence':None,
                            'queued_resume_history':(*current.queued_resume_history,history)})
            disposition = 'reconciliation_required' if (current.queued_start_correlation is not None
                or current.queued_resume_correlation is not None) else status
            return current.model_copy(update={
                'status':disposition, 'updated_at':now,
                'started_at':now if disposition == 'started' else None,
                'blockers':self._blockers(detail) if detail else (),
                'last_error':str((detail or {}).get('message') or '') or None,
            })
        return self.store.update(attempt_id, update)

    @staticmethod
    def recorded_profile_refusal(attempt: ExecutionPreflightAttempt) -> bool:
        if (attempt.queued_turn_snapshot is None or attempt.queued_start_receipt is not None
                or attempt.queued_start_correlation is not None
                or attempt.queued_resume_correlation is not None
                or attempt.started_at is not None or not attempt.agent_profile_id
                or attempt.agent_profile_revision is None):
            return False
        snapshot = attempt.queued_turn_snapshot
        if (snapshot.get('agent_profile_id') != attempt.agent_profile_id
                or snapshot.get('agent_profile_revision') != attempt.agent_profile_revision):
            return False
        for blocker in attempt.blockers:
            detail = blocker.model_dump(mode='json')
            effective = detail.get('effectiveAgentProfile')
            if not isinstance(effective, dict):
                continue
            if (detail.get('code') == 'thread_agent_profile_immutable'
                    and detail.get('requestedAgentProfileId') == attempt.agent_profile_id
                    and detail.get('requestedAgentProfileRevision') == attempt.agent_profile_revision
                    and effective.get('profile_id') == attempt.agent_profile_id
                    and type(effective.get('profile_revision')) is int
                    and effective['profile_revision'] >= 1
                    and effective['profile_revision'] != attempt.agent_profile_revision):
                return True
        return False

    @staticmethod
    def recorded_maintenance_refusal(attempt: ExecutionPreflightAttempt) -> bool:
        # These structured controller refusals occur before native turn/start.
        # Queue absence, timeouts and stale active markers are not evidence.
        snapshot = attempt.queued_turn_snapshot
        if (snapshot is None or attempt.queued_start_receipt is not None
                or attempt.queued_start_correlation is not None
                or attempt.queued_resume_correlation is not None
                or attempt.started_at is not None
                or snapshot.get('thread_id') != attempt.thread_id):
            return False
        target = attempt.replacement_thread_id or attempt.thread_id
        if attempt.status not in {'queued', 'blocked'}:
            # Historical unknown starts can retain stale blocker text. Their
            # outcome needs separate authoritative reconciliation evidence.
            return False
        return any(blocker.code in {'upgrade_maintenance', 'upgrade_maintenance_active'}
                   and blocker.model_dump(mode='json').get('threadId') == target
                   for blocker in attempt.blockers)

    def recorded_prestart_refusal(self, attempt: ExecutionPreflightAttempt) -> bool:
        if attempt.queued_resume_evidence is not None:
            from codex_web.services.queued_native_resume import validate_resume_observation
            try:
                _, evidence = validate_resume_observation(self, attempt)
                return (evidence.outcome == 'archived_refusal' and attempt.status == 'blocked'
                    and attempt.queued_start_correlation is None and attempt.queued_start_receipt is None
                    and attempt.queued_start_evidence is None and attempt.started_at is None
                    and attempt.scheduling_retirement is None)
            except ValueError:
                return False
        return self.recorded_profile_refusal(attempt) or self.recorded_maintenance_refusal(attempt)

    def reconcile_prestart_refusal(self, attempt_id: str, *, actor: AuthenticationActor) -> ExecutionPreflightAttempt:
        initial = self.get(attempt_id, actor=actor)
        self._authorize(initial, actor, require_admin=True)
        self._require_individual_recovery(initial)
        if initial.queued_start_evidence is not None:
            if actor.principal_kind == PrincipalKind.HUMAN:
                try:
                    IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
                except RuntimeError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
            # Existing authorized inspector action can finish a crash-interrupted
            # receipt projection; it accepts no caller-selected proof or scope.
            from codex_web.services.queued_native_start import reconcile_captured_start
            try:
                return reconcile_captured_start(self, attempt_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail={
                    'code':'native_start_evidence_conflict',
                    'message':'Stored native start attribution no longer matches the original.'}) from exc
        if initial.queued_resume_evidence is not None:
            if actor.principal_kind == PrincipalKind.HUMAN:
                try:
                    IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
                except RuntimeError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
            from codex_web.services.queued_native_resume import reconcile_captured_resume
            try:
                return reconcile_captured_resume(self, attempt_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail={
                    'code':'native_resume_evidence_conflict',
                    'message':'Stored resume refusal does not prove this retained request unstarted.'}) from exc
        def update(current):
            # The recorded controller refusal occurs before native turn/start.
            # Absence, timeout, active markers and expired claims prove nothing.
            if current.status == 'started':
                return current
            if (current.status not in {'queued', 'reconciliation_required', 'blocked'}
                    or not self.recorded_prestart_refusal(current)
                    or (current.status == 'queued' and not self.recorded_maintenance_refusal(current))):
                raise HTTPException(status_code=409, detail={
                    'code':'prestart_refusal_reconciliation_unavailable',
                    'message':'No matching authoritative pre-start controller refusal is retained.'})
            return current.model_copy(update={'status':'blocked', 'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update)

    def retained_queued(self, queued: QueuedTurn) -> ExecutionPreflightAttempt | None:
        attempt = self.store.get(f'preflight-queued-{queued.id}')
        if attempt is None or attempt.queued_turn_snapshot is None:
            return None
        if attempt.queued_turn_snapshot.get('id') != queued.id:
            raise ValueError('retained queued request identity changed')
        return attempt

    def has_explicit_queue_priority(self, queued: QueuedTurn) -> bool:
        """A canonical retry claim increments this retained intent's attempt number.

        This is ordering provenance, never permission to start or evidence about
        a native outcome. Queue bodies and creation timestamps cannot grant it.
        """
        attempt = self.retained_queued(queued)
        return bool(attempt is not None and attempt.attempt_number > 1
            and attempt.project_id == queued.project_id
            and (attempt.replacement_thread_id or attempt.thread_id) == queued.thread_id)

    def queue_retry_committed(self, attempt_id: str, *, claim_id: str) -> ExecutionPreflightAttempt:
        def update(current):
            if current.status != 'retrying' or current.retry_claim_id != claim_id:
                return current
            return current.model_copy(update={'status':'queued', 'retry_claim_id':None,
                'retry_started_at':None, 'queued_admission_claim_id':None,
                'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update)

    def prepare_scheduling_retirement(
        self,
        attempt_id: str,
        *,
        evidence: QueuedSchedulingRetirement,
        expected_snapshot: dict[str, Any],
    ) -> ExecutionPreflightAttempt:
        """Fence native admission before the exact queue item is removed."""
        def update(current: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            if current.scheduling_retirement is not None:
                if current.scheduling_retirement != evidence:
                    raise HTTPException(status_code=409, detail={
                        "code": "scheduling_retirement_conflict",
                        "message": "Scheduling retirement evidence already changed.",
                    })
                return current
            if (
                current.status != "reconciliation_required"
                or current.queued_turn_snapshot != expected_snapshot
                or current.queued_start_receipt is not None
                or current.queued_start_evidence is not None
                or current.started_at is not None
            ):
                raise HTTPException(status_code=409, detail={
                    "code": "scheduling_retirement_unavailable",
                    "message": "Only an unchanged unknown-start queued request can retire scheduling.",
                })
            return current.model_copy(update={
                "scheduling_retirement": evidence,
                "updated_at": float(self.clock()),
            })
        return self.store.update(attempt_id, update)

    def complete_scheduling_retirement(
        self,
        attempt_id: str,
        *,
        prepared: QueuedSchedulingRetirement,
    ) -> ExecutionPreflightAttempt:
        removed_at = float(self.clock())
        completed = prepared.model_copy(update={
            "state": "retired",
            "queue_removed_at": removed_at,
        })
        def update(current: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            if current.scheduling_retirement == completed:
                return current
            if current.scheduling_retirement != prepared:
                raise HTTPException(status_code=409, detail={
                    "code": "scheduling_retirement_conflict",
                    "message": "Scheduling retirement evidence changed before completion.",
                })
            return current.model_copy(update={
                "scheduling_retirement": completed,
                "updated_at": removed_at,
            })
        return self.store.update(attempt_id, update)

    def mark_scheduling_retirement_audited(
        self,
        attempt_id: str,
        *,
        retired: QueuedSchedulingRetirement,
    ) -> ExecutionPreflightAttempt:
        audited_at = float(self.clock())
        def update(current: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            evidence = current.scheduling_retirement
            if evidence is None or evidence.state != "retired":
                raise HTTPException(status_code=409, detail={
                    "code": "scheduling_retirement_incomplete",
                    "message": "Scheduling retirement is not complete.",
                })
            if evidence.audit_recorded_at is not None:
                return current
            if evidence != retired:
                raise HTTPException(status_code=409, detail={
                    "code": "scheduling_retirement_conflict",
                    "message": "Scheduling retirement evidence changed before audit.",
                })
            return current.model_copy(update={
                "scheduling_retirement": evidence.model_copy(update={
                    "audit_recorded_at": audited_at,
                }),
                "updated_at": audited_at,
            })
        return self.store.update(attempt_id, update)

    @staticmethod
    def _blockers(detail: Any) -> tuple[ExecutionPreflightBlocker, ...]:
        if not isinstance(detail, dict):
            return (
                ExecutionPreflightBlocker(
                    code="execution_preflight_blocked",
                    message=str(detail or "execution preflight blocked"),
                ),
            )
        raw = detail.get("blockers")
        if not isinstance(raw, list):
            raw = [detail]
        blockers: list[ExecutionPreflightBlocker] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = dict(item)
            value.setdefault(
                "code",
                str(detail.get("code") or "execution_preflight_blocked"),
            )
            value.setdefault(
                "message",
                str(
                    detail.get("message")
                    or "execution preflight blocked"
                ),
            )
            blockers.append(
                ExecutionPreflightBlocker.model_validate(value)
            )
        return tuple(blockers) or (
            ExecutionPreflightBlocker(
                code="execution_preflight_blocked",
                message=str(
                    detail.get("message")
                    or "execution preflight blocked"
                ),
            ),
        )

    @staticmethod
    def is_preflight_http_error(exc: Exception) -> bool:
        return (
            isinstance(exc, HTTPException)
            and exc.status_code in {400, 403, 409, 422, 503}
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "execution_preflight_blocked"
        )

    def record_blocked(
        self,
        *,
        actor: AuthenticationActor,
        thread_id: str,
        project_id: str,
        execution_id: str,
        payload: TurnCreate,
        effective: dict[str, Any],
        detail: dict[str, Any],
        source: str = "web",
    ) -> ExecutionPreflightAttempt:
        now = float(self.clock())
        blockers = self._blockers(detail)
        attempt_id = self._attempt_id(execution_id)
        existing = self.store.get(attempt_id)
        if existing is not None and (
            thread_id not in {existing.thread_id, existing.replacement_thread_id}
            or existing.project_id != project_id
            or existing.message != payload.message
            or existing.organization_id != actor.organization_id
            or existing.workspace_id != actor.workspace_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "execution_preflight_identity_conflict",
                    "message": (
                        "execution id is already bound to a different retained "
                        "turn identity"
                    ),
                    "correlationId": execution_id,
                },
            )
        if existing is None:
            attempt = ExecutionPreflightAttempt(
                id=attempt_id,
                correlation_id=execution_id,
                execution_id=execution_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                thread_id=thread_id,
                project_id=project_id,
                message=payload.message,
                sandbox=effective.get("sandbox"),
                approval_policy=effective.get("approval_policy"),
                model=effective.get("model"),
                reasoning_effort=effective.get("reasoning_effort"),
                repository_resource_id=effective.get(
                    "repository_resource_id"
                ),
                writable_repository_resource_ids=tuple(
                    effective.get("writable_repository_resource_ids")
                    or ()
                ),
                read_only_repository_resource_ids=tuple(
                    effective.get("read_only_repository_resource_ids")
                    or ()
                ),
                **({"resource_context":effective["resource_context"]} if effective.get("resource_context") else {}),
                execution_profile_id=effective.get(
                    "execution_profile_id"
                ),
                agent_profile_id=effective.get(
                    "agent_profile_id"
                ),
                agent_profile_revision=effective.get(
                    "agent_profile_revision"
                ),
                agent_profile_actor_id=actor.identity_id,
                source=source,
                status="blocked",
                blockers=blockers,
                blocker_history=(
                    ExecutionPreflightBlockerSnapshot(
                        blockers=blockers,
                        recorded_at=now,
                        attempt_number=1,
                    ),
                ),
                attempt_number=1,
                created_at=now,
                updated_at=now,
                last_error=str(detail.get("message") or ""),
            )
            self.store.put(attempt)
            return attempt

        def update(
            current: ExecutionPreflightAttempt,
        ) -> ExecutionPreflightAttempt:
            history = list(current.blocker_history)
            if not history or history[-1].blockers != blockers:
                history.append(
                    ExecutionPreflightBlockerSnapshot(
                        blockers=blockers,
                        recorded_at=now,
                        attempt_number=current.attempt_number,
                    )
                )
            return current.model_copy(
                update={
                    "status": "blocked",
                    "blockers": blockers,
                    "blocker_history": tuple(
                        history[-self.MAX_HISTORY :]
                    ),
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": str(
                        detail.get("message") or ""
                    ),
                    "updated_at": now,
                }
            )

        return self.store.update(attempt_id, update)

    def _authorize(
        self,
        attempt: ExecutionPreflightAttempt,
        actor: AuthenticationActor,
        *,
        require_admin: bool = False,
    ) -> None:
        if (
            attempt.organization_id != actor.organization_id
            or attempt.workspace_id != actor.workspace_id
        ):
            raise HTTPException(status_code=404, detail="preflight attempt not found")
        if require_admin:
            try:
                IdentityService.require_admin(actor)
            except Exception as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

    def get(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionPreflightAttempt:
        attempt = self.store.get(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="preflight attempt not found")
        self._authorize(attempt, actor)
        return attempt

    def for_thread(
        self,
        thread_id: str,
        *,
        actor: AuthenticationActor,
        limit: int = 50,
    ) -> list[ExecutionPreflightAttempt]:
        return [
            item
            for item in self.store.for_thread(thread_id, limit=limit)
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]

    def authorize_retry(self, attempt_id: str, *, actor: AuthenticationActor) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        self._require_individual_recovery(current)
        if current.bootstrap_recovery is not None and current.bootstrap_recovery.state != 'created':
            raise HTTPException(status_code=409, detail={
                'code':'bootstrap_recovery_pending',
                'message':'Reconcile the fresh bootstrap before retrying the retained request.'})
        return current

    @staticmethod
    def queued_snapshot_digest(snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def reserve_bootstrap_recovery(
        self, attempt_id: str, *, actor: AuthenticationActor,
        expected_snapshot_sha256: str, reason: str,
    ) -> tuple[ExecutionPreflightAttempt, bool]:
        """Commit once before bootstrap I/O; expiration never proves its outcome."""
        initial = self.get(attempt_id, actor=actor)
        self._authorize(initial, actor, require_admin=True)
        self._require_individual_recovery(initial)
        if actor.principal_kind == PrincipalKind.HUMAN:
            try:
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            except RuntimeError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        reason = reason.strip()
        if not reason or len(reason) > 1000:
            raise HTTPException(status_code=422, detail='Bootstrap recovery requires a bounded reason.')
        token = uuid.uuid4().hex
        reserved = False
        def update(current):
            nonlocal reserved
            reserved = False
            self._authorize(current, actor, require_admin=True)
            snapshot = current.queued_turn_snapshot
            if (snapshot is None or self.queued_snapshot_digest(snapshot) != expected_snapshot_sha256):
                raise HTTPException(status_code=409, detail={
                    'code':'bootstrap_recovery_snapshot_changed',
                    'message':'The original queued request no longer matches the recovery preview.'})
            if (current.status != 'blocked' or current.scheduling_retirement is not None
                    or not self.recorded_prestart_refusal(current)):
                raise HTTPException(status_code=409, detail={
                    'code':'bootstrap_recovery_unavailable',
                    'message':'Fresh bootstrap recovery requires a proven unstarted, blocked original request.'})
            if current.bootstrap_recovery is not None:
                if (current.bootstrap_recovery.queued_turn_id != snapshot['id']
                        or current.bootstrap_recovery.queued_snapshot_sha256 != expected_snapshot_sha256):
                    raise HTTPException(status_code=409, detail='Bootstrap recovery identity changed.')
                if current.bootstrap_recovery.state != 'refused':
                    return current
                if len(current.bootstrap_recovery_history) >= 8:
                    raise HTTPException(status_code=409, detail='Fresh bootstrap recovery limit reached.')
            reserved = True
            recovery = QueuedBootstrapRecovery(id='bootstrap-recovery-' + token,
                queued_turn_id=snapshot['id'], queued_snapshot_sha256=expected_snapshot_sha256,
                requested_by=actor.identity_id, requested_at=float(self.clock()), reason=reason,
                bootstrap_id='bootstrap-' + token, execution_id='thread-bootstrap-' + token)
            history = current.bootstrap_recovery_history
            if current.bootstrap_recovery is not None:
                history += (current.bootstrap_recovery,)
            return current.model_copy(update={'bootstrap_recovery':recovery,
                'bootstrap_recovery_history':history,
                'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update), reserved

    def record_bootstrap_refusal(self, attempt_id, *, recovery_id, code, message):
        proof = QueuedBootstrapPrestartRefusal(code=code[:100], message=message[:1000],
            observed_at=float(self.clock()))
        def update(current):
            recovery = current.bootstrap_recovery
            if recovery is None or recovery.id != recovery_id or recovery.state != 'pending':
                raise HTTPException(status_code=409, detail='Fresh bootstrap reservation changed.')
            return current.model_copy(update={'bootstrap_recovery':recovery.model_copy(update={
                'state':'refused', 'prestart_refusal':proof}), 'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update)

    def authorize_scheduling_retirement(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        self._require_individual_recovery(current)
        return current

    def claim_retry(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[ExecutionPreflightAttempt, str | None, bool]:
        current = self.authorize_retry(attempt_id, actor=actor)
        if current.status == "started":
            return current, None, False
        if current.scheduling_retirement is not None:
            raise HTTPException(status_code=409, detail={
                "code": "scheduling_retired",
                "message": "Scheduling was retired; the retained native outcome remains unknown.",
            })

        claim_id = f"retry-{uuid.uuid4().hex}"
        updated, claimed = self.store.claim_retry(
            attempt_id,
            claim_id=claim_id,
            now=float(self.clock()),
            stale_after_seconds=self.RETRY_CLAIM_TTL_SECONDS,
        )
        self._authorize(updated, actor, require_admin=True)
        return updated, updated.retry_claim_id, claimed

    def bind_replacement(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str,
        previous_thread_id: str,
        replacement_thread_id: str,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)

        def update(value: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            if (
                value.status != "retrying"
                or value.retry_claim_id != claim_id
                or (value.replacement_thread_id or value.thread_id) != previous_thread_id
            ):
                raise HTTPException(status_code=409, detail="preflight retry target changed")
            return value.model_copy(update={
                "replacement_thread_id": replacement_thread_id,
                "updated_at": float(self.clock()),
            })

        return self.store.update(attempt_id, update)

    def mark_started(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        now = float(self.clock())

        def update(
            value: ExecutionPreflightAttempt,
        ) -> ExecutionPreflightAttempt:
            if value.status == "started":
                return value
            if value.scheduling_retirement is not None:
                return value
            if claim_id and value.retry_claim_id != claim_id:
                return value
            return value.model_copy(
                update={
                    "status": "started",
                    "blockers": (),
                    "started_at": now,
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": None,
                    "updated_at": now,
                }
            )

        return self.store.update(attempt_id, update)

    def get_for_execution(
        self,
        execution_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionPreflightAttempt | None:
        attempt = self.store.get(self._attempt_id(execution_id))
        if attempt is None:
            return None
        self._authorize(attempt, actor)
        return attempt

    def mark_started_for_execution(
        self,
        execution_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
    ) -> ExecutionPreflightAttempt | None:
        attempt = self.get_for_execution(execution_id, actor=actor)
        if attempt is None:
            return None
        return self.mark_started(
            attempt.id,
            actor=actor,
            claim_id=claim_id,
        )

    def mark_failed(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
        error: Exception,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        if claim_id and current.retry_claim_id not in {None, claim_id}:
            return current
        return self.store.mark_failed(
            attempt_id,
            now=float(self.clock()),
            error=str(error)[:1000],
        )

    def hold_renewal_retry(self, attempt_id, *, actor, claim_id, error):
        """Keep the same original when resumed delivery did not settle.

        A controller-owned prestart refusal remains blocked. Other failures,
        including cancellation, require reconciliation; a stale retry TTL is
        never evidence that native input or resume did not happen.
        """
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        known = isinstance(error, ExecutionPrestartRefusal)
        detail = error.detail if known and isinstance(error.detail, dict) else {}
        message = ('Native admission refused before input; inspect its blocker before retrying.' if known
            else 'Native resume or input outcome is unknown. Reconcile the current native thread before retrying this original.')
        message = detail.get('message') or message
        blocker = ExecutionPreflightBlocker(code=detail.get('code') or ('execution_prestart_refused' if known else 'native_start_outcome_unknown'),
            message=message, retryable=known, target_type='thread', target_id=current.thread_id,
            remediation_route=detail.get('remediation_route') or f'/api/threads/{current.thread_id}/preflight-attempts')
        def update(value):
            if value.status != 'retrying' or value.retry_claim_id != claim_id:
                return value
            return value.model_copy(update={'status': 'blocked' if known else 'reconciliation_required',
                'blockers': (blocker,), 'last_error': message, 'updated_at': float(self.clock())})
        return self.store.update(attempt_id, update)

    @staticmethod
    def can_retry(actor: AuthenticationActor) -> bool:
        try:
            IdentityService.require_admin(actor)
        except Exception:
            return False
        return True

    @staticmethod
    def _require_individual_recovery(attempt):
        if attempt.notification_batch_id is not None:
            raise HTTPException(status_code=409, detail={
                'code':'notification_batch_reconciliation_required',
                'message':'This original belongs to a batch. Inspect and reconcile the shared admission before individual recovery.',
                'batch_id':attempt.notification_batch_id})

    def public(
        self,
        attempt: ExecutionPreflightAttempt,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._authorize(attempt, actor)
        payload = attempt.model_dump(mode="json")
        payload['can_admit_original'] = (self.can_retry(actor) and attempt.status == 'queued'
            and attempt.queued_turn_snapshot is not None and attempt.queued_start_receipt is None
            and attempt.started_at is None and attempt.scheduling_retirement is None
            and attempt.notification_batch_id is None)
        payload['operator_admission_href'] = (
            f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/'
            f'{attempt.id}/operator-admission')
        payload["can_retry"] = (self.can_retry(actor)
            and attempt.scheduling_retirement is None
            and (attempt.bootstrap_recovery is None or attempt.bootstrap_recovery.state == 'created')
            and attempt.status not in {"queued", "reconciliation_required", "started"})
        payload['can_recover_bootstrap'] = (self.can_retry(actor)
            and attempt.status == 'blocked' and attempt.scheduling_retirement is None
            and attempt.queued_turn_snapshot is not None
            and (attempt.bootstrap_recovery is not None or self.recorded_prestart_refusal(attempt)))
        payload['bootstrap_recovery_href'] = (
            f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/'
            f'{attempt.id}/bootstrap-recovery')
        payload['can_reconcile_prestart'] = (self.can_retry(actor)
            and ((attempt.status == 'reconciliation_required' and (attempt.queued_start_evidence is not None
                    or (attempt.queued_resume_evidence is not None
                        and attempt.queued_resume_evidence.outcome == 'archived_refusal')))
                 or (attempt.scheduling_retirement is None and (
                     (attempt.status == 'reconciliation_required' and self.recorded_prestart_refusal(attempt))
                     or (attempt.status == 'queued' and self.recorded_maintenance_refusal(attempt))))))
        payload['can_retire_scheduling'] = (self.can_retry(actor)
            and attempt.scheduling_retirement is None
            and attempt.status == 'reconciliation_required'
            and attempt.queued_start_receipt is None
            and attempt.queued_start_evidence is None
            and attempt.started_at is None
            and bool((attempt.queued_turn_snapshot or {}).get('work_item_ref')))
        payload['reconcile_prestart_href'] = (
            f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/{attempt.id}/reconcile-prestart')
        payload["retry_href"] = (
            f"/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/"
            f"{attempt.id}/retry"
        )
        payload['retire_scheduling_href'] = (
            f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/'
            f'{attempt.id}/retire-scheduling')
        if attempt.notification_batch_id is not None:
            for capability in ('can_retry', 'can_recover_bootstrap', 'can_reconcile_prestart', 'can_retire_scheduling'):
                payload[capability] = False
            payload['notification_batch_href'] = (
                f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/notification-batches/{attempt.notification_batch_id}')
        return payload

    def retry_payload(
        self,
        attempt: ExecutionPreflightAttempt,
    ) -> TurnCreate:
        return TurnCreate(
            **({"read_only_resource_ids":attempt.resource_context.read_only_resource_ids,
                "resource_context_work_item_ref":attempt.resource_context.work_item_ref} if attempt.resource_context else {}),
            message=attempt.message,
            project_id=attempt.project_id,
            model=attempt.model,
            reasoning_effort=attempt.reasoning_effort,
            approval_policy=attempt.approval_policy,
            sandbox=attempt.sandbox,
            repository_resource_id=attempt.repository_resource_id,
            writable_repository_resource_ids=(
                attempt.writable_repository_resource_ids
            ),
            read_only_repository_resource_ids=(
                attempt.read_only_repository_resource_ids
            ),
            execution_profile_id=attempt.execution_profile_id,
            agent_profile_id=attempt.agent_profile_id,
            agent_profile_revision=attempt.agent_profile_revision,
        )
