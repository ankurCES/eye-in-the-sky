#!/usr/bin/env bash
#
# godSeye CI — the real zero-GPU gate (PLAN T8).
#
# The whole point of godseye_uav.fake_airsim is that this suite needs NO GPU,
# NO Unreal Engine and NO AirSim binary: FakeAirSim speaks the msgpack-rpc
# protocol to the REAL AirSim PythonClient over loopback. So CI runs the WHOLE
# suite, not a hand-picked pair of geo files.
#
# AIRSIM CLIENT DEPENDENCY — the choice, and why
# ----------------------------------------------
# The tests import the real `airsim` PythonClient, which is not a dependency
# this package can declare: the PyPI `airsim` release (1.8.1, 2022) is older
# than the client the contract tests are pinned against, and it drags in
# opencv-contrib-python, which the tests never touch.
#
# Of vendor / fetch / skip, this script FETCHES, pinned:
#   * VENDOR was rejected — copying microsoft/airsim's client into this tree
#     forks it silently; a drift between our copy and the simulator everyone
#     actually flies against is exactly the class of bug the contract tests
#     exist to catch.
#   * SKIP was rejected outright — the bridge/server/mission/launch/target
#     modules are ALL airsim importers. Skipping them leaves ~1/3 of the suite
#     unrun while the summary line still says "passed". A green build that
#     tested nothing is worse than a red one.
#   * FETCH, at the exact commit below, gives CI the same client a developer
#     has in their sibling checkout, reproducibly, with no GPU and no Unreal.
# A developer's existing ../airsim checkout is always preferred, so this script
# does not touch the network on a machine that already has one. If the fetch is
# needed and fails, this script FAILS — it does not fall through to a subset.
set -euo pipefail

# Gates 4, 5 and 6 decide pass/fail with `assert`, and `python -O` deletes
# assert statements outright -- an inherited PYTHONOPTIMIZE would turn every
# one of those gates into an unconditional PASS. A gate whose whole job is to
# refuse to pass silently must not be disableable by an environment variable.
unset PYTHONOPTIMIZE

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# microsoft/airsim. Bump deliberately, never "latest": the PythonClient is half
# of the wire contract tests/test_fake_airsim.py pins.
AIRSIM_REPO="${GODSEYE_AIRSIM_REPO:-https://github.com/microsoft/airsim}"
AIRSIM_PIN="${GODSEYE_AIRSIM_PIN:-1ca93f6f77e4e8a39b2b241c1fe2764da4d7dd41}"
# Fetch target lives under .godseye/, which .gitignore already covers, so a CI
# run never leaves the working tree dirty.
AIRSIM_DIR="${GODSEYE_AIRSIM_DIR:-$ROOT/.godseye/vendor/airsim}"

STEP=0
step() { STEP=$((STEP + 1)); printf '\n== %d. %s ==\n' "$STEP" "$1"; }
die()  { printf '\nCI FAILED: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
step "python environment"
# ---------------------------------------------------------------------------
VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "no $VENV — creating it"
    "${GODSEYE_BOOTSTRAP_PYTHON:-python3}" -m venv "$VENV"
    "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
    "$VENV/bin/python" -m pip install -e "${ROOT}[dev]"
elif [ "${GODSEYE_CI_INSTALL:-0}" = "1" ]; then
    echo "GODSEYE_CI_INSTALL=1 — refreshing $VENV"
    "$VENV/bin/python" -m pip install -e "${ROOT}[dev]"
else
    echo "using existing $VENV (set GODSEYE_CI_INSTALL=1 to reinstall)"
fi
PY="$VENV/bin/python"
"$PY" -VV

# Import-check the declared runtime deps here rather than discovering a missing
# one 20 minutes into the suite as a confusing test failure.
"$PY" - <<'PYCHECK' || die "runtime dependencies are missing; run: .venv/bin/pip install -e '.[dev]'"
import importlib, sys
missing = []
# httpx2 is listed separately from httpx on purpose: they are different
# packages and the suite imports BOTH (test_bridge -> httpx, test_server /
# test_launch -> httpx2).  Checking only "httpx" is how an httpx2 that arrives
# by transitive luck passes this gate and then vanishes on the next resolve.
for mod in ("pyproj", "egm96", "msgpack", "msgpackrpc", "fastapi", "uvicorn",
            "pydantic", "anyio", "mcp", "pytest", "httpx", "httpx2", "numpy"):
    try:
        importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"  {mod}: {type(exc).__name__}: {exc}")
