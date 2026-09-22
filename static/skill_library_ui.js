import { request } from "./api_client.js";

const state = {
  items: [],
  profiles: [],
  selected: null,
  revisions: [],
  usage: [],
  loading: false,
};

const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const csv = (value) => String(value || "").split(",").map((v) => v.trim()).filter(Boolean);
const lines = (value) => String(value || "").split("\n").map((v) => v.trim()).filter(Boolean);
const query = (root, selector) => root.querySelector(selector);
const payload = (response) => response?.item ?? response;

function errorText(error) {
  const detail = error?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (detail?.code) return detail.code;
  return error?.message || "Request failed";
}

function lifecycleLabel(item) {
  if (item.definitionLifecycle === "superseded") return "superseded";
  if (item.definitionLifecycle === "draft") return "draft";
  if (item.skill?.lifecycle === "archived") return "archived";
  return item.definitionLifecycle || item.skill?.lifecycle || "unknown";
}

function statusClass(value) {
  if (["published", "active"].includes(value)) return "positive";
  if (["draft", "superseded"].includes(value)) return "warning";
  if (["archived", "incompatible"].includes(value)) return "negative";
  return "neutral";
}

function refLabel(ref) {
  if (!ref) return "—";
  return `${ref.definition_id || ref.definitionId || "skill"} · r${ref.revision ?? "?"} · ${ref.checksum || ref.record_id || ref.recordId || "unavailable"}`;
}

function cardMarkup() {
  return `
    <h2>Skills</h2>
    <div class="skill-ui" data-skill-ui>
      <div class="skill-toolbar">
        <input data-skill-search type="search" placeholder="Search name, tag, capability…" aria-label="Search skills">
        <select data-skill-lifecycle aria-label="Filter lifecycle">
          <option value="">All lifecycle states</option>
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
        <button class="ghost-button" type="button" data-skill-refresh>Refresh</button>
        <button class="primary-button" type="button" data-skill-new>New Skill</button>
      </div>
      <div class="skill-layout">
        <aside class="skill-list" data-skill-list aria-live="polite"></aside>
        <section class="skill-detail" data-skill-detail>
          <div class="skill-empty">Select a Skill or create a new draft.</div>
        </section>
      </div>
      <details class="skill-import">
        <summary>Import / promote</summary>
        <div class="skill-import-grid">
          <section>
            <h3>Import portable bundle</h3>
            <textarea data-skill-import-json rows="8" spellcheck="false" placeholder='{"format":"codex-web-skill-bundle",...}'></textarea>
            <div class="skill-actions">
              <button class="ghost-button" type="button" data-skill-import-preview>Preview</button>
              <button class="primary-button" type="button" data-skill-import-confirm disabled>Create draft</button>
            </div>
            <pre data-skill-import-result>Paste a bundle and preview it before creating a draft.</pre>
          </section>
          <section>
            <h3>Promote verified procedure</h3>
            <label>Skill ID <input data-promote-id placeholder="release-check"></label>
            <label>Name <input data-promote-name></label>
            <label>Source execution ID <input data-promote-execution></label>
            <label>Evidence IDs <input data-promote-evidence placeholder="ev-1, ev-2"></label>
            <label>Procedure <textarea data-promote-procedure rows="5"></textarea></label>
            <button class="primary-button" type="button" data-skill-promote>Create draft for review</button>
            <div class="form-result" data-promote-result hidden></div>
          </section>
        </div>
      </details>
    </div>`;
}

function installCard() {
  if (document.querySelector("[data-skill-ui]")) return document.querySelector("[data-skill-ui]");
  const grid = document.querySelector(".developer-grid");
  if (!grid) return null;
  const card = document.createElement("div");
  card.className = "developer-card skill-product-card";
  card.innerHTML = cardMarkup();
  grid.appendChild(card);
  return card.querySelector("[data-skill-ui]");
}

