"""godseye_uav.host: the single-process, single-origin app host.

The live tests boot a real host (FakeAirSim + MCP + bridge on ONE app) with
``serve()`` on a worker thread, the way ``app.py --window`` runs it, and talk
to it over real sockets. Ports come from 53700-53749 (A12's range, WG §4.3)
and are bound for real, with a retry on a clash. The wiring tests swap in fake ``intel_graph`` and
``chat`` modules, so they check the host's side of those interfaces without
depending on the modules built in parallel.
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import godseye_uav
import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from godseye_uav import host as hostmod
from godseye_uav import theater_switch, theaters
from godseye_uav.host import (
    HostConfig,
    bearer_auth,
    build_host,
    default_store_dir,
    default_ui_dir,
    frozen_cli_path,
    host_header_name,
    inject_config,
    port_in_use,
    serve,
    sse_auth,
)

TOKEN = f"test-token-host-{os.getpid()}"
_PORTS = list(range(53700, 53750))            # A12: 53700-53749 here, 53750-53799 in test_app
_rng = random.Random(os.getpid() ^ time.time_ns())

INDEX_HTML = (
    "<!doctype html><html><head><title>t</title>"
    '<script src="/classic.js"></script>'
    '<script type="module" crossorigin src="/assets/app.js"></script>'
    '<script type="module" src="/assets/second.js"></script>'
    "</head><body></body></html>"
)


@pytest.fixture(scope="module", autouse=True)
def _llm_keys_in_memory():
    """The hosts built here keep analyst keys in memory: no test reads or
    writes a keychain (BYOK spec §11)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GODSEYE_LLM_SECRET_STORE", "memory")
        yield

def _make_ui(root: Path) -> Path:
    ui = root / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (ui / "assets" / "app.js").write_text("console.log('app')", encoding="utf-8")
    # Files named like bridge routes: the static mount must never answer them.
    (ui / "snapshot").write_text("STATIC-SHADOW", encoding="utf-8")
    (ui / "events").write_text("STATIC-SHADOW", encoding="utf-8")
    return ui


def _boot(tmp: Path, *, ui_dir: Path | None, mcp_port: bool = True, **kw) -> hostmod.Host:
    """build_host on free ports from this module's range (bind-retry)."""
    last: OSError | None = None
    # Popped ONCE: popping inside the loop dropped `chat`/`theater` on a retry
    # after a busy port, so the retried host silently booted with chat=False.
    theater, chat = kw.pop("theater", None), kw.pop("chat", False)
    for _ in range(12):
        port, compat, sim = _rng.sample(_PORTS, 3)
        cfg = HostConfig(theater=theater, sim_port=sim, port=port,
                         mcp_port=compat if mcp_port else None, token=TOKEN,
                         store_dir=tmp / "store", ui_dir=ui_dir, chat=chat, **kw)
        try:
            return build_host(cfg)
        except OSError as exc:
            last = exc
    assert last is not None
    raise last


class Live:
    """``serve(host)`` on a worker thread with its own loop."""

    def __init__(self, host: hostmod.Host) -> None:
        self.host = host
        self.ready = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(serve(self.host, ready=self.ready))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.errors.append(exc)

    def start(self) -> Live:
        self.thread.start()
        assert self.ready.wait(60), f"host never became ready: {self.errors}"
        return self

    def stop(self, timeout: float = 20.0) -> float:
        t0 = time.monotonic()
        self.host.request_stop()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "serve() did not return after request_stop()"
        return time.monotonic() - t0

    def client(self, port: int | None = None, *, auth: bool = True, **kw) -> httpx.Client:
        headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
        headers.update(kw.pop("headers", {}))
        return httpx.Client(base_url=f"http://127.0.0.1:{port or self.host.port}",
                            headers=headers, timeout=20.0, **kw)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("host-live")
    host = _boot(tmp, ui_dir=_make_ui(tmp))
    lv = Live(host).start()
    try:
        yield lv
    finally:
        lv.stop()


# ---------------------------------------------------------------------------
# the one app, on both ports
# ---------------------------------------------------------------------------

def test_health_is_open_on_the_app_port_and_the_mcp_compat_port(live):
    for port in (live.host.port, live.host.mcp_port):
        with live.client(port, auth=False) as c:
            r = c.get("/health")
        assert r.status_code == 200, port
        assert r.json()["ok"] is True
        assert r.json()["sim_state"] == "up"


def test_app_config_is_open_and_never_carries_the_token(live):
    with live.client(auth=False) as c:
        r = c.get("/app/config")
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "eye-in-the-sky"
    assert body["version"]
    # WG §4.1.10: the running theater with its epoch; no secret rides along.
    assert body["theater"] == {"id": "default", "label": live.host.theater.label, "epoch": 0}
    assert body["mcp_path"] == "/mcp"
    assert body["ui"] == "built"
    assert body["chat"]["available"] is False           # chat=False in this host
    assert "model" in body["chat"]
    assert TOKEN not in r.text
    assert "console_key" not in r.text and "consoleKey" not in r.text


def test_index_gets_the_runtime_config_before_the_first_module_script(live):
    for path in ("/", "/index.html"):
        with live.client(auth=False) as c:
            r = c.get(path)
        assert r.status_code == 200, path
        page = r.text
        tag = f'<script>window.__GODSEYE__={{"bridgeUrl":"","token":"{TOKEN}"}}</script>'
        assert page.count(tag) == 1, path
        assert page.index('<script src="/classic.js">') < page.index(tag)
        assert page.index(tag) < page.index('<script type="module" crossorigin')
        assert "no-store" in r.headers["cache-control"]


def test_static_mount_serves_assets_but_never_shadows_bridge_routes(live):
    with live.client(auth=False) as c:
        asset = c.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('app')"
        assert c.get("/snapshot").status_code == 401     # the bridge, not the file
        assert c.get("/events").status_code == 401
    with live.client() as c:
        snap = c.get("/snapshot")
    assert snap.status_code == 200
    assert "STATIC-SHADOW" not in snap.text
    assert "vehicles" in snap.json()
    conn = http.client.HTTPConnection("127.0.0.1", live.host.port, timeout=10)
    try:
        conn.request("GET", f"/events?token={TOKEN}", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("content-type").startswith("text/event-stream")
        assert b"STATIC-SHADOW" not in resp.read1(64)
    finally:
        conn.close()


def _mcp(port: int, method: str, token: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(f"http://127.0.0.1:{port}/mcp", headers=headers, timeout=20.0,
                      json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})


def _rpc_result(resp: httpx.Response) -> dict:
    text = resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)["result"]


def test_mcp_is_mounted_on_both_ports_behind_the_bearer_token(live):
    for port in (live.host.port, live.host.mcp_port):
        ok = _mcp(port, "tools/list", TOKEN)
        assert ok.status_code == 200, (port, ok.text[:200])
        names = {t["name"] for t in _rpc_result(ok)["tools"]}
        assert {"uav_takeoff", "uav_get_telemetry", "mission_dry_run"} <= names
        assert _mcp(port, "tools/list", None).status_code == 401
        assert _mcp(port, "tools/list", "wrong-token").status_code == 401


def test_mcp_auth_metadata_names_this_hosts_port_not_8791(live):
    """The 401's RFC 9728 pointer used to name the legacy stack's 8791 on
    every host, whatever port it actually served."""
    base = f"http://127.0.0.1:{live.host.port}"
    assert live.host.server.public_url == base
    for port in (live.host.port, live.host.mcp_port):
        challenge = _mcp(port, "tools/list", None).headers["www-authenticate"]
        assert f'resource_metadata="{base}/.well-known/oauth-protected-resource"' \
            in challenge, challenge
        assert ":8791" not in challenge


def test_the_bridge_feed_polls_mcp_on_its_own_origin(live):
    """Loop C still reaches MCP over loopback HTTP, now on the app's own port."""
    assert live.host.mcp_url == f"http://127.0.0.1:{live.host.port}/mcp"
    deadline = time.monotonic() + 20.0
    feed: dict = {}
    with live.client(auth=False) as c:
        while time.monotonic() < deadline:
            feed = c.get("/health").json()["mission_feed"]
            if feed.get("mission_state", {}).get("ok"):
                break
            time.sleep(0.25)
    assert feed["url"] == live.host.mcp_url
    assert feed["mission_state"]["ok"] is True, feed


