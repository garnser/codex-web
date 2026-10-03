import { confirmAction } from './action_confirmation.js';
import { administrationEditsPending, clearAdministrationEditors, trackAdministrationEditor } from './administration_editor_state.js';
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(value) {
  if (!value) return "Never";
  try {
    return new Date(Number(value) * 1000).toLocaleString();
  } catch {
    return String(value);
  }
}

function fmtDuration(value) {
  const seconds = Number(value || 0);
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} hour(s)`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} minute(s)`;
  return `${seconds} second(s)`;
}

function humanName(context, identityId) {
  const human = (context?.identity?.humans || []).find((item) => item.id === identityId);
  return human?.display_name || identityId;
}

function serviceName(context, serviceId) {
  const service = (context?.identity?.services || []).find((item) => item.id === serviceId);
  return service?.name || serviceId;
}

function sessionRows(context) {
  const items = (context?.identity?.sessions || [])
    .filter((item) => (
      item.organization_id === context.organizationId
      && item.workspace_id === context.workspaceId
      && item.revoked_at == null
    ))
    .sort((a, b) => Number(b.last_seen_at || b.created_at || 0) - Number(a.last_seen_at || a.created_at || 0));
  if (!items.length) return '<div class="workspace-state">No active sessions in this Workspace.</div>';
  return items.map((item) => {
    const current = item.id === context?.actor?.session_id;
    return `
      <article class="administration-user-card" data-auth-session="${esc(item.id)}">
        <header>
          <div>
            <h3>${esc(humanName(context, item.identity_id))}</h3>
            <p>${esc(item.identity_id)} · ${esc(item.assurance || "unknown assurance")}${current ? " · current session" : ""}</p>
          </div>
          <span class="product-status-badge ${current ? "status-positive" : ""}">${current ? "Current" : "Active"}</span>
        </header>
        <p>Created ${esc(fmtTime(item.created_at))} · last seen ${esc(fmtTime(item.last_seen_at))}</p>
        <small>Idle expiry ${esc(fmtTime(item.idle_expires_at))} · absolute expiry ${esc(fmtTime(item.absolute_expires_at))} · step-up until ${esc(fmtTime(item.step_up_until))}</small>
        ${current ? "" : `<button type="button" data-auth-revoke-session="${esc(item.id)}">Revoke session</button>`}
      </article>
    `;
  }).join("");
}

function authenticationStatusView(status) {
  const external = status?.external_identity || {};
  const recovery = status?.recovery || {};
  const providers = (external.providers || [])
    .map((item) => `${item.provider} (${Number(item.linked_identity_count || 0)} linked)`)
    .join(", ");
  const recoveryProviders = (recovery.providers || [])
    .map((item) => `${item.provider} (${Number(item.active_factor_count || 0)} active)`)
    .join(", ");
  const ownership = (status?.field_ownership || []).map((item) => `
    <li><strong>${esc(item.field)}</strong> · ${esc(item.disposition)} · ${esc(item.owner)}<br><small>${esc(item.change_path)}</small></li>
  `).join("");

  return `
    <div class="administration-user-card" data-auth-status-loaded>
      <header>
        <div>
          <h3>${esc(status?.identity_mode || "unknown")} identity mode</h3>
          <p>Current assurance: ${esc(status?.current_assurance || "unknown")} · step-up ${status?.step_up_active ? "active" : "not active"}</p>
        </div>
        <span class="product-status-badge ${status?.step_up_active ? "status-positive" : ""}">${status?.step_up_active ? "Step-up active" : "Step-up required for sensitive changes"}</span>
      </header>
      <p>Session authentication: ${status?.session_authentication_supported ? "supported" : "unavailable"} · service-token authentication: ${status?.service_token_authentication_supported ? "supported" : "unavailable"}.</p>
      <p>External identity mapping: ${external.configured ? esc(providers || "configured") : "not configured"}.</p>
      <small>Linked identities: ${Number(external.linked_identity_count || 0)}. External claims grant authority: ${external.claims_grant_authority ? "yes" : "no — codex-web authorization remains canonical and separately scoped"}.</small>
      <p>Recovery factors: ${recovery.configured ? esc(recoveryProviders || `${Number(recovery.active_factor_count || 0)} active`) : "none configured"}.</p>
      <small>Active Workspace sessions: ${Number(status?.active_session_count || 0)} · active service tokens: ${Number(status?.active_service_token_count || 0)}.</small>
      <details data-auth-field-ownership>
        <summary>Field ownership and change paths</summary>
        <ul>${ownership || "<li>No ownership metadata was returned.</li>"}</ul>
      </details>
    </div>
  `;
}

