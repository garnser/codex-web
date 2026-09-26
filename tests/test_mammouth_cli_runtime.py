from __future__ import annotations

import json
import tempfile
import subprocess
import unittest
from pathlib import Path

from codex_web.cli_runtime import CliRuntimeReadinessStatus
from codex_web.mammouth_cli_runtime import (
    MammouthCliAdapter,
    MammouthCliJsonEventStream,
    MammouthModelCatalog,
    humanize_mammouth_model_name,
)


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
    def test_step_start_maps_to_thread_turn_and_item_start(self) -> None:
        stream = MammouthCliJsonEventStream()
        events = stream.parse(
            json.dumps(
                {
                    "sessionID": "ses-1",
                    "type": "step_start",
                    "part": {"id": "part-1", "messageID": "msg-2"},
                }
            )
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["turn/started", "item/started"],
        )
        self.assertEqual(events[0].provider_native_session_id, "ses-1")
        self.assertEqual(events[0].provider_native_turn_id, "msg-2")
        self.assertEqual(events[1].payload["item"]["id"], "msg-2")

    def test_text_and_reasoning_parts_map_to_deltas_and_carry_session(self) -> None:
        stream = MammouthCliJsonEventStream()
        stream.parse(
            '{"sessionID":"ses-1","type":"step_start",'
            '"part":{"messageID":"msg-2"}}'
        )
        text = stream.parse(
            '{"type":"text","part":{"messageID":"msg-2","text":"hello"}}'
        )
        reasoning = stream.parse(
            '{"type":"reasoning","part":{"messageID":"msg-2","text":"think"}}'
        )
        self.assertEqual(
            [event.event_type for event in text],
            ["item/agentMessage/delta"],
        )
        self.assertEqual(text[0].payload["delta"], "hello")
        self.assertEqual(reasoning[0].event_type, "item/reasoning/summaryText/delta")
        self.assertEqual(reasoning[0].provider_native_session_id, "ses-1")

    def test_tool_use_maps_to_started_or_completed_item(self) -> None:
        stream = MammouthCliJsonEventStream()
        started = stream.parse(
            '{"sessionID":"ses-1","type":"tool_use","part":{"messageID":"msg-2",'
            '"callID":"call-1","name":"read","state":{"status":"running"}}}'
        )
        completed = stream.parse(
            '{"type":"tool_use","part":{"messageID":"msg-2",'
            '"callID":"call-1","name":"read",'
            '"state":{"status":"completed","output":"file contents"}}}'
        )
        self.assertEqual(started[0].event_type, "turn/started")
        self.assertEqual(started[1].event_type, "item/started")
        self.assertEqual(started[1].payload["item"]["type"], "dynamicToolCall")
        self.assertEqual(completed[0].event_type, "item/completed")
        self.assertEqual(completed[0].payload["item"]["status"], "completed")
        self.assertEqual(
            completed[0].payload["item"]["contentItems"],
            [{"type": "inputText", "text": "file contents"}],
        )

    def test_step_finish_completes_item_and_error_fails_turn(self) -> None:
        stream = MammouthCliJsonEventStream()
        finished = stream.parse(
            '{"sessionID":"ses-1","type":"step_finish",'
            '"part":{"messageID":"msg-2"}}'
        )
        failed = stream.parse(
            '{"type":"error","sessionID":"ses-1",'
            '"part":{"messageID":"msg-2","error":"hidden"}}'
        )
        self.assertEqual(finished[0].event_type, "turn/started")
        self.assertEqual(finished[1].event_type, "item/completed")
        self.assertEqual(failed[0].event_type, "turn/failed")
        self.assertNotIn("hidden", repr(failed[-1].payload))

    def test_invalid_and_unsupported_events_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid JSONL"):
            MammouthCliJsonEventStream().parse("not-json")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            MammouthCliJsonEventStream().parse('{"type":"session.status"}')


class MammouthModelCatalogTests(unittest.TestCase):
    def test_humanize_models_upstream_style_names(self) -> None:
        self.assertEqual(
            humanize_mammouth_model_name("claude-sonnet-5"),
            "Claude Sonnet 5",
        )
        self.assertEqual(
            humanize_mammouth_model_name("gpt-5.6-sol"),
            "GPT 5.6 Sol",
        )
        self.assertEqual(
            humanize_mammouth_model_name("glm-5.3-flash"),
            "GLM 5.3 Flash",
        )

    def test_catalog_qualifies_ids_humanizes_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            calls = Path(temp) / "calls"
            executable = Path(temp) / "mammouth"
            executable.write_text(
                "#!/bin/sh\n"
                f"echo probe >> {calls}\n"
                "echo claude-sonnet-5\n"
                "echo mammouth-ai/gpt-6-sol\n"
                "echo ''\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            catalog = MammouthModelCatalog(
                executable=str(executable),
                ttl_seconds=60,
            )
            entries = catalog.models()
            self.assertEqual(
                [entry["model"] for entry in entries],
                [
                    "mammouth-ai/claude-sonnet-5",
                    "mammouth-ai/gpt-6-sol",
                ],
            )
            self.assertEqual(
                entries[0]["displayName"],
                "Claude Sonnet 5",
            )
            self.assertEqual(catalog.models(), entries)
            self.assertEqual(calls.read_text().count("probe"), 1)

    def test_failed_catalog_probe_returns_no_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "mammouth"
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            executable.chmod(0o755)
            catalog = MammouthModelCatalog(
                executable=str(executable),
                environ={},
            )
            self.assertEqual(catalog.models(), ())


if __name__ == "__main__":
    unittest.main()
