(() => {
  const INITIAL_LIMIT = 40;
  const PAGE_SIZE = 40;
  const MAX_LIMIT = 1000;
  const LIVE_DOM_CAP = 200;
  const STICKY_BOTTOM_THRESHOLD = 96;

  const requestedLimits = new Map();
  const threadMeta = new Map();
  const locallyPruned = new Set();
  let pendingScrollRestore = null;
  let messagesElement = null;
  let mutationObserver = null;
  let scrollFrame = null;
  let maintenanceFrame = null;
  let stickToBottom = true;
  let reloadThread = null;

  function currentThreadId() {
    const value = document.getElementById("thread-meta")?.textContent || "";
    const separator = value.indexOf(" · ");
    return (separator >= 0 ? value.slice(0, separator) : value).trim() || null;
  }

  function effectiveLimit(threadId) {
    return requestedLimits.get(threadId) || INITIAL_LIMIT;
  }

  function nearBottom(messages = messagesElement) {
    if (!messages) return true;
    return messages.scrollHeight - messages.clientHeight - messages.scrollTop <= STICKY_BOTTOM_THRESHOLD;
  }

  function attachMessages(messages) {
    if (!messages || messages === messagesElement) return;

    if (mutationObserver) mutationObserver.disconnect();
    messagesElement = messages;
    stickToBottom = nearBottom(messages);

    messages.addEventListener("scroll", () => {
      stickToBottom = nearBottom(messages);
    }, { passive: true });

    mutationObserver = new MutationObserver(() => scheduleMaintenance());
    mutationObserver.observe(messages, { childList: true });
  }

  function requestBottomScroll(messages = messagesElement) {
    if (!messages) return;
    attachMessages(messages);
    if (pendingScrollRestore || !stickToBottom || scrollFrame !== null) return;

    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null;
      if (pendingScrollRestore || !stickToBottom) return;
      messages.scrollTo({ top: messages.scrollHeight, behavior: "auto" });
    });
  }

  function updateControl() {
    const control = document.getElementById("thread-history-control");
    if (!control) return;

    const threadId = currentThreadId();
    const meta = threadId ? threadMeta.get(threadId) : null;
    const hasOlder = Boolean(meta?.truncated || (threadId && locallyPruned.has(threadId)));

    control.replaceChildren();
    control.hidden = !hasOlder;
    if (!hasOlder || !threadId) return;

    control.style.display = "flex";
    control.style.justifyContent = "center";
    control.style.padding = "8px 16px 0";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button";
    const omitted = Number(meta?.omitted || 0);
    button.textContent = omitted > 0
      ? `Load earlier activity (${omitted} hidden)`
      : "Load earlier activity";

    const limit = effectiveLimit(threadId);
    if (limit >= MAX_LIMIT) {
      button.disabled = true;
      button.textContent = "Earlier history limit reached";
    }

    button.addEventListener("click", async () => {
      const nextLimit = Math.min(MAX_LIMIT, limit + PAGE_SIZE);
      if (nextLimit <= limit) return;

      const messages = messagesElement || document.getElementById("messages");
      if (messages) {
        pendingScrollRestore = {
          threadId,
          scrollHeight: messages.scrollHeight,
          scrollTop: messages.scrollTop,
        };
      }

      requestedLimits.set(threadId, nextLimit);
      button.disabled = true;
      button.textContent = "Loading earlier activity…";

      if (typeof reloadThread === "function") {
        try {
          await reloadThread(threadId);
        } catch (error) {
          pendingScrollRestore = null;
          button.disabled = false;
          button.textContent = "Load earlier activity";
          throw error;
        }
      }
    });

    control.appendChild(button);
  }

  function pruneLiveDom(threadId = currentThreadId(), messages = messagesElement) {
    if (!messages || !threadId) return;

    const cap = Math.max(LIVE_DOM_CAP, effectiveLimit(threadId));
    const nodes = Array.from(messages.children).filter((element) => element.matches("article.message"));
    const excess = nodes.length - cap;
    if (excess <= 0) return;

    nodes.slice(0, excess).forEach((node) => node.remove());
    locallyPruned.add(threadId);
  }

  function restoreScrollIfNeeded(threadId = currentThreadId(), messages = messagesElement) {
    if (!pendingScrollRestore || !messages || threadId !== pendingScrollRestore.threadId) return false;

    const addedHeight = Math.max(0, messages.scrollHeight - pendingScrollRestore.scrollHeight);
    const target = pendingScrollRestore.scrollTop + addedHeight;
    pendingScrollRestore = null;
    messages.scrollTo({ top: target, behavior: "auto" });
    stickToBottom = nearBottom(messages);
    return true;
  }

  function scheduleMaintenance() {
    if (maintenanceFrame !== null) return;
    maintenanceFrame = requestAnimationFrame(() => {
      maintenanceFrame = null;
      const threadId = currentThreadId();
      const messages = messagesElement || document.getElementById("messages");
      if (messages) attachMessages(messages);
      pruneLiveDom(threadId, messages);
      restoreScrollIfNeeded(threadId, messages);
      updateControl();
    });
  }

  function recordThread(threadId, thread = {}) {
    if (!threadId) return;
    threadMeta.set(threadId, {
      truncated: Boolean(thread.messagesTruncated),
      omitted: Number(thread.messagesOmitted || 0),
      limit: Number(thread.messageLimit || effectiveLimit(threadId)),
    });
    if (!thread.messagesTruncated) locallyPruned.delete(threadId);
  }

  function afterThreadRendered(threadId, messages = document.getElementById("messages")) {
    if (messages) attachMessages(messages);
    pruneLiveDom(threadId, messages);
    const restored = restoreScrollIfNeeded(threadId, messages);
    updateControl();
    if (!restored) requestBottomScroll(messages);
  }

  function configure(options = {}) {
    if (typeof options.reloadThread === "function") reloadThread = options.reloadThread;
    const messages = document.getElementById("messages");
    if (messages) attachMessages(messages);
    updateControl();
  }

  window.codexThreadHistory = Object.freeze({
    configure,
    messageLimit: effectiveLimit,
    recordThread,
    afterThreadRendered,
    requestBottomScroll,
  });

  window.addEventListener("DOMContentLoaded", () => {
    const messages = document.getElementById("messages");
    if (messages) attachMessages(messages);
    updateControl();
  });
})();
