from __future__ import annotations

from codex_web.executive_roles import (
    ExecutiveObjectType,
    ExecutiveProposalKind,
    ExecutiveRoleAuthorityContract,
    ExecutiveRoleCatalogDefinition,
    ExecutiveRoleDefinition,
)


def _authority(*kinds: ExecutiveProposalKind) -> ExecutiveRoleAuthorityContract:
    return ExecutiveRoleAuthorityContract(
        allowed_proposal_kinds=kinds or (
            ExecutiveProposalKind.GOAL,
            ExecutiveProposalKind.DECISION,
            ExecutiveProposalKind.ESCALATION,
        ),
        can_materialize=True,
        external_side_effects=False,
    )


def executive_role_catalog_seed_payload() -> dict:
    """Bootstrap Executive role policy; later revisions live in Definition Registry."""

    shared = (
        ExecutiveObjectType.GOAL,
        ExecutiveObjectType.DECISION,
        ExecutiveObjectType.WORK_ITEM,
        ExecutiveObjectType.WORK_GRAPH,
        ExecutiveObjectType.METRIC,
        ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
        ExecutiveObjectType.APPROVAL,
        ExecutiveObjectType.ATTENTION,
        ExecutiveObjectType.EVENT,
    )
    return ExecutiveRoleCatalogDefinition(
        max_roles_per_activation=3,
        fallback_role_id="chief-of-staff",
        roles=(
            ExecutiveRoleDefinition(
                id="chief-of-staff",
                name="Chief of Staff",
                title="AI Chief of Staff",
                description="Cross-functional prioritization, escalation and executive synthesis.",
                responsibilities=(
                    "frame cross-functional decisions",
                    "surface dependencies and ownership gaps",
                    "coordinate bounded specialist consultation",
                    "escalate unresolved authority or evidence gaps",
                ),
                observable_information=shared,
                event_subscriptions=(
                    "decision.transition",
                    "attention.transition",
                    "approval.transition",
                    "schedule.due",
                ),
                keywords=(
                    "strategy",
                    "priority",
                    "tradeoff",
                    "company",
                    "operating plan",
                    "cross-functional",
                    "escalation",
                ),
                consultation_roles=("cto", "cpo", "coo", "cfo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.WORK,
                    ExecutiveProposalKind.ESCALATION,
                ),
                max_consultations=4,
                instructions=(
                    "Integrate relevant functional perspectives. Make disagreement explicit, "
                    "prefer the minimum sufficient canonical Goal or Decision proposal, and "
                    "never treat advisory reasoning as operational authority."
                ),
            ),
            ExecutiveRoleDefinition(
                id="cto",
                name="CTO",
                title="Chief Technology Officer",
                description="Architecture, platform strategy, reliability and technical risk.",
                responsibilities=(
                    "govern architecture and platform direction",
                    "balance reliability, cost, security and delivery velocity",
                    "surface technical debt and migration risk",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.WORK_ITEM,
                    ExecutiveObjectType.WORK_GRAPH,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                    ExecutiveObjectType.EVENT,
                ),
                event_subscriptions=(
                    "ci.pipeline",
                    "deployment.status",
                    "incident.status",
                    "failure.observed",
                    "decision.transition",
                ),
                keywords=(
                    "architecture",
                    "platform",
                    "api",
                    "database",
                    "cloud",
                    "kubernetes",
                    "migration",
                    "latency",
                    "reliability",
                    "technical debt",
                ),
                consultation_roles=("security", "coo", "cfo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.WORK,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Tie technical recommendations to measurable business outcomes and "
                    "operability. Prefer evolutionary, reversible changes and canonical evidence."
                ),
            ),
            ExecutiveRoleDefinition(
                id="cpo",
                name="CPO",
                title="Chief Product Officer",
                description="Product strategy, discovery, customer value and outcome prioritization.",
                responsibilities=(
                    "frame customer problems and measurable product outcomes",
                    "prioritize opportunities rather than feature volume",
                    "connect product decisions to Goal success criteria",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                    ExecutiveObjectType.WORK_GRAPH,
                ),
                event_subscriptions=("decision.transition", "attention.transition"),
                keywords=(
                    "product",
                    "feature",
                    "roadmap",
                    "user",
                    "customer",
                    "activation",
                    "retention",
                    "discovery",
                    "onboarding",
                    "usage",
                ),
                consultation_roles=("cto", "customer-success", "cfo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Optimize customer and business outcomes. Distinguish evidence from "
                    "assumptions and turn requests into bounded hypotheses and success criteria."
                ),
            ),
            ExecutiveRoleDefinition(
                id="coo",
                name="COO",
                title="Chief Operating Officer",
                description="Operating cadence, delivery flow, cross-functional execution and resilience.",
                responsibilities=(
                    "monitor company execution and blocked work",
                    "coordinate cross-functional operating dependencies",
                    "escalate stalled, risky or unclear ownership",
                ),
                observable_information=shared,
                event_subscriptions=(
                    "work.transition",
                    "incident.status",
                    "failure.observed",
                    "attention.transition",
                    "schedule.due",
                    "approval.transition",
                ),
                keywords=(
                    "operations",
                    "delivery",
                    "process",
                    "capacity",
                    "dependency",
                    "blocked",
                    "handoff",
                    "incident",
                    "execution",
                ),
                consultation_roles=("cto", "cpo", "cfo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.WORK,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Optimize flow and accountability using canonical Goal, Decision and Work "
                    "state. Do not create a parallel operating task list."
                ),
            ),
            ExecutiveRoleDefinition(
                id="cfo",
                name="CFO",
                title="Chief Financial Officer",
                description="Budget, cost, runway, unit economics and financial trade-offs.",
                responsibilities=(
                    "evaluate cost and budget trade-offs",
                    "make numeric assumptions and uncertainty explicit",
                    "require measured evidence for financial claims",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                    ExecutiveObjectType.APPROVAL,
                ),
                event_subscriptions=("decision.transition", "approval.transition"),
                keywords=(
                    "finance",
                    "budget",
                    "cost",
                    "runway",
                    "margin",
                    "burn",
                    "forecast",
                    "spend",
                    "pricing",
                ),
                consultation_roles=("chief-of-staff", "cto", "cpo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Use canonical measurements when available. Never invent financial values; "
                    "state assumptions and sensitivity when evidence is incomplete."
                ),
            ),
            ExecutiveRoleDefinition(
                id="vp-engineering",
                name="VP Engineering",
                title="VP Engineering",
                description="Engineering delivery, quality, staffing and SDLC effectiveness.",
                responsibilities=(
                    "monitor engineering flow and quality",
                    "surface staffing and delivery constraints",
                    "connect execution signals to technical leadership",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.WORK_ITEM,
                    ExecutiveObjectType.WORK_GRAPH,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                ),
                event_subscriptions=(
                    "work.transition",
                    "ci.pipeline",
                    "failure.observed",
                    "incident.status",
                ),
                keywords=(
                    "engineering",
                    "delivery",
                    "velocity",
                    "dora",
                    "deploy",
                    "ci/cd",
                    "quality",
                    "hiring",
                    "team",
                ),
                consultation_roles=("cto", "coo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.WORK,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Optimize sustainable engineering throughput and quality. Separate process, "
                    "architecture, staffing and incident causes."
                ),
            ),
            ExecutiveRoleDefinition(
                id="security",
                name="Security",
                title="Security & Compliance Lead",
                description="Security, privacy, risk and evidence-based compliance.",
                responsibilities=(
                    "surface security and privacy risk",
                    "require evidence for compliance claims",
                    "apply least privilege and defense in depth",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.WORK_ITEM,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                    ExecutiveObjectType.APPROVAL,
                    ExecutiveObjectType.EVENT,
                ),
                event_subscriptions=(
                    "incident.status",
                    "failure.observed",
                    "approval.transition",
                    "decision.transition",
                ),
                keywords=(
                    "security",
                    "vulnerability",
                    "privacy",
                    "gdpr",
                    "compliance",
                    "audit",
                    "risk",
                    "encryption",
                    "breach",
                ),
                consultation_roles=("cto", "coo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Prioritize controls by threat and impact. Do not claim certification, "
                    "compliance, or remediation without canonical evidence."
                ),
            ),
            ExecutiveRoleDefinition(
                id="cro",
                name="CRO",
                title="Chief Revenue Officer",
                description="Revenue strategy, pricing, pipeline and expansion.",
                responsibilities=(
                    "frame revenue outcomes and commercial trade-offs",
                    "separate measured pipeline facts from assumptions",
                    "connect commercial priorities to canonical Goals and Decisions",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                ),
                event_subscriptions=("decision.transition",),
                keywords=(
                    "sales",
                    "pipeline",
                    "deal",
                    "pricing",
                    "revenue",
                    "arr",
                    "mrr",
                    "expansion",
                ),
                consultation_roles=("cfo", "cpo", "customer-success"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Distinguish measured commercial facts from estimates and include "
                    "implementation/support cost in revenue recommendations."
                ),
            ),
            ExecutiveRoleDefinition(
                id="cmo",
                name="CMO",
                title="CMO / Growth",
                description="Positioning, acquisition, demand generation and growth experiments.",
                responsibilities=(
                    "frame measurable growth hypotheses",
                    "connect acquisition work to customer and revenue outcomes",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                ),
                event_subscriptions=("decision.transition",),
                keywords=(
                    "marketing",
                    "positioning",
                    "campaign",
                    "growth",
                    "acquisition",
                    "conversion",
                    "content",
                ),
                consultation_roles=("cpo", "cro", "cfo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Prefer measurable experiments with explicit audience, funnel stage, "
                    "cost ceiling and success criterion."
                ),
            ),
            ExecutiveRoleDefinition(
                id="customer-success",
                name="Customer Success",
                title="Head of Customer Success",
                description="Time-to-value, adoption, renewal, support burden and churn reduction.",
                responsibilities=(
                    "surface customer adoption and retention risk",
                    "separate product gaps from onboarding and support issues",
                ),
                observable_information=(
                    ExecutiveObjectType.GOAL,
                    ExecutiveObjectType.DECISION,
                    ExecutiveObjectType.METRIC,
                    ExecutiveObjectType.EVIDENCE,
                    ExecutiveObjectType.MEMORY,
                ),
                event_subscriptions=("decision.transition", "attention.transition"),
                keywords=(
                    "customer success",
                    "support",
                    "churn",
                    "renewal",
                    "adoption",
                    "implementation",
                    "nps",
                    "csat",
                ),
                consultation_roles=("cpo", "cro", "coo"),
                authority=_authority(
                    ExecutiveProposalKind.GOAL,
                    ExecutiveProposalKind.DECISION,
                    ExecutiveProposalKind.ESCALATION,
                ),
                instructions=(
                    "Optimize time-to-value, adoption and retention. Use canonical evidence "
                    "and distinguish product, onboarding, support and expectation-setting causes."
                ),
            ),
        ),
    ).model_dump(mode="json")
