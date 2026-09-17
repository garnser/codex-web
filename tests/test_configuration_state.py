from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.models import (
    AgentChannelPresenceSettings,
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    IndexedThread,
)
from codex_web.runtime import core
from codex_web.storage.configuration_state import (
    ConfigurationStateRepositories,
    install_configuration_state,
    migrate_agent_channel_presence_settings,
    migrate_gitlab_routing_settings,
    normalize_agent_channel_presence_settings,
    normalize_gitlab_routing_settings,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class ConfigurationStateTests(unittest.TestCase):
    def repositories(self, root: Path) -> ConfigurationStateRepositories:
        return ConfigurationStateRepositories(
            SQLiteStateStore(root / "codex-web.db"),
            host=core,
            gitlab_routing_file=root / "gitlab_routing.json",
            agent_channel_presence_file=root / "agent_channel_presence.json",
            thread_index_file=root / "thread_index.json",
            slack_thread_icons_file=root / "slack_thread_icons.json",
        )

    def test_gitlab_routing_imports_once_then_sqlite_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "gitlab_routing.json"
            legacy.write_text(json.dumps({"enabled": False, "ignored_event_kinds": ["note"], "projects": {}}))
            repositories = self.repositories(root)

            first = repositories.gitlab_routing.load()
            self.assertFalse(first.enabled)

            legacy.write_text(json.dumps({"enabled": True, "projects": {}}))
            second = repositories.gitlab_routing.load()
            self.assertFalse(second.enabled)

    def test_gitlab_routing_save_mirrors_normalized_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repositories = self.repositories(root)

            saved = repositories.gitlab_routing.save(
                GitLabRoutingSettings(enabled=False, ignored_event_kinds=["wiki_page"], projects={})
            )

            mirrored = json.loads((root / "gitlab_routing.json").read_text())
            stored = repositories.gitlab_routing.store.get("gitlab_routing")
            self.assertEqual(stored, mirrored)
            self.assertEqual(mirrored, saved.model_dump())
            self.assertEqual((root / "gitlab_routing.json").stat().st_mode & 0o777, 0o600)

    def test_thread_index_and_slack_icons_use_sqlite_with_rollback_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repositories = self.repositories(root)

            repositories.thread_index.save(
                [IndexedThread(id="thread-1", name="One", cwd="/workspace", updatedAt=123.0)]
            )
            repositories.slack_thread_icons.save({"thread-1": ":rocket:"})

            self.assertEqual(repositories.thread_index.load()[0].name, "One")
            self.assertEqual(repositories.slack_thread_icons.load()["thread-1"], ":rocket:")
            self.assertEqual(json.loads((root / "thread_index.json").read_text())[0]["id"], "thread-1")
            self.assertEqual(json.loads((root / "slack_thread_icons.json").read_text())["thread-1"], ":rocket:")

    def test_normalizers_are_storage_owned_and_preserve_legacy_shapes(self) -> None:
        migrated = migrate_gitlab_routing_settings(
            {
                "enabled": True,
                "channel_id": "C1",
                "project_mappings": [
                    {"project_id": "home", "namespace": "Group/Project"},
                ],
                "fallback_agents_by_kind": {"issue": ["Dana"]},
            }
        )
        normalized = normalize_gitlab_routing_settings(migrated)
        self.assertEqual(normalized.projects["home"].channel_ids, ["C1"])
        self.assertEqual(normalized.projects["home"].project_paths, ["group/project"])
        self.assertEqual(normalized.projects["home"].fallback_agents_by_kind, {"issue": ["dana"]})

        presence = migrate_agent_channel_presence_settings(
            {"default_project_id": "home", "agent_channels": {"Dana": ["C2", "C1", "C2"]}}
        )
        normalized_presence = normalize_agent_channel_presence_settings(presence)
        self.assertEqual(normalized_presence.projects["home"].agent_channels, {"dana": ["C1", "C2"]})

    def test_installer_rebinds_historical_normalizer_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = SimpleNamespace()
            app = SimpleNamespace(state=SimpleNamespace(sqlite_state_store=SQLiteStateStore(root / "codex-web.db")))

            # The installer imports canonical production paths; only verify the
            # compatibility functions are rebound after repository composition.
            install_configuration_state(app, host)

            self.assertIs(host._normalize_gitlab_routing_settings, normalize_gitlab_routing_settings)
            self.assertIs(host._migrate_gitlab_routing_settings, migrate_gitlab_routing_settings)
            self.assertIs(host._normalize_agent_channel_presence_settings, normalize_agent_channel_presence_settings)
            self.assertIs(host._migrate_agent_channel_presence_settings, migrate_agent_channel_presence_settings)


if __name__ == "__main__":
    unittest.main()
