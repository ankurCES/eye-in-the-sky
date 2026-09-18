#!/usr/bin/env bash
# setup.sh — make a fresh clone of eye-in-the-sky runnable.
#
# A clone cannot start without this: `import airsim` is a RUNTIME dependency of
# the godSeye package (launch.py, bridge.py, server.py, rpc_patch.py) and the
# AirSim PythonClient is NOT on PyPI in the form this project pins. It is a
# sibling checkout of microsoft/airsim, fetched here at the same commit CI uses.
#
#   ./scripts/setup.sh              # python side + the pinned AirSim client
#   ./scripts/setup.sh --with-ui    # also npm install the God's Eye View UI
#
# Env overrides: GODSEYE_AIRSIM_REPO, GODSEYE_AIRSIM_PIN, PY
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GS="$ROOT/godseye"
GEV="$ROOT/gods-eye-view"

AIRSIM_REPO="${GODSEYE_AIRSIM_REPO:-https://github.com/microsoft/airsim}"
AIRSIM_PIN="${GODSEYE_AIRSIM_PIN:-1ca93f6f77e4e8a39b2b241c1fe2764da4d7dd41}"
AIRSIM_DIR="$GS/.godseye/vendor/airsim"

WITH_UI=0
[ "${1:-}" = "--with-ui" ] && WITH_UI=1

say()  { printf '\n[setup] %s\n' "$*"; }
die()  { printf '\n[setup] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "python"
# ---------------------------------------------------------------------------
PY="${PY:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "no python3 on PATH"
"$PY" - <<'EOF' || die "python >= 3.11 is required (pyproject requires-python)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
EOF
echo "  $("$PY" --version)"

if [ ! -x "$GS/.venv/bin/python" ]; then
    say "creating $GS/.venv"
    "$PY" -m venv "$GS/.venv"
fi
VPY="$GS/.venv/bin/python"

say "installing godseye-uav + dev extras"
"$VPY" -m pip install -q --upgrade pip
# -e so the tests import the working tree, [dev] for pytest/httpx/httpx2/ruff.
"$VPY" -m pip install -q -e "$GS[dev]"

# ---------------------------------------------------------------------------
say "AirSim PythonClient (pinned $AIRSIM_PIN)"
# ---------------------------------------------------------------------------
# tests/conftest.py resolves this relative to the repo and honours
# $GODSEYE_AIRSIM_PYTHONCLIENT; .godseye/vendor is the location it already
# searches, and it is gitignored, so a fetch never dirties the tree.
if [ -f "$AIRSIM_DIR/PythonClient/airsim/__init__.py" ] \
   && [ "$(git -C "$AIRSIM_DIR" rev-parse HEAD 2>/dev/null || echo none)" = "$AIRSIM_PIN" ]; then
    echo "  already vendored at the pin"
else
    command -v git >/dev/null 2>&1 || die "git is required to fetch the AirSim client"
    echo "  fetching $AIRSIM_REPO@$AIRSIM_PIN"
    rm -rf "$AIRSIM_DIR"; mkdir -p "$AIRSIM_DIR"
    git -C "$AIRSIM_DIR" init -q
    git -C "$AIRSIM_DIR" remote add origin "$AIRSIM_REPO"
    git -C "$AIRSIM_DIR" sparse-checkout init --cone
    git -C "$AIRSIM_DIR" sparse-checkout set PythonClient/airsim
    # blobless + sparse: ~0.8 MB instead of the ~210 MB full tree. Same commit
    # either way, so falling back to a plain shallow fetch is safe.
    if ! git -C "$AIRSIM_DIR" fetch -q --depth 1 --filter=blob:none origin "$AIRSIM_PIN" 2>/dev/null; then
        echo "  partial clone unsupported — retrying full shallow fetch"
        git -C "$AIRSIM_DIR" fetch -q --depth 1 origin "$AIRSIM_PIN" \
            || die "could not fetch $AIRSIM_REPO@$AIRSIM_PIN (no network, or the pin is gone)"
    fi
    git -C "$AIRSIM_DIR" checkout -q FETCH_HEAD
fi
[ -f "$AIRSIM_DIR/PythonClient/airsim/__init__.py" ] \
    || die "$AIRSIM_DIR/PythonClient has no airsim/__init__.py"
"$VPY" -c "
import sys; sys.path.insert(0, '$AIRSIM_DIR/PythonClient')
import airsim; print('  airsim OK:', airsim.__version__)
" || die "the vendored AirSim client does not import"

# ---------------------------------------------------------------------------
say "datum self-check (T1)"
# ---------------------------------------------------------------------------
# geo.canonical_altitude() RAISES rather than degrading when no geoid source is
# available, so a bad install is an immediate hard stop rather than a silently
# wrong altitude. Prove it works now, not on the first mission.
"$VPY" -c "
from godseye_uav.geo import geoid_undulation, geoid_source
n = geoid_undulation(0.0, 0.0)
print(f'  N(0,0) = {n:.2f} m via {geoid_source()}')
assert abs(n - 17.16) < 0.5, f'EGM96 looks wrong: {n}'
" || die "the EGM96 geoid is not working — see godseye/README.md 'Datum'"

# ---------------------------------------------------------------------------
if [ "$WITH_UI" = "1" ]; then
    say "God's Eye View UI"
    command -v npm >/dev/null 2>&1 || die "npm is required for --with-ui"
    NODE_V="$(node -v 2>/dev/null || echo none)"
    echo "  node $NODE_V (package.json wants >=24.14.0 <25 || >=26 <27)"
    case "$NODE_V" in
        v24.*|v26.*) : ;;
        *) echo "  !! WARNING: node $NODE_V is outside the range the UI declares."
           echo "  !! vite may fail to start. The simulation half works regardless." ;;
    esac
    ( cd "$GEV" && npm install --no-audit --no-fund )
else
    say "skipping the UI (pass --with-ui to npm install it)"
fi

cat <<EOF

[setup] done.

  run the tests      cd godseye && .venv/bin/python -m pytest tests -q
  one-command demo   cd godseye && ./scripts/demo_laptop.sh
  services only      cd godseye && ./start.sh

The demo needs no GPU and no Unreal: it runs against the built-in fake AirSim.
EOF
