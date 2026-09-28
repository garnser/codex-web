export async function reconcileSteeringFailure({
  error,
  button,
  message,
  threadId,
  api,
  refreshQueueStatus,
  hydrateThreadActivity,
  addMessage,
  logEvent,
}) {
  const retryable = error?.detail?.retryable === true;
  if (button) {
    button.disabled = true;
    button.textContent = "Reconciling...";
  }
  message.classList.remove("steering-retryable");
  const detailMessage = typeof error?.detail?.message === "string"
    ? error.detail.message
    : error.message;
  try {
    const queue = await refreshQueueStatus(threadId);
    if (!Array.isArray(queue?.queued)) throw new Error("Queue state is unavailable");
    const data = await api(`/api/threads/${encodeURIComponent(threadId)}`);
    const thread = data.thread || data;
    if (thread?.id !== threadId) throw new Error("Thread state is unavailable");
    hydrateThreadActivity(thread);
    const queuedId = message.dataset?.queuedId;
    const stillQueued = Boolean(queuedId && queue.queued.some(item => item.id === queuedId));
    message.classList.toggle("steering-retryable", retryable && stillQueued);
    if (button) {
      button.disabled = !stillQueued;
      button.textContent = stillQueued ? (retryable ? "Retry steer" : "Steer now") : "No longer queued";
    }
    addMessage(
      "Queue",
      `${detailMessage} ${stillQueued ? "The message remains queued" : "The message is no longer queued"}; refreshed canonical queue and turn state.`,
      "tool",
      new Date(),
    );
  } catch (refreshError) {
    if (button) button.textContent = "Refresh required";
    logEvent("steer.reconcile.error", { message: refreshError.message });
    addMessage("Queue", `${detailMessage} Reconciliation failed; refresh before retrying.`, "tool", new Date());
  }
}
