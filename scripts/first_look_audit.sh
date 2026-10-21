#!/usr/bin/env bash
# One command that produces the first-use audit: screenshots, visible text and
# the executable assertions behind them.
#
# It brings up its own read-only API on a private port, seeded with real
# historical cases copied into a temporary approvals root. It never writes to
# .runtime/*-cases, never starts the ERP sandbox, and stops everything it
# started. Output lands in .runtime/first-look-audit/ (override with $1).
#
# Usage:
#   scripts/first_look_audit.sh [outdir]
#
# Exit code is non-zero if any assertion fails.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/.runtime/first-look-audit}"
PORT="${FORGEFLOW_AUDIT_PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"
INVITE="first-look-audit-invite"
SECRET="first-look-audit-secret-not-a-real-key-0123456789"
BASE="http://127.0.0.1:$PORT"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/forgeflow-first-look.XXXXXX")"
CASES="$WORK/cases"
mkdir -p "$CASES/product-cases" "$OUT/screens"

cleanup() {
  # The API runs under `uv run`, so killing the subshell alone leaves it alive
  # and holding the port for the next run. Kill both, then verify the port.
  if [[ -n "${API_PID:-}" ]]; then kill "$API_PID" 2>/dev/null || true; fi
  pkill -f -- "forgeflow api --host 127.0.0.1 --port $PORT" 2>/dev/null || true
  sleep 0.5
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "first-look audit"
echo "  workspace: $ROOT"
echo "  output:    $OUT"

# --- seed real historical cases ------------------------------------------------
for source in ".runtime/demo-approvals" ".runtime/v10-revision-cases-b-r1"; do
  if [[ -d "$ROOT/$source/product-cases" ]]; then
    cp -R "$ROOT/$source/product-cases/." "$CASES/product-cases/" 2>/dev/null || true
    echo "  seeded from $source"
  fi
done
if [[ -z "$(ls -A "$CASES/product-cases" 2>/dev/null)" ]]; then
  echo "  note: no historical cases found; the audit runs against an empty workspace"
fi

# --- start the read-only API --------------------------------------------------
(
  cd "$ROOT"
  FORGEFLOW_APPROVALS_ROOT="$CASES" \
  FORGEFLOW_DATA_ROOT="$WORK/runs" \
  FORGEFLOW_SESSION_SECRET="$SECRET" \
  FORGEFLOW_INVITE_CODES="$INVITE" \
  FORGEFLOW_DEPLOYMENT=local \
  FORGEFLOW_SECURE_COOKIE=0 \
  uv run forgeflow api --host 127.0.0.1 --port "$PORT" >"$WORK/api.log" 2>&1
) &
API_PID=$!
disown "$API_PID" 2>/dev/null || true   # keep the shell from reporting the killed job

if python3 -c "import socket,sys;s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sys.exit(0 if s.connect_ex((\"127.0.0.1\",$PORT))==0 else 1)"; then
  echo "  FAIL port $PORT is already in use; refusing to audit against an unknown server"
  exit 1
fi
for _ in $(seq 1 60); do
  if curl -s -o /dev/null --max-time 1 "$BASE/"; then break; fi
  sleep 0.5
done
if ! curl -s -o /dev/null --max-time 2 "$BASE/"; then
  echo "  FAIL the audit API did not start; log follows"
  tail -5 "$WORK/api.log" || true
  exit 1
fi
echo "  api:       $BASE (read-only, isolated approvals root)"

# The copied cases belong to another principal; hand them to this session.
SESSION_UID="$(curl -s -X POST "$BASE/api/auth/session" -H 'content-type: application/json' \
  -d "{\"invite_code\":\"$INVITE\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["uid"])')"
python3 - "$CASES" "$SESSION_UID" <<'PY'
import json, pathlib, sys
root, uid = pathlib.Path(sys.argv[1]), sys.argv[2]
for path in root.glob("product-cases/*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["uid"] = uid
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
PY

# --- run the audits -----------------------------------------------------------
cd "$ROOT/web"
status=0
declare -a PASSED=() FAILED=()
run() {
  local label="$1"; shift
  echo "  --- $label"
  set +e
  "$@" >"$OUT/$label.log" 2>&1
  local code=$?
  set -e
  tail -3 "$OUT/$label.log"
  if [[ $code -eq 0 ]]; then
    echo "  ok   $label"
    PASSED+=("$label")
  else
    echo "  FAIL $label (exit $code)"
    FAILED+=("$label")
    status=1
  fi
}

export FORGEFLOW_BASE="$BASE"
export FORGEFLOW_BASE_URL="$BASE"   # a11y reads this name
export FORGEFLOW_INVITE="$INVITE"
export FORGEFLOW_OUT="$OUT"
export CAPTURE_TAG=audit

run capture-copy-context node e2e/v13-copy-capture.mjs || true
run capture-narrow node e2e/narrow-state.mjs || true
run verify-approval-gate node e2e/verify-approval-gate.mjs
run verify-success-narrative node e2e/verify-success-narrative.mjs
run verify-copy-context node e2e/verify-copy-context.mjs
run verify-start-page node e2e/verify-start-page.mjs
run verify-vocabulary node e2e/verify-vocabulary.mjs
run verify-bundle-parity node e2e/verify-bundle-parity.mjs
run verify-stage-b node e2e/verify-stage-b.mjs
run check-web-parity python3 ../scripts/check_web_parity.py
run a11y node e2e/a11y.mjs

# --- summarise ----------------------------------------------------------------
python3 - "$OUT" "$PORT" "${PASSED[*]:-}" "${FAILED[*]:-}" <<'PYSUM'
import json, pathlib, sys, time
out, port, passed, failed = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3].split(), sys.argv[4].split()
summary = {
    "port": port,
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "passed": passed,
    "failed": failed,
    "screens": sorted(path.name for path in (out / "screens").glob("*.png")),
}
(out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
print(json.dumps({"passed": passed, "failed": failed, "screens": len(summary["screens"])}, indent=1))
PYSUM

echo "  evidence:  $OUT"
if [[ $status -ne 0 ]]; then
  echo "first-look audit: FAILED"
  exit $status
fi
echo "first-look audit: ok"
