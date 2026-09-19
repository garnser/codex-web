from __future__ import annotations

import asyncio

from adapter import ExampleTicketSource

from codex_web.models import TaskSourceIdentity
from codex_web.services.task_sources import TaskSourceSnapshot


async def main() -> None:
    identity = TaskSourceIdentity(
        source_type="example-ticket",
        source_instance="demo",
        external_id="TICKET-42",
        external_url="https://tickets.example.invalid/TICKET-42",
        revision="7",
    )
    source = ExampleTicketSource(
        snapshots=(
            TaskSourceSnapshot(
                identity=identity,
                title="Rotate staging credentials",
                source_state="review",
                owners=("maya",),
                labels=("security", "staging"),
            ),
        )
    )

    rows = await source.discover(scope="team-platform")
    projection = source.project(rows[0])

    print(rows[0])
    print(projection)

    event = await source.normalize_event(
        {
            "event_type": "ticket.updated",
            "external_id": "TICKET-42",
            "revision": "8",
            "title": "Rotate staging credentials",
            "state": "done",
            "owner": "maya",
            "labels": ["security", "staging"],
        }
    )
    print(event)
    print(source.project(event.snapshot) if event and event.snapshot else None)


if __name__ == "__main__":
    asyncio.run(main())
