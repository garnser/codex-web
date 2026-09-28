from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_web.action_providers import (
    ActionCapability,
    ActionDefinition,
    ActionProviderBinding,
    ActionRequest,
    ActionRiskClass,
    ActionResult,
)
from codex_web.execution_workspaces import ExecutionWorkspaceStatus, LeaseMode
from codex_web.resources import Resource, ResourceLifecycle, ResourceType
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.resources import ResourceCatalogService


CODE_HOST_ISSUE_COMMENT_ACTION_ID = "code-host.issue.comment"
CODE_HOST_ISSUE_CREATE_ACTION_ID = "code-host.issue.create"
CODE_HOST_ISSUE_UPDATE_ACTION_ID = "code-host.issue.update"
# The durable ID predates GitLab support. It remains stable while the contract
# normalizes the operation as a provider-neutral change request.
CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID = "code-host.pull-request.upsert"
CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID = CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID
CODE_HOST_BRANCH_PUBLISH_ACTION_ID = "code-host.branch.publish"
CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID = "code-host.pull-request.merge"

CODE_HOST_ISSUE_COMMENT_EVIDENCE = "code-host-issue-comment"
CODE_HOST_ISSUE_CREATE_EVIDENCE = "code-host-issue"
CODE_HOST_ISSUE_STATE_EVIDENCE = "code-host-issue-state"
CODE_HOST_CHANGE_REQUEST_EVIDENCE = "code-host-change-request"
CODE_HOST_BRANCH_EVIDENCE = "code-host-branch"
CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE = "code-host-pull-request-merge"

_LOCATOR_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class AttestedBranch:
    workspace_path: Path
    workspace_id: str
    branch: str
    revision: str


