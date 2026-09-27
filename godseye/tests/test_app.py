"""godseye_uav.app: the entry point (argv, env hygiene, paths, modes, the
``--selftest`` verdict, the frozen app's log) and the scripts that launch or
package it (start.sh, the repo-root ``eye-in-the-sky``,
``scripts/build_desktop.sh`` and ``godseye/packaging/macos``).

The packaged app itself is exercised by one opt-in test (it opens a window):
``GODSEYE_TEST_DESKTOP_APP=1 pytest tests/test_app.py -k built_app`` after
``scripts/build_desktop.sh``.

The end-to-end tests run ``python -m godseye_uav.app --headless`` in its own
process, from a cwd outside the repo (as a Finder launch would), on ports from
52600-52799, then stop it with SIGTERM/SIGINT the way start.sh and a terminal
do.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import airsim
import httpx
import pytest
from godseye_uav import app as appmod
from godseye_uav import theaters
from godseye_uav.app import (
    bridge_signals,
    build_parser,
    default_mode,
    display_available,
    resolve_token,
    sanitize_env,
    write_harness_config,
)
from godseye_uav.host import HostConfig, build_host

GS = Path(__file__).resolve().parents[1]           # godseye/
REPO = GS.parent
START_SH = GS / "start.sh"
LAUNCHER = REPO / "eye-in-the-sky"
_PORTS = list(range(52600, 52800))
_rng = random.Random(os.getpid() ^ time.time_ns())


# ---------------------------------------------------------------------------
# env hygiene (contract §2)
# ---------------------------------------------------------------------------

KEEP = {
    "PATH": "/usr/bin", "HOME": "/Users/x",
    "ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
    "AWS_REGION": "us-east-1", "AWS_PROFILE": "p", "GOOGLE_CLOUD_PROJECT": "g",
}
SESSION = {
    "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "5555",
    "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "r",
    "CLAUDE_CODE_SDK_VERSION": "0.2", "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH": "1",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/s", "CLAUDE_EFFORT": "high",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
}


def test_sanitize_env_drops_session_vars_when_launched_from_claude_code():
    env = {**KEEP, **SESSION}
    out = sanitize_env(env)
    assert out == KEEP
    assert env == {**KEEP, **SESSION}                  # input untouched


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"])
def test_either_marker_alone_triggers_the_cleanup(marker):
    env = {**KEEP, marker: "1", "CLAUDE_CODE_SSE_PORT": "1", "ANTHROPIC_BASE_URL": "x"}
    assert sanitize_env(env) == KEEP


def test_outside_a_claude_code_session_nothing_is_touched():
    env = {**KEEP, "ANTHROPIC_BASE_URL": "https://proxy.example",
           "CLAUDE_CODE_SSE_PORT": "1", "CLAUDE_EFFORT": "low"}
    assert sanitize_env(env) == env


def test_apply_sanitized_env_mutates_in_place_and_reports_names_only():
    env = {**KEEP, "CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "secret-value"}
    removed = appmod.apply_sanitized_env(env)
    assert removed == ["CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN"]
    assert env == KEEP
    assert all("secret-value" not in r for r in removed)


# ---------------------------------------------------------------------------
# argv, token, mode
# ---------------------------------------------------------------------------

def test_parser_defaults():
    a = build_parser().parse_args([])
    assert a.mode is None
    assert a.theater is None
    assert a.port is None                              # -> 8780 (explicitness tracked)
    assert a.mcp_port is None
    assert a.host == "127.0.0.1"
    assert a.token is None and a.store is None and a.ui_dir is None
    assert a.real is False and a.sim_port is None
    assert a.no_chat is False and a.model is None and a.debug is False
    assert a.effort is None
    assert appmod.DEFAULT_PORT == 8780


@pytest.mark.parametrize("flag", ["--window", "--browser", "--headless"])
def test_mode_flags(flag):
    assert build_parser().parse_args([flag]).mode == flag[2:]


def test_mode_flags_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--window", "--headless"])
    capsys.readouterr()


def test_theater_choices_come_from_the_table(capsys):
    assert build_parser().parse_args(["--theater", "iran-isfahan"]).theater == "iran-isfahan"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--theater", "atlantis"])
    assert "iran-isfahan" in capsys.readouterr().err
    assert set(theaters.ids()) >= {"default", "iran-isfahan"}


def test_effort_flag_reaches_the_host_config(capsys):
    from godseye_uav import chat

    assert appmod.ANALYST_EFFORTS == chat.EFFORT_LEVELS
    cfg = appmod._config_from_args(build_parser().parse_args(["--effort", "high"]), "tok")
    assert cfg.effort == "high"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--effort", "ludicrous"])
    capsys.readouterr()


def test_config_from_args(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = appmod._config_from_args(build_parser().parse_args(
        ["--store", "rel/store", "--ui-dir", "rel/ui", "--no-chat", "--model", "m"]), "tok")
    assert fake.sim_backend == "fake" and fake.sim_port == 0     # a free port
    assert fake.port == 8780 and fake.mcp_port is None
    assert fake.store_dir == (tmp_path / "rel" / "store").resolve()
    assert fake.ui_dir == (tmp_path / "rel" / "ui").resolve()
    assert fake.chat is False and fake.model == "m" and fake.token == "tok"
    real = appmod._config_from_args(build_parser().parse_args(
        ["--real", "--port", "9000", "--mcp-port", "8791"]), "tok")
    assert real.sim_backend == "real" and real.sim_port == 41451
    assert real.port == 9000 and real.mcp_port == 8791
    assert real.store_dir.is_absolute() and real.ui_dir.is_absolute()


def test_token_precedence():
    assert resolve_token("given", {"GODSEYE_TOKEN": "env"}) == ("given", "--token")
    assert resolve_token(None, {"GODSEYE_TOKEN": "env"}) == ("env", "GODSEYE_TOKEN")
    tok, src = resolve_token(None, {})
    assert src == "generated" and len(tok) >= 24
    assert resolve_token(None, {})[0] != tok


@pytest.mark.parametrize("env,platform,webview,mode", [
    ({}, "darwin", True, "window"),
    ({}, "darwin", False, "browser"),
    ({"SSH_CONNECTION": "1 2 3 4"}, "darwin", True, "browser"),
    ({"CI": "true"}, "darwin", True, "browser"),
    ({"DISPLAY": ":0"}, "linux", True, "window"),
    ({}, "linux", True, "browser"),
    ({}, "win32", True, "window"),
])
def test_default_mode(env, platform, webview, mode):
    assert default_mode(env, has_webview=webview, platform=platform) == mode


def test_display_available():
    assert display_available({}, "darwin") is True
    assert display_available({"SSH_TTY": "/dev/ttys001"}, "darwin") is False
    assert display_available({"WAYLAND_DISPLAY": "wayland-0"}, "linux") is True
    assert display_available({}, "linux") is False


# ---------------------------------------------------------------------------
# harness config file
# ---------------------------------------------------------------------------

def test_harness_config_is_0600_and_a_usable_mcp_config(tmp_path):
    path = tmp_path / "EyeInTheSky" / "mcp.json"
    write_harness_config(path, mcp_url="http://127.0.0.1:8780/mcp", token="t0k",
                         app_url="http://127.0.0.1:8780/")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(path.read_text())
    assert doc["url"] == "http://127.0.0.1:8780/mcp" and doc["token"] == "t0k"
    srv = doc["mcpServers"]["godseye-uav"]
    assert srv == {"type": "http", "url": "http://127.0.0.1:8780/mcp",
                   "headers": {"Authorization": "Bearer t0k"}}
    assert [p.name for p in path.parent.iterdir()] == ["mcp.json"]   # no temp left over


def test_harness_config_tightens_an_existing_loose_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{}")
    path.chmod(0o644)
    write_harness_config(path, mcp_url="u", token="t", app_url="a")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["token"] == "t"


@pytest.mark.parametrize("source", ["generated", "--token", "$GODSEYE_TOKEN"])
@pytest.mark.parametrize("harness", [True, False])
def test_the_startup_banner_never_prints_the_token(tmp_path, capsys, source, harness):
    secret = f"secret-{_rng.randrange(10**12)}-token"
    host = types.SimpleNamespace(
        theater=types.SimpleNamespace(id="default", place="Redmond", label="AirSim default"),
        url="http://127.0.0.1:1/", mcp_url="http://127.0.0.1:1/mcp", mcp_port=None,
        token=secret, store_dir=tmp_path / "store", ui_built=True, ui_dir=tmp_path,
        chat_summary=lambda: {"available": False, "reason": "off"})
    where = tmp_path / "mcp.json" if harness else None
    appmod._describe(host, token_source=source, harness_file=where)
    out = capsys.readouterr().out
    assert secret not in out
    line = next(ln for ln in out.splitlines() if "token    :" in ln)
    assert "not printed" in line
    assert ("generated for this launch" in line) == (source == "generated")
    assert (f"harness config: {where} (0600)" in line) == harness


def test_airsim_client_resolution_is_a_noop_when_importable():
    assert appmod.ensure_airsim_client() is None


def test_optional_gui_and_agent_sdks_are_not_imported_at_module_load():
    """Contract §0.9: the package works without claude_agent_sdk / webview."""
    code = ("import sys, godseye_uav.app, godseye_uav.host; "
            "print(sorted(m for m in ('webview', 'claude_agent_sdk', 'airsim') "
            "if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], env=_child_env(), cwd=str(GS),
                         capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ---------------------------------------------------------------------------
# end to end: python -m godseye_uav.app --headless
# ---------------------------------------------------------------------------

def _child_env(**extra: str) -> dict:
    # Start from an environment with no Claude Code session in it (this suite
    # may itself run inside one), then add exactly what the test asks for.
    env = {k: v for k, v in sanitize_env({**os.environ, "CLAUDECODE": ""}).items()
           if k not in ("GODSEYE_TOKEN", "PYTHONPATH")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(GS / "mcp"), str(Path(airsim.__file__).resolve().parents[1])])
    env.update(extra)
    return env


class AppProc:
    def __init__(self, tmp: Path, args: list[str], env: dict) -> None:
        self.tmp = tmp
        self.log = tmp / "app.log"
        self.port = None
        last = ""
        for _ in range(8):
            port, mcp_port = _rng.sample(_PORTS, 2)
            argv = [sys.executable, "-m", "godseye_uav.app", "--headless",
                    "--port", str(port), "--mcp-port", str(mcp_port),
                    "--store", str(tmp / "data" / "store"), "--ui-dir", str(tmp / "ui"),
                    "--no-chat", *args]
            with open(self.log, "w") as fh:
                self.proc = subprocess.Popen(argv, cwd=str(tmp), env=env, stdout=fh,
                                             stderr=subprocess.STDOUT, text=True)
            if self._wait_ready(port):
                self.port, self.mcp_port = port, mcp_port
                return
            last = self.output()
            self.kill()
        raise AssertionError(f"app never came up:\n{last}")

    def _wait_ready(self, port: int) -> bool:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    return "Ctrl-C to stop" in self.output()
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        return False

    def output(self) -> str:
        return self.log.read_text(errors="replace")

    def stop(self, sig: int) -> int:
        self.proc.send_signal(sig)
        return self.proc.wait(timeout=30)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


def test_headless_app_with_a_given_token_end_to_end(tmp_path):
    token = f"given-{os.getpid()}-{_rng.randrange(10**9)}"
    env = _child_env(CLAUDECODE="1", CLAUDE_CODE_SSE_PORT="1")
    app = AppProc(tmp_path, ["--token", token], env)
    try:
        auth = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{app.port}"
        assert httpx.get(f"{base}/snapshot", timeout=10).status_code == 401
        assert httpx.get(f"{base}/snapshot", headers=auth, timeout=10).status_code == 200
        cfg = httpx.get(f"{base}/app/config", timeout=10).json()
        assert cfg["ui"] == "missing" and cfg["chat"]["available"] is False
        mcp = httpx.post(
            f"http://127.0.0.1:{app.mcp_port}/mcp", timeout=20,
            headers={**auth, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert mcp.status_code == 200
        out = app.output()
        assert token not in out                          # a given token is never printed
        assert "from --token, not printed" in out
        assert f"http://127.0.0.1:{app.port}/" in out
        assert f"http://127.0.0.1:{app.mcp_port}/mcp" in out
        assert "dropped 2 session variable(s): CLAUDECODE, CLAUDE_CODE_SSE_PORT" in out
        harness = tmp_path / "data" / "mcp.json"
        assert stat.S_IMODE(harness.stat().st_mode) == 0o600
        assert json.loads(harness.read_text())["token"] == token
        assert (tmp_path / "data" / "store").is_dir()
        assert app.stop(signal.SIGTERM) == 0             # start.sh / demo_laptop.sh stop it so
    finally:
        app.kill()


def test_headless_app_generates_and_records_its_token_without_printing_it(tmp_path):
    app = AppProc(tmp_path, [], _child_env())
    try:
        out = app.output()
        m = re.search(r"token\s+:\s+generated for this launch, not printed; harness config: "
                      r"(.+?) \(0600\)", out)
        assert m, out
        where = Path(m.group(1))
        assert where == tmp_path / "data" / "mcp.json"
        token = json.loads(where.read_text())["token"]
        assert len(token) >= 24
        assert token not in out                          # a log of stdout holds no bearer token
        r = httpx.get(f"http://127.0.0.1:{app.port}/snapshot",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert r.status_code == 200
        assert app.stop(signal.SIGINT) == 0              # Ctrl-C
    finally:
        app.kill()


def test_a_busy_default_port_falls_back_for_window_and_browser_only(tmp_path):
    import socket
    blocker = socket.socket()
    try:
        busy = next(p for p in _rng.sample(_PORTS, 40) if _try_bind(blocker, p))
        blocker.listen(1)
        sim = next(p for p in _rng.sample(_PORTS, 40) if p != busy)

        def cfg(sub: str) -> HostConfig:
            return HostConfig(theater=None, sim_port=sim, port=busy, token="t",
                              store_dir=tmp_path / sub / "store", ui_dir=None, chat=False)

        for mode, explicit in (("headless", False), ("browser", True)):
            with pytest.raises(OSError):
                appmod._build(cfg(mode), port_explicit=explicit, mode=mode)
        host = appmod._build(cfg("fallback"), port_explicit=False, mode="browser")
        try:
            assert host.port != busy and host.port > 0
            assert host.url == f"http://127.0.0.1:{host.port}/"
        finally:
            host.close()
    finally:
        blocker.close()


def _try_bind(sock, port: int) -> bool:
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# --window orchestration (a fake pywebview; no GUI in the test run)
# ---------------------------------------------------------------------------

def test_a_signal_during_window_startup_is_a_clean_stop(tmp_path, fake_window, monkeypatch):
    """Ctrl-C before the window exists must not escape as KeyboardInterrupt."""
    host = _boot_host(tmp_path)
    real_serve = __import__("godseye_uav.host", fromlist=["serve"]).serve

    async def slow_serve(h, *, ready=None):
        os.kill(os.getpid(), signal.SIGINT)          # arrives while the host boots
        await real_serve(h, ready=ready)

    monkeypatch.setattr("godseye_uav.host.serve", slow_serve)
    assert appmod._run_window(host, store_dir=host.store_dir, debug=False) == 0
    if "window" in fake_window:                      # the host may win the race
        assert fake_window["window"].destroyed >= 1
        assert fake_window["after_start_ended"]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    with pytest.raises(httpx.HTTPError):
        httpx.get(host.url, timeout=2)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_bridge_signals_routes_a_signal_to_a_thread_and_restores(sig):
    """While Cocoa owns the main thread, Python-level handlers barely run;
    the wakeup fd is what actually reaches us.
    """
    before = signal.getsignal(sig)
    hit = threading.Event()
    restore = bridge_signals(hit.set)
    try:
        os.kill(os.getpid(), sig)
        assert hit.wait(5), "the signal never reached the watcher thread"
    finally:
        restore()
    assert signal.getsignal(sig) is before


class _FakeWindow:
    def __init__(self, record: dict | None = None) -> None:
        self.events = types.SimpleNamespace(shown=threading.Event(), closed=threading.Event(),
                                            loaded=threading.Event())
        self.destroyed = 0
        self._record = record if record is not None else {}

    def destroy(self) -> None:
        self.destroyed += 1
        self.events.closed.set()

    def evaluate_js(self, script: str):      # the "page": whatever the test plays
        page = self._record.get("page")
        if page is None:
            raise RuntimeError("this fake window has no page")
        return page(script)


def _fake_webview(record: dict) -> types.ModuleType:
    mod = types.ModuleType("webview")
    mod.settings = {}

    def create_window(title, url=None, **kw):
        record.update(title=title, url=url, kw=kw, window=_FakeWindow(record))
        return record["window"]

    def start(func=None, **kw):              # plays the GUI loop on the main thread
        record["start_kw"] = kw
        win = record["window"]
        t = threading.Thread(target=func)
        t.start()
        win.events.shown.set()
        if record.get("page_loads", True):
            win.events.loaded.set()
        assert win.events.closed.wait(30), "the window was never closed"
        t.join(10)
        record["after_start_ended"] = not t.is_alive()

    mod.create_window = create_window
    mod.start = start
    return mod


def _boot_host(tmp: Path):
    last: OSError | None = None
    for _ in range(10):
        port, sim = _rng.sample(_PORTS, 2)
        try:
            return build_host(HostConfig(theater=None, sim_port=sim, port=port,
                                         token="win-token", store_dir=tmp / "data" / "store",
                                         ui_dir=None, chat=False))
        except OSError as exc:
            last = exc
    raise last


@pytest.fixture
def fake_window(monkeypatch):
    record: dict = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(record))
    monkeypatch.setattr(appmod, "_keep_sigint_on_python", lambda: None)
    return record


def test_window_mode_serves_the_app_and_a_signal_closes_it_cleanly(tmp_path, fake_window):
    host = _boot_host(tmp_path)

    def operator() -> None:                  # wait for the window and page, then Ctrl-C
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            win = fake_window.get("window")
            if win is not None and win.events.shown.is_set():
                with contextlib.suppress(httpx.HTTPError):
                    if httpx.get(host.url, timeout=2).status_code == 200:
                        fake_window["page_ok"] = True
                        break
            time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=operator, daemon=True).start()
    rc = appmod._run_window(host, store_dir=host.store_dir, debug=False)
    assert rc == 0
    assert fake_window.get("page_ok")
    assert fake_window["title"] == "Eye in the Sky"
    assert fake_window["url"] == host.url
    kw = fake_window["kw"]
    assert (kw["width"], kw["height"]) == (1440, 900)
    assert kw["min_size"] == (1024, 700)
    start = fake_window["start_kw"]
    assert start["private_mode"] is False            # keeps localStorage
    assert Path(start["storage_path"]) == tmp_path / "data" / "webview"
    assert fake_window["window"].destroyed >= 1
    assert fake_window["after_start_ended"]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    with pytest.raises(httpx.HTTPError):
        httpx.get(host.url, timeout=2)


def test_window_closes_itself_when_the_host_stops(tmp_path, fake_window):
    host = _boot_host(tmp_path)

    def crash() -> None:
        while not host.started:
            time.sleep(0.05)
        host.request_stop()

    threading.Thread(target=crash, daemon=True).start()
    assert appmod._run_window(host, store_dir=host.store_dir, debug=False) == 0
    assert fake_window["window"].destroyed >= 1
    assert fake_window["after_start_ended"]


# ---------------------------------------------------------------------------
# the scripts that launch it
# ---------------------------------------------------------------------------

def test_start_sh_runs_the_single_process_host_on_the_legacy_ports():
    src = START_SH.read_text(encoding="utf-8")
    assert "-m godseye_uav.app --headless" in src
    assert "-m godseye_uav.launch" not in src.split("PYTHEATER")[-1]
    for flag in ('--port "$BRIDGE_PORT"', '--mcp-port "$MCP_PORT"',
                 '--sim-port "$AIRSIM_PORT"', '--store "$STORE"',
                 '--theater "$THEATER"', "$REAL_FLAG"):
        assert flag in src, flag
    # The token reaches the app through its environment, never its argv (ps).
    assert 'GODSEYE_TOKEN="$TOKEN" "$PY" -m godseye_uav.app --headless' in src
    assert '--token "$TOKEN"' not in src
    assert 'STORE="${STORE:-$ROOT/.godseye/store}"' in src       # absolute, as before
    assert "npm run dev" in src                                   # the vite UI stays
    for line in ("MCP      : http://127.0.0.1:$MCP_PORT/mcp",
                 "bridge   : http://127.0.0.1:$BRIDGE_PORT/snapshot",
                 "UI       : http://localhost:$UI_PORT"):
        assert line in src, line
    subprocess.run(["bash", "-n", str(START_SH)], check=True)


def test_repo_root_launcher_is_executable_and_forwards_to_the_app():
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(LAUNCHER)], check=True)
    src = LAUNCHER.read_text(encoding="utf-8")
    assert "scripts/_airsim_client.sh" in src
    assert "npm run build" in src and "--no-build" in src
    assert "-m godseye_uav.app" in src
    out = subprocess.run([str(LAUNCHER), "--no-build", "--help"], capture_output=True,
                         text=True, timeout=60, env=_child_env(), cwd=str(REPO), check=False)
    assert out.returncode == 0, out.stderr
    for flag in ("--window", "--browser", "--headless", "--no-chat", "--mcp-port"):
        assert flag in out.stdout


# ---------------------------------------------------------------------------
# review-finding regressions: --window shutdown and GUI fallback
# ---------------------------------------------------------------------------

def test_window_mode_keeps_signals_bridged_until_the_host_has_shut_down(
        tmp_path, fake_window, monkeypatch):
    """Review finding: restore_signals() ran BEFORE the (5 s+) host shutdown,
    so a second SIGTERM in that window hit the default handler: instant death,
    no cleanup, an orphaned Claude CLI still working. The handlers must stay
    bridged until the host has stopped and closed."""
    host = _boot_host(tmp_path)
    real_serve = __import__("godseye_uav.host", fromlist=["serve"]).serve
    seen: dict = {}

    async def serve_then_slow_shutdown(h, *, ready=None):
        try:
            await real_serve(h, ready=ready)
        finally:
            seen["closed_before_restore"] = h._closed
            seen["sigterm"] = signal.getsignal(signal.SIGTERM)
            seen["sigint"] = signal.getsignal(signal.SIGINT)

    monkeypatch.setattr("godseye_uav.host.serve", serve_then_slow_shutdown)
    before = signal.getsignal(signal.SIGTERM)

    def user_closes_the_window() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            win = fake_window.get("window")
            if win is not None and win.events.shown.is_set():
                win.destroy()
                return
            time.sleep(0.05)

    threading.Thread(target=user_closes_the_window, daemon=True).start()
    assert appmod._run_window(host, store_dir=host.store_dir, debug=False) == 0
    assert seen["sigterm"] is appmod._ignore_signal, seen      # still bridged during shutdown
    assert seen["sigint"] is appmod._ignore_signal, seen
    assert seen["closed_before_restore"] is True
    assert signal.getsignal(signal.SIGTERM) is before           # restored at the very end


def test_a_second_signal_reaps_cli_children_before_the_forced_exit():
    calls: list = []
    first = threading.Event()
    exited = threading.Event()

    def on_signal():
        calls.append("close")
        first.set()

    def on_force():
        calls.append("reap")

    def exit_fn(code):
        calls.append(("exit", code))
        exited.set()

    restore = bridge_signals(on_signal, on_force=on_force, exit_fn=exit_fn)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        assert first.wait(5)
        os.kill(os.getpid(), signal.SIGTERM)
        assert exited.wait(5), calls
    finally:
        restore()
    assert calls == ["close", "reap", ("exit", 130)]


def test_reap_cli_children_runs_the_sdks_own_reaper(monkeypatch):
    hit: list = []
    mod = types.ModuleType("claude_agent_sdk._internal.transport.subprocess_cli")
    mod._kill_active_children = lambda: hit.append(True)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    appmod.reap_cli_children()
    assert hit == [True]
    monkeypatch.delitem(sys.modules, mod.__name__)
    appmod.reap_cli_children()                                   # not loaded: a no-op


def test_window_problem_names_a_gui_backend_that_cannot_load():
    """pywebview raises WebViewException (not ImportError) when no GUI backend
    loads; the probe catches any exception and says why."""
    class WebViewException(Exception):
        pass

    broken = types.ModuleType("webview")

    def initialize():
        raise WebViewException("You must have either PyObjC or Qt installed")

    broken.initialize = initialize
    assert appmod.window_problem(broken).startswith("WebViewException: You must have")
    fine = types.ModuleType("webview")
    fine.initialize = lambda: None
    assert appmod.window_problem(fine) is None


def _main_argv(tmp_path: Path) -> list[str]:
    port, sim = _rng.sample(_PORTS, 2)
    return ["--window", "--port", str(port), "--sim-port", str(sim), "--token", "fallback-t",
            "--store", str(tmp_path / "data" / "store"), "--ui-dir", str(tmp_path / "ui"),
            "--no-chat"]


@pytest.fixture
def main_env(monkeypatch):
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "GODSEYE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(appmod, "webview_available", lambda: True)
    monkeypatch.setattr(appmod, "_keep_sigint_on_python", lambda: None)
    served: list = []

    def fake_headless(host, *, open_browser):
        served.append({"host": host, "open_browser": open_browser,
                       "closed": host._closed, "started": host._uvicorn is not None})
        host.close()
        return 0

    monkeypatch.setattr(appmod, "_run_headless", fake_headless)
    return served


def test_a_window_that_cannot_open_falls_back_to_the_browser_before_serving(
        tmp_path, monkeypatch, main_env):
    broken = types.ModuleType("webview")

    def initialize():
        raise RuntimeError("no GUI backend")

    broken.initialize = initialize
    broken.start = lambda *a, **kw: pytest.fail("the window must not be started")
    monkeypatch.setitem(sys.modules, "webview", broken)
    assert appmod.main(_main_argv(tmp_path)) == 0
    assert len(main_env) == 1
    run = main_env[0]
    assert run["open_browser"] is True
    assert run["closed"] is False and run["started"] is False   # a fresh, unused host


def test_a_window_that_fails_after_starting_gets_a_fresh_host_for_the_browser(
        tmp_path, monkeypatch, main_env):
    """Review finding: `except ImportError` missed WebViewException, and the
    ImportError path reused the host _run_window had already served and
    closed (uvicorn: '.run() can only be called once', rc 3)."""
    record: dict = {}
    wv = _fake_webview(record)
    wv.initialize = lambda: None                                 # the probe passes ...

    def start(func=None, **kw):                                  # ... the real start fails
        raise RuntimeError("WKWebView could not be created")

    wv.start = start
    monkeypatch.setitem(sys.modules, "webview", wv)
    built: list = []
    real_build = appmod._build

    def spy_build(cfg, **kw):
        h = real_build(cfg, **kw)
        built.append(h)
        return h

    monkeypatch.setattr(appmod, "_build", spy_build)
    assert appmod.main(_main_argv(tmp_path)) == 0
    assert len(built) == 2 and built[0]._closed
    run = main_env[0]
    assert run["host"] is built[1] and run["open_browser"] is True
    assert run["closed"] is False and run["started"] is False


# ---------------------------------------------------------------------------
# --selftest: the packaged app proves it shows a working console
# ---------------------------------------------------------------------------

def _good_report() -> dict:
    return {"state": "done", "checks": {
        "console_mounted": {"found": True, "mode": "orb"},
        "webgl2": {"available": True, "version": "WebGL 2.0", "renderer": "Apple GPU"},
        "app_config": {"status": 200, "app": "eye-in-the-sky", "ui": "built"},
        "intel_graph": {"status": 200, "nodes": 11, "edges": 5, "fetched_by_console": True},
    }, "info": {"origin": "http://127.0.0.1:1", "errors": []}}


GOOD_PACKAGE = {"ui_built": True, "analyst_prompt": True, "geoid_grid": True,
                "geoid_source": "egm96-grid:us_nga_egm96_15.tif"}


def test_selftest_flags_imply_the_window_and_reject_the_other_modes(capsys):
    args = appmod.parse_args(["--selftest", "--selftest-out", "v.json"])
    assert args.mode == "window" and args.selftest_out == "v.json"
    assert args.selftest_timeout == appmod.SELFTEST_TIMEOUT_S == 60.0
    assert appmod.parse_args(["--selftest", "--window"]).mode == "window"
    assert appmod.parse_args([]).selftest is False
    for bad in (["--selftest", "--headless"], ["--selftest", "--browser"],
                ["--selftest-out", "v.json"], ["--selftest", "--selftest-timeout", "0"]):
        with pytest.raises(SystemExit):
            appmod.parse_args(bad)
    assert "--selftest" in capsys.readouterr().err


def test_a_selftest_passes_only_when_every_check_holds():
    verdict = appmod.evaluate_selftest(_good_report(), package=GOOD_PACKAGE)
    assert verdict["ok"] is True and verdict["failures"] == [] and verdict["error"] is None
    assert list(verdict["checks"]) == list(appmod.SELFTEST_CHECKS)
    assert all(c["ok"] for c in verdict["checks"].values())
    assert verdict["info"]["origin"] == "http://127.0.0.1:1"


@pytest.mark.parametrize(("check", "field", "value"), [
    ("console_mounted", "found", False),
    ("webgl2", "available", False),
    ("app_config", "status", 503),
    ("app_config", "app", "something-else"),
    ("intel_graph", "status", 401),
    ("intel_graph", "nodes", 0),
    ("intel_graph", "nodes", None),
    ("intel_graph", "fetched_by_console", False),   # reachable, but the console never asked
])
def test_each_page_fact_can_fail_the_selftest(check, field, value):
    report = _good_report()
    report["checks"][check][field] = value
    verdict = appmod.evaluate_selftest(report, package=GOOD_PACKAGE)
    assert verdict["ok"] is False and verdict["failures"] == [check]
    assert verdict["checks"][check]["ok"] is False


@pytest.mark.parametrize("field", ["ui_built", "analyst_prompt", "geoid_grid"])
def test_a_build_that_lost_its_data_files_fails_the_selftest(field):
    verdict = appmod.evaluate_selftest(_good_report(), package={**GOOD_PACKAGE, field: False})
    assert verdict["failures"] == ["package_data"]


def test_checks_that_never_ran_fail_and_say_so():
    verdict = appmod.evaluate_selftest(None, error="the page did not finish loading",
                                       timed_out=True)
    assert verdict["ok"] is False
    assert verdict["failures"] == [*appmod.SELFTEST_CHECKS, "timeout"]
    assert all(c == {"ok": False, "error": "not run"} for c in verdict["checks"].values())
    assert verdict["error"] == "the page did not finish loading"
    broken = {"state": "done", "error": "TypeError: x", "checks": {"webgl2": "garbage"}}
    verdict = appmod.evaluate_selftest(broken, package=GOOD_PACKAGE)
    assert verdict["error"] == "page: TypeError: x"
    assert "webgl2" in verdict["failures"] and "package_data" not in verdict["failures"]


def test_the_page_script_parses_and_carries_its_budget():
    script = appmod.selftest_script(12_345)
    assert "__KEY__" not in script and "__BUDGET_MS__" not in script
    assert script.rstrip().endswith("(12345)")
    assert appmod.selftest_script(5).rstrip().endswith("(1000)")        # floor
    for needle in (".ic-root", "webgl2", "/app/config", "/intel/graph", "__eitsSelftest",
                   "fetched_by_console", "setResourceTimingBufferSize"):
        assert needle in script, needle
    assert appmod.SELFTEST_POLL_JS == "window.__eitsSelftest || null"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; syntax not checked")
    subprocess.run([node, "-e", "new Function(process.argv[1])", script], check=True,
                   capture_output=True, timeout=30)


def test_a_verdict_is_written_once_and_amended_when_the_app_will_not_quit(tmp_path):
    out = tmp_path / "deep" / "verdict.json"
    st = appmod.Selftest(out=out, timeout_s=30)
    st.app = {"frozen": False}
    first = st.finish(appmod.evaluate_selftest(_good_report(), package=GOOD_PACKAGE))
    assert first["ok"] is True and st.exit_code() == 0
    assert json.loads(out.read_text()) == first
    assert first["app"] == {"frozen": False} and first["elapsed_s"] >= 0
    st.finish({"ok": False, "failures": ["late"]})                    # ignored
    st.fail("timeout", "late")                                        # ignored too
    assert json.loads(out.read_text())["ok"] is True
    amended = st.amend("shutdown", "the app did not exit within 30 s")
    assert amended["ok"] is False and amended["failures"] == ["shutdown"]
    assert json.loads(out.read_text()) == amended and st.exit_code() == 1
    assert not list(out.parent.glob(".*.tmp"))


def test_a_verdict_without_a_file_goes_to_stdout(capsys):
    st = appmod.Selftest(out=None)
    st.fail("window", "no display")
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and printed["failures"][0] == "window"
    assert printed["error"] == "no display"


def test_the_watchdog_records_a_timeout_reaps_and_exits(tmp_path):
    out = tmp_path / "v.json"
    calls: list = []
    exited = threading.Event()

    def exit_fn(code):
        calls.append(("exit", code))
        exited.set()

    st = appmod.Selftest(out=out, timeout_s=0.3)
    appmod.start_selftest_watchdog(st, exit_fn=exit_fn, on_force=lambda: calls.append("reap"))
    assert exited.wait(10)
    assert calls == ["reap", ("exit", 1)]
    verdict = json.loads(out.read_text())
    assert verdict["failures"][0] == "timeout" and "0 s" in verdict["error"]


def test_the_watchdog_fails_a_passed_run_that_cannot_quit(tmp_path):
    out = tmp_path / "v.json"
    exited = threading.Event()
    st = appmod.Selftest(out=out, timeout_s=0.3)
    st.finish(appmod.evaluate_selftest(_good_report(), package=GOOD_PACKAGE))
    appmod.start_selftest_watchdog(st, exit_fn=lambda code: exited.set(), on_force=None)
    assert exited.wait(10)
    verdict = json.loads(out.read_text())
    assert verdict["ok"] is False and verdict["failures"] == ["shutdown"]


class _Page:
    """Plays the page side of pywebview's evaluate_js for run_selftest."""

    def __init__(self, report: dict | None, *, polls_before_done: int = 2) -> None:
        self.report = report
        self.polls_before_done = polls_before_done
        self.started: list[str] = []
        self.polls = 0

    def __call__(self, script: str):
        if script == appmod.SELFTEST_POLL_JS:
            self.polls += 1
            if not self.started:
                return None
            if self.report is None or self.polls <= self.polls_before_done:
                return {"state": "running", "checks": {}}
            return self.report
        self.started.append(script)
        return True


