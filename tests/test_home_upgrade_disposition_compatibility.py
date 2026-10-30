"""Forward Upgrade-state format; legacy bytes preserved, marked old reads refuse."""
import unittest
from pydantic import ValidationError
from codex_web import upgrades as domain
from codex_web.storage.upgrades import UpgradeStore

OLD_PLAN_SOURCE = 'class UpgradePlan(BaseModel):\n    model_config = ConfigDict(extra="forbid")\n\n    id: str = Field(default_factory=lambda: f"upgrade-{uuid.uuid4().hex}")\n    organization_id: str\n    workspace_id: str\n    project_id: str | None = None\n    release_id: str\n    current_app_version: str\n    target_app_version: str\n    compatibility: UpgradeCompatibilityProfile\n    observed_control_plane_versions: tuple[str, ...]\n    steps: tuple[UpgradeStep, ...]\n    require_recovery_qualification: bool = True\n    require_drain: bool = True\n    status: UpgradeStatus = UpgradeStatus.DRAFT\n    preflight: UpgradePreflight | None = None\n    maintenance_mode: bool = False\n    maintenance_scope: UpgradeMaintenanceScope = UpgradeMaintenanceScope.TENANT\n    drain_started_at: float | None = None\n    service_instance_id: str | None = None\n    covered_scopes: tuple[UpgradeHostedScope, ...] = ()\n    hosted_scope_fingerprint: str | None = None\n    admission_epoch: int = Field(default=0, ge=0)\n    deployment_source_revision: str | None = None\n    deployment_target_revision: str | None = None\n    irreversible_boundary_crossed: bool = False\n    rollback_available: bool = True\n    definition_migrations: tuple[DefinitionMigrationRecord, ...] = ()\n    pre_upgrade_backup_id: str | None = None\n    post_upgrade_evidence_id: str | None = None\n    rollback_evidence_id: str | None = None\n    created_by: str\n    created_at: float = Field(default_factory=time.time)\n    updated_at: float = Field(default_factory=time.time)\n    completed_at: float | None = None'


class HomeUpgradeDispositionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        namespace = dict(vars(domain))
        exec(compile(OLD_PLAN_SOURCE, '<actual-cf35705-UpgradePlan>', 'exec'), namespace)
        self.old = namespace['UpgradePlan']
        profile = domain.UpgradeCompatibilityProfile(source_app_version='1.0', target_app_version='2.0',
            supported_control_plane_versions_during_rollout=('1.0', '2.0'), source_state_schema_version=1,
            target_state_schema_version=1, target_definition_engine_version='1.0', target_extension_host_version='1.0')
        self.plan = self.old(organization_id='local', workspace_id='default', release_id='release-test',
            current_app_version='1.0', target_app_version='2.0', compatibility=profile,
            observed_control_plane_versions=('1.0',), steps=(), created_by='operator')

    def test_actual_old_plan_bytes_unchanged_when_diagnostic_absent(self):
        legacy = self.plan.model_dump(mode='json')
        current = domain.UpgradePlan.model_validate(legacy)
        self.assertIsNone(current.home_checkpoint_disposition)
        self.assertEqual(current.model_dump_json(), self.plan.model_dump_json())
        self.assertEqual(self.old.model_validate(current.model_dump(mode='json')), self.plan)

    def test_legacy_state_migration_is_nonauthorizing_and_unknown_version_refused(self):
        state = UpgradeStore._decode({'schema_version': '1.0', 'plans': {self.plan.id: self.plan.model_dump(mode='json')}})
        self.assertEqual(state.schema_version, '1.1')
        self.assertIsNone(state.plans[self.plan.id].home_checkpoint_disposition)
        self.assertNotIn('home_checkpoint_disposition', state.model_dump(mode='json')['plans'][self.plan.id])
        with self.assertRaises(ValueError):
            UpgradeStore._decode({'schema_version': '2.0', 'plans': {}})

    def test_actual_old_reader_rejects_populated_diagnostic_new_roundtrip_preserves_it(self):
        plan = domain.UpgradePlan.model_validate(self.plan.model_dump(mode='json'))
        plan.home_checkpoint_disposition = domain.HomeCheckpointDisposition(outcome='deferred_unknown',
            reason_code='home_checkpoint_original_outcome_unknown')
        raw = plan.model_dump(mode='json')
        self.assertEqual(domain.UpgradePlan.model_validate(raw), plan)
        with self.assertRaises(ValidationError) as refused:
            self.old.model_validate(raw)
        self.assertEqual(refused.exception.errors()[0]['type'], 'extra_forbidden')
        self.assertEqual(refused.exception.errors()[0]['loc'], ('home_checkpoint_disposition',))

    def test_public_creation_cannot_inject_home_diagnostic_or_continuation_grant(self):
        fields = {'release_id': 'release-test', 'compatibility': self.plan.compatibility.model_dump(),
            'observed_control_plane_versions': ('1.0',), 'home_checkpoint_disposition': {'outcome': 'recorded'}}
        with self.assertRaises(ValidationError) as refused:
            domain.UpgradePlanCreate.model_validate(fields)
        self.assertTrue(any(e['loc'] == ('home_checkpoint_disposition',) and e['type'] == 'extra_forbidden'
            for e in refused.exception.errors()))
        with self.assertRaises(ValidationError):
            domain.HomeCheckpointDisposition(outcome='pending_unknown', continuation_authorized=True)

    def test_existing_control_center_renders_approval_and_home_diagnostic_together(self):
        import json
        import subprocess
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'static/autonomy_control_center.js').read_text()
        # Execute the actual renderer, without imports/installation or a browser.
        source = '\n'.join(row for row in source.splitlines() if not row.startswith('import '))
        source = source.split('if (document.readyState ===', 1)[0]
        script = r'''
const assert = require('node:assert/strict');
const nodes = new Map();
const card = {querySelector(selector) {
 if (!nodes.has(selector)) nodes.set(selector, {innerHTML:'', dataset:{}, addEventListener(){}});
 return nodes.get(selector);
}, querySelectorAll(){return []}};
const procedureRunsDirty = () => false, disposeProcedureRuns = () => {}, focusReference = () => {};
const data = {approvals:{pending:[{id:'approval-live', status:'pending', target:{operation:'action.execute'}}]},
 upgrades:{recent:[{id:'upgrade-current', current_app_version:'old', target_app_version:'new', status:'draining',
 home_checkpoint_disposition:{outcome:'deferred_unknown', reason_code:'home_<script>',
 checkpoint_artifact_id:'artifact-original-checkpoint', preservation_artifact_id:'artifact-original-preservation'}}]}};
eval(SOURCE + '\naccRender(card, data);');
const approval = nodes.get('[data-acc-approvals]').innerHTML;
const upgrade = nodes.get('[data-acc-upgrades]').innerHTML;
assert.match(approval, /approval-live/);
assert.doesNotMatch(approval, /artifact-original/);
assert.match(upgrade, /deferred_unknown/);
assert.match(upgrade, /no Home continuation authorized/);
assert.match(upgrade, /home_&lt;script&gt;/);
assert.match(upgrade, /\/api\/artifacts\/artifact-original-checkpoint/);
assert.match(upgrade, /\/api\/artifacts\/artifact-original-preservation/);
assert.doesNotMatch(upgrade, /<script>/);
'''
        subprocess.run(['node', '-e', script.replace('SOURCE', json.dumps(source))],
            check=True, timeout=10, capture_output=True, text=True)
