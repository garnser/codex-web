from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from codex_web.business_data_sources import (
    BUSINESS_DATA_SOURCE_CONTRACT,
    BusinessDataField,
    BusinessDataPage,
    BusinessDataSnapshot,
    BusinessDataSourceCapabilities,
    BusinessDataSourceCapability,
)


WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE = "codex-work-items"
WORK_ITEM_BUSINESS_DATA_SOURCE_INSTANCE = "codex-web://work-items"
WORK_ITEM_BUSINESS_OBJECT_TYPE = "project_delivery"


class WorkItemBusinessDataSource:
    """Read-only business projection over canonical GitLab-backed Work Items."""

    source_type = WORK_ITEM_BUSINESS_DATA_SOURCE_TYPE
    contract_version = BUSINESS_DATA_SOURCE_CONTRACT.current
    credential_required = False
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
        *,
        source_instance: str,
        project_id: str,
        load_states: Callable[[], dict[str, Any]],
        load_projects: Callable[[], list[Any]],
    ) -> None:
        self.source_instance = source_instance
        self.project_id = project_id
        self.load_states = load_states
        self.load_projects = load_projects

    def _project(self) -> Any:
        project = next(
            (
                item
                for item in self.load_projects()
                if getattr(item, "id", None) == self.project_id
            ),
            None,
        )
        if project is None:
            raise ValueError(
                f"BusinessDataSource project not found: {self.project_id}"
            )
        return project

    def _snapshot(self, scope: str) -> BusinessDataSnapshot:
        if scope != self.project_id:
            raise ValueError(
                "work-item BusinessDataSource scope must match its configured project"
            )
        project = self._project()
        states = sorted(
            (
                item
                for item in self.load_states().values()
                if getattr(item, "project_id", None) == self.project_id
            ),
            key=lambda item: str(getattr(item, "ref", "")),
        )
        open_states = tuple(
            item
            for item in states
            if getattr(item, "current_stage", None) != "closed"
            and getattr(item, "closed_at", None) is None
        )
        revision_payload = [
            {
                "ref": str(getattr(item, "ref", "")),
                "stage": str(getattr(item, "current_stage", "")),
                "closed_at": getattr(item, "closed_at", None),
                "updated_at": float(getattr(item, "updated_at", 0.0) or 0.0),
            }
            for item in states
        ]
        revision = hashlib.sha256(
            json.dumps(
                revision_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        updated_at = max(
            (float(getattr(item, "updated_at", 0.0) or 0.0) for item in states),
            default=0.0,
        )
        return BusinessDataSnapshot(
            object_type=WORK_ITEM_BUSINESS_OBJECT_TYPE,
            external_id=self.project_id,
            entity_key=self.project_id,
            entity_name=str(getattr(project, "name", self.project_id)),
            fields=(
                BusinessDataField(
                    source_field="open_delivery_work_items",
                    value=len(open_states),
                ),
            ),
            source_updated_at=updated_at,
            source_revision=revision,
        )

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        if limit < 1:
            raise ValueError("BusinessDataSource discovery limit must be positive")
        snapshot = self._snapshot(scope)
        return BusinessDataPage(
            items=(snapshot,),
            checkpoint=snapshot.source_revision,
            exhausted=True,
        )

    async def sync_since(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        return await self.discover_page(
            scope=scope,
            cursor=cursor,
            limit=limit,
        )

    async def read(
        self,
        *,
        object_type: str,
        external_id: str,
    ) -> BusinessDataSnapshot:
        if object_type != WORK_ITEM_BUSINESS_OBJECT_TYPE:
            raise ValueError(f"unsupported business object type: {object_type}")
        if external_id != self.project_id:
            raise ValueError(f"business project record not found: {external_id}")
        return self._snapshot(self.project_id)

    async def normalize_event(self, payload: object):
        return None
