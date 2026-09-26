# Workspace interaction feedback

codex-web uses one interaction-feedback model for user-initiated operations. The UI must make it clear whether an action registered, is still running, completed, failed, or needs authoritative intervention.

## State model

| State | Meaning | UI requirements |
| --- | --- | --- |
| `acknowledged` | The client accepted the user's action and started the request. | Immediately disable duplicate submission for non-idempotent actions and show concise acknowledgement. |
| `in_progress` | The authoritative operation is still running or launch has been accepted. | Keep a stable pending state. Show determinate progress only when canonical progress exists; otherwise use an indeterminate state. |
| `succeeded` | Canonical state confirms the requested outcome or accepted background continuation. | Confirm success and refresh/reconcile the affected object without requiring a manual reload. |
| `failed` | The request or authoritative operation failed. | Use alert semantics, identify the affected action/object, preserve useful context, and expose retry/recovery when available. |
| `needs_attention` | The operation is blocked by approval, policy, readiness, remediation, or another authoritative gate. | Link or route to the authoritative check/object; do not imply completion. |

The shared `actionFeedback()` component in `static/workspace_components.js` is the preferred rendering primitive. Persistent workflow truth remains on the authoritative object rather than in transient notifications.

## Interaction rules

1. **Acknowledge immediately.** User input should never appear to disappear. Disable or guard the initiating control while a non-idempotent operation is pending.
2. **Do not invent progress.** Percentages and step counts come only from canonical backend progress. If none exists, show an indeterminate running state.
3. **Reconcile before claiming success.** UI success should be based on the returned or refreshed canonical state, not only on a successful click handler.
4. **Make recovery actionable.** Validation and backend failures should preserve enough context to correct, retry, or open the relevant remediation surface.
5. **Keep authority server-owned.** Optimistic UI is inappropriate for security-, approval-, lifecycle-, or authority-sensitive mutations unless rollback and reconciliation are deterministic.
6. **Allow safe navigation.** Long-running background work should continue independently when the canonical operation supports it; the UI may surface completion later without trapping the user.
7. **Avoid duplicate submission.** Non-idempotent actions must guard repeated clicks while pending. Where APIs support idempotency keys, clients should supply them.
8. **Respect reduced motion.** Feedback must remain understandable with `prefers-reduced-motion: reduce`; animation is optional decoration, never the only state signal.
9. **Avoid notification noise.** Use transient feedback for the action lifecycle and durable object state for ongoing status.

## Qualified representative flows

The browser suite currently qualifies the shared semantics and representative async workflows:

- `tests/browser/workspace_components.spec.js` verifies accessible lifecycle states and confirms that progress is not fabricated.
- `tests/browser/project_setup_ui.spec.js` verifies Project bootstrap acknowledgement, duplicate-submit prevention, canonical success, failure context, and a remediation/retry path.
- `tests/browser/automation_product_ui.spec.js` verifies Automation run acknowledgement and completion against canonical run state.

Additional surfaces should reuse the same state names and semantics when they adopt action feedback.

## Review checklist

Before shipping a user-initiated async action, verify:

- the initiating control acknowledges immediately;
- duplicate submission is prevented where needed;
- a stable pending state exists for non-immediate operations;
- success reconciles with canonical state;
- failure names the failed action/object and exposes recovery when possible;
- blocked/approval states point to the authoritative source;
- no fabricated progress is displayed;
- keyboard, screen-reader, narrow-layout, and reduced-motion behavior remain usable.