def test_intel_and_chat_routes_require_the_bearer_token(live):
    with live.client(auth=False) as c:
        for path in ("/intel/graph", "/intel/entity/veh:Drone1", "/intel/events/recent",
                     "/chat/status"):
            assert c.get(path).status_code == 401, path
        assert c.post("/chat/sessions").status_code == 401
    with live.client() as c:
        status = c.get("/chat/status")
        assert status.status_code == 200
        assert status.json()["available"] is False
        # 200 with the real intel module, 503 if it could not start; never 401.
        assert c.get("/intel/graph").status_code in (200, 503)


def test_node_only_api_paths_answer_json_404(live):
    with live.client(auth=False) as c:
        for method, path in (("GET", "/api/setup/status"),
                             ("GET", "/api/google/nearby-places"),
                             ("POST", "/api/openai/hud-summary"),
                             ("POST", "/api/realtime/debug-log")):
            r = c.request(method, path)
            assert r.status_code == 404, (method, path)
            assert r.json() == {"error": "not_available_in_app_host"}


@pytest.mark.parametrize("host_header,expected", [
    ("evil.com", 400),
    ("evil.com:{port}", 400),
    ("127.0.0.1.evil.com:{port}", 400),
    ("localhost.evil.com", 400),
    ("", 400),
    ("127.0.0.1:{port}", 200),
    ("localhost:{port}", 200),
    ("LOCALHOST:{port}", 200),
    ("[::1]:{port}", 200),
])
def test_dns_rebinding_guard_only_answers_loopback_host_names(live, host_header, expected):
    conn = http.client.HTTPConnection("127.0.0.1", live.host.port, timeout=10)
    try:
        conn.putrequest("GET", "/health", skip_host=True)
        if host_header:
            conn.putheader("Host", host_header.format(port=live.host.port))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == expected
        resp.read()
    finally:
        conn.close()


def test_rebinding_guard_also_covers_the_index_page_that_holds_the_token(live):
    with live.client(auth=False, headers={"Host": "attacker.example"}) as c:
        r = c.get("/")
    assert r.status_code == 400
    assert TOKEN not in r.text


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def _can_bind(port: int) -> bool:
    """Bindable the way the host binds (SO_REUSEADDR: a TIME_WAIT leftover
    from a closed connection is fine, a live listener is not).
    """
    try:
        with contextlib.closing(socket.socket()) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def test_shutdown_is_prompt_with_an_open_event_stream_and_releases_everything(tmp_path):
    host = _boot(tmp_path, ui_dir=None)
    lv = Live(host).start()
    conn = http.client.HTTPConnection("127.0.0.1", host.port, timeout=15)
    conn.request("GET", f"/events?token={TOKEN}", headers={"Accept": "text/event-stream"})
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read1(32)                                   # the stream is live
    took = lv.stop()
    # Streams are told the client went away; they are not left to the
    # graceful-shutdown timeout (which an open stream would otherwise hit).
    assert took < hostmod.GRACEFUL_SHUTDOWN_S + 1.5, took
    assert lv.errors == [] or all(isinstance(e, KeyboardInterrupt) for e in lv.errors)
    rest = b""
    with contextlib.suppress(Exception):
        rest = resp.read()
    assert b"STATIC" not in rest
    conn.close()
    for port in (host.port, host.mcp_port, host.sim_port):
        assert _can_bind(port), f"port {port} still held after shutdown"
    # the store lock is released: a second host can use the same store
    again = _boot(tmp_path, ui_dir=None, mcp_port=False)
    again.close()


def test_request_stop_before_serve_returns_immediately(tmp_path):
    host = _boot(tmp_path, ui_dir=None, mcp_port=False)
    host.request_stop()
    t0 = time.monotonic()
    asyncio.run(serve(host))
    assert time.monotonic() - t0 < 10
    assert _can_bind(host.port)


def test_missing_ui_serves_build_instructions(tmp_path):
    host = _boot(tmp_path, ui_dir=tmp_path / "no-such-dist", mcp_port=False)
    try:
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            page = c.get("/")
            assert page.status_code == 200
            assert page.headers["content-type"].startswith("text/html")
            assert "not built" in page.text
            assert "npm run build" in page.text
            assert str(tmp_path / "no-such-dist") in page.text
            assert TOKEN not in page.text
            assert c.get("/app/config").json()["ui"] == "missing"
            assert c.get("/health").status_code == 200
    finally:
        host.close()


def test_busy_app_port_fails_before_the_sim_or_store_are_touched(tmp_path):
    blocker = socket.socket()
    try:
        for port in _rng.sample(_PORTS, 20):
            try:
                blocker.bind(("127.0.0.1", port))
                break
            except OSError:
                continue
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        sim = next(p for p in _rng.sample(_PORTS, 20) if p != busy and _can_bind(p))
        cfg = HostConfig(theater=None, sim_port=sim, port=busy, token=TOKEN,
                         store_dir=tmp_path / "store", ui_dir=None, chat=False)
        with pytest.raises(OSError) as exc:
            build_host(cfg)
        assert port_in_use(exc.value)
        assert _can_bind(sim), "the fake sim was started although the app port was busy"
        assert not (tmp_path / "store").exists()
    finally:
        blocker.close()


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_non_loopback_bind_is_refused(tmp_path, bind):
    with pytest.raises(ValueError, match="loopback"):
        build_host(HostConfig(theater=None, host=bind, store_dir=tmp_path / "s"))
    assert not (tmp_path / "s").exists()


def test_unknown_theater_is_refused_loudly(tmp_path):
    with pytest.raises(KeyError):
        build_host(HostConfig(theater="atlantis", store_dir=tmp_path / "s", port=0))


def test_a_store_is_owned_by_one_host_at_a_time(tmp_path):
    first = hostmod._lock_store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="in use"):
            hostmod._lock_store(tmp_path)
    finally:
        first.close()
    hostmod._lock_store(tmp_path).close()


# ---------------------------------------------------------------------------
# wiring of intel_graph and chat (fakes; the interfaces from the contract)
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self) -> None:
        self.intel_args: tuple = ()
        self.chat_kwargs: dict = {}
        self.chat_shutdown = 0


def _fake_modules(rec: _Recorder, *, chat_fails: bool = False,
                  intel_fails: bool = False) -> tuple[types.ModuleType, types.ModuleType]:
    intel = types.ModuleType("godseye_uav.intel_graph")

    class IntelService:
        def __init__(self, bridge_ctx, server=None):
            if intel_fails:
                raise RuntimeError("intel boom")
            rec.intel_args = (bridge_ctx, server)

    def intel_router(service, auth):
        r = APIRouter()

        @r.get("/intel/graph")
        def graph(_: bool = Depends(auth)):
            return {"fake": "graph", "service": type(service).__name__}

        return r

    intel.IntelService = IntelService
    intel.intel_router = intel_router

    chat = types.ModuleType("godseye_uav.chat")

    class ChatService:
        def __init__(self, **kwargs):
            if chat_fails:
                raise RuntimeError("chat boom")
            rec.chat_kwargs = kwargs

        def status(self):
            return {"available": True, "model": "fake-model", "effort": "high"}

        async def shutdown(self):
            rec.chat_shutdown += 1

    def chat_router(service, auth, sse_auth_dep):
        r = APIRouter()

        @r.get("/chat/status")
        def status(_: bool = Depends(auth)):
            return service.status()

        @r.get("/chat/sessions/{sid}/stream")
        def stream(sid: str, _: bool = Depends(sse_auth_dep)):
            return {"sid": sid}

        return r

    chat.ChatService = ChatService
    chat.chat_router = chat_router
    return intel, chat


@pytest.fixture
def fake_modules(monkeypatch):
    def install(**kw):
        rec = _Recorder()
        intel, chat = _fake_modules(rec, **kw)
        for name, mod in (("intel_graph", intel), ("chat", chat)):
            monkeypatch.setitem(sys.modules, f"godseye_uav.{name}", mod)
            monkeypatch.setattr(godseye_uav, name, mod, raising=False)
        return rec
    return install


def test_intel_and_chat_are_wired_with_the_contract_arguments(tmp_path, fake_modules):
    rec = fake_modules()
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True, model="m-1")
    try:
        ctx, server = rec.intel_args
        assert server is host.server
        for attr in ("state", "hub", "feed", "mcp", "adapter", "active_theater", "token"):
            assert hasattr(ctx, attr), attr
        assert callable(ctx.active_theater)
        kw = rec.chat_kwargs
        assert kw["server"] is host.server
        assert kw["intel"] is host.intel
        assert Path(kw["store_dir"]) == host.store_dir and host.store_dir.is_absolute()
        assert kw["model"] == "m-1"
        assert kw["enabled"] is True
        assert kw["cli_path"] is None                  # not frozen, no override
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            assert c.get("/intel/graph").status_code == 401
            assert c.get("/intel/graph", headers=auth).json()["fake"] == "graph"
            assert c.get("/chat/status").status_code == 401
            assert c.get("/chat/status", headers=auth).json()["available"] is True
            assert c.get("/chat/sessions/s1/stream").status_code == 401
            assert c.get(f"/chat/sessions/s1/stream?token={TOKEN}").json() == {"sid": "s1"}
            assert c.get("/chat/sessions/s1/stream?token=nope").status_code == 401
            cfg = c.get("/app/config").json()
            assert cfg["chat"] == {"available": True, "model": "fake-model"}
        assert rec.chat_shutdown == 1                  # awaited in the lifespan teardown
    finally:
        host.close()


