from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

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
        kwargs: dict[str, Any] = {}
        if credential:
            kwargs["api_key"] = credential
        elif provider.adapter_type != "openai" or provider.base_url:
            # Local/OpenAI-compatible endpoints commonly require a syntactic
            # key even when they do not authenticate it.
            kwargs["api_key"] = "local"
        # Native OpenAI compatibility bootstrap intentionally leaves api_key
        # unset so the SDK can resolve OPENAI_API_KEY from the environment.
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
            status = getattr(exc, "status_code", None)
            message = str(exc).lower()
            unsupported_reasoning = (
                "reasoning_effort" in kwargs
                and status in {400, 422}
                and (
                    "reasoning" in message
                    or "unsupported" in message
                    or "unknown parameter" in message
                )
            )
            if unsupported_reasoning:
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


class AnthropicModelProviderAdapter:
    """Native Anthropic Messages API adapter for the canonical ModelGateway."""

    adapter_type = "anthropic"
    default_base_url = "https://api.anthropic.com"
    api_version = "2023-06-01"

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    @staticmethod
    def _classify_status(status_code: int, message: str) -> ModelProviderAdapterError:
        detail = f"Anthropic API {status_code}: {message}"
        if status_code in {408, 409, 429} or status_code >= 500:
            return ModelProviderTransientError(detail)
        return ModelProviderAdapterError(detail)

    @staticmethod
    def _messages(request: ModelInvocationRequest) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for item in request.messages:
            if item.role not in {"user", "assistant"}:
                raise ModelProviderAdapterError(
                    f"Anthropic adapter does not support message role: {item.role}"
                )
            messages.append({"role": item.role, "content": item.content})
        return messages

    async def invoke(
        self,
        provider: ModelProviderRecord,
        model: ModelDefinitionRecord,
        request: ModelInvocationRequest,
        *,
        credential: str | None,
    ) -> ModelProviderResult:
        if provider.credential_required and not credential:
            raise ModelProviderAdapterError("provider credential is required")
        if request.text_verbosity:
            raise ModelProviderAdapterError(
                "Anthropic adapter does not support canonical text_verbosity"
            )

        payload: dict[str, Any] = {
            "model": model.concrete_model,
            "messages": self._messages(request),
            "max_tokens": min(request.max_output_tokens, model.max_output_tokens),
        }
        if request.system_prompt:
            payload["system"] = request.system_prompt
        if request.reasoning_effort:
            if request.reasoning_effort not in {"low", "medium", "high"}:
                raise ModelProviderAdapterError(
                    f"unsupported Anthropic reasoning effort: {request.reasoning_effort}"
                )
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": request.reasoning_effort}

        headers = {
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }
        if credential:
            headers["x-api-key"] = credential
        base_url = (provider.base_url or self.default_base_url).rstrip("/") + "/"

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=request.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post("v1/messages", json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelProviderTransientError(
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderAdapterError(
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code >= 400:
            try:
                body = response.json()
                error = body.get("error") if isinstance(body, dict) else None
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else response.text
                )
            except ValueError:
                message = response.text
            raise self._classify_status(response.status_code, str(message or "request failed"))

        try:
            body = response.json()
        except ValueError as exc:
            raise ModelProviderAdapterError(
                "Anthropic API returned invalid JSON"
            ) from exc
        if not isinstance(body, dict):
            raise ModelProviderAdapterError("Anthropic API returned an invalid response")

        blocks = body.get("content") or []
        text_parts = [
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        request_id = response.headers.get("request-id") or body.get("id")
        return ModelProviderResult(
            text="\n".join(part for part in text_parts if part).strip(),
            usage=ModelProviderUsage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            ),
            provider_request_id=str(request_id) if request_id else None,
            stop_reason=(
                str(body["stop_reason"])
                if body.get("stop_reason") is not None
                else None
            ),
        )
