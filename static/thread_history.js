(() => {
  const INITIAL_LIMIT = 40;
  const PAGE_SIZE = 40;
  const MAX_LIMIT = 1000;
  const LIVE_DOM_CAP = 200;

  const requestedLimits = new Map();
  const threadMeta = new Map();
  const locallyPruned = new Set();
  let pendingScrollRestore = null;
  let renderTimer = null;

  const originalFetch = window.fetch.bind(window);

  function threadReadRequest(input, init = {}) {
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method !== "GET") return null;

    const rawUrl = input instanceof Request ? input.url : input;
    const url = new URL(rawUrl, window.location.href);
    const base = window.location.pathname.startsWith("/codex") ? "/codex" : "";
    const prefix = `${base}/api/threads/`;
    if (!url.pathname.startsWith(prefix)) return null;

    const suffix = url.pathname.slice(prefix.length);
    if (!suffix || suffix.includes("/")) return null;

    return { threadId: decodeURIComponent(suffix), url };
  }

  function currentThreadId() {
    const value = document.getElementById("thread-meta")?.textContent || "";
    const separator = value.indexOf(" · ");
    return (separator >= 0 ? value.slice(0, separator) : value).trim() || null;
  }

  function effectiveLimit(threadId) {
    return requestedLimits.get(threadId) || INITIAL_LIMIT;
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

    button.addEventListener("click", () => {
      const nextLimit = Math.min(MAX_LIMIT, limit + PAGE_SIZE);
      if (nextLimit <= limit) return;

      const messages = document.getElementById("messages");
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

      const activeThread = document.querySelector("#threads .item.active .item-main");
      if (activeThread instanceof HTMLElement) {
        activeThread.click();
      }
    });

    control.appendChild(button);
  }

  function pruneLiveDom() {
    const messages = document.getElementById("messages");
    const threadId = currentThreadId();
    if (!messages || !threadId) return;

    const cap = Math.max(LIVE_DOM_CAP, effectiveLimit(threadId));
    const nodes = Array.from(messages.children).filter((element) => element.matches("article.message"));
    const excess = nodes.length - cap;
    if (excess <= 0) return;

    nodes.slice(0, excess).forEach((node) => node.remove());
    locallyPruned.add(threadId);
  }

  function restoreScrollIfNeeded() {
    if (!pendingScrollRestore) return;
    const messages = document.getElementById("messages");
    const threadId = currentThreadId();
    if (!messages || threadId !== pendingScrollRestore.threadId) return;

    const addedHeight = Math.max(0, messages.scrollHeight - pendingScrollRestore.scrollHeight);
    messages.scrollTop = pendingScrollRestore.scrollTop + addedHeight;
    pendingScrollRestore = null;
  }

  function afterMessageRender() {
    if (renderTimer) clearTimeout(renderTimer);
    renderTimer = setTimeout(() => {
      renderTimer = null;
      pruneLiveDom();
      restoreScrollIfNeeded();
      updateControl();
    }, 0);
  }

  window.fetch = async function pagedThreadFetch(input, init = {}) {
    const match = threadReadRequest(input, init);
    if (!match) return originalFetch(input, init);

    const { threadId, url } = match;
    if (!url.searchParams.has("message_limit")) {
      url.searchParams.set("message_limit", String(effectiveLimit(threadId)));
    }

    const requestInput = input instanceof Request
      ? new Request(url.toString(), input)
      : url.toString();
    const response = await originalFetch(requestInput, init);

    if (response.ok) {
      response.clone().json().then((payload) => {
        const thread = payload?.thread || payload || {};
        threadMeta.set(threadId, {
          truncated: Boolean(thread.messagesTruncated),
          omitted: Number(thread.messagesOmitted || 0),
          limit: Number(thread.messageLimit || effectiveLimit(threadId)),
        });
        if (!thread.messagesTruncated) locallyPruned.delete(threadId);
        afterMessageRender();
      }).catch(() => {});
    }

    return response;
  };

  window.addEventListener("DOMContentLoaded", () => {
    const messages = document.getElementById("messages");
    if (messages) {
      const observer = new MutationObserver(afterMessageRender);
      observer.observe(messages, { childList: true });
    }
    updateControl();
  });
})();
