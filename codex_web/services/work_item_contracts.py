from __future__ import annotations

from typing import Any, Callable

from codex_web.execution_contract_schema import (
    ExecutionContractV1,
    execution_contract_for_work_item,
)
from codex_web.execution_contracts import (
    ExecutionRoleContract,
    execution_contract_prompt,
    execution_role_for_work_item,
)
from codex_web.models import WorkItemState
from codex_web.security import TrustZone, envelope_untrusted, render_untrusted_content, security_boundary_instructions


class WorkItemContractService:
    """Apply execution-role policy to canonical work-item wake-ups.

    GitLab/work-item ingestion remains the source of operational truth. This
    service derives and validates one versioned execution contract from that
    state before decorating the existing canonical dispatch text. It does not
    create a second task, ownership, permission, or authority model.
    """

    def __init__(self, host: Any, base_formatter: Callable[[WorkItemState], str]) -> None:
        self.host = host
        self.base_formatter = base_formatter

    def _resolution(self, state: WorkItemState) -> tuple[ExecutionRoleContract, list[str]]:
        findings: list[str] = []
        split_brain = getattr(self.host, "_work_item_split_brain_findings", None)
        if split_brain:
            try:
                findings = [str(item) for item in (split_brain(state) or []) if str(item).strip()]
            except Exception:
                findings = []
        role = execution_role_for_work_item(state, split_brain=bool(findings))
        return role, findings

    def role_for_state(self, state: WorkItemState) -> ExecutionRoleContract:
        role, _ = self._resolution(state)
        return role

    def contract_for_state(self, state: WorkItemState) -> ExecutionContractV1:
        """Derive and validate the canonical execution contract deterministically."""

        role, findings = self._resolution(state)
        return execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
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
        role, findings = self._resolution(state)
        contract = execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
        )
        return (
            f"{security_boundary_instructions()}\n\n"
            f"{untrusted_base}\n\n"
            f"CANONICAL EXECUTION CONTRACT (schema {contract.schema_version})\n"
            "This work item was populated/reconciled from GitLab and codex-web state remains authoritative for "
            "owner, stage, handoff and next action. The versioned contract was validated from that canonical state "
            "before dispatch. Apply the execution role below; do not create a parallel ownership model or a parallel "
            "permission model.\n\n"
            f"{execution_contract_prompt(role)}"
        )


def install_work_item_contract_service(app: Any, host: Any) -> WorkItemContractService:
    """Decorate the canonical work-item dispatcher once and preserve its API."""

    existing = getattr(app.state, "work_item_contract_service", None)
    if isinstance(existing, WorkItemContractService) and existing.host is host:
        return existing

    base_formatter = host._work_item_dispatch_text
    service = WorkItemContractService(host, base_formatter)
    app.state.work_item_contract_service = service
    host._work_item_execution_contract = service.contract_for_state
    host._work_item_dispatch_text = service.dispatch_text
    return service