if missing:
    print("MISSING / BROKEN IMPORTS:", *missing, sep="\n", file=sys.stderr)
    raise SystemExit(1)
print("runtime imports OK")
PYCHECK

# ---------------------------------------------------------------------------
step "AirSim PythonClient (fetch, pinned $AIRSIM_PIN)"
# ---------------------------------------------------------------------------
SIBLING="$ROOT/../airsim/PythonClient"
if [ -f "$SIBLING/airsim/__init__.py" ]; then
    AIRSIM_PYTHONCLIENT="$(cd "$SIBLING" && pwd)"
    echo "using the sibling checkout: $AIRSIM_PYTHONCLIENT (no fetch)"
    # Say out loud which client this run actually tested against. A sibling
    # checkout sitting at a different commit is legitimate on a dev box and
    # fatal to trust in a result, so it is never silent.
    SIBLING_HEAD="$(git -C "$AIRSIM_PYTHONCLIENT" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [ "$SIBLING_HEAD" = "$AIRSIM_PIN" ]; then
        echo "  at the pinned commit $AIRSIM_PIN"
    else
        echo "  !! WARNING: sibling checkout is at $SIBLING_HEAD, NOT the pin"
        echo "  !! $AIRSIM_PIN — this run tests a DIFFERENT AirSim client than CI will."
        echo "  !! Set GODSEYE_AIRSIM_DIR / move the checkout aside to force the fetch."
    fi
elif [ -f "$AIRSIM_DIR/PythonClient/airsim/__init__.py" ] \
     && [ "$(git -C "$AIRSIM_DIR" rev-parse HEAD 2>/dev/null || echo none)" = "$AIRSIM_PIN" ]; then
    AIRSIM_PYTHONCLIENT="$AIRSIM_DIR/PythonClient"
    echo "using the cached fetch at pin: $AIRSIM_PYTHONCLIENT"
else
    command -v git >/dev/null 2>&1 || die "git is required to fetch the AirSim PythonClient"
    echo "fetching $AIRSIM_REPO@$AIRSIM_PIN into $AIRSIM_DIR"
    rm -rf "$AIRSIM_DIR"
    mkdir -p "$AIRSIM_DIR"
    git -C "$AIRSIM_DIR" init -q
    git -C "$AIRSIM_DIR" remote add origin "$AIRSIM_REPO"
    git -C "$AIRSIM_DIR" sparse-checkout init --cone
    git -C "$AIRSIM_DIR" sparse-checkout set PythonClient/airsim
    # blobless + sparse: ~0.8 MB and a second or two, instead of the ~210 MB the
    # full AirSim tree costs. Only the transport shape changes -- the commit is
    # the same pin either way -- so falling back to a plain shallow fetch on a
    # server without partial-clone support is safe, and it says so out loud.
    if ! git -C "$AIRSIM_DIR" fetch -q --depth 1 --filter=blob:none origin "$AIRSIM_PIN" 2>/dev/null; then
        echo "  partial clone unsupported by the remote — retrying full shallow fetch"
        git -C "$AIRSIM_DIR" fetch -q --depth 1 origin "$AIRSIM_PIN" \
            || die "could not fetch $AIRSIM_REPO@$AIRSIM_PIN — no network, or the pin is gone. Refusing to run a subset of the suite without the AirSim client."
    fi
    git -C "$AIRSIM_DIR" checkout -q FETCH_HEAD
    AIRSIM_PYTHONCLIENT="$AIRSIM_DIR/PythonClient"
    [ -f "$AIRSIM_PYTHONCLIENT/airsim/__init__.py" ] \
        || die "$AIRSIM_REPO@$AIRSIM_PIN has no PythonClient/airsim/__init__.py"
