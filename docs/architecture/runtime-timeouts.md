# Execution duration limits

Application-owned execution duration settings accept `0` or `-1` to disable
that elapsed limit. Positive values enforce a duration. Other negative values,
NaN and infinity are invalid. Stored request/definition sentinels remain intact;
normalize only when constructing timers. An omitted value retains the owning
API's existing default/inheritance behavior, so omission is distinct from zero.

Independent enabled limits intersect: disabling a step duration does not disable
a positive procedure total, registered operation cap, or assignment deadline.
Explicit cancellation still cancels and joins the actual producer. A child
operation's timeout cancels that child; parent cancellation reaches every child.
Storage write guards continue to refuse writes after cancellation.

Finite renewable worker/ActionIntent/capacity leases, fencing, authority expiry,
break-glass TTL, network connection liveness and CPU/memory/disk/process quotas
retain their existing semantics. Poll/watchdog intervals are scheduling controls;
setting those to zero stops the corresponding watchdog, rather than disabling an
execution limit. No additional model calls or retries are introduced.

## Operator configuration

These existing RuntimePolicy duration settings accept either disabled sentinel:

| Environment setting | Duration controlled |
| --- | --- |
| `CODEX_WEB_CONTINUITY_DISPATCH_TIMEOUT_SECONDS` | One continuity dispatch |
| `CODEX_WEB_NATIVE_RECOVERY_CYCLE_TIMEOUT_SECONDS` | One native recovery cycle |
| `CODEX_WEB_LOCAL_PREPARATION_TIMEOUT_SECONDS` | Local preparation operations |
| `CODEX_WEB_NATIVE_RPC_TIMEOUT_SECONDS` | Native RPC/startup/turn waits |
| `CODEX_WEB_WORKER_WALL_TIMEOUT_SECONDS` | Default for new worker assignments |
| `CODEX_WEB_PROCEDURE_OPERATION_TIMEOUT_SECONDS` | Built-in operation defaults |
| `CODEX_WEB_ACTION_TIMEOUT_SECONDS` | Newly created intent default when omitted |
| `CODEX_WEB_WORK_ITEM_HANDOFF_TIMEOUT_SECONDS` | Handoff elapsed warning |
| `CODEX_WEB_WORK_ITEM_HANDOFF_DELIVERY_TIMEOUT_SECONDS` | Delivery elapsed warning |

Explicit positive assignment and intent durations remain enforceable. Historical
assignments, published definitions, pinned procedures and uncertain outcomes are
not rewritten. Set the canonical Executive invocation duration and publish new
procedure/Automation revisions through their existing configuration/definition
APIs when those independently configured limits should be disabled. UI duration
fields accept the sentinels and inspected disabled durations display **Disabled**.
Install compatible code before setting live configuration to a disabled value.

## Compatibility

Disabled procedure durations require procedure schema `1.5`; a new procedure
checkpoint with no elapsed deadline uses `1.2`. Automation definitions add schema
`1.3`. Model Gateway state uses `1.8` with the registered no-rewrite migration
from `1.7`. ActionProvider contract `1.3` advertises sentinel support; older
providers cannot advertise sentinel ActionDefinitions. ActionIntent state emits
`1.7` when it contains a sentinel, retaining `1.6` for positive-only records.
Worker state emits `1.16` for disabled wall limits or new procedure checkpoints;
older supported positive-only shapes remain readable. Work lifecycle timeout
metadata has no separate nested version marker; old positive-only validators
cannot read newly stored sentinel metadata.

Treat deployment as a protocol extension: drain old local workers/readers before
new-format writes. Old-reader rollback after such writes is unsupported. Preserve
existing UNKNOWN outcomes, action keys, ownership receipts and historical pins;
a disabled timer supplies no evidence that a timed-out action had no effects.
