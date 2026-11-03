"""Request authentication survives canonical accepted-recipient revalidation."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from starlette.requests import Request

from codex_web.identity import (
    AuthenticationAssurance, HumanIdentity, IdentityState, Membership,
    MembershipRole, Organization, PrincipalKind, Workspace,
)
from codex_web.services.identity import (
    AuthenticationError, AuthorizationError, IdentityService, TenantIsolationError,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_accepted_unbound_recipient_replacement as lineage


class AcceptedReplacementAuthenticatedOriginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = lineage.AcceptedUnboundRecipientReplacementTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        actor = self.f.f.f.actor
        self.identities = IdentityStateStore(self.f.f.f.env.env.state)
        self.identities.save(IdentityState(
            organizations=[Organization(id=actor.organization_id, name='Private organization')],
            workspaces=[Workspace(id=actor.workspace_id, organization_id=actor.organization_id,
                name='Private workspace')],
            humans=[HumanIdentity(id=actor.identity_id, display_name='Private operator')],
            memberships=[Membership(identity_id=actor.identity_id,
                principal_kind=PrincipalKind.HUMAN, organization_id=actor.organization_id,
                workspace_id=actor.workspace_id, roles=list(actor.roles))]))
        self.identity = IdentityService(self.identities)
        # Real private session creation + authentication, never model_copy(MFA).
        credentials = self.identity.create_session(identity_id=actor.identity_id,
            scope=actor.tenant, assurance=AuthenticationAssurance.MFA)
        self.actor = self.identity.authenticate_session(credentials.session_token, touch=False).actor

    async def context(self):
        state, _ = await self.f.terminal()
        service, project, recovery = self.f.replacement_service(state)
        self.f.n.routing.actor_resolver = lambda identity, project: self.identity.actor_for_identity(
            identity, scope=self.actor.tenant)
        # Attribute injection also works on OLD code, which ignores it and
        # faithfully fails at its credential-free PRIMARY -> MFA check.
        self.f.n.routing.authenticated_origin_revalidator = self.identity.revalidate_authenticated_origin
        return state, service, project, recovery

    def change_session(self, **changes):
        def apply(state):
            state.sessions = [s.model_copy(update=changes) if s.id == self.actor.session_id else s
                for s in state.sessions]
            return state
        self.identities.update(apply)

    async def test_real_mfa_session_replaces_cancelled_recipient_without_losing_origin(self):
        state, service, project, recovery = await self.context()
        self.assertEqual(self.identity.actor_for_identity(self.actor.identity_id,
            scope=self.actor.tenant).assurance, AuthenticationAssurance.PRIMARY)
        result, error = None, None
        try:
            result = await service.replace(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=self.actor, project=project)
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        self.assertIsNone(error, error)
        self.assertEqual(result['newThreadId'], 'successor-thread')
        self.assertEqual(len(self.f.new_calls), 1)
        self.assertEqual(recovery.replacement_thread_id(state.handoff.delivery.thread_id), 'successor-thread')
        self.assertEqual(self.identity.revalidate_authenticated_origin(self.actor).session_id, self.actor.session_id)

    async def test_expired_revoked_and_stepup_expired_sessions_refuse_before_creation(self):
        state, service, project, _ = await self.context()
        original = self.identities.load()
        for changes in ({'revoked_at': time.time()}, {'idle_expires_at': 0},
                {'created_at': time.time() - 120, 'last_seen_at': time.time() - 90,
                 'absolute_expires_at': time.time() - 60},
                {'assurance': AuthenticationAssurance.PRIMARY, 'step_up_until': time.time() - 1}):
            with self.subTest(changes=tuple(changes)):
                self.identities.save(original)
                self.change_session(**changes)
                with self.assertRaises(AuthenticationError):
                    service.replacement_eligibility(state.handoff.delivery.thread_id,
                        work_item_ref=state.ref, actor=self.actor, project=project)
                self.assertEqual(self.f.new_calls, [])

    async def test_current_membership_disabled_identity_and_tenant_drift_refuse(self):
        state, service, project, _ = await self.context()
        original = self.identities.load()
        for kind in ('membership', 'disabled', 'tenant', 'roles', 'teams'):
            with self.subTest(kind=kind):
                changed = original.model_copy(deep=True)
                if kind == 'membership':
                    changed.memberships[0].revoked_at = time.time()
                elif kind == 'disabled':
                    changed.humans[0].disabled_at = time.time()
                elif kind == 'tenant':
                    changed.sessions[0].workspace_id = 'foreign-workspace'
                elif kind == 'roles':
                    changed.memberships[0].roles = [MembershipRole.MEMBER]
                else:
                    changed.memberships[0].team_ids = ['new-team']
                self.identities.save(changed)
                with self.assertRaises(TenantIsolationError if kind == 'membership' else AuthenticationError):
                    service.replacement_eligibility(state.handoff.delivery.thread_id,
                        work_item_ref=state.ref, actor=self.actor, project=project)
                self.assertEqual(self.f.new_calls, [])

    async def test_primary_and_forged_credential_free_mfa_are_not_elevated(self):
        state, service, project, _ = await self.context()
        credentials = self.identity.create_session(identity_id=self.actor.identity_id,
            scope=self.actor.tenant, assurance=AuthenticationAssurance.PRIMARY)
        primary = self.identity.authenticate_session(credentials.session_token, touch=False).actor
        with self.assertRaises(AuthorizationError):
            service.replacement_eligibility(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=primary, project=project)
        forged = self.identity.actor_for_identity(self.actor.identity_id, scope=self.actor.tenant).model_copy(
            update={'assurance': AuthenticationAssurance.MFA})
        with self.assertRaises(AuthenticationError):
            service.replacement_eligibility(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=forged, project=project)
        self.assertEqual(self.f.new_calls, [])

    async def test_postawait_revocation_cleans_successor_and_does_not_publish_graph(self):
        state, service, project, recovery = await self.context()
        self.f.during_create = lambda: self.change_session(revoked_at=time.time())
        with self.assertRaises(AuthenticationError):
            await service.replace(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=self.actor, project=project)
        self.assertEqual(self.f.cancelled_new, ['successor-assignment'])
        self.assertIsNone(recovery.replacement_thread_id(state.handoff.delivery.thread_id))
        self.assertEqual(self.f.f.f.env.states.get(state.ref).handoff.requested_at, self.f.nonce)

    async def test_missing_revalidator_fails_closed_without_delegated_fallback(self):
        state, service, project, _ = await self.context()
        self.f.n.routing.authenticated_origin_revalidator = None
        with self.assertRaisesRegex(HTTPException, 'owner_replacement_authenticated_origin_unavailable'):
            service.replacement_eligibility(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=self.actor, project=project)
        self.assertEqual(self.f.new_calls, [])

    async def test_get_and_post_map_real_identity_errors_to_http_denials(self):
        from codex_web.api.turns import build_turns_router
        state, service, project, _ = await self.context()
        router = build_turns_router(service, SimpleNamespace())
        get = next(r.endpoint for r in router.routes if r.path.endswith('/replacement-eligibility'))
        post = next(r.endpoint for r in router.routes if r.path.endswith('/replace'))
        request = Request({'type': 'http', 'method': 'GET', 'path': '/',
            'headers': [], 'state': {'identity_actor': self.actor, 'thread_project': project}})
        original = self.identities.load()
        for status in (401, 403):
            self.identities.save(original)
            if status == 401:
                self.change_session(revoked_at=time.time())
                request.state.identity_actor = self.actor
            else:
                credentials = self.identity.create_session(identity_id=self.actor.identity_id,
                    scope=self.actor.tenant, assurance=AuthenticationAssurance.PRIMARY)
                request.state.identity_actor = self.identity.authenticate_session(credentials.session_token,
                    touch=False).actor
            for endpoint in (get, post):
                with self.subTest(status=status, endpoint=endpoint.__name__):
                    with self.assertRaises(HTTPException) as raised:
                        await endpoint(state.handoff.delivery.thread_id, request, work_item_ref=state.ref)
                    self.assertEqual(raised.exception.status_code, status)
        self.assertEqual(self.f.new_calls, [])


class LocalTrustedReplacementOriginTests(unittest.TestCase):
    def test_real_local_trusted_origin_reaches_business_guard_without_assurance_copy(self):
        from codex_web.services.owner_routing import OwnerRoutingService
        with tempfile.TemporaryDirectory() as directory:
            identity = IdentityService(IdentityStateStore(SQLiteStateStore(Path(directory) / 'identity.sqlite')))
            actor = identity.local_trusted_actor()
            routing = object.__new__(OwnerRoutingService)
            routing.actor_resolver = lambda identity_id, project: identity.actor_for_identity(identity_id,
                scope=actor.tenant)
            routing.authenticated_origin_revalidator = identity.revalidate_authenticated_origin
            class BusinessGuardReached(Exception):
                pass
            def get_state(_):
                raise BusinessGuardReached()
            routing.get_state = get_state
            with self.assertRaises(BusinessGuardReached):
                routing.accepted_replacement_context('thread', 'work', actor=actor,
                    project=SimpleNamespace())
