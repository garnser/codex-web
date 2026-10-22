import unittest
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, MembershipRole


class ActorAuthorityTests(unittest.TestCase):
    def test_observation_time_only_is_excluded_and_every_authority_field_is_pinned(self):
        actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.SERVICE,
            organization_id='org', workspace_id='ws', assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_token_id='token', service_scopes=('upgrade:admin',), authenticated_at=1)
        self.assertTrue(actor.has_same_authority(actor.model_copy(update={'authenticated_at': 2})))
        changes = dict(identity_id='other', principal_kind=PrincipalKind.HUMAN,
            organization_id='other', workspace_id='other', roles=(MembershipRole.ADMIN,),
            team_ids=('other',), assurance=AuthenticationAssurance.MFA, session_id='other',
            service_token_id='other', service_scopes=('recovery:admin',))
        self.assertEqual(set(changes), set(AuthenticationActor.model_fields) - {'authenticated_at'})
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertFalse(actor.has_same_authority(actor.model_copy(update={field: value})))
        self.assertFalse(actor.has_same_authority(None))