def _window_with(page) -> _FakeWindow:
    win = _FakeWindow({"page": page})
    win.events.shown.set()
    win.events.loaded.set()
    return win


def test_run_selftest_drives_the_page_then_closes_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "SELFTEST_POLL_S", 0.01)
    page = _Page(_good_report())
    win = _window_with(page)
    closed: list = []
    st = appmod.Selftest(out=tmp_path / "v.json", timeout_s=30)
    verdict = appmod.run_selftest(win, st, close=lambda: closed.append(True),
                                  package=lambda: GOOD_PACKAGE)
    assert verdict["ok"] is True and closed == [True]
    assert len(page.started) == 1
    budget_ms = int(page.started[0].rstrip().rsplit("(", 1)[1].rstrip(")"))
    assert 1000 <= budget_ms <= (30 - st.reserve_s) * 1000
    assert json.loads((tmp_path / "v.json").read_text())["ok"] is True


def test_run_selftest_gives_up_on_a_page_that_never_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "SELFTEST_POLL_S", 0.01)
    win = _window_with(_Page(None))
    closed: list = []
    st = appmod.Selftest(out=tmp_path / "v.json", timeout_s=1.2)
    t0 = time.monotonic()
    verdict = appmod.run_selftest(win, st, close=lambda: closed.append(True),
                                  package=lambda: GOOD_PACKAGE)
    assert time.monotonic() - t0 < st.timeout_s                  # the reserve is left for exit
    assert closed == [True]
    assert verdict["failures"] == [*appmod.SELFTEST_PAGE_CHECKS, "timeout"]
    assert verdict["error"] == "the page checks did not finish in time"


