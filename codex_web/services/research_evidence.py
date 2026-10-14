"""Resolve research provenance and eligible observed-source verification."""

from codex_web.organizational_memory import KnowledgeFreshness, KnowledgeOrigin
from codex_web.artifact_evidence import ArtifactLifecycle, EvidenceLifecycle, EvidenceResult, VerificationResult
from codex_web.services.organizational_memory import KnowledgeAuthorizationError
from codex_web.storage.organizational_memory import KnowledgeNotFoundError

OBSERVED_RESEARCH_METHOD = "observed-user-research"


class ResearchSourceError(ValueError):
    pass


def research_sources(memory, ids, *, actor, project_id):
    if not ids:
        return ()
    if memory is None:
        raise ResearchSourceError("canonical research sources are unavailable")
    sources = []
    for source_id in dict.fromkeys(ids):
        try:
            source = memory.get(source_id, actor=actor)
        except (KnowledgeNotFoundError, KnowledgeAuthorizationError) as error:
            raise ResearchSourceError("research source is unavailable in this scope") from error
        if source.project_id not in {None, project_id}:
            raise ResearchSourceError("research source Project does not match")
        if memory.freshness(source) != KnowledgeFreshness.FRESH:
            raise ResearchSourceError("research source is not current and fresh")
        sources.append(source)
    return tuple(sources)


def research_origin(sources):
    origins = {source.provenance.origin for source in sources}
    if not origins:
        return KnowledgeOrigin.UNKNOWN
    return next(iter(origins)) if len(origins) == 1 else KnowledgeOrigin.MIXED


def observed_verifications(item, *, memory, state, actor, verify_content, now, max_age_seconds=None):
    """A source label alone never qualifies; exact source proof must be reviewed."""
    try:
        sources = research_sources(memory, item.knowledge_ids, actor=actor, project_id=item.project_id)
        observed_ids = set()
        for source in sources:
            if source.provenance.origin == KnowledgeOrigin.HUMAN_OBSERVED:
                observed_ids.add(source.id)
            observed_ids.update(row.knowledge_id for row in source.provenance.origin_sources
                                if row.origin == KnowledgeOrigin.HUMAN_OBSERVED)
        observed = []
        for source_id in observed_ids:
            source = memory.get(source_id, actor=actor, include_inactive=True)
            if source.project_id not in {None, item.project_id}:
                raise ResearchSourceError("observed source Project does not match")
            if memory.freshness(source) == KnowledgeFreshness.FRESH:
                observed.append(source)
    except (ResearchSourceError, KnowledgeNotFoundError, KnowledgeAuthorizationError):
        return ()
    evidence_ids = {evidence_id for source in observed
                    if source.provenance.origin == KnowledgeOrigin.HUMAN_OBSERVED
                    for evidence_id in source.provenance.evidence_ids}
    eligible = set()
    artifacts = {row.id: row for row in state.artifacts}
    for proof in state.evidence:
        if proof.id not in evidence_ids or proof.lifecycle != EvidenceLifecycle.VALID or proof.result != EvidenceResult.PASS:
            continue
        if (proof.organization_id, proof.workspace_id) != (actor.organization_id, actor.workspace_id):
            continue
        if proof.project_id not in {None, item.project_id} or not proof.artifact_ids:
            continue
        if proof.retention_expires_at is not None and now >= proof.retention_expires_at:
            continue
        if max_age_seconds is not None and now - proof.observed_at > max_age_seconds:
            continue
        linked = [artifacts.get(source_id) for source_id in proof.artifact_ids]
        if any(source is None or source.lifecycle != ArtifactLifecycle.ACTIVE or source.digest is None
               or (source.organization_id, source.workspace_id) != (actor.organization_id, actor.workspace_id)
               or source.project_id not in {None, item.project_id} for source in linked):
            continue
        if any(source.retention_expires_at is not None and now >= source.retention_expires_at for source in linked):
            continue
        if not all(verify_content(source) for source in linked):
            continue
        eligible.add(proof.id)
    return tuple(row.id for row in state.verifications
                 if (row.organization_id, row.workspace_id) == (actor.organization_id, actor.workspace_id)
                 and row.result == VerificationResult.VERIFIED and row.independent
                 and row.method == OBSERVED_RESEARCH_METHOD
                 and eligible.intersection(row.evidence_ids))
