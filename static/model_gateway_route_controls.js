function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function populateTaskRouteControls(models, providerIds = []) {
  const eligible = providerIds.length
    ? models.filter(item => providerIds.includes(item.provider_id))
    : models;
  const workloads = Array.from(new Set(
    eligible.flatMap((item) => item.workload_classes || []),
  )).sort();
  const workloadOptions = document.getElementById("model-workload-options");
  if (workloadOptions) {
    workloadOptions.innerHTML = workloads.map(
      (value) => `<option value="${escapeHtml(value)}"></option>`,
    ).join("");
  }

  const pinnedModel = document.getElementById("model-route-pinned-model");
  if (pinnedModel) {
    const selected = pinnedModel.value;
    pinnedModel.innerHTML = '<option value="">Automatic selection</option>' + eligible.map(
      (item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.provider_id)} / ${escapeHtml(item.id)}</option>`,
    ).join("");
    // Never silently broaden a strict pin to automatic routing on refresh.
    // A missing pin remains explicit and is rejected by the canonical API.
    if (selected && !eligible.some((item) => item.id === selected)) {
      pinnedModel.innerHTML += `<option value="${escapeHtml(selected)}">Unavailable / ${escapeHtml(selected)}</option>`;
    }
    pinnedModel.value = selected;
  }
}

export function readTaskRoutePreferences() {
  const preferredLatency = Array.from(
    document.getElementById("model-route-preferred-latency")?.selectedOptions || [],
  ).map((item) => item.value);
  return {
    workload_class: document.getElementById("model-route-workload")?.value.trim() || null,
    pinned_model_id: document.getElementById("model-route-pinned-model")?.value || null,
    preferred_latency_classes: preferredLatency,
    prefer_lower_cost: Boolean(document.getElementById("model-route-low-cost")?.checked),
  };
}
