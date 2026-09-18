from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from typing import Any

from codex_web.entitlements import (
    METRIC_MODEL_COST_USD,
    METRIC_MODEL_INPUT_TOKENS,
    METRIC_MODEL_OUTPUT_TOKENS,
    UsageEventCreate,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.model_gateway import (
    ModelDefinitionRecord,
    ModelDefinitionUpsert,
    ModelGatewayState,
    ModelInvocationAttempt,
    ModelInvocationRecord,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ModelLifecycle,
    ModelProviderRecord,
    ModelProviderStatus,
    ModelProviderUpsert,
    ModelRouteCandidate,
    ModelRouteResult,
    PromptTemplateRecord,
    PromptTemplateUpsert,
    TenantModelPolicy,
    TenantModelPolicyUpdate,
)
from codex_web.model_providers import (
    ModelProviderAdapter,
    ModelProviderAdapterError,
    ModelProviderTransientError,
)
from codex_web.services.entitlements import EntitlementService
from codex_web.services.identity import AuthorizationError
from codex_web.services.secrets import SecretBroker
from codex_web.storage.model_gateway import ModelGatewayStore


class ModelGatewayError(RuntimeError):
    pass


class ModelRoutingError(ModelGatewayError):
    pass


class ModelRegistryConflictError(ModelGatewayError):
    pass


class ModelProviderUnavailableError(ModelGatewayError):
    pass


class ModelGatewayService:
    def __init__(
        self,
        store: ModelGatewayStore,
        *,
        secret_broker: SecretBroker | None = None,
        entitlements: EntitlementService | None = None,
    ) -> None:
        self.store = store
        self.secret_broker = secret_broker
        self.entitlements = entitlements
        self.adapters: dict[str, ModelProviderAdapter] = {}

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
            self._provider(state, payload.provider_id, actor)
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

    def route(
        self,
        request: ModelInvocationRequest,
        *,
        actor: AuthenticationActor,
    ) -> ModelRouteResult:
        state = self.store.load()
        template = self._template(state, request, actor)
        policy = self._policy(state, actor)
        rendered_system_prompt = self._render_system_prompt(template, request)
        input_tokens = self._estimate_tokens(
            request,
            rendered_system_prompt=rendered_system_prompt,
        )
        requested_residency = set(policy.required_residency_tags) | set(
            request.required_residency_tags
        )
        requested_compliance = set(policy.required_compliance_tags) | set(
            request.required_compliance_tags
        )
        required_capabilities = set(request.required_capabilities)
        max_costs = [
            value
            for value in (policy.max_invocation_cost_usd, request.max_cost_usd)
            if value is not None
        ]
        effective_max_cost = min(max_costs) if max_costs else None
        preferred = {
            provider_id: index
            for index, provider_id in enumerate(request.preferred_provider_ids)
        }

        candidates: list[tuple[int, int, int, str, ModelRouteCandidate]] = []
        rejected: list[str] = []
        for model in state.models:
            if not self._same_scope(model, actor):
                continue
            if request.model_class not in model.model_classes:
                continue
            if model.lifecycle != ModelLifecycle.ACTIVE:
                rejected.append(f"{model.id}:lifecycle:{model.lifecycle.value}")
                continue
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
            if provider.status not in {
                ModelProviderStatus.ACTIVE,
                ModelProviderStatus.DEGRADED,
            }:
                rejected.append(f"{model.id}:provider_disabled")
                continue
            if policy.allowed_provider_ids and provider.id not in policy.allowed_provider_ids:
                rejected.append(f"{model.id}:provider_not_allowed")
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
            preference = preferred.get(provider.id, len(preferred))
            degraded_penalty = 10000 if provider.status == ModelProviderStatus.DEGRADED else 0
            candidates.append(
                (
                    preference,
                    degraded_penalty,
                    model.route_priority,
                    model.id,
                    ModelRouteCandidate(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        estimated_input_tokens=input_tokens,
                        max_output_tokens=output_tokens,
                        estimated_upper_cost_usd=cost,
                        routing_reason=(
                            f"class={request.model_class};priority={model.route_priority};"
                            f"provider={provider.status.value}"
                        ),
                    ),
                )
            )

        candidates.sort(key=lambda item: item[:4])
        routed = tuple(item[4] for item in candidates)
        if not routed:
            detail = ",".join(rejected[:20]) or "no models registered for class"
            raise ModelRoutingError(
                f"no eligible model for class {request.model_class}: {detail}"
            )
        max_attempts = min(
            len(routed),
            policy.max_attempts if request.allow_fallback else 1,
        )
        return ModelRouteResult(
            model_class=request.model_class,
            prompt_template_id=template.template_id,
            prompt_template_version=template.version,
            prompt_template_checksum_sha256=template.checksum_sha256,
            candidates=routed,
            policy_max_attempts=max_attempts,
            policy_fingerprint_sha256=self._policy_hash(policy),
            effective_required_residency_tags=tuple(sorted(requested_residency)),
            effective_required_compliance_tags=tuple(sorted(requested_compliance)),
            effective_max_cost_usd=effective_max_cost,
        )

    def _append_invocation(self, record: ModelInvocationRecord) -> None:
        def apply(state: ModelGatewayState) -> ModelGatewayState:
            state.invocations.append(record)
            state.invocations = state.invocations[-5000:]
            return state

        self.store.update(apply)

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
        route = self.route(request, actor=actor)
        state = self.store.load()
        template = next(
            item
            for item in state.prompt_templates
            if item.template_id == route.prompt_template_id
            and item.version == route.prompt_template_version
            and item.checksum_sha256 == route.prompt_template_checksum_sha256
            and self._same_scope(item, actor)
        )
        rendered_request = request.model_copy(
            update={"system_prompt": self._render_system_prompt(template, request)}
        )
        attempts: list[ModelInvocationAttempt] = []
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
                        timeout=request.timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise ModelProviderTransientError(
                        f"provider attempt timed out after {request.timeout_seconds}s"
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
                            "model_class": request.model_class,
                        },
                    )
                else:
                    if provider.credential_required:
                        raise ModelProviderUnavailableError(
                            f"provider {provider.id} requires credential_ref"
                        )
                    result = await call(None)
            except ModelProviderTransientError as exc:
                completed = time.time()
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="transient_failure",
                        error_code=type(exc).__name__,
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
            except Exception as exc:
                completed = time.time()
                attempts.append(
                    ModelInvocationAttempt(
                        provider_id=provider.id,
                        model_id=model.id,
                        concrete_model=model.concrete_model,
                        model_version=model.model_version,
                        outcome="failure",
                        error_code=type(exc).__name__,
                        estimated_upper_cost_usd=candidate.estimated_upper_cost_usd,
                        started_at=started,
                        completed_at=completed,
                    )
                )
                final_error = exc
                break

            completed = time.time()
            actual_cost = self._price(
                model,
                result.usage.input_tokens or candidate.estimated_input_tokens,
                result.usage.output_tokens or candidate.max_output_tokens,
            )
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
                    actual_cost_usd=actual_cost,
                    provider_request_id=result.provider_request_id,
                    started_at=started,
                    completed_at=completed,
                )
            )
            record = ModelInvocationRecord(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.identity_id,
                model_class=request.model_class,
                purpose=request.purpose,
                prompt_template_id=route.prompt_template_id,
                prompt_template_version=route.prompt_template_version,
                prompt_template_checksum_sha256=route.prompt_template_checksum_sha256,
                rendered_prompt_sha256=self._rendered_prompt_hash(rendered_request),
                message_count=len(request.messages),
                input_character_count=len(rendered_request.system_prompt)
                + sum(len(item.content) for item in rendered_request.messages),
                required_capabilities=request.required_capabilities,
                required_residency_tags=route.effective_required_residency_tags,
                required_compliance_tags=route.effective_required_compliance_tags,
                max_cost_usd=route.effective_max_cost_usd,
                policy_fingerprint_sha256=route.policy_fingerprint_sha256,
                route_reason=candidate.routing_reason,
                work_item_ref=request.work_item_ref,
                goal_id=request.goal_id,
                decision_id=request.decision_id,
                execution_id=request.execution_id,
                attempts=tuple(attempts),
                selected_provider_id=provider.id,
                selected_model_id=model.id,
                selected_concrete_model=model.concrete_model,
                selected_model_version=model.model_version,
                status="succeeded",
                completed_at=completed,
            )
            self._append_invocation(record)
            self._meter_invocation(record, actor=actor)
            return ModelInvocationResponse(text=result.text, invocation=record)

        completed = time.time()
        record = ModelInvocationRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            actor_id=actor.identity_id,
            model_class=request.model_class,
            purpose=request.purpose,
            prompt_template_id=route.prompt_template_id,
            prompt_template_version=route.prompt_template_version,
            prompt_template_checksum_sha256=route.prompt_template_checksum_sha256,
            rendered_prompt_sha256=self._rendered_prompt_hash(rendered_request),
            message_count=len(request.messages),
            input_character_count=len(rendered_request.system_prompt)
            + sum(len(item.content) for item in rendered_request.messages),
            required_capabilities=request.required_capabilities,
            required_residency_tags=route.effective_required_residency_tags,
            required_compliance_tags=route.effective_required_compliance_tags,
            max_cost_usd=route.effective_max_cost_usd,
            policy_fingerprint_sha256=route.policy_fingerprint_sha256,
            route_reason="all eligible attempts exhausted",
            work_item_ref=request.work_item_ref,
            goal_id=request.goal_id,
            decision_id=request.decision_id,
            execution_id=request.execution_id,
            attempts=tuple(attempts),
            status="failed",
            completed_at=completed,
        )
        self._append_invocation(record)
        if final_error is not None:
            raise ModelProviderUnavailableError(str(final_error)) from final_error
        raise ModelProviderUnavailableError("no model attempt could run within budget")

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
