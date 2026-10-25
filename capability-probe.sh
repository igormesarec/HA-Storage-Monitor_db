#!/usr/bin/env bash
# READ ONLY. Single host disk-usage GET. Token never printed or written to disk.
# Run from Terminal & SSH ONLY with explicit owner approval. Its token may have
# less permission than Core's; failure here is not conclusive for Core.
set -eu
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
  printf '%s\n' 'Supervisor token not available in this terminal; no request sent.'
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  printf '%s\n' 'jq not available; no request sent. Install nothing automatically.'
  exit 2
fi
output="$(printf 'Authorization: Bearer %s\n' "$SUPERVISOR_TOKEN" | curl -sS --max-time 70 --write-out '\nHTTP_CODE=%{http_code}' \
  -H @- \
  'http://supervisor/host/disks/default/usage?max_depth=2')" || {
  printf '%s\n' 'Read-only Supervisor probe failed (timeout or connection).'; exit 1;
}
code="${output##*HTTP_CODE=}"
printf 'Supervisor status: %s\n' "$code"
[ "$code" = 200 ] || { printf '%s\n' 'No authorized disk response. Do not infer Influx size.'; exit 3; }
printf '%s\n' "${output%$'\n'HTTP_CODE=*}" | jq -r '
  (.data // .) as $d |
  [ $d.children[]? | select(.id == "addons_data" or .id == "apps_data") |
    .children[]? | select(.id == "a0d7b954_influxdb") | .used_bytes ] as $found |
  if ($found|length)==1 and ($found[0]|type)=="number" then
    "InfluxDB add-on total measured: \($found[0]) bytes"
  else "InfluxDB add-on exact subtree not available; show Unavailable"
  end'
