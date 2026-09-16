from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from codex_web.execution_contracts import (
    ROLE_CONTRACTS,
    ExecutionRoleContract,
    execution_agent_key,
    execution_contract_prompt,
    execution_role,
    execution_role_catalog_prompt,
    execution_role_for_work_item,
    route_execution_role,
)


@dataclass(frozen=True)
class ExecutiveAgent:
    id: str
    name: str
    title: str
    description: str
    focus: tuple[str, ...]


AGENTS: dict[str, ExecutiveAgent] = {
    "chief-of-staff": ExecutiveAgent(
        "chief-of-staff",
        "Alex",
        "AI Chief of Staff",
        "Cross-functional prioritization, decision framing, portfolio trade-offs and executive synthesis.",
        ("strategy", "priority", "decision", "tradeoff", "board", "company", "operating plan", "okr"),
    ),
    "cto": ExecutiveAgent(
        "cto",
        "Maya",
        "CTO / Principal Architect",
        "Architecture, platform strategy, technical debt, build-vs-buy and engineering systems.",
        ("architecture", "api", "database", "cloud", "aws", "azure", "gcp", "kubernetes", "scalability", "technical debt", "stack", "migration", "latency"),
    ),
    "vp-engineering": ExecutiveAgent(
        "vp-engineering",
        "Noah",
        "VP Engineering",
        "Delivery, staffing, SDLC, DORA metrics, quality, incidents and engineering execution.",
        ("sprint", "delivery", "developer", "engineering", "velocity", "dora", "deploy", "ci/cd", "incident", "on-call", "hiring", "team", "bug", "quality"),
    ),
    "cpo": ExecutiveAgent(
        "cpo",
        "Sofia",
        "Chief Product Officer",
        "Product strategy, discovery, roadmap, activation, retention and customer value.",
        ("roadmap", "feature", "product", "user", "activation", "retention", "discovery", "persona", "onboarding", "usage", "feedback", "prioritize"),
    ),
    "cro": ExecutiveAgent(
        "cro",
        "Elias",
        "Chief Revenue Officer",
        "B2B SaaS sales, pipeline, pricing, packaging, expansion and revenue operations.",
        ("sales", "pipeline", "deal", "pricing", "package", "arr", "mrr", "revenue", "quota", "enterprise", "contract", "expansion", "upsell"),
    ),
    "cmo": ExecutiveAgent(
        "cmo",
        "Lea",
        "CMO / Growth",
        "Positioning, acquisition, PLG, demand generation, content and growth experiments.",
        ("marketing", "positioning", "brand", "campaign", "seo", "content", "lead", "plg", "growth", "acquisition", "conversion", "website"),
    ),
    "cfo": ExecutiveAgent(
        "cfo",
        "Oskar",
        "CFO / SaaS FinOps",
        "SaaS metrics, runway, budgets, unit economics, cloud cost and scenario planning.",
        ("cash", "runway", "budget", "finance", "cost", "margin", "cac", "ltv", "payback", "burn", "forecast", "cloud cost", "gross margin"),
    ),
    "customer-success": ExecutiveAgent(
        "customer-success",
        "Amira",
        "Head of Customer Success",
        "Implementation, support, renewals, NRR/GRR, health scores and churn reduction.",
        ("customer success", "support", "churn", "renewal", "nrr", "grr", "health score", "implementation", "adoption", "csat", "nps"),
    ),
    "security": ExecutiveAgent(
        "security",
        "Viktor",
        "Security & Compliance Lead",
        "Application/cloud security, privacy, SOC 2, ISO 27001, GDPR and risk management.",
        ("security", "vulnerability", "soc 2", "soc2", "iso 27001", "gdpr", "privacy", "risk", "sso", "rbac", "encryption", "compliance", "audit", "breach"),
    ),
}


