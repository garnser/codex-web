"""Rebuildable selected-record index; canonical document TEXT remains authoritative.

Version 1 indexes only string identity fields in a records array. Unsupported
shapes retain the original document reader. These helpers own no connection or
transaction: callers must retain the canonical namespace lock and write owner.
"""
from __future__ import annotations

VERSION = 1
ENABLE_PREFIX = 'document-record-projection-v1:'


def initialize(cursor):
    cursor.execute("SELECT value FROM codex_state_metadata WHERE key='document-record-projection-version'")
    row = cursor.fetchone()
    if row is not None and row[0] != str(VERSION):
        raise RuntimeError('Unsupported derived document projection schema')
    cursor.execute('''CREATE TABLE IF NOT EXISTS codex_document_record_headers (
        namespace TEXT PRIMARY KEY REFERENCES codex_state_documents(namespace)
            ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
        version INTEGER NOT NULL,
        source_sha256 BYTEA,
        records_sha256 BYTEA,
        top_keys TEXT[],
        field_types JSONB,
        schema_value JSONB,
        eligible BOOLEAN NOT NULL DEFAULT FALSE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS codex_document_record_rows (
        namespace TEXT NOT NULL REFERENCES codex_document_record_headers(namespace)
            ON DELETE CASCADE,
        ordinal BIGINT NOT NULL,
        item_id TEXT NOT NULL,
        organization_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY(namespace, ordinal))''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS codex_document_record_rows_identity_idx
        ON codex_document_record_rows(namespace, item_id, ordinal)''')
    # An old binary cannot maintain this derived index. Its changed document
    # physically erases projected raw records in that SAME transaction.
    cursor.execute('''CREATE OR REPLACE FUNCTION codex_invalidate_document_records_v1()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF EXISTS (SELECT 1 FROM codex_document_record_headers
            WHERE namespace=NEW.namespace AND
                source_sha256 IS DISTINCT FROM sha256(convert_to(NEW.payload,'UTF8'))) THEN
            DELETE FROM codex_document_record_rows WHERE namespace=NEW.namespace;
            UPDATE codex_document_record_headers SET source_sha256=NULL,
                records_sha256=NULL, top_keys=NULL, field_types=NULL,
                schema_value=NULL, eligible=FALSE WHERE namespace=NEW.namespace;
        END IF;
        RETURN NEW;
        END $$''')
    cursor.execute('''CREATE OR REPLACE TRIGGER codex_document_records_invalidate_v1
        AFTER UPDATE OF payload ON codex_state_documents FOR EACH ROW
        EXECUTE FUNCTION codex_invalidate_document_records_v1()''')
    cursor.execute('''CREATE OR REPLACE FUNCTION codex_prepare_document_records_v1(ns text, source text)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE document jsonb; original json; records json; record_digest bytea;
            previous_digest bytea; previous_source_digest bytea; previous_source text;
            previous_records json; record_count integer; index_key_budget integer; supported boolean;
        BEGIN
        IF NOT EXISTS (SELECT 1 FROM codex_state_metadata
            WHERE key='document-record-projection-version' AND value='1') OR
            NOT EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-record-projection-v1:' || ns AND value='1') THEN RETURN; END IF;
        index_key_budget := current_setting('block_size')::integer / 4;
        -- Match old/direct writer lock order: document first, derived rows second.
        SELECT payload INTO previous_source FROM codex_state_documents WHERE namespace=ns FOR UPDATE;
        document := source::jsonb;
        original := source::json;
        supported := jsonb_typeof(document)='object'
            AND jsonb_typeof(document->'records')='array'
            AND COALESCE(jsonb_typeof(document->'schema_version'),'null') IN ('string','null');
        IF supported THEN
            supported := NOT EXISTS (SELECT 1 FROM jsonb_array_elements(document->'records') AS member(value)
                WHERE jsonb_typeof(value)='object' AND (
                    COALESCE(jsonb_typeof(value->'id'),'null')!='string' OR
                    COALESCE(jsonb_typeof(value->'organization_id'),'null')!='string' OR
                    COALESCE(jsonb_typeof(value->'workspace_id'),'null')!='string' OR
                    octet_length(ns)+octet_length(value->>'id')>index_key_budget));
        END IF;
        IF NOT COALESCE(supported,FALSE) THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
            RETURN;
        END IF;
        records := original->'records';
        record_count := json_array_length(records);
        record_digest := sha256(convert_to(records::text,'UTF8'));
        SELECT records_sha256,source_sha256 INTO previous_digest,previous_source_digest
            FROM codex_document_record_headers WHERE namespace=ns;
        INSERT INTO codex_document_record_headers(namespace,version,source_sha256,records_sha256,
            top_keys,field_types,schema_value,eligible)
        SELECT ns,1,sha256(convert_to(source,'UTF8')),record_digest,
            ARRAY(SELECT jsonb_object_keys(document)),
            (SELECT jsonb_object_agg(key,jsonb_typeof(value)) FROM jsonb_each(document)),
            document->'schema_version',TRUE
        ON CONFLICT(namespace) DO UPDATE SET version=EXCLUDED.version,
            source_sha256=EXCLUDED.source_sha256,records_sha256=EXCLUDED.records_sha256,
            top_keys=EXCLUDED.top_keys,field_types=EXCLUDED.field_types,
            schema_value=EXCLUDED.schema_value,eligible=EXCLUDED.eligible;
        IF previous_digest IS NOT DISTINCT FROM record_digest THEN RETURN; END IF;
        previous_records := '[]'::json;
        IF previous_source_digest IS NOT NULL AND previous_source_digest=
            sha256(convert_to(previous_source,'UTF8')) THEN
            previous_records := previous_source::json->'records';
        END IF;
        INSERT INTO codex_document_record_rows(namespace,ordinal,item_id,organization_id,workspace_id,payload)
        SELECT ns,ordinality,value->>'id',value->>'organization_id',value->>'workspace_id',value::text
            FROM ROWS FROM(json_array_elements(records),json_array_elements(previous_records))
                WITH ORDINALITY AS member(value,previous_value,ordinality)
            WHERE json_typeof(value)='object' AND value::text IS DISTINCT FROM previous_value::text
        ON CONFLICT(namespace,ordinal) DO UPDATE SET item_id=EXCLUDED.item_id,
            organization_id=EXCLUDED.organization_id,workspace_id=EXCLUDED.workspace_id,payload=EXCLUDED.payload
        WHERE (codex_document_record_rows.item_id,codex_document_record_rows.organization_id,
            codex_document_record_rows.workspace_id,codex_document_record_rows.payload)
            IS DISTINCT FROM (EXCLUDED.item_id,EXCLUDED.organization_id,EXCLUDED.workspace_id,EXCLUDED.payload);
        DELETE FROM codex_document_record_rows WHERE namespace=ns AND (
            ordinal>record_count OR ordinal IN (
                SELECT ordinality FROM json_array_elements(records) WITH ORDINALITY AS member(value,ordinality)
                WHERE json_typeof(value)!='object'));
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR untranslatable_character THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
        END $$''')



