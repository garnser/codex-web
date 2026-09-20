export async function loadExecutionPreflightAttempts({
  api,
  threadId,
  logEvent,
  limit = 50,
}) {
  try {
    const response = await api(
      `/api/threads/${threadId}/preflight-attempts?limit=${limit}`,
    );
    return Array.isArray(response?.items) ? response.items : [];
  } catch (error) {
    logEvent?.("preflight.error", { message: error.message });
    return [];
  }
}

export function renderExecutionPreflightAttempts({
  attempts,
  threadId,
  addMessage,
  api,
  loadThread,
  scheduleRefresh,
}) {
  [...(attempts || [])].reverse().forEach((attempt) => {
    const status = String(attempt?.status || "blocked");
    const correlation = (
      attempt?.correlation_id
      || attempt?.execution_id
      || attempt?.id
      || ""
    );
    const blockers = Array.isArray(attempt?.blockers)
      ? attempt.blockers
      : [];

    if (status !== "started") {
      addMessage(
        status === "retrying" ? "You (retrying)" : "You (blocked)",
        attempt?.message || "",
        "user",
        new Date((attempt?.created_at || Date.now() / 1000) * 1000),
      );
    }

    const lines = [
      `Preflight attempt ${Number(attempt?.attempt_number || 1)} · ${status}`,
      correlation ? `Correlation: ${correlation}` : "",
      ...blockers.map((blocker) => {
        const remediation = blocker?.remediation
          ? ` Remediation: ${blocker.remediation}`
          : "";
        return `${blocker?.code || "execution_preflight_blocked"}: ${blocker?.message || "Execution could not start."}${remediation}`;
      }),
    ].filter(Boolean);

    const card = addMessage(
      status === "started" ? "Preflight resolved" : "Execution blocked",
      lines.join("\n"),
      "tool",
      new Date((attempt?.updated_at || Date.now() / 1000) * 1000),
    );
    if (
      status === "started"
      || !attempt?.can_retry
      || !attempt?.retry_href
    ) {
      return;
    }

    const body = card?.querySelector(".body");
    if (!body) return;
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = status === "retrying"
      ? "Retry in progress"
      : "Retry";
    retry.disabled = status === "retrying";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      retry.textContent = "Retrying…";
      try {
        await api(attempt.retry_href, { method: "POST" });
      } catch (error) {
        if (error?.detail?.code !== "execution_preflight_blocked") {
          addMessage("Error", error.message, "tool", new Date());
        }
      } finally {
        await loadThread(threadId);
        scheduleRefresh(0);
      }
    });
    actions.appendChild(retry);
    body.appendChild(actions);
  });
}
