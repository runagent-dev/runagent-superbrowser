#!/usr/bin/env bash
# Release checklist: the SuperBrowser deploy manifest is duplicated across repos
# (the runagent serverless image BAKES its own copies and overwrites the upload,
# and the runagent template is downloaded by `runagent init`). The single source
# of truth is the installed package's nanobot/runagent_superbrowser/_nanobot_config.py
# + deploy/main.py; the other copies must stay byte-identical. This script fails
# if they drift.
#
# Sibling repos default to /root/runagent-new/* but can be pointed elsewhere:
#   RUNAGENT_REPO=/path/to/runagent RUNAGENT_SERVERLESS_REPO=/path/to/runagent-serverless \
#     scripts/check_deploy_sync.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

RUNAGENT_REPO="${RUNAGENT_REPO:-/root/runagent-new/runagent}"
RUNAGENT_SERVERLESS_REPO="${RUNAGENT_SERVERLESS_REPO:-/root/runagent-new/runagent-serverless}"

SB=nanobot/runagent_superbrowser/_nanobot_config.py
DEP=deploy/_nanobot_config.py
MAIN=deploy/main.py
TPL_DIR="$RUNAGENT_REPO/templates/superbrowser/default"
SRV_DIR="$RUNAGENT_SERVERLESS_REPO/scripts"

pass=0; fail=0
chk(){ if eval "$2"; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
skip(){ echo "SKIP  $1 (not found: $2)"; }

echo "== in-repo invariants =="
chk "package bridge == deploy/_nanobot_config.py" "diff -q '$SB' '$DEP' >/dev/null"
chk "deploy/main.py imports bridge package-first"  "grep -q 'from runagent_superbrowser._nanobot_config import ensure_nanobot_config' '$MAIN'"
chk "deploy/main.py has sibling fallback import"   "grep -q 'from _nanobot_config import ensure_nanobot_config' '$MAIN'"
chk "_runtime.py bootstraps nanobot config"        "grep -q 'bootstrap_nanobot_config()' nanobot/runagent_superbrowser/_runtime.py"
chk "deploy config has run + run_stream"           "grep -q '\"run_stream\"' deploy/runagent.config.json && grep -q '\"run\"' deploy/runagent.config.json"
chk "deploy/.gitignore excludes .env"              "grep -qx '.env' deploy/.gitignore"

echo ""
echo "== runagent template ($TPL_DIR) =="
if [ -d "$TPL_DIR" ]; then
  chk "template _nanobot_config.py == package" "diff -q '$SB' '$TPL_DIR/_nanobot_config.py' >/dev/null"
  chk "template main.py == deploy/main.py"     "diff -q '$MAIN' '$TPL_DIR/main.py' >/dev/null"
  chk "template config has run + run_stream"   "grep -q '\"run_stream\"' '$TPL_DIR/runagent.config.json'"
else
  skip "runagent template checks" "$TPL_DIR"
fi

echo ""
echo "== serverless baked copies ($SRV_DIR) =="
if [ -d "$SRV_DIR" ]; then
  chk "serverless _nanobot_config.py == package"        "diff -q '$SB' '$SRV_DIR/_nanobot_config.py' >/dev/null"
  chk "serverless superbrowser-main.py == deploy/main.py" "diff -q '$MAIN' '$SRV_DIR/superbrowser-main.py' >/dev/null"
  chk "serverless config has run + run_stream"          "grep -q '\"run_stream\"' '$SRV_DIR/superbrowser.config.json'"
else
  skip "serverless checks" "$SRV_DIR"
fi

echo ""
echo "================  $pass passed, $fail failed  ================"
[ "$fail" -eq 0 ]
