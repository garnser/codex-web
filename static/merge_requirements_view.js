import { esc } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';
const count = value => Number.isInteger(value) ? esc(value) : 'unknown';
const flag = value => value === true ? 'required' : value === false ? 'not required' : 'unknown';
const values = list => (list || []).map(esc).join(', ') || 'none reported';
function pullRequestLink(fact) {
  try {
    const url = new URL(fact.web_url);
    if (url.protocol === 'https:' && url.host === fact.provider_instance && !url.username && !url.password) {
      return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">PR ${esc(fact.external_id)}</a>`;
    }
  } catch {}
  return `PR ${esc(fact.external_id)} (provider link unavailable)`;
}

export function mergeRequirementsFormHtml(context) {
  const declaration = context.merge_requirements_declaration;
  if (!declaration?.declared) return '<small>Merge requirements: unavailable in the current pinned execution declaration.</small>';
  const scope = context.declared_repository_scope || {};
  const ids = [...new Set([...(scope.writable_repository_ids || []), ...(scope.read_only_repository_ids || []),
    ...(context.declared_administrative_scope?.repository_ids || [])])];
  if (!ids.length) return '<small>Merge requirements: no repository declared.</small>';
  return `<form data-merge-requirements><label>Repository <select name="repository" aria-label="Repository" required>${ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join('')}</select></label>
    <label>Pull request number <input name="pull_request" type="number" min="1" step="1" required inputmode="numeric"></label>
    <button type="submit" class="ghost-button">Inspect current merge requirements</button>
    <small>Read with your own operator authority and credential rights. This grants no merge permission.</small>
    <div role="status" aria-live="polite" data-merge-status>No requirements read yet.</div><div data-merge-results></div></form>`;
}

export function mergeRequirementsHtml(response, projectId, now = Date.now() / 1000) {
  const fact = response.item;
  if (fact?.schema_version !== '1.0') throw new Error('Unsupported merge-requirement schema');
  const stale = !Number.isFinite(fact.expires_at) || fact.expires_at <= now;
  return `<div class="comm-entry"><strong>Merge requirements · ${stale ? 'stale' : fact.changed ? 'changed during read' : fact.complete ? 'complete observation' : 'partial / unknown observation'}</strong>
    <small>${referenceLink('resource', fact.resource_id, {projectId})} · ${esc(fact.provider_type)} / ${esc(fact.provider_instance)} · ${pullRequestLink(fact)} · target ${esc(fact.target_branch)}</small>
    <small>Assignment ${referenceLink('assignment', response.scope?.assignment_id, {projectId})} · execution ${esc(response.scope?.execution_id)} · fence ${esc(response.scope?.fence)}</small>
    <small>Head ${esc(fact.head_revision)} · base ${esc(fact.base_revision)}</small>
    <small>Observed ${esc(new Date(fact.observed_at * 1000).toISOString())} · expires ${esc(new Date(fact.expires_at * 1000).toISOString())}</small>
    <small>Read principal ${esc(response.scope?.inspecting_identity_id)} · requester ${esc(response.scope?.requester_identity_id)} · ${esc(response.scope?.inspection_kind)} inspection</small>
    <small>Provider state ${esc(fact.pull_request_state)} · draft ${fact.draft === true ? 'yes' : fact.draft === false ? 'no' : 'unknown'} · mergeability ${esc(fact.mergeable || 'unknown')} · native merge gate ${esc(fact.merge_state || 'unknown')} · native review gate ${esc(fact.review_decision || 'unknown')}</small>
    <small>Review minimum ${count(fact.reviews?.approving_count)} · independent approvals ${count(fact.independent_approval_count)} · current head approvals ${count(fact.current_head_approval_count)} · CODEOWNERS ${flag(fact.reviews?.code_owner_required)} · last push approval ${flag(fact.reviews?.last_push_approval_required)} · dismiss stale approvals ${flag(fact.reviews?.dismiss_stale_approvals)}</small>
    <small>Conversation resolution ${flag(fact.conversation_resolution_required)} · conversations ${count(fact.conversation_count)} · unresolved ${count(fact.unresolved_conversation_count)} · strict checks ${flag(fact.strict_checks_required)}</small>
    <details><summary>Policy sources and check evidence</summary>
    ${(fact.sources || []).map(source => `<small>${esc(source.source)}: ${esc(source.availability)} · ${esc(source.provider_source)} · ${esc(source.reason_code || '')} · records ${count(source.record_count)} · rules ${values(source.rule_ids)}</small>`).join('')}
    ${(fact.required_checks || []).map(check => `<small>Required check ${esc(check.context)} · publisher ${check.provider_app_id == null ? 'any app (explicit requirement)' : esc(check.provider_app_id)} · source ${values(check.source_ids)}</small>`).join('')}
    ${(fact.observed_checks || []).map(check => `<small>Observed ${esc(check.context)} · ${esc(check.state)} · publisher ${check.publisher_known ? esc(check.provider_app_id || 'none') : 'unknown'} · ${esc(check.provider_state)} / ${esc(check.provider_conclusion || 'unknown')}</small>`).join('')}</details>
    <small>Blockers: ${values(fact.blockers)} · unsupported rules: ${values(fact.unsupported_rules)}</small>
    <small>Source completeness is not merge authorization. Provider enforcement and the canonical merge ActionIntent remain authoritative; refresh expired or changed evidence before deciding.</small></div>`;
}