def test_run_selftest_reports_a_page_that_never_loads(tmp_path):
    win = _FakeWindow({"page": _Page(_good_report())})          # loaded is never set
    st = appmod.Selftest(out=tmp_path / "v.json", timeout_s=1.2)
    verdict = appmod.run_selftest(win, st, close=lambda: None, package=lambda: GOOD_PACKAGE)
    assert verdict["error"] == "the page did not finish loading"
    assert "timeout" in verdict["failures"]


def test_run_selftest_survives_a_page_that_throws(tmp_path):
    def page(script):
        raise RuntimeError("JavascriptException: boom")

    st = appmod.Selftest(out=tmp_path / "v.json", timeout_s=5)
    verdict = appmod.run_selftest(_window_with(page), st, close=lambda: None,
                                  package=lambda: GOOD_PACKAGE)
    assert verdict["ok"] is False and "boom" in verdict["error"]


def _tiny_ui(tmp_path: Path) -> Path:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text(
        "<!doctype html><html><head><title>t</title>"
        '<script type="module" src="/main.js"></script></head><body></body></html>')
    (ui / "main.js").write_text("")
    return ui


class _HttpPage(_Page):
    """A page whose "fetches" are real HTTP to the running host (the DOM and
    WebGL facts are played); the token comes from the served index, as in
    the real console.
    """

    def __init__(self, host_url: str) -> None:
        super().__init__(None, polls_before_done=0)
        self.host_url = host_url

    def __call__(self, script: str):
        if script != appmod.SELFTEST_POLL_JS and not self.started:
            threading.Thread(target=self._fetch_facts, daemon=True).start()
        return super().__call__(script)

    def _fetch_facts(self) -> None:
        base = self.host_url
        index = httpx.get(base, timeout=10).text
        injected = re.search(r"window\.__GODSEYE__=(\{.*?\})</script>", index)
        token = json.loads(injected.group(1))["token"]
        conf = httpx.get(f"{base}app/config", timeout=10)
        graph = httpx.get(f"{base}intel/graph?scope=theater", timeout=20,
                          headers={"Authorization": f"Bearer {token}"})
        body = graph.json() if graph.status_code == 200 else {}
        self.report = {"state": "done", "info": {}, "checks": {
            "console_mounted": {"found": True},
            "webgl2": {"available": True},
            "app_config": {"status": conf.status_code, "app": conf.json().get("app")},
            "intel_graph": {"status": graph.status_code,
                            "nodes": len(body.get("nodes", [])),
                            "fetched_by_console": True}}}


