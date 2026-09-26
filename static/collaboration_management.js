import { request } from "./api_client.js";

const EDITABLE_PROFILE_FIELDS = [
  "name", "avatar_ref", "description", "owner_identity_id", "role_id",
  "role_definition_ref", "instructions_ref", "skill_refs", "access",
  "authority_role_id", "authority_definition_ref", "runtime_policy",
  "model_policy", "execution_profile_id", "sandbox_requirement", "budgets",
];
const EDITABLE_TEAM_FIELDS = [
  "name", "description", "owner_identity_id", "leader_profile_id", "members",
  "instructions_ref", "budgets", "allowed_identity_ids", "allowed_role_ids",
  "escalation_target",
];

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function pick(item, fields) {
  return Object.fromEntries(fields
    .filter((key) => Object.prototype.hasOwnProperty.call(item || {}, key))
    .map((key) => [key, item[key]]));
}

function editorPayload(kind, item) {
  if (!item) {
    return kind === "profile"
      ? { profile_id: "", name: "", description: "", reason: "" }
      : { team_id: "", name: "", description: "", leader_profile_id: "", reason: "" };
  }
  const fields = kind === "profile" ? EDITABLE_PROFILE_FIELDS : EDITABLE_TEAM_FIELDS;
  return { ...pick(item, fields), reason: "" };
}

function endpoint(kind, item) {
  const plural = kind === "profile" ? "agent-profiles" : "agent-teams";
  const id = item?.profile_id || item?.team_id;
  return id ? `/api/${plural}/${encodeURIComponent(id)}` : `/api/${plural}`;
}

function title(kind, item) {
  const noun = kind === "profile" ? "Agent Profile" : "Team";
  return item ? `Edit ${noun} · create revision` : `Create ${noun}`;
}

function dialogShell(titleText, detail) {
  const dialog = document.createElement("dialog");
  dialog.className = "product-section-dialog";
  dialog.innerHTML = `
    <div class="product-section-dialog-shell">
      <header><div><h2></h2><p></p></div><button type="button" class="icon-button" data-close aria-label="Close">×</button></header>
      <div class="route-test" data-body></div>
      <div class="form-result" data-status hidden aria-live="polite"></div>
    </div>`;
  dialog.querySelector("h2").textContent = titleText;
  dialog.querySelector("header p").textContent = detail || "";
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  document.body.appendChild(dialog);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  return dialog;
}

function status(dialog, message, failed = false) {
  const host = dialog.querySelector("[data-status]");
  host.hidden = false;
  host.textContent = message;
  host.classList.toggle("workspace-state-error", failed);
}

function parsePayload(text, kind, creating) {
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    throw new Error(`JSON is invalid: ${error.message}`);
  }
  if (!payload || Array.isArray(payload) || typeof payload !== "object") {
    throw new Error("Payload must be a JSON object.");
  }
  if (creating) {
    const idKey = kind === "profile" ? "profile_id" : "team_id";
    if (!String(payload[idKey] || "").trim()) throw new Error(`${idKey} is required.`);
  }
  if (!String(payload.name || "").trim()) throw new Error("name is required.");
  if (kind === "team" && creating && !String(payload.leader_profile_id || "").trim()) {
    throw new Error("leader_profile_id is required.");
  }
  if (!creating && !String(payload.reason || "").trim()) {
    throw new Error("reason is required for a revision.");
  }
  return payload;
}

export function openEditor(kind, item, { onChanged } = {}) {
  const creating = !item;
  const dialog = dialogShell(
    title(kind, item),
    creating
      ? "Only canonical mutable fields are accepted; server schema and authorization validate before persistence."
      : `Current revision ${item.revision}. Immutable identity and provenance are intentionally excluded.`,
  );
  const body = dialog.querySelector("[data-body]");
  body.innerHTML = `
    <label>Canonical payload
      <textarea data-payload rows="20" spellcheck="false"></textarea>
    </label>
    <small>Changes create a new immutable revision. References remain references; do not paste secret material.</small>
    <button type="button" class="primary-button" data-save>${creating ? "Create" : "Validate & create revision"}</button>`;
  const area = body.querySelector("[data-payload]");
  area.value = JSON.stringify(editorPayload(kind, item), null, 2);
  body.querySelector("[data-save]").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    try {
      const payload = parsePayload(area.value, kind, creating);
      button.disabled = true;
      status(dialog, "Validating canonical payload…");
      const result = await request(endpoint(kind, item), {
        method: creating ? "POST" : "PATCH",
        body: JSON.stringify(payload),
      });
      status(dialog, `Saved revision ${result?.item?.revision || "successfully"}.`);
      await onChanged?.(result?.item);
      dialog.close();
    } catch (error) {
      status(dialog, error.message || "Change rejected.", true);
      button.disabled = false;
    }
  });
  dialog.showModal();
}

