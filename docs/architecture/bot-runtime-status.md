# Bot runtime status

`GET /api/bots` distinguishes transport observations from current Project
admission. Each `runtimeStatus` row retains its transport `status`, ping and
connection measurements. Its `gateReason` and `gateEligible` are resolved from
the existing canonical Project `bot-runtime` reconciliation gate for that
connection; `gateReasonSource` and `gateObservedAt` identify this fresh
projection. Connections sharing a Project reuse one decision within a response.
An unavailable connection or gate lookup is not eligible.

A connected socket does not establish admission eligibility. A Project may be
paused while transport metadata still says connected; conversely, a retained
pause observation must not imply the Project remains paused after canonical
resume. Projection reads do not change the gate, transport, telemetry or queues.

Previously observed telemetry reasons remain `lastObservedGateReason` with
`lastObservedGateReasonAt` when their observation time was recorded. Legacy
observations have a null time. A ping's later `updatedAt` is never substituted
for a gate observation time. This distinction is descriptive and grants no
authority: runtime ingress and dispatch continue to use the canonical gate.
