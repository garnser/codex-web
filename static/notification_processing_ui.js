// Explicit preparation and canonical processing. There is no send/replay action.
import { referenceNode } from './reference_navigation.js';
import { currentProjectId } from './project_view_scope.js';

export function attachNotificationProcessing({api, captureView, scheduleRefresh, projectId = currentProjectId()}, href, body) {
  const ownerCurrent = captureView();
  let previewGeneration = 0;
  const review = document.createElement('button');
  review.type = 'button';
  review.textContent = 'Review original processing and replies';
  const section = document.createElement('div');
  section.setAttribute('aria-live', 'polite');
  review.addEventListener('click', async () => {
    if (!ownerCurrent() || review.disabled) return;
    const requestCurrent = captureView();
    const generation = ++previewGeneration;
    const current = () => ownerCurrent() && requestCurrent() && generation === previewGeneration;
    if (!current()) return;
    review.disabled = true;
    section.textContent = 'Loading current proof, follow-up ownership and retained reply destinations…';
    try {
      const result = await api(href + '/processing');
      if (!current()) return;
      section.textContent = '';
      for (const item of result.originals || []) {
        const row = document.createElement('section');
        const heading = document.createElement('h4');
        heading.textContent = `Original ${item.original_id} · ${item.processing_state}`;
        const detail = document.createElement('div');
        detail.style.whiteSpace = 'pre-wrap';
        detail.style.overflowWrap = 'anywhere';
        for (const line of [
          `Reviewed disposition: ${item.outcome}`,
          `Blockers: ${(item.blockers || []).join(', ') || 'none'}`,
          `Evidence: ${(item.evidence_ids || []).join(', ') || 'none'}`,
          `Verification: ${(item.verification_ids || []).join(', ') || 'none'}`,
          `Follow-up owner: ${item.follow_up?.next_owner || item.follow_up?.current_owner || 'none linked'}`,
          `Follow-up action: ${item.follow_up?.next_action || 'none linked'}`,
          `Follow-up handoff: ${item.follow_up?.handoff?.status || 'none'}; recipient ACK: ${item.follow_up?.handoff?.acknowledged_at ? 'recorded on canonical Work Item' : 'not established'}`,
          `Reply destination: ${JSON.stringify(item.reply?.destination || null)}`,
          `Reply preparation: ${item.reply?.state}; external delivery: ${item.reply?.delivery_state || 'unknown'}`,
          `Provider receipt: ${item.reply?.provider_receipt_id || 'none'}`,
          `Reply preview: ${item.reply?.text || 'unavailable'}`,
          `Processing receipt: ${item.historical_receipt ? 'recorded; see current eligibility and historical details' : 'not recorded'}`,
          'Processing retains originals and links existing work. Recipient ACK, execution and external send remain separate.',
        ].filter(Boolean)) {
          const paragraph = document.createElement('p');
          paragraph.textContent = line;
          detail.append(paragraph);
        }
        row.append(heading, detail);
        const appendReference = (kind, id, label) => {
          const node = referenceNode(kind, id, {projectId, label});
          node.addEventListener('click', event => { if (!current()) event.preventDefault(); });
          row.append(node, document.createTextNode(' '));
        };
        if (item.follow_up?.ref) {
          appendReference('work_item', item.follow_up.ref, `Follow-up ${item.follow_up.ref}`);
        }
        for (const [kind, ids] of [['evidence', item.evidence_ids], ['verification', item.verification_ids]]) {
          for (const id of ids || []) appendReference(kind, id, `${kind} ${id}`);
        }
        if (item.historical_receipt) {
          const details = document.createElement('details');
          const summary = document.createElement('summary');
          summary.textContent = 'Inspect historical receipt and exact controls';
          const pre = document.createElement('pre');
          pre.style.whiteSpace = 'pre-wrap'; pre.style.overflowWrap = 'anywhere';
          pre.textContent = JSON.stringify(item.historical_receipt, null, 2);
          details.append(summary, pre); row.append(details);
        }
        if (item.can_process) {
          const process = document.createElement('button');
          process.type = 'button';
          process.textContent = `Process reviewed original ${item.original_id}`;
          const status = document.createElement('p');
          process.addEventListener('click', async () => {
            if (!current() || process.disabled) return;
            const processingCurrent = captureView();
            const stillCurrent = () => current() && processingCurrent();
            if (!stillCurrent()) return;
            process.disabled = true;
            status.textContent = 'Committing exact reviewed proof and follow-up link…';
            try {
              const response = await api(href + '/processing', {method: 'POST', body: JSON.stringify({
                original_id: item.original_id, expected_processing_sha256: item.expected_processing_sha256,
              })});
              if (!stillCurrent()) return;
              status.textContent = `Canonical processing: ${response.original.processing_state}. External delivery: ${response.original.reply.delivery_state}. Reload review for current eligibility.`;
              scheduleRefresh?.(0);
            } catch (error) {
              // An unknown HTTP outcome must be reconciled by reading current
              // state. Never automatically repeat a processing mutation.
              if (stillCurrent()) status.textContent = `Processing unavailable or outcome unknown: ${error.message}. Reload review before any next action.`;
            }
          });
          row.append(process, status);
        }
        section.append(row);
      }
      if (!(result.originals || []).length) section.textContent = 'No retained originals available.';
    } catch (error) {
      if (current()) section.textContent = `Processing review unavailable or denied: ${error.message}`;
    } finally {
      if (current()) review.disabled = false;
    }
  });
  body.append(review, section);
}
