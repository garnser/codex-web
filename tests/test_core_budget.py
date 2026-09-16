from __future__ import annotations

import ast
import unittest
from pathlib import Path

CORE_PATH = Path(__file__).resolve().parents[1] / "codex_web" / "runtime" / "core.py"
MAX_CORE_BYTES = 262144
REMOVED_RUNTIME_DEFINITIONS = ['BotRuntime', '_append_work_item_event', '_archive_active_handoff', '_clear_thread_active', '_coerce_owner', '_current_status_label', '_derived_status_label_for_work_item', '_drain_thread_queue', '_enqueue_turn', '_ensure_work_item_lane_defaults', '_find_duplicate_queued_turn', '_gitlab_issue_timestamp', '_gitlab_json_request', '_gitlab_payload_timestamp', '_gitlab_projection_is_stale', '_gitlab_projection_semantics', '_gitlab_stage_from_projection', '_handle_bot_inbound', '_handle_slack_interaction', '_has_routing_labels', '_infer_artifact_state_from_gitlab_payload', '_infer_artifact_state_from_state', '_latest_gitlab_timestamp', '_load_active_turns', '_load_agent_channel_presence_settings', '_load_approval_messages', '_load_bot_bindings', '_load_bot_connections', '_load_bot_delivery_targets', '_load_bot_details', '_load_bot_reply_targets', '_load_gitlab_routing_settings', '_load_gitlab_semantic_events', '_load_projects', '_load_slack_thread_icons', '_load_support_servicedesk_state', '_load_thread_index', '_load_thread_settings', '_load_turn_queues', '_load_work_item_states', '_mark_thread_active', '_normalize_artifact_state', '_normalize_blocking_findings', '_normalize_closed_work_item_state', '_normalize_work_item_stage', '_owner_label_from_labels', '_parse_gitlab_timestamp', '_pop_latest_queued_turn', '_pop_next_queued_turn', '_pop_queued_turn', '_preserve_accepted_handoff_recipient', '_priority_from_labels', '_publish_queue_status', '_reconcile_blocked_work_item_state', '_record_bot_approval_request', '_record_bot_outbound', '_record_handoff_history', '_record_terminal_turn_result', '_record_thread_activity', '_requeue_turn_front', '_resolve_approval_request', '_resume_active_threads_after_startup', '_routing_error_detail', '_save_active_turns', '_save_agent_channel_presence_settings', '_save_approval_messages', '_save_bot_bindings', '_save_bot_connections', '_save_bot_delivery_targets', '_save_bot_details', '_save_bot_reply_targets', '_save_gitlab_routing_settings', '_save_gitlab_semantic_events', '_save_projects', '_save_slack_thread_icons', '_save_support_servicedesk_state', '_save_thread_index', '_save_thread_settings', '_save_turn_queues', '_save_work_item_state', '_save_work_item_states', '_schedule_queue_drain', '_schedule_terminal_thread_recovery', '_send_bot_details', '_send_bot_outbound', '_start_thread_turn_now', '_structured_ack', '_structured_handoff', '_structured_progress', '_sync_gitlab_issue_labels_from_work_item', '_sync_work_item_status_label', '_terminal_failure_window_seconds', '_thread_is_active', '_touch_work_item_progress', '_update_slack_approval_messages', '_upsert_work_item_state_from_gitlab_event', '_upsert_work_item_state_from_gitlab_issue', '_validate_handoff_edge', '_work_item_event', '_work_item_issue_ref_parts', '_work_item_split_brain_findings', '_work_item_state', '_work_item_state_public', 'gitlab_events']

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
