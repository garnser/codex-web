import { expect, test } from "@playwright/test";

const fixture = "http://127.0.0.1:18766/tests/browser/fixtures/work-items-ui.html";

function workItems(count, offset = 0) {
  return Array.from({ length: count }, (_, index) => {
    const value = offset + index;
    return {
      ref: `group/project#${String(value).padStart(5, "0")}`,
      project_id: "project-a",
      title: `Work Item ${value}`,
      current_stage: "implementation_active",
      current_owner: value % 2 ? "dana" : "carl",
      next_owner: null,
      routingError: null,
      updated_at: 10000 - value,
    };
  });
}

async function installCommonRoutes(page) {
  await page.route("**/api/projects", async (route) => {
    await route.fulfill({
      json: [
        { id: "home", name: "Home", path: "/workspace/home" },
        { id: "project-a", name: "Project A", path: "/workspace/a" },
        { id: "project-b", name: "Project B", path: "/workspace/b" },
      ],
    });
  });
  await page.route("**/api/task-sources", async (route) => {
    await route.fulfill({ json: { items: [], sync: {} } });
  });
  await page.route("**/api/secrets", async (route) => {
    await route.fulfill({ json: { items: [] } });
  });
  await page.route("**/api/work-items/**/runs?**", async (route) => {
    await route.fulfill({
      json: { active: [], items: [], nextCursor: null, hasMore: false, activeTruncated: false },
    });
  });
  await page.route("**/api/work-items/**/operator", async (route) => {
    const ref = decodeURIComponent(route.request().url().split("/api/work-items/")[1].split("/operator")[0]);
    await route.fulfill({
      json: {
        item: {
          ref,
          title: ref,
          current_stage: "implementation_active",
          current_owner: "carl",
          next_owner: null,
          artifact_state: "branch",
          release_gate: false,
          execution: {},
        },
        external: {},
        execution_contract: {},
        diagnostics: [],
        history: { items: [] },
        actions: {},
        execution_policy: {},
      },
    });
  });
}

test("opens in current Project and pages without rendering the full collection", async ({ page }) => {
  await installCommonRoutes(page);
  const pages = new Map([
    ["", { items: workItems(50, 0), nextCursor: "p2", hasMore: true }],
    ["p2", { items: workItems(50, 50), nextCursor: "p3", hasMore: true }],
    ["p3", { items: workItems(50, 100), nextCursor: "p4", hasMore: true }],
  ]);
  const requests = [];
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url.search);
    const cursor = url.searchParams.get("cursor") || "";
    await route.fulfill({ json: pages.get(cursor) || { items: [], nextCursor: null, hasMore: false } });
  });

  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("codex:open-work-items")));

  await expect(page.locator(".work-items-project")).toHaveValue("project-a");
  await expect(page.locator(".work-item-row")).toHaveCount(50);
  expect(requests[0]).toContain("project_id=project-a");
  expect(requests[0]).toContain("limit=50");

  await page.getByRole("button", { name: "Load more" }).click();
  await expect(page.locator(".work-item-row")).toHaveCount(60);
  expect(requests[1]).toContain("cursor=p2");

  await page.getByRole("button", { name: "Load more" }).click();
  await expect(page.locator(".work-item-row")).toHaveCount(60);
  expect(requests[2]).toContain("cursor=p3");
  expect(await page.locator(".work-item-row").count()).toBeLessThanOrEqual(60);
  const perf = await page.evaluate(() => (
    window.__codexFrontendPerf.requestWindowStatus({ sinceMs: 5000 })
  ));
  expect(perf.total).toBeLessThanOrEqual(8);
  expect(perf.repeated).toEqual([]);
  expect(perf.ok).toBeTruthy();
});

test("failed next page keeps loaded rows and exposes retry", async ({ page }) => {
  await installCommonRoutes(page);
  let pageTwoAttempts = 0;
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get("cursor")) {
      await route.fulfill({
        json: { items: workItems(50, 0), nextCursor: "p2", hasMore: true },
      });
      return;
    }
    pageTwoAttempts += 1;
    if (pageTwoAttempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "backend unavailable" }),
      });
      return;
    }
    await route.fulfill({
      json: { items: workItems(50, 50), nextCursor: null, hasMore: false },
    });
  });

  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("codex:open-work-items")));
  await page.getByRole("button", { name: "Load more" }).click();

  await expect(page.locator(".work-item-row")).toHaveCount(50);
  await expect(page.locator(".work-items-status")).toContainText("backend unavailable");
  await expect(page.getByRole("button", { name: "Retry next page" })).toBeVisible();

  await page.getByRole("button", { name: "Retry next page" }).click();
  await expect(page.locator(".work-item-row")).toHaveCount(60);
  await expect(page.locator(".work-items-status")).toContainText("100 Work Items loaded");
});

