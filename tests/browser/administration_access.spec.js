const { test, expect } = require("@playwright/test");

test("Administration Access renders canonical direct, delegated and inherited effective grants", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAccess } = await import("/static/administration_access.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: {
        humans: [
          { id: "human-a", display_name: "Alice", disabled_at: null },
          { id: "human-disabled", display_name: "Disabled", disabled_at: 1 },
        ],
        memberships: [
          {
            identity_id: "human-a",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            revoked_at: null,
          },
          {
            identity_id: "human-disabled",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            revoked_at: null,
          },
        ],
      },
    };
    const api = async (path) => {
      calls.push(path);
      if (path === "/api/projects") return [{ id: "project-a", name: "Project A" }];
      if (path.startsWith("/api/authority/roles")) return { items: [{ id: "developer", name: "Developer", description: "Repository development access" }] };
      if (path.startsWith("/api/resources")) {
        return { items: [{ id: "repo-a", name: "Repository A", resource_type: "repository" }] };
      }
      if (path.startsWith("/api/authority/effective")) {
        return {
          assignments: [
            {
              source_type: "binding",
              source_id: "binding-a",
              role_id: "developer",
              project_ids: ["project-a"],
            },
            {
              source_type: "delegation",
              source_id: "delegation-a",
              role_id: "reviewer",
              project_ids: ["project-a"],
            },
          ],
          permission_matrix: [
            {
              capability: "repository.write",
              level: "execute",
              role_id: "developer",
              grant_id: "developer.repo",
              source_type: "binding",
              source_id: "binding-a",
              inheritance_path: ["developer", "repository-user"],
              project_ids: ["project-a"],
              resource_ids: ["repo-a"],
              resource_types: ["repository"],
              environments: [],
            },
          ],
        };
      }
      throw new Error(`unexpected API ${path}`);
    };

    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAccess(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-identity]").value = "human-a";
    host.querySelector("[data-access-project]").value = "project-a";
    host.querySelector("[data-access-identity]").dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-repository]").value = "repo-a";
    host.querySelector("[data-access-repository]").dispatchEvent(new Event("change"));

    return {
      calls,
      userOptions: [...host.querySelectorAll("[data-access-identity] option")].map((item) => item.textContent),
      text: host.textContent,
      sources: [...host.querySelectorAll("[data-access-source]")].map((item) => item.dataset.accessSource),
    };
  });

  expect(result.userOptions).toEqual(["Select a human user…", "Alice"]);
  expect(result.calls).toContain("/api/projects");
  expect(result.calls).toContain("/api/resources?resource_type=repository&lifecycle=active");
  expect(result.calls).toContain("/api/authority/effective?identity_id=human-a&project_id=project-a");
  expect(result.sources).toEqual(["binding", "delegation"]);
  expect(result.text).toContain("repository.write");
  expect(result.text).toContain("developer → repository-user");
  expect(result.text).toContain("Repository registration/binding describes where a repository exists");
  expect(result.text).toContain("direct binding binding-a");
});

test("routed Administration Access uses canonical authority endpoint and fails closed on denial", async ({ page }) => {
  const fs = require("fs");
  const path = require("path");
  const html = fs.readFileSync(path.join(__dirname, "product_workspaces_fixture.html"), "utf8");
  const authorityCalls = [];

  await page.route("**/administration/**", async (route) => {
    if (route.request().resourceType() !== "document") return route.continue();
    await route.fulfill({ status: 200, contentType: "text/html", body: html });
  });
  await page.route("**/api/identity/me", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        identity_id: "human-admin",
        organization_id: "org-a",
        workspace_id: "workspace-a",
      }),
    });
  });
  await page.route("**/api/identity", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        humans: [{ id: "human-user", display_name: "Casey", disabled_at: null }],
        memberships: [{
          identity_id: "human-user",
          principal_kind: "human",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          revoked_at: null,
        }],
        teams: [],
      }),
    });
  });
  await page.route("**/api/projects", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ id: "project-a", name: "Project A" }]) });
  });
  await page.route("**/api/resources?*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [{ id: "repo-a", name: "Repo A", resource_type: "repository" }] }) });
  });
  await page.route("**/api/authority/effective?*", async (route) => {
    authorityCalls.push(route.request().url());
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "administrator MFA/step-up required" }),
    });
  });

  await page.goto("http://127.0.0.1:18766/administration/access");
  await expect(page.locator(".administration-access-surface")).toBeVisible();
  await page.locator("[data-access-identity]").selectOption("human-user");
  await expect.poll(() => authorityCalls.length).toBe(1);
  await expect(page.locator("[data-access-message]")).toContainText("administrator MFA/step-up required");
  await expect(page.locator("[data-access-grants]")).toContainText("No effective operational grants");
});


