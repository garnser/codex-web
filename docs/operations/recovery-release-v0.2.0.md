# Recovery release v0.2.0

The v0.2.0 recovery line replaces the v0.1.0 emergency deployment pattern where selected Python modules could be bind-mounted into an older image.

A qualified v0.2.0 deployment must run the application code contained in the immutable image. Application Python files are not supported as production compatibility bind mounts.

## Release model

Release publication is intentionally two phase.

### 1. Immutable candidate

After the repository tests workflow succeeds on main, the release workflow:

1. checks out the exact tested commit;
2. resolves the current .release-version;
3. pins the runtime base by digest for that build;
4. builds a local candidate with the release version and exact Git revision embedded;
5. verifies the Codex pin and OCI revision/version labels;
6. executes the Slack Socket Mode, stale-turn recovery, runtime-health hot-path and journal-lifecycle regression suites inside the exact candidate image;
7. launches the candidate through repository-controlled compose.yaml + compose.recovery.yaml + nginx;
8. reuses the clean-install onboarding qualification through nginx;
9. verifies restart persistence and repeated health/status latency;
10. pushes only the immutable sha-<commit> candidate with BuildKit SBOM and mode=max provenance attestations;
11. uploads sanitized candidate evidence.

The candidate is not promoted to the version tag or latest at this stage.

### 2. Qualified promotion

Promotion is a manual publish-release workflow dispatch in the protected recovery-release environment.

Promotion requires:

- exact candidate commit SHA;
- deployed candidate URL behind nginx;
- evidence identifier/timestamp for a real Slack root message;
- evidence identifier/timestamp for a real Slack threaded reply;
- GitLab webhook delivery/event evidence identifier;
- at least 15 minutes of stable Slack Socket Mode observation;
- at least the three migrated stale turns accounted for as reconciled or explicitly operator-blocked.

The workflow verifies that the candidate commit had a successful repository test run, pulls the immutable SHA image, checks its OCI revision/version labels, and verifies the deployed /api/version result matches the same commit.

It then repeatedly checks /api/status against the two-second qualification bound, verifies liveness/readiness/static/API routing through nginx, records the real-provider evidence, and only then promotes the existing candidate manifest to:

- <version>;
- v<version>;
- latest.

Promotion creates the immutable Git tag and attaches release-evidence.json to the GitHub Release.

## Runtime identity

A release image exposes GET /api/version.

The response contains releaseVersion, gitRevision, source and staticVersion. /api/livez includes the same release version/revision in its build object.

For a release deployment, releaseVersion and gitRevision must match the promoted image/tag evidence. Values are embedded at image build time and do not depend on source files mounted from the host.

## Supported recovery deployment

Set CODEX_WEB_IMAGE to garnser/codex-web:sha-<qualified-commit>, set CODEX_WORKSPACE to the approved workspace, then run:

    docker compose -f compose.yaml -f compose.recovery.yaml up -d --no-build

The recovery overlay disables application image building, requires an explicit image reference, removes direct host publication of the application container, publishes nginx instead, mounts only the tracked nginx configuration, and does not mount application Python modules.

Production TLS/authentication can remain in the operator's outer nginx/load-balancer boundary; the checked-in recovery nginx file defines the qualified reverse-proxy/WebSocket behavior.

## Upgrade from the v0.1.0 emergency recovery

Before changing the deployment:

1. record the current v0.1.0 image digest and container configuration;
2. list every source-file bind mount currently applied;
3. take and verify the state backup required by the normal recovery procedure;
4. stop ordinary execution or enter the appropriate maintenance/drain posture;
5. pull the qualified SHA-tagged candidate/release image;
6. remove application-source bind mounts from the service definition;
7. use the tracked Compose + recovery overlay;
8. start the image and verify /api/version reports the expected commit;
9. run the Project/bootstrap/readiness checks;
10. run stale active-turn reconciliation/inspection;
11. verify Slack Socket Mode, real message/reply, GitLab webhook/sync and health/status latency;
12. resume normal execution only after qualification succeeds.

Do not copy the former patched Python files into the new container. The fixes from the recovery period are part of tracked source and the immutable image.

## Rollback

Rollback must restore a compatible image + state combination.

If rollback remains compatible with the current canonical state:

1. stop/drain new execution;
2. retain the current v0.2.0 state backup and release evidence;
3. select the recorded previous image digest, not an unpinned mutable tag;
4. restore the state snapshot compatible with that image when required;
5. restore the previous deployment definition;
6. if the previous deployment genuinely depended on emergency compatibility mounts, restore only the exact recorded versions of those mounts together with their matching image/state snapshot;
7. verify liveness, readiness, Project readiness, workers, Slack/GitLab state and pending actions before resuming.

Do not roll an older binary onto newer incompatible canonical state merely by reintroducing old bind-mounted modules. If the canonical upgrade/rollback plan says rollback is unavailable, use recovery/forward-fix procedures.

## Evidence

Candidate and promotion evidence identify the exact Git commit, release version, immutable image digest, pinned base image digest, deployment-config SHA-256, nginx image reference when available, SBOM/provenance attestation mode, deterministic regression/Compose/nginx qualification, real Slack/GitLab evidence required for promotion, stale-turn accounting and the health/status latency threshold.

Emergency local overrides are not release evidence and must remain outside the supported repository-controlled deployment definition.
