from __future__ import annotations

from collections.abc import Iterable

from codex_web.business_data_sources import (
    BUSINESS_DATA_SOURCE_CONTRACT,
    BusinessDataEventKind,
    BusinessDataPage,
    BusinessDataSnapshot,
    BusinessDataSourceCapabilities,
    BusinessDataSourceCapability,
    BusinessDataSourceEvent,
)


class ReferenceCRMDataSource:
    """Deterministic CRM-like fixture with deltas, events and tombstones."""

    source_type = "reference-crm"
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

    def __init__(
        self,
        source_instance: str,
        snapshots: Iterable[BusinessDataSnapshot] = (),
    ) -> None:
        self.source_instance = source_instance
        self.snapshots = list(snapshots)
        self.fail_with: Exception | None = None

    def _page(
        self,
        cursor: str | None,
        limit: int,
    ) -> BusinessDataPage:
        if self.fail_with is not None:
            raise self.fail_with
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

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        return self._page(cursor, limit)

    async def sync_since(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        return self._page(cursor, limit)

    async def read(
        self,
        *,
        object_type: str,
        external_id: str,
    ) -> BusinessDataSnapshot:
        return next(
            item
            for item in self.snapshots
            if item.object_type == object_type
            and item.external_id == external_id
        )

    async def normalize_event(
        self,
        payload: object,
    ) -> BusinessDataSourceEvent | None:
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


class ReferenceBillingDataSource:
    """Polling-only billing-like fixture with no event or delta capability."""

    source_type = "reference-billing"
    contract_version = BUSINESS_DATA_SOURCE_CONTRACT.current
    capabilities = BusinessDataSourceCapabilities(
        frozenset(
            {
                BusinessDataSourceCapability.DISCOVERY,
                BusinessDataSourceCapability.PAGED_DISCOVERY,
                BusinessDataSourceCapability.READ,
            }
        )
    )

    def __init__(
        self,
        source_instance: str,
        snapshots: Iterable[BusinessDataSnapshot] = (),
    ) -> None:
        self.source_instance = source_instance
        self.snapshots = list(snapshots)
        self.fail_with: Exception | None = None

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        if self.fail_with is not None:
            raise self.fail_with
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

    async def sync_since(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        raise NotImplementedError("reference billing source is polling-only")

    async def read(
        self,
        *,
        object_type: str,
        external_id: str,
    ) -> BusinessDataSnapshot:
        return next(
            item
            for item in self.snapshots
            if item.object_type == object_type
            and item.external_id == external_id
        )

    async def normalize_event(
        self,
        payload: object,
    ) -> BusinessDataSourceEvent | None:
        raise NotImplementedError("reference billing source has no event capability")
