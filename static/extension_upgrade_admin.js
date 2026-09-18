(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const ACCEPTED_EVIDENCE = new Set([
    "policy_evaluation",
    "artifact_verification",
    "test_result",
    "ci_check",
  ]);
  let generation = 0;

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function hostFor(packageRef) {
    return Array.from(document.querySelectorAll("[data-extension-upgrade-host]"))
      .find((element) => element.dataset.extensionUpgradeHost === packageRef) || null;
  }

  function validEvidence(manifest, evidence) {
    return evidence.filter((item) => (
      item.lifecycle === "valid"
      && item.result === "pass"
      && ACCEPTED_EVIDENCE.has(item.evidence_type)
      && item.metadata?.extension_id === manifest.id
      && item.metadata?.to_version === manifest.version
    ));
  }

  function renderUpgrade(candidate, installed, evidence, evidenceError, canMutate) {
    const host = hostFor(candidate.package_ref);
    if (!host) return;
    const manifest = candidate.manifest || {};
    if (installed.lifecycle === "enabled") {
      host.innerHTML = `<small>Installed version ${escapeHtml(installed.manifest?.version || "unknown")} is enabled. Disable it before upgrade.</small>`;
      return;
    }

    const migrationEntrypoint = manifest.migrations?.entrypoint || null;
    const matches = migrationEntrypoint ? validEvidence(manifest, evidence) : [];
    const blocked = !canMutate || Boolean(migrationEntrypoint && (evidenceError || !matches.length));
    const evidenceControl = migrationEntrypoint
      ? `<div class="comm-entry">
          <small>Migration: ${escapeHtml(migrationEntrypoint)} · canonical passing evidence is required.</small>
          ${evidenceError ? `<small>Evidence unavailable: ${escapeHtml(evidenceError)}</small>` : ""}
          <select data-extension-migration-evidence ${blocked ? "disabled" : ""}>
            <option value="">Select migration evidence</option>
            ${matches.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.evidence_type)} · ${escapeHtml(item.summary || item.id)} · ${escapeHtml(item.id)}</option>`).join("")}
          </select>
        </div>`
      : '<small>Target declares no migration entrypoint; no migration evidence will be sent.</small>';

    host.innerHTML = `<div data-extension-upgrade-controls data-installation-id="${escapeHtml(installed.id)}" data-package-ref="${escapeHtml(candidate.package_ref)}" data-extension-id="${escapeHtml(manifest.id)}" data-target-version="${escapeHtml(manifest.version)}">
      <small>Installed: ${escapeHtml(installed.manifest?.version || "unknown")} · lifecycle: ${escapeHtml(installed.lifecycle)}.</small>
      ${!canMutate ? '<small>Upgrade mutation requires tenant admin/owner plus MFA/local-trusted assurance or extensions:admin service authority.</small>' : ""}
      ${evidenceControl}
      <button type="button" class="ghost-button" data-extension-package-upgrade ${blocked ? "disabled" : ""}>Apply package upgrade</button>
    </div>`;
  }

  async function hydrateUpgrades(discovery, installations, canMutate) {
    const current = ++generation;
    const candidates = discovery?.items || [];
    const upgradePairs = candidates.map((candidate) => {
      const installed = installations.find((item) => (
        item.lifecycle !== "removed"
        && item.manifest?.id === candidate.manifest?.id
        && item.manifest?.version !== candidate.manifest?.version
      ));
      return installed ? [candidate, installed] : null;
    }).filter(Boolean);
    if (!upgradePairs.length) return;

    let evidence = [];
    let evidenceError = null;
    if (upgradePairs.some(([candidate]) => candidate.manifest?.migrations?.entrypoint)) {
      await apiRequest("/api/evidence?include_inactive=false")
        .then((value) => { evidence = value.items || []; })
        .catch((error) => { evidenceError = error.message; });
    }
    if (current !== generation) return;
    upgradePairs.forEach(([candidate, installed]) => {
      renderUpgrade(candidate, installed, evidence, evidenceError, canMutate);
    });
  }

  async function upgrade(button) {
    const root = button.closest("[data-extension-upgrade-controls]");
    const installationId = root?.dataset.installationId;
    const packageRef = root?.dataset.packageRef;
    const extensionId = root?.dataset.extensionId || installationId;
    const targetVersion = root?.dataset.targetVersion || "unknown";
    if (!root || !installationId || !packageRef || button.disabled) return;

    const evidenceSelect = root.querySelector("[data-extension-migration-evidence]");
    const migrationEvidenceId = evidenceSelect?.value || null;
    if (evidenceSelect && !migrationEvidenceId) return;

    const evidenceText = migrationEvidenceId
      ? ` Migration evidence: ${migrationEvidenceId}.`
      : " No migration evidence will be sent.";
    if (!window.confirm(
      `Upgrade ${extensionId} to ${targetVersion} using server-verified package ${packageRef}?${evidenceText} The server revokes removed capabilities, prunes obsolete secret bindings, resets health, and leaves the extension disabled or incompatible.`,
    )) return;

    button.disabled = true;
    const status = document.getElementById("extension-package-status");
    if (status) status.textContent = `Upgrading ${extensionId} to ${targetVersion}...`;
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

  window.addEventListener("codex:extension-packages-rendered", (event) => {
    hydrateUpgrades(
      event.detail?.discovery || { items: [] },
      event.detail?.installations || [],
      Boolean(event.detail?.canMutateMutation),
    ).catch(console.error);
  });

  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("extension-package-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-extension-package-upgrade]");
      if (button) upgrade(button).catch(console.error);
    });
  });
})();
