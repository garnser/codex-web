import { request } from './api_client.js';
import { applyRunner, runnerPresentation, runnerFailureText } from './thread_runner_ui.js';

export function appendHomeOperatorRuntime(host, state, { signal } = {}) {
  const threadId = state.thread?.id;
  if (!threadId) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Inspect runner and queue';
  const observation = document.createElement('p');
  observation.setAttribute('role', 'status');
  observation.dataset.operatorRuntime = '';
  host.append(button, observation);
  let expiry;
  signal?.addEventListener('abort', () => clearTimeout(expiry), { once: true });
  button.addEventListener('click', async () => {
    button.disabled = true;
    clearTimeout(expiry);
    observation.textContent = 'Reading runner and queue…';
    try {
      const path = `/api/threads/${encodeURIComponent(threadId)}`;
      const [thread, queue] = await Promise.all([
        request(`${path}/runner?project_id=home`, { signal }),
        request(`${path}/queue?project_id=home`, { signal }),
      ]);
      const current = await request('/api/home/operator', { signal });
      if (!observation.isConnected || signal?.aborted) return;
      if (current.binding?.revision !== state.binding?.revision || current.thread?.id !== threadId
          || thread.id !== threadId || queue.threadId !== threadId
          || thread.runner?.schemaVersion !== '1.0'
          || !Number.isSafeInteger(queue.queueDepth) || queue.queueDepth < 0) {
        throw new Error('Operator selection or runtime evidence changed. Refresh Home.');
      }
      applyRunner(thread);
      const status = runnerPresentation(threadId);
      observation.textContent = `${status.label} · Queued: ${queue.queueDepth}`;
      const failure = runnerFailureText(thread.runner);
      if (failure) observation.append(` · ${failure}`);
      const freshUntil = Math.min(thread.runner.leaseExpiresAt || 0, (thread.runner.observedAt || 0) + 60);
      if (thread.runner.live && freshUntil > Date.now() / 1000) {
        expiry = setTimeout(() => {
          if (observation.isConnected) observation.textContent = 'Runner observation expired. Inspect again for current state.';
        }, Math.ceil(freshUntil * 1000 - Date.now()) + 1);
      }
    } catch (error) {
      if (error.name !== 'AbortError' && observation.isConnected) {
        observation.textContent = `Runner and queue unavailable: ${error.message}`;
      }
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
}
