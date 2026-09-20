from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import BotBinding
from codex_web.services.bot_binding_selection import (
    BotBindingSelectionService,
    install_bot_binding_selection_service,
)


class Host:
    def __init__(self, bindings: list[BotBinding] | None = None) -> None:
        self.bindings = bindings or []

    def _load_bot_bindings(self) -> list[BotBinding]:
        return [binding.model_copy(deep=True) for binding in self.bindings]

    @staticmethod
    def _binding_prefix(binding: BotBinding) -> str | None:
        return binding.route_prefix

    @staticmethod
    def _binding_report_name(binding: BotBinding) -> str | None:
        return binding.thread_name


def _service(host: Host) -> BotBindingSelectionService:
    return BotBindingSelectionService(
        host._load_bot_bindings,
        binding_report_name=host._binding_report_name,
        binding_prefix=host._binding_prefix,
    )


def binding(
    id: str,
    *,
    provider: str = "slack",
    conversation: str = "C1",
    thread: str = "t1",
    project: str = "home",
    name: str | None = None,
    prefix: str | None = None,
    master: bool = False,
    updated: float = 1,
) -> BotBinding:
    return BotBinding(
        id=id,
        provider=provider,
        external_conversation_id=conversation,
        thread_id=thread,
        project_id=project,
        thread_name=name,
        route_prefix=prefix,
        is_master=master,
        created_at=1,
        updated_at=updated,
    )


class BotBindingSelectionServiceTests(unittest.TestCase):
    def test_connection_lookup_is_provider_normalized_and_unique_lookup_is_strict(self) -> None:
        host = Host([
            binding("a", provider="slack", conversation="C1"),
            binding("b", provider="telegram", conversation="C1"),
        ])
        service = _service(host)

        matches = service.for_connection("SLACK", "C1")

        self.assertEqual([item.id for item in matches], ["a"])
        self.assertEqual(service.find_unique("slack", "C1").id, "a")
        self.assertIsNone(service.find_unique("slack", "missing"))

        host.bindings.append(binding("c", provider="slack", conversation="C1", thread="t2"))
        self.assertIsNone(service.find_unique("slack", "C1"))

    def test_thread_and_project_lookups_filter_only_requested_scope(self) -> None:
        host = Host([
            binding("a", thread="t1", project="p1"),
            binding("b", thread="t2", project="p1"),
            binding("c", provider="telegram", thread="t1", project="p2"),
        ])
        service = _service(host)

        self.assertEqual({item.id for item in service.for_thread("t1")}, {"a", "c"})
        self.assertEqual([item.id for item in service.for_project("SLACK", "p1")], ["a", "b"])

    def test_master_prefers_latest_but_orchestrator_prefers_named_master(self) -> None:
        host = Host([
            binding("orchestrator", project="p1", thread="orchestrator-thread", name="Orchestrator", master=True, updated=1),
            binding("latest", project="p1", thread="latest-thread", name="Manager", master=True, updated=5),
        ])
        service = _service(host)

        self.assertEqual(service.master("p1").id, "latest")
        self.assertEqual(service.orchestrator("p1").id, "orchestrator")

    def test_primary_prefers_orchestrator_thread_and_same_channel_clone(self) -> None:
        host = Host([
            binding("master", project="p1", conversation="C1", thread="t-orch", prefix="orchestrator", master=True, updated=1),
            binding("same-channel", project="p1", conversation="C2", thread="t-orch", prefix="orchestrator", master=False, updated=2),
            binding("other-master", project="p1", conversation="C3", thread="t-other", prefix="manager", master=True, updated=10),
        ])
        service = _service(host)

        self.assertEqual(service.primary_for_project("slack", "p1", "C2").id, "same-channel")
        self.assertEqual(service.primary_for_project("slack", "p1", "missing").id, "master")

    def test_indexed_callbacks_avoid_full_binding_loads(self) -> None:
        rows = [
            binding(
                "master",
                project="p1",
                thread="t-master",
                master=True,
                updated=5,
            ),
            binding(
                "agent",
                project="p1",
                thread="t-agent",
                conversation="C2",
            ),
        ]
        loads = 0

        def load_bindings():
            nonlocal loads
            loads += 1
            raise AssertionError("indexed routing must not load all bindings")

        by_id = {item.id: item for item in rows}
        by_thread = {
            "t-master": [rows[0]],
            "t-agent": [rows[1]],
        }
        by_project = {("slack", "p1"): rows}
        by_connection = {
            ("slack", "C1"): [rows[0]],
            ("slack", "C2"): [rows[1]],
        }
        service = BotBindingSelectionService(
            load_bindings,
            binding_report_name=lambda item: item.thread_name,
            binding_prefix=lambda item: item.route_prefix,
            lookup_by_id=by_id.get,
            indexed_for_connection=lambda provider, conversation: list(
                by_connection.get((provider, conversation), [])
            ),
            indexed_for_thread=lambda thread_id: list(
                by_thread.get(thread_id, [])
            ),
            indexed_for_project=lambda provider, project_id: list(
                by_project.get((provider, project_id), [])
            ),
            indexed_masters=lambda project_id: [
                item
                for item in rows
                if item.project_id == project_id and item.is_master
            ],
        )

        self.assertEqual(service.by_id("agent").id, "agent")
        self.assertEqual(service.for_thread("t-agent")[0].id, "agent")
        self.assertEqual(service.for_connection("SLACK", "C2")[0].id, "agent")
        self.assertEqual(service.for_project("SLACK", "p1")[0].id, "master")
        self.assertEqual(service.master("p1").id, "master")
        self.assertEqual(service.primary_for_project("slack", "p1").id, "master")
        self.assertEqual(loads, 0)

    def test_installer_rebinds_historical_lookup_surface(self) -> None:
        host = Host()
        app = SimpleNamespace(state=SimpleNamespace())

        service = install_bot_binding_selection_service(app, host)

        self.assertIs(app.state.bot_binding_selection_service, service)
        self.assertIs(host._bindings_for_connection.__self__, service)
        self.assertIs(host._bindings_for_thread.__self__, service)
        self.assertIs(host._primary_binding_for_project.__self__, service)
        self.assertIs(host._orchestrator_binding.__self__, service)


if __name__ == "__main__":
    unittest.main()