fi
# tests/conftest.py is the single place that puts this on sys.path; this is the
# documented override it honours. No PYTHONPATH pointing at somebody's $HOME.
export GODSEYE_AIRSIM_PYTHONCLIENT="$AIRSIM_PYTHONCLIENT"
"$PY" -c "
import sys; sys.path.insert(0, '$AIRSIM_PYTHONCLIENT')
import airsim; print('airsim client OK:', airsim.__file__)
" || die "the AirSim PythonClient at $AIRSIM_PYTHONCLIENT does not import"

# ---------------------------------------------------------------------------
step "lint (ADVISORY — not a gate)"
# ---------------------------------------------------------------------------
if "$PY" -m ruff check mcp/ tests/; then
    echo "ruff clean"
else
    echo "ruff reported findings above — ADVISORY ONLY, not failing the build"
fi

# ---------------------------------------------------------------------------
step "packaging gate: the wheel must ship a usable geoid (T1)"
# ---------------------------------------------------------------------------
# geo.canonical_altitude() raises GeoidUnavailableError rather than degrade, so
# a wheel that omits the EGM96 grid or does not depend on a reader is not
# "slightly worse" — it is a package that hard-stops on its first altitude.
# Proven against a real install, not against the source tree, because the
# source tree has the .tif sitting there whether the wheel ships it or not.
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
# Build from a staged copy of the packaged inputs, not from $ROOT: setuptools
# writes build/ and *.egg-info next to the sources it is pointed at, and a CI
# run must not dirty the working tree. Staging also means the wheel can only
# contain what pyproject.toml DECLARES -- stray files in the checkout cannot
# sneak in and make the gate pass for the wrong reason.
SRC_DIR="$BUILD_DIR/src"
mkdir -p "$SRC_DIR/mcp"
cp "$ROOT/pyproject.toml" "$SRC_DIR/pyproject.toml"
cp -R "$ROOT/mcp/godseye_uav" "$SRC_DIR/mcp/godseye_uav"
find "$SRC_DIR" \( -name '__pycache__' -o -name '*.egg-info' \) -prune -exec rm -rf {} +
# Build isolation left ON: it makes the build use exactly the [build-system]
# requires this file declares, instead of whatever happens to be in .venv.
"$PY" -m pip wheel --no-deps -w "$BUILD_DIR" "$SRC_DIR" >/dev/null \
    || die "wheel build failed"
WHEEL="$(ls "$BUILD_DIR"/godseye_uav-*.whl 2>/dev/null | head -1 || true)"
[ -n "$WHEEL" ] || die "no godseye_uav wheel was produced in $BUILD_DIR"
echo "built $(basename "$WHEEL")"

"$PY" - "$WHEEL" <<'PYWHEEL' || die "the built wheel does not carry the EGM96 grid and/or a declared geoid reader"
import re, sys, zipfile
wheel = sys.argv[1]
zf = zipfile.ZipFile(wheel)
names = zf.namelist()

grid = [n for n in names if n.endswith("godseye_uav/data/us_nga_egm96_15.tif")]
if not grid:
    print("FAIL: godseye_uav/data/us_nga_egm96_15.tif is NOT in the wheel.\n"
          "      geo.GEOID_GRID_PATH resolves next to the installed package, so\n"
          "      the PRIMARY geoid source would be a missing file.\n"
          "      Fix: [tool.setuptools.package-data] godseye_uav = [\"data/*.tif\"]",
          file=sys.stderr)
    raise SystemExit(1)
size = zf.getinfo(grid[0]).file_size
if size < 1_000_000:
    print(f"FAIL: {grid[0]} is only {size} bytes — not the 15' EGM96 grid.",
          file=sys.stderr)
    raise SystemExit(1)

meta = [n for n in names if n.endswith(".dist-info/METADATA")][0]
requires = [
    line.split(":", 1)[1].strip()
    for line in zf.read(meta).decode().splitlines()
    if line.lower().startswith("requires-dist:")
]
# Only unconditional requirements count. `pyproj>=3.6; extra == "geo"` is
# exactly the shape that made a clean install unable to compute a geoid: it
# reads like a dependency and installs like nothing.
core = [r for r in requires if "extra ==" not in r]


def req_name(spec):
    return re.match(r"\s*([A-Za-z0-9._-]+)", spec).group(1).lower().replace("_", "-")