def test_chat_disabled_flag_reaches_the_service(tmp_path, fake_modules):
    rec = fake_modules()
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=False)
    try:
        assert rec.chat_kwargs["enabled"] is False
    finally:
        host.close()


def test_a_failing_analyst_leaves_the_rest_of_the_app_up(tmp_path, fake_modules):
    fake_modules(chat_fails=True, intel_fails=True)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        assert host.chat is None and host.intel is None
        assert "chat boom" in host.chat_error
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            auth = {"Authorization": f"Bearer {TOKEN}"}
            st = c.get("/chat/status", headers=auth)
            assert st.status_code == 200
            assert st.json()["available"] is False
            assert "chat boom" in st.json()["hint"]
            assert c.post("/chat/sessions").status_code == 401
            assert c.post("/chat/sessions", headers=auth).status_code == 503
            assert c.get("/intel/graph").status_code == 401
            assert c.get("/intel/graph", headers=auth).json() == {"error": "intel_unavailable"}
            cfg = c.get("/app/config").json()
            assert cfg["chat"]["available"] is False
            assert c.get("/health").status_code == 200
    finally:
        host.close()


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def test_inject_config_is_compact_and_goes_before_the_first_module_script():
    out = inject_config(INDEX_HTML, {"bridgeUrl": "", "token": "abc"})
    tag = '<script>window.__GODSEYE__={"bridgeUrl":"","token":"abc"}</script>'
    assert out.count(tag) == 1
    assert out.index(tag) < out.index('type="module" crossorigin')
    assert out.index('src="/classic.js"') < out.index(tag)


def test_inject_config_falls_back_to_head_then_top():
    tag = '<script>window.__GODSEYE__={"a":1}</script>'
    assert inject_config("<html><head></head></html>", {"a": 1}) == (
        f"<html><head>{tag}</head></html>")
    assert inject_config("<p>x</p>", {"a": 1}) == tag + "<p>x</p>"
    assert inject_config("<SCRIPT TYPE=module src=x></SCRIPT>", {"a": 1}).startswith(tag)


def test_injected_values_cannot_close_the_script_element():
    out = inject_config("<head></head>", {"token": "</script><script>alert(1)</script>&"})
    assert "</script><script>alert" not in out
    assert out.count("</script>") == 1
    payload = out[len("<head><script>window.__GODSEYE__="):out.index("</script>")]
    assert json.loads(payload)["token"] == "</script><script>alert(1)</script>&"


@pytest.mark.parametrize("value,name", [
    ("127.0.0.1:8780", "127.0.0.1"), ("localhost", "localhost"),
    ("LocalHost:1", "localhost"), ("[::1]:8780", "[::1]"), ("[::1]", "[::1]"),
    ("evil.com:80", "evil.com"), ("", ""),
])
def test_host_header_name(value, name):
    assert host_header_name(value) == name


def test_bearer_and_sse_auth_dependencies():
    app = FastAPI()
    auth, sse = bearer_auth("s3cret"), sse_auth("s3cret")

    @app.get("/a")
    def a(_: bool = Depends(auth)):
        return {"ok": True}

    @app.get("/s")
    def s(_: bool = Depends(sse)):
        return {"ok": True}

    c = TestClient(app)
    good = {"Authorization": "Bearer s3cret"}
    assert c.get("/a").status_code == 401
    assert c.get("/a", headers={"Authorization": "Bearer nope"}).status_code == 401
    # a non-ASCII credential is a 401, not a 500 (compare_digest on str would raise)
    assert c.get("/a", headers={"Authorization": "Bearer s3crét".encode("latin-1")},
                 ).status_code == 401
    assert c.get("/a?token=s3cret").status_code == 401        # no query form on plain routes
    assert c.get("/a", headers=good).json() == {"ok": True}
    assert c.get("/a").json() == {"detail": "unauthorized"}
    assert c.get("/s").status_code == 401
    assert c.get("/s?token=nope").status_code == 401
    assert c.get("/s?token=s3cret").status_code == 200
    assert c.get("/s", headers=good).status_code == 200


def test_default_store_dir_is_absolute_per_platform(tmp_path):
    home = tmp_path / "home"
    mac = default_store_dir(platform="darwin", env={}, home=home)
    assert mac == home / "Library" / "Application Support" / "EyeInTheSky" / "store"
    xdg = default_store_dir(platform="linux", env={"XDG_DATA_HOME": str(tmp_path / "x")},
                            home=home)
    assert xdg == tmp_path / "x" / "eye-in-the-sky" / "store"
    plain = default_store_dir(platform="linux", env={}, home=home)
    assert plain == home / ".local" / "share" / "eye-in-the-sky" / "store"
    win = default_store_dir(platform="win32", env={"LOCALAPPDATA": str(tmp_path / "l")},
                            home=home)
    assert win == tmp_path / "l" / "EyeInTheSky" / "store"
    assert default_store_dir().is_absolute()


def test_default_ui_dir(monkeypatch, tmp_path):
    assert default_ui_dir(env={"GODSEYE_UI_DIR": str(tmp_path)}) == tmp_path.resolve()
    repo = default_ui_dir(env={})
    assert repo.parts[-2:] == ("gods-eye-view", "dist") and repo.is_absolute()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    assert default_ui_dir(env={}) == tmp_path / "meipass" / "ui"


def test_frozen_cli_path(monkeypatch, tmp_path):
    assert frozen_cli_path() is None                     # not frozen
    macos = tmp_path / "Eye in the Sky.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "Eye in the Sky"
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert frozen_cli_path() is None                     # no helper copied in
    helper = macos.parent / "Helpers" / "claude"
    helper.parent.mkdir()
    helper.write_text("")
    assert frozen_cli_path() == str(helper.resolve())


# ---------------------------------------------------------------------------
# review-finding regressions
# ---------------------------------------------------------------------------

