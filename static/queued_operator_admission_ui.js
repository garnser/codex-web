export function createQueuedOperatorAdmissionUi({api, loadThread, scheduleRefresh, captureView}) {
  function attach(attempt, body) {
    if (!body || !attempt?.can_admit_original || !attempt.operator_admission_href) return;
    const current = captureView();
    const box = document.createElement('div'); box.className = 'message-actions';
    const inspect = document.createElement('button'); inspect.type = 'button';
    inspect.textContent = 'Review single original admission';
    const status = document.createElement('div'); status.setAttribute('role', 'status');
    inspect.addEventListener('click', async () => {
      inspect.disabled = true; status.textContent = 'Loading original admission scope…';
      try {
        const preview = await api(attempt.operator_admission_href);
        if (!current()) return;
        const details = document.createElement('pre');
        details.textContent = `Original: ${preview.queued_turn_id}\nProject: ${preview.project_id}\nSource: ${preview.source}\n`
          + `Actor: ${preview.original_actor_id}\nProfile: ${preview.agent_profile_id} revision ${preview.agent_profile_revision}\n`
          + `Execution profile: ${preview.execution_profile_id}\nWritable repositories: ${preview.writable_repository_resource_ids.join(', ')}\n`
          + `Read-only repositories: ${preview.read_only_repository_resource_ids.join(', ')}\nReply target: ${JSON.stringify(preview.reply_target)}`;
        status.textContent = 'Submits this original once with its existing scope. Automation stays paused. Unknown native outcomes require reconciliation.';
        const label = document.createElement('label'); label.textContent = 'Admission reason ';
        const reason = document.createElement('input'); reason.maxLength = 1000;
        reason.setAttribute('aria-label', 'Single original admission reason'); label.append(reason);
        const submit = document.createElement('button'); submit.type = 'button'; submit.disabled = true;
        submit.textContent = 'Admit this original once';
        let submitted = false;
        reason.addEventListener('input', () => {submit.disabled = submitted || !reason.value.trim();});
        submit.addEventListener('click', async () => {
          if (submitted || !current()) return;
          submitted = true; submit.disabled = true;
          status.textContent = 'Admitting reviewed original…';
          try {
            const result = await api(attempt.operator_admission_href, {method:'POST', body:JSON.stringify({
              expected_snapshot_sha256:preview.snapshot_sha256, expected_queue_sha256:preview.queue_sha256,
              reason:reason.value.trim()})});
            if (!current()) return;
            const receipt = result.attempt?.queued_start_receipt;
            status.textContent = receipt ? `Native start confirmed: ${receipt.provider_native_turn_id}. Automation remains paused.`
              : `Native start is not confirmed (${result.attempt?.status || 'unknown'}). Inspect the retained outcome before another action.`;
            await loadThread(result.threadId);
            if (current()) scheduleRefresh(0);
          } catch (error) {
            if (current()) status.textContent = `Admission was not confirmed: ${error.message}. Reload and inspect the original outcome. The reason is retained here.`;
          }
        });
        box.append(details, label, submit);
      } catch (error) {
        if (current()) status.textContent = `Admission unavailable: ${error.message}`;
      }
    });
    box.append(inspect, status); body.append(box);
  }
  return {attach};
}
