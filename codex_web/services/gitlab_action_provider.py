from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT,
    ActionEvidence,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionVerification,
)
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_EVIDENCE,
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_CHANGE_REQUEST_EVIDENCE,
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_EVIDENCE,
    CODE_HOST_ISSUE_STATE_EVIDENCE,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_JOB_RERUN_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,
    CodeHostActionContract,
    AttestedBranch,
    result_resource_output,
)
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.action_providers import ActionRequirementError


GITLAB_ACTION_PROVIDER_TYPE = "gitlab"
GITLAB_ACTION_PROVIDER_INSTANCE = "gitlab.com"

BranchPublisher = Callable[[Path, str, str, str, str, str | None], Awaitable[None]]


class GitLabActionProvider:
    """Governed GitLab mutations using the shared code-host action contract."""

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = GITLAB_ACTION_PROVIDER_TYPE

    def __init__(
        self,
        resources: ResourceCatalogService,
        client: GitLabClient | None = None,
        workspaces: ExecutionWorkspaceService | None = None,
        *,
        provider_instance: str = GITLAB_ACTION_PROVIDER_INSTANCE,
        api_base: str = "https://gitlab.com/api/v4",
        web_base: str | None = None,
        branch_publisher: BranchPublisher | None = None,
    ) -> None:
        self.client = client or GitLabClient()
        self.provider_instance = provider_instance
        self.api_base = self._https_base(api_base, field="api_base")
        derived_web = (
            self.api_base.removesuffix("/api/v4")
            if self.api_base.endswith("/api/v4")
            else None
        )
        self.web_base = self._https_base(
            web_base or derived_web or "",
            field="web_base",
        )
        if urlparse(self.api_base).netloc != urlparse(self.web_base).netloc:
            raise ValueError("GitLab API and Web bases must use the same authority")
        if urlparse(self.web_base).netloc != provider_instance:
            raise ValueError("GitLab Web base must match provider_instance")
        self.branch_publisher = branch_publisher
        self.contract = CodeHostActionContract(
            resources,
            workspaces,
            provider_type=self.provider_type,
            provider_label="GitLab",
            credential_purpose="gitlab-api-token",
            alias_namespaces=("provider", "repository", "gitlab-project"),
        )

    @staticmethod
    def _https_base(value: str, *, field: str) -> str:
        parsed = urlparse(str(value or "").strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"GitLab {field} must be an HTTPS URL without credentials")
        return str(value).rstrip("/")

    def actions(self):
        return tuple(
            action
            for action in self.contract.actions()
            if action.action_id != CODE_HOST_JOB_RERUN_ACTION_ID
        )

    @staticmethod
    def _owned_body(body: str, marker: str) -> str:
        return f"{body}\n\n{marker}".strip()

    @staticmethod
    def _positive_iid(item: dict[str, Any], *, field: str) -> int:
        value = item.get("iid")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"GitLab {field} response omitted a valid iid")
        return value

    def _publish_branch_sync(
        self,
        workspace_path: Path,
        project: str,
        branch: str,
        revision: str,
        credential: str,
        expected_remote_revision: str | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-gitlab-askpass-") as root:
            askpass = Path(root) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' oauth2 ;;\n"
                "  *Password*) printf '%s\\n' \"$CODEX_GITLAB_ACTION_TOKEN\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            environment = dict(os.environ)
            environment.update(
                {
                    "CODEX_GITLAB_ACTION_TOKEN": credential,
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )
            try:
                command = [
                        "git",
                        "push",
                        "--porcelain",
                    ]
                if expected_remote_revision is not None:
                    command.append(
                        "--force-with-lease="
                        f"refs/heads/{branch}:{expected_remote_revision}"
                    )
                command.extend(
                    [
                        f"{self.web_base}/{project}.git",
                        f"{revision}:refs/heads/{branch}",
                    ]
                )
                subprocess.run(
                    command,
                    cwd=workspace_path,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                subprocess.run(
                    [
                        "git",
                        "update-ref",
                        f"refs/remotes/codex-web-published/{branch}",
                        revision,
                    ],
                    cwd=workspace_path,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("GitLab branch publication failed") from exc

    async def _attest_existing_merge_request(
        self, attested: AttestedBranch, project: str, credential: str,
    ) -> None:
        iid = attested.existing_change_request_number
        mr = await self.client.merge_request(self.api_base, project, iid, token=credential)
        issue = await self.client.project_issue(
            self.api_base, project, attested.work_item_issue_number, token=credential,
        )
        closes = await self.client.merge_request_closes_issues(
            self.api_base, project, iid, token=credential,
        )
        remote = await self.client.branch(
            self.api_base, project, attested.branch, token=credential,
        )
        project_id = issue.get("project_id")
        if (
            type(project_id) is not int
            or project_id < 1
            or issue.get("iid") != attested.work_item_issue_number
            or type(issue.get("iid")) is not int
            or type(mr.get("iid")) is not int
            or mr.get("iid") != iid
            or mr.get("state") != "opened"
            or type(mr.get("source_project_id")) is not int
            or type(mr.get("target_project_id")) is not int
            or mr.get("source_project_id") != project_id
            or mr.get("target_project_id") != project_id
            or mr.get("source_branch") != attested.branch
            or mr.get("sha") != attested.expected_remote_revision
            or not isinstance(mr.get("target_branch"), str)
            or not mr.get("target_branch")
            or mr.get("target_branch") == attested.branch
            or remote.get("protected") is not False
            or remote.get("default") is not False
            or remote.get("name") != attested.branch
            or (remote.get("commit") or {}).get("id") != attested.expected_remote_revision
            or not any(
                type(row.get("iid")) is int
                and row.get("iid") == attested.work_item_issue_number
                and type(row.get("project_id")) is int
                and row.get("project_id") == project_id
                for row in closes
            )
        ):
            raise ActionRequirementError(
                "existing merge request source, current revision and assigned issue relation could not be attested"
            )

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]:
        project = self.contract.locator(request)
        if request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            body = self.contract.comment(request)
            return {
                "operation": "issue-comment",
                "repository": project,
                "issue_number": self.contract.issue_number(request),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "body_characters": len(body),
            }
        if request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            return {
                "operation": "issue-update",
                "repository": project,
                "issue_number": self.contract.issue_number(request),
                "state": self.contract.issue_state(request),
            }
        if request.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            payload = self.contract.change_request(request)
            return {
                "operation": "change-request-upsert",
                "repository": project,
                "head": payload["head"],
                "base": payload["base"],
                "draft": payload["draft"],
                "title_sha256": hashlib.sha256(payload["title"].encode()).hexdigest(),
                "body_sha256": hashlib.sha256(payload["body"].encode()).hexdigest(),
            }
        if request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            attested = self.contract.attest_branch(request)
            return {
                "operation": "branch-publish",
                "repository": project,
                "execution_workspace_id": attested.workspace_id,
                "branch": attested.branch,
                "head_revision": attested.revision,
                "workspace_attested": attested.workspace_path.is_dir(),
                "existing_change_request_number": attested.existing_change_request_number,
                "work_item_issue_number": attested.work_item_issue_number,
            }
        if request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            payload = self.contract.pull_request_merge(request)
            return {
                "operation": "pull-request-merge",
                "repository": project,
                "pull_request_number": payload["number"],
                "merge_method": payload["merge_method"],
                "expected_head_sha": payload["expected_head_sha"],
            }
        raise ValueError("unsupported GitLab action")

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if not credential:
            raise ValueError("GitLab credential is required")
        project = self.contract.locator(request)
        started = time.time()
        output: dict[str, Any] = {
            "repository": project,
            **result_resource_output(request),
        }
        if request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            iid = self.contract.issue_number(request)
            marker = self.contract.marker(request)
            notes = await self.client.project_issue_notes(
                self.api_base, project, iid, token=credential
            )
            item = next(
                (row for row in notes if marker in str(row.get("body") or "")),
                None,
            )
            if item is None:
                desired_body = self._owned_body(
                    self.contract.comment(request), marker
                )
                item = await self.client.create_project_issue_note(
                    self.api_base,
                    project,
                    iid,
                    token=credential,
                    body=desired_body,
                )
            else:
                desired_body = self._owned_body(
                    self.contract.comment(request), marker
                )
            evidence_type = CODE_HOST_ISSUE_COMMENT_EVIDENCE
            external_id = str(item.get("id") or "") or None
            url = str(item.get("web_url") or "") or None
            output.update(
                {
                    "issue_number": iid,
                    "comment_id": external_id,
                    "body_sha256": hashlib.sha256(
                        desired_body.encode()
                    ).hexdigest(),
                }
            )
            summary = "GitLab issue note is owned by this action idempotency key."
        elif request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            iid = self.contract.issue_number(request)
            state = self.contract.issue_state(request)
            expected = "opened" if state == "open" else "closed"
            current = await self.client.project_issue(
                self.api_base, project, iid, token=credential
            )
            item = current
            if str(current.get("state") or "").casefold() != expected:
                item = await self.client.update_project_issue(
                    self.api_base,
                    project,
                    iid,
                    token=credential,
                    payload={"state_event": "reopen" if state == "open" else "close"},
                )
            evidence_type = CODE_HOST_ISSUE_STATE_EVIDENCE
            external_id = str(item.get("id") or iid)
            url = str(item.get("web_url") or "") or None
            output.update({"issue_number": iid, "state": state})
            summary = f"GitLab issue state is {state}."
        elif request.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            # Only this pure first-party parser runs before any MR API call.
            # A ValueError from a later provider read/write is still ambiguous.
            try:
                payload = self.contract.change_request(request)
            except ValueError as exc:
                raise ActionRequirementError(
                    f"{exc}; supported change request parameters: "
                    "title, body, head, base, draft"
                ) from exc
            marker = self.contract.marker(request)
            desired_body = self._owned_body(payload["body"], marker)
            merge_requests = await self.client.merge_requests(
                self.api_base,
                project,
                token=credential,
                source_branch=payload["head"],
                target_branch=payload["base"],
            )
            owned = next(
                (
                    row
                    for row in merge_requests
                    if marker in str(row.get("description") or "")
                ),
                None,
            )
            if owned is None and merge_requests:
                raise ValueError(
                    "an existing merge request for head/base is not owned by this ActionIntent"
                )
            api_payload = {
                "title": payload["title"],
                "description": desired_body,
                "source_branch": payload["head"],
                "target_branch": payload["base"],
                "draft": payload["draft"],
            }
            if owned is None:
                item = await self.client.create_merge_request(
                    self.api_base,
                    project,
                    token=credential,
                    payload=api_payload,
                )
            else:
                iid = self._positive_iid(owned, field="merge request")
                changed = any(
                    (
                        str(owned.get("title") or "") != payload["title"],
                        str(owned.get("description") or "") != desired_body,
                        str(owned.get("target_branch") or "") != payload["base"],
                        bool(owned.get("draft")) != payload["draft"],
                    )
                )
                item = (
                    await self.client.update_merge_request(
                        self.api_base,
                        project,
                        iid,
                        token=credential,
                        payload={
                            "title": api_payload["title"],
                            "description": api_payload["description"],
                            "target_branch": api_payload["target_branch"],
                            "draft": api_payload["draft"],
                        },
                    )
                    if changed
                    else owned
                )
            iid = self._positive_iid(item, field="merge request")
            evidence_type = CODE_HOST_CHANGE_REQUEST_EVIDENCE
            external_id = str(item.get("id") or iid)
            url = str(item.get("web_url") or "") or None
            output.update(
                {
                    "change_request_number": iid,
                    "head": payload["head"],
                    "base": payload["base"],
                    "draft": payload["draft"],
                    "title_sha256": hashlib.sha256(
                        payload["title"].encode()
                    ).hexdigest(),
                    "body_sha256": hashlib.sha256(
                        desired_body.encode()
                    ).hexdigest(),
                }
            )
            summary = "GitLab merge request is owned and reconciled by this action."
        elif request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            try:
                attested = self.contract.attest_branch(request)
            except ValueError as exc:
                raise ActionRequirementError(str(exc)) from exc
            if attested.existing_change_request_number is not None:
                await self._attest_existing_merge_request(attested, project, credential)
                # Provider reads may outlive the lease or overlap workspace mutation.
                # Re-attest every local guard after the remote proof, before push.
                if self.contract.attest_branch(request) != attested:
                    raise ActionRequirementError("execution workspace attestation changed during existing merge request verification")
            if self.branch_publisher is not None:
                await self.branch_publisher(
                    attested.workspace_path,
                    project,
                    attested.branch,
                    attested.revision,
                    credential,
                    attested.expected_remote_revision,
                )
            else:
                await asyncio.to_thread(
                    self._publish_branch_sync,
                    attested.workspace_path,
                    project,
                    attested.branch,
                    attested.revision,
                    credential,
                    attested.expected_remote_revision,
                )
            evidence_type = CODE_HOST_BRANCH_EVIDENCE
            external_id = attested.revision
            url = (
                f"{self.web_base}/{project}/-/tree/"
                f"{quote(attested.branch, safe='/')}"
            )
            output.update(
                {
                    "branch": attested.branch,
                    "head_revision": attested.revision,
                    "execution_workspace_id": attested.workspace_id,
                    "existing_change_request_number": attested.existing_change_request_number,
                    "work_item_issue_number": attested.work_item_issue_number,
                }
            )
            summary = "GitLab branch matches the committed execution workspace head."
        elif request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            payload = self.contract.pull_request_merge(request)
            if payload["merge_method"] == "rebase":
                raise ValueError(
                    "GitLab rebase-and-merge is not supported atomically"
                )
            iid = payload["number"]
            current = await self.client.merge_request(
                self.api_base, project, iid, token=credential
            )
            current_sha = str(current.get("sha") or "").casefold()
            expected_head_sha = payload["expected_head_sha"]
            if expected_head_sha and current_sha != expected_head_sha:
                raise ValueError(
                    "GitLab merge request head no longer matches expected_head_sha"
                )
            if str(current.get("state") or "").casefold() != "merged":
                if bool(current.get("draft")) or bool(
                    current.get("work_in_progress")
                ):
                    raise ValueError("GitLab merge request is still a draft")
                detailed_status = str(
                    current.get("detailed_merge_status") or ""
                ).casefold()
                merge_status = str(
                    current.get("merge_status") or ""
                ).casefold()
                if detailed_status and detailed_status != "mergeable":
                    raise ValueError(
                        "GitLab merge request is not clean and mergeable"
                    )
                if merge_status and merge_status != "can_be_merged":
                    raise ValueError(
                        "GitLab merge request is not clean and mergeable"
                    )
                pipeline = current.get("head_pipeline")
                if not isinstance(pipeline, dict) or str(
                    pipeline.get("status") or ""
                ).casefold() != "success":
                    raise ValueError(
                        "GitLab merge request head pipeline is not successful"
                    )
                merged = await self.client.accept_merge_request(
                    self.api_base,
                    project,
                    iid,
                    token=credential,
                    payload={
                        "sha": current_sha,
                        "squash": payload["merge_method"] == "squash",
                    },
                )
                if str(merged.get("state") or "").casefold() != "merged":
                    raise ValueError("GitLab declined the merge request merge")
                merge_sha = str(merged.get("merge_commit_sha") or "")
            else:
                merge_sha = str(current.get("merge_commit_sha") or "")
            evidence_type = CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE
            external_id = str(iid)
            url = str(current.get("web_url") or "") or None
            output.update(
                {
                    "pull_request_number": iid,
                    "head_sha": current_sha,
                    "merge_commit_sha": merge_sha,
                }
            )
            summary = (
                "GitLab merge request is merged after clean pipeline validation."
            )
        else:
            raise ValueError("unsupported GitLab action")
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="succeeded",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=request.idempotency_key,
            external_id=external_id,
            output={**output, "external_url": url},
            evidence=(
                ActionEvidence(
                    evidence_type=evidence_type,
                    reference=url or external_id,
                    summary=summary,
                    metadata={
                        "resource_id": request.resource_ids[0],
                        "repository": project,
                    },
                ),
            ),
        )

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionVerification:
        if not credential:
            raise ValueError("GitLab credential is required for verification")
        if result.provider_binding_id != binding.id:
            raise ValueError("GitLab result binding does not match verification binding")
        project = self.contract.locator_for_result(result, binding)
        verified = False
        if result.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            iid = int(result.output["issue_number"])
            marker = self.contract.marker_from_key(result.idempotency_key)
            notes = await self.client.project_issue_notes(
                self.api_base, project, iid, token=credential
            )
            verified = any(
                marker in str(item.get("body") or "")
                and hashlib.sha256(
                    str(item.get("body") or "").encode()
                ).hexdigest()
                == result.output["body_sha256"]
                and (
                    result.external_id is None
                    or str(item.get("id") or "") == result.external_id
                )
                for item in notes
            )
        elif result.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            item = await self.client.project_issue(
                self.api_base,
                project,
                int(result.output["issue_number"]),
                token=credential,
            )
            expected = "opened" if result.output["state"] == "open" else "closed"
            verified = str(item.get("state") or "").casefold() == expected
        elif result.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            iid = int(result.output["change_request_number"])
            item = await self.client.merge_request(
                self.api_base, project, iid, token=credential
            )
            marker = self.contract.marker_from_key(result.idempotency_key)
            verified = (
                self._positive_iid(item, field="merge request") == iid
                and marker in str(item.get("description") or "")
                and str(item.get("source_branch") or "") == result.output["head"]
                and str(item.get("target_branch") or "") == result.output["base"]
                and bool(item.get("draft")) == bool(result.output["draft"])
                and hashlib.sha256(
                    str(item.get("title") or "").encode()
                ).hexdigest()
                == result.output["title_sha256"]
                and hashlib.sha256(
                    str(item.get("description") or "").encode()
                ).hexdigest()
                == result.output["body_sha256"]
            )
        elif result.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            item = await self.client.branch(
                self.api_base,
                project,
                str(result.output["branch"]),
                token=credential,
            )
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            verified = str(commit.get("id") or "") == str(
                result.output["head_revision"]
            )
        elif result.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            item = await self.client.merge_request(
                self.api_base,
                project,
                int(result.output["pull_request_number"]),
                token=credential,
            )
            verified = str(item.get("state") or "").casefold() == "merged" and (
                not result.output.get("merge_commit_sha")
                or str(item.get("merge_commit_sha") or "")
                == str(result.output["merge_commit_sha"])
            ) and (
                not result.output.get("head_sha")
                or str(item.get("sha") or "").casefold()
                == str(result.output["head_sha"]).casefold()
            )
        else:
            raise ValueError("unsupported GitLab action result")
        evidence = (
            ActionEvidence(
                evidence_type=self.contract.evidence_type(result.action_id),
                reference=str(result.output.get("external_url") or "") or None,
                summary=(
                    "GitLab provider state independently matches the action receipt."
                    if verified
                    else "GitLab provider state differs from the action receipt."
                ),
                metadata={
                    "resource_id": str(result.output["resource_id"]),
                    "repository": project,
                    "verified": verified,
                },
            ),
        )
        return ActionVerification(
            verified=verified,
            evidence=evidence,
            findings=(
                ()
                if verified
                else ("GitLab provider state does not match the action receipt",)
            ),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        raise ValueError("GitLab code-host actions are not rollback-capable")
