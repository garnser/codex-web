# Usability observation and report template

This is a blank template, not a study result. Keep completed participant records
in approved governed Artifact/Evidence storage; do not commit them to source.
Use the [study protocol](../how-to/usability-study.md) and
[synthetic task fixture](usability-study-tasks.md).

## Session record

Record only the approved minimum:

| Field | Entry |
| --- | --- |
| Session alias | Random alias; no name or contact details |
| Consent reference | Restricted canonical reference, with no consent payload copied here |
| Experience category | New/experienced; individual developer/team operator |
| Protocol and fixture | Exact reviewed revisions and review Evidence references |
| Environment | UI/source commit, mock-state revision, input mode and viewport |
| Facilitator and observers | Authorized canonical references; observer consent confirmed |
| Task order | Preassigned order and any deviations |
| Governance | Classification, retention expiry/policy, access, residency and model-context denial |

For each task, record start/end or elapsed time, outcome, neutral restatements,
additional hints, navigation errors, observed actions and explanation. Use outcomes
**independent success**, **assisted success**, **incomplete**, **unsafe attempted
action**, **skipped**, or **fixture interruption**. Leave unavailable measurements
unset with a reason; do not replace them with zero or a presumed success.

Each observation must distinguish:

- observed behavior and its governed source reference;
- an optional, explicitly consented de-identified quotation;
- researcher interpretation and alternatives;
- the participant's stated confidence and demonstrated result;
- limits caused by simulation, fixture failure, assistance or missing evidence.

## De-identified report

Record the human authors, review date, protocol/fixture/UI revisions, approved
research Work Item and exact source Evidence. State actual participant categories,
recruitment method, missing groups and sampling limitations without identifying
people. Describe simulation limits, task order and exclusions.

Provide an outcome table by task with attempted count, independent success,
assisted success, incomplete, unsafe attempts and skipped/interrupted tasks.
Explain denominators and overlap: an unsafe attempt may also be incomplete;
avoid adding overlapping categories as if they were exclusive. Summarize time
descriptively and show assistance rather than concealing it in a single rate.

For each finding, include:

| Field | Required content |
| --- | --- |
| Finding | Concrete observed barrier or effective behavior |
| Task and revision | Exact affected task, UI and fixture revisions |
| Support | Observed/attempted count and governed source Evidence references |
| Interpretation | Human analysis, alternative explanation and contradictory observations |
| Severity | User impact, reversibility and strength of observed evidence |
| Recommendation | Act, investigate or defer, with rationale |
| Validation | A measurable behavior and proposed follow-up method |
| Follow-up | Canonical GitHub issue link when justified; no invented issue ID |

Report what worked as well as barriers. Keep qualitative observations separate
from aggregate telemetry and explain both sources' limitations. Label any
authorized AI-assisted summaries and their human verification. Do not infer
population prevalence, semantic calibration, independent technical verification
or production readiness from the study.

## Debrief and disposition

Record the authorized team debrief's date, attendees by role, reviewed findings,
disagreements, decisions and Evidence references. Explain which recommendations
were accepted, deferred or rejected and why. Link decisions concerning semantic
rubrics and prototype comparisons to the existing Work Items; observed demand
must come from actual sessions.

Record that the data owner verified retention/deletion responsibilities and any
withdrawals or legal holds without copying personal details. Publish only the
approved de-identified report. Actual consenting sessions, the delivered report
and team debrief must be evidenced before the research issue can close.
