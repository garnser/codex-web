import { esc } from './reference_links.js';

function mergeSha(value) {
  return typeof value === 'string' && /^[a-f0-9]{40}$/i.test(value)
    ? esc(value.toLowerCase()) : 'not recorded';
}

export function readyRequestHtml(intent) {
    const params = intent.request?.parameters || {};
    const number = Number.isSafeInteger(params.pull_request_number) && params.pull_request_number > 0 ? params.pull_request_number : 'not recorded';
    const origin = intent.readiness_origin;
    return `<small>Ready for review · PR ${number} · expected head ${mergeSha(params.expected_head_sha)} · expected base ${mergeSha(params.expected_base_sha)}</small>
      <small>Repository ${origin ? esc(origin.repository) : 'not recorded'} · base ref ${params.expected_base_ref ? esc(params.expected_base_ref) : 'not recorded'}</small>
      <small>Canonical readiness origin: ${origin ? `${esc(origin.assignment_id)} · fence ${esc(origin.fence)} · worker ${esc(origin.worker_id)} · Definition ${esc(origin.execution_profile_definition?.definition_id)}@${esc(origin.execution_profile_definition?.revision)}` : 'not recorded; do not infer or adopt ownership'}.</small>
      <small>Requires explicit current execution-profile declaration and canonical readiness authority. Inspect the guarded worker broker result; this inspector grants no execution permission. No approval or merge is requested.</small>`;
}

export function readyReceiptHtml(result) {
  return `<small>Readiness receipt · expected head ${mergeSha(result.output?.expected_head_sha)} · draft ${result.output?.draft === false ? 'false' : 'not confirmed'} · mutation ${result.output?.provider_mutation_performed === true ? 'performed' : result.output?.read_only_reconciliation ? 'not replayed; read-only reconciliation' : 'not recorded'}. Independent verification remains required.</small>`;
}

export function readyVerificationHtml(provider) {
  return (provider.evidence || []).filter(e => e.evidence_type === 'code-host-pull-request-ready').map(e => `<small>Readiness verification · expected head ${mergeSha(e.metadata?.expected_head_sha)} · observed head ${mergeSha(e.metadata?.observed_head_sha)} · draft ${e.metadata?.draft === false ? 'false' : 'not confirmed'} · verified ${provider.verified === true ? 'yes' : 'no'}. This does not establish merge, CI qualification or installed acceptance.</small>`).join('');
}
