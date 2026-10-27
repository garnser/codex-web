from __future__ import annotations

import unittest
from unittest.mock import patch

import tests.test_agent_profiles as fixtures
from codex_web.agent_profiles import (
    AgentProfileAccessMode,
    AgentProfileAccessPolicy,
    AgentProfileLifecycle,
    AgentProfileState,
)
from codex_web.services.agent_profiles import AgentProfileAccessDenied, AgentProfileNotFound


class AgentProfileResolutionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.AgentProfileTests()
        await self.fixture.asyncSetUp()
        self.seed = self.fixture._create()
        self.current = self.seed.model_copy(update={
            'revision': 2, 'record_id': 'current-revision-2', 'name': 'Current coder',
        })
        self.rows = [self.seed, self.current]
        for index in range(203):
            self.rows.append(self.seed.model_copy(update={
                'profile_id': 'unrelated-' + str(index),
                'record_id': 'unrelated-record-' + str(index),
                'organization_id': 'org-a' if index % 2 else 'foreign-org',
            }))
        self.save()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def save(self):
        self.fixture.sqlite.put('agent_profiles', AgentProfileState(revisions=self.rows).model_dump(mode='json'))

    def resolve(self, revision=None, actor=None, profile_id='coder'):
        return self.fixture.profiles.resolve_for_execution(
            profile_id, actor=actor or self.fixture.member, project_id='project-a', revision=revision,
        )

    def test_pinned_resolution_reads_205_row_catalog_once(self):
        store = self.fixture.profiles.store
        with patch.object(store, 'load', wraps=store.load) as load:
            selected, decision = self.resolve(1)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(selected, self.seed)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.profile_revision, 2)

    def test_unpinned_resolution_returns_current_from_one_read(self):
        store = self.fixture.profiles.store
        with patch.object(store, 'load', wraps=store.load) as load:
            selected, decision = self.resolve()
        self.assertEqual(load.call_count, 1)
        self.assertEqual(selected, self.current)
        self.assertTrue(decision.allowed)

    def test_next_call_rechecks_current_lifecycle_and_access(self):
        self.resolve(1)
        for changes in (
            {'lifecycle': AgentProfileLifecycle.DISABLED},
            {'access': AgentProfileAccessPolicy(mode=AgentProfileAccessMode.OWNER)},
        ):
            with self.subTest(changes=changes):
                self.rows[1] = self.current.model_copy(update=changes)
                self.save()
                with self.assertRaises(AgentProfileAccessDenied):
                    self.resolve(1)
                self.rows[1] = self.current
                self.save()
                self.assertEqual(self.resolve(1)[0], self.seed)

    def test_foreign_latest_cannot_change_current_or_supply_missing_pin(self):
        self.rows.append(self.current.model_copy(update={
            'organization_id': 'foreign-org', 'revision': 99,
            'record_id': 'foreign-same-profile', 'lifecycle': AgentProfileLifecycle.DISABLED,
        }))
        self.save()
        self.assertEqual(self.resolve()[0], self.current)
        with self.assertRaisesRegex(AgentProfileNotFound, 'revision not found'):
            self.resolve(99)
        with self.assertRaisesRegex(AgentProfileNotFound, 'profile not found'):
            self.resolve(1, actor=self.fixture.foreign)

    def test_denied_current_precedes_missing_pinned_revision(self):
        self.rows[1] = self.current.model_copy(update={'lifecycle': AgentProfileLifecycle.DISABLED})
        self.save()
        with self.assertRaises(AgentProfileAccessDenied):
            self.resolve(999)
        with self.assertRaisesRegex(AgentProfileNotFound, 'profile not found'):
            self.resolve(1, profile_id='missing')

    def test_malformed_unrelated_revision_still_fails_catalog_validation(self):
        raw = AgentProfileState(revisions=self.rows).model_dump(mode='json')
        raw['revisions'][-1]['revision'] = 'invalid-revision'
        self.fixture.sqlite.put('agent_profiles', raw)
        with self.assertRaises(ValueError):
            self.resolve(1)
