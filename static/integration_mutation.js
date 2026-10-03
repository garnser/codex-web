export async function saveIntegrationDraft({ editors, key, button, result, api, path, payload, apply, refresh }) {
  if (button.disabled) return;
  const ticket = editors.submission(key);
  button.disabled = true;
  result.hidden = false; result.textContent = 'Saving...';
  try {
    const response = await api(path, { method: 'POST', body: JSON.stringify(payload) });
    if (!ticket.current()) return;
    ticket.saved(); apply(response);
    result.textContent = editors.dirty(key) ? `Saved; newer ${key} edits remain unsaved.` : 'Saved';
    await refresh();
  } catch (error) {
    if (ticket.current()) result.textContent = error.message;
  } finally { button.disabled = false; }
}
