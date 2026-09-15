from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException

from codex_web.executive import (
    AGENTS,
    ContextUpdate,
    DelegateRequest,
    ExecutiveChatRequest,
    ExecutiveService,
)


class MultiProviderExecutiveService(ExecutiveService):
    """ExecutiveService with native OpenAI and OpenAI-compatible local backends."""

    def __init__(self, host: Any):
        super().__init__(host)
        self.provider = (os.environ.get("CODEX_WEB_EXECUTIVE_PROVIDER") or "openai").strip().lower()
        if self.provider not in {"openai", "ollama", "openai-compatible"}:
            self.provider = "openai-compatible"
        self.base_url = (os.environ.get("CODEX_WEB_EXECUTIVE_BASE_URL") or "").strip() or None
        if self.provider == "ollama" and not self.base_url:
            self.base_url = "http://127.0.0.1:11434/v1"
        if self.provider == "ollama" and "CODEX_WEB_EXECUTIVE_MODEL" not in os.environ:
            self.model = "gpt-oss:20b"
        self.api_key_env = (os.environ.get("CODEX_WEB_EXECUTIVE_API_KEY_ENV") or "OPENAI_API_KEY").strip()

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

    async def _respond(self, instructions: str, messages: list[dict[str, str]]) -> str:
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
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            # Some OpenAI-compatible servers do not implement reasoning_effort.
            if "reasoning_effort" not in kwargs:
                raise
            kwargs.pop("reasoning_effort", None)
            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception:
                raise exc
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError("LLM provider returned no completion choices")
        content = choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
        return str(content or "").strip()

    def provider_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "baseUrl": self.base_url if self.provider != "openai" else None,
            "reasoningEffort": self.reasoning_effort,
            "textVerbosity": self.text_verbosity,
            "runtimeContextDefault": False,
        }


def install_executive_integrated(app: FastAPI, host: Any) -> MultiProviderExecutiveService:
    """Attach the Executive API router to the existing application once."""

    existing = getattr(app.state, "executive_service", None)
    if isinstance(existing, MultiProviderExecutiveService):
        return existing

    service = MultiProviderExecutiveService(host)
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
            **service.provider_status(),
        }

    @router.get("/api/executive/context")
    async def get_context() -> dict[str, Any]:
        return {"company": service.store.company().model_dump()}

    @router.post("/api/executive/context")
    async def update_context(payload: ContextUpdate) -> dict[str, Any]:
        return {"ok": True, "company": service.store.save_company(payload.company).model_dump()}

    @router.get("/api/executive/runtime")
    async def executive_runtime() -> dict[str, Any]:
        return {
            **service.provider_status(),
            "threadMap": service.store.thread_map(),
            "summary": json.loads(service.runtime_summary()),
        }

    @router.post("/api/executive/chat")
    async def executive_chat(payload: ExecutiveChatRequest) -> dict[str, Any]:
        try:
            return (await service.chat(payload)).model_dump()
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
