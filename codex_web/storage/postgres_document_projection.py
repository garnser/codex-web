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


# Array-format 2 shares the v1 table layout, not its row-key encoding. Keeping
# the layout marker at 1 lets old initialized owners retain canonical fallback.
ARRAY_FORMAT_KEY = 'document-array-projection-format'
ARRAY_ENABLE_PREFIX = 'document-array-projection-v2:'
ARRAY_NAMES = ('artifacts', 'evidence', 'verifications')


def initialize_arrays(cursor):
    """Install optional array helpers without enabling a namespace or format."""
    cursor.execute('''CREATE OR REPLACE FUNCTION codex_prepare_document_arrays_v2(ns text, source text)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE document jsonb; original json; records json; previous_records json;
            previous_source text; previous_digest bytea; previous_source_digest bytea;
            previous_version integer; array_digest bytea; selected_array text; slot integer;
            record_count integer; index_key_budget integer; supported boolean;
        BEGIN
        IF NOT EXISTS (SELECT 1 FROM codex_state_metadata
            WHERE key='document-record-projection-version' AND value='1') OR
            NOT EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-array-projection-format' AND value='2') OR
            NOT EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-array-projection-v2:' || ns AND value='2') THEN RETURN; END IF;
        index_key_budget := current_setting('block_size')::integer / 4;
        -- The canonical document lock precedes every derived header/row lock.
        SELECT payload INTO previous_source FROM codex_state_documents WHERE namespace=ns FOR UPDATE;
        document := source::jsonb;
        original := source::json;
        supported := jsonb_typeof(document)='object'
            AND COALESCE(jsonb_typeof(document->'schema_version'),'null') IN ('string','null');
        IF supported THEN
            FOR selected_array,slot IN SELECT * FROM (VALUES
                ('artifacts'::text,1),('evidence'::text,2),('verifications'::text,3)) AS arrays(name,number)
            LOOP
                IF COALESCE(jsonb_typeof(document->selected_array),'array')!='array' THEN
                    supported := FALSE; EXIT;
                END IF;
                records := COALESCE(original->selected_array,'[]'::json);
                IF json_array_length(records)::numeric*4+slot>9223372036854775807 OR EXISTS (
                    SELECT 1 FROM json_array_elements(records) AS member(value)
                    WHERE json_typeof(value)='object' AND (
                        COALESCE(json_typeof(value->'id'),'null')!='string' OR
                        COALESCE(json_typeof(value->'organization_id'),'null')!='string' OR
                        COALESCE(json_typeof(value->'workspace_id'),'null')!='string' OR
                        octet_length(ns)+octet_length(json_build_array(selected_array,value->>'id')::text)>index_key_budget)) THEN
                    supported := FALSE; EXIT;
                END IF;
            END LOOP;
        END IF;
        IF NOT COALESCE(supported,FALSE) THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
            RETURN;
        END IF;
        -- Hash original array text, never a JSONB-normalized member payload.
        array_digest := sha256(convert_to(json_build_array(
            COALESCE(original->'artifacts','[]'::json),COALESCE(original->'evidence','[]'::json),
            COALESCE(original->'verifications','[]'::json))::text,'UTF8'));
        SELECT version,records_sha256,source_sha256 INTO previous_version,previous_digest,previous_source_digest
            FROM codex_document_record_headers WHERE namespace=ns;
        IF previous_version IS DISTINCT FROM 2 THEN
            DELETE FROM codex_document_record_rows WHERE namespace=ns;
            previous_source_digest := NULL;
        END IF;
        INSERT INTO codex_document_record_headers(namespace,version,source_sha256,records_sha256,
            top_keys,field_types,schema_value,eligible)
        SELECT ns,2,sha256(convert_to(source,'UTF8')),array_digest,
            ARRAY(SELECT jsonb_object_keys(document)),
            (SELECT jsonb_object_agg(key,jsonb_typeof(value)) FROM jsonb_each(document)),
            document->'schema_version',TRUE
        ON CONFLICT(namespace) DO UPDATE SET version=EXCLUDED.version,
            source_sha256=EXCLUDED.source_sha256,records_sha256=EXCLUDED.records_sha256,
            top_keys=EXCLUDED.top_keys,field_types=EXCLUDED.field_types,
            schema_value=EXCLUDED.schema_value,eligible=EXCLUDED.eligible;
        IF previous_version=2 AND previous_digest IS NOT DISTINCT FROM array_digest THEN RETURN; END IF;
        FOR selected_array,slot IN SELECT * FROM (VALUES
            ('artifacts'::text,1),('evidence'::text,2),('verifications'::text,3)) AS arrays(name,number)
        LOOP
            records := COALESCE(original->selected_array,'[]'::json);
            record_count := json_array_length(records);
            previous_records := '[]'::json;
            IF previous_source_digest IS NOT NULL AND previous_source_digest=
                sha256(convert_to(previous_source,'UTF8')) THEN
                previous_records := COALESCE(previous_source::json->selected_array,'[]'::json);
            END IF;
            INSERT INTO codex_document_record_rows(namespace,ordinal,item_id,organization_id,workspace_id,payload)
            SELECT ns,ordinality*4+slot,json_build_array(selected_array,value->>'id')::text,
                value->>'organization_id',value->>'workspace_id',value::text
                FROM ROWS FROM(json_array_elements(records),json_array_elements(previous_records))
                    WITH ORDINALITY AS member(value,previous_value,ordinality)
                WHERE json_typeof(value)='object' AND value::text IS DISTINCT FROM previous_value::text
            ON CONFLICT(namespace,ordinal) DO UPDATE SET item_id=EXCLUDED.item_id,
                organization_id=EXCLUDED.organization_id,workspace_id=EXCLUDED.workspace_id,payload=EXCLUDED.payload
            WHERE (codex_document_record_rows.item_id,codex_document_record_rows.organization_id,
                codex_document_record_rows.workspace_id,codex_document_record_rows.payload)
                IS DISTINCT FROM (EXCLUDED.item_id,EXCLUDED.organization_id,EXCLUDED.workspace_id,EXCLUDED.payload);
            DELETE FROM codex_document_record_rows WHERE namespace=ns AND mod(ordinal,4)=slot AND (
                ordinal/4>record_count OR ordinal IN (
                    SELECT ordinality*4+slot FROM json_array_elements(records) WITH ORDINALITY AS member(value,ordinality)
                    WHERE json_typeof(value)!='object'));
        END LOOP;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR untranslatable_character THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
        END $$''')


