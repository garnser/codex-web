import { esc as escapeHtml } from './reference_links.js';
import { apiBase } from './api_client.js';
const BASE = apiBase();

export function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }


export function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }


export function safeLink(value) {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      if (!["http:", "https:"].includes(url.protocol)) return "";
      return `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">open source</a>`;
    } catch (_) {
      return "";
    }
  }


export function governanceHtml(record) {
    if (!record) {
      return "<small>Governance metadata: not linked/backfilled.</small>";
    }
    const hold = record.legal_hold_at
      ? `legal hold since ${timeText(record.legal_hold_at)} by ${record.legal_hold_by || "unknown"}`
      : "no legal hold";
    return `<small>Governance: ${escapeHtml(record.classification)} · ${escapeHtml(record.lifecycle)} · retention ${escapeHtml(record.retention_action)} at ${timeText(record.retention_expires_at)} · ${escapeHtml(hold)}</small>
      <small>Residency: ${listText(record.residency_tags)} · deny model context: ${record.deny_model_context ? "yes" : "no"} · governance ID: ${escapeHtml(record.id)}</small>`;
  }


export function researchLinks(item) {
    const project = item.project_id || document.body?.dataset.projectId || document.body?.dataset.activeProject;
    const route = project ? `${BASE}/projects/${encodeURIComponent(project)}/memory` : `${BASE}/`;
    const snapshots = new Map((item.research_sources || []).map(source => [source.knowledge_id, source]));
    const ids = [...new Set([...(item.knowledge_ids || []), ...snapshots.keys()])];
    return ids.map(id => {
      const source = snapshots.get(id);
      const label = source ? `${id}@v${source.version}: ${source.origin}` : id;
      return `<a href="${route}?knowledge_id=${encodeURIComponent(id)}">${escapeHtml(label)}</a>`;
    }).join(', ') || 'none';
  }
