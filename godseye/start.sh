#!/usr/bin/env bash
# start.sh — single startup for the Godseye UAV ISR sim.
#
# ONE Python process (godseye_uav.app --headless) runs the sim backend (fake
# or real), the MCP server, the telemetry bridge, the intel graph, the AI
# analyst and the built console UI, all on the bridge port (:8790). The same
# app is ALSO served on the MCP port (:8791), so http://127.0.0.1:8791/mcp and
# every other existing URL keep working. We add the Gods-Eye-View dev UI
# (vite, :4173) for UI development.
#
# godseye_uav.launch (the older three-listener launcher) is unchanged and still
# used by the tests; this script no longer starts it.
#
# For the full PLAN §8.1 experience — boot, wait for readiness, open the
# browser and fly the scripted recon mission — use ./scripts/demo_laptop.sh,
# which drives this script.
#
# Usage:
#   ./start.sh                       # fake sim (no Unreal needed)
#   SIM_BACKEND=real ./start.sh      # real AirSim/Unreal on AIRSIM_PORT
#   THEATER=ukraine-donbas ./start.sh
#
# Env overrides: THEATER SIM_BACKEND AIRSIM_PORT BRIDGE_PORT MCP_PORT UI_PORT TOKEN
#                STORE (default: godseye/.godseye/store, absolute)
#
# THEATER ids come from godseye_uav/theaters.py — the single source of truth
# for home, AO and demo geometry. This script does not keep its own list; an
# unknown id is rejected here, before anything binds a port. With THEATER
# unset, --theater is not passed: the app boots the theater persisted in
# $STORE/theater.json (a theater set from chat survives a restart), else the
# table default (WG §4.1.4).
#
# Map data (geocoding, mapped sites) is on: --geodata on. Real-data hydration
# of the safety loop stays off unless GODSEYE_REAL_DATA says otherwise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEV="$ROOT/../gods-eye-view"

SIM_BACKEND="${SIM_BACKEND:-fake}"        # fake | real
AIRSIM_PORT="${AIRSIM_PORT:-41451}"
BRIDGE_PORT="${BRIDGE_PORT:-8790}"
MCP_PORT="${MCP_PORT:-8791}"
UI_PORT="${UI_PORT:-4173}"
TOKEN="${TOKEN:-dev-token}"
STORE="${STORE:-$ROOT/.godseye/store}"

# AirSim client: override, else sibling checkout, else the pinned copy that
# scripts/setup.sh fetches. One resolver shared with demo_laptop.sh so the two
# cannot drift (they did: setup.sh vendored into .godseye/vendor while these
# scripts looked only at ../airsim, so a fresh clone installed it and then
# could not find it).
GS_ROOT="$ROOT"
# shellcheck source=scripts/_airsim_client.sh
. "$ROOT/scripts/_airsim_client.sh"
if ! resolve_airsim_client; then
    airsim_missing_help
    exit 1
fi
echo "[start] airsim client: $AIRSIM_CLIENT"
export PYTHONPATH="$ROOT/mcp:$AIRSIM_CLIENT${PYTHONPATH:+:$PYTHONPATH}"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
echo "[start] python: $PY"

# Theater: validation comes from the table, not from here. Unset = let the app
# restore the persisted theater (else the table default); only a THEATER you
# set is passed as --theater.
THEATER="${THEATER:-}"
THEATER_ARGS=()
if [ -n "$THEATER" ]; then THEATER_ARGS=(--theater "$THEATER"); fi
if ! "$PY" - "$THEATER" <<'PYTHEATER'
import sys
from godseye_uav import theaters
# Self-check the table before anything binds a port. Only demo_laptop.sh used
# to do this, so `./start.sh` on its own would happily boot on an internally
# inconsistent table (home outside its own AO, a geofence-rejected demo box, a
# POI whose orbit ring leaves the AO) and only fail mid-mission.
problems = theaters.validate()
if problems:
    print("[start] FATAL: theater table is INCONSISTENT:", file=sys.stderr)
    for p in problems:
        print("   " + p, file=sys.stderr)
    sys.exit(1)
if not sys.argv[1]:
    print(f"[start] theater: the one persisted in the store, else "
          f"{theaters.DEFAULT_THEATER_ID} (set THEATER to choose)")
    sys.exit(0)
