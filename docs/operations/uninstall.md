# Uninstall or remove a deployment

> Applies to: current main  
> Audience: administrators  
> Risk: potentially destructive

## Goal

Stop and remove codex-web while making an explicit choice about canonical state, Codex authentication state and mounted repositories.

## Before removal

Decide which of these must be retained:

- canonical codex-web state/backups;
- Codex CLI authentication/configuration;
- local artifact content;
- mounted repositories/workspaces;
- external provider credentials and webhook configuration.

Removing codex-web does not automatically delete state owned by GitLab, Slack, model/action providers or other external systems.

## Docker Compose

### Stop without deleting persistent state

```bash
docker compose down
```

This stops the stack. Named volumes remain unless you explicitly remove them.

### Export/backup state first

Use the product Recovery/backup flow for state you intend to preserve. For a simple local deployment, also confirm the persistent volumes listed in [DOCKER.md](../../DOCKER.md).

### Permanently delete local codex-web volumes

This is destructive. Inspect the Compose project and volume names first. Only after confirming the data is no longer needed:

```bash
docker compose down --volumes
```

Do not run this as a routine upgrade step.

The host workspace configured by `CODEX_WORKSPACE` is a bind mount and is not intended to be deleted by codex-web.

## Native Python

1. Stop `python server.py` or the service manager running it.
2. Preserve or remove the configured codex-web data directory intentionally.
3. Remove the Python virtual environment if desired.
4. Keep/delete repositories separately; they are not application cache.

## External cleanup

If the deployment will not return, separately review:

- webhook registrations;
- provider/API credentials;
- service identities/tokens;
- reverse-proxy routes;
- secrets/key backend entries created specifically for this deployment.

Revoke credentials through their authoritative systems instead of merely deleting a local reference.

## Verify

- the codex-web HTTP endpoint is no longer reachable;
- no scheduler/worker/service process remains;
- retained backups can still be accessed with their required key backend;
- external webhooks no longer target a dead endpoint unless intentionally retained.
