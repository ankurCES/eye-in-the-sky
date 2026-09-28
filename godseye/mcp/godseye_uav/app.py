"""Eye in the Sky: the application entry point.

    python -m godseye_uav.app [--window | --browser | --headless] [options]
    godseye-app ...                       (console script, same thing)

One process runs the sim, the MCP server, the telemetry bridge, the intel
graph, the analyst chat and the built UI on one origin (``host.build_host``).
The three modes:

* ``--window`` (the default when pywebview is importable and a display is
  available): a native window (WKWebView on macOS). The window owns the main
  thread, and the host runs on a worker thread with its own event loop.
* ``--browser``: the default browser opens the console. The host runs on the
  main thread.
* ``--headless``: no UI is opened. ``start.sh`` uses this mode.

``--selftest`` opens the window, checks from inside the page that the console
mounted, WebGL2 works, ``/app/config`` answers and the intel graph was
fetched, writes a JSON verdict (``--selftest-out``, else stdout) and exits 0
(pass) or 1 (fail) within ``--selftest-timeout`` seconds (60). The packaged
``.app`` is verified with it (``scripts/build_desktop.sh``).

Defaults for the desktop app: port 8780, a random token per launch (or
``$GODSEYE_TOKEN``), and the store under the user's application-support
directory (absolute; a Finder launch runs with cwd ``/``). For external
harnesses, the MCP URL and token are written to ``<store>/../mcp.json``
(mode 0600). A frozen app launched from Finder has nowhere to print, so its
output goes to ``<store>/../logs/eye-in-the-sky.log`` (mode 0600).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import secrets
import signal
import socket
import stat
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "[eye-in-the-sky]"
WINDOW_TITLE = "Eye in the Sky"
WINDOW_SIZE = (1440, 900)
WINDOW_MIN_SIZE = (1024, 700)
WINDOW_BACKGROUND = "#05070a"
DEFAULT_PORT = 8780
#: Mirrors chat.EFFORT_LEVELS (not imported: the parser must work even when the
#: analyst module cannot load; the host then serves an honest 503 for it).
ANALYST_EFFORTS = ("low", "medium", "high", "xhigh", "max")
READY_TIMEOUT_S = 60.0
#: --selftest: the whole run (boot, page, checks, shutdown) fits in this.
SELFTEST_TIMEOUT_S = 60.0
#: Page-side checks, judged by ``evaluate_selftest``; ``package_data`` is
#: checked in Python (a frozen build that dropped its data files).
SELFTEST_PAGE_CHECKS = ("console_mounted", "webgl2", "app_config", "intel_graph")
SELFTEST_CHECKS = (*SELFTEST_PAGE_CHECKS, "package_data")
SELFTEST_POLL_S = 0.25
APP_NAME = "eye-in-the-sky"
LOG_NAME = "eye-in-the-sky.log"
LOG_MAX_BYTES = 5 * 1024 * 1024

# Session-bound variables a Claude Code parent leaks into child processes.
# If they are inherited, the analyst's CLI attaches to the PARENT session
# (its IDE port, OAuth handoff, SDK entrypoint) instead of starting cleanly.
_SESSION_VARS = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_EFFORT", "ANTHROPIC_BASE_URL",
})
_SESSION_PREFIXES = ("CLAUDE_CODE_OAUTH_", "CLAUDE_CODE_SDK_", "CLAUDE_CODE_MESSAGING_")


def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` without Claude Code session-bound variables.

    Only acts when we were launched from inside a Claude Code session
    (``CLAUDECODE`` or ``CLAUDE_CODE_ENTRYPOINT`` present). Credentials and
    provider selection are kept: ``ANTHROPIC_API_KEY``,
    ``CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY``, ``AWS_*``, ``GOOGLE_*``.
    ``ANTHROPIC_BASE_URL`` is dropped only in that case (the session's proxy is
    not ours); outside a session a user-set base URL is respected.
    """
    out = dict(env)
    if "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env:
        return out
    for key in list(out):
        if key in _SESSION_VARS or key.startswith(_SESSION_PREFIXES):
            del out[key]
    return out


def apply_sanitized_env(environ: MutableMapping[str, str]) -> list[str]:
    """Apply ``sanitize_env`` in place; return the removed names (never values)."""
    clean = sanitize_env(environ)
    removed = sorted(k for k in environ if k not in clean)
    for key in removed:
        del environ[key]
    return removed


#: BYOK spec §3.3: every provider, credential and model variable the analyst's
#: CLI would read. Captured at launch for the settings, then popped: the SDK
#: can only SET child variables, never unset them, and a stray base URL with no
#: key sends the Claude login to that host. The AWS/Google credential chains,
#: proxies and NODE_EXTRA_CA_CERTS stay (inert without a CLAUDE_CODE_USE_* switch).
LLM_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_AWS_API_KEY",
    "OPENROUTER_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
    "ZAI_API_KEY", "ZHIPU_API_KEY", "DASHSCOPE_API_KEY", "OLLAMA_API_KEY",
    "GODSEYE_LLM_PROVIDER",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
    "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID", "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_GATEWAY_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "ANTHROPIC_UNIX_SOCKET",
)