function authenticationPolicyView(policy) {
  const events = policy?.audit_events || [];
  const latest = events.at(-1);
  const ownership = (policy?.field_ownership || []).map((item) => (
    `${item.field}: ${item.disposition} (${item.owner})`
  )).join(" · ");
  return `
    <article class="administration-user-card" data-auth-policy-loaded>
      <header>
        <div>
          <h3>Workspace policy revision ${Number(policy?.revision || 0)}</h3>
          <p>Source: ${esc(policy?.source || "code_default")} · scope ${esc(policy?.organization_id || "unknown")}/${esc(policy?.workspace_id || "unknown")}</p>
        </div>
        <span class="product-status-badge ${policy?.revision ? "status-positive" : ""}">${policy?.revision ? "Locally managed" : "Defaults active"}</span>
      </header>
      <p>Idle ${esc(fmtDuration(policy?.values?.session_idle_seconds))} · absolute ${esc(fmtDuration(policy?.values?.session_absolute_seconds))} · step-up ${esc(fmtDuration(policy?.values?.step_up_seconds))}.</p>
      <small>${esc(ownership || "Session policy fields are locally managed by codex-web.")}</small>
      ${latest ? `<p>Last change by ${esc(latest.actor_id)} at ${esc(fmtTime(latest.created_at))}: ${esc(latest.reason)}. Revoked sessions: ${Number(latest.invalidated_session_count || 0)}.</p>` : "<p>No local policy mutation has been audited yet.</p>"}
    </article>
  `;
}

function tokenRows(context) {
  const items = (context?.identity?.service_tokens || [])
    .filter((item) => (
      item.organization_id === context.organizationId
      && item.workspace_id === context.workspaceId
    ))
    .sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  if (!items.length) return '<div class="workspace-state">No service tokens have been created in this Workspace.</div>';
  return items.map((item) => {
    const revoked = item.revoked_at != null;
    const expired = item.expires_at != null && Number(item.expires_at) <= Date.now() / 1000;
    const status = revoked ? "Revoked" : expired ? "Expired" : "Active";
    return `
      <article class="administration-user-card" data-auth-token="${esc(item.id)}">
        <header>
          <div>
            <h3>${esc(serviceName(context, item.service_identity_id))}</h3>
            <p>${esc(item.service_identity_id)} · token metadata ${esc(item.id)}</p>
          </div>
          <span class="product-status-badge ${!revoked && !expired ? "status-positive" : ""}">${esc(status)}</span>
        </header>
        <p>Scopes: ${esc((item.scopes || []).join(", ") || "No scopes")}</p>
        <small>Created ${esc(fmtTime(item.created_at))} · expires ${esc(fmtTime(item.expires_at))} · last used ${esc(fmtTime(item.last_used_at))}</small>
        ${Number(item.rotation || 0) > 0 ? `<small>Rotated ${Number(item.rotation)} time(s) · last rotation ${esc(fmtTime(item.rotated_at))}</small>` : ""}
        ${revoked
          ? `<small>Revoked ${esc(fmtTime(item.revoked_at))} · ${esc(item.revoke_reason || "no reason recorded")}</small>`
          : expired
            ? ""
            : `<div class="administration-membership-actions"><button type="button" data-auth-rotate-token="${esc(item.id)}">Rotate token</button><button type="button" data-auth-revoke-token="${esc(item.id)}">Revoke token</button></div>`}
      </article>
    `;
  }).join("");
}

