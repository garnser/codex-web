from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from codex_web.approval_requests import (
    APPROVAL_REQUEST_STATE_CONTRACT,
    ApprovalRequest,
    ApprovalRequestState,
)
from codex_web.compatibility import MigrationRegistry
from codex_web.scheduler import ScheduleRecord, SchedulerState
from codex_web.storage.sqlite_state import SQLiteStateStore


APPROVAL_REQUEST_STATE_MIGRATIONS = MigrationRegistry("approval-request-state")
APPROVAL_REQUEST_STATE_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": APPROVAL_REQUEST_STATE_CONTRACT.current,
        "requests": dict(payload.get("requests") or {}),
    },
)


class ApprovalRequestNotFoundError(KeyError):
    pass


class ApprovalRequestConflictError(RuntimeError):
    pass


T = TypeVar("T")


class ApprovalRequestStore:
    namespace = "approval_requests"
    scheduler_namespace = "scheduler"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ApprovalRequestState:
        if payload is None:
            return ApprovalRequestState()
        if not isinstance(payload, dict):
            raise ValueError("approval request state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != APPROVAL_REQUEST_STATE_CONTRACT.current:
            payload = APPROVAL_REQUEST_STATE_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=APPROVAL_REQUEST_STATE_CONTRACT.current,
            )
        APPROVAL_REQUEST_STATE_CONTRACT.require(payload.get("schema_version", ""))
        return ApprovalRequestState.model_validate(payload)

    @staticmethod
    def _decode_scheduler(payload: Any) -> SchedulerState:
        if payload is None:
            return SchedulerState()
        return SchedulerState.model_validate(payload)

    def load(self) -> ApprovalRequestState:
        return self._decode(self.store.get(self.namespace))

    def list(
        self,
        *,
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[ApprovalRequest]:
        requests = self.load().requests.values()
        return sorted(
            (
                item
                for item in requests
                if (organization_id is None or item.organization_id == organization_id)
                and (workspace_id is None or item.workspace_id == workspace_id)
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    def get(self, request_id: str) -> ApprovalRequest:
        request = self.load().requests.get(request_id)
        if request is None:
            raise ApprovalRequestNotFoundError(request_id)
        return request

    def create(
        self,
        request: ApprovalRequest,
        *,
        expiry_schedule: ScheduleRecord | None = None,
    ) -> ApprovalRequest:
        if expiry_schedule is None:
            def apply(raw: Any) -> dict[str, Any]:
                state = self._decode(raw)
                if request.id in state.requests:
                    raise ApprovalRequestConflictError(
                        f"approval request already exists: {request.id}"
                    )
                state.requests[request.id] = request
                return state.model_dump(mode="json")

            payload = self.store.update(
                self.namespace,
                apply,
                default=ApprovalRequestState().model_dump(mode="json"),
            )
            return self._decode(payload).requests[request.id]

        defaults = {
            self.namespace: ApprovalRequestState().model_dump(mode="json"),
            self.scheduler_namespace: SchedulerState().model_dump(mode="json"),
        }

        def apply_many(documents: dict[str, Any]) -> dict[str, Any]:
            approval_state = self._decode(documents[self.namespace])
            scheduler_state = self._decode_scheduler(
                documents[self.scheduler_namespace]
            )
            if request.id in approval_state.requests:
                raise ApprovalRequestConflictError(
                    f"approval request already exists: {request.id}"
                )
            if expiry_schedule.id in scheduler_state.schedules:
                raise ApprovalRequestConflictError(
                    f"approval expiry schedule already exists: {expiry_schedule.id}"
                )
            bound = request.model_copy(
                update={"expiry_schedule_id": expiry_schedule.id}
            )
            approval_state.requests[request.id] = bound
            scheduler_state.schedules[expiry_schedule.id] = expiry_schedule
            return {
                self.namespace: approval_state.model_dump(mode="json"),
                self.scheduler_namespace: scheduler_state.model_dump(mode="json"),
            }

        updated = self.store.update_many(defaults, apply_many)
        return self._decode(updated[self.namespace]).requests[request.id]

    def update_request(
        self,
        request_id: str,
        updater: Callable[[ApprovalRequest], ApprovalRequest],
    ) -> ApprovalRequest:
        result: dict[str, ApprovalRequest] = {}

        def apply(raw: Any) -> dict[str, Any]:
            state = self._decode(raw)
            current = state.requests.get(request_id)
            if current is None:
                raise ApprovalRequestNotFoundError(request_id)
            updated = updater(current)
            if updated.id != current.id:
                raise ValueError("approval request updater cannot change request id")
            state.requests[request_id] = updated
            result["value"] = updated
            return state.model_dump(mode="json")

        self.store.update(
            self.namespace,
            apply,
            default=ApprovalRequestState().model_dump(mode="json"),
        )
        return result["value"]

    def consume_with_document(
        self,
        request_id: str,
        *,
        guarded_namespace: str,
        guarded_default: Any,
        updater: Callable[[ApprovalRequest, Any], tuple[ApprovalRequest, Any, T]],
    ) -> tuple[ApprovalRequest, T]:
        if guarded_namespace == self.namespace:
            raise ValueError("guarded namespace must differ from approval namespace")

        result: dict[str, Any] = {}
        defaults = {
            self.namespace: ApprovalRequestState().model_dump(mode="json"),
            guarded_namespace: guarded_default,
        }

        def apply(documents: dict[str, Any]) -> dict[str, Any]:
            state = self._decode(documents[self.namespace])
            current = state.requests.get(request_id)
            if current is None:
                raise ApprovalRequestNotFoundError(request_id)
            updated_request, updated_guarded, mutation_result = updater(
                current,
                documents[guarded_namespace],
            )
            if updated_request.id != current.id:
                raise ValueError("approval request updater cannot change request id")
            state.requests[request_id] = updated_request
            result["request"] = updated_request
            result["mutation"] = mutation_result
            return {
                self.namespace: state.model_dump(mode="json"),
                guarded_namespace: updated_guarded,
            }

        self.store.update_many(defaults, apply)
        return result["request"], result["mutation"]
