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
    const acceptConfirmation = async () => {
      const dialog = document.querySelector('[data-action-confirmation]');
      if (!dialog) throw new Error('Expected a consequence confirmation');
      const closed = new Promise(resolve => dialog.addEventListener('close', resolve, { once: true }));
      dialog.querySelector('[data-action-ack]')?.click(); dialog.querySelector('[data-action-apply]').click();
      await closed;
    };
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
    await acceptConfirmation();
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-auth-revoke-session='session-other']").click();
    await acceptConfirmation();
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-auth-revoke-token='token-a']").click();
    await acceptConfirmation();
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
    const acceptConfirmation = async () => {
      const dialog = document.querySelector('[data-action-confirmation]');
      if (!dialog) throw new Error('Expected a consequence confirmation');
      const closed = new Promise(resolve => dialog.addEventListener('close', resolve, { once: true }));
      dialog.querySelector('[data-action-ack]')?.click(); dialog.querySelector('[data-action-apply]').click();
      await closed;
    };
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
    await acceptConfirmation();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const sessionMessage = host.querySelector("[data-auth-message]").textContent;
    const sessionGone = !host.querySelector("[data-auth-session='session-other']");

    host.querySelector("[data-auth-revoke-token='token-a']").click();
    await acceptConfirmation();
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
    const acceptConfirmation = async () => {
      const dialog = document.querySelector('[data-action-confirmation]');
      if (!dialog) throw new Error('Expected a consequence confirmation');
      const closed = new Promise(resolve => dialog.addEventListener('close', resolve, { once: true }));
      dialog.querySelector('[data-action-ack]')?.click(); dialog.querySelector('[data-action-apply]').click();
      await closed;
    };
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
    await acceptConfirmation();
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

async function tokenDraft(page, mode = 'held') {
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(async mode => {
    const { renderAdministrationAuthentication } = await import('/static/administration_authentication.js');
    const host = document.createElement('div'); host.id = 'token-draft'; document.body.appendChild(host);
    const context = { allowed: true, organizationId: 'org-a', workspaceId: 'workspace-a', actor: {}, identity: {
      services: [{ id: 'service-a', name: 'A' }, { id: 'service-b', name: 'B' }], humans: [], sessions: [], service_tokens: [],
    } };
    window.tokenDraftContext = context; window.tokenDraftPosts = 0;
    const options = { context, api: async (path, options) => {
      if (options?.method !== 'POST') return {};
      window.tokenDraftPosts += 1;
      if (mode === 'failure') throw new Error('Request metadata denied');
      return new Promise(resolve => { window.finishTokenDraft = resolve; });
    } };
    renderAdministrationAuthentication(host, options);
    window.refreshTokenDraft = () => renderAdministrationAuthentication(host, options);
    window.denyTokenDraft = () => renderAdministrationAuthentication(host, { ...options, context: { allowed: false } });
  }, mode);
}

test('failed token metadata request survives refresh and supports deliberate discard', async ({ page }) => {
  await tokenDraft(page, 'failure');
  const host = page.locator('#token-draft');
  await host.locator('[data-auth-service]').selectOption('service-a');
  await host.locator('[data-auth-scopes]').fill('automation.run');
  await host.locator('[data-auth-create-token]').click();
  await expect(host.locator('[data-auth-message]')).toContainText('Request metadata denied');
  await page.evaluate(() => window.refreshTokenDraft());
  await expect(host.locator('[data-auth-scopes]')).toHaveValue('automation.run');
  page.once('dialog', dialog => dialog.accept());
  await host.locator('[data-administration-discard]').click();
  await expect(host.locator('[data-auth-scopes]')).toHaveValue('');
  await expect(host.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
});

test('late token creation preserves newer metadata and attributes the accepted request to its original service', async ({ page }) => {
  await tokenDraft(page);
  const host = page.locator('#token-draft');
  await host.locator('[data-auth-service]').selectOption('service-a');
  await host.locator('[data-auth-scopes]').fill('automation.run');
  await host.locator('[data-auth-create-token]').click();
  await expect.poll(() => page.evaluate(() => window.tokenDraftPosts)).toBe(1);
  await host.locator('[data-auth-service]').selectOption('service-b');
  await host.locator('[data-auth-scopes]').fill('newer.scope');
  await page.evaluate(() => window.finishTokenDraft({ token_id: 'created-token', token: 'synthetic-one-time-value' }));
  await expect(host.locator('[data-auth-message]')).toContainText('Newer request edits remain unsaved');
  await expect(host.locator('[data-auth-scopes]')).toHaveValue('newer.scope');
  expect(await page.evaluate(() => window.tokenDraftContext.identity.service_tokens[0].service_identity_id)).toBe('service-a');
  page.once('dialog', dialog => dialog.accept());
  await host.locator('[data-administration-discard]').click();
  await expect(host.locator('[data-auth-service]')).toHaveValue('service-a');
  await expect(host.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  expect(await page.evaluate(() => JSON.stringify(window.tokenDraftContext).includes('synthetic-one-time-value'))).toBe(false);
});

test('late one-time token response stays out of a different Project view', async ({ page }) => {
  await tokenDraft(page);
  const host = page.locator('#token-draft');
  await host.locator('[data-auth-service]').selectOption('service-a');
  await host.locator('[data-auth-create-token]').click();
  await expect.poll(() => page.evaluate(() => window.tokenDraftPosts)).toBe(1);
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'another-project' } }));
    window.finishTokenDraft({ token_id: 'created-token', token: 'synthetic-one-time-value' });
  });
  await expect(host.locator('[data-auth-create-token]')).toBeEnabled();
  await expect(host.locator('[data-auth-created-secret]')).toBeHidden();
  await expect(host.locator('[data-auth-created-token]')).toHaveCount(0);
});

test('denied Administration projection replaces unsaved token metadata controls', async ({ page }) => {
  await tokenDraft(page);
  await page.locator('#token-draft [data-auth-scopes]').fill('Unsaved metadata');
  await page.evaluate(() => window.denyTokenDraft());
  await expect(page.locator('#token-draft')).toContainText('Administration access is required');
  await expect(page.locator('#token-draft input')).toHaveCount(0);
});
