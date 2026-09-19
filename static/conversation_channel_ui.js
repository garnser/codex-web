import { request } from './api_client.js';

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function ensureInspector() {
  const dialog = document.querySelector('#bot-dialog');
  const form = document.querySelector('#bot-form');
  if (!dialog || !form || document.querySelector('#conversation-channel-inspector')) return;

  const section = document.createElement('details');
  section.id = 'conversation-channel-inspector';
  section.className = 'provider-fields';
  section.innerHTML = `
    <summary>Conversation channel contract & provenance</summary>
    <div class="developer-toolbar">
      <button id="refresh-conversation-channels" type="button" class="ghost-button">Refresh</button>
      <span id="conversation-channel-status" class="form-result" aria-live="polite"></span>
    </div>
    <div id="conversation-channel-provider-contracts" class="comm-log"></div>
    <div class="section-title"><span>Recent canonical message state</span></div>
    <div id="conversation-channel-message-state" class="comm-log"></div>
    <div class="section-title"><span>Recent routing provenance</span></div>
    <div id="conversation-channel-receipts" class="comm-log"></div>
  `;
  const menu = form.querySelector('menu');
  form.insertBefore(section, menu || null);

  section.addEventListener('toggle', () => {
    if (section.open) loadInspector();
  });
  section.querySelector('#refresh-conversation-channels')
    ?.addEventListener('click', loadInspector);
}

function providerRows(items, selected) {
  if (!items.length) return '<div class="comm-log-empty">No ConversationChannel providers registered.</div>';
  return items.map((item) => {
    const capabilities = (item.capabilities || []).join(', ') || 'none';
    const selectedMark = item.provider_type === selected ? ' · selected' : '';
    return `<div class="comm-log-row">
      <strong>${esc(item.provider_type)}</strong>
      <span>${item.available ? 'available' : 'unavailable'}${selectedMark}</span>
      <small>contract ${esc(item.contract_version || 'unknown')} · ${esc(capabilities)}</small>
      ${item.error ? `<small>${esc(item.error)}</small>` : ''}
    </div>`;
  }).join('');
}

function messageRows(items) {
  if (!items.length) return '<div class="comm-log-empty">No canonical channel message state for this provider in the current workspace.</div>';
  return items.slice(0, 8).map((item) => {
    const msg = item.message || {};
    const conversation = msg.conversation || {};
    return `<div class="comm-log-row">
      <strong>${esc(conversation.provider_type)} · ${esc(conversation.conversation_id)}</strong>
      <span>${esc(item.event_kind)}${item.deleted ? ' · deleted' : ''}</span>
      <small>instance ${esc(conversation.provider_instance)} · thread ${esc(conversation.thread_id || '—')} · message ${esc(msg.message_id)}</small>
      <small>canonical event ${esc(item.last_canonical_event_id)}</small>
    </div>`;
  }).join('');
}

function receiptRows(items, selected) {
  const needle = `${selected}::`;
  const filtered = items.filter((item) => String(item.message_key || '').toLowerCase().startsWith(needle.toLowerCase()));
  if (!filtered.length) return '<div class="comm-log-empty">No recent routing receipts for this provider in the current workspace.</div>';
  return filtered.slice(0, 8).map((item) => `<div class="comm-log-row">
    <strong>${esc(item.outcome)}</strong>
    <span>${esc(item.thread_id || item.queued_id || 'no canonical thread/work id')}</span>
    <small>event ${esc(item.canonical_event_id)} · external ${esc(item.message_key)}</small>
    ${item.reason ? `<small>${esc(item.reason)}</small>` : ''}
  </div>`).join('');
}

async function loadInspector() {
  const provider = document.querySelector('#bot-provider')?.value || 'slack';
  const status = document.querySelector('#conversation-channel-status');
  if (status) status.textContent = 'Loading canonical channel state…';
  try {
    const [providers, messages, receipts] = await Promise.all([
      request('/api/conversation-channels/providers'),
      request(`/api/conversation-channels/messages?provider_type=${encodeURIComponent(provider)}&limit=20`),
      request('/api/conversation-channels/receipts?limit=50'),
    ]);
    const providerHost = document.querySelector('#conversation-channel-provider-contracts');
    const messageHost = document.querySelector('#conversation-channel-message-state');
    const receiptHost = document.querySelector('#conversation-channel-receipts');
    if (providerHost) providerHost.innerHTML = providerRows(providers.items || [], provider);
    if (messageHost) messageHost.innerHTML = messageRows(messages.items || []);
    if (receiptHost) receiptHost.innerHTML = receiptRows(receipts.items || [], provider);
    if (status) status.textContent = 'Canonical state loaded';
  } catch (error) {
    if (status) status.textContent = error?.message || 'Failed to load ConversationChannel state';
  }
}

ensureInspector();
document.querySelector('#bot-provider')?.addEventListener('change', () => {
  if (document.querySelector('#conversation-channel-inspector')?.open) {
    loadInspector();
  }
});
