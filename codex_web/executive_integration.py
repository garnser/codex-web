from __future__ import annotations

import contextvars
import json
import os
import time
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from codex_web.api.identity import request_actor
from codex_web.execution_contracts import ROLE_CONTRACTS
from codex_web.identity import AuthenticationActor
from codex_web.model_gateway import (
    MODEL_CLASS_HIGH_REASONING,
    MODEL_CLASS_LIGHTWEIGHT,
    MODEL_CLASS_STRATEGIC,
    ModelDefinitionUpsert,
    ModelInvocationRequest,
    ModelMessage,
    ModelProviderUpsert,
    PromptTemplateUpsert,
)
from codex_web.executive import (
    AGENTS,
    ContextUpdate,
    DelegateRequest,
    ExecutiveChatRequest,
    ExecutiveService,
)
from codex_web.services.model_gateway import ModelGatewayService
from codex_web.services.executive_knowledge import (
    ExecutiveKnowledgeStore,
    ExecutiveKnowledgeUpsert,
)
from codex_web.storage.executive_state import ExecutiveStateStore


class MultiProviderExecutiveService(ExecutiveService):
    """ExecutiveService with native OpenAI and OpenAI-compatible local backends."""

    def __init__(
        self,
        host: Any,
        *,
        model_gateway: ModelGatewayService | None = None,
    ):
        super().__init__(host)
        # Executive runtime state uses the same SQLite document store as the
        # rest of codex-web, importing legacy JSON on first access and keeping
        # compatibility mirrors for rollback.
        self.store = ExecutiveStateStore(host)
        self.knowledge = ExecutiveKnowledgeStore(host)
        self.model_gateway = model_gateway
        self._knowledge_prompt: contextvars.ContextVar[str] = contextvars.ContextVar(
            "executive_knowledge_prompt",
            default="",
        )
        self._request_actor: contextvars.ContextVar[AuthenticationActor | None] = (
            contextvars.ContextVar("executive_request_actor", default=None)
        )
        self._invocation_ids: contextvars.ContextVar[list[str] | None] = (
            contextvars.ContextVar("executive_model_invocation_ids", default=None)
        )
        self.provider = (os.environ.get("CODEX_WEB_EXECUTIVE_PROVIDER") or "openai").strip().lower()
        if self.provider not in {"openai", "ollama", "openai-compatible"}:
            self.provider = "openai-compatible"
        self.base_url = (os.environ.get("CODEX_WEB_EXECUTIVE_BASE_URL") or "").strip() or None
        if self.provider == "ollama" and not self.base_url:
            self.base_url = "http://127.0.0.1:11434/v1"
        if self.provider == "ollama" and "CODEX_WEB_EXECUTIVE_MODEL" not in os.environ:
            self.model = "gpt-oss:20b"
        self.api_key_env = (os.environ.get("CODEX_WEB_EXECUTIVE_API_KEY_ENV") or "OPENAI_API_KEY").strip()
        self._bootstrap_gateway_compatibility()

    def _fallback_actor(self) -> AuthenticationActor | None:
        app = getattr(self.host, "app", None)
        identity = getattr(getattr(app, "state", None), "identity_service", None)
        if identity is None:
            return None
        try:
            return identity.local_trusted_actor()
        except Exception:
            return None

    def _gateway_actor(self) -> AuthenticationActor:
        actor = self._request_actor.get() or self._fallback_actor()
        if actor is None:
            raise RuntimeError(
                "Executive model gateway requires an authenticated request actor"
            )
        return actor

    def _bootstrap_gateway_compatibility(self) -> None:
        """Seed a local compatibility mapping only when no strategic model exists.

        This preserves current self-hosted environment defaults while moving
        runtime selection to stable model classes. Canonical administration may
        replace these records without changing Executive orchestration code.
        """
        if self.model_gateway is None:
            return
        actor = self._fallback_actor()
        if actor is None:
            return
        templates = self.model_gateway.list_templates(actor)
        if not any(
            item.template_id == "executive.system" and item.version == "1.0"
            for item in templates
        ):
            self.model_gateway.upsert_template(
                PromptTemplateUpsert(
                    template_id="executive.system",
                    version="1.0",
                    content="{{ instructions }}",
                ),
                actor=actor,
            )
        models = self.model_gateway.list_models(actor)
        if any(MODEL_CLASS_STRATEGIC in item.model_classes for item in models):
            return
        provider_id = f"executive-legacy-{self.provider}"
        self.model_gateway.upsert_provider(
            ModelProviderUpsert(
                id=provider_id,
                adapter_type=self.provider,
                display_name=f"Executive compatibility provider ({self.provider})",
                base_url=self.base_url,
                credential_required=False,
            ),
            actor=actor,
        )
        self.model_gateway.upsert_model(
            ModelDefinitionUpsert(
                id="executive-legacy-default",
                provider_id=provider_id,
                concrete_model=self.model,
                model_version="legacy-env",
                model_classes=(
                    MODEL_CLASS_STRATEGIC,
                    MODEL_CLASS_HIGH_REASONING,
                    MODEL_CLASS_LIGHTWEIGHT,
                ),
                capabilities=("text", "reasoning"),
                context_window_tokens=max(32768, self.store.max_context_tokens + 8192),
                max_output_tokens=8192,
                route_priority=100,
            ),
            actor=actor,
        )

    def _client(self) -> Any:
        if self._openai_client is not None:
            return self._openai_client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("The openai Python package is not installed; run pip install -r requirements.txt") from exc

        if self.provider == "openai":
            api_key = os.environ.get(self.api_key_env, "").strip()
            if not api_key:
                raise RuntimeError(f"{self.api_key_env} is not configured")
            self._openai_client = AsyncOpenAI(api_key=api_key)
            return self._openai_client

        api_key = os.environ.get(self.api_key_env, "").strip() or "ollama"
        if not self.base_url:
            raise RuntimeError("CODEX_WEB_EXECUTIVE_BASE_URL is required for openai-compatible providers")
        self._openai_client = AsyncOpenAI(api_key=api_key, base_url=self.base_url.rstrip("/") + "/")
        return self._openai_client

    async def _respond_legacy(
        self,
        instructions: str,
        messages: list[dict[str, str]],
    ) -> str:
        client = self._client()
        if self.provider == "openai":
            response = await client.responses.create(
                model=self.model,
                instructions=instructions,
                input=messages,
                reasoning={"effort": self.reasoning_effort},
                text={"verbosity": self.text_verbosity},
            )
            return response.output_text.strip()

        compatible_messages = [{"role": "system", "content": instructions}, *messages]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": compatible_messages,
        }
        if self.reasoning_effort in {"low", "medium", "high"}:
            kwargs["reasoning_effort"] = self.reasoning_effort
        response = await client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM provider returned no completion choices")
        content = choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            ).strip()
        return str(content or "").strip()

    async def _respond(
        self,
        instructions: str,
        messages: list[dict[str, str]],
        *,
        model_class: str = MODEL_CLASS_STRATEGIC,
        purpose: str = "executive-response",
    ) -> str:
        knowledge_prompt = self._knowledge_prompt.get()
        if knowledge_prompt:
            instructions = (
                f"{instructions}\n\nDURABLE COMPANY / PROJECT KNOWLEDGE\n"
                "The following entries are explicitly maintained operational knowledge. "
                "Use them as factual context, preserve their scope/provenance, and do not "
                "invent facts that are not present.\n"
                f"{knowledge_prompt}"
            )

        if self.model_gateway is None:
            return await self._respond_legacy(instructions, messages)

        result = await self.model_gateway.invoke(
            ModelInvocationRequest(
                model_class=model_class,
                messages=tuple(
                    ModelMessage(
                        role=str(item.get("role") or "user"),
                        content=str(item.get("content") or ""),
                    )
                    for item in messages
                ),
                system_prompt=instructions,
                prompt_template_id="executive.system",
                prompt_template_version="1.0",
                required_capabilities=("text", "reasoning"),
                max_output_tokens=4096,
                reasoning_effort=self.reasoning_effort,
                text_verbosity=self.text_verbosity,
                purpose=purpose,
            ),
            actor=self._gateway_actor(),
        )
        invocation_ids = self._invocation_ids.get()
        if invocation_ids is not None:
            invocation_ids.append(result.invocation.id)
        return result.text

    async def _compact_session_if_needed(self, session_id: str) -> None:
        plan = self.store.compaction_plan(session_id)
        if plan is None:
            return
        old, recent = plan
        transcript = "\n\n".join(
            f"{str(row.get('role') or 'unknown').upper()}: {str(row.get('content') or '')}"
            for row in old
        )
        instructions = (
            "Compact this earlier executive conversation into durable context. "
            "Preserve decisions, constraints, numeric assumptions, commitments, "
            "unresolved questions, owners and important rationale. Do not add new "
            "facts. Return concise plain text that can replace the earlier turns."
        )
        try:
            summary = await self._respond(
                instructions,
                [{"role": "user", "content": transcript}],
                model_class=MODEL_CLASS_LIGHTWEIGHT,
                purpose="executive-compaction",
            )
        except Exception:
            # Context safety is more important than allowing failed compaction to
            # make every future request oversized. The recent half-budget tail is
            # still retained verbatim; a later turn can compact successfully.
            self.store.replace_history(session_id, recent)
            return
        compacted = {
            "role": "assistant",
            "content": f"Compacted earlier executive context:\n{summary}",
            "at": time.time(),
        }
        self.store.replace_history(session_id, [compacted, *recent])

    async def chat(
        self,
        request: ExecutiveChatRequest,
        *,
        actor: AuthenticationActor | None = None,
    ):
        knowledge_prompt = self.knowledge.prompt_for(request.message)
        knowledge_token = self._knowledge_prompt.set(knowledge_prompt)
        actor_token = self._request_actor.set(actor) if actor is not None else None
        invocation_ids: list[str] = []
        invocation_token = self._invocation_ids.set(invocation_ids)
        try:
            result = await super().chat(request)
            await self._compact_session_if_needed(request.session_id)
            return result.model_copy(
                update={
                    "model_class": MODEL_CLASS_STRATEGIC,
                    "model_invocation_ids": list(invocation_ids),
                }
            )
        finally:
            self._invocation_ids.reset(invocation_token)
            if actor_token is not None:
                self._request_actor.reset(actor_token)
            self._knowledge_prompt.reset(knowledge_token)

    async def delegate(self, request: DelegateRequest) -> dict[str, Any]:
        project_id = request.project_id
        if request.work_item_ref:
            try:
                state = self.host._work_item_state(request.work_item_ref)
                project_id = state.project_id or project_id
            except Exception:
                pass
        knowledge_prompt = self.knowledge.prompt_for(request.task, project_id=project_id)
        if knowledge_prompt:
            existing = request.executive_reply.strip()
            augmented = (
                (existing + "\n\n") if existing else ""
            ) + "Durable company/project knowledge:\n" + knowledge_prompt
            request = request.model_copy(update={"executive_reply": augmented})
        return await super().delegate(request)

    def provider_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "baseUrl": self.base_url if self.provider != "openai" else None,
            "reasoningEffort": self.reasoning_effort,
            "textVerbosity": self.text_verbosity,
            "runtimeContextDefault": False,
            "modelGateway": self.model_gateway is not None,
            "modelClass": MODEL_CLASS_STRATEGIC,
            "maxContextTokens": self.store.max_context_tokens,
            "compactTargetTokens": self.store.compact_target_tokens,
            "stateBackend": "sqlite",
            "knowledgeEntries": len(self.knowledge.list()),
            "knowledgeBackend": "sqlite",
        }


