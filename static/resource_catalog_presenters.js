import { esc as escapeHtml } from './reference_links.js';

export function ownerOptions(owners, selectedId = "") {
    const options = ['<option value="">Unassigned owner</option>'];
    owners.forEach((owner) => {
      const selected = owner.id === selectedId ? " selected" : "";
      options.push(`<option value="${escapeHtml(owner.id)}"${selected}>${escapeHtml(owner.label)} · ${escapeHtml(owner.kind)} · ${escapeHtml(owner.id)}</option>`);
    });
    if (selectedId && !owners.some((owner) => owner.id === selectedId)) {
      options.push(`<option value="${escapeHtml(selectedId)}" selected>Current owner · ${escapeHtml(selectedId)}</option>`);
    }
    return options.join("");
  }

export function option(value, current, label = value) {
    return `<option value="${escapeHtml(value)}"${value === current ? " selected" : ""}>${escapeHtml(label)}</option>`;
  }