GLOBAL_OPERATING_SYSTEM = """
You are part of an AI executive team for a SaaS/software-development company.
Operate like an experienced executive who has built and scaled software products.

Rules:
1. Start from the business outcome, then work backward to product, engineering, revenue and operations.
2. Never invent company metrics. Clearly label assumptions and ask for a missing number only when it materially changes the decision; otherwise provide a useful range or scenario.
3. Use SaaS language correctly: ARR/MRR, NRR/GRR, logo and revenue churn, CAC, LTV, CAC payback, gross margin, activation, trial-to-paid, expansion, pipeline coverage and runway.
4. For engineering, consider DORA metrics, SLO/SLA, reliability, observability, CI/CD, security, maintainability, cloud cost and developer experience.
5. Prefer small reversible experiments before expensive irreversible commitments.
6. Make trade-offs explicit: speed, quality, cost, risk, customer impact and opportunity cost.
7. Give concrete ownership and next actions. Avoid generic consultant language.
8. Flag legal, tax, employment or compliance topics that require qualified local advice; still provide operational framing.
9. For security/privacy, use least privilege, defense in depth, data minimization and auditable controls.
10. Keep the answer concise by default, but include enough detail to make a decision.

Default response shape when it fits:
- Recommendation
- Why
- Risks / trade-offs
- Next 3 actions
- Metrics to watch
""".strip()


ROLE_INSTRUCTIONS = {
    "chief-of-staff": """Act as the Chief of Staff and executive integrator. Clarify the decision, expose dependencies between functions, prioritize by company impact, and turn ambiguity into an executable operating plan. When perspectives conflict, explain the conflict and make a recommendation.""",
    "cto": """Act as the CTO / Principal Architect. Own architecture, platform strategy, technical debt, scalability, reliability, cloud/platform choices, build-vs-buy and technical risk. Tie every technical recommendation to product velocity, total cost, operability and business constraints. Prefer evolutionary architecture and measurable migration stages over big-bang rewrites.""",
    "vp-engineering": """Act as VP Engineering. Optimize sustainable delivery and engineering effectiveness. Think in terms of team topology, WIP, cycle time, deployment frequency, change failure rate, MTTR, quality signals, incident load and hiring leverage. Separate process problems from architecture or staffing problems.""",
    "cpo": """Act as Chief Product Officer. Optimize customer value and business outcomes, not feature volume. Use discovery, segmentation, activation/retention cohorts, qualitative evidence, product analytics and opportunity cost. Convert roadmap requests into problems, hypotheses and measurable outcomes.""",
    "cro": """Act as Chief Revenue Officer for a B2B SaaS company. Own pipeline, win rate, ACV, sales cycle, pricing/packaging, expansion and forecasting. Distinguish PLG, sales-assisted and enterprise motions and account for implementation/support burden when evaluating revenue.""",
    "cmo": """Act as CMO / Growth lead. Own positioning, category narrative, demand generation, acquisition, PLG loops and conversion. Prefer measurable channel experiments with a clear ICP, message, funnel stage, cost ceiling and success criterion.""",
    "cfo": """Act as a SaaS CFO / FinOps leader. Model revenue quality, cash runway, burn, gross margin, CAC/LTV, payback, hiring capacity and cloud spend. Show formulas and sensitivity ranges when numbers are incomplete. Treat ARR growth without retention or margin context as insufficient.""",
    "customer-success": """Act as Head of Customer Success. Optimize time-to-value, adoption, renewals, expansion and referenceability. Use health signals, implementation milestones, support burden, NRR/GRR and churn reasons. Separate product gaps from onboarding, enablement and expectation-setting issues.""",
    "security": """Act as Security & Compliance Lead for a SaaS vendor. Cover application security, cloud security, identity, privacy, incident response, vendor risk and evidence-based compliance such as SOC 2, ISO 27001 and GDPR. Prioritize controls by threat/risk and customer requirements rather than checkbox compliance. Do not claim certification or legal compliance without evidence.""",
}


class CompanyContext(BaseModel):
    company_name: str = "Our SaaS Company"
    product: str = ""
    stage: str = ""
    icp: str = ""
    arr: str = ""
    mrr: str = ""
    team_size: str = ""
    stack: str = ""
    priorities: str = ""
    constraints: str = ""

    def as_prompt(self) -> str:
        rows = [
            ("Company", self.company_name),
            ("Product", self.product),
            ("Stage", self.stage),
            ("ICP", self.icp),
            ("ARR", self.arr),
            ("MRR", self.mrr),
            ("Team size", self.team_size),
            ("Stack", self.stack),
            ("Priorities", self.priorities),
            ("Constraints", self.constraints),
        ]
        populated = [f"- {key}: {value}" for key, value in rows if value.strip()]
        return "\n".join(populated) if populated else "- No company context supplied."


class ExecutiveChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str | None = None
    mode: Literal["advisor", "board"] = "advisor"
    company: CompanyContext | None = None
    include_runtime_context: bool = False


class SpecialistAnswer(BaseModel):
    agent_id: str
    agent_name: str
    title: str
    reply: str


class ExecutiveChatResponse(BaseModel):
    session_id: str
    mode: Literal["advisor", "board"]
    agent_id: str
    agent_name: str
    agent_title: str
    reply: str
    consulted: list[SpecialistAnswer] = Field(default_factory=list)
    model: str


class DelegateRequest(BaseModel):
    task: str = Field(min_length=1, max_length=50000)
    executive_reply: str = ""
    agent_id: str = "chief-of-staff"
    execution_role_id: str | None = None
    work_item_ref: str | None = None
    change_classification: Literal["cosmetic-only", "localized functional", "shared-surface", "release/security-sensitive"] | None = None
    project_id: str = "home"
    sandbox: str | None = None
    approval_policy: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    new_thread: bool = False


class ContextUpdate(BaseModel):
    company: CompanyContext


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _contains_keyword(text: str, keyword: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def rank_agents(message: str) -> list[ExecutiveAgent]:
    text = _normalize(message)
    explicit: list[ExecutiveAgent] = []
    for agent in AGENTS.values():
        handles = {f"@{agent.id}", f"@{agent.id.replace('-', '')}"}
        if any(handle in text for handle in handles):
            explicit.append(agent)
    if explicit:
        return explicit + [agent for agent in AGENTS.values() if agent not in explicit]

    scored: list[tuple[int, ExecutiveAgent]] = []
    for agent in AGENTS.values():
        score = sum(3 if " " in keyword else 1 for keyword in agent.focus if _contains_keyword(text, keyword))
        if agent.id == "chief-of-staff":
            score += 1
        scored.append((score, agent))
    scored.sort(key=lambda item: (-item[0], list(AGENTS).index(item[1].id)))
    return [agent for _, agent in scored]


def route_agent(message: str) -> ExecutiveAgent:
    return rank_agents(message)[0]


def _agent(agent_id: str | None) -> ExecutiveAgent:
    if agent_id and agent_id in AGENTS:
        return AGENTS[agent_id]
    return AGENTS["chief-of-staff"]


def _build_instructions(agent: ExecutiveAgent, company_prompt: str, runtime_prompt: str = "") -> str:
    runtime = f"\n\nCODEX-WEB RUNTIME CONTEXT\n{runtime_prompt}" if runtime_prompt else ""
    return f"{GLOBAL_OPERATING_SYSTEM}\n\nROLE\n{ROLE_INSTRUCTIONS[agent.id]}\n\nEXECUTION ROLE CATALOG\n{execution_role_catalog_prompt()}\n\nCOMPANY CONTEXT\n{company_prompt}{runtime}"


def _build_board_instructions(company_prompt: str, runtime_prompt: str = "") -> str:
    runtime = f"\n\nCODEX-WEB RUNTIME CONTEXT\n{runtime_prompt}" if runtime_prompt else ""
    return f"""{GLOBAL_OPERATING_SYSTEM}

You are the Chief of Staff chairing an executive review. You will receive independent specialist views. Synthesize them rather than merely summarizing them. Resolve conflicts, call out unknowns, and produce one prioritized recommendation.

EXECUTION ROLE CATALOG
{execution_role_catalog_prompt()}

COMPANY CONTEXT
{company_prompt}{runtime}

Output:
1. Executive decision
2. Key reasoning and trade-offs
3. 30-day action plan with owners
4. Metrics / guardrails
5. Open questions that could change the decision
""".strip()


def _codex_execution_instructions(
    agent: ExecutiveAgent,
    company: CompanyContext,
    execution_role_contract: ExecutionRoleContract,
    change_classification: str | None = None,
) -> str:
    return f"""You are the Codex execution counterpart for the {agent.title} executive advisor in a SaaS/software-development company.

EXECUTIVE CONTEXT
{ROLE_INSTRUCTIONS[agent.id]}

The executive persona is advisory. Your operational authority and lane are defined by the execution contract below. When the executive recommendation conflicts with the execution contract, do not silently cross lanes: preserve the objective, refuse/reroute the incompatible action, and record the exact owner or Orchestrator decision needed.

{execution_contract_prompt(execution_role_contract, change_classification)}

CODEX-WEB EXECUTION RULES
- Inspect the actual workspace and canonical work-item state before changing anything.
- Use codex-web handoff/progress/ack state for ownership transitions; do not emulate handoffs only in prose.
- Keep GitLab owner/status labels synchronized with codex-web state in the same turn when the integration is available.
- Preserve Codex sandbox and approval controls.
- Use tests, linting, focused validation, and exact artifact identity appropriate to the declared change classification.
- If the objective is not directly implementable in this role, produce the required role artifact and hand it to the exact next lane rather than absorbing another role.

Company context:
{company.as_prompt()}
""".strip()


class ExecutiveStore:
    def __init__(self, host: Any):
        self.host = host
        data_dir = Path(getattr(host, "DATA_DIR", Path("data")))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.context_file = data_dir / "executive_company.json"
        self.sessions_file = data_dir / "executive_sessions.json"
        self.thread_map_file = data_dir / "executive_codex_threads.json"
        self.max_history = max(4, int(os.environ.get("CODEX_WEB_EXECUTIVE_MAX_HISTORY", "24")))

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        writer = getattr(self.host, "_atomic_write_text", None)
        if writer:
            writer(path, text, private=True)
        else:
            path.write_text(text)

    def company(self) -> CompanyContext:
        return CompanyContext.model_validate(self._read_json(self.context_file, {}))

    def save_company(self, company: CompanyContext) -> CompanyContext:
        self._write_json(self.context_file, company.model_dump())
        return company

    def history(self, session_id: str) -> list[dict[str, str]]:
        sessions = self._read_json(self.sessions_file, {})
        rows = sessions.get(session_id, [])
        if not isinstance(rows, list):
            return []
        return [row for row in rows[-self.max_history :] if isinstance(row, dict) and row.get("role") and row.get("content")]

    def append_history(self, session_id: str, role: str, content: str) -> None:
        sessions = self._read_json(self.sessions_file, {})
        rows = sessions.setdefault(session_id, [])
        rows.append({"role": role, "content": content, "at": time.time()})
        sessions[session_id] = rows[-self.max_history :]
        self._write_json(self.sessions_file, sessions)

    def thread_for(self, project_id: str, agent_id: str) -> str | None:
        mapping = self._read_json(self.thread_map_file, {})
        value = mapping.get(f"{project_id}:{agent_id}")
        return str(value) if value else None

    def remember_thread(self, project_id: str, agent_id: str, thread_id: str) -> None:
        mapping = self._read_json(self.thread_map_file, {})
        mapping[f"{project_id}:{agent_id}"] = thread_id
        self._write_json(self.thread_map_file, mapping)

    def thread_map(self) -> dict[str, str]:
        payload = self._read_json(self.thread_map_file, {})
        return {str(key): str(value) for key, value in payload.items()}


class ExecutiveService:
    def __init__(self, host: Any):
        self.host = host
        self.store = ExecutiveStore(host)
        self.model = os.environ.get("CODEX_WEB_EXECUTIVE_MODEL", "gpt-5.6-terra")
        self.reasoning_effort = os.environ.get("CODEX_WEB_EXECUTIVE_REASONING_EFFORT", "medium")
        self.text_verbosity = os.environ.get("CODEX_WEB_EXECUTIVE_TEXT_VERBOSITY", "medium")
        self.board_specialists = min(5, max(2, int(os.environ.get("CODEX_WEB_EXECUTIVE_BOARD_SPECIALISTS", "3"))))
        self._openai_client: Any = None

    def _client(self) -> Any:
        if self._openai_client is not None:
            return self._openai_client
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError("The openai Python package is not installed; run pip install -r requirements.txt") from exc
        self._openai_client = AsyncOpenAI(api_key=api_key)
        return self._openai_client

    async def _respond(self, instructions: str, messages: list[dict[str, str]]) -> str:
        response = await self._client().responses.create(
            model=self.model,
            instructions=instructions,
            input=messages,
            reasoning={"effort": self.reasoning_effort},
            text={"verbosity": self.text_verbosity},
        )
        return response.output_text.strip()

    def runtime_summary(self) -> str:
        projects = []
        active_turns: dict[str, Any] = {}
        queues: dict[str, Any] = {}
        work_items: dict[str, Any] = {}
        try:
            projects = list(self.host._load_projects())
        except Exception:
            pass
        try:
            active_turns = self.host._load_active_turns()
        except Exception:
            pass
        try:
            queues = self.host._load_turn_queues()
        except Exception:
            pass
        try:
            work_items = self.host._load_work_item_states()
        except Exception:
            pass

        open_items = []
        for state in sorted(work_items.values(), key=lambda item: getattr(item, "updated_at", 0), reverse=True)[:20]:
            if getattr(state, "closed_at", None):
                continue
            open_items.append(
                {
                    "ref": getattr(state, "ref", None),
                    "title": getattr(state, "title", None),
                    "stage": getattr(state, "current_stage", None),
                    "owner": getattr(state, "current_owner", None) or getattr(state, "next_owner", None),
                    "release_gate": getattr(state, "release_gate", False),
                }
            )
        ready = getattr(getattr(self.host, "codex", None), "ready", None)
        payload = {
            "codex_ready": bool(ready and ready.is_set()),
            "projects": [{"id": p.id, "name": p.name} for p in projects],
            "active_turns": len(active_turns),
            "queued_turns": sum(len(items) for items in queues.values()),
            "open_work_items": open_items,
        }
        return json.dumps(payload, indent=2)

    async def chat(self, request: ExecutiveChatRequest) -> ExecutiveChatResponse:
        company = request.company or self.store.company()
        company_prompt = company.as_prompt()
        runtime_prompt = self.runtime_summary() if request.include_runtime_context else ""
        history = self.store.history(request.session_id)
        messages = [*history, {"role": "user", "content": request.message}]

        if request.mode == "board":
            result = await self._board(request, messages, company_prompt, runtime_prompt)
        else:
            agent = _agent(request.agent_id) if request.agent_id else route_agent(request.message)
            reply = await self._respond(_build_instructions(agent, company_prompt, runtime_prompt), messages)
            result = ExecutiveChatResponse(
                session_id=request.session_id,
                mode="advisor",
                agent_id=agent.id,
                agent_name=agent.name,
                agent_title=agent.title,
                reply=reply,
                model=self.model,
            )

        self.store.append_history(request.session_id, "user", request.message)
        self.store.append_history(request.session_id, "assistant", result.reply)
        return result

    async def _board(
        self,
        request: ExecutiveChatRequest,
        messages: list[dict[str, str]],
        company_prompt: str,
        runtime_prompt: str,
    ) -> ExecutiveChatResponse:
        ranked = [agent for agent in rank_agents(request.message) if agent.id != "chief-of-staff"]
        specialists = ranked[: self.board_specialists]
        if len(specialists) < 2:
            specialists = [AGENTS["cto"], AGENTS["cpo"], AGENTS["cfo"]][: self.board_specialists]

        async def consult(agent: ExecutiveAgent) -> SpecialistAnswer:
            reply = await self._respond(_build_instructions(agent, company_prompt, runtime_prompt), messages)
            return SpecialistAnswer(agent_id=agent.id, agent_name=agent.name, title=agent.title, reply=reply)

        consulted = await asyncio.gather(*(consult(agent) for agent in specialists))
        evidence = "\n\n".join(f"## {item.agent_name} ({item.title})\n{item.reply}" for item in consulted)
        synthesis = [{"role": "user", "content": f"Original question:\n{request.message}\n\nSpecialist views:\n{evidence}"}]
        reply = await self._respond(_build_board_instructions(company_prompt, runtime_prompt), synthesis)
        chair = AGENTS["chief-of-staff"]
        return ExecutiveChatResponse(
            session_id=request.session_id,
            mode="board",
            agent_id=chair.id,
            agent_name=chair.name,
            agent_title=chair.title,
            reply=reply,
            consulted=consulted,
            model=self.model,
        )

    async def _delegate_canonical_work_item(
        self,
        request: DelegateRequest,
        agent: ExecutiveAgent,
        execution_role_contract: ExecutionRoleContract,
        state: Any,
        project: Any,
    ) -> dict[str, Any]:
        agent_key = execution_agent_key(execution_role_contract)
        binding = self.host._binding_for_agent(
            agent_key,
            project.id,
            preferred_conversation_id=getattr(self.host, "HANDOFF_COORDINATION_CHANNEL", None),
        )
        if binding is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Canonical work item {state.ref} routes to {execution_role_contract.name}, "
                    f"but no active codex-web agent binding exists for {agent_key!r} in project {project.id}. "
                    "Repair the agent binding or reroute the work item through Orchestrator; Open Executive will not "
                    "create a parallel owner thread for an existing canonical item."
                ),
            )

        recommendation = request.executive_reply.strip()
        classification = request.change_classification or "agent must classify before substantive work"
        canonical_prompt = self.host._work_item_dispatch_text(state)
        message = (
            f"Open Executive context for canonical work item {state.ref}.\n"
            f"Advisory source: {agent.title}.\n"
            f"Canonical execution role: {execution_role_contract.name} ({execution_role_contract.lane}).\n"
            f"Change classification: {classification}.\n\n"
            f"{canonical_prompt}\n\n"
            f"Executive objective/context:\n{request.task.strip()}\n"
            + (f"\nExecutive recommendation:\n{recommendation}\n" if recommendation else "")
            + "\nDo not change ownership outside the canonical handoff/ack/progress path. Process this existing work item in its current lane and update codex-web/GitLab state as the contract requires."
        )
        result = await self.host._dispatch_event_to_binding(binding, message, "executive-work-item")
        effective_thread_id = binding.thread_id
        if isinstance(result, dict):
            effective_thread_id = result.get("newThreadId") or result.get("threadId") or binding.thread_id
        public_state = self.host._work_item_state_public(state)
        return {
            "ok": True,
            "agentId": agent.id,
            "agentTitle": agent.title,
            "executionRole": execution_role_contract.public(),
            "changeClassification": request.change_classification,
            "projectId": project.id,
            "threadId": effective_thread_id,
            "createdNewThread": False,
            "turn": result,
            "approvalPolicy": binding.approval_policy,
            "sandbox": binding.sandbox,
            "dispatchMode": "canonical-work-item",
            "workItemRef": state.ref,
            "canonicalWorkItem": public_state,
        }

    async def delegate(self, request: DelegateRequest) -> dict[str, Any]:
        agent = _agent(request.agent_id)
        requested_role = execution_role(request.execution_role_id)
        if request.execution_role_id and requested_role is None:
            raise HTTPException(status_code=422, detail=f"Unknown execution role: {request.execution_role_id}")

        work_item_state = None
        execution_role_contract = requested_role
        project_id = request.project_id
        if request.work_item_ref:
            work_item_state = self.host._work_item_state(request.work_item_ref)
            split_brain = False
            split_brain_finder = getattr(self.host, "_work_item_split_brain_findings", None)
            if split_brain_finder:
                split_brain = bool(split_brain_finder(work_item_state))
            canonical_role = execution_role_for_work_item(work_item_state, split_brain=split_brain)
            if requested_role is not None and requested_role.id != canonical_role.id:
                owner = getattr(work_item_state, "current_owner", None) or getattr(work_item_state, "next_owner", None) or "unowned"
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Canonical work item {work_item_state.ref} is currently routed to {canonical_role.name} "
                        f"(owner={owner}, stage={work_item_state.current_stage}). "
                        f"Requested execution role {requested_role.name} would bypass canonical ownership. "
                        "Record the reassignment through codex-web handoff/ack/progress and GitLab labels first."
                    ),
                )
            execution_role_contract = canonical_role
            project_id = work_item_state.project_id or request.project_id

        if execution_role_contract is None:
            execution_role_contract = route_execution_role(request.task, agent.id)
        company = self.store.company()
        project = self.host._project(project_id)
        if work_item_state is not None:
            return await self._delegate_canonical_work_item(
                request,
                agent,
                execution_role_contract,
                work_item_state,
                project,
            )

        sandbox = request.sandbox or project.sandbox
        approval_policy = request.approval_policy or project.approval_policy
        model = request.model or project.model

        thread_role_key = f"{agent.id}:{execution_role_contract.id}"
        thread_id = None if request.new_thread else self.store.thread_for(project.id, thread_role_key)
        created_new_thread = False
        if not thread_id:
            created_new_thread = True
            response = await self.host.create_thread(
                project_id=project.id,
                sandbox=sandbox,
                approval_policy=approval_policy,
                model=model,
                reasoning_effort=request.reasoning_effort,
            )
            thread = response.get("thread", response) if isinstance(response, dict) else {}
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not thread_id:
                raise RuntimeError("Codex did not return a thread id")
            set_thread_name = getattr(self.host, "_set_thread_name", None)
            if set_thread_name:
                try:
                    await set_thread_name(thread_id, f"Executive · {agent.title} → {execution_role_contract.name}")
                except Exception:
                    pass

        remember = getattr(self.host, "_remember_thread_run_settings", None)
        if remember:
            remember(
                thread_id,
                sandbox=sandbox,
                approval_policy=approval_policy,
                model=model,
                reasoning_effort=request.reasoning_effort,
                developer_instructions=_codex_execution_instructions(
                    agent,
                    company,
                    execution_role_contract,
                    request.change_classification,
                ),
            )

        recommendation = request.executive_reply.strip()
        classification = request.change_classification or "agent must classify before substantive work"
        message = (
            f"Executive delegation from {agent.title}.\n"
            f"Execution role: {execution_role_contract.name} ({execution_role_contract.lane}).\n"
            f"Change classification: {classification}.\n\n"
            f"Objective:\n{request.task.strip()}\n"
            + (f"\nExecutive recommendation/context:\n{recommendation}\n" if recommendation else "")
            + "\nApply the installed execution-role contract. Inspect canonical project/work-item state first. If this objective falls outside your lane, refuse the wrong-lane action and create or identify the exact handoff/reroute instead. Otherwise execute within the configured Codex sandbox and approval policy and validate the exact artifact appropriate to the lane before reporting completion."
        )
        payload = self.host.TurnCreate(
            message=message,
            project_id=project.id,
            model=model,
            reasoning_effort=request.reasoning_effort,
            approval_policy=approval_policy,
            sandbox=sandbox,
        )
        result = await self.host.start_turn(thread_id, payload)
        effective_thread_id = thread_id
        if isinstance(result, dict):
            effective_thread_id = result.get("newThreadId") or result.get("threadId") or thread_id
        self.store.remember_thread(project.id, thread_role_key, effective_thread_id)
        return {
            "ok": True,
            "agentId": agent.id,
            "agentTitle": agent.title,
            "executionRole": execution_role_contract.public(),
            "changeClassification": request.change_classification,
            "projectId": project.id,
            "threadId": effective_thread_id,
            "createdNewThread": created_new_thread or effective_thread_id != thread_id,
            "turn": result,
            "dispatchMode": "executive-thread",
            "workItemRef": None,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox,
        }


