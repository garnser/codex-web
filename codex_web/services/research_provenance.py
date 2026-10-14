"""Canonical source lifecycle checks for derived Memory research provenance."""

from collections.abc import Mapping

from codex_web.organizational_memory import KnowledgeFreshness, KnowledgeLifecycle, KnowledgeRecord


def derived_source_freshness(
    item: KnowledgeRecord,
    records: Mapping[str, KnowledgeRecord],
    *,
    now: float,
) -> KnowledgeFreshness | None:
    # A newer cited revision carries older attribution for reproducibility.
    # Inspect the newest explicitly cited version of each source logical key;
    # never substitute a newer uncited version from the store.
    sources: dict[str, KnowledgeRecord] = {}
    for snapshot in item.provenance.origin_sources:
        source = records.get(snapshot.knowledge_id)
        if source is None or source.version != snapshot.version or (
            source.organization_id, source.workspace_id
        ) != (item.organization_id, item.workspace_id):
            return KnowledgeFreshness.INVALID
        if source.logical_key == item.logical_key:
            continue  # Historical attribution within this record's own lineage.
        previous = sources.get(source.logical_key)
        if previous is None or source.version > previous.version:
            sources[source.logical_key] = source
    outcomes = set()
    for source in sources.values():
        if source.lifecycle in {KnowledgeLifecycle.INVALID, KnowledgeLifecycle.REDACTED, KnowledgeLifecycle.DELETED}:
            outcomes.add(KnowledgeFreshness.INVALID)
        elif source.lifecycle == KnowledgeLifecycle.SUPERSEDED:
            outcomes.add(KnowledgeFreshness.STALE)
        if any(deadline is not None and now >= deadline for deadline in (
            source.valid_until, source.retention_expires_at,
        )):
            outcomes.add(KnowledgeFreshness.EXPIRED)
        elif source.review_after is not None and now >= source.review_after:
            outcomes.add(KnowledgeFreshness.STALE)
    for outcome in (KnowledgeFreshness.INVALID, KnowledgeFreshness.EXPIRED, KnowledgeFreshness.STALE):
        if outcome in outcomes:
            return outcome
    return None
