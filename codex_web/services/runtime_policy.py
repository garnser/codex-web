from __future__ import annotations

import os
from pathlib import Path


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
            600.0,
            minimum=60.0,
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