test("main Project changes replace stale operator context", async ({ page }) => {
  await installCommonRoutes(page);
  const projects = [];
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    projects.push(url.searchParams.get("project_id"));
    await route.fulfill({
      json: {
        items: [{
          ref: `${url.searchParams.get("project_id")}#1`,
          project_id: url.searchParams.get("project_id"),
          title: "Scoped",
          current_stage: "implementation_active",
          current_owner: "carl",
        }],
        nextCursor: null,
        hasMore: false,
      },
    });
  });

  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("codex:open-work-items")));
  await expect(page.locator(".work-items-project")).toHaveValue("project-a");

  await page.evaluate(() => {
    document.body.dataset.projectId = "project-b";
    window.dispatchEvent(new CustomEvent("codex:project-changed", {
      detail: { projectId: "project-b" },
    }));
  });

  await expect(page.locator(".work-items-project")).toHaveValue("project-b");
  await expect.poll(() => projects.at(-1)).toBe("project-b");
  await expect(page.locator(".work-item-row")).toHaveCount(1);
});

test("row window remains bounded for a representative 1,300 item session", async ({ page }) => {
  await installCommonRoutes(page);
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    const cursor = Number(url.searchParams.get("cursor") || 0);
    const next = cursor + 50;
    await route.fulfill({
      json: {
        items: workItems(50, cursor),
        nextCursor: next < 1300 ? String(next) : null,
        hasMore: next < 1300,
      },
    });
  });

  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("codex:open-work-items")));

  for (let pageIndex = 1; pageIndex < 8; pageIndex += 1) {
    await page.getByRole("button", { name: "Load more" }).click();
    await expect(page.locator(".work-item-row")).toHaveCount(60);
  }
  expect(await page.locator(".work-item-row").count()).toBeLessThanOrEqual(60);
  await expect(page.locator(".work-items-window-controls")).toContainText("400 loaded");
});


test("inherits the shared shell active Project without asking again", async ({ page }) => {
  await installCommonRoutes(page);
  const projects = [];
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    projects.push(url.searchParams.get("project_id"));
    await route.fulfill({ json: { items: [], nextCursor: null, hasMore: false } });
  });

  await page.goto(fixture);
  await page.evaluate(() => {
    delete document.body.dataset.projectId;
    document.body.dataset.activeProject = "project-b";
    sessionStorage.removeItem("codex-web-work-item-project");
    window.dispatchEvent(new CustomEvent("codex:open-work-items"));
  });

  await expect(page.locator(".work-items-project")).toHaveValue("project-b");
  await expect.poll(() => projects.at(-1)).toBe("project-b");
  expect(new URL(page.url()).searchParams.get("work_item_project")).toBeNull();
});

test("without active Project context Work Items waits for an explicit selection", async ({ page }) => {
  await installCommonRoutes(page);
  let listRequests = 0;
  await page.route("**/api/work-items?**", async (route) => {
    listRequests += 1;
    await route.fulfill({ json: { items: [], nextCursor: null, hasMore: false } });
  });

  await page.goto(fixture);
  await page.evaluate(() => {
    delete document.body.dataset.projectId;
    delete document.body.dataset.activeProject;
    sessionStorage.removeItem("codex-web-work-item-project");
    history.replaceState({}, "", window.location.pathname);
    window.dispatchEvent(new CustomEvent("codex:open-work-items"));
  });

  await expect(page.locator(".work-items-project")).toHaveValue("");
  await expect(page.locator(".work-items-project option").first()).toHaveText("Select Project…");
  await expect(page.locator(".work-items-list")).toContainText("Select a Project");
  expect(listRequests).toBe(0);
});


test("search stays server-side and composes with cursor pagination", async ({ page }) => {
  await installCommonRoutes(page);
  const requests = [];
  await page.route("**/api/work-items?**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(Object.fromEntries(url.searchParams.entries()));
    const cursor = url.searchParams.get("cursor");
    await route.fulfill({
      json: cursor
        ? { items: workItems(2, 50), nextCursor: null, hasMore: false }
        : { items: workItems(50, 0), nextCursor: "p2", hasMore: true },
    });
  });

  await page.goto(fixture);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("codex:open-work-items")));
  await expect(page.locator(".work-item-row")).toHaveCount(50);

  await page.locator(".work-items-search").fill("platform");
  await expect.poll(() => requests.at(-1)?.q).toBe("platform");
  expect(requests.at(-1)?.cursor).toBeUndefined();

  await page.getByRole("button", { name: "Load more" }).click();
  await expect.poll(() => requests.at(-1)?.cursor).toBe("p2");
  expect(requests.at(-1)?.q).toBe("platform");
  expect(requests.at(-1)?.limit).toBe("50");
});
