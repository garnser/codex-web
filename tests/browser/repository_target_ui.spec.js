const { test, expect } = require("@playwright/test");

async function mirrorProductionStaticMount(page) {
  await page.route("**/static/static/**", async (route) => {
    const url = new URL(route.request().url());
    const corrected = url.pathname.replace("/static/static/", "/static/");
    await route.fulfill({
      status: 302,
      headers: { Location: corrected + url.search },
    });
  });
}

test("explicit repository policy requires a target and submits canonical resource id", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error));
  await mirrorProductionStaticMount(page);

  let threadCreated = false;
  let threadCreates = 0;
  const turnPayloads = [];
  const resources = [
    { id: "repo-app", name: "Application", resource_type: "repository", lifecycle: "active" },
    { id: "repo-platform", name: "Platform", resource_type: "repository", lifecycle: "active" },
  ];
  const project = {
    id: "home",
    name: "Home",
    path: "/workspace/home",
    repository_selection_policy: "explicit",
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/projects") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([project]) });
      return;
    }

    if (path === "/api/projects/home/ui-state") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project,
          executionProfiles: {
            items: [{
              id: "repository-write",
              name: "Repository write",
              repositoryAccess: "write",
              workspaceMode: "repository",
              requiredWorkerCapabilities: [],
            }],
            default_profile_id: "repository-write",
          },
          resources: { items: resources },
          bindings: { items: [] },
          threadSettings: threadCreated ? {
            "thread-1": {
              repository_resource_id: "repo-app",
              read_only_repository_resource_ids: [],
              execution_profile_id: "repository-write",
            },
          } : {},
          channels: { items: [] },
          threads: { data: threadCreated ? [{ id: "thread-1", name: "Targeted", cwd: "/workspace/home" }] : [] },
        }),
      });
      return;
    }

    if (path === "/api/models") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [] }) });
      return;
    }

    if (path === "/api/threads" && request.method() === "POST") {
      threadCreates += 1;
      threadCreated = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "thread-1", name: "Targeted", cwd: "/workspace/home" }),
      });
      return;
    }

    if (path === "/api/threads/thread-1/turns" && request.method() === "POST") {
      turnPayloads.push(JSON.parse(request.postData() || "{}"));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ queued: false }) });
      return;
    }

    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });

  await page.goto("http://127.0.0.1:18766/static/index.html");
  await expect(page.locator("#repository-target-status")).toHaveText("Repository target required per turn");
  await expect(page.locator("#repository-target")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#repository-target option")).toHaveCount(3);

  await page.locator("#prompt").fill("Implement the change");
  await page.locator("#send").click();
  expect(threadCreates).toBe(0);
  expect(turnPayloads).toHaveLength(0);
  await expect(page.locator("#messages")).toContainText("repository_target_missing");

  await page.locator("#repository-target").selectOption("repo-app");
  await expect(page.locator("#repository-target-status")).toContainText("Application");
  await expect(page.locator("#repository-target-status")).toContainText("explicit selection");
  await expect(page.locator("#repository-target")).toHaveAttribute("aria-invalid", "false");

  await page.locator("#send").click();
  await expect.poll(() => turnPayloads.length).toBe(1);
  expect(turnPayloads[0].repository_resource_id).toBe("repo-app");
  expect(pageErrors).toEqual([]);
});

test("thread-bound repository stays visible and contradictory selection is blocked", async ({ page }) => {
  await mirrorProductionStaticMount(page);
  const project = {
    id: "home",
    name: "Home",
    path: "/workspace/home",
    repository_selection_policy: "explicit",
  };
  let turnCalls = 0;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/projects") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([project]) });
      return;
    }
    if (path === "/api/projects/home/ui-state") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project,
          executionProfiles: { items: [], default_profile_id: "repository-write" },
          resources: { items: [
            { id: "repo-app", name: "Application", resource_type: "repository", lifecycle: "active" },
            { id: "repo-platform", name: "Platform", resource_type: "repository", lifecycle: "active" },
          ] },
          bindings: { items: [] },
          threadSettings: {
            "thread-1": {
              repository_resource_id: "repo-app",
              read_only_repository_resource_ids: [],
              execution_profile_id: "repository-write",
            },
          },
          channels: { items: [] },
          threads: { data: [{ id: "thread-1", name: "Existing", cwd: "/workspace/home" }] },
        }),
      });
      return;
    }
    if (path === "/api/models") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ data: [] }) });
      return;
    }
    if (path === "/api/threads/thread-1") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "thread-1", name: "Existing", cwd: "/workspace/home", turns: [] }),
      });
      return;
    }
    if (path === "/api/threads/thread-1/preflight-attempts") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [] }) });
      return;
    }
    if (path === "/api/threads/thread-1/queue") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ queueDepth: 0, active: false }) });
      return;
    }
    if (path === "/api/threads/thread-1/turns") {
      turnCalls += 1;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });

  await page.goto("http://127.0.0.1:18766/static/index.html");
  await page.locator("#threads .item-main").first().click();
  await expect(page.locator("#thread-meta")).toContainText("repository repo-app");
  await expect(page.locator("#repository-target-status")).toContainText("repo-app");
  await expect(page.locator("#repository-target-status")).toContainText("thread/profile binding");

  await page.locator("#repository-target").selectOption("repo-platform");
  await expect(page.locator("#repository-target-status")).toContainText("Repository conflict");
  await page.locator("#prompt").fill("Do not run in the wrong repository");
  await page.locator("#send").click();
  expect(turnCalls).toBe(0);
  await expect(page.locator("#messages")).toContainText("repository_target_conflict");
});
