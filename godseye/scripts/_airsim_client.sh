# shellcheck shell=bash
# Resolve the AirSim PythonClient. SOURCE this; do not execute it.
#
# One resolver, used by start.sh and demo_laptop.sh, in the SAME order
# tests/conftest.py uses. They diverged once already: setup.sh vendors the
# pinned client into .godseye/vendor/, while the runtime scripts looked only
# for a sibling ../airsim checkout, so a fresh clone installed the client and
# then failed to find it.
#
# Sets AIRSIM_CLIENT to the directory holding airsim/__init__.py, or "".
# Expects GS_ROOT to be the godseye/ root.
_airsim_ok() { [ -f "$1/airsim/__init__.py" ]; }

resolve_airsim_client() {
    AIRSIM_CLIENT=""
    # 1. explicit override — the same variable conftest.py honours
    if [ -n "${GODSEYE_AIRSIM_PYTHONCLIENT:-}" ]; then
        if _airsim_ok "$GODSEYE_AIRSIM_PYTHONCLIENT"; then
            AIRSIM_CLIENT="$GODSEYE_AIRSIM_PYTHONCLIENT"; return 0
        fi
        echo "[airsim] WARNING: GODSEYE_AIRSIM_PYTHONCLIENT=$GODSEYE_AIRSIM_PYTHONCLIENT" >&2
        echo "[airsim]          has no airsim/__init__.py — ignoring it." >&2
    fi
    # 2. sibling checkout (dev box: microsoft/airsim cloned next to the repo)
    if _airsim_ok "$GS_ROOT/../airsim/PythonClient"; then
        AIRSIM_CLIENT="$(cd "$GS_ROOT/../airsim/PythonClient" && pwd)"; return 0
    fi
    # 3. the pinned copy scripts/setup.sh and scripts/ci.sh fetch
    if _airsim_ok "$GS_ROOT/.godseye/vendor/airsim/PythonClient"; then
        AIRSIM_CLIENT="$(cd "$GS_ROOT/.godseye/vendor/airsim/PythonClient" && pwd)"; return 0
    fi
    return 1
}

# Print what to do when nothing was found. The repo root is one level up from
# godseye/ in eye-in-the-sky; setup.sh lives there.
airsim_missing_help() {
    cat >&2 <<HELP
[airsim] The AirSim PythonClient was not found. It is a RUNTIME dependency
[airsim] (godseye_uav.launch imports it) and it is NOT the PyPI release, so
[airsim] pip install alone does not provide it.
[airsim]
[airsim] Fix it with:
[airsim]     ./scripts/setup.sh            # from the repository root
[airsim]
[airsim] Searched, in order:
[airsim]   - \$GODSEYE_AIRSIM_PYTHONCLIENT (${GODSEYE_AIRSIM_PYTHONCLIENT:-unset})
[airsim]   - $GS_ROOT/../airsim/PythonClient
[airsim]   - $GS_ROOT/.godseye/vendor/airsim/PythonClient
HELP
}
