from __future__ import annotations

import ast
import unittest
from pathlib import Path

CORE_PATH = Path(__file__).resolve().parents[1] / "codex_web" / "runtime" / "core.py"
MAX_CORE_BYTES = 331776
REMOVED_RUNTIME_DEFINITIONS = ['BotRuntime', '_clear_thread_active', '_drain_thread_queue', '_enqueue_turn', '_find_duplicate_queued_turn', '_handle_bot_inbound', '_handle_slack_interaction', '_mark_thread_active', '_pop_latest_queued_turn', '_pop_next_queued_turn', '_pop_queued_turn', '_publish_queue_status', '_record_bot_approval_request', '_record_bot_outbound', '_record_terminal_turn_result', '_record_thread_activity', '_requeue_turn_front', '_resolve_approval_request', '_resume_active_threads_after_startup', '_schedule_queue_drain', '_schedule_terminal_thread_recovery', '_send_bot_details', '_send_bot_outbound', '_start_thread_turn_now', '_terminal_failure_window_seconds', '_thread_is_active', '_update_slack_approval_messages']

class CoreBudgetTests(unittest.TestCase):
    def test_core_stays_below_ratchet_budget(self) -> None:
        size = CORE_PATH.stat().st_size
        self.assertLessEqual(size, MAX_CORE_BYTES, f"core.py grew to {size} bytes; budget is {MAX_CORE_BYTES}. Extract new behavior instead of adding it back to core.py.")

    def test_extracted_runtime_definitions_do_not_return(self) -> None:
        tree = ast.parse(CORE_PATH.read_text())
        top_level = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        returned = sorted(set(REMOVED_RUNTIME_DEFINITIONS) & top_level)
        self.assertEqual(returned, [], f"Extracted runtime definitions returned to core.py: {returned}")

if __name__ == "__main__":
    unittest.main()