def _lan_ipv4() -> str | None:
    """This machine's non-loopback IPv4, if it has one (the UDP connect only
    picks a route; no packet is sent)."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        try:
            s.connect(("192.0.2.1", 9))                 # TEST-NET-1
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def test_the_fake_sim_listens_on_loopback_only(live):
    """Review finding (HITL, high): msgpack-rpc threw the Address host away and
    the fake sim bound *:port, so any host on the LAN could fly the drones
    with a plain AirSim client -- no analyst policy, no approval card, no
    server gate. The sim must answer on loopback only."""
    import ipaddress

    addrs = hostmod.sim_listen_addresses(live.host.sim)
    assert addrs, "no sim sockets found"
    assert all(ipaddress.ip_address(a).is_loopback for a in addrs), addrs
    lan = _lan_ipv4()
    if lan is not None:                                  # black-box: the LAN side is closed
        with contextlib.closing(socket.socket()) as probe:
            probe.settimeout(2.0)
            with pytest.raises(OSError):
                probe.connect((lan, live.host.sim_port))
    with contextlib.closing(socket.create_connection(("127.0.0.1", live.host.sim_port), 2)):
        pass                                             # loopback still answers


def test_a_sim_reachable_off_loopback_is_refused():
    import types as _t

    wildcard = socket.socket()
    try:
        wildcard.bind(("0.0.0.0", 0))                    # bound, never listening
        fake = _t.SimpleNamespace(_server=_t.SimpleNamespace(_listeners=[
            _t.SimpleNamespace(_mp_server=_t.SimpleNamespace(_sockets={1: wildcard}))]))
        with pytest.raises(RuntimeError, match="not loopback"):
            hostmod._assert_sim_loopback(fake)
        empty = _t.SimpleNamespace(_server=_t.SimpleNamespace(_listeners=[]))
        with pytest.raises(RuntimeError, match="cannot verify"):
            hostmod._assert_sim_loopback(empty)
    finally:
        wildcard.close()
    hostmod.honour_msgpack_bind_host()                   # idempotent
    from msgpackrpc.transport import tcp
    assert tcp.ServerTransport.listen._godseye_honours_host is True


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:4173",
                                    "https://evil.example"])
def test_the_token_page_is_never_readable_from_another_origin(live, origin):
    """Review finding: the bridge's CORS allowlist also covered GET /, which
    embeds the token, so an allowed origin (a dev port, or every origin with
    GODSEYE_BRIDGE_CORS_ORIGINS='*') could read the token and drive every tool
    with no approval. Token pages carry no CORS grant and cannot be framed."""
    with live.client(auth=False, headers={"Origin": origin}) as c:
        for path in ("/", "/index.html"):
            r = c.get(path)
            assert r.status_code == 200 and TOKEN in r.text, path
            assert not [k for k in r.headers if k.lower().startswith("access-control-")], \
                (path, dict(r.headers))
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["cross-origin-resource-policy"] == "same-origin"
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        pre = c.options("/", headers={"Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in pre.headers
    policy = live.host.app.state.cors
    if origin in policy.origins or policy.allow_any:     # the API keeps its CORS grant
        with live.client(headers={"Origin": origin}) as c:
            assert c.get("/health").headers.get("access-control-allow-origin") == origin


def test_the_token_page_guard_beats_an_allow_any_cors_policy():
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse

    app = FastAPI()

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse('<script>window.__GODSEYE__={"token":"t"}</script>')

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    # exactly what the bridge installs for GODSEYE_BRIDGE_CORS_ORIGINS='*'
    app.add_middleware(CORSMiddleware, allow_origins=[], allow_origin_regex=".*",
                       allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(hostmod.TokenPageCorsGuardMiddleware)
    with TestClient(app) as c:
        evil = {"Origin": "https://evil.example"}
        page = c.get("/", headers=evil)
        assert "access-control-allow-origin" not in page.headers
        assert c.get("/health", headers=evil).headers["access-control-allow-origin"] \
            == "https://evil.example"


def test_a_receive_from_a_foreign_loop_never_binds_the_shutdown_event():
    """Review finding: the bridge's sync /control/* handlers read their body
    with anyio.run in a worker thread; the middleware awaited the serving
    loop's Event there, which bound it to that throwaway loop, and from then on
    every guarded receive faked a disconnect (empty MCP replies, streams
    closing at once)."""
    msg = {"type": "http.request", "body": b"{}", "more_body": False}

    async def upstream_receive():
        await asyncio.sleep(0.02)
        return dict(msg)

    async def main():
        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()
        got: list = []
        foreign_errors: list = []

        async def app(scope, receive, send):
            if scope["path"] == "/control/command":
                def body():
                    lp = asyncio.new_event_loop()
                    lp.set_exception_handler(lambda _l, ctx: foreign_errors.append(ctx))
                    try:
                        return lp.run_until_complete(receive())
                    finally:
                        lp.close()
                got.append(await asyncio.to_thread(body))
            else:
                got.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        mw = hostmod.ShutdownDisconnectMiddleware(app, stopping=lambda: stopping,
                                                  loop=lambda: loop)

        async def send(_m):
            return None

        await mw({"type": "http", "path": "/control/command", "headers": []},
                 upstream_receive, send)
        await mw({"type": "http", "path": "/mcp", "headers": []}, upstream_receive, send)
        stopping.set()                                   # and shutdown still works
        await mw({"type": "http", "path": "/events", "headers": []}, upstream_receive, send)
        return got, foreign_errors

    got, foreign_errors = asyncio.run(main())
    assert got[:2] == [msg, msg]                         # no faked disconnect
    assert got[2] == {"type": "http.disconnect"}
    assert foreign_errors == []


def test_a_control_call_first_after_boot_leaves_mcp_working(tmp_path):
    """The live shape of the same finding: with the bridge loops off, the first
    request after boot is a /control/command. Before the fix it answered 502
    'unparseable MCP response' and MCP stayed broken until a restart."""
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    lv = Live(host).start()
    try:
        with lv.client() as c:
            for _ in range(2):
                r = c.post("/control/command",
                           json={"tool": "uav_get_telemetry", "vehicle": "Drone1"})
                assert r.status_code == 200, r.text
                assert r.json().get("error") is None, r.json()
        ok = _mcp(host.port, "tools/list", TOKEN)
        assert ok.status_code == 200 and _rpc_result(ok)["tools"], ok.text[:200]
    finally:
        lv.stop()


def test_bind_refuses_a_port_a_wildcard_listener_already_holds():
    """Review finding: with SO_REUSEADDR, macOS let the host bind
    127.0.0.1:port under another process's *:port listener and silently take
    its loopback traffic; the port-busy handling never fired."""
    wildcard = socket.socket()
    try:
        port = next(p for p in _rng.sample(_PORTS, 40) if _try_bind_any(wildcard, p))
        wildcard.listen(1)
        with pytest.raises(OSError) as err:
            hostmod._bind("127.0.0.1", port)
        assert port_in_use(err.value)
    finally:
        wildcard.close()


def _try_bind_any(sock: socket.socket, port: int) -> bool:
    try:
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# start.sh hands the token to the host through its environment, not argv
# ---------------------------------------------------------------------------

def test_start_sh_passes_the_token_in_the_environment_not_on_argv():
    """argv is world-readable (`ps`); a process's environment is readable only
    by its owner. app.py reads GODSEYE_TOKEN (and pops it before spawning)."""
    import subprocess

    from godseye_uav import app as appmod

    src = (Path(__file__).resolve().parents[1] / "start.sh").read_text(encoding="utf-8")
    start = src.index('GODSEYE_TOKEN="$TOKEN" "$PY" -m godseye_uav.app --headless')
    call = src[start:src.index("PIDS+=", start)]
    assert "--token" not in call, call
    assert '--port "$BRIDGE_PORT"' in call and '--mcp-port "$MCP_PORT"' in call
    # only the public dev default is ever echoed back
    assert "--token $TOKEN " not in src and "--token $TOKEN\"" not in src
    assert appmod.resolve_token(None, {"GODSEYE_TOKEN": "t-env"}) == ("t-env", "GODSEYE_TOKEN")
    subprocess.run(["bash", "-n", str(Path(__file__).resolve().parents[1] / "start.sh")],
                   check=True)


# ---------------------------------------------------------------------------
# BYOK analyst settings (spec §7, §8, §12 PY-HOST): a fake llm_settings module
# ---------------------------------------------------------------------------
# Fake keys only; nothing here reads or writes a keychain.

FAKE_KEY = "test-key-123-host"


class _LlmRec:
    def __init__(self) -> None:
        self.settings_args: tuple = ()
        self.settings_kwargs: dict = {}
        self.guard_kwargs: dict = {}
        self.router_args: tuple = ()


def _fake_llm_module(rec: _LlmRec, *, settings_fails: bool = False,
                     guard_needs: str | None = None) -> types.ModuleType:
    mod = types.ModuleType("godseye_uav.llm_settings")

    class LlmSettings:
        def __init__(self, settings_dir, llm_env=None, *, store_dir=None, model=None,
                     effort=None, app_port=None, cli_path=None):
            if settings_fails:
                raise RuntimeError(f"broken settings {FAKE_KEY}")
            rec.settings_args = (settings_dir, dict(llm_env or {}))
            rec.settings_kwargs = {"store_dir": store_dir, "model": model, "effort": effort,
                                   "app_port": app_port, "cli_path": cli_path}
            self.generation = 1

        def status(self):
            return {"provider": {"id": "openrouter", "label": "OpenRouter",
                                 "kind": "anthropic_compatible", "model_family": "mixed",
                                 "host": "openrouter.ai", "key_source": "environment",
                                 "configured": True, "secret": FAKE_KEY},
                    "cost_basis": "unreliable", "settings_rev": 4, "ready": True,
                    "model": "anthropic/claude-opus-5.5"}

    class SettingsGuardMiddleware:
        def __init__(self, app, port: int, **kw):
            if guard_needs:
                raise TypeError(f"missing {guard_needs}")
            rec.guard_kwargs = {"port": port, **kw}
            self.app, self.port = app, port

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/settings/"):
                headers = dict(scope.get("headers") or [])
                origin = headers.get(b"origin", b"").decode()
                if origin and origin != f"http://127.0.0.1:{self.port}":
                    from starlette.responses import JSONResponse
                    await JSONResponse({"error": "cross_origin"}, status_code=403)(
                        scope, receive, send)
                    return
            await self.app(scope, receive, send)

    def llm_settings_router(svc, auth, chat):
        rec.router_args = (svc, auth, chat)
        r = APIRouter()

        @r.get("/settings/llm")
        def get_settings(_: bool = Depends(auth)):
            return {"schema": "eye-in-the-sky.llm-settings/1", "rev": 4}

        return r

    mod.LlmSettings = LlmSettings
    mod.SettingsGuardMiddleware = SettingsGuardMiddleware
    mod.llm_settings_router = llm_settings_router
    return mod


@pytest.fixture
def fake_llm(monkeypatch, fake_modules):
    def install(**kw):
        rec = _LlmRec()
        mod = _fake_llm_module(rec, **kw)
        monkeypatch.setitem(sys.modules, "godseye_uav.llm_settings", mod)
        monkeypatch.setattr(godseye_uav, "llm_settings", mod, raising=False)
        return rec, fake_modules()
    return install


def test_settings_are_built_before_the_chat_and_their_routes_are_guarded(tmp_path, fake_llm):
    rec, chat_rec = fake_llm()
    host = _boot(tmp_path, ui_dir=_make_ui(tmp_path), mcp_port=False, chat=True,
                 model="m-1", effort="high", llm_env={"OPENROUTER_API_KEY": FAKE_KEY})
    try:
        assert rec.settings_args == (host.store_dir.parent, {"OPENROUTER_API_KEY": FAKE_KEY})
        assert rec.settings_kwargs["store_dir"] == host.store_dir
        assert rec.settings_kwargs["model"] == "m-1" and rec.settings_kwargs["effort"] == "high"
        assert rec.settings_kwargs["app_port"] == host.port
        assert chat_rec.chat_kwargs["llm"] is host.llm         # the chat resolves through it
        assert rec.router_args[0] is host.llm and rec.router_args[2] is host.chat
        assert rec.guard_kwargs["port"] == host.port
        assert FAKE_KEY not in repr(host.config)                # llm_env is kept out of repr
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.get("/settings/llm").status_code == 401
            assert c.get(f"/settings/llm?token={TOKEN}").status_code == 401   # header only
            ok = c.get("/settings/llm", headers=auth)
            assert ok.status_code == 200 and ok.json()["rev"] == 4  # not the static mount
            evil = c.get("/settings/llm", headers={**auth, "Origin": "http://localhost:5173"})
            assert evil.status_code == 403 and evil.json() == {"error": "cross_origin"}
            same = c.get("/settings/llm",
                         headers={**auth, "Origin": f"http://127.0.0.1:{host.port}"})
            assert same.status_code == 200
            assert c.get("/settings/llm", headers={**auth, "Host": "evil.example"}
                         ).status_code == 400                    # the Host guard is outside
    finally:
        host.close()


def test_a_broken_settings_module_leaves_the_app_up_on_the_claude_login(tmp_path, fake_llm):
    _, chat_rec = fake_llm(settings_fails=True)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        assert host.llm is None and host.llm_error == "RuntimeError"   # never the message
        assert chat_rec.chat_kwargs["llm"] is None
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.get("/settings/llm").status_code == 401
            r = c.get("/settings/llm", headers=auth)
            assert r.status_code == 503 and r.json() == {"error": "settings_unavailable"}
            assert c.get("/chat/status", headers=auth).json()["available"] is True
    finally:
        host.close()


def test_a_guard_that_cannot_be_built_keeps_the_settings_routes_closed(tmp_path, fake_llm):
    """Starlette builds middleware at the first request: an unusable guard
    must not take every route down, and no settings route may run unguarded."""
    fake_llm(guard_needs="allowed_hosts")
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.get("/health").status_code == 200
            r = c.get("/settings/llm", headers=auth)
            assert r.status_code == 503 and r.json() == {"error": "settings_unavailable"}
    finally:
        host.close()


def test_the_fallback_status_and_app_config_name_the_provider_without_secrets(
        tmp_path, fake_llm, fake_modules):
    fake_llm()
    fake_modules(chat_fails=True)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        assert host.chat is None and host.llm is not None
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            st = c.get("/chat/status", headers=auth)
            assert st.json()["provider"] == {
                "id": "openrouter", "label": "OpenRouter", "kind": "anthropic_compatible",
                "model_family": "mixed", "host": "openrouter.ai",
                "key_source": "environment", "configured": True}
            cfg = c.get("/app/config")
            assert cfg.json()["chat"]["provider"] == {"id": "openrouter", "label": "OpenRouter"}
            for body in (st.text, cfg.text):
                assert FAKE_KEY not in body
            assert "openrouter.ai" not in cfg.text               # no host on the open route
    finally:
        host.close()


def test_chat_summary_passes_only_id_and_label_from_the_chat_status():
    host = hostmod.Host(config=HostConfig(theater=None, model="m"))
    host.chat = types.SimpleNamespace(status=lambda: {
        "available": True, "model": "glm-5.3", "cost_basis": "unreliable",
        "provider": {"id": "zai", "label": "Z.ai GLM", "host": "api.z.ai",
                     "key_source": "keychain", "configured": True}})
    assert host.chat_summary() == {"available": True, "model": "glm-5.3",
                                   "provider": {"id": "zai", "label": "Z.ai GLM"}}
    host.chat = types.SimpleNamespace(status=lambda: {"available": True, "model": "x"})
    assert host.chat_summary() == {"available": True, "model": "x"}


def _real_llm_settings():
    try:
        import godseye_uav.llm_settings as mod
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"llm_settings does not import: {type(exc).__name__}")
    for name in ("LlmSettings", "llm_settings_router", "SettingsGuardMiddleware"):
        if not hasattr(mod, name):
            pytest.skip(f"llm_settings has no {name} yet")
    return mod


def test_the_real_settings_module_is_wired_guarded_and_never_echoes_a_key(tmp_path):
    """The real llm_settings + chat on one host: a launch-env key is locked
    in, named by source only, and on no answer (spec §7, §8, §9.1)."""
    _real_llm_settings()
    host = _boot(tmp_path, ui_dir=_make_ui(tmp_path), mcp_port=False, chat=True,
                 llm_env={"OPENROUTER_API_KEY": FAKE_KEY,
                          "GODSEYE_LLM_PROVIDER": "openrouter"})
    try:
        assert host.llm is not None and host.llm_error is None
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.get("/settings/llm").status_code == 401
            view = c.get("/settings/llm", headers=auth)
            assert view.status_code == 200, view.text
            assert view.json()["active"] == "openrouter"
            assert view.headers["cache-control"] == "no-store"
            assert c.get(f"/settings/llm?token={TOKEN}").status_code in (400, 401)
            cross = c.get("/settings/llm",
                          headers={**auth, "Origin": "http://localhost:5173"})
            assert cross.status_code == 403
            assert "access-control-allow-origin" not in cross.headers
            st = c.get("/chat/status", headers=auth).json()
            assert st["provider"]["id"] == "openrouter"
            assert st["provider"]["key_source"] == "environment"
            assert st["cost_basis"] == "unreliable"
            cfg = c.get("/app/config").json()
            assert cfg["chat"]["provider"] == {"id": "openrouter", "label": "OpenRouter"}
            for r in (view, cross):
                assert FAKE_KEY not in r.text
            assert FAKE_KEY not in json.dumps(st) and FAKE_KEY not in json.dumps(cfg)
            assert "openrouter.ai" not in json.dumps(cfg)
    finally:
        host.close()


def test_the_real_settings_default_to_the_claude_login(tmp_path):
    _real_llm_settings()
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        auth = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            st = c.get("/chat/status", headers=auth).json()
            assert st["provider"]["id"] == "anthropic_login"
            assert st["provider"]["key_source"] == "login"
            assert c.get("/app/config").json()["chat"]["provider"] == {
                "id": "anthropic_login", "label": "Claude login (this Mac)"}
        # nothing was written until the operator saves (spec §3.4)
        assert not (host.store_dir.parent / "llm-settings.json").exists()
        # The full check builds its options with the analyst's own builder
        # (spec §6.2), and a temp store never defaults to the login keychain.
        assert host.llm._options_builder == host.chat.check_options
        assert host.llm.key_store["kind"] in ("file", "memory")
    finally:
        host.close()


# ---------------------------------------------------------------------------
# runtime theaters (WG §4.1.4 boot order, §4.1.10 host wiring; unit A12)
# ---------------------------------------------------------------------------

#: Bengaluru centre (E2E A1): a chat theater far from the Redmond default.
BLR = (12.9716, 77.5946)
QUAD, GROUP3 = "quad_suas_electric", "group3_fixed_wing"


def _call(srv, name: str, **args) -> dict:
    """A registered MCP tool's function, in-process (no transport)."""
    return asyncio.run(srv.mcp._tool_manager._tools[name].fn(**args))


