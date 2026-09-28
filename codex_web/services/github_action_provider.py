from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
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
from codex_web.integrations.github_client import GitHubClient
from codex_web.execution_workspaces import (
    ExecutionWorkspaceStatus,
    LeaseMode,
)
from codex_web.resources import Resource, ResourceLifecycle, ResourceType
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService


GITHUB_ACTION_PROVIDER_TYPE = "github"
GITHUB_ACTION_PROVIDER_INSTANCE = "github.com"
CODE_HOST_ISSUE_COMMENT_ACTION_ID = "code-host.issue.comment"
CODE_HOST_ISSUE_UPDATE_ACTION_ID = "code-host.issue.update"
CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID = "code-host.pull-request.upsert"
CODE_HOST_BRANCH_PUBLISH_ACTION_ID = "code-host.branch.publish"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")

BranchPublisher = Callable[[Path, str, str, str, str], Awaitable[None]]


class GitHubActionProvider:
    """Governed GitHub mutations behind ActionIntent/SecretBroker execution."""

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = GITHUB_ACTION_PROVIDER_TYPE

    def __init__(
        self,
        resources: ResourceCatalogService,
        client: GitHubClient | None = None,
        workspaces: ExecutionWorkspaceService | None = None,
        *,
        provider_instance: str = GITHUB_ACTION_PROVIDER_INSTANCE,
        api_base: str = "https://api.github.com",
        branch_publisher: BranchPublisher | None = None,
    ) -> None:
        self.resources = resources
        self.client = client or GitHubClient()
        self.workspaces = workspaces
        self.provider_instance = provider_instance
        self.api_base = api_base.rstrip("/")
        self.branch_publisher = branch_publisher

    def actions(self) -> tuple[ActionDefinition, ...]:
        common = {
            "capabilities": ActionCapability(
                read=False,
                prepare=True,
                execute=True,
                idempotency=True,
                evidence=True,
            ),
            "risk_class": ActionRiskClass.MEDIUM,
            "required_resource_types": (ResourceType.REPOSITORY,),
            "credential_required": True,
            "credential_purpose": "github-api-token",
            "timeout_seconds": 30.0,
            "retry_max_attempts": 3,
            "network_access": True,
        }
        return (
            ActionDefinition(
                action_id=CODE_HOST_ISSUE_COMMENT_ACTION_ID,
                title="Post issue progress comment",
                description="Post one idempotent comment to a GitHub issue.",
                required_authority=("repository.issue.comment",),
                expected_evidence=("github-issue-comment",),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_ISSUE_UPDATE_ACTION_ID,
                title="Update issue state",
                description="Open or close one GitHub issue idempotently.",
                required_authority=("repository.issue.update",),
                expected_evidence=("github-issue-state",),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,
                title="Create or reconcile pull request",
                description=(
                    "Create one GitHub pull request or reconcile the pull request "
                    "already owned by the same ActionIntent idempotency key."
                ),
                required_authority=("repository.pull-request.create",),
                expected_evidence=("github-pull-request",),
                **common,
            ),
            ActionDefinition(
                action_id=CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
                title="Publish execution branch",
                description=(
                    "Publish the validated committed head of a canonical execution "
                    "workspace without exposing credentials to the worker."
                ),
                capabilities=ActionCapability(
                    read=False,
                    prepare=True,
                    execute=True,
                    idempotency=True,
                    evidence=True,
                ),
                risk_class=ActionRiskClass.HIGH,
                required_resource_types=(ResourceType.REPOSITORY,),
                required_authority=("repository.branch.publish",),
                credential_required=True,
                credential_purpose="github-api-token",
                expected_evidence=("github-branch",),
                timeout_seconds=120.0,
                retry_max_attempts=3,
                network_access=True,
                filesystem_access="read",
                process_access=True,
            ),
        )

    def _resource(self, request: ActionRequest) -> Resource:
        if len(request.resource_ids) != 1:
            raise ValueError("GitHub actions require exactly one repository Resource")
        resource = next(
            (
                item
                for item in self.resources.store.load().resources
                if item.id == request.resource_ids[0]
                and item.organization_id == request.organization_id
                and item.workspace_id == request.workspace_id
            ),
            None,
        )
        if resource is None or resource.resource_type != ResourceType.REPOSITORY:
            raise ValueError("GitHub repository Resource was not found in tenant scope")
        if resource.lifecycle != ResourceLifecycle.ACTIVE:
            raise ValueError("GitHub repository Resource is not active")
        return resource

    def _repository(self, request: ActionRequest) -> str:
        resource = self._resource(request)
        aliases = [
            alias.value
            for alias in resource.aliases
            if (alias.provider or "").casefold() == "github"
            and alias.namespace in {"provider", "repository", "github-repository"}
        ]
        provenance = resource.provenance
        if not aliases and provenance and provenance.provider == "github":
            aliases = [str(provenance.external_id or "")]
        repository = aliases[0].strip() if len(aliases) == 1 else ""
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError(
                "GitHub repository Resource needs one validated owner/repository alias"
            )
        return repository

    @staticmethod
    def _issue_number(request: ActionRequest) -> int:
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
    def _comment(request: ActionRequest) -> str:
        unknown = set(request.parameters) - {"issue_number", "body"}
        if unknown:
            raise ValueError("unsupported issue comment parameters")
        body = request.parameters.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("issue comment body is required")
        if len(body) > 65536:
            raise ValueError("issue comment body exceeds GitHub limit")
        return body.strip()

    @staticmethod
    def _state(request: ActionRequest) -> str:
        unknown = set(request.parameters) - {"issue_number", "state"}
        if unknown:
            raise ValueError("unsupported issue update parameters")
        state = str(request.parameters.get("state") or "").strip().casefold()
        if state not in {"open", "closed"}:
            raise ValueError("issue state must be 'open' or 'closed'")
        return state

    @staticmethod
    def _marker(request: ActionRequest) -> str:
        key = str(request.idempotency_key or "").strip()
        if not key:
            raise ValueError("GitHub issue comments require an idempotency key")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"<!-- codex-web-action:{digest} -->"

    @staticmethod
    def _branch(value: Any, *, field: str) -> str:
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
    def _pull_request(cls, request: ActionRequest) -> dict[str, Any]:
        allowed = {"title", "body", "head", "base", "draft"}
        if set(request.parameters) - allowed:
            raise ValueError("unsupported pull request parameters")
        title = request.parameters.get("title")
        body = request.parameters.get("body", "")
        draft = request.parameters.get("draft", False)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("pull request title is required")
        if len(title) > 256:
            raise ValueError("pull request title exceeds limit")
        if not isinstance(body, str) or len(body) > 65536:
            raise ValueError("pull request body must be a bounded string")
        if not isinstance(draft, bool):
            raise ValueError("pull request draft must be a boolean")
        return {
            "title": title.strip(),
            "body": body.strip(),
            "head": cls._branch(request.parameters.get("head"), field="head"),
            "base": cls._branch(request.parameters.get("base"), field="base"),
            "draft": draft,
        }

    def _branch_publication(
        self,
        request: ActionRequest,
    ) -> tuple[Path, str, str]:
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
        branch = self._branch(request.parameters.get("branch"), field="branch")
        revision = str(request.parameters.get("head_revision") or "").strip()
        if not _REVISION.fullmatch(revision):
            raise ValueError("head_revision must be a full Git commit revision")
        workspace = next(
            (
                item
                for item in self.workspaces.store.load().workspaces
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
                for item in self.workspaces.store.load().leases
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
        if member.head_revision != revision:
            raise ValueError("requested revision does not match canonical workspace head")
        if (
            workspace.repository_resource_id == resource_id
            and workspace.head_revision != revision
        ):
            raise ValueError("requested revision does not match primary workspace head")
        root = getattr(self.workspaces.backend, "root", None)
        if root is None:
            raise ValueError("execution workspace backend cannot attest local paths")
        root_path = Path(root).resolve(strict=True)
        workspace_path = Path(member.workspace_path).resolve(strict=True)
        if workspace_path == root_path or not workspace_path.is_relative_to(root_path):
            raise ValueError("execution workspace path escapes canonical backend root")
        actual_head = self._git(workspace_path, "rev-parse", "HEAD")
        actual_branch = self._git(
            workspace_path, "symbolic-ref", "--short", "HEAD"
        )
        if actual_head != revision or actual_branch != branch:
            raise ValueError("workspace Git head does not match requested publication")
        self._git(
            workspace_path,
            "merge-base",
            "--is-ancestor",
            member.base_revision,
            revision,
        )
        if self._git(workspace_path, "status", "--porcelain"):
            raise ValueError("execution workspace has uncommitted changes")
        return workspace_path, branch, revision

    @staticmethod
    def _git(path: Path, *args: str) -> str:
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

    @staticmethod
    def _publish_branch_sync(
        workspace_path: Path,
        repository: str,
        branch: str,
        revision: str,
        credential: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-github-askpass-") as root:
            askpass = Path(root) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' x-access-token ;;\n"
                "  *Password*) printf '%s\\n' \"$CODEX_GITHUB_ACTION_TOKEN\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            environment = dict(os.environ)
            environment.update(
                {
                    "CODEX_GITHUB_ACTION_TOKEN": credential,
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )
            remote = f"https://github.com/{repository}.git"
            try:
                subprocess.run(
                    [
                        "git",
                        "push",
                        "--porcelain",
                        remote,
                        f"{revision}:refs/heads/{branch}",
                    ],
                    cwd=workspace_path,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("GitHub branch publication failed") from exc

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]:
        repository = self._repository(request)
        if request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            number = self._issue_number(request)
            body = self._comment(request)
            return {
                "operation": "issue-comment",
                "repository": repository,
                "issue_number": number,
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "body_characters": len(body),
            }
        if request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            number = self._issue_number(request)
            return {
                "operation": "issue-update",
                "repository": repository,
                "issue_number": number,
                "state": self._state(request),
            }
        if request.action_id == CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID:
            payload = self._pull_request(request)
            return {
                "operation": "pull-request-upsert",
                "repository": repository,
                "head": payload["head"],
                "base": payload["base"],
                "draft": payload["draft"],
                "title_sha256": hashlib.sha256(
                    payload["title"].encode("utf-8")
                ).hexdigest(),
                "body_sha256": hashlib.sha256(
                    payload["body"].encode("utf-8")
                ).hexdigest(),
            }
        if request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            path, branch, revision = self._branch_publication(request)
            return {
                "operation": "branch-publish",
                "repository": repository,
                "execution_workspace_id": request.parameters[
                    "execution_workspace_id"
                ],
                "branch": branch,
                "head_revision": revision,
                "workspace_attested": path.is_dir(),
            }
        raise ValueError("unsupported GitHub action")

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if not credential:
            raise ValueError("GitHub credential is required")
        repository = self._repository(request)
        started = time.time()
        if request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            number = self._issue_number(request)
            body = self._comment(request)
            marker = self._marker(request)
            comments = await self.client.list_issue_comments(
                self.api_base, repository, number, token=credential
            )
            item = next(
                (row for row in comments if marker in str(row.get("body") or "")),
                None,
            )
            if item is None:
                item = await self.client.create_comment(
                    self.api_base,
                    repository,
                    number,
                    token=credential,
                    body=f"{body}\n\n{marker}",
                )
            evidence_type = "github-issue-comment"
            external_id = str(item.get("id") or "") or None
            summary = "GitHub issue comment exists for the action idempotency key."
        elif request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            number = self._issue_number(request)
            state = self._state(request)
            item = await self.client.update_issue(
                self.api_base,
                repository,
                number,
                token=credential,
                payload={"state": state},
            )
            evidence_type = "github-issue-state"
            external_id = str(item.get("id") or number)
            summary = f"GitHub issue state is {state}."
        elif request.action_id == CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID:
            payload = self._pull_request(request)
            marker = self._marker(request)
            pulls = await self.client.list_pull_requests(
                self.api_base,
                repository,
                token=credential,
                head=f"{repository.split('/', 1)[0]}:{payload['head']}",
                base=payload["base"],
            )
            owned = next(
                (row for row in pulls if marker in str(row.get("body") or "")),
                None,
            )
            if owned is None and pulls:
                raise ValueError(
                    "an existing pull request for head/base is not owned by this ActionIntent"
                )
            if owned is None:
                owned = await self.client.create_pull_request(
                    self.api_base,
                    repository,
                    token=credential,
                    payload={
                        **payload,
                        "body": f"{payload['body']}\n\n{marker}".strip(),
                    },
                )
            item = owned
            number = self._positive_response_number(item, field="pull request")
            evidence_type = "github-pull-request"
            external_id = str(item.get("id") or number)
            summary = "GitHub pull request exists for the action idempotency key."
        elif request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            workspace_path, branch, revision = self._branch_publication(request)
            publisher = self.branch_publisher
            if publisher is not None:
                await publisher(
                    workspace_path, repository, branch, revision, credential
                )
            else:
                await asyncio.to_thread(
                    self._publish_branch_sync,
                    workspace_path,
                    repository,
                    branch,
                    revision,
                    credential,
                )
            item = {
                "id": revision,
                "html_url": f"https://github.com/{repository}/tree/{branch}",
            }
            number = 0
            evidence_type = "github-branch"
            external_id = revision
            summary = "GitHub branch points at the committed execution workspace head."
        else:
            raise ValueError("unsupported GitHub action")
        url = str(item.get("html_url") or "") or None
        target_output = (
            {"pull_request_number": number}
            if request.action_id == CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID
            else {"issue_number": number}
        )
        if request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            target_output = {
                "branch": branch,
                "head_revision": revision,
                "execution_workspace_id": request.parameters[
                    "execution_workspace_id"
                ],
            }
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=request.idempotency_key,
            external_id=external_id,
            output={
                "repository": repository,
                **target_output,
                "external_url": url,
            },
            evidence=(
                ActionEvidence(
                    evidence_type=evidence_type,
                    reference=url or f"{repository}#{number}",
                    summary=summary,
                    metadata={
                        "repository": repository,
                        (
                            "branch"
                            if request.action_id
                            == CODE_HOST_BRANCH_PUBLISH_ACTION_ID
                            else "pull_request_number"
                            if request.action_id
                            == CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID
                            else "issue_number"
                        ): (
                            branch
                            if request.action_id
                            == CODE_HOST_BRANCH_PUBLISH_ACTION_ID
                            else number
                        ),
                    },
                ),
            ),
        )

    @staticmethod
    def _positive_response_number(item: dict[str, Any], *, field: str) -> int:
        value = item.get("number")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"GitHub {field} response omitted a valid number")
        return value

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
    ) -> ActionVerification:
        return ActionVerification(
            verified=False,
            evidence=result.evidence,
            findings=("GitHub verification is not declared for this action slice",),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        raise ValueError("GitHub issue actions are not rollback-capable")
