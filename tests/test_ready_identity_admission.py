import unittest

from codex_web.identity import (IdentityState, SessionRecord, ServiceTokenRecord,
    HumanIdentity, Membership, MembershipRole)
from codex_web.services.code_host_readiness import ReadyIdentityObservation


class ReadyIdentityAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = IdentityState(
            humans=[HumanIdentity(id='human', display_name='Fixture')],
            memberships=[Membership(identity_id='human', principal_kind='human',
                organization_id='org', workspace_id='workspace')],
            sessions=[SessionRecord(id='session', identity_id='human', organization_id='org',
                workspace_id='workspace', assurance='primary', session_token_hash='a'*64,
                refresh_token_hash='b'*64, csrf_token_hash='c'*64,
                created_at=0, last_seen_at=100, idle_expires_at=200, absolute_expires_at=300)],
            service_tokens=[ServiceTokenRecord(service_identity_id='worker', organization_id='org',
                workspace_id='workspace', token_hash='d'*64, scopes=['execution-worker:run'],
                created_at=0, expires_at=250)])
        self.pin = ReadyIdentityObservation(self.state, 100)

    def test_only_activity_and_monotonic_idle_extension_match(self):
        current = self.state.model_copy(deep=True)
        current.sessions[0].last_seen_at = 130
        current.sessions[0].idle_expires_at = 240
        current.service_tokens[0].last_used_at = 130
        self.assertTrue(self.pin.matches(current, now=130))
        self.assertFalse(self.pin.matches(current, now=200))
        self.assertFalse(self.pin.matches(current, now=201))

    def test_shortening_absolute_expiry_and_session_revocation_do_not_match(self):
        for field, value in [('idle_expires_at', 190), ('absolute_expires_at', 290),
                             ('revoked_at', 110), ('rotation', 1), ('step_up_until', 180)]:
            current = self.state.model_copy(deep=True)
            setattr(current.sessions[0], field, value)
            with self.subTest(field=field):
                self.assertFalse(self.pin.matches(current, now=120))

    def test_membership_disable_policy_and_token_authority_stay_exact(self):
        mutations = [lambda s: setattr(s.humans[0], 'disabled_at', 110),
                     lambda s: setattr(s.memberships[0], 'roles', [MembershipRole.ADMIN]),
                     lambda s: setattr(s.memberships[0], 'revoked_at', 110),
                     lambda s: setattr(s.service_tokens[0], 'revoked_at', 110),
                     lambda s: setattr(s.service_tokens[0], 'scopes', ['different']),
                     lambda s: setattr(s.service_tokens[0], 'expires_at', 240)]
        for mutate in mutations:
            current = self.state.model_copy(deep=True)
            mutate(current)
            self.assertFalse(self.pin.matches(current, now=120))

    def test_already_expired_or_revoked_sessions_cannot_extend(self):
        for values in [{'idle_expires_at':90}, {'revoked_at':90}]:
            previous = self.state.model_copy(deep=True)
            for field, value in values.items():
                setattr(previous.sessions[0], field, value)
            pin = ReadyIdentityObservation(previous, 100)
            self.assertTrue(pin.matches(previous.model_copy(deep=True), now=120))
            current = previous.model_copy(deep=True)
            current.sessions[0].idle_expires_at = 240
            self.assertFalse(pin.matches(current, now=120))

    def test_absolute_step_up_and_token_expiry_during_checkpoint_refuse(self):
        for deadline in ['absolute', 'step_up', 'token']:
            previous = self.state.model_copy(deep=True)
            if deadline == 'absolute':
                previous.sessions[0].absolute_expires_at = 150
                previous.sessions[0].idle_expires_at = 150
            elif deadline == 'step_up':
                previous.sessions[0].step_up_until = 150
            else:
                previous.service_tokens[0].expires_at = 150
            pin = ReadyIdentityObservation(previous, 100)
            self.assertFalse(pin.matches(previous.model_copy(deep=True), now=150))

    def test_invalid_time_or_removed_session_cannot_match(self):
        for now in [99, float('nan'), float('inf')]:
            self.assertFalse(self.pin.matches(self.state.model_copy(deep=True), now=now))
        current = self.state.model_copy(deep=True)
        current.sessions = []
        self.assertFalse(self.pin.matches(current, now=120))
        current = self.state.model_copy(deep=True)
        current.sessions[0].idle_expires_at = float('nan')
        self.assertFalse(self.pin.matches(current, now=120))
