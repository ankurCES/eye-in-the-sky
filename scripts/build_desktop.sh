#!/usr/bin/env bash
# build_desktop.sh — build the macOS app "Eye in the Sky.app" into dist/.
#
# Usage:
#   ./scripts/build_desktop.sh               # UI build + PyInstaller + Claude CLI + ad-hoc sign
#   ./scripts/build_desktop.sh --dmg         # ... and dist/Eye in the Sky.dmg
#   ./scripts/build_desktop.sh --selftest    # ... and run the packaged app's --selftest
#
# Options:
#   --dmg          also build a compressed disk image (hdiutil, UDZO)
#   --selftest     launch the built app once with --selftest (opens a window for a
#                  few seconds; temp store, free port) and fail the build if it fails
#   --no-cli       do not bundle the Claude CLI (225 MB); the analyst then uses the
#                  `claude` on the user's machine (~/.local/bin/claude, PATH, ...)
#   --no-ui-build  reuse the UI staged by the previous build (godseye/build/desktop/ui)
#   --bake-keys    let the UI build read GOOGLE_MAPS_API_KEY / CESIUM_ION_TOKEN from the
#                  environment or gods-eye-view/.env (default: built WITHOUT them, so no
#                  key ends up inside a redistributable bundle)
#   --clean        drop PyInstaller's cache and work dir first (slower, from scratch)
#   -h, --help     this text
#
# Environment: PYTHON (default godseye/.venv/bin/python; needs `pip install -e
# 'godseye[app,desktop]'`), GODSEYE_AIRSIM_PYTHONCLIENT (see godseye/scripts/_airsim_client.sh).
#
# Layout: work files under godseye/build/desktop/ (gitignored), the app in
# dist/ (gitignored). The UI is built into godseye/build/desktop/ui, never into
# gods-eye-view/dist, so a running dev stack's UI is left alone.
#
# The Claude CLI is copied into Contents/Helpers/claude AFTER PyInstaller and
# only the outer bundle is re-signed (ad hoc): PyInstaller would re-sign the
# CLI itself and strip Anthropic's signature. The result is for this machine
# (ad-hoc signature, arm64, the macOS the Python was built for); distribution
# needs a Developer ID signature and notarization.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GS="$REPO/godseye"
GEV="$REPO/gods-eye-view"
SPEC="$GS/packaging/macos/EyeInTheSky.spec"
BUILD="$GS/build/desktop"
UI_STAGE="$BUILD/ui"
WORK="$BUILD/pyinstaller"
PI_DIST="$BUILD/dist"
OUT="$REPO/dist"
APP_NAME="Eye in the Sky"
EXE_NAME="EyeInTheSky"
APP="$OUT/$APP_NAME.app"
DMG="$OUT/$APP_NAME.dmg"
ANTHROPIC_TEAM_ID="Q6L2SF6YDW"

say() { printf '[build_desktop] %s\n' "$*"; }
die() { printf '[build_desktop] FATAL: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

WANT_DMG=0; WANT_CLI=1; WANT_UI=1; WANT_SELFTEST=0; BAKE_KEYS=0; CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --dmg) WANT_DMG=1 ;;
    --selftest) WANT_SELFTEST=1 ;;
    --no-cli) WANT_CLI=0 ;;
    --no-ui-build) WANT_UI=0 ;;
    --bake-keys) BAKE_KEYS=1 ;;
    --clean) CLEAN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $arg" ;;
  esac
done

T0=$SECONDS
kib() { du -sk "$1" | awk '{print $1}'; }
human() { awk -v k="$1" 'BEGIN { printf (k >= 1048576 ? "%.2f GB" : "%.0f MB"), (k >= 1048576 ? k/1048576 : k/1024) }'; }

# --- prerequisites -------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "this builds a macOS .app; run it on macOS"
PY="${PYTHON:-$GS/.venv/bin/python}"
[ -x "$PY" ] || die "no python at $PY (run ./scripts/setup.sh, or set PYTHON)"
for tool in codesign ditto otool; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found (Xcode command line tools)"
done
if [ "$WANT_DMG" = 1 ]; then command -v hdiutil >/dev/null 2>&1 || die "hdiutil not found"; fi
[ -f "$SPEC" ] || die "missing $SPEC"

missing="$("$PY" - <<'PY'
import importlib.util
need = {"PyInstaller": "desktop", "webview": "app", "claude_agent_sdk": "app"}
print(" ".join(f"{m}[{extra}]" for m, extra in need.items() if importlib.util.find_spec(m) is None))
PY
)"
[ -z "$missing" ] || die "python packages missing: $missing -> $PY -m pip install -e '${GS}[app,desktop]'"