async function loadSkills(root) {
  state.loading = true;
  renderList(root);
  try {
    const search = query(root, "[data-skill-search]")?.value.trim() || "";
    const lifecycle = query(root, "[data-skill-lifecycle]")?.value || "";
    const params = new URLSearchParams({ include_drafts: "true" });
    if (search) params.set("search", search);
    if (lifecycle) params.set("lifecycle", lifecycle);
    const result = await request(`/api/skills?${params.toString()}`);
    state.items = result?.items || [];
    if (state.selected) {
      state.selected = state.items.find((item) => item.skillId === state.selected.skillId) || state.selected;
    }
  } catch (error) {
    state.items = [];
    query(root, "[data-skill-list]").innerHTML = `<div class="skill-state negative">Unable to load Skills: ${esc(errorText(error))}</div>`;
    return;
  } finally {
    state.loading = false;
  }
  renderList(root);
}

async function loadProfiles() {
  try {
    const result = await request("/api/agent-profiles?include_archived=true");
    state.profiles = result?.items || [];
  } catch {
    state.profiles = [];
  }
}

function renderList(root) {
  const host = query(root, "[data-skill-list]");
  if (!host) return;
  if (state.loading) {
    host.innerHTML = '<div class="skill-state">Loading Skills…</div>';
    return;
  }
  if (!state.items.length) {
    host.innerHTML = '<div class="skill-state">No Skills match this workspace/filter.</div>';
    return;
  }
  host.innerHTML = state.items.map((item) => {
    const status = lifecycleLabel(item);
    const selected = item.skillId === state.selected?.skillId ? " selected" : "";
    const assetKinds = [...new Set((item.skill?.assets || []).map((a) => a.kind))];
    return `
      <button type="button" class="skill-list-item${selected}" data-skill-id="${esc(item.skillId)}">
        <span><strong>${esc(item.skill?.name || item.skillId)}</strong><small>${esc(item.skillId)}</small></span>
        <span class="skill-list-meta">
          <span class="skill-status ${statusClass(status)}">${esc(status)}</span>
          <small>r${esc(item.revision)} · ${esc(item.checksum?.slice(0, 10) || "no checksum")}</small>
          ${assetKinds.length ? `<small>${esc(assetKinds.join(", "))}</small>` : ""}
        </span>
      </button>`;
  }).join("");
  host.querySelectorAll("[data-skill-id]").forEach((button) => {
    button.addEventListener("click", () => selectSkill(root, button.dataset.skillId));
  });
}

async function selectSkill(root, skillId, revision = null) {
  const suffix = revision ? `?revision=${encodeURIComponent(revision)}` : "";
  const [skill, revisions, usage] = await Promise.all([
    request(`/api/skills/${encodeURIComponent(skillId)}${suffix}`),
    request(`/api/skills/${encodeURIComponent(skillId)}/revisions`),
    request(`/api/skills/${encodeURIComponent(skillId)}/usage${revision ? `?revision=${encodeURIComponent(revision)}` : ""}`),
  ]);
  state.selected = payload(skill);
  state.revisions = revisions?.items || [];
  state.usage = usage?.items || [];
  renderList(root);
  renderDetail(root);
}

function assetRows(item) {
  const assets = item.skill?.assets || [];
  if (!assets.length) return '<div class="skill-state">No bounded assets.</div>';
  return `
    <div class="skill-assets">
      ${assets.map((asset) => `
        <div class="skill-asset">
          <strong>${esc(asset.path)}</strong>
          <span>${esc(asset.kind)}</span>
          <span class="skill-status ${asset.security_class === "executable_untrusted" ? "negative" : "neutral"}">${esc(asset.security_class)}</span>
          <small>context: ${esc(asset.context_mode)}</small>
          ${asset.kind === "helper_script" ? '<small class="skill-warning">Executable/untrusted · never auto-runs</small>' : ""}
        </div>`).join("")}
    </div>`;
}

function usageRows() {
  if (!state.usage.length) return '<div class="skill-state">No Agent Profiles currently reference this exact revision.</div>';
  return state.usage.map((item) => `
    <div class="skill-usage-row">
      <span><strong>${esc(item.object_id)}</strong><small>profile revision ${esc(item.revision ?? "—")}</small></span>
      <button class="ghost-button" type="button" data-skill-detach="${esc(item.object_id)}">Detach</button>
    </div>`).join("");
}

