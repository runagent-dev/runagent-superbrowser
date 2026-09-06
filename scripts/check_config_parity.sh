#!/usr/bin/env bash
# Guard the duplicated config data tables:
#   nanobot/superbrowser_config/presets.json  <->  src/config/presets.ts  (marker-wrapped)
#   nanobot/superbrowser_config/envmap.json   <->  src/config/envmap.ts   (marker-wrapped)
# Same discipline as scripts/check_deploy_sync.sh: the copies must stay
# semantically identical (compared as canonical JSON) or CI fails.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail=0

canon() {
  python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))'
}

extract_ts_json() { # file start_marker end_marker
  awk -v start="$2" -v end="$3" '
    index($0, end) { grab = 0 }
    grab { print }
    index($0, start) { grab = 1 }
  ' "$1"
}

check_pair() { # json_file ts_file start_marker end_marker label
  local json_file="$1" ts_file="$2" start="$3" end="$4" label="$5"
  local left right
  if ! left="$(canon <"$json_file")"; then
    echo "FAIL: $json_file is not valid JSON"
    fail=1
    return
  fi
  if ! right="$(extract_ts_json "$ts_file" "$start" "$end" | canon)"; then
    echo "FAIL: could not extract/parse JSON between $start/$end in $ts_file"
    fail=1
    return
  fi
  if [[ "$left" != "$right" ]]; then
    echo "FAIL: $label drift — $json_file != $ts_file"
    echo "  Fix: edit both copies together (the JSON file is canonical)."
    fail=1
  else
    echo "OK: $label in sync ($json_file <-> $ts_file)"
  fi
}

check_pair \
  nanobot/superbrowser_config/presets.json \
  src/config/presets.ts \
  PRESETS_JSON_START PRESETS_JSON_END \
  "machine-profile presets"

check_pair \
  nanobot/superbrowser_config/envmap.json \
  src/config/envmap.ts \
  ENVMAP_JSON_START ENVMAP_JSON_END \
  "env projection map"

exit "$fail"
