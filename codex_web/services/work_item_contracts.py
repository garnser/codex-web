from __future__ import annotations

from typing import Any, Callable

from codex_web.execution_contracts import execution_contract_prompt, execution_role_for_work_item
from codex_web.models import WorkItemState


class WorkItemContractService:
    """Apply the same execution-role policy to canonical work-item wake-ups.

    GitLab/work-item ingestion remains the source of operational truth. This
    service decorates the existing canonical dispatch text with the execution
    contract derived from the work item's owner/stage instead of introducing a
    second task or ownership model.
    """

    def __init__(self, host: Any, base_formatter: Callable[[WorkItemState], str]) -> None:
        self.host = host
        self.base_formatter = base_formatter

    def role_for_state(self, state: WorkItemState):
        findings = []
        split_brain = getattr(self.host, "_work_item_split_brain_findings", None)
        if split_brain:
            try:
                findings = split_brain(state) or []
            except Exception:
                findings = []
        return execution_role_for_work_item(state, split_brain=bool(findings))

    def dispatch_text(self, state: WorkItemState) -> str:
        base = self.base_formatter(state)
        role = self.role_for_state(state)
        return (
            f"{base}\n\n"
            "CANONICAL EXECUTION CONTRACT\n"
            "This work item was populated/reconciled from GitLab and codex-web state remains authoritative for "
            "owner, stage, handoff and next action. Apply the execution role below to that existing state; do not "
            "create a parallel ownership model.\n\n"
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
    host._work_item_dispatch_text = service.dispatch_text
    return service
