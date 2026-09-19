from __future__ import annotations

from typing import Any

from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.canonical_events import CanonicalEventStore


class OrchestrationInspectorService:
    """Read-only projection over canonical event and autonomy state.

    UI refreshes hit this projection only. No observer, reasoner, model gateway,
    ActionIntent mutation, or provider call is reachable from this service.
    """

    def __init__(
        self,
        events: CanonicalEventStore,
        autonomy: AutonomyController,
    ) -> None:
        self.events = events
        self.autonomy = autonomy

    def snapshot(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        count = max(1, min(int(limit), 500))
        wanted_type = str(event_type or "").strip()
        wanted_source = str(source or "").strip().casefold()

        events = self.events.recent(limit=500)
        if wanted_type:
            events = [item for item in events if item.event_type == wanted_type]
        if wanted_source:
            events = [
                item
                for item in events
                if wanted_source in item.source.casefold()
            ]
        events = events[:count]

        autonomy = self.autonomy.status()
        cycles = autonomy["recent_cycles"]
        cycles_by_event: dict[str, list[dict[str, Any]]] = {}
        for cycle in cycles:
            cycles_by_event.setdefault(str(cycle["event_id"]), []).append(cycle)

        timeline = []
        for event in events:
            event_cycles = cycles_by_event.get(event.event_id, [])
            action_intent_ids = []
            for cycle in event_cycles:
                for intent_id in cycle.get("action_intent_ids") or ():
                    if intent_id not in action_intent_ids:
                        action_intent_ids.append(intent_id)
            timeline.append(
                {
                    "event": event.model_dump(mode="json"),
                    "cycles": event_cycles,
                    "filtering_result": (
                        event_cycles[0]["outcome"]
                        if event_cycles
                        else "no_autonomy_cycle"
                    ),
                    "reasoning": {
                        "invoked": any(
                            bool(cycle.get("reasoning_invoked"))
                            for cycle in event_cycles
                        ),
                        "reasons": [
                            str(cycle.get("reason") or "")
                            for cycle in event_cycles
                            if cycle.get("reason")
                        ],
                    },
                    "resulting_action_intent_ids": action_intent_ids,
                }
            )

        return {
            "control": autonomy["control"],
            "timeline": timeline,
            "dead_letters": autonomy["dead_letters"],
            "cycle_count": len(cycles),
            "event_count": len(timeline),
            "dependencies": {
                "scheduler": {
                    "available": False,
                    "issue": 158,
                    "reason": "durable scheduler foundation is not implemented yet",
                },
                "evaluation_replay": {
                    "available": False,
                    "issue": 159,
                    "reason": "evaluation/replay foundation is not implemented yet",
                },
                "attention_queue": {
                    "available": False,
                    "issue": 160,
                    "reason": "canonical attention queue foundation is not implemented yet",
                },
            },
        }
