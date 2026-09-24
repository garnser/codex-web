from __future__ import annotations

from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from codex_web.devhealth import (
    build_context as build_devhealth_context,
    render_html as render_devhealth_html,
)
from codex_web.devstatus import (
    build_context as build_devstatus_context,
    render_html as render_devstatus_html,
)


class OperatorUiService:
    """Own operator UI presentation inputs and dev-health projections."""

    def __init__(
        self,
        *,
        static_dir: Path,
        version: Callable[[], str],
        health: Callable[[], dict[str, Any]],
        load_turn_queues: Callable[[], dict[str, list[Any]]],
        load_active_turns: Callable[[], dict[str, Any]],
        load_work_item_states: Callable[[], dict[str, Any]],
        event_hub: Any,
    ) -> None:
        self.static_dir = static_dir
        self.version = version
        self.health = health
        self.load_turn_queues = load_turn_queues
        self.load_active_turns = load_active_turns
        self.load_work_item_states = load_work_item_states
        self.event_hub = event_hub

    def index_html(self, *, base_href: str | None = None) -> str:
        version = self.version()
        html = (self.static_dir / "index.html").read_text()
        if base_href is not None:
            normalized_base = "/" + str(base_href).strip("/") if str(base_href).strip("/") else ""
            html = html.replace(
                "<head>",
                f'<head>\n    <base href="{escape(normalized_base + "/", quote=True)}" />',
                1,
            )
        html = html.replace(
            'href="static/styles.css"',
            f'href="static/styles.css?v={version}"',
        )
        html = html.replace(
            'src="static/app.js"',
            f'src="static/app.js?v={version}"',
        )
        html = html.replace(
            "</body>",
            (
                f'<script src="static/work_items_ui.js?v={version}" '
                'type="module"></script>\n'
                f'  <script src="static/orchestration_ui.js?v={version}" '
                'type="module"></script>\n'
                f'  <script src="static/approval_requests_ui.js?v={version}" '
                'type="module"></script>\n'
                f'  <script src="static/attention_ui.js?v={version}" '
                'type="module"></script>\n'
                "  </body>"
            ),
        )
        return html

    @staticmethod
    def devstatus_html(*, force_refresh: bool = False) -> str:
        return render_devstatus_html(
            build_devstatus_context(force_refresh=force_refresh)
        )

    def work_item_stats(self) -> dict[str, int]:
        open_states = [
            state
            for state in self.load_work_item_states().values()
            if state.current_stage != "closed"
        ]
        return {
            "open_count": len(open_states),
            "blocked_count": sum(
                1
                for state in open_states
                if state.current_stage == "failed_with_action_owner"
            ),
            "pending_handoff_count": sum(
                1
                for state in open_states
                if state.handoff and state.handoff.status == "pending"
            ),
            "release_gate_count": sum(
                1 for state in open_states if state.release_gate
            ),
            "ready_for_validation_count": sum(
                1
                for state in open_states
                if state.current_stage == "ready_for_validation"
            ),
            "implementation_active_count": sum(
                1
                for state in open_states
                if state.current_stage == "implementation_active"
            ),
        }

    def devhealth_html(self, *, force_refresh: bool = False) -> str:
        queues = self.load_turn_queues()
        status_context = build_devstatus_context(
            force_refresh=force_refresh
        )
        context = build_devhealth_context(
            self.health(),
            active_turns=len(self.load_active_turns()),
            queued_turns=sum(len(items) for items in queues.values()),
            status_context=status_context,
            work_item_stats=self.work_item_stats(),
            refresh_url="/devhealth?refresh=1",
        )
        return render_devhealth_html(context)
