from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from typing import Any

from codex_web.failures import (
    FailureReason,
    FailureRecord,
    create_failure,
)
from codex_web.entitlements import (
    METRIC_MODEL_COST_USD,
    METRIC_MODEL_INPUT_TOKENS,
    METRIC_MODEL_OUTPUT_TOKENS,
    UsageEventCreate,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.input_plugins import (
    InputEnvelope,
    InputMessage as PluginInputMessage,
    InputPipelineResult,
    InputPluginPipeline,
)
from codex_web.model_gateway import (
    ModelAccessSource,
    ModelAuthenticationStatus,
    ModelAvailabilitySource,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    ModelCatalogStatus,
    ModelDefinitionRecord,
    ModelDefinitionUpsert,
    ModelGatewayState,
    ModelInvocationAttempt,
    ModelGoalUsage,
    ModelDecisionUsage,
    ModelInvocationRecord,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ModelLifecycle,
    ModelMessage,
    ModelProviderRecord,
    ModelProviderEligibility,
    ModelProviderHealth,
    ModelProviderStatus,
    ModelProviderUpsert,
    ModelRouteCandidate,
    ModelRouteExclusion,
    ModelRouteResult,
    PromptTemplateRecord,
    PromptTemplateUpsert,
    TenantModelPolicy,
    TenantModelPolicyUpdate,
)
from codex_web.model_qualification import (
    CriticIndependenceLevel,
    ModelQualificationRevision,
    ModelQualificationStatus,
    ModelQualificationUpdate,
    ModelRoutingDefinitionCreate,
    ModelRoutingDefinitionRevision,
    ModelRoutingRollback,
    ModelRoutingRole,
    WorkloadEvaluationProfile,
    WorkloadEvaluationProfileUpsert,
)
from codex_web.model_providers import (
    ModelProviderAdapter,
    ModelProviderAdapterError,
    ModelProviderCapacityError,
    ModelProviderTransientError,
)
from codex_web.observability import current_correlation
from codex_web.provider_capacity import (
    ProviderCapacityReport,
    ProviderCapacityWaitCreate,
)
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.security import TrustZone, envelope_untrusted, render_untrusted_content
from codex_web.services.entitlements import EntitlementService
from codex_web.services.identity import AuthorizationError
from codex_web.services.secrets import SecretBroker
from codex_web.secrets import SecretStatus
from codex_web.storage.model_gateway import ModelGatewayStore


class ModelGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure: FailureRecord | None = None,
    ) -> None:
        self.failure = failure
        super().__init__(message)


class ModelRoutingError(ModelGatewayError):
    pass


class ModelRegistryConflictError(ModelGatewayError):
    pass


class ModelProviderUnavailableError(ModelGatewayError):
    pass


class ModelCapacityRoutingError(ModelRoutingError):
    def __init__(
        self,
        message: str,
        *,
        retry_at: float | None,
        provider_keys: tuple[str, ...],
    ) -> None:
        self.retry_at = retry_at
        self.provider_keys = provider_keys
        super().__init__(message)


class ModelProviderCapacityUnavailableError(ModelProviderUnavailableError):
    def __init__(
        self,
        message: str,
        *,
        retry_at: float | None,
        wait_id: str | None = None,
        failure: FailureRecord | None = None,
    ) -> None:
        self.retry_at = retry_at
        self.wait_id = wait_id
        super().__init__(message, failure=failure)


InputPipelineResolver = Callable[
    [ModelInvocationRequest, AuthenticationActor],
    InputPluginPipeline | None,
]
EvaluationRunResolver = Callable[..., Any]
RoutingBaselineResolver = Callable[[AuthenticationActor], Any]


