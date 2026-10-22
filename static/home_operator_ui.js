import { request, apiBase } from './api_client.js';
import { appendHomeOperatorRuntime } from './home_operator_runtime.js';

export async function renderHomeOperator(host, { signal } = {}) {
  const title = document.createElement('h3');
  title.textContent = 'Home operator conversation';
  host.replaceChildren(title);
  const detail = document.createElement('p');
  detail.setAttribute('role', 'status');
    detail.dataset.operatorSelection = '';
  detail.textContent = 'Reading the saved conversation…';
  host.append(detail);
  try {
    const state = await request('/api/home/operator', { signal });
    if (!host.isConnected || signal?.aborted) return;
    detail.textContent = state.thread
      ? `${state.thread.name} · Profile: ${state.thread.execution_profile_id || 'not selected'} · Sandbox: ${state.thread.sandbox || 'inherited'} · Approval: ${state.thread.approval_policy || 'inherited'}`
      : state.status === 'unavailable'
        ? 'The saved conversation is unavailable. Its identity is retained; no replacement has been created.'
        : 'Choose an existing named Home conversation after explicitly selecting its execution profile.';
    if (state.binding?.agent_profile_id) {
      const profile = document.createElement('p');
      profile.textContent = state.agent_profile
        ? `Agent Profile: ${state.agent_profile.name} · revision ${state.agent_profile.revision}`
        : 'The selected Agent Profile changed or is unavailable. Select its current revision before sending.';
      host.append(profile);
    }
    const authority = document.createElement('p');
    authority.textContent = 'This shortcut grants no host access or cross-project permissions. Execution uses the conversation’s current canonical profile and controls.';
    host.append(authority);
    if (state.thread) {
      const open = document.createElement('a');
      open.href = `${apiBase()}${state.thread.href}`;
      open.className = 'primary-button';
      open.textContent = 'Open operator conversation';
      host.append(open);
      appendHomeOperatorRuntime(host, state, { signal });
    }
    if (!state.can_configure) return;
    const choose = document.createElement('button');
    choose.type = 'button';
    choose.className = 'ghost-button';
    choose.textContent = 'Choose conversation';
    host.append(choose);
    choose.addEventListener('click', async () => {
      choose.disabled = true;
      try {
        const page = await request('/api/threads?project_id=home&limit=50', { signal });
        if (!host.isConnected || signal?.aborted) return;
        const form = document.createElement('form');
        const label = document.createElement('label');
        label.textContent = 'Existing Home conversation ';
        const select = document.createElement('select');
        select.required = true;
        const empty = new Option('Select a conversation', '');
        select.add(empty);
        for (const row of page.data || page.threads || []) {
          select.add(new Option(`${row.name || row.id} (${row.id})`, row.id));
        }
        label.append(select);
        const profileLabel = document.createElement('label');
        profileLabel.textContent = 'Agent Profile ';
        const profileSelect = document.createElement('select');
        profileSelect.setAttribute('aria-label', 'Agent Profile');
        profileSelect.add(new Option('Navigation only', ''));
        if (state.binding?.agent_profile_id) {
          profileSelect.add(new Option(state.agent_profile?.name || state.binding.agent_profile_id,
            state.binding.agent_profile_id));
          profileSelect.value = state.binding.agent_profile_id;
        }
        profileLabel.append(profileSelect);
        const loadProfiles = document.createElement('button');
        loadProfiles.type = 'button';
        loadProfiles.textContent = 'Load available Agent Profiles';
        loadProfiles.addEventListener('click', async () => {
          loadProfiles.disabled = true;
          try {
            const profiles = await request('/api/agent-profiles/picker', { signal });
            if (!host.isConnected || signal?.aborted) return;
            const selected = profileSelect.value;
            profileSelect.replaceChildren(new Option('Navigation only', ''));
            for (const item of profiles.items || []) {
              profileSelect.add(new Option(`${item.name} · ${item.executionProfileId} · revision ${item.revision}`, item.id));
            }
            profileSelect.value = selected;
          } catch (error) {
            if (error.name !== 'AbortError') detail.textContent = error.message;
          }
        });
        const save = document.createElement('button');
        save.type = 'submit';
        save.textContent = 'Save Home shortcut';
        form.append(label, profileLabel, loadProfiles, save);
        form.addEventListener('submit', async event => {
          event.preventDefault();
          if (!select.value || save.disabled) return;
          save.disabled = true;
          try {
            await request('/api/home/operator', { method: 'PUT', signal,
              body: JSON.stringify({ thread_id: select.value, expected_revision: state.binding?.revision || 0,
                ...(profileSelect.value ? { agent_profile_id: profileSelect.value } : {}) }) });
            await renderHomeOperator(host, { signal });
          } catch (error) {
            if (error.name !== 'AbortError') detail.textContent = `${error.message}. Refresh Home before trying again.`;
            // Unknown network outcomes are not automatically retried.
          }
        });
        host.append(form);
        if (page.hasMore || page.nextCursor) {
          const note = document.createElement('p');
          note.textContent = 'Showing the latest 50 conversations. Open an older conversation from Threads to bring it into this list.';
          host.append(note);
        }
      } catch (error) {
        if (error.name !== 'AbortError') detail.textContent = error.message;
      }
    });
    if (state.binding?.thread_id) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'ghost-button';
      clear.textContent = 'Remove Home shortcut';
      clear.addEventListener('click', async () => {
        clear.disabled = true;
        try {
          await request('/api/home/operator', { method: 'PUT', signal,
            body: JSON.stringify({ thread_id: null, expected_revision: state.binding.revision }) });
          await renderHomeOperator(host, { signal });
        } catch (error) {
          if (error.name !== 'AbortError') detail.textContent = `${error.message}. Refresh Home before trying again.`;
        }
      });
      host.append(clear);
    }
  } catch (error) {
    if (error.name !== 'AbortError' && host.isConnected) detail.textContent = `Operator shortcut unavailable: ${error.message}`;
  }
}
