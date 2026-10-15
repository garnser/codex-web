# Synthetic usability task fixture

Fixture revision: `usability-study-tasks/1.0`. These are mock records and facilitator
instructions, not live Work Items, actual verification, consent or study findings.
Use with the [reviewed study protocol](../how-to/usability-study.md). This document
does not provision an environment or implement a simulator. The facilitator must
prepare and pilot the following states in an authorized isolated environment.

Use the same visible text, record relationships and state for all participants.
Record any deviation and actual source/UI revision. Never seed a simulated result
into a production ledger or present a mock provider receipt as actual delivery.

## Shared context

The fictional Project is **Harbor demo**. Its two mock repositories are **harbor-web**
and **harbor-infra**. The example role can edit harbor-web in an isolated workspace
but cannot deploy or change harbor-infra. No provider credentials are present.
Use anonymous synthetic customer examples, with no real names or business data.

## A: First request and first value

Participant prompt: “You want the Harbor demo web application to show a helpful
message when a saved search returns no results. Prepare a request for that change
and show how you would check what happened.”

Starting state: open a plain Thread with no optional setup enabled. Project and
repository selectors are available; the initial repository is harbor-infra to
make scope review observable. Optional request helpers remain in Thread settings.
An explicitly labelled simulator may return a proposed empty-state change followed
by a separately inspectable deterministic test result. It must never run a real
model, push source or modify the repositories.

Success observation: the participant selects Harbor demo/harbor-web, describes a
useful empty-state result without authorizing deployment, finds the resulting work
and explains the displayed scope and actual evidence. Record request preparation,
simulated submission and evidence inspection separately. If no working simulator
is available, stop at preparation and mark first-value inspection unassessed;
do not claim end-to-end success from a static screen.

## B: Proposal versus verified outcome

Participant prompt: “A teammate says the empty-search task is finished. Decide
what you can tell them from these records, and explain what you would do next.”

Starting state: a synthetic agent proposal says “Implemented and ready to release.”
Its source is informational and has no independent Verification. A separate test
Evidence record passes for exact source revision **demo-r1**. The displayed change
is now **demo-r2**, which has no passing proof. Neither has provider delivery or a
deployment receipt. Label all these records as fixture content.

Success observation: the participant distinguishes the claim, the valid historical
test result and the unverified current revision. They do not report release,
delivery or business acceptance, and identify review/testing of demo-r2 as a next
action. Record what evidence they inspect, including contradictory interpretations.

## C: Approval target and consequences

Participant prompt: “An approval request is waiting for you. Review it and explain
whether you would approve it, decline it or ask for more information.”

Starting state: the task concerns harbor-web, while a synthetic approval targets
harbor-infra and grants a deployment action. Its evidence covers harbor-web's
demo-r1 test and does not verify the proposed infrastructure operation. The control
may record a local simulated choice only; it cannot approve a canonical live action.

Success observation: the participant checks the target, impact and evidence,
recognizes the scope mismatch and declines or asks for clarification. A decision
to approve without recognizing the mismatch is recorded as an unsafe attempted
action, even though the simulator prevents external effects. Ask for reasoning
before disclosing the intended mismatch.

## D: Recovery and uncertainty

Participant prompt: “Your task appears blocked after a connection failed. Find a
safe next action and explain what you know about whether work has already started.”

Starting state: an original request retains its complete scope and reply target.
The native start outcome is unknown and requires reconciliation; no receipt is
available. A second, clearly separate fixture request has a controller-proven
pre-start refusal and an actionable owner/permission blocker. Viewing either record
does not retry it. Any recovery click is simulated and clearly labelled.

Success observation: the participant does not infer that the uncertain original
never started from an absent receipt or retry it blindly. They find reconciliation
or an accountable operator and distinguish the proven refusal's safe next action.
Record the two dispositions separately. Finding a Retry button alone is not success.

## Facilitator reset

Return to a clean isolated fixture snapshot between participants and between
independent tasks. Verify that drafts, approvals, participant choices and simulated
responses do not leak into the next session. Record reset/fixture failures and
exclude interrupted task time from ordinary completion-time summaries. Dispose of
temporary local choices under the reviewed data-handling plan; retain only approved
governed observations. No production queue, identity, authority or native replay
belongs in fixture setup or reset.
