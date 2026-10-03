export function normalizeUsageResources(raw = {}) {
  return Array.isArray(raw.usage_resources) ? raw.usage_resources : [];
}

function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 0 : 1)}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(number >= 10_000 ? 0 : 1)}k`;
  return number.toLocaleString();
}

function currency(resource, value) {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: resource.currency,
      maximumFractionDigits: 4,
    }).format(value);
  } catch {
    return `${Number(value).toLocaleString()} ${resource.currency || resource.unit}`;
  }
}

function value(resource, amount) {
  if (amount == null) return null;
  if (resource.kind === "money") return currency(resource, amount);
  if (resource.unit === "tokens") return `${compactNumber(amount)} tokens`;
  if (resource.unit === "percent") return `${Math.round(amount)}%`;
  return `${Number(amount).toLocaleString()} ${resource.unit}`;
}

export function renderUsageResources(container, resources = []) {
  if (!container) return;
  container.replaceChildren();
  if (!resources.length) {
    const empty = document.createElement("small");
    empty.className = "usage-empty";
    empty.textContent = "Provider capacity unavailable";
    container.append(empty);
    return;
  }
  for (const resource of resources) {
    const card = document.createElement("div");
    card.className = "limit-card usage-resource";
    card.dataset.resourceId = resource.resource_id || "usage";

    const row = document.createElement("div");
    row.className = "token-row";
    const label = document.createElement("span");
    label.textContent = resource.label || "Usage";
    const summary = document.createElement("strong");
    const consumed = value(resource, resource.consumed);
    const limit = value(resource, resource.limit);
    const remaining = value(resource, resource.remaining);
    summary.textContent = consumed && limit
      ? `${consumed} / ${limit}`
      : consumed || (remaining ? `${remaining} remaining` : "Unavailable");
    row.append(label, summary);
    card.append(row);

    const hasDenominator = resource.authoritative === true
      && Number(resource.limit) > 0
      && resource.consumed != null;
    if (hasDenominator) {
      const percent = Math.max(
        0,
        Math.min(100, Number(resource.consumed) / Number(resource.limit) * 100),
      );
      const meter = document.createElement("div");
      meter.className = "token-meter limit-meter";
      meter.setAttribute("role", "progressbar");
      meter.setAttribute("aria-label", resource.label || "Usage");
      meter.setAttribute("aria-valuenow", String(Math.round(percent)));
      meter.setAttribute("aria-valuemin", "0");
      meter.setAttribute("aria-valuemax", "100");
      const fill = document.createElement("span");
      fill.style.width = `${percent}%`;
      meter.append(fill);
      card.append(meter);
    }

    const details = document.createElement("small");
    const detailParts = [];
    if (remaining && resource.limit != null) detailParts.push(`${remaining} remaining`);
    if (resource.reset_at) {
      detailParts.push(`Resets ${new Date(resource.reset_at * 1000).toLocaleString()}`);
    }
    detailParts.push(
      `${resource.provider_id || "provider"}${resource.model_id ? ` · ${resource.model_id}` : ""}`,
    );
    if (resource.source === "codex_calculated") {
      detailParts.push("Estimated from recorded pricing");
    }
    details.textContent = detailParts.join(" · ");
    card.append(details);
    container.append(card);
  }
}
