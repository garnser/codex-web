export function createExecutionPreflightUi({
  api,
  addMessage,
  loadThread,
  scheduleRefresh,
  logEvent,
}) {
  const attemptsByThread = new Map();

  async function load(threadId, limit = 50) {
    try {
      const response = await api(
        `/api/threads/${threadId}/preflight-attempts?limit=${limit}`,
      );
      attemptsByThread.set(
        threadId,
        Array.isArray(response?.items) ? response.items : [],
      );
    } catch (error) {
      logEvent?.("preflight.error", { message: error.message });
      attemptsByThread.set(threadId, []);
    }
  }

  function render(threadId) {
    [...(attemptsByThread.get(threadId) || [])]
      .reverse()
      .forEach((attempt) => {
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

        const context = [
          attempt?.execution_profile_id
            ? `Profile: ${attempt.execution_profile_id}`
            : "",
          attempt?.repository_resource_id
            ? `Repository: ${attempt.repository_resource_id}`
            : "",
          attempt?.read_only_repository_resource_ids?.length
            ? `Read-only repositories: ${attempt.read_only_repository_resource_ids.join(", ")}`
            : "",
          attempt?.sandbox ? `Sandbox: ${attempt.sandbox}` : "",
          attempt?.approval_policy
            ? `Approval: ${attempt.approval_policy}`
            : "",
        ].filter(Boolean);
        const lines = [
          `Preflight attempt ${Number(attempt?.attempt_number || 1)} · ${status}`,
          correlation ? `Correlation: ${correlation}` : "",
          ...context,
          ...blockers.map((blocker) => {
            const target = blocker?.target_type
              ? ` Target: ${blocker.target_type}${blocker?.target_id ? `/${blocker.target_id}` : ""}.`
              : "";
            const remediation = blocker?.remediation
              ? ` Remediation: ${blocker.remediation}`
              : "";
            return `${blocker?.code || "execution_preflight_blocked"}: ${blocker?.message || "Execution could not start."}${target}${remediation}`;
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

  async function handleError(error, threadId) {
    if (error?.detail?.code !== "execution_preflight_blocked") {
      return false;
    }
    await loadThread(threadId);
    scheduleRefresh(0);
    return true;
  }

  return { load, render, handleError };
}
