(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  }

  function renderPackages(discovery, installations) {
    const status = document.getElementById("extension-package-status");
    const list = document.getElementById("extension-package-list");
    if (!status || !list) return;
    const items = discovery?.items || [];
    const errors = discovery?.errors || [];
    status.hidden = false;
    status.textContent = `${items.length} verified candidate(s)${errors.length ? ` · ${errors.length} discovery error(s)` : ""}`;

    const packageCards = items.map((candidate) => {
      const manifest = candidate.manifest || {};
      const verification = candidate.verification || {};
      const installed = installations.find((item) => (
        item.lifecycle !== "removed" && item.manifest?.id === manifest.id
      ));
      const requested = manifest.capabilities?.requested || [];
      const installationState = installed
        ? `<small>Installed version ${escapeHtml(installed.manifest?.version || "unknown")} · use the upgrade lifecycle for another version.</small>`
        : `<button type="button" class="ghost-button" data-extension-package-install data-package-ref="${escapeHtml(candidate.package_ref)}" data-package-id="${escapeHtml(manifest.id)}" data-package-version="${escapeHtml(manifest.version)}" data-package-publisher="${escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown")}" data-package-signature="${escapeHtml(verification.signature_status || "unknown")}" data-package-digest-verified="${verification.digest_verified ? "true" : "false"}">Install package</button>`;
      return `<div class="comm-entry">
        <strong>${escapeHtml(manifest.id || candidate.package_ref)} @ ${escapeHtml(manifest.version || "unknown")}</strong>
        <small>Publisher: ${escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown")} · Types: ${(manifest.types || []).map(escapeHtml).join(", ") || "unknown"}</small>
        <small>Compatibility: ${escapeHtml(manifest.compatibility?.codex_web || "unspecified")} · Payload: ${escapeHtml(formatBytes(candidate.payload_size_bytes))}</small>
        <small>Digest verified: ${verification.digest_verified ? "yes" : "no"} · Signature: ${escapeHtml(verification.signature_status || "unknown")} · Requested capabilities: ${requested.length ? requested.map(escapeHtml).join(", ") : "none"}</small>
        <small>Package ref: ${escapeHtml(candidate.package_ref)} · Source: ${escapeHtml(candidate.relative_directory || "unknown")}</small>
        ${installationState}
      </div>`;
    });

    const errorCards = errors.map((item) => `<div class="comm-entry">
      <strong>Package discovery error · ${escapeHtml(item.relative_directory || "unknown")}</strong>
      <small>${escapeHtml(item.error || item.detail || "unknown package error")}</small>
    </div>`);
    list.innerHTML = [...packageCards, ...errorCards].join("")
      || '<div class="comm-entry"><strong>No packages discovered</strong><small>Add a canonical manifest.json + payload.cwext package to the configured catalog root.</small></div>';
  }

  async function refreshPackages() {
    const status = document.getElementById("extension-package-status");
    if (status) {
      status.hidden = false;
      status.textContent = "Loading package catalog...";
    }
    try {
      const [discovery, extensions] = await Promise.all([
        apiRequest("/api/extensions/packages"),
        apiRequest("/api/extensions"),
      ]);
      renderPackages(discovery, extensions.items || []);
    } catch (error) {
      if (status) status.textContent = `Package catalog unavailable: ${error.message}`;
      const list = document.getElementById("extension-package-list");
      if (list) list.innerHTML = "";
    }
  }

  async function installPackage(button) {
    const packageRef = button.dataset.packageRef;
    const extensionId = button.dataset.packageId || packageRef;
    const version = button.dataset.packageVersion || "unknown";
    const publisher = button.dataset.packagePublisher || "unknown";
    const signature = button.dataset.packageSignature || "unknown";
    const digestVerified = button.dataset.packageDigestVerified === "true";
    if (!packageRef) return;

    const confirmation = `Install ${extensionId}@${version} from ${publisher}? Digest verified: ${digestVerified ? "yes" : "no"}; signature: ${signature}. Installation does not authorize capabilities and does not enable the extension.`;
    if (!window.confirm(confirmation)) return;

    const status = document.getElementById("extension-package-status");
    button.disabled = true;
    if (status) status.textContent = `Installing ${extensionId}@${version}...`;
    try {
      await apiRequest(
        `/api/extensions/packages/${encodeURIComponent(packageRef)}/install`,
        {
          method: "POST",
          body: JSON.stringify({ deployment_mode: "self_hosted" }),
        },
      );
      document.getElementById("refresh-extensions")?.click();
    } catch (error) {
      button.disabled = false;
      if (status) status.textContent = `Package installation failed: ${error.message}`;
    }
  }

  function bindPackageAdmin() {
    const panel = document.getElementById("developer-panel");
    const refresh = document.getElementById("refresh-extensions");
    const list = document.getElementById("extension-package-list");
    refresh?.addEventListener("click", refreshPackages);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refreshPackages().catch(console.error);
    });
    list?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-extension-package-install]");
      if (button) installPackage(button).catch(console.error);
    });
    if (panel?.open) refreshPackages().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bindPackageAdmin);
})();