def install_executive(app: FastAPI, host: Any) -> ExecutiveService:
    service = ExecutiveService(host)
    router = APIRouter()
    static_dir = Path(getattr(host, "STATIC_DIR", Path(__file__).resolve().parents[1] / "static"))

    @router.get("/executive", include_in_schema=False)
    async def executive_page() -> FileResponse:
        return FileResponse(static_dir / "executive.html", media_type="text/html")

    @router.get("/api/executive/agents")
    async def list_agents() -> dict[str, Any]:
        return {
            "agents": [
                {
                    "id": agent.id,
                    "name": agent.name,
                    "title": agent.title,
                    "description": agent.description,
                }
                for agent in AGENTS.values()
            ],
            "executionRoles": [role.public() for role in ROLE_CONTRACTS.values()],
            "model": service.model,
        }

    @router.get("/api/executive/context")
    async def get_context() -> dict[str, Any]:
        return {"company": service.store.company().model_dump()}

    @router.post("/api/executive/context")
    async def update_context(payload: ContextUpdate) -> dict[str, Any]:
        return {"ok": True, "company": service.store.save_company(payload.company).model_dump()}

    @router.get("/api/executive/runtime")
    async def executive_runtime() -> dict[str, Any]:
        return {
            "model": service.model,
            "reasoningEffort": service.reasoning_effort,
            "threadMap": service.store.thread_map(),
            "summary": json.loads(service.runtime_summary()),
        }

    @router.post("/api/executive/chat")
    async def executive_chat(payload: ExecutiveChatRequest) -> dict[str, Any]:
        try:
            return (await service.chat(payload)).model_dump()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/api/executive/delegate")
    async def executive_delegate(payload: DelegateRequest) -> dict[str, Any]:
        try:
            return await service.delegate(payload)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    app.include_router(router)
    return service