test("Administration Access answers who can access a selected Project and repository from one canonical projection", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAccess } = await import("/static/administration_access.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: { humans: [], memberships: [] },
    };
    const api = async (path) => {
      calls.push(path);
      if (path === "/api/projects") return [{ id: "project-a", name: "Project A" }];
      if (path.startsWith("/api/authority/roles")) return { items: [{ id: "developer", name: "Developer", description: "Repository development access" }] };
      if (path.startsWith("/api/resources")) {
        return { items: [{ id: "repo-a", name: "Repository A", resource_type: "repository" }] };
      }
      if (path === "/api/authority/access-subjects?project_id=project-a&resource_id=repo-a") {
        return {
          items: [{
            identity: { id: "human-a", display_name: "Alice", email: "alice@example.test" },
            assignments: [{
              source_type: "binding",
              source_id: "binding-a",
              role_id: "developer",
            }],
            permission_matrix: [{
              capability: "repository.write",
              level: "execute",
            }],
          }, {
            identity: { id: "human-b", display_name: "Bob", email: null },
            assignments: [{
              source_type: "delegation",
              source_id: "delegation-b",
              role_id: "reviewer",
            }],
            permission_matrix: [{
              capability: "repository.read",
              level: "read",
            }],
          }],
        };
      }
      throw new Error(`unexpected API ${path}`);
    };

    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAccess(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-project]").value = "project-a";
    host.querySelector("[data-access-repository]").value = "repo-a";
    host.querySelector("[data-access-target-load]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    return {
      calls,
      subjectIds: [...host.querySelectorAll("[data-access-subject]")].map((item) => item.dataset.accessSubject),
      text: host.querySelector("[data-access-target-results]")?.textContent,
    };
  });

  expect(result.calls).toContain("/api/authority/access-subjects?project_id=project-a&resource_id=repo-a");
  expect(result.subjectIds).toEqual(["human-a", "human-b"]);
  expect(result.text).toContain("Direct developer");
  expect(result.text).toContain("Delegated reviewer");
  expect(result.text).toContain("repository.write (execute)");
  expect(result.text).toContain("repository.read (read)");
});

test("object-centric Access requires an explicit Project or repository scope before querying", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAccess } = await import("/static/administration_access.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: { humans: [], memberships: [] },
    };
    const api = async (path) => {
      calls.push(path);
      if (path === "/api/projects") return [];
      if (path.startsWith("/api/authority/roles")) return { items: [] };
      if (path.startsWith("/api/resources")) return { items: [] };
      throw new Error("access projection must not be called without explicit scope");
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAccess(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-target-load]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      calls,
      message: host.querySelector("[data-access-message]")?.textContent,
      results: host.querySelector("[data-access-target-results]")?.textContent,
    };
  });

  expect(result.calls).toEqual([
    "/api/projects",
    "/api/resources?resource_type=repository&lifecycle=active",
    "/api/authority/roles",
  ]);
  expect(result.message).toContain("Select a Project or repository");
  expect(result.results).toContain("scope is required");
});


