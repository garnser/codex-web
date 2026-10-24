"""Source gates shared by every canonical Memory consumer of typed UX briefs."""
from codex_web.data_governance import ContextFilterRequest
from codex_web.organizational_memory import KnowledgeFreshness
from codex_web.services.ux_briefs import UxBriefService
from codex_web.ux_briefs import UxBrief


def _pins(item):
    brief = UxBrief.model_validate_json(item.content)
    return tuple(dict.fromkeys(pin for section in brief.sections.values() for pin in section.sources))


def brief_sources_readable(memory, item, actor):
    try:
        service = UxBriefService(memory)
        for pin in _pins(item):
            service._source(pin.kind, pin.record_id, item.project_id, actor)
        return True
    except (RuntimeError, ValueError, KeyError):
        return False


def brief_source_freshness(memory, item, *, records=None, seen=frozenset()):
    if item.id in seen or len(seen) >= 32:
        return KnowledgeFreshness.INVALID
    seen = seen | {item.id}
    try:
        pins = _pins(item)
        knowledge = records if records is not None else {row.id:row for row in memory.store.load().records}
        external = None
        service = UxBriefService(memory)
        for pin in pins:
            if pin.kind == "knowledge":
                source = knowledge.get(pin.record_id)
            else:
                if memory.artifact_evidence is None:
                    return KnowledgeFreshness.INVALID
                if external is None:
                    state = memory.artifact_evidence.store.load()
                    external = {"artifact":{row.id:row for row in state.artifacts},
                        "evidence":{row.id:row for row in state.evidence}}
                source = external[pin.kind].get(pin.record_id)
            if source is None or (source.organization_id,source.workspace_id)!=(item.organization_id,item.workspace_id):
                return KnowledgeFreshness.INVALID
            if source.project_id not in {None,item.project_id}:
                return KnowledgeFreshness.INVALID
            lifecycle = source.lifecycle.value
            if lifecycle in {"invalid","invalidated","redacted","deleted"}:
                return KnowledgeFreshness.INVALID
            if lifecycle == "expired":
                return KnowledgeFreshness.EXPIRED
            if pin.kind == "knowledge" and source.object_type == "ux_context_brief":
                nested = brief_source_freshness(memory, source, records=knowledge, seen=seen)
                if nested is not None:
                    return nested
            if lifecycle == "superseded" or service._pin(pin.kind,source)!=pin:
                return KnowledgeFreshness.STALE
            for field in ("valid_until","retention_expires_at"):
                deadline = getattr(source,field,None)
                if deadline is not None and memory.clock()>=deadline:
                    return KnowledgeFreshness.EXPIRED
            review = getattr(source,"review_after",None)
            if review is not None and memory.clock()>=review:
                return KnowledgeFreshness.STALE
        return None
    except (ValueError, RuntimeError, KeyError):
        return KnowledgeFreshness.INVALID


def brief_context_allowed(memory, item, actor, max_classification):
    try:
        service = UxBriefService(memory)
        ids = tuple(dict.fromkeys(service._source(pin.kind,pin.record_id,item.project_id,actor).governance_record_id for pin in _pins(item)))
        if not ids:
            return True
        # Constraints can tighten after curation. Check the current source
        # governance ancestry rather than relying only on inherited snapshots.
        pending = list(ids)
        visited = set()
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            if len(visited) >= 1000:
                return False
            governed = memory.governance.get_record(identity, actor, context_only=True)
            visited.add(identity)
            pending.extend(governed.source_record_ids)
        result = memory.governance.filter_context(ContextFilterRequest(record_ids=tuple(visited),
            max_classification=max_classification),actor=actor)
        return visited.issubset(result.allowed_record_ids)
    except (RuntimeError, ValueError, KeyError):
        return False
