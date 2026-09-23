from __future__ import annotations

import unittest
from pathlib import Path

from codex_web.codex_cli_runtime import (
    CodexCliAdapter,
    CodexCliJsonEventStream,
)


class CodexCliEventStreamTests(unittest.TestCase):
    def test_thread_started_establishes_session_for_following_events(self) -> None:
        stream = CodexCliJsonEventStream()

        started = stream.parse(
            '{"type":"thread.started","thread_id":"thread-123"}'
        )
        completed = stream.parse(
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}'
        )

        self.assertEqual(started.provider_native_session_id, "thread-123")
        self.assertEqual(completed.provider_native_session_id, "thread-123")
        self.assertEqual(completed.event_type, "item.completed")
        self.assertEqual(
            completed.payload["item"]["type"],
            "agent_message",
        )

    def test_explicit_turn_id_is_projected_when_provider_emits_one(self) -> None:
        stream = CodexCliJsonEventStream()
        stream.parse('{"type":"thread.started","thread_id":"thread-123"}')

        event = stream.parse(
            '{"type":"turn.completed","turn_id":"turn-9","usage":{"input_tokens":12}}'
        )

        self.assertEqual(event.provider_native_turn_id, "turn-9")
        self.assertEqual(event.payload["usage"]["input_tokens"], 12)

    def test_invalid_or_untyped_json_fails_closed(self) -> None:
        stream = CodexCliJsonEventStream()

        with self.assertRaisesRegex(ValueError, "invalid JSONL"):
            stream.parse("not-json")
        with self.assertRaisesRegex(ValueError, "must be an object"):
            stream.parse("[]")
        with self.assertRaisesRegex(ValueError, "has no type"):
            stream.parse('{"thread_id":"thread-123"}')
        with self.assertRaisesRegex(ValueError, "has no thread_id"):
            stream.parse('{"type":"thread.started"}')


class CodexCliResumeTests(unittest.TestCase):
    def test_resume_builds_provider_native_resume_command(self) -> None:
        adapter = CodexCliAdapter(
            sandbox="workspace-write",
            approval_policy="never",
        )

        command = adapter.build_resume_command(
            executable="/usr/bin/codex",
            cwd=Path("/workspace/repo"),
            session_id="thread-123",
            prompt="Continue the fix",
            model="gpt-5.6-sol",
            extra_args=("--ephemeral",),
        )

        self.assertEqual(
            command.argv,
            (
                "/usr/bin/codex",
                "--ask-for-approval",
                "never",
                "--model",
                "gpt-5.6-sol",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "-C",
                "/workspace/repo",
                "--ephemeral",
                "resume",
                "thread-123",
                "Continue the fix",
            ),
        )

    def test_resume_requires_session_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "session id"):
            CodexCliAdapter().build_resume_command(
                executable="codex",
                cwd=Path("/workspace/repo"),
                session_id=" ",
                prompt="continue",
            )


if __name__ == "__main__":
    unittest.main()
