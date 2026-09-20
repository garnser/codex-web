from __future__ import annotations

import re
from collections.abc import Callable

from codex_web.models import WorkItemState


class WorkflowClaimPolicy:
    """Validate outbound workflow claims against canonical work-item state."""

    def __init__(
        self,
        *,
        load_states: Callable[[], dict[str, WorkItemState]],
        ensure_defaults: Callable[[WorkItemState], WorkItemState],
        coerce_owner: Callable[[str | None], str | None],
        owner_names: tuple[str, ...],
    ) -> None:
        self.load_states = load_states
        self.ensure_defaults = ensure_defaults
        self.coerce_owner = coerce_owner
        self.owner_names = owner_names

    def mentioned_states(self, text: str) -> list[WorkItemState]:
        states = self.load_states()
        refs = set(
            re.findall(
                r"\b[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#\d+\b",
                text,
            )
        )
        mentioned = [states[ref] for ref in refs if ref in states]
        for iid in set(
            re.findall(
                r"(?<![A-Za-z0-9_./-])#(\d+)\b",
                text,
            )
        ):
            matches = [
                state
                for ref, state in states.items()
                if ref.endswith(f"#{iid}")
            ]
            if (
                len(matches) == 1
                and all(
                    state.ref != matches[0].ref
                    for state in mentioned
                )
            ):
                mentioned.append(matches[0])
        return mentioned

    def findings(
        self,
        text: str,
    ) -> tuple[list[str], list[WorkItemState]]:
        mentioned = self.mentioned_states(text)
        if len(mentioned) != 1:
            return [], mentioned
        state = self.ensure_defaults(mentioned[0])
        findings: list[str] = []
        owner_names = sorted(
            set(self.owner_names)
            | {
                "orchestrator",
                "release manager",
                "compliance manager",
            },
            key=len,
            reverse=True,
        )
        owners = "|".join(
            re.escape(owner) for owner in owner_names
        )
        owner_patterns = (
            rf"\bcurrent[_ ]owner\s*[=:]\s*?({owners})\b",
            rf"\bowned by\s+?({owners})\b",
            (
                rf"\bownership\s+(?:moved|transferred)\s+to\s+"
                rf"?({owners})\b"
            ),
        )
        claimed_owner: str | None = None
        for pattern in owner_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                claimed_owner = self.coerce_owner(match.group(1))
                break
        canonical_owner = self.coerce_owner(state.current_owner)
        if claimed_owner and claimed_owner != canonical_owner:
            findings.append(
                f"owner claim mismatch for {state.ref}: "
                f"claimed={claimed_owner} "
                f"canonical={canonical_owner or 'none'}"
            )

        stage_match = re.search(
            r"\bcurrent[_ ]stage\s*[=:]\s*?([a-z][a-z0-9 _-]*)",
            text,
            re.IGNORECASE,
        )
        if stage_match:
            claimed_stage = (
                stage_match.group(1)
                .strip()
                .lower()
                .replace(" ", "_")
                .rstrip("._-")
            )
            if claimed_stage != state.current_stage:
                findings.append(
                    f"stage claim mismatch for {state.ref}: "
                    f"claimed={claimed_stage} "
                    f"canonical={state.current_stage}"
                )

        if re.search(
            r"\bhandoff\s+(?:is\s+|was\s+)?accepted\b",
            text,
            re.IGNORECASE,
        ):
            handoff_status = (
                state.handoff.status if state.handoff else None
            )
            if handoff_status != "accepted":
                findings.append(
                    f"handoff claim mismatch for {state.ref}: "
                    "claimed=accepted "
                    f"canonical={handoff_status or 'none'}"
                )
        return findings, mentioned

    @staticmethod
    def correction(
        report_name: str,
        states: list[WorkItemState],
        findings: list[str],
    ) -> str:
        snapshots = []
        for state in states:
            handoff = (
                state.handoff.status if state.handoff else "none"
            )
            snapshots.append(
                f"{state.ref}: "
                f"current_owner={state.current_owner or 'none'}, "
                f"current_stage={state.current_stage}, "
                f"handoff={handoff}, "
                f"next_owner={state.next_owner or 'none'}"
            )
        return (
            f"{report_name}: Codex-web withheld an agent update "
            "because its workflow claim conflicted with canonical "
            f"state. {'; '.join(findings)}. Canonical state: "
            f"{'; '.join(snapshots)}."
        )
