from __future__ import annotations

import time
from typing import Any, Callable

from codex_web.definitions import DefinitionReference
from codex_web.execution_contract_schema import (
    ExecutionContractV1,
    execution_contract_for_work_item,
)
from codex_web.execution_contracts import (
    ExecutionRoleContract,
    execution_contract_prompt,
    execution_role_for_work_item,
)
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.models import WorkItemEvent, WorkItemState
from codex_web.services.execution_role_definitions import ExecutionRoleDefinitionService
from codex_web.security import TrustZone, envelope_untrusted, render_untrusted_content, security_boundary_instructions


class WorkItemContractService:
    """Derive execution contracts from canonical work state + published definitions."""

    def __init__(
        self,
        host: Any | None,
        base_formatter: Callable[[WorkItemState], str],
        execution_roles: ExecutionRoleDefinitionService,
        *,
        split_brain_findings: Callable[
            [WorkItemState], list[str]
        ] | None = None,
        save_state: Callable[
            [WorkItemState], WorkItemState
        ] | None = None,
        append_event: Callable[[WorkItemEvent], None] | None = None,
    ) -> None:
        self.base_formatter = base_formatter
        self.execution_roles = execution_roles
        self.split_brain_findings = (
            split_brain_findings
            or (
                getattr(host, "_work_item_split_brain_findings")
                if host is not None
                and hasattr(host, "_work_item_split_brain_findings")
                else lambda _state: []
            )
        )
        self.save_state = (
            save_state
            or (
                getattr(host, "_save_work_item_state")
                if host is not None
                and hasattr(host, "_save_work_item_state")
                else lambda state: state
            )
        )
        self.append_event = (
            append_event
            or (
                getattr(host, "_append_work_item_event")
                if host is not None
                and hasattr(host, "_append_work_item_event")
                else lambda _event: None
            )
        )

    def _resolution(
        self,
        state: WorkItemState,
    ) -> tuple[
        ExecutionRoleContract,
        list[str],
        ExecutionRoleCatalogDefinition,
        DefinitionReference,
    ]:
        findings: list[str] = []
        try:
            findings = [
                str(item)
                for item in (self.split_brain_findings(state) or [])
                if str(item).strip()
            ]
        except Exception:
            findings = []

        catalog = self.execution_roles.catalog(project_id=state.project_id)
        definition_ref = self.execution_roles.reference(project_id=state.project_id)
        role = execution_role_for_work_item(
            state,
            split_brain=bool(findings),
            catalog=catalog,
        )
        return role, findings, catalog, definition_ref

    def role_for_state(self, state: WorkItemState) -> ExecutionRoleContract:
        role, _, _, _ = self._resolution(state)
        return role

    def contract_for_state(self, state: WorkItemState) -> ExecutionContractV1:
        """Derive and validate the canonical execution contract deterministically."""

        role, findings, _catalog, definition_ref = self._resolution(state)
        return execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
            definition_refs=(definition_ref,),
        )

    def _pin_definition(self, state: WorkItemState, definition_ref: DefinitionReference) -> None:
        if state.execution.definition_refs == [definition_ref]:
            return
        state.execution.definition_refs = [definition_ref]
        self.save_state(state)
        self.append_event(
            WorkItemEvent(
                ref=state.ref,
                event_type="execution_definition_pinned",
                created_at=time.time(),
                source="definition-registry",
                reason="execution contract resolved",
                payload={
                    "definition_id": definition_ref.definition_id,
                    "kind": definition_ref.kind,
                    "revision": definition_ref.revision,
                    "record_id": definition_ref.record_id,
                    "checksum": definition_ref.checksum,
                },
            )
        )

    def dispatch_text(self, state: WorkItemState) -> str:
        base = self.base_formatter(state)
        untrusted_base = render_untrusted_content(
            envelope_untrusted(
                TrustZone.TASK_TEXT,
                f"work-item:{state.ref}",
                base,
            )
        )
        role, findings, catalog, definition_ref = self._resolution(state)
        self._pin_definition(state, definition_ref)
        contract = execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
            definition_refs=(definition_ref,),
        )
        return (
            f"{security_boundary_instructions()}\n\n"
            f"{untrusted_base}\n\n"
            f"CANONICAL EXECUTION CONTRACT (schema {contract.schema_version})\n"
            "This work item was populated/reconciled from its authoritative task source and codex-web state remains "
            "authoritative for owner, stage, handoff and next action. The versioned execution contract and its exact "
            f"Definition Registry revision ({definition_ref.definition_id}@{definition_ref.revision}) were validated "
            "before dispatch. Apply the resolved role below; do not create parallel ownership, definition or "
            "permission state.\n\n"
            f"{execution_contract_prompt(role, catalog=catalog)}"
        )


def install_work_item_contract_service(
    app: Any,
    host: Any | None,
    execution_roles: ExecutionRoleDefinitionService,
    *,
    base_formatter: Callable[[WorkItemState], str] | None = None,
    split_brain_findings: Callable[
        [WorkItemState], list[str]
    ] | None = None,
    save_state: Callable[
        [WorkItemState], WorkItemState
    ] | None = None,
    append_event: Callable[[WorkItemEvent], None] | None = None,
) -> WorkItemContractService:
    """Compose execution-contract decoration from explicit dependencies."""

    existing = getattr(app.state, "work_item_contract_service", None)
    if isinstance(existing, WorkItemContractService):
        return existing

    if base_formatter is None:
        if host is None:
            raise TypeError(
                "work-item contract service requires a base formatter"
            )
        base_formatter = host._work_item_dispatch_text

    service = WorkItemContractService(
        host,
        base_formatter,
        execution_roles,
        split_brain_findings=split_brain_findings,
        save_state=save_state,
        append_event=append_event,
    )
    app.state.work_item_contract_service = service
    if host is not None:
        host._work_item_execution_contract = service.contract_for_state
        host._work_item_dispatch_text = service.dispatch_text
    return service
