from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import BotBinding, ThreadRunSettings
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
    install_thread_execution_settings_service,
)


class Host:
    def __init__(self) -> None:
        self.settings: dict[str, ThreadRunSettings] = {}
        self.bindings: list[BotBinding] = []
        self.gitlab_enabled = True

    def _load_thread_settings(self) -> dict[str, ThreadRunSettings]:
        return {key: value.model_copy(deep=True) for key, value in self.settings.items()}

    def _save_thread_settings(self, values: dict[str, ThreadRunSettings]) -> None:
        self.settings = {key: value.model_copy(deep=True) for key, value in values.items()}

    def _load_bot_bindings(self) -> list[BotBinding]:
        return [binding.model_copy(deep=True) for binding in self.bindings]

    def _save_bot_bindings(self, bindings: list[BotBinding]) -> None:
        self.bindings = [binding.model_copy(deep=True) for binding in bindings]

    def _bindings_for_thread(self, thread_id: str) -> list[BotBinding]:
        return [binding.model_copy(deep=True) for binding in self.bindings if binding.thread_id == thread_id]

    def _gitlab_routing_enabled_for_project(self, project_id: str) -> bool:
        return self.gitlab_enabled

    @staticmethod
    def _binding_report_name(binding: BotBinding) -> str | None:
        return binding.thread_name

    @staticmethod
    def _binding_prefix(binding: BotBinding) -> str | None:
        return binding.route_prefix



def service_for(host: Host) -> ThreadExecutionSettingsService:
    bindings = SimpleNamespace(for_thread=host._bindings_for_thread)
    return ThreadExecutionSettingsService(
        load_settings=host._load_thread_settings,
        save_settings=host._save_thread_settings,
        bindings=bindings,
        load_bindings=host._load_bot_bindings,
        save_bindings=host._save_bot_bindings,
        gitlab_routing_enabled_for_project=host._gitlab_routing_enabled_for_project,
        binding_report_name=host._binding_report_name,
        binding_prefix=host._binding_prefix,
    )


def binding(*, is_master: bool = False) -> BotBinding:
    return BotBinding(
        id="b1",
        provider="slack",
        external_conversation_id="C1",
        thread_id="t1",
        project_id="home",
        thread_name="orchestrator" if is_master else "james",
        route_prefix="orchestrator" if is_master else "james",
        is_master=is_master,
        sandbox="read-only",
        approval_policy="on-request",
        created_at=1,
        updated_at=2,
    )


class ThreadExecutionSettingsServiceTests(unittest.TestCase):
    def test_get_falls_back_to_binding_execution_settings(self) -> None:
        host = Host()
        host.bindings = [binding()]
        service = service_for(host)

        settings = service.get("t1")

        self.assertEqual(settings.sandbox, "read-only")
        self.assertEqual(settings.approval_policy, "on-request")

    def test_remember_persists_settings_and_syncs_binding(self) -> None:
        host = Host()
        host.bindings = [binding()]
        service = service_for(host)

        settings = service.remember(
            "t1",
            sandbox="workspace-write",
            approval_policy="never",
            model="gpt-test",
            reasoning_effort="high",
            repository_resource_id="repo-app",
            writable_repository_resource_ids=("repo-app", "repo-api", "repo-app"),
            read_only_repository_resource_ids=("repo-docs",),
            execution_profile_id="repository-write",
        )

        self.assertEqual(host.settings["t1"].model, "gpt-test")
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertEqual(settings.repository_resource_id, "repo-app")
        self.assertEqual(
            settings.writable_repository_resource_ids,
            ("repo-app", "repo-api"),
        )
        self.assertEqual(settings.read_only_repository_resource_ids, ("repo-docs",))
        self.assertEqual(settings.execution_profile_id, "repository-write")
        self.assertEqual(host.bindings[0].sandbox, "workspace-write")
        self.assertEqual(host.bindings[0].approval_policy, "never")

    def test_effective_instructions_add_contract_once_and_base_strips_it(self) -> None:
        host = Host()
        host.bindings = [binding(is_master=True)]
        service = service_for(host)

        contract = service.work_item_contract_instructions("t1")
        self.assertIsNotNone(contract)
        self.assertIn("Required endpoint usage", contract)
        self.assertIn("Orchestrator-specific rules", contract)

        effective = service.effective_developer_instructions("t1", "Custom instruction")
        self.assertIsNotNone(effective)
        self.assertTrue(effective.startswith("Custom instruction\n\n"))
        self.assertEqual(effective.count("codex-web structured work-item contract"), 1)
        self.assertEqual(effective.count("SECURITY TRUST BOUNDARY"), 1)

        stripped = service.base_developer_instructions("t1", effective)
        self.assertEqual(stripped, "Custom instruction")

    def test_contract_is_disabled_when_gitlab_routing_is_disabled(self) -> None:
        host = Host()
        host.bindings = [binding()]
        host.gitlab_enabled = False
        service = service_for(host)

        self.assertIsNone(service.work_item_contract_instructions("t1"))
        effective = service.effective_developer_instructions("t1", "Only custom")
        self.assertTrue(effective.startswith("Only custom\n\n"))
        self.assertIn("SECURITY TRUST BOUNDARY", effective)
        self.assertEqual(
            service.base_developer_instructions("t1", effective),
            "Only custom",
        )

    def test_installer_rebinds_historical_host_surface(self) -> None:
        host = Host()
        app = SimpleNamespace(state=SimpleNamespace())

        bindings = SimpleNamespace(for_thread=host._bindings_for_thread)
        service = install_thread_execution_settings_service(
            app,
            host,
            load_settings=host._load_thread_settings,
            save_settings=host._save_thread_settings,
            bindings=bindings,
            load_bindings=host._load_bot_bindings,
            save_bindings=host._save_bot_bindings,
        )

        self.assertIs(app.state.thread_execution_settings_service, service)
        self.assertIs(host._thread_run_settings.__self__, service)
        self.assertIs(host._remember_thread_run_settings.__self__, service)
        self.assertIs(host._effective_developer_instructions.__self__, service)
        self.assertIs(host._sync_bot_binding_settings.__self__, service)


if __name__ == "__main__":
    unittest.main()
