export function mergeProviderEligibility(providers, rows) {
  return providers.map(item => {
    const eligibility = rows.find(row => row.provider_id === item.id) || {};
    return { ...item, ...eligibility, routing_eligible: Boolean(eligibility.eligible) };
  });
}

export function invocationEligibilityHtml(item, esc) {
  const excluded = (item.excluded_candidates || []).map(row => (
    `${esc(row.model_id)} (${esc(row.reason)})`
  )).join(", ");
  return `<small>Execution path: ${esc(item.selected_provider_family || "unknown")} / ${esc(item.selected_runtime_provider || "unknown")} · ${esc(item.selected_access_source || "unknown")} · ${esc(item.selected_usage_semantics || "unknown")}</small>
    <small>Candidate set: ${esc(item.active_candidate_set_revision || "legacy")} · critic independence requested ${esc(item.requested_critic_independence || "n/a")} / achieved ${esc(item.achieved_critic_independence || "n/a")}</small>
    ${excluded ? `<small>Excluded: ${excluded}</small>` : ""}`;
}

export function routeEligibilityHtml(result, esc) {
  const excluded = (result.excluded_candidates || []).map(item => (
    `${esc(item.model_id)} (${esc(item.reason)})`
  )).join(", ");
  return `<small>Active provider/runtime set: ${esc(result.active_candidate_set_revision)} · critic independence requested ${esc(result.requested_critic_independence || "n/a")} / achieved ${esc(result.achieved_critic_independence || "n/a")}</small>
    ${excluded ? `<small>Excluded candidates: ${excluded}</small>` : ""}`;
}

export function candidateExecutionPathHtml(candidate, esc) {
  return `<small>${esc(candidate.provider_family)} / ${esc(candidate.runtime_provider)} · ${esc(candidate.access_source)} · ${esc(candidate.usage_semantics)}${candidate.critic_independence ? ` · critic ${esc(candidate.critic_independence)}` : ""}</small>`;
}
