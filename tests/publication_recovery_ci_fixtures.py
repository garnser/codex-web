"""Public GitHub observations, with explicit synthetic configuration/revision substitutions.

Metadata captured from qualified main fc7b53f3 and PR 1442 on 2026-10-14.
The default-setup response was independently read by the operator at
2026-10-14T20:56:57.297806+00:00; it is metadata, not future qualification proof.
"""
import json
from pathlib import Path
from codex_web.code_hosts import CodeHostCheckFact, CodeHostValidationRunFact

ROOT = Path(__file__).parent / 'fixtures/publication_recovery'
SECURITY_CONFIGURATION = {'state': 'configured', 'languages': ['actions', 'javascript', 'javascript-typescript', 'python', 'typescript'],
                          'updated_at': '2026-10-13T14:36:19Z', 'query_suite': 'default', 'protocol_version': 'github-codeql-default-setup/1'}

def observation(run_id, suffix):
    return json.loads((ROOT / f'{run_id}-{suffix}.json').read_text())

def qualified_runs(revision, *, recovery=True):
    facts = []
    for run_id in (38627482110, 38627481555, 38628116952):
        row = observation(run_id, 'run')
        if not recovery and row['path'].endswith('publish-release.yml'):
            continue
        checks = tuple(CodeHostCheckFact(external_id=str(j['id']), name=j['name'], state=j['status'],
            conclusion=j['conclusion'], revision=revision, web_url=j['html_url'])
            for j in observation(run_id, 'jobs')['jobs'])
        tests = row['path'].endswith('tests.yml')
        security = row['path'] == 'dynamic/github-code-scanning/codeql'
        facts.append(CodeHostValidationRunFact(external_id=str(row['id']), workflow_path=row['path'],
            event=row['event'], head_branch=row['head_branch'], revision=revision, state=row['status'],
            conclusion=row['conclusion'], selection_full=True if tests else None,
            selection_revision=revision if tests else None, checks=checks,
            security_configuration=SECURITY_CONFIGURATION if security else None))
    return tuple(facts)
