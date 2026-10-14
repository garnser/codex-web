export function createQueuedBootstrapRecoveryUi({api, loadThread, scheduleRefresh, captureView}) {
  function attach(attempt, body) {
    if (!body || !attempt?.can_recover_bootstrap || !attempt?.bootstrap_recovery_href) return;
    const current = captureView();
    const container = document.createElement('div');
    container.className = 'message-actions';
    const inspect = document.createElement('button');
    inspect.type = 'button'; inspect.textContent = 'Inspect fresh bootstrap recovery';
    const status = document.createElement('div'); status.setAttribute('role', 'status');
    let inspected = false;
    inspect.addEventListener('click', async () => {
      if (inspected || !current()) return;
      inspected = true; inspect.disabled = true; status.textContent = 'Loading recovery scope…';
      try {
        const preview = await api(attempt.bootstrap_recovery_href);
        if (!current()) return;
        const details = document.createElement('pre');
        details.textContent = `Original request: ${preview.queued_turn_id}\nProject: ${preview.project_id}\n`
          + `Actor: ${preview.original_actor_id}\nProfile: ${preview.agent_profile_id} revision ${preview.agent_profile_revision}\n`
          + `Execution profile: ${preview.execution_profile_id}\nWritable repositories: ${preview.writable_repository_resource_ids.join(', ')}\n`
          + `Read-only repositories: ${preview.read_only_repository_resource_ids.join(', ')}\nBootstrap state: ${preview.state}`;
        container.append(details);
        if (!preview.can_create && !preview.can_reconcile) {
          status.textContent = 'Fresh bootstrap is verified. Use Retry to restore the original request.';
          return;
        }
        status.textContent = preview.prestart_refusal
          ? `Previous bootstrap was refused before provider startup: ${preview.prestart_refusal.message}. A new attempt requires an explicit request.`
          : preview.can_create
          ? 'Creates a fresh runtime thread with the original scope. Retry remains a separate action.'
          : 'Checks the existing correlated receipt. An unresolved creation will remain held.';
        const label = document.createElement('label'); label.textContent = 'Recovery reason ';
        const reason = document.createElement('input'); reason.maxLength = 1000;
        reason.setAttribute('aria-label', 'Fresh bootstrap recovery reason'); label.append(reason);
        const submit = document.createElement('button'); submit.type = 'button'; submit.disabled = true;
        submit.textContent = preview.can_create ? 'Create fresh bootstrap' : 'Reconcile bootstrap receipt';
        let submitted = false;
        reason.addEventListener('input', () => {submit.disabled = submitted || !reason.value.trim();});
        submit.addEventListener('click', async () => {
          if (submitted || !current()) return;
          submitted = true; submit.disabled = true; status.textContent = 'Recovering bootstrap…';
          try {
            const result = await api(attempt.bootstrap_recovery_href, {method:'POST',
              body:JSON.stringify({expected_snapshot_sha256:preview.snapshot_sha256, reason:reason.value.trim()})});
            if (!current()) return;
            status.textContent = 'Fresh bootstrap verified. The original request remains blocked until Retry.';
            await loadThread(result.threadId);
            if (current()) scheduleRefresh(0);
          } catch (error) {
            if (current()) status.textContent = `Recovery was not confirmed: ${error.message}. Reload and reconcile its receipt before another action.`;
          }
        });
        container.append(label, submit);
      } catch (error) {
        if (current()) status.textContent = `Recovery unavailable: ${error.message}`;
      }
    });
    container.append(inspect, status); body.append(container);
  }
  return {attach};
}
