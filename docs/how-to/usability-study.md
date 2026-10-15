# Prepare a human-led usability study

This is a proposed protocol for human review. It contains no participant results,
approved retention policy, recruitment authorization or claim of study completion.
Delivery and review decisions belong in the linked research Work Item and its PR.

Use it to investigate whether people understand scope, evidence, authority and the
next action in codex-web. Browser tests establish behavior; this study observes
people using that behavior. Neither synthetic personas nor generated transcripts
count as participants.

## Prerequisites and review

A named human research owner and a privacy/data owner must review the protocol
before recruitment or sessions. Record their authenticated review Evidence, the
exact protocol commit, fixture revision and decision in the canonical Work Item.
Review must cover task wording, consent, sampling, accessibility, session methods,
retention, permitted observers and the handling of withdrawals. A source merge
only makes the proposal available; it does not approve a study.

Recruitment and participant contact need separate explicit authorization. No
invitation, message or provider action is part of this guide. A facilitator must
also confirm authority for the isolated study environment. Do not use production
Projects, retained requests, provider credentials or live business work.

Use the existing Artifact/Evidence and data-governance boundaries for collected
material. Confirm the owning domain actually supports governed deletion of each
stored payload; metadata-only deletion is insufficient. If approved storage,
retention, access or deletion cannot be established, do not collect session data.

## Questions and sampling

The study asks whether participants can:

1. Submit a request with the intended Project, repository and execution scope.
2. Distinguish an agent proposal from an independently verified outcome.
3. Explain an approval request's target, impact and evidence before deciding.
4. Find a safe next action for blocked work without replaying an uncertain start.

Propose an initial qualitative round of five consenting participants: three
individual developers and two team operators. Include both people new to codex-web
and experienced users, with at least one of each. Record actual experience and
recruitment limitations in broad categories. Five observations support qualitative
findings, not population estimates, an accuracy rate or a saturation claim.

Offer keyboard-only participation, adjustable text size, breaks and an accessible
alternative to screen sharing. Ask about accommodations without collecting a
diagnosis. A participant may decline any task. Record a skipped task separately
from an attempted failure. Do not silently replace missing participant groups
with employees, simulations or repeated sessions from one person.

## Consent and proposed data handling

Before each session, explain purpose, approximate duration, tasks, observers and
the difference between product evaluation and evaluation of the person. State
that participation is voluntary, a task or session can stop without penalty, and
withdrawal follows the reviewed retention plan. Obtain affirmative consent using
the approved method before observation; a preselected checkbox is insufficient.
Ask permission for observers separately and introduce them. Recording is optional
and requires distinct consent. Default to no audio/video recording.

The following values are proposals for human/privacy review, not deployed policy:

| Material | Proposed handling |
| --- | --- |
| Scheduling/contact and consent records | Restricted access for the research owner; separate from observation notes; delete 30 days after the debrief unless an approved obligation requires another term. |
| Raw notes and optional recordings | Confidential, deny model context, named research team only; delete 30 days after the debrief, with an outside limit of 90 days after collection. |
| De-identified findings and supporting extracts | Confidential by default, deny model context; review necessity after 180 days and delete or renew through canonical governance. |

Before collection, resolve approved dates into effective canonical retention
metadata and record the policy reference, classification, residency and permitted
viewers. Respect legal holds and inherited source restrictions. Do not put names,
emails, credentials, raw session notes or recordings in Git, GitHub issues, public
artifacts, telemetry or model prompts. Use a random participant alias; keep its
identity mapping only in the separately governed consent record.

For withdrawal, stop collection and have the authorized data owner locate and
redact/delete linked payloads through their owning domain. Verify the actual
receipts and dependent evidence invalidation. Explain any legal hold and the
limits of withdrawal after de-identification. Do not promise deletion from external
systems that the existing domain handler cannot perform.

## Session procedure

