from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from codex_web.execution_role_models import (
    ExecutionRoleCatalogDefinition,
    ExecutionRoleContract,
)


CatalogProvider = Callable[[], ExecutionRoleCatalogDefinition]
_catalog_provider: CatalogProvider | None = None


class ExecutionRoleCatalogUnavailable(RuntimeError):
    pass


def install_execution_role_catalog_provider(provider: CatalogProvider) -> None:
    global _catalog_provider
    _catalog_provider = provider


def current_execution_role_catalog() -> ExecutionRoleCatalogDefinition:
    if _catalog_provider is None:
        raise ExecutionRoleCatalogUnavailable(
            "execution-role catalog provider is not installed; "
            "compose the Definition Registry before execution-role resolution"
        )
    return _catalog_provider()


def _catalog(
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> ExecutionRoleCatalogDefinition:
    return catalog or current_execution_role_catalog()


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def execution_roles(
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> tuple[ExecutionRoleContract, ...]:
    return _catalog(catalog).roles


def execution_role(
    role_id: str | None,
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> ExecutionRoleContract | None:
    if not role_id:
        return None
    return _catalog(catalog).role_map.get(role_id.strip().lower())


def route_execution_role(
    task: str,
    executive_agent_id: str | None = None,
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> ExecutionRoleContract:
    resolved = _catalog(catalog)
    roles = resolved.role_map
    text = _normalized(task)

    # Explicit role naming always wins, including roles intentionally excluded
    # from automatic routing.
    for role in resolved.roles:
        handles = {
            f"@{role.id}",
            f"@{role.id.replace('-', '')}",
            role.name.lower(),
        }
        if any(handle and handle in text for handle in handles):
            return role

    default_id = resolved.executive_default_execution_role.get(
        executive_agent_id or "",
        "orchestrator",
    )
    scored: list[tuple[int, int, ExecutionRoleContract]] = []
    order = [role.id for role in resolved.roles]
    for role in resolved.roles:
        if not role.auto_select:
            continue
        score = 0
        for keyword in role.keywords:
            if keyword in text:
                score += 4 if " " in keyword else 2
        if role.id == default_id:
            score += 1
        scored.append((score, -order.index(role.id), role))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]
    return roles.get(default_id) or roles["orchestrator"]


def execution_role_catalog_prompt(
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> str:
    resolved = _catalog(catalog)
    lines = ["Operational execution roles available after an executive decision:"]
    for role in resolved.roles:
        suffix = " (explicit selection only)" if not role.auto_select else ""
        lines.append(f"- {role.name} [{role.lane}]{suffix}: {role.description}")
    lines.append(
        "Executive personas advise; these execution roles own operational lanes. "
        "Do not conflate the two."
    )
    return "\n".join(lines)


def execution_contract_prompt(
    role: ExecutionRoleContract,
    change_classification: str | None = None,
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> str:
    resolved = _catalog(catalog)
    classification = change_classification or "UNDECLARED — classify before substantive work"

    def section(title: str, rows: tuple[str, ...]) -> str:
        return title + "\n" + "\n".join(f"- {row}" for row in rows)

    return "\n\n".join(
        [
            "SHARED EXECUTION CONTRACT\n"
            + "\n".join(f"- {rule}" for rule in resolved.shared_execution_rules),
            (
                f"EXECUTION ROLE\n- Name: {role.name}\n- Lane: {role.lane}\n"
                f"- Change classification: {classification}\n- Purpose: {role.description}"
            ),
            section("EXPECTED WORK", role.expected_work),
            section("MUST REFUSE OR REROUTE", role.must_refuse),
            section("REQUIRED ARTIFACTS", role.required_artifacts),
            section("DEFAULT HANDOFF", role.hands_to),
            section("ROLE FAILURE CONDITIONS", role.failure_conditions),
        ]
    )


def _normalized_owner(owner: str | None) -> str:
    return re.sub(
        r"\s+",
        " ",
        (owner or "").strip().lower().replace("_", " ").replace("-", " "),
    )


def execution_role_for_agent(
    agent: str | None,
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> ExecutionRoleContract | None:
    resolved = _catalog(catalog)
    role_id = resolved.owner_to_execution_role.get(_normalized_owner(agent))
    return resolved.role_map.get(role_id) if role_id else None


def execution_agent_key(
    role: ExecutionRoleContract | str,
    *,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> str:
    resolved = _catalog(catalog)
    role_id = role.id if isinstance(role, ExecutionRoleContract) else role
    for owner, mapped_role_id in resolved.owner_to_execution_role.items():
        if mapped_role_id == role_id:
            return owner
    return str(role_id).replace("-", " ")


def execution_role_for_work_item(
    state: Any,
    *,
    split_brain: bool = False,
    catalog: ExecutionRoleCatalogDefinition | None = None,
) -> ExecutionRoleContract:
    resolved = _catalog(catalog)
    roles = resolved.role_map
    if split_brain:
        return roles["orchestrator"]

    handoff = getattr(state, "handoff", None)
    if handoff is not None and getattr(handoff, "status", None) == "pending":
        recipient_role = execution_role_for_agent(
            getattr(handoff, "to_agent", None),
            catalog=resolved,
        )
        if recipient_role is not None:
            return recipient_role

    owner = getattr(state, "current_owner", None) or getattr(state, "next_owner", None)
    owner_role = execution_role_for_agent(owner, catalog=resolved)
    if owner_role is not None:
        return owner_role

    stage = str(getattr(state, "current_stage", "") or "").strip().lower()
    if stage in {"ready_for_validation", "validation_running"}:
        validation_role = execution_role_for_agent(
            getattr(state, "validation_owner", None),
            catalog=resolved,
        )
        return validation_role or roles["quinn"]
    if stage == "ready_to_close" and bool(getattr(state, "release_gate", False)):
        release_role = execution_role_for_agent(
            getattr(state, "release_owner", None),
            catalog=resolved,
        )
        return release_role or roles["release-manager"]
    if stage == "failed_with_action_owner":
        action_role = execution_role_for_agent(
            getattr(state, "next_owner", None),
            catalog=resolved,
        )
        if action_role is not None:
            return action_role
    return roles["orchestrator"]
