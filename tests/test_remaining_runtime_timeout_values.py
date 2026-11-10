from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from codex_web.input_plugin_transports import HttpInputPlugin, ExternalInputTransportError
from codex_web.input_plugins import InputPhase
from codex_web.mammouth_cli_runtime import MammouthModelCatalog
from tests import test_input_plugin_transports as input_fixture
from tests import test_home_operator_coordination as home_fixture
from tests import test_skill_procedures as skill_fixture


class RemainingRuntimeTimeoutValuesTests(unittest.IsolatedAsyncioTestCase):
    def test_mammouth_catalog_passes_disabled_and_positive_timeout_to_real_probe_boundary(self):
        for value in (0, -1, 0.25):
            with self.subTest(value=value), \
                    patch("codex_web.mammouth_cli_runtime.shutil.which", return_value="/bin/mammouth"), \
                    patch("codex_web.mammouth_cli_runtime.subprocess.run",
                          return_value=SimpleNamespace(returncode=0, stdout="example-model\n")) as run:
                catalog = MammouthModelCatalog(timeout_seconds=value)
                self.assertTrue(catalog.models())
                self.assertEqual(run.call_args.kwargs["timeout"], None if value in (0, -1) else value)
        with patch.dict(os.environ, {"CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS": "0"}):
            self.assertIsNone(MammouthModelCatalog().timeout_seconds)
            self.assertEqual(MammouthModelCatalog(timeout_seconds=2).timeout_seconds, 2)

    async def test_external_plugin_disabled_transport_awaits_same_call_and_preserves_cancellation(self):
        fixture = input_fixture.ExternalInputPluginTransportTests(methodName="runTest")
        for value in (0, -1):
            entered, finished = asyncio.Event(), asyncio.Event()
            calls = []

            async def invoke(_request):
                calls.append(True)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()

            plugin = HttpInputPlugin(plugin_id="external", plugin_version="1", phase=InputPhase.COMPOSE,
                                     invoker=invoke, timeout_seconds=value)
            task = asyncio.create_task(plugin.transform(fixture._envelope(), fixture._context()))
            try:
                await asyncio.wait_for(entered.wait(), 30)  # fixture deadlock guard, not response SLA
                self.assertFalse(task.done())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(finished.is_set())
            self.assertEqual(calls, [True])

    async def test_external_plugin_positive_timeout_still_cancels_same_invocation(self):
        fixture = input_fixture.ExternalInputPluginTransportTests(methodName="runTest")
        finished = asyncio.Event()

        async def invoke(_request):
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        plugin = HttpInputPlugin(plugin_id="external", plugin_version="1", phase=InputPhase.COMPOSE,
                                 invoker=invoke, timeout_seconds=0.001)
        with self.assertRaises(ExternalInputTransportError):
            await plugin.transform(fixture._envelope(), fixture._context())
        self.assertTrue(finished.is_set())

    async def test_home_disabled_elapsed_guard_preserves_live_lease_and_explicit_positive_expiry(self):
        for value, expired_lease, allowed in ((0, False, True), (-1, False, True),
                                               (1, False, False), (0, True, False)):
            f = home_fixture.HomeCoordinationProviderTests(methodName="runTest")
            f.setUp()
            f.intent.timeout_seconds = value
            if expired_lease:
                f.intent.lease.expires_at = 99
            calls = []

            async def start(_thread, _payload, **kwargs):
                kwargs["admission_guard"]()
                self.assertFalse(kwargs["allow_assignment_renewal"])
                calls.append(True)
                return {"queuedId": "queued"}

            f.service.turns = SimpleNamespace(start=start)
            if allowed:
                result = await f.provider.execute(f.request, binding=f.provider_binding)
                self.assertEqual(result.output["queued_id"], "queued")
                self.assertEqual(calls, [True])
            else:
                with self.assertRaises(HTTPException):
                    await f.provider.execute(f.request, binding=f.provider_binding)
                self.assertEqual(calls, [])

    def test_skills_accept_positive_step_under_disabled_registered_operation_cap(self):
        for operation_limit in (0, -1, 5):
            f = skill_fixture.ProcedureTests(methodName="runTest")
            f.setUp()
            try:
                provider = f.actions.provider("ops", "test", actor=f.admin)
                operations = tuple(action.model_copy(update={"timeout_seconds": operation_limit})
                                   for action in provider.actions())
                content = skill_fixture.procedure()
                for step in content["steps"]:
                    if step.get("action"):
                        step["action"]["provider_contract_version"] = "1.3"
                with patch.object(provider, "contract_version", "1.3"), \
                        patch.object(provider, "actions", return_value=operations):
                    if operation_limit in (0, -1):
                        self.assertIsNotNone(f.draft(content=content))
                    else:
                        with self.assertRaises(skill_fixture.DefinitionGateDeniedError):
                            f.draft(content=content)
            finally:
                f.tearDown()
