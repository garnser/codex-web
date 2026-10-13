// Presentation of canonical queue API/event state; grants no admission authority.
export function applyQueueAdmissionStatus(state, status) {
  const message = status.automationWaitReason?.message;
  if (message) state.automationWaitByThread.set(status.threadId, message);
  else state.automationWaitByThread.delete(status.threadId);
}

export function renderQueueAdmissionStatus(state, doc = document) {
  const notice = doc.getElementById("queue-admission-status");
  if (!notice) return;
  const message = state.automationWaitByThread.get(state.threadId);
  notice.textContent = message || "";
  notice.hidden = !message;
}
