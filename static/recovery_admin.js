import { request } from "./api_client.js";

let state = null;

function el(id) { return document.getElementById(id); }
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}
function time(value) {
  if (!value) return "unknown";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
}
function status(message, failed = false) {
  const host = el("recovery-admin-status");
  if (!host) return;
  host.textContent = message;
  host.classList.toggle("workspace-state-error", failed);
}
function defaultPolicy() {
  return {
    id: "recovery-policy-default",
    version: "1",
    deployment_mode: "local",
    backup_key_id: "",
    destination_id: "local",
    backup_interval_seconds: 3600,
    restore_verification_interval_seconds: 86400,
    retention_count: 30,
    objectives: [
      { data_class: "canonical_state", rpo_seconds: 3600, rto_seconds: 14400 },
      { data_class: "audit", rpo_seconds: 3600, rto_seconds: 14400 },
    ],
    require_audit_integrity: true,
    require_key_manifest: true,
    allow_point_in_time_recovery: false,
  };
}
function renderHealth() {
  const host = el("recovery-health");
  if (!host || !state) return;
  const health = state.health || {};
  const policy = state.policy;
  host.innerHTML = `
    <div class="comm-entry">
      <strong>Recovery qualified: ${health.recovery_qualified ? "yes" : "no"}</strong>
      <small>Policy configured: ${health.policy_configured ? "yes" : "no"} · RPO: ${health.rpo_satisfied ? "satisfied" : "not satisfied"} · RTO: ${health.rto_satisfied ? "satisfied" : "not satisfied"}</small>
      <small>Latest backup: ${esc(health.latest_backup_id || "none")} · age ${esc(health.latest_backup_age_seconds ?? "unknown")}s</small>
      <small>Latest restore verification: ${esc(health.latest_restore_verification_id || "none")} · passed: ${health.latest_restore_passed ? "yes" : "no"}</small>
      <small>Blockers: ${esc((health.blockers || []).join(", ") || "none")}</small>
      <small>Schedules: backup ${esc(state.backup_schedule_id || "none")} · verification ${esc(state.verification_schedule_id || "none")}</small>
      ${policy ? `<small>Policy ${esc(policy.id)}@${esc(policy.version)} · key ref ${esc(policy.backup_key_id)} · destination ${esc(policy.destination_id)}</small>` : ""}
    </div>`;
}
function verificationFor(backupId) {
  return (state?.verifications || []).find((item) => item.backup_id === backupId) || null;
}
function renderBackups() {
  const host = el("recovery-backups");
  if (!host || !state) return;
  const backups = state.backups || [];
  host.innerHTML = backups.length ? backups.map((item) => {
    const verification = verificationFor(item.id);
    return `<div class="comm-entry" data-recovery-backup="${esc(item.id)}">
      <strong>${esc(item.id)} · ${esc(item.document_count)} document(s)</strong>
      <small>Created: ${time(item.created_at)} · expires: ${time(item.expires_at)}</small>
      <small>Policy: ${esc(item.policy_id)}@${esc(item.policy_version)} · destination: ${esc(item.destination_id)} · state schema: ${esc(item.state_schema_version)}</small>
      <small>Envelope SHA-256: ${esc(item.envelope_sha256)} · key ref: ${esc(item.key_id)}:v${esc(item.key_version)}</small>
      <small>Restore verification: ${verification ? `${esc(verification.status)} · ${time(verification.verified_at)} · evidence ${esc(verification.evidence_id || "none")}` : "not yet verified"}</small>
      <button type="button" class="ghost-button" data-verify-backup="${esc(item.id)}">Verify isolated restore</button>
    </div>`;
  }).join("") : '<div class="comm-entry"><strong>No backups recorded.</strong><small>Create a backup after configuring a valid policy and backup key reference.</small></div>';
}
async function refresh() {
  status("Loading canonical recovery policy, health and backup evidence…");
  try {
    state = await request("/api/recovery/status");
    const area = el("recovery-policy-json");
    if (area) area.value = JSON.stringify(state.policy || defaultPolicy(), null, 2);
    renderHealth();
    renderBackups();
    status(`${state.backups?.length || 0} backup(s) · ${state.verifications?.length || 0} restore verification(s).`);
  } catch (error) {
    status(`Recovery state unavailable: ${error.message}`, true);
  }
}
function parsedPolicy() {
  const area = el("recovery-policy-json");
  let policy;
  try { policy = JSON.parse(area?.value || "{}"); }
  catch (error) { throw new Error(`Policy JSON is invalid: ${error.message}`); }
  if (!policy || Array.isArray(policy) || typeof policy !== "object") throw new Error("Recovery policy must be a JSON object.");
  if (!String(policy.backup_key_id || "").trim()) throw new Error("backup_key_id is required and must reference a managed backup key.");
  if (Number(policy.backup_interval_seconds) < 60) throw new Error("backup_interval_seconds must be at least 60.");
  if (Number(policy.restore_verification_interval_seconds) < 300) throw new Error("restore_verification_interval_seconds must be at least 300.");
  if (Number(policy.retention_count) < 1) throw new Error("retention_count must be at least 1.");
  if (!Array.isArray(policy.objectives) || !policy.objectives.length) throw new Error("At least one RPO/RTO objective is required.");
  return policy;
}
async function savePolicy() {
  if (!el("recovery-policy-confirm")?.checked) throw new Error("Review and confirm recovery-policy impact before applying.");
  const policy = parsedPolicy();
  status("Validating and applying canonical recovery policy…");
  await request("/api/recovery/policy", { method: "PUT", body: JSON.stringify(policy) });
  el("recovery-policy-confirm").checked = false;
  await refresh();
}
async function createBackup() {
  if (!state?.policy) throw new Error("Configure a recovery policy before creating a backup.");
  if (!window.confirm("Create an encrypted backup now using the configured key and destination? Retention enforcement may remove older backup manifests.")) return;
  status("Creating encrypted backup…");
  await request("/api/recovery/backups", { method: "POST" });
  await refresh();
}
async function verifyBackup(backupId) {
  if (!window.confirm(`Verify backup ${backupId} by isolated restore? Verification publishes evidence but does not enable restored side effects.`)) return;
  status(`Verifying isolated restore for ${backupId}…`);
  await request(`/api/recovery/backups/${encodeURIComponent(backupId)}/verify`, { method: "POST" });
  await refresh();
}
function run(button, fn) {
  button?.addEventListener("click", async () => {
    button.disabled = true;
    try { await fn(); } catch (error) { status(error.message || "Recovery operation failed.", true); }
    finally { button.disabled = false; }
  });
}
function bind() {
  run(el("refresh-recovery"), refresh);
  run(el("save-recovery-policy"), savePolicy);
  run(el("create-recovery-backup"), createBackup);
  el("recovery-backups")?.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-verify-backup]");
    if (button) void verifyBackup(button.dataset.verifyBackup).catch((error) => status(error.message, true));
  });
  el("recovery-management-panel")?.addEventListener("toggle", (event) => {
    if (event.currentTarget.open && !state) void refresh();
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind, { once: true });
else bind();
