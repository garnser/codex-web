import { expect, test } from "@playwright/test";

const fixture = "http://127.0.0.1:18766/tests/browser/fixtures/frontend-perf.html";

async function openFixture(page) {
  await page.goto(fixture);
  await page.waitForFunction(() => window.frontendPerfReady === true);
}

function workspace(projectId = "project-a", search = "") {
  const rows = Array.from({ length: 50 }, (_, index) => ({
    id: `${projectId}-thread-${index}`,
    name: search ? `${search} ${index}` : `Thread ${index}`,
    updatedAt: 1000 - index,
    status: { type: "idle" },
  }));
  return {
    project: {
      id: projectId,
      name: projectId === "project-a" ? "Project A" : "Project B",
      path: `/workspace/${projectId}`,
    },
    resources: { items: [] },
    bindings: { items: [] },
    threadSettings: {},
    channels: { items: [] },
    threads: {
      data: rows,
      pageSize: 50,
      nextCursor: "next",
      hasMore: true,
    },
    executionProfiles: {
      items: [],
      default_profile_id: "repository-write",
    },
  };
}

test("initial Project load stays within request and payload budgets", async ({ page }) => {
  await page.route("**/api/projects", async (route) => {
    await route.fulfill({
      json: [
        { id: "project-a", name: "Project A", path: "/workspace/project-a" },
      ],
    });
  });
  await page.route("**/api/models", async (route) => {
    await route.fulfill({
      json: {
        data: [{ id: "gpt-test", displayName: "Test" }],
      },
    });
  });
  await page.route("**/api/projects/project-a/ui-state?**", async (route) => {
    await route.fulfill({ json: workspace("project-a") });
  });

  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request, loadProjectUiState } = window.frontendPerfFixture;
    perf.reset();
    const snapshot = await loadProjectUiState({
      api: request,
      projectId: "project-a",
    });
    return {
      rows: snapshot.threads.data.length,
      perf: perf.snapshot(),
      status: perf.budgetStatus(),
    };
  });

  expect(result.rows).toBe(50);
  expect(result.perf.requests).toHaveLength(3);
  expect(result.perf.requests.every((item) => item.bytes < 1_000_000)).toBeTruthy();
  expect(result.status.responseBytes).toBeTruthy();
  expect(result.perf.requests.every((item) => !item.endpoint.includes("project-a-thread"))).toBeTruthy();
});

test("cached Project switch and search each use one bounded workspace request", async ({ page }) => {
  await page.route("**/api/projects/project-b/ui-state?**", async (route) => {
    await route.fulfill({ json: workspace("project-b") });
  });
  await page.route("**/api/projects/project-a/ui-state?**", async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({
      json: workspace("project-a", url.searchParams.get("search") || ""),
    });
  });

  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request, loadProjectUiState } = window.frontendPerfFixture;
    const projects = [
      { id: "project-a", name: "Project A", path: "/workspace/project-a" },
      { id: "project-b", name: "Project B", path: "/workspace/project-b" },
    ];
    const models = [{ id: "gpt-test" }];
    const cachedStatic = {
      project: projects[0],
      executionProfiles: { items: [], default_profile_id: "repository-write" },
    };

    perf.reset();
    await loadProjectUiState({
      api: request,
      projectId: "project-b",
      projects,
      models,
      cachedStatic,
    });
    const switchRequests = perf.snapshot().requests;

    perf.reset();
    await loadProjectUiState({
      api: request,
      projectId: "project-a",
      search: "needle",
      projects,
      models,
      cachedStatic,
    });
    const searchRequests = perf.snapshot().requests;

    return { switchRequests, searchRequests };
  });

  expect(result.switchRequests).toHaveLength(1);
  expect(result.searchRequests).toHaveLength(1);
  expect(result.switchRequests[0].endpoint).toContain("/ui-state?");
  expect(result.searchRequests[0].endpoint).toContain("search");
  expect(result.searchRequests[0].endpoint).not.toContain("needle");
  expect(result.switchRequests[0].bytes).toBeLessThan(1_000_000);
  expect(result.searchRequests[0].bytes).toBeLessThan(1_000_000);
});

