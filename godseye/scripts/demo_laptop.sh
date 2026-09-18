#!/usr/bin/env bash
# demo_laptop.sh — THE one-command godSeye demo (PLAN §8.1).
#
#   "starts fake or real AirSim, MCP server, GEV; opens browser; harness skill
#    runs scripted recon mission visible live in command center"
#
# Zero GPU required: the default SIM_BACKEND=fake runs the whole stack — sim,
# MCP server, telemetry bridge, God's-Eye-View — on a laptop CPU.
#
# Usage:
#   ./scripts/demo_laptop.sh                         # fake sim, default theater
#   THEATER=ukraine-donbas ./scripts/demo_laptop.sh
#   SIM_BACKEND=real ./scripts/demo_laptop.sh        # attach to real AirSim/Unreal
#   NO_BROWSER=1 KEEP_UP=1 ./scripts/demo_laptop.sh  # headless, leave stack up
#
# Env overrides (same names start.sh documents, plus four of its own):
#   THEATER SIM_BACKEND AIRSIM_PORT BRIDGE_PORT MCP_PORT UI_PORT TOKEN
#   NO_BROWSER=1     do not open a browser
#   KEEP_UP=1        leave the stack running after the mission finishes
#   READY_TIMEOUT    seconds to wait for each service to answer (default 120)
#   MONITOR_S        seconds to watch the grid fly before the sensor phase.
#                    Unset (the default) = auto-size from the server's own
#                    est_time_s, so the recon leg actually finishes.
#
# THEATER, when unset, comes from godseye_uav.theaters.DEFAULT_THEATER_ID —
# this script keeps no theater knowledge of its own (that is the drift
# theaters.py exists to retire).
#
# Exits non-zero if a prerequisite is missing or a stage never comes up. It
# polls the real health endpoints — it never just sleeps and hopes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GEV="$ROOT/../gods-eye-view"

THEATER="${THEATER:-}"            # empty = ask the theater table (after PY is found)
SIM_BACKEND="${SIM_BACKEND:-fake}"
AIRSIM_PORT="${AIRSIM_PORT:-41451}"
BRIDGE_PORT="${BRIDGE_PORT:-8790}"
MCP_PORT="${MCP_PORT:-8791}"
UI_PORT="${UI_PORT:-4173}"
TOKEN="${TOKEN:-dev-token}"
READY_TIMEOUT="${READY_TIMEOUT:-120}"
# Empty = let demo_mission.py size the window from the server's own plan. A
# fixed 120 s here walked away from a ~346 s recon grid every single run.
MONITOR_S="${MONITOR_S:-}"

LOG_DIR="${LOG_DIR:-$ROOT/.godseye/logs}"
STACK_LOG="$LOG_DIR/stack-$$.log"

say()  { printf '[demo_laptop] %s\n' "$*"; }
die()  { printf '[demo_laptop] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. prerequisites — fail loudly, with the fix, before starting anything
# ---------------------------------------------------------------------------
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] && [ -x "$PY" ] || die "no python found. Expected $ROOT/.venv/bin/python (create it: python3 -m venv .venv && .venv/bin/pip install -e .)"

command -v curl >/dev/null 2>&1 || die "curl is required to poll the health endpoints"
[ -x "$ROOT/start.sh" ] || die "missing $ROOT/start.sh — this script boots the stack through it"
[ -f "$ROOT/scripts/demo_mission.py" ] || die "missing $ROOT/scripts/demo_mission.py — that is the mission driver"

AIRSIM_CLIENT="$ROOT/../airsim/PythonClient"
[ -d "$AIRSIM_CLIENT/airsim" ] || AIRSIM_CLIENT=""
export PYTHONPATH="$ROOT/mcp${AIRSIM_CLIENT:+:$AIRSIM_CLIENT}${PYTHONPATH:+:$PYTHONPATH}"

"$PY" - <<'PYCHECK' || die "python dependencies are missing. Install them: .venv/bin/pip install -e '.[dev]' (and put the AirSim PythonClient on PYTHONPATH)"
import sys
missing = []
for mod in ("airsim", "httpx2", "mcp.client.streamable_http", "godseye_uav.theaters",
            "uvicorn", "fastapi", "pyproj"):
    try:
        __import__(mod)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        missing.append(f"{mod} ({type(exc).__name__}: {exc})")
