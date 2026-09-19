from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT,
    ActionCapability,
    ActionDefinition,
    ActionEvidence,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionRiskClass,
    ActionVerification,
)
from codex_web.identity import TenantScope
from codex_web.services.task_sources import TaskSourceCreateRequest
from codex_web.services.work_items import WorkItemService


TASK_SOURCE_ACTION_PROVIDER_TYPE = "task-source"
TASK_SOURCE_ACTION_PROVIDER_INSTANCE = "authoritative"
TASK_SOURCE_CREATE_ACTION_ID = "task-source.create"


class TaskSourceActionProvider:
    """ActionProvider adapter for authoritative TaskSource task creation.

    The adapter intentionally exposes only CREATE. Provider-specific credentials
    and schemas remain behind WorkItemService/TaskSource resolution.
    """

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = TASK_SOURCE_ACTION_PROVIDER_TYPE
    provider_instance = TASK_SOURCE_ACTION_PROVIDER_INSTANCE

    def __init__(self, work_items: WorkItemService) -> None:
        self.work_items = work_items

    def actions(self) -> tuple[ActionDefinition, ...]:
        return (
            ActionDefinition(
                action_id=TASK_SOURCE_CREATE_ACTION_ID,
                title="Create authoritative task",
                description=(
                    "Create one task through the project's configured authoritative "
                    "TaskSource and project the returned identity into canonical Work Item state."
                ),
                capabilities=ActionCapability(
                    read=False,
                    prepare=True,
                    execute=True,
                    dry_run=False,
                    idempotency=False,
                    rollback=False,
                    verification=False,
                    progress=False,
                    evidence=True,
                ),
                risk_class=ActionRiskClass.MEDIUM,
                required_authority=("work-item.create",),
                credential_required=False,
                expected_evidence=("authoritative-task",),
                timeout_seconds=60.0,
                retry_max_attempts=1,
                reversible=False,
                network_access=True,
            ),
        )

    @staticmethod
    def _string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            raise ValueError(f"task-source.create parameter {field!r} must be an array")
        rows: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                rows.append(text)
        return tuple(dict.fromkeys(rows))

    @classmethod
    def _payload(cls, request: ActionRequest) -> TaskSourceCreateRequest:
        allowed = {"title", "body", "owners", "labels"}
        unknown = sorted(set(request.parameters) - allowed)
        if unknown:
            raise ValueError(
                "task-source.create received unsupported parameters: "
                + ", ".join(unknown)
            )
        title = str(request.parameters.get("title") or "").strip()
        if not title:
            raise ValueError("task-source.create requires parameter 'title'")
        body_raw = request.parameters.get("body")
        if body_raw is not None and not isinstance(body_raw, str):
            raise ValueError("task-source.create parameter 'body' must be a string")
        return TaskSourceCreateRequest(
            title=title,
            body=body_raw,
            owners=cls._string_tuple(request.parameters.get("owners"), field="owners"),
            labels=cls._string_tuple(request.parameters.get("labels"), field="labels"),
        )

    @staticmethod
    def _scope(request: ActionRequest) -> TenantScope:
        return TenantScope(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        )

    @staticmethod
    def _project_id(request: ActionRequest) -> str:
        project_id = str(request.project_id or "").strip()
        if not project_id:
            raise ValueError("task-source.create requires project_id")
        return project_id

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]:
        project_id = self._project_id(request)
        payload = self._payload(request)
        configuration, source = self.work_items.resolve_authoritative_create(
            project_id,
            scope=self._scope(request),
        )
        return {
            "operation": "create",
            "project_id": project_id,
            "title": payload.title,
            "owner_count": len(payload.owners),
            "label_count": len(payload.labels),
            "source_type": source.source_type,
            "source_instance": source.source_instance,
            "source_scope": configuration.scope,
        }

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if credential is not None:
            raise ValueError(
                "task-source.create receives credentials only through the TaskSource boundary"
            )
        project_id = self._project_id(request)
        payload = self._payload(request)
        started = time.time()
        state = await self.work_items.create_authoritative(
            project_id,
            payload,
            scope=self._scope(request),
        )
        identity = getattr(state, "source_identity", None)
        ref = str(getattr(state, "ref", "") or "").strip()
        if identity is None or not ref:
            raise RuntimeError(
                "authoritative TaskSource creation did not return canonical identity"
            )
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=started,
            completed_at=time.time(),
            external_id=identity.external_id,
            output={
                "work_item_ref": ref,
                "source_type": identity.source_type,
                "source_instance": identity.source_instance,
                "external_id": identity.external_id,
                "external_url": identity.external_url,
            },
            evidence=(
                ActionEvidence(
                    evidence_type="authoritative-task",
                    reference=ref,
                    summary="Authoritative task created and projected into canonical Work Item state.",
                    metadata={
                        "project_id": project_id,
                        "source_type": identity.source_type,
                        "source_instance": identity.source_instance,
                        "external_id": identity.external_id,
                    },
                ),
            ),
        )

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
    ) -> ActionVerification:
        return ActionVerification(
            verified=False,
            evidence=result.evidence,
            findings=(
                "task-source.create uses ActionIntent receipt/reconciliation; "
                "provider verification is not declared",
            ),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        raise ValueError("task-source.create is not rollback-capable")
