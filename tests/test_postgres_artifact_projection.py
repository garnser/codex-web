"""Private PostgreSQL contracts for rebuildable, array-separated metadata reads."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_web.storage import postgres_document_projection as projection
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.postgres_state import PostgresStateStore
from tests import test_autonomy_append_scaling as fixtures


ARRAYS = ('artifacts', 'evidence', 'verifications')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresArtifactProjectionTests(unittest.TestCase):
    setUp = fixtures.PostgresAutonomyAppendTests.setUp

    @staticmethod
    def document(**arrays):
        return {'schema_version': '1.5', **dict.fromkeys(ARRAYS, []), **arrays}

    @staticmethod
    def row(identity='selected', organization='local', **extra):
        return dict(id=identity, organization_id=organization, workspace_id='default', **extra)

    def enable(self, namespace='array-test', backend=None):
        return (backend or self.backend).enable_document_array_projection(namespace, array_names=ARRAYS)

    def read(self, array='artifacts', ids=('selected',), *, first=False, global_first=False,
             namespace='array-test', backend=None):
        return (backend or self.backend).document_array_items_envelope(namespace, array, ids,
            organization_id='local', workspace_id='default', collection_names=ARRAYS,
            first_per_id=first, scope_after_first=global_first)

    def point(self, array='artifacts', *, namespace='array-test', backend=None):
        return (backend or self.backend).document_array_item_envelope(namespace, array, 'selected',
            organization_id='local', workspace_id='default', collection_names=ARRAYS)

    def physical(self, namespace='array-test'):
        with self.backend._read_connection() as connection:
            return connection.execute('SELECT ordinal,payload,ctid::text,xmin::text '
                'FROM codex_document_record_rows WHERE namespace=%s ORDER BY ordinal',
                (namespace,)).fetchall()

    def input_records(self, *, artifact='selected', verification='selected',
                      evidence=('selected',), namespace='array-test'):
        return self.backend.artifact_input_envelope(namespace, artifact, verification, evidence,
            organization_id='local', workspace_id='default')

    def input_records_oracle(self, *, artifact='selected', verification='selected',
                             evidence=('selected',), namespace='array-test'):
        # Independently retain the released three-reader policies: the first
        # scoped Artifact/Evidence, but every scoped Verification duplicate.
        selected = self.backend.document_array_item_envelope(namespace, 'artifacts', artifact,
            organization_id='local', workspace_id='default', collection_names=ARRAYS)
        proofs = self.read('evidence', evidence, first=True, namespace=namespace)
        records = self.read('verifications', (verification,), namespace=namespace)
        return {'schema_version': selected['schema_version'],
                'artifacts': selected.get('artifacts', []),
                'evidence': proofs.get('evidence', []),
                'verifications': records.get('verifications', [])}

    def test_input_envelope_indexed_and_unindexed_match_each_array_duplicate_policy(self):
        rows = {name: [self.row(organization='foreign', value=name+'-foreign'),
                       self.row(value=name+'-first'), self.row(value=name+'-later')]
                for name in ARRAYS}
        self.backend.put('array-test', self.document(**rows))
        expected = self.input_records_oracle(evidence=('missing', 'selected', 'selected'))
        self.assertEqual(expected['artifacts'], rows['artifacts'][1:2])
        self.assertEqual(expected['evidence'], rows['evidence'][1:2])
        self.assertEqual(expected['verifications'], rows['verifications'][1:])
        self.assertEqual(self.input_records(evidence=('missing', 'selected', 'selected')), expected)
        self.assertTrue(self.enable())
        original = projection.read_artifact_input_v2
        observed = []

        def indexed(*args, **kwargs):
            row = original(*args, **kwargs)
            observed.append(row is not None)
            return row

        with patch.object(projection, 'read_artifact_input_v2', indexed):
            self.assertEqual(self.input_records(evidence=('missing', 'selected', 'selected')), expected)
        self.assertEqual(observed, [True])
        self.assertEqual(self.input_records(artifact='missing', verification='missing', evidence=()),
                         self.document())

    def test_input_envelope_preserves_original_json_numbers_and_raw_member_order(self):
        raw = r'''{"schema_version":"1.5","artifacts":[null,
            {"id":"selected","organization_id":"local","workspace_id":"default",
             "value":{"scientific":1e0,"zero":-0.0,"large":9007199254740993,"same":1,"same":2}}],
            "evidence":[{"id":"proof-b","organization_id":"local","workspace_id":"default","value":-0.0},
                {"id":"proof-a","organization_id":"local","workspace_id":"default","value":1e0}],
            "verifications":[{"id":"selected","organization_id":"local","workspace_id":"default","value":1e0},
                {"id":"selected","organization_id":"local","workspace_id":"default","value":-0.0}]}'''
        self.raw('array-test', raw)
        expected = self.input_records_oracle(evidence=('proof-a', 'proof-b'))
        self.assertEqual(self.input_records(evidence=('proof-a', 'proof-b')), expected)
        self.assertTrue(self.enable())
        actual = self.input_records(evidence=('proof-a', 'proof-b'))
        self.assertEqual(actual, expected)
        value = actual['artifacts'][0]['value']
        self.assertIs(type(value['scientific']), float)
        self.assertEqual(math.copysign(1, value['zero']), -1)
        self.assertEqual(value['large'], 9007199254740993)
        self.assertEqual(value['same'], 2)
        self.assertEqual([row['id'] for row in actual['evidence']], ['proof-b', 'proof-a'])
        self.assertEqual(math.copysign(1, actual['verifications'][1]['value']), -1)

    def test_input_envelope_legacy_headers_keep_complete_canonical_fallback(self):
        self.enable()
        for document in (self.document(unexpected={'retained': True}),
                         self.document(evidence={}),
                         {**self.document(), 'schema_version': 1.5},
                         {'schema_version': '1.0', 'artifacts': [], 'legacy': 'retained'}):
            with self.subTest(document=document):
                self.backend.put('array-test', document)
                self.assertEqual(self.input_records(), document)
        self.raw('array-test', 'NaN')
        self.assertTrue(math.isnan(self.input_records()))

    def test_input_envelope_stale_projection_hash_refuses_poisoned_derived_rows(self):
        self.enable()
        original = self.document(**{name: [self.row(value='canonical-'+name)] for name in ARRAYS})
        self.backend.put('array-test', original)
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_document_record_rows SET payload=%s WHERE namespace=%s',
                (json.dumps(self.row(value='poisoned-derived-row')), 'array-test'))
            connection.execute("UPDATE codex_document_record_headers SET source_sha256=sha256('wrong'::bytea) "
                               'WHERE namespace=%s', ('array-test',))
        read = projection.read_artifact_input_v2
        observations = []

        def checked(*args, **kwargs):
            row = read(*args, **kwargs)
            observations.append(row is None)
            return row

        with patch.object(projection, 'read_artifact_input_v2', checked):
            self.assertEqual(self.input_records(), original)
        self.assertEqual(observations, [True])
        changed = self.document(**{name: [self.row(value='current-'+name)] for name in ARRAYS})
        self.backend.put('array-test', changed)
        self.assertEqual(self.input_records(), changed)

    def test_input_envelope_selected_models_and_unknown_schema_still_fail_closed(self):
        from pydantic import ValidationError
        store = ArtifactEvidenceStore(self.backend)
        self.backend.put(store.namespace, self.document(artifacts=[self.row()]))
        with self.assertRaises(ValidationError):
            store.input_records_state('selected', 'selected', (),
                organization_id='local', workspace_id='default')
        self.backend.put(store.namespace, {**self.document(), 'schema_version': '999.0'})
        with self.assertRaises(ValueError):
            store.input_records_state('selected', 'selected', (),
                organization_id='local', workspace_id='default')

    def raw(self, namespace, serialized):
        # Deliberately model the pre-index writer. Enabling afterwards must seed
        # from these exact canonical bytes, not reconstructed Python numbers.
        self.backend.put(namespace, self.document())
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                (serialized, namespace))

    def test_array_identity_collision_and_first_scoped_vs_all_verification_matches(self):
        self.assertTrue(self.enable())
        rows = {name: [self.row(organization='foreign', value=name+'-foreign'),
                       self.row(value=name+'-first'), self.row(value=name+'-later')]
                for name in ARRAYS}
        self.backend.put('array-test', self.document(**rows))
        physical = self.physical()
        self.assertEqual(len(physical), 9)
        self.assertEqual(len({r[0] for r in physical}), 9)
        # Every array uses the same IDs and original ordinals. None may borrow
        # another array's first row or collapse ambiguous Verification records.
        with patch.object(self.backend, 'get', side_effect=AssertionError('unexpected fallback')):
            for array in ARRAYS:
                self.assertEqual(self.point(array), {'schema_version':'1.5', array:rows[array][1:2]})
                self.assertEqual(self.read(array, first=True), self.point(array))
                self.assertEqual(self.read(array), {'schema_version':'1.5', array:rows[array][1:]})
                self.assertEqual(self.read(array, first=True, global_first=True),
                                 {'schema_version':'1.5', array:[]})

    def test_raw_values_order_and_missing_ids_match_unindexed_reader(self):
        raw = r'''{"schema_version":"1.5","artifacts":[null,17,
            {"id":"selected","organization_id":"local","workspace_id":"default",
             "value":{"scientific":1e0,"signedzero":-0.0,"bigint":9007199254740993,
                      "unicode":"\u96ea","duplicate":1,"duplicate":2}},
            {"id":"selected","organization_id":"local","workspace_id":"default","value":"later"}],
            "evidence":[],"verifications":[]}'''
        self.raw('array-test', raw)
        before = self.read(ids=('missing','selected','selected'))
        self.assertTrue(self.enable())
        self.assertEqual(self.read(ids=('missing','selected','selected')), before)
        value = self.point()['artifacts'][0]['value']
        self.assertIs(type(value['scientific']), float)
        self.assertEqual(math.copysign(1, value['signedzero']), -1)
        self.assertEqual(value['bigint'], 9007199254740993)
        self.assertEqual(value['unicode'], '雪')
        self.assertEqual(value['duplicate'], 2)
        self.assertEqual(self.read(ids=()), {'schema_version':'1.5', 'artifacts':[]})

    def test_canonical_fingerprint_required_and_next_read_observes_current_rows(self):
        self.enable()
        self.backend.put('array-test', self.document(artifacts=[self.row(value='current')]))
        with self.backend._connection() as connection:
            connection.execute("UPDATE codex_document_record_headers SET source_sha256=sha256('wrong'::bytea) "
                               'WHERE namespace=%s', ('array-test',))
        self.assertEqual(self.point()['artifacts'], [self.row(value='current')])
        self.backend.put('array-test', self.document(artifacts=[self.row(value='revoked')]))
        self.assertEqual(self.point()['artifacts'], [self.row(value='revoked')])
        with self.backend._connection() as connection:
            connection.execute('DELETE FROM codex_document_record_headers WHERE namespace=%s', ('array-test',))
        self.assertEqual(self.point()['artifacts'], [self.row(value='revoked')])

    def test_direct_old_writer_redaction_erases_every_array_before_commit(self):
        self.enable()
        self.backend.put('array-test', self.document(**{
            name:[self.row(secret='erase-'+name)] for name in ARRAYS}))
        self.assertEqual(len(self.physical()), 3)
        replacement = self.document(artifacts=[self.row(value='redacted')])
        with self.backend._connection() as connection:
            connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                               (json.dumps(replacement), 'array-test'))
            self.assertEqual(connection.execute('SELECT count(*) FROM codex_document_record_rows '
                'WHERE namespace=%s', ('array-test',)).fetchone()[0], 0)
        for array in ARRAYS:
            self.assertEqual(self.read(array)[array], replacement[array])
        self.backend.put('array-test', replacement)
        self.backend.delete('array-test')
        self.assertEqual(self.physical(), [])

    def test_new_writer_delta_preserves_unchanged_array_rows_and_removes_erased_bytes(self):
        self.enable()
        original = self.document(artifacts=[self.row(secret='erase-old')],
            evidence=[self.row(value='unchanged-evidence')], verifications=[self.row(value='unchanged-verification')])
        self.backend.put('array-test', original)
        before = {r[1]:r for r in self.physical()}
        changed = self.document(artifacts=[self.row(value='redacted'), self.row('append')],
            evidence=original['evidence'], verifications=original['verifications'])
        self.backend.put('array-test', changed)
        after = {r[1]:r for r in self.physical()}
        for payload, physical in before.items():
            if 'unchanged-' in payload:
                self.assertEqual(after[payload], physical)
        self.assertFalse(any('erase-old' in payload for payload in after))
        for array in ARRAYS:
            self.assertEqual(self.read(array, ids=('selected','append'))[array], changed[array])
        # Truncation and a non-object replacing a formerly indexed ordinal
        # must remove that array's raw bytes without erasing a peer array.
        self.backend.put('array-test', self.document(artifacts=[None], evidence=original['evidence']))
        self.assertEqual(self.read()['artifacts'], [])
        self.assertEqual(self.read('verifications')['verifications'], [])
        self.assertEqual(self.read('evidence')['evidence'], original['evidence'])
        self.assertEqual(len(self.physical()), 1)

    def test_document_and_all_projected_arrays_roll_back_together(self):
        self.enable()
        original = self.document(**{name:[self.row(value='before-'+name)] for name in ARRAYS})
        self.backend.put('array-test', original)
        before = self.physical()
        with self.assertRaisesRegex(RuntimeError, '^rollback$'):
            with self.backend._connection() as connection:
                with connection.cursor() as cursor:
                    self.backend._lock(cursor, 'array-test')
                    self.backend._upsert(cursor, 'array-test', self.document())
                    raise RuntimeError('rollback')
        self.assertEqual(self.backend.get('array-test'), original)
        self.assertEqual(self.physical(), before)
        for array in ARRAYS:
            self.assertEqual(self.read(array)[array], original[array])

    def test_malformed_envelopes_unsupported_identities_and_oversized_keys_fall_back(self):
        self.enable()
        for value in (self.document(artifacts={}), self.document(evidence={}),
                      self.document(unexpected=True), {**self.document(), 'schema_version':1.0}):
            with self.subTest(value=value):
                self.backend.put('array-test', value)
                self.assertEqual(self.point(), value)
        for identity in (1000.0, 'x'*12800,
                         ''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(200))):
            row = self.row(identity, value='canonical')
            self.backend.put('array-test', self.document(artifacts=[row]))
            self.assertEqual(self.backend.get('array-test')['artifacts'], [row])
            # Numeric identities keep the original JSON-text comparison.
            self.assertEqual(self.read(ids=(str(identity),))['artifacts'], [row])
            self.assertEqual(self.physical(), [])
        self.assertFalse(self.enable('n'*2500))

    def test_readonly_and_callback_constructors_never_enable_or_rebuild(self):
        readonly = PostgresStateStore(self.backend.dsn, connection_pool=self.backend._pool, initialize=False)
        self.addCleanup(readonly.close)
        with patch.object(readonly, '_connection', side_effect=AssertionError('undeclared write')):
            self.assertFalse(self.enable(backend=readonly))
            ArtifactEvidenceStore(readonly)
        def callback(state):
            self.assertFalse(self.enable())
            ArtifactEvidenceStore(self.backend)
            return state
        self.backend.update('callback-test', callback, default={})
        with self.backend._read_connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM codex_state_metadata '
                "WHERE key LIKE 'document-array-projection%'").fetchone()[0], 0)

    def test_v1_records_and_v2_arrays_coexist_without_global_layout_upgrade(self):
        self.backend.enable_document_record_projection('v1-test')
        self.backend.put('v1-test', {'schema_version':'1.1','records':[self.row(value='v1')],
                                     'requests':[], 'events':[]})
        self.enable()
        self.backend.put('array-test', self.document(evidence=[self.row(value='v2')]))
        with self.backend._read_connection() as connection:
            markers = dict(connection.execute('SELECT key,value FROM codex_state_metadata').fetchall())
            headers = dict(connection.execute('SELECT namespace,version FROM codex_document_record_headers').fetchall())
        self.assertEqual(markers['schema_version'], '1')
        self.assertEqual(markers['document-record-projection-version'], '1')
        self.assertEqual(markers['document-array-projection-format'], '2')
        self.assertEqual(headers, {'v1-test':1, 'array-test':2})
        self.assertEqual(self.backend.document_array_item_envelope('v1-test','records','selected',
            organization_id='local',workspace_id='default',collection_names=('records','requests','events'))['records'],
            [self.row(value='v1')])
        self.assertEqual(self.read('evidence')['evidence'], [self.row(value='v2')])

    def test_unknown_optional_format_is_not_overwritten_or_used(self):
        self.enable()
        self.backend.put('array-test', self.document(artifacts=[self.row(value='before')]))
        with self.backend._connection() as connection:
            connection.execute("UPDATE codex_state_metadata SET value='999' "
                               "WHERE key='document-array-projection-format'")
        # Both an already-enabled owner and a newly initialized owner must
        # preserve an imported marker they cannot interpret.
        self.assertFalse(self.enable())
        owner = PostgresStateStore(self.backend.dsn, connection_pool=self.backend._pool)
        self.addCleanup(owner.close)
        self.assertFalse(self.enable(backend=owner))
        self.backend.put('array-test', self.document(artifacts=[self.row(value='after')]))
        self.assertEqual(self.physical(), [])
        self.assertEqual(self.point()['artifacts'], [self.row(value='after')])
        with self.backend._read_connection() as connection:
            self.assertEqual(connection.execute("SELECT value FROM codex_state_metadata "
                "WHERE key='document-array-projection-format'").fetchone()[0], '999')

    def test_selected_domain_records_still_require_complete_canonical_models(self):
        from pydantic import ValidationError
        store = ArtifactEvidenceStore(self.backend)
        # The optional SQL index knows only selection headers. It cannot make
        # an incomplete selected domain record valid or change its schema.
        self.backend.put(store.namespace, self.document(artifacts=[self.row()]))
        with self.assertRaises(ValidationError):
            store.artifact_state('selected', organization_id='local', workspace_id='default')
        self.backend.put(store.namespace, {**self.document(), 'schema_version':'999.0'})
        with self.assertRaises(ValueError):
            store.artifact_state('selected', organization_id='local', workspace_id='default')

    def test_canonical_domain_getters_keep_first_points_and_verification_work_ambiguity(self):
        from codex_web.artifact_evidence import Artifact, Evidence, Verification
        from codex_web.identity import AuthenticationActor
        from codex_web.services.artifact_evidence import ArtifactEvidenceService, VerificationNotFoundError
        actor = AuthenticationActor(identity_id='reader', principal_kind='human', organization_id='local',
                                    workspace_id='default', assurance='mfa')
        store = ArtifactEvidenceStore(self.backend)
        service = ArtifactEvidenceService(store)
        common = dict(id='selected', organization_id='local', workspace_id='default')
        artifact = Artifact(**common, artifact_type='report', name='first', producer_identity_id='producer')
        proof = Evidence(**common, evidence_type='review', result='pass', producer_identity_id='producer')
        verification = Verification(**common, verifier_identity_id='verifier', independent=True,
                                    artifact_ids=('selected',), method='synthetic', result='verified',
                                    work_item_ref='group/project#1')
        document = self.document(artifacts=[artifact.model_dump(mode='json'),
            artifact.model_copy(update={'name':'later'}).model_dump(mode='json')],
            evidence=[proof.model_dump(mode='json'),
            proof.model_copy(update={'result':'fail'}).model_dump(mode='json')],
            verifications=[verification.model_dump(mode='json'),
            verification.model_copy(update={'work_item_ref':'group/project#2'}).model_dump(mode='json')])
        self.backend.put(store.namespace, document)
        with patch.object(store, 'load', side_effect=AssertionError('unexpected full domain load')):
            self.assertEqual(service.get_artifact('selected', actor=actor).name, 'first')
            self.assertEqual(tuple(p.result for p in service.get_evidence_batch(
                ('selected','selected'), actor=actor)), ('pass','pass'))
            with self.assertRaises(VerificationNotFoundError):
                service.get_verification('selected', actor=actor)
            self.assertEqual(service.get_verification('selected', actor=actor,
                work_item_ref='group/project#1').work_item_ref, 'group/project#1')
        # Two complete same-Work rows remain ambiguous too; no LIMIT/FIRST is
        # allowed for the Verification array in either backend or typed owner.
        document['verifications'][1]['work_item_ref'] = 'group/project#1'
        self.backend.put(store.namespace, document)
        with self.assertRaises(VerificationNotFoundError):
            service.get_verification('selected', actor=actor, work_item_ref='group/project#1')

    def test_projection_metadata_roundtrips_exactly_through_fresh_storage_seed(self):
        self.enable()
        self.backend.put('array-test', self.document(evidence=[self.row(value='authoritative')]))
        target_case = unittest.TestCase()
        fixtures.PostgresAutonomyAppendTests.setUp(target_case)
        self.addCleanup(target_case.doCleanups)
        target = target_case.backend
        def digest(store):
            value = hashlib.sha256()
            for chunk in store.iter_storage_snapshot():
                value.update(chunk)
            return value.hexdigest()
        expected = digest(self.backend)
        target.restore_storage_snapshot(self.backend.iter_storage_snapshot())
        self.assertEqual(digest(target), expected)
        self.assertTrue(self.enable(backend=target))
        self.assertEqual(digest(target), expected)
        self.assertEqual(self.read('evidence', backend=target)['evidence'],
                         [self.row(value='authoritative')])

    def test_reader_observes_one_snapshot_during_uncommitted_new_writer(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.enable()
        original = self.document(artifacts=[self.row(deny=False)], evidence=[self.row(value='old')])
        changed = self.document(artifacts=[self.row(deny=True)], evidence=[self.row(value='new')])
        self.backend.put('array-test', original)
        prepared, release = Event(), Event()
        def writer():
            with self.backend._connection() as connection:
                with connection.cursor() as cursor:
                    self.backend._lock(cursor, 'array-test')
                    self.backend._upsert(cursor, 'array-test', changed)
                    prepared.set()
                    if not release.wait(10):
                        raise AssertionError('writer not released')
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            try:
                self.assertTrue(prepared.wait(10))
                for array in ARRAYS:
                    self.assertEqual(self.read(array)[array], original[array])
            finally:
                release.set()
            future.result(timeout=10)
        for array in ARRAYS:
            self.assertEqual(self.read(array)[array], changed[array])

    def test_seed_locks_canonical_source_before_deriving_raw_array_rows(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        self.backend.put('array-test', self.document(**{
            name:[self.row(secret='erase-'+name)] for name in ARRAYS}))
        entered, release = Event(), Event()
        prepare = projection.prepare_v2
        def paused(cursor, namespace, serialized):
            entered.set()
            if not release.wait(10):
                raise AssertionError('seed not released')
            return prepare(cursor, namespace, serialized)
        def redact():
            with self.backend._connection() as connection:
                connection.execute('UPDATE codex_state_documents SET payload=%s WHERE namespace=%s',
                    (json.dumps(self.document()), 'array-test'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(projection, 'prepare_v2', paused):
                seeded = pool.submit(self.enable)
                try:
                    self.assertTrue(entered.wait(10))
                    with self.backend._connection() as connection:
                        with self.assertRaises(Exception) as refused:
                            with connection.transaction():
                                connection.execute('SELECT payload FROM codex_state_documents '
                                    'WHERE namespace=%s FOR UPDATE NOWAIT', ('array-test',))
                        self.assertEqual(getattr(refused.exception,'sqlstate',None), '55P03')
                    redacted = pool.submit(redact)
                finally:
                    release.set()
                self.assertTrue(seeded.result(timeout=10))
                redacted.result(timeout=10)
        self.assertEqual(self.physical(), [])
        self.assertEqual(self.point()['artifacts'], [])

    def test_old_initializer_reader_and_writer_use_pinned_old_projection_helper(self):
        directory = os.environ.get('CODEX_WEB_TEST_LEGACY_PROJECTION_SOURCE')
        if not directory:
            self.skipTest('Pinned old PostgreSQL source pair supplied by private compatibility qualification')
        def load(name, filename):
            spec = importlib.util.spec_from_file_location(name, Path(directory)/filename)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        old_helper = load('private_v1_document_projection', 'postgres_document_projection.py')
        old_store = load('private_v1_postgres_state', 'postgres_state.py')
        old_store.postgres_document_projection = old_helper
        self.assertIsNot(old_helper, projection)
        self.enable()
        self.backend.put('array-test', self.document(**{
            name:[self.row(secret='erase-'+name)] for name in ARRAYS}))
        old = old_store.PostgresStateStore(self.backend.dsn, connection_pool=self.backend._pool)
        self.addCleanup(old.close)
        # The old optional index reader cannot interpret version 2; its ordinary
        # canonical document reader must still resolve all three current arrays.
        for array in ARRAYS:
            self.assertEqual(self.point(array, backend=old), self.point(array))
        old.put('array-test', self.document(artifacts=[self.row(value='old writer')]))
        self.assertEqual(self.physical(), [])
        self.assertEqual(self.point()['artifacts'], [self.row(value='old writer')])
        with self.backend._read_connection() as connection:
            markers = dict(connection.execute('SELECT key,value FROM codex_state_metadata').fetchall())
        self.assertEqual(markers['document-record-projection-version'], '1')
        self.assertEqual(markers['document-array-projection-format'], '2')
