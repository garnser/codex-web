# Docker deployment

The application image is self-contained at runtime: it runs codex-web and includes the Codex CLI, Node.js, Python, and the locked Python runtime dependencies. Normal builds use the trusted `garnser/codex-web:base` image published from this repository's `main` branch, so those slow dependencies do not need to be rebuilt for every application change. The host only needs Docker/Compose; it does not need Codex, Node.js, npm, or Python installed.

The container uses a non-root `codex` user, persists application state under `/app/data`, stores Codex CLI state under `/home/codex/.codex`, and expects developer repositories to be mounted below `/workspace`.

## Start with Docker Compose

```bash
cp .env.example .env
mkdir -p workspace
docker compose build
docker compose run --rm codex-web codex login
docker compose up -d
```

The UI is exposed on `127.0.0.1:8765` by default. Set `CODEX_WEB_BIND=0.0.0.0` only when a reverse proxy or trusted network boundary protects the service.

The image has a Docker healthcheck against `/api/livez`. `/api/healthz` remains the deeper readiness/daemon-health endpoint and can return 503 when the Codex app-server is unavailable.

## Production release image

Production releases are driven by the repository-level `.release-version` file. After the normal `tests` workflow succeeds for a `main` commit, `.github/workflows/publish-release.yml` checks out that exact tested SHA and publishes it only when the declared version has not already been tagged. The workflow:

1. builds the final application image from the trusted `garnser/codex-web:base`;
2. verifies the pinned Codex CLI version;
3. smoke-tests `/api/livez`;
4. publishes `garnser/codex-web:<version>`, `garnser/codex-web:v<version>`, `garnser/codex-web:sha-<commit>`, and `garnser/codex-web:latest`;
5. creates the matching `v<version>` Git tag and GitHub Release from that exact commit.

The workflow creates the tag only after the production image has built, passed its smoke test, and been pushed successfully. Existing release tags are never moved.

For the first release:

```bash
docker pull garnser/codex-web:0.1.0
# equivalent immutable release alias
docker pull garnser/codex-web:v0.1.0
```

## Runtime base image

`Dockerfile.base` owns the slow, rarely changing runtime foundation:

- Python 3.14 base image;
- required Debian runtime tools;
- Node.js 24;
- the pinned Codex CLI;
- packages from `requirements.lock`.

`.github/workflows/publish-base-image.yml` cold-builds and verifies that image on trusted `main` changes, then publishes both:

- `garnser/codex-web:base` as the current trusted base used by ordinary builds;
- `garnser/codex-web:base-<main-commit>` as an immutable historical tag for traceability and rollback.

