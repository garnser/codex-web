from __future__ import annotations

import base64
import json
import io
import hashlib
import zipfile
from datetime import datetime
import re
from urllib.parse import quote, urlparse

import httpx

from codex_web.canonical_events import CanonicalEventType
from codex_web.code_hosts import (
    CodeHostArtifactDownloadFact,
    CodeHostArtifactFact,
    CodeHostCapability,
    CodeHostCheckFact,
    CodeHostValidationRunFact,
    CodeHostJobLogFact,
    CodeHostCommitFact,
    CodeHostCompareFact,
    CodeHostError,
    CodeHostProviderBinding,
    CodeHostPullRequestFact,
    CodeHostRefFact,
    CodeHostReleaseFact,
    CodeHostReviewFact,
    CodeHostRepositoryFact,
    CodeHostTransientError,
    CodeHostWebhookFact,
)
from codex_web.integrations.github_client import GitHubClient
from codex_web.resources import Resource


def _repository_name(resource: Resource) -> str:
    for alias in resource.aliases:
        if (alias.provider or "").casefold() == "github" and alias.namespace in {
            "provider",
            "repository",
            "github-repository",
        }:
            return alias.value
    provenance = resource.provenance
    if provenance and provenance.external_url:
        path = urlparse(provenance.external_url).path.strip("/")
        if path:
            return path.removesuffix(".git")
    if provenance and provenance.external_id and "/" in provenance.external_id:
        return provenance.external_id
    raise CodeHostError(
        "GitHub repository Resource needs a provider alias or external URL locator"
    )


