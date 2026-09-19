# API authorization and OpenAPI metadata

codex-web has one canonical HTTP authorization declaration for every `/api`
operation. The declaration is used both by runtime request enforcement and by
generated OpenAPI/Swagger metadata.

It does not replace stricter domain authorization. It provides the common HTTP
boundary that runs after authentication/tenant validation and before the route
handler.

## Policy classes

Every API operation is classified as one of:

- **public** — no authenticated actor is required. This is reserved for the
  liveness/auth-verifier endpoints.
- **authenticated** — a canonical human or service actor in the tenant/workspace
  is required. Domain ownership, service scope, ApprovalRequest, lease,
  entitlement, policy and other checks may still be stricter.
- **admin** — human callers require Owner/Admin membership, optionally with MFA
  step-up. Service callers remain subject to the endpoint's exact service-scope
  contract.
- **operational** — the request additionally requires a canonical
  `AuthorityRoleService` capability/level decision. Work-item/operator
  mutations use this class.

Safe read operations default to the authenticated class unless explicitly
public. Mutation families must be explicitly declared. Adding an unknown
mutation family causes API authorization installation to fail rather than
silently making the route member-writable.

## Ordering with authentication, tenancy and CSRF

The request path is:

```text
session/service authentication
-> tenant/workspace header isolation
-> cookie CSRF validation for mutations
-> canonical API authorization policy
-> route/domain authorization
-> service/domain behavior
```

The API authorization middleware consumes `request.state.identity_actor`
created by the existing identity boundary. It does not parse tokens or infer
tenant identity itself.

Cross-tenant object hiding and tenant-scoped service lookups remain implemented
by the canonical domain services.

## Human administration and step-up

Administrative API declarations can require:

- membership role: Owner/Admin;
- assurance: MFA.

The API boundary checks those coarse requirements before invoking the handler.
If the endpoint already has a stronger or more specific rule, that rule still
runs.

Self-service session revocation is explicitly classified as authenticated rather
than admin-only. The identity endpoint continues to decide whether a requested
session is the caller's own session or requires administrator step-up.

## Service identities and service scopes

Service identities authenticate with tenant-scoped service tokens.

For domains whose service scopes already differ operation by operation, the API
policy records `service_scope_mode=domain`. The HTTP boundary does not guess a
scope name and thereby weaken or conflict with the existing endpoint rule.

The exact endpoint/domain check remains authoritative, for example
`metrics:admin`, `scheduler:admin`, `business-data:admin`, or other
domain-defined scopes.

Policies can also declare explicit service scopes where a route has one stable
HTTP-level requirement.

## Operational capability authorization

Operational mutations reuse `AuthorityRoleService`; there is no separate API
role engine.

The policy declares:

- capability;
- required `AuthorityLevel`.

At request time the boundary constructs an `AuthorityEvaluationRequest` and
uses the effective canonical authority-role Definition for the actor and
tenant/workspace.

For example, work-item/operator mutations require:

```text
capability = work_items.operator
level = execute
```

A downstream work-item route may still apply stricter project/tenant/object
checks. API authorization cannot broaden an AuthorityRole grant.

The local-trusted compatibility administrator continues to work because the
bootstrap local authority catalog binds the local administrator to the wildcard
approve-level grant.

## Swagger / OpenAPI

Protected operations include:

- `security` entries for bearer/session authentication;
- standard `401` and `403` response descriptions;
- `x-codex-authorization` structured metadata;
- a visible **Authorization:** block in the Swagger operation description.

Example vendor metadata:

```json
{
  "kind": "operational",
  "human_roles": [],
  "required_assurance": null,
  "service_scope_mode": "domain",
  "service_scopes": [],
  "capability": "work_items.operator",
  "authority_level": "execute",
  "domain_checks_remain_authoritative": true
}
```

OpenAPI also declares:

- `bearerAuth` — session bearer or tenant-scoped service token;
- `cookieSession` — browser session cookie.

Cookie mutation requests remain subject to CSRF even though OpenAPI represents
authentication separately.

## Developer rules for new routes

When adding an API operation:

1. decide whether it is truly public;
2. for reads, use authenticated tenant semantics unless a stronger policy is
   required;
3. put administrative configuration mutations in the appropriate admin family;
4. if the operation is a domain mutation with an existing exact service-scope
   or authority gate, declare the mutation family explicitly and preserve that
   downstream check;
5. for operational authority, use `AuthorityRoleService` capability/level
   rather than adding another role list;
6. add representative allow/deny tests;
7. verify the generated OpenAPI operation contains
   `x-codex-authorization`, security metadata and 401/403 responses.

Do not add an unclassified mutation route. The composed-application contract
test requires every `/api` operation to have an explicit policy.

## Failure behavior

- no actor on a protected route -> `401`;
- authenticated actor lacking API coarse policy -> `403`;
- operational Role authority denied -> `403` with canonical authority reason;
- domain service-scope/approval/policy denied -> the existing domain response;
- undeclared composed API mutation -> startup/test failure;
- unknown/nonexistent URL -> normal `404`.

Authorization metadata is explanatory state, not browser-side authorization
truth. The server always enforces the canonical policy.