Allow approximately 45 minutes: five for consent and background, five for fixture
orientation, 28 for four tasks and seven for questions and closing. Explain that
all example content is synthetic. Use the [task fixture](../examples/usability-study-tasks.md)
and a fresh isolated account/Project for each participant. A browser fixture adapter
may display synthetic states, but label it and verify the intended interaction
works; a screenshot alone cannot measure a working submission/recovery flow.

Before the first participant, a facilitator should pilot every task, including
keyboard access, the simulated approval and uncertainty states. A pilot is not
participant evidence. Record environment commit, fixture revision, viewport/input
mode and any prototype limitations. Stop a task if it would invoke a live provider,
send a message or execute unreviewed generated code.

Use tasks A–B–C–D for three participants and A–C–D–B for two, assigning order before
sessions. Task A remains first to preserve the new-user first-value observation.
Later tasks start from independent fixture state, so a previous failure does not
prevent participation. Record order as a possible learning effect.

Read the task prompt verbatim. Ask participants to describe what they are looking
for when comfortable; silence is acceptable. Do not name the control or tell them
which status to trust. Useful neutral prompts include “What would you do next?”,
“What tells you that?” and “What would you expect to happen?” Avoid “Did you notice
the verified badge?” or “Isn't this blocked because the profile changed?”

Time each task from the end of its prompt until success, withdrawal or seven
minutes. At the limit, record incomplete and ask about the participant's intended
next action. Allow one neutral restatement; record additional navigation hints as
assistance. Intervene immediately to prevent unsafe action, recording the attempted
action without allowing its side effect. Observers take notes silently; the
facilitator alone speaks during tasks.

## Observation and analysis

Use the [observation and report template](../examples/usability-study-report.md).
Record independent success, assisted success, incomplete, unsafe attempted action
or skipped. Keep counts and denominators for attempted tasks; do not turn an
assisted success into independent success. Record elapsed time, hints, wrong turns,
the explanation given, and whether confidence matches the demonstrated outcome.
Time is descriptive, not a comparative benchmark for five people.

Link each observation to its exact fixture/task, participant alias, UI revision
and governed source Evidence. Separate direct observation, consented quotation,
researcher interpretation and recommendations. AI-assisted summaries, if separately
authorized and permitted by source governance, must be labelled and checked by a
human against the source; they cannot manufacture observations or human labels.

Use existing privacy-bounded [UX telemetry](../architecture/ux-telemetry.md) only as
supporting aggregate context. State its time window, event taxonomy, scope and
limitations. Do not add participant aliases or task text to telemetry, join random
journey IDs to identities, or treat absence of an event as proof of comprehension.

After sessions, invite only authorized product/engineering observers to a debrief.
Have observers record interpretations independently before discussing themes.
Retain disagreements, contrary examples, tasks that worked and sampling limits.
For each finding, report observed count/attempted count, evidence strength and a
recommendation to act, investigate or defer. Severity depends on impact and
observed behavior; recurrence in five sessions is not a prevalence estimate.

Create evidence-linked GitHub follow-ups only for supported findings, with the
affected task, concrete behavior and measurable validation. Review whether findings
support task-specific semantic rubrics or prototype comparisons before treating
those proposals as demonstrated demand. Do not change policy or product authority
from study scores.

## Verification, failures and recovery

The research Work Item remains open until the human-reviewed protocol, actual
consenting sessions, governed de-identified report and team debrief are evidenced.
An approved protocol or passing fixture test alone does not complete research.
Source publication, session approval, research findings and production readiness
are separate outcomes.

If a fixture fails, stop timing, mark a fixture interruption, preserve the permitted
diagnostic reference and repair it before continuing. Report affected tasks rather
than imputing participant failure. If unexpected live/sensitive content appears,
stop sharing and collection; ask the authorized data owner to contain and assess
it through existing incident/governance controls. Do not copy it into the report.
If a revision changes mid-study, pin observations to their actual revision and
report cohorts separately; never apply earlier feedback to a replacement artifact.

Related contracts: [data governance](../architecture/data-governance.md),
[Artifact/Evidence](../architecture/artifact-evidence.md), and
[product workspaces](../architecture/product-workspaces.md).
