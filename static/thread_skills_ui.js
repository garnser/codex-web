import { request } from "./api_client.js";

const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

export function install({ byId, activeThreadId }) {
  const root = byId("thread-skills-manager");
  if (!root) return { load: async () => {} };
  const status = byId("thread-skills-status");
  const list = byId("thread-skills-list");
  const effective = byId("thread-skills-effective");
  const search = byId("thread-skills-search");
  const category = byId("thread-skills-category");
  const source = byId("thread-skills-source");
  const security = byId("thread-skills-security");
  let assignment = null;
  let catalog = [];
  let sources = [];

  const selectedIds = () => new Set((assignment?.explicit || []).map((item) => item.recordId));
  function render() {
    const selected = selectedIds();
    effective.innerHTML = [
      ...(assignment?.explicit || []).map((item) => `<span class="skill-status positive">${esc(item.skill?.name || item.skillId)} · explicit r${esc(item.revision)}</span>`),
      ...(assignment?.inherited || []).map((item) => `<span class="skill-status neutral" title="${esc(item.inheritedFrom || "parent scope")}">${esc(item.skill?.name || item.skillId)} · inherited</span>`),
    ].join("") || "<small>No effective Skills.</small>";
    list.innerHTML = catalog.map((item) => {
      const assigned = selected.has(item.recordId);
      const allowed = item.security?.assignmentDecision?.allowed !== false;
      return `<label class="thread-skill-option">
      <input type="checkbox" data-skill-record="${esc(item.recordId)}" ${assigned ? "checked" : ""} ${!allowed && !assigned ? "disabled" : ""}>
      <span><strong>${esc(item.skill?.name || item.skillId)}</strong><small>${esc((item.skill?.categories || []).join(", ") || "Uncategorized")} · ${esc(item.skill?.provenance?.source_id || "local")} · r${esc(item.revision)} · security ${esc(item.security?.status || "not_scanned")}${allowed ? "" : ` · blocked: ${esc(item.security?.assignmentDecision?.reason)}`}</small></span>
    </label>`;
    }).join("") || '<div class="skill-state">No published Skills match these filters.</div>';
  }

  async function load() {
    const threadId = activeThreadId();
    if (!threadId) { status.textContent = "Select a Thread to manage Skills."; assignment = null; catalog = []; render(); return; }
    status.textContent = "Loading effective Skills…";
    try {
      const params = new URLSearchParams({ include_drafts: "false", limit: "200" });
      if (search.value.trim()) params.set("search", search.value.trim());
      if (category.value.trim()) params.set("category", category.value.trim());
      if (source.value) params.set("source_id", source.value);
      if (security?.value) params.set("security_status", security.value);
      const [assigned, available, sourceResult] = await Promise.all([
        request(`/api/threads/${encodeURIComponent(threadId)}/skills`),
        request(`/api/skills?${params}`),
        request("/api/skill-sources").catch(() => ({ items: [] })),
      ]);
      if (threadId !== activeThreadId()) return;
      assignment = assigned; catalog = available?.items || []; sources = sourceResult?.items || [];
      const current = source.value;
      source.innerHTML = `<option value="">All sources</option>${sources.map((item)=>`<option value="${esc(item.source_id)}">${esc(item.name)}</option>`).join("")}`;
      source.value = current;
      status.textContent = `${assignment.effective?.length || 0} effective Skills; ${assignment.explicit?.length || 0} explicitly assigned.`;
      render();
    } catch (error) { status.textContent = error?.detail?.message || error?.detail || error.message; }
  }

  root.addEventListener("toggle", () => { if (root.open) load(); });
  byId("thread-skills-save").addEventListener("click", async () => {
    const threadId = activeThreadId();
    if (!threadId) return;
    const refs = catalog.filter((item) => list.querySelector(`[data-skill-record="${CSS.escape(item.recordId)}"]`)?.checked)
      .map((item) => item.definitionReference);
    status.textContent = "Saving exact Skill revisions…";
    try {
      assignment = await request(`/api/threads/${encodeURIComponent(threadId)}/skills`, { method: "PUT", body: JSON.stringify({ skill_refs: refs }) });
      status.textContent = `${assignment.effective?.length || 0} effective Skills saved.`; render();
    } catch (error) { status.textContent = error?.detail?.message || error?.detail || error.message; }
  });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 220); });
  category.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 220); });
  source.addEventListener("change", load);
  security?.addEventListener("change", load);
  return { load };
}
