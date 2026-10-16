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
from codex_web.execution_profiles import ExecutionProfileContract
from codex_web.models import WorkItemEvent, WorkItemState
from codex_web.services.execution_role_definitions import ExecutionRoleDefinitionService
from codex_web.services.execution_profile_definitions import ExecutionProfileDefinitionService
from codex_web.security import TrustZone, envelope_untrusted, render_untrusted_content, security_boundary_instructions


def assignment_control_plane_instructions() -> str:
    """Describe the existing assignment broker; never grant additional access."""

    return (
        "ASSIGNMENT CONTROL-PLANE ACCESS\n"
        "Direct Git host DNS, SSH, HTTP, and localhost access may be intentionally unavailable in the execution "
        "sandbox. When CODEX_WEB_CONTROL_PLANE_URL is set, use that assignment-bound broker before reporting a "
        "GitLab or repository-access blocker. This is an HTTP tool channel; absence of MCP tools does not "
        "mean governed operations are unavailable. Discover the exact allowlisted methods and paths with "
        "GET /api/control-plane-broker/operations, for example: "
        "curl --noproxy '*' --connect-timeout 5 --max-time 60 --silent --show-error "
        "--write-out '\\nHTTP %{http_code}\\n' "
        "\"${CODEX_WEB_CONTROL_PLANE_URL}/api/control-plane-broker/operations\". "
        "Bypass proxies only for this assignment-loopback request; do not change global proxy or network "
        "settings. A short client timeout is not proof of denial. Preserve HTTP status, broker denial and "
        "correlation evidence; a client timeout does not prove whether a mutation ran, so reconcile its "
        "existing ActionIntent before retrying. "
        "For Work Item coordination use GET /api/work-items/{encoded_ref}?view=coordination: it preserves "
        "live handoff/ACK, scope and owner_dispatch preconditions while omitting accumulated history. "
        "Oversized Work Item read and mutation replies automatically use that projection; inspect "
        "_projection.omitted_fields rather than treating omitted history as absent canonical evidence. "
        "Repository facts, refs, merge requests, checks, job logs, artifacts, governed branch publication, "
        "merge-request upsert/merge, and check rerun operations exposed "
        "there are the supported access paths. Do not use direct GitLab API calls or direct git push when the "
        "broker provides the required operation. If the required operation is absent or the broker denies it, "
        "record the exact broker operation, status, and denial as the blocker. "
        "For repository.branch.publish put branch and the exact current full head_revision inside parameters; "
        "the broker supplies the current assignment's execution_workspace_id. A recovered workspace may have "
        "a different attested local branch from the preserved publication destination. Read the destination "
        "ref through the broker before creating a NEW publication intent: if the preserved codex/ ref is "
        "confirmed absent, set expected_remote_absent: true; if it exists, set expected_remote_revision to "
        "its observed full SHA. These preconditions are mutually exclusive. Never use an empty or all-zero "
        "revision to mean absent, infer absence from a timeout/denial, or silently replace an existing ref. "
        "Keep the local checkout on its currently attested branch with a clean tree and exact requested head; "
        "do not rename it to the destination branch. A released assignment requires a newly authorized "
        "assignment and active writable lease; historical authority cannot be reused. Preserve commits and "
        "evidence before releasing workspaces. Reconcile failed/uncertain receipts and observed provider "
        "state; never replay an attempted historical intent. Publication success requires a succeeded receipt "
        "and verification of the exact remote head, followed by the existing MR and owner/validation handoff "
        "process. Existing-MR recovery must use its observed head, not expected_remote_absent.\n\n"
        "For an existing GitLab MR whose branch is not codex/, a repository-write owner running under a "
        "thread or thread-bootstrap subject must first request bounded issue scope with POST "
        "/api/repository-workspace/publication-recovery and JSON {\"work_item_ref\": \"<qualified-ref>\"}. "
        "The broker accepts only the current Work Item owner for the assignment's exact writable repository "
        "and records the original execution, subject, assignment fence and lease expiry without relabeling "
        "the execution subject. Then publish with existing_change_request_number and the observed "
        "expected_remote_revision. A denial is an actionable scope/owner/lease blocker; do not ask "
        "Orchestrator to grant repository authority or use Operator provisioning as a substitute.\n\n"
        "MANAGED VALIDATION OUTPUT\n"
        "When CODEX_WEB_MANAGED_VALIDATION_RUNNER and CODEX_WEB_MANAGED_VALIDATION_DIR are present, "
        "the native runtime has registered an assignment/fence-scoped output slot in canonical Artifacts. "
        "Invoke validation as: python \"$CODEX_WEB_MANAGED_VALIDATION_RUNNER\" --source <workspace-relative-source> "
        "[--source <another-source>] -- <validation-command> [arguments]. This existing worker command runs "
        "inside the same sandbox and limits. The managed report records command/source digests and an "
        "unverified exit observation, never stdout, credentials or a passed-test claim. Runtime stop/loss "
        "captures the bounded registered report through canonical governed ArtifactContent/Evidence. "
        "Source files stay in the retained execution workspace. Arbitrary /tmp logs and commands outside "
        "this managed-output protocol have no native custody guarantee; report their result as unknown "
        "after interruption. Missing registration/content service is unavailable evidence, not permission "
        "to use a different API, copy secrets, or claim durable validation. Independent validation gates "
        "remain authoritative.\n\n"
    ).rstrip()


