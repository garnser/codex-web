import { request } from './api_client.js';

export async function loadProfileUsage(host, profileId) {
  host.textContent = 'Checking canonical consumers…';
  try {
    const result = await request(`/api/agent-profiles/${encodeURIComponent(profileId)}/usage`);
    if (!host.isConnected) return null;
    host.replaceChildren();
    if (result.schema_version !== '1.0') throw new Error('Unsupported consumer impact version.');
    if (!result.available) throw new Error(result.reason || 'Consumer impact is unavailable.');
    const summary = document.createElement('p');
    summary.textContent = `${result.count} consumer(s), ${result.blocking_count} active dependency or invocation(s).`;
    host.appendChild(summary);
    const list = document.createElement('ul');
    for (const item of result.items || []) {
      const row = document.createElement('li');
      row.textContent = `${item.object_type}: ${item.label || item.object_id} (${item.object_id})`
        + (item.revision ? ` · r${item.revision}` : '')
        + (item.project_id ? ` · Project ${item.project_id}` : '')
        + (item.blocking ? ' · blocks disable/archive' : ' · historical or inactive');
      list.appendChild(row);
    }
    host.appendChild(list);
    if (result.restricted_count) {
      const note = document.createElement('p');
      note.textContent = `${result.restricted_count} consumer(s) have restricted metadata. Ask an authorized owner to inspect them.`;
      host.appendChild(note);
    }
    if (result.truncated) {
      const note = document.createElement('p');
      note.textContent = 'The first 100 visible consumers are shown; counts and the lifecycle gate include all verified consumers.';
      host.appendChild(note);
    }
    const help = document.createElement('p');
    help.textContent = result.blocking_count
      ? 'Pause dependent Automations, disable or edit Teams, and finish or cancel active/queued executions before disabling or archiving this profile.'
      : 'No active consumers in the canonical Team, Automation, assignment and queue projection. Historical revisions remain unchanged.';
    host.appendChild(help);
    return result;
  } catch (error) {
    if (host.isConnected) host.textContent = `Consumer impact unavailable: ${error.message}`;
    return null;
  }
}
