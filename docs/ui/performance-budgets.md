# Frontend performance budgets

Codex Web treats responsiveness as a bounded-data and bounded-work contract, not only a wall-clock target. Wall-clock telemetry remains useful, but deterministic request, payload, pagination, and rendered-row limits are the primary regression guards.

## Global budgets

The shared frontend telemetry in `static/frontend_perf.js` records request metadata, render durations, event-to-visible latency, long tasks, and milestones without recording request or response content.

| Budget | Limit |
| --- | ---: |
| Project useful state | 1000 ms |
| Live event visible | 250 ms |
| Routine response body | 1 MB |
| Single render | 200 ms |
| Long task | 200 ms |
| Requests in a 1-second burst | 12 |
| Repeated requests to the same endpoint identity in a 1-second burst | 4 |
| Thread rows rendered | 100 |
| Work Item rows rendered | 60 |

Endpoint identity retains the path and query-key names but not query values, so performance telemetry does not retain search terms, IDs, prompts, or other query values.

## Primary surfaces

### Project / thread workspace

Initial Project bootstrap is expected to use at most three bounded requests when projects and models are not cached. A cached Project switch or thread search uses one server-shaped `/ui-state` request. Turn-completion events update visible thread state incrementally and must not trigger HTTP revalidation in the normal case.

### Home

Home is a read-only server-shaped projection. The browser issues one `/api/home?project_id=...` request per selected Project. The service caps each section at five items by default (and never above ten), while Work Item source reads are bounded. Project changes abort stale Home requests so older responses cannot replace newer Project state.

### Work Items and Runs

Work Item list pages contain 50 items. The browser renders at most 60 loaded Work Item rows and keeps additional rows outside the active DOM window. Run history uses a separate bounded page of 20 items, with active Runs pinned. Live Run events refresh the bounded Run projection rather than replaying complete Work Item history.

### Operations / Observability

A normal Operations refresh consists of five bounded requests: observability, operations summary, execution assignments, current identity, and Projects. Concurrent refresh triggers are coalesced into a single in-flight refresh so opening the panel and pressing refresh cannot recursively multiply the request batch.

## Regression policy

Browser tests should assert request counts and rendered-row limits directly. Large-state fixtures should prove that increasing canonical dataset size does not increase initial client rendering without an explicit page/load-more action. The shared request-window guard flags both total request bursts and repeated calls to one endpoint identity.

Performance changes must not weaken authorization, provenance, canonical consistency, or visibility of authoritative state. If a screen needs more data, prefer a bounded server projection, pagination, or an explicit drill-down rather than silently omitting data or loading a whole registry into the browser.
