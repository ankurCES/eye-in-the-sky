# -*- mode: python -*-
# PyInstaller spec for "Eye in the Sky.app" (macOS, onedir, windowed).
#
# Build it with scripts/build_desktop.sh (repo root), which stages the UI,
# runs PyInstaller on this file, then copies the Claude CLI into
# Contents/Helpers and re-signs the outer bundle. Direct use:
#
#   EITS_UI_DIR=<vite build output> pyinstaller --noconfirm \
#       --distpath <dir> --workpath <dir> godseye/packaging/macos/EyeInTheSky.spec
#
# Inputs (environment):
#   EITS_UI_DIR          built UI (index.html + assets); REQUIRED. Bundled as "ui"
#                        (sys._MEIPASS/ui, where host.default_ui_dir looks when frozen).
#   EITS_AIRSIM_CLIENT   AirSim PythonClient dir; default: the same search as
#                        godseye/scripts/_airsim_client.sh.
#   EITS_ICON            optional .icns for the bundle.
#   EITS_VERSION         optional CFBundleShortVersionString; default: pyproject version.
#
# What is deliberately NOT here: the Agent SDK's bundled `claude` (225 MB).
# PyInstaller re-signs every Mach-O it collects (ad hoc), which strips
# Anthropic's Developer ID signature; the build script copies it into
# Contents/Helpers/claude afterwards instead, where host.frozen_cli_path()
# finds it.
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

APP_NAME = "Eye in the Sky"
EXE_NAME = "EyeInTheSky"
BUNDLE_ID = "io.eyeinthesky.console"

SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821 - injected by PyInstaller
GS = SPEC_DIR.parents[1]                     # godseye/
ENTRY = SPEC_DIR / "eye_in_the_sky.py"


def _die(msg: str) -> None:
    raise SystemExit(f"[EyeInTheSky.spec] {msg}")


def _airsim_client() -> Path:
    """Same order as scripts/_airsim_client.sh (and tests/conftest.py)."""
    for cand in (os.environ.get("EITS_AIRSIM_CLIENT"),
                 os.environ.get("GODSEYE_AIRSIM_PYTHONCLIENT"),
                 GS.parent / "airsim" / "PythonClient",
                 GS / ".godseye" / "vendor" / "airsim" / "PythonClient"):
        if cand and (Path(cand) / "airsim" / "__init__.py").is_file():
            return Path(cand).resolve()
    _die("AirSim PythonClient not found; run ./scripts/setup.sh or set EITS_AIRSIM_CLIENT")


def _ui_dir() -> Path:
    raw = os.environ.get("EITS_UI_DIR")
    if not raw:
        _die("EITS_UI_DIR is not set (the built UI to bundle)")
    ui = Path(raw).expanduser().resolve()
    if not (ui / "index.html").is_file():
        _die(f"{ui} has no index.html; build the UI first (npm run build)")
    return ui


def _version() -> str:
    if os.environ.get("EITS_VERSION"):
        return os.environ["EITS_VERSION"]
    with open(GS / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _min_macos() -> str:
    """The oldest macOS the frozen interpreter runs on (LC_BUILD_VERSION minos
    of libpython). Homebrew builds for the machine it runs on, so this is
    honest rather than a hopeful constant.
    """
    lib = Path(sys.base_prefix) / "Python"               # framework build
    if not lib.is_file():
        found = sorted(Path(sys.base_prefix, "lib").glob("libpython3*.dylib"))
        lib = found[0] if found else Path(sys.executable)
    try:
        out = subprocess.run(["otool", "-l", str(lib)], capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "12.0"
    found = re.findall(r"^\s*minos\s+(\d+(?:\.\d+)*)", out, flags=re.MULTILINE)
    return found[0] if found else "12.0"


def _is_bundled_cli(dest: str) -> bool:
    parts = Path(dest).parts
    return "claude_agent_sdk" in parts and "_bundled" in parts


AIRSIM = _airsim_client()
UI = _ui_dir()
VERSION = _version()
ICON = os.environ.get("EITS_ICON") or None
if ICON and not Path(ICON).is_file():
    _die(f"EITS_ICON={ICON} does not exist")

datas = [
    (str(UI), "ui"),
    # geo.py reads data/us_nga_egm96_15.tif next to the package (PROJ vgridshift);
    # chat.py reads the analyst prompt (base, ISR identity, wargame addendum; M14a)
    # through importlib.resources.
    *collect_data_files("godseye_uav", includes=["data/*.tif", "analyst_prompt.md",
                                                 "analyst_prompt_isr.md",
                                                 "analyst_prompt_wargame.md"]),
    *collect_data_files("egm96"),                 # the geoid fallback's model file
    *copy_metadata("godseye-uav"),                # app_version() for /app/config
]

hiddenimports = [
    *collect_submodules("godseye_uav"),
    "airsim",                                     # vendored PythonClient (pathex)
    # Imported with importlib.import_module (chat.py) or by name at runtime.
    *collect_submodules("claude_agent_sdk", filter=lambda n: ".testing" not in n),
    "webview",
    "webview.platforms.cocoa",
]

excludes = [
    # airsim's optional extras (utils.py imports cv2 lazily; pfm.py is unused).
    "cv2", "matplotlib",
    # pywebview's other GUI backends; this bundle is Cocoa (WKWebView) only.
    "webview.platforms.gtk", "webview.platforms.qt", "webview.platforms.android",
    "webview.platforms.winforms", "webview.platforms.edgechromium",
    "webview.platforms.mshtml", "webview.platforms.cef",
    "gi", "qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "clr", "cefpython3",
    # development-only
    "tkinter", "pytest", "_pytest", "IPython", "PyInstaller",
]

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(GS / "mcp"), str(AIRSIM)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Belt and braces: whatever a hook collects from claude_agent_sdk/_bundled
# stays out (see the header).
a.datas = [e for e in a.datas if not _is_bundled_cli(e[0])]
a.binaries = [e for e in a.binaries if not _is_bundled_cli(e[0])]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=EXE_NAME,
)

app = BUNDLE(  # noqa: F821
    coll,
    name=f"{APP_NAME}.app",
    icon=ICON,
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": _min_macos(),
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSSupportsAutomaticGraphicsSwitching": True,
        "NSHumanReadableCopyright": "Eye in the Sky: ISR intelligence console (simulation).",
    },
)
