const { test, expect } = require("@playwright/test");

test("Administration Authentication renders safe session and service-token metadata", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: {
        identity_id: "human-admin",
        session_id: "session-current",
        assurance: "mfa",
      },
      identity: {
        humans: [{ id: "human-admin", display_name: "Admin" }, { id: "human-user", display_name: "User" }],
        services: [{ id: "service-a", name: "Automation Service", disabled_at: null }],
        sessions: [
          {
            id: "session-current",
            identity_id: "human-admin",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            assurance: "mfa",
            created_at: 1,
            last_seen_at: 2,
            idle_expires_at: 100,
            absolute_expires_at: 200,
            step_up_until: 50,
            revoked_at: null,
          },
          {
            id: "session-user",
            identity_id: "human-user",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            assurance: "oidc",
            created_at: 1,
            last_seen_at: 3,
            idle_expires_at: 100,
            absolute_expires_at: 200,
            step_up_until: null,
            revoked_at: null,
          },
        ],
        service_tokens: [{
          id: "token-a",
          service_identity_id: "service-a",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          scopes: ["automation.run"],
          created_at: 1,
          expires_at: 2000000000,
          last_used_at: 2,
          revoked_at: null,
        }],
      },
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api: async () => ({}) });
    return {
      text: host.textContent,
      sessionIds: [...host.querySelectorAll("[data-auth-session]")].map((item) => item.dataset.authSession),
      tokenIds: [...host.querySelectorAll("[data-auth-token]")].map((item) => item.dataset.authToken),
      rawSecretInHtml: host.innerHTML.includes("token_hash"),
    };
  });

  expect(result.sessionIds).toEqual(["session-user", "session-current"]);
  expect(result.tokenIds).toEqual(["token-a"]);
  expect(result.text).toContain("current session");
  expect(result.text).toContain("automation.run");
  expect(result.text).toContain("raw token is shown once");
  expect(result.text).toContain("external identity claims do not grant codex-web authorization");
  expect(result.rawSecretInHtml).toBe(false);
});

test("Administration Authentication creates a service token and shows the secret once", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "mfa" },
      identity: {
        humans: [{ id: "human-admin", display_name: "Admin" }],
        services: [{ id: "service-a", name: "Automation Service", disabled_at: null }],
        sessions: [],
        service_tokens: [],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
      if (path === "/api/identity/service-tokens") {
        return { token_id: "token-new", token: "secret-once", expires_at: null };
      }
      return {};
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api });
    host.querySelector("[data-auth-service]").value = "service-a";
    host.querySelector("[data-auth-scopes]").value = "automation.run, repo.read";
    host.querySelector("[data-auth-create-token]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      calls,
      secret: host.querySelector("[data-auth-created-token]")?.textContent,
      note: host.querySelector("[data-auth-created-secret]")?.textContent,
    };
  });

  expect(result.calls[0]).toEqual({
    path: "/api/identity/service-tokens",
    method: "POST",
    body: {
      service_identity_id: "service-a",
      organization_id: "org-a",
      workspace_id: "workspace-a",
      scopes: ["automation.run", "repo.read"],
      expires_at: null,
    },
  });
  expect(result.secret).toBe("secret-once");
  expect(result.note).toContain("returned once");
});

test("Administration Authentication revokes sessions and tokens through canonical endpoints", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const calls = [];
    window.confirm = () => true;
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "mfa" },
      identity: {
        humans: [{ id: "human-admin", display_name: "Admin" }, { id: "human-user", display_name: "User" }],
        services: [{ id: "service-a", name: "Automation Service", disabled_at: null }],
        sessions: [
          { id: "session-current", identity_id: "human-admin", organization_id: "org-a", workspace_id: "workspace-a", assurance: "mfa", revoked_at: null },
          { id: "session-other", identity_id: "human-user", organization_id: "org-a", workspace_id: "workspace-a", assurance: "oidc", revoked_at: null },
        ],
        service_tokens: [{ id: "token-a", service_identity_id: "service-a", organization_id: "org-a", workspace_id: "workspace-a", scopes: [], revoked_at: null }],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      if (path === "/api/identity/sessions/revoke-others") return { revoked: 2 };
      return { ok: true };
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api });

    host.querySelector("[data-auth-revoke-others]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-auth-revoke-session='session-other']").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-auth-revoke-token='token-a']").click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    return { calls, message: host.querySelector("[data-auth-message]").textContent };
  });

  expect(result.calls).toContainEqual({ path: "/api/identity/sessions/revoke-others", method: "POST" });
  expect(result.calls).toContainEqual({ path: "/api/identity/sessions/session-other", method: "DELETE" });
  expect(result.calls).toContainEqual({ path: "/api/identity/service-tokens/token-a", method: "DELETE" });
  expect(result.message).toContain("Service token revoked");
});


