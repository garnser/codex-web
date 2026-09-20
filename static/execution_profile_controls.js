(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let profiles = [];
  let defaultProfileId = "";

  function selectedProfile() {
    const value = document.getElementById("execution-profile")?.value || "";
    const id = value || defaultProfileId;
    return profiles.find((item) => item.id === id) || null;
  }

  function renderSummary() {
    const profile = selectedProfile();
    const summary = document.getElementById("execution-profile-summary");
    if (!summary) return;
    if (!profile) {
      summary.textContent = "";
      return;
    }
    const capabilities = (profile.requiredWorkerCapabilities || []).join(", ") || "none";
    summary.textContent = `${profile.repositoryAccess} · ${capabilities} · ${profile.authorityExplanation}`;
  }

  function applyConstraints() {
    const profile = selectedProfile();
    if (!profile) return;
    const sandbox = document.getElementById("sandbox");
    if (
      sandbox
      && profile.allowedSandboxes?.length
      && !profile.allowedSandboxes.includes(sandbox.value)
    ) {
      sandbox.value = profile.allowedSandboxes[0];
      sandbox.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const noRepository = profile.repositoryAccess === "none";
    const mutable = document.getElementById("repository-target");
    const readOnly = document.getElementById("repository-read-context");
    if (mutable) mutable.disabled = noRepository;
    if (readOnly) readOnly.disabled = noRepository;
    renderSummary();
  }

  async function refresh(projectId, selectedId = "") {
    const select = document.getElementById("execution-profile");
    if (!select || !projectId) return;
    try {
      const response = await apiRequest(
        `/api/execution-profiles?project_id=${encodeURIComponent(projectId)}`,
      );
      profiles = response.items || [];
      defaultProfileId = response.defaultProfileId || "";
      select.innerHTML = [
        `<option value="">Default (${defaultProfileId || "repository-write"})</option>`,
        ...profiles.map((profile) => (
          `<option value="${profile.id}">${profile.name}</option>`
        )),
      ].join("");
      select.value = selectedId && profiles.some((item) => item.id === selectedId)
        ? selectedId
        : "";
      applyConstraints();
    } catch (error) {
      profiles = [];
      defaultProfileId = "";
      select.innerHTML = '<option value="">Profiles unavailable</option>';
      const summary = document.getElementById("execution-profile-summary");
      if (summary) summary.textContent = `Execution profiles unavailable: ${error.message}`;
    }
  }

  document.getElementById("execution-profile")?.addEventListener("change", applyConstraints);
  window.executionProfileControls = {
    refresh,
    renderSummary,
    selectedProfile,
  };
})();
