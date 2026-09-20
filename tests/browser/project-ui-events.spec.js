import { expect, test } from "@playwright/test";

async function openFixture(page) {
  await page.goto(
    "http://127.0.0.1:18766/tests/browser/fixtures/project-ui-events.html",
  );
  await page.waitForFunction(() => window.projectUiEventsReady === true);
}

test("turn and name deltas patch rows without API requests", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const calls = [];
    const state = {
      projectId: "project-a",
      threads: {
        data: [
          {
            id: "thread-1",
            name: "Before",
            updatedAt: 1,
            status: { type: "active" },
          },
        ],
      },
      botBindings: [],
      botChannels: [],
    };
    const reconciler = window.createProjectUiEventReconciler({
      state,
      api: async (path) => {
        calls.push(path);
        return {};
      },
      renderThreads: () => {},
      reconcileWorkspace: async () => calls.push("workspace"),
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });

    const completed = {
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 1,
      eventPublishedAt: 10,
      message: {
        method: "turn/completed",
        params: { threadId: "thread-1" },
      },
    };
    if (reconciler.accept(completed)) {
      reconciler.handleCodexSummary(completed.message, completed);
    }

    const renamed = {
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 2,
      eventPublishedAt: 11,
      message: {
        method: "thread/name/updated",
        params: { threadId: "thread-1", name: "After" },
      },
    };
    if (reconciler.accept(renamed)) {
      reconciler.handleCodexSummary(renamed.message, renamed);
    }
    await new Promise((resolve) => setTimeout(resolve, 120));
    return { calls, row: state.threads.data[0] };
  });

  expect(result.calls).toEqual([]);
  expect(result.row.name).toBe("After");
  expect(result.row.status.type).toBe("idle");
  expect(result.row.updatedAt).toBe(11);
});

test("binding event bursts coalesce into one narrow request", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const calls = [];
    const state = {
      projectId: "project-a",
      threads: { data: [{ id: "thread-1" }] },
      botBindings: [],
      botChannels: [],
    };
    const reconciler = window.createProjectUiEventReconciler({
      state,
      api: async (path) => {
        calls.push(path);
        return {
          bindings: {
            items: [
              {
                id: "binding-1",
                project_id: "project-a",
                thread_id: "thread-1",
                provider: "slack",
                external_conversation_id: "C1",
              },
            ],
          },
          channels: {
            items: [{ provider: "slack", id: "C1", label: "#one" }],
          },
        };
      },
      renderThreads: () => {},
      reconcileWorkspace: async () => {},
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });
    for (let sequence = 1; sequence <= 8; sequence += 1) {
      const event = {
        type: "binding.updated",
        projectId: "project-a",
        threadId: "thread-1",
        eventStreamId: "stream-a",
        eventSequence: sequence,
      };
      if (reconciler.accept(event)) reconciler.handleBindingEvent(event);
    }
    await new Promise((resolve) => setTimeout(resolve, 180));
    return {
      calls,
      bindings: state.botBindings,
      status: reconciler.status(),
    };
  });

  expect(result.calls).toHaveLength(1);
  expect(result.calls[0]).toContain("/ui-state/bindings?thread_id=thread-1");
  expect(result.bindings).toHaveLength(1);
  expect(result.status.pending).toEqual([]);
  expect(result.status.inflight).toEqual([]);
});

test("stale events cannot overwrite newer state", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(() => {
    const state = {
      projectId: "project-a",
      threads: { data: [{ id: "thread-1", name: "Initial" }] },
      botBindings: [],
      botChannels: [],
    };
    const reconciler = window.createProjectUiEventReconciler({
      state,
      api: async () => ({}),
      renderThreads: () => {},
      reconcileWorkspace: async () => {},
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });
    const fresh = {
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 1,
      message: {
        method: "thread/name/updated",
        params: { threadId: "thread-1", name: "Fresh" },
      },
    };
    if (reconciler.accept(fresh)) {
      reconciler.handleCodexSummary(fresh.message, fresh);
    }
    const stale = {
      ...fresh,
      message: {
        method: "thread/name/updated",
        params: { threadId: "thread-1", name: "Stale" },
      },
    };
    if (reconciler.accept(stale)) {
      reconciler.handleCodexSummary(stale.message, stale);
    }
    return state.threads.data[0].name;
  });

  expect(result).toBe("Fresh");
});

test("sequence gaps and missed reconnect events coalesce bounded reconciliation", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    let reconciles = 0;
    const state = {
      projectId: "project-a",
      threads: { data: [] },
      botBindings: [],
      botChannels: [],
    };
    const reconciler = window.createProjectUiEventReconciler({
      state,
      api: async () => ({}),
      renderThreads: () => {},
      reconcileWorkspace: async () => {
        reconciles += 1;
      },
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });
    reconciler.accept({
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 3,
      message: { method: "turn/completed", params: {} },
    });
    reconciler.accept({
      type: "codex.event",
      eventStreamId: "stream-a",
      eventSequence: 5,
      message: { method: "turn/completed", params: {} },
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    const afterGap = reconciles;

    reconciler.noteDisconnect();
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 8 });
    await new Promise((resolve) => setTimeout(resolve, 150));
    return { afterGap, reconciles };
  });

  expect(result.afterGap).toBe(1);
  expect(result.reconciles).toBe(2);
});

test("failed narrow revalidation keeps usable state and clears inflight status", async ({ page }) => {
  await openFixture(page);
  const result = await page.evaluate(async () => {
    const state = {
      projectId: "project-a",
      threads: { data: [{ id: "thread-1", name: "Usable" }] },
      botBindings: [{ id: "old", project_id: "project-a", thread_id: "thread-1" }],
      botChannels: [],
    };
    const reconciler = window.createProjectUiEventReconciler({
      state,
      api: async () => {
        throw new Error("provider unavailable");
      },
      renderThreads: () => {},
      reconcileWorkspace: async () => {},
      logEvent: () => {},
    });
    reconciler.accept({ type: "hello", streamId: "stream-a", sequence: 0 });
    const event = {
      type: "binding.updated",
      projectId: "project-a",
      threadId: "thread-1",
      eventStreamId: "stream-a",
      eventSequence: 1,
    };
    if (reconciler.accept(event)) reconciler.handleBindingEvent(event);
    await new Promise((resolve) => setTimeout(resolve, 180));
    return {
      name: state.threads.data[0].name,
      bindings: state.botBindings.length,
      status: reconciler.status(),
    };
  });

  expect(result.name).toBe("Usable");
  expect(result.bindings).toBe(1);
  expect(result.status.pending).toEqual([]);
  expect(result.status.inflight).toEqual([]);
});