try:
    t = theaters.get(sys.argv[1])
except KeyError as exc:
    print(f"[start] FATAL: {exc}", file=sys.stderr)
    sys.exit(1)
print(f"[start] theater {t.id}: {t.label} — {t.place}")
print(f"[start] home {t.home_lat:.5f},{t.home_lon:.5f} @ {t.home_alt_msl_m:.0f} m MSL "
      f"(converted to HAE once, at launch.py ingest — T1)")
PYTHEATER
then
  exit 1
fi

# Job control so each background job becomes its own process group: killing
# the group takes the whole tree down. Without it `npm run dev` is killed but
# the vite/node process it spawned keeps the UI port bound, and the next run
# dies on "port already in use".
set -m

PIDS=()
cleanup() {
  echo ""
  echo "[start] shutting down…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] || continue
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

if [ "$SIM_BACKEND" = "real" ]; then
  echo "[start] SIM_BACKEND=real → connect to AirSim/Unreal on :$AIRSIM_PORT"
  echo "[start] (launch Unreal/AirSim yourself first)"
  REAL_FLAG="--real"
else
  echo "[start] SIM_BACKEND=fake → FakeAirSim on :$AIRSIM_PORT"
  REAL_FLAG=""
fi

# godseye_uav.app starts sim + MCP + bridge + intel + analyst in one process,
# serving the same app on the bridge port and the MCP port.
#
# The token goes in through the environment of that one process, NOT on its
# command line: argv is readable by every local user (`ps`), a process's
# environment only by its owner. app.py reads GODSEYE_TOKEN and removes it
# from its own environment before anything (the analyst's CLI) is spawned.
THEATER_SHOWN="${THEATER:-persisted, else default}"
echo "[start] sim+MCP+bridge: theater=$THEATER_SHOWN sim=:$AIRSIM_PORT mcp=:$MCP_PORT bridge=:$BRIDGE_PORT"
GODSEYE_TOKEN="$TOKEN" "$PY" -m godseye_uav.app --headless \
  ${THEATER_ARGS[@]+"${THEATER_ARGS[@]}"} \
  --geodata on \
  --sim-port "$AIRSIM_PORT" \
  --port "$BRIDGE_PORT" \
  --mcp-port "$MCP_PORT" \
  --store "$STORE" \
  $REAL_FLAG &
PIDS+=($!)

# Gods-Eye-View UI
if [ -d "$GEV" ]; then
  echo "[start] Gods-Eye-View UI on :$UI_PORT"
  ( cd "$GEV" && npm run dev -- --port "$UI_PORT" --strictPort ) &
  PIDS+=($!)
else
  echo "[start] WARNING: gods-eye-view not found at $GEV — UI skipped"
fi

echo ""
echo "[start] up:"
echo "        sim      : $SIM_BACKEND (127.0.0.1:$AIRSIM_PORT)"
echo "        MCP      : http://127.0.0.1:$MCP_PORT/mcp"
echo "        bridge   : http://127.0.0.1:$BRIDGE_PORT/snapshot"
echo "        UI       : http://localhost:$UI_PORT"
echo "        console  : http://127.0.0.1:$BRIDGE_PORT/  (built UI, same origin; needs 'npm run build')"
echo "        theater  : $THEATER_SHOWN"
echo "        store    : $STORE"
echo ""
# Only the well-known dev default is echoed; a token you chose stays out of
# the terminal and of any log this output is redirected into.
if [ "$TOKEN" = "dev-token" ]; then TOKEN_SHOWN="dev-token"; else TOKEN_SHOWN='"$TOKEN"'; fi
# The demo flies a table theater: name it only when THEATER was given (it
# checks the server's theater and says so if they differ).
DEMO_THEATER=""
if [ -n "$THEATER" ]; then DEMO_THEATER=" --theater $THEATER"; fi
echo "[start] fly the scripted demo against this stack:"
echo "        $PY $ROOT/scripts/demo_mission.py --mcp-url http://127.0.0.1:$MCP_PORT/mcp --token $TOKEN_SHOWN$DEMO_THEATER"
echo ""
echo "[start] Ctrl-C to stop everything."

wait