class WorkItemContractService:
    """Derive execution contracts from canonical work state + published definitions."""

    def __init__(
        self,
        host: Any | None,
        base_formatter: Callable[[WorkItemState], str],
        execution_roles: ExecutionRoleDefinitionService,
        execution_profiles: ExecutionProfileDefinitionService | None = None,
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
        self.execution_profiles = execution_profiles
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
        ExecutionProfileContract | None,
        DefinitionReference | None,
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

        context = {
            "organization_id": state.organization_id,
            "workspace_id": state.workspace_id,
            "project_id": state.project_id,
        }
        catalog, definition_ref = self.execution_roles.resolve_catalog(**context)
        role = execution_role_for_work_item(
            state,
            split_brain=bool(findings),
            catalog=catalog,
        )
        if self.execution_profiles is None:
            profile = None
            profile_ref = None
        else:
            profile, profile_ref = self.execution_profiles.resolve(
                role.execution_profile_id,
                **context,
            )
        return role, findings, catalog, definition_ref, profile, profile_ref

    def role_for_state(self, state: WorkItemState) -> ExecutionRoleContract:
        role, _, _, _, _, _ = self._resolution(state)
        return role

    def contract_for_state(self, state: WorkItemState) -> ExecutionContractV1:
        """Derive and validate the canonical execution contract deterministically."""

        (
            role,
            findings,
            _catalog,
            definition_ref,
            profile,
            profile_ref,
        ) = self._resolution(state)
        return execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
            definition_refs=(
                (definition_ref, profile_ref)
                if profile_ref is not None
                else (definition_ref,)
            ),
            execution_profile=profile,
            execution_profile_definition=profile_ref,
        )

    def _pin_definitions(
        self,
        state: WorkItemState,
        definition_refs: tuple[DefinitionReference, ...],
    ) -> None:
        refs = list(definition_refs)
        if state.execution.definition_refs == refs:
            return
        state.execution.definition_refs = refs
        self.save_state(state)
        self.append_event(
            WorkItemEvent(
                ref=state.ref,
                event_type="execution_definition_pinned",
                created_at=time.time(),
                source="definition-registry",
                reason="execution contract resolved",
                payload={
                    "definition_id": definition_refs[0].definition_id,
                    "kind": definition_refs[0].kind,
                    "revision": definition_refs[0].revision,
                    "record_id": definition_refs[0].record_id,
                    "checksum": definition_refs[0].checksum,
                    "definition_refs": [
                        (
                            f"{item.kind}:{item.definition_id}@r{item.revision}:"
                            f"{item.record_id}:{item.checksum}"
                        )
                        for item in definition_refs
                    ],
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
        (
            role,
            findings,
            catalog,
            definition_ref,
            profile,
            profile_ref,
        ) = self._resolution(state)
        definition_refs = (
            (definition_ref, profile_ref)
            if profile_ref is not None
            else (definition_ref,)
        )
        self._pin_definitions(state, definition_refs)
        contract = execution_contract_for_work_item(
            state,
            role,
            split_brain_findings=findings,
            definition_refs=(
                (definition_ref, profile_ref)
                if profile_ref is not None
                else (definition_ref,)
            ),
            execution_profile=profile,
            execution_profile_definition=profile_ref,
        )
        profile_text = (
            (
                f"Definition Registry revisions ({definition_ref.definition_id}@{definition_ref.revision}, "
                f"{profile_ref.definition_id}@{profile_ref.revision}) were validated before dispatch. "
                f"Execution profile: {profile.id}; workspace mode: {profile.workspace_mode}; "
                f"repository access: {profile.repository_access}; required worker capabilities: "
                f"{', '.join(profile.required_worker_capabilities)}. "
                "The execution profile constrains the environment; it does not grant operational authority. "
            )
            if profile is not None and profile_ref is not None
            else (
                f"Definition Registry revision ({definition_ref.definition_id}@"
                f"{definition_ref.revision}) was validated before dispatch. "
            )
        )
        return (
            f"{security_boundary_instructions()}\n\n"
            f"{untrusted_base}\n\n"
            f"CANONICAL EXECUTION CONTRACT (schema {contract.schema_version})\n"
            "This work item was populated/reconciled from its authoritative task source and codex-web state remains "
            "authoritative for owner, stage, handoff and next action. The versioned execution contract and its exact "
            f"{profile_text}"
            "Apply the resolved role below; do not create parallel ownership, definition or permission state.\n\n"
            f"{assignment_control_plane_instructions()}\n\n"
            "Work-item reads include `assigned_scope`; when canonical "
            "`next_action` is absent it carries the title, body/acceptance criteria, and provenance read through the "
            "configured TaskSource without exposing provider credentials. Orchestrators may wake only the exact "
            "current owner with the catalogued `work_item.steer` operation. Supply the canonical expected owner and "
            "owner-thread ID plus a bounded idempotency key; never call arbitrary `/api/threads` mutations. Active, "
            "queued, stale-owner, stale-thread, cross-Project, and duplicate requests return explicit outcomes or "
            "denials. For a configured coordination owner alias, also supply the exact `expected_definition` "
            "from `owner_dispatch`. The alias selects a role reference and grants no identity or capability. "
            "An aliased handoff ACK requires its exact `expected_handoff_requested_at`, original bound "
            "recipient and scope-verified actual native delivery. Do not infer delivery from queue admission "
            "or rewrite historical recipient evidence after a mapping changes.\n\n"
            f"{execution_contract_prompt(role, catalog=catalog)}"
        )


def install_work_item_contract_service(
    app: Any,
    host: Any | None,
    execution_roles: ExecutionRoleDefinitionService,
    execution_profiles: ExecutionProfileDefinitionService | None = None,
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
        execution_profiles,
        split_brain_findings=split_brain_findings,
        save_state=save_state,
        append_event=append_event,
    )
    app.state.work_item_contract_service = service
    if host is not None:
        host._work_item_execution_contract = service.contract_for_state
        host._work_item_dispatch_text = service.dispatch_text
    return service
