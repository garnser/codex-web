export function effectiveSettings({ project, saved = {}, thread = {}, defaultProfileId }) {
  return {
    sandbox: thread.sandbox || saved.sandbox || project?.sandbox || "workspace-write",
    approvalPolicy: thread.approval_policy || saved.approvalPolicy || project?.approval_policy || "on-request",
    profileId: thread.execution_profile_id || saved.profileId || defaultProfileId,
    repositoryResourceId: thread.repository_resource_id || saved.repositoryResourceId || "",
    writableRepositoryResourceIds: Array.isArray(thread.writable_repository_resource_ids)
      ? thread.writable_repository_resource_ids
      : (Array.isArray(saved.writableRepositoryResourceIds) ? saved.writableRepositoryResourceIds : []),
    readOnlyRepositoryResourceIds: Array.isArray(thread.read_only_repository_resource_ids)
      ? thread.read_only_repository_resource_ids
      : (Array.isArray(saved.readOnlyRepositoryResourceIds) ? saved.readOnlyRepositoryResourceIds : []),
  };
}

export function responseSettings(current, response) {
  return {
    ...current,
    sandbox: response.sandbox || current.sandbox || null,
    approval_policy: response.approval_policy || current.approval_policy || null,
    model: response.model || null,
    reasoning_effort: response.reasoning_effort || null,
    repository_resource_id: response.repository_resource_id || null,
    writable_repository_resource_ids: response.writable_repository_resource_ids || [],
    read_only_repository_resource_ids: response.read_only_repository_resource_ids || [],
    execution_profile_id: response.execution_profile_id || null,
  };
}

export function updatePayload(current, updates) {
  return {
    sandbox: current.sandbox || null,
    approval_policy: current.approval_policy || null,
    model: current.model || "",
    reasoning_effort: current.reasoning_effort || "",
    repository_resource_id: current.repository_resource_id || null,
    writable_repository_resource_ids: current.writable_repository_resource_ids || [],
    read_only_repository_resource_ids: current.read_only_repository_resource_ids || [],
    execution_profile_id: current.execution_profile_id || null,
    ...updates,
  };
}

export function install({
  byId,
  activeThreadId,
  updateThread,
  persistDefaults,
  defaultProfileId,
  renderProfile,
  renderRepositories,
}) {
  const menuIds = ["thread-settings-menu", "thread-actions-menu"];
  menuIds.forEach((id) => {
    byId(id).addEventListener("toggle", () => {
      if (!byId(id).open) return;
      menuIds.forEach((otherId) => {
        if (otherId !== id) byId(otherId).open = false;
      });
    });
  });
  document.addEventListener("click", (event) => {
    menuIds.forEach((id) => {
      const menu = byId(id);
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  });

  const save = (updates) => {
    const threadId = activeThreadId();
    if (threadId) updateThread(threadId, updates).catch(console.error);
    else persistDefaults();
  };
  byId("execution-profile").addEventListener("change", () => {
    const profileId = byId("execution-profile").value || defaultProfileId();
    save({ execution_profile_id: profileId });
    renderProfile(profileId);
    renderRepositories();
  });
  byId("repository-target").addEventListener("change", () => {
    save({ repository_resource_id: byId("repository-target").value || null });
    renderRepositories();
  });
  byId("repository-write-targets").addEventListener("change", () => {
    save({
      writable_repository_resource_ids: Array.from(
        byId("repository-write-targets").selectedOptions,
        (option) => option.value,
      ),
    });
  });
  byId("repository-read-context").addEventListener("change", () => {
    save({
      read_only_repository_resource_ids: Array.from(
        byId("repository-read-context").selectedOptions,
        (option) => option.value,
      ),
    });
  });
  byId("sandbox").addEventListener("change", () => save({ sandbox: byId("sandbox").value }));
  byId("approval-policy").addEventListener("change", () => save({ approval_policy: byId("approval-policy").value }));
  byId("thread-model-setting").addEventListener("change", () => {
    const threadId = activeThreadId();
    if (threadId) updateThread(threadId, { model: byId("thread-model-setting").value }).catch(console.error);
  });
  byId("thread-reasoning-setting").addEventListener("change", () => {
    const threadId = activeThreadId();
    if (threadId) updateThread(threadId, { reasoning_effort: byId("thread-reasoning-setting").value }).catch(console.error);
  });
}