function profileOptions() {
  return state.profiles
    .filter((profile) => profile.lifecycle === "active")
    .map((profile) => `<option value="${esc(profile.profile_id)}">${esc(profile.name)} · r${esc(profile.revision)}</option>`)
    .join("");
}

function editorMarkup(item) {
  const skill = item?.skill || {};
  const isNew = !item;
  const assetsJson = JSON.stringify(skill.assets || [], null, 2);
  return `
    <form data-skill-editor class="skill-editor">
      <input type="hidden" data-field="skillId" value="${esc(item?.skillId || "")}">
      <div class="skill-form-grid">
        <label>Skill ID <input data-field="newSkillId" ${isNew ? "" : "disabled"} value="${esc(item?.skillId || "")}" pattern="[a-z0-9][a-z0-9._-]*" required></label>
        <label>Name <input data-field="name" value="${esc(skill.name || "")}" required></label>
        <label class="skill-wide">Description <textarea data-field="description" rows="2">${esc(skill.description || "")}</textarea></label>
        <label class="skill-wide">Instructions <textarea data-field="instructions" rows="7" required>${esc(skill.instructions || "")}</textarea></label>
        <label>Applicability tags <input data-field="applicability" value="${esc((skill.applicability_tags || []).join(", "))}"></label>
        <label>Capability tags <input data-field="capability" value="${esc((skill.capability_tags || []).join(", "))}"></label>
        <label>Provider capabilities <input data-field="providerCaps" value="${esc((skill.required_provider_capabilities || []).join(", "))}"></label>
        <label>Worker capabilities <input data-field="workerCaps" value="${esc((skill.required_worker_capabilities || []).join(", "))}"></label>
        <label>Input expectations <textarea data-field="inputs" rows="3">${esc((skill.input_expectations || []).join("\n"))}</textarea></label>
        <label>Output expectations <textarea data-field="outputs" rows="3">${esc((skill.output_expectations || []).join("\n"))}</textarea></label>
        <label class="skill-wide">Assets (typed JSON array)
          <textarea data-field="assets" rows="8" spellcheck="false">${esc(assetsJson)}</textarea>
          <small>Reference/template assets are bounded context. Helper scripts must be executable_untrusted with context_mode=never and are never auto-executed.</small>
        </label>
        <label>Provenance source type <input data-field="sourceType" value="${esc(skill.provenance?.source_type || "manual")}"></label>
        <label>Provenance source ref <input data-field="sourceRef" value="${esc(skill.provenance?.source_ref || "")}"></label>
        <label class="skill-wide">Change reason <input data-field="reason" placeholder="Required for revisions" ${isNew ? "" : "required"}></label>
      </div>
      <div class="skill-actions">
        <button class="primary-button" type="submit">${isNew ? "Create draft" : "Create new draft revision"}</button>
        <button class="ghost-button" type="button" data-editor-cancel>Cancel</button>
      </div>
      <div class="form-result" data-editor-result hidden></div>
    </form>`;
}

