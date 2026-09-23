#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:18767}"
MODE="${1:-qualify}"

json_get() {
  curl --fail --silent --show-error "$1"
}

json_post() {
  local url="$1"
  local body="${2-}"
  if [[ -z "$body" ]]; then
    body='{}'
  fi
  curl --fail-with-body --silent --show-error \
    -H 'Content-Type: application/json' \
    -X POST \
    --data "$body" \
    "$url"
}

ensure_codex_credential() {
  local project_id="$1"
  local secret_id draft_id existing_reference

  secret_id="$(json_get "$BASE_URL/api/secrets" | jq -r '
    [.items[] | select(.name == "CI onboarding Codex access token" and .status == "active")][0].id // empty
  ')"
  if [[ -z "$secret_id" ]]; then
    secret_id="$(json_post "$BASE_URL/api/secrets" '{
      "name": "CI onboarding Codex access token",
      "value": "ci-qualification-token-not-a-real-credential",
      "provider": "openai",
      "purpose": "clean-install readiness qualification"
    }' | jq -r '.item.id')"
  fi

  existing_reference="$(json_post "$BASE_URL/api/configuration/resolve" "$(jq -nc \
      --arg project_id "$project_id" \
      '{
        key:"codex.worker.access_token_secret",
        context:{project_id:$project_id}
      }')" 2>/dev/null || true)"
  if jq -e '.value.kind == "secret" and (.value.secret_id | type == "string" and length > 0)' \
      <<<"$existing_reference" >/dev/null 2>&1; then
    return 0
  fi

  draft_id="$(json_post "$BASE_URL/api/configuration/drafts" "$(jq -nc \
      --arg project_id "$project_id" \
      --arg secret_id "$secret_id" \
      '{
        key:"codex.worker.access_token_secret",
        scope_type:"project",
        scope_id:$project_id,
        value:{kind:"secret",secret_id:$secret_id},
        reason:"clean-install readiness qualification"
      }')" | jq -r '.record.id')"
  json_post "$BASE_URL/api/configuration/$draft_id/publish" \
    '{"reason":"clean-install readiness qualification"}' >/dev/null
}

assert_credential_blocker() {
  local project_id="$1"
  local readiness
  readiness="$(json_get "$BASE_URL/api/projects/$project_id/readiness")"
  jq -e '
    .execution_ready == false
    and ([.checks[]
      | select(.status == "blocked")
      | .code
    ] | any(. == "credential_reference_missing"))
  ' <<<"$readiness" >/dev/null
}

wait_endpoint() {
  local path="$1"
  for _ in $(seq 1 90); do
    if curl --fail --silent "$BASE_URL$path" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "endpoint did not become ready: $path" >&2
  return 1
}

project_by_name() {
  local name="$1"
  json_get "$BASE_URL/api/projects" |
    jq -c --arg name "$name" '.[] | select(.name == $name)' |
    head -n1
}

assert_project_ready() {
  local project_id="$1"
  local readiness
  readiness="$(json_get "$BASE_URL/api/projects/$project_id/readiness")"
  jq -e '.semantic_ready == true and .execution_ready == true'     <<<"$readiness" >/dev/null
}

assert_single_repo() {
  local project_id="$1"
  local resources
  resources="$(json_get "$BASE_URL/api/projects/$project_id/resources")"
  jq -e '
    [.items[]
      | select(.resource_type == "repository" and .lifecycle == "active")
    ] | length == 1
  ' <<<"$resources" >/dev/null
  jq -r '
    [.items[]
      | select(.resource_type == "repository" and .lifecycle == "active")
    ][0].id
  ' <<<"$resources"
}

register_qualification_worker() {
  local workers existing worker
  workers="$(json_get "$BASE_URL/api/execution-workers")"
  existing="$(jq -c '
    .items[]
    | select(
        .service_identity_id == "execution-worker-local"
        and .pool == "qualification"
        and .lifecycle != "revoked"
      )
  ' <<<"$workers" | head -n1 || true)"
  if [[ -n "$existing" ]]; then
    worker="$(jq -r '.id' <<<"$existing")"
    json_post       "$BASE_URL/api/execution-workers/$worker/activate"       '{}' >/dev/null
    printf '%s\n' "$worker"
    return 0
  fi
  json_post "$BASE_URL/api/execution-workers" '{
    "service_identity_id": "execution-worker-local",
    "pool": "qualification",
    "version": "ci-onboarding-v1",
    "capabilities": ["git", "command_execution", "artifact_upload"],
    "supported_execution_contract_versions": [
      "1.0",
      "thread-turn/1.0",
      "thread-bootstrap/1.0"
    ],
    "max_concurrency": 2
  }' | jq -r '.item.id'
}

create_project() {
  local name="$1"
  local path="$2"
  local sandbox="${3:-workspace-write}"
  json_post "$BASE_URL/api/projects" "$(jq -nc     --arg name "$name"     --arg path "$path"     --arg sandbox "$sandbox"     '{
      name: $name,
      path: $path,
      sandbox: $sandbox,
      approval_policy: "on-request",
      repository_selection_policy: "deterministic"
    }'
  )"
}

