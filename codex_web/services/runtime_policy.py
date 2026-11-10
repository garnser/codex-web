from __future__ import annotations

import os
from pathlib import Path

from codex_web.timeout_values import normalize_timeout


class RuntimePolicy:
    """Typed runtime/autonomy timing policy backed by environment configuration."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def autonomy_enabled(self) -> bool:
        if (self.data_dir / "AUTONOMY_DISABLED").exists():
            return False
        value = (
            os.environ.get("CODEX_WEB_AUTONOMY_ENABLED") or "1"
        ).strip().casefold()
        return value not in {"0", "false", "no", "off"}

    @staticmethod
    def _seconds(
        name: str,
        default: float,
        *,
        minimum: float,
        disabled_below_or_equal_zero: bool = False,
    ) -> float:
        try:
            seconds = float(os.environ.get(name) or str(default))
        except ValueError:
            return default
        if disabled_below_or_equal_zero and seconds <= 0:
            return 0.0
        return max(minimum, seconds)

    def owner_work_watchdog_interval(self) -> float:
        if not self.autonomy_enabled():
            return 0.0
        return self._seconds(
            "CODEX_WEB_OWNER_WORK_WATCHDOG_SECONDS",
            30.0,
            minimum=10.0,
            disabled_below_or_equal_zero=True,
        )

    def release_gate_watchdog_interval(self) -> float:
        if not self.autonomy_enabled():
            return 0.0
        return self._seconds(
            "CODEX_WEB_RELEASE_GATE_WATCHDOG_SECONDS",
            300.0,
            minimum=60.0,
            disabled_below_or_equal_zero=True,
        )

    def work_item_sla_watchdog_interval(self) -> float:
        if not self.autonomy_enabled():
            return 0.0
        return self._seconds(
            "CODEX_WEB_WORK_ITEM_SLA_WATCHDOG_SECONDS",
            120.0,
            minimum=30.0,
            disabled_below_or_equal_zero=True,
        )

    def orchestrator_watchdog_interval(self) -> float:
        if not self.autonomy_enabled():
            return 0.0
        return self._seconds(
            "CODEX_WEB_ORCHESTRATOR_WATCHDOG_SECONDS",
            180.0,
            minimum=30.0,
            disabled_below_or_equal_zero=True,
        )

    def split_brain_watchdog_interval(self) -> float:
        if not self.autonomy_enabled():
            return 0.0
        return self._seconds(
            "CODEX_WEB_SPLIT_BRAIN_WATCHDOG_SECONDS",
            60.0,
            minimum=15.0,
            disabled_below_or_equal_zero=True,
        )

    def actionable_owner_continuity_delay(self) -> float:
        return self._seconds(
            "CODEX_WEB_ACTIONABLE_OWNER_CONTINUITY_DELAY_SECONDS",
            45.0,
            minimum=5.0,
        )

    def handoff_continuity_delay(self) -> float:
        return self._seconds(
            "CODEX_WEB_HANDOFF_CONTINUITY_DELAY_SECONDS",
            45.0,
            minimum=5.0,
        )

    def native_recovery_cooldown(self) -> float:
        return self._seconds(
            "CODEX_WEB_NATIVE_RECOVERY_SCHEDULE_COOLDOWN_SECONDS",
            30.0,
            minimum=1.0,
        )

    @staticmethod
    def _elapsed_timeout(name: str, default: float | None, *, minimum: float = 0.0) -> float:
        # Numeric zero travels through existing callers; timer owners normalize it.
        raw = os.environ.get(name)
        try:
            seconds = normalize_timeout(float(raw) if raw and raw.strip() else default)
        except (TypeError, ValueError):
            seconds = normalize_timeout(default)
        return 0.0 if seconds is None else max(minimum, seconds)

    def native_recovery_cycle_timeout(self) -> float:
        return self._elapsed_timeout(
            "CODEX_WEB_NATIVE_RECOVERY_CYCLE_TIMEOUT_SECONDS", 300.0, minimum=5.0)

    def continuity_dispatch_timeout(self) -> float:
        return self._elapsed_timeout(
            "CODEX_WEB_CONTINUITY_DISPATCH_TIMEOUT_SECONDS", 120.0, minimum=5.0)

    @staticmethod
    def local_preparation_timeout(default: float | None) -> float:
        return RuntimePolicy._elapsed_timeout(
            "CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS", default)

    @staticmethod
    def native_rpc_timeout(default: float | None) -> float:
        return RuntimePolicy._elapsed_timeout(
            "CODEX_WEB_NATIVE_RPC_TIMEOUT_SECONDS", default)

    @staticmethod
    def worker_wall_timeout(default: float | None) -> float:
        return RuntimePolicy._elapsed_timeout(
            "CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS", default)

    @staticmethod
    def procedure_operation_timeout(default: float | None) -> float:
        return RuntimePolicy._elapsed_timeout(
            "CODEX_WEB_PROCEDURE_OPERATION_TIMEOUT_SECONDS", default)

    @staticmethod
    def action_timeout(default: float | None) -> float:
        return RuntimePolicy._elapsed_timeout(
            "CODEX_WEB_ACTION_TIMEOUT_SECONDS", default)

    def local_worker_max_concurrency(self) -> int:
        try:
            value = int(os.environ.get("CODEX_WEB_LOCAL_WORKER_MAX_CONCURRENCY") or "8")
        except ValueError:
            value = 8
        return max(1, min(value, 128))

    def background_task_max_concurrency(self) -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_BACKGROUND_TASK_MAX_CONCURRENCY"
                )
                or "16"
            )
        except ValueError:
            value = 16
        return max(1, min(value, 128))

    def continuity_per_project_concurrency(self) -> int:
        try:
            value = int(
                os.environ.get(
                    "CODEX_WEB_CONTINUITY_PER_PROJECT_CONCURRENCY"
                )
                or "4"
            )
        except ValueError:
            value = 4
        return max(1, min(value, 32))
