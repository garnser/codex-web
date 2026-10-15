import { esc } from './reference_links.js';
import { referenceLink } from './reference_navigation.js';
export const listText = values => values?.length ? values.map(esc).join(', ') : 'none';
// Presentation for the code-owned discovery outcome enum, never an admission gate.
const nextAction = {
  matched: 'Review the exact pins; execution still checks current policy, approvals and live capabilities.',
  no_match: 'No applicable guidance was found. If guidance was expected, inspect triggers, Resource facts and Definition lifecycle; existing policy remains independent.',
  ambiguous: 'Resolve conflicting candidates with a reviewed Definition revision. Precedence cannot override policy.',
  inaccessible: 'Inspect current identity, tenant scope and source access. Required guidance cannot be replaced by copied prose.',
  unsupported_enforcement: 'Use a supported canonical ActionIntent or isolated assignment. Instructions cannot enable unsupported host or native tool mutations.',
  failed: 'Resolve listed Resource, lifecycle/security, capability or policy blockers in their canonical surfaces, then preview again.',
};


export function discoveryGuidanceHtml(item) {
  return `<small>${esc(nextAction[item.outcome] || 'Inspect canonical discovery evidence before continuing.')} · evaluated ${esc(item.evaluated_at ? new Date(item.evaluated_at * 1000).toISOString() : 'unknown')}</small>`;
}
export function discoveryCandidatesHtml(candidates, projectId) {
  return `<small>${(candidates || []).map(candidate => {
    const ref = candidate.reference, label = `${ref.definition_id}@${ref.revision}`;
    const link = ref.record_id ? referenceLink('definition', ref.record_id, {projectId,label}) : esc(label);
    return `${link}: ${esc(candidate.outcome)} · ${(candidate.reasons || []).map(esc).join(', ') || 'none'}${ref.checksum ? ` · ${esc(ref.checksum)}` : ''}`;
  }).join('<br>')}</small>`;
}
