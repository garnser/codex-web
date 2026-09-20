from __future__ import annotations

from typing import Any

from codex_web.security import security_boundary_instructions
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.services.thread_resume import ThreadResumeService
from codex_web.services.threads import ThreadService
from codex_web.services.turns import TurnService


class _Projects:
    def __init__(self, host: Any) -> None:
        self.host = host

    def get(self, project_id):
        return self.host._project(project_id)

    def find_by_cwd(self, cwd):
        return self.host._project_for_cwd(cwd)

    def params(self, project, overrides=None):
        return self.host._project_params(project, overrides)

    def sandbox_policy(self, mode, cwd):
        return self.host._sandbox_policy(mode, cwd)


class _Settings:
    def __init__(
        self,
        host: Any,
        canonical: ThreadExecutionSettingsService,
    ) -> None:
        self.host = host
        self.canonical = canonical

    def get(self, thread_id):
        return self.host._thread_run_settings(thread_id)

    def remember(self, thread_id, **kwargs):
        return self.host._remember_thread_run_settings(thread_id, **kwargs)

    def retarget(self, old_thread_id, new_thread_id):
        return self.canonical.retarget(old_thread_id, new_thread_id)

    def base_developer_instructions(self, thread_id, instructions):
        if not instructions or not instructions.strip():
            return None
        normalized = instructions.strip()
        contract = self.host._work_item_contract_instructions(thread_id)
        removable = [security_boundary_instructions()]
        if contract:
            removable.append(contract)
        for generated in removable:
            while generated in normalized:
                normalized = normalized.replace(generated, "").strip()
        while "\n\n\n" in normalized:
            normalized = normalized.replace("\n\n\n", "\n\n")
        return normalized.strip() or None

    def effective_developer_instructions(self, thread_id, instructions):
        contract = self.host._work_item_contract_instructions(thread_id)
        parts = [
            part.strip()
            for part in (
                instructions,
                security_boundary_instructions(),
                contract,
            )
            if part and part.strip()
        ]
        return "\n\n".join(parts) if parts else None


class _Bindings:
    def __init__(self, host: Any) -> None:
        self.host = host

    def for_thread(self, thread_id):
        return self.host._bindings_for_thread(thread_id)


class _ThreadIndex:
    def __init__(self, host: Any) -> None:
        self.host = host

    def load(self):
        return self.host._load_thread_index()

    def save(self, rows):
        return self.host._save_thread_index(rows)

    def upsert(self, row):
        return self.host._upsert_indexed_thread(row)

    def remove(self, thread_id):
        return self.host._remove_indexed_thread(thread_id)


class _Naming:
    def __init__(self, host: Any) -> None:
        self.host = host

    async def set_name(self, thread_id, name):
        return await self.host._set_thread_name(thread_id, name)


class _Resume:
    def __init__(
        self,
        host: Any,
        service: ThreadResumeService,
    ) -> None:
        self.host = host
        self.service = service

    def active_task(self, thread_id):
        return self.service.active_task(thread_id)

    def schedule(self, thread_id, project_id, params):
        return self.host._web_thread_resume_task(
            thread_id,
            project_id,
            params,
        )

    def handoff_timeout(self):
        return self.host._web_thread_resume_handoff_timeout()

    def retry_delay(self):
        return self.host._thread_resume_retry_delay()

    def is_timeout_error(self, exc):
        return self.host._is_codex_timeout_error(exc)

    def is_stale_thread_error(self, exc):
        return self.host._is_stale_thread_error(exc)

    def read_timeout_response(self, thread_id, limit, exc, *, event_type="web_read_timeout"):
        return self.host._thread_read_timeout_response(
            thread_id,
            limit,
            exc,
            event_type=event_type,
        )


class _Recovery:
    def __init__(self, host: Any) -> None:
        self.host = host

    def replacement_thread_id(self, thread_id):
        return self.host._replacement_thread_id(thread_id)

    def raise_if_thread_replaced(self, thread_id):
        return self.host._raise_if_thread_replaced(thread_id)

    def release_stale_active_turn(self, thread_id, source):
        return self.host._release_stale_active_turn(thread_id, source)

    async def replace_stale_bot_thread(self, binding, error):
        return await self.host._replace_stale_bot_thread(binding, error)

    async def replace_stale_web_thread(self, thread_id, project, error):
        return await self.host._replace_stale_web_thread(
            thread_id,
            project,
            error,
        )


class _QueuePolicy:
    def __init__(self, host: Any) -> None:
        self.host = host

    def queue(self, thread_id):
        return self.host._thread_queue(thread_id)

    def depth(self, thread_id):
        return self.host._thread_queue_depth(thread_id)

    def record_steer(self, thread_id):
        return self.host._record_thread_steer(thread_id)


class _Execution:
    def __init__(self, host: Any) -> None:
        self.host = host

    def enqueue_turn(self, **kwargs):
        return self.host._enqueue_turn(**kwargs)

    async def publish_queue_status(self, thread_id):
        return await self.host._publish_queue_status(thread_id)

    def wait_for_thread_capacity(self, **kwargs):
        return self.host._wait_for_thread_capacity(**kwargs)

    def thread_is_active(self, thread_id):
        return self.host._thread_is_active(thread_id)

    async def start_thread_turn_now(self, thread_id, **kwargs):
        return await self.host._start_thread_turn_now(thread_id, **kwargs)

    def schedule_queue_drain(self, thread_id):
        return self.host._schedule_queue_drain(thread_id)

    def pop_latest_queued_turn(self, thread_id):
        return self.host._pop_latest_queued_turn(thread_id)

    def pop_queued_turn(self, thread_id, queued_id):
        return self.host._pop_queued_turn(thread_id, queued_id)

    def requeue_turn_front(self, queued):
        return self.host._requeue_turn_front(queued)

    def clear_thread_active(self, thread_id):
        return self.host._clear_thread_active(thread_id)

    async def request_for_thread(self, thread_id, method, params=None):
        return await self.host._codex_request_for_thread(
            thread_id,
            method,
            params or {},
        )


