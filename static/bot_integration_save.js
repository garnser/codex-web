import { botEditorSubmission } from './bot_page_editor.js';

const byId = id => document.getElementById(id);
export async function saveBotIntegration(event, { target, api, runSettings, refreshConnections, refresh }) {
  event.preventDefault();
  const save = byId('save-bot-integration');
  if (!target || save.disabled) return;
  const ticket = botEditorSubmission(), provider = byId('bot-provider').value;
  const conversation = byId(provider === 'slack' ? 'bot-conversation-id' : 'bot-telegram-chat-id').value.trim();
  const result = byId('bot-result');
  if (!conversation) {
    result.hidden = false;
    result.textContent = provider === 'slack' ? 'Slack channel ID is required.' : 'Telegram chat ID is required.';
    return;
  }
  const connectionPayload = {
    id: byId('bot-connection').value || null, provider, name: byId('bot-name').value.trim(), project_id: target.projectId,
    bot_token: byId('bot-token').value.trim() || null,
    slack_app_token: provider === 'slack' ? byId('bot-slack-app-token').value.trim() || null : null,
    signing_secret: provider === 'slack' ? byId('bot-signing-secret').value.trim() || null : null,
    webhook_secret: provider === 'telegram' ? byId('bot-webhook-secret').value.trim() || null : null,
    default_external_conversation_id: conversation,
    default_external_name: byId('bot-external-name').value.trim() || null,
  };
  const bindToThread = target.scope === 'thread' && byId('bot-bind-existing-thread').checked;
  const settings = runSettings();
  const bindingPayload = {
    provider, external_conversation_id: conversation, external_name: connectionPayload.default_external_name,
    project_id: target.projectId, thread_id: bindToThread ? target.threadId : null,
    thread_name: bindToThread ? null : byId('bot-route-prefix').value.trim(),
    route_prefix: byId('bot-route-prefix').value.trim() || target.title,
    post_in_thread: provider === 'slack' && byId('bot-post-in-thread').checked,
    sandbox: settings.sandbox, approval_policy: settings.approvalPolicy,
  };
  byId('bot-dialog').querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
  save.disabled = true;
  try {
    const connection = await api('/api/bots/connections', { method: 'POST', body: JSON.stringify(connectionPayload) });
    const binding = await api('/api/bots/bindings', { method: 'POST', body: JSON.stringify({ ...bindingPayload, connection_id: connection.id }) });
    if (!ticket.current()) return;
    ticket.saved(); result.hidden = false;
    result.textContent = ticket.dirty()
      ? `Saved ${connection.name}; newer metadata remains unsaved.`
      : `Saved ${connection.name}; bound ${provider} conversation ${conversation} to thread ${binding.thread_id}.`;
    if (ticket.dirty()) return;
    await refreshConnections(); await refresh();
    if (ticket.current()) byId('bot-dialog').close();
  } finally { if (save.isConnected) save.disabled = false; }
}