class ModelGatewayService:
    MODEL_CATALOG_MAX_ENTRIES = 10_000
    def __init__(
        self,
        store: ModelGatewayStore,
        *,
        secret_broker: SecretBroker | None = None,
        entitlements: EntitlementService | None = None,
        input_pipeline: InputPluginPipeline | None = None,
        input_pipeline_resolver: InputPipelineResolver | None = None,
        provider_capacity: ProviderCapacityService | None = None,
        evaluation_run_resolver: EvaluationRunResolver | None = None,
        routing_baseline_resolver: RoutingBaselineResolver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if input_pipeline is not None and input_pipeline_resolver is not None:
            raise ValueError(
                "configure either input_pipeline or input_pipeline_resolver, not both"
            )
        self.store = store
        self.secret_broker = secret_broker
        self.entitlements = entitlements
        self.input_pipeline = input_pipeline
        self.input_pipeline_resolver = input_pipeline_resolver
        self.provider_capacity = provider_capacity
        self.evaluation_run_resolver = evaluation_run_resolver
        self.routing_baseline_resolver = routing_baseline_resolver
        self.clock = clock
        self.adapters: dict[str, ModelProviderAdapter] = {}

    @staticmethod
    def _input_envelope(
        request: ModelInvocationRequest,
        actor: AuthenticationActor,
    ) -> InputEnvelope:
        return InputEnvelope(
            request_id=f"model-input-{uuid.uuid4().hex}",
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            actor_id=actor.identity_id,
            model_class=request.model_class,
            messages=tuple(
                PluginInputMessage(role=item.role, content=item.content)
                for item in request.messages
            ),
            system_prompt=request.system_prompt,
            text_verbosity=request.text_verbosity,
            reasoning_effort=request.reasoning_effort,
            prompt_template_id=request.prompt_template_id,
            prompt_template_version=request.prompt_template_version,
            required_capabilities=request.required_capabilities,
            required_residency_tags=request.required_residency_tags,
            required_compliance_tags=request.required_compliance_tags,
            preferred_provider_ids=request.preferred_provider_ids,
            max_output_tokens=request.max_output_tokens,
            timeout_seconds=request.timeout_seconds,
            max_cost_usd=request.max_cost_usd,
            allow_fallback=request.allow_fallback,
            work_item_ref=request.work_item_ref,
            goal_id=request.goal_id,
            decision_id=request.decision_id,
            execution_id=request.execution_id,
            purpose=request.purpose,
        )

    @staticmethod
    def _plugin_guidance(result: InputPipelineResult) -> str:
        sections: list[str] = []
        for block in result.envelope.context_blocks:
            sections.append(
                render_untrusted_content(
                    envelope_untrusted(
                        TrustZone.TOOL_OUTPUT,
                        f"input-plugin-context:{block.source}:{block.id}",
                        block.content,
                    )
                )
            )
        if result.envelope.output_contract:
            sections.append(
                render_untrusted_content(
                    envelope_untrusted(
                        TrustZone.TOOL_OUTPUT,
                        "input-plugin-output-contract",
                        json.dumps(
                            result.envelope.output_contract,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                    )
                )
            )
        return "\n\n".join(sections)

    async def _compose_input(
        self,
        request: ModelInvocationRequest,
        *,
        actor: AuthenticationActor,
    ) -> tuple[ModelInvocationRequest, InputPipelineResult | None]:
        pipeline = self.input_pipeline
        if self.input_pipeline_resolver is not None:
            pipeline = self.input_pipeline_resolver(request, actor)
        if pipeline is None:
            return request, None
        result = await pipeline.execute(
            self._input_envelope(request, actor)
        )
        guidance = self._plugin_guidance(result)
        system_prompt = result.envelope.system_prompt
        if guidance:
            system_prompt = (
                f"{system_prompt}\n\n{guidance}" if system_prompt else guidance
            )
        effective = request.model_copy(
            update={
                "model_class": result.envelope.model_class,
                "messages": tuple(
                    ModelMessage(role=item.role, content=item.content)
                    for item in result.envelope.messages
                ),
                "system_prompt": system_prompt,
                "text_verbosity": result.envelope.text_verbosity,
                "reasoning_effort": result.envelope.reasoning_effort,
                "preferred_provider_ids": result.envelope.preferred_provider_ids,
                "max_output_tokens": result.envelope.max_output_tokens,
                "max_cost_usd": result.envelope.max_cost_usd,
            }
        )
        return ModelInvocationRequest.model_validate(
            effective.model_dump(mode="python")
        ), result

    @staticmethod
    def _provider_failure(
        exc: Exception,
        *,
        provider: ModelProviderRecord,
        model: ModelDefinitionRecord,
        request: ModelInvocationRequest,
        attempt: int,
    ) -> FailureRecord:
        correlation = current_correlation()
        reason = getattr(
            exc,
            "reason_code",
            FailureReason.UNCLASSIFIED,
        )
        return create_failure(
            reason,
            source_subsystem="model_gateway",
            provider_id=provider.id,
            execution_id=request.execution_id,
            correlation_id=(
                correlation.correlation_id
                if correlation is not None
                else None
            ),
            causation_id=(
                correlation.causation_id
                if correlation is not None
                else None
            ),
            source_native_code=type(exc).__name__,
            source_native_status=getattr(
                exc,
                "source_native_status",
                None,
            ),
            attempt=attempt,
            details={
                "model_id": model.id,
                "concrete_model": model.concrete_model,
                "adapter_type": provider.adapter_type,
            },
        )

    def register_adapter(self, adapter: ModelProviderAdapter) -> None:
        existing = self.adapters.get(adapter.adapter_type)
        if existing is not None and existing is not adapter:
            raise ModelRegistryConflictError(
                f"model adapter already registered: {adapter.adapter_type}"
            )
        self.adapters[adapter.adapter_type] = adapter
        for alias in ("openai-compatible", "ollama"):
            if adapter.adapter_type == "openai":
                self.adapters.setdefault(alias, adapter)

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "model-gateway:admin" not in actor.service_scopes:
                raise AuthorizationError("model-gateway:admin service scope required")
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    @staticmethod
    def _render_system_prompt(
        template: PromptTemplateRecord,
        request: ModelInvocationRequest,
    ) -> str:
        content = template.content
        markers = ("{{ instructions }}", "{{instructions}}")
        if any(marker in content for marker in markers):
            for marker in markers:
                content = content.replace(marker, request.system_prompt)
            return content
        if request.system_prompt:
            return f"{content}\n\n{request.system_prompt}"
        return content

    @staticmethod
    def _estimate_tokens(
        request: ModelInvocationRequest,
        *,
        rendered_system_prompt: str | None = None,
    ) -> int:
        system_prompt = (
            request.system_prompt
            if rendered_system_prompt is None
            else rendered_system_prompt
        )
        chars = len(system_prompt) + sum(len(item.content) for item in request.messages)
        return max(1, math.ceil(chars / 4))

    @staticmethod
    def _rendered_prompt_hash(request: ModelInvocationRequest) -> str:
        payload = request.system_prompt + "\n" + "\n".join(
            f"{item.role}:{item.content}" for item in request.messages
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _policy_hash(policy: TenantModelPolicy) -> str:
        payload = policy.model_dump(
            mode="json",
            exclude={"updated_by", "updated_at"},
        )
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _price(
        model: ModelDefinitionRecord,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        if (
            model.input_price_per_million_usd is None
            or model.output_price_per_million_usd is None
        ):
            return None
        return (
            input_tokens * model.input_price_per_million_usd
            + output_tokens * model.output_price_per_million_usd
        ) / 1_000_000.0

    @staticmethod
    def _pricing_revision(model: ModelDefinitionRecord) -> str | None:
        if (
            model.input_price_per_million_usd is None
            or model.output_price_per_million_usd is None
        ):
            return None
        payload = {
            "model_id": model.id,
            "concrete_model": model.concrete_model,
            "model_version": model.model_version,
            "input_price_per_million_usd": model.input_price_per_million_usd,
            "output_price_per_million_usd": model.output_price_per_million_usd,
            "definition_updated_at": model.updated_at,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _policy(state: ModelGatewayState, actor: AuthenticationActor) -> TenantModelPolicy:
        existing = next(
            (
                item
                for item in state.policies
                if ModelGatewayService._same_scope(item, actor)
            ),
            None,
        )
        return existing or TenantModelPolicy(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            updated_by="system-default",
        )

    @staticmethod
    def _provider(
        state: ModelGatewayState,
        provider_id: str,
        actor: AuthenticationActor,
    ) -> ModelProviderRecord:
        item = next(
            (
                provider
                for provider in state.providers
                if provider.id == provider_id
                and ModelGatewayService._same_scope(provider, actor)
            ),
            None,
        )
        if item is None:
            raise ModelRoutingError(f"model provider not found: {provider_id}")
        return item

    @staticmethod
    def _template(
        state: ModelGatewayState,
        request: ModelInvocationRequest,
        actor: AuthenticationActor,
    ) -> PromptTemplateRecord:
        versions = [
            item
            for item in state.prompt_templates
            if item.template_id == request.prompt_template_id
            and ModelGatewayService._same_scope(item, actor)
            and (
                request.prompt_template_version is None
                or item.version == request.prompt_template_version
            )
            and item.active
        ]
        if not versions:
            raise ModelRoutingError(
                f"active prompt template not found: {request.prompt_template_id}"
            )
        versions.sort(key=lambda item: (item.updated_at, item.version), reverse=True)
        return versions[0]

    def list_providers(self, actor: AuthenticationActor) -> list[ModelProviderRecord]:
        return sorted(
            [
                item
                for item in self.store.load().providers
                if self._same_scope(item, actor)
            ],
            key=lambda item: item.id,
        )

    def list_models(self, actor: AuthenticationActor) -> list[ModelDefinitionRecord]:
        return sorted(
            [item for item in self.store.load().models if self._same_scope(item, actor)],
            key=lambda item: (item.route_priority, item.id),
        )

    def list_catalogs(self, actor: AuthenticationActor) -> list[ModelCatalogSnapshot]:
        now = self.clock()
        result = []
        for item in self.store.load().catalogs:
            if not self._same_scope(item, actor):
                continue
            if (
                item.status == ModelCatalogStatus.READY
                and item.expires_at is not None
                and item.expires_at <= now
            ):
                item = item.model_copy(update={"status": ModelCatalogStatus.STALE})
            result.append(item)
        return sorted(result, key=lambda item: item.provider_id)

    @staticmethod
    def _catalog_revision(entries: tuple[ModelCatalogEntry, ...]) -> str:
        payload = [item.model_dump(mode="json") for item in entries]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _save_catalog(
        self,
        snapshot: ModelCatalogSnapshot,
        *,
        actor: AuthenticationActor,
    ) -> ModelCatalogSnapshot:
        def apply(state: ModelGatewayState) -> ModelGatewayState:
            state.catalogs = [
                item for item in state.catalogs
                if not (
                    item.provider_id == snapshot.provider_id
                    and self._same_scope(item, actor)
                )
            ]
            state.catalogs.append(snapshot)
            return state
        self.store.update(apply)
        return snapshot

    async def refresh_catalog(
        self,
        provider_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ModelCatalogSnapshot:
        self._require_admin(actor)
        state = self.store.load()
        provider = self._provider(state, provider_id, actor)
        now = self.clock()
        existing = next(
            (
                item for item in state.catalogs
                if item.provider_id == provider.id and self._same_scope(item, actor)
            ),
            None,
        )
        if not provider.catalog_discovery_enabled:
            return self._save_catalog(ModelCatalogSnapshot(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                provider_id=provider.id,
                status=ModelCatalogStatus.ERROR,
                entries=existing.entries if existing else (),
                revision=existing.revision if existing else None,
                discovered_at=existing.discovered_at if existing else None,
                expires_at=existing.expires_at if existing else None,
                error="provider catalog discovery is disabled; static definitions are explicit fallback",
                updated_by=actor.identity_id,
                updated_at=now,
            ), actor=actor)
        adapter = self.adapters.get(provider.adapter_type)
        discover = getattr(adapter, "discover_models", None)

        async def call(credential: str | None):
            if not callable(discover):
                raise ModelProviderUnavailableError(
                    f"model adapter does not support catalog discovery: {provider.adapter_type}"
                )
            return await asyncio.wait_for(
                discover(provider, credential=credential), timeout=30.0
            )

        try:
            if provider.credential_ref:
                if self.secret_broker is None:
                    raise ModelProviderUnavailableError(
                        "secret broker is required for provider catalog discovery"
                    )
                discovered = await self.secret_broker.use_async(
                    provider.credential_ref,
                    actor=actor,
                    operation="model-gateway.catalog-discover",
                    consumer=lambda secret: call(secret),
                    context={"provider_id": provider.id},
                )
            else:
                if provider.credential_required:
                    raise ModelProviderUnavailableError(
                        f"provider {provider.id} requires credential_ref"
                    )
                discovered = await call(None)
            normalized: dict[str, ModelCatalogEntry] = {}
            for index, item in enumerate(discovered):
                if index >= self.MODEL_CATALOG_MAX_ENTRIES:
                    raise ModelProviderUnavailableError(
                        f"provider catalog exceeds {self.MODEL_CATALOG_MAX_ENTRIES} entries"
                    )
                entry = ModelCatalogEntry.model_validate(item)
                normalized[entry.concrete_model] = entry
            entries = tuple(normalized[key] for key in sorted(normalized))
            snapshot = ModelCatalogSnapshot(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                provider_id=provider.id,
                status=ModelCatalogStatus.READY,
                entries=entries,
                revision=self._catalog_revision(entries),
                discovered_at=now,
                expires_at=now + provider.catalog_ttl_seconds,
                updated_by=actor.identity_id,
                updated_at=now,
            )
        except Exception as exc:
            snapshot = ModelCatalogSnapshot(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                provider_id=provider.id,
                status=ModelCatalogStatus.ERROR,
                entries=existing.entries if existing else (),
                revision=existing.revision if existing else None,
                discovered_at=existing.discovered_at if existing else None,
                expires_at=existing.expires_at if existing else None,
                error=f"{type(exc).__name__}: {str(exc)[:400]}",
                updated_by=actor.identity_id,
                updated_at=now,
            )
        return self._save_catalog(snapshot, actor=actor)

    def list_templates(self, actor: AuthenticationActor) -> list[PromptTemplateRecord]:
        return sorted(
            [
                item
                for item in self.store.load().prompt_templates
                if self._same_scope(item, actor)
            ],
            key=lambda item: (item.template_id, item.version),
        )

    def get_policy(self, actor: AuthenticationActor) -> TenantModelPolicy:
        return self._policy(self.store.load(), actor)

    def upsert_provider(
        self,
        payload: ModelProviderUpsert,
        *,
        actor: AuthenticationActor,
        expected_revision: str | None = None,
    ) -> ModelProviderRecord:
        self._require_admin(actor)
        updated: list[ModelProviderRecord] = []

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            existing = next(
                (
                    item
                    for item in state.providers
                    if item.id == payload.id and self._same_scope(item, actor)
                ),
                None,
            )
            from codex_web.services.model_provider_administration import provider_fingerprint
            if expected_revision is not None and expected_revision != provider_fingerprint(existing):
                raise ModelRegistryConflictError("model provider binding changed; reload and review again")
            now = time.time()
            item = ModelProviderRecord(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                updated_by=actor.identity_id,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            state.providers = [
                value
                for value in state.providers
                if not (value.id == item.id and self._same_scope(value, actor))
            ]
            state.providers.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def upsert_model(
        self,
        payload: ModelDefinitionUpsert,
        *,
        actor: AuthenticationActor,
    ) -> ModelDefinitionRecord:
        self._require_admin(actor)
        updated: list[ModelDefinitionRecord] = []

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            provider = self._provider(state, payload.provider_id, actor)
            if (
                payload.availability_source == ModelAvailabilitySource.DISCOVERED
                and not provider.catalog_discovery_enabled
            ):
                raise ModelRegistryConflictError(
                    "discovered model availability requires provider catalog discovery"
                )
            existing = next(
                (
                    item
                    for item in state.models
                    if item.id == payload.id and self._same_scope(item, actor)
                ),
                None,
            )
            now = time.time()
            item = ModelDefinitionRecord(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                updated_by=actor.identity_id,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            state.models = [
                value
                for value in state.models
                if not (value.id == item.id and self._same_scope(value, actor))
            ]
            state.models.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def upsert_template(
        self,
        payload: PromptTemplateUpsert,
        *,
        actor: AuthenticationActor,
    ) -> PromptTemplateRecord:
        self._require_admin(actor)
        updated: list[PromptTemplateRecord] = []

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            existing = next(
                (
                    item
                    for item in state.prompt_templates
                    if item.template_id == payload.template_id
                    and item.version == payload.version
                    and self._same_scope(item, actor)
                ),
                None,
            )
            checksum = PromptTemplateRecord.checksum(payload.content)
            if existing is not None and existing.checksum_sha256 != checksum:
                raise ModelRegistryConflictError(
                    "prompt template version content is immutable; publish a new version"
                )
            now = time.time()
            item = PromptTemplateRecord(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                checksum_sha256=checksum,
                updated_by=actor.identity_id,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            state.prompt_templates = [
                value
                for value in state.prompt_templates
                if not (
                    value.template_id == item.template_id
                    and value.version == item.version
                    and self._same_scope(value, actor)
                )
            ]
            state.prompt_templates.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def set_policy(
        self,
        payload: TenantModelPolicyUpdate,
        *,
        actor: AuthenticationActor,
    ) -> TenantModelPolicy:
        self._require_admin(actor)
        updated: list[TenantModelPolicy] = []

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            item = TenantModelPolicy(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                updated_by=actor.identity_id,
            )
            state.policies = [
                value for value in state.policies if not self._same_scope(value, actor)
            ]
            state.policies.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def list_evaluation_profiles(
        self,
        actor: AuthenticationActor,
    ) -> list[WorkloadEvaluationProfile]:
        return sorted(
            [
                item
                for item in self.store.load().evaluation_profiles
                if self._same_scope(item, actor)
            ],
            key=lambda item: (item.workload_class, item.revision),
        )

    def routing_baseline(self, actor: AuthenticationActor) -> dict[str, Any]:
        if self.routing_baseline_resolver is None:
            raise ModelRegistryConflictError("routing baseline is unavailable")
        baseline, reference = self.routing_baseline_resolver(actor)
        return {
            "item": baseline.model_dump(mode="json"),
            "definition": reference.model_dump(mode="json"),
        }

    def upsert_evaluation_profile(
        self,
        payload: WorkloadEvaluationProfileUpsert,
        *,
        actor: AuthenticationActor,
    ) -> WorkloadEvaluationProfile:
        self._require_admin(actor)
        created: list[WorkloadEvaluationProfile] = []

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            prior = [
                item
                for item in state.evaluation_profiles
                if item.workload_class == payload.workload_class
                and self._same_scope(item, actor)
            ]
            item = WorkloadEvaluationProfile(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                revision=max((value.revision for value in prior), default=0) + 1,
                created_by=actor.identity_id,
                created_at=self.clock(),
            )
            state.evaluation_profiles.append(item)
            created.append(item)
            return state

        self.store.update(apply)
        return created[0]

    def list_qualifications(
        self,
        actor: AuthenticationActor,
    ) -> list[ModelQualificationRevision]:
        return sorted(
            [
                item
                for item in self.store.load().qualifications
                if self._same_scope(item, actor)
            ],
            key=lambda item: (item.model_id, item.workload_class, item.revision),
        )

    def _qualification_evidence(
        self,
        payload: ModelQualificationUpdate,
        *,
        actor: AuthenticationActor,
        profile: WorkloadEvaluationProfile,
    ) -> None:
        if not payload.evaluation_run_ids:
            return
        if self.evaluation_run_resolver is None:
            raise ModelRegistryConflictError(
                "evaluation evidence resolver is unavailable"
            )
        runs = []
        for run_id in payload.evaluation_run_ids:
            try:
                run = self.evaluation_run_resolver(run_id, actor=actor)
            except Exception as exc:
                raise ModelRegistryConflictError(
                    f"evaluation run is unavailable: {run_id}"
                ) from exc
            if not run.passed:
                raise ModelRegistryConflictError(
                    f"evaluation run did not pass: {run_id}"
                )
            if not any(item.model_id == payload.model_id for item in run.models):
                raise ModelRegistryConflictError(
                    f"evaluation run does not pin model {payload.model_id}: {run_id}"
                )
            runs.append(run)
        available_suites = {
            suite_id for run in runs for suite_id in run.suite_ids
        }
        missing = set(profile.required_suite_ids) - available_suites
        if missing:
            raise ModelRegistryConflictError(
                "qualification evidence is missing required suites: "
                + ", ".join(sorted(missing))
            )
        measured_quality = [
            run.trace.quality_score
            for run in runs
            if run.trace.quality_score is not None
        ]
        quality = min(measured_quality) if measured_quality else payload.quality_score
        if quality is None or quality < profile.minimum_quality_score:
            raise ModelRegistryConflictError(
                "qualification evidence does not meet the quality threshold"
            )
        measured_cost = sum(run.trace.cost_usd for run in runs) / len(runs)
        cost_per_success = measured_cost
        if (
            profile.maximum_cost_per_successful_outcome_usd is not None
            and cost_per_success
            > profile.maximum_cost_per_successful_outcome_usd
        ):
            raise ModelRegistryConflictError(
                "qualification evidence exceeds the cost-per-success threshold"
            )
        if (
            profile.require_provider_reported_cost
            and payload.cost_source != "provider_reported"
        ):
            raise ModelRegistryConflictError(
                "qualification profile requires provider-reported cost evidence"
            )

    def _authentication_status(
        self,
        provider: ModelProviderRecord,
        actor: AuthenticationActor,
    ) -> ModelAuthenticationStatus:
        if not provider.credential_required:
            return ModelAuthenticationStatus.NOT_REQUIRED
        if not provider.credential_ref or self.secret_broker is None:
            return ModelAuthenticationStatus.MISSING
        try:
            reference = self.secret_broker.metadata(
                provider.credential_ref,
                actor=actor,
                require_use=True,
            )
        except Exception:
            return ModelAuthenticationStatus.UNAVAILABLE
        return (
            ModelAuthenticationStatus.READY
            if reference.status(self.clock()) == SecretStatus.ACTIVE
            else ModelAuthenticationStatus.UNAVAILABLE
        )

    def provider_eligibility(
        self,
        actor: AuthenticationActor,
    ) -> tuple[ModelProviderEligibility, ...]:
        state = self.store.load()
        policy = self._policy(state, actor)
        now = self.clock()
        results: list[ModelProviderEligibility] = []
        for provider in sorted(
            (item for item in state.providers if self._same_scope(item, actor)),
            key=lambda item: item.id,
        ):
            reasons: list[str] = []
            authentication = self._authentication_status(provider, actor)
            if provider.status == ModelProviderStatus.DISABLED:
                reasons.append("provider_disabled")
            if provider.health == ModelProviderHealth.UNAVAILABLE:
                reasons.append("provider_unavailable")
            if provider.adapter_type not in self.adapters:
                reasons.append("adapter_unavailable")
            if authentication in {
                ModelAuthenticationStatus.MISSING,
                ModelAuthenticationStatus.UNAVAILABLE,
            }:
                reasons.append(f"authentication_{authentication.value}")
            if policy.allowed_provider_ids and provider.id not in policy.allowed_provider_ids:
                reasons.append("provider_not_allowed")
            catalog = next(
                (
                    item for item in state.catalogs
                    if item.provider_id == provider.id and self._same_scope(item, actor)
                ),
                None,
            )
            catalog_required = provider.catalog_required
            if catalog_required:
                if not provider.catalog_discovery_enabled:
                    reasons.append("catalog_discovery_disabled")
                elif catalog is None:
                    reasons.append("catalog_missing")
                elif catalog.status != ModelCatalogStatus.READY:
                    reasons.append(f"catalog_{catalog.status.value}")
                elif catalog.expires_at is None or catalog.expires_at <= now:
                    reasons.append("catalog_stale")
            executable: list[str] = []
            catalog_models = {
                item.concrete_model
                for item in (catalog.entries if catalog else ())
                if item.executable
                and item.runtime_provider in {None, provider.runtime_provider or provider.adapter_type}
                and item.access_source in {None, provider.access_source}
            }
            for model in state.models:
                if model.provider_id != provider.id or not self._same_scope(model, actor):
                    continue
                if model.lifecycle != ModelLifecycle.ACTIVE:
                    continue
                if policy.allowed_model_ids and model.id not in policy.allowed_model_ids:
                    continue
                requires_catalog = (
                    catalog_required
                    or model.availability_source == ModelAvailabilitySource.DISCOVERED
                )
                if requires_catalog and model.concrete_model not in catalog_models:
                    continue
                executable.append(model.id)
            results.append(
                ModelProviderEligibility(
                    provider_id=provider.id,
                    provider_family=provider.provider_family or provider.id,
                    runtime_provider=provider.runtime_provider or provider.adapter_type,
                    access_source=provider.access_source,
                    usage_semantics=provider.usage_semantics,
                    authentication_status=authentication,
                    provider_status=provider.status,
                    provider_health=provider.health,
                    catalog_status=(
                        catalog.status.value if catalog is not None
                        else "required_missing" if catalog_required
                        else "static"
                    ),
                    catalog_revision=(catalog.revision if catalog else None),
                    eligible=not reasons,
                    executable_model_ids=tuple(sorted(executable)) if not reasons else (),
                    exclusion_reasons=tuple(reasons),
                )
            )
        return tuple(results)

    @staticmethod
    def _candidate_set_revision(
        eligibility: tuple[ModelProviderEligibility, ...],
    ) -> str:
        payload = [item.model_dump(mode="json") for item in eligibility]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def record_qualification(
        self,
        payload: ModelQualificationUpdate,
        *,
        actor: AuthenticationActor,
    ) -> ModelQualificationRevision:
        self._require_admin(actor)
        state = self.store.load()
        model = next(
            (
                item
                for item in state.models
                if item.id == payload.model_id and self._same_scope(item, actor)
            ),
            None,
        )
        if model is None:
            raise ModelRegistryConflictError(
                f"model definition not found: {payload.model_id}"
            )
        provider = self._provider(state, model.provider_id, actor)
        profile = next(
            (
                item
                for item in state.evaluation_profiles
                if item.workload_class == payload.workload_class
                and item.revision == payload.evaluation_profile_revision
                and self._same_scope(item, actor)
            ),
            None,
        )
        if profile is None:
            raise ModelRegistryConflictError(
                "evaluation profile revision not found"
            )
        prior = [
            item
            for item in state.qualifications
            if item.model_id == payload.model_id
            and item.provider_id == model.provider_id
            and item.workload_class == payload.workload_class
            and self._qualification_matches_model(item, model, provider)
            and self._same_scope(item, actor)
        ]
        previous = max(prior, key=lambda item: item.revision) if prior else None
        if (
            payload.status == ModelQualificationStatus.CANARY
            and (
                previous is None
                or previous.status not in {
                    ModelQualificationStatus.QUALIFIED,
                    ModelQualificationStatus.CANARY,
                }
            )
        ):
            raise ModelRegistryConflictError(
                "canary requires a prior qualified revision"
            )
        if (
            payload.status == ModelQualificationStatus.ACTIVE
            and profile.require_canary
            and (
                previous is None
                or previous.status != ModelQualificationStatus.CANARY
            )
        ):
            raise ModelRegistryConflictError(
                "active promotion requires a prior canary revision"
            )
        self._qualification_evidence(payload, actor=actor, profile=profile)
        created: list[ModelQualificationRevision] = []

        def apply(current: ModelGatewayState) -> ModelGatewayState:
            prior = [
                item
                for item in current.qualifications
                if item.model_id == payload.model_id
                and item.provider_id == model.provider_id
                and item.workload_class == payload.workload_class
                and self._qualification_matches_model(item, model, provider)
                and self._same_scope(item, actor)
            ]
            previous = max(prior, key=lambda item: item.revision) if prior else None
            item = ModelQualificationRevision(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                revision=(previous.revision + 1 if previous else 1),
                provider_id=model.provider_id,
                concrete_model=model.concrete_model,
                model_version=model.model_version,
                runtime_provider=provider.runtime_provider or provider.adapter_type,
                access_source=provider.access_source.value,
                previous_revision_id=(previous.id if previous else None),
                created_by=actor.identity_id,
                created_at=self.clock(),
            )
            current.qualifications.append(item)
            created.append(item)
            return current

        self.store.update(apply)
        return created[0]

    def list_routing_definitions(
        self,
        actor: AuthenticationActor,
    ) -> list[ModelRoutingDefinitionRevision]:
        return sorted(
            [
                item
                for item in self.store.load().routing_definitions
                if self._same_scope(item, actor)
            ],
            key=lambda item: (item.mapping_id, item.revision),
        )

    @staticmethod
    def _latest_qualifications(
        state: ModelGatewayState,
        actor: AuthenticationActor,
    ) -> dict[tuple[str, str, str], ModelQualificationRevision]:
        latest: dict[tuple[str, str, str], ModelQualificationRevision] = {}
        for item in state.qualifications:
            if not ModelGatewayService._same_scope(item, actor):
                continue
            key = (item.provider_id, item.model_id, item.workload_class)
            if key not in latest or item.revision > latest[key].revision:
                latest[key] = item
        return latest

    def publish_routing_definition(
        self,
        payload: ModelRoutingDefinitionCreate,
        *,
        actor: AuthenticationActor,
        previous_known_good_revision_id: str | None = None,
    ) -> ModelRoutingDefinitionRevision:
        self._require_admin(actor)
        state = self.store.load()
        models = {
            item.id: item
            for item in state.models
            if self._same_scope(item, actor)
        }
        providers = {
            item.id: item
            for item in state.providers
            if self._same_scope(item, actor)
        }
        referenced = tuple(
            dict.fromkeys(
                payload.primary_model_ids
                + payload.escalation_model_ids
                + payload.critic_model_ids
            )
        )
        missing = [item for item in referenced if item not in models]
        if missing:
            raise ModelRegistryConflictError(
                "routing definition references unknown models: "
                + ", ".join(missing)
            )
        latest = self._latest_qualifications(state, actor)
        profile = max(
            (
                item
                for item in state.evaluation_profiles
                if item.workload_class == payload.workload_class
                and self._same_scope(item, actor)
            ),
            key=lambda item: item.revision,
            default=None,
        )
        eligible = {
            ModelQualificationStatus.QUALIFIED,
            ModelQualificationStatus.CANARY,
            ModelQualificationStatus.ACTIVE,
        }
        unqualified = [
            model_id
            for model_id in referenced
            if latest.get((models[model_id].provider_id, model_id, payload.workload_class)) is None
            or latest[(models[model_id].provider_id, model_id, payload.workload_class)].status not in eligible
            or not self._qualification_matches_model(
                latest[(models[model_id].provider_id, model_id, payload.workload_class)],
                models[model_id],
                providers.get(models[model_id].provider_id),
            )
            or (
                profile is not None
                and profile.require_canary
                and latest[(models[model_id].provider_id, model_id, payload.workload_class)].status
                != ModelQualificationStatus.ACTIVE
            )
        ]
        if unqualified:
            raise ModelRegistryConflictError(
                "routing definition requires production-qualified models: "
                + ", ".join(unqualified)
            )
        created: list[ModelRoutingDefinitionRevision] = []

        def apply(current: ModelGatewayState) -> ModelGatewayState:
            prior = [
                item
                for item in current.routing_definitions
                if item.mapping_id == payload.mapping_id
                and self._same_scope(item, actor)
            ]
            previous = max(prior, key=lambda item: item.revision) if prior else None
            current.routing_definitions = [
                item.model_copy(update={"active": False})
                if item.mapping_id == payload.mapping_id
                and item.active
                and self._same_scope(item, actor)
                else item
                for item in current.routing_definitions
            ]
            item = ModelRoutingDefinitionRevision(
                **payload.model_dump(mode="python"),
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                revision=(previous.revision + 1 if previous else 1),
                previous_revision_id=(previous.id if previous else None),
                previous_known_good_revision_id=(
                    previous_known_good_revision_id
                    or (previous.id if previous else None)
                ),
                created_by=actor.identity_id,
                created_at=self.clock(),
            )
            current.routing_definitions.append(item)
            created.append(item)
            return current

        self.store.update(apply)
        return created[0]

    @staticmethod
    def _qualification_matches_model(
        qualification: ModelQualificationRevision,
        model: ModelDefinitionRecord,
        provider: ModelProviderRecord | None,
    ) -> bool:
        if provider is None or qualification.provider_id != model.provider_id:
            return False
        return (
            qualification.model_version == model.model_version
            and qualification.concrete_model in {None, model.concrete_model}
            and qualification.runtime_provider
            in {None, provider.runtime_provider or provider.adapter_type}
            and qualification.access_source in {None, provider.access_source.value}
        )

    def rollback_routing_definition(
        self,
        mapping_id: str,
        payload: ModelRoutingRollback,
        *,
        actor: AuthenticationActor,
    ) -> ModelRoutingDefinitionRevision:
        self._require_admin(actor)
        target = next(
            (
                item
                for item in self.store.load().routing_definitions
                if item.mapping_id == mapping_id
                and item.revision == payload.target_revision
                and self._same_scope(item, actor)
            ),
            None,
        )
        if target is None:
            raise ModelRegistryConflictError("routing rollback target not found")
        source = target.model_dump(
            mode="python",
            exclude={
                "id", "organization_id", "workspace_id", "revision",
                "previous_revision_id", "previous_known_good_revision_id",
                "active", "created_by", "created_at",
            },
        )
        source["source"] = f"rollback:{payload.reason}"
        return self.publish_routing_definition(
            ModelRoutingDefinitionCreate.model_validate(source),
            actor=actor,
            previous_known_good_revision_id=target.id,
        )

    def route(
        self,
        request: ModelInvocationRequest,
        *,
        actor: AuthenticationActor,
    ) -> ModelRouteResult:
        state = self.store.load()
        template = self._template(state, request, actor)
        policy = self._policy(state, actor)
        matching_mappings = [
            item
            for item in state.routing_definitions
            if item.active
            and self._same_scope(item, actor)
            and request.workload_class == item.workload_class
            and item.function_id in {request.purpose, "*"}
            and item.effective_at <= self.clock()
        ]
        matching_mappings.sort(
            key=lambda item: (
                item.function_id == request.purpose,
                item.effective_at,
                item.revision,
            ),
            reverse=True,
        )
        mapping = matching_mappings[0] if matching_mappings else None
        mapped_model_ids: tuple[str, ...] = ()
        other_role_model_ids: set[str] = set()
        if mapping is not None:
            mapped_model_ids = {
                ModelRoutingRole.PRIMARY: mapping.primary_model_ids,
                ModelRoutingRole.ESCALATION: mapping.escalation_model_ids,
                ModelRoutingRole.CRITIC: (
                    mapping.critic_model_ids
                    + (mapping.primary_model_ids if mapping.allow_same_model_critic else ())
                ),
            }[request.routing_role]
            mapped_model_ids = tuple(dict.fromkeys(mapped_model_ids))
            other_role_model_ids = {
                ModelRoutingRole.PRIMARY: set(
                    mapping.escalation_model_ids + mapping.critic_model_ids
                ),
                ModelRoutingRole.ESCALATION: set(
                    mapping.primary_model_ids + mapping.critic_model_ids
                ),
                ModelRoutingRole.CRITIC: set(mapping.escalation_model_ids)
                | (
                    set()
                    if mapping.allow_same_model_critic
                    else set(mapping.primary_model_ids)
                ),
            }[request.routing_role]
            if not mapped_model_ids:
                raise ModelRoutingError(
                    f"routing definition {mapping.mapping_id}@{mapping.revision} "
                    f"has no {request.routing_role.value} models"
                )
        mapping_order = {
            model_id: index for index, model_id in enumerate(mapped_model_ids)
        }
        latest_qualifications = self._latest_qualifications(state, actor)
        mapping_profile = max(
            (
                item
                for item in state.evaluation_profiles
                if mapping is not None
                and item.workload_class == mapping.workload_class
                and self._same_scope(item, actor)
            ),
            key=lambda item: item.revision,
            default=None,
        )
        rendered_system_prompt = self._render_system_prompt(template, request)
        input_tokens = self._estimate_tokens(
            request,
            rendered_system_prompt=rendered_system_prompt,
        )
        if (
            request.max_input_tokens is not None
            and input_tokens > request.max_input_tokens
        ):
            raise ModelRoutingError(
                "estimated input token budget exceeded: "
                f"{input_tokens} > {request.max_input_tokens}"
            )
        requested_residency = set(policy.required_residency_tags) | set(
            request.required_residency_tags
        )
        requested_compliance = set(policy.required_compliance_tags) | set(
            request.required_compliance_tags
        )
        required_capabilities = set(request.required_capabilities) | set(
            mapping.required_capabilities if mapping is not None else ()
        )
        max_costs = [
            value
            for value in (
                policy.max_invocation_cost_usd,
                request.max_cost_usd,
                mapping.max_cost_per_invocation_usd if mapping is not None else None,
            )
            if value is not None
        ]
        effective_max_cost = min(max_costs) if max_costs else None
        preferred = {
            provider_id: index
            for index, provider_id in enumerate(request.preferred_provider_ids)
        }
        preferred_latency = {
            latency: index
            for index, latency in enumerate(request.preferred_latency_classes)
        }
        provider_eligibility = self.provider_eligibility(actor)
        eligibility_by_provider = {
            item.provider_id: item for item in provider_eligibility
        }
        candidate_set_revision = self._candidate_set_revision(provider_eligibility)

        candidates: list[tuple[Any, ...]] = []
        rejected: list[str] = []
        capacity_blocks = []
        for model in state.models:
            if not self._same_scope(model, actor):
                continue
            if (
                mapping is not None
                and model.id not in mapping_order
                and (
                    not mapping.include_qualified_candidates
                    or model.id in other_role_model_ids
                )
            ):
                continue
            if request.pinned_model_id and model.id != request.pinned_model_id:
                continue
            if request.model_class not in model.model_classes:
                continue
            if (
                request.workload_class
                and model.workload_classes
                and request.workload_class not in model.workload_classes
            ):
                rejected.append(f"{model.id}:workload_mismatch")
                continue
            if model.lifecycle != ModelLifecycle.ACTIVE:
                rejected.append(f"{model.id}:lifecycle:{model.lifecycle.value}")
                continue
            qualification = None
            if policy.allowed_model_ids and model.id not in policy.allowed_model_ids:
                rejected.append(f"{model.id}:model_not_allowed")
                continue
            provider = next(
                (
                    item
                    for item in state.providers
                    if item.id == model.provider_id and self._same_scope(item, actor)
                ),
                None,
            )
            if provider is None:
                rejected.append(f"{model.id}:provider_missing")
                continue
            eligibility = eligibility_by_provider.get(provider.id)
            if eligibility is None or not eligibility.eligible:
                reasons = (
                    eligibility.exclusion_reasons
                    if eligibility is not None
                    else ("provider_missing",)
                )
                rejected.extend(f"{model.id}:{reason}" for reason in reasons)
                continue
            if request.allowed_provider_ids and provider.id not in request.allowed_provider_ids:
                rejected.append(f"{model.id}:runtime_provider_incompatible")
                continue
            catalog = None
            catalog_entry = None
            if (
                provider.catalog_required
                or model.availability_source == ModelAvailabilitySource.DISCOVERED
            ):
                if not provider.catalog_discovery_enabled:
                    rejected.append(f"{model.id}:catalog_discovery_disabled")
                    continue
                catalog = next(
                    (
                        item for item in state.catalogs
                        if item.provider_id == provider.id
                        and self._same_scope(item, actor)
                    ),
                    None,
                )
                if catalog is None:
                    rejected.append(f"{model.id}:catalog_missing")
                    continue
                if catalog.status != ModelCatalogStatus.READY:
                    rejected.append(
                        f"{model.id}:catalog_{catalog.status.value}"
                    )
                    continue
                if (
                    catalog.expires_at is None
                    or catalog.expires_at <= self.clock()
                ):
                    rejected.append(f"{model.id}:catalog_stale")
                    continue
                catalog_entry = next(
                    (
                        item for item in catalog.entries
                        if item.concrete_model == model.concrete_model
                    ),
                    None,
                )
                if catalog_entry is None:
                    rejected.append(f"{model.id}:not_in_provider_catalog")
                    continue
                if not catalog_entry.executable:
                    rejected.append(
                        f"{model.id}:runtime_access_rejected:"
                        f"{catalog_entry.exclusion_reason or 'provider runtime rejected model'}"
                    )
                    continue
                if catalog_entry.runtime_provider not in {
                    None,
                    provider.runtime_provider or provider.adapter_type,
                }:
                    rejected.append(f"{model.id}:runtime_provider_incompatible")
                    continue
                if catalog_entry.access_source not in {None, provider.access_source}:
                    rejected.append(f"{model.id}:access_source_incompatible")
                    continue
            if policy.allowed_provider_ids and provider.id not in policy.allowed_provider_ids:
                rejected.append(f"{model.id}:provider_not_allowed")
                continue
            if mapping is not None:
                qualification = latest_qualifications.get(
                    (model.provider_id, model.id, mapping.workload_class)
                )
                if qualification is None or qualification.status not in {
                    ModelQualificationStatus.QUALIFIED,
                    ModelQualificationStatus.CANARY,
                    ModelQualificationStatus.ACTIVE,
                }:
                    rejected.append(f"{model.id}:qualification_not_active")
                    continue
                if not self._qualification_matches_model(
                    qualification,
                    model,
                    provider,
                ):
                    rejected.append(f"{model.id}:qualification_runtime_mismatch")
                    continue
                if (
                    mapping_profile is not None
                    and mapping_profile.require_canary
                    and qualification.status != ModelQualificationStatus.ACTIVE
                ):
                    rejected.append(f"{model.id}:canary_promotion_required")
                    continue
            if not required_capabilities.issubset(set(model.capabilities)):
                rejected.append(f"{model.id}:capability_mismatch")
                continue
            if requested_residency and (
                not requested_residency.issubset(set(provider.residency_tags))
                or not requested_residency.issubset(set(model.residency_tags))
            ):
                rejected.append(f"{model.id}:residency_mismatch")
                continue
            if requested_compliance and (
                not requested_compliance.issubset(set(provider.compliance_tags))
                or not requested_compliance.issubset(set(model.compliance_tags))
            ):
                rejected.append(f"{model.id}:compliance_mismatch")
                continue
            output_tokens = min(request.max_output_tokens, model.max_output_tokens)
            if input_tokens + output_tokens > model.context_window_tokens:
                rejected.append(f"{model.id}:context_limit")
                continue
            cost = self._price(model, input_tokens, output_tokens)
            if effective_max_cost is not None:
                if cost is None:
                    rejected.append(f"{model.id}:pricing_required_for_budget")
                    continue
                if cost > effective_max_cost:
                    rejected.append(f"{model.id}:budget_exceeded")
                    continue
            if self.provider_capacity is not None:
                blocked = self.provider_capacity.blocking_record(
                    provider.id,
                    None,
                    actor=actor,
                )
                if blocked is not None:
                    rejected.append(
                        f"{model.id}:capacity_{blocked.status.value}"
                        + (
                            f":retry_at={blocked.retry_at:.3f}"
                            if blocked.retry_at is not None
                            else ""
                        )
                    )
                    capacity_blocks.append(blocked)
                    continue
            preference = preferred.get(provider.id, len(preferred))
            degraded_penalty = 10000 if (
                provider.status == ModelProviderStatus.DEGRADED
                or provider.health == ModelProviderHealth.DEGRADED
            ) else 0
            workload_preference = (
                0
                if (
                    request.workload_class
                    and request.workload_class in model.workload_classes
                )
                else (1 if request.workload_class else 0)
            )
            latency_preference = preferred_latency.get(
                model.latency_class,
                len(preferred_latency),
            )
            unknown_cost_penalty = int(
                request.prefer_lower_cost and cost is None
            )
            cost_preference = (
                cost
                if request.prefer_lower_cost and cost is not None
                else 0.0
            )
            workload_match = (
                "exact" if workload_preference == 0 and request.workload_class
                else ("generic" if request.workload_class else "unspecified")
            )
            candidates.append(
                (
                    mapping_order.get(model.id, len(mapping_order)),
                    workload_preference,
                    preference,
                    degraded_penalty,
                    latency_preference,
                    unknown_cost_penalty,
                    cost_preference,
                    model.route_priority,
                    model.id,
                    ModelRouteCandidate(
                        provider_id=provider.id,
                        provider_family=provider.provider_family or provider.id,
                        runtime_provider=provider.runtime_provider or provider.adapter_type,
                        access_source=provider.access_source,
                        usage_semantics=provider.usage_semantics,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        upstream_provider_id=(
                            catalog_entry.upstream_provider_id
                            if catalog_entry else model.upstream_provider_id
                        ),
                        upstream_model_id=(
                            catalog_entry.upstream_model_id
                            if catalog_entry else model.upstream_model_id
                        ),
                        catalog_revision=(catalog.revision if catalog else None),
                        catalog_discovered_at=(
                            catalog.discovered_at if catalog else None
                        ),
                        estimated_input_tokens=input_tokens,
                        max_output_tokens=output_tokens,
                        estimated_upper_cost_usd=cost,
                        routing_reason=(
                            f"class={request.model_class};"
                            f"workload={request.workload_class or 'unspecified'};"
                            f"workload_match={workload_match};"
                            f"pin={request.pinned_model_id or 'none'};"
                            f"latency={model.latency_class.value};"
                            f"low_cost={str(request.prefer_lower_cost).lower()};"
                            f"priority={model.route_priority};"
                            f"provider={provider.status.value};"
                            f"health={provider.health.value};"
                            f"runtime={provider.runtime_provider or provider.adapter_type};"
                            f"access={provider.access_source.value}"
                            + (
                                f";mapping={mapping.mapping_id}@{mapping.revision};"
                                f"role={request.routing_role.value};"
                                f"qualification={qualification.id}"
                                if mapping is not None and qualification is not None
                                else ""
                            )
                        ),
                        qualification_revision_id=(
                            qualification.id if qualification is not None else None
                        ),
                    ),
                )
            )

        candidates.sort(key=lambda item: item[:-1])
        routed = tuple(item[-1] for item in candidates)
        requested_critic = None
        achieved_critic = None
        if mapping is not None and request.routing_role == ModelRoutingRole.CRITIC:
            requested_critic = mapping.requested_critic_independence
            primary_id = next(
                (item for item in mapping.primary_model_ids if any(model.id == item for model in state.models)),
                None,
            )
            primary_model = next(
                (item for item in state.models if item.id == primary_id),
                None,
            )

            def independence(candidate: ModelRouteCandidate) -> CriticIndependenceLevel:
                critic_model = next(item for item in state.models if item.id == candidate.model_id)
                if primary_model is None:
                    return CriticIndependenceLevel.NONE
                primary_provider = next(
                    item for item in state.providers if item.id == primary_model.provider_id
                )
                critic_provider = next(
                    item for item in state.providers if item.id == critic_model.provider_id
                )
                if (
                    (primary_provider.provider_family or primary_provider.id)
                    != (critic_provider.provider_family or critic_provider.id)
                ):
                    return CriticIndependenceLevel.DIFFERENT_PROVIDER_FAMILY
                if critic_model.id == primary_model.id:
                    return CriticIndependenceLevel.SAME_MODEL_INDEPENDENT_RUN
                if (critic_model.model_family or critic_model.concrete_model) != (
                    primary_model.model_family or primary_model.concrete_model
                ):
                    return CriticIndependenceLevel.DIFFERENT_MODEL_FAMILY_SAME_PROVIDER
                return CriticIndependenceLevel.DIFFERENT_MODEL_SAME_FAMILY

            classified = [
                candidate.model_copy(update={"critic_independence": independence(candidate)})
                for candidate in routed
            ]
            classified.sort(
                key=lambda candidate: candidate.critic_independence.strength,
                reverse=True,
            )
            routed = tuple(
                candidate for candidate in classified
                if candidate.critic_independence.strength
                >= mapping.minimum_critic_independence.strength
            )
            if routed:
                achieved_critic = routed[0].critic_independence
            elif classified:
                rejected.append(
                    "critic:minimum_critic_independence_not_met:"
                    f"required={mapping.minimum_critic_independence.value}:"
                    f"achieved={classified[0].critic_independence}"
                )
        if not routed:
            detail = ",".join(rejected[:20]) or "no models registered for class"
            if request.pinned_model_id:
                detail = f"pinned_model={request.pinned_model_id};{detail}"
            if capacity_blocks:
                retries = [
                    item.retry_at
                    for item in capacity_blocks
                    if item.retry_at is not None
                ]
                raise ModelCapacityRoutingError(
                    f"no eligible model for class {request.model_class}: {detail}",
                    retry_at=min(retries) if retries else None,
                    provider_keys=tuple(
                        dict.fromkeys(item.key for item in capacity_blocks)
                    ),
                )
            raise ModelRoutingError(
                f"no eligible model for class {request.model_class}: {detail}"
            )
        max_attempts = min(
            len(routed),
            policy.max_attempts
            if request.allow_fallback
            and (mapping is None or mapping.allow_fallback)
            else 1,
        )
        return ModelRouteResult(
            model_class=request.model_class,
            workload_class=request.workload_class,
            pinned_model_id=request.pinned_model_id,
            preferred_latency_classes=request.preferred_latency_classes,
            prefer_lower_cost=request.prefer_lower_cost,
            prompt_template_id=template.template_id,
            prompt_template_version=template.version,
            prompt_template_checksum_sha256=template.checksum_sha256,
            candidates=routed,
            policy_max_attempts=max_attempts,
            policy_fingerprint_sha256=self._policy_hash(policy),
            effective_required_residency_tags=tuple(sorted(requested_residency)),
            effective_required_compliance_tags=tuple(sorted(requested_compliance)),
            effective_max_cost_usd=effective_max_cost,
            routing_role=request.routing_role,
            routing_definition_id=(mapping.mapping_id if mapping else None),
            routing_definition_revision=(mapping.revision if mapping else None),
            qualification_revision=(mapping.qualification_revision if mapping else None),
            active_candidate_set_revision=candidate_set_revision,
            provider_eligibility=provider_eligibility,
            excluded_candidates=tuple(
                ModelRouteExclusion(
                    model_id=item.split(":", 1)[0],
                    provider_id=next(
                        (
                            model.provider_id for model in state.models
                            if model.id == item.split(":", 1)[0]
                        ),
                        None,
                    ),
                    stage=(item.split(":", 1)[1].split(":", 1)[0] if ":" in item else "eligibility"),
                    reason=(item.split(":", 1)[1] if ":" in item else item),
                )
                for item in rejected
            ),
            requested_critic_independence=requested_critic,
            achieved_critic_independence=achieved_critic,
        )

    def _append_invocation(self, record: ModelInvocationRecord) -> None:
        def apply(state: ModelGatewayState) -> ModelGatewayState:
            state.invocations.append(record)
            state.invocations = state.invocations[-5000:]
            return state

        self.store.update(apply)

    def _record_runtime_access_rejection(
        self,
        provider: ModelProviderRecord,
        model: ModelDefinitionRecord,
        error: ModelProviderAdapterError,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        message = str(error).strip()
        normalized = message.lower()
        if not any(
            marker in normalized
            for marker in (
                "not supported when using",
                "not available for",
                "does not have access",
                "not entitled",
            )
        ):
            return False

        changed = False

        def apply(state: ModelGatewayState) -> ModelGatewayState:
            nonlocal changed
            updated_catalogs: list[ModelCatalogSnapshot] = []
            for catalog in state.catalogs:
                if (
                    catalog.provider_id != provider.id
                    or not self._same_scope(catalog, actor)
                ):
                    updated_catalogs.append(catalog)
                    continue
                entries = tuple(
                    item.model_copy(
                        update={
                            "runtime_provider": (
                                provider.runtime_provider or provider.adapter_type
                            ),
                            "access_source": provider.access_source,
                            "executable": False,
                            "exclusion_reason": message[:1000],
                        }
                    )
                    if item.concrete_model == model.concrete_model
                    else item
                    for item in catalog.entries
                )
                changed = entries != catalog.entries
                updated_catalogs.append(
                    catalog.model_copy(
                        update={
                            "entries": entries,
                            "revision": self._catalog_revision(entries),
                            "updated_by": actor.identity_id,
                            "updated_at": self.clock(),
                        }
                    )
                )
            state.catalogs = updated_catalogs
            return state

        self.store.update(apply)
        return changed

    def _meter_invocation(
        self,
        record: ModelInvocationRecord,
        *,
        actor: AuthenticationActor,
    ) -> None:
        """Best-effort canonical usage attribution after a successful provider call."""
        if self.entitlements is None or not record.attempts:
            return
        successful = next(
            (item for item in reversed(record.attempts) if item.outcome == "success"),
            None,
        )
        if successful is None:
            return
        events: list[tuple[str, float]] = []
        if successful.input_tokens is not None and successful.input_tokens > 0:
            events.append((METRIC_MODEL_INPUT_TOKENS, float(successful.input_tokens)))
        if successful.output_tokens is not None and successful.output_tokens > 0:
            events.append((METRIC_MODEL_OUTPUT_TOKENS, float(successful.output_tokens)))
        if successful.actual_cost_usd is not None and successful.actual_cost_usd > 0:
            events.append((METRIC_MODEL_COST_USD, float(successful.actual_cost_usd)))
        for metric, amount in events:
            try:
                self.entitlements.record_domain_usage(
                    UsageEventCreate(
                        idempotency_key=f"{record.id}:{metric}",
                        metric=metric,
                        amount=amount,
                        source="model-gateway",
                        work_item_ref=record.work_item_ref,
                    ),
                    actor=actor,
                )
            except Exception:
                # Provider success must remain the source of truth. Usage
                # reconciliation can repair a metering sink independently.
                continue

    async def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        actor: AuthenticationActor,
    ) -> ModelInvocationResponse:
        effective_request, input_result = await self._compose_input(
            request,
            actor=actor,
        )
        try:
            route = self.route(effective_request, actor=actor)
        except ModelCapacityRoutingError as exc:
            wait_id = None
            if (
                self.provider_capacity is not None
                and exc.retry_at is not None
                and any(
                    (
                        effective_request.work_item_ref,
                        effective_request.execution_id,
                    )
                )
            ):
                wait = self.provider_capacity.wait_for_capacity(
                    ProviderCapacityWaitCreate(
                        work_item_ref=effective_request.work_item_ref,
                        execution_id=effective_request.execution_id,
                        provider_keys=exc.provider_keys,
                        retry_at=exc.retry_at,
                        reason=str(exc),
                    ),
                    actor=actor,
                )
                wait_id = wait.id
            raise ModelProviderCapacityUnavailableError(
                str(exc),
                retry_at=exc.retry_at,
                wait_id=wait_id,
                failure=create_failure(
                    FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT,
                    source_subsystem="model_gateway",
                    execution_id=effective_request.execution_id,
                    source_native_code=type(exc).__name__,
                    details={
                        "provider_key_count": len(exc.provider_keys),
                    },
                ),
            ) from exc
        state = self.store.load()
        template = next(
            item
            for item in state.prompt_templates
            if item.template_id == route.prompt_template_id
            and item.version == route.prompt_template_version
            and item.checksum_sha256 == route.prompt_template_checksum_sha256
            and self._same_scope(item, actor)
        )
        rendered_request = effective_request.model_copy(
            update={
                "system_prompt": self._render_system_prompt(
                    template,
                    effective_request,
                )
            }
        )
        attempts: list[ModelInvocationAttempt] = []
        capacity_failures = []
        budget_remaining = route.effective_max_cost_usd
        final_error: Exception | None = None

        for candidate in route.candidates[: route.policy_max_attempts]:
            provider = self._provider(state, candidate.provider_id, actor)
            model = next(
                item
                for item in state.models
                if item.id == candidate.model_id and self._same_scope(item, actor)
            )
            if (
                budget_remaining is not None
                and candidate.estimated_upper_cost_usd is not None
                and candidate.estimated_upper_cost_usd > budget_remaining
            ):
                continue
            adapter = self.adapters.get(provider.adapter_type)
            if adapter is None:
                final_error = ModelProviderUnavailableError(
                    f"model adapter unavailable: {provider.adapter_type}"
                )
                break
            started = time.time()

            async def call(credential: str | None):
                try:
                    return await asyncio.wait_for(
                        adapter.invoke(
                            provider,
                            model,
                            rendered_request,
                            credential=credential,
                        ),
                        timeout=effective_request.timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise ModelProviderTransientError(
                        f"provider attempt timed out after {effective_request.timeout_seconds}s",
                        reason_code=FailureReason.PROVIDER_NETWORK,
                    ) from exc

            try:
                if provider.credential_ref:
                    if self.secret_broker is None:
                        raise ModelProviderUnavailableError(
                            "secret broker is required for provider credentials"
                        )
                    result = await self.secret_broker.use_async(
                        provider.credential_ref,
                        actor=actor,
                        operation="model-gateway.invoke",
                        consumer=lambda secret: call(secret),
                        context={
                            "provider_id": provider.id,
                            "model_id": model.id,
                            "model_class": effective_request.model_class,
                        },
                    )
                else:
                    if provider.credential_required:
                        raise ModelProviderUnavailableError(
                            f"provider {provider.id} requires credential_ref"
                        )
                    result = await call(None)
            except ModelProviderCapacityError as exc:
                completed = time.time()
                capacity_record = None
                if self.provider_capacity is not None:
                    retry_at = exc.retry_at
                    if retry_at is None:
                        retry_at = (
                            float(self.provider_capacity.clock())
                            + self.provider_capacity.default_retry_seconds
                        )
                    capacity_record = self.provider_capacity.report(
                        ProviderCapacityReport(
                            provider_id=provider.id,
                            status=exc.capacity_status,
                            reason=str(exc)[:500],
                            retry_at=retry_at,
                            source=f"model-provider:{provider.adapter_type}",
                            metadata={
                                "model_id": model.id,
                                "error_type": type(exc).__name__,
                            },
                            observed_at=completed,
                        ),
                        actor=actor,
                    )
                    capacity_failures.append(capacity_record)
                failure = self._provider_failure(
                    exc,
                    provider=provider,
                    model=model,
                    request=effective_request,
                    attempt=len(attempts) + 1,
                )
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="capacity_failure",
                        error_code=exc.capacity_status.value,
                        failure=failure,
                        estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                        started_at=started,
                        completed_at=completed,
                    )
                )
                if budget_remaining is not None and candidate.estimated_upper_cost_usd is not None:
                    budget_remaining = max(
                        0.0,
                        budget_remaining - candidate.estimated_upper_cost_usd,
                    )
                final_error = exc
                continue
            except ModelProviderTransientError as exc:
                completed = time.time()
                failure = self._provider_failure(
                    exc,
                    provider=provider,
                    model=model,
                    request=effective_request,
                    attempt=len(attempts) + 1,
                )
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="transient_failure",
                        error_code=type(exc).__name__,
                        failure=failure,
                        estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                        started_at=started,
                        completed_at=completed,
                    )
                )
                if budget_remaining is not None and candidate.estimated_upper_cost_usd is not None:
                    budget_remaining = max(
                        0.0,
                        budget_remaining - candidate.estimated_upper_cost_usd,
                    )
                final_error = exc
                continue
            except ModelProviderAdapterError as exc:
                completed = time.time()
                access_rejected = self._record_runtime_access_rejection(
                    provider,
                    model,
                    exc,
                    actor=actor,
                )
                failure = self._provider_failure(
                    exc,
                    provider=provider,
                    model=model,
                    request=effective_request,
                    attempt=len(attempts) + 1,
                )
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="failure",
                        error_code=type(exc).__name__,
                        failure=failure,
                        estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                        started_at=started,
                        completed_at=completed,
                    )
                )
                final_error = exc
                if access_rejected:
                    continue
                break
            except Exception as exc:
                completed = time.time()
                failure = create_failure(
                    FailureReason.UNCLASSIFIED,
                    source_subsystem="model_gateway",
                    provider_id=provider.id,
                    execution_id=effective_request.execution_id,
                    source_native_code=type(exc).__name__,
                    attempt=len(attempts) + 1,
                    details={
                        "model_id": model.id,
                        "adapter_type": provider.adapter_type,
                    },
                )
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="failure",
                        error_code=type(exc).__name__,
                        failure=failure,
                        estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                        started_at=started,
                        completed_at=completed,
                    )
                )
                final_error = exc
                break

            completed = time.time()
            if self.provider_capacity is not None:
                self.provider_capacity.mark_available(
                    provider.id,
                    None,
                    actor=actor,
                    source="model-provider-success",
                    metadata={"model_id": model.id},
                )
            provider_cost = result.usage.cost
            currency = result.usage.currency
            calculated_cost = None
            pricing_revision = None
            if (
                provider_cost is None
                and provider.usage_semantics.value != "entitlement"
            ):
                calculated_cost = self._price(
                    model,
                    result.usage.input_tokens or candidate.estimated_input_tokens,
                    result.usage.output_tokens or candidate.max_output_tokens,
                )
                currency = "USD" if calculated_cost is not None else None
                pricing_revision = self._pricing_revision(model)
            actual_cost = provider_cost if provider_cost is not None else calculated_cost
            attempts.append(
                ModelInvocationAttempt(
                    provider_id=provider.id,
                    model_id=model.id,
                    concrete_model=model.concrete_model,
                    model_version=model.model_version,
                    outcome="success",
                    estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    actual_cost_usd=(actual_cost if currency == "USD" else None),
                    actual_cost=actual_cost,
                    cost_currency=currency,
                    cost_source=(
                        "provider_reported" if provider_cost is not None
                        else "codex_calculated" if calculated_cost is not None
                        else None
                    ),
                    pricing_revision=pricing_revision,
                    provider_request_id=result.provider_request_id,
                    provider_stop_reason=result.stop_reason,
                    started_at=started,
                    completed_at=completed,
                )
            )
            record = ModelInvocationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.identity_id,
                model_class=effective_request.model_class,
                workload_class=effective_request.workload_class,
                pinned_model_id=effective_request.pinned_model_id,
                preferred_latency_classes=(
                    effective_request.preferred_latency_classes
                ),
                prefer_lower_cost=effective_request.prefer_lower_cost,
                purpose=effective_request.purpose,
                routing_role=route.routing_role,
                prompt_template_id=route.prompt_template_id,
                prompt_template_version=route.prompt_template_version,
                prompt_template_checksum_sha256=route.prompt_template_checksum_sha256,
                rendered_prompt_sha256=self._rendered_prompt_hash(rendered_request),
                message_count=len(effective_request.messages),
                input_character_count=len(rendered_request.system_prompt)
                + sum(len(item.content) for item in rendered_request.messages),
                required_capabilities=effective_request.required_capabilities,
                required_residency_tags=route.effective_required_residency_tags,
                required_compliance_tags=route.effective_required_compliance_tags,
                max_cost_usd=route.effective_max_cost_usd,
                policy_fingerprint_sha256=route.policy_fingerprint_sha256,
                route_reason=candidate.routing_reason,
                routing_definition_id=route.routing_definition_id,
                routing_definition_revision=route.routing_definition_revision,
                qualification_revision=candidate.qualification_revision_id,
                active_candidate_set_revision=route.active_candidate_set_revision,
                excluded_candidates=route.excluded_candidates,
                requested_critic_independence=route.requested_critic_independence,
                achieved_critic_independence=route.achieved_critic_independence,
                work_item_ref=effective_request.work_item_ref,
                goal_id=effective_request.goal_id,
                decision_id=effective_request.decision_id,
                execution_id=effective_request.execution_id,
                input_plugin_provenance=(
                    input_result.provenance if input_result is not None else ()
                ),
                input_gated_proposals=(
                    input_result.gated_proposals if input_result is not None else ()
                ),
                attempts=tuple(attempts),
                selected_provider_id=provider.id,
                selected_provider_family=candidate.provider_family,
                selected_runtime_provider=candidate.runtime_provider,
                selected_access_source=candidate.access_source,
                selected_usage_semantics=candidate.usage_semantics,
                selected_model_id=model.id,
                selected_concrete_model=model.concrete_model,
                selected_model_version=model.model_version,
                selected_upstream_provider_id=candidate.upstream_provider_id,
                selected_upstream_model_id=candidate.upstream_model_id,
                selected_catalog_revision=candidate.catalog_revision,
                selected_catalog_discovered_at=candidate.catalog_discovered_at,
                status="succeeded",
                completed_at=completed,
            )
            self._append_invocation(record)
            self._meter_invocation(record, actor=actor)
            return ModelInvocationResponse(text=result.text, invocation=record)

        completed = time.time()
        retry_at = None
        wait_id = None
        all_capacity_failures = bool(attempts) and all(
            item.outcome == "capacity_failure"
            for item in attempts
        )
        if all_capacity_failures:
            retries = [
                item.retry_at
                for item in capacity_failures
                if item.retry_at is not None
            ]
            retry_at = min(retries) if retries else None
            if (
                self.provider_capacity is not None
                and retry_at is not None
                and any(
                    (
                        effective_request.work_item_ref,
                        effective_request.execution_id,
                    )
                )
            ):
                wait = self.provider_capacity.wait_for_capacity(
                    ProviderCapacityWaitCreate(
                        work_item_ref=effective_request.work_item_ref,
                        execution_id=effective_request.execution_id,
                        provider_keys=tuple(
                            dict.fromkeys(item.key for item in capacity_failures)
                        ),
                        retry_at=retry_at,
                        reason="all eligible model attempts are capacity constrained",
                    ),
                    actor=actor,
                )
                wait_id = wait.id
        record = ModelInvocationRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            actor_id=actor.identity_id,
            model_class=effective_request.model_class,
            workload_class=effective_request.workload_class,
            pinned_model_id=effective_request.pinned_model_id,
            preferred_latency_classes=(
                effective_request.preferred_latency_classes
            ),
            prefer_lower_cost=effective_request.prefer_lower_cost,
            purpose=effective_request.purpose,
            routing_role=route.routing_role,
            prompt_template_id=route.prompt_template_id,
            prompt_template_version=route.prompt_template_version,
            prompt_template_checksum_sha256=route.prompt_template_checksum_sha256,
            rendered_prompt_sha256=self._rendered_prompt_hash(rendered_request),
            message_count=len(effective_request.messages),
            input_character_count=len(rendered_request.system_prompt)
            + sum(len(item.content) for item in rendered_request.messages),
            required_capabilities=effective_request.required_capabilities,
            required_residency_tags=route.effective_required_residency_tags,
            required_compliance_tags=route.effective_required_compliance_tags,
            max_cost_usd=route.effective_max_cost_usd,
            policy_fingerprint_sha256=route.policy_fingerprint_sha256,
            route_reason="all eligible attempts exhausted",
            routing_definition_id=route.routing_definition_id,
            routing_definition_revision=route.routing_definition_revision,
            qualification_revision=route.qualification_revision,
            active_candidate_set_revision=route.active_candidate_set_revision,
            excluded_candidates=route.excluded_candidates,
            requested_critic_independence=route.requested_critic_independence,
            achieved_critic_independence=route.achieved_critic_independence,
            work_item_ref=effective_request.work_item_ref,
            goal_id=effective_request.goal_id,
            decision_id=effective_request.decision_id,
            execution_id=effective_request.execution_id,
            input_plugin_provenance=(
                input_result.provenance if input_result is not None else ()
            ),
            input_gated_proposals=(
                input_result.gated_proposals if input_result is not None else ()
            ),
            attempts=tuple(attempts),
            status=("waiting_for_capacity" if all_capacity_failures else "failed"),
            completed_at=completed,
        )
        self._append_invocation(record)
        if all_capacity_failures:
            final_failure = (
                attempts[-1].failure
                if attempts and attempts[-1].failure is not None
                else create_failure(
                    FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT,
                    source_subsystem="model_gateway",
                    execution_id=effective_request.execution_id,
                )
            )
            raise ModelProviderCapacityUnavailableError(
                str(final_error or "model provider capacity exhausted"),
                retry_at=retry_at,
                wait_id=wait_id,
                failure=final_failure,
            ) from final_error
        if final_error is not None:
            final_failure = (
                attempts[-1].failure
                if attempts and attempts[-1].failure is not None
                else create_failure(
                    FailureReason.UNCLASSIFIED,
                    source_subsystem="model_gateway",
                    execution_id=effective_request.execution_id,
                    source_native_code=type(final_error).__name__,
                )
            )
            raise ModelProviderUnavailableError(
                str(final_error),
                failure=final_failure,
            ) from final_error
        raise ModelProviderUnavailableError(
            "no model attempt could run within budget",
            failure=create_failure(
                FailureReason.BUDGET_EXHAUSTED,
                source_subsystem="model_gateway",
                execution_id=effective_request.execution_id,
            ),
        )

    def goal_usage(
        self,
        goal_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ModelGoalUsage:
        rows = [
            item
            for item in self.store.load().invocations
            if item.goal_id == goal_id and self._same_scope(item, actor)
        ]
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        for row in rows:
            for attempt in row.attempts:
                input_tokens += int(attempt.input_tokens or 0)
                output_tokens += int(attempt.output_tokens or 0)
                cost_usd += float(attempt.actual_cost_usd or 0.0)
        return ModelGoalUsage(
            goal_id=goal_id,
            calls=len(rows),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

    def decision_usage(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ModelDecisionUsage:
        rows = [
            item
            for item in self.store.load().invocations
            if item.decision_id == decision_id and self._same_scope(item, actor)
        ]
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        for row in rows:
            for attempt in row.attempts:
                input_tokens += int(attempt.input_tokens or 0)
                output_tokens += int(attempt.output_tokens or 0)
                cost_usd += float(attempt.actual_cost_usd or 0.0)
        return ModelDecisionUsage(
            decision_id=decision_id,
            calls=len(rows),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

    def invocations(
        self,
        actor: AuthenticationActor,
        *,
        limit: int = 100,
    ) -> list[ModelInvocationRecord]:
        self._require_admin(actor)
        rows = [
            item for item in self.store.load().invocations if self._same_scope(item, actor)
        ]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return rows[: max(1, min(limit, 1000))]
