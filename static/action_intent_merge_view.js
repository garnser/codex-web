import { esc, esc as escapeHtml } from './reference_links.js';

import { timeText, listText } from './action_intent_outcome_view.js';

const MERGE_ACTION = 'code-host.pull-request.merge';
const READY_ACTION = 'code-host.pull-request.ready';

function mergeSha(value) {
  return typeof value === 'string' && /^[a-f0-9]{40}$/i.test(value)
    ? esc(value.toLowerCase()) : 'not recorded';
}

export function mergeRequestHtml(intent) {
  if (intent.action_id === READY_ACTION) {
    const number = Number(intent.request?.parameters?.pull_request_number);
    return `<small>Review target PR ${Number.isSafeInteger(number) && number > 0 ? number : 'not recorded'} · expected head: ${mergeSha(intent.request?.parameters?.expected_head_sha)}. Independent approval and merge gates still apply.</small>`;
  }
  if (intent.action_id !== MERGE_ACTION) return '';
  return `<small>Requested merge head: ${mergeSha(intent.request?.parameters?.expected_head_sha)}. An omitted pin has no mutation-time head guarantee.</small>`;
}

export function mergeReceiptHtml(result) {
  if (result.action_id === READY_ACTION) return `<small>Readiness receipt head: ${mergeSha(result.output?.head_sha)} · target branch: ${esc(result.output?.base_ref || 'not recorded')}. Read the independent verification receipt before claiming readiness.</small>`;
  if (result.action_id !== MERGE_ACTION) return '';
  return `<small>Receipt head: ${mergeSha(result.output?.head_sha)} · merge commit: ${mergeSha(result.output?.merge_commit_sha)}. Read the verification receipt for independent provider confirmation.</small>`;
}

export function mergeVerificationHtml(provider) {
  return (provider.evidence || [])
    .filter(e => e.evidence_type === 'code-host-pull-request-merge')
    .map(e => `<small>Merge verification: expected head ${mergeSha(e.metadata?.expected_head_sha)} · observed head ${mergeSha(e.metadata?.observed_head_sha)} · observed merge commit ${mergeSha(e.metadata?.merge_commit_sha)}</small>`).join('');
}

function readinessVerificationHtml(provider) {
  return (provider.evidence || []).filter(e => e.evidence_type === 'code-host-pull-request-ready')
    .map(e => `<small>Readiness verification: expected head ${mergeSha(e.metadata?.expected_head_sha)} · observed head ${mergeSha(e.metadata?.observed_head_sha)} · provider ready: ${e.metadata?.ready_for_review === true ? 'yes' : e.metadata?.ready_for_review === false ? 'no' : 'unknown'}</small>`).join('');
}

export function verificationHtml(item) {
    const provider = item.provider_verification || {};
    return `<div class="comm-entry">
      <strong>Verification receipt · ${item.verified ? "verified" : "not verified"}</strong>
      <small>ID: ${escapeHtml(item.id)} · verified ${timeText(item.verified_at)} · evidence gate ${item.evidence_satisfied == null ? "not evaluated" : item.evidence_satisfied ? "satisfied" : "blocked"}</small>
      <small>Findings: ${listText(item.findings)} · provider verified: ${provider.verified == null ? "not evaluated" : provider.verified ? "yes" : "no"} · provider findings: ${listText(provider.findings)}</small>
      ${mergeVerificationHtml(provider)}
      ${readinessVerificationHtml(provider)}
      <small>Evidence-evaluation payload is retained canonically but not dumped into this UI; use the Artifact/Evidence explorer for evidence detail.</small>
    </div>`;
  }
