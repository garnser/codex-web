import { request } from './api_client.js';
import { mountHomeHostOutputReview } from './home_host_output_review.js';

/** Human-only discovery and explicit review, never a model/decrypt shortcut. */
export function appendHomeHostOutputs(host, state, { signal, read = request, mountReview = mountHomeHostOutputReview } = {}) {
  if (!state.can_configure || !state.thread?.id || !state.binding?.revision) return () => {};
  const revision = state.binding.revision, threadId = state.thread.id;
  const section = document.createElement('section');
  const heading = document.createElement('h4'); heading.textContent = 'Protected command outputs';
  const refresh = document.createElement('button'); refresh.type = 'button'; refresh.textContent = 'Load command output references';
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  const choices = document.createElement('div');
  const reviewHost = document.createElement('div');
  section.append(heading, refresh, status, choices, reviewHost); host.append(section);
  let valid = true, generation = 0, disposeReview = () => {};
  const dispose = () => { valid = false; generation++; disposeReview(); choices.replaceChildren(); reviewHost.replaceChildren(); };
  signal?.addEventListener('abort', dispose, { once: true });
  const check = () => { if (!valid || signal?.aborted || !section.isConnected) throw new Error('Home selection changed. Refresh Home.'); };
  const verifyCurrent = async () => {
    check();
    const current = await read('/api/home/operator', { signal });
    check();
    if (current.binding?.revision !== revision || current.thread?.id !== threadId || !current.can_configure) {
      dispose(); throw new Error('Home selection or administrator authority changed. Refresh Home.');
    }
  };
  const api = async (url, options = {}) => {
    await verifyCurrent();
    const result = await read(url, { ...options, signal });
    await verifyCurrent();
    return result;
  };
  refresh.addEventListener('click', async () => {
    const own = ++generation;
    refresh.disabled = true; disposeReview(); choices.replaceChildren(); reviewHost.replaceChildren();
    try {
      const listing = await api(`/api/home/operator/host-outputs?expected_home_revision=${revision}`);
      check(); if (own !== generation) return;
      if (listing.home_revision !== revision || listing.thread_id !== threadId || !Array.isArray(listing.items) || listing.items.length > 20) {
        throw new Error('Output reference list changed. Refresh Home.');
      }
      for (const item of listing.items) {
        const button = document.createElement('button'); button.type = 'button';
        button.textContent = `${item.artifact_id} · exit ${Number.isInteger(item.exit_code) ? item.exit_code : 'unknown'}${item.output_truncated ? ' · truncated' : ''}${item.timed_out ? ' · timed out' : ''}`;
        button.addEventListener('click', () => {
          try {
            check(); disposeReview();
            disposeReview = mountReview(reviewHost, { api, artifactId: item.artifact_id, homeRevision: revision,
              currentRevision: () => valid && !signal?.aborted ? revision : null });
          } catch (error) { status.textContent = error.message; }
        });
        choices.append(button);
      }
      status.textContent = listing.items.length
        ? `Showing ${listing.items.length} current command output references.${listing.has_more ? ' Only the latest 20 are shown.' : ''} Preview and release require your explicit review.`
        : 'No completed command outputs are recorded for this current operator assignment.';
    } catch (error) {
      if (error.name !== 'AbortError' && section.isConnected) status.textContent = error.message || 'Output references unavailable.';
    } finally { if (valid && section.isConnected) refresh.disabled = false; }
  });
  return dispose;
}
