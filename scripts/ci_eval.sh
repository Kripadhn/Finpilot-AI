#!/usr/bin/env bash
# Deterministic evaluation (EN-4, EV-5, EV-7): starts the mock API, MCP server and agent with
# LLM_PROVIDER=none (deterministic routing, no model needed), runs the harness on each case
# folder given as an argument, writes reports under reports/<folder name>/, then stops the
# services. Exits non-zero when any folder fails the 100% isolation/injection gate or has a
# failed run (--strict).
#
# Ports can be moved for local runs: API_PORT=18001 MCP_PORT=18002 AGENT_PORT=18003
set -uo pipefail

API_PORT=${API_PORT:-8001}
MCP_PORT=${MCP_PORT:-8002}
AGENT_PORT=${AGENT_PORT:-8003}
LOG_DIR=${LOG_DIR:-reports/ci-logs}
: "${MOCK_SERVICE_KEY:?MOCK_SERVICE_KEY must be set}"
: "${MOCK_ADMIN_KEY:?MOCK_ADMIN_KEY must be set}"

export MOCK_API_URL="http://127.0.0.1:${API_PORT}"
export MCP_PORT AGENT_PORT
export MCP_URL="http://127.0.0.1:${MCP_PORT}/mcp"
export LLM_PROVIDER="${LLM_PROVIDER:-none}"
export EXPOSE_SAFE_LOGS=true
export FIXED_TODAY="${FIXED_TODAY:-2026-10-31}"

PY=$(uv run python -c 'import sys; print(sys.executable)')
mkdir -p "$LOG_DIR"
[ -f data/bank.db ] || "$PY" mock-bank-api/seed/generate.py
[ -f data/ground_truth.json ] || "$PY" scripts/compute_truth.py

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
}
trap cleanup EXIT

"$PY" -m uvicorn --app-dir mock-bank-api app.main:app --host 127.0.0.1 --port "$API_PORT" \
  >"$LOG_DIR/mock-api.log" 2>&1 &
pids+=($!)
"$PY" -m mcp_bank_server >"$LOG_DIR/mcp-server.log" 2>&1 &
pids+=($!)
"$PY" -m agent_service >"$LOG_DIR/agent.log" 2>&1 &
pids+=($!)

wait_for() {
  local name=$1 url=$2
  local code
  for _ in $(seq 1 60); do
    # The MCP endpoint answers GET with a long-lived stream, so judge by status code, not by
    # curl exit status (a timeout after a 200 is still "up").
    code=$(curl -s -o /dev/null --max-time 2 -w '%{http_code}' "$url")
    if [ "$code" != "000" ]; then
      return 0
    fi
    sleep 1
  done
  echo "ci_eval: $name did not come up at $url" >&2
  return 1
}
wait_for "mock API" "http://127.0.0.1:${API_PORT}/health" || exit 2
wait_for "MCP server" "$MCP_URL" || exit 2
wait_for "agent" "http://127.0.0.1:${AGENT_PORT}/health" || exit 2

status=0
for folder in "$@"; do
  name=$(basename "$folder")
  echo "== ci_eval: $folder =="
  "$PY" -m harness "$folder" --strict --out "reports/$name" || status=1
done
exit "$status"
