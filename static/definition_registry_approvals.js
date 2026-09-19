import { request as apiRequest } from './api_client.js';

export function canApprove(actor, scopeType) {
  if (!actor) return false;
  if (actor.principal_kind === 'service') {
    return scopeType === 'global'
      ? (actor.service_scopes || []).includes('definitions:global-approve')
      : (actor.service_scopes || []).includes('definitions:approve');
  }
  const elevated = ['mfa', 'local_trusted'].includes(actor.assurance)
    && (actor.roles || []).some((role) => ['owner', 'admin', 'approver'].includes(role));
  if (!elevated) return false;
  return scopeType !== 'global' || actor.assurance === 'local_trusted';
}

export async function publicationAssessment(record) {
  return apiRequest(
    '/api/definitions/' + encodeURIComponent(record.record_id) + '/publication-assessment',
  );
}

export async function recordPublicationApproval(record, actor) {
  const assessment = await publicationAssessment(record);
  const reasons = assessment.reasons?.length
    ? assessment.reasons.join(' · ')
    : 'No sensitive expansion detected; approval is optional.';
  const reference = window.prompt(
    'Approval reference for ' + record.definition_id + ' r' + record.revision
      + '.\n\nClassifier: ' + reasons,
    '',
  );
  if (reference === null || !reference.trim()) return null;
  const reason = window.prompt(
    'Approval reason (durable evidence is tied to the candidate checksum and current active revision):',
    '',
  );
  if (reason === null || !reason.trim()) return null;
  if (!window.confirm(
    'Record publication approval as ' + (actor?.identity_id || 'current actor')
      + '? A sensitive publication still requires a different identity to perform the final publish.',
  )) return null;

  const response = await apiRequest(
    '/api/definitions/' + encodeURIComponent(record.record_id) + '/publication-approvals',
    {
      method: 'POST',
      body: JSON.stringify({
        reference: reference.trim(),
        reason: reason.trim(),
      }),
    },
  );
  const count = response.record?.publication_approvals?.length || 0;
  return 'Recorded durable publication approval for ' + record.definition_id
    + ' r' + record.revision + '; ' + count + ' approval(s) now attached.';
}

export async function publicationGate(record, actor) {
  const assessment = await publicationAssessment(record);
  const independentApprovals = (assessment.approvals || []).filter(
    (item) => item.approved_by !== actor?.identity_id,
  );
  const blockedMessage = (
    assessment.requires_independent_approval && !independentApprovals.length
  )
    ? 'Sensitive publication requires independent approval before publish: '
      + (assessment.reasons || []).join(' · ')
    : null;
  return { assessment, independentApprovals, blockedMessage };
}

export function publicationGateSummary(assessment, independentApprovals) {
  return assessment.requires_independent_approval
    ? ' Sensitive expansion classifier: ' + (assessment.reasons || []).join(' · ')
      + '. Independent approval evidence: ' + independentApprovals.length + '.'
    : ' No sensitive authority expansion was detected by the code-owned classifier.';
}

export function rollbackApprovalRecovery(error) {
  if (error.detail?.code !== 'definition_approval_required' || !error.detail?.record_id) {
    return null;
  }
  return {
    refresh: true,
    message: 'Rollback prepared draft ' + error.detail.record_id
      + ', but activation requires independent approval: '
      + (error.detail.reasons || []).join(' · ')
      + '. Refresh, record approval on that draft, then publish it.',
  };
}