async def _legacy_runtime_request(host: Any, method: str, params: dict[str, Any]):
    return await host.codex.request(method, params)


def install_thread_compatibility_facade(
    app: Any,
    host: Any,
    *,
    canonical_settings: ThreadExecutionSettingsService,
) -> tuple[ThreadService, TurnService, ThreadRecoveryService]:
    """Preserve direct import-server behavior without coupling production routes.

    The application routers use their explicitly composed services. This facade
    exists only for the historical module-level helper surface and deliberately
    resolves those helper attributes at call time so supported monkeypatch/test
    callers keep the same semantics during legacy extraction.
    """

    projects = _Projects(host)
    settings = _Settings(host, canonical_settings)
    bindings = _Bindings(host)
    index = _ThreadIndex(host)

    async def runtime_request(method, params):
        return await _legacy_runtime_request(host, method, params)

    compat_resume_service = ThreadResumeService(
        runtime_request,
        index,
        bindings,
        event_sink=lambda payload: host._append_bot_event(payload),
        truncate_text=lambda value, limit: host._truncate_text(value, limit),
    )

    # Dynamic wrappers below consult these helper names, so publish the
    # compatibility resume implementation before composing the wrappers.
    host._is_codex_timeout_error = compat_resume_service.is_timeout_error
    host._is_stale_thread_error = compat_resume_service.is_stale_thread_error
    host._thread_read_timeout_response = compat_resume_service.read_timeout_response
    host._web_thread_resume_handoff_timeout = compat_resume_service.handoff_timeout
    host._thread_resume_retry_delay = compat_resume_service.retry_delay
    host._web_thread_resume_task = compat_resume_service.schedule
    host.WEB_THREAD_RESUME_TASKS = compat_resume_service.tasks

    resume = _Resume(host, compat_resume_service)
    naming = _Naming(host)
    compat_recovery_service = ThreadRecoveryService(
        host,
        projects=projects,
        settings=settings,
        naming=naming,
        thread_index=index,
        runtime_request=runtime_request,
        event_sink=lambda payload: host._append_bot_event(payload),
        truncate_text=lambda value, limit: host._truncate_text(value, limit),
    )

    # The compatibility host keeps the old names, but every implementation is
    # the extracted recovery service.
    host._logical_binding_name = compat_recovery_service.logical_binding_name
    host._same_logical_binding = compat_recovery_service.same_logical_binding
    host._preferred_binding_for_replacement = (
        compat_recovery_service.preferred_binding_for_replacement
    )
    host._retarget_logical_bot_bindings = (
        compat_recovery_service.retarget_logical_bot_bindings
    )
    host._retarget_thread_settings = compat_recovery_service.retarget_thread_settings
    host._retarget_active_turn = compat_recovery_service.retarget_active_turn
    host._retarget_turn_queue = compat_recovery_service.retarget_turn_queue
    host._retarget_slack_thread_icon = compat_recovery_service.retarget_slack_thread_icon
    host._retarget_bot_thread_state = compat_recovery_service.retarget_bot_thread_state
    host._archive_replaced_bot_thread = compat_recovery_service.archive_replaced_bot_thread
    host._replace_stale_bot_thread = compat_recovery_service.replace_stale_bot_thread
    host._replace_stale_web_thread = compat_recovery_service.replace_stale_web_thread
    host._active_turn_stale_seconds = compat_recovery_service.active_turn_stale_seconds
    host._active_turn_is_stale = compat_recovery_service.active_turn_is_stale
    host._release_stale_active_turn = compat_recovery_service.release_stale_active_turn
    host._replacement_thread_id = compat_recovery_service.replacement_thread_id
    host._raise_if_thread_replaced = compat_recovery_service.raise_if_thread_replaced

    recovery = _Recovery(host)
    compat_thread_service = ThreadService(
        runtime_transport=host.codex,
        runtime_request_for_thread=lambda thread_id, method, params=None: host.codex.request(
            method,
            params or {},
        ),
        event_sink=lambda payload: host._append_bot_event(payload),
        project_runtime=projects,
        settings=settings,
        recovery=recovery,
        resume_runtime=resume,
        thread_index=index,
    )
    compat_turn_service = TurnService(
        projects=projects,
        settings=settings,
        recovery=recovery,
        resume_runtime=resume,
        bindings=bindings,
        queue_policy=_QueuePolicy(host),
        execution=_Execution(host),
        event_sink=lambda payload: host._append_bot_event(payload),
        truncate_text=lambda value, limit: host._truncate_text(value, limit),
        binding_public=lambda binding: host._binding_public(binding),
    )

    # Dynamic instruction composition is part of the direct module facade.
    host._base_developer_instructions = settings.base_developer_instructions
    host._effective_developer_instructions = settings.effective_developer_instructions

    host.read_thread = compat_thread_service.read
    host.resume_thread = compat_turn_service.resume
    host.start_turn = compat_turn_service.start

    app.state.thread_compatibility_service = compat_thread_service
    app.state.turn_compatibility_service = compat_turn_service
    app.state.thread_recovery_compatibility_service = compat_recovery_service
    return compat_thread_service, compat_turn_service, compat_recovery_service