test("turn completion is visible with zero HTTP revalidation and measured latency", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request, createProjectUiEventReconciler } = window.frontendPerfFixture;
    perf.reset();
    let renders = 0;
    const state = {
      projectId: "project-a",
      threads: {
        data: [{
          id: "thread-1",
          name: "One",
          updatedAt: 1,
          status: { type: "active" },
        }],
      },
      botBindings: [],
      botChannels: [],
    };
    const reconciler = createProjectUiEventReconciler({
      state,
      api: request,
      renderThreads: () => { renders += 1; },
      reconcileWorkspace: async () => {},
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });
    const event = {
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 1,
      eventPublishedAt: Date.now() / 1000,
      message: {
        method: "turn/completed",
        params: { threadId: "thread-1" },
      },
    };
    if (reconciler.accept(event)) {
      reconciler.handleCodexSummary(event.message, event);
    }
    const metrics = perf.snapshot();
    return {
      renders,
      status: state.threads.data[0].status.type,
      requests: metrics.requests,
      latency: metrics.eventLatencies.at(-1)?.latencyMs ?? null,
    };
  });

  expect(result.renders).toBe(1);
  expect(result.status).toBe("idle");
  expect(result.requests).toEqual([]);
  expect(result.latency).not.toBeNull();
  expect(result.latency).toBeLessThan(250);
});

test("Thread selection is one bounded detail request", async ({ page }) => {
  await page.route("**/api/threads/thread-1?**", async (route) => {
    await route.fulfill({
      json: {
        thread: {
          id: "thread-1",
          name: "One",
          turns: [{ items: [{ type: "agentMessage", text: "small" }] }],
        },
      },
    });
  });

  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request } = window.frontendPerfFixture;
    perf.reset();
    await request("/api/threads/thread-1?message_limit=100");
    return perf.snapshot().requests;
  });

  expect(result).toHaveLength(1);
  expect(result[0].endpoint).toBe("/api/threads/thread-1?message_limit");
  expect(result[0].bytes).toBeLessThan(1_000_000);
});

test("aborted Project load is recorded as aborted and cannot return usable state", async ({ page }) => {
  await page.route("**/api/projects/project-a/ui-state?**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({ json: workspace("project-a") });
  });

  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request, loadProjectUiState } = window.frontendPerfFixture;
    perf.reset();
    const controller = new AbortController();
    const promise = loadProjectUiState({
      api: request,
      projectId: "project-a",
      projects: [{ id: "project-a", name: "Project A", path: "/workspace/project-a" }],
      models: [{ id: "gpt-test" }],
      cachedStatic: {
        project: { id: "project-a" },
        executionProfiles: { items: [] },
      },
      signal: controller.signal,
    }).then(() => "resolved").catch((error) => error.name);
    controller.abort();
    const outcome = await promise;
    return { outcome, metrics: perf.snapshot().requests };
  });

  expect(result.outcome).toBe("AbortError");
  expect(result.metrics.some((item) => item.aborted)).toBeTruthy();
});


test("flags repetitive endpoint bursts deterministically", async ({ page }) => {
  await page.route("**/api/perf-loop?**", async (route) => {
    await route.fulfill({ json: { ok: true } });
  });
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request } = window.frontendPerfFixture;
    perf.reset();
    for (let index = 0; index < 5; index += 1) {
      await request(`/api/perf-loop?item=${index}`);
    }
    return {
      window: perf.requestWindowStatus({ sinceMs: 5000 }),
      status: perf.budgetStatus(),
    };
  });

  expect(result.window.total).toBe(5);
  expect(result.window.ok).toBeFalsy();
  expect(result.window.repeated).toEqual([
    { key: "GET /api/perf-loop?item", count: 5 },
  ]);
  expect(result.status.duplicateRequests).toBeFalsy();
});

test("bounded mixed requests stay within burst budgets", async ({ page }) => {
  await page.route("**/api/perf-a", async (route) => route.fulfill({ json: { ok: true } }));
  await page.route("**/api/perf-b", async (route) => route.fulfill({ json: { ok: true } }));
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const { perf, request } = window.frontendPerfFixture;
    perf.reset();
    await Promise.all([
      request("/api/perf-a"),
      request("/api/perf-b"),
    ]);
    return perf.requestWindowStatus({ sinceMs: 5000 });
  });

  expect(result.ok).toBeTruthy();
  expect(result.total).toBe(2);
  expect(result.repeated).toEqual([]);
});
