#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:18768}"
EXPECTED_RELEASE_VERSION="${EXPECTED_RELEASE_VERSION:?EXPECTED_RELEASE_VERSION is required}"
EXPECTED_REVISION="${EXPECTED_REVISION:?EXPECTED_REVISION is required}"
MAX_STATUS_SECONDS="${MAX_STATUS_SECONDS:-2.0}"
STATUS_SAMPLES="${STATUS_SAMPLES:-20}"
EVIDENCE_DIR="${EVIDENCE_DIR:-}"

stage="initialization"

record_stage() {
  stage="$1"
  if [ -n "$EVIDENCE_DIR" ]; then
    mkdir -p "$EVIDENCE_DIR"
    printf '%s\n' "$stage" > "$EVIDENCE_DIR/qualification-stage.txt"
  fi
  printf 'qualification stage: %s\n' "$stage" >&2
}

fail() {
  printf 'recovery qualification failed at stage: %s\n' "$stage" >&2
  if [ -n "$EVIDENCE_DIR" ]; then
    printf '%s\n' "$stage" > "$EVIDENCE_DIR/qualification-failed-stage.txt"
  fi
  exit 1
}

wait_endpoint() {
  local path="$1"
  local name="$2"
  record_stage "wait-$name"
  for _ in $(seq 1 90); do
    if curl --fail --silent "$BASE_URL$path" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'endpoint did not become ready: %s\n' "$path" >&2
  fail
}

capture_json() {
  local name="$1"
  local path="$2"
  local output
  record_stage "fetch-$name"
  if ! output="$(curl --fail --silent --show-error "$BASE_URL$path")"; then
    fail
  fi
  if [ -n "$EVIDENCE_DIR" ]; then
    printf '%s\n' "$output" > "$EVIDENCE_DIR/$name.json"
  fi
  printf '%s' "$output"
}

wait_endpoint /api/livez livez
wait_endpoint /api/readyz readyz

version_json="$(capture_json version /api/version)"
record_stage "verify-version"
if ! jq -e   --arg version "$EXPECTED_RELEASE_VERSION"   --arg revision "$EXPECTED_REVISION"   '.releaseVersion == $version and .gitRevision == $revision'   <<<"$version_json" >/dev/null; then
  jq -c     '{releaseVersion, gitRevision, staticVersion}'     <<<"$version_json" >&2 || true
  fail
fi

live_json="$(capture_json livez /api/livez)"
record_stage "verify-livez"
if ! jq -e   --arg version "$EXPECTED_RELEASE_VERSION"   --arg revision "$EXPECTED_REVISION"   '.ok == true and .build.releaseVersion == $version and .build.gitRevision == $revision'   <<<"$live_json" >/dev/null; then
  jq -c '{ok, status, build, version}' <<<"$live_json" >&2 || true
  fail
fi

ready_json="$(capture_json readyz /api/readyz)"
record_stage "verify-readyz"
if ! jq -e '.ok == true and .status == "ready"' <<<"$ready_json" >/dev/null; then
  jq -c     '{
      ok,
      status,
      stateStore,
      runtime: {
        ok: .runtime.ok,
        problems: .runtime.problems,
        healthCache: .runtime.healthCache
      }
    }'     <<<"$ready_json" >&2 || true
  fail
fi

record_stage "verify-browser"
curl --fail --silent --show-error "$BASE_URL/" >/dev/null || fail

record_stage "verify-static-app"
curl --fail --silent --show-error "$BASE_URL/static/app.js" >/dev/null || fail

record_stage "verify-status-latency"
latencies=()
for sample in $(seq 1 "$STATUS_SAMPLES"); do
  latency="$(
    curl --fail --silent --show-error       --output /tmp/codex-web-release-status.json       --write-out '%{time_total}'       "$BASE_URL/api/status"
  )" || fail
  latencies+=("$latency")
  if ! awk     -v value="$latency"     -v maximum="$MAX_STATUS_SECONDS"     'BEGIN { exit !(value <= maximum) }'; then
    printf 'status sample %s exceeded bound: %ss > %ss\n'       "$sample" "$latency" "$MAX_STATUS_SECONDS" >&2
    if [ -n "$EVIDENCE_DIR" ]; then
      cp /tmp/codex-web-release-status.json         "$EVIDENCE_DIR/status-failed.json" || true
    fi
    fail
  fi
done

max_latency="$(printf '%s\n' "${latencies[@]}" | sort -n | tail -n1)"
record_stage "qualified"
jq -nc   --arg release_version "$EXPECTED_RELEASE_VERSION"   --arg revision "$EXPECTED_REVISION"   --argjson samples "$STATUS_SAMPLES"   --arg max_latency_seconds "$max_latency"   --arg max_allowed_seconds "$MAX_STATUS_SECONDS"   '{
    status: "qualified",
    release_version: $release_version,
    revision: $revision,
    nginx: true,
    livez: true,
    readyz: true,
    static_assets: true,
    api_routing: true,
    status_latency: {
      samples: $samples,
      max_seconds: ($max_latency_seconds | tonumber),
      allowed_seconds: ($max_allowed_seconds | tonumber)
    }
  }'