def test_window_selftest_end_to_end_against_a_live_host(tmp_path, fake_window, monkeypatch):
    """The whole Python side of --selftest with a fake pywebview: the host
    serves while the page is checked, the verdict is written (without the
    token), the window closes and the host shuts down."""
    monkeypatch.setattr(appmod, "SELFTEST_POLL_S", 0.05)
    ui = _tiny_ui(tmp_path)
    last: OSError | None = None
    for _ in range(10):
        port, sim = _rng.sample(_PORTS, 2)
        try:
            host = build_host(HostConfig(theater=None, sim_port=sim, port=port,
                                         token="selftest-secret-token", ui_dir=ui,
                                         store_dir=tmp_path / "data" / "store", chat=False))
            break
        except OSError as exc:
            last = exc
    else:
        raise last
    fake_window["page"] = _HttpPage(host.url)
    out = tmp_path / "verdict.json"
    st = appmod.Selftest(out=out, timeout_s=60)
    rc = appmod._run_selftest_window(host, st, debug=False)
    text = out.read_text()
    verdict = json.loads(text)
    assert rc == 0, verdict
    assert verdict["ok"] is True and verdict["failures"] == []
    assert verdict["checks"]["intel_graph"]["nodes"] > 0
    assert verdict["checks"]["package_data"]["analyst_prompt"] is True
    assert verdict["app"]["url"] == host.url and verdict["app"]["frozen"] is False
    assert "selftest-secret-token" not in text
    assert fake_window["window"].destroyed >= 1
    assert host._closed
    with pytest.raises(httpx.HTTPError):
        httpx.get(host.url, timeout=2)