def prepare_v2(cursor, namespace, serialized):
    """An older initialized/cold backend may not have the optional helper."""
    cursor.execute("SELECT to_regprocedure('codex_prepare_document_arrays_v2(text,text)')")
    if cursor.fetchone()[0] is not None:
        cursor.execute('SELECT codex_prepare_document_arrays_v2(%s,%s)', (namespace, serialized))


def read_v2(cursor, namespace, array_name, item_ids, organization_id, workspace_id,
            names, first_per_id, scope_after_first):
    """Read tagged originals only when the current canonical TEXT hash agrees."""
    if array_name not in (*ARRAY_NAMES, 'references'):
        return None
    first = 'DISTINCT ON (item_id) ' if first_per_id else ''
    order = ' ORDER BY item_id, ordinal' if first_per_id else ''
    selection = f'''SELECT {first}ordinal,payload,organization_id,workspace_id
        FROM codex_document_record_rows WHERE namespace=%s AND item_id IN (
            SELECT json_build_array(%s::text,requested.id)::text FROM unnest(%s::text[]) AS requested(id))
        {'' if scope_after_first else 'AND organization_id=%s AND workspace_id=%s'}{order}'''
    parameters = [namespace, array_name, list(item_ids)]
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
            %s::text,COALESCE((SELECT json_agg(payload::json ORDER BY ordinal) FROM selected),'[]'::json))::text
        FROM codex_document_record_headers AS header,current_document
        WHERE header.namespace=%s AND header.version=2 AND header.eligible
            AND EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-record-projection-version' AND value='1')
            AND EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-array-projection-format' AND value='2')
            AND EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key=%s AND value='2')
            AND header.source_sha256=current_document.source_sha256
            AND header.top_keys <@ %s::text[]
            AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) AS field(name)
                WHERE header.field_types->>name IS NOT NULL AND header.field_types->>name!='array')'''
    cursor.execute(query, [namespace, *parameters, array_name, namespace,
        (REFERENCE_ENABLE_PREFIX if array_name == 'references' else ARRAY_ENABLE_PREFIX)+namespace,
        ['schema_version', *names], list(names)])
    return cursor.fetchone()


def read_artifact_input_v2(
    cursor,
    namespace,
    artifact_id,
    verification_id,
    evidence_ids,
    organization_id,
    workspace_id,
):
    """One current-hash observation, with each array's ordinary duplicate policy."""
    query = """WITH current_document AS MATERIALIZED (
        SELECT sha256(convert_to(payload,'UTF8')) AS source_sha256
        FROM codex_state_documents WHERE namespace=%s),
        requested(array_name,id,first_only) AS MATERIALIZED (
            SELECT 'artifacts'::text,%s::text,true UNION ALL
            SELECT 'verifications'::text,%s::text,false UNION ALL
            SELECT 'evidence'::text,unnest(%s::text[]),true),
        selected AS MATERIALIZED (
            SELECT requested.array_name,member.ordinal,member.payload,requested.first_only,
                row_number() OVER(PARTITION BY requested.array_name,member.item_id ORDER BY member.ordinal) AS occurrence
            FROM codex_document_record_rows AS member JOIN requested
                ON member.item_id=json_build_array(requested.array_name,requested.id)::text
            WHERE member.namespace=%s AND member.organization_id=%s AND member.workspace_id=%s)
        SELECT json_build_object('schema_version',COALESCE(header.schema_value,'"0.0"'::jsonb),
            'artifacts',COALESCE((SELECT json_agg(payload::json ORDER BY ordinal) FROM selected
                WHERE array_name='artifacts' AND occurrence=1),'[]'::json),
            'verifications',COALESCE((SELECT json_agg(payload::json ORDER BY ordinal) FROM selected
                WHERE array_name='verifications'),'[]'::json),
            'evidence',COALESCE((SELECT json_agg(payload::json ORDER BY ordinal) FROM selected
                WHERE array_name='evidence' AND occurrence=1),'[]'::json))::text
        FROM codex_document_record_headers AS header,current_document
        WHERE header.namespace=%s AND header.version=2 AND header.eligible
            AND EXISTS(SELECT 1 FROM codex_state_metadata WHERE key='document-record-projection-version' AND value='1')
            AND EXISTS(SELECT 1 FROM codex_state_metadata WHERE key='document-array-projection-format' AND value='2')
            AND EXISTS(SELECT 1 FROM codex_state_metadata WHERE key=%s AND value='2')
            AND header.source_sha256=current_document.source_sha256
            AND header.top_keys <@ ARRAY['schema_version','artifacts','evidence','verifications']::text[]
            AND NOT EXISTS(SELECT 1 FROM unnest(ARRAY['artifacts','evidence','verifications']) AS field(name)
                WHERE header.field_types->>name IS NOT NULL AND header.field_types->>name!='array')"""
    cursor.execute(
        query,
        (
            namespace,
            artifact_id,
            verification_id,
            list(dict.fromkeys(evidence_ids)),
            namespace,
            organization_id,
            workspace_id,
            namespace,
            ARRAY_ENABLE_PREFIX + namespace,
        ),
    )
    return cursor.fetchone()


REFERENCE_NAMES = ('references',)
REFERENCE_ENABLE_PREFIX = 'document-reference-projection-v2:'


def initialize_references(cursor):
    """A distinct helper survives an older array-only initializer unchanged."""
    cursor.execute('''CREATE OR REPLACE FUNCTION codex_prepare_document_references_v2(ns text, source text)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE document jsonb; original json; records json; previous_records json;
            previous_source text; previous_digest bytea; previous_source_digest bytea;
            previous_version integer; previous_types jsonb; reference_digest bytea;
            record_count integer; index_key_budget integer; supported boolean;
        BEGIN
        IF NOT EXISTS (SELECT 1 FROM codex_state_metadata
            WHERE key='document-record-projection-version' AND value='1') OR
            NOT EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-array-projection-format' AND value='2') OR
            NOT EXISTS (SELECT 1 FROM codex_state_metadata
                WHERE key='document-reference-projection-v2:' || ns AND value='2') THEN RETURN; END IF;
        index_key_budget := current_setting('block_size')::integer / 4;
        SELECT payload INTO previous_source FROM codex_state_documents WHERE namespace=ns FOR UPDATE;
        document := source::jsonb;
        original := source::json;
        supported := jsonb_typeof(document)='object'
            AND COALESCE(jsonb_typeof(document->'references'),'array')='array'
            AND COALESCE(jsonb_typeof(document->'schema_version'),'null') IN ('string','null');
        IF supported THEN
            records := COALESCE(original->'references','[]'::json);
            supported := json_array_length(records)::numeric*4<=9223372036854775807
                AND NOT EXISTS (SELECT 1 FROM json_array_elements(records) AS member(value)
                    WHERE json_typeof(value)='object' AND (
                        COALESCE(json_typeof(value->'id'),'null')!='string' OR
                        COALESCE(json_typeof(value->'organization_id'),'null')!='string' OR
                        COALESCE(json_typeof(value->'workspace_id'),'null')!='string' OR
                        octet_length(ns)+octet_length(json_build_array('references'::text,value->>'id')::text)>index_key_budget));
        END IF;
        IF NOT COALESCE(supported,FALSE) THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
            RETURN;
        END IF;
        reference_digest := sha256(convert_to(records::text,'UTF8'));
        SELECT version,records_sha256,source_sha256,field_types
            INTO previous_version,previous_digest,previous_source_digest,previous_types
            FROM codex_document_record_headers WHERE namespace=ns;
        -- Version 2 also serves the distinct Artifact group. Never interpret
        -- its digest or ordinal slots as an earlier reference projection.
        IF previous_version IS DISTINCT FROM 2 OR
            COALESCE(previous_types ?| ARRAY['artifacts','evidence','verifications'],FALSE) THEN
            DELETE FROM codex_document_record_rows WHERE namespace=ns;
            previous_source_digest := NULL;
            previous_digest := NULL;
        END IF;
        INSERT INTO codex_document_record_headers(namespace,version,source_sha256,records_sha256,
            top_keys,field_types,schema_value,eligible)
        SELECT ns,2,sha256(convert_to(source,'UTF8')),reference_digest,
            ARRAY(SELECT jsonb_object_keys(document)),
            (SELECT jsonb_object_agg(key,jsonb_typeof(value)) FROM jsonb_each(document)),
            document->'schema_version',TRUE
        ON CONFLICT(namespace) DO UPDATE SET version=EXCLUDED.version,
            source_sha256=EXCLUDED.source_sha256,records_sha256=EXCLUDED.records_sha256,
            top_keys=EXCLUDED.top_keys,field_types=EXCLUDED.field_types,
            schema_value=EXCLUDED.schema_value,eligible=EXCLUDED.eligible;
        -- Audit appends update the canonical source hash but never copy audit
        -- members or rewrite unchanged reference payloads.
        IF previous_version=2 AND previous_digest IS NOT DISTINCT FROM reference_digest THEN RETURN; END IF;
        previous_records := '[]'::json;
        IF previous_source_digest IS NOT NULL AND previous_source_digest=
            sha256(convert_to(previous_source,'UTF8')) THEN
            previous_records := COALESCE(previous_source::json->'references','[]'::json);
        END IF;
        record_count := json_array_length(records);
        INSERT INTO codex_document_record_rows(namespace,ordinal,item_id,organization_id,workspace_id,payload)
        SELECT ns,ordinality*4,json_build_array('references'::text,value->>'id')::text,
            value->>'organization_id',value->>'workspace_id',value::text
            FROM ROWS FROM(json_array_elements(records),json_array_elements(previous_records))
                WITH ORDINALITY AS member(value,previous_value,ordinality)
            WHERE json_typeof(value)='object' AND value::text IS DISTINCT FROM previous_value::text
        ON CONFLICT(namespace,ordinal) DO UPDATE SET item_id=EXCLUDED.item_id,
            organization_id=EXCLUDED.organization_id,workspace_id=EXCLUDED.workspace_id,payload=EXCLUDED.payload
        WHERE (codex_document_record_rows.item_id,codex_document_record_rows.organization_id,
            codex_document_record_rows.workspace_id,codex_document_record_rows.payload)
            IS DISTINCT FROM (EXCLUDED.item_id,EXCLUDED.organization_id,EXCLUDED.workspace_id,EXCLUDED.payload);
        DELETE FROM codex_document_record_rows WHERE namespace=ns AND (
            ordinal/4>record_count OR ordinal IN (
                SELECT ordinality*4 FROM json_array_elements(records) WITH ORDINALITY AS member(value,ordinality)
                WHERE json_typeof(value)!='object'));
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR untranslatable_character THEN
            DELETE FROM codex_document_record_headers WHERE namespace=ns;
        END $$''')


def prepare_references_v2(cursor, namespace, serialized):
    """Never dispatch Secret writes through an older Artifact-only helper."""
    cursor.execute("SELECT to_regprocedure('codex_prepare_document_references_v2(text,text)')")
    if cursor.fetchone()[0] is not None:
        cursor.execute('SELECT codex_prepare_document_references_v2(%s,%s)', (namespace, serialized))
