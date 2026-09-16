from __future__ import annotations

import ast
import unittest
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "codex_web" / "runtime"
CORE_PATH = RUNTIME_DIR / "core.py"
LEGACY_CORE_PATH = RUNTIME_DIR / "legacy_core.py"
MAX_CORE_BYTES = 4096
MAX_LEGACY_CORE_BYTES = 232360
REMOVED_RUNTIME_DEFINITIONS = ['BotRuntime', '_append_work_item_event', '_archive_active_handoff', '_archive_replaced_bot_thread', '_clear_thread_active', '_coerce_owner', '_current_status_label', '_derived_status_label_for_work_item', '_drain_thread_queue', '_enqueue_turn', '_ensure_work_item_lane_defaults', '_find_duplicate_queued_turn', '_forget_approval_messages', '_gitlab_issue_timestamp', '_gitlab_json_request', '_gitlab_payload_timestamp', '_gitlab_projection_is_stale', '_gitlab_projection_semantics', '_gitlab_stage_from_projection', '_handle_bot_inbound', '_handle_slack_interaction', '_has_routing_labels', '_infer_artifact_state_from_gitlab_payload', '_infer_artifact_state_from_state', '_latest_bot_detail', '_latest_gitlab_timestamp', '_load_active_turns', '_load_agent_channel_presence_settings', '_load_approval_messages', '_load_bot_bindings', '_load_bot_connections', '_load_bot_delivery_targets', '_load_bot_details', '_load_bot_reply_targets', '_load_gitlab_routing_settings', '_load_gitlab_semantic_events', '_load_projects', '_load_slack_thread_icons', '_load_support_servicedesk_state', '_load_thread_index', '_load_thread_settings', '_load_turn_queues', '_load_work_item_states', '_logical_binding_name', '_mark_thread_active', '_normalize_artifact_state', '_normalize_blocking_findings', '_normalize_closed_work_item_state', '_normalize_work_item_stage', '_owner_label_from_labels', '_parse_gitlab_timestamp', '_pop_latest_queued_turn', '_pop_next_queued_turn', '_pop_queued_turn', '_preferred_binding_for_replacement', '_preserve_accepted_handoff_recipient', '_priority_from_labels', '_publish_queue_status', '_raise_if_thread_replaced', '_reconcile_blocked_work_item_state', '_record_bot_approval_request', '_record_bot_detail', '_record_bot_outbound', '_record_handoff_history', '_record_terminal_turn_result', '_record_thread_activity', '_remember_approval_message', '_replace_stale_bot_thread', '_replace_stale_web_thread', '_replacement_thread_id', '_requeue_turn_front', '_resolve_approval_request', '_resume_active_threads_after_startup', '_retarget_active_turn', '_retarget_bot_details', '_retarget_bot_thread_state', '_retarget_logical_bot_bindings', '_retarget_slack_thread_icon', '_retarget_thread_settings', '_retarget_turn_queue', '_routing_error_detail', '_run_slack_backfill_cycle', '_same_logical_binding', '_save_active_turns', '_save_agent_channel_presence_settings', '_save_approval_messages', '_save_bot_bindings', '_save_bot_connections', '_save_bot_delivery_targets', '_save_bot_details', '_save_bot_reply_targets', '_save_gitlab_routing_settings', '_save_gitlab_semantic_events', '_save_projects', '_save_slack_thread_icons', '_save_support_servicedesk_state', '_save_thread_index', '_save_thread_settings', '_save_turn_queues', '_save_work_item_state', '_save_work_item_states', '_schedule_queue_drain', '_schedule_terminal_thread_recovery', '_send_bot_details', '_send_bot_outbound', '_slack_backfill_apply_rate_limit', '_slack_backfill_channels', '_slack_backfill_cooldown_remaining_seconds', '_slack_backfill_get_json', '_slack_backfill_interval_seconds', '_slack_backfill_is_rate_limited', '_slack_backfill_loop', '_slack_backfill_rate_limit_max_seconds', '_slack_backfill_rate_limit_min_seconds', '_slack_backfill_record_failure', '_slack_backfill_retry_after', '_slack_backfill_thread_targets', '_slack_backfill_window_seconds', '_start_thread_turn_now', '_structured_ack', '_structured_handoff', '_structured_progress', '_sync_gitlab_issue_labels_from_work_item', '_sync_work_item_status_label', '_terminal_failure_window_seconds', '_thread_is_active', '_touch_work_item_progress', '_update_slack_approval_messages', '_upsert_work_item_state_from_gitlab_event', '_upsert_work_item_state_from_gitlab_issue', '_validate_handoff_edge', '_work_item_event', '_work_item_issue_ref_parts', '_work_item_split_brain_findings', '_work_item_state', '_work_item_state_public', 'gitlab_events', 'slack_events']


class CoreBudgetTests(unittest.TestCase):
    def test_core_is_only_a_thin_compatibility_alias(self) -> None:
        size = CORE_PATH.stat().st_size
        self.assertLessEqual(
            size,
            MAX_CORE_BYTES,
            f"core.py grew to {size} bytes; budget is {MAX_CORE_BYTES}. New runtime behavior belongs outside the compatibility alias.",
        )
        tree = ast.parse(CORE_PATH.read_text())
        top_level_defs = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        self.assertEqual(top_level_defs, [], f"core.py must remain definition-free: {top_level_defs}")

    def test_legacy_core_can_only_shrink(self) -> None:
        size = LEGACY_CORE_PATH.stat().st_size
        self.assertLessEqual(
            size,
            MAX_LEGACY_CORE_BYTES,
            f"legacy_core.py grew to {size} bytes; budget is {MAX_LEGACY_CORE_BYTES}. Extract or delete behavior instead of adding to the quarantined runtime.",
        )

    def test_extracted_runtime_definitions_do_not_return(self) -> None:
        tree = ast.parse(LEGACY_CORE_PATH.read_text())
        top_level = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        returned = sorted(set(REMOVED_RUNTIME_DEFINITIONS) & top_level)
        self.assertEqual(
            returned,
            [],
            f"Extracted runtime definitions returned to legacy_core.py: {returned}",
        )


if __name__ == "__main__":
    unittest.main()
