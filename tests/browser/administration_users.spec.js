const { test, expect } = require("@playwright/test");

test("Administration user projection is scoped and never treats revoked/disabled roles as active", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { administrationUserView, renderAdministrationUsers } = await import("/static/administration_users.js");
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: {
        humans: [
          {
            id: "human-active",
            display_name: "Active Person",
            email: "active@example.test",
            disabled_at: null,
            external_links: [{ provider: "oidc", issuer: "secret-issuer", subject: "secret-subject" }],
          },
          { id: "human-disabled", display_name: "Disabled Person", disabled_at: 12, external_links: [] },
          { id: "human-other", display_name: "Other Workspace", disabled_at: null, external_links: [] },
        ],
        memberships: [
          {
            id: "membership-active",
            identity_id: "human-active",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            roles: ["member", "approver"],
            team_ids: [],
            revoked_at: null,
          },
          {
            id: "membership-revoked",
            identity_id: "human-active",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: null,
            roles: ["admin"],
            team_ids: [],
            revoked_at: 10,
            revoked_by: "human-admin",
          },
          {
            id: "membership-disabled",
            identity_id: "human-disabled",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            roles: ["admin"],
            team_ids: [],
            revoked_at: null,
          },
          {
            id: "membership-other",
            identity_id: "human-other",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-b",
            roles: ["owner"],
            team_ids: [],
            revoked_at: null,
          },
        ],
        teams: [{
          id: "human-group-1",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          name: "Security reviewers",
          member_identity_ids: ["human-active"],
        }],
      },
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    renderAdministrationUsers(host, { context, api: async () => ({}) });
    return {
      ids: administrationUserView(context).map((entry) => entry.human.id),
      activeStatus: host.querySelector('[data-membership-id="membership-active"]')?.dataset.membershipStatus,
      revokedStatus: host.querySelector('[data-membership-id="membership-revoked"]')?.dataset.membershipStatus,
      disabledStatus: host.querySelector('[data-membership-id="membership-disabled"]')?.dataset.membershipStatus,
      disabledSave: host.querySelector('[data-membership-id="membership-disabled"] [data-save-membership]')?.disabled,
      text: host.textContent,
    };
  });

  expect(result.ids).toEqual(["human-active", "human-disabled"]);
  expect(result.activeStatus).toBe("active");
  expect(result.revokedStatus).toBe("revoked");
  expect(result.disabledStatus).toBe("disabled");
  expect(result.disabledSave).toBe(true);
  expect(result.text).toContain("Human groups");
  expect(result.text).toContain("not Agent Teams/Squads");
  expect(result.text).toContain("identity source: oidc");
  expect(result.text).not.toContain("secret-issuer");
  expect(result.text).not.toContain("secret-subject");
});

test("routed Users administration saves roles and revokes through canonical membership APIs", async ({ page }) => {
  const fs = require("fs");
  const path = require("path");
  const html = fs.readFileSync(path.join(__dirname, "product_workspaces_fixture.html"), "utf8");
  const requests = [];
  let roles = ["member"];
  let revokedAt = null;

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
  await page.route("**/api/identity{,/**}", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/identity") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          organizations: [{ id: "org-a", name: "Organization A" }],
          workspaces: [{ id: "workspace-a", organization_id: "org-a", name: "Workspace A" }],
          humans: [{
            id: "human-user",
            display_name: "Casey User",
            email: "casey@example.test",
            disabled_at: null,
            external_links: [],
          }],
          memberships: [{
            id: "membership-user",
            identity_id: "human-user",
            principal_kind: "human",
            organization_id: "org-a",
            workspace_id: "workspace-a",
            roles,
            team_ids: [],
            revoked_at: revokedAt,
            revoked_by: revokedAt ? "human-admin" : null,
          }],
          teams: [],
        }),
      });
      return;
    }
    if (url.pathname === "/api/identity/memberships/membership-user" && request.method() === "PATCH") {
      const payload = JSON.parse(request.postData() || "{}");
      requests.push({ method: "PATCH", payload });
      roles = payload.roles;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "membership-user", roles, revoked_at: null }),
      });
      return;
    }
    if (url.pathname === "/api/identity/memberships/membership-user" && request.method() === "DELETE") {
      requests.push({ method: "DELETE" });
      revokedAt = 100;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "membership-user", roles, revoked_at: revokedAt, revoked_by: "human-admin" }),
      });
      return;
    }
    await route.continue();
  });

  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("http://127.0.0.1:18766/administration/users");
  await expect(page.locator(".administration-users-surface")).toBeVisible();
  await expect(page.locator('[data-human-id="human-user"]')).toContainText("Casey User");

  await page.locator('[data-membership-id="membership-user"] [data-membership-roles]').selectOption(["admin", "member"]);
  await page.locator('[data-membership-id="membership-user"] [data-save-membership]').click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]).toEqual({ method: "PATCH", payload: { roles: ["admin", "member"] } });
  await expect(page.locator('[data-membership-id="membership-user"]')).toContainText("admin");

  await page.locator('[data-membership-id="membership-user"] [data-revoke-membership]').click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toEqual({ method: "DELETE" });
  await expect(page.locator('[data-membership-id="membership-user"]')).toHaveAttribute("data-membership-status", "revoked");
  await expect(page.locator('[data-membership-id="membership-user"] [data-save-membership]')).toBeDisabled();
});

test("membership mutation authorization failures are explicit instead of optimistic UI state", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");

  const result = await page.evaluate(async () => {
    const { renderAdministrationUsers } = await import("/static/administration_users.js");
    const context = {
      allowed: true,
      organizationId: "org-a",
      workspaceId: "workspace-a",
      identity: {
        humans: [{ id: "human-user", display_name: "User", disabled_at: null, external_links: [] }],
        memberships: [{
          id: "membership-user",
          identity_id: "human-user",
          principal_kind: "human",
          organization_id: "org-a",
          workspace_id: "workspace-a",
          roles: ["member"],
          team_ids: [],
          revoked_at: null,
        }],
        teams: [],
      },
    };
    const host = document.createElement("div");
    document.body.appendChild(host);
    const api = async () => {
      const error = new Error("authentication assurance 'mfa' required");
      error.status = 403;
      throw error;
    };
    renderAdministrationUsers(host, { context, api });
    host.querySelector("[data-save-membership]").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    return {
      message: host.querySelector("[data-administration-users-message]")?.textContent,
      roles: [...host.querySelector("[data-membership-roles]")?.selectedOptions || []].map((option) => option.value),
    };
  });

  expect(result.message).toContain("MFA/step-up");
  expect(result.roles).toEqual(["member"]);
});
