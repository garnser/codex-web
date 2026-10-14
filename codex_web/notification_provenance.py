"""Canonical provider-event pins, supplied only by internal event dispatch."""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


def event_checksum(event):
    return hashlib.sha256(json.dumps(event.model_dump(mode="json"),sort_keys=True,
        separators=(",",":"),allow_nan=False).encode()).hexdigest()


class ProviderNotificationPin(BaseModel):
    model_config=ConfigDict(extra="forbid",frozen=True,str_strip_whitespace=True)
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1,max_length=200)
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_source: str = Field(min_length=1,max_length=1000)
    source_origin: str = Field(min_length=1,max_length=80)
    provider_event_kind: Literal["issue","merge_request","pipeline"]
    organization_id: str = Field(min_length=1,max_length=200)
    workspace_id: str = Field(min_length=1,max_length=200)
    project_id: str = Field(min_length=1,max_length=200)
    work_item_ref: str = Field(min_length=1,max_length=1000)
    message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_event(cls,event,*,source_origin,provider_event_kind,work_item_ref,message):
        return cls(event_id=event.event_id,event_sha256=event_checksum(event),event_source=event.source,
            source_origin=source_origin,provider_event_kind=provider_event_kind,
            organization_id=event.tenant_id,workspace_id=event.workspace_id,
            project_id=event.payload.get("project_id"),work_item_ref=work_item_ref,
            message_sha256=hashlib.sha256(message.encode()).hexdigest())

    def matches(self,queued,event):
        kind=str(event.payload.get("provider_event_type","")).split(".",1)[0] if event is not None else ""
        return (event is not None and event.event_id==self.event_id and event_checksum(event)==self.event_sha256
            and kind==self.provider_event_kind
            and event.source==self.event_source and event.tenant_id==self.organization_id
            and event.workspace_id==self.workspace_id and event.payload.get("project_id")==self.project_id
            and queued.project_id==self.project_id and queued.work_item_ref==self.work_item_ref
            and queued.handoff_requested_at is None and queued.source==self.source_origin
            and hashlib.sha256(queued.message.encode()).hexdigest()==self.message_sha256)


def verified_notification_ids(items,*,event_lookup,organization_id,workspace_id):
    """Fail closed per original if its exact canonical source is unavailable."""
    result=set()
    for item in items:
        pin=item.provider_notification_pin
        if (pin is None or pin.organization_id!=organization_id or pin.workspace_id!=workspace_id):
            continue
        try:
            event=event_lookup(pin.event_id)
            if pin.matches(item,event):
                result.add(item.id)
        except (ValueError,LookupError,RuntimeError):
            continue
    return frozenset(result)