@pytest.fixture
def no_watchdog(monkeypatch):
    armed: list = []
    monkeypatch.setattr(appmod, "start_selftest_watchdog", lambda st: armed.append(st))
    return armed


def test_main_selftest_without_a_window_fails_fast_and_never_opens_a_browser(
        tmp_path, monkeypatch, main_env, no_watchdog):
    monkeypatch.setattr(appmod, "window_problem", lambda: "no display")
    monkeypatch.setattr(appmod, "_build", lambda *a, **kw: pytest.fail("no host for a "
                                                                        "window-less selftest"))
    out = tmp_path / "v.json"
    rc = appmod.main(["--selftest", "--selftest-out", str(out),
                      "--store", str(tmp_path / "data" / "store")])
    assert rc == 1 and len(no_watchdog) == 1 and main_env == []
    verdict = json.loads(out.read_text())
    assert verdict["failures"][0] == "window" and "no display" in verdict["error"]


def test_main_selftest_with_no_ui_built_fails_without_opening_the_window(
        tmp_path, monkeypatch, main_env, no_watchdog):
    monkeypatch.setattr(appmod, "window_problem", lambda: None)
    monkeypatch.setattr(appmod, "_run_window",
                        lambda *a, **kw: pytest.fail("the window must not open"))
    order: list = []
    real_build = appmod._build

    def spy_build(cfg, **kw):
        order.append("build")
        return real_build(cfg, **kw)

    monkeypatch.setattr(appmod, "_build", spy_build)
    out = tmp_path / "v.json"
    argv = [a for a in _main_argv(tmp_path) if a != "--window"]
    rc = appmod.main(["--selftest", "--selftest-out", str(out), *argv])
    assert rc == 1 and main_env == []
    assert order == ["build"]
    verdict = json.loads(out.read_text())
    assert verdict["failures"][0] == "ui" and "not built" in verdict["error"]
    assert verdict["app"]["ui_dir"] == str(tmp_path / "ui")


