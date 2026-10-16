from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from codex_web.models import QueuedTurn


WORK_ITEM_WAKEUP_BATCH_HEADER = (
    "Owned-work wakeup batch. Reconcile every listed item against canonical codex-web state, "
    "then process each item that is currently actionable."
)


class WorkItemWakeupQueuePolicy:
    """Own work-item wakeup parsing, batching, and queued-message compaction."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def entries(self, message: str) -> list[dict[str, str]]:
        if message.startswith(WORK_ITEM_WAKEUP_BATCH_HEADER):
            entries: list[dict[str, str]] = []
            for line in message.splitlines()[1:]:
                if not line.startswith("- "):
                    continue
                with contextlib.suppress(Exception):
                    raw = json.loads(line[2:])
                    if isinstance(raw, dict) and raw.get("ref"):
                        entries.append(
                            {
                                str(key): str(value)
                                for key, value in raw.items()
                                if value is not None
                            }
                        )
            return entries

        ref_match = re.search(
            r"\bOwned-work wakeup for ([\w.-]+/[\w.-]+#\d+)\b",
            message,
            re.IGNORECASE,
        )
        if not ref_match:
            return []

        def field(pattern: str) -> str:
            match = re.search(pattern, message, re.IGNORECASE | re.DOTALL)
            return " ".join((match.group(1) if match else "").split()).strip()

        entry = {
            "ref": ref_match.group(1),
            "classification": field(r"Change classification:\s*([^\.\n]+)"),
            "stage": field(r"Current stage:\s*([^\.\n]+)"),
            "next_action": field(r"Exact next action:\s*(.*?)(?:\s+If blocked,|\Z)"),
        }
        return [{key: value for key, value in entry.items() if value}]

    def render_batch(self, entries: list[dict[str, str]]) -> str:
        latest_by_ref: dict[str, dict[str, str]] = {}
        for entry in entries:
            ref = entry.get("ref")
            if not ref:
                continue
            normalized = dict(entry)
            if normalized.get("next_action"):
                normalized["next_action"] = self.host._truncate_text(
                    normalized["next_action"],
                    600,
                )
            latest_by_ref[ref] = normalized
        lines = [WORK_ITEM_WAKEUP_BATCH_HEADER]
        lines.extend(
            "- " + json.dumps(entry, separators=(",", ":"), sort_keys=True)
            for entry in latest_by_ref.values()
        )
        return "\n".join(lines)

    def _work_item_is_terminal(self, ref: str) -> bool:
        loader = getattr(self.host, "_work_item_state", None)
        if not callable(loader):
            loader = getattr(self.host, "_get_work_item_state_record", None)
        if not callable(loader):
            return False
        try:
            state = loader(ref)
        except Exception:
            return False
        if state is None:
            return False
        return bool(
            getattr(state, "current_stage", None) == "closed"
            or getattr(state, "closed_at", None) is not None
            or getattr(state, "terminal_outcome", None) is not None
        )

    def coalesce(self, items: list[QueuedTurn]) -> tuple[list[QueuedTurn], bool]:
        compacted = list(items)
        changed = False
        candidates = [
            (index, queued, self.entries(queued.message))
            for index, queued in enumerate(compacted)
            if queued.reply_target is None and queued.provider_notification_pin is None and queued.owner_routing_pin is None
        ]
        candidates = [candidate for candidate in candidates if candidate[2]]
        if len(candidates) >= 2:
            first_index, representative, _ = candidates[0]
            merged_entries = [
                entry
                for _, _, entries in candidates
                for entry in entries
            ]
            representative.message = self.render_batch(merged_entries)
            candidate_ids = {id(queued) for _, queued, _ in candidates}
            compacted = [
                queued for queued in compacted if id(queued) not in candidate_ids
            ]
            compacted.insert(min(first_index, len(compacted)), representative)
            changed = True

        # GitLab issue updates are state-reconciliation signals. If several
        # updates for the same canonical work item accumulated while an agent
        # was busy, only the newest one can be actionable; processing older
        # snapshots wastes queue capacity and may reintroduce stale routing.
        latest_gitlab_index: dict[tuple[str, str], int] = {}
        for index, queued in enumerate(compacted):
            if (queued.source == "gitlab" and queued.work_item_ref and queued.reply_target is None
                    and queued.provider_notification_pin is None and queued.owner_routing_pin is None):
                key = (queued.thread_id, queued.work_item_ref)
                previous = latest_gitlab_index.get(key)
                # Front requeue and recovery can reorder the list. Admission
                # time identifies recency; ID breaks equal-time ties stably.
                if previous is None or (queued.created_at, queued.id) > (
                    compacted[previous].created_at, compacted[previous].id
                ):
                    latest_gitlab_index[key] = index
        if latest_gitlab_index:
            deduplicated = [
                queued
                for index, queued in enumerate(compacted)
                if not (
                    queued.source == "gitlab"
                    and queued.work_item_ref
                    and queued.reply_target is None
                    and queued.provider_notification_pin is None
                    and queued.owner_routing_pin is None
                    and latest_gitlab_index[(queued.thread_id, queued.work_item_ref)] != index
                )
            ]
            if len(deduplicated) != len(compacted):
                compacted = deduplicated
                changed = True

        without_terminal_notifications = [
            queued
            for queued in compacted
            if not (
                queued.source == "gitlab"
                and queued.work_item_ref
                and queued.reply_target is None
                and queued.provider_notification_pin is None
                and queued.owner_routing_pin is None
                and self._work_item_is_terminal(queued.work_item_ref)
            )
        ]
        if len(without_terminal_notifications) != len(compacted):
            compacted = without_terminal_notifications
            changed = True

        return (compacted, changed) if changed else (items, False)

    def compact_queues(self) -> None:
        queues = self.host._load_turn_queues()
        before = {key: list(items) for key, items in queues.items()}
        changed = False
        for thread_id, items in list(queues.items()):
            queues[thread_id], queue_changed = self.coalesce(items)
            changed = changed or queue_changed
            if not queues[thread_id]:
                del queues[thread_id]
                changed = True
        if changed:
            self.host._save_turn_queues(queues)
            append_event = getattr(self.host, "_append_bot_event", None)
            if callable(append_event):
                for thread_id, items in before.items():
                    retained = queues.get(thread_id, [])
                    retained_ids = {item.id for item in retained}
                    retained_by_ref = {
                        (item.thread_id, item.work_item_ref): item
                        for item in retained
                        if item.source == "gitlab" and item.work_item_ref
                        and item.reply_target is None
                    }
                    replacements = []
                    for item in items:
                        replacement = retained_by_ref.get((item.thread_id, item.work_item_ref))
                        if (
                            item.id not in retained_ids and replacement is not None
                            and item.source == "gitlab" and item.reply_target is None
                        ):
                            replacements.append({
                                "removed_id": item.id, "retained_id": replacement.id,
                                "work_item_ref": item.work_item_ref, "project_id": item.project_id,
                            })
                    if replacements:
                        append_event({"type": "queued_gitlab_notifications_coalesced",
                                      "thread_id": thread_id, "replacements": replacements})


def install_work_item_wakeup_queue_policy(app: Any, host: Any) -> WorkItemWakeupQueuePolicy:
    existing = getattr(app.state, "work_item_wakeup_queue_policy", None)
    if isinstance(existing, WorkItemWakeupQueuePolicy) and existing.host is host:
        policy = existing
    else:
        policy = WorkItemWakeupQueuePolicy(host)
        app.state.work_item_wakeup_queue_policy = policy

    host.WORK_ITEM_WAKEUP_BATCH_HEADER = WORK_ITEM_WAKEUP_BATCH_HEADER
    host._work_item_wakeup_entries = policy.entries
    host._render_work_item_wakeup_batch = policy.render_batch
    host._coalesce_queued_work_item_wakeups = policy.coalesce
    host._compact_turn_queues = policy.compact_queues
    return policy