def _audits(host, kind: str) -> list[dict]:
    return [r for r in host.store.audit.read_all() if r.get("kind") == kind]


def _state_file(store: Path) -> dict:
    return json.loads((store / "theater.json").read_text(encoding="utf-8"))


def _blr_theater(label: str = "Bengaluru centre") -> theaters.Theater:
    return theaters.make_dynamic(label=label, place="Bengaluru", center=BLR,
                                 half_extent_m=2940.0, home=BLR, home_alt_msl_m=920.0,
                                 provenance=None)


def _persist_dynamic(store: Path, *, epoch: int, airframe: str = GROUP3,
                     label: str = "Bengaluru centre") -> theaters.Theater:
    """Write `<store>/theater.json` as a switch would, WITHOUT registering the
    theater: only the host's boot (load_persisted) may register it."""
    t = _blr_theater(label)
    srv = types.SimpleNamespace(theater=t, airframe_id=airframe, theater_epoch=epoch,
                                theater_set_at_ms=1_700_000_000_000,
                                theater_set_via="console",
                                theater_previous={"id": "default", "label": "Redmond"})
    store.mkdir(parents=True, exist_ok=True)
    assert theater_switch.persist(store, theater_switch.state_of(srv)) is not None
    assert t.id not in {d.id for d in theaters.dynamic_theaters()}
    return t


