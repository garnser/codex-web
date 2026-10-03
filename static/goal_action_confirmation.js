import { confirmAction } from './action_confirmation.js';

// UI descriptions only; Goal APIs own authority, revisions and runtime outcomes.
export function confirmGoalAction(action, goal, context, reference = '') {
  const terminal = ['Cancel Goal', 'Complete Goal'].includes(action);
  const effects = {
    'Cancel Goal': 'Permanently cancel this Goal. This transition does not cancel its bound Work Items.',
    'Complete Goal': 'Permanently complete this Goal using the selected evaluation. The server rechecks current bound work and evaluation eligibility.',
    'Activate Goal': 'Activate this Goal for governed execution within its canonical budgets and approvals.',
    'Resume Goal': 'Resume this Goal within its canonical budgets and approvals.',
    'Resume runtime': 'Resume bounded continuation for this execution binding. This may admit further provider work.',
    'Cancel runtime': 'Cancel continuation for this execution binding. Cancellation does not prove that already accepted provider work has stopped.',
    'Reconcile runtime': 'Record the operator-verified provider outcome for this execution binding. Use confirmed provider evidence before settling an unknown outcome.',
  };
  return confirmAction({
    action, target: `${goal.title} · ${goal.id}@${goal.revision}${reference ? ` · ${reference}` : ''}`,
    consequence: effects[action], risk: 'high', current: context.current,
    impact: `Known bound Projects: ${(goal.work_graph_bindings || []).map(item => item.project_id).join(', ') || 'none'}. Inspect the Work Graph and runtime bindings for their current state.`,
    recovery: terminal ? 'This Goal lifecycle transition is terminal; there is no Undo.'
      : action === 'Cancel runtime' || action === 'Reconcile runtime'
        ? 'Inspect the canonical runtime outcome before taking further action; this does not undo provider side effects.'
        : 'Pause remains available to stop further continuation; already accepted provider work may require reconciliation.',
  });
}
