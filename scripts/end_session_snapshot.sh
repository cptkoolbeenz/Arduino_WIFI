#!/usr/bin/env bash
set -euo pipefail

# Create a timestamped session handoff snapshot in docs/handoffs.
# Run from anywhere inside the repo.

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$ROOT_DIR" ]]; then
  echo "Error: not inside a git repository." >&2
  exit 1
fi

cd "$ROOT_DIR"

STAMP="$(date +"%Y-%m-%d_%H%M%S")"
OUT="docs/handoffs/session_${STAMP}.md"

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
HEAD_HASH="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
PUSH_STATUS="no"
if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
  LOCAL_HEAD="$(git rev-parse @ 2>/dev/null || true)"
  UPSTREAM_HEAD="$(git rev-parse @{u} 2>/dev/null || true)"
  if [[ -n "$LOCAL_HEAD" && -n "$UPSTREAM_HEAD" && "$LOCAL_HEAD" == "$UPSTREAM_HEAD" ]]; then
    PUSH_STATUS="yes"
  fi
fi

ACTIVE_PROFILE="unknown"
if [[ -f config/network_profile.json ]]; then
  ACTIVE_PROFILE="$(python3 - <<'PY'
import json
from pathlib import Path
p = Path('config/network_profile.json')
try:
    data = json.loads(p.read_text())
    print(data.get('active_profile', 'unknown'))
except Exception:
    print('unknown')
PY
)"
fi

NOW_ISO="$(date +"%Y-%m-%d %H:%M:%S %Z")"

{
  echo "# Session Handoff - ${STAMP}"
  echo
  echo "## 1) Session Metadata"
  echo "- Date: ${NOW_ISO}"
  echo "- Operator: ${USER:-unknown}"
  echo "- Branch: ${BRANCH}"
  echo "- Latest commit hash: ${HEAD_HASH}"
  echo "- Pushed to remote: ${PUSH_STATUS}"
  echo
  echo "## 2) Environment"
  echo "- Active network profile (config/network_profile.json): ${ACTIVE_PROFILE}"
  echo
  echo "## 3) Git Status"
  echo '```bash'
  git status --short --branch || true
  echo '```'
  echo
  echo "## 4) Recent Commits"
  echo '```bash'
  git log --oneline --decorate -n 10 || true
  echo '```'
  echo
  echo "## 5) Changed Files Since HEAD"
  echo '```bash'
  git diff --name-status || true
  echo '```'
  echo
  echo "## 6) Running Processes (best effort)"
  echo '```bash'
  ps -ef | rg "bsm_network.py|bsm_web.py" || true
  echo '```'
  echo
  echo "## 7) Next Actions"
  echo "1. "
  echo "2. "
  echo "3. "
  echo
  echo "## 8) Notes"
  echo "- Add any important context from this session here."
} > "$OUT"

echo "Wrote: $OUT"
