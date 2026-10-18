"""Bounded read-only metadata context, not a credential/mount/network grant."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_serializer


def bounded_ids(values):
    if any(not value or value != value.strip() for value in values) or len(set(values)) != len(values):
        raise ValueError('Resource context requires unique nonempty canonical IDs')
    return values


ReadOnlyResourceIds = Annotated[tuple[str, ...], Field(max_length=8), AfterValidator(bounded_ids)]


class ReadOnlyResourceContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1.0']
    read_only_resource_ids: ReadOnlyResourceIds = Field(min_length=1)
    requester_identity_id: str = Field(min_length=1)
    work_item_ref: str = Field(min_length=1)


class ResourceContextModel(BaseModel):
    resource_context: ReadOnlyResourceContext | None = None

    @model_serializer(mode='wrap')
    def serialize_resource_context(self, handler):
        result=handler(self)
        if self.resource_context is None:
            result.pop('resource_context',None)
        if not getattr(self,'read_only_resource_ids',()):
            result.pop('read_only_resource_ids',None)
        return result
