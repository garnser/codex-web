"""Native opt-in reaches owners constructed earlier, without starting a runtime."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from fastapi import HTTPException

from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.codex_reasoning_policy import ReasoningOnlyCodexAdapter
from codex_web.services.native_reasoning_runtime import register_native_reasoning_components
from codex_web.services.threads import ThreadService


class NativeReasoningRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.key = ('openai', 'codex-reasoning')
        self.ordinary = object()
        self.managers = {('openai', 'codex'): self.ordinary}
        self.fallback = Mock(return_value=object())
        self.execution = TurnExecutionService(NS(), session_managers=self.managers,
            session_manager=self.ordinary, runtime_adapter_factory=self.fallback)
        self.execution.procedure_admission = NS(reasoning_preparation=None)
        self.threads = ThreadService(runtime_transport=None, runtime_request_for_thread=None,
            event_sink=lambda event: None, session_managers=self.managers,
            session_manager=self.ordinary, runtime_adapter_factory=self.fallback)
        self.previous_notifier = Mock()
        self.telemetry = NS(observation_notifier=self.previous_notifier, subscribe=Mock())
        self.gateway, self.registry, self.executives = Mock(), Mock(), NS()
        self.components = NS(manager=object(), runtime_adapter=object(), model_adapter=object(),
            bridge=NS(observe_usage=Mock()), invocation_authorizer=object(),
            preparation_context=object())
        self.binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex-reasoning',
            capability_revision=1)

    def register(self):
        register_native_reasoning_components(self.components, gateway=self.gateway,
            registry=self.registry, telemetry=self.telemetry, session_managers=self.managers,
            thread_service=self.threads, turn_execution=self.execution, executives=self.executives)

    def test_registration_after_real_owner_construction_reaches_both_copies(self):
        for owner in (self.threads, self.execution):
            self.assertIsNot(owner.session_managers, self.managers)
            with self.assertRaises(HTTPException) as caught:
                owner._manager_for_binding(self.binding)
            self.assertEqual(caught.exception.status_code, 503)
        self.register()
        for managers in (self.managers, self.threads.session_managers,
                         self.execution.session_managers):
            self.assertIs(managers[self.key], self.components.manager)
            self.assertIs(managers[('openai', 'codex')], self.ordinary)
        ordinary_binding = self.binding.model_copy(update={'runtime_id': 'codex'})
        for owner in (self.threads, self.execution):
            self.assertIs(owner._manager_for_binding(self.binding), self.components.manager)
            self.assertIs(owner._manager_for_binding(ordinary_binding), self.ordinary)
            self.assertIs(owner._manager_for_binding(None), self.ordinary)
            self.assertIsInstance(owner.runtime_adapter_factory(self.binding, NS()),
                                  ReasoningOnlyCodexAdapter)
            self.assertIs(owner.runtime_adapter_factory(ordinary_binding, NS()),
                          self.fallback.return_value)
        self.assertIs(self.execution.procedure_admission.reasoning_preparation,
                      self.components.preparation_context)
        self.assertIs(self.executives.native_invocation_authorizer,
                      self.components.invocation_authorizer)
        self.registry.register.assert_called_once()
        self.gateway.register_adapter.assert_called_once_with(self.components.model_adapter)
        self.telemetry.subscribe.assert_called_once_with(self.components.runtime_adapter)
        record = object()
        self.telemetry.observation_notifier(record)
        self.components.bridge.observe_usage.assert_called_once_with(record)
        self.previous_notifier.assert_called_once_with(record)

    def test_existing_native_manager_in_any_owner_refuses_before_registration(self):
        for index in range(3):
            with self.subTest(index=index):
                self.setUp()
                maps = (self.managers, self.threads.session_managers,
                        self.execution.session_managers)
                incumbent = object()
                maps[index][self.key] = incumbent
                with self.assertRaisesRegex(ValueError, 'already configured'):
                    self.register()
                self.registry.register.assert_not_called()
                self.gateway.register_adapter.assert_not_called()
                self.telemetry.subscribe.assert_not_called()
                self.assertIs(self.telemetry.observation_notifier, self.previous_notifier)
                for position, managers in enumerate(maps):
                    if position == index:
                        self.assertIs(managers[self.key], incumbent)
                    else:
                        self.assertNotIn(self.key, managers)

    def test_shared_map_aliases_remain_supported_and_duplicate_optin_refuses(self):
        self.threads.session_managers = self.managers
        self.execution.session_managers = self.managers
        self.register()
        self.assertIs(self.threads._manager_for_binding(self.binding), self.components.manager)
        self.assertIs(self.execution._manager_for_binding(self.binding), self.components.manager)
        with self.assertRaisesRegex(ValueError, 'already configured'):
            self.register()
        self.registry.register.assert_called_once()
        self.gateway.register_adapter.assert_called_once()
