# API Authorization

Codex Web applies authorization at a canonical HTTP boundary after authentication and tenant resolution and before an API handler runs.

Every operation under `/api/` must resolve to an explicit API authorization policy. Unknown API domains fail closed during application composition and request handling.

## Policy modes

Each operation exposes one of four access modes:

| Mode | Meaning |
| --- | --- |
| `public` | No authenticated actor is required. Reserved for explicitly classified probes such as process liveness. |
| `authenticated` | Any authenticated actor with a valid canonical tenant/workspace membership may reach the handler. Domain services may impose stricter object or operational authority. |
| `admin` | Human callers require Owner/Admin membership. Sensitive policies may additionally require MFA/step-up. Service identities continue through the domain-specific service-scope checks enforced by the handler/service. |
| `authority` | The request must be allowed by the canonical `AuthorityRoleService` for the declared capability and authority level. |

The HTTP boundary is additive. Tenant isolation, CSRF, MFA, service-token scopes, ApprovalRequests, ActionIntent policy, resource constraints, and domain-specific authorization remain authoritative and may deny a request that passes the coarse API policy.

## Operational authority

API policies do not create a second operational-role engine. Operations classified as `authority` are evaluated through the canonical operational Role catalog and `AuthorityRoleService`.

For example, retrying or reconciling a Work Item requires the `work-item.operate` capability at `execute` level. A tenant member without a matching operational Role is denied even if the Work Item is visible.

The local-trusted bootstrap administrator retains compatibility through its canonical wildcard operational grant.

## OpenAPI and Swagger

Generated OpenAPI operations include an `x-codex-authorization` extension containing:

- access mode;
- required capability;
- authority level;
- required assurance when applicable;
- declared service scopes when applicable.

Protected operations also advertise the Codex session cookie and bearer-token schemes and document `401` and `403` responses. The in-app Swagger browser therefore displays the same authorization contract used by runtime enforcement.

## Adding an API

A new top-level `/api/<domain>` route must be classified in the canonical API authorization registry before the application is considered valid. Do not add an unclassified fallback or perform authorization only in browser code.

When an operation needs stronger policy than its domain default, add an exact operation policy. Prefer operational capabilities for actions whose authority depends on assigned Roles/delegations rather than membership administration.

Tests must cover both the route policy and the domain/service authorization that protects the underlying object or side effect.
