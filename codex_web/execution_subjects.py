from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ExecutionSubjectKind(StrEnum):
    WORK_ITEM = "work_item"
    THREAD = "thread"
    THREAD_BOOTSTRAP = "thread_bootstrap"


class ExecutionSubject(BaseModel):
    """Canonical subject whose work is executed by a worker/workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: ExecutionSubjectKind
    ref: str = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.ref}"


def normalize_execution_subject(
    subject: ExecutionSubject | None,
    work_item_ref: str | None,
) -> tuple[ExecutionSubject, str | None]:
    legacy = (work_item_ref or "").strip() or None
    if subject is None:
        if legacy is None:
            raise ValueError("execution subject is required")
        return ExecutionSubject(
            kind=ExecutionSubjectKind.WORK_ITEM,
            ref=legacy,
        ), legacy

    if subject.kind == ExecutionSubjectKind.WORK_ITEM:
        if legacy is not None and legacy != subject.ref:
            raise ValueError("work_item_ref conflicts with execution subject")
        return subject, subject.ref

    if legacy is not None:
        raise ValueError("work_item_ref is only valid for work_item execution subjects")
    return subject, None