# msgpack-rpc-python only works with msgpack < 1.0 (see godseye/pyproject.toml),
# and two dists share the msgpack/ dir, so check what actually imports.
"$PY" -c 'import msgpack, sys; sys.exit(0 if msgpack.version < (1, 0) else 1)' \
  || die "msgpack >= 1.0 is installed; the AirSim transport needs msgpack < 1.0"

# shellcheck disable=SC2034  # read by the sourced resolver
GS_ROOT="$GS"
# shellcheck source=SCRIPTDIR/../godseye/scripts/_airsim_client.sh
. "$GS/scripts/_airsim_client.sh"
if ! resolve_airsim_client; then airsim_missing_help; exit 1; fi

CLI_SRC=""
if [ "$WANT_CLI" = 1 ]; then
  CLI_SRC="$("$PY" -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")')"
  [ -x "$CLI_SRC" ] || die "the Agent SDK has no bundled CLI at $CLI_SRC (pass --no-cli to build without it)"
fi

free_kib="$(df -Pk "$REPO" | awk 'NR==2 {print $4}')"
if [ "${free_kib:-0}" -lt 2097152 ]; then
  say "WARNING: only $(human "$free_kib") free on the build volume (a build needs about 1 GB)"
fi

say "python      : $("$PY" -c 'import sys, platform; print(sys.version.split()[0], platform.machine())') ($PY)"
say "pyinstaller : $("$PY" -c 'import PyInstaller; print(PyInstaller.__version__)')"
say "airsim      : $AIRSIM_CLIENT"
mkdir -p "$BUILD" "$OUT"

# --- 1. the UI ---------------------------------------------------------------------
if [ "$WANT_UI" = 1 ]; then
  command -v npm >/dev/null 2>&1 || die "npm not found (needed to build the UI; or pass --no-ui-build)"
  [ -d "$GEV/node_modules" ] || die "gods-eye-view has no node_modules: (cd gods-eye-view && npm install)"
  say "building the UI -> $UI_STAGE"
  t=$SECONDS
  if [ "$BAKE_KEYS" = 1 ]; then
    (cd "$GEV" && npm run build -- --outDir "$UI_STAGE" --emptyOutDir)
  else
    # Empty (not unset): vite's config only fills keys that are undefined, so
    # this also keeps a gods-eye-view/.env from supplying them.
    (cd "$GEV" && GOOGLE_MAPS_API_KEY="" CESIUM_ION_TOKEN="" \
       npm run build -- --outDir "$UI_STAGE" --emptyOutDir)
  fi
  UI_SECS=$((SECONDS - t))
  say "UI built in ${UI_SECS}s"
else
  UI_SECS=0
  say "reusing the staged UI in $UI_STAGE"
fi
[ -f "$UI_STAGE/index.html" ] || die "no UI at $UI_STAGE (drop --no-ui-build)"
if [ "$BAKE_KEYS" = 0 ]; then
  # Values are compared, never printed.
  for key in GOOGLE_MAPS_API_KEY CESIUM_ION_TOKEN; do
    val="${!key:-}"
    if [ "${#val}" -ge 8 ] && grep -rqF -- "$val" "$UI_STAGE"; then
      die "the staged UI contains the value of \$$key; refusing to bundle it"
    fi
  done
fi

# --- 2. the app icon (best effort: PyInstaller's default icon otherwise) -------------
ICON_SVG="$GS/packaging/macos/icon.svg"
ICNS="$BUILD/EyeInTheSky.icns"
make_icon() {
  local set="$BUILD/EyeInTheSky.iconset" render="$BUILD/icon-render" size
  rm -rf "$set" "$render" "$ICNS"
  mkdir -p "$set" "$render"
  # Quick Look renders SVG (WebKit) with alpha; sips scales; iconutil packs.
  qlmanage -t -s 1024 -o "$render" "$ICON_SVG" >/dev/null 2>&1 || return 1
  [ -f "$render/icon.svg.png" ] || return 1
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$render/icon.svg.png" \
      --out "$set/icon_${size}x${size}.png" >/dev/null 2>&1 || return 1
    sips -z "$((size * 2))" "$((size * 2))" "$render/icon.svg.png" \
      --out "$set/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || return 1
  done
  iconutil -c icns "$set" -o "$ICNS" || return 1
  rm -rf "$set" "$render"
}
ICON_ENV=""
if command -v qlmanage >/dev/null 2>&1 && command -v sips >/dev/null 2>&1 \
   && command -v iconutil >/dev/null 2>&1 && make_icon; then
  ICON_ENV="$ICNS"
else
  say "WARNING: could not render $ICON_SVG; the app keeps PyInstaller's default icon"
fi

