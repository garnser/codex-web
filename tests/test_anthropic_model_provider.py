from __future__ import annotations

import json
import unittest

import httpx

from codex_web.failures import FailureReason
from codex_web.model_gateway import (
    ModelDefinitionRecord,
    ModelInvocationRequest,
    ModelMessage,
    ModelProviderRecord,
)
from codex_web.model_providers import (
    AnthropicModelProviderAdapter,
    ModelProviderAdapter,
    ModelProviderAdapterError,
    ModelProviderTransientError,
)


class AnthropicModelProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _provider(**overrides) -> ModelProviderRecord:
        payload = {
            "id": "anthropic-main",
            "adapter_type": "anthropic",
            "display_name": "Anthropic",
            "credential_ref": "secret-anthropic",
            "credential_required": True,
            "organization_id": "org-a",
            "workspace_id": "ws-a",
            "updated_by": "admin",
        }
        payload.update(overrides)
        return ModelProviderRecord(**payload)

    @staticmethod
    def _model(**overrides) -> ModelDefinitionRecord:
        payload = {
            "id": "claude",
            "provider_id": "anthropic-main",
            "concrete_model": "claude-sonnet-5",
            "model_classes": ("strategic",),
            "capabilities": ("text", "reasoning"),
            "max_output_tokens": 8192,
            "organization_id": "org-a",
            "workspace_id": "ws-a",
            "updated_by": "admin",
        }
        payload.update(overrides)
        return ModelDefinitionRecord(**payload)

    @staticmethod
    def _request(**overrides) -> ModelInvocationRequest:
        payload = {
            "model_class": "strategic",
            "system_prompt": "Be concise.",
            "messages": (
                ModelMessage(role="user", content="Summarize the plan."),
            ),
            "max_output_tokens": 1024,
            "reasoning_effort": "high",
        }
        payload.update(overrides)
        return ModelInvocationRequest(**payload)

    async def test_native_messages_request_and_response_are_normalized(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                headers={"request-id": "req_123"},
                json={
                    "id": "msg_123",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "First"},
                        {"type": "text", "text": "Second"},
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 21, "output_tokens": 8},
                },
            )

        adapter = AnthropicModelProviderAdapter(
            transport=httpx.MockTransport(handler)
        )
        self.assertIsInstance(adapter, ModelProviderAdapter)

        result = await adapter.invoke(
            self._provider(),
            self._model(),
            self._request(),
            credential="secret-value",
        )

        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "secret-value")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        body = seen["body"]
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["system"], "Be concise.")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(
            body["messages"],
            [{"role": "user", "content": "Summarize the plan."}],
        )
        self.assertEqual(result.text, "First\nSecond")
        self.assertEqual(result.usage.input_tokens, 21)
        self.assertEqual(result.usage.output_tokens, 8)
        self.assertEqual(result.provider_request_id, "req_123")
        self.assertEqual(result.stop_reason, "end_turn")

    async def test_rate_limit_is_transient(self) -> None:
        adapter = AnthropicModelProviderAdapter(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    429,
                    json={
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": "rate limited",
                        },
                    },
                )
            )
        )

        with self.assertRaises(ModelProviderTransientError) as caught:
            await adapter.invoke(
                self._provider(),
                self._model(),
                self._request(reasoning_effort=None),
                credential="secret-value",
            )
        self.assertEqual(
            caught.exception.reason_code,
            FailureReason.PROVIDER_CAPACITY_OR_RATE_LIMIT,
        )

    async def test_auth_and_context_failures_have_stable_reason_codes(self) -> None:
        for status, message, expected in (
            (
                401,
                "invalid api key",
                FailureReason.PROVIDER_AUTH_OR_ACCESS,
            ),
            (
                400,
                "maximum context length exceeded",
                FailureReason.CONTEXT_OVERFLOW,
            ),
        ):
            with self.subTest(status=status):
                adapter = AnthropicModelProviderAdapter(
                    transport=httpx.MockTransport(
                        lambda request, status=status, message=message: httpx.Response(
                            status,
                            json={
                                "type": "error",
                                "error": {
                                    "type": "test_error",
                                    "message": message,
                                },
                            },
                        )
                    )
                )
                with self.assertRaises(
                    ModelProviderAdapterError
                ) as caught:
                    await adapter.invoke(
                        self._provider(),
                        self._model(),
                        self._request(reasoning_effort=None),
                        credential="secret-value",
                    )
                self.assertEqual(
                    caught.exception.reason_code,
                    expected,
                )

    async def test_invalid_request_is_terminal(self) -> None:
        adapter = AnthropicModelProviderAdapter(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    400,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "bad request",
                        },
                    },
                )
            )
        )

        with self.assertRaises(ModelProviderAdapterError) as caught:
            await adapter.invoke(
                self._provider(),
                self._model(),
                self._request(reasoning_effort=None),
                credential="secret-value",
            )
        self.assertNotIsInstance(caught.exception, ModelProviderTransientError)

    async def test_unsupported_canonical_semantics_are_not_silently_dropped(self) -> None:
        adapter = AnthropicModelProviderAdapter()

        with self.assertRaisesRegex(ModelProviderAdapterError, "text_verbosity"):
            await adapter.invoke(
                self._provider(),
                self._model(),
                self._request(text_verbosity="high"),
                credential="secret-value",
            )

        with self.assertRaisesRegex(ModelProviderAdapterError, "message role"):
            await adapter.invoke(
                self._provider(),
                self._model(),
                self._request(
                    reasoning_effort=None,
                    messages=(ModelMessage(role="system", content="nested"),),
                ),
                credential="secret-value",
            )

    async def test_required_credential_must_arrive_through_gateway_boundary(self) -> None:
        adapter = AnthropicModelProviderAdapter()

        with self.assertRaisesRegex(
            ModelProviderAdapterError,
            "provider credential is required",
        ):
            await adapter.invoke(
                self._provider(),
                self._model(),
                self._request(reasoning_effort=None),
                credential=None,
            )


if __name__ == "__main__":
    unittest.main()
