from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import TypeVar
from urllib.parse import urlsplit

from codex_web.code_hosts import (
    validate_artifact_member_path,
    CodeHostArtifactDownloadFact,
    CodeHostArtifactFact,
    CodeHostCapability,
    CodeHostCheckFact,
    CodeHostValidationRunFact,
    CodeHostJobLogFact,
    CodeHostCommitFact,
    CodeHostCompareFact,
    CodeHostError,
    CodeHostProvider,
    CodeHostProviderBinding,
    CodeHostPullRequestFact,
    CodeHostRefFact,
    CodeHostReleaseFact,
    CodeHostRevisionMaterializationFact,
    CodeHostReviewFact,
    CodeHostRepositoryFact,
    CodeHostTransientError,
    CodeHostUnsupportedCapabilityError,
    CodeHostWebhookFact,
)
from codex_web.identity import AuthenticationActor
from codex_web.resources import Resource, ResourceType
from codex_web.services.canonical_events import (
    CanonicalEventDelivery,
    CanonicalEventIngestionService,
)
from codex_web.services.identity import TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker


T = TypeVar("T")


class CodeHostRegistry:
    """Runtime registry for provider adapters and tenant-scoped provider bindings."""

    def __init__(self) -> None:
        self._providers: dict[str, CodeHostProvider] = {}
        self._bindings: dict[str, CodeHostProviderBinding] = {}

    def register_provider(self, provider: CodeHostProvider) -> None:
        existing = self._providers.get(provider.provider_type)
        if existing is not None and existing is not provider:
            raise CodeHostError(
                f"code-host provider already registered: {provider.provider_type}"
            )
        self._providers[provider.provider_type] = provider

    def provider(self, provider_type: str) -> CodeHostProvider:
        """Resolve an installed adapter; a binding cannot create capabilities."""
        provider = self._providers.get(provider_type)
        if provider is None:
            raise CodeHostError(f"code-host provider unavailable: {provider_type}")
        return provider

    def register_binding(self, binding: CodeHostProviderBinding) -> None:
        provider = self._providers.get(binding.provider_type)
        if provider is None:
            raise CodeHostError(
                f"code-host provider unavailable: {binding.provider_type}"
            )
        unsupported = set(binding.capabilities) - set(provider.capabilities())
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise CodeHostUnsupportedCapabilityError(
                f"binding declares unsupported capabilities: {names}"
            )
        self._bindings[binding.id] = binding

    def resolve(
        self,
        binding_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostProviderBinding, CodeHostProvider]:
        binding = self._bindings.get(binding_id)
        if binding is None:
            raise CodeHostError("code-host binding not found")
        if (
            binding.organization_id != actor.organization_id
            or binding.workspace_id != actor.workspace_id
        ):
            raise TenantIsolationError("cross-tenant code-host binding access denied")
        provider = self._providers.get(binding.provider_type)
        if provider is None:
            raise CodeHostError(
                f"code-host provider unavailable: {binding.provider_type}"
            )
        return binding, provider


