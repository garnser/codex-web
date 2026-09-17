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

    def coalesce(self, items: list[QueuedTurn]) -> tuple[list[QueuedTurn], bool]:
        candidates = [
            (index, queued, self.entries(queued.message))
            for index, queued in enumerate(items)
            if queued.reply_target is None
        ]
        candidates = [candidate for candidate in candidates if candidate[2]]
        if len(candidates) < 2:
            return items, False

        first_index, representative, _ = candidates[0]
        merged_entries = [
            entry
            for _, _, entries in candidates
            for entry in entries
        ]
        representative.message = self.render_batch(merged_entries)
        candidate_ids = {id(queued) for _, queued, _ in candidates}
        compacted = [queued for queued in items if id(queued) not in candidate_ids]
        compacted.insert(min(first_index, len(compacted)), representative)
        return compacted, True

    def compact_queues(self) -> None:
        queues = self.host._load_turn_queues()
        changed = False
        for thread_id, items in list(queues.items()):
            queues[thread_id], queue_changed = self.coalesce(items)
            changed = changed or queue_changed
        if changed:
            self.host._save_turn_queues(queues)


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
