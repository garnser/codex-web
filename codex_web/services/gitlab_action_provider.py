from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from codex_web.action_providers import (
    ActionEvidence,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionVerification,
)
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.code_host_ci_observation import (
    CI_OBSERVATION_ACTION_ID, CI_OBSERVATION_EVIDENCE, CI_OBSERVATION_VERSION,
    CIObservationParameters, parameters as ci_parameters, project_observation, unavailable_prerequisites,
)
from codex_web.services.code_host_readiness import readiness_checkpoint, readiness_verification_checkpoint
from codex_web.services.code_host_continuation import continuation_for
from codex_web.services.code_host_action_contract import (
    READINESS_OBSERVATION_ACTION_IDS,
    CODE_HOST_BRANCH_EVIDENCE,
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID,
    CODE_HOST_CHANGE_REQUEST_EVIDENCE,
    CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_ACTION_ID,
    CODE_HOST_ISSUE_CREATE_EVIDENCE,
    CODE_HOST_ISSUE_COMMENT_ACTION_ID,
    CODE_HOST_ISSUE_COMMENT_EVIDENCE,
    CODE_HOST_ISSUE_STATE_EVIDENCE,
    CODE_HOST_ISSUE_UPDATE_ACTION_ID,
    CODE_HOST_JOB_RERUN_ACTION_ID,
    CODE_HOST_JOB_RERUN_EVIDENCE,
    CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_EVIDENCE,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_EVIDENCE,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_EVIDENCE,
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
    """Governed GitLab actions and read observations under the shared contract."""

    # Positive-only producer contract; registry reader upgrades do not change pins.
    contract_version = "1.2"
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
        ci_prerequisite_validator=None,
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
        self.ci_prerequisite_validator = ci_prerequisite_validator
        self.branch_publisher = branch_publisher
        from codex_web.services.procedure_provider_results import CodeHostProcedureResultAdapter
        self.procedure_result_adapters = (CodeHostProcedureResultAdapter(provider_instance=self.provider_instance),)
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
        return self.contract.actions()

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
        if attested.work_item_merge_request_number is not None:
            repository = await self.client.project(self.api_base, project, token=credential)
            project_id = repository.get("id")
            relation_valid = (repository.get("path_with_namespace") == project
                and attested.work_item_merge_request_number == iid
                and type(mr.get("project_id")) is int and mr["project_id"] == project_id)
        else:
            issue = await self.client.project_issue(
                self.api_base, project, attested.work_item_issue_number, token=credential,
            )
            related = await self.client.issue_related_merge_requests(
                self.api_base, project, attested.work_item_issue_number, token=credential,
            )
            project_id = issue.get("project_id")
            relation_valid = (type(issue.get("iid")) is int
                and issue.get("iid") == attested.work_item_issue_number
                and any(type(row.get("iid")) is int and row["iid"] == iid
                    and type(row.get("project_id")) is int and row["project_id"] == project_id
                    for row in related))
        remote = await self.client.branch(
            self.api_base, project, attested.branch, token=credential,
        )
        if (
            type(project_id) is not int
            or project_id < 1
            or not relation_valid
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
        if request.action_id == CI_OBSERVATION_ACTION_ID:
            if not binding.credential_ref or request.credential_ref != binding.credential_ref:
                raise ValueError("CI observation requires its configured credential reference")
            return {"operation": "pull-request-ci-observation", "repository": project,
                    **ci_parameters(request).model_dump(mode="json")}
        if request.action_id in READINESS_OBSERVATION_ACTION_IDS:
            if not binding.credential_ref or request.credential_ref != binding.credential_ref:
                raise ValueError('Readiness observation requires its configured provider credential reference')
            return {"operation":"pull-request-readiness-observation", "repository":project,
                **self.contract.pull_request_observe_readiness(request)}
        if request.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID:
            return {"operation":"pull-request-merge-observation","repository":project,
                **self.contract.pull_request_verify_merge(request)}
        if request.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            payload = self.contract.issue_create(request)
            return {
                "operation": "issue-create",
                "repository": project,
                "title_sha256": hashlib.sha256(payload["title"].encode()).hexdigest(),
                "body_sha256": hashlib.sha256(payload["body"].encode()).hexdigest(),
            }
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
            continuation_for(request, binding.id)
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
                "expected_remote_absent": attested.expected_remote_revision == "",
                "workspace_attested": attested.workspace_path.is_dir(),
                "existing_change_request_number": attested.existing_change_request_number,
                "work_item_issue_number": attested.work_item_issue_number,
                "work_item_merge_request_number": attested.work_item_merge_request_number,
            }
        if request.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            return {"operation": "job-rerun", "repository": project,
                    "job_id": self.contract.job_rerun(request)}
        if request.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            return {"operation": "pull-request-ready", "repository": project,
                    **self.contract.pull_request_ready(request)}
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

    @staticmethod
    def _merge_request_title(title: str, draft: bool) -> str:
        # GitLab's REST interface represents Draft state through the title.
        # Only documented leading markers are formatting; mid-title words
        # remain user content and exact verification still checks the result.
        prefixes = ("Draft:", "[Draft]", "(Draft)")
        if draft:
            return title if any(title.casefold().startswith(p.casefold()) for p in prefixes) else f"Draft: {title}"
        remaining = title
        while True:
            prefix = next((p for p in prefixes if remaining.casefold().startswith(p.casefold())), None)
            if prefix is None:
                break
            remaining = remaining[len(prefix):].lstrip()
        if not remaining:
            raise ValueError("ready merge-request title must contain content after its Draft marker")
        return remaining

    async def _ready_snapshot(self, project, parameters, credential, checkpoint):
        """Read the canonical project, exact open MR and current target tip.

        diff_refs.base_sha is a merge base, not the current target branch head.
        Every network await is followed by the existing intent admission guard.
        """
        await checkpoint()
        repository = await self.client.project(self.api_base, project, token=credential)
        await checkpoint()
        item = await self.client.merge_request(self.api_base, project,
            parameters["pull_request_number"], token=credential)
        await checkpoint()
        target = await self.client.branch(self.api_base, project,
            parameters["expected_base_ref"], token=credential)
        await checkpoint()
        project_id = repository.get("id")
        if (type(project_id) is not int or project_id < 1
                or repository.get("path_with_namespace") != project
                or type(item.get("iid")) is not int
                or item.get("iid") != parameters["pull_request_number"]
                or type(item.get("id")) is not int or item["id"] < 1
                or any(type(item.get(key)) is not int or item[key] != project_id
                       for key in ("project_id", "source_project_id", "target_project_id"))
                or item.get("state") != "opened"
                or item.get("sha") != parameters["expected_head_sha"]
                or item.get("target_branch") != parameters["expected_base_ref"]
                or not isinstance(item.get("source_branch"), str) or not item["source_branch"]
                or item["source_branch"] == item["target_branch"]
                or target.get("name") != parameters["expected_base_ref"]
                or (target.get("commit") or {}).get("id") != parameters["expected_base_sha"]
                or type(item.get("draft")) is not bool
                or not isinstance(item.get("title"), str) or not item["title"].strip()
                or item.get("description") is not None and not isinstance(item["description"], str)):
            raise ActionRequirementError("GitLab readiness requires the exact open same-project MR/head/target tip")
        return {key: item.get(key) for key in (
            "id", "iid", "project_id", "source_project_id", "target_project_id", "state",
            "sha", "source_branch", "target_branch", "draft", "title", "description")}

    @staticmethod
    def _job_snapshot(item, *, project_id: int, job_id: int | None = None):
        """Validate the scoped Jobs API identity, without trusting a Web URL."""
        if not isinstance(item, dict) or type(project_id) is not int or project_id < 1:
            raise ValueError("GitLab job project identity is invalid")
        observed_id = item.get("id")
        pipeline = item.get("pipeline")
        commit = item.get("commit")
        if (type(observed_id) is not int or observed_id < 1
                or job_id is not None and observed_id != job_id
                or not isinstance(pipeline, dict) or not isinstance(commit, dict)
                or type(pipeline.get("id")) is not int or pipeline["id"] < 1
                or type(pipeline.get("project_id")) is not int
                or pipeline["project_id"] != project_id):
            raise ValueError("GitLab job/pipeline identity does not match its project")
        sha = pipeline.get("sha")
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or commit.get("id") != sha
                or not isinstance(item.get("ref"), str) or not item["ref"]
                or pipeline.get("ref") != item["ref"]
                or not isinstance(item.get("name"), str) or not item["name"]
                or not isinstance(item.get("stage"), str) or not item["stage"]):
            raise ValueError("GitLab job omitted its exact commit/ref/name/stage")
        return {"job_id": observed_id, "pipeline_id": pipeline["id"],
                "head_sha": sha, "ref": item["ref"],
                "job_name": item["name"], "job_stage": item["stage"]}

    @staticmethod
    def _same_job_scope(left, right):
        return all(left[key] == right[key] for key in
                   ("pipeline_id", "head_sha", "ref", "job_name", "job_stage"))

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if not credential:
            raise ActionRequirementError("GitLab credential is required")
        # Preparation is side-effect free. Invalid inputs or workspace scope
        # must fail before any provider mutation, never become unknown outcomes.
        try:
            await self.prepare(request, binding=binding)
        except ValueError as exc:
            prefix = (
                "GitLab branch could not be attested"
                if request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID
                else "GitLab action precondition failed"
            )
            if request.action_id == CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID:
                prefix += "; supported change request parameters: title, body, head, base, draft"
            raise ActionRequirementError(f"{prefix}: {exc}") from exc

        project = self.contract.locator(request)
        started = time.time()
        output: dict[str, Any] = {
            "repository": project,
            **result_resource_output(request),
        }
        if request.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            payload = self.contract.issue_create(request)
            marker = self.contract.marker(request)
            # Search all states in this credential-bound repository. A unique
            # ownership marker bounds recovery without scanning its backlog.
            issues = await self.client.project_issues(
                self.api_base, project, token=credential,
                params={"state": "all", "scope": "all", "search": marker, "per_page": 100},
            )
            if len(issues) >= 100:
                raise ActionRequirementError("GitLab issue ownership search exceeded its bounded result limit")
            owned = [row for row in issues if marker in str(row.get("description") or "")]
            if len(owned) > 1:
                raise ActionRequirementError("multiple GitLab issues share the action ownership marker")
            desired_body = self._owned_body(payload["body"], marker)
            item = owned[0] if owned else await self.client.create_project_issue(
                self.api_base, project, token=credential,
                payload={"title": payload["title"], "description": desired_body},
            )
            iid = self._positive_iid(item, field="issue")
            evidence_type = CODE_HOST_ISSUE_CREATE_EVIDENCE
            external_id = str(item.get("id") or iid)
            url = str(item.get("web_url") or "") or None
            output.update({
                "issue_number": iid,
                "title_sha256": hashlib.sha256(payload["title"].encode()).hexdigest(),
                "body_sha256": hashlib.sha256(desired_body.encode()).hexdigest(),
            })
            summary = "GitLab issue is owned by this action idempotency key."
        elif request.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
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
                desired_title = self._merge_request_title(payload["title"], payload["draft"])
            except ValueError as exc:
                raise ActionRequirementError(
                    f"{exc}; supported change request parameters: "
                    "title, body, head, base, draft"
                ) from exc
            proof = continuation_for(request, binding.id)
            marker = proof.marker if proof else self.contract.marker(request)
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
            if proof and (
                owned is None or str(owned.get("id") or "") != proof.external_id
                or self._positive_iid(owned, field="merge request") != proof.number
                or owned.get("state") != "opened"
                or owned.get("source_branch") != payload["head"]
                or owned.get("target_branch") != payload["base"]
            ):
                raise ActionRequirementError("original owned merge request target is not verified")
            if owned is None and merge_requests:
                raise ActionRequirementError(
                    "an existing merge request for head/base is not owned by this ActionIntent"
                )
            api_payload = {
                "title": desired_title,
                "description": desired_body,
                "source_branch": payload["head"],
                "target_branch": payload["base"],
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
                        str(owned.get("title") or "") != desired_title,
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
                        desired_title.encode()
                    ).hexdigest(),
                    "body_sha256": hashlib.sha256(
                        desired_body.encode()
                    ).hexdigest(),
                }
            )
            if proof:
                output["origin_intent_id"] = proof.origin_intent_id
                output["ownership_marker"] = proof.marker
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
                try:
                    reattested = self.contract.attest_branch(request)
                except ValueError as exc:
                    raise ActionRequirementError(str(exc)) from exc
                if reattested != attested:
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
                    "expected_remote_absent": attested.expected_remote_revision == "",
                    "execution_workspace_id": attested.workspace_id,
                    "existing_change_request_number": attested.existing_change_request_number,
                    "work_item_issue_number": attested.work_item_issue_number,
                    "work_item_merge_request_number": attested.work_item_merge_request_number,
                }
            )
            summary = "GitLab branch matches the committed execution workspace head."
        elif request.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            parameters = self.contract.pull_request_ready(request)
            async def checkpoint():
                await readiness_checkpoint(request, binding.id)
            try:
                current = await self._ready_snapshot(project, parameters, credential, checkpoint)
                title = self._merge_request_title(current["title"], False)
                repeated = await self._ready_snapshot(project, parameters, credential, checkpoint)
                if repeated != current:
                    raise ActionRequirementError("GitLab readiness target metadata changed before mutation")
                await checkpoint()
            except ActionRequirementError:
                raise
            except Exception as exc:
                raise ActionRequirementError("GitLab readiness preflight is unavailable") from exc
            mutated = current["draft"]
            if mutated:
                # GitLab's title update has no expected-head/title CAS. From this
                # point all failures are uncertain; never automatically replay.
                await self.client.update_merge_request(self.api_base, project,
                    parameters["pull_request_number"], token=credential, payload={"title": title})
            try:
                observed = await self._ready_snapshot(project, parameters, credential, checkpoint)
                expected = {**current, "draft": False, "title": title}
                if observed != expected:
                    raise RuntimeError("GitLab readiness metadata or target changed")
            except Exception as exc:
                raise RuntimeError("GitLab readiness outcome requires read-only reconciliation") from exc
            evidence_type = CODE_HOST_PULL_REQUEST_READY_EVIDENCE
            external_id = str(parameters["pull_request_number"])
            url = f"{self.web_base}/{project}/-/merge_requests/{external_id}"
            output.update({**parameters, "draft": False,
                "provider_mutation_performed": mutated,
                "metadata_sha256": hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
            summary = "Exact open GitLab merge request is ready for review; no approval or merge was performed."
        elif request.action_id == CI_OBSERVATION_ACTION_ID:
            output.update(await self._read_ci_observation(request, binding, credential))
            project_observation(output, request=request, binding_id=binding.id,
                                provider_instance=self.provider_instance)
            evidence_type = CI_OBSERVATION_EVIDENCE
            external_id = str(ci_parameters(request).pull_request_number)
            url = None
            summary = "Read-only exact-head CI observation; readiness receipts never authorize privileged installation."
        elif request.action_id in READINESS_OBSERVATION_ACTION_IDS:
            from codex_web.services.code_host_readiness_observation import VERSION, VERSION_V2, observation_outputs, borrowed_credential_metadata
            from codex_web.services.code_host_readiness import current_observation_policy
            parameters = self.contract.pull_request_observe_readiness(request)
            async def checkpoint():
                await readiness_checkpoint(request, binding.id)
            fact = await self._read_readiness_fact(binding, self.contract.resource(request),
                project, parameters, credential, checkpoint)
            policy = current_observation_policy(binding.id)
            output.update({**parameters, 'observation_version': VERSION_V2 if policy else VERSION,
                **({'observation_policy': policy.model_dump(mode='json')} if policy else {}),
                'merge_requirements': fact.model_dump(mode='json'), 'provider_instance': self.provider_instance,
                **borrowed_credential_metadata()})
            observation_outputs(output, binding_id=binding.id, resource_id=request.resource_ids[0],
                provider_instance=self.provider_instance, parameters=parameters, observed_at=time.time())
            evidence_type = CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_EVIDENCE
            external_id = str(parameters['pull_request_number'])
            url = None
            summary = 'Read-only exact-head readiness observation; pending and blockers are not QA approval or merge permission.'
        elif request.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID:
            parameters = self.contract.pull_request_verify_merge(request)
            item = await self.client.merge_request(self.api_base,project,
                parameters["pull_request_number"],token=credential)
            if not self._observed_merge(item,parameters):
                raise ActionRequirementError("Exact original merged MR state, head or merge commit does not match")
            evidence_type = CODE_HOST_PULL_REQUEST_VERIFY_MERGE_EVIDENCE
            external_id = str(parameters["pull_request_number"])
            url = None
            output.update({**parameters,"merge_verified":True})
            summary = "Read-only observation matches the exact original merged MR."
        elif request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            payload = self.contract.pull_request_merge(request)
            if payload["merge_method"] == "rebase":
                raise ActionRequirementError(
                    "GitLab rebase-and-merge is not supported atomically"
                )
            iid = payload["number"]
            current = await self.client.merge_request(
                self.api_base, project, iid, token=credential
            )
            current_sha = str(current.get("sha") or "").casefold()
            expected_head_sha = payload["expected_head_sha"]
            if expected_head_sha and current_sha != expected_head_sha:
                raise ActionRequirementError(
                    "GitLab merge request head no longer matches expected_head_sha"
                )
            if str(current.get("state") or "").casefold() != "merged":
                if bool(current.get("draft")) or bool(
                    current.get("work_in_progress")
                ):
                    raise ActionRequirementError("GitLab merge request is still a draft")
                detailed_status = str(
                    current.get("detailed_merge_status") or ""
                ).casefold()
                merge_status = str(
                    current.get("merge_status") or ""
                ).casefold()
                if detailed_status and detailed_status != "mergeable":
                    raise ActionRequirementError(
                        "GitLab merge request is not clean and mergeable"
                    )
                if merge_status and merge_status != "can_be_merged":
                    raise ActionRequirementError(
                        "GitLab merge request is not clean and mergeable"
                    )
                pipeline = current.get("head_pipeline")
                if not isinstance(pipeline, dict) or str(
                    pipeline.get("status") or ""
                ).casefold() != "success":
                    raise ActionRequirementError(
                        "GitLab merge request head pipeline is not successful"
                    )
                if not re.fullmatch(r"[0-9a-f]{40}", current_sha) or pipeline.get("sha") != current_sha:
                    raise ActionRequirementError("GitLab successful pipeline does not identify the exact MR head")
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
        elif request.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            job_id = self.contract.job_rerun(request)
            repository = await self.client.project(self.api_base, project, token=credential)
            current = await self.client.job(self.api_base, project, job_id, token=credential)
            try:
                project_id = repository.get("id") if isinstance(repository, dict) else None
                original = self._job_snapshot(current, project_id=project_id, job_id=job_id)
                if current.get("status") not in {"failed", "canceled"} or current.get("archived") is not False:
                    raise ValueError("GitLab job must be a nonarchived failed or canceled job")
            except ValueError as exc:
                raise ActionRequirementError(str(exc)) from exc
            # The original remains untouched. The POST returns a distinct retry
            # job; any transport/normalization failure from here is uncertain.
            retried = await self.client.retry_job(self.api_base, project, job_id, token=credential)
            fresh = self._job_snapshot(retried, project_id=project_id)
            if (fresh["job_id"] == job_id or not self._same_job_scope(original, fresh)
                    or retried.get("archived") is not False
                    or retried.get("status") not in {
                        "created", "pending", "preparing", "waiting_for_resource", "scheduled",
                        "running", "success", "failed", "canceled"}):
                raise ValueError("GitLab retry response does not identify the exact new job")
            evidence_type = CODE_HOST_JOB_RERUN_EVIDENCE
            external_id = str(fresh["job_id"])
            url = str(retried.get("web_url") or "") or None
            output.update({**original, "retried_job_id": fresh["job_id"],
                           "original_status": current["status"], "job_project_id": project_id})
            summary = "GitLab accepted one governed job retry; this is not CI success."
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

    async def _read_readiness_fact(self, binding, resource, project, parameters, credential, checkpoint):
        from types import SimpleNamespace
        from codex_web.services.gitlab_merge_requirements import GitLabMergeRequirementsReader
        # Adapter of the EXISTING ActionProvider binding, not a new stored binding
        # or grant. API base belongs to this registered provider, never the caller.
        reader_binding = SimpleNamespace(id=binding.id, provider_instance=binding.provider_instance,
            base_url=self.api_base)
        from codex_web.services.code_host_readiness import current_observation_policy, current_observation_execution_budget
        policy = current_observation_policy(binding.id)
        execution_budget = current_observation_execution_budget(binding.id)
        fact = await GitLabMergeRequirementsReader(self.client, reader_binding, resource,
            project, credential, checkpoint=checkpoint,
            observation_seconds=policy.seconds if policy else 30, execution_budget=execution_budget).read(str(parameters['pull_request_number']))
        if fact.head_revision != parameters['expected_head_sha'] or fact.changed:
            raise ActionRequirementError('Readiness observation exact head changed')
        return fact

    @staticmethod
    def _observed_merge(item, parameters):
        return (type(item.get("iid")) is int and item["iid"] == parameters["pull_request_number"]
            and item.get("state") == "merged" and item.get("sha") == parameters["head_sha"]
            and item.get("merge_commit_sha") == parameters["merge_commit_sha"])

    async def _read_ci_observation(self, request, binding, credential):
        from codex_web.code_hosts import CodeHostProviderBinding, CodeHostCapability
        from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
        p = ci_parameters(request)
        resource = self.contract.resource(request)
        project = self.contract.locator(request)
        async def current():
            item = await self.client.read_json_bounded(self.api_base,
                f"projects/{quote(project, safe='')}/merge_requests/{p.pull_request_number}", token=credential)
            pipeline = item.get("head_pipeline") if isinstance(item, dict) else None
            if (not isinstance(item, dict) or type(item.get("iid")) is not int or item.get("iid") != p.pull_request_number
                    or type(item.get("project_id")) is not int or item["project_id"] < 1
                    or item.get("sha") != p.expected_head_sha
                    or not isinstance(pipeline, dict) or type(pipeline.get("id")) is not int
                    or pipeline.get("sha") != p.expected_head_sha):
                raise ActionRequirementError("CI observation exact current MR head/pipeline unavailable")
            return (item.get("project_id"), pipeline["id"])
        original = await current()
        fact_binding = CodeHostProviderBinding(id=binding.id,
            organization_id=binding.organization_id, workspace_id=binding.workspace_id,
            provider_type="gitlab", provider_instance=self.provider_instance, base_url=self.api_base,
            credential_ref=binding.credential_ref, capabilities=(CodeHostCapability.CHECKS_READ,))
        runs = await GitLabCodeHostProvider(self.client).validation_runs(fact_binding, resource,
            p.expected_head_sha, credential=credential, expected_project_id=original[0])
        if str(original[1]) not in {run.external_id for run in runs}:
            raise ActionRequirementError("CI current MR head pipeline is absent from complete inventory")
        prerequisites = (self.ci_prerequisite_validator(request, binding)
                         if self.ci_prerequisite_validator else unavailable_prerequisites(request))
        if await current() != original:
            raise ActionRequirementError("CI original MR pipeline changed during observation")
        return {**p.model_dump(mode="json"), "head_sha": p.expected_head_sha,
            "observation_version": CI_OBSERVATION_VERSION, "provider_instance": self.provider_instance,
            "binding_id": binding.id, "resource_id": resource.id,
            "requested_by": request.requested_by, "project_id": request.project_id,
            "pull_request_number": p.pull_request_number, "current_pipeline_id": str(original[1]),
            "runs": [run.model_dump(mode="json") for run in runs],
            "prerequisites": prerequisites.model_dump(mode="json")}

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
        if result.action_id == CI_OBSERVATION_ACTION_ID:
            from types import SimpleNamespace
            p = {key: result.output.get(key) for key in CIObservationParameters.model_fields}
            request = SimpleNamespace(parameters=p, resource_ids=(result.output.get("resource_id"),),
                requested_by=result.output.get("requested_by"), organization_id=binding.organization_id,
                workspace_id=binding.workspace_id, project_id=result.output.get("project_id"))
            if result.status != "succeeded":
                return ActionVerification(verified=False, findings=("CI original result unavailable",))
            project_observation(result.output, request=request, binding_id=binding.id,
                                provider_instance=self.provider_instance)
            fresh = await self._read_ci_observation(request, binding, credential)
            verified = all(result.output.get(k) == v for k, v in fresh.items())
        elif result.action_id in READINESS_OBSERVATION_ACTION_IDS:
            from types import SimpleNamespace
            from codex_web.services.code_host_readiness_observation import observation_outputs, borrowed_credential_metadata
            parameters = self.contract.pull_request_observe_readiness(SimpleNamespace(parameters={
                key:result.output.get(key) for key in ('pull_request_number','expected_head_sha')}))
            if result.status != 'succeeded' or result.external_id != str(parameters['pull_request_number']):
                return ActionVerification(verified=False, findings=('Original readiness observation result is invalid',))
            resource = self.contract.resource_for_result(result, binding)
            observation_outputs(result.output, binding_id=binding.id, resource_id=resource.id,
                provider_instance=self.provider_instance, parameters=parameters, observed_at=result.completed_at)
            if any(result.output.get(key) != value for key,value in borrowed_credential_metadata().items()):
                raise ActionRequirementError('Readiness observation original credential version changed')
            from codex_web.services.code_host_readiness import current_observation_policy
            policy = current_observation_policy(binding.id)
            if result.output.get('observation_policy') != (policy.model_dump(mode='json') if policy else None):
                raise ActionRequirementError('Readiness observation original policy changed')
            async def checkpoint():
                await readiness_verification_checkpoint(binding.id)
            fresh = await self._read_readiness_fact(binding, resource, project, parameters, credential, checkpoint)
            # Independent current read verifies the original observation, including
            # pending gates; neither its success nor its receipt is QA acceptance.
            original = dict(result.output['merge_requirements'])
            def stable(fact):
                value = dict(fact)
                value.pop('observed_at', None); value.pop('expires_at', None)
                value['sources'] = [{k:v for k,v in row.items() if k != 'observed_at'} for row in value['sources']]
                return value
            verified = stable(original) == stable(fresh.model_dump(mode='json'))
        elif result.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID:
            from types import SimpleNamespace
            parameters = self.contract.pull_request_verify_merge(SimpleNamespace(parameters={
                key:result.output.get(key) for key in ("pull_request_number","head_sha","merge_commit_sha")}))
            if (result.status != "succeeded" or result.output.get("merge_verified") is not True
                or result.external_id != str(parameters["pull_request_number"])):
                return ActionVerification(verified=False,findings=("Original merge observation result is invalid",))
            item = await self.client.merge_request(self.api_base,project,parameters["pull_request_number"],token=credential)
            verified = self._observed_merge(item,parameters)
        elif result.action_id == CODE_HOST_ISSUE_CREATE_ACTION_ID:
            iid = result.output.get("issue_number")
            external_id = result.external_id
            if (
                type(iid) is int
                and iid > 0
                and isinstance(external_id, str)
                and bool(external_id)
            ):
                item = await self.client.project_issue(
                    self.api_base, project, iid, token=credential,
                )
                marker = self.contract.marker_from_key(result.idempotency_key)
                verified = (
                    type(item.get("iid")) is int and item["iid"] == iid
                    and str(item.get("id") or iid) == external_id
                    and marker in str(item.get("description") or "")
                    and hashlib.sha256(str(item.get("title") or "").encode()).hexdigest()
                        == result.output.get("title_sha256")
                    and hashlib.sha256(str(item.get("description") or "").encode()).hexdigest()
                        == result.output.get("body_sha256")
                )
        elif result.action_id == CODE_HOST_ISSUE_COMMENT_ACTION_ID:
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
            marker = self.contract.change_request_result_marker(result)
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
        elif result.action_id == CODE_HOST_PULL_REQUEST_READY_ACTION_ID:
            parameters = {key: result.output[key] for key in (
                "pull_request_number", "expected_head_sha", "expected_base_sha", "expected_base_ref")}
            async def checkpoint():
                await readiness_verification_checkpoint(binding.id)
            try:
                observed = await self._ready_snapshot(project, parameters, credential, checkpoint)
                repeated = await self._ready_snapshot(project, parameters, credential, checkpoint)
                verified = (observed == repeated and observed["draft"] is False
                    and self._merge_request_title(observed["title"], False) == observed["title"]
                    and (not result.output.get("metadata_sha256") or
                        result.output["metadata_sha256"] == hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()))
            except ActionRequirementError:
                # Canonical admission did not reach provider observation. Retain
                # its typed cause in the originating verification receipt rather
                # than claiming that GitLab state differs from the receipt.
                raise
            except (KeyError, TypeError, ValueError):
                verified = False
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
        elif result.action_id == CODE_HOST_JOB_RERUN_ACTION_ID:
            try:
                raw = result.output
                old_id, new_id = raw.get("job_id"), raw.get("retried_job_id")
                if (result.status != "succeeded" or type(old_id) is not int or old_id < 1
                        or type(new_id) is not int or new_id < 1 or old_id == new_id
                        or result.external_id != str(new_id)
                        or raw.get("original_status") not in {"failed", "canceled"}):
                    raise ValueError("GitLab original retry receipt is invalid")
                repository = await self.client.project(self.api_base, project, token=credential)
                project_id = repository.get("id") if isinstance(repository, dict) else None
                if (type(project_id) is not int or type(raw.get("job_project_id")) is not int
                        or type(raw.get("pipeline_id")) is not int
                        or raw.get("job_project_id") != project_id):
                    raise ValueError("GitLab retry receipt project changed")
                original = await self.client.job(self.api_base, project, old_id, token=credential)
                retried = await self.client.job(self.api_base, project, new_id, token=credential)
                old = self._job_snapshot(original, project_id=project_id, job_id=old_id)
                new = self._job_snapshot(retried, project_id=project_id, job_id=new_id)
                verified = (all(raw.get(key) == value for key, value in old.items())
                    and original.get("status") == raw["original_status"]
                    and original.get("archived") is False and retried.get("archived") is False
                    and self._same_job_scope(old, new)
                    and retried.get("status") in {
                        "created", "pending", "preparing", "waiting_for_resource", "scheduled",
                        "running", "success", "failed", "canceled"})
            except (KeyError, TypeError, ValueError):
                verified = False
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


# Captured once; metadata read scopes must refuse later class/instance overrides.
_CANONICAL_GITLAB_ACTIONS = GitLabActionProvider.actions
