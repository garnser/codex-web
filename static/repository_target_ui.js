export function activeRepositories(resources = []) {
  return resources.filter((item) => (
    item.resource_type === "repository" && item.lifecycle === "active"
  ));
}

export function targetState({
  policy = "deterministic",
  repositories = [],
  selectedId = "",
  boundId = "",
} = {}) {
  const byId = new Map(repositories.map((item) => [item.id, item]));

  if (boundId && selectedId && boundId !== selectedId) {
    return {
      status: "conflict",
      blocked: true,
      code: "repository_target_conflict",
      message: `Repository conflict: this thread is bound to ${boundId}, but ${selectedId} is selected.`,
      id: selectedId,
      provenance: "explicit selection conflicts with thread binding",
    };
  }

  const id = boundId || selectedId;
  if (id) {
    const repository = byId.get(id);
    if (!repository) {
      return {
        status: "stale",
        blocked: false,
        code: "repository_target_stale",
        message: `Repository ${id} is no longer an active Project-bound option; canonical preflight will validate it.`,
        id,
        provenance: boundId ? "thread/profile binding" : "explicit selection",
      };
    }
    return {
      status: "selected",
      blocked: false,
      code: null,
      message: `${repository.name || repository.id} · ${boundId ? "thread/profile binding" : "explicit selection"}`,
      id,
      repository,
      provenance: boundId ? "thread/profile binding" : "explicit selection",
    };
  }

  if (policy === "explicit") {
    return {
      status: "required",
      blocked: true,
      code: "repository_target_missing",
      message: "Repository target required per turn",
      id: "",
      provenance: "explicit Project policy",
    };
  }

  return {
    status: "canonical",
    blocked: false,
    code: null,
    message: "Repository target resolved by canonical Work Item/routing/default context",
    id: "",
    provenance: "canonical preflight",
  };
}

export function renderStatus({ status, mutable, target, required }) {
  if (!status || !mutable) return;
  status.textContent = target.message;
  status.dataset.state = target.status;
  mutable.setAttribute("aria-invalid", String(target.blocked));
  mutable.required = Boolean(required);
}

export function renderControls({
  mutable,
  readOnly,
  status,
  policy = "deterministic",
  repositories = [],
  settings = {},
  boundId = "",
  escapeHtml,
}) {
  if (!mutable || !readOnly) return;
  const explicit = policy === "explicit";
  mutable.innerHTML = [
    `<option value="">${explicit ? "Select repository…" : "Auto"}</option>`,
    ...repositories.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(item.id)}</option>`
    )),
  ].join("");
  mutable.value = settings.repositoryResourceId || "";

  const selected = new Set(settings.readOnlyRepositoryResourceIds || []);
  readOnly.innerHTML = repositories
    .filter((item) => item.id !== settings.repositoryResourceId)
    .map((item) => (
      `<option value="${escapeHtml(item.id)}" ${selected.has(item.id) ? "selected" : ""}>${escapeHtml(item.name)}</option>`
    ))
    .join("");

  const target = targetState({
    policy,
    repositories,
    selectedId: settings.repositoryResourceId || "",
    boundId,
  });
  renderStatus({
    status,
    mutable,
    target,
    required: explicit && !boundId,
  });
}
