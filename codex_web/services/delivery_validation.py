"""Use the repository's canonical selection and qualification protocol gates."""
from datetime import datetime

from scripts.ci.check_gate import OWNERS, validate, expected_functional_shard_names
from codex_web.services.publication_recovery import PublicationRecoveryConflict


# GitHub CodeQL default-setup v1 aliases are provider protocol normalization,
# not a separately maintained operational language/requirement catalog.
def expected_security_job_names(configuration):
    if not configuration or configuration.get('protocol_version') != 'github-codeql-default-setup/1':
        raise PublicationRecoveryConflict('Security configuration protocol is missing or incompatible')
    languages = configuration.get('languages')
    if (configuration.get('state') != 'configured' or not isinstance(languages, list) or not languages
            or any(not isinstance(language, str) or not language for language in languages)
            or len(languages) != len(set(languages)) or not configuration.get('updated_at')
            or configuration.get('query_suite') not in {'default', 'extended'}):
        raise PublicationRecoveryConflict('Security configuration is unavailable, disabled or unknown')
    try:
        observed_update = datetime.fromisoformat(configuration['updated_at'].replace('Z', '+00:00'))
        if observed_update.tzinfo is None:
            raise ValueError('Timezone missing')
    except (ValueError, TypeError, AttributeError) as exc:
        raise PublicationRecoveryConflict('Security configuration update provenance is invalid') from exc
    aliases = {'javascript': 'javascript-typescript', 'typescript': 'javascript-typescript'}
    supported = {'actions', 'c-cpp', 'csharp', 'go', 'java-kotlin', 'javascript-typescript',
                 'python', 'ruby', 'rust', 'swift'}
    normalized = {aliases.get(language, language) for language in languages}
    if normalized - supported:
        raise PublicationRecoveryConflict('Security language configuration is unsupported')
    return {f'Analyze ({language})' for language in normalized}


def validate_delivery_runs(runs, revision, *, recovery_required):
    by_path = {r.workflow_path: r for r in runs}
    if len(by_path) != len(runs):
        raise PublicationRecoveryConflict('Workflow provenance is ambiguous')
    tests = by_path.get('.github/workflows/tests.yml')
    if tests is None or tests.selection_full is None or tests.selection_revision != revision:
        raise PublicationRecoveryConflict('Exact successful test selection/applicability proof is missing')
    names = {c.name: c for c in tests.checks}
    try:
        expected_shards = expected_functional_shard_names()
    except ValueError as exc:
        raise PublicationRecoveryConflict(str(exc)) from exc
    required = {*OWNERS, 'chromium', 'validation-selection', 'focused-documentation'}
    if len(names) != len(tests.checks) or required - names.keys() or names.keys() - required - expected_shards:
        raise PublicationRecoveryConflict('Required selected validation jobs are missing or ambiguous')
    if recovery_required and (tests.event != 'push' or tests.head_branch != 'main' or not tests.selection_full):
        raise PublicationRecoveryConflict('Exact full main validation trigger/applicability evidence is missing')
    full = tests.selection_full
    needs = {'changes': {'result': names['validation-selection'].conclusion, 'outputs': {'full': 'true' if full else 'false'}},
             'documentation': {'result': names['focused-documentation'].conclusion}}
    for check, owner in OWNERS.items():
        if names[check].state != 'completed' or names[check].conclusion != 'success':
            raise PublicationRecoveryConflict(f'Required check {check} is pending, failed or cancelled')
        needs[owner] = {'result': names[check].conclusion}
    shards = [c for c in tests.checks if c.name.startswith('chromium-functional-shard-')]
    if full and {c.name for c in shards} != expected_shards:
        raise PublicationRecoveryConflict('Complete selected functional validation shard set is missing')
    expected_result = 'success' if full else 'skipped'
    if any(c.state != 'completed' or c.conclusion != expected_result for c in shards):
        raise PublicationRecoveryConflict('Selected functional shard failed or applicability is unproven')
    # Aggregate success follows only a complete, independently successful matrix.
    needs['functional-shards'] = {'result': expected_result}
    security = by_path.get('dynamic/github-code-scanning/codeql')
    configuration = security.security_configuration if security else None
    expected_security = expected_security_job_names(configuration)
    security_result = security.security_result
    if tests.event == 'pull_request' and security_result is None:
        raise PublicationRecoveryConflict('Required native PR CodeQL result evidence is missing')
    if security_result is not None and (security_result.get('protocol_version') != 'github-codeql-check/1'
            or security_result.get('issuer') != 'github-advanced-security'
            or security_result.get('revision') != revision or security_result.get('state') != 'completed'
            or security_result.get('conclusion') != 'success'):
        raise PublicationRecoveryConflict('Native CodeQL result is pending, failed, cancelled or incompatible')
    if recovery_required and (security.event != 'dynamic' or security.head_branch != 'main'):
        raise PublicationRecoveryConflict('Exact main security workflow trigger evidence is missing')
    security_names = [c.name for c in security.checks]
    if (len(security_names) != len(set(security_names))
            or set(security_names) != expected_security):
        raise PublicationRecoveryConflict('Complete configured security job set is missing or ambiguous')
    try:
        validate('chromium', needs)
    except ValueError as exc:
        raise PublicationRecoveryConflict(f'Canonical selection/applicability gate denied: {exc}') from exc
    release = by_path.get('.github/workflows/publish-release.yml')
    if recovery_required and release is None:
        raise PublicationRecoveryConflict('Required recovery publication is missing')
    retained = []
    for run in runs:
        if run.revision != revision or run.state != 'completed' or run.conclusion != 'success':
            raise PublicationRecoveryConflict(f'Workflow {run.workflow_path} is pending, failed or cancelled')
        if run is release and recovery_required:
            release_names = {c.name: c for c in run.checks}
            if len(release_names) != len(run.checks) or set(release_names) != {'immutable-recovery-candidate', 'qualified-release-promotion'}:
                raise PublicationRecoveryConflict('Recovery publication job set is missing or ambiguous')
            candidate = release_names.get('immutable-recovery-candidate')
            promotion = release_names.get('qualified-release-promotion')
            if (run.event != 'workflow_run' or run.head_branch != 'main' or candidate is None or promotion is None
                    or candidate.state != 'completed' or candidate.conclusion != 'success'
                    or not run.recovery_publication or run.recovery_publication.get('commit') != revision):
                raise PublicationRecoveryConflict('Exact main recovery publication/qualification is missing or unproven')
        for check in run.checks:
            inapplicable = (run is tests and tests.selection_revision == revision and
                ((check.name == 'focused-documentation' and full) or
                 (check.name.startswith('chromium-functional-shard-') and not full))) or (
                run is release and run.event == 'workflow_run' and check.name == 'qualified-release-promotion')
            if (check.revision != revision or check.state != 'completed' or
                    (check.conclusion != 'success' and not (check.conclusion == 'skipped' and inapplicable))):
                raise PublicationRecoveryConflict(f'Job {check.name} has no successful or proven-inapplicable qualification')
            retained.append({**check.model_dump(mode='json'), 'workflow_run_id': run.external_id,
                'workflow_path': run.workflow_path, 'workflow_event': run.event,
                'control_revision': run.control_revision, 'tested_revision': run.tested_revision,
                'selection_full': run.selection_full, 'security_result': run.security_result, 'security_configuration': run.security_configuration, 'recovery_publication': run.recovery_publication, 'inapplicable': check.conclusion == 'skipped'})
    return retained