class CodeHostActionContract:
    """Provider-neutral schema and canonical scope checks for code-host writes."""

    def __init__(
        self,
        resources: ResourceCatalogService,
        workspaces: ExecutionWorkspaceService | None,
        *,
        provider_type: str,
        provider_label: str,
        credential_purpose: str,
        alias_namespaces: tuple[str, ...],
    ) -> None:
        self.resources = resources
        self.workspaces = workspaces
        self.provider_type = provider_type
        self.provider_label = provider_label
        self.credential_purpose = credential_purpose
        self.alias_namespaces = alias_namespaces

    def actions(self) -> tuple[ActionDefinition, ...]:
        capabilities = ActionCapability(
            read=False,
            prepare=True,
            execute=True,
            idempotency=True,
            verification=True,
            evidence=True,
        )
        common = {
            "capabilities": capabilities,
            "risk_class": ActionRiskClass.MEDIUM,
            "required_resource_types": (ResourceType.REPOSITORY,),
            "credential_required": True,
            "credential_purpose": self.credential_purpose,
            "timeout_seconds": 30.0,
            "retry_max_attempts": 3,
            "network_access": True,
        }
        return (
            ActionDefinition(
                action_id=CODE_HOST_ISSUE_CREATE_ACTION_ID,
                title="Create issue",
                description="Create one idempotently owned code-host issue.",
                required_authority=("repository.issue.create",),
                expected_evidence=(CODE_HOST_ISSUE_CREATE_EVIDENCE,),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_ISSUE_COMMENT_ACTION_ID,
                title="Post issue progress comment",
                description="Post one idempotent comment to a code-host issue.",
                required_authority=("repository.issue.comment",),
                expected_evidence=(CODE_HOST_ISSUE_COMMENT_EVIDENCE,),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_ISSUE_UPDATE_ACTION_ID,
                title="Update issue state",
                description="Open or close one code-host issue idempotently.",
                required_authority=("repository.issue.update",),
                expected_evidence=(CODE_HOST_ISSUE_STATE_EVIDENCE,),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
                title="Create or reconcile change request",
                description=(
                    "Create or update one pull/merge request owned by the same "
                    "ActionIntent idempotency key."
                ),
                required_authority=("repository.pull-request.create",),
                expected_evidence=(CODE_HOST_CHANGE_REQUEST_EVIDENCE,),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
                title="Publish execution branch",
                description=(
                    "Publish the validated committed head of a canonical isolated "
                    "execution workspace."
                ),
                capabilities=capabilities,
                risk_class=ActionRiskClass.HIGH,
                required_resource_types=(ResourceType.REPOSITORY,),
                required_authority=("repository.branch.publish",),
                credential_required=True,
                credential_purpose=self.credential_purpose,
                expected_evidence=(CODE_HOST_BRANCH_EVIDENCE,),
                timeout_seconds=120.0,
                retry_max_attempts=3,
                network_access=True,
                filesystem_access="read",
                process_access=True,
            ),
            ActionDefinition(
                action_id=CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
                title="Merge pull request",
                description=(
                    "Merge one pull request only after GitHub reports it clean and mergeable."
                ),
                capabilities=capabilities,
                risk_class=ActionRiskClass.HIGH,
                required_resource_types=(ResourceType.REPOSITORY,),
                required_authority=("repository.pull-request.merge",),
                credential_required=True,
                credential_purpose=self.credential_purpose,
                expected_evidence=(CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,),
                timeout_seconds=30.0,
                retry_max_attempts=1,
                network_access=True,
            ),
        )

    @staticmethod
    def evidence_type(action_id: str) -> str:
        evidence_types = {
            CODE_HOST_ISSUE_COMMENT_ACTION_ID: CODE_HOST_ISSUE_COMMENT_EVIDENCE,
            CODE_HOST_ISSUE_CREATE_ACTION_ID: CODE_HOST_ISSUE_CREATE_EVIDENCE,
            CODE_HOST_ISSUE_UPDATE_ACTION_ID: CODE_HOST_ISSUE_STATE_EVIDENCE,
            CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID: (
                CODE_HOST_CHANGE_REQUEST_EVIDENCE
            ),
            CODE_HOST_BRANCH_PUBLISH_ACTION_ID: CODE_HOST_BRANCH_EVIDENCE,
            CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID: (
                CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE
            ),
        }
        try:
            return evidence_types[action_id]
        except KeyError as exc:
            raise ValueError("unsupported code-host action") from exc

    def resource(self, request: ActionRequest) -> Resource:
        if len(request.resource_ids) != 1:
            raise ValueError(
                f"{self.provider_label} actions require exactly one repository Resource"
            )
        return self._resource_id(
            request.resource_ids[0],
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        )

    def resource_for_result(
        self,
        result: ActionResult,
        binding: ActionProviderBinding,
    ) -> Resource:
        resource_id = str(result.output.get("resource_id") or "").strip()
        if not resource_id:
            raise ValueError("code-host action result omitted canonical resource_id")
        if binding.resource_ids and resource_id not in binding.resource_ids:
            raise ValueError("code-host result resource is outside provider binding")
        return self._resource_id(
            resource_id,
            organization_id=binding.organization_id,
            workspace_id=binding.workspace_id,
        )

    def _resource_id(
        self,
        resource_id: str,
        *,
        organization_id: str,
        workspace_id: str,
    ) -> Resource:
        resource = next(
            (
                item
                for item in self.resources.store.load().resources
                if item.id == resource_id
                and item.organization_id == organization_id
                and item.workspace_id == workspace_id
            ),
            None,
        )
        if resource is None or resource.resource_type != ResourceType.REPOSITORY:
            raise ValueError(
                f"{self.provider_label} repository Resource was not found in tenant scope"
            )
        if resource.lifecycle != ResourceLifecycle.ACTIVE:
            raise ValueError(f"{self.provider_label} repository Resource is not active")
        return resource

    def locator(self, request: ActionRequest) -> str:
        return self.locator_for_resource(self.resource(request))

    def locator_for_result(
        self,
        result: ActionResult,
        binding: ActionProviderBinding,
    ) -> str:
        resource = self.resource_for_result(result, binding)
        expected = self.locator_for_resource(resource)
        observed = str(result.output.get("repository") or "").strip()
        if observed != expected:
            raise ValueError("code-host result repository does not match canonical Resource")
        return expected

    def locator_for_resource(self, resource: Resource) -> str:
        aliases = [
            alias.value.strip()
            for alias in resource.aliases
            if (alias.provider or "").casefold() == self.provider_type
            and alias.namespace in self.alias_namespaces
        ]
        provenance = resource.provenance
        if (
            not aliases
            and provenance
            and (provenance.provider or "").casefold() == self.provider_type
        ):
            aliases = [str(provenance.external_id or "").strip()]
        locator = aliases[0] if len(aliases) == 1 else ""
        segments = locator.split("/")
        if (
            len(segments) < 2
            or any(not _LOCATOR_SEGMENT.fullmatch(item) for item in segments)
        ):
            raise ValueError(
                f"{self.provider_label} repository Resource needs one validated "
                "namespace/project alias"
            )
        return locator

    @staticmethod
    def issue_number(request: ActionRequest) -> int:
        value = request.parameters.get("issue_number")
        if isinstance(value, bool):
            raise ValueError("issue_number must be a positive integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("issue_number must be a positive integer") from exc
        if number < 1 or str(number) != str(value).strip():
            raise ValueError("issue_number must be a positive integer")
        return number

    @staticmethod
    def comment(request: ActionRequest) -> str:
        if set(request.parameters) - {"issue_number", "body"}:
            raise ValueError("unsupported issue comment parameters")
        body = request.parameters.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("issue comment body is required")
        if len(body) > 65536:
            raise ValueError("issue comment body exceeds limit")
        return body.strip()

    @staticmethod
    def issue_create(request: ActionRequest) -> dict[str, str]:
        if set(request.parameters) - {"title", "body"}:
            raise ValueError("unsupported issue create parameters")
        title = request.parameters.get("title")
        body = request.parameters.get("body", "")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("issue title is required")
        if len(title) > 256:
            raise ValueError("issue title exceeds limit")
        if not isinstance(body, str) or len(body) > 65536:
            raise ValueError("issue body must be a bounded string")
        return {"title": title.strip(), "body": body.strip()}

    @staticmethod
    def issue_state(request: ActionRequest) -> str:
        if set(request.parameters) - {"issue_number", "state"}:
            raise ValueError("unsupported issue update parameters")
        state = str(request.parameters.get("state") or "").strip().casefold()
        if state not in {"open", "closed"}:
            raise ValueError("issue state must be 'open' or 'closed'")
        return state

    @staticmethod
    def marker_from_key(idempotency_key: str | None) -> str:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("code-host mutations require an idempotency key")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"<!-- codex-web-action:{digest} -->"

    @classmethod
    def marker(cls, request: ActionRequest) -> str:
        return cls.marker_from_key(request.idempotency_key)

    @staticmethod
    def branch(value: Any, *, field: str) -> str:
        branch = str(value or "").strip()
        if (
            not branch
            or len(branch) > 255
            or branch.startswith("-")
            or branch.endswith(".")
            or ".." in branch
            or "@{" in branch
            or any(character.isspace() for character in branch)
            or any(character in branch for character in "~^:?*[\\")
        ):
            raise ValueError(f"{field} is not a safe Git branch name")
        return branch

    @classmethod
    def change_request(cls, request: ActionRequest) -> dict[str, Any]:
        if set(request.parameters) - {"title", "body", "head", "base", "draft"}:
            raise ValueError("unsupported change request parameters")
        title = request.parameters.get("title")
        body = request.parameters.get("body", "")
        draft = request.parameters.get("draft", False)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("change request title is required")
        if len(title) > 256:
            raise ValueError("change request title exceeds limit")
        if not isinstance(body, str) or len(body) > 65536:
            raise ValueError("change request body must be a bounded string")
        if not isinstance(draft, bool):
            raise ValueError("change request draft must be a boolean")
        return {
            "title": title.strip(),
            "body": body.strip(),
            "head": cls.branch(request.parameters.get("head"), field="head"),
            "base": cls.branch(request.parameters.get("base"), field="base"),
            "draft": draft,
        }

    @classmethod
    def pull_request_merge(cls, request: ActionRequest) -> dict[str, Any]:
        if set(request.parameters) - {"pull_request_number", "merge_method"}:
            raise ValueError("unsupported pull request merge parameters")
        value = request.parameters.get("pull_request_number")
        if isinstance(value, bool):
            raise ValueError("pull_request_number must be a positive integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("pull_request_number must be a positive integer") from exc
        if number < 1 or str(number) != str(value).strip():
            raise ValueError("pull_request_number must be a positive integer")
        method = str(request.parameters.get("merge_method") or "squash").strip()
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("merge_method must be merge, squash, or rebase")
        return {"number": number, "merge_method": method}

    def attest_branch(self, request: ActionRequest) -> AttestedBranch:
        if set(request.parameters) - {
            "execution_workspace_id",
            "branch",
            "head_revision",
        }:
            raise ValueError("unsupported branch publication parameters")
        if self.workspaces is None:
            raise ValueError("execution workspace service is unavailable")
        workspace_id = str(
            request.parameters.get("execution_workspace_id") or ""
        ).strip()
        if not workspace_id:
            raise ValueError("execution_workspace_id is required")
        branch = self.branch(request.parameters.get("branch"), field="branch")
        revision = str(request.parameters.get("head_revision") or "").strip()
        if not _REVISION.fullmatch(revision):
            raise ValueError("head_revision must be a full Git commit revision")
        state = self.workspaces.store.load()
        workspace = next(
            (
                item
                for item in state.workspaces
                if item.id == workspace_id
                and item.organization_id == request.organization_id
                and item.workspace_id == request.workspace_id
            ),
            None,
        )
        if workspace is None or workspace.status != ExecutionWorkspaceStatus.ACTIVE:
            raise ValueError("active execution workspace was not found in tenant scope")
        lease = next(
            (
                item
                for item in state.leases
                if item.id == workspace.lease_id and item.active
            ),
            None,
        )
        if lease is None:
            raise ValueError("execution workspace lease is not active")
        resource_id = request.resource_ids[0]
        member = next(
            (
                item
                for item in workspace.repository_members
                if item.resource_id == resource_id
                and item.access_mode == LeaseMode.WRITE
            ),
            None,
        )
        if member is None:
            raise ValueError("repository is not writable in the execution workspace")
        if member.branch_name != branch:
            raise ValueError("requested branch does not match canonical workspace branch")
        root = getattr(self.workspaces.backend, "root", None)
        if root is None:
            raise ValueError("execution workspace backend cannot attest local paths")
        root_path = Path(root).resolve(strict=True)
        workspace_path = Path(member.workspace_path).resolve(strict=True)
        if workspace_path == root_path or not workspace_path.is_relative_to(root_path):
            raise ValueError("execution workspace path escapes canonical backend root")
        actual_head = self.git(workspace_path, "rev-parse", "HEAD")
        actual_branch = self.git(
            workspace_path,
            "symbolic-ref",
            "--short",
            "HEAD",
        )
        if actual_head != revision or actual_branch != branch:
            raise ValueError("workspace Git head does not match requested publication")
        self.git(
            workspace_path,
            "merge-base",
            "--is-ancestor",
            member.base_revision,
            revision,
        )
        if self.git(workspace_path, "status", "--porcelain"):
            raise ValueError("execution workspace has uncommitted changes")
        return AttestedBranch(
            workspace_path=workspace_path,
            workspace_id=workspace_id,
            branch=branch,
            revision=revision,
        )

    @staticmethod
    def git(path: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("execution workspace Git validation failed") from exc
        return result.stdout.strip()


def result_resource_output(request: ActionRequest) -> dict[str, str]:
    return {"resource_id": request.resource_ids[0]}