function renderDetail(root) {
  const host = query(root, "[data-skill-detail]");
  const item = state.selected;
  if (!host || !item) return;
  const status = lifecycleLabel(item);
  const usage = usageRows();
  host.innerHTML = `
    <header class="skill-detail-header">
      <div>
        <h3>${esc(item.skill?.name || item.skillId)}</h3>
        <p>${esc(item.skill?.description || "No description")}</p>
      </div>
      <span class="skill-status ${statusClass(status)}">${esc(status)}</span>
    </header>
    <div class="skill-revision-strip">
      <label>Revision
        <select data-skill-revision>
          ${[...state.revisions].sort((a,b)=>b.revision-a.revision).map((rev)=>`<option value="${rev.revision}" ${rev.revision===item.revision?"selected":""}>r${rev.revision} · ${esc(lifecycleLabel(rev))}</option>`).join("")}
        </select>
      </label>
      <code>record ${esc(item.recordId)}</code>
      <code>checksum ${esc(item.checksum || "—")}</code>
    </div>
    <div class="skill-detail-grid">
      <section><h4>Instructions</h4><pre class="skill-instructions">${esc(item.skill?.instructions || "")}</pre></section>
      <section><h4>Provenance</h4>
        <dl class="skill-meta">
          <dt>Owner</dt><dd>${esc(item.skill?.owner_identity_id || "—")}</dd>
          <dt>Created by</dt><dd>${esc(item.createdBy || "—")}</dd>
          <dt>Published by</dt><dd>${esc(item.publishedBy || "—")}</dd>
          <dt>Source</dt><dd>${esc(item.skill?.provenance?.source_type || "manual")} · ${esc(item.skill?.provenance?.source_ref || "—")}</dd>
        </dl>
      </section>
      <section class="skill-wide"><h4>Assets & security classification</h4>${assetRows(item)}</section>
      <section><h4>Routing requirements</h4>
        <p><strong>Provider:</strong> ${esc((item.skill?.required_provider_capabilities || []).join(", ") || "none")}</p>
        <p><strong>Worker:</strong> ${esc((item.skill?.required_worker_capabilities || []).join(", ") || "none")}</p>
      </section>
      <section><h4>Exact Agent Profile pins</h4><div data-skill-usage>${usage}</div></section>
    </div>
    <div class="skill-actions">
      <button class="ghost-button" type="button" data-skill-edit>Edit as new draft</button>
      ${item.definitionLifecycle === "draft" ? '<button class="primary-button" type="button" data-skill-publish>Publish revision</button>' : ""}
      ${item.skill?.lifecycle === "archived" ? '<button class="ghost-button" type="button" data-skill-restore>Restore</button>' : '<button class="ghost-button" type="button" data-skill-archive>Archive</button>'}
      <button class="ghost-button" type="button" data-skill-export>Export bundle</button>
    </div>
    <section class="skill-attach">
      <h4>Attach exact revision to Agent Profile</h4>
      <p>This pins r${esc(item.revision)} and its checksum. Profiles never silently upgrade to newer revisions.</p>
      <div class="skill-actions">
        <select data-skill-profile><option value="">Select active profile…</option>${profileOptions()}</select>
        <button class="primary-button" type="button" data-skill-attach>Attach r${esc(item.revision)}</button>
      </div>
      <div class="form-result" data-skill-action-result hidden></div>
    </section>
    <details class="skill-provenance">
      <summary>Execution provenance</summary>
      <p>Choose a Profile to inspect executions and resolve the exact Profile revision and Skill pins used for each run.</p>
      <div class="skill-actions">
        <select data-provenance-profile><option value="">Select profile…</option>${profileOptions()}</select>
        <button class="ghost-button" type="button" data-provenance-load>Load executions</button>
      </div>
      <div data-provenance-results class="skill-provenance-results"></div>
    </details>`;
  bindDetail(root);
}

function formValue(form, field) {
  return form.querySelector(`[data-field="${field}"]`)?.value ?? "";
}

function editorPayload(form, isNew) {
  let assets;
  try { assets = JSON.parse(formValue(form, "assets") || "[]"); }
  catch { throw new Error("Assets must be a valid JSON array."); }
  if (!Array.isArray(assets)) throw new Error("Assets must be a JSON array.");
  const base = {
    name: formValue(form, "name").trim(),
    description: formValue(form, "description"),
    instructions: formValue(form, "instructions"),
    applicability_tags: csv(formValue(form, "applicability")),
    capability_tags: csv(formValue(form, "capability")),
    assets,
    required_provider_capabilities: csv(formValue(form, "providerCaps")),
    required_worker_capabilities: csv(formValue(form, "workerCaps")),
    input_expectations: lines(formValue(form, "inputs")),
    output_expectations: lines(formValue(form, "outputs")),
    provenance: {
      source_type: formValue(form, "sourceType").trim() || "manual",
      source_ref: formValue(form, "sourceRef").trim() || null,
      evidence_ids: state.selected?.skill?.provenance?.evidence_ids || [],
      imported_at: state.selected?.skill?.provenance?.imported_at || null,
    },
  };
  if (isNew) {
    return { skill_id: formValue(form, "newSkillId").trim(), ...base, reason: formValue(form, "reason").trim() || null };
  }
  return { ...base, reason: formValue(form, "reason").trim() };
}

