import { confirmAction } from './action_confirmation.js';
import { projectViewOperation, currentProjectId } from './project_view_scope.js';

export function edgeCreator({ draft, canMutate, setManagementStatus, loadGraph }) {
  let creating = false;
  return async function addEdge() {
    if (creating) return;
    const ticket = draft.submission();
    const operation = projectViewOperation(setManagementStatus, 'refresh-work-graph');
    if (!canMutate()) return setManagementStatus("Graph mutation requires admin + MFA/step-up or work-graph:admin service authority.");
    const relation = document.getElementById("work-graph-relation")?.value || "blocks";
    const sourceRef = document.getElementById("work-graph-source")?.value || "";
    const targetRef = document.getElementById("work-graph-target")?.value || "";
    const failure = document.getElementById("work-graph-failure-behavior")?.value || "pause";
    const reason = document.getElementById("work-graph-edge-reason")?.value.trim() || null;
    if (!sourceRef || !targetRef) return setManagementStatus("Choose both source and target Work Items.");
    if (sourceRef === targetRef) return setManagementStatus("A relationship cannot target the same Work Item.");
    const failureBehavior = relation === "parent" ? "pause" : failure;
    if (!await confirmAction({ action: 'Add Work Item relationship', target: `${sourceRef} ${relation} ${targetRef}`, risk: 'bounded', consequence: `Add ${relation} relationship ${sourceRef} → ${targetRef}? The server will reject cycles, scope conflicts and duplicate-policy conflicts before saving.`, recovery: 'An authorized operator can remove this relationship; readiness will be recomputed.', current: operation.current })) return;
    if (!ticket.current() || creating) return;
    creating = true;
    try {
      await operation.request(`/api/work-graph/edges?project_id=${encodeURIComponent(currentProjectId())}`, {
        method: "POST",
        body: JSON.stringify({
          relation,
          source_ref: sourceRef,
          target_ref: targetRef,
          failure_behavior: failureBehavior,
          reason,
        }),
      });
      if (!ticket.current()) return;
      ticket.saved();
      if (draft.dirty()) return setManagementStatus("Relationship saved. Newer edits remain unsaved.");
      setManagementStatus("Relationship saved through the canonical graph service.");
      await loadGraph();
    } catch (error) {
      if (!operation.current() || !ticket.current()) return;
      setManagementStatus(`Relationship rejected: ${error.message}`);
    } finally { creating = false; }
  };

}
