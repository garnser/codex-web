import { request } from './api_client.js';

export function appendHomeOperatorGoals(host, state, { signal } = {}) {
  if (!state.can_configure || !state.thread || state.status !== 'configured') return;
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Inspect goals and checkpoints';
  const result = document.createElement('div');
  result.setAttribute('role', 'status');
  result.dataset.operatorGoals = '';
  host.append(button, result);
  button.addEventListener('click', async () => {
    button.disabled = true;
    result.replaceChildren('Reading canonical goal bindings…');
    try {
      const observation = await request(`/api/home/operator/goals?expected_revision=${state.binding.revision}`, { signal });
      const current = await request('/api/home/operator', { signal });
      if (!result.isConnected || signal?.aborted) return;
      if (current.status !== 'configured' || current.binding?.revision !== state.binding.revision
          || current.thread?.id !== state.thread.id || observation.thread_id !== state.thread.id
          || observation.binding_revision !== state.binding.revision || !Array.isArray(observation.items)
          || observation.items.length > 50) {
        throw new Error('Operator selection changed. Refresh Home.');
      }
      result.replaceChildren();
      if (!observation.items.length) result.textContent = 'No canonical goal is bound to this conversation.';
      for (const item of observation.items) {
        const row = document.createElement('p');
        row.textContent = `${item.title} · Goal: ${item.goal_status} · Execution: ${item.status}`;
        if (item.goal_revision !== item.binding_goal_revision) row.append(' · Goal revision changed');
        row.append(` · Checkpoint: ${item.checkpoint_ref || 'not recorded'}`);
        if (item.stop_reason) row.append(` · Blocker: ${item.stop_reason}`);
        const open = document.createElement('button');
        open.type = 'button';
        open.textContent = 'Open goal and controls';
        open.addEventListener('click', () => window.dispatchEvent(new CustomEvent('codex:open-goal', {
          detail: { goalId: item.goal_id },
        })));
        row.append(' ', open);
        result.append(row);
      }
      if (observation.has_more) result.append('Showing the latest 50 bindings. Inspect Goals for the complete history.');
      result.append(' This is a recorded observation; inspect again for current state.');
    } catch (error) {
      if (error.name !== 'AbortError' && result.isConnected) result.textContent = `Goals unavailable: ${error.message}`;
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
}