# ---------------------------------------------------------------------------
# frozen .app output: a LaunchServices launch writes to /dev/null
# ---------------------------------------------------------------------------

def test_output_discarded_recognises_dev_null_and_closed_fds(tmp_path):
    with open(os.devnull, "w") as null:
        assert appmod.output_discarded(null.fileno()) is True
    r, w = os.pipe()
    try:
        assert appmod.output_discarded(w) is False
    finally:
        os.close(r)
        os.close(w)
    with open(tmp_path / "f", "w") as fh:
        assert appmod.output_discarded(fh.fileno()) is False
    assert appmod.output_discarded(w) is True                   # closed above


def test_the_app_log_is_private_and_rotates(tmp_path):
    log = tmp_path / "logs" / "eye-in-the-sky.log"
    fd = appmod.open_app_log(log, max_bytes=10)
    os.write(fd, b"first launch, long enough to rotate\n")
    os.close(fd)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    fd = appmod.open_app_log(log, max_bytes=10)
    os.write(fd, b"second\n")
    os.close(fd)
    assert log.read_bytes() == b"second\n"
    assert (log.parent / "eye-in-the-sky.log.1").read_bytes().startswith(b"first launch")


def test_a_frozen_app_with_nowhere_to_print_logs_to_a_file(tmp_path):
    log = tmp_path / "logs" / "eye-in-the-sky.log"
    code = ("import os, sys; sys.frozen = True\n"
            "from godseye_uav import app\n"
            f"print(app.redirect_output_to_log(__import__('pathlib').Path({str(log)!r})))\n"
            "print('hello from print'); os.write(2, b'raw stderr\\n')\n")
    subprocess.run([sys.executable, "-c", code], env=_child_env(), cwd=str(tmp_path),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
    text = log.read_text()
    assert str(log) in text and "hello from print" in text and "raw stderr" in text
    assert "---- launch" in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    # Not frozen, or a terminal/pipe attached: nothing is redirected.
    out = subprocess.run([sys.executable, "-c", code.replace("sys.frozen = True", "pass")],
                         env=_child_env(), cwd=str(tmp_path), capture_output=True, text=True,
                         timeout=60, check=True)
    assert out.stdout.splitlines()[0] == "None" and "hello from print" in out.stdout


# ---------------------------------------------------------------------------
# packaging: the spec, its entry point and scripts/build_desktop.sh
# ---------------------------------------------------------------------------

PACKAGING = GS / "packaging" / "macos"
SPEC = PACKAGING / "EyeInTheSky.spec"
ENTRY = PACKAGING / "eye_in_the_sky.py"
BUILD_SH = REPO / "scripts" / "build_desktop.sh"


def test_the_spec_names_the_bundle_and_keeps_the_cli_out():
    import ast

    src = SPEC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts["APP_NAME"] == "Eye in the Sky"
    assert consts["BUNDLE_ID"] == "io.eyeinthesky.console"
    assert consts["EXE_NAME"] == "EyeInTheSky"
    for needle in ('(str(UI), "ui")', '"data/*.tif"', '"analyst_prompt.md"',
                   'collect_data_files("egm96")', 'copy_metadata("godseye-uav")',
                   '"airsim"', '"webview.platforms.cocoa"', "console=False",
                   "exclude_binaries=True"):
        assert needle in src, needle
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_is_bundled_cli")
    ns: dict = {"Path": Path}
    # Run just this one function from the spec (the rest needs PyInstaller).
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SPEC), "exec"), ns)  # noqa: S102
    assert ns["_is_bundled_cli"]("claude_agent_sdk/_bundled/claude")
    assert not ns["_is_bundled_cli"]("claude_agent_sdk/types.py")
    assert not ns["_is_bundled_cli"]("ui/assets/index.js")