function lifecycleImpact(kind, item, action, usage) {
  const noun = kind === "profile" ? "Agent Profile" : "Team";
  const base = action === "restore"
    ? `Restore this ${noun} to active lifecycle.`
    : `${action === "archive" ? "Archive" : "Disable"} this ${noun}. New execution selection will no longer treat it as active.`;
  return usage ? `${base} Impact: ${usage}` : base;
}

export function openLifecycle(kind, item, action, { usage = "", onChanged } = {}) {
  const id = item?.profile_id || item?.team_id;
  const dialog = dialogShell(
    `${action[0].toUpperCase() + action.slice(1)} ${kind === "profile" ? "Agent Profile" : "Team"}`,
    lifecycleImpact(kind, item, action, usage),
  );
  const body = dialog.querySelector("[data-body]");
  body.innerHTML = `
    <label>Change reason <textarea data-reason rows="3" maxlength="1000"></textarea></label>
    <label class="checkbox-line"><input type="checkbox" data-confirm> I reviewed the lifecycle impact above.</label>
    <button type="button" class="primary-button" data-apply>Confirm ${action}</button>`;
  body.querySelector("[data-apply]").addEventListener("click", async (event) => {
    const reason = body.querySelector("[data-reason]").value.trim();
    if (!reason) return status(dialog, "A change reason is required.", true);
    if (!body.querySelector("[data-confirm]").checked) return status(dialog, "Confirm the impact before applying.", true);
    const button = event.currentTarget;
    try {
      button.disabled = true;
      const plural = kind === "profile" ? "agent-profiles" : "agent-teams";
      const result = await request(`/api/${plural}/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      await onChanged?.(result?.item);
      dialog.close();
    } catch (error) {
      status(dialog, error.message || "Lifecycle change rejected.", true);
      button.disabled = false;
    }
  });
  dialog.showModal();
}

export async function openHistory(kind, item) {
  const id = item?.profile_id || item?.team_id;
  const plural = kind === "profile" ? "agent-profiles" : "agent-teams";
  const dialog = dialogShell(
    `${kind === "profile" ? "Agent Profile" : "Team"} revision history`,
    "Immutable revisions, lifecycle, actor, timestamp and change reason.",
  );
  const body = dialog.querySelector("[data-body]");
  body.textContent = "Loading revisions…";
  dialog.showModal();
  try {
    const result = await request(`/api/${plural}/${encodeURIComponent(id)}/revisions`);
    const items = result?.items || [];
    body.innerHTML = items.length ? items.slice().reverse().map((revision) => `
      <article class="comm-entry">
        <strong>Revision ${esc(revision.revision)} · ${esc(revision.lifecycle)}</strong>
        <small>Record: ${esc(revision.record_id)}</small>
        <small>Changed by: ${esc(revision.updated_by || revision.created_by || "unknown")} · ${esc(revision.updated_at ? new Date(revision.updated_at * 1000).toISOString() : "unknown time")}</small>
        <small>Reason: ${esc(revision.change_reason || "none recorded")}</small>
      </article>`).join("") : "<small>No revision history returned.</small>";
  } catch (error) {
    status(dialog, error.message || "Revision history unavailable.", true);
    body.textContent = "";
  }
}

export function managementActions(kind, item, { usage = "", onChanged } = {}) {
  const noun = kind === "profile" ? "Agent Profile" : "Team";
  const group = document.createElement("div");
  group.className = "collab-context-actions";
  group.setAttribute("aria-label", `${noun} actions`);

  const button = (text, handler, { destructive = false, target = group } = {}) => {
    const control = document.createElement("button");
    control.type = "button";
    control.className = destructive
      ? "ghost-button collab-destructive-action"
      : "ghost-button";
    control.textContent = text;
    control.addEventListener("click", handler);
    target.appendChild(control);
    return control;
  };

  button("Edit", () => openEditor(kind, item, { onChanged }));

  const more = document.createElement("details");
  more.className = "collab-context-more";
  const summary = document.createElement("summary");
  summary.className = "ghost-button";
  summary.textContent = "More";
  summary.setAttribute("aria-label", `More ${noun} actions`);
  const menu = document.createElement("div");
  menu.className = "collab-context-menu";
  more.append(summary, menu);
  group.appendChild(more);

  const closeThen = (handler) => () => {
    more.removeAttribute("open");
    handler();
  };
  button("History", closeThen(() => void openHistory(kind, item)), { target: menu });

  const current = String(item?.lifecycle || "active");
  if (current === "archived" || current === "disabled") {
    button(
      "Restore",
      closeThen(() => openLifecycle(kind, item, "restore", { usage, onChanged })),
      { target: menu },
    );
  } else {
    button(
      "Disable",
      closeThen(() => openLifecycle(kind, item, "disable", { usage, onChanged })),
      { target: menu },
    );
    button(
      "Archive",
      closeThen(() => openLifecycle(kind, item, "archive", { usage, onChanged })),
      { destructive: true, target: menu },
    );
  }
  return group;
}
