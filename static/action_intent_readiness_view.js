import { esc } from './reference_links.js';

function sha(value) {
  return typeof value === 'string' && /^[a-f0-9]{40}$/i.test(value)
    ? esc(value.toLowerCase()) : 'not recorded';
}

export function readinessRequestHtml(intent) {
  const number = Number(intent.request?.parameters?.pull_request_number);
  return `<small>Review target PR ${Number.isSafeInteger(number) && number > 0 ? number : 'not recorded'} · expected head: ${sha(intent.request?.parameters?.expected_head_sha)}. Independent approval and merge gates still apply.</small>`;
}

export function readinessReceiptHtml(result) {
  return `<small>Readiness receipt head: ${sha(result.output?.head_sha)} · target branch: ${esc(result.output?.base_ref || 'not recorded')}. Read the independent verification receipt before claiming readiness.</small>`;
}

export function readinessVerificationHtml(provider) {
  return (provider.evidence || []).filter(e => e.evidence_type === 'code-host-pull-request-ready')
    .map(e => `<small>Readiness verification: expected head ${sha(e.metadata?.expected_head_sha)} · observed head ${sha(e.metadata?.observed_head_sha)} · provider ready: ${e.metadata?.ready_for_review === true ? 'yes' : e.metadata?.ready_for_review === false ? 'no' : 'unknown'}</small>`).join('');
}
