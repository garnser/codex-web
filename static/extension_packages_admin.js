(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const MIGRATION_EVIDENCE_TYPES = new Set([
    "policy_evaluation",
    "artifact_verification",
    "test_result",
    "ci_check",
  ]);

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

  function matchingMigrationEvidence(manifest, evidence) {
    return evidence.filter((item) => (
      item.lifecycle === "valid"
      && item.result === "pass"
      && MIGRATION_EVIDENCE_TYPES.has(item.evidence_type)
      && item.metadata?.extension_id === manifest.id
      && item.metadata?.to_version === manifest.version
    ));
  }

  function packageAction(candidate, installed, evidence, evidenceError) {
    const manifest = candidate.manifest || {};
    const verification = candidate.verification || {};
    const ref = escapeHtml(candidate.package_ref);
    const id = escapeHtml(manifest.id);
    const version = escapeHtml(manifest.version);
    const publisher = escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown");

    if (!installed) {
      return `<button type="button" class="ghost-button" data-extension-package-install data-package-ref="${ref}" data-package-id="${id}" data-package-version="${version}" data-package-publisher="${publisher}" data-package-signature="${escapeHtml(verification.signature_status || "unknown")}" data-package-digest-verified="${verification.digest_verified ? "true" : "false"}">Install package</button>`;
    }
    if (installed.manifest?.version === manifest.version) {
      return `<small>Installed version ${escapeHtml(installed.manifest?.version || "unknown")} matches this candidate.</small>`;
    }
    if (installed.lifecycle === "enabled") {
      return `<small>Installed version ${escapeHtml(installed.manifest?.version || "unknown")} is enabled. Disable it before using the canonical upgrade lifecycle.</small>`;
    }

    const migrationEntrypoint = manifest.migrations?.entrypoint || null;
    const matches = migrationEntrypoint ? matchingMigrationEvidence(manifest, evidence) : [];
    const evidenceSelect = migrationEntrypoint
      ? `<div class="comm-entry">
          <small>Migration entrypoint: ${escapeHtml(migrationEntrypoint)} · canonical passing evidence is required.</small>
          ${evidenceError ? `<small>Evidence unavailable: ${escapeHtml(evidenceError)}</small>` : ""}
          <select data-extension-migration-evidence ${evidenceError || !matches.length ? "disabled" : ""}>
            <option value="">Select migration evidence</option>
            ${matches.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.evidence_type)} · ${escapeHtml(item.summary || item.id)} · ${escapeHtml(item.id)}</option>`).join("")}
          </select>
        </div>`
      : '<small>Target manifest declares no migration entrypoint; migration evidence must not be supplied.</small>';
    const disabled = Boolean(migrationEntrypoint && (evidenceError || !matches.length));
    return `<div data-extension-upgrade-controls>
      <small>Installed version: ${escapeHtml(installed.manifest?.version || "unknown")} · lifecycle: ${escapeHtml(installed.lifecycle)}.</small>
      ${evidenceSelect}
      <button type="button" class="ghost-button" data-extension-package-upgrade data-package-ref="${ref}" data-installation-id="${escapeHtml(installed.id)}" data-package-id="${id}" data-package-version="${version}" ${disabled ? "disabled" : ""}>Apply package upgrade</button>
    </div>`;
  }

  function renderPackages(discovery, installations, evidence, evidenceError) {
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
      return `<div class="comm-entry" data-extension-package-row>
        <strong>${escapeHtml(manifest.id || candidate.package_ref)} @ ${escapeHtml(manifest.version || "unknown")}</strong>
        <small>Publisher: ${escapeHtml(manifest.publisher?.name || manifest.publisher?.id || "unknown")} · Types: ${(manifest.types || []).map(escapeHtml).join(", ") || "unknown"}</small>
        <small>Compatibility: ${escapeHtml(manifest.compatibility?.codex_web || "unspecified")} · Payload: ${escapeHtml(formatBytes(candidate.payload_size_bytes))}</small>
        <small>Digest verified: ${verification.digest_verified ? "yes" : "no"} · Signature: ${escapeHtml(verification.signature_status || "unknown")} · Requested capabilities: ${requested.length ? requested.map(escapeHtml).join(", ") : "none"}</small>
        <small>Package ref: ${escapeHtml(candidate.package_ref)} · Source: ${escapeHtml(candidate.relative_directory || "unknown")}</small>
        ${packageAction(candidate, installed, evidence, evidenceError)}
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
      let evidence = [];
      let evidenceError = null;
      const [discovery, extensions] = await Promise.all([
        apiRequest("/api/extensions/packages"),
        apiRequest("/api/extensions"),
        apiRequest("/api/evidence?include_inactive=false")
          .then((value) => { evidence = value.items || []; })
          .catch((error) => { evidenceError = error.message; }),
      ]);
      renderPackages(discovery, extensions.items || [], evidence, evidenceError);
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

  async function upgradePackage(button) {
    const row = button.closest("[data-extension-package-row]");
    const packageRef = button.dataset.packageRef;
    const installationId = button.dataset.installationId;
    const extensionId = button.dataset.packageId || installationId;
    const version = button.dataset.packageVersion || "unknown";
    if (!row || !packageRef || !installationId) return;

    const evidenceSelect = row.querySelector("[data-extension-migration-evidence]");
    const migrationEvidenceId = evidenceSelect?.value || null;
    if (evidenceSelect && !migrationEvidenceId) return;

    const evidenceText = migrationEvidenceId
      ? ` Migration evidence: ${migrationEvidenceId}.`
      : " Target declares no migration, so no migration evidence will be sent.";
    const confirmation = `Upgrade ${extensionId} to ${version} using server-verified package ${packageRef}?${evidenceText} Removed capabilities are revoked, obsolete secret bindings are pruned, health is reset, and the extension remains disabled or incompatible after upgrade.`;
    if (!window.confirm(confirmation)) return;

    const status = document.getElementById("extension-package-status");
    button.disabled = true;
    if (status) status.textContent = `Upgrading ${extensionId} to ${version}...`;
    try {
      await apiRequest(
        `/api/extensions/${encodeURIComponent(installationId)}/packages/${encodeURIComponent(packageRef)}/upgrade`,
        {
          method: "POST",
          body: JSON.stringify({ migration_evidence_id: migrationEvidenceId }),
        },
      );
      document.getElementById("refresh-extensions")?.click();
    } catch (error) {
      button.disabled = false;
      if (status) status.textContent = `Package upgrade failed: ${error.message}`;
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
      const installButton = event.target.closest?.("[data-extension-package-install]");
      if (installButton) {
        installPackage(installButton).catch(console.error);
        return;
      }
      const upgradeButton = event.target.closest?.("[data-extension-package-upgrade]");
      if (upgradeButton) upgradePackage(upgradeButton).catch(console.error);
    });
    if (panel?.open) refreshPackages().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bindPackageAdmin);
})();