installed = {req_name(r) for r in core}
for pkg, why in (("pyproj", "reads the shipped NGA 15' grid (PRIMARY source)"),
                 ("egm96", "pure-Python EGM96 reader (BACKUP source)")):
    if pkg not in installed:
        extra = [r for r in requires if req_name(r) == pkg]
        print(f"FAIL: {pkg!r} is not an unconditional runtime dependency of the "
              f"wheel ({why}).\n"
              f"      Declared instead as: {extra or 'nothing at all'}\n"
              f"      An optional extra is not a dependency: a clean "
              f"'pip install .' would boot\n"
              f"      straight into GeoidUnavailableError.", file=sys.stderr)
        raise SystemExit(1)
print(f"wheel ships {grid[0]} ({size} bytes)")
print("  unconditional requires: " + ", ".join(sorted(installed)))
PYWHEEL

# ---------------------------------------------------------------------------
step "clean-install gate: N at a known point, from a throwaway venv"
# ---------------------------------------------------------------------------
# The proof that matters: a venv that has NEVER seen this repo's source tree,
# installs the wheel and nothing else, and computes a real EGM96 undulation.
CLEAN_VENV="$BUILD_DIR/clean-venv"
"${GODSEYE_BOOTSTRAP_PYTHON:-python3}" -m venv "$CLEAN_VENV"
"$CLEAN_VENV/bin/python" -m pip install -q --upgrade pip >/dev/null
"$CLEAN_VENV/bin/python" -m pip install -q "$WHEEL" \
    || die "the wheel does not install into a clean venv"
# cd out of the repo so the source tree cannot possibly satisfy the import.
(cd "$BUILD_DIR" && "$CLEAN_VENV/bin/python" - <<'PYGEOID') \
    || die "a clean install of godseye-uav cannot compute a geoid undulation"
import godseye_uav.geo as geo

assert "site-packages" in geo.__file__, f"not the installed copy: {geo.__file__}"

# EGM96 N(0, 0) = +17.16 m — a published, independently checkable value.
fix = geo.canonical_altitude(100.0, 0.0, 0.0, datum="msl")
assert not fix.degraded, f"clean install DEGRADED to {fix.source}"
assert fix.source in (geo._SOURCE_GRID, geo._SOURCE_WHEEL), fix.source
assert abs(fix.undulation_m - 17.16) < 0.05, f"N(0,0) = {fix.undulation_m}"
assert abs(fix.alt_hae - 117.16) < 0.05, f"altHae = {fix.alt_hae}"

# and the PRIMARY source specifically — the shipped grid — must be the one
# answering, which is only true if the .tif actually landed in the wheel.
import os
assert os.path.isfile(geo.GEOID_GRID_PATH), (
    f"EGM96 grid not installed: {geo.GEOID_GRID_PATH}")
assert abs(geo._grid_undulation(0.0, 0.0) - 17.16) < 0.05

print(f"clean install OK: {geo.__file__}")
print(f"  grid   {geo.GEOID_GRID_PATH}")
print(f"  N(0,0) {fix.undulation_m:.3f} m via {fix.source}")
PYGEOID

# ---------------------------------------------------------------------------
step "seam gate: tests/conftest.py must refuse a fake AirSim, not accept it"
# ---------------------------------------------------------------------------
# The collection gate below proves the seam WORKS. This one proves it FAILS
# CORRECTLY, which is the half that rots silently. Each case is run against a
# throwaway repo skeleton holding nothing but a copy of tests/conftest.py, so
# the developer's real sibling checkout cannot answer for it.
"$PY" - "$ROOT/tests/conftest.py" "$AIRSIM_PYTHONCLIENT" <<'PYSEAM' \
    || die "tests/conftest.py does not fail correctly on a missing/fake AirSim client"
import os, pathlib, shutil, subprocess, sys, tempfile

CONFTEST, REAL_CLIENT = sys.argv[1], sys.argv[2]

