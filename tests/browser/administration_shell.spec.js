const { test, expect } = require("@playwright/test");

test("Administration route helpers preserve page and reverse-proxy prefix", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const admin = await import("/static/administration_shell.js");
    return {
      users: admin.currentAdministrationRoute("/administration/users"),
      auth: admin.currentAdministrationRoute("/codex/administration/authentication"),
      project: admin.currentAdministrationRoute("/projects/home/overview"),
      prefixed: admin.administrationPath("access", { prefix: "/codex" }),
      fallback: admin.administrationPath("not-a-page"),
    };
  });

  expect(result.users).toEqual({ prefix: "", page: "users" });
  expect(result.auth).toEqual({ prefix: "/codex", page: "authentication" });
  expect(result.project).toBeNull();
  expect(result.prefixed).toBe("/codex/administration/access");
  expect(result.fallback).toBe("/administration/overview");
});

test("Administration context uses canonical identity state for allowed access", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { loadAdministrationContext } = await import("/static/administration_shell.js");
    const calls = [];
    const api = async (path) => {
      calls.push(path);
      if (path === "/api/identity/me") {
        return {
          identity_id: "human-admin",
          organization_id: "local",
          workspace_id: "default",
        };
      }
      if (path === "/api/identity") {
        return {
          organizations: [{ id: "local", name: "Local" }],
          workspaces: [{ id: "default", organization_id: "local", name: "Default" }],
          humans: [],
          memberships: [],
        };
      }
      throw new Error(`unexpected path: ${path}`);
    };
    return {
      context: await loadAdministrationContext(api),
      calls,
    };
  });

  expect(result.calls).toEqual(["/api/identity/me", "/api/identity"]);
  expect(result.context.allowed).toBe(true);
  expect(result.context.denied).toBe(false);
  expect(result.context.organizationId).toBe("local");
  expect(result.context.workspaceId).toBe("default");
  expect(result.context.identity.organizations).toHaveLength(1);
});

test("Administration context exposes canonical denied state and rethrows non-authorization failures", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { loadAdministrationContext } = await import("/static/administration_shell.js");
    const actor = {
      identity_id: "human-user",
      organization_id: "local",
      workspace_id: "default",
    };
    const deniedApi = async (path) => {
      if (path === "/api/identity/me") return actor;
      const error = new Error("administrator required");
      error.status = 403;
      throw error;
    };
    const failedApi = async (path) => {
      if (path === "/api/identity/me") return actor;
      const error = new Error("identity service unavailable");
      error.status = 500;
      throw error;
    };

    const denied = await loadAdministrationContext(deniedApi);
    let failure = null;
    try {
      await loadAdministrationContext(failedApi);
    } catch (error) {
      failure = { status: error.status, message: error.message };
    }
    return { denied, failure };
  });

  expect(result.denied.allowed).toBe(false);
  expect(result.denied.denied).toBe(true);
  expect(result.denied.reason).toContain("administrator required");
  expect(result.failure).toEqual({
    status: 500,
    message: "identity service unavailable",
  });
});


test("Administration renderer keeps scope visible and disables navigation when denied", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationNavigation } = await import("/static/administration_shell.js");
    const host = document.createElement("div");
    host.id = "administration-test-host";
    document.body.appendChild(host);
    renderAdministrationNavigation(host, {
      page: "access",
      context: {
        denied: true,
        reason: "administrator required",
        organizationId: "org-a",
        workspaceId: "workspace-a",
      },
    });
    return {
      title: host.querySelector("[data-administration-title]")?.textContent,
      purpose: host.querySelector("[data-administration-purpose]")?.textContent,
      scope: host.querySelector(".product-administration-scope")?.textContent,
      denied: host.querySelector("[data-administration-denied]")?.textContent,
      active: host.querySelector('[data-administration-page="access"]')?.getAttribute("aria-current"),
      disabled: [...host.querySelectorAll("[data-administration-page]")].every((button) => button.disabled),
    };
  });

  expect(result.title).toBe("Access");
  expect(result.purpose).toContain("direct, inherited and effective access");
  expect(result.scope).toContain("org-a");
  expect(result.scope).toContain("workspace-a");
  expect(result.denied).toContain("administrator required");
  expect(result.active).toBe("page");
  expect(result.disabled).toBe(true);
});

test("Administration renderer emits navigation through the supplied route callback", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const selected = await page.evaluate(async () => {
    const { renderAdministrationNavigation } = await import("/static/administration_shell.js");
    const host = document.createElement("div");
    document.body.appendChild(host);
    let selectedPage = null;
    renderAdministrationNavigation(host, {
      page: "overview",
      context: {
        allowed: true,
        organizationId: "local",
        workspaceId: "default",
      },
      onNavigate: (pageId) => {
        selectedPage = pageId;
      },
    });
    host.querySelector('[data-administration-page="authentication"]').click();
    return selectedPage;
  });

  expect(selected).toBe("authentication");
});
