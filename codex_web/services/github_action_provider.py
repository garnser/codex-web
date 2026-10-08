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
from codex_web.integrations.github_client import GitHubClient
from codex_web.services.code_host_action_contract import (
    CODE_HOST_BRANCH_EVIDENCE,
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_CHANGE_REQUEST_EVIDENCE,
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_EVIDENCE,
    CODE_HOST_ISSUE_COMMENT_EVIDENCE,
    CODE_HOST_ISSUE_STATE_EVIDENCE,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_JOB_RERUN_ACTION_ID,
    CODE_HOST_JOB_RERUN_EVIDENCE,
    CODE_HOST_PULL_REQUEST_UPSERT_ACTION_ID,  # noqa: F401 - compatibility export
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,
    CodeHostActionContract,
    result_resource_output,
)
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.action_providers import ActionRequirementError


GITHUB_ACTION_PROVIDER_TYPE = "github"
GITHUB_ACTION_PROVIDER_INSTANCE = "github.com"

BranchPublisher = Callable[[Path, str, str, str, str, str | None], Awaitable[None]]


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
        web_base: str = "https://github.com",
        branch_publisher: BranchPublisher | None = None,
    ) -> None:
        self.client = client or GitHubClient()
        self.provider_instance = provider_instance
        self.api_base = self._https_base(api_base, field="api_base")
        self.web_base = self._https_base(web_base, field="web_base")
        api_authority = urlparse(self.api_base).netloc
        web_authority = urlparse(self.web_base).netloc
        if web_authority != provider_instance:
            raise ValueError("GitHub Web base must match provider_instance")
        if api_authority not in {web_authority, f"api.{web_authority}"}:
            raise ValueError("GitHub API base does not match provider authority")
        self.branch_publisher = branch_publisher
        self.contract = CodeHostActionContract(
            resources,
            workspaces,
            provider_type=self.provider_type,
            provider_label="GitHub",
            credential_purpose="github-api-token",
            alias_namespaces=("provider", "repository", "github-repository"),
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
            raise ValueError(f"GitHub {field} must be an HTTPS URL without credentials")
        return str(value).rstrip("/")

    def actions(self):
        return self.contract.actions()

    @staticmethod
    def _owned_body(body: str, marker: str) -> str:
        return f"{body}\n\n{marker}".strip()

    @staticmethod
    def _positive_number(item: dict[str, Any], *, field: str) -> int:
        value = item.get("number")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"GitHub {field} response omitted a valid number")
        return value

    @staticmethod
    def _ref(item: dict[str, Any], name: str) -> str:
        value = item.get(name)
        if isinstance(value, dict):
            return str(value.get("ref") or "")
        return str(value or "")

    def _publish_branch_sync(
        self,
        workspace_path: Path,
        repository: str,
        branch: str,
        revision: str,
        credential: str,
        expected_remote_revision: str | None = None,
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
                        f"{self.web_base}/{repository}.git",
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
                raise RuntimeError("GitHub branch publication failed") from exc

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]:
        repository = self.contract.locator(request)
        if request.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            payload = self.contract.issue_create(request)
            return {
                "operation": "issue-create",
                "repository": repository,
                "title_sha256": hashlib.sha256(payload["title"].encode()).hexdigest(),
                "body_sha256": hashlib.sha256(payload["body"].encode()).hexdigest(),
            }
        if request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            body = self.contract.comment(request)
            return {
                "operation": "issue-comment",
                "repository": repository,
                "issue_number": self.contract.issue_number(request),
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "body_characters": len(body),
            }
        if request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            return {
                "operation": "issue-update",
                "repository": repository,
                "issue_number": self.contract.issue_number(request),
                "state": self.contract.issue_state(request),
            }
        if request.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            payload = self.contract.change_request(request)
            return {
                "operation": "change-request-upsert",
                "repository": repository,
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
                "repository": repository,
                "execution_workspace_id": attested.workspace_id,
                "branch": attested.branch,
                "head_revision": attested.revision,
                "workspace_attested": attested.workspace_path.is_dir(),
            }
        if request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            payload = self.contract.pull_request_merge(request)
            return {
                "operation": "pull-request-merge",
                "repository": repository,
                "pull_request_number": payload["number"],
                "merge_method": payload["merge_method"],
                "expected_head_sha": payload["expected_head_sha"],
            }
        if request.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            return {
                "operation": "job-rerun",
                "repository": repository,
                "job_id": self.contract.job_rerun(request),
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
        repository = self.contract.locator(request)
        started = time.time()
        output: dict[str, Any] = {
            "repository": repository,
            **result_resource_output(request),
        }
        if request.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            payload = self.contract.issue_create(request)
            marker = self.contract.marker(request)
            issues = await self.client.list_issues(
                self.api_base, repository, token=credential, state="all"
            )
            item = next(
                (row for row in issues if marker in str(row.get("body") or "")),
                None,
            )
            desired_body = self._owned_body(payload["body"], marker)
            if item is None:
                item = await self.client.create_issue(
                    self.api_base,
                    repository,
                    token=credential,
                    payload={"title": payload["title"], "body": desired_body},
                )
            number = self._positive_number(item, field="issue")
            evidence_type = CODE_HOST_ISSUE_CREATE_EVIDENCE
            external_id = str(item.get("id") or number)
            url = str(item.get("html_url") or "") or None
            output.update({"issue_number": number, "title": payload["title"]})
            summary = "GitHub issue is owned by this action idempotency key."
        elif request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            number = self.contract.issue_number(request)
            marker = self.contract.marker(request)
            comments = await self.client.list_issue_comments(
                self.api_base, repository, number, token=credential
            )
            item = next(
                (row for row in comments if marker in str(row.get("body") or "")),
                None,
            )
            if item is None:
                desired_body = self._owned_body(
                    self.contract.comment(request), marker
                )
                item = await self.client.create_comment(
                    self.api_base,
                    repository,
                    number,
                    token=credential,
                    body=desired_body,
                )
            else:
                desired_body = self._owned_body(
                    self.contract.comment(request), marker
                )
            evidence_type = CODE_HOST_ISSUE_COMMENT_EVIDENCE
            external_id = str(item.get("id") or "") or None
            url = str(item.get("html_url") or "") or None
            output.update(
                {
                    "issue_number": number,
                    "comment_id": external_id,
                    "body_sha256": hashlib.sha256(
                        desired_body.encode()
                    ).hexdigest(),
                }
            )
            summary = "GitHub issue comment is owned by this action idempotency key."
        elif request.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            number = self.contract.issue_number(request)
            state = self.contract.issue_state(request)
            current = await self.client.issue(
                self.api_base, repository, number, token=credential
            )
            item = current
            if str(current.get("state") or "").casefold() != state:
                item = await self.client.update_issue(
                    self.api_base,
                    repository,
                    number,
                    token=credential,
                    payload={"state": state},
                )
            evidence_type = CODE_HOST_ISSUE_STATE_EVIDENCE
            external_id = str(item.get("id") or number)
            url = str(item.get("html_url") or "") or None
            output.update({"issue_number": number, "state": state})
            summary = f"GitHub issue state is {state}."
        elif request.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            payload = self.contract.change_request(request)
            marker = self.contract.marker(request)
            desired_body = self._owned_body(payload["body"], marker)
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
            conflicting_open = next(
                (
                    row
                    for row in pulls
                    if str(row.get("state") or "open").strip().casefold()
                    == "open"
                ),
                None,
            )
            if owned is None and conflicting_open is not None:
                raise ValueError(
                    "an existing pull request for head/base is not owned by this ActionIntent"
                )
            if owned is None:
                item = await self.client.create_pull_request(
                    self.api_base,
                    repository,
                    token=credential,
                    payload={**payload, "body": desired_body},
                )
            else:
                number = self._positive_number(owned, field="pull request")
                if bool(owned.get("draft")) != payload["draft"]:
                    raise ValueError(
                        "GitHub pull request draft transitions require a separate governed action"
                    )
                update = {
                    "title": payload["title"],
                    "body": desired_body,
                    "base": payload["base"],
                }
                changed = (
                    str(owned.get("title") or "") != update["title"]
                    or str(owned.get("body") or "") != update["body"]
                    or self._ref(owned, "base") not in {"", update["base"]}
                )
                item = (
                    await self.client.update_pull_request(
                        self.api_base,
                        repository,
                        number,
                        token=credential,
                        payload=update,
                    )
                    if changed
                    else owned
                )
            number = self._positive_number(item, field="pull request")
            evidence_type = CODE_HOST_CHANGE_REQUEST_EVIDENCE
            external_id = str(item.get("id") or number)
            url = str(item.get("html_url") or "") or None
            output.update(
                {
                    "change_request_number": number,
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
            summary = "GitHub pull request is owned and reconciled by this action."
        elif request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            try:
                attested = self.contract.attest_branch(request)
            except ValueError as exc:
                raise ActionRequirementError(str(exc)) from exc
            if self.branch_publisher is not None:
                await self.branch_publisher(
                    attested.workspace_path,
                    repository,
                    attested.branch,
                    attested.revision,
                    credential,
                    attested.expected_remote_revision,
                )
            else:
                await asyncio.to_thread(
                    self._publish_branch_sync,
                    attested.workspace_path,
                    repository,
                    attested.branch,
                    attested.revision,
                    credential,
                    attested.expected_remote_revision,
                )
            evidence_type = CODE_HOST_BRANCH_EVIDENCE
            external_id = attested.revision
            url = (
                f"{self.web_base}/{repository}/tree/"
                f"{quote(attested.branch, safe='/')}"
            )
            output.update(
                {
                    "branch": attested.branch,
                    "head_revision": attested.revision,
                    "execution_workspace_id": attested.workspace_id,
                }
            )
            summary = "GitHub branch matches the committed execution workspace head."
        elif request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            payload = self.contract.pull_request_merge(request)
            number = payload["number"]
            current = await self.client.pull_request(
                self.api_base, repository, number, token=credential
            )
            expected_head_sha = payload["expected_head_sha"]
            current_head = (
                current.get("head")
                if isinstance(current.get("head"), dict)
                else {}
            )
            if expected_head_sha and str(
                current_head.get("sha") or ""
            ).casefold() != expected_head_sha:
                raise ValueError(
                    "GitHub pull request head no longer matches expected_head_sha"
                )
            if not bool(current.get("merged")):
                if current.get("mergeable") is not True or str(
                    current.get("mergeable_state") or ""
                ) != "clean":
                    raise ValueError(
                        "GitHub pull request is not clean and mergeable; required checks may not be green"
                    )
                merged = await self.client.merge_pull_request(
                    self.api_base,
                    repository,
                    number,
                    token=credential,
                    payload={"merge_method": payload["merge_method"]},
                )
                if not bool(merged.get("merged")):
                    raise ValueError("GitHub declined the pull request merge")
                merge_sha = str(merged.get("sha") or "")
            else:
                merge_sha = str(current.get("merge_commit_sha") or "")
            evidence_type = CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE
            external_id = str(number)
            url = str(current.get("html_url") or "") or None
            output.update(
                {
                    "pull_request_number": number,
                    "head_sha": str(current_head.get("sha") or "").casefold(),
                    "merge_commit_sha": merge_sha,
                }
            )
            summary = "GitHub pull request is merged after clean mergeability validation."
        elif request.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            job_id = self.contract.job_rerun(request)
            current = await self.client.actions_job(
                self.api_base, repository, job_id, token=credential
            )
            if str(current.get("status") or "").casefold() != "completed":
                raise ValueError("GitHub job must be completed before it can be rerun")
            if str(current.get("conclusion") or "").casefold() not in {
                "failure", "cancelled", "timed_out", "action_required", "stale"
            }:
                raise ValueError("GitHub job conclusion is not rerunnable")
            run_id = int(current.get("run_id") or 0)
            if run_id < 1:
                raise ValueError("GitHub job response omitted its workflow run")
            run = await self.client.actions_run(
                self.api_base, repository, run_id, token=credential
            )
            previous_attempt = int(run.get("run_attempt") or 0)
            if previous_attempt < 1:
                raise ValueError("GitHub workflow run omitted its attempt number")
            await self.client.rerun_actions_job(
                self.api_base, repository, job_id, token=credential
            )
            evidence_type = CODE_HOST_JOB_RERUN_EVIDENCE
            external_id = str(job_id)
            url = str(current.get("html_url") or "") or None
            output.update({
                "job_id": job_id,
                "workflow_run_id": run_id,
                "previous_run_attempt": previous_attempt,
            })
            summary = "GitHub accepted the governed CI job rerun request."
        else:
            raise ValueError("unsupported GitHub action")
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
                        "repository": repository,
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
            raise ValueError("GitHub credential is required for verification")
        if result.provider_binding_id != binding.id:
            raise ValueError("GitHub result binding does not match verification binding")
        repository = self.contract.locator_for_result(result, binding)
        findings: list[str] = []
        verified = False
        if result.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            item = await self.client.issue(
                self.api_base,
                repository,
                int(result.output["issue_number"]),
                token=credential,
            )
            marker = self.contract.marker_from_key(result.idempotency_key)
            verified = marker in str(item.get("body") or "")
        elif result.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
            number = int(result.output["issue_number"])
            marker = self.contract.marker_from_key(result.idempotency_key)
            comments = await self.client.list_issue_comments(
                self.api_base, repository, number, token=credential
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
                for item in comments
            )
        elif result.action_id == CODE_HOST_ISSUE_UPDATE_ACTION_ID:
            item = await self.client.issue(
                self.api_base,
                repository,
                int(result.output["issue_number"]),
                token=credential,
            )
            verified = (
                str(item.get("state") or "").casefold()
                == str(result.output["state"]).casefold()
            )
        elif result.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
            number = int(result.output["change_request_number"])
            item = await self.client.pull_request(
                self.api_base, repository, number, token=credential
            )
            marker = self.contract.marker_from_key(result.idempotency_key)
            verified = (
                self._positive_number(item, field="pull request") == number
                and marker in str(item.get("body") or "")
                and self._ref(item, "head") in {"", result.output["head"]}
                and self._ref(item, "base") in {"", result.output["base"]}
                and bool(item.get("draft")) == bool(result.output["draft"])
                and hashlib.sha256(
                    str(item.get("title") or "").encode()
                ).hexdigest()
                == result.output["title_sha256"]
                and hashlib.sha256(
                    str(item.get("body") or "").encode()
                ).hexdigest()
                == result.output["body_sha256"]
            )
        elif result.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            item = await self.client.branch(
                self.api_base,
                repository,
                str(result.output["branch"]),
                token=credential,
            )
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            verified = str(commit.get("sha") or "") == str(
                result.output["head_revision"]
            )
        elif result.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            item = await self.client.pull_request(
                self.api_base,
                repository,
                int(result.output["pull_request_number"]),
                token=credential,
            )
            head = item.get("head") if isinstance(item.get("head"), dict) else {}
            verified = bool(item.get("merged")) and (
                not result.output.get("merge_commit_sha")
                or str(item.get("merge_commit_sha") or "")
                == str(result.output["merge_commit_sha"])
            ) and (
                not result.output.get("head_sha")
                or str(head.get("sha") or "").casefold()
                == str(result.output["head_sha"]).casefold()
            )
        elif result.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            item = await self.client.actions_run(
                self.api_base, repository, int(result.output["workflow_run_id"]), token=credential
            )
            verified = (
                int(item.get("run_attempt") or 0)
                > int(result.output["previous_run_attempt"])
                and str(item.get("status") or "").casefold()
                in {"queued", "in_progress", "completed"}
            )
        else:
            raise ValueError("unsupported GitHub action result")
        if not verified:
            findings.append("GitHub provider state does not match the action receipt")
        evidence = (
            ActionEvidence(
                evidence_type=self.contract.evidence_type(result.action_id),
                reference=str(result.output.get("external_url") or "") or None,
                summary=(
                    "GitHub provider state independently matches the action receipt."
                    if verified
                    else "GitHub provider state differs from the action receipt."
                ),
                metadata={
                    "resource_id": str(result.output["resource_id"]),
                    "repository": repository,
                    "verified": verified,
                },
            ),
        )
        return ActionVerification(
            verified=verified,
            evidence=evidence,
            findings=tuple(findings),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        raise ValueError("GitHub code-host actions are not rollback-capable")
