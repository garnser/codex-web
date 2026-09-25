from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from codex_web.models import BotBinding, ThreadRunSettings
from codex_web.security import security_boundary_instructions
from codex_web.services.bot_binding_selection import BotBindingSelectionService


class ThreadExecutionSettingsService:
    """Own thread run settings and developer-instruction contract composition."""

    def __init__(
        self,
        *,
        load_settings: Callable[[], dict[str, ThreadRunSettings]],
        save_settings: Callable[[dict[str, ThreadRunSettings]], None],
        bindings: BotBindingSelectionService,
        load_bindings: Callable[[], list[BotBinding]],
        save_bindings: Callable[[list[BotBinding]], None],
        gitlab_routing_enabled_for_project: Callable[[str], bool],
        binding_report_name: Callable[[BotBinding], str | None],
        binding_prefix: Callable[[BotBinding], str | None],
        get_setting: Callable[[str], ThreadRunSettings | None] | None = None,
        put_setting: Callable[[str, ThreadRunSettings], Any] | None = None,
        delete_setting: Callable[[str], bool] | None = None,
    ) -> None:
        self.load_settings = load_settings
        self.save_settings = save_settings
        self.bindings = bindings
        self.load_bindings = load_bindings
        self.save_bindings = save_bindings
        self.gitlab_routing_enabled_for_project = gitlab_routing_enabled_for_project
        self.binding_report_name = binding_report_name
        self.binding_prefix = binding_prefix
        self.get_setting = get_setting
        self.put_setting = put_setting
        self.delete_setting = delete_setting

    def remember(
        self,
        thread_id: str,
        *,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        developer_instructions: str | None = None,
        repository_resource_id: str | None = None,
        writable_repository_resource_ids: tuple[str, ...] | None = None,
        read_only_repository_resource_ids: tuple[str, ...] | None = None,
        execution_profile_id: str | None = None,
    ) -> ThreadRunSettings:
        all_settings = None
        current = (
            self.get_setting(thread_id)
            if self.get_setting is not None
            else None
        ) or ThreadRunSettings()
        if sandbox is not None:
            current.sandbox = sandbox
        if approval_policy is not None:
            current.approval_policy = approval_policy
        if model is not None:
            current.model = model or None
        if reasoning_effort is not None:
            current.reasoning_effort = reasoning_effort or None
        if developer_instructions is not None:
            current.developer_instructions = self.base_developer_instructions(
                thread_id,
                developer_instructions,
            )
        if repository_resource_id is not None:
            current.repository_resource_id = repository_resource_id or None
        if writable_repository_resource_ids is not None:
            current.writable_repository_resource_ids = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in writable_repository_resource_ids
                    if value and value.strip()
                )
            )
        if read_only_repository_resource_ids is not None:
            current.read_only_repository_resource_ids = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in read_only_repository_resource_ids
                    if value and value.strip()
                )
            )
        if execution_profile_id is not None:
            current.execution_profile_id = execution_profile_id or None
        if self.put_setting is not None:
            self.put_setting(thread_id, current)
        else:
            all_settings = all_settings or self.load_settings()
            all_settings[thread_id] = current
            self.save_settings(all_settings)
        self.sync_bot_binding_settings(thread_id, current)
        return current

    def all(self) -> dict[str, ThreadRunSettings]:
        return dict(self.load_settings())

    def retarget(self, old_thread_id: str, new_thread_id: str) -> None:
        if (
            self.get_setting is not None
            and self.put_setting is not None
            and self.delete_setting is not None
        ):
            old_settings = self.get_setting(old_thread_id)
            if old_settings is None:
                return
            if self.get_setting(new_thread_id) is None:
                self.put_setting(new_thread_id, old_settings)
            self.delete_setting(old_thread_id)
            return
        all_settings = self.load_settings()
        old_settings = all_settings.pop(old_thread_id, None)
        if old_settings is None:
            return
        if new_thread_id not in all_settings:
            all_settings[new_thread_id] = old_settings
        self.save_settings(all_settings)

    def get(self, thread_id: str | None) -> ThreadRunSettings:
        if not thread_id:
            return ThreadRunSettings()
        settings = (
            self.get_setting(thread_id)
            if self.get_setting is not None
            else self.load_settings().get(thread_id)
        )
        if settings:
            return settings
        bindings = self.bindings.for_thread(thread_id)
        if bindings:
            return ThreadRunSettings(
                sandbox=bindings[0].sandbox,
                approval_policy=bindings[0].approval_policy,
            )
        return ThreadRunSettings()

    @staticmethod
    def internal_base_url() -> str:
        override = (os.environ.get("CODEX_WEB_INTERNAL_BASE_URL") or "").strip()
        if override:
            return override.rstrip("/")
        port = int(os.environ.get("CODEX_WEB_PORT", "8765"))
        return f"http://127.0.0.1:{port}"

    def work_item_contract_binding(self, thread_id: str | None) -> BotBinding | None:
        if not thread_id:
            return None
        bindings = self.bindings.for_thread(thread_id)
        return max(bindings, key=lambda item: item.updated_at) if bindings else None

    def work_item_contract_instructions(self, thread_id: str | None) -> str | None:
        binding = self.work_item_contract_binding(thread_id)
        if not binding or not self.gitlab_routing_enabled_for_project(binding.project_id):
            return None
        role = (
            self.binding_report_name(binding)
            or self.binding_prefix(binding)
            or "Agent"
        ).strip()
        role_key = role.lower()
        base_url = self.internal_base_url()
        lines = [
            "codex-web structured work-item contract. These rules are mandatory for GitLab-driven work.",
            f"Use `{base_url}/api/work-items` as the system of record for ownership, handoff, and progress.",
            "Before calling a work-item endpoint, URL-encode the full GitLab ref path with `urllib.parse.quote(ref, safe='')`.",
            "Do not rely on Slack narration alone. Every meaningful GitLab work step must also update codex-web state.",
            "",
            "Required endpoint usage:",
            "- POST `/api/work-items/{ref}/progress` after every meaningful step, blocker change, owner change, or next-action change.",
            "- POST `/api/work-items/{ref}/handoff` immediately when you push work to another named agent.",
            "- POST `/api/work-items/{ref}/ack` immediately when you accept or reject a handoff addressed to you.",
            "",
            "Progress payload minimums:",
            f"- `actor`: `{role}`",
            "- `current_owner`: the agent currently responsible",
            "- `current_stage`: one of `implementation_active`, `ready_for_validation`, `validation_running`, `failed_with_action_owner`, `ready_to_close`, `closed`",
            "- `next_action`: one exact next action",
            "- `next_owner`: set this whenever the next owner differs from the current owner",
            "- `blocker`: one exact blocker if work is blocked, otherwise omit or clear it",
            "- `blocking_findings`: optional list of additional concrete defects or follow-up findings that support the single canonical blocker",
            "",
            "Handoff rules:",
            "- A handoff is not complete until the sender records `/handoff` and the recipient records `/ack`.",
            "- If you hand work to release/validation, update the stage accordingly and set the exact expected action.",
            "- If you receive a handoff, acknowledge it in the same turn before doing deeper work.",
            "",
            "Loop discipline:",
            "- Never stop at a status summary. Either keep working, hand off explicitly, or record one exact blocker with the next owner.",
            "- If GitLab labels or status changed, reconcile the work-item state in codex-web before ending the turn.",
        ]
        if binding.is_master or role_key in {"orchestrator", "codex"}:
            lines.extend(
                [
                    "",
                    "Orchestrator-specific rules:",
                    "- For every open GitLab work item you touch, ensure there is always a current owner, an exact next action, and a follow-up path until the item is closed.",
                    "- When an owner stalls, issue a direct follow-up to the named agent thread and record the reassignment or escalation through `/progress` or `/handoff` in the same turn.",
                    "- If a handoff expires or validation stalls, do not just restate the blocker. Push the next owner and update the structured state so the watchdog loop can continue.",
                ]
            )
        return "\n".join(lines)

    def effective_developer_instructions(self, thread_id: str | None, instructions: str | None) -> str | None:
        contract = self.work_item_contract_instructions(thread_id)
        trust_boundary = security_boundary_instructions()
        parts = [
            part.strip()
            for part in (instructions, trust_boundary, contract)
            if part and part.strip()
        ]
        if not parts:
            return None
        return "\n\n".join(parts)

    def base_developer_instructions(self, thread_id: str | None, instructions: str | None) -> str | None:
        if not instructions or not instructions.strip():
            return None
        normalized = instructions.strip()
        contract = self.work_item_contract_instructions(thread_id)
        removable = [security_boundary_instructions()]
        if contract:
            removable.append(contract)
        for generated in removable:
            while generated in normalized:
                normalized = normalized.replace(generated, "").strip()
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return normalized or None

    def sync_bot_binding_settings(self, thread_id: str, settings: ThreadRunSettings) -> None:
        bindings = self.load_bindings()
        changed = False
        for binding in bindings:
            if binding.thread_id != thread_id:
                continue
            binding_changed = False
            if settings.sandbox is not None and binding.sandbox != settings.sandbox:
                binding.sandbox = settings.sandbox
                binding_changed = True
            if settings.approval_policy is not None and binding.approval_policy != settings.approval_policy:
                binding.approval_policy = settings.approval_policy
                binding_changed = True
            if binding_changed:
                binding.updated_at = time.time()
                changed = True
        if changed:
            self.save_bindings(bindings)


