# Documentation versioning and review

Documentation describes a specific product contract and must change with that contract.

## Version awareness

- Guides should state whether they apply to a named release or current main.
- Exact API schemas come from the running release's OpenAPI output and versioned code.
- Architecture documents describe durable contracts and should identify compatibility/version boundaries where applicable.
- Examples must not claim a feature exists merely because it appears on the roadmap.

## Ownership

A PR that changes user/operator behavior should review:

- Getting Started when prerequisites/startup change;
- Core Concepts when the mental model or authority boundary changes;
- Administration when identity, policy, secrets, providers or definitions change;
- Operations when health/recovery/incident/release/upgrade behavior changes;
- Reference when API/configuration/version contracts change.

## Release review

Before a release:

1. run the documentation link/reference checks;
2. verify the Getting Started commands against a clean supported environment;
3. verify screenshots/examples against the release UI;
4. confirm deprecated/removed behavior is not presented as current;
5. confirm security-sensitive examples contain no real secrets.

## Links

Prefer relative repository links for versioned documentation so a checked-out tag/commit points to matching docs.