function showEditor(root, item = null) {
  const host = query(root, "[data-skill-detail]");
  host.innerHTML = editorMarkup(item);
  const form = query(host, "[data-skill-editor]");
  query(form, "[data-editor-cancel]").addEventListener("click", () => item ? renderDetail(root) : (host.innerHTML='<div class="skill-empty">Select a Skill or create a new draft.</div>'));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const result = query(form, "[data-editor-result]");
    try {
      const isNew = !item;
      const body = editorPayload(form, isNew);
      const response = await request(isNew ? "/api/skills" : `/api/skills/${encodeURIComponent(item.skillId)}`, {
        method: isNew ? "POST" : "PATCH",
        body: JSON.stringify(body),
      });
      const saved = payload(response);
      await loadSkills(root);
      await selectSkill(root, saved.skillId, saved.revision);
    } catch (error) {
      result.hidden = false;
      result.textContent = errorText(error);
    }
  });
}

async function mutation(root, path, body = { reason: "Skill library UI action" }, method = "POST") {
  const result = query(root, "[data-skill-action-result]");
  try {
    const response = await request(path, { method, body: body == null ? undefined : JSON.stringify(body) });
    if (result) { result.hidden = false; result.textContent = "Saved."; }
    const saved = payload(response);
    await loadSkills(root);
    if (saved?.skillId || state.selected?.skillId) {
      await selectSkill(root, saved?.skillId || state.selected.skillId, saved?.revision || null);
    }
    return response;
  } catch (error) {
    if (result) { result.hidden = false; result.textContent = errorText(error); }
    throw error;
  }
}

async function loadExecutionProvenance(root, profileId) {
  const host = query(root, "[data-provenance-results]");
  host.innerHTML = '<div class="skill-state">Loading execution provenance…</div>';
  try {
    const history = await request(`/api/agent-profiles/${encodeURIComponent(profileId)}/executions?limit=20`);
    const rows = [];
    for (const run of history?.items || []) {
      const profile = payload(await request(`/api/agent-profiles/${encodeURIComponent(profileId)}?revision=${encodeURIComponent(run.profileRevision)}`));
      rows.push({ run, profile });
    }
    if (!rows.length) {
      host.innerHTML = '<div class="skill-state">No executions recorded for this Profile.</div>';
      return;
    }
    host.innerHTML = rows.map(({run, profile}) => `
      <article class="skill-run">
        <header><strong>${esc(run.executionId || run.assignmentId)}</strong><span class="skill-status ${statusClass(run.status)}">${esc(run.status)}</span></header>
        <small>Profile r${esc(run.profileRevision)} · provider ${esc(run.providerId || "—")} · runtime ${esc(run.runtimeId || "—")} · worker ${esc(run.workerId || "—")}</small>
        <div>${(profile.skill_refs || []).length ? profile.skill_refs.map((ref)=>`<code>${esc(refLabel(ref))}</code>`).join("") : "<small>No Skills pinned.</small>"}</div>
        <small>Context asset selection is shown only when persisted by the execution record; helper assets are never implied to have executed.</small>
      </article>`).join("");
  } catch (error) {
    host.innerHTML = `<div class="skill-state negative">${esc(errorText(error))}</div>`;
  }
}