test("Administration Authentication preserves canonical revocation feedback without route refresh", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const calls = [];
    let changed = 0;
    window.confirm = () => true;
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "mfa" },
      identity: {
        humans: [{ id: "human-admin", display_name: "Admin" }, { id: "human-user", display_name: "User" }],
        services: [{ id: "service-a", name: "Service A", disabled_at: null }],
        sessions: [
          { id: "session-current", identity_id: "human-admin", organization_id: "org-a", workspace_id: "workspace-a", assurance: "mfa", revoked_at: null },
          { id: "session-other", identity_id: "human-user", organization_id: "org-a", workspace_id: "workspace-a", assurance: "oidc", revoked_at: null },
        ],
        service_tokens: [
          { id: "token-a", service_identity_id: "service-a", organization_id: "org-a", workspace_id: "workspace-a", scopes: ["run"], revoked_at: null },
        ],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      return path === "/api/identity/sessions/revoke-others" ? { revoked: 1 } : { ok: true };
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, {
      context,
      api,
      onChanged: async () => { changed += 1; },
    });

    host.querySelector("[data-auth-revoke-session='session-other']").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const sessionMessage = host.querySelector("[data-auth-message]").textContent;
    const sessionGone = !host.querySelector("[data-auth-session='session-other']");

    host.querySelector("[data-auth-revoke-token='token-a']").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const tokenMessage = host.querySelector("[data-auth-message]").textContent;
    const tokenButtonGone = !host.querySelector("[data-auth-revoke-token='token-a']");

    return { calls, changed, sessionMessage, sessionGone, tokenMessage, tokenButtonGone };
  });

  expect(result.changed).toBe(0);
  expect(result.sessionMessage).toContain("Session revoked");
  expect(result.sessionGone).toBe(true);
  expect(result.tokenMessage).toContain("Service token revoked");
  expect(result.tokenButtonGone).toBe(true);
});


test("Administration Authentication renders canonical authentication status without inventing authorization", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "mfa" },
      identity: { humans: [], services: [], sessions: [], service_tokens: [] },
    };
    const api = async (path) => {
      calls.push(path);
      if (path === "/api/identity/authentication-status") {
        return {
          identity_mode: "enforced",
          current_assurance: "mfa",
          step_up_active: true,
          session_authentication_supported: true,
          service_token_authentication_supported: true,
          external_identity: {
            configured: true,
            providers: [{ provider: "oidc", linked_identity_count: 3 }],
            linked_identity_count: 3,
            claims_grant_authority: false,
          },
          recovery: {
            configured: true,
            active_factor_count: 2,
            providers: [{ provider: "webauthn", active_factor_count: 2 }],
          },
          active_session_count: 4,
          active_service_token_count: 2,
        };
      }
      return {};
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      calls,
      text: host.querySelector("[data-auth-status]").textContent,
    };
  });

  expect(result.calls).toContain("/api/identity/authentication-status");
  expect(result.text).toContain("enforced identity mode");
  expect(result.text).toContain("oidc (3 linked)");
  expect(result.text).toContain("External claims grant authority: no");
  expect(result.text).toContain("webauthn (2 active)");
  expect(result.text).toContain("Active Workspace sessions: 4");
});

test("Administration Authentication fails closed when canonical status requires step-up", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "primary" },
      identity: { humans: [], services: [], sessions: [], service_tokens: [] },
    };
    const api = async (path) => {
      if (path === "/api/identity/authentication-status") {
        const error = new Error("authentication assurance 'mfa' required");
        error.status = 403;
        throw error;
      }
      return {};
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    return host.querySelector("[data-auth-status]").textContent;
  });

  expect(result).toContain("requires administrator step-up");
  expect(result).toContain("No authentication configuration is inferred");
});


test("Administration Authentication rotates a service token and shows the replacement secret once", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAuthentication } = await import("/static/administration_authentication.js");
    const calls = [];
    window.confirm = () => true;
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      actor: { identity_id: "human-admin", session_id: "session-current", assurance: "mfa" },
      identity: {
        humans: [{ id: "human-admin", display_name: "Admin" }],
        services: [{ id: "service-a", name: "Automation Service", disabled_at: null }],
        sessions: [],
        service_tokens: [{
          id: "token-a",
          service_identity_id: "service-a",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          scopes: ["automation.run"],
          created_at: 1,
          expires_at: 2000000000,
          last_used_at: 2,
          rotation: 0,
          rotated_at: null,
          revoked_at: null,
        }],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      if (path === "/api/identity/authentication-status") return {};
      if (path === "/api/identity/service-tokens/token-a/rotate") {
        return { token_id: "token-a", token: "rotated-secret-once", expires_at: 2000000000 };
      }
      return {};
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAuthentication(host, { context, api });
    host.querySelector("[data-auth-rotate-token='token-a']").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      calls,
      secret: host.querySelector("[data-auth-created-token]")?.textContent,
      message: host.querySelector("[data-auth-message]")?.textContent,
      tokenText: host.querySelector("[data-auth-token='token-a']")?.textContent,
      storedToken: context.identity.service_tokens[0],
      html: host.innerHTML,
    };
  });

  expect(result.calls).toContainEqual({
    path: "/api/identity/service-tokens/token-a/rotate",
    method: "POST",
  });
  expect(result.secret).toBe("rotated-secret-once");
  expect(result.message).toContain("Service token rotated");
  expect(result.tokenText).toContain("Rotated 1 time(s)");
  expect(result.storedToken.rotation).toBe(1);
  expect(result.storedToken.last_used_at).toBeNull();
  expect(result.storedToken).not.toHaveProperty("token");
  expect(result.html).toContain("previous secret is invalid");
});