def enable_version(cursor):
    """Only explicit projection opt-in changes the bare restore seed metadata."""
    cursor.execute("""INSERT INTO codex_state_metadata(key,value,updated_at)
        VALUES('document-record-projection-version','1',extract(epoch from clock_timestamp()))
        ON CONFLICT(key) DO NOTHING""")
    cursor.execute("SELECT value FROM codex_state_metadata "
        "WHERE key='document-record-projection-version' FOR SHARE")
    if cursor.fetchone()[0] != str(VERSION):
        raise RuntimeError('Unsupported derived document projection schema')


def prepare(cursor, namespace, serialized):
    """Optional derived work; initialize=False can target an older state schema."""
    cursor.execute("SELECT to_regprocedure('codex_prepare_document_records_v1(text,text)')")
    if cursor.fetchone()[0] is not None:
        cursor.execute('SELECT codex_prepare_document_records_v1(%s,%s)', (namespace, serialized))


def read(cursor, namespace, item_ids, organization_id, workspace_id, names,
         first_per_id, scope_after_first):
    first = 'DISTINCT ON (item_id) ' if first_per_id else ''
    order = ' ORDER BY item_id, ordinal' if first_per_id else ''
    selection = f'''SELECT {first}ordinal,payload,organization_id,workspace_id
        FROM codex_document_record_rows WHERE namespace=%s AND item_id=ANY(%s::text[])
        {'' if scope_after_first else 'AND organization_id=%s AND workspace_id=%s'}{order}'''
    parameters = [namespace, list(item_ids)]
    if not scope_after_first:
        parameters += [organization_id, workspace_id]
    if scope_after_first:
        selection = f'''SELECT * FROM ({selection}) AS first_global
            WHERE organization_id=%s AND workspace_id=%s'''
        parameters += [organization_id, workspace_id]
    query = f'''WITH current_document AS MATERIALIZED (
        SELECT sha256(convert_to(payload,'UTF8')) AS source_sha256
        FROM codex_state_documents WHERE namespace=%s),
        selected AS MATERIALIZED ({selection})
        SELECT json_build_object('schema_version',COALESCE(header.schema_value,'"0.0"'::jsonb),
            'records',COALESCE((SELECT json_agg(payload::json ORDER BY ordinal) FROM selected),'[]'::json))::text
        FROM codex_document_record_headers AS header,current_document
        WHERE header.namespace=%s AND header.version=1 AND header.eligible
            AND EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-record-projection-version' AND value='1')
            AND header.source_sha256=current_document.source_sha256
            AND header.top_keys <@ %s::text[]
            AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) AS field(name)
                WHERE header.field_types->>name IS NOT NULL AND header.field_types->>name!='array')'''
    cursor.execute(query, [namespace, *parameters, namespace, ['schema_version', *names], list(names)])
    return cursor.fetchone()
