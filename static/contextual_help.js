const helpDocsBase =
  document.documentElement.dataset.docsBase
  || "https://github.com/garnser/codex-web/blob/main/docs";

const helpTopics = [
  { pattern: /definition|contract/i, path: "administration/definition-registry.md", label: "Definitions help" },
  { pattern: /attention|inbox/i, path: "troubleshooting/operator-matrix.md", label: "Attention help" },
  { pattern: /observability|structured logs|operations/i, path: "operations/runbooks.md#health-logs-and-observability", label: "Operations help" },
  { pattern: /worker|execution plane/i, path: "operations/runbooks.md#execution-worker-drain-quarantine-and-replacement", label: "Worker help" },
  { pattern: /extension|plugin/i, path: "extensions/developer-guide.md", label: "Extension help" },
  { pattern: /encryption|crypto|key/i, path: "administration/platform-administration.md#secrets-credentials-and-encryption-keys", label: "Key help" },
  { pattern: /release|promotion/i, path: "operations/runbooks.md#release-promotion-and-rollback", label: "Release help" },
  { pattern: /upgrade|migration/i, path: "operations/upgrade-and-rollback.md", label: "Upgrade help" },
  { pattern: /incident/i, path: "operations/runbooks.md#incident-response", label: "Incident help" },
  { pattern: /recovery|backup|restore|dr/i, path: "operations/runbooks.md#backup-restore-and-recovery-drill", label: "Recovery help" },
  { pattern: /autonomy|audit/i, path: "administration/platform-administration.md#autonomy-administration", label: "Autonomy help" },
];

function helpHref(path) {
  return helpDocsBase.replace(/\/$/, "") + "/" + path;
}

function helpDecorateHeading(heading) {
  if (!(heading instanceof HTMLElement)) return;
  if (heading.dataset.contextHelpDecorated === "true") return;
  const text = (heading.textContent || "").trim();
  const topic = helpTopics.find((item) => item.pattern.test(text));
  if (!topic) return;

  const link = document.createElement("a");
  link.className = "context-help-link";
  link.href = helpHref(topic.path);
  link.target = "_blank";
  link.rel = "noreferrer";
  link.textContent = "Help";
  link.setAttribute("aria-label", topic.label + " for " + text);
  link.dataset.contextHelpPath = topic.path;

  heading.append(" ");
  heading.appendChild(link);
  heading.dataset.contextHelpDecorated = "true";
}

function helpDecorate(root = document) {
  root.querySelectorAll?.(
    "#developer-panel h2, #developer-panel h3, #autonomy-control-center-card h2, #autonomy-control-center-card h3"
  ).forEach(helpDecorateHeading);
}

function installContextHelp() {
  if (!document.getElementById("context-help-styles")) {
    const style = document.createElement("style");
    style.id = "context-help-styles";
    style.textContent =
      ".context-help-link{font-size:.72rem;font-weight:500;margin-left:.35rem;white-space:nowrap}" +
      ".context-help-link:focus-visible{outline:2px solid currentColor;outline-offset:2px;border-radius:.2rem}";
    document.head.appendChild(style);
  }

  helpDecorate();
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.("h2,h3")) helpDecorateHeading(node);
        helpDecorate(node);
      }
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", installContextHelp, { once: true });
} else {
  installContextHelp();
}