class CodeHostService:
    """Provider-read SCM facade with bounded local object materialization."""

    def __init__(
        self,
        registry: CodeHostRegistry,
        resources: ResourceCatalogService,
        *,
        secrets: SecretBroker | None = None,
        canonical_events: CanonicalEventIngestionService | None = None,
    ) -> None:
        self.registry = registry
        self.resources = resources
        self.secrets = secrets
        self.canonical_events = canonical_events
        self._materialization_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _require_capability(
        binding: CodeHostProviderBinding,
        provider: CodeHostProvider,
        capability: CodeHostCapability,
    ) -> None:
        if capability not in binding.capabilities or capability not in provider.capabilities():
            raise CodeHostUnsupportedCapabilityError(
                f"code-host capability unavailable: {capability.value}"
            )

    def _resource(
        self,
        resource_id: str,
        *,
        actor: AuthenticationActor,
        binding: CodeHostProviderBinding,
    ) -> Resource:
        resource = self.resources.get(resource_id, actor)
        if resource.resource_type != ResourceType.REPOSITORY:
            raise CodeHostError("code-host operations require a repository Resource")
        provenance = resource.provenance
        if provenance and provenance.provider:
            if provenance.provider.casefold() != binding.provider_type.casefold():
                raise CodeHostError(
                    "repository Resource provider provenance does not match code-host binding"
                )
            if (
                provenance.provider_instance
                and provenance.provider_instance != binding.provider_instance
            ):
                raise CodeHostError(
                    "repository Resource provider instance does not match code-host binding"
                )
        return resource

    async def _with_credential(
        self,
        binding: CodeHostProviderBinding,
        *,
        actor: AuthenticationActor,
        resource: Resource,
        operation: str,
        callback: Callable[[str | None], Awaitable[T]],
    ) -> T:
        if not binding.credential_ref:
            return await callback(None)
        if self.secrets is None:
            raise CodeHostError("secret broker is required for code-host credentials")
        return await self.secrets.use_async(
            binding.credential_ref,
            actor=actor,
            operation=operation,
            consumer=lambda value: callback(value),
            context={
                "binding_id": binding.id,
                "provider_type": binding.provider_type,
                "provider_instance": binding.provider_instance,
                "resource_id": resource.id,
            },
        )

    async def repository(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostRepositoryFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REPOSITORY_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.repository.read",
            callback=lambda credential: provider.repository(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def refs(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostRefFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REFS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.refs.read",
            callback=lambda credential: provider.refs(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def commit(
        self,
        binding_id: str,
        resource_id: str,
        revision: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostCommitFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.COMMITS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.commit.read",
            callback=lambda credential: provider.commit(
                binding,
                resource,
                revision,
                credential=credential,
            ),
        )

    async def materialize_default_branch_revision(
        self,
        binding_id: str,
        resource_id: str,
        revision: str,
        *,
        checkout: Path,
        actor: AuthenticationActor,
    ) -> CodeHostRevisionMaterializationFact:
        """Fetch one exact, provider-attested default-branch tip into a checkout."""
        requested = str(revision or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{40}", requested):
            raise CodeHostError("materialization requires an exact full commit revision")
        if not checkout.is_dir():
            raise CodeHostError("execution workspace checkout is unavailable")
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        for capability in (
            CodeHostCapability.REPOSITORY_READ,
            CodeHostCapability.REFS_READ,
            CodeHostCapability.COMMITS_READ,
        ):
            self._require_capability(binding, provider, capability)
        if not binding.credential_ref:
            raise CodeHostError("authenticated provider access is required for materialization")
        resource = self._resource(resource_id, actor=actor, binding=binding)
        lock_key = f"{resource.id}:{checkout.resolve()}"
        lock = self._materialization_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            async def materialize(credential: str | None) -> CodeHostRevisionMaterializationFact:
                if not credential:
                    raise CodeHostError(
                        "authenticated provider access is required for materialization"
                    )
                repository = await provider.repository(
                    binding, resource, credential=credential
                )
                if (
                    repository.resource_id != resource.id
                    or repository.provider_type.casefold() != binding.provider_type.casefold()
                    or repository.provider_instance != binding.provider_instance
                ):
                    raise CodeHostError("provider repository attestation changed")
                self._validate_repository_provenance(resource, binding, repository)
                branch = str(repository.default_branch or "").strip()
                if (
                    not branch
                    or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
                    or any(part in {"", ".", ".."} for part in branch.split("/"))
                ):
                    raise CodeHostError("provider repository has no safe default branch")
                refs = await provider.refs(binding, resource, credential=credential)
                matches = [
                    item for item in refs
                    if item.kind == "branch" and item.name == branch
                ]
                if len(matches) != 1 or matches[0].revision.casefold() != requested:
                    raise CodeHostError(
                        "requested revision is not the attested provider default-branch tip"
                    )
                commit = await provider.commit(
                    binding, resource, requested, credential=credential
                )
                if commit.revision.casefold() != requested:
                    raise CodeHostError("provider commit attestation changed")
                remote = self._verified_remote_url(binding, repository)
                already_present = await asyncio.to_thread(
                    self._git_has_commit, checkout, requested
                )
                if not already_present:
                    await asyncio.to_thread(
                        self._materialize_git_sync,
                        checkout,
                        remote,
                        branch,
                        requested,
                        credential,
                        binding.provider_type,
                        resource.id,
                    )
                return CodeHostRevisionMaterializationFact(
                    resource_id=resource.id,
                    provider_type=binding.provider_type,
                    provider_instance=binding.provider_instance,
                    repository_full_name=repository.full_name,
                    default_branch=branch,
                    revision=requested,
                    fetched=not already_present,
                )

            return await self._with_credential(
                binding,
                actor=actor,
                resource=resource,
                operation="code-host.revision.materialize",
                callback=materialize,
            )

    @staticmethod
    def _validate_repository_provenance(
        resource: Resource,
        binding: CodeHostProviderBinding,
        repository: CodeHostRepositoryFact,
    ) -> None:
        candidates: set[str] = set()
        provenance = resource.provenance
        if provenance and provenance.external_url:
            parsed = urlsplit(provenance.external_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != urlsplit(f"//{binding.provider_instance}").hostname
                or parsed.username
                or parsed.password
            ):
                raise CodeHostError("repository Resource URL provenance is invalid")
            path = parsed.path.strip("/")
            if path:
                candidates.add(path.removesuffix(".git"))
        if provenance and provenance.external_id:
            candidates.add(provenance.external_id.strip().strip("/"))
        candidates.update(
            alias.value.strip().strip("/").removesuffix(".git")
            for alias in resource.aliases
            if (alias.provider or "").casefold() == binding.provider_type.casefold()
            and alias.value.strip()
        )
        if not candidates or not (
            repository.full_name in candidates or repository.external_id in candidates
        ):
            raise CodeHostError("provider repository does not match Resource provenance")

    @staticmethod
    def _verified_remote_url(
        binding: CodeHostProviderBinding,
        repository: CodeHostRepositoryFact,
    ) -> str:
        if binding.provider_type.casefold() not in {"github", "gitlab"}:
            raise CodeHostError("provider does not support revision materialization")
        parsed = urlsplit(f"//{binding.provider_instance}")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise CodeHostError("provider instance is not a safe repository host")
        full_name = repository.full_name.strip().strip("/")
        if (
            not full_name
            or len(full_name) > 512
            or any(part in {"", ".", ".."} for part in full_name.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", full_name)
        ):
            raise CodeHostError("provider repository locator is invalid")
        web_url = repository.web_url
        if not web_url:
            if binding.provider_type.casefold() != "github":
                raise CodeHostError("provider repository has no attested clone endpoint")
            web_url = f"https://{binding.provider_instance}/{full_name}"
        endpoint = urlsplit(web_url)
        try:
            same_authority = (
                endpoint.hostname == parsed.hostname
                and (endpoint.port or 443) == (parsed.port or 443)
            )
        except ValueError as exc:
            raise CodeHostError("provider repository endpoint is invalid") from exc
        path = endpoint.path.rstrip("/").removesuffix(".git")
        if (
            endpoint.scheme != "https" or not same_authority
            or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment
            or not path.endswith("/" + full_name)
            or not re.fullmatch(r"/[A-Za-z0-9._/-]+", path)
            or any(part in {"", ".", ".."} for part in path[1:].split("/"))
        ):
            raise CodeHostError("provider repository endpoint is invalid")
        return f"https://{binding.provider_instance}{path}.git"

    @staticmethod
    def _git_has_commit(checkout: Path, revision: str) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(checkout), "cat-file", "-e", f"{revision}^{{commit}}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodeHostTransientError(
                "execution workspace Git object inspection failed"
            ) from exc
        return result.returncode == 0

    @staticmethod
    def _materialize_git_sync(
        checkout: Path,
        remote: str,
        branch: str,
        revision: str,
        credential: str,
        provider_type: str,
        resource_id: str,
    ) -> None:
        internal_ref = (
            "refs/codex/materialized/"
            + hashlib.sha256(f"{resource_id}:{branch}".encode()).hexdigest()
        )
        with tempfile.TemporaryDirectory(prefix="codex-materialize-") as root:
            root_path = Path(root)
            askpass = root_path / "askpass.sh"
            username = "x-access-token" if provider_type.casefold() == "github" else "oauth2"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                f"  *Username*) printf '%s\\n' {username} ;;\n"
                "  *Password*) printf '%s\\n' \"$CODEX_MATERIALIZE_TOKEN\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            os.chmod(askpass, 0o700)
            environment = {
                key: os.environ[key]
                for key in (
                    "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                )
                if key in os.environ
            }
            environment.update(
                {
                    "HOME": str(root_path),
                    "CODEX_MATERIALIZE_TOKEN": credential,
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                }
            )
            # Never expose provider credentials to checkout-local config,
            # includes, URL rewrites, transport helpers or object alternates.
            isolated = root_path / "objects.git"
            safe_environment = {
                key: value for key, value in environment.items()
                if key not in {"CODEX_MATERIALIZE_TOKEN", "GIT_ASKPASS"}
            }
            isolated_git = ["git", "--git-dir", str(isolated)]
            command = [
                *isolated_git,
                "-c", "core.hooksPath=/dev/null",
                "-c", "credential.helper=",
                "-c", "http.followRedirects=false",
                "-c", "credential.useHttpPath=true",
                "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always",
                "-c", "submodule.recurse=false",
                "fetch", "--no-tags", "--no-recurse-submodules", "--depth=1",
                remote, f"+refs/heads/{branch}:{internal_ref}",
            ]
            try:
                subprocess.run(
                    ["git", "init", "--bare", "--template=", str(isolated)],
                    env=safe_environment, check=True, capture_output=True,
                    text=True, timeout=30,
                )
                subprocess.run(
                    command,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                observed = subprocess.run(
                    [*isolated_git, "rev-parse", "--verify", f"{internal_ref}^{{commit}}"],
                    env=safe_environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                ).stdout.strip().casefold()
                if observed != revision:
                    raise CodeHostError("provider default branch changed during materialization")
                # Import only the verified object pack. This step has no token,
                # network transport, reference updates or working-tree writes.
                with (root_path / "verified.pack").open("w+b") as pack:
                    subprocess.run(
                        [*isolated_git, "pack-objects", "--stdout", "--revs"],
                        input=(revision + "\n").encode(), stdout=pack,
                        stderr=subprocess.PIPE, env=safe_environment,
                        check=True, timeout=120,
                    )
                    pack.seek(0)
                    subprocess.run(
                        ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null",
                         "-c", "gc.auto=0", "index-pack", "--stdin"],
                        stdin=pack, capture_output=True, env=safe_environment,
                        check=True, timeout=120,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                raise CodeHostTransientError(
                    "provider revision materialization failed"
                ) from exc

    async def pull_request(
        self,
        binding_id: str,
        resource_id: str,
        external_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostPullRequestFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.PULL_REQUEST_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.pull-request.read",
            callback=lambda credential: provider.pull_request(
                binding,
                resource,
                external_id,
                credential=credential,
            ),
        )

    async def pull_requests(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
        state: str = "open",
    ) -> tuple[CodeHostPullRequestFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.PULL_REQUEST_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        normalized_state = str(state or "open").strip().casefold()
        if normalized_state not in {"open", "closed", "all"}:
            raise CodeHostError("pull-request state must be open, closed, or all")
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.pull-requests.read",
            callback=lambda credential: provider.pull_requests(
                binding,
                resource,
                credential=credential,
                state=normalized_state,
            ),
        )

    async def reviews(
        self,
        binding_id: str,
        resource_id: str,
        external_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostReviewFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.REVIEW_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.reviews.read",
            callback=lambda credential: provider.reviews(
                binding,
                resource,
                external_id,
                credential=credential,
            ),
        )

    async def checks(
        self,
        binding_id: str,
        resource_id: str,
        revision: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostCheckFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.CHECKS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.checks.read",
            callback=lambda credential: provider.checks(
                binding,
                resource,
                revision,
                credential=credential,
            ),
        )

    async def validation_runs(self, binding_id: str, resource_id: str, revision: str, *, actor: AuthenticationActor) -> tuple[CodeHostValidationRunFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        for capability in (CodeHostCapability.CHECKS_READ, CodeHostCapability.JOB_LOGS_READ, CodeHostCapability.ARTIFACTS_READ):
            self._require_capability(binding, provider, capability)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        if not callable(getattr(provider, "validation_runs", None)):
            raise CodeHostUnsupportedCapabilityError("Provider has no qualified validation observation adapter")
        try:
            return await self._with_credential(binding, actor=actor, resource=resource,
                operation="code-host.validation.read", callback=lambda credential: provider.validation_runs(
                    binding, resource, revision, credential=credential))
        except (KeyError, TypeError, AttributeError, UnicodeError) as exc:
            raise CodeHostError("Validation provider protocol is incomplete or unsupported; inspect canonical evidence") from exc

    async def job_logs(
        self, binding_id: str, resource_id: str, job_id: int, *,
        actor: AuthenticationActor, max_bytes: int,
    ) -> CodeHostJobLogFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.JOB_LOGS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding, actor=actor, resource=resource,
            operation="code-host.job-logs.read",
            callback=lambda credential: provider.job_logs(
                binding, resource, job_id, credential=credential, max_bytes=max_bytes
            ),
        )

    async def artifacts(
        self, binding_id: str, resource_id: str, run_id: int, *, actor: AuthenticationActor,
    ) -> tuple[CodeHostArtifactFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.ARTIFACTS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            raise CodeHostError("run identifiers must be positive integers")
        return await self._with_credential(
            binding, actor=actor, resource=resource,
            operation="code-host.artifacts.read",
            callback=lambda credential: provider.artifacts(
                binding, resource, run_id, credential=credential
            ),
        )

    async def artifact_download(
        self, binding_id: str, resource_id: str, artifact_id: int, *,
        actor: AuthenticationActor, max_bytes: int, member_path: str | None = None,
    ) -> CodeHostArtifactDownloadFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.ARTIFACTS_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id < 1:
            raise CodeHostError("artifact identifiers must be positive integers")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 320 * 1024:
            raise CodeHostError("max_bytes must be between 1 and 327680")
        download = provider.artifact_download
        options = {}
        if member_path is not None:
            validate_artifact_member_path(member_path)
            download = getattr(provider, "artifact_member", None)
            if not callable(download):
                raise CodeHostUnsupportedCapabilityError("provider does not support artifact member reads")
            options["member_path"] = member_path
        return await self._with_credential(
            binding, actor=actor, resource=resource,
            operation="code-host.artifact.download",
            callback=lambda credential: download(
                binding, resource, artifact_id,
                credential=credential, max_bytes=max_bytes, **options,
            ),
        )

    async def releases(
        self,
        binding_id: str,
        resource_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[CodeHostReleaseFact, ...]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.RELEASE_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.releases.read",
            callback=lambda credential: provider.releases(
                binding,
                resource,
                credential=credential,
            ),
        )

    async def compare(
        self,
        binding_id: str,
        resource_id: str,
        base_revision: str,
        head_revision: str,
        *,
        actor: AuthenticationActor,
    ) -> CodeHostCompareFact:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.COMPARE_READ)
        resource = self._resource(resource_id, actor=actor, binding=binding)
        return await self._with_credential(
            binding,
            actor=actor,
            resource=resource,
            operation="code-host.compare.read",
            callback=lambda credential: provider.compare(
                binding,
                resource,
                base_revision,
                head_revision,
                credential=credential,
            ),
        )

    async def ingest_webhook(
        self,
        binding_id: str,
        *,
        actor: AuthenticationActor,
        event_kind: str,
        event_id: str,
        payload: dict,
    ) -> tuple[CodeHostWebhookFact, CanonicalEventDelivery | None]:
        binding, provider = self.registry.resolve(binding_id, actor=actor)
        self._require_capability(binding, provider, CodeHostCapability.WEBHOOK_NORMALIZE)
        fact = provider.normalize_webhook(
            binding,
            payload,
            event_kind=event_kind,
            event_id=event_id,
        )
        if self.canonical_events is None:
            return fact, None
        delivery = await self.canonical_events.ingest(
            event_type=fact.canonical_event_type,
            source=f"code-host:{binding.provider_type}:{binding.provider_instance}",
            idempotency_key=f"{binding.provider_instance}:{fact.event_id}",
            payload={
                "provider_type": fact.provider_type,
                "provider_instance": fact.provider_instance,
                "event_kind": fact.event_kind,
                "repository_external_id": fact.repository_external_id,
                "subject_external_id": fact.subject_external_id,
                "action": fact.action,
                "state": fact.state,
                "web_url": fact.web_url,
                "provider_payload": fact.provider_payload,
            },
            occurred_at=fact.occurred_at,
            tenant_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )
        return fact, delivery
