import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

// Load the production ES module without changing the repository package type.
const source = await readFile(new URL("../../static/queue_steering.js", import.meta.url), "utf8");
const { reconcileSteeringFailure } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

function fixture() {
  const classes = new Set();
  const messages = [];
  const events = [];
  const args = {
    error: { detail: { retryable: true, message: "Runtime unavailable" } },
    button: { disabled: true, textContent: "Steering..." },
    message: {
      dataset: { queuedId: "queued-1" },
      classList: {
        remove: value => classes.delete(value),
        toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value),
      },
    },
    threadId: "thread-1",
    api: async () => ({ thread: { id: "thread-1" } }),
    refreshQueueStatus: async () => ({ queued: [{ id: "queued-1" }] }),
    hydrateThreadActivity: thread => events.push(thread.id),
    addMessage: (_title, text) => messages.push(text),
    logEvent: event => events.push(event),
  };
  return { args, classes, messages, events };
}

test("retry remains disabled until both canonical reads complete", async () => {
  const { args, classes, messages } = fixture();
  let resolveQueue;
  args.refreshQueueStatus = () => new Promise(resolve => { resolveQueue = resolve; });
  let resolveThread;
  args.api = () => new Promise(resolve => { resolveThread = resolve; });
  const pending = reconcileSteeringFailure(args);
  assert.equal(args.button.disabled, true);
  assert.equal(messages.length, 0);
  resolveQueue({ queued: [{ id: "queued-1" }] });
  await Promise.resolve();
  assert.equal(args.button.disabled, true);
  resolveThread({ thread: { id: "thread-1" } });
  await pending;
  assert.equal(args.button.disabled, false);
  assert.equal(args.button.textContent, "Retry steer");
  assert.ok(classes.has("steering-retryable"));
  assert.match(messages[0], /message remains queued/);
});

test("absent exact queue item cannot be retried", async () => {
  const { args, messages } = fixture();
  args.refreshQueueStatus = async () => ({ queued: [{ id: "different-item" }] });
  await reconcileSteeringFailure(args);
  assert.equal(args.button.disabled, true);
  assert.equal(args.button.textContent, "No longer queued");
  assert.match(messages[0], /no longer queued/);
});

for (const failure of ["queue", "thread", "wrong-thread"]) {
  test(`${failure} reconciliation failure never offers or claims a safe retry`, async () => {
    const { args, messages, events } = fixture();
    if (failure === "queue") args.refreshQueueStatus = async () => undefined;
    if (failure === "thread") args.api = async () => { throw new Error("read timed out"); };
    if (failure === "wrong-thread") args.api = async () => ({ thread: { id: "other" } });
    await reconcileSteeringFailure(args);
    assert.equal(args.button.disabled, true);
    assert.equal(args.button.textContent, "Refresh required");
    assert.match(messages[0], /Reconciliation failed/);
    assert.doesNotMatch(messages[0], /remains queued|refreshed canonical/);
    assert.ok(events.includes("steer.reconcile.error"));
  });
}