def install_thread_execution_settings_service(
    app: Any,
    host: Any,
    *,
    load_settings: Callable[[], dict[str, ThreadRunSettings]] | None = None,
    save_settings: Callable[[dict[str, ThreadRunSettings]], None] | None = None,
    bindings: BotBindingSelectionService | None = None,
    load_bindings: Callable[[], list[BotBinding]] | None = None,
    save_bindings: Callable[[list[BotBinding]], None] | None = None,
    gitlab_routing_enabled_for_project: Callable[[str], bool] | None = None,
    binding_report_name: Callable[[BotBinding], str | None] | None = None,
    binding_prefix: Callable[[BotBinding], str | None] | None = None,
    get_setting: Callable[[str], ThreadRunSettings | None] | None = None,
    put_setting: Callable[[str, ThreadRunSettings], Any] | None = None,
    delete_setting: Callable[[str], bool] | None = None,
) -> ThreadExecutionSettingsService:
    service = ThreadExecutionSettingsService(
        load_settings=load_settings or host._load_thread_settings,
        save_settings=save_settings or host._save_thread_settings,
        bindings=bindings or app.state.bot_binding_selection_service,
        load_bindings=load_bindings or host._load_bot_bindings,
        save_bindings=save_bindings or host._save_bot_bindings,
        gitlab_routing_enabled_for_project=(
            gitlab_routing_enabled_for_project
            or host._gitlab_routing_enabled_for_project
        ),
        binding_report_name=(
            binding_report_name or host._binding_report_name
        ),
        binding_prefix=binding_prefix or host._binding_prefix,
        get_setting=(
            get_setting
            or getattr(host, "_get_thread_setting_record", None)
        ),
        put_setting=(
            put_setting
            or getattr(host, "_put_thread_setting_record", None)
        ),
        delete_setting=(
            delete_setting
            or getattr(host, "_delete_thread_setting_record", None)
        ),
    )
    app.state.thread_execution_settings_service = service

    # Compatibility aliases for direct import-server consumers.
    host._remember_thread_run_settings = service.remember
    host._thread_run_settings_all = service.all
    host._retarget_thread_settings = service.retarget
    host._thread_run_settings = service.get
    host._codex_web_internal_base_url = service.internal_base_url
    host._work_item_contract_binding = service.work_item_contract_binding
    host._work_item_contract_instructions = service.work_item_contract_instructions
    host._effective_developer_instructions = service.effective_developer_instructions
    host._base_developer_instructions = service.base_developer_instructions
    host._sync_bot_binding_settings = service.sync_bot_binding_settings
    return service