if [[ "$MODE" == "verify-restart" ]]; then
  wait_endpoint "/api/livez"
  wait_endpoint "/api/readyz"
  project="$(project_by_name "Onboarding Qualification")"
  test -n "$project"
  project_id="$(jq -r '.id' <<<"$project")"
  assert_project_ready "$project_id"
  repo_id="$(assert_single_repo "$project_id")"
  jq -nc     --arg project_id "$project_id"     --arg repository_id "$repo_id"     '{status:"restart-ready",project_id:$project_id,repository_id:$repository_id}'
  exit 0
fi

wait_endpoint "/api/livez"

# The clean container's built-in Bubblewrap worker can correctly fail its
# isolation probe. Register the test-safe canonical qualification worker
# before asserting application/runtime readiness.
worker_id="$(register_qualification_worker)"
wait_endpoint "/api/readyz"

worker_readiness="$(json_get   "$BASE_URL/api/execution-workers/readiness?required_capability=git&required_capability=command_execution&execution_contract_version=thread-turn%2F1.0")"
jq -e '.execution.ready == true' <<<"$worker_readiness" >/dev/null

existing="$(project_by_name "Onboarding Qualification")"
if [[ -n "$existing" ]]; then
  project="$existing"
else
  project="$(create_project     "Onboarding Qualification"     "/workspace/onboarding-single")"
fi
project_id="$(jq -r '.id' <<<"$project")"
fresh_status="$(jq -r '.freshBootstrap.status // empty' <<<"$project")"
if [[ -n "$fresh_status" ]]; then
  test "$fresh_status" = "blocked"
fi
assert_credential_blocker "$project_id"
ensure_codex_credential "$project_id"
assert_project_ready "$project_id"
repo_before="$(assert_single_repo "$project_id")"

json_post   "$BASE_URL/api/projects/$project_id/fresh-bootstrap"   '{}' >/tmp/onboarding-bootstrap-repeat.json
assert_project_ready "$project_id"
repo_after="$(assert_single_repo "$project_id")"
test "$repo_before" = "$repo_after"

multi="$(create_project   "Onboarding Multi Repository"   "/workspace/onboarding-multi")"
multi_id="$(jq -r '.id' <<<"$multi")"
multi_readiness="$(json_get "$BASE_URL/api/projects/$multi_id/readiness")"
jq -e '.execution_ready == false' <<<"$multi_readiness" >/dev/null
jq -e '
  [.checks[]
    | select(.status == "blocked")
    | .code
  ] | any(. == "repository_target_ambiguous")
' <<<"$multi_readiness" >/dev/null
json_get "$BASE_URL/api/livez" >/dev/null

outside_status="$(
  curl --silent --output /tmp/onboarding-outside.json --write-out '%{http_code}' \
    -H 'Content-Type: application/json' \
    -X POST \
    --data '{
      "name":"Outside Workspace",
      "path":"/etc",
      "sandbox":"workspace-write",
      "approval_policy":"on-request"
    }' \
    "$BASE_URL/api/projects"
)"
test "$outside_status" = "400"

sandbox_status="$(
  curl --silent --output /tmp/onboarding-sandbox.json --write-out '%{http_code}' \
    -H 'Content-Type: application/json' \
    -X POST \
    --data '{
      "name":"Unsupported Sandbox",
      "path":"/workspace/onboarding-single",
      "sandbox":"not-a-sandbox",
      "approval_policy":"on-request"
    }' \
    "$BASE_URL/api/projects"
)"
test "$sandbox_status" = "422"

workers="$(json_get "$BASE_URL/api/execution-workers")"
mapfile -t active_workers < <(
  jq -r '.items[] | select(.lifecycle == "active") | .id' <<<"$workers"
)
for id in "${active_workers[@]}"; do
  json_post     "$BASE_URL/api/execution-workers/$id/quarantine"     '{"reason":"clean-install negative qualification"}' >/dev/null
done

no_worker="$(create_project   "Onboarding Missing Worker"   "/workspace/onboarding-no-worker")"
no_worker_id="$(jq -r '.id' <<<"$no_worker")"
no_worker_readiness="$(json_get   "$BASE_URL/api/projects/$no_worker_id/readiness")"
jq -e '.execution_ready == false' <<<"$no_worker_readiness" >/dev/null
jq -e '
  [.checks[]
    | select(.status == "blocked")
    | .code
  ] | any(
      . == "worker_capability_missing"
      or . == "worker_unavailable"
    )
' <<<"$no_worker_readiness" >/dev/null

json_post   "$BASE_URL/api/execution-workers/$worker_id/activate"   '{}' >/dev/null
assert_project_ready "$project_id"

jq -nc   --arg project_id "$project_id"   --arg repository_id "$repo_before"   --arg worker_id "$worker_id"   --arg multi_project_id "$multi_id"   --arg blocked_project_id "$no_worker_id"   '{
    status:"qualified",
    project_id:$project_id,
    repository_id:$repository_id,
    worker_id:$worker_id,
    multi_project_id:$multi_project_id,
    blocked_project_id:$blocked_project_id
  }'
