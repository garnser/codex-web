from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.storage.state_store import StateStore


class SlackBackfillCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    watermark: float = 0.0
    completed_at: float | None = None
    processed: int = 0
    skipped: int = 0
    errors: int = 0


class SlackBackfillState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    checkpoints: dict[str, SlackBackfillCheckpoint] = Field(
        default_factory=dict
    )
    last_start_at: float | None = None
    last_completion_at: float | None = None
    last_duration_seconds: float | None = None
    last_processed: int = 0
    last_skipped: int = 0
    last_errors: int = 0
    current_cursor: str | None = None
    last_completed_cursor: str | None = None
    target_count: int = 0
    coalesced_cycles: int = 0
    cooldown_until: float | None = None
    rate_limit_failures: int = 0


class SlackBackfillStore:
    namespace = "slack_backfill"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> SlackBackfillState:
        return SlackBackfillState.model_validate(
            self.store.get(self.namespace) or {}
        )

    def update(
        self,
        updater: Callable[[SlackBackfillState], SlackBackfillState],
    ) -> SlackBackfillState:
        raw = self.store.update(
            self.namespace,
            lambda current: updater(
                SlackBackfillState.model_validate(current or {})
            ).model_dump(mode="json"),
            default={},
        )
        return SlackBackfillState.model_validate(raw)

    def checkpoint(
        self,
        key: str,
        *,
        watermark: float,
        completed_at: float,
        processed: int,
        skipped: int,
        errors: int,
    ) -> SlackBackfillState:
        def apply(state: SlackBackfillState) -> SlackBackfillState:
            previous = state.checkpoints.get(key)
            state.checkpoints[key] = SlackBackfillCheckpoint(
                key=key,
                watermark=max(
                    float(watermark),
                    previous.watermark if previous else 0.0,
                ),
                completed_at=float(completed_at),
                processed=(
                    (previous.processed if previous else 0)
                    + int(processed)
                ),
                skipped=(
                    (previous.skipped if previous else 0)
                    + int(skipped)
                ),
                errors=(
                    (previous.errors if previous else 0)
                    + int(errors)
                ),
            )
            state.current_cursor = key
            state.last_completed_cursor = key
            return state

        return self.update(apply)

    def diagnostics(self) -> dict[str, Any]:
        return self.load().model_dump(mode="json")
