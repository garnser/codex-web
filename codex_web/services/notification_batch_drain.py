"""Event-driven batch selection inside the existing owned thread queue drain."""

import asyncio
import uuid
from copy import deepcopy

from codex_web.notification_batches import select_notification_batch
from codex_web.notification_provenance import verified_notification_ids


class NotificationBatchDrain:
    def __init__(
        self,
        *,
        coordinator,
        event_lookup,
        policy_for,
        authority_for=None,
        control_for=None,
    ):
        self.coordinator = coordinator
        self.control_for = control_for
        self.event_lookup, self.policy_for, self.authority_for = (
            event_lookup,
            policy_for,
            authority_for,
        )

    async def try_drain(self, runtime, anchor, project):
        if (
            anchor.provider_notification_pin is None
            or runtime.ownership is None
            or runtime.actor_resolver is None
        ):
            return False
        actor = runtime.actor_resolver(anchor.agent_profile_actor_id, project)
        policy = deepcopy(await asyncio.to_thread(self.policy_for, actor, project))
        if not policy or not policy.get("enabled"):
            return False
        fence = runtime.ownership.require_fence("thread-queue:" + anchor.thread_id)
        queued = self.coordinator.queue.get(anchor.thread_id)
        anchor_index = next(
            (index for index, item in enumerate(queued) if item.id == anchor.id), None
        )
        if anchor_index is None:
            return False
        # Bound provenance and authority reads as well as the native context.
        # Entries outside this window remain in the original durable queue.
        items = queued[anchor_index : anchor_index + 256]
        verified = await asyncio.to_thread(
            verified_notification_ids,
            items,
            event_lookup=self.event_lookup,
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
        )
        if anchor.id not in verified:
            return False
        attempts, scopes, controls = {}, {}, {}
        for item in items:
            if item.id not in verified or item.project_id != project.id:
                continue
            control = (
                await asyncio.to_thread(self.control_for, item, project)
                if self.control_for
                else None
            )
            scope = (
                control.fingerprint()
                if control is not None
                else (
                    await asyncio.to_thread(self.authority_for, item, project)
                    if self.authority_for
                    else None
                )
            )
            if not scope:
                continue
            if control is not None:
                controls[item.id] = control
            scopes[item.id] = scope
            attempts[item.id] = await asyncio.to_thread(
                runtime.queued_preflight.retain_queued,
                item,
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
            )
        control = controls.get(anchor.id)
        plan = select_notification_batch(
            items,
            anchor_id=anchor.id,
            attempts_by_original_id=attempts,
            verified_notification_ids=verified,
            authority_scope_sha256_by_original_id=scopes,
            organization_id=project.organization_id,
            workspace_id=project.workspace_id,
            notification_source_origin=anchor.provider_notification_pin.source_origin,
            max_members=control.max_members if control else policy["max_members"],
            max_context_bytes=control.max_context_bytes
            if control
            else policy["max_context_bytes"],
            control_snapshot=control,
        )
        if plan is None:
            return False

        def current(allow_active=False):
            observed_fence = runtime.ownership.require_fence(
                "thread-queue:" + anchor.thread_id
            )
            if observed_fence.fencing_token != fence.fencing_token:
                return False
            if (
                not allow_active and runtime.thread_is_active(anchor.thread_id)
            ) or self.policy_for(actor, project) != policy:
                return False
            fresh = verified_notification_ids(
                plan.members,
                event_lookup=self.event_lookup,
                organization_id=project.organization_id,
                workspace_id=project.workspace_id,
            )

            def current_scope(item):
                if self.control_for is not None:
                    current_control = self.control_for(item, project)
                    return (
                        current_control.fingerprint()
                        if current_control is not None
                        else None
                    )
                return self.authority_for(item, project)

            return all(
                item.id in fresh
                and runtime.automation_wait_reason(item.source) is None
                and current_scope(item) == plan.authority_scope_sha256
                for item in plan.members
            )

        admission = None
        claim_id = "notification-claim-" + uuid.uuid4().hex
        try:
            admission = await asyncio.to_thread(
                self.coordinator.prepare, plan, claim_id=claim_id, current=current
            )
            await self.coordinator.submit_native(
                admission,
                runtime=runtime,
                project=project,
                actor=actor,
                current=current,
                native_current=lambda: current(allow_active=True),
            )
        except Exception as exc:
            if admission is None:
                # Claims may have committed before a guarded queue removal
                # failed. Never fall through into a second individual admission.
                retained = self.coordinator.preflight.get(
                    "preflight-queued-" + anchor.id
                )
                if retained is None or retained.notification_batch_id is None:
                    raise
                admission = self.coordinator.preflight.get_notification_batch(
                    retained.notification_batch_id
                )
                if admission is None or admission.claim_id != claim_id:
                    # A competing owner claimed these originals. Its claim
                    # cannot be released by this invocation.
                    return True
            observed = self.coordinator.preflight.get_notification_batch(admission.id)
            if observed.phase in {"prepared", "prestart_refused"}:
                try:
                    await asyncio.to_thread(
                        self.coordinator.recover_prepared,
                        observed.id,
                        claim_id=observed.claim_id,
                        current=current,
                    )
                except (ValueError, RuntimeError):
                    pass
            runtime.host._append_bot_event(
                {
                    "type": "notification_batch_admission_held",
                    "thread_id": anchor.thread_id,
                    "batch_id": admission.id,
                    "original_count": len(plan.members),
                    "error_type": type(exc).__name__,
                }
            )
            return True
        runtime.host._append_bot_event(
            {
                "type": "notification_batch_input_admitted",
                "thread_id": anchor.thread_id,
                "batch_id": admission.id,
                "original_count": len(plan.members),
                "per_original_completion": "unresolved",
            }
        )
        return True
