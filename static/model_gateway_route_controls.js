function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function populateTaskRouteControls(models) {
  const workloads = Array.from(new Set(
    models.flatMap((item) => item.workload_classes || []),
  )).sort();
  const workloadOptions = document.getElementById("model-workload-options");
  if (workloadOptions) {
    workloadOptions.innerHTML = workloads.map(
      (value) => `<option value="${escapeHtml(value)}"></option>`,
    ).join("");
  }

  const pinnedModel = document.getElementById("model-route-pinned-model");
  if (pinnedModel) {
    pinnedModel.innerHTML = '<option value="">Automatic selection</option>' + models.map(
      (item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.provider_id)} / ${escapeHtml(item.id)}</option>`,
    ).join("");
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