The publishing workflow authenticates with the GitHub Actions secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`. Never place those credentials in `.env`, a Dockerfile, build arguments, repository files, or application configuration.

`CODEX_WEB_BASE_IMAGE` controls the base used by Compose. The default is:

```text
garnser/codex-web:base
```

To reproduce or roll back to a particular runtime foundation, set it to an immutable tag, for example:

```bash
CODEX_WEB_BASE_IMAGE=garnser/codex-web:base-<main-commit> docker compose build
```

Normal CI pulls the published base and builds only the small codex-web application layer. If a PR changes `Dockerfile.base`, `requirements.txt`, `requirements.lock`, the base publishing workflow, or the configured Codex version, CI instead cold-builds a candidate base locally and builds the application on top of that candidate. This keeps dependency/runtime changes fully validated without paying the cold-build cost on unrelated commits.

## Workspace mounts and project paths

Set `CODEX_WORKSPACE` to the host directory that contains the repositories Codex should be allowed to work with:

```bash
CODEX_WORKSPACE=/srv/development docker compose up -d
```

Inside the container that directory is `/workspace`. Compose sets `CODEX_WEB_WORKSPACE_ROOT=/workspace`, so project records are stored as portable paths relative to that root. For example, a project entered as `product` is persisted as `product` while Codex receives `/workspace/product` as its runtime working directory. Nested paths such as `customer/api` work the same way.

When `CODEX_WORKSPACE` is an absolute host path, Compose also supplies it as `CODEX_WEB_WORKSPACE_SOURCE_ROOT`. Existing project records below that old/native prefix are migrated automatically. A stored `/srv/development/product`, for example, resolves to `/workspace/product` and is rewritten as the portable value `product` on load.

If an older project record contains an absolute path outside both `/workspace` and the configured source root, codex-web rejects it instead of guessing by basename. Set `CODEX_WEB_WORKSPACE_SOURCE_ROOT` to the previous common workspace directory, or update the project record explicitly. This keeps project paths from escaping the mounted workspace into `/app`, `/etc`, or another container filesystem location.

For a native/non-Compose deployment you can opt into the same behavior by setting an absolute `CODEX_WEB_WORKSPACE_ROOT`. If it is unset, codex-web retains the historical native behavior and stores normal absolute project paths.

The container runs as UID/GID 1000 by default. If the mounted workspace uses another owner, set `CODEX_UID` and `CODEX_GID` before building:

```bash
CODEX_UID=$(id -u) CODEX_GID=$(id -g) docker compose build
```

The UID/GID-specific `codex` user is created in the final application layer rather than the shared base image, so these overrides remain local to the deployment.

## Persistent data

Compose creates two named volumes:

- `codex-web-data` -> `/app/data` for the SQLite runtime database plus compatibility JSON/JSONL state.
- `codex-home` -> `/home/codex/.codex` for Codex CLI authentication and configuration.

Runtime state is stored in `/app/data/codex-web.db` using SQLite WAL mode. During the migration window, SQLite-backed state is also mirrored to legacy JSON files on every write. This keeps rollback to an earlier release safe while SQLite is introduced incrementally.

Back up the entire `codex-web-data` volume before destructive upgrades or storage migrations. A consistent backup should include `codex-web.db` together with its `-wal`/`-shm` files when the service is running, or be taken while the service is stopped.

## Secrets and webhooks

Copy `.env.example` to `.env` and populate only the integrations in use. Slack, Telegram, and GitLab webhook verification fail closed: an inbound request to an enabled webhook route without a configured verification secret receives HTTP 503.

Do not bake secrets into the image. For production, inject them through the orchestrator, a secrets manager, or an environment file with restricted permissions.

## Reproducible runtime versions

The repository separates human-maintained compatibility constraints from the exact runtime set:

- `requirements.txt` declares supported top-level Python dependency ranges.
- `requirements.lock` contains the complete Python runtime versions validated by CI and installed into the published base.
- `.env.example` declares the Codex CLI version expected by the application image and CI.

The final Dockerfile checks that `CODEX_VERSION` matches the Codex CLI already present in the selected base. It does not silently install another version. A mismatch fails the build explicitly.

To test a new Codex/runtime foundation locally before it is published:

```bash
docker build \
  -f Dockerfile.base \
  --build-arg CODEX_VERSION=<version> \
  -t codex-web-base:test \
  .

docker build \
  --build-arg BASE_IMAGE=codex-web-base:test \
  --build-arg CODEX_VERSION=<version> \
  -t codex-web:test \
  .
```

CI performs the equivalent cold candidate build automatically when runtime-base inputs change. After such a change is merged, the trusted `main` workflow publishes the new base tags to Docker Hub.

For a reproducible local Python environment use:

```bash
python -m pip install -r requirements.lock
```

## Direct Docker usage

```bash
docker build -t codex-web .
docker run --rm \
  -p 127.0.0.1:8765:8765 \
  -e CODEX_WEB_WORKSPACE_ROOT=/workspace \
  -v codex-web-data:/app/data \
  -v codex-home:/home/codex/.codex \
  -v "$PWD/workspace:/workspace" \
  codex-web
```

The container deliberately does not mount the Docker socket, SSH keys, or arbitrary host directories. Add only the credentials and mounts required for the repositories and integrations you intend Codex to access.

## Rootless Podman

Rootless Podman may run the Codex Web control plane, but the built-in local
Bubblewrap execution worker is currently **not supported inside a rootless
Podman container**. Qualification found that nested Bubblewrap cannot establish
its required `/proc`/namespace boundary reliably under the tested rootless
Podman profiles.

See [docs/operations/rootless-podman.md](docs/operations/rootless-podman.md)
for the qualification evidence, readiness behavior, and supported execution
alternatives. Do not use privileged mode or host-level privilege escalation as
an automatic fallback.

