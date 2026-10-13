// Presentation of canonical queue API/event state; grants no admission authority.
export function applyQueueAdmissionStatus(state, status) {
  const message = [
    status.humanSlackPriorityCount > 0 ? status.humanSlackPriorityReason : null,
    status.automationWaitReason?.message,
  ].filter(Boolean).join(" ");
  if (message) state.queueAdmissionMessageByThread.set(status.threadId, message);
  else state.queueAdmissionMessageByThread.delete(status.threadId);
}

export function renderQueueAdmissionStatus(state, doc = document) {
  const notice = doc.getElementById("queue-admission-status");
  if (!notice) return;
  const message = state.queueAdmissionMessageByThread.get(state.threadId);
  notice.textContent = message || "";
  notice.hidden = !message;
}
