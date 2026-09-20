from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.models import BotThreadDetail


class BotDetailService:
    """Own per-thread command/file detail bookkeeping."""

    def __init__(
        self,
        host: Any | None = None,
        *,
        load_details: Callable[[], dict[str, list[BotThreadDetail]]] | None = None,
        save_details: Callable[[dict[str, list[BotThreadDetail]]], None] | None = None,
    ) -> None:
        if host is not None:
            load_details = load_details or getattr(
                host,
                "_load_bot_details",
                None,
            )
            save_details = save_details or getattr(
                host,
                "_save_bot_details",
                None,
            )
        if load_details is None or save_details is None:
            raise TypeError("BotDetailService requires detail state dependencies")
        self.load_details = load_details
        self.save_details = save_details
        if host is not None:
            host._record_bot_detail = self.record
            host._latest_bot_detail = self.latest
            host._retarget_bot_details = self.retarget

    def record(
        self,
        thread_id: str,
        item_type: str,
        title: str,
        text: str,
    ) -> None:
        if not text.strip():
            return
        details = self.load_details()
        items = details.setdefault(thread_id, [])
        items.append(
            BotThreadDetail(
                thread_id=thread_id,
                item_type=item_type,
                title=title,
                text=text,
                created_at=time.time(),
            )
        )
        details[thread_id] = items[-20:]
        self.save_details(details)

    def latest(self, thread_id: str) -> BotThreadDetail | None:
        items = self.load_details().get(thread_id) or []
        return items[-1] if items else None

    def retarget(self, old_thread_id: str, new_thread_id: str) -> None:
        details = self.load_details()
        old_items = details.pop(old_thread_id, [])
        if not old_items:
            return
        moved = [
            item.model_copy(update={"thread_id": new_thread_id})
            if item.thread_id == old_thread_id
            else item
            for item in old_items
        ]
        details.setdefault(new_thread_id, [])
        details[new_thread_id] = (details[new_thread_id] + moved)[-20:]
        self.save_details(details)


def install_bot_detail_service(
    app: Any,
    host: Any,
    *,
    load_details=None,
    save_details=None,
) -> BotDetailService:
    existing = getattr(app.state, "bot_detail_service", None)
    if isinstance(existing, BotDetailService):
        service = existing
    else:
        repositories = getattr(
            app.state,
            "auxiliary_state_repositories",
            None,
        )
        service = BotDetailService(
            load_details=(
                load_details
                or (
                    repositories.bot_details.load
                    if repositories is not None
                    else host._load_bot_details
                )
            ),
            save_details=(
                save_details
                or (
                    repositories.bot_details.save
                    if repositories is not None
                    else host._save_bot_details
                )
            ),
        )
        app.state.bot_detail_service = service

    # Compatibility names remain until the terminal legacy cutover.
    host._record_bot_detail = service.record
    host._latest_bot_detail = service.latest
    host._retarget_bot_details = service.retarget
    return service