class _Spy:
    """Wraps a bound method: counts calls, then calls through."""

    def __init__(self, fn) -> None:
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.fn(*a, **kw)


def test_a_theater_set_from_chat_survives_a_restart_with_its_epoch_and_airframe(
        tmp_path, monkeypatch):
    """The whole path on real hosts: switch through the real tools on host A
    (the listener drops the bridge geofence, flown tracks and intel cache;
    the adapter origin moves; /app/config follows), then host B on the same
    store boots that theater, its epoch and its airframe (§4.1.4)."""
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        srv, bridge = host.server, host.app.state.bridge
        assert len(srv.theater_listeners) == 1 and host.adapter in srv.origin_holders
        spies = {"feed": _Spy(bridge.feed.invalidate_geofence),
                 "flown": _Spy(bridge.reset_flown), "intel": _Spy(host.intel.invalidate)}
        monkeypatch.setattr(bridge.feed, "invalidate_geofence", spies["feed"])
        monkeypatch.setattr(bridge, "reset_flown", spies["flown"])
        monkeypatch.setattr(host.intel, "invalidate", spies["intel"])
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert srv.boot_recovery_done.wait(20), "restart recovery never finished"
            with bridge._lock:
                bridge._flown["Drone1"] = deque([(47.6, -122.1), (47.61, -122.1)])
            before = c.get("/app/config").json()["theater"]
            assert before == {"id": "default", "label": srv.theater.label, "epoch": 0}
            p = _call(srv, "theater_propose", lat=BLR[0], lon=BLR[1],
                      label="Bengaluru centre", ground_msl_m=920.0, airframe=GROUP3)
            assert not p.get("rejected") and "error" not in p, p
            out = _call(srv, "sim_set_theater", **p["set_args"])
            assert out["ok"] is True and out["status"] == "accepted", out
            tid = out["theater"]["id"]
            assert tid.startswith("dyn-") and out["theater"]["epoch"] == 1
            assert {k: s.calls for k, s in spies.items()} == {"feed": 1, "flown": 1, "intel": 1}
            assert bridge.flown("Drone1") == []
            assert not _audits(host, "theater_listener_failed")
            geo = host.adapter.home_geo                     # the third origin copy moved
            assert (round(geo.latitude, 6), round(geo.longitude, 6)) == (
                round(srv.theater.home_lat, 6), round(srv.theater.home_lon, 6))
            assert host.theater.id == tid                   # the listener keeps it current
            r = c.get("/app/config")
            assert r.json()["theater"] == {"id": tid, "label": "Bengaluru centre", "epoch": 1}
            assert TOKEN not in r.text and "console_key" not in r.text
        doc = _state_file(host.store_dir)
        assert (doc["theater_id"], doc["epoch"], doc["airframe"], doc["set_via"]) == (
            tid, 1, GROUP3, "mcp")
    finally:
        host.close()

    theaters.clear_dynamic()                # a new process knows only the store's file
    again = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        srv = again.server
        assert again.boot.source == "store" and again.boot.error is None
        assert srv.theater.id == tid and srv.theater.dynamic
        assert srv.theater_epoch == 1 and srv.airframe_id == GROUP3
        assert srv.theater_set_via == "boot"
        with TestClient(again.app, base_url="http://127.0.0.1") as c:
            assert c.get("/app/config").json()["theater"] == {
                "id": tid, "label": "Bengaluru centre", "epoch": 1}
        doc = _state_file(again.store_dir)
        assert (doc["theater_id"], doc["epoch"], doc["set_via"]) == (tid, 1, "boot")
        assert not _audits(again, "theater_restore_failed")
    finally:
        again.close()


def test_an_explicit_theater_wins_over_the_store_and_is_persisted(tmp_path):
    store = tmp_path / "store"
    blr = _persist_dynamic(store, epoch=3, airframe=GROUP3)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False,
                 theater="iran-isfahan")
    try:
        srv = host.server
        assert host.boot.source == "flag" and host.boot.error is None
        assert srv.theater.id == "iran-isfahan"
        assert srv.theater_epoch == 0                    # another theater: no epoch carried
        assert srv.airframe_id == QUAD                   # ... and not its airframe either
        doc = _state_file(store)
        assert (doc["theater_id"], doc["epoch"], doc["airframe"], doc["set_via"]) == (
            "iran-isfahan", 0, QUAD, "boot")
        assert doc["theater"] is None                    # a preset is stored by id only
        assert blr.id in {t.id for t in theaters.dynamic_theaters()}   # read, not booted
    finally:
        host.close()


def test_the_persisted_theater_keeps_its_epoch_but_an_explicit_airframe_wins(tmp_path):
    store = tmp_path / "store"
    blr = _persist_dynamic(store, epoch=4, airframe=GROUP3)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False,
                 theater=blr.id, airframe=QUAD)
    try:
        srv = host.server
        assert (srv.theater.id, srv.theater_epoch, srv.airframe_id) == (blr.id, 4, QUAD)
        assert _state_file(store)["airframe"] == QUAD
    finally:
        host.close()


@pytest.mark.parametrize("content", [
    "{not json",
    json.dumps({"schema": "something/else", "theater_id": "default"}),
    json.dumps({"schema": theater_switch.SCHEMA, "theater_id": "atlantis", "epoch": 2}),
    json.dumps({"schema": theater_switch.SCHEMA, "theater_id": "dyn-gone-1234abcd",
                "theater": None, "epoch": 2}),
    json.dumps({"schema": theater_switch.SCHEMA, "theater_id": "default", "epoch": -1}),
], ids=["corrupt", "wrong-schema", "unknown-id", "dynamic-without-row", "bad-epoch"])
def test_an_unusable_theater_file_boots_the_default_audited_and_replaced(tmp_path, content):
    store = tmp_path / "store"
    store.mkdir()
    (store / "theater.json").write_text(content, encoding="utf-8")
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        srv = host.server
        assert host.boot.source == "default" and host.boot.error
        assert srv.theater.id == theaters.DEFAULT_THEATER_ID and srv.theater_epoch == 0
        rows = _audits(host, "theater_restore_failed")
        assert len(rows) == 1 and rows[0]["fallback"] == theaters.DEFAULT_THEATER_ID
        doc = _state_file(store)                         # re-persisted: now valid
        assert doc["schema"] == theater_switch.SCHEMA and doc["theater_id"] == "default"
        assert theater_switch.load_persisted(store)["theater_id"] == "default"
    finally:
        host.close()


def test_a_first_boot_persists_the_default_with_no_audit(tmp_path):
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        assert host.boot == hostmod.BootTheater(host.server.theater, None, 0, "default")
        assert not _audits(host, "theater_restore_failed")
        doc = _state_file(host.store_dir)
        assert (doc["theater_id"], doc["epoch"], doc["set_via"]) == ("default", 0, "boot")
        assert host.server.theater_set_at_ms is not None
    finally:
        host.close()