def capture_llm_env(environ: MutableMapping[str, str]) -> dict[str, str]:
    """Take the model-provider variables out of ``environ`` (spec §3.3).

    Runs after ``sanitize_env`` and before anything can spawn the CLI.
    ``llm_settings.capture_llm_env`` does the capture; whatever it leaves,
    and everything when that module cannot load, is popped from
    ``LLM_ENV_VARS`` here (fail closed). Returns name -> value for
    ``HostConfig.llm_env``: it holds secrets, so print names only.
    """
    captured: dict[str, str] = {}
    try:
        from .llm_settings import capture_llm_env as settings_capture
    except Exception:  # noqa: BLE001 - a broken settings module must not keep keys around
        settings_capture = None
    if settings_capture is not None:
        got = settings_capture(environ)
        if isinstance(got, Mapping):
            captured.update({str(k): str(v) for k, v in got.items()})
    for name in LLM_ENV_VARS:
        if name in environ:
            captured.setdefault(name, environ.pop(name))
    return captured


def resolve_token(arg: str | None, env: Mapping[str, str]) -> tuple[str, str]:
    """(token, source): ``--token`` > ``$GODSEYE_TOKEN`` > random per launch."""
    if arg:
        return arg, "--token"
    if env.get("GODSEYE_TOKEN"):
        return env["GODSEYE_TOKEN"], "GODSEYE_TOKEN"
    return secrets.token_urlsafe(24), "generated"


def webview_available() -> bool:
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def window_problem(webview_mod=None) -> str | None:
    """Why a pywebview window cannot open here, or None if it can.

    ``find_spec`` only proves pywebview is installed. Its GUI backend (Cocoa
    via PyObjC, GTK, Qt) is loaded by ``webview.start()``, which raises
    ``WebViewException`` -- not ``ImportError`` -- when none loads. Loading
    it here, on the main thread and BEFORE the host serves, lets the caller
    fall back to the browser with a host that has not been used yet.
    """
    try:
        webview = webview_mod if webview_mod is not None else importlib.import_module("webview")
        init = getattr(webview, "initialize", None)   # pywebview's guilib.initialize
        if callable(init):
            init()
    except Exception as exc:  # noqa: BLE001 - WebViewException, ImportError, ...
        return f"{type(exc).__name__}: {exc}"
    return None


def reap_cli_children() -> None:
    """SIGTERM every Claude CLI the Agent SDK still has running.

    The SDK registers this very reaper with ``atexit``; a forced exit
    (``os._exit``) skips atexit, and a CLI in the middle of an API request
    does not exit when its stdin closes, so it would be orphaned (ppid 1) and
    keep working. Only acts when the SDK transport module is already loaded.
    """
    mod = sys.modules.get("claude_agent_sdk._internal.transport.subprocess_cli")
    kill = getattr(mod, "_kill_active_children", None) if mod is not None else None
    if callable(kill):
        with contextlib.suppress(Exception):
            kill()