def _timestamp(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHubCodeHostProvider:
    provider_type = "github"

    def __init__(self, client: GitHubClient | None = None) -> None:
        self.client = client or GitHubClient()

    def capabilities(self) -> frozenset[CodeHostCapability]:
        return frozenset(
            {
                CodeHostCapability.REPOSITORY_READ,
                CodeHostCapability.REFS_READ,
                CodeHostCapability.COMMITS_READ,
                CodeHostCapability.PULL_REQUEST_READ,
                CodeHostCapability.REVIEW_READ,
                CodeHostCapability.CHECKS_READ,
                CodeHostCapability.JOB_LOGS_READ,
                CodeHostCapability.ARTIFACTS_READ,
                CodeHostCapability.RELEASE_READ,
                CodeHostCapability.COMPARE_READ,
                CodeHostCapability.WEBHOOK_NORMALIZE,
            }
        )

    @staticmethod
    def _classify(exc: Exception) -> CodeHostError:
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return CodeHostTransientError(f"{type(exc).__name__}: {exc}")
        status = getattr(exc, "status_code", None)
        if status in {408, 409, 429} or (isinstance(status, int) and status >= 500):
            return CodeHostTransientError(str(exc))
        return CodeHostError(str(exc))

    async def repository(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> CodeHostRepositoryFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub repository response is invalid")
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        external_id = str(data.get("id") or repository)
        full_name = str(data.get("full_name") or repository)
        return CodeHostRepositoryFact(
            resource_id=resource.id,
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            external_id=external_id,
            name=str(data.get("name") or full_name.rsplit("/", 1)[-1]),
            full_name=full_name,
            default_branch=data.get("default_branch"),
            web_url=data.get("html_url"),
            archived=bool(data.get("archived")),
            visibility=data.get("visibility") or ("private" if data.get("private") else "public"),
        )

    async def refs(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostRefFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/git/matching-refs/",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub refs response is invalid")
        facts = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("ref") or "")
            obj = item.get("object") if isinstance(item.get("object"), dict) else {}
            if ref.startswith("refs/heads/"):
                kind = "branch"
                name = ref.removeprefix("refs/heads/")
            elif ref.startswith("refs/tags/"):
                kind = "tag"
                name = ref.removeprefix("refs/tags/")
            else:
                kind = "ref"
                name = ref
            revision = str(obj.get("sha") or "")
            if name and revision:
                facts.append(CodeHostRefFact(name=name, kind=kind, revision=revision))
        return tuple(facts)

    async def commit(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCommitFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/commits/{quote(revision, safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub commit response is invalid")
        commit_data = data.get("commit") if isinstance(data.get("commit"), dict) else {}
        author = data.get("author") if isinstance(data.get("author"), dict) else {}
        author_data = commit_data.get("author") if isinstance(commit_data.get("author"), dict) else {}
        return CodeHostCommitFact(
            revision=str(data.get("sha") or revision),
            message=str(commit_data.get("message") or ""),
            author_external_id=(
                str(author.get("id")) if author.get("id") is not None else None
            ),
            authored_at=_timestamp(author_data.get("date")),
            web_url=data.get("html_url"),
        )

    async def pull_request(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> CodeHostPullRequestFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/pulls/{quote(str(external_id), safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub pull request response is invalid")
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return CodeHostPullRequestFact(
            external_id=str(data.get("id") or external_id),
            number=data.get("number"),
            title=str(data.get("title") or ""),
            state=str(data.get("state") or "unknown"),
            source_ref=head.get("ref"),
            target_ref=base.get("ref"),
            author_external_id=str(user.get("id")) if user.get("id") is not None else None,
            web_url=data.get("html_url"),
            draft=bool(data.get("draft")),
            merge_revision=data.get("merge_commit_sha"),
        )

    async def pull_requests(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
        state: str = "open",
    ) -> tuple[CodeHostPullRequestFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/pulls",
                token=credential,
                params={"state": state, "per_page": 100},
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub pull request collection response is invalid")
        facts = []
        for item in data:
            if not isinstance(item, dict):
                continue
            head = item.get("head") if isinstance(item.get("head"), dict) else {}
            base = item.get("base") if isinstance(item.get("base"), dict) else {}
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            facts.append(CodeHostPullRequestFact(
                external_id=str(item.get("id") or item.get("number") or ""),
                number=item.get("number"),
                title=str(item.get("title") or ""),
                state=str(item.get("state") or "unknown"),
                source_ref=head.get("ref"),
                target_ref=base.get("ref"),
                author_external_id=str(user.get("id")) if user.get("id") is not None else None,
                web_url=item.get("html_url"),
                draft=bool(item.get("draft")),
                merge_revision=item.get("merge_commit_sha"),
            ))
        return tuple(facts)

    async def reviews(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        external_id: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReviewFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/pulls/{quote(str(external_id), safe='')}/reviews",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub reviews response is invalid")
        facts = []
        for item in data:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            facts.append(
                CodeHostReviewFact(
                    external_id=str(item["id"]),
                    state=str(item.get("state") or "unknown").casefold(),
                    author_external_id=(
                        str(user.get("id")) if user.get("id") is not None else None
                    ),
                    submitted_at=_timestamp(item.get("submitted_at")),
                    web_url=item.get("html_url"),
                )
            )
        return tuple(facts)

    async def checks(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        revision: str,
        *,
        credential: str | None,
    ) -> tuple[CodeHostCheckFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/commits/{quote(revision, safe='')}/check-runs",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub checks response is invalid")
        rows = data.get("check_runs")
        if not isinstance(rows, list):
            rows = []
        return tuple(
            CodeHostCheckFact(
                external_id=str(item.get("id")),
                name=str(item.get("name") or "check"),
                state=str(item.get("status") or "unknown"),
                conclusion=item.get("conclusion"),
                revision=str(item.get("head_sha") or revision),
                web_url=item.get("html_url"),
            )
            for item in rows
            if isinstance(item, dict) and item.get("id") is not None
        )

    async def _recovery_publication(self, binding, resource, run_id, revision, *, credential, allow_absent=False):
        """Interpret the existing immutable candidate-evidence v1 protocol, never logs."""
        repository = _repository_name(resource)
        data = await self.client.get_json(binding.base_url,
            f"repos/{repository}/actions/runs/{run_id}/artifacts", token=credential, params={"per_page": 100})
        rows = data.get('artifacts', []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or data.get('total_count') != len(rows) or len(rows) > 100:
            raise CodeHostError('Recovery artifact inventory is incomplete or exceeds the inspection bound')
        candidates = [row for row in rows if row.get('name') == 'recovery-candidate-' + revision]
        if not candidates and allow_absent:
            return None
        if len(candidates) != 1 or candidates[0].get('expired') or candidates[0].get('size_in_bytes', 0) > 320 * 1024:
            raise CodeHostError('Exact immutable recovery candidate artifact is missing, expired or ambiguous')
        candidate = candidates[0]
        downloaded = await self.artifact_download(binding, resource, int(candidate['id']),
                                                   credential=credential, max_bytes=320 * 1024)
        if downloaded.truncated:
            raise CodeHostError('Recovery candidate artifact is truncated')
        raw = base64.b64decode(downloaded.content_base64, validate=True)
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if candidate.get('digest') and candidate['digest'] != digest:
            raise CodeHostError('Recovery artifact digest does not match immutable provider metadata')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 64 or sum(entry.file_size for entry in entries) > 1048576:
                    raise CodeHostError('Recovery artifact expanded contents exceed the inspection bound')
                manifests = [entry for entry in entries if entry.filename == 'candidate-evidence.json']
                if len(manifests) != 1:
                    raise CodeHostError('Recovery candidate evidence manifest is missing or ambiguous')
                with archive.open(manifests[0]) as member:
                    payload = member.read(1048576 + 1)
                if len(payload) > 1048576:
                    raise CodeHostError('Recovery manifest exceeds the expanded read bound')
                manifest = json.loads(payload)
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise CodeHostError('Recovery candidate artifact protocol is unreadable or unsupported') from exc
        # Structural requirements of the existing versioned candidate protocol.
        flags = {'compose_nginx', 'clean_onboarding', 'restart', 'slack_socket_regression',
                 'stale_turn_regression', 'health_hot_path_regression', 'event_journal_regression'}
        qualification = manifest.get('deterministic_qualification') if isinstance(manifest, dict) else None
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != '1.0'
                or manifest.get('stage') != 'candidate' or manifest.get('commit') != revision
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(manifest.get('image_digest', '')))
                or not isinstance(qualification, dict) or set(qualification) != flags
                or any(value is not True for value in qualification.values())):
            raise CodeHostError('Recovery candidate does not prove exact source and complete deterministic qualification')
        return {'artifact_id': str(candidate['id']), 'artifact_digest': digest,
                'schema_version': '1.0', 'stage': 'candidate', 'commit': revision,
                'image_digest': manifest['image_digest'], 'deterministic_qualification': qualification}

    async def validation_runs(self, binding, resource, revision, *, credential):
        """Bounded exact-revision workflow/job observations and selection output.

        This is a provider read through the existing credential boundary. Logs
        are interpreted only as the selection engine's structured output, never
        as instructions or as evidence of provider-action non-start.
        """
        repository = _repository_name(resource)
        data = await self.client.get_json(binding.base_url, f"repos/{repository}/actions/runs",
            token=credential, params={"head_sha": revision, "per_page": 100})
        rows = data.get("workflow_runs", []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or int(data.get("total_count", len(rows))) != len(rows) or len(rows) > 100:
            raise CodeHostError("Exact-revision workflow evidence exceeds inspection bound")
        latest = {}
        for row in rows:
            if row.get("head_sha") != revision:
                continue
            path = str(row.get("path") or "")
            if not path or not row.get("id"):
                raise CodeHostError("Workflow provenance is incomplete")
            order = (int(row.get("run_number", 0)), int(row.get("run_attempt", 0)), int(row["id"]))
            if path not in latest or order > latest[path][0]:
                latest[path] = (order, row)
        if len(latest) > 16:
            raise CodeHostError("Workflow evidence exceeds inspection bound")
        # Default setup is canonical provider configuration, not an expected-job
        # set guessed from whichever successful jobs happened to be observed.
        security_configuration = await self.client.get_json(binding.base_url,
            f"repos/{repository}/code-scanning/default-setup", token=credential)
        if not isinstance(security_configuration, dict):
            raise CodeHostError("Security applicability configuration is unavailable")
        security_configuration = {key: security_configuration.get(key)
            for key in ("state", "languages", "updated_at", "query_suite")}
        security_configuration["protocol_version"] = "github-codeql-default-setup/1"
        facts = []
        for _, row in latest.values():
            run_id = int(row["id"])
            jobs = await self.client.get_json(binding.base_url, f"repos/{repository}/actions/runs/{run_id}/jobs",
                token=credential, params={"per_page": 100, "filter": "latest"})
            job_rows = jobs.get("jobs", []) if isinstance(jobs, dict) else []
            if not isinstance(job_rows, list) or int(jobs.get("total_count", len(job_rows))) != len(job_rows) or len(job_rows) > 100:
                raise CodeHostError("Workflow job evidence exceeds inspection bound")
            if any(j.get("head_sha") != revision for j in job_rows):
                raise CodeHostError("Workflow job source revision is missing or inconsistent")
            checks = tuple(CodeHostCheckFact(external_id=str(j["id"]), name=str(j["name"]),
                state=str(j.get("status") or "unknown"), conclusion=j.get("conclusion"), revision=revision,
                web_url=j.get("html_url")) for j in job_rows)
            full = selected_revision = tested_revision = None
            if row["path"] == ".github/workflows/tests.yml":
                selections = [j for j in job_rows if j["name"] == "validation-selection"]
                if len(selections) == 1 and selections[0].get("conclusion") == "success":
                    log = await self.job_logs(binding, resource, int(selections[0]["id"]), credential=credential, max_bytes=320 * 1024)
                    if log.truncated:
                        raise CodeHostError("Selection log exceeds the bounded complete read; inspect canonical selection proof (truncated evidence is refused)")
                    candidates = []
                    for line in log.content.splitlines():
                        start = line.find('{')
                        if start < 0:
                            continue
                        try:
                            parsed = json.loads(line[start:])
                        except ValueError:
                            continue
                        if (parsed.get("full") in {"true", "false"}
                                and (parsed.get("head") == revision or parsed.get("tested_revision") == revision)):
                            candidates.append(parsed)
                    if len(candidates) != 1:
                        raise CodeHostError("Exact selection output is missing or ambiguous")
                    full = candidates[0]["full"] == "true"
                    selected_revision = revision
                    tested_revision = candidates[0].get("tested_revision")
            security_result = None
            if row['path'] == 'dynamic/github-code-scanning/codeql':
                results = await self.client.get_json(binding.base_url,
                    f'repos/{repository}/commits/{revision}/check-runs', token=credential,
                    params={'per_page':100, 'filter':'latest'})
                result_rows = results.get('check_runs', []) if isinstance(results, dict) else []
                if not isinstance(result_rows, list) or results.get('total_count') != len(result_rows) or len(result_rows) > 100:
                    raise CodeHostError('Security result check inventory is incomplete or exceeds inspection bound')
                scanning = [r for r in result_rows if r.get('name') == 'CodeQL'
                            and (r.get('app') or {}).get('slug') == 'github-advanced-security']
                if len(scanning) > 1:
                    raise CodeHostError('Native CodeQL result provenance is ambiguous')
                if scanning:
                    result = scanning[0]
                    if result.get('head_sha') != revision:
                        raise CodeHostError('Native CodeQL result does not identify the exact source')
                    security_result = {'protocol_version':'github-codeql-check/1', 'external_id':str(result['id']),
                        'issuer':result['app']['slug'], 'revision':revision, 'state':result.get('status'),
                        'conclusion':result.get('conclusion'), 'web_url':result.get('html_url')}
            recovery_publication = None
            if row['path'] == '.github/workflows/publish-release.yml' and row.get('conclusion') == 'success':
                recovery_publication = await self._recovery_publication(binding, resource, run_id, revision, credential=credential)
            facts.append(CodeHostValidationRunFact(external_id=str(run_id), workflow_path=row["path"],
                event=str(row.get("event") or "unknown"), revision=revision, head_branch=row.get("head_branch"),
                state=str(row.get("status") or "unknown"), conclusion=row.get("conclusion"),
                selection_full=full, selection_revision=selected_revision, tested_revision=tested_revision,
                control_revision=revision, checks=checks,
                security_configuration=security_configuration if row["path"] == "dynamic/github-code-scanning/codeql" else None,
                recovery_publication=recovery_publication, security_result=security_result))
        # A workflow_run control SHA may have advanced independently of its
        # producer. Find immutable evidence of the requested source without
        # relabelling or trusting the control revision as candidate identity.
        if (any(f.workflow_path == '.github/workflows/tests.yml' and f.event == 'push' and f.head_branch == 'main' for f in facts)
                and not any(f.workflow_path == '.github/workflows/publish-release.yml' for f in facts)):
            recent = await self.client.get_json(binding.base_url,
                f'repos/{repository}/actions/workflows/publish-release.yml/runs', token=credential,
                params={'per_page':20, 'branch':'main', 'event':'workflow_run'})
            candidates = recent.get('workflow_runs', []) if isinstance(recent, dict) else []
            if not isinstance(recent, dict) or 'total_count' not in recent or not isinstance(candidates, list) or len(candidates) > 20:
                raise CodeHostError('Recent recovery workflow inventory is unsupported')
            for row in candidates:
                if (row.get('path') != '.github/workflows/publish-release.yml' or not row.get('id')
                        or not re.fullmatch(r'[0-9a-f]{40}', str(row.get('head_sha', '')))):
                    raise CodeHostError('Recovery workflow provenance is incomplete')
                publication = await self._recovery_publication(binding, resource, int(row['id']), revision,
                    credential=credential, allow_absent=True)
                if publication is None:
                    continue
                jobs = await self.client.get_json(binding.base_url,
                    f"repos/{repository}/actions/runs/{int(row['id'])}/jobs", token=credential,
                    params={'per_page':100, 'filter':'latest'})
                job_rows = jobs.get('jobs', []) if isinstance(jobs, dict) else []
                if (not isinstance(job_rows, list) or jobs.get('total_count') != len(job_rows) or len(job_rows) > 100
                        or any(j.get('head_sha') != row.get('head_sha') for j in job_rows)):
                    raise CodeHostError('Recovery job evidence is incomplete or inconsistent')
                facts.append(CodeHostValidationRunFact(external_id=str(row['id']), workflow_path=row['path'],
                    event=str(row.get('event') or 'unknown'), revision=revision, control_revision=row.get('head_sha'),
                    head_branch=row.get('head_branch'), state=str(row.get('status') or 'unknown'), conclusion=row.get('conclusion'),
                    recovery_publication=publication, checks=tuple(CodeHostCheckFact(external_id=str(j['id']), name=str(j['name']),
                        state=str(j.get('status') or 'unknown'), conclusion=j.get('conclusion'), revision=revision,
                        web_url=j.get('html_url')) for j in job_rows)))
                break
        return tuple(facts)

    async def job_logs(
        self, binding: CodeHostProviderBinding, resource: Resource, job_id: int,
        *, credential: str | None, max_bytes: int,
    ) -> CodeHostJobLogFact:
        if not credential:
            raise CodeHostError("GitHub credential is required for job logs")
        repository = _repository_name(resource)
        try:
            raw, truncated, _ = await self.client.request_bytes(
                "GET", binding.base_url,
                f"repos/{quote(repository, safe='/')}/actions/jobs/{job_id}/logs",
                token=credential, max_bytes=max_bytes,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        content = raw.decode("utf-8", errors="replace")
        patterns = (
            re.compile(r"(?i)(authorization\s*:\s*(?:bearer|token)\s+)[^\s]+"),
            re.compile(r"(?i)((?:token|secret|password|api[_-]?key)\s*[=:]\s*)[^\s]+"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        )
        redacted = False
        for pattern in patterns:
            replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
            content, count = pattern.subn(replacement, content)
            redacted = redacted or count > 0
        return CodeHostJobLogFact(
            job_id=job_id, content=content, byte_count=len(raw),
            truncated=truncated, redacted=redacted,
        )

    async def artifacts(
        self, binding: CodeHostProviderBinding, resource: Resource, run_id: int,
        *, credential: str | None,
    ) -> tuple[CodeHostArtifactFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{quote(repository, safe='/')}/actions/runs/{run_id}/artifacts",
                token=credential, params={"per_page": 100},
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        rows = data.get("artifacts", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise CodeHostError("GitHub artifacts response is invalid")
        return tuple(
            CodeHostArtifactFact(
                external_id=str(item.get("id")), name=str(item.get("name")),
                size_bytes=int(item.get("size_in_bytes") or 0),
                expired=bool(item.get("expired")),
                created_at=_timestamp(item.get("created_at")),
                expires_at=_timestamp(item.get("expires_at")),
                web_url=item.get("archive_download_url"),
            )
            for item in rows
            if isinstance(item, dict) and item.get("id") and item.get("name")
        )

    async def artifact_download(
        self, binding: CodeHostProviderBinding, resource: Resource, artifact_id: int,
        *, credential: str | None, max_bytes: int,
    ) -> CodeHostArtifactDownloadFact:
        if not credential:
            raise CodeHostError("GitHub credential is required for artifact download")
        repository = _repository_name(resource)
        try:
            raw, truncated, media_type = await self.client.request_bytes(
                "GET", binding.base_url,
                f"repos/{quote(repository, safe='/')}/actions/artifacts/{artifact_id}/zip",
                token=credential, max_bytes=max_bytes,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        return CodeHostArtifactDownloadFact(
            external_id=str(artifact_id),
            content_base64=base64.b64encode(raw).decode("ascii"),
            byte_count=len(raw), truncated=truncated, media_type=media_type,
        )

    async def releases(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        *,
        credential: str | None,
    ) -> tuple[CodeHostReleaseFact, ...]:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/releases",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, list):
            raise CodeHostError("GitHub releases response is invalid")
        return tuple(
            CodeHostReleaseFact(
                external_id=str(item.get("id")),
                tag=str(item.get("tag_name") or ""),
                name=str(item.get("name") or item.get("tag_name") or ""),
                state=("draft" if item.get("draft") else "prerelease" if item.get("prerelease") else "published"),
                web_url=item.get("html_url"),
            )
            for item in data
            if isinstance(item, dict) and item.get("id") is not None and item.get("tag_name")
        )

    async def compare(
        self,
        binding: CodeHostProviderBinding,
        resource: Resource,
        base_revision: str,
        head_revision: str,
        *,
        credential: str | None,
    ) -> CodeHostCompareFact:
        repository = _repository_name(resource)
        try:
            data = await self.client.get_json(
                binding.base_url,
                f"repos/{repository}/compare/{quote(base_revision, safe='')}...{quote(head_revision, safe='')}",
                token=credential,
            )
        except Exception as exc:
            raise self._classify(exc) from exc
        if not isinstance(data, dict):
            raise CodeHostError("GitHub compare response is invalid")
        files = data.get("files") if isinstance(data.get("files"), list) else []
        return CodeHostCompareFact(
            base_revision=base_revision,
            head_revision=head_revision,
            ahead_by=data.get("ahead_by"),
            behind_by=data.get("behind_by"),
            total_commits=data.get("total_commits"),
            changed_files=tuple(
                str(item.get("filename"))
                for item in files
                if isinstance(item, dict) and item.get("filename")
            ),
        )

    def normalize_webhook(
        self,
        binding: CodeHostProviderBinding,
        payload: dict,
        *,
        event_kind: str,
        event_id: str,
    ) -> CodeHostWebhookFact:
        kind = str(event_kind or "").strip().casefold()
        repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
        subject = None
        canonical = CanonicalEventType.TASK_SOURCE
        action = payload.get("action")
        state = None
        web_url = None
        if kind == "pull_request":
            subject = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
            canonical = CanonicalEventType.PULL_REQUEST
            state = subject.get("state")
            web_url = subject.get("html_url")
        elif kind in {"check_run", "check_suite", "workflow_run", "workflow_job"}:
            key = kind
            subject = payload.get(key) if isinstance(payload.get(key), dict) else {}
            canonical = CanonicalEventType.CI_PIPELINE
            state = subject.get("status") or subject.get("conclusion")
            web_url = subject.get("html_url")
        elif kind in {"deployment", "deployment_status"}:
            subject = payload.get(kind) if isinstance(payload.get(kind), dict) else {}
            canonical = CanonicalEventType.DEPLOYMENT
            state = subject.get("state") or subject.get("status")
            web_url = subject.get("target_url") or subject.get("environment_url")
        if not isinstance(subject, dict):
            subject = {}
        return CodeHostWebhookFact(
            provider_type=self.provider_type,
            provider_instance=binding.provider_instance,
            event_id=event_id,
            event_kind=kind or "unknown",
            canonical_event_type=canonical.value,
            repository_external_id=(
                str(repository.get("id"))
                if repository.get("id") is not None
                else repository.get("full_name")
            ),
            subject_external_id=(
                str(subject.get("id")) if subject.get("id") is not None else None
            ),
            action=str(action) if action is not None else None,
            state=str(state) if state is not None else None,
            web_url=str(web_url) if web_url is not None else None,
            provider_payload={
                "repository_full_name": repository.get("full_name"),
                "sender_id": (
                    payload.get("sender", {}).get("id")
                    if isinstance(payload.get("sender"), dict)
                    else None
                ),
            },
        )
