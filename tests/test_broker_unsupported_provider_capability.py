import unittest
from types import SimpleNamespace

from codex_web.code_hosts import CodeHostCapability
from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError, ControlPlaneBrokerService
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider


class UnsupportedProviderCapabilityTests(unittest.TestCase):
    def resolve(self, provider, *, copies=1, api_base='https://provider.example/api'):
        binding = SimpleNamespace(enabled=True, resource_ids=('repo',), project_id='project',
                                  provider_type=provider.provider_type, provider_instance='provider.example')
        return ControlPlaneBrokerService._repository_action_binding(
            action_registry=SimpleNamespace(list_bindings=lambda actor: [binding] * copies,
                provider=lambda *args, **kwargs: SimpleNamespace(api_base=api_base)),
            actor=object(), project_id='project', repository_id='repo',
            code_host_registry=SimpleNamespace(provider=lambda provider_type: provider),
            capability=CodeHostCapability.MERGE_REQUIREMENTS_READ)

    def test_legacy_adapter_reports_unsupported_instead_of_binding_repair(self):
        class LegacyGitLab(GitLabCodeHostProvider):
            def capabilities(self):
                return super().capabilities() - {CodeHostCapability.MERGE_REQUIREMENTS_READ}
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,
                                    'provider does not support.*pull_request.merge_requirements.read') as caught:
            self.resolve(LegacyGitLab())
        self.assertNotIn('exactly one eligible', str(caught.exception))
        self.assertIn('another binding cannot implement', str(caught.exception))

    def test_gitlab_native_requirements_resolve_through_existing_binding(self):
        self.assertEqual(self.resolve(GitLabCodeHostProvider()).provider_type, 'gitlab')

    def test_absent_binding_remains_a_configuration_refusal(self):
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, 'exactly one eligible'):
            self.resolve(GitLabCodeHostProvider(), copies=0)

    def test_supported_provider_resolves_and_ambiguity_still_fails_closed(self):
        provider = GitHubCodeHostProvider()
        self.assertEqual(self.resolve(provider).provider_type, 'github')
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, 'exactly one eligible'):
            self.resolve(provider, copies=2)

    def test_supported_adapter_with_missing_api_base_is_a_configuration_refusal(self):
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, 'exactly one eligible'):
            self.resolve(GitHubCodeHostProvider(), api_base='')
