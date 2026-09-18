from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from codex_web.model_gateway import (
    ModelDefinitionRecord,
    ModelInvocationRequest,
    ModelProviderRecord,
    ModelProviderResult,
    ModelProviderUsage,
)


class ModelProviderAdapterError(RuntimeError):
    pass


class ModelProviderTransientError(ModelProviderAdapterError):
    pass


@runtime_checkable
class ModelProviderAdapter(Protocol):
    adapter_type: str

    async def invoke(
        self,
        provider: ModelProviderRecord,
        model: ModelDefinitionRecord,
        request: ModelInvocationRequest,
        *,
        credential: str | None,
    ) -> ModelProviderResult: ...


class OpenAIModelProviderAdapter:
    """Reference adapter for OpenAI Responses and OpenAI-compatible chat APIs."""

    adapter_type = "openai"

    @staticmethod
    def _classify(exc: Exception) -> ModelProviderAdapterError:
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__.lower()
        if (
            status == 429
            or (isinstance(status, int) and status >= 500)
            or any(token in name for token in ("ratelimit", "timeout", "connection", "unavailable"))
        ):
            return ModelProviderTransientError(f"{type(exc).__name__}: {exc}")
        return ModelProviderAdapterError(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _client(provider: ModelProviderRecord, credential: str | None):
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ModelProviderAdapterError(
                "The openai Python package is required for the OpenAI model adapter"
            ) from exc
        if provider.credential_required and not credential:
            raise ModelProviderAdapterError("provider credential is required")
        kwargs: dict[str, Any] = {"api_key": credential or "local"}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url.rstrip("/") + "/"
        return AsyncOpenAI(**kwargs)

    async def invoke(
        self,
        provider: ModelProviderRecord,
        model: ModelDefinitionRecord,
        request: ModelInvocationRequest,
        *,
        credential: str | None,
    ) -> ModelProviderResult:
        client = self._client(provider, credential)
        if provider.adapter_type == "openai":
            kwargs: dict[str, Any] = {
                "model": model.concrete_model,
                "instructions": request.system_prompt,
                "input": [item.model_dump(mode="json") for item in request.messages],
                "max_output_tokens": min(request.max_output_tokens, model.max_output_tokens),
            }
            if request.reasoning_effort:
                kwargs["reasoning"] = {"effort": request.reasoning_effort}
            if request.text_verbosity:
                kwargs["text"] = {"verbosity": request.text_verbosity}
            try:
                response = await client.responses.create(**kwargs)
            except Exception as exc:
                raise self._classify(exc) from exc
            usage = getattr(response, "usage", None)
            return ModelProviderResult(
                text=str(getattr(response, "output_text", "") or "").strip(),
                usage=ModelProviderUsage(
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                ),
                provider_request_id=getattr(response, "id", None),
            )

        if provider.adapter_type not in {"openai-compatible", "ollama"}:
            raise ModelProviderAdapterError(
                f"unsupported OpenAI adapter mode: {provider.adapter_type}"
            )
        messages = [
            {"role": "system", "content": request.system_prompt},
            *[item.model_dump(mode="json") for item in request.messages],
        ]
        kwargs = {
            "model": model.concrete_model,
            "messages": messages,
            "max_tokens": min(request.max_output_tokens, model.max_output_tokens),
        }
        if request.reasoning_effort in {"low", "medium", "high"}:
            kwargs["reasoning_effort"] = request.reasoning_effort
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "reasoning_effort" in kwargs:
                kwargs.pop("reasoning_effort", None)
                try:
                    response = await client.chat.completions.create(**kwargs)
                except Exception as retry_exc:
                    raise self._classify(retry_exc) from retry_exc
            else:
                raise self._classify(exc) from exc
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ModelProviderAdapterError("model provider returned no completion choices")
        content = choices[0].message.content
        if isinstance(content, list):
            text = "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            ).strip()
        else:
            text = str(content or "").strip()
        usage = getattr(response, "usage", None)
        return ModelProviderResult(
            text=text,
            usage=ModelProviderUsage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            ),
            provider_request_id=getattr(response, "id", None),
        )
