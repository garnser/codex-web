# Security Policy

## Supported versions

Security fixes are delivered on the current `main` branch. Use the latest
revision of `main`, or a release containing the relevant fix. Older releases,
tags, and forks do not have a separate security maintenance commitment; reports
affecting them are still welcome when the issue also affects current `main`.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through
[GitHub's Report a vulnerability form](https://github.com/garnser/codex-web/security/advisories/new).
Private vulnerability reporting is enabled for this repository. Do not disclose
exploit details in public issues, pull requests, discussions, or logs.

If you cannot use GitHub private reporting, email the maintainer,
[Jonathan Petersson](mailto:jpetersson@garnser.se), with the subject
`codex-web security report`.

Include, when available:

- The affected commit or release, installation method, and relevant configuration.
- The affected component and the permissions or access needed to reproduce it.
- Reproduction steps or a minimal proof of concept using synthetic data.
- Expected and observed behavior, impact, and any suggested mitigation.
- Whether the issue has been disclosed elsewhere.

Redact credentials, tokens, cryptographic keys, personal data, private repository
content, and tenant data. Share only the minimum information needed to reproduce
the issue. Test only on systems you own or are authorized to assess.

## Coordinated disclosure

The maintainer will review the report, request additional information when
needed, and coordinate fixes and disclosure through the private report. Response
and remediation timing depend on severity and available maintainer capacity;
there is no guaranteed service-level agreement or bug bounty.

Please coordinate public disclosure with the maintainer so users have an
opportunity to apply a fix or mitigation. Reporter credit can be included with
the reporter's consent.

## Deployment guidance

Codex Web can execute tools and modify repositories. Review the deployment and
execution boundaries in [README.md](README.md#security-model),
[DOCKER.md](DOCKER.md), and the [administration documentation](docs/administration/README.md).
Restrict access to trusted users, preserve authentication and authorization
controls, and choose sandbox and approval settings appropriate to your environment.

Suspected authorization bypasses, cross-tenant data access, credential leaks,
webhook-verification failures, and worker or sandbox escapes belong in the
private reporting channel. Ordinary bugs and feature requests can use the
[public issue templates](https://github.com/garnser/codex-web/issues/new/choose).
