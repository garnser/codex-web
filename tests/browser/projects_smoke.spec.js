const { test, expect } = require("@playwright/test");

test("renders API projects without page errors and refresh refetches projects", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  let projectRequests = 0;
  let projects = [
    { id: "home", name: "Home", path: "/workspace/home" },
    { id: "veridataops", name: "Veridataops", path: "/workspace/veridataops" },
  ];

  // static/index.html is exercised by the repository-root test server; mirror
  // the production mount where relative static/ URLs resolve from the app root.
  await page.route("**/static/static/**", async (route) => {
    const url = new URL(route.request().url());
    const corrected = url.pathname.replace("/static/static/", "/static/");
    await route.fulfill({
      status: 302,
      headers: { Location: corrected + url.search },
    });
  });

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/projects") {
      projectRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(projects),
      });
      return;
    }

    if (path === "/api/projects/home/ui-state") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          project: projects.find((project) => project.id === "home"),
          executionProfiles: [],
          resources: { items: [] },
          bindings: { items: [] },
          threadSettings: {},
          channels: { items: [] },
          threads: { data: [] },
        }),
      });
      return;
    }

    if (path === "/api/models") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ data: [] }),
      });
      return;
    }

    if (path === "/api/bots/connections") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: "{}",
    });
  });

  await page.goto("http://127.0.0.1:18766/static/index.html");

  const projectSwitcher = page.locator("#product-project-switcher");
  await expect(projectSwitcher.locator("option")).toHaveCount(2);
  await expect(projectSwitcher).toHaveValue("home");
  await expect(page.locator("#projects")).toHaveCount(0);
  expect(projectRequests).toBeGreaterThan(0);
  const requestsBeforeRefresh = projectRequests;
  expect(pageErrors).toEqual([]);

  projects = [
    ...projects,
    { id: "new-project", name: "New Project", path: "/workspace/new-project" },
  ];

  await page.locator("#refresh").click();

  await expect(projectSwitcher.locator("option")).toHaveCount(3);
  await expect(projectSwitcher.locator("option").last()).toHaveText("New Project");
  expect(projectRequests).toBeGreaterThan(requestsBeforeRefresh);

  await page.locator("#new-project").click();
  await expect(page.locator("#project-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#project-dialog")).toBeHidden();

  await page.locator("[data-project-bot-integration]").click();
  await expect(page.locator("#bot-context")).toHaveText("Project: Home");
  expect(pageErrors).toEqual([]);
});
