"""Typed original-row recovery predicate shared by canonical state backends.

No caller SQL, arbitrary JSON paths or derived-alias absence proofs are accepted.
"""
from contextlib import contextmanager
from pydantic import BaseModel, ConfigDict, Field, model_validator
from codex_web.storage.state_store import (StorageOperationBudget, storage_operation_budget,
    state_record_storage_key)


class HomeCommandIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    requested_by: str = Field(min_length=1)
    provider_type: str = Field(min_length=1)
    provider_instance: str = Field(min_length=1)
    resource_ids: tuple[str, ...] = Field(max_length=100)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)


class HomeIntentRecoveryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    execution_id: str | None = Field(default=None, min_length=1)
    command: HomeCommandIdentity | None = None

    @model_validator(mode='after')
    def mode(self):
        if (self.execution_id is None) == (self.command is None):
            raise ValueError('Exactly one original Home recovery scope is required')
        return self


@contextmanager
def recovery_query_budget():
    parent = storage_operation_budget.get()
    budget = StorageOperationBudget(min(5, parent.remaining()) if parent else 5)
    if parent is not None:
        budget.cancelled = parent.cancelled
    token = storage_operation_budget.set(budget)
    try:
        yield
    finally:
        storage_operation_budget.reset(token)


def recovery_statement(namespace, query, *, postgres=False):
    """Full original blockers only; settled history does not consume row budget."""
    if not isinstance(query, HomeIntentRecoveryQuery):
        raise ValueError('Typed canonical Home recovery query required')
    table = 'codex_state_documents' if postgres else 'state_documents'
    slot = '%s' if postgres else '?'
    args = []
    # Project only query headers in one JSON record conversion.  Repeated
    # payload::json field operators otherwise reparse each full historical
    # ActionIntent (including its potentially large request/result metadata).
    headers = ('id','organization_id','workspace_id','status','execution_id',
        'project_id','action_id','provider_type','provider_instance','requested_by',
        'last_receipt_id','execution_started_at','attempt','idempotency_key','correlation_id')
    json_headers = ('resource_ids','request')
    def document(alias):
        return f'{alias}.payload::json'
    def json_value(alias, path):
        if alias == 'i' and path and path[0] in json_headers:
            base = f'h.{path[0]}'
            return base if len(path) == 1 else f"({base} #> '{{{','.join(path[1:])}}}')"
        return f"({document(alias)} #> '{{{','.join(path)}}}')"
    def param(value):
        args.append(value)
        return slot
    def scalar(alias, *path):
        if postgres:
            if alias == 'i' and len(path) == 1 and path[0] in headers:
                return f"(h.{path[0]} #>> '{{}}')"
            if alias == 'i' and path and path[0] in json_headers:
                return f"({json_value(alias,path)} #>> '{{}}')"
            return f"({document(alias)} #>> '{{{','.join(path)}}}')"
        return f"json_extract(CASE WHEN json_valid({alias}.payload) THEN {alias}.payload ELSE '{{}}' END, '$.{'.'.join(path)}')"
    def kind(alias, *path):
        if postgres:
            return f"json_typeof({json_value(alias,path)})"
        return f"json_type(CASE WHEN json_valid({alias}.payload) THEN {alias}.payload ELSE '{{}}' END, '$.{'.'.join(path)}')"
    def array(alias, path, expected):
        if postgres:
            length = f"json_array_length(CASE WHEN {kind(alias,*path)} = 'array' THEN {json_value(alias,path)} ELSE '[]'::json END)"
            values = [f"{scalar(alias,*path,str(i))} = {param(value)}" for i,value in enumerate(expected)]
        else:
            length = f"json_array_length(CASE WHEN {kind(alias,*path)} = 'array' THEN {scalar(alias,*path)} ELSE '[]' END)"
            values = [f"json_extract({scalar(alias,*path)}, '$[{i}]') = {param(value)}" for i,value in enumerate(expected)]
        # Wrong/missing array type is indeterminate and must reach full validation.
        return f"CASE WHEN {kind(alias,*path)} IS NULL OR {kind(alias,*path)} != 'array' THEN TRUE ELSE ({length} = {len(expected)} AND {' AND '.join(values) if values else 'TRUE'}) END"
    lower = state_record_storage_key(namespace, 'intents:')
    # Parameters occur in textual SQL order: namespace bounds, then scope values.
    collation = ' COLLATE \"C\"' if postgres else ''
    bounds = f"i.namespace{collation} >= {param(lower)} AND i.namespace{collation} < {param(lower + chr(65535))}"
    scope = [f"{scalar('i','organization_id')} = {param(query.organization_id)}",
             f"{scalar('i','workspace_id')} = {param(query.workspace_id)}"]
    if query.execution_id is not None:
        scope.append(f"{scalar('i','execution_id')} = {param(query.execution_id)}")
    else:
        command = query.command
        for field,value in (('project_id','home'),('action_id','operator.host.command'),
                ('provider_type',command.provider_type),('provider_instance',command.provider_instance),
                ('requested_by',command.requested_by)):
            scope.append(f"{scalar('i',field)} = {param(value)}")
        scope.append(array('i',('resource_ids',),command.resource_ids))
        scope.append(array('i',('request','parameters','argv'),command.argv))
    identifier = scalar('i','id')
    safe_id = f"{identifier} ~ '^[A-Za-z0-9_-]+$'" if postgres else f"(length({identifier}) > 0 AND {identifier} NOT GLOB '*[^A-Za-z0-9_-]*')"
    # Canonical generated IDs use these ASCII characters; unsupported retained
    # IDs are returned as indeterminate, never silently excluded.
    receipt_base = state_record_storage_key(namespace,'receipts:')
    # This internal constant is encoded locally, never supplied SQL text.
    literal = "'" + receipt_base.replace("%", "%%" if postgres else "%").replace("'", "''") + "'"
    separator = "%%3A" if postgres else "%3A"
    prefix = f"({literal} || {identifier} || '{separator}')"
    col = 'r.namespace COLLATE "C"' if postgres else 'r.namespace'
    receipt_bounds = f"{col} >= {prefix} AND {col} < ({prefix} || '{chr(65535)}')"
    matching_receipt = f"{receipt_bounds} AND {scalar('r','id')} = {scalar('i','last_receipt_id')}"
    identity_fields = ('attempt','provider_type','provider_instance','action_id','idempotency_key','correlation_id')
    identities = ' AND '.join(f"{scalar('r',field)} = {scalar('i',field)}" for field in identity_fields)
    known = f"{scalar('r','intent_id')} = {identifier} AND {identities} AND {scalar('r','outcome')} IN ('completed','failed','callback')"
    total = f"(SELECT COUNT(*) FROM {table} r WHERE {matching_receipt})"
    positive = f"(SELECT COUNT(*) FROM {table} r WHERE {matching_receipt} AND {known})"
    malformed_receipt = ('FALSE' if postgres else
        f"EXISTS (SELECT 1 FROM {table} r WHERE {receipt_bounds} AND NOT json_valid(r.payload))")
    status = scalar('i','status')
    blockers = f"""CASE
        WHEN {status} IS NULL OR {status} NOT IN ('succeeded','failed','cancelled','expired') THEN TRUE
        WHEN NOT ({safe_id}) THEN TRUE
        WHEN {scalar('i','last_receipt_id')} IS NOT NULL THEN
            ({total} != 1 OR {positive} != 1 OR {malformed_receipt})
        ELSE {scalar('i','execution_started_at')} IS NOT NULL END"""
    malformed = ('FALSE' if postgres else 'NOT json_valid(i.payload)')
    # Malformed identifying fields cannot safely establish tenant/scope absence.
    missing = ' OR '.join(scalar('i',field) + ' IS NULL' for field in ('id','organization_id','workspace_id','status'))
    # A boolean AND does not constrain PostgreSQL's evaluation order.  Keep
    # correlated receipt validation behind an explicit scope CASE so unrelated
    # settled history cannot consume the bounded original-row query budget.
    # Indeterminate identifying fields still reach canonical full validation.
    where = f"""{bounds} AND CASE
        WHEN {malformed} OR {missing} THEN TRUE
        WHEN {' AND '.join(scope)} THEN ({blockers})
        ELSE FALSE END"""
    order = 'i.namespace COLLATE "C"' if postgres else 'i.namespace'
    relation = f'{table} i'
    if postgres:
        # Preserve JSON scalar/array semantics instead of normalizing through
        # jsonb or coercing projected headers to text. Unsupported retained JSON
        # still refuses under the existing canonical conversion-error boundary.
        columns = ', '.join(field+' json' for field in (*headers,*json_headers))
        # Preserve non-object/missing header behavior: return the original row
        # for full canonical validation rather than let record conversion hide it.
        relation += f""" CROSS JOIN LATERAL json_to_record(
            CASE WHEN json_typeof(i.payload::json) = 'object'
                THEN i.payload::json ELSE '{{}}'::json END) h({columns})"""
    return f"SELECT i.namespace, i.payload FROM {relation} WHERE {where} ORDER BY {order} LIMIT 257", tuple(args)