def test_boot_theater_rules_without_a_host():
    cfg = HostConfig(theater=None)
    saved = {"theater_id": "iran-isfahan", "airframe": GROUP3, "epoch": 7}
    b = hostmod.boot_theater(cfg, saved)
    assert (b.theater.id, b.airframe, b.epoch, b.source, b.error) == (
        "iran-isfahan", GROUP3, 7, "store", None)
    b = hostmod.boot_theater(HostConfig(theater="iran-isfahan"), saved)
    assert (b.source, b.epoch, b.airframe) == ("flag", 7, GROUP3)   # same theater
    b = hostmod.boot_theater(HostConfig(theater="default"), saved)
    assert (b.theater.id, b.epoch, b.airframe) == ("default", 0, None)
    b = hostmod.boot_theater(HostConfig(theater=None, airframe=QUAD), saved)
    assert (b.epoch, b.airframe) == (7, QUAD)
    b = hostmod.boot_theater(cfg, {"error": "theater.json is unreadable"})
    assert (b.theater.id, b.source, b.error) == ("default", "default",
                                                 "theater.json is unreadable")
    b = hostmod.boot_theater(cfg, {"theater_id": "dyn-not-registered-1", "epoch": 2})
    assert b.theater.id == "default" and "did not resolve" in b.error and b.epoch == 0
    assert hostmod.boot_theater(cfg, None).source == "default"
    with pytest.raises(KeyError):
        hostmod.boot_theater(HostConfig(theater="atlantis"), saved)
    with pytest.raises(ValueError, match="unknown airframe"):
        hostmod.boot_theater(HostConfig(theater=None, airframe="zeppelin"), None)


def test_a_real_airsim_never_gets_a_restored_theater_it_was_not_given():
    """settings.json fixes a real AirSim's origin: a theater the fake sim was
    switched to must not be restored onto it (only --theater or the default)."""
    real = HostConfig(theater=None, sim_backend="real", sim_port=1)
    b = hostmod.boot_theater(real, {"theater_id": "iran-isfahan", "airframe": GROUP3,
                                    "epoch": 4})
    assert (b.theater.id, b.source, b.epoch, b.airframe) == ("default", "default", 0, None)
    assert b.error.startswith("real AirSim: the origin is fixed by settings.json")
    b = hostmod.boot_theater(real, {"theater_id": "default", "airframe": None, "epoch": 2})
    assert (b.theater.id, b.source, b.epoch, b.error) == ("default", "store", 2, None)
    flagged = HostConfig(theater="iran-isfahan", sim_backend="real", sim_port=1)
    b = hostmod.boot_theater(flagged, {"theater_id": "iran-isfahan", "epoch": 4})
    assert (b.theater.id, b.source, b.epoch, b.error) == ("iran-isfahan", "flag", 4, None)


def test_build_server_gets_the_geodata_real_data_and_airframe_switches(tmp_path, monkeypatch):
    from godseye_uav import launch

    seen: list[dict] = []
    real_build = launch.build_server

    def spy(t, backend, store, **kw):
        seen.append(kw)
        return real_build(t, backend, store, **kw)

    monkeypatch.setattr(launch, "build_server", spy)
    monkeypatch.delenv("GODSEYE_REAL_DATA", raising=False)
    monkeypatch.delenv("GODSEYE_GEODATA", raising=False)
    host = _boot(tmp_path / "a", ui_dir=None, mcp_port=False, start_loops=False,
                 geodata=True, real_data="direct", airframe=GROUP3)
    try:
        kw = seen[-1]
        assert (kw["geodata"], kw["real_data"], kw["airframe"]) == (True, "direct", GROUP3)
        srv = host.server
        assert srv.geodata_enabled is True and srv.real is not None
        assert srv.airframe_id == GROUP3
    finally:
        host.close()
    host = _boot(tmp_path / "b", ui_dir=None, mcp_port=False, start_loops=False)
    try:
        kw = seen[-1]
        assert (kw["geodata"], kw["real_data"], kw["airframe"]) == (False, None, None)
        assert host.server.geodata_enabled is False and host.server.real is None
        assert host.server.sites.as_dict()["reason"] == "map data is off"
    finally:
        host.close()


def _listener_host(**over):
    """A host-shaped namespace for the listener: every step records its name."""
    order: list[str] = []
    feed = types.SimpleNamespace(invalidate_geofence=lambda: order.append("feed"))
    bridge = types.SimpleNamespace(feed=feed, reset_flown=lambda: order.append("flown"))
    intel = types.SimpleNamespace(invalidate=lambda: order.append("intel"))
    ns = types.SimpleNamespace(
        server=types.SimpleNamespace(theater="new"), theater="old",
        bridge_app=types.SimpleNamespace(state=types.SimpleNamespace(bridge=bridge)),
        intel=intel)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns, order


def test_the_theater_listener_drops_the_feed_geofence_flown_tracks_and_intel_cache():
    ns, order = _listener_host()
    listen = hostmod.theater_listener(ns)
    listen({"theater_id": "dyn-x"})
    assert order == ["feed", "flown", "intel"] and ns.theater == "new"
    # The feed is read at call time: a swapped feed is the one invalidated.
    swapped: list[str] = []
    ns.bridge_app.state.bridge.feed = types.SimpleNamespace(
        invalidate_geofence=lambda: swapped.append("new feed"))
    listen({})
    assert swapped == ["new feed"]
    # No intel (it failed to load) and a feed without the hook: the rest runs.
    ns, order = _listener_host(intel=None)
    ns.bridge_app.state.bridge.feed = object()
    hostmod.theater_listener(ns)({})
    assert order == ["flown"]


def test_a_failing_listener_step_never_skips_the_others_and_is_raised():
    def boom():
        raise OSError("feed gone")

    ns, order = _listener_host()
    ns.bridge_app.state.bridge.feed = types.SimpleNamespace(invalidate_geofence=boom)
    with pytest.raises(RuntimeError, match="feed.invalidate_geofence: OSError: feed gone"):
        hostmod.theater_listener(ns)({})
    assert order == ["flown", "intel"]


def test_a_listener_failure_is_audited_and_the_switch_still_lands(tmp_path, monkeypatch):
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        srv = host.server

        def broken():
            raise RuntimeError("intel cache wedged")

        monkeypatch.setattr(host.intel, "invalidate", broken)
        srv.boot_recovery_done.set()             # no lifespan here: nothing to recover
        p = _call(srv, "theater_propose", lat=BLR[0], lon=BLR[1], label="Bengaluru centre",
                  ground_msl_m=920.0, airframe=QUAD)
        out = _call(srv, "sim_set_theater", **p["set_args"])
        assert out["status"] == "accepted" and srv.theater_epoch == 1
        rows = _audits(host, "theater_listener_failed")
        assert len(rows) == 1 and "intel cache wedged" in rows[0]["message"]
        assert host.app.state.bridge.flown("Drone1") == []
    finally:
        host.close()


def test_without_an_origin_holder_no_listener_is_added_so_a_switch_is_refused(tmp_path):
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        srv = host.server
        srv.theater_listeners.clear()
        srv.origin_holders.clear()
        hostmod._wire_theater(host, object())    # no .relocate / .home_geo
        assert srv.theater_listeners == [] and srv.origin_holders == []
        srv.boot_recovery_done.set()
        p = _call(srv, "theater_propose", lat=BLR[0], lon=BLR[1], label="Bengaluru centre",
                  ground_msl_m=920.0, airframe=QUAD)
        out = _call(srv, "sim_set_theater", **p["set_args"])
        assert out.get("rejected") is True and srv.theater.id == "default"
        assert theater_switch.MSG_NO_HOST in json.dumps(out)
        hostmod._wire_theater(host, host.adapter)
        assert len(srv.theater_listeners) == 1 and srv.origin_holders == [host.adapter]
    finally:
        host.close()


# ---------------------------------------------------------------------------
# M14a (WG v2 §3.5, §5.2.12 B11): the console key and the wargame routes
# ---------------------------------------------------------------------------

#: `secrets.token_urlsafe(24)`, and what the console's `isConsoleKey` accepts.
CONSOLE_KEY_SHAPE = r"[A-Za-z0-9_-]{32}"


