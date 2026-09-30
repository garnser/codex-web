// Typed presentation and parsing use canonical configuration specifications.
import { secretPath } from './secret_reference_ui.js';
function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

export function valueEditor(spec, { secrets = [], definitions = [] } = {}) {
  if (!spec) return "<small>Select a configuration spec.</small>";
  if (!spec.editable) {
    return '<small>Read-only here; change it through its owning canonical workflow.</small>';
  }
  const kind = spec.value_kind;
  const min = spec.minimum ?? "";
  const max = spec.maximum ?? "";
  const bounds = `${min !== "" ? ` min="${escapeHtml(min)}"` : ""}${max !== "" ? ` max="${escapeHtml(max)}"` : ""}`;
  if (kind === "boolean") {
    return '<label>Value <input id="configuration-draft-value" type="checkbox" /></label>';
  }
  if (kind === "integer") {
    return `<label>Value <input id="configuration-draft-value" type="number" step="1"${bounds} /></label>`;
  }
  if (kind === "number") {
    return `<label>Value <input id="configuration-draft-value" type="number" step="any"${bounds} /></label>`;
  }
  if (kind === "string") {
    if (spec.allowed_values?.length) {
      return `<label>Value <select id="configuration-draft-value">${spec.allowed_values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}</select></label>`;
    }
    return '<label>Value <input id="configuration-draft-value" /></label>';
  }
  if (kind === "string_list") {
    return '<label>Values <textarea id="configuration-draft-value" rows="3" placeholder="one per line or comma-separated"></textarea></label>';
  }
  if (kind === "secret_ref") {
    const options = secrets
      .filter((item) => item.status === "active" && item.use_allowed !== false)
      .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)} · ${escapeHtml(item.id)} · ${escapeHtml(item.status || "unknown")}</option>`)
      .join("");
    return `<label>SecretBroker reference <select id="configuration-draft-value"><option value="">Select secret reference</option>${options}</select></label><small>Raw secret values are never configuration. <a href="${escapeHtml(secretPath())}">Manage Project Secrets</a></small>`;
  }
  if (kind === "definition_ref") {
    const options = definitions
      .filter((item) => item.lifecycle === "published")
      .map((item) => `<option value="${escapeHtml(item.record_id)}">${escapeHtml(item.kind)} · ${escapeHtml(item.definition_id)} · r${escapeHtml(item.revision)} · ${escapeHtml(item.scope_type)}</option>`)
      .join("");
    return `<label>Published Definition Registry revision <select id="configuration-draft-value"><option value="">Select definition revision</option>${options}</select></label>`;
  }
  return `<small>Unsupported value kind: ${escapeHtml(kind)}</small>`;
}

export function draftValue(spec, { definitions = [] } = {}) {
  const input = document.getElementById("configuration-draft-value");
  if (!input) throw new Error("No value editor for this configuration kind.");
  if (spec.value_kind === "boolean") return Boolean(input.checked);
  if (spec.value_kind === "integer") {
    if (!input.value.trim()) throw new Error("Enter an integer value before saving.");
    const value = Number(input.value);
    if (!Number.isInteger(value)) throw new Error("Configuration value must be an integer.");
    return value;
  }
  if (spec.value_kind === "number") {
    if (!input.value.trim()) throw new Error("Enter a numeric value before saving.");
    const value = Number(input.value);
    if (!Number.isFinite(value)) throw new Error("Configuration value must be a number.");
    return value;
  }
  if (spec.value_kind === "string") return input.value;
  if (spec.value_kind === "string_list") {
    return input.value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  }
  if (spec.value_kind === "secret_ref") {
    if (!input.value) throw new Error("Choose a SecretBroker reference.");
    return { kind: "secret", secret_id: input.value };
  }
  if (spec.value_kind === "definition_ref") {
    const record = definitions.find((item) => item.record_id === input.value);
    if (!record || record.lifecycle !== "published") throw new Error("Choose a published Definition Registry revision.");
    return {
      kind: "definition",
      definition_id: record.definition_id,
      revision: String(record.revision),
    };
  }
  throw new Error(`Unsupported configuration kind: ${spec.value_kind}`);
}