test("Administration Access stages and removes canonical direct assignments with refresh", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAccess } = await import("/static/administration_access.js");
    const calls = [];
    let effectiveCalls = 0;
    let directPresent = false;
    window.confirm = () => true;
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: {
        humans: [{ id: "human-a", display_name: "Alice", disabled_at: null }],
        memberships: [{
          identity_id: "human-a",
          principal_kind: "human",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          revoked_at: null,
        }],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
      if (path === "/api/projects") return [{ id: "project-a", name: "Project A" }];
      if (path.startsWith("/api/resources")) return { items: [{ id: "repo-a", name: "Repository A", resource_type: "repository" }] };
      if (path.startsWith("/api/authority/roles")) {
        return { items: [{ id: "developer", name: "Developer", description: "Repository development access" }] };
      }
      if (path.startsWith("/api/authority/effective")) {
        effectiveCalls += 1;
        return {
          assignments: directPresent ? [{
            source_type: "binding",
            source_id: "binding-a",
            role_id: "developer",
            project_ids: ["project-a"],
          }] : [],
          permission_matrix: [],
        };
      }
      if (path === "/api/authority/direct-bindings" && options.method === "POST") {
        directPresent = true;
        return { status: "pending_approval", binding: { id: "binding-a" } };
      }
      if (path === "/api/authority/direct-bindings/binding-a" && options.method === "DELETE") {
        directPresent = false;
        return { status: "published", binding: { id: "binding-a" } };
      }
      throw new Error(`unexpected API ${path}`);
    };

    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAccess(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));

    host.querySelector("[data-access-identity]").value = "human-a";
    host.querySelector("[data-access-project]").value = "project-a";
    host.querySelector("[data-access-project]").dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-identity]").dispatchEvent(new Event("change"));
    await new Promise((resolve) => setTimeout(resolve, 0));

    host.querySelector("[data-access-role]").value = "developer";
    host.querySelector("[data-access-reason]").value = "Grant Project delivery access";
    host.querySelector("[data-access-add-binding]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    const pendingMessage = host.querySelector("[data-access-message]").textContent;
    const remove = host.querySelector("[data-access-remove-binding]");
    const assignmentVisible = Boolean(remove);
    remove?.click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    return {
      calls,
      effectiveCalls,
      pendingMessage,
      assignmentVisible,
      finalMessage: host.querySelector("[data-access-message]").textContent,
      removeStillPresent: Boolean(host.querySelector("[data-access-remove-binding]")),
    };
  });

  const add = result.calls.find((item) => item.path === "/api/authority/direct-bindings");
  expect(add).toEqual({
    path: "/api/authority/direct-bindings",
    method: "POST",
    body: {
      identity_id: "human-a",
      role_id: "developer",
      project_id: "project-a",
      reason: "Grant Project delivery access",
    },
  });
  expect(result.pendingMessage).toContain("pending independent publication approval");
  expect(result.assignmentVisible).toBe(true);
  expect(result.calls).toContainEqual({
    path: "/api/authority/direct-bindings/binding-a",
    method: "DELETE",
    body: {
      project_id: "project-a",
      reason: "Grant Project delivery access",
    },
  });
  expect(result.finalMessage).toContain("authority reduction published");
  expect(result.removeStillPresent).toBe(false);
  expect(result.effectiveCalls).toBeGreaterThanOrEqual(3);
});

test("Administration Access requires an audit reason before access mutation", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationAccess } = await import("/static/administration_access.js");
    const calls = [];
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: {
        humans: [{ id: "human-a", display_name: "Alice", disabled_at: null }],
        memberships: [{
          identity_id: "human-a",
          principal_kind: "human",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          revoked_at: null,
        }],
      },
    };
    const api = async (path, options = {}) => {
      calls.push({ path, method: options.method || "GET" });
      if (path === "/api/projects") return [];
      if (path.startsWith("/api/resources")) return { items: [] };
      if (path.startsWith("/api/authority/roles")) return { items: [{ id: "developer", name: "Developer" }] };
      if (path.startsWith("/api/authority/effective")) return { assignments: [], permission_matrix: [] };
      throw new Error("mutation should not be called without reason");
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationAccess(host, { context, api });
    await new Promise((resolve) => setTimeout(resolve, 0));
    host.querySelector("[data-access-identity]").value = "human-a";
    host.querySelector("[data-access-role]").value = "developer";
    host.querySelector("[data-access-add-binding]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      calls,
      message: host.querySelector("[data-access-message]").textContent,
    };
  });

  expect(result.message).toContain("change reason is required");
  expect(result.calls.some((item) => item.path === "/api/authority/direct-bindings")).toBe(false);
});
