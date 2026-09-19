from __future__ import annotations

from codex_web.business_data_sources import (
    BUSINESS_DATA_SOURCE_CONTRACT,
    BusinessDataEventKind,
    BusinessDataPage,
    BusinessDataSnapshot,
    BusinessDataSourceCapabilities,
    BusinessDataSourceCapability,
    BusinessDataSourceEvent,
)


class ExampleCRMSource:
    source_type = "example-crm"
    contract_version = BUSINESS_DATA_SOURCE_CONTRACT.current
    capabilities = BusinessDataSourceCapabilities(
        frozenset(
            {
                BusinessDataSourceCapability.DISCOVERY,
                BusinessDataSourceCapability.PAGED_DISCOVERY,
                BusinessDataSourceCapability.INCREMENTAL_SYNC,
                BusinessDataSourceCapability.READ,
                BusinessDataSourceCapability.EVENTS,
                BusinessDataSourceCapability.TOMBSTONES,
            }
        )
    )

    def __init__(self, source_instance: str, snapshots=()) -> None:
        self.source_instance = source_instance
        self.snapshots = list(snapshots)

    def _page(self, cursor: str | None, limit: int) -> BusinessDataPage:
        offset = int(cursor or "0")
        rows = tuple(self.snapshots[offset : offset + limit])
        next_offset = offset + len(rows)
        exhausted = next_offset >= len(self.snapshots)
        return BusinessDataPage(
            items=rows,
            next_cursor=None if exhausted else str(next_offset),
            checkpoint=str(next_offset),
            exhausted=exhausted,
        )

    async def discover_page(self, *, scope: str, cursor=None, limit=100):
        return self._page(cursor, limit)

    async def sync_since(self, *, scope: str, cursor=None, limit=100):
        return self._page(cursor, limit)

    async def read(self, *, object_type: str, external_id: str):
        return next(
            row
            for row in self.snapshots
            if row.object_type == object_type and row.external_id == external_id
        )

    async def normalize_event(self, payload: object):
        if not isinstance(payload, dict):
            return None
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, BusinessDataSnapshot):
            return None
        return BusinessDataSourceEvent(
            event_id=str(payload.get("event_id") or ""),
            event_kind=(
                BusinessDataEventKind.TOMBSTONE
                if snapshot.tombstone
                else BusinessDataEventKind.UPSERT
            ),
            snapshot=snapshot,
            occurred_at=payload.get("occurred_at"),
        )
