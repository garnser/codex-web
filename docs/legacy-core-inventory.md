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

### Thread execution settings and contract composition

Moved out of legacy ownership into `codex_web/services/thread_execution_settings.py`:
- `_remember_thread_run_settings`
- `_thread_run_settings`
- `_codex_web_internal_base_url`
- `_work_item_contract_binding`
- `_work_item_contract_instructions`
- `_effective_developer_instructions`
- `_base_developer_instructions`
- `_sync_bot_binding_settings`

`ThreadExecutionSettingsService` now owns thread run-setting updates/fallbacks and the legacy GitLab work-item developer-instruction contract composition. The existing runtime-state repository still owns persistence, while historical `core._...` names remain compatibility aliases.

### Bot binding lookup and primary/master selection

Moved out of legacy ownership into `codex_web/services/bot_binding_selection.py`:
- `_find_bot_binding`
- `_first_binding_for_connection`
- `_bindings_for_connection`
- `_bindings_for_thread`
- `_bindings_for_project`
- `_master_binding`
- `_orchestrator_binding`
- `_primary_binding_for_project`

`BotBindingSelectionService` now owns deterministic lookup and primary/master selection while leaving agent-channel preference, cloning, cross-channel routing and dispatch behavior untouched.

## Extraction candidates

### Remaining bot binding/routing and dispatch behavior

Representative definitions:
- `_binding_for_agent`
- `_logical_bindings_for_binding`
- `_binding_for_external_target`
- `_clone_binding_for_conversation`
- `_clone_binding_to_known_channel`
- `_dispatch_event_to_binding`

Likely owner: extracted bot-routing service. The next routing cleanup should keep agent/channel preference and cloning separate from actual dispatch/queue/recovery behavior where practical.

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
2. ~~Move bot connection management helpers.~~ Completed.
3. ~~Consolidate thread run settings / execution-contract composition.~~ Completed.
4. ~~Move deterministic binding lookup/primary selection.~~ Completed.
5. Move remaining agent/channel preference and binding-cloning helpers.
6. Move work-item policy/projection helpers into the work-item owner.
7. Consolidate queue/recovery helpers under runtime execution/supervisors.
8. Move diagnostics aggregation to observability/devhealth.
9. Re-inventory remaining globals and compatibility shims.

Each extraction should include focused compatibility tests and tighten the legacy-core no-return/size ratchet after duplicate definitions are physically removed.
