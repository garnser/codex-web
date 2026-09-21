#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:18768}"
EXPECTED_RELEASE_VERSION="${EXPECTED_RELEASE_VERSION:?EXPECTED_RELEASE_VERSION is required}"
EXPECTED_REVISION="${EXPECTED_REVISION:?EXPECTED_REVISION is required}"
MAX_STATUS_SECONDS="${MAX_STATUS_SECONDS:-2.0}"
STATUS_SAMPLES="${STATUS_SAMPLES:-20}"

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

wait_endpoint /api/livez
wait_endpoint /api/readyz

version_json="$(curl --fail --silent --show-error "$BASE_URL/api/version")"
jq -e   --arg version "$EXPECTED_RELEASE_VERSION"   --arg revision "$EXPECTED_REVISION"   '.releaseVersion == $version and .gitRevision == $revision'   <<<"$version_json" >/dev/null

live_json="$(curl --fail --silent --show-error "$BASE_URL/api/livez")"
jq -e   --arg version "$EXPECTED_RELEASE_VERSION"   --arg revision "$EXPECTED_REVISION"   '.ok == true and .build.releaseVersion == $version and .build.gitRevision == $revision'   <<<"$live_json" >/dev/null

ready_json="$(curl --fail --silent --show-error "$BASE_URL/api/readyz")"
jq -e '.ok == true and .status == "ready"' <<<"$ready_json" >/dev/null

curl --fail --silent --show-error "$BASE_URL/" >/dev/null
curl --fail --silent --show-error "$BASE_URL/static/app.js" >/dev/null

latencies=()
for _ in $(seq 1 "$STATUS_SAMPLES"); do
  latency="$(curl --fail --silent --show-error     --output /tmp/codex-web-release-status.json     --write-out '%{time_total}'     "$BASE_URL/api/status")"
  latencies+=("$latency")
  awk     -v value="$latency"     -v maximum="$MAX_STATUS_SECONDS"     'BEGIN { exit !(value <= maximum) }'
done

max_latency="$(printf '%s
' "${latencies[@]}" | sort -n | tail -n1)"
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
