from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MethodType
from typing import Any, Callable

from codex_web.models import BotBinding, BotInboundMessage, BotReplyTarget


@dataclass
class BotRoutingContext:
    active_turns: dict[str, Any] = field(default_factory=dict)
    active_loaded: set[str] = field(default_factory=set)
    reply_targets: dict[str, BotReplyTarget | None] = field(
        default_factory=dict
    )
    delivery_targets: dict[str, BotReplyTarget | None] = field(
        default_factory=dict
    )
    full_active_turns: dict[str, Any] | None = None
    full_reply_targets: dict[str, BotReplyTarget] | None = None
    full_delivery_targets: dict[str, BotReplyTarget] | None = None


class BotTargetService:
    """Own reply/delivery target selection over explicit state collaborators."""

    def __init__(
        self,
        *,
        load_reply_targets: Callable[[], dict[str, BotReplyTarget]],
        save_reply_targets: Callable[[dict[str, BotReplyTarget]], None],
        load_delivery_targets: Callable[[], dict[str, BotReplyTarget]],
        save_delivery_targets: Callable[[dict[str, BotReplyTarget]], None],
        load_active_turns: Callable[[], dict[str, Any]],
        bindings_for_project: Callable[[str, str], list[BotBinding]],
        put_reply_target: Callable[[str, BotReplyTarget], Any] | None = None,
        put_delivery_target: Callable[[str, BotReplyTarget], Any] | None = None,
        get_reply_target: Callable[[str], BotReplyTarget | None] | None = None,
        get_delivery_target: Callable[[str], BotReplyTarget | None] | None = None,
        get_active_turn: Callable[[str], Any | None] | None = None,
        page_reply_targets: Callable[..., tuple[dict[str, BotReplyTarget], str | None]] | None = None,
        page_delivery_targets: Callable[..., tuple[dict[str, BotReplyTarget], str | None]] | None = None,
    ) -> None:
        self.load_reply_targets = load_reply_targets
        self.save_reply_targets = save_reply_targets
        self.load_delivery_targets = load_delivery_targets
        self.save_delivery_targets = save_delivery_targets
        self.load_active_turns = load_active_turns
        self.bindings_for_project = bindings_for_project
        self.put_reply_target = put_reply_target
        self.put_delivery_target = put_delivery_target
        self.get_reply_target = get_reply_target
        self.get_delivery_target = get_delivery_target
        self.get_active_turn = get_active_turn
        self.page_reply_targets = page_reply_targets
        self.page_delivery_targets = page_delivery_targets
        self._routing_metrics = {
            "replyKeyReads": 0,
            "deliveryKeyReads": 0,
            "activeTurnReads": 0,
            "compatibilityRepairScans": 0,
            "compatibilityRepairHits": 0,
        }

    def routing_context(self) -> BotRoutingContext:
        return BotRoutingContext()

    def metrics(self) -> dict[str, int]:
        return dict(self._routing_metrics)

    def _active_turn(
        self,
        thread_id: str,
        context: BotRoutingContext,
    ) -> Any | None:
        if thread_id in context.active_loaded:
            return context.active_turns.get(thread_id)
        if self.get_active_turn is not None:
            self._routing_metrics["activeTurnReads"] += 1
            active = self.get_active_turn(thread_id)
        else:
            if context.full_active_turns is None:
                context.full_active_turns = self.load_active_turns()
            active = context.full_active_turns.get(thread_id)
        context.active_loaded.add(thread_id)
        if active is not None:
            context.active_turns[thread_id] = active
        return active

    def _reply_target(
        self,
        key: str,
        context: BotRoutingContext,
    ) -> BotReplyTarget | None:
        if key in context.reply_targets:
            return context.reply_targets[key]
        if self.get_reply_target is not None:
            self._routing_metrics["replyKeyReads"] += 1
            target = self.get_reply_target(key)
        else:
            if context.full_reply_targets is None:
                context.full_reply_targets = self.load_reply_targets()
            target = context.full_reply_targets.get(key)
        context.reply_targets[key] = target
        return target

    def _delivery_target(
        self,
        key: str,
        context: BotRoutingContext,
    ) -> BotReplyTarget | None:
        if key in context.delivery_targets:
            return context.delivery_targets[key]
        if self.get_delivery_target is not None:
            self._routing_metrics["deliveryKeyReads"] += 1
            target = self.get_delivery_target(key)
        else:
            if context.full_delivery_targets is None:
                context.full_delivery_targets = self.load_delivery_targets()
            target = context.full_delivery_targets.get(key)
        context.delivery_targets[key] = target
        return target

    def _bounded_repair_lookup(
        self,
        *,
        provider: str,
        external_conversation_id: str,
        external_thread_id: str,
        context: BotRoutingContext,
    ) -> BotReplyTarget | None:
        prefix = f"{provider}:{external_conversation_id}:"
        for pager, putter, cache in (
            (
                self.page_reply_targets,
                self.put_reply_target,
                context.reply_targets,
            ),
            (
                self.page_delivery_targets,
                self.put_delivery_target,
                context.delivery_targets,
            ),
        ):
            if pager is None:
                continue
            self._routing_metrics["compatibilityRepairScans"] += 1
            page, _cursor = pager(
                key_prefix=prefix,
                limit=250,
            )
            for target in page.values():
                if (
                    target.provider == provider
                    and target.external_conversation_id
                    == external_conversation_id
                    and (
                        target.external_thread_id == external_thread_id
                        or target.message_id == external_thread_id
                    )
                ):
                    self._routing_metrics["compatibilityRepairHits"] += 1
                    alias = self.external_target_key(
                        provider,
                        external_conversation_id,
                        external_thread_id,
                    )
                    cache[alias] = target
                    if putter is not None:
                        putter(alias, target)
                    return target
        return None

    @staticmethod
    def reply_target_key(binding: BotBinding) -> str:
        return f"{binding.provider}:{binding.external_conversation_id}:{binding.thread_id}"

    @staticmethod
    def external_target_key(
        provider: str,
        external_conversation_id: str,
        external_id: str,
    ) -> str:
        return (
            f"{provider}:{external_conversation_id}:external:{external_id}"
        )

    @staticmethod
    def thread_target_key(binding: BotBinding) -> str:
        return (
            f"thread:{binding.thread_id}:"
            f"{binding.provider}:{binding.external_conversation_id}"
        )

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
        target = BotReplyTarget(
            thread_id=binding.thread_id,
            provider=binding.provider,
            external_conversation_id=binding.external_conversation_id,
            external_thread_id=message.external_thread_id,
            message_id=message.message_id,
            updated_at=time.time(),
        )
        keys = [
            self.reply_target_key(binding),
            self.thread_target_key(binding),
        ]
        keys.extend(
            self.external_target_key(
                binding.provider,
                binding.external_conversation_id,
                external_id,
            )
            for external_id in {
                message.external_thread_id,
                message.message_id,
            }
            if external_id
        )
        if self.put_reply_target is not None:
            for key in dict.fromkeys(keys):
                self.put_reply_target(key, target)
            return target
        targets = self.load_reply_targets()
        for key in dict.fromkeys(keys):
            targets[key] = target
        self.save_reply_targets(targets)
        return target

    def reply_target_for_binding(
        self,
        binding: BotBinding,
        context: BotRoutingContext | None = None,
    ) -> BotReplyTarget | None:
        context = context or self.routing_context()
        target = (
            self._reply_target(self.reply_target_key(binding), context)
            or self._reply_target(self.thread_target_key(binding), context)
            or self._reply_target(binding.thread_id, context)
        )
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def delivery_target_for_binding(
        self,
        binding: BotBinding,
        context: BotRoutingContext | None = None,
    ) -> BotReplyTarget | None:
        context = context or self.routing_context()
        target = (
            self._delivery_target(self.reply_target_key(binding), context)
            or self._delivery_target(
                self.thread_target_key(binding),
                context,
            )
            or self._delivery_target(binding.thread_id, context)
        )
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def should_reply_in_external_thread(
        self,
        binding: BotBinding,
        context: BotRoutingContext | None = None,
    ) -> bool:
        context = context or self.routing_context()
        active = self._active_turn(binding.thread_id, context)
        source = (active.source or "").lower() if active else ""
        target = (
            self.active_reply_target_for_binding(binding, context)
            or self.reply_target_for_binding(binding, context)
        )
        recent = bool(
            active
            and target
            and target.updated_at >= active.started_at - 30
        )
        return binding.post_in_thread or "slack" in source or recent

    def active_reply_target_for_binding(
        self,
        binding: BotBinding,
        context: BotRoutingContext | None = None,
    ) -> BotReplyTarget | None:
        context = context or self.routing_context()
        active = self._active_turn(binding.thread_id, context)
        target = active.reply_target if active else None
        if not target:
            return None
        if target.provider != binding.provider or target.external_conversation_id != binding.external_conversation_id:
            return None
        return target

    def active_reply_target_for_thread_provider(
        self,
        thread_id: str,
        provider: str,
        context: BotRoutingContext | None = None,
    ) -> BotReplyTarget | None:
        context = context or self.routing_context()
        active = self._active_turn(thread_id, context)
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
        context = self.routing_context()
        key = self.external_target_key(
            normalized_provider,
            external_conversation_id,
            external_thread_id,
        )
        direct = (
            self._reply_target(key, context)
            or self._delivery_target(key, context)
        )
        if direct is not None:
            return direct
        return self._bounded_repair_lookup(
            provider=normalized_provider,
            external_conversation_id=external_conversation_id,
            external_thread_id=external_thread_id,
            context=context,
        )

    def remember_delivery_target(self, binding: BotBinding, delivery: dict[str, Any]) -> None:
        response = delivery.get("providerResponse") or {}
        ts = response.get("ts")
        if not delivery.get("sent") or not ts:
            return
        target = BotReplyTarget(
            thread_id=binding.thread_id,
            provider=binding.provider,
            external_conversation_id=binding.external_conversation_id,
            external_thread_id=str(ts),
            message_id=str(ts),
            updated_at=time.time(),
        )
        keys = (
            self.reply_target_key(binding),
            self.thread_target_key(binding),
            self.external_target_key(
                binding.provider,
                binding.external_conversation_id,
                str(ts),
            ),
        )
        if self.put_delivery_target is not None:
            for key in keys:
                self.put_delivery_target(key, target)
            return
        targets = self.load_delivery_targets()
        for key in keys:
            targets[key] = target
        self.save_delivery_targets(targets)

    def master_reply_target_for_binding(
        self,
        binding: BotBinding,
        context: BotRoutingContext | None = None,
    ) -> BotReplyTarget | None:
        context = context or self.routing_context()
        if binding.is_master:
            return None
        candidates = [
            candidate
            for candidate in self.bindings_for_project(binding.provider, binding.project_id)
            if candidate.is_master and candidate.external_conversation_id == binding.external_conversation_id
        ]
        candidates.sort(key=lambda candidate: (candidate.updated_at, candidate.created_at), reverse=True)
        active_for = self.active_reply_target_for_binding
        reply_for = self.reply_target_for_binding
        delivery_for = self.delivery_target_for_binding
        for candidate in candidates:
            # Keep the host-level compatibility seam intact. Existing consumers
            # and tests can replace one target source without replacing the
            # entire target-selection service.
            target = (
                active_for(candidate, context)
                or reply_for(candidate, context)
                or delivery_for(candidate, context)
            )
            if target:
                return target
        return None

    def thread_target_for_outbound(
        self,
        binding: BotBinding,
        reply_in_thread: bool | None = None,
    ) -> tuple[BotReplyTarget | None, bool]:
        context = self.routing_context()
        active_for = self.active_reply_target_for_binding
        reply_for = self.reply_target_for_binding
        master_for = self.master_reply_target_for_binding
        delivery_for = self.delivery_target_for_binding

        active_target = active_for(binding, context)
        if active_target:
            return active_target, True if reply_in_thread is None else reply_in_thread

        own_target = reply_for(binding, context)
        if own_target:
            should_thread = self.should_reply_in_external_thread(binding, context) if reply_in_thread is None else reply_in_thread
            return own_target, should_thread

        master_target = master_for(binding, context)
        if master_target:
            return master_target, True if reply_in_thread is None else reply_in_thread

        delivery_target = delivery_for(binding, context)
        if delivery_target:
            should_thread = (
                self.should_reply_in_external_thread(binding, context)
                if reply_in_thread is None
                else reply_in_thread
            )
            if should_thread:
                return delivery_target, True

        return None, False if reply_in_thread is None else reply_in_thread

    def outbound_bindings_for_thread(self, thread_id: str, bindings: list[BotBinding]) -> list[BotBinding]:
        context = self.routing_context()
        active_provider_for = self.active_reply_target_for_thread_provider
        active_for = self.active_reply_target_for_binding
        reply_for = self.reply_target_for_binding

        def score(binding: BotBinding) -> tuple[int, float, int, float]:
            active_target = active_provider_for(
                binding.thread_id,
                binding.provider,
                context,
            )
            if active_target:
                target = active_for(binding, context)
                return (
                    2 if target else 0,
                    target.updated_at if target else 0,
                    0,
                    binding.updated_at,
                )
            target = self._reply_target(
                self.reply_target_key(binding),
                context,
            )
            target_score = (
                target.updated_at
                if (
                    target
                    and self.should_reply_in_external_thread(
                        binding,
                        context,
                    )
                )
                else 0
            )
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
            if (
                active_for(binding, context)
                or reply_for(binding, context)
            ):
                continue
            if binding.post_in_thread:
                continue
            ordered.append(binding)
            seen.add(key)
        return ordered

    def forget_reply_target(self, thread_id: str) -> None:
        for loader, saver in (
            (self.load_reply_targets, self.save_reply_targets),
            (self.load_delivery_targets, self.save_delivery_targets),
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

        self.save_reply_targets(rewrite(self.load_reply_targets()))
        self.save_delivery_targets(rewrite(self.load_delivery_targets()))


def install_bot_target_service(
    app: Any,
    host: Any,
    *,
    load_reply_targets: Callable[[], dict[str, BotReplyTarget]] | None = None,
    save_reply_targets: Callable[[dict[str, BotReplyTarget]], None] | None = None,
    load_delivery_targets: Callable[[], dict[str, BotReplyTarget]] | None = None,
    save_delivery_targets: Callable[[dict[str, BotReplyTarget]], None] | None = None,
    load_active_turns: Callable[[], dict[str, Any]] | None = None,
    bindings_for_project: Callable[[str, str], list[BotBinding]] | None = None,
    put_reply_target: Callable[[str, BotReplyTarget], Any] | None = None,
    put_delivery_target: Callable[[str, BotReplyTarget], Any] | None = None,
    get_reply_target: Callable[
        [str],
        BotReplyTarget | None,
    ] | None = None,
    get_delivery_target: Callable[
        [str],
        BotReplyTarget | None,
    ] | None = None,
    get_active_turn: Callable[[str], Any | None] | None = None,
    page_reply_targets: Callable[
        ...,
        tuple[dict[str, BotReplyTarget], str | None],
    ] | None = None,
    page_delivery_targets: Callable[
        ...,
        tuple[dict[str, BotReplyTarget], str | None],
    ] | None = None,
) -> BotTargetService:
    service = BotTargetService(
        load_reply_targets=load_reply_targets or host._load_bot_reply_targets,
        save_reply_targets=save_reply_targets or host._save_bot_reply_targets,
        load_delivery_targets=load_delivery_targets or host._load_bot_delivery_targets,
        save_delivery_targets=save_delivery_targets or host._save_bot_delivery_targets,
        load_active_turns=load_active_turns or host._load_active_turns,
        bindings_for_project=bindings_for_project or host._bindings_for_project,
        put_reply_target=(
            put_reply_target
            or getattr(host, "_put_bot_reply_target_record", None)
        ),
        put_delivery_target=(
            put_delivery_target
            or getattr(host, "_put_bot_delivery_target_record", None)
        ),
        get_reply_target=(
            get_reply_target
            or getattr(host, "_get_bot_reply_target_record", None)
        ),
        get_delivery_target=(
            get_delivery_target
            or getattr(host, "_get_bot_delivery_target_record", None)
        ),
        get_active_turn=(
            get_active_turn
            or getattr(host, "_get_active_turn_record", None)
        ),
        page_reply_targets=page_reply_targets,
        page_delivery_targets=page_delivery_targets,
    )
    app.state.bot_target_service = service

    host._reply_target_key = service.reply_target_key
    host._external_target_key = service.external_target_key
    host._thread_target_key = service.thread_target_key
    host._conversation_target_for_binding = service.conversation_target
    host._remember_bot_reply_target = service.remember_reply_target
    host._reply_target_for_binding = service.reply_target_for_binding
    host._delivery_target_for_binding = service.delivery_target_for_binding
    host._active_reply_target_for_binding = service.active_reply_target_for_binding
    host._active_reply_target_for_thread_provider = service.active_reply_target_for_thread_provider
    host._target_for_external_thread = service.target_for_external_thread
    host._remember_bot_delivery_target = service.remember_delivery_target
    def _compat_master_reply_target_for_binding(
        binding: BotBinding,
    ) -> BotReplyTarget | None:
        if binding.is_master:
            return None
        candidates = [
            candidate
            for candidate in host._bindings_for_project(
                binding.provider,
                binding.project_id,
            )
            if candidate.is_master
            and candidate.external_conversation_id
            == binding.external_conversation_id
        ]
        candidates.sort(
            key=lambda candidate: (
                candidate.updated_at,
                candidate.created_at,
            ),
            reverse=True,
        )
        for candidate in candidates:
            target = (
                host._active_reply_target_for_binding(candidate)
                or host._reply_target_for_binding(candidate)
                or host._delivery_target_for_binding(candidate)
            )
            if target:
                return target
        return None

    def _compat_thread_target_for_outbound(
        _service: BotTargetService,
        binding: BotBinding,
        reply_in_thread: bool | None = None,
    ) -> tuple[BotReplyTarget | None, bool]:
        active_target = host._active_reply_target_for_binding(binding)
        if active_target:
            return (
                active_target,
                True if reply_in_thread is None else reply_in_thread,
            )

        own_target = host._reply_target_for_binding(binding)
        if own_target:
            should_thread = (
                host._should_reply_in_external_thread(binding)
                if reply_in_thread is None
                else reply_in_thread
            )
            return own_target, should_thread

        master_target = host._master_reply_target_for_binding(binding)
        if master_target:
            return (
                master_target,
                True if reply_in_thread is None else reply_in_thread,
            )

        delivery_target = host._delivery_target_for_binding(binding)
        if delivery_target:
            should_thread = (
                host._should_reply_in_external_thread(binding)
                if reply_in_thread is None
                else reply_in_thread
            )
            if should_thread:
                return delivery_target, True

        return (
            None,
            False if reply_in_thread is None else reply_in_thread,
        )

    # Direct-import compatibility remains dynamic so supported callers that
    # replace one historical helper keep working through the compatibility facade.
    host._master_reply_target_for_binding = (
        _compat_master_reply_target_for_binding
    )
    host._should_reply_in_external_thread = (
        service.should_reply_in_external_thread
    )
    host._thread_target_for_outbound = MethodType(
        _compat_thread_target_for_outbound,
        service,
    )
    host._outbound_bindings_for_thread = service.outbound_bindings_for_thread
    host._forget_bot_reply_target = service.forget_reply_target
    host._retarget_bot_targets = service.retarget
    return service
