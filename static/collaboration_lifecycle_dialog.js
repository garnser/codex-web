import { protectActionDialog } from './action_confirmation.js';
import { dialogShell, status } from './collaboration_dialog.js';
import { loadConsumerUsage } from './collaboration_lifecycle_usage.js';
import { request } from './api_client.js';

function lifecycleImpact(kind, item, action, usage) {
  const noun = kind === "profile" ? "Agent Profile" : "Team";
  const base = action === "restore"
    ? `Restore this ${noun} to active lifecycle.`
    : `${action === "archive" ? "Archive" : "Disable"} this ${noun}. New execution selection will no longer treat it as active.`;
  return usage ? `${base} Impact: ${usage}` : base;
}

export function openLifecycle(kind, item, action, { usage = "", onChanged } = {}) {
  const id = item?.profile_id || item?.team_id;
  const dialog = dialogShell(
    `${action[0].toUpperCase() + action.slice(1)} ${kind === "profile" ? "Agent Profile" : "Team"}`,
    `Target: ${id}. ${lifecycleImpact(kind, item, action, usage)} ${action === "restore" ? "Reactivation remains subject to canonical compatibility and authority checks." : "Restore is available through the lifecycle menu; it does not resume stopped work automatically."}`,
  );
  const body = dialog.querySelector("[data-body]");
  body.innerHTML = `
    <div data-consumer-impact role="status" aria-live="polite"></div>
    <label>Change reason <textarea data-reason rows="3" maxlength="1000"></textarea></label>
    <label class="checkbox-line"><input type="checkbox" data-confirm> I reviewed the lifecycle impact above.</label>
    <button type="button" class="primary-button" data-apply>Confirm ${action}</button>`;
  let verifiedImpact = false;
  const destructive = action !== "restore";
  const apply = body.querySelector('[data-apply]');
  apply.dataset.actionApply = '';
  const guard = protectActionDialog(dialog, { risk: destructive ? 'high' : 'bounded' });
  {
    apply.disabled = destructive;
    void loadConsumerUsage(body.querySelector('[data-consumer-impact]'), kind, id).then(result => {
      verifiedImpact = Boolean(result?.available && result.blocking_count === 0);
      if (dialog.isConnected) apply.disabled = destructive && !verifiedImpact;
    });
  }
  body.querySelector("[data-apply]").addEventListener("click", async (event) => {
    if (!guard.current()) { dialog.close(); return; }
    if (destructive && !verifiedImpact) return status(dialog, "Resolve or verify consumer impact before applying.", true);
    const reason = body.querySelector("[data-reason]").value.trim();
    if (!reason) return status(dialog, "A change reason is required.", true);
    if (!body.querySelector("[data-confirm]").checked) return status(dialog, "Confirm the impact before applying.", true);
    const button = event.currentTarget;
    try {
      button.disabled = true;
      const plural = kind === "profile" ? "agent-profiles" : "agent-teams";
      const result = await request(`/api/${plural}/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        body: JSON.stringify({ reason, expected_revision: item.revision }),
      });
      await onChanged?.(result?.item);
      dialog.close();
    } catch (error) {
      status(dialog, error.message || "Lifecycle change rejected.", true);
      button.disabled = false;
    }
  });
  dialog.showModal(); dialog.querySelector('[data-close]').focus();
}