def _claim(c: TestClient, *, token: str | None = TOKEN, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post(hostmod.CONSOLE_CLAIM_PATH, headers=headers, **kw)


def _store_bytes(store_dir: Path) -> bytes:
    """Everything the store wrote (audit, tasks, theater, wargame, ...)."""
    return b"".join(p.read_bytes() for p in sorted(store_dir.rglob("*")) if p.is_file())


def test_no_get_answer_carries_the_console_key(live):
    """V3: the key is never GET-able: not in the page's injected config,
    `/app/config`, a status answer, or the host's repr; a GET of the claim
    path does not claim it either."""
    import re as _re

    key = live.host.console.key
    assert _re.fullmatch(CONSOLE_KEY_SHAPE, key) and key != TOKEN
    with live.client(auth=False) as c:
        for path in ("/", "/index.html", "/app/config", "/health",
                     hostmod.CONSOLE_CLAIM_PATH):
            r = c.get(path)
            assert (key in r.text) is False, path
            assert "console_key" not in r.text and "consoleKey" not in r.text, path
        assert c.get(hostmod.CONSOLE_CLAIM_PATH).status_code in (404, 405)
        page = c.get("/").text
        assert page.count("window.__GODSEYE__=") == 1
        assert f'window.__GODSEYE__={{"bridgeUrl":"","token":"{TOKEN}"}}' in page
    with live.client() as c:
        for path in ("/chat/status", "/snapshot", "/intel/graph"):
            r = c.get(path)
            assert (key in r.text) is False, path
    assert live.host.console.claimed is False           # nothing above claimed it
    assert (key in repr(live.host.console)) is False
    assert (key in repr(live.host.config)) is False


def test_the_console_key_is_claimed_once_then_refused_and_audited(tmp_path):
    """First claim: 200 `{console_key}` + `console_claimed`. Every later one:
    409 `console_already_claimed` + `console_claim_refused` (the console's
    banner signal). The bearer is required and a refused call never claims.
    The real ChatService holds the same key; nothing on disk does."""
    import re as _re

    from godseye_uav import chat

    host = _boot(tmp_path, ui_dir=_make_ui(tmp_path), mcp_port=False, start_loops=False)
    try:
        key = host.console.key
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert _claim(c, token=None).status_code == 401
            assert _claim(c, token="wrong-token").status_code == 401
            assert c.post(f"{hostmod.CONSOLE_CLAIM_PATH}?token={TOKEN}").status_code == 401
            assert host.console.claimed is False and _audits(host, "console_claimed") == []

            first = _claim(c, json={})
            assert first.status_code == 200
            assert first.json() == {"console_key": key}
            assert _re.fullmatch(CONSOLE_KEY_SHAPE, first.json()["console_key"])
            assert "no-store" in first.headers["cache-control"]
            for _ in range(2):
                again = _claim(c, json={"anything": "ignored"})
                assert again.status_code == 409
                assert again.json()["error"] == "console_already_claimed"
                assert again.json()["rejected"] is True and again.json()["message"]
                assert (key in again.text) is False
            for path in ("/", "/app/config"):
                assert (key in c.get(path).text) is False, path

        claimed = _audits(host, "console_claimed")
        refused = _audits(host, "console_claim_refused")
        assert len(claimed) == 1 and claimed[0]["claimed_at_ms"] == host.console.claimed_at_ms
        assert [r["attempt"] for r in refused] == [1, 2]
        assert host.console.refused == 2
        # The analyst's approval router checks the header against this key.
        assert host.chat is not None and host.chat.console_key == key
        assert chat.console_matches(host.chat.console_key, key) is True
        wrong = key[:-1] + ("A" if key[-1] != "A" else "B")
        assert chat.console_matches(host.chat.console_key, wrong) is False
        assert (key in json.dumps(host.chat.status())) is False
        # Never persisted: not in the audit trail or any other store file.
        assert (key.encode() in _store_bytes(host.store_dir)) is False
    finally:
        host.close()


def test_each_launch_makes_its_own_console_key(tmp_path):
    keys = []
    for name in ("a", "b"):
        host = _boot(tmp_path / name, ui_dir=None, mcp_port=False, start_loops=False)
        try:
            keys.append(host.console.key)
            assert host.console.key not in (TOKEN, host.token)
        finally:
            host.close()
    assert keys[0] != keys[1]


def test_concurrent_claims_have_exactly_one_winner():
    claim = hostmod.ConsoleClaim()
    gate = threading.Barrier(24)
    got: list = []

    def race() -> None:
        gate.wait()
        got.append(claim.claim())

    threads = [threading.Thread(target=race) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    winners = [k for k, _ in got if k is not None]
    assert winners == [claim.key]
    assert sorted(n for k, n in got if k is None) == list(range(1, 24))
    assert (claim.key in repr(claim)) is False


def test_the_chat_service_receives_the_console_key(tmp_path, fake_modules):
    rec = fake_modules()
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, chat=True)
    try:
        assert rec.chat_kwargs["console_key"] == host.console.key
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert _claim(c).json() == {"console_key": rec.chat_kwargs["console_key"]}
            assert _claim(c).status_code == 409
    finally:
        host.close()


def _wg_names(registry) -> set[str]:
    return {t.name for t in asyncio.run(registry.list_tools()) if t.name.startswith("wg_")}


def test_the_wargame_end_route_is_mounted_behind_the_bearer(tmp_path):
    """`POST /wargame/session/end` rides on the app (outside create_app): 401
    without the bearer, 409 with no session, 200 with the AAR, then 409. The
    host's close stops the engine's thread and pools."""
    from godseye_uav import wargame as wg

    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    srv = host.server
    try:
        assert host.wargame_error is None
        srv.wargame.run_thread = False                  # no step thread in this test
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.post("/wargame/session/end").status_code == 401
            assert c.post(f"/wargame/session/end?token={TOKEN}").status_code == 401
            r = c.post("/wargame/session/end", headers=bearer)
            assert r.status_code == 409
            assert r.json()["error"] == "wargame_inactive" and r.json()["simulated"] is True
            tool = srv.wargame_mcp._tool_manager._tools["wg_session_start"].fn
            started = asyncio.run(tool(seed=4417, red_engages=False))
            assert started["simulated"] is True and srv.wargame.active is True
            r = c.post("/wargame/session/end", headers=bearer,
                       json={"reason": "<img src=x onerror=alert(1)>"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True and body["simulated"] is True
            assert body["aar_id"] == f"aar-{started['session_id']}"
            assert srv.reports[body["aar_id"]]["reason"] == "operator"
            assert srv.wargame.active is False
            assert c.post("/wargame/session/end", headers=bearer).status_code == 409
    finally:
        calls = []
        real_close = srv.wargame.close
        srv.wargame.close = lambda: (calls.append(1), real_close())
        host.close()
        assert calls == [1]
    assert isinstance(srv.wargame, wg.WargameEngine)


def test_the_wargame_mcp_flag_reaches_the_server(tmp_path, monkeypatch):
    """`HostConfig.wargame_mcp` (`app.py --wargame-mcp`) -> `build_server(
    wargame_mcp=)`: only then does `/mcp` list `wg_*`. The analyst's own
    wargame registry has them either way."""
    from godseye_uav import launch, wargame_tools

    seen: list[dict] = []
    real_build = launch.build_server

    def spy(t, backend, store, **kw):
        seen.append(kw)
        return real_build(t, backend, store, **kw)

    monkeypatch.setattr(launch, "build_server", spy)
    assert HostConfig(theater=None).wargame_mcp is False
    host = _boot(tmp_path / "isr", ui_dir=None, mcp_port=False, start_loops=False)
    try:
        assert seen[-1]["wargame_mcp"] is False
        assert host.server.wargame_mcp_enabled is False
        assert _wg_names(host.server.mcp) == set()
        assert _wg_names(host.server.wargame_mcp) == set(wargame_tools.TOOL_NAMES)
    finally:
        host.close()
    host = _boot(tmp_path / "flag", ui_dir=None, mcp_port=False, start_loops=False,
                 wargame_mcp=True)
    try:
        assert seen[-1]["wargame_mcp"] is True
        assert host.server.wargame_mcp_enabled is True
        assert _wg_names(host.server.mcp) == set(wargame_tools.TOOL_NAMES)
        # MCP's own DNS-rebinding check wants the port in the Host header.
        with TestClient(host.app, base_url=f"http://127.0.0.1:{host.port}") as c:
            listed = c.post(hostmod.MCP_PATH, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"})
            assert listed.status_code == 200, listed.text[:200]
            names = {t["name"] for t in _rpc_result(listed)["tools"]}
            assert "wg_execute_engagement" in names
            # Still no key anywhere a GET reaches, flag or not.
            assert (host.console.key in c.get("/app/config").text) is False
    finally:
        host.close()



def test_a_broken_wargame_module_leaves_the_rest_of_the_app_up(tmp_path, monkeypatch):
    from godseye_uav import wargame_tools

    def boom(srv, auth):
        raise RuntimeError("wargame boom")

    monkeypatch.setattr(wargame_tools, "wargame_router", boom)
    host = _boot(tmp_path, ui_dir=None, mcp_port=False, start_loops=False)
    try:
        assert "wargame boom" in host.wargame_error
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        with TestClient(host.app, base_url="http://127.0.0.1") as c:
            assert c.post("/wargame/session/end").status_code == 401
            r = c.post("/wargame/session/end", headers=bearer)
            assert r.status_code == 503 and r.json() == {"error": "wargame_unavailable"}
            assert c.get("/health").status_code == 200
            assert _claim(c).status_code == 200          # the console key is unaffected
    finally:
        host.close()
