from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from codex_web.cli_runtime import CliRuntimeReadinessStatus
from codex_web.mammouth_cli_runtime import (\n    MammouthCliAdapter,\n    MammouthCliJsonEventStream,\n)


class MammouthCliAdapterTests(unittest.TestCase):
    def test_readiness_probes_mammouth_provider_catalog(self) -> None:
        adapter = MammouthCliAdapter()
        self.assertEqual(
            tuple(adapter.readiness_command("/usr/bin/mammouth")),
            ("/usr/bin/mammouth", "models", "mammouth-ai"),
        )

    def test_successful_catalog_probe_is_ready_without_exposing_output(self) -> None:
        adapter = MammouthCliAdapter()
        result = adapter.interpret_readiness(
            executable="mammouth",
            resolved_executable="/usr/bin/mammouth",
            result=subprocess.CompletedProcess(
                ("mammouth", "models", "mammouth-ai"),
                0,
                stdout="mammouth-ai/mammouth-recommended",
                stderr="",
            ),
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.status, CliRuntimeReadinessStatus.READY)
        self.assertNotIn("mammouth-recommended", result.message or "")

    def test_failed_catalog_probe_is_authentication_or_configuration_not_ready(self) -> None:
        adapter = MammouthCliAdapter()
        result = adapter.interpret_readiness(
            executable="mammouth",
            resolved_executable="/usr/bin/mammouth",
            result=subprocess.CompletedProcess(
                ("mammouth", "models", "mammouth-ai"),
                1,
                stdout="",
                stderr="secret provider diagnostic",
            ),
        )
        self.assertEqual(result.status, CliRuntimeReadinessStatus.UNAUTHENTICATED)
        self.assertNotIn("secret provider diagnostic", result.message or "")

    def test_build_command_uses_noninteractive_json_mode_and_directory(self) -> None:
        adapter = MammouthCliAdapter()
        command = adapter.build_command(
            executable="/usr/bin/mammouth",
            cwd=Path("/workspace/repo"),
            prompt="Fix issue #844",
            model="mammouth-recommended",
            extra_args=("--variant", "high"),
            environment={"MAMMOUTH_API_KEY": "injected-by-worker"},
        )
        self.assertEqual(
            command.argv,
            (
                "/usr/bin/mammouth",
                "run",
                "--format",
                "json",
                "--dir",
                "/workspace/repo",
                "--model",
                "mammouth-ai/mammouth-recommended",
                "--variant",
                "high",
                "Fix issue #844",
            ),
        )
        self.assertEqual(command.environment, {"MAMMOUTH_API_KEY": "injected-by-worker"})

    def test_provider_qualified_model_is_preserved(self) -> None:
        adapter = MammouthCliAdapter()
        command = adapter.build_command(
            executable="mammouth",
            cwd=Path("/repo"),
            prompt="test",
            model="mammouth-ai/custom-model",
        )
        self.assertIn("mammouth-ai/custom-model", command.argv)

    def test_resume_uses_explicit_session_not_folder_last_session(self) -> None:
        adapter = MammouthCliAdapter()
        command = adapter.build_resume_command(
            executable="mammouth",
            cwd=Path("/repo"),
            session_id="session-123",
            prompt="continue",
        )
        self.assertIn("--session", command.argv)
        self.assertIn("session-123", command.argv)
        self.assertNotIn("--continue", command.argv)

    def test_resume_requires_explicit_session_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "session id"):
            MammouthCliAdapter().build_resume_command(
                executable="mammouth",
                cwd=Path("/repo"),
                session_id="  ",
                prompt="continue",
            )


class MammouthCliEventStreamTests(unittest.TestCase):
    def test_json_event_projects_session_and_turn_identity(self) -> None:
        stream = MammouthCliJsonEventStream()
        event = stream.parse(json.dumps({
            "type": "message.part.updated",
            "sessionID": "ses-1",
            "messageID": "msg-2",
            "properties": {"text": "hello"},
        }))
        self.assertEqual(event.event_type, "message.part.updated")
        self.assertEqual(event.provider_native_session_id, "ses-1")
        self.assertEqual(event.provider_native_turn_id, "msg-2")

    def test_session_id_carries_forward_for_subsequent_events(self) -> None:
        stream = MammouthCliJsonEventStream()
        stream.parse('{"type":"session.created","sessionID":"ses-1"}')
        event = stream.parse('{"type":"step_start"}')
        self.assertEqual(event.provider_native_session_id, "ses-1")

    def test_invalid_json_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid JSONL"):
            MammouthCliJsonEventStream().parse("not-json")


if __name__ == "__main__":
    unittest.main()
