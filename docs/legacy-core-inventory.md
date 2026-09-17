# Legacy core inventory

`codex_web/runtime/legacy_core.py` is still the migration boundary for a mixture of compatibility state, runtime orchestration, storage helpers, bot routing, work-item policy and diagnostics. This inventory is intentionally ownership-focused: each group should either move to an existing owner, become a thin compatibility alias, or remain only when the runtime layer is the correct owner.

## Completed cleanup blocks

### JSON/state-file primitives

Removed from legacy ownership:
- `_state_file_lock`
- `_atomic_write_text`
- `STATE_FILE_LOCKS`

Canonical ownership now lives in `codex_web/storage/json_files.py` as `state_file_lock()` / `atomic_write_text()`, with historical host names rebound during application composition.

### Bot connection management helpers

Moved out of legacy ownership into `codex_web/services/bot_connections.py`:
- `_bot_connection`
- `_bot_connection_for_conversation`
- `_mask_secret`
- `_bot_connection_public`
- `_connection_identity`
- `_connection_matches_payload`
- `_upsert_bot_connection`
- `_dedupe_bot_integrations`
- `_update_bot_connection`

`BotConnectionService` now owns lookup, public projection, mutation and deduplication while continuing to use the existing bot connection/binding repositories. Historical `core._...` names remain compatibility aliases to the extracted owner.

## Extraction candidates

### Thread execution settings and contract composition

Representative definitions:
- `_remember_thread_run_settings`
- `_thread_run_settings`
- `_work_item_contract_binding`
- `_work_item_contract_instructions`
- `_effective_developer_instructions`
- `_base_developer_instructions`
- `_sync_bot_binding_settings`

Likely owners: runtime execution / Executive integration / configuration storage. These functions currently mix persistence, policy and execution-contract construction.

This is the next cleanup block to analyze because it is smaller and more deterministic than the broader routing/dispatch surface.

### Bot binding/routing and dispatch

Representative definitions:
- `_binding_for_agent`
- `_logical_bindings_for_binding`
- `_master_binding`
- `_orchestrator_binding`
- `_dispatch_event_to_binding`
- `_find_bot_binding`
- `_first_binding_for_connection`
- `_bindings_for_connection`
- `_bindings_for_thread`
- `_bindings_for_project`
- `_primary_binding_for_project`

Likely owner: extracted bot-routing service. This is a high-value block but has broader behavioral surface than connection management and should remain a separate PR.

### Work-item policy and projection helpers

Representative definitions:
- `_work_item_handoff_timeout_seconds`
- `_work_item_progress_sla_seconds`
- `_release_validation_sla_seconds`
- `_accepted_handoff_owner_idle_seconds`
- `_leading_owner_cue_in_action`
- `_maybe_infer_pending_handoff_from_gitlab_projection`
- work-item wakeup parsing/rendering helpers

Likely owner: work-item service/state machine, with environment-derived timing in a small configuration boundary.

### Runtime queue/recovery controls

Representative definitions:
- `_active_turn_stale_seconds`
- `_queue_recovery_interval_seconds`
- `_active_turn_is_stale`
- `_release_stale_active_turn`
- `_thread_queue`
- `_thread_queue_depth`
- steer/rate-limit helpers

Likely owner: runtime execution / worker supervisor. Preserve only true process-global runtime state in `legacy_core.py` during migration.

### Diagnostics aggregation

Representative definition:
- `_devhealth_work_item_stats`

Likely owner: devhealth/observability adapter, consuming work-item repository/service state instead of reaching through legacy globals.

## Runtime-owned items to review rather than automatically move

The process-global task handles, active queue/drain task maps, terminal recovery maps, Codex start lock, shutdown flag and similar live in-memory coordination may legitimately remain in the runtime layer until their owning supervisors have absorbed them. They should be classified separately from persistence and domain-policy helpers so cleanup does not merely move globals for cosmetic reasons.

## Cleanup order

1. ~~Remove duplicated JSON/state-file primitives.~~ Completed.
2. ~~Move bot connection management helpers.~~ Completed by the bot connection service extraction.
3. Consolidate thread run settings / execution-contract composition.
4. Move binding lookup/routing helpers into the bot-routing owner.
5. Move work-item policy/projection helpers into the work-item owner.
6. Consolidate queue/recovery helpers under runtime execution/supervisors.
7. Move diagnostics aggregation to observability/devhealth.
8. Re-inventory remaining globals and compatibility shims.

Each extraction should include focused compatibility tests and tighten the legacy-core no-return/size ratchet after duplicate definitions are physically removed.
