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
    button.disabled = false;
    button.textContent = retryable ? "Retry steer" : "Steer now";
  }
  message.classList.toggle("steering-retryable", retryable);
  const detailMessage = typeof error?.detail?.message === "string"
    ? error.detail.message
    : error.message;
  addMessage(
    "Queue",
    retryable
      ? `${detailMessage} The message remains queued; refreshed canonical queue and turn state.`
      : detailMessage,
    "tool",
    new Date(),
  );
  await refreshQueueStatus(threadId);
  if (!retryable) return;
  try {
    const data = await api(`/api/threads/${encodeURIComponent(threadId)}`);
    hydrateThreadActivity(data.thread || data);
  } catch (refreshError) {
    logEvent("steer.reconcile.error", { message: refreshError.message });
  }
}
