from __future__ import annotations

import time
from typing import Any

from codex_web.models import BotBinding, BotInboundMessage, BotReplyTarget


class BotTargetService:
    """Own reply/delivery target selection independently from runtime.core."""

    def __init__(self, host: Any) -> None:
        self.host = host

    @staticmethod
    def reply_target_key(binding: BotBinding) -> str:
        return f"{binding.provider}:{binding.external_conversation_id}:{binding.thread_id}"

    @staticmethod
    def external_target_key(provider: str, external_conversation_id: str, external_id: str) -> str:
        return f"{provider}:{external_conversation_id}:external:{external_id}"

    def conversation_target(self, binding: BotBinding) -> BotReplyTarget:
        return BotReplyTarget(
            thread_id=binding.thread_id,
            provider=binding.provider,
            external_conversation_id=binding.external_conversation_id,
            external_thread_id=None,
            message_id=None,
            updated_at=time.time(),
        )

    def remember_reply_target(self, binding: BotBinding, message: BotInboundMessage) -> BotReplyTarget | None:
        if not message.external_thread_id and not message.message_id:
            return None
        h = self.host
        targets = h._load_bot_reply_targets()
        target = BotReplyTarget(
            thread_id=binding.thread_id,
            provider=binding.provider,
            external_conversation_id=binding.external_conversation_id,
            external_thread_id=message.external_thread_id,
            message_id=message.message_id,
            updated_at=time.time(),
        )
        targets[self.reply_target_key(binding)] = target
        for external_id in {message.external_thread_id, message.message_id}:
            if external_id:
                targets[self.external_target_key(binding.provider, binding.external_conversation_id, external_id)] = target
        h._save_bot_reply_targets(targets)
        return target

    def reply_target_for_binding(self, binding: BotBinding) -> BotReplyTarget | None:
        targets = self.host._load_bot_reply_targets()
        target = targets.get(self.reply_target_key(binding)) or targets.get(binding.thread_id)
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def delivery_target_for_binding(self, binding: BotBinding) -> BotReplyTarget | None:
        targets = self.host._load_bot_delivery_targets()
        target = targets.get(self.reply_target_key(binding)) or targets.get(binding.thread_id)
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def active_reply_target_for_binding(self, binding: BotBinding) -> BotReplyTarget | None:
        active = self.host._load_active_turns().get(binding.thread_id)
        target = active.reply_target if active else None
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def active_reply_target_for_thread_provider(self, thread_id: str, provider: str) -> BotReplyTarget | None:
        active = self.host._load_active_turns().get(thread_id)
        target = active.reply_target if active else None
        if target and target.provider == provider:
            return target
        return None

    def target_for_external_thread(
        self,
        provider: str,
        external_conversation_id: str,
        external_thread_id: str | None,
    ) -> BotReplyTarget | None:
        if not external_thread_id:
            return None
        normalized_provider = provider.lower()
        for targets in (
            self.host._load_bot_reply_targets(),
            self.host._load_bot_delivery_targets(),
        ):
            direct = targets.get(
                self.external_target_key(normalized_provider, external_conversation_id, external_thread_id)
            )
            if direct:
                return direct
            for target in targets.values():
                if target.provider != normalized_provider or target.external_conversation_id != external_conversation_id:
                    continue
                if target.external_thread_id == external_thread_id or target.message_id == external_thread_id:
                    return target
        return None

    def remember_delivery_target(self, binding: BotBinding, delivery: dict[str, Any]) -> None:
        response = delivery.get("providerResponse") or {}
        ts = response.get("ts")
        if not delivery.get("sent") or not ts:
            return
        targets = self.host._load_bot_delivery_targets()
        target = BotReplyTarget(
            thread_id=binding.thread_id,
            provider=binding.provider,
            external_conversation_id=binding.external_conversation_id,
            external_thread_id=str(ts),
            message_id=str(ts),
            updated_at=time.time(),
        )
        targets[self.reply_target_key(binding)] = target
        targets[self.external_target_key(binding.provider, binding.external_conversation_id, str(ts))] = target
        self.host._save_bot_delivery_targets(targets)

    def master_reply_target_for_binding(self, binding: BotBinding) -> BotReplyTarget | None:
        if binding.is_master:
            return None
        h = self.host
        candidates = [
            candidate
            for candidate in h._bindings_for_project(binding.provider, binding.project_id)
            if candidate.is_master and candidate.external_conversation_id == binding.external_conversation_id
        ]
        candidates.sort(key=lambda candidate: (candidate.updated_at, candidate.created_at), reverse=True)
        for candidate in candidates:
            # Keep the host-level compatibility seam intact. Existing consumers
            # and tests can replace one target source without replacing the
            # entire target-selection service.
            target = (
                h._active_reply_target_for_binding(candidate)
                or h._reply_target_for_binding(candidate)
                or h._delivery_target_for_binding(candidate)
            )
            if target:
                return target
        return None

    def thread_target_for_outbound(
        self,
        binding: BotBinding,
        reply_in_thread: bool | None = None,
    ) -> tuple[BotReplyTarget | None, bool]:
        h = self.host
        active_target = h._active_reply_target_for_binding(binding)
        if active_target:
            return active_target, True if reply_in_thread is None else reply_in_thread

        own_target = h._reply_target_for_binding(binding)
        if own_target:
            should_thread = h._should_reply_in_external_thread(binding) if reply_in_thread is None else reply_in_thread
            return own_target, should_thread

        master_target = h._master_reply_target_for_binding(binding)
        if master_target:
            return master_target, True if reply_in_thread is None else reply_in_thread

        delivery_target = h._delivery_target_for_binding(binding)
        if delivery_target:
            should_thread = h._should_reply_in_external_thread(binding) if reply_in_thread is None else reply_in_thread
            if should_thread:
                return delivery_target, True

        return None, False if reply_in_thread is None else reply_in_thread

    def outbound_bindings_for_thread(self, thread_id: str, bindings: list[BotBinding]) -> list[BotBinding]:
        h = self.host
        targets = h._load_bot_reply_targets()

        def score(binding: BotBinding) -> tuple[int, float, int, float]:
            active_target = h._active_reply_target_for_thread_provider(binding.thread_id, binding.provider)
            if active_target:
                target = h._active_reply_target_for_binding(binding)
                return (
                    2 if target else 0,
                    target.updated_at if target else 0,
                    0,
                    binding.updated_at,
                )
            target = targets.get(self.reply_target_key(binding))
            target_score = target.updated_at if (target and h._should_reply_in_external_thread(binding)) else 0
            return (
                1 if target_score else 0,
                target_score,
                1 if binding.is_primary_channel else 0,
                binding.updated_at,
            )

        selected: dict[str, BotBinding] = {}
        for binding in bindings:
            current = selected.get(binding.provider)
            if current is None or score(binding) > score(current):
                selected[binding.provider] = binding

        ordered = list(selected.values())
        seen = {
            (binding.provider, binding.external_conversation_id, binding.thread_id)
            for binding in ordered
        }
        for binding in bindings:
            key = (binding.provider, binding.external_conversation_id, binding.thread_id)
            if key in seen or selected.get(binding.provider) is None:
                continue
            if h._active_reply_target_for_binding(binding) or h._reply_target_for_binding(binding):
                continue
            if binding.post_in_thread:
                continue
            ordered.append(binding)
            seen.add(key)
        return ordered

    def forget_reply_target(self, thread_id: str) -> None:
        for loader, saver in (
            (self.host._load_bot_reply_targets, self.host._save_bot_reply_targets),
            (self.host._load_bot_delivery_targets, self.host._save_bot_delivery_targets),
        ):
            targets = loader()
            removed = False
            for key, target in list(targets.items()):
                if key == thread_id or target.thread_id == thread_id:
                    targets.pop(key, None)
                    removed = True
            if removed:
                saver(targets)

    def retarget(self, old_thread_id: str, new_thread_id: str) -> None:
        def rewrite(targets: dict[str, BotReplyTarget]) -> dict[str, BotReplyTarget]:
            rewritten: dict[str, BotReplyTarget] = {}
            for key, target in targets.items():
                next_key = key
                if key == old_thread_id:
                    next_key = new_thread_id
                elif key.endswith(f":{old_thread_id}") and ":external:" not in key:
                    next_key = f"{key.rsplit(':', 1)[0]}:{new_thread_id}"
                if target.thread_id == old_thread_id:
                    target = target.model_copy(update={"thread_id": new_thread_id, "updated_at": time.time()})
                rewritten[next_key] = target
            return rewritten

        self.host._save_bot_reply_targets(rewrite(self.host._load_bot_reply_targets()))
        self.host._save_bot_delivery_targets(rewrite(self.host._load_bot_delivery_targets()))


def install_bot_target_service(app: Any, host: Any) -> BotTargetService:
    existing = getattr(app.state, "bot_target_service", None)
    if isinstance(existing, BotTargetService) and existing.host is host:
        service = existing
    else:
        service = BotTargetService(host)
        app.state.bot_target_service = service

    host._reply_target_key = service.reply_target_key
    host._external_target_key = service.external_target_key
    host._conversation_target_for_binding = service.conversation_target
    host._remember_bot_reply_target = service.remember_reply_target
    host._reply_target_for_binding = service.reply_target_for_binding
    host._delivery_target_for_binding = service.delivery_target_for_binding
    host._active_reply_target_for_binding = service.active_reply_target_for_binding
    host._active_reply_target_for_thread_provider = service.active_reply_target_for_thread_provider
    host._target_for_external_thread = service.target_for_external_thread
    host._remember_bot_delivery_target = service.remember_delivery_target
    host._master_reply_target_for_binding = service.master_reply_target_for_binding
    host._thread_target_for_outbound = service.thread_target_for_outbound
    host._outbound_bindings_for_thread = service.outbound_bindings_for_thread
    host._forget_bot_reply_target = service.forget_reply_target
    host._retarget_bot_targets = service.retarget
    return service
