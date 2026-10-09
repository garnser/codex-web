import { confirmAction } from "./action_confirmation.js";

export function createActionProviderBindingControls({
  state,
  apiRequest,
  setStatus,
  renderCatalog,
  refresh,
  projectText,
  resourceText,
}) {
  const pending = new Set();
  const refreshRequired = new Set();

  function bindingTarget(binding) {
    return `${binding.provider_type}/${binding.provider_instance} · binding ${binding.id} · tenant ${binding.organization_id}/${binding.workspace_id} · Project ${projectText(binding.project_id, state.projects)} · Resources ${resourceText(binding.resource_ids, state.resources)}`;
  }

  function toggleError(error, action, binding) {
    const detail = error?.message || "request failed";
    if (error?.status === 403) {
      return `Denied: ${action.toLowerCase()} ${binding.id} was not authorized. Administrator MFA or scoped service authority is required. ${detail}`;
    }
    if (error?.status === 404) {
      return `Binding missing: ${binding.id} is no longer visible in this tenant scope. Refresh canonical state before acting again. ${detail}`;
    }
    if (error?.status === 409) {
      return `Conflict: ${binding.id} could not be ${action.toLowerCase()}d in its current canonical state. Refresh and review the latest provider, Project and Resource target. ${detail}`;
    }
    if (!error?.status) {
      return `Update outcome unknown for ${binding.id}: ${detail}. Refresh canonical state before retrying.`;
    }
    return `${action} failed for ${binding.id}: ${detail}`;
  }

  async function toggle(button) {
    const bindingId = button.dataset.actionProviderToggle;
    const entry = state.items.find((item) => item.binding?.id === bindingId);
    if (!entry || pending.has(bindingId)) return;
    const binding = entry.binding;
    const enable = !binding.enabled;
    const action = enable ? "Enable" : "Disable";
    const promptGeneration = state.generation;
    const current = () => (
      button.isConnected
      && state.generation === promptGeneration
      && state.items.find((item) => item.binding?.id === bindingId)?.binding?.enabled === binding.enabled
    );
    const confirmed = await confirmAction({
      action: `${action} ActionProvider binding`,
      target: bindingTarget(binding),
      risk: "high",
      consequence: enable
        ? "This binding becomes eligible for new provider action resolution. This control does not prepare or execute an action; current authority, policy, security, Resource and provider gates still apply."
        : "This binding stops resolving for new provider actions. Existing durable ActionIntents and provider outcomes remain canonical and must finish or reconcile separately.",
      impact: `${entry.actions?.length || 0} advertised action contract(s) · credential reference ${binding.credential_ref || "none"}.`,
      recovery: `${enable ? "Disable" : "Enable"} the binding again after reviewing the current canonical target; every change requires fresh server authorization and applicable MFA/step-up.`,
      current,
      trigger: button,
    });
    if (!confirmed || !current()) return;

    pending.add(bindingId);
    renderCatalog();
    setStatus(`${action} pending for ${binding.provider_type}/${binding.provider_instance} binding ${bindingId}…`);
    let mutationAccepted = false;
    try {
      const result = await apiRequest(`/api/action-providers/bindings/${encodeURIComponent(bindingId)}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: enable }),
      });
      mutationAccepted = true;
      if (result?.item?.id !== bindingId || result.item.enabled !== enable) {
        throw new Error("PATCH response did not confirm the requested canonical enabled state");
      }
      await refresh({ loadingMessage: "Refreshing canonical ActionProvider state after update…" });
      const refreshed = state.items.find((item) => item.binding?.id === bindingId)?.binding;
      if (!refreshed || refreshed.enabled !== enable) {
        throw new Error("canonical refresh did not confirm the requested enabled state");
      }
      setStatus(`${action} succeeded for ${binding.provider_type}/${binding.provider_instance} binding ${bindingId}. Canonical enabled state: ${enable ? "yes" : "no"}.`);
    } catch (error) {
      if (mutationAccepted) {
        refreshRequired.add(bindingId);
        setStatus(`${action} was accepted for ${bindingId}, but canonical refresh could not confirm the result: ${error.message}. Refresh before another change.`);
      } else {
        if (!error?.status) refreshRequired.add(bindingId);
        setStatus(toggleError(error, action, binding));
      }
    } finally {
      pending.delete(bindingId);
      renderCatalog();
    }
  }

  return { pending, refreshRequired, toggle };
}