function bindDetail(root) {
  const item = state.selected;
  query(root, "[data-skill-revision]")?.addEventListener("change", (event) => selectSkill(root, item.skillId, Number(event.target.value)));
  query(root, "[data-skill-edit]")?.addEventListener("click", () => showEditor(root, item));
  query(root, "[data-skill-publish]")?.addEventListener("click", () => mutation(root,
    `/api/skills/${encodeURIComponent(item.skillId)}/revisions/${encodeURIComponent(item.recordId)}/publish`,
    { reason: "Publish reviewed Skill revision" }
  ));
  query(root, "[data-skill-archive]")?.addEventListener("click", () => mutation(root, `/api/skills/${encodeURIComponent(item.skillId)}/archive`, { reason: "Archive Skill" }));
  query(root, "[data-skill-restore]")?.addEventListener("click", () => mutation(root, `/api/skills/${encodeURIComponent(item.skillId)}/restore`, { reason: "Restore Skill" }));
  query(root, "[data-skill-export]")?.addEventListener("click", async () => {
    const bundle = await request(`/api/skills/${encodeURIComponent(item.skillId)}/export?revision=${encodeURIComponent(item.revision)}`);
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = `${item.skillId}-r${item.revision}.skill.json`;
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  });
  query(root, "[data-skill-attach]")?.addEventListener("click", async () => {
    const profileId = query(root, "[data-skill-profile]")?.value;
    if (!profileId) return;
    await mutation(root, `/api/skills/${encodeURIComponent(item.skillId)}/profiles/${encodeURIComponent(profileId)}?revision=${encodeURIComponent(item.revision)}`, null);
  });
  root.querySelectorAll("[data-skill-detach]").forEach((button) => button.addEventListener("click", async () => {
    await mutation(root, `/api/skills/${encodeURIComponent(item.skillId)}/profiles/${encodeURIComponent(button.dataset.skillDetach)}`, null, "DELETE");
  }));
  query(root, "[data-provenance-load]")?.addEventListener("click", () => {
    const profileId = query(root, "[data-provenance-profile]")?.value;
    if (profileId) loadExecutionProvenance(root, profileId);
  });
}

function bindImport(root) {
  let preview = null;
  query(root, "[data-skill-import-preview]").addEventListener("click", () => {
    const result = query(root, "[data-skill-import-result]");
    const confirm = query(root, "[data-skill-import-confirm]");
    try {
      preview = JSON.parse(query(root, "[data-skill-import-json]").value);
      if (preview?.format !== "codex-web-skill-bundle" || !preview?.manifest?.skill_id || !preview?.manifest?.instructions) {
        throw new Error("Bundle must use codex-web-skill-bundle and include manifest.skill_id and instructions.");
      }
      result.textContent = `Draft import preview\nSkill: ${preview.manifest.name || preview.manifest.skill_id}\nID: ${preview.manifest.skill_id}\nAssets: ${(preview.manifest.assets || []).length}\nPublication: never automatic`;
      confirm.disabled = false;
    } catch (error) {
      preview = null;
      confirm.disabled = true;
      result.textContent = error.message;
    }
  });
  query(root, "[data-skill-import-confirm]").addEventListener("click", async () => {
    if (!preview) return;
    const result = query(root, "[data-skill-import-result]");
    try {
      const response = await request("/api/skills/import", { method: "POST", body: JSON.stringify(preview) });
      result.textContent = `Created draft ${payload(response).skillId} r${payload(response).revision}. Human review and publish are still required.`;
      await loadSkills(root);
    } catch (error) { result.textContent = errorText(error); }
  });
  query(root, "[data-skill-promote]").addEventListener("click", async () => {
    const result = query(root, "[data-promote-result]");
    try {
      const response = await request("/api/skills/promote-verified", {
        method: "POST",
        body: JSON.stringify({
          skill_id: query(root, "[data-promote-id]").value.trim(),
          name: query(root, "[data-promote-name]").value.trim(),
          procedure: query(root, "[data-promote-procedure]").value,
          source_execution_id: query(root, "[data-promote-execution]").value.trim(),
          evidence_ids: csv(query(root, "[data-promote-evidence]").value),
          reason: "Promote verified procedure to draft Skill for human review",
        }),
      });
      result.hidden = false;
      result.textContent = `Created draft ${payload(response).skillId} r${payload(response).revision}; publication requires a separate review action.`;
      await loadSkills(root);
    } catch (error) {
      result.hidden = false;
      result.textContent = errorText(error);
    }
  });
}

async function install() {
  const root = installCard();
  if (!root) return;
  query(root, "[data-skill-refresh]").addEventListener("click", () => loadSkills(root));
  query(root, "[data-skill-new]").addEventListener("click", () => showEditor(root));
  let timer = null;
  query(root, "[data-skill-search]").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadSkills(root), 220);
  });
  query(root, "[data-skill-lifecycle]").addEventListener("change", () => loadSkills(root));
  bindImport(root);
  await Promise.all([loadProfiles(), loadSkills(root)]);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, { once: true });
else install();
