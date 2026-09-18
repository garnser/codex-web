from __future__ import annotations

import ast
import unittest
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "codex_web" / "runtime"
CORE_PATH = RUNTIME_DIR / "core.py"
LEGACY_CORE_PATH = RUNTIME_DIR / "legacy_core.py"
MAX_CORE_BYTES = 4096
MAX_LEGACY_CORE_BYTES = 117772
REMOVED_RUNTIME_DEFINITIONS = ['BotRuntime', '_accepted_handoff_owner_idle_seconds', '_active_reply_target_for_binding', '_active_reply_target_for_thread_provider', '_append_work_item_event', '_archive_active_handoff', '_archive_replaced_bot_thread', '_atomic_write_text', '_base_developer_instructions', '_binding_for_agent', '_bindings_for_connection', '_bindings_for_project', '_bindings_for_thread', '_bot_connection', '_bot_connection_for_conversation', '_bot_connection_public', '_clear_thread_active', '_clone_binding_to_known_channel', '_coalesce_queued_work_item_wakeups', '_codex_web_internal_base_url', '_coerce_owner', '_coerce_thread_message_limit', '_compact_turn_queues', '_connection_identity', '_connection_matches_payload', '_conversation_target_for_binding', '_current_status_label', '_dedupe_bot_integrations', '_default_thread_message_limit', '_delivery_target_for_binding', '_derived_status_label_for_work_item', '_dispatch_support_servicedesk_ticket', '_drain_thread_queue', '_effective_developer_instructions', '_enqueue_turn', '_ensure_work_item_lane_defaults', '_external_target_key', '_find_bot_binding', '_find_duplicate_queued_turn', '_first_binding_for_connection', '_forget_approval_messages', '_forget_bot_reply_target', '_format_support_servicedesk_prompt', '_gitlab_api_base_url', '_gitlab_api_token', '_gitlab_event_id', '_gitlab_event_target_agents', '_gitlab_group_path', '_gitlab_issue_timestamp', '_gitlab_json_request', '_gitlab_label_names', '_gitlab_owner_agents', '_gitlab_payload_timestamp', '_gitlab_project_path_matches', '_gitlab_project_settings_for_payload', '_gitlab_projection_is_stale', '_gitlab_projection_semantics', '_gitlab_reference', '_gitlab_routing_agents', '_gitlab_routing_bindings_for_agent', '_gitlab_routing_bindings_for_master', '_gitlab_routing_enabled_for_project', '_gitlab_semantic_dedupe_seconds', '_gitlab_semantic_key', '_gitlab_semantic_key_for_state', '_gitlab_stage_from_projection', '_gitlab_token_for_project', '_gitlab_url', '_handle_bot_inbound', '_handle_slack_interaction', '_has_routing_labels', '_infer_artifact_state_from_gitlab_payload', '_infer_artifact_state_from_state', '_is_support_servicedesk_ticket_payload', '_issue_to_support_servicedesk_payload', '_latest_bot_detail', '_latest_gitlab_timestamp', '_legacy_agent_channel_presence_from_gitlab_file', '_load_active_turns', '_load_agent_channel_presence_settings', '_load_approval_messages', '_load_bot_bindings', '_load_bot_connections', '_load_bot_delivery_targets', '_load_bot_details', '_load_bot_reply_targets', '_load_gitlab_routing_settings', '_load_gitlab_semantic_events', '_load_projects', '_load_slack_thread_icons', '_load_support_servicedesk_state', '_load_thread_index', '_load_thread_settings', '_load_turn_queues', '_load_work_item_states', '_logical_binding_name', '_logical_bindings_for_binding', '_mark_thread_active', '_mask_secret', '_master_binding', '_master_reply_target_for_binding', '_max_steers_per_window', '_max_thread_queue_depth', '_migrate_agent_channel_presence_settings', '_migrate_gitlab_routing_settings', '_normalize_agent_channel_mapping', '_normalize_agent_channel_presence_project_settings', '_normalize_agent_channel_presence_settings', '_normalize_artifact_state', '_normalize_blocking_findings', '_normalize_closed_work_item_state', '_normalize_gitlab_project_settings', '_normalize_gitlab_routing_settings', '_normalize_string_list', '_normalize_work_item_stage', '_orchestrator_binding', '_orchestrator_watch_reason', '_orchestrator_watchdog_candidates', '_orchestrator_watchdog_loop', '_outbound_bindings_for_thread', '_owner_label_from_labels', '_owner_work_watchdog_loop', '_parse_agent_channel_overrides', '_parse_gitlab_timestamp', '_pop_latest_queued_turn', '_pop_next_queued_turn', '_pop_queued_turn', '_preferred_agent_conversation', '_preferred_agent_conversations', '_preferred_binding_for_replacement', '_preserve_accepted_handoff_recipient', '_primary_binding_for_project', '_priority_from_labels', '_publish_queue_status', '_queue_recovery_loop', '_raise_if_thread_replaced', '_reconcile_blocked_work_item_state', '_record_bot_approval_request', '_record_bot_detail', '_record_bot_outbound', '_record_handoff_history', '_record_terminal_turn_result', '_record_thread_activity', '_record_thread_steer', '_record_watchdog_dispatch', '_release_gate_watchdog_loop', '_release_validation_sla_seconds', '_remember_approval_message', '_remember_bot_delivery_target', '_remember_bot_reply_target', '_remember_gitlab_event', '_remember_gitlab_semantic_issue_state', '_remember_gitlab_semantic_key', '_remember_support_servicedesk_ticket', '_remember_thread_run_settings', '_remove_indexed_thread', '_render_work_item_wakeup_batch', '_replace_stale_bot_thread', '_replace_stale_web_thread', '_replacement_thread_id', '_reply_target_for_binding', '_reply_target_key', '_requeue_turn_front', '_resolve_approval_request', '_resume_active_threads_after_startup', '_retarget_active_turn', '_retarget_bot_details', '_retarget_bot_targets', '_retarget_bot_thread_state', '_retarget_logical_bot_bindings', '_retarget_slack_thread_icon', '_retarget_thread_settings', '_retarget_turn_queue', '_routing_error_detail', '_run_slack_backfill_cycle', '_run_support_servicedesk_sweep_once', '_same_logical_binding', '_save_active_turns', '_save_agent_channel_presence_settings', '_save_approval_messages', '_save_bot_bindings', '_save_bot_connections', '_save_bot_delivery_targets', '_save_bot_details', '_save_bot_reply_targets', '_save_gitlab_routing_settings', '_save_gitlab_semantic_events', '_save_json_private', '_save_projects', '_save_slack_thread_icons', '_save_support_servicedesk_state', '_save_thread_index', '_save_thread_settings', '_save_turn_queues', '_save_work_item_state', '_save_work_item_states', '_schedule_queue_drain', '_schedule_terminal_thread_recovery', '_send_bot_details', '_send_bot_outbound', '_slack_backfill_apply_rate_limit', '_slack_backfill_channels', '_slack_backfill_cooldown_remaining_seconds', '_slack_backfill_get_json', '_slack_backfill_interval_seconds', '_slack_backfill_is_rate_limited', '_slack_backfill_loop', '_slack_backfill_rate_limit_max_seconds', '_slack_backfill_rate_limit_min_seconds', '_slack_backfill_record_failure', '_slack_backfill_retry_after', '_slack_backfill_thread_targets', '_slack_backfill_window_seconds', '_split_brain_watchdog_candidates', '_split_brain_watchdog_loop', '_start_thread_turn_now', '_state_file_lock', '_steer_queued_turn', '_steer_window_seconds', '_structured_ack', '_structured_handoff', '_structured_progress', '_support_servicedesk_owner_agent', '_support_servicedesk_project_matches', '_support_servicedesk_project_paths', '_support_servicedesk_sweep_interval', '_support_servicedesk_sweep_lookback_hours', '_support_servicedesk_sweep_loop', '_support_servicedesk_sweep_project', '_support_servicedesk_ticket_key', '_support_servicedesk_ticket_seen', '_sync_bot_binding_settings', '_sync_gitlab_issue_labels_from_work_item', '_sync_work_item_status_label', '_target_for_external_thread', '_terminal_failure_window_seconds', '_thread_is_active', '_thread_queue', '_thread_queue_depth', '_thread_run_settings', '_thread_target_for_outbound', '_touch_work_item_progress', '_trim_thread_messages', '_update_bot_connection', '_update_slack_approval_messages', '_upsert_bot_connection', '_upsert_indexed_thread', '_upsert_work_item_state_from_gitlab_event', '_upsert_work_item_state_from_gitlab_issue', '_validate_handoff_edge', '_watchdog_dispatch_allowed', '_watchdog_dispatch_cooldown_seconds', '_watchdog_loop', '_work_item_contract_binding', '_work_item_contract_instructions', '_work_item_event', '_work_item_handoff_timeout_seconds', '_work_item_issue_ref_parts', '_work_item_progress_sla_seconds', '_work_item_sla_threshold_seconds', '_work_item_sla_watchdog_loop', '_work_item_split_brain_findings', '_work_item_state', '_work_item_state_public', '_work_item_wakeup_entries', 'account_rate_limits', 'ack_work_item_handoff', 'approvals', 'archive_thread', 'auth_verifier', 'bot_inbound', 'bot_status', 'create_bot_binding', 'create_project', 'create_thread', 'create_work_item_handoff', 'decide_approval', 'delete_project', 'diagnostics', 'diagnostics_route_test', 'get_agent_channel_presence', 'get_gitlab_integration', 'get_thread_settings', 'get_work_item', 'gitlab_events', 'healthz', 'interrupt_turn', 'list_bot_bindings', 'list_bot_channels', 'list_bot_connections', 'list_models', 'list_projects', 'list_thread_settings', 'list_threads', 'list_work_items', 'read_thread', 'recovery_resume', 'rename_thread', 'replace_bot_thread', 'resume_thread', 'save_bot_connection', 'shutdown', 'slack_events', 'start_turn', 'startup', 'status', 'steer_queued_turn', 'steer_specific_queued_turn', 'sweep_support_servicedesk', 'sync_work_items_from_gitlab', 'telegram_webhook', 'thread_queue', 'unarchive_thread', 'update_agent_channel_presence', 'update_gitlab_integration', 'update_thread_primary', 'update_thread_primary_channel', 'update_thread_settings', 'update_work_item_progress']


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