def install_executive_integrated(
    app: FastAPI,
    host: Any,
    *,
    model_gateway: ModelGatewayService | None = None,
) -> MultiProviderExecutiveService:
    """Attach the Executive API router to the existing application once."""

    existing = getattr(app.state, "executive_service", None)
    if isinstance(existing, MultiProviderExecutiveService):
        return existing

    service = MultiProviderExecutiveService(host, model_gateway=model_gateway)
    router = APIRouter()

    @router.get("/api/executive/agents")
    async def list_agents() -> dict[str, Any]:
        return {
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "title": agent.title,
                    "description": agent.description,
                }
                for agent in AGENTS.values()
            ],
            "executionRoles": [role.public() for role in ROLE_CONTRACTS.values()],
            **service.provider_status(),
        }

    @router.get("/api/executive/context")
    async def get_context() -> dict[str, Any]:
        return {"company": service.store.company().model_dump()}

    @router.post("/api/executive/context")
    async def update_context(payload: ContextUpdate) -> dict[str, Any]:
        return {"ok": True, "company": service.store.save_company(payload.company).model_dump()}

    @router.get("/api/executive/knowledge")
    async def list_knowledge(
        scope: str | None = None,
        project_id: str | None = None,
        q: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if q:
            entries = service.knowledge.search(q, project_id=project_id, limit=limit)
        else:
            entries = service.knowledge.list(scope=scope, project_id=project_id)[: max(1, min(limit, 100))]
        return {"items": [entry.model_dump() for entry in entries]}

    @router.post("/api/executive/knowledge")
    async def upsert_knowledge(payload: ExecutiveKnowledgeUpsert) -> dict[str, Any]:
        try:
            entry = service.knowledge.upsert(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "item": entry.model_dump()}

    @router.delete("/api/executive/knowledge/{entry_id}")
    async def delete_knowledge(entry_id: str) -> dict[str, Any]:
        if not service.knowledge.delete(entry_id):
            raise HTTPException(status_code=404, detail="Executive knowledge entry not found")
        return {"ok": True}

    @router.get("/api/executive/runtime")
    async def executive_runtime() -> dict[str, Any]:
        return {
            **service.provider_status(),
            "threadMap": service.store.thread_map(),
            "summary": json.loads(service.runtime_summary()),
        }

    @router.post("/api/executive/chat")
    async def executive_chat(
        payload: ExecutiveChatRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return (
                await service.chat(
                    payload,
                    actor=request_actor(request),
                )
            ).model_dump()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            provider = service.provider_status()
            raise HTTPException(
                status_code=502,
                detail=f"Executive LLM request failed via {provider['provider']} ({provider['model']}): {exc}",
            ) from exc

    @router.post("/api/executive/delegate")
    async def executive_delegate(payload: DelegateRequest) -> dict[str, Any]:
        try:
            return await service.delegate(payload)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.include_router(router)
    app.state.executive_service = service
    return service
