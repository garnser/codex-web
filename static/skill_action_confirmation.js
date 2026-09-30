import { confirmAction } from './action_confirmation.js';
import { request } from './api_client.js';
import { captureProjectView } from './project_view_scope.js';

export async function confirmSkillAction(action, item, root, current, profileId = '') {
  const view = captureProjectView();
  const valid = () => view.current() && root.isConnected && current();
  let impact = '';
  try {
    if (['Archive', 'Publish'].includes(action)) {
      const usage = await request(`/api/skills/${encodeURIComponent(item.skillId)}/usage?revision=${encodeURIComponent(item.revision)}`);
      if (!Array.isArray(usage.items)) throw new Error('Canonical usage unavailable');
      impact = `${usage.items.length} visible exact-revision pins: ${usage.items.slice(0, 100).map(pin => `${pin.object_id} r${pin.revision}`).join(', ')}. Other revisions and external consumers are outside this preview.`;
    }
    return await confirmAction({
      action: `${action} Skill`, target: `${item.skillId} r${item.revision}${profileId ? ` / Agent Profile ${profileId}` : ''}`,
      risk: action === 'Detach' ? 'bounded' : 'high', impact,
      consequence: action === 'Archive' ? 'The Skill becomes unavailable for new execution. Existing recorded provenance remains.'
        : action === 'Publish' ? 'The reviewed revision becomes published and eligible for canonical selection; existing exact pins remain attributed.'
        : `${action} this exact Skill reference in the Agent Profile. Future executions use its resulting revision.`,
      recovery: action === 'Archive' ? 'Restore is available in the Skill lifecycle controls; it does not replay stopped work.'
        : 'A later authorized revision or reference change can restore selection; previous executions are not undone.',
      current: valid,
    });
  } catch (error) {
    if (valid()) {
      const result = root.querySelector('[data-skill-action-result]');
      if (result) { result.hidden = false; result.textContent = `Skill action blocked: ${error.message}`; }
    }
    return false;
  }
}
