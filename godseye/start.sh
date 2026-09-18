#!/usr/bin/env bash
# start.sh — single startup for the Godseye UAV ISR sim.
#
# launch.py already starts: sim backend (fake or real), MCP server (:8791),
# and the telemetry bridge (:8790). We only add the Gods-Eye-View UI (vite).
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
#
# THEATER ids come from godseye_uav/theaters.py — the single source of truth
# for home, AO and demo geometry. This script does not keep its own list; an
# unknown id is rejected here, before anything binds a port.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEV="$ROOT/../gods-eye-view"

SIM_BACKEND="${SIM_BACKEND:-fake}"        # fake | real
AIRSIM_PORT="${AIRSIM_PORT:-41451}"
BRIDGE_PORT="${BRIDGE_PORT:-8790}"
MCP_PORT="${MCP_PORT:-8791}"
UI_PORT="${UI_PORT:-4173}"
TOKEN="${TOKEN:-dev-token}"

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

# Theater: default and validation both come from the table, not from here.
THEATER="${THEATER:-$("$PY" -c 'from godseye_uav import theaters; print(theaters.DEFAULT_THEATER_ID)')}"
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

# launch.py starts sim + MCP server + telemetry bridge in one process
echo "[start] sim+MCP+bridge: theater=$THEATER sim=:$AIRSIM_PORT mcp=:$MCP_PORT bridge=:$BRIDGE_PORT"
"$PY" -m godseye_uav.launch \
  --theater "$THEATER" \
  --sim-port "$AIRSIM_PORT" \
  --mcp-port "$MCP_PORT" \
  --bridge-port "$BRIDGE_PORT" \
  --token "$TOKEN" \
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
echo "        theater  : $THEATER"
echo ""
echo "[start] fly the scripted demo against this stack:"
echo "        $PY $ROOT/scripts/demo_mission.py --mcp-url http://127.0.0.1:$MCP_PORT/mcp --token $TOKEN --theater $THEATER"
echo ""
echo "[start] Ctrl-C to stop everything."

wait