def test_the_frozen_entry_runs_app_main_and_drops_launchservices_args(monkeypatch):
    import importlib.util as ilu

    spec = ilu.spec_from_file_location("eye_in_the_sky_entry", ENTRY)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)                                # not __main__: main not run
    assert mod.main is appmod.main
    monkeypatch.setattr(sys, "argv", ["EyeInTheSky", "-psn_0_12345", "--selftest"])
    assert mod._argv() == ["--selftest"]


def test_build_desktop_script_is_valid_and_documents_itself():
    assert BUILD_SH.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(BUILD_SH)], check=True)
    out = subprocess.run([str(BUILD_SH), "--help"], capture_output=True, text=True,
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    for flag in ("--dmg", "--selftest", "--no-cli", "--no-ui-build", "--bake-keys", "--clean"):
        assert flag in out.stdout, flag
    bad = subprocess.run([str(BUILD_SH), "--nope"], capture_output=True, text=True,
                         timeout=30, check=False)
    assert bad.returncode != 0 and "unknown option: --nope" in bad.stderr
    src = BUILD_SH.read_text(encoding="utf-8")
    # The CLI goes in after PyInstaller and only the outer bundle is re-signed.
    assert src.index("-m PyInstaller") < src.index('ditto "$CLI_SRC" "$APP/Contents/Helpers/claude"')
    assert 'codesign --force --sign - "$APP"' in src and "--deep --strict" in src
    assert "--deep --force" not in src and "--force --deep" not in src
    assert '--outDir "$UI_STAGE"' in src                        # never gods-eye-view/dist


@pytest.mark.skipif(os.environ.get("GODSEYE_TEST_DESKTOP_APP") != "1",
                    reason="opens a window; set GODSEYE_TEST_DESKTOP_APP=1 after "
                           "scripts/build_desktop.sh")
def test_the_built_app_passes_its_selftest(tmp_path):
    exe = REPO / "dist" / "Eye in the Sky.app" / "Contents" / "MacOS" / "EyeInTheSky"
    assert exe.is_file(), "run scripts/build_desktop.sh first"
    out = tmp_path / "verdict.json"
    env = {k: v for k, v in _child_env().items() if k != "PYTHONPATH"}
    proc = subprocess.run([str(exe), "--selftest", "--selftest-out", str(out),
                           "--store", str(tmp_path / "data" / "store"), "--port", "0"],
                          env=env, cwd="/", capture_output=True, text=True, timeout=90,
                          check=False)
    verdict = json.loads(out.read_text())
    assert proc.returncode == 0 and verdict["ok"], verdict
    assert verdict["app"]["frozen"] is True
