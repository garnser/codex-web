# Frontend performance budgets

Codex Web treats UI responsiveness as an operational contract. Large Projects must remain usable without transferring or rendering complete registries in routine interactions.

## Budgets

These defaults are enforced by frontend code and regression tests:

| Interaction | Budget |
| --- | ---: |
| First useful Project workspace state | <= 1,000 ms when the server response is <= 500 ms |
| WebSocket event to visible Thread-row update | <= 250 ms |
| HTTP requests caused by an ordinary turn-completion event | 0; at most 2 narrow requests when bounded reconciliation is required |
| Thread rows in one list page | <= 100 (normal workspace page: 50) |
| Work Item DOM rows | <= 60 |
| Routine single-response payload | < 1 MB |
| One measured list render | <= 200 ms |
| Long main-thread task | <= 200 ms |

The browser exposes a bounded diagnostic snapshot at `window.__codexFrontendPerf` for development and test inspection. It is process-local browser state only; no performance sample is sent to an external service by this feature.

## What is measured

Request samples contain only:

- normalized endpoint identity (pathname plus query **keys**, never query values);
- HTTP method and status;
- response byte count;
- request latency;
- JSON parse duration;
- whether the request was aborted.

Render samples contain only the render name, elapsed time, row count and optional node count. Event samples contain only event-to-visible latency. Samples are capped in memory.

Performance telemetry must never include request bodies, response bodies, prompt text, provider payloads, Secret values, credentials or conversation content.

## Navigation and search

Project switches and Thread searches use latest-navigation-wins cancellation. An obsolete in-flight Project workspace request is aborted and its result is not allowed to overwrite newer Project/search state.

Routine turn/name/status events patch visible state directly. Narrow revalidation is reserved for missing data, binding changes, stream gaps and reconnect recovery.

## Large collections

Thread and Work Item collections are server-paginated. The browser does not render an entire large collection:

- normal Thread workspace pages contain up to 50 rows;
- the Work Item operator loads 50 rows per page and renders a maximum 60-row window even when many pages have been accumulated.

Full Thread history and Work Item detail remain on explicit detail paths rather than collection responses.

## Regression gates

Browser tests record request counts and payload bytes for:

- first Project load;
- cached Project switch;
- Thread search;
- turn completion;
- Thread selection;
- aborted Project loads.

Representative large-data tests cover 1,000-thread server-side list behavior and a 1,300+ Work Item browser session. Static boundary tests also prevent bundle growth, unbounded row rendering and removal of cancellation/telemetry contracts.

If a budget must change, update the documented contract, the `FRONTEND_BUDGETS` constants and the corresponding tests in the same reviewed change. Do not loosen a test solely to make a regression pass.