def display_available(env: Mapping[str, str], platform: str | None = None) -> bool:
    """Best guess whether a GUI window can be shown."""
    platform = sys.platform if platform is None else platform
    if env.get("CI"):
        return False
    if platform == "darwin":
        return not (env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    if platform.startswith("win"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def default_mode(env: Mapping[str, str], *, has_webview: bool,
                 platform: str | None = None) -> str:
    """``window`` if pywebview is importable and a display is available, else
    ``browser``. ``--headless`` is never implied; it is always asked for.
    """
    if has_webview and display_available(env, platform):
        return "window"
    return "browser"


def build_parser() -> argparse.ArgumentParser:
    from . import theaters

    ap = argparse.ArgumentParser(
        prog="godseye-app",
        description="Eye in the Sky: ISR intelligence console (sim + MCP + bridge + "
                    "analyst + UI in one process)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--window", dest="mode", action="store_const", const="window",
                      help="native window (pywebview); default when available")
    mode.add_argument("--browser", dest="mode", action="store_const", const="browser",
                      help="open the console in the default browser")
    mode.add_argument("--headless", dest="mode", action="store_const", const="headless",
                      help="serve only; open nothing")
    ap.add_argument("--theater", default=None, choices=theaters.ids(),
                    help=f"AO preset (default: {theaters.DEFAULT_THEATER_ID})")
    ap.add_argument("--port", type=int, default=None,
                    help=f"app port (default {DEFAULT_PORT}; UI, bridge and MCP share it)")
    ap.add_argument("--mcp-port", type=int, default=None,
                    help="also serve the same app on this port (legacy MCP URL, e.g. 8791)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback bind address: 127.0.0.1 (default), localhost or ::1")
    ap.add_argument("--token", default=None,
                    help="API/MCP bearer token (default: $GODSEYE_TOKEN, else random)")
    ap.add_argument("--store", default=None,
                    help="store directory (default: the per-user app data directory)")
    ap.add_argument("--ui-dir", default=None,
                    help="built UI directory (default: $GODSEYE_UI_DIR or gods-eye-view/dist)")
    ap.add_argument("--real", action="store_true",
                    help="connect to a real AirSim on --sim-port instead of the fake")
    ap.add_argument("--sim-port", type=int, default=None,
                    help="AirSim RPC port (default: 41451 with --real, a free port for the fake)")
    ap.add_argument("--no-chat", action="store_true", help="disable the AI analyst")
    ap.add_argument("--model", default=None,
                    help="analyst model id (default: $GODSEYE_CHAT_MODEL, else claude-opus-5)")
    ap.add_argument("--effort", default=None, choices=ANALYST_EFFORTS,
                    help="analyst reasoning effort (default: $GODSEYE_CHAT_EFFORT, else the "
                         "model's default)")
    ap.add_argument("--debug", action="store_true",
                    help="enable the web inspector in --window mode")
    st = ap.add_argument_group("self-test (packaging check; implies --window)")
    st.add_argument("--selftest", action="store_true",
                    help="open the window, check the console loaded (UI mounted, WebGL2, "
                         "/app/config, intel graph, package data), write a JSON verdict "
                         "and exit 0 (pass) or 1 (fail)")
    st.add_argument("--selftest-out", default=None, metavar="PATH",
                    help="write the --selftest verdict here (default: stdout)")
    st.add_argument("--selftest-timeout", type=float, default=SELFTEST_TIMEOUT_S,
                    metavar="S", help=f"--selftest time limit, boot to exit "
                                      f"(default {SELFTEST_TIMEOUT_S:.0f})")
    return ap


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """``build_parser().parse_args`` plus the cross-flag rules."""
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        if args.mode not in (None, "window"):
            ap.error(f"--selftest checks the native window; it cannot run with --{args.mode}")
        if not args.selftest_timeout > 0:
            ap.error("--selftest-timeout must be positive")
        args.mode = "window"
    elif args.selftest_out:
        ap.error("--selftest-out needs --selftest")
    return args


def resolve_store(arg: str | None) -> Path:
    from .host import default_store_dir

    return Path(arg).expanduser().resolve() if arg else default_store_dir()


def write_harness_config(path: Path, *, mcp_url: str, token: str, app_url: str) -> Path:
    """Write the MCP URL + token for external harnesses, mode 0600.

    Also usable directly as a Claude Code ``--mcp-config`` file (``mcpServers``).
    """
    doc = {
        "app_url": app_url,
        "url": mcp_url,
        "token": token,
        "mcpServers": {"godseye-uav": {
            "type": "http", "url": mcp_url,
            "headers": {"Authorization": f"Bearer {token}"}}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    finally:
        if fd != -1:
            os.close(fd)
    os.replace(tmp, path)
    return path


def ensure_airsim_client(env: Mapping[str, str] | None = None) -> str | None:
    """Put the vendored AirSim PythonClient on ``sys.path`` if needed.

    Same order as ``scripts/_airsim_client.sh``: ``$GODSEYE_AIRSIM_PYTHONCLIENT``,
    then ``godseye/../airsim/PythonClient``, then
    ``godseye/.godseye/vendor/airsim/PythonClient``. Returns the directory
    used, or None if ``airsim`` was already importable (or nothing was found;
    ``build_host`` then fails on ``import airsim`` with the real error).
    """
    env = os.environ if env is None else env
    if importlib.util.find_spec("airsim") is not None:
        return None
    gs_root = Path(__file__).resolve().parents[2]
    candidates = [env.get("GODSEYE_AIRSIM_PYTHONCLIENT") or "",
                  str(gs_root.parent / "airsim" / "PythonClient"),
                  str(gs_root / ".godseye" / "vendor" / "airsim" / "PythonClient")]
    for cand in candidates:
        if cand and (Path(cand) / "airsim" / "__init__.py").is_file():
            sys.path.insert(0, str(Path(cand).resolve()))
            return cand
    return None


# ---------------------------------------------------------------------------
# frozen .app output: a Finder/LaunchServices launch has no terminal
# ---------------------------------------------------------------------------

def output_discarded(fd: int = 1) -> bool:
    """True if ``fd`` is closed or is ``/dev/null`` (a LaunchServices launch)."""
    try:
        st = os.fstat(fd)
    except OSError:
        return True
    try:
        null = os.stat(os.devnull)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == null.st_rdev


def open_app_log(path: Path, *, max_bytes: int = LOG_MAX_BYTES) -> int:
    """Open the app log for appending (mode 0600) and return its fd.

    A log over ``max_bytes`` is rotated to ``<name>.1`` first (one
    generation), so a long-lived install does not grow it forever.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def redirect_output_to_log(log_path: Path) -> Path | None:
    """Frozen app with discarded stdout: send fds 1 and 2 (Python's prints,
    tracebacks and anything a child inherits) to ``log_path``. Returns the
    path, or None when nothing was redirected (a terminal launch keeps its
    output).
    """
    from .host import is_frozen

    if not is_frozen() or not (output_discarded(1) or sys.stdout is None):
        return None
    try:
        fd = open_app_log(log_path)
    except OSError:
        return None
    for target in (1, 2):
        os.dup2(fd, target)
    os.close(fd)
    sys.stdout = open(1, "w", encoding="utf-8", errors="replace",  # noqa: SIM115
                      buffering=1, closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", errors="replace",  # noqa: SIM115
                      buffering=1, closefd=False)
    print(f"\n{PREFIX} ---- launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"pid {os.getpid()} ----", flush=True)
    return log_path


# ---------------------------------------------------------------------------
# --selftest: prove the window really shows a working console
# ---------------------------------------------------------------------------

_SELFTEST_KEY = "__eitsSelftest"
SELFTEST_POLL_JS = f"window.{_SELFTEST_KEY} || null"

# Runs inside the page (pywebview evaluate_js). It only gathers FACTS into
# window.__eitsSelftest; evaluate_selftest() judges them, so the verdict
# logic is plain Python with tests. The token is used for one fetch and never
# copied into the report.
_SELFTEST_JS = r"""
(function (budgetMs) {
  var KEY = '__KEY__';
  if (window[KEY]) return true;
  var state = { state: 'running', checks: {}, info: {} };
  window[KEY] = state;
  var errors = [];
  window.addEventListener('error', function (e) {
    errors.push(String((e && e.message) || e));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    errors.push('unhandled rejection: ' + String((r && r.message) || r));
  });
  // The console polls /intel/graph every 2 s; a full resource-timing buffer
  // (Cesium assets) would hide the next poll.
  try { performance.setResourceTimingBufferSize(10000); } catch (e) {}
  var t0 = Date.now();
  var deadline = t0 + budgetMs;
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function waitFor(fn, until) {
    for (;;) {
      try { var v = fn(); if (v) return v; } catch (e) {}
      if (Date.now() >= until) return null;
      await sleep(100);
    }
  }
  function withTimeout(p, ms) {
    return Promise.race([p, new Promise(function (_, reject) {
      setTimeout(function () { reject(new Error('timed out after ' + ms + ' ms')); }, ms);
    })]);
  }
  async function getJson(url, headers) {
    var r = await withTimeout(fetch(url, { headers: headers || {}, cache: 'no-store' }), 8000);
    var body = null;
    try { body = await withTimeout(r.json(), 8000); } catch (e) {}
    return { status: r.status, body: body };
  }
  function consoleFetchedGraph() {
    return performance.getEntriesByType('resource').some(function (e) {
      return e.name.indexOf('/intel/graph') !== -1;
    });
  }
  (async function () {
    var c = state.checks;
    var root = await waitFor(function () {
      return document.querySelector('.ic-root');
    }, deadline - 4000);
    c.console_mounted = { found: !!root, mode: root ? root.getAttribute('data-mode') : null };
    try {
      var gl = document.createElement('canvas').getContext('webgl2');
      var w = { available: !!gl };
      if (gl) {
        w.version = String(gl.getParameter(gl.VERSION));
        var dbg = gl.getExtension('WEBGL_debug_renderer_info');
        w.renderer = String(gl.getParameter(dbg ? dbg.UNMASKED_RENDERER_WEBGL : gl.RENDERER));
      }
      c.webgl2 = w;
    } catch (e) { c.webgl2 = { available: false, error: String(e) }; }
    try {
      var cfg = await getJson('/app/config');
      var b = cfg.body || {};
      c.app_config = { status: cfg.status, app: b.app || null, version: b.version || null,
        ui: b.ui || null, theater: (b.theater && b.theater.id) || null,
        chat_available: !!(b.chat && b.chat.available),
        chat_reason: (b.chat && b.chat.reason) || null };
    } catch (e) { c.app_config = { status: 0, error: String(e) }; }
    var seen = await waitFor(consoleFetchedGraph, deadline - 1500);
    try {
      var token = (window.__GODSEYE__ && window.__GODSEYE__.token) || '';
      var g = await getJson('/intel/graph?scope=theater', { Authorization: 'Bearer ' + token });
      var gb = g.body || {};
      c.intel_graph = { status: g.status, schema: gb.schema || null,
        nodes: Array.isArray(gb.nodes) ? gb.nodes.length : null,
        edges: Array.isArray(gb.edges) ? gb.edges.length : null,
        fetched_by_console: !!seen };
    } catch (e) {
      c.intel_graph = { status: 0, error: String(e), fetched_by_console: !!seen };
    }
    state.info = { origin: location.origin, title: document.title,
      user_agent: navigator.userAgent, elapsed_ms: Date.now() - t0,
      errors: errors.slice(0, 10) };
    state.state = 'done';
  })().catch(function (e) { state.error = String(e); state.state = 'done'; });
  return true;
})(__BUDGET_MS__)
"""


def selftest_script(budget_ms: int) -> str:
    """The page-side check, given the milliseconds it may take."""
    return (_SELFTEST_JS.replace("__KEY__", _SELFTEST_KEY)
            .replace("__BUDGET_MS__", str(max(1000, int(budget_ms)))))


def _judge(name: str, raw: dict) -> bool:
    if name == "console_mounted":
        return raw.get("found") is True
    if name == "webgl2":
        return raw.get("available") is True
    if name == "app_config":
        return raw.get("status") == 200 and raw.get("app") == APP_NAME
    if name == "intel_graph":
        nodes = raw.get("nodes")
        return (raw.get("status") == 200 and isinstance(nodes, int) and nodes > 0
                and raw.get("fetched_by_console") is True)
    if name == "package_data":
        return (raw.get("ui_built") is True and raw.get("analyst_prompt") is True
                and raw.get("geoid_grid") is True)
    return False


def evaluate_selftest(report: Mapping | None, *, package: Mapping | None = None,
                      error: str | None = None, timed_out: bool = False) -> dict:
    """Judge the page's facts (``report``) and the Python-side ``package``
    facts. Pure. Every check in ``SELFTEST_CHECKS`` appears in the verdict;
    one that never ran is ``{"ok": false, "error": "not run"}``.
    """
    raw_checks = (report or {}).get("checks") if isinstance(report, Mapping) else None
    raw_checks = raw_checks if isinstance(raw_checks, Mapping) else {}
    sources = {**{n: raw_checks.get(n) for n in SELFTEST_PAGE_CHECKS}, "package_data": package}
    checks: dict[str, dict] = {}
    for name in SELFTEST_CHECKS:
        raw = sources[name]
        if isinstance(raw, Mapping):
            checks[name] = {**raw, "ok": _judge(name, raw)}
        else:
            checks[name] = {"ok": False, "error": "not run"}
    failures = [n for n in SELFTEST_CHECKS if not checks[n]["ok"]]
    page_error = report.get("error") if isinstance(report, Mapping) else None
    errors = [e for e in (error, page_error and f"page: {page_error}") if e]
    if timed_out:
        failures.append("timeout")
    info = report.get("info") if isinstance(report, Mapping) else None
    return {"ok": not failures, "failures": failures, "checks": checks,
            "info": dict(info) if isinstance(info, Mapping) else {},
            "error": "; ".join(errors) or None}


def selftest_package_facts(host) -> dict:
    """Python-side facts: what a frozen build most easily loses."""
    from importlib.resources import files

    from . import geo

    try:
        prompt = files("godseye_uav").joinpath("analyst_prompt.md").is_file()
    except Exception:  # noqa: BLE001 - reported as missing
        prompt = False
    source = geo.geoid_source()
    return {"ui_built": bool(host.ui_built), "analyst_prompt": bool(prompt),
            "geoid_source": source,
            "geoid_grid": bool(source) and source == getattr(geo, "_SOURCE_GRID", None)}


def selftest_app_facts(host) -> dict:
    """Where this run came from (no token)."""
    from .host import app_version, frozen_cli_path, is_frozen

    chat = host.chat_summary()
    return {"frozen": is_frozen(), "version": app_version(), "url": host.url,
            "executable": sys.executable, "python": sys.version.split()[0],
            "ui_dir": str(host.ui_dir), "store_dir": str(host.store_dir),
            "analyst": {"available": bool(chat.get("available")),
                        "reason": chat.get("reason"),
                        "cli_path": host.config.cli_path or frozen_cli_path()}}


def emit_selftest_result(out: Path | None, result: Mapping) -> None:
    """Write the verdict atomically to ``out``, or print it to stdout."""
    text = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    if out is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)


@dataclass
class Selftest:
    """One --selftest run: its clock, its output and its (single) verdict."""

    out: Path | None
    timeout_s: float = SELFTEST_TIMEOUT_S
    started: float = field(default_factory=time.monotonic)
    result: dict | None = None
    app: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def reserve_s(self) -> float:
        """Kept back from the checks for the shutdown after them."""
        return min(8.0, self.timeout_s / 4)

    def check_budget_s(self) -> float:
        """Seconds the checks may still take."""
        return self.started + self.timeout_s - self.reserve_s - time.monotonic()

    def finish(self, result: Mapping) -> dict:
        """Record and emit the verdict once; later calls return the first."""
        with self._lock:
            if self.result is None:
                self.result = {**result, "app": {**self.app, **result.get("app", {})},
                               "elapsed_s": round(time.monotonic() - self.started, 2)}
                emit_selftest_result(self.out, self.result)
            return self.result

    def fail(self, failure: str, error: str) -> dict:
        """A verdict for a run that never reached (or never finished) the
        checks. Once a verdict exists this does nothing.
        """
        if self.result is not None:
            return self.result
        verdict = evaluate_selftest(None, error=error)
        verdict["failures"].insert(0, failure)
        return self.finish(verdict)

    def amend(self, failure: str, error: str) -> dict:
        """Turn an existing verdict into a failure (e.g. the app then would
        not quit) and re-emit it; without a verdict, same as ``fail``.
        """
        with self._lock:
            if self.result is not None:
                errors = [e for e in (self.result.get("error"), error) if e]
                self.result = {**self.result, "ok": False,
                               "failures": [*self.result.get("failures", []), failure],
                               "error": "; ".join(errors)}
                emit_selftest_result(self.out, self.result)
                return self.result
        return self.fail(failure, error)

    def exit_code(self) -> int:
        return 0 if self.result and self.result.get("ok") else 1


def run_selftest(window, selftest: Selftest, *, close: Callable[[], None],
                 package: Callable[[], dict] = dict) -> dict:
    """Drive the page-side check in ``window`` and record the verdict; then
    close the window (which ends the run). Call on its own thread: pywebview's
    ``evaluate_js`` blocks until the page answers.
    """
    report, error, timed_out = None, None, False
    try:
        if not window.events.loaded.wait(max(0.0, selftest.check_budget_s())):
            timed_out, error = True, "the page did not finish loading"
        else:
            window.evaluate_js(selftest_script(int(selftest.check_budget_s() * 1000) - 500))
            while True:
                got = window.evaluate_js(SELFTEST_POLL_JS)
                if isinstance(got, Mapping) and got.get("state") == "done":
                    report = got
                    break
                if selftest.check_budget_s() <= 0:
                    report = got if isinstance(got, Mapping) else None
                    timed_out, error = True, "the page checks did not finish in time"
                    break
                time.sleep(SELFTEST_POLL_S)
    except Exception as exc:  # noqa: BLE001 - JavascriptException, a closed window, ...
        error = f"{type(exc).__name__}: {exc}"
    try:
        facts = package()
    except Exception as exc:  # noqa: BLE001
        facts = None
        error = "; ".join(filter(None, [error, f"package facts: {type(exc).__name__}: {exc}"]))
    result = selftest.finish(
        evaluate_selftest(report, package=facts, error=error, timed_out=timed_out))
    close()
    return result


def start_selftest_watchdog(selftest: Selftest, *,
                            exit_fn: Callable[[int], None] = os._exit,
                            on_force: Callable[[], None] | None = reap_cli_children,
                            ) -> threading.Thread:
    """Hard limit for a --selftest run: at ``timeout_s`` after it started,
    record a timeout verdict (unless one exists), reap the analyst CLI and
    exit. A verdict that passed but whose shutdown hung still exits 1: a
    packaged app that cannot quit is a failure too.
    """
    def watch() -> None:
        delay = selftest.started + selftest.timeout_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        limit = f"{selftest.timeout_s:.0f} s"
        if selftest.result is not None:
            selftest.amend("shutdown", f"the app did not exit within {limit}")
        else:
            selftest.fail("timeout", f"the self-test did not finish within {limit}")
        _say(f"self-test: still running after {limit}; exiting now")
        if on_force is not None:
            with contextlib.suppress(Exception):
                on_force()
        exit_fn(1)

    t = threading.Thread(target=watch, name="selftest-watchdog", daemon=True)
    t.start()
    return t


def _say(msg: str) -> None:
    print(f"{PREFIX} {msg}", flush=True)


def _describe(host, *, token_source: str, harness_file: Path | None) -> None:
    t = host.theater
    _say(f"theater  : {t.id} ({t.place}), {t.label}")
    _say(f"app      : {host.url}")
    mcp = host.mcp_url
    if host.mcp_port:
        mcp += f"  (also http://127.0.0.1:{host.mcp_port}/mcp)"
    _say(f"MCP      : {mcp}")
    # The token is never printed: stdout is often captured to a log, and a
    # bearer token there would authenticate /control, /mcp and /chat without
    # the console's approval slips. Harnesses read it from the 0600 file; the
    # UI gets it through the injected index.html.
    where = f"; harness config: {harness_file} (0600)" if harness_file else ""
    origin = "generated for this launch" if token_source == "generated" else f"from {token_source}"
    _say(f"token    : {origin}, not printed{where}")
    _say(f"store    : {host.store_dir}")
    ui = "built" if host.ui_built else f"NOT BUILT (looked in {host.ui_dir})"
    _say(f"ui       : {ui}")
    chat = host.chat_summary()
    # The provider is named by its catalog label; its key is never printed.
    provider = chat.get("provider") if isinstance(chat.get("provider"), dict) else {}
    via = f" via {provider['label']}" if provider.get("label") else ""
    if chat.get("available"):
        _say(f"analyst  : available (model {chat.get('model')}{via})")
    else:
        _say(f"analyst  : unavailable ({chat.get('reason', 'unknown')}){via}")


def _config_from_args(args, token: str, *, llm_env: Mapping[str, str] | None = None):
    from .host import HostConfig, default_ui_dir

    sim_backend = "real" if args.real else "fake"
    sim_port = args.sim_port if args.sim_port is not None else (41451 if args.real else 0)
    return HostConfig(
        theater=args.theater, sim_backend=sim_backend, sim_port=sim_port,
        port=DEFAULT_PORT if args.port is None else args.port, mcp_port=args.mcp_port,
        host=args.host, token=token, store_dir=resolve_store(args.store),
        ui_dir=Path(args.ui_dir).expanduser().resolve() if args.ui_dir else default_ui_dir(),
        chat=not args.no_chat, model=args.model, effort=args.effort,
        llm_env=dict(llm_env or {}))


def _build(cfg, *, port_explicit: bool, mode: str):
    from .host import build_host, port_in_use

    try:
        return build_host(cfg)
    except OSError as exc:
        if port_in_use(exc) and not port_explicit and mode != "headless":
            # Desktop default port taken by something else: keep going on a
            # free port (localStorage is per-origin, so UI settings will not
            # carry over for this run).
            _say(f"port {cfg.port} is busy; using a free port for this run")
            cfg.port = 0
            return build_host(cfg)
        raise


def _run_headless(host, *, open_browser: bool) -> int:
    from .host import serve

    ready = threading.Event()
    if open_browser:
        def _open() -> None:
            if ready.wait(READY_TIMEOUT_S):
                webbrowser.open(host.url)
        threading.Thread(target=_open, name="open-browser", daemon=True).start()

    # uvicorn re-raises the captured signal after a graceful shutdown with
    # the handler that was active before it; make SIGTERM an exception (not
    # the default kill) so serve()'s cleanup finishes.
    def _term(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _term)
    try:
        asyncio.run(serve(host, ready=ready))
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        host.close()
    return 0


def _run_window(host, *, store_dir: Path, debug: bool,
                selftest: Selftest | None = None) -> int:
    import webview

    from .host import serve

    ready = threading.Event()
    failure: list[BaseException] = []
    closing = threading.Event()
    window = None

    def close_window() -> None:
        closing.set()
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()

    # First, so a Ctrl-C/SIGTERM during startup is a clean stop, not a
    # KeyboardInterrupt that skips the cleanup below. Restored LAST (after the
    # host has shut down and closed): the shutdown can take several seconds
    # with a busy analyst CLI, and a second signal in that window must reach
    # bridge_signals (which reaps the CLI before a forced exit), not the
    # default handler (instant death, orphaned CLI) or a KeyboardInterrupt.
    restore_signals = bridge_signals(close_window, on_force=reap_cli_children)

    def _host_thread() -> None:
        try:
            asyncio.run(serve(host, ready=ready))
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            failure.append(exc)

    worker = threading.Thread(target=_host_thread, name="godseye-host", daemon=True)
    try:
        worker.start()
        while not ready.wait(0.1):
            if not worker.is_alive() or closing.is_set():
                if closing.is_set():
                    return 0
                detail = f"{type(failure[0]).__name__}: {failure[0]}" if failure else "exited"
                _say(f"FATAL: the host did not start ({detail})")
                return 1
        if closing.is_set():
            return 0

        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(
            WINDOW_TITLE, host.url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE, background_color=WINDOW_BACKGROUND,
            text_select=True, zoomable=True)

        def _after_start() -> None:
            # Not a daemon thread: it must end when the window does.
            if selftest is not None:     # a daemon: evaluate_js can block forever
                threading.Thread(
                    target=run_selftest, args=(window, selftest), name="selftest",
                    kwargs={"close": close_window,
                            "package": lambda: selftest_package_facts(host)},
                    daemon=True).start()
            window.events.shown.wait(30)
            _keep_sigint_on_python()
            if closing.is_set():          # a signal arrived before the window existed
                close_window()
            while worker.is_alive() and not window.events.closed.is_set():
                worker.join(0.25)
            if not worker.is_alive() and not window.events.closed.is_set():
                close_window()            # the host died: do not leave a dead window

        storage = store_dir.parent / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(_after_start, private_mode=False, storage_path=str(storage),
                      debug=debug)
    finally:
        try:
            host.request_stop()
            if worker.is_alive():
                worker.join(timeout=15)
            host.close()
        finally:
            restore_signals()
    if failure and not isinstance(failure[0], (KeyboardInterrupt, SystemExit)):
        _say(f"host stopped with {type(failure[0]).__name__}: {failure[0]}")
        return 1
    return 0


def _ignore_signal(signum, frame) -> None:
    """Python-level half of ``bridge_signals``: the work happens on the
    watcher thread, so this must not raise into a GUI callback.
    """


def bridge_signals(on_signal: Callable[[], None],
                   signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
                   *, on_force: Callable[[], None] | None = None,
                   exit_fn: Callable[[int], None] = os._exit,
                   ) -> Callable[[], None]:
    """Route SIGINT/SIGTERM to ``on_signal`` (run on a watcher thread) while
    a GUI loop owns the main thread. Call from the main thread; returns a
    function that restores the previous handlers.

    Python runs signal handlers only between bytecodes on the main thread,
    and a Cocoa run loop rarely hands it any, so a Ctrl-C would sit unhandled
    (PyObjC's Mach-port signals did not fire under pywebview on macOS 26
    either). The C-level handler still writes the signal number to the wakeup
    fd at once; the watcher thread reads it. A second signal forces an exit:
    ``on_force`` runs first (it must reap child processes: the forced exit
    skips atexit), then ``exit_fn(130)``.
    """
    rsock, wsock = socket.socketpair()
    wsock.setblocking(False)
    old_fd = signal.set_wakeup_fd(wsock.fileno(), warn_on_full_buffer=False)
    old = {sig: signal.signal(sig, _ignore_signal) for sig in signals}

    def watch() -> None:
        seen = 0
        while True:
            try:
                data = rsock.recv(64)
            except OSError:
                return
            if not data:
                return
            if any(sig in data for sig in signals):
                seen += 1
                if seen > 1:
                    _say("second interrupt: exiting now")
                    if on_force is not None:
                        with contextlib.suppress(Exception):
                            on_force()
                    exit_fn(130)
                    return
                on_signal()

    watcher = threading.Thread(target=watch, name="signal-bridge", daemon=True)
    watcher.start()

    def restore() -> None:
        signal.set_wakeup_fd(old_fd)
        for sig, handler in old.items():
            signal.signal(sig, handler)
        wsock.close()                     # EOF ends the watcher's recv
        watcher.join(2)
        rsock.close()

    return restore


def _keep_sigint_on_python() -> None:
    """Pywebview (cocoa) re-points SIGINT at a Mach handler that calls
    ``NSApp.terminate`` (exit with no cleanup) just before its run loop
    starts; put SIGINT back on ``bridge_signals`` from the main thread.
    """
    try:
        from PyObjCTools import AppHelper
    except ImportError:  # other GUI backends leave SIGINT alone
        return
    AppHelper.callAfter(signal.signal, signal.SIGINT, _ignore_signal)


def _fatal_window(message: str) -> None:
    """Show a startup failure in a window (a Finder launch has no terminal)."""
    import html

    # Best effort: the terminal message was already printed.
    with contextlib.suppress(Exception):
        import webview
        page = (f"<body style='font:15px -apple-system,system-ui;background:#05070a;"
                f"color:#d8e1ea;padding:24px'><h2>{WINDOW_TITLE} could not start</h2>"
                f"<pre style='white-space:pre-wrap'>{html.escape(message)}</pre></body>")
        webview.create_window(WINDOW_TITLE, html=page, width=720, height=360)
        webview.start()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selftest = None
    if args.selftest:
        out = Path(args.selftest_out).expanduser().resolve() if args.selftest_out else None
        selftest = Selftest(out=out, timeout_s=args.selftest_timeout)
        from .host import is_frozen
        selftest.app = {"frozen": is_frozen(), "executable": sys.executable}
        start_selftest_watchdog(selftest)
    removed = apply_sanitized_env(os.environ)
    # Before anything can spawn the analyst's CLI (BYOK spec §3.3).
    llm_env = capture_llm_env(os.environ)
    token, token_source = resolve_token(args.token, os.environ)
    # The token never lives in our environment: the analyst's CLI inherits it.
    os.environ.pop("GODSEYE_TOKEN", None)
    has_webview = webview_available()
    mode = args.mode or default_mode(os.environ, has_webview=has_webview)
    if mode == "window" and not has_webview:
        if selftest is not None:
            return _selftest_abort(selftest, "window", "pywebview is not installed")
        _say("pywebview is not installed (pip install -e 'godseye[app]'); "
             "opening the browser instead")
        mode = "browser"
    if mode == "window":
        why = window_problem()
        if why:
            if selftest is not None:
                return _selftest_abort(selftest, "window", f"cannot open a window ({why})")
            _say(f"cannot open a window ({why}); opening the browser instead")
            mode = "browser"

    ensure_airsim_client()
    cfg = _config_from_args(args, token, llm_env=llm_env)
    log_file = redirect_output_to_log(cfg.store_dir.parent / "logs" / LOG_NAME)
    if log_file:
        _say(f"output is logged to {log_file}")
    if removed:
        _say(f"launched from a Claude Code session; dropped {len(removed)} "
             f"session variable(s): {', '.join(removed)}")
    if llm_env:
        _say(f"analyst provider settings from the environment (names only): "
             f"{', '.join(sorted(llm_env))}")
    try:
        host = _build(cfg, port_explicit=args.port is not None, mode=mode)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - one readable line, then exit
        msg = f"{type(exc).__name__}: {exc}"
        from .host import port_in_use
        if port_in_use(exc):
            ports = [f"app :{cfg.port}"]
            if cfg.mcp_port:
                ports.append(f"MCP :{cfg.mcp_port}")
            if cfg.sim_port:
                ports.append(f"sim :{cfg.sim_port}")
            msg += (f"\nA port is already in use ({', '.join(ports)}). "
                    "Pass --port / --mcp-port / --sim-port, or stop the other process.")
        _say(f"FATAL: {msg}")
        if selftest is not None:
            return _selftest_abort(selftest, "host", msg)
        if mode == "window":
            _fatal_window(msg)
        return 1

    _announce(host, token_source=token_source)

    if selftest is not None:
        return _run_selftest_window(host, selftest, debug=args.debug)
    if mode == "window":
        try:
            return _run_window(host, store_dir=host.store_dir, debug=args.debug)
        except Exception as exc:  # noqa: BLE001 - the GUI failed despite the probe
            _say(f"cannot open a window ({type(exc).__name__}: {exc}); "
                 "opening the browser instead")
            mode = "browser"
        if host._closed:
            # _run_window served and closed that host; a host serves once
            # (its MCP session manager cannot run twice). Build a fresh one.
            try:
                host = _build(cfg, port_explicit=args.port is not None, mode=mode)
            except Exception as exc:  # noqa: BLE001 - one readable line, then exit
                _say(f"FATAL: {type(exc).__name__}: {exc}")
                return 1
            _announce(host, token_source=token_source)
    _say("Ctrl-C to stop")
    return _run_headless(host, open_browser=(mode == "browser"))


def _selftest_abort(selftest: Selftest, failure: str, error: str) -> int:
    """--selftest could not reach the page: record why, exit 1 (no browser
    fallback, no error window: a self-test must not wait for a human).
    """
    result = selftest.fail(failure, error)
    _say(f"self-test: FAIL ({', '.join(result['failures'])}): {error}")
    return 1


def _run_selftest_window(host, selftest: Selftest, *, debug: bool) -> int:
    selftest.app = {**selftest.app, **selftest_app_facts(host)}
    if not host.ui_built:
        host.close()
        return _selftest_abort(selftest, "ui", f"the UI is not built (looked in {host.ui_dir})")
    try:
        _run_window(host, store_dir=host.store_dir, debug=debug, selftest=selftest)
    except Exception as exc:  # noqa: BLE001 - the verdict names it
        host.close()
        return _selftest_abort(selftest, "window", f"{type(exc).__name__}: {exc}")
    if selftest.result is None:
        return _selftest_abort(selftest, "window", "the window closed before the checks finished")
    result = selftest.result
    verdict = "PASS" if result.get("ok") else f"FAIL ({', '.join(result.get('failures', []))})"
    where = f" -> {selftest.out}" if selftest.out else ""
    _say(f"self-test: {verdict} in {result.get('elapsed_s')} s{where}")
    return selftest.exit_code()


def _announce(host, *, token_source: str) -> None:
    """Write the harness config and print the startup summary."""
    harness = None
    try:
        harness = write_harness_config(host.store_dir.parent / "mcp.json",
                                       mcp_url=host.mcp_url, token=host.token,
                                       app_url=host.url)
    except OSError as exc:
        _say(f"WARNING: could not write the harness config: {exc}")
    _describe(host, token_source=token_source, harness_file=harness)


if __name__ == "__main__":
    sys.exit(main())