export function renderAdministrationAuthentication(container, {
  context,
  api,
  onChanged = null,
} = {}) {
  if (!container) return;
  if (!context?.allowed) {
    clearAdministrationEditors(container);
    container.innerHTML = '<div class="workspace-state workspace-state-error">Administration access is required.</div>';
    return;
  }
  if (administrationEditsPending(container)) return;
  clearAdministrationEditors(container);

  const services = (context.identity?.services || [])
    .filter((item) => item.disabled_at == null)
    .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));

  container.className = "administration-authentication-surface";
  container.innerHTML = `
    <section class="administration-users-toolbar">
      <div>
        <strong>Authentication & sessions</strong>
        <p>Authentication establishes identity. Operational authorization remains separate and is managed through Memberships and Access.</p>
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-auth-revoke-others>Revoke my other sessions</button>
      </div>
    </section>
    <div class="administration-users-message" data-auth-message role="status" hidden></div>

    <section class="administration-user-create" data-auth-policy-editor>
      <h3>Workspace authentication policy</h3>
      <p>These session controls are locally managed. Changes require administrator MFA/step-up, a current revision, impact preview, confirmation, and an audit reason.</p>
      <div class="workspace-state workspace-state-loading" data-auth-policy-summary role="status">Loading canonical authentication policy…</div>
      <div class="administration-user-create-fields" data-auth-policy-fields>
        <label><span>Idle session lifetime (seconds)</span><input data-auth-policy-idle type="number" min="60" max="86400" required /></label>
        <label><span>Absolute session lifetime (seconds)</span><input data-auth-policy-absolute type="number" min="300" max="604800" required /></label>
        <label><span>Step-up window (seconds)</span><input data-auth-policy-step-up type="number" min="60" max="3600" required /></label>
        <label><span>Audit reason</span><input data-auth-policy-reason type="text" maxlength="500" required placeholder="Why this policy is changing" /></label>
        <label><input data-auth-policy-invalidate type="checkbox" /> Revoke other active Workspace sessions when applying</label>
      </div>
      <div class="administration-membership-actions"><button type="button" data-auth-policy-save>Preview and update policy</button></div>
    </section>

    <section>
      <h3>Active sessions</h3>
      <p>Session revocation is canonical. Revoking another user's session requires administrator MFA/step-up at the API boundary.</p>
      <div data-auth-sessions>${sessionRows(context)}</div>
    </section>

    <section class="administration-user-create">
      <h3>Create service token</h3>
      <p>Service tokens represent non-human identities. The raw token is shown once at creation and is never retrievable from stored token metadata.</p>
      <div class="administration-user-create-fields" data-auth-token-fields>
        <label><span>Service identity</span>
          <select data-auth-service>
            <option value="">Select service identity…</option>
            ${services.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)} · ${esc(item.id)}</option>`).join("")}
          </select>
        </label>
        <label><span>Scopes</span>
          <input data-auth-scopes type="text" placeholder="scope.one, scope.two" />
        </label>
        <label><span>Expiry</span>
          <input data-auth-expiry type="datetime-local" />
        </label>
      </div>
      <div class="administration-membership-actions">
        <button type="button" data-auth-create-token>Create service token</button>
      </div>
      <div data-auth-created-secret hidden></div>
    </section>

    <section>
      <h3>Service token metadata</h3>
      <div data-auth-tokens>${tokenRows(context)}</div>
    </section>

    <section>
      <h3>Authentication mechanisms</h3>
      <p>Configuration and status below come from the canonical identity boundary; external identity claims do not grant codex-web authorization by themselves. SSO/OIDC claims establish identity only and authorization remains separately canonical and scoped.</p>
      <div class="workspace-state workspace-state-loading" data-auth-status role="status">
        Loading canonical authentication status…
      </div>
    </section>
  `;

  const message = container.querySelector("[data-auth-message]");
  const sessions = container.querySelector("[data-auth-sessions]");
  const tokens = container.querySelector("[data-auth-tokens]");
  const service = container.querySelector("[data-auth-service]");
  const scopes = container.querySelector("[data-auth-scopes]");
  const expiry = container.querySelector("[data-auth-expiry]");
  const createButton = container.querySelector("[data-auth-create-token]");
  const revokeOthers = container.querySelector("[data-auth-revoke-others]");
  const createdSecret = container.querySelector("[data-auth-created-secret]");
  const authStatus = container.querySelector("[data-auth-status]");
  const policySummary = container.querySelector("[data-auth-policy-summary]");
  const policyIdle = container.querySelector("[data-auth-policy-idle]");
  const policyAbsolute = container.querySelector("[data-auth-policy-absolute]");
  const policyStepUp = container.querySelector("[data-auth-policy-step-up]");
  const policyReason = container.querySelector("[data-auth-policy-reason]");
  const policyInvalidate = container.querySelector("[data-auth-policy-invalidate]");
  const policySave = container.querySelector("[data-auth-policy-save]");
  // Snapshot request metadata only; the one-time credential output is outside this root.
  const editor = trackAdministrationEditor(container.querySelector('[data-auth-token-fields]'), 'Service token request');
  let tokenMutationPending = false;
  let policyRevision = null;

  const loadAuthenticationStatus = async () => {
    try {
      const status = await api("/api/identity/authentication-status");
      authStatus.className = "";
      authStatus.innerHTML = authenticationStatusView(status);
    } catch (error) {
      authStatus.className = "workspace-state workspace-state-denied";
      authStatus.innerHTML = `
        <strong>Authentication status requires administrator step-up</strong>
        <p>${esc(error?.message || "Canonical authentication status is unavailable.")}</p>
        <small>No authentication configuration is inferred from browser state when the canonical status projection is unavailable.</small>
      `;
    }
  };

  const loadAuthenticationPolicy = async () => {
    try {
      const policy = await api("/api/identity/authentication-policy");
      policyRevision = Number(policy?.revision || 0);
      policyIdle.value = Number(policy?.values?.session_idle_seconds || 3600);
      policyAbsolute.value = Number(policy?.values?.session_absolute_seconds || 43200);
      policyStepUp.value = Number(policy?.values?.step_up_seconds || 900);
      policyReason.value = "";
      policyInvalidate.checked = false;
      policySummary.className = "";
      policySummary.innerHTML = authenticationPolicyView(policy);
    } catch (error) {
      policyRevision = null;
      policySummary.className = "workspace-state workspace-state-denied";
      policySummary.innerHTML = `<strong>Authentication policy requires administrator step-up</strong><p>${esc(error?.message || "Canonical authentication policy is unavailable.")}</p>`;
      policySave.disabled = true;
    }
  };

  queueMicrotask(() => { void Promise.all([loadAuthenticationStatus(), loadAuthenticationPolicy()]); });

  const setMessage = (value, kind = "info") => {
    message.hidden = !value;
    message.dataset.kind = kind;
    message.textContent = value || "";
  };

  policySave.addEventListener("click", async () => {
    if (policyRevision == null) return;
    const payload = {
      session_idle_seconds: Number(policyIdle.value),
      session_absolute_seconds: Number(policyAbsolute.value),
      step_up_seconds: Number(policyStepUp.value),
      invalidate_existing_sessions: policyInvalidate.checked,
      reason: policyReason.value.trim(),
    };
    if (!payload.reason) {
      setMessage("An audit reason is required for authentication policy changes.", "error");
      policyReason.focus();
      return;
    }
    policySave.disabled = true;
    try {
      const preview = await api("/api/identity/authentication-policy/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const confirmed = await confirmAction({
        action: "Update authentication policy",
        target: `${context.organizationId}/${context.workspaceId} revision ${preview.expected_revision}`,
        risk: payload.invalidate_existing_sessions ? "high" : "medium",
        consequence: `${preview.consequence} ${preview.assurance_consequence}`,
        impact: `${Number(preview.invalidated_session_count || 0)} other active session(s) will be revoked. Changed fields: ${(preview.changed_fields || []).join(", ") || "none"}.`,
        recovery: "Publish another policy revision. Revoked sessions must authenticate again.",
        trigger: policySave,
      });
      if (!confirmed) return;
      const result = await api(`/api/identity/authentication-policy?expected_revision=${encodeURIComponent(preview.expected_revision)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (payload.invalidate_existing_sessions) {
        context.identity.sessions = (context.identity.sessions || []).map((item) => (
          item.organization_id === context.organizationId
          && item.workspace_id === context.workspaceId
          && item.id !== context.actor?.session_id
          && item.revoked_at == null
            ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: `authentication-policy-revision:${result?.item?.revision}` }
            : item
        ));
        sessions.innerHTML = sessionRows(context);
      }
      setMessage(`Authentication policy revision ${Number(result?.item?.revision || 0)} saved. ${Number(result?.invalidated_session_count || 0)} session(s) revoked.`);
      await loadAuthenticationPolicy();
    } catch (error) {
      setMessage(error?.message || "Unable to update authentication policy.", "error");
    } finally {
      if (policyRevision != null) policySave.disabled = false;
    }
  });

  revokeOthers.addEventListener("click", async () => {
    if (!await confirmAction({ action: 'Revoke other sessions', target: context.actor?.identity_id || 'Current identity', risk: 'high', consequence: 'All other active sessions of this identity lose access; this session remains.', impact: `${(context.identity.sessions || []).filter(item => item.identity_id === context.actor?.identity_id && item.id !== context.actor?.session_id && item.revoked_at == null).length} other sessions in the loaded canonical inventory.`, recovery: 'Authenticate again to create a new session.', trigger: revokeOthers })) return;
    revokeOthers.disabled = true;
    try {
      const result = await api("/api/identity/sessions/revoke-others", { method: "POST" });
      context.identity.sessions = (context.identity.sessions || []).map((item) => (
        item.identity_id === context.actor?.identity_id
        && item.id !== context.actor?.session_id
        && item.revoked_at == null
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: "revoked-other-sessions" }
          : item
      ));
      sessions.innerHTML = sessionRows(context);
      setMessage(`Revoked ${Number(result?.revoked || 0)} other session(s).`);
    } catch (error) {
      setMessage(error?.message || "Unable to revoke other sessions.", "error");
      revokeOthers.disabled = false;
    }
  });

  sessions.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-auth-revoke-session]");
    if (!button) return;
    const sessionId = button.dataset.authRevokeSession;
    if (!await confirmAction({ action: 'Revoke session', target: sessionId, risk: 'high', consequence: 'This session loses access immediately, including this browser if it is the current session.', recovery: 'Authenticate again to create a new session.', trigger: button })) return;
    button.disabled = true;
    try {
      await api(`/api/identity/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      context.identity.sessions = (context.identity.sessions || []).map((item) => (
        item.id === sessionId
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: `revoked-by:${context.actor?.identity_id || "administrator"}` }
          : item
      ));
      sessions.innerHTML = sessionRows(context);
      setMessage("Session revoked.");
    } catch (error) {
      setMessage(error?.message || "Unable to revoke session.", "error");
      button.disabled = false;
    }
  });

  createButton.addEventListener("click", async () => {
    if (tokenMutationPending || createButton.disabled) return;
    const ticket = editor.submission(), serviceId = service.value;
    if (!service.value) {
      setMessage("Select a service identity before creating a token.", "error");
      return;
    }
    const scopeValues = String(scopes.value || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    const expiresAt = expiry.value ? new Date(expiry.value).getTime() / 1000 : null;
    if (expiry.value && !Number.isFinite(expiresAt)) {
      setMessage("Token expiry is invalid.", "error");
      return;
    }
    createButton.disabled = true;
    tokenMutationPending = true;
    createdSecret.hidden = true;
    createdSecret.textContent = "";
    try {
      const result = await api("/api/identity/service-tokens", {
        method: "POST",
        body: JSON.stringify({
          service_identity_id: serviceId,
          organization_id: context.organizationId,
          workspace_id: context.workspaceId,
          scopes: scopeValues,
          expires_at: expiresAt,
        }),
      });
      if (!ticket.current()) return;
      ticket.saved();
      createdSecret.hidden = false;
      createdSecret.innerHTML = `
        <div class="workspace-state workspace-state-warning" role="status">
          <strong>Copy this token now</strong>
          <p data-auth-created-token>${esc(result.token || "")}</p>
          <small>This secret is returned once. Stored Administration state contains metadata only.</small>
        </div>
      `;
      context.identity.service_tokens = [
        {
          id: result.token_id,
          service_identity_id: serviceId,
          organization_id: context.organizationId,
          workspace_id: context.workspaceId,
          scopes: scopeValues,
          created_at: Date.now() / 1000,
          expires_at: result.expires_at ?? expiresAt,
          last_used_at: null,
          revoked_at: null,
        },
        ...(context.identity.service_tokens || []),
      ];
      tokens.innerHTML = tokenRows(context);
      setMessage(editor.dirty() ? 'Service token created. Newer request edits remain unsaved.' : 'Service token created.');
    } catch (error) {
      if (ticket.current()) setMessage(error?.message || "Unable to create service token.", "error");
    } finally {
      tokenMutationPending = false;
      createButton.disabled = false;
    }
  });

  tokens.addEventListener("click", async (event) => {
    const rotateButton = event.target.closest("[data-auth-rotate-token]");
    if (rotateButton) {
      if (tokenMutationPending || rotateButton.disabled) return;
      const ticket = editor.submission();
      const tokenId = rotateButton.dataset.authRotateToken;
      if (!await confirmAction({ action: 'Rotate service token', target: tokenId, risk: 'high', consequence: 'The previous credential stops working immediately. Update every consumer with the replacement.', recovery: 'The replacement is shown once; rotation does not recover the previous credential.', trigger: rotateButton })) return;
      if (!ticket.current() || tokenMutationPending) return;
      tokenMutationPending = true;
      rotateButton.disabled = true;
      createdSecret.hidden = true;
      createdSecret.textContent = "";
      try {
        const result = await api(`/api/identity/service-tokens/${encodeURIComponent(tokenId)}/rotate`, { method: "POST" });
        if (!ticket.current()) return;
        const rotatedAt = Date.now() / 1000;
        context.identity.service_tokens = (context.identity.service_tokens || []).map((item) => (
          item.id === tokenId
            ? {
                ...item,
                last_used_at: null,
                rotation: Number(item.rotation || 0) + 1,
                rotated_at: rotatedAt,
                rotated_by: context.actor?.identity_id || null,
              }
            : item
        ));
        tokens.innerHTML = tokenRows(context);
        createdSecret.hidden = false;
        createdSecret.innerHTML = `
          <div class="workspace-state workspace-state-warning" role="status">
            <strong>Copy this rotated token now</strong>
            <p data-auth-created-token>${esc(result.token || "")}</p>
            <small>The previous secret is invalid. This replacement secret is returned once and is not stored in Administration state.</small>
          </div>
        `;
        setMessage("Service token rotated.");
      } catch (error) {
        if (ticket.current()) setMessage(error?.message || "Unable to rotate service token.", "error");
      } finally {
        tokenMutationPending = false;
        rotateButton.disabled = false;
      }
      return;
    }

    const button = event.target.closest("[data-auth-revoke-token]");
    if (!button) return;
    const tokenId = button.dataset.authRevokeToken;
    if (!await confirmAction({ action: 'Revoke service token', target: tokenId, risk: 'high', consequence: 'Consumers using this token lose access. Unregistered consumers cannot be enumerated here.', recovery: 'Revocation cannot be undone. Create and bind a replacement token.', trigger: button })) return;
    button.disabled = true;
    try {
      await api(`/api/identity/service-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
      context.identity.service_tokens = (context.identity.service_tokens || []).map((item) => (
        item.id === tokenId
          ? { ...item, revoked_at: Date.now() / 1000, revoke_reason: `revoked-by:${context.actor?.identity_id || "administrator"}` }
          : item
      ));
      tokens.innerHTML = tokenRows(context);
      setMessage("Service token revoked.");
    } catch (error) {
      setMessage(error?.message || "Unable to revoke service token.", "error");
      button.disabled = false;
    }
  });
}
