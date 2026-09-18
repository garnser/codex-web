from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from codex_web.entitlements import (
    METRIC_MODEL_COST_USD,
    METRIC_MODEL_INPUT_TOKENS,
    METRIC_MODEL_OUTPUT_TOKENS,
)
from codex_web.executive import ExecutiveChatRequest
from codex_web.executive_integration import MultiProviderExecutiveService
from codex_web.model_gateway import (
    MODEL_CLASS_LIGHTWEIGHT,
    MODEL_CLASS_STRATEGIC,
    ModelDefinitionUpsert,
    ModelProviderResult,
    ModelProviderUpsert,
    ModelProviderUsage,
    PromptTemplateUpsert,
)
from codex_web.services.entitlements import EntitlementService
from codex_web.services.identity import IdentityService
from codex_web.services.model_gateway import ModelGatewayService
from codex_web.storage.entitlements import EntitlementStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.model_gateway import ModelGatewayStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Host:
    def __init__(self, data_dir: Path, app: FastAPI) -> None:
        self.DATA_DIR = data_dir
        self.app = app


class _ExecutiveAdapter:
    adapter_type = "fake"

    def __init__(self) -> None:
        self.model_ids: list[str] = []

    async def invoke(self, provider, model, request, *, credential):
        self.model_ids.append(model.id)
        return ModelProviderResult(
            text=f"gateway reply from {model.id}",
            usage=ModelProviderUsage(input_tokens=120, output_tokens=30),
            provider_request_id=f"request-{len(self.model_ids)}",
        )


class ExecutiveModelGatewayTests(unittest.TestCase):
    def _setup(self, root: Path):
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        actor = identity.local_trusted_actor()
        entitlements = EntitlementService(EntitlementStore(sqlite))
        gateway = ModelGatewayService(
            ModelGatewayStore(sqlite),
            entitlements=entitlements,
        )
        adapter = _ExecutiveAdapter()
        gateway.register_adapter(adapter)
        gateway.upsert_provider(
            ModelProviderUpsert(
                id="test-provider",
                adapter_type="fake",
                display_name="Test provider",
                credential_required=False,
            ),
            actor=actor,
        )
        gateway.upsert_template(
            PromptTemplateUpsert(
                template_id="executive.system",
                version="1.0",
                content="{{ instructions }}",
            ),
            actor=actor,
        )
        gateway.upsert_model(
            ModelDefinitionUpsert(
                id="strategic-model",
                provider_id="test-provider",
                concrete_model="strategic-concrete",
                model_version="2026-09",
                model_classes=(MODEL_CLASS_STRATEGIC,),
                capabilities=("text", "reasoning"),
                context_window_tokens=64000,
                max_output_tokens=4096,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=2.0,
                route_priority=1,
            ),
            actor=actor,
        )
        gateway.upsert_model(
            ModelDefinitionUpsert(
                id="lightweight-model",
                provider_id="test-provider",
                concrete_model="lightweight-concrete",
                model_version="2026-09",
                model_classes=(MODEL_CLASS_LIGHTWEIGHT,),
                capabilities=("text", "reasoning"),
                context_window_tokens=64000,
                max_output_tokens=4096,
                input_price_per_million_usd=0.2,
                output_price_per_million_usd=0.4,
                route_priority=1,
            ),
            actor=actor,
        )
        app = FastAPI()
        app.state.sqlite_state_store = sqlite
        app.state.identity_service = identity
        host = _Host(root, app)
        service = MultiProviderExecutiveService(host, model_gateway=gateway)
        return service, gateway, entitlements, adapter, actor

    def test_advisor_chat_routes_through_strategic_gateway_and_returns_invocation_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, gateway, entitlements, adapter, actor = self._setup(Path(temp_dir))

            response = asyncio.run(
                service.chat(
                    ExecutiveChatRequest(
                        message="Should we change our pricing strategy?",
                        session_id="session-1",
                    ),
                    actor=actor,
                )
            )

            self.assertEqual(response.reply, "gateway reply from strategic-model")
            self.assertEqual(response.model_class, MODEL_CLASS_STRATEGIC)
            self.assertEqual(adapter.model_ids, ["strategic-model"])
            self.assertEqual(len(response.model_invocation_ids), 1)
            invocation = gateway.invocations(actor)[0]
            self.assertEqual(response.model_invocation_ids, [invocation.id])
            self.assertEqual(invocation.model_class, MODEL_CLASS_STRATEGIC)
            self.assertEqual(invocation.selected_model_id, "strategic-model")

            self.assertEqual(
                entitlements.usage_total(actor, metric=METRIC_MODEL_INPUT_TOKENS),
                120,
            )
            self.assertEqual(
                entitlements.usage_total(actor, metric=METRIC_MODEL_OUTPUT_TOKENS),
                30,
            )
            self.assertGreater(
                entitlements.usage_total(actor, metric=METRIC_MODEL_COST_USD),
                0,
            )

    def test_compaction_uses_lightweight_model_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "CODEX_WEB_EXECUTIVE_MAX_CONTEXT_TOKENS": "1000",
                "CODEX_WEB_EXECUTIVE_COMPACT_TARGET_TOKENS": "500",
            },
            clear=False,
        ):
            service, gateway, _, adapter, actor = self._setup(Path(temp_dir))
            for index in range(5):
                service.store.append_history(
                    "session-compact",
                    "user",
                    f"{index}:" + ("x" * 1800),
                )

            token = service._request_actor.set(actor)
            try:
                asyncio.run(service._compact_session_if_needed("session-compact"))
            finally:
                service._request_actor.reset(token)

            self.assertEqual(adapter.model_ids, ["lightweight-model"])
            invocation = gateway.invocations(actor)[0]
            self.assertEqual(invocation.model_class, MODEL_CLASS_LIGHTWEIGHT)
            rows = service.store.store.get("executive_sessions")["session-compact"]
            self.assertTrue(
                rows[0]["content"].startswith("Compacted earlier executive context:")
            )

    def test_gateway_invocation_ledger_does_not_store_executive_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service, gateway, _, _, actor = self._setup(Path(temp_dir))
            secret_phrase = "PRIVATE EXECUTIVE FORECAST 12345"

            response = asyncio.run(
                service.chat(
                    ExecutiveChatRequest(
                        message=secret_phrase,
                        session_id="session-private",
                    ),
                    actor=actor,
                )
            )
            serialized = gateway.store.load().model_dump_json()

            self.assertNotIn(secret_phrase, serialized)
            self.assertNotIn(response.reply, serialized)
            self.assertIn(response.model_invocation_ids[0], serialized)

    def test_provider_status_keeps_legacy_defaults_but_reports_gateway_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"CODEX_WEB_EXECUTIVE_PROVIDER": "ollama"},
            clear=False,
        ):
            os.environ.pop("CODEX_WEB_EXECUTIVE_BASE_URL", None)
            os.environ.pop("CODEX_WEB_EXECUTIVE_MODEL", None)
            service, _, _, _, _ = self._setup(Path(temp_dir))
            status = service.provider_status()

            self.assertEqual(status["provider"], "ollama")
            self.assertEqual(status["model"], "gpt-oss:20b")
            self.assertTrue(status["modelGateway"])
            self.assertEqual(status["modelClass"], MODEL_CLASS_STRATEGIC)


if __name__ == "__main__":
    unittest.main()