# --- 3. PyInstaller ----------------------------------------------------------------
say "PyInstaller (onedir, windowed) -> $APP_NAME.app"
t=$SECONDS
[ "$CLEAN" = 1 ] && rm -rf "$WORK"
rm -rf "$PI_DIST"
pi_args=(--noconfirm --log-level WARN --distpath "$PI_DIST" --workpath "$WORK")
[ "$CLEAN" = 1 ] && pi_args+=(--clean)
EITS_UI_DIR="$UI_STAGE" EITS_AIRSIM_CLIENT="$AIRSIM_CLIENT" EITS_ICON="$ICON_ENV" \
  "$PY" -m PyInstaller "${pi_args[@]}" "$SPEC"
PI_SECS=$((SECONDS - t))
[ -d "$PI_DIST/$APP_NAME.app" ] || die "PyInstaller produced no $APP_NAME.app"
rm -rf "$APP"
mv "$PI_DIST/$APP_NAME.app" "$APP"
rm -rf "$PI_DIST"                       # the unbundled onedir copy (same files again)
SIZE_NO_CLI="$(kib "$APP")"
say "PyInstaller done in ${PI_SECS}s; app without the CLI: $(human "$SIZE_NO_CLI")"

# --- 4. the Claude CLI (keeps Anthropic's signature) --------------------------------
if [ "$WANT_CLI" = 1 ]; then
  mkdir -p "$APP/Contents/Helpers"
  ditto "$CLI_SRC" "$APP/Contents/Helpers/claude"
  codesign --verify --strict "$APP/Contents/Helpers/claude" \
    || die "the copied CLI's signature does not verify"
  team="$(codesign -dv "$APP/Contents/Helpers/claude" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
  [ "$team" = "$ANTHROPIC_TEAM_ID" ] \
    || say "WARNING: the CLI's TeamIdentifier is '${team:-none}', expected $ANTHROPIC_TEAM_ID"
  say "Claude CLI $("$APP/Contents/Helpers/claude" --version 2>/dev/null | head -1) -> Contents/Helpers/claude (team ${team:-none})"
fi

# --- 5. sign the outer bundle (ad hoc) and verify -----------------------------------
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP" || die "codesign --verify --deep --strict failed"
say "codesign: ad-hoc, verify --deep --strict OK"
SIZE_APP="$(kib "$APP")"

# --- 6. optional self-test of the packaged app ----------------------------------------
if [ "$WANT_SELFTEST" = 1 ]; then
  st_dir="$(mktemp -d "${TMPDIR:-/tmp}/eits-selftest.XXXXXX")"
  st_out="$BUILD/selftest.json"
  rm -f "$st_out"
  say "self-test: $APP_NAME.app --selftest (window opens for a few seconds)"
  # A throwaway token through the environment: the app prints a token only
  # when it generated it, and this log is kept.
  st_token="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  if GODSEYE_TOKEN="$st_token" "$APP/Contents/MacOS/$EXE_NAME" --selftest \
       --selftest-out "$st_out" --store "$st_dir/store" --port 0 \
       >"$BUILD/selftest.log" 2>&1; then
    say "self-test: PASS ($st_out)"
  else
    say "self-test: FAIL; verdict $st_out, log $BUILD/selftest.log"
    rm -rf "$st_dir"
    exit 1
  fi
  rm -rf "$st_dir"
fi

# --- 7. optional disk image -----------------------------------------------------------
if [ "$WANT_DMG" = 1 ]; then
  stage="$BUILD/dmg"
  rm -rf "$stage" "$DMG"
  mkdir -p "$stage"
  ditto "$APP" "$stage/$APP_NAME.app"
  ln -s /Applications "$stage/Applications"
  t=$SECONDS
  hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDZO "$DMG"
  rm -rf "$stage"
  say "disk image: $DMG ($(human "$(kib "$DMG")"), $((SECONDS - t))s)"
fi

# --- summary -----------------------------------------------------------------------------
say "done in $((SECONDS - T0))s (UI ${UI_SECS}s, PyInstaller ${PI_SECS}s)"
say "app : $APP"
if [ "$WANT_CLI" = 1 ]; then
  say "size: $(human "$SIZE_APP") ($(human "$SIZE_NO_CLI") without the Claude CLI)"
else
  say "size: $(human "$SIZE_APP") (no Claude CLI bundled)"
fi
say "arch: $(lipo -archs "$APP/Contents/MacOS/$EXE_NAME" 2>/dev/null || echo unknown); needs macOS $(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$APP/Contents/Info.plist" 2>/dev/null || echo '?')+"
say "run : open \"$APP\"   (or \"$APP/Contents/MacOS/$EXE_NAME\" --help)"
