from __future__ import annotations

import time
from collections.abc import Callable

from codex_web.data_governance import (
    CLASSIFICATION_RANK,
    ContextFilterDecision,
    ContextFilterRequest,
    ContextFilterResult,
    DataCategory,
    DataClassification,
    DataGovernanceState,
    ExportAuthorizationRequest,
    ExportAuthorizationResult,
    ExportManifestItem,
    GovernedDataCreate,
    GovernedDataLifecycle,
    GovernedDataRecord,
    GovernedDeletionRequest,
    GovernanceAction,
    GovernanceActionRequest,
    GovernanceAuditEvent,
    GovernanceRequestStatus,
    RetentionSweepResult,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.data_governance import DataGovernanceStore


class DataGovernanceError(RuntimeError):
    pass


class GovernanceNotFoundError(DataGovernanceError):
    pass


class GovernanceConflictError(DataGovernanceError):
    pass


ActionHandler = Callable[[GovernedDataRecord, GovernanceAction], str | None]


class DataGovernanceService:
    """Tenant-scoped classification, retention and governed-data lifecycle service.

    The registry stores metadata only. Domain stores retain ownership of their
    payloads and register an action handler before redaction/anonymization/delete
    can be reported as completed.
    """

    def __init__(self, store: DataGovernanceStore) -> None:
        self.store = store
        self._action_handlers: dict[str, ActionHandler] = {}

    def register_action_handler(self, object_type: str, handler: ActionHandler) -> None:
        key = object_type.strip()
        if not key:
            raise ValueError("object_type is required")
        existing = self._action_handlers.get(key)
        if existing is not None and existing is not handler:
            raise GovernanceConflictError(f"action handler already registered: {key}")
        self._action_handlers[key] = handler

    @staticmethod
    def _require_mutation(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "data-governance:write" not in actor.service_scopes:
                raise AuthorizationError("data-governance:write service scope required")
            return
        IdentityService.require_admin(actor)

    @staticmethod
    def _same_scope(record: GovernedDataRecord, actor: AuthenticationActor) -> bool:
        return (
            record.organization_id == actor.organization_id
            and record.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _audit(
        state: DataGovernanceState,
        *,
        actor: AuthenticationActor,
        operation: str,
        outcome: str,
        record: GovernedDataRecord | None = None,
        request: GovernedDeletionRequest | None = None,
        reason_code: str | None = None,
    ) -> None:
        state.events.append(
            GovernanceAuditEvent(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.identity_id,
                operation=operation,
                outcome=outcome,
                record_id=record.id if record is not None else None,
                request_id=request.id if request is not None else None,
                object_type=record.object_type if record is not None else None,
                object_id=record.object_id if record is not None else None,
                classification=record.classification if record is not None else None,
                reason_code=reason_code,
            )
        )

    @staticmethod
    def _record_from_state(
        state: DataGovernanceState,
        record_id: str,
        actor: AuthenticationActor,
    ) -> GovernedDataRecord:
        item = next(
            (
                record
                for record in state.records
                if record.id == record_id
                and record.organization_id == actor.organization_id
                and record.workspace_id == actor.workspace_id
            ),
            None,
        )
        if item is None:
            raise GovernanceNotFoundError("governed data record not found")
        return item

    @staticmethod
    def _request_from_state(
        state: DataGovernanceState,
        request_id: str,
        actor: AuthenticationActor,
    ) -> GovernedDeletionRequest:
        item = next(
            (
                request
                for request in state.requests
                if request.id == request_id
                and request.organization_id == actor.organization_id
                and request.workspace_id == actor.workspace_id
            ),
            None,
        )
        if item is None:
            raise GovernanceNotFoundError("governance action request not found")
        return item

    def list_records(
        self,
        actor: AuthenticationActor,
        *,
        category: DataCategory | None = None,
        classification: DataClassification | None = None,
        project_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[GovernedDataRecord]:
        items = [item for item in self.store.load().records if self._same_scope(item, actor)]
        if category is not None:
            items = [item for item in items if item.category == category]
        if classification is not None:
            items = [item for item in items if item.classification == classification]
        if project_id is not None:
            items = [item for item in items if item.project_id == project_id]
        if not include_inactive:
            items = [item for item in items if item.lifecycle == GovernedDataLifecycle.ACTIVE]
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    def get_record(self, record_id: str, actor: AuthenticationActor) -> GovernedDataRecord:
        return self._record_from_state(self.store.load(), record_id, actor)

    def register(
        self,
        payload: GovernedDataCreate,
        *,
        actor: AuthenticationActor,
    ) -> GovernedDataRecord:
        self._require_mutation(actor)
        created: list[GovernedDataRecord] = []

        def apply(state: DataGovernanceState) -> DataGovernanceState:
            sources = [
                self._record_from_state(state, source_id, actor)
                for source_id in payload.source_record_ids
            ]
            effective = payload.classification
            if sources:
                effective = max(
                    [payload.classification, *(item.classification for item in sources)],
                    key=lambda value: CLASSIFICATION_RANK[value],
                )

            source_expiries = [
                item.retention_expires_at
                for item in sources
                if item.retention_expires_at is not None
            ]
            retention_expires_at = payload.retention_expires_at
            if source_expiries:
                source_limit = min(source_expiries)
                if retention_expires_at is None or retention_expires_at > source_limit:
                    retention_expires_at = source_limit

            residency_tags = set(payload.residency_tags)
            for source in sources:
                residency_tags.update(source.residency_tags)

            deny_model_context = payload.deny_model_context or any(
                item.deny_model_context for item in sources
            )
            if payload.category == DataCategory.CREDENTIAL:
                deny_model_context = True

            now = time.time()
            record = GovernedDataRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=payload.project_id,
                object_type=payload.object_type,
                object_id=payload.object_id,
                category=payload.category,
                requested_classification=payload.classification,
                classification=effective,
                retention_policy_ref=payload.retention_policy_ref,
                retention_expires_at=retention_expires_at,
                retention_action=payload.retention_action,
                residency_tags=tuple(residency_tags),
                source_record_ids=payload.source_record_ids,
                deny_model_context=deny_model_context,
                created_by=actor.identity_id,
                create_reason=payload.reason,
                created_at=now,
                updated_at=now,
            )
            state.records.append(record)
            self._audit(
                state,
                actor=actor,
                operation="record_registered",
                outcome="success",
                record=record,
                reason_code=(
                    "classification_elevated_from_sources"
                    if effective != payload.classification
                    else "registered"
                ),
            )
            created.append(record)
            return state

        self.store.update(apply)
        return created[0]

    def set_legal_hold(
        self,
        record_id: str,
        reason: str,
        *,
        actor: AuthenticationActor,
    ) -> GovernedDataRecord:
        self._require_mutation(actor)
        updated: list[GovernedDataRecord] = []

        def apply(state: DataGovernanceState) -> DataGovernanceState:
            record = self._record_from_state(state, record_id, actor)
            now = time.time()
            replacement = record.model_copy(
                update={
                    "legal_hold_at": record.legal_hold_at or now,
                    "legal_hold_by": actor.identity_id,
                    "legal_hold_reason": reason,
                    "updated_at": now,
                }
            )
            state.records = [
                replacement if item.id == record_id else item for item in state.records
            ]
            self._audit(
                state,
                actor=actor,
                operation="legal_hold_set",
                outcome="success",
                record=replacement,
                reason_code="legal_hold",
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def release_legal_hold(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> GovernedDataRecord:
        self._require_mutation(actor)
        updated: list[GovernedDataRecord] = []

        def apply(state: DataGovernanceState) -> DataGovernanceState:
            record = self._record_from_state(state, record_id, actor)
            replacement = record.model_copy(
                update={
                    "legal_hold_at": None,
                    "legal_hold_by": None,
                    "legal_hold_reason": None,
                    "updated_at": time.time(),
                }
            )
            state.records = [
                replacement if item.id == record_id else item for item in state.records
            ]
            for index, request in enumerate(state.requests):
                if (
                    request.record_id == record_id
                    and request.status == GovernanceRequestStatus.BLOCKED
                    and request.blocked_reason == "legal_hold"
                ):
                    state.requests[index] = request.model_copy(
                        update={
                            "status": GovernanceRequestStatus.PENDING,
                            "blocked_reason": None,
                        }
                    )
            self._audit(
                state,
                actor=actor,
                operation="legal_hold_released",
                outcome="success",
                record=replacement,
                reason_code="legal_hold_released",
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def request_action(
        self,
        payload: GovernanceActionRequest,
        *,
        actor: AuthenticationActor,
    ) -> GovernedDeletionRequest:
        self._require_mutation(actor)
        created: list[GovernedDeletionRequest] = []

        def apply(state: DataGovernanceState) -> DataGovernanceState:
            record = self._record_from_state(state, payload.record_id, actor)
            if record.lifecycle != GovernedDataLifecycle.ACTIVE:
                raise GovernanceConflictError("governance action requires an active record")
            blocked_reason = "legal_hold" if record.legal_hold_at is not None else None
            request = GovernedDeletionRequest(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                record_id=record.id,
                action=payload.action,
                status=(
                    GovernanceRequestStatus.BLOCKED
                    if blocked_reason
                    else GovernanceRequestStatus.PENDING
                ),
                requested_by=actor.identity_id,
                reason=payload.reason,
                blocked_reason=blocked_reason,
            )
            state.requests.append(request)
            self._audit(
                state,
                actor=actor,
                operation="governance_action_requested",
                outcome="blocked" if blocked_reason else "pending",
                record=record,
                request=request,
                reason_code=blocked_reason or payload.action.value,
            )
            created.append(request)
            return state

        self.store.update(apply)
        return created[0]

    def list_requests(self, actor: AuthenticationActor) -> list[GovernedDeletionRequest]:
        return sorted(
            [
                item
                for item in self.store.load().requests
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
            key=lambda item: (item.requested_at, item.id),
            reverse=True,
        )

    def execute_request(
        self,
        request_id: str,
        *,
        actor: AuthenticationActor,
    ) -> GovernedDeletionRequest:
        self._require_mutation(actor)
        state = self.store.load()
        request = self._request_from_state(state, request_id, actor)
        record = self._record_from_state(state, request.record_id, actor)

        if request.status == GovernanceRequestStatus.COMPLETED:
            return request
        if record.legal_hold_at is not None:
            raise GovernanceConflictError("record is under legal hold")
        handler = self._action_handlers.get(record.object_type)
        if handler is None:
            blocked: list[GovernedDeletionRequest] = []

            def mark_blocked(current: DataGovernanceState) -> DataGovernanceState:
                selected = self._request_from_state(current, request_id, actor)
                current_record = self._record_from_state(current, selected.record_id, actor)
                replacement = selected.model_copy(
                    update={
                        "status": GovernanceRequestStatus.BLOCKED,
                        "blocked_reason": "adapter_unavailable",
                    }
                )
                current.requests = [
                    replacement if item.id == request_id else item for item in current.requests
                ]
                self._audit(
                    current,
                    actor=actor,
                    operation="governance_action_execute",
                    outcome="blocked",
                    record=current_record,
                    request=replacement,
                    reason_code="adapter_unavailable",
                )
                blocked.append(replacement)
                return current

            self.store.update(mark_blocked)
            raise GovernanceConflictError(
                f"no governed-data action handler registered for {record.object_type}"
            )

        receipt_ref = handler(record, request.action)
        completed: list[GovernedDeletionRequest] = []

        lifecycle = {
            GovernanceAction.REDACT: GovernedDataLifecycle.REDACTED,
            GovernanceAction.ANONYMIZE: GovernedDataLifecycle.ANONYMIZED,
            GovernanceAction.DELETE: GovernedDataLifecycle.DELETED,
        }[request.action]

        def apply(current: DataGovernanceState) -> DataGovernanceState:
            selected = self._request_from_state(current, request_id, actor)
            current_record = self._record_from_state(current, selected.record_id, actor)
            now = time.time()
            record_replacement = current_record.model_copy(
                update={
                    "lifecycle": lifecycle,
                    "action_completed_at": now,
                    "updated_at": now,
                }
            )
            request_replacement = selected.model_copy(
                update={
                    "status": GovernanceRequestStatus.COMPLETED,
                    "blocked_reason": None,
                    "completed_by": actor.identity_id,
                    "completed_at": now,
                    "adapter_receipt_ref": receipt_ref,
                }
            )
            current.records = [
                record_replacement if item.id == current_record.id else item
                for item in current.records
            ]
            current.requests = [
                request_replacement if item.id == request_id else item
                for item in current.requests
            ]
            self._audit(
                current,
                actor=actor,
                operation="governance_action_execute",
                outcome="success",
                record=record_replacement,
                request=request_replacement,
                reason_code=request.action.value,
            )
            completed.append(request_replacement)
            return current

        self.store.update(apply)
        return completed[0]

    def retention_sweep(
        self,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
        execute: bool = False,
    ) -> RetentionSweepResult:
        self._require_mutation(actor)
        current_time = time.time() if now is None else now
        state = self.store.load()
        due = [
            item
            for item in state.records
            if self._same_scope(item, actor)
            and item.lifecycle == GovernedDataLifecycle.ACTIVE
            and item.retention_expires_at is not None
            and item.retention_expires_at <= current_time
        ]
        held = [item.id for item in due if item.legal_hold_at is not None]
        requests: list[GovernedDeletionRequest] = []

        for record in due:
            if record.legal_hold_at is not None:
                continue
            existing = next(
                (
                    request
                    for request in self.list_requests(actor)
                    if request.record_id == record.id
                    and request.reason == "retention_expired"
                    and request.status
                    in {
                        GovernanceRequestStatus.PENDING,
                        GovernanceRequestStatus.BLOCKED,
                        GovernanceRequestStatus.COMPLETED,
                    }
                ),
                None,
            )
            if existing is not None:
                requests.append(existing)
                continue
            requests.append(
                self.request_action(
                    GovernanceActionRequest(
                        record_id=record.id,
                        action=record.retention_action,
                        reason="retention_expired",
                    ),
                    actor=actor,
                )
            )

        completed: list[str] = []
        if execute:
            for request in requests:
                if request.status == GovernanceRequestStatus.COMPLETED:
                    completed.append(request.id)
                    continue
                try:
                    result = self.execute_request(request.id, actor=actor)
                except GovernanceConflictError:
                    continue
                if result.status == GovernanceRequestStatus.COMPLETED:
                    completed.append(result.id)

        return RetentionSweepResult(
            due_record_ids=tuple(item.id for item in due),
            held_record_ids=tuple(held),
            request_ids=tuple(item.id for item in requests),
            completed_request_ids=tuple(completed),
        )

    def filter_context(
        self,
        payload: ContextFilterRequest,
        *,
        actor: AuthenticationActor,
    ) -> ContextFilterResult:
        state = self.store.load()
        decisions: list[ContextFilterDecision] = []
        for record_id in payload.record_ids:
            record = self._record_from_state(state, record_id, actor)
            if record.lifecycle != GovernedDataLifecycle.ACTIVE:
                allowed, reason = False, f"lifecycle:{record.lifecycle.value}"
            elif record.category == DataCategory.CREDENTIAL:
                allowed, reason = False, "credential_data_never_enters_model_context"
            elif record.classification == DataClassification.SECRET:
                allowed, reason = False, "secret_data_never_enters_model_context"
            elif record.deny_model_context:
                allowed, reason = False, "record_denies_model_context"
            elif CLASSIFICATION_RANK[record.classification] > CLASSIFICATION_RANK[
                payload.max_classification
            ]:
                allowed, reason = False, "classification_exceeds_context_limit"
            else:
                allowed, reason = True, "allowed"
            decisions.append(
                ContextFilterDecision(
                    record_id=record.id,
                    allowed=allowed,
                    classification=record.classification,
                    reason=reason,
                )
            )
        return ContextFilterResult(
            allowed_record_ids=tuple(item.record_id for item in decisions if item.allowed),
            denied_record_ids=tuple(item.record_id for item in decisions if not item.allowed),
            decisions=tuple(decisions),
        )

    def authorize_export(
        self,
        payload: ExportAuthorizationRequest,
        *,
        actor: AuthenticationActor,
    ) -> ExportAuthorizationResult:
        self._require_mutation(actor)
        state = self.store.load()
        items: list[ExportManifestItem] = []
        denied: list[ContextFilterDecision] = []

        def apply(current: DataGovernanceState) -> DataGovernanceState:
            for record_id in payload.record_ids:
                record = self._record_from_state(current, record_id, actor)
                if record.lifecycle == GovernedDataLifecycle.DELETED:
                    decision = ContextFilterDecision(
                        record_id=record.id,
                        allowed=False,
                        classification=record.classification,
                        reason="record_deleted",
                    )
                    denied.append(decision)
                    self._audit(
                        current,
                        actor=actor,
                        operation="export_authorize",
                        outcome="denied",
                        record=record,
                        reason_code=decision.reason,
                    )
                    continue
                if record.category == DataCategory.CREDENTIAL:
                    reason = "credential_export_denied"
                elif record.classification == DataClassification.SECRET:
                    reason = "secret_export_denied"
                elif CLASSIFICATION_RANK[record.classification] > CLASSIFICATION_RANK[
                    payload.max_classification
                ]:
                    reason = "classification_exceeds_export_limit"
                else:
                    reason = ""
                if reason:
                    decision = ContextFilterDecision(
                        record_id=record.id,
                        allowed=False,
                        classification=record.classification,
                        reason=reason,
                    )
                    denied.append(decision)
                    self._audit(
                        current,
                        actor=actor,
                        operation="export_authorize",
                        outcome="denied",
                        record=record,
                        reason_code=reason,
                    )
                    continue
                items.append(
                    ExportManifestItem(
                        record_id=record.id,
                        object_type=record.object_type,
                        object_id=record.object_id,
                        classification=record.classification,
                        project_id=record.project_id,
                        residency_tags=record.residency_tags,
                    )
                )
                self._audit(
                    current,
                    actor=actor,
                    operation="export_authorize",
                    outcome="success",
                    record=record,
                    reason_code="manifest_only",
                )
            return current

        self.store.update(apply)
        return ExportAuthorizationResult(items=tuple(items), denied=tuple(denied))

    def events(self, actor: AuthenticationActor) -> list[GovernanceAuditEvent]:
        self._require_mutation(actor)
        return sorted(
            [
                item
                for item in self.store.load().events
                if item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
            key=lambda item: (item.occurred_at, item.id),
            reverse=True,
        )
