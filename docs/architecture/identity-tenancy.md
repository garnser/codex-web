# Identity, tenancy, sessions, and service principals

Codex-web treats **human identity**, **service identity**, **agent identity**, **execution-worker identity**, and **execution role** as separate concepts. Authentication establishes who or what is calling; canonical membership and later policy determine what that actor may do.

## Tenant hierarchy

The platform tenant hierarchy is:

`Organization -> Workspace -> Project -> Work Item / execution state`

Every new tenant-scoped domain should carry an unambiguous organization/workspace scope. Existing single-user installations migrate deterministically to organization `local`, workspace `default`, and human identity `local-admin`.

Project and Work Item models retain migration-safe defaults for legacy persisted records. Newly projected work items inherit tenant scope from their owning project.

Cross-tenant API access fails as not found where object existence itself should not be disclosed.

## Principals and membership

`HumanIdentity` represents a person. `ServiceIdentity` represents non-human callers such as automation. Membership is a separate canonical record binding one principal to an organization and optionally a workspace with administrative roles and teams.

External authentication claims do **not** grant codex-web authority. OIDC/SSO subjects are linked to a canonical human identity, then ordinary codex-web membership is resolved. External group claims are retained only as provider metadata until an explicit mapping mechanism reconciles them.

Service tokens are always attributed to a ServiceIdentity and never masquerade as a human session.

## Local compatibility mode

`CODEX_WEB_IDENTITY_MODE=local-trusted` is the current self-hosted compatibility default. It bootstraps one local administrator in `local/default` and preserves the existing no-login workflow.

`CODEX_WEB_IDENTITY_MODE=enforced` disables this fallback. Requests then require a valid server-side session or service token, except the minimal liveness/auth-verifier endpoints.

Hosted or remotely exposed deployments should use enforced mode once an authentication adapter is configured.

## Browser sessions

Server-side session records contain only SHA-256 hashes of randomly generated high-entropy session, refresh, and CSRF tokens. Raw values are returned only when credentials are minted or rotated.

Session controls include:

- idle and absolute expiry;
- session-token and refresh-token rotation;
- replay detection for previously used refresh tokens;
- immediate server-side revocation;
- active-session enumeration and revoke-other-sessions;
- step-up/MFA assurance windows;
- CSRF validation for cookie-authenticated state-changing requests.

A detected refresh-token replay revokes the affected session before returning the authentication error.

Bearer-authenticated API sessions do not require CSRF because the credential is not ambient browser state.

## Service tokens

Service tokens carry:

- canonical service identity;
- organization/workspace scope;
- explicit service scopes;
- optional expiry;
- rotation counter and last-use metadata;
- revocation metadata.

The raw token is never returned by list/inspection APIs after initial creation. A token remains subject to canonical membership and cannot use request headers to change tenant scope.

## Authentication assurance and sensitive administration

Authentication assurance is explicit. Sensitive identity administration requires an administrator role plus MFA-equivalent assurance. The local-trusted compatibility actor has the strongest local assurance only while local-trusted mode is deliberately enabled.

The identity service exposes a provider-neutral step-up hook; concrete MFA/SSO proof remains the responsibility of the authentication adapter rather than a client-provided boolean.

## Security boundaries

Identity middleware enforces:

- tenant header consistency;
- session/service-token revocation and expiry;
- CSRF for cookie mutations;
- no external-claim-to-role shortcut;
- no cross-workspace service-token use;
- no credential hashes in administration responses.

`AuthenticationRateLimiter` is a shared deterministic brute-force hook for current/future local/OIDC authentication adapters. Successful authentication clears the key; repeated failures within the configured window fail closed.

Future identity-provider, SCIM, recovery, approval, secret, resource, worker, and policy features must build on these canonical identities/memberships instead of maintaining independent authorization state.