CHILD = r"""
import sys, importlib.util
case = sys.argv[1]
spec = importlib.util.spec_from_file_location("ct", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except RuntimeError as exc:
    print("RUNTIMEERROR:", str(exc).splitlines()[0]); raise SystemExit(0)
if case == "late":
    sys.path.insert(0, sys.argv[3])
try:
    import airsim
except ModuleNotFoundError as exc:
    print("MODULENOTFOUND:", str(exc).splitlines()[0]); raise SystemExit(0)
print("IMPORTED: MultirotorClient=%s" % hasattr(airsim, "MultirotorClient"))
"""

# (case, expected stdout prefix, what a regression here would mean)
CASES = [
    ("hollow", "MODULENOTFOUND",
     "a bare directory named 'airsim' on sys.path (i.e. the AirSim checkout's "
     "PARENT on PYTHONPATH) was accepted as a namespace package"),
    ("blank", "RUNTIMEERROR",
     "GODSEYE_AIRSIM_PYTHONCLIENT='' silently fell through to another client"),
    ("wrong", "RUNTIMEERROR",
     "a wrong GODSEYE_AIRSIM_PYTHONCLIENT silently fell through"),
    ("late", "IMPORTED: MultirotorClient=True",
     "a REAL client added to sys.path after conftest ran was wrongly refused"),
]

failed = []
for case, expect, harm in CASES:
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        (tmp / "repo" / "tests").mkdir(parents=True)
        shutil.copy(CONFTEST, tmp / "repo" / "tests" / "conftest.py")
        (tmp / "poison" / "airsim").mkdir(parents=True)   # hollow: no __init__.py
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONPATH", "GODSEYE_AIRSIM_PYTHONCLIENT")}
        argv = [sys.executable, "-c", CHILD, case,
                str(tmp / "repo" / "tests" / "conftest.py")]
        if case == "hollow":
            env["PYTHONPATH"] = str(tmp / "poison")
        elif case == "blank":
            (tmp / "airsim").mkdir()      # a real SIBLING to fall through TO
            os.symlink(REAL_CLIENT, tmp / "airsim" / "PythonClient")
            env["GODSEYE_AIRSIM_PYTHONCLIENT"] = ""
        elif case == "wrong":
            env["GODSEYE_AIRSIM_PYTHONCLIENT"] = str(tmp / "nope")
        elif case == "late":
            argv.append(REAL_CLIENT)
        out = subprocess.run(argv, env=env, cwd=str(tmp), text=True,
                             capture_output=True)
        got = (out.stdout + out.stderr).strip().splitlines()
        got = got[0] if got else "(no output)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if got.startswith(expect):
        print(f"  ok   {case:7s} {got[:90]}")
    else:
        failed.append((case, expect, got, harm))
        print(f"  FAIL {case:7s} expected {expect!r}, got: {got[:140]}", file=sys.stderr)

if failed:
    for case, expect, got, harm in failed:
        print(f"\nseam case {case!r}: {harm}", file=sys.stderr)
    raise SystemExit(1)
PYSEAM

# ---------------------------------------------------------------------------
step "collection gate: every test module must import on its own"
# ---------------------------------------------------------------------------
# The suite used to pass only because test_bridge.py sorts first and patched
# sys.path for everyone behind it. Collecting each file alone is what catches
# that regression the moment it comes back.
# PYTHONPATH is UNSET for the rest of the run, on purpose. The old gate exported
# PYTHONPATH=mcp:/Users/<someone>/.../airsim/PythonClient; with that in the
# environment this check would pass without tests/conftest.py doing anything.
# Running without it is what actually proves the seam works.
COLLECT_FAILED=0
for f in "$ROOT"/tests/test_*.py; do
    if env -u PYTHONPATH "$PY" -m pytest "$f" -q --collect-only \
            >"$BUILD_DIR/collect.err" 2>&1; then
        printf '  ok   %s\n' "$(basename "$f")"
    else
        COLLECT_FAILED=1
        printf '  FAIL %s\n' "$(basename "$f")"
        sed 's/^/       /' "$BUILD_DIR/collect.err" | tail -25
    fi
done
[ "$COLLECT_FAILED" -eq 0 ] || die "test modules are not independently collectable"

# ---------------------------------------------------------------------------
step "full UE-free suite"
# ---------------------------------------------------------------------------
env -u PYTHONPATH "$PY" -m pytest "$ROOT/tests" -q

printf '\nALL GATES PASS\n'