if missing:
    print("[demo_laptop] cannot import: " + "; ".join(missing), file=sys.stderr)
    sys.exit(1)
PYCHECK

# The theater table is the single source of truth (godseye_uav/theaters.py) —
# including for the DEFAULT id. Hardcoding one here would be a fresh copy of
# exactly the knowledge theaters.py exists to hold.
if [ -z "$THEATER" ]; then
  THEATER="$("$PY" -c 'from godseye_uav import theaters; print(theaters.DEFAULT_THEATER_ID)')" \
    || die "could not read DEFAULT_THEATER_ID from godseye_uav.theaters"
  [ -n "$THEATER" ] || die "godseye_uav.theaters.DEFAULT_THEATER_ID is empty"
fi

# Validate the id and the geometry here rather than discovering a geofence
# rejection halfway through the mission.
"$PY" - "$THEATER" <<'PYTHEATER' || die "theater '$THEATER' is not usable — see the message above"
import sys
from godseye_uav import theaters
problems = theaters.validate()
if problems:
    print("[demo_laptop] theater table is INCONSISTENT:", file=sys.stderr)
    for p in problems:
        print("   " + p, file=sys.stderr)
    sys.exit(1)
try:
    t = theaters.get(sys.argv[1])
except KeyError as exc:
    print(f"[demo_laptop] {exc}", file=sys.stderr)
    sys.exit(1)
print(f"[demo_laptop] theater {t.id}: {t.label} — {t.place}")
print(f"[demo_laptop] AO {t.bounds()}  home {t.home_lat:.5f},{t.home_lon:.5f} "
      f"@ {t.home_alt_msl_m:.0f} m MSL")
PYTHEATER

if [ ! -d "$GEV" ]; then
  say "WARNING: God's-Eye-View not found at $GEV — there will be no command"
  say "         center to watch. The mission still runs and still reports."
fi

mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# 1. boot the stack (start.sh owns sim + MCP + bridge + GEV)
# ---------------------------------------------------------------------------
cleanup() {
  local rc=$?
  if [ -n "${STACK_PID:-}" ] && kill -0 "$STACK_PID" 2>/dev/null; then
    if [ "${KEEP_UP:-0}" = "1" ] && [ "$rc" -eq 0 ]; then
      say "KEEP_UP=1 — leaving the stack running (pid $STACK_PID). Stop it with: kill $STACK_PID"
      return
    fi
    say "shutting the stack down (pid $STACK_PID)…"
    kill "$STACK_PID" 2>/dev/null || true
    wait "$STACK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

say "booting the stack via start.sh (log: $STACK_LOG)"
THEATER="$THEATER" SIM_BACKEND="$SIM_BACKEND" AIRSIM_PORT="$AIRSIM_PORT" \
BRIDGE_PORT="$BRIDGE_PORT" MCP_PORT="$MCP_PORT" UI_PORT="$UI_PORT" TOKEN="$TOKEN" \
  "$ROOT/start.sh" >"$STACK_LOG" 2>&1 &
STACK_PID=$!

# ---------------------------------------------------------------------------
# 2. wait for readiness — poll the real endpoints, never a blind sleep
# ---------------------------------------------------------------------------
stack_alive() {
  kill -0 "$STACK_PID" 2>/dev/null
}

dump_log() {
  say "----- last 40 lines of $STACK_LOG -----"
  tail -40 "$STACK_LOG" >&2 || true
}

poll() {                          # poll <label> <timeout_s> <probe-command...>
  local label="$1" timeout="$2"; shift 2
  local started="$SECONDS" deadline=$(( SECONDS + timeout ))
  printf '[demo_laptop] waiting for %s ' "$label"
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$@" >/dev/null 2>&1; then
      printf ' ready (%ss)\n' "$(( SECONDS - started ))"
      return 0
    fi
    if ! stack_alive; then
      printf ' STACK DIED\n'
      return 2
    fi
    printf '.'
    sleep 1
  done
  printf ' TIMEOUT after %ss\n' "$timeout"
  return 1
}

require_ready() {                 # require_ready <label> <probe-command...>
  local label="$1"; shift
  poll "$label" "$READY_TIMEOUT" "$@" && return 0
  dump_log
  die "$label never came up — the demo cannot run without it"
}

probe_bridge() { curl -fsS "http://127.0.0.1:$BRIDGE_PORT/health"; }

probe_mcp() {
  # A real MCP Streamable-HTTP request: initialize over POST /mcp with the
  # Bearer token. 200 means transport + auth + tool registry are all live.
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "Authorization: Bearer $TOKEN" \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo_laptop","version":"1"}}}' \
    "http://127.0.0.1:$MCP_PORT/mcp" 2>/dev/null)
  [ "$code" = "200" ]
}

probe_ui() { curl -fsS -o /dev/null "http://localhost:$UI_PORT/"; }

require_ready "telemetry bridge :$BRIDGE_PORT/health" probe_bridge
require_ready "MCP server :$MCP_PORT/mcp"            probe_mcp

# The UI is where the operator WATCHES, not what the mission needs, so a slow
# or absent vite is a loud warning rather than a hard stop.
UI_READY=0
if [ -d "$GEV" ]; then
  if poll "God's-Eye-View :$UI_PORT" "${UI_TIMEOUT:-60}" probe_ui; then
    UI_READY=1
  else
    say "WARNING: the God's-Eye-View UI never answered on :$UI_PORT."
    say "         The mission will still run, but nobody can watch it there."
    say "         Check $STACK_LOG (is 'npm install' done in $GEV?)."
  fi
fi

# ---------------------------------------------------------------------------
# 3. open the command center
# ---------------------------------------------------------------------------
if [ "$UI_READY" = "1" ] && [ "${NO_BROWSER:-0}" != "1" ]; then
  if command -v open >/dev/null 2>&1; then
    open "http://localhost:$UI_PORT" || say "could not open a browser; go to http://localhost:$UI_PORT"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:$UI_PORT" >/dev/null 2>&1 || say "could not open a browser; go to http://localhost:$UI_PORT"
  else
    say "no 'open'/'xdg-open' on this machine — go to http://localhost:$UI_PORT"
  fi
fi

echo ""
say "================ WATCH HERE ================"
if [ "$UI_READY" = "1" ]; then
  say "  command center : http://localhost:$UI_PORT   <- the drone flies here"
else
  say "  command center : NOT RUNNING (no GEV) — follow the console below instead"
fi
say "  live telemetry : http://127.0.0.1:$BRIDGE_PORT/snapshot  (Bearer $TOKEN)"
say "  MCP endpoint   : http://127.0.0.1:$MCP_PORT/mcp          (Bearer $TOKEN)"
say "  stack log      : $STACK_LOG"
say "  theater        : $THEATER   sim: $SIM_BACKEND on :$AIRSIM_PORT"
say "============================================"
echo ""

# ---------------------------------------------------------------------------
# 4. run the scripted recon mission over the REAL MCP transport
# ---------------------------------------------------------------------------
say "running the scripted recon mission (MCP Streamable HTTP + Bearer auth)…"
# --monitor-s is passed ONLY when the operator set MONITOR_S. Left out, the
# driver sizes the window from the server's own est_time_s so the grid finishes
# instead of being abandoned a third of the way through.
MONITOR_ARGS=()
if [ -n "$MONITOR_S" ]; then
  MONITOR_ARGS=(--monitor-s "$MONITOR_S")
  say "MONITOR_S=$MONITOR_S — the grid will be cut off if the plan is longer"
fi
set +e
"$PY" "$ROOT/scripts/demo_mission.py" \
  --mcp-url "http://127.0.0.1:$MCP_PORT/mcp" \
  --token "$TOKEN" \
  --theater "$THEATER" \
  ${MONITOR_ARGS[@]+"${MONITOR_ARGS[@]}"} \
  --bridge-url "http://localhost:$UI_PORT"
DEMO_RC=$?
set -e

echo ""
case "$DEMO_RC" in
  0) say "mission complete. Replay/audit log: $ROOT/.godseye/store/" ;;
  3) say "THE STACK IS FINE BUT THE FLIGHT FAILED: the demo drove every step over"
     say "MCP, and the server failed the mission task itself. The reason is printed"
     say "above; the server-side traceback is in $STACK_LOG." ;;
  *) say "DEMO FAILED (exit $DEMO_RC) — see the messages above and $STACK_LOG" ;;
esac
exit "$DEMO_RC"
