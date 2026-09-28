"""The single-process, single-origin Eye in the Sky host.

One FastAPI app on one asyncio loop carries everything the console needs:

* the fake (or real) AirSim sim, the MCP server (mounted at ``/mcp``), the
  telemetry bridge (``create_app``, unchanged), the intel graph routes and the
  analyst chat routes;
* the built God's-Eye-View UI, served from ``/`` with the runtime config
  (``window.__GODSEYE__``) injected into ``index.html``.

``launch.py`` is left as it is (tests and legacy scripts use it). This module
reuses its helpers (``resolve_theater``, ``home_geopoint``, ``build_server``)
and differs from ``launch.main`` in three ways:

* The MCP server is not a second uvicorn: its Streamable-HTTP ASGI app is
  mounted on the bridge app, and its session manager plus the server's safety
  monitor run inside the app lifespan. The bridge's mission feed still polls
  MCP over loopback HTTP, which now lands on the same port.
* Nothing is written to ``os.environ`` (``launch.main`` exports
  ``GODSEYE_MCP_URL``/``GODSEYE_MCP_TOKEN``). The MCP URL and token go to
  ``create_app`` as arguments.
* ``serve()`` is a coroutine, so a GUI can own the main thread and run the
  host on a worker thread with its own loop.

Routes are added from outside ``create_app``, never inside it (the bridge's
tests pin its route set). ``intel_graph``, ``chat`` and ``llm_settings`` are
imported lazily. If one fails to load or to start, the host still boots: it
answers that module's routes with an honest 503, so the operator keeps the
rest of the app (without the settings, the analyst runs on the Claude login).

The analyst's model-provider settings (BYOK spec §7): ``LlmSettings`` is built
before ``ChatService`` (which resolves every turn through it), its
``/settings/llm*`` routes sit before the ``/api/*`` catch-all and the static
mount, and ``SettingsGuardMiddleware`` wraps them inside the Host check.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import hmac
import html as html_lib
import inspect
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import launch

log = logging.getLogger("godseye_uav.host")

APP_NAME = "eye-in-the-sky"
APP_TITLE = "Eye in the Sky"
MCP_PATH = "/mcp"
#: Host names the app answers to. The token is served inside index.html, so a
#: page on any other origin must not be able to read it (DNS-rebinding guard).
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
#: Bind addresses accepted for ``HostConfig.host``. Anything else would put the
#: token-bearing index page on the network.
LOOPBACK_BIND = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1",
                 "[::1]": "::1"}
#: Open SSE streams must not hold shutdown hostage (verified: an open /events
#: stream keeps uvicorn alive forever with the default ``None``).
GRACEFUL_SHUTDOWN_S = 2
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_REPO_GODSEYE = Path(__file__).resolve().parents[2]      # <repo>/godseye


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@dataclass
class HostConfig:
    theater: str | None
    sim_backend: str = "fake"              # "fake" | "real"
    sim_port: int = 41451                  # 0 = pick a free port (fake sim only)
    port: int = 8780                       # 0 = pick a free port
    mcp_port: int | None = None            # optional second listener (same app)
    host: str = "127.0.0.1"                # loopback only
    token: str | None = None               # None = random per launch
    store_dir: Path | None = None          # None = default_store_dir()
    ui_dir: Path | None = None             # None = default_ui_dir()
    chat: bool = True
    model: str | None = None
    # --- extensions (all optional) ---
    cli_path: str | None = None            # None = frozen helper if bundled, else SDK discovery
    effort: str | None = None
    start_loops: bool = True               # bridge telemetry/camera/mission loops
    #: Provider/credential variables ``app.capture_llm_env`` took out of the
    #: launch environment (BYOK spec §3.3). Holds secrets: never log or print it.
    llm_env: dict[str, str] | None = field(default=None, repr=False)


def default_store_dir(*, platform: str | None = None, env: Any = None,
                      home: Path | None = None) -> Path:
    """The app's absolute default store directory (contract §2).

    macOS: ``~/Library/Application Support/EyeInTheSky/store``; Windows:
    ``%LOCALAPPDATA%/EyeInTheSky/store``; elsewhere:
    ``$XDG_DATA_HOME/eye-in-the-sky/store`` (``~/.local/share`` by default).
    Never cwd-relative: a Finder-launched app runs with cwd ``/``.
    """
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "EyeInTheSky" / "store"
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "EyeInTheSky" / "store"
    base = env.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / "eye-in-the-sky" / "store"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_ui_dir(env: Any = None) -> Path:
    """Where the built UI is looked for: ``$GODSEYE_UI_DIR``, the frozen
    bundle's ``ui/``, else the repo's ``gods-eye-view/dist``. Returned even
    when it does not exist, so the "not built" page can name the path.
    """
    env = os.environ if env is None else env
    if env.get("GODSEYE_UI_DIR"):
        return Path(env["GODSEYE_UI_DIR"]).expanduser().resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if is_frozen() and meipass:
        return Path(meipass) / "ui"
    return (_REPO_GODSEYE.parent / "gods-eye-view" / "dist").resolve()


def frozen_cli_path() -> str | None:
    """The Claude CLI copied into ``Contents/Helpers/claude`` of a frozen .app
    (kept outside PyInstaller so Anthropic's signature survives), or None.
    """
    if not is_frozen():
        return None
    exe = Path(sys.executable).resolve()          # <App>.app/Contents/MacOS/<name>
    helper = exe.parent.parent / "Helpers" / "claude"
    return str(helper) if helper.is_file() else None


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("godseye-uav")
    except Exception:  # noqa: BLE001 - frozen builds may carry no metadata
        return "0.1.0"


# ---------------------------------------------------------------------------
# auth dependencies (same semantics as the bridge's auth / auth_sse)
# ---------------------------------------------------------------------------

def _token_ok(given: str | None, token: str) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


def bearer_auth(token: str) -> Callable[..., bool]:
    """FastAPI dependency: ``Authorization: Bearer <token>`` or 401."""
    bearer = HTTPBearer(auto_error=False)

    def _auth(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> bool:  # noqa: B008
        if cred is None or not _token_ok(cred.credentials, token):
            raise HTTPException(status_code=401, detail="unauthorized")
        return True

    return _auth


def sse_auth(token: str) -> Callable[..., bool]:
    """FastAPI dependency for SSE: bearer header OR ``?token=`` (EventSource
    cannot set headers).
    """
    bearer = HTTPBearer(auto_error=False)

    def _auth(request: Request,
              cred: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
              ) -> bool:
        if cred is not None and _token_ok(cred.credentials, token):
            return True
        if _token_ok(request.query_params.get("token"), token):
            return True
        raise HTTPException(status_code=401, detail="unauthorized")

    return _auth


# ---------------------------------------------------------------------------
# DNS-rebinding guard
# ---------------------------------------------------------------------------

def host_header_name(value: str) -> str:
    """Host header -> bare lower-case host name. Bracketed IPv6 keeps its
    brackets (``[::1]:8780`` -> ``[::1]``); Starlette's own parser splits on the
    first colon and would turn it into ``[``.
    """
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[:end + 1] if end > 0 else value
    return value.split(":", 1)[0]


class LoopbackHostMiddleware(TrustedHostMiddleware):
    """``TrustedHostMiddleware`` with IPv6-aware Host parsing."""

    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS) -> None:
        super().__init__(app, allowed_hosts=list(allowed_hosts), www_redirect=False)
        self._allowed = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        name = host_header_name(Headers(scope=scope).get("host", ""))
        if name in self._allowed:
            await self.app(scope, receive, send)
            return
        await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)


class ShutdownDisconnectMiddleware:
    """Once the server starts shutting down, in-flight requests see
    ``http.disconnect``.

    Streaming responses (the bridge's ``/events``, the chat stream) then end
    the way they do when a browser tab closes: promptly and without an error.
    Otherwise they run until uvicorn's graceful-shutdown timeout cancels them
    and logs a traceback, and the UI always holds some open. Every HTTP
    request is guarded, not only ``Accept: text/event-stream`` ones: a
    fetch-based stream client need not send that header, and a streaming
    response may start listening for disconnects before its headers are
    sent. ``stopping`` returns the serving loop's ``asyncio.Event``, or None
    when ``serve()`` is not running the app (TestClient).

    ``loop`` returns the loop that owns that Event. A receive called from any
    OTHER loop is passed through unguarded: the bridge's sync ``/control/*``
    handlers read their body with ``anyio.run`` in a threadpool thread (a new,
    throwaway loop), and awaiting the Event there would bind it to that loop
    -- after which every guarded receive on the real loop failed at once and
    faked a disconnect (empty MCP replies, SSE streams closing after 20 ms).
    """

    def __init__(self, app: ASGIApp, stopping: Callable[[], asyncio.Event | None],
                 loop: Callable[[], asyncio.AbstractEventLoop | None] = lambda: None) -> None:
        self.app = app
        self.stopping = stopping
        self.loop = loop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        event = self.stopping() if scope["type"] == "http" else None
        if event is None:
            await self.app(scope, receive, send)
            return
        owner = self.loop()

        progress = {"started": False, "complete": False, "faked": False}

        async def guarded_receive():
            if owner is not None and asyncio.get_running_loop() is not owner:
                return await receive()        # a foreign loop: never touch the Event
            if event.is_set():
                progress["faked"] = True
                return {"type": "http.disconnect"}
            got = asyncio.ensure_future(receive())
            stop = asyncio.ensure_future(event.wait())
            try:
                await asyncio.wait({got, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for fut in (got, stop):
                    if not fut.done():
                        fut.cancel()
            if got.done() and not got.cancelled():
                return got.result()
            progress["faked"] = True
            return {"type": "http.disconnect"}

        async def tracking_send(message):
            if message["type"] == "http.response.start":
                progress["started"] = True
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                progress["complete"] = True
            await send(message)

        await self.app(scope, guarded_receive, tracking_send)
        # The client is in fact still connected: finish the response it saw
        # start, so it gets a clean end of stream (and uvicorn no error log).
        if progress["faked"] and progress["started"] and not progress["complete"]:
            with contextlib.suppress(Exception):
                await send({"type": "http.response.body", "body": b"", "more_body": False})


#: Pages whose body carries the API/MCP token (``window.__GODSEYE__``).
TOKEN_PAGES = frozenset({"/", "/index.html"})
#: Extra response headers on a token page: no other origin may read it, embed
#: it as a resource, or frame it (a framed console could be clickjacked into
#: approving a command).
TOKEN_PAGE_HEADERS = {
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


class TokenPageCorsGuardMiddleware:
    """Keep the bridge's CORS allowlist off the pages that embed the token.

    The bridge's ``CORSMiddleware`` wraps the whole app, so it also answered
    ``GET /`` with ``Access-Control-Allow-Origin`` for any allowed origin (the
    default dev origins, or every origin under
    ``GODSEYE_BRIDGE_CORS_ORIGINS='*'``): a page on such an origin could fetch
    the index, read the token out of it and drive every tool with no approval.
    For the token pages this strips the request's ``Origin`` (so CORS treats
    it as same-origin and adds nothing), drops any ``Access-Control-*``
    response header that still appears, and adds ``TOKEN_PAGE_HEADERS``.
    """

    def __init__(self, app: ASGIApp, paths: frozenset[str] = TOKEN_PAGES) -> None:
        self.app = app
        self.paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() != b"origin"]

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if not k.lower().startswith(b"access-control-")]
                present = {k.lower() for k, _ in headers}
                for name, value in TOKEN_PAGE_HEADERS.items():
                    if name.lower().encode("latin-1") not in present:
                        headers.append((name.lower().encode("latin-1"),
                                        value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, guarded_send)


# ---------------------------------------------------------------------------
# index.html config injection
# ---------------------------------------------------------------------------

_MODULE_SCRIPT = re.compile(r"<script\b[^>]*\btype\s*=\s*[\"']?module\b", re.IGNORECASE)


def inject_config(page: str, config: dict) -> str:
    """Insert ``<script>window.__GODSEYE__={...}</script>`` before the first
    ``<script type="module">`` (else before ``</head>``, else at the top).
    The JSON is escaped so no value can close the script element.
    """
    payload = (json.dumps(config, separators=(",", ":"))
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    tag = f"<script>window.__GODSEYE__={payload}</script>"
    m = _MODULE_SCRIPT.search(page)
    if m:
        return page[:m.start()] + tag + page[m.start():]
    i = page.lower().find("</head>")
    if i >= 0:
        return page[:i] + tag + page[i:]
    return tag + page


class _IndexCache:
    """Reads the built index.html once per (mtime, size) and injects config."""

    def __init__(self, path: Path, config: dict) -> None:
        self.path = path
        self.config = config
        self._key: tuple[int, int] | None = None
        self._body = ""
        self._lock = threading.Lock()

    def get(self) -> str:
        st = self.path.stat()
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if key != self._key:
                self._body = inject_config(self.path.read_text(encoding="utf-8"), self.config)
                self._key = key
            return self._body


def _not_built_page(ui_dir: Path) -> str:
    where = html_lib.escape(str(ui_dir))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{APP_TITLE}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: dark light; }}
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0;
         background: #05070a; color: #d8e1ea; display: grid; place-items: center;
         min-height: 100vh; padding: 16px; box-sizing: border-box; }}
  main {{ max-width: 40rem; }}
  code, pre {{ font: 13px ui-monospace, Menlo, monospace; }}
  pre {{ background: #10161d; padding: 12px; border-radius: 6px; overflow-x: auto; }}
</style></head>
<body><main>
<h1>The console UI is not built</h1>
<p>The server is running (sim, MCP, bridge and analyst APIs are up), but there is
no built UI at <code>{where}</code>.</p>
<p>Build it once, then restart:</p>
<pre>cd gods-eye-view
npm install
npm run build</pre>
<p>Or start the app with <code>./eye-in-the-sky</code> from the repository root,
which builds the UI when it is missing. Use <code>--ui-dir</code> or
<code>GODSEYE_UI_DIR</code> to point at a build elsewhere.</p>
</main></body></html>
"""


# ---------------------------------------------------------------------------
# sockets
# ---------------------------------------------------------------------------

def _refuse_if_listening(addr: str, port: int) -> None:
    """Raise EADDRINUSE when something already accepts on ``(addr, port)``.

    With SO_REUSEADDR, BSD/macOS lets a socket bind a SPECIFIC address on a
    port another process holds as a WILDCARD listener (``*:8780``): the bind
    succeeds, the host silently captures all loopback traffic for that port,
    and the "port busy" handling never fires. A listener on the wildcard
    accepts a loopback connect, so a successful connect means "in use".
    """
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    with contextlib.closing(socket.socket(family, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect((addr, port))
        except OSError:
            return                        # refused / unreachable: nobody listens
    raise OSError(errno.EADDRINUSE,
                  f"port {port} is already in use (a listener answers on {addr}:{port})")


def _bind(addr: str, port: int) -> socket.socket:
    if port:
        _refuse_if_listening(addr, port)
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((addr, port))
    except OSError:
        sock.close()
        raise
    return sock


def _free_port(addr: str = "127.0.0.1") -> int:
    with contextlib.closing(_bind(addr, 0)) as s:
        return s.getsockname()[1]


def honour_msgpack_bind_host() -> None:
    """Make msgpack-rpc's TCP server bind the host its ``Address`` names.

    ``msgpackrpc.transport.tcp.ServerTransport.listen`` passes only the PORT
    to tornado (``self._mp_server.listen(self._address.port)``), so
    ``FakeAirSim.start()``'s ``Address("127.0.0.1", port)`` binds ALL
    interfaces. Every human-in-the-loop gate (analyst policy, approval card,
    the server's fuel/BINGO/geofence gate) sits above MCP; an AirSim client on
    the LAN talking straight to the sim port bypasses all of them. Patched
    once per process, idempotently; the patched method honours whatever host
    the caller named (the fake sim names loopback).
    """
    from msgpackrpc.transport import tcp

    if getattr(tcp.ServerTransport.listen, "_godseye_honours_host", False):
        return

    def listen(self, server):
        self._server = server
        self._mp_server = tcp.MessagePackServer(
            self, io_loop=server._loop._ioloop, encodings=self._encodings)
        self._mp_server.listen(self._address.port, address=self._address.host)

    listen._godseye_honours_host = True   # type: ignore[attr-defined]
    tcp.ServerTransport.listen = listen


def sim_listen_addresses(sim: Any) -> list[str]:
    """The addresses the fake sim's RPC server actually listens on."""
    out: list[str] = []
    for listener in getattr(sim._server, "_listeners", []):
        for sock in getattr(listener._mp_server, "_sockets", {}).values():
            out.append(str(sock.getsockname()[0]))
    return out


def _assert_sim_loopback(sim: Any) -> None:
    """Refuse to run a fake sim reachable from anything but loopback."""
    import ipaddress

    try:
        addrs = sim_listen_addresses(sim)
    except Exception as exc:  # cannot verify => do not run
        raise RuntimeError(f"cannot verify the fake sim listens on loopback only: {exc}") from exc
    if not addrs:
        raise RuntimeError("cannot verify the fake sim listens on loopback only: no sockets")
    exposed = [a for a in addrs if not ipaddress.ip_address(a.split("%", 1)[0]).is_loopback]
    if exposed:
        raise RuntimeError(
            f"the fake sim listens on {', '.join(exposed)}, not loopback only; anything on "
            "the network could fly the aircraft with no approval, so the host refuses to run")


def _start_fake_sim(home, port: int):
    """FakeAirSim on ``port``, loopback only; port 0 = walk free ports (bind-retry)."""
    from .fake_airsim import FakeAirSim

    honour_msgpack_bind_host()
    attempts = 1 if port else 8
    last: OSError | None = None
    for _ in range(attempts):
        p = port or _free_port()
        sim = FakeAirSim(home=home, port=p)
        try:
            sim.start()
        except OSError as exc:
            last = exc
            with contextlib.suppress(Exception):
                sim.stop()
            continue
        try:
            _assert_sim_loopback(sim)
        except BaseException:
            with contextlib.suppress(Exception):
                sim.stop()
            raise
        return sim, p
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# store lock (two hosts on one store would replay each other's tasks)
# ---------------------------------------------------------------------------

def _lock_store(store_dir: Path):
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    fh = open(store_dir / ".host.lock", "a+")  # noqa: SIM115 - held for the process
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise RuntimeError(
            f"the store {store_dir} is in use by another Eye in the Sky host; "
            "stop it first or pass a different --store") from exc
    return fh


# ---------------------------------------------------------------------------
# the host
# ---------------------------------------------------------------------------

class Host:
    """Everything ``build_host`` wired. ``app`` is ``bridge_app`` (one app)."""

    app: FastAPI
    server: Any                 # GodseyeUavServer
    bridge_app: FastAPI
    intel: Any                  # intel_graph.IntelService | None
    chat: Any                   # chat.ChatService | None
    config: HostConfig
    token: str

    def __init__(self, **kw: Any) -> None:
        self.intel = None
        self.chat = None
        self.llm = None                      # llm_settings.LlmSettings | None
        self.llm_error: str | None = None
        self.intel_error: str | None = None
        self.chat_error: str | None = None
        self.chat_reason: str | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._stopping: asyncio.Event | None = None      # set by serve() at shutdown
        self._loop: asyncio.AbstractEventLoop | None = None   # the loop serve() runs on
        self._closed = False
        self._close_lock = threading.Lock()
        for k, v in kw.items():
            setattr(self, k, v)

    # -- lifecycle --------------------------------------------------------
    def request_stop(self) -> None:
        """Thread-safe: ask a running ``serve()`` to shut down gracefully."""
        self._stop_requested = True
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True

    @property
    def started(self) -> bool:
        return bool(self._uvicorn is not None and self._uvicorn.started)

    def close(self) -> None:
        """Release everything ``build_host`` acquired. Idempotent; ``serve()``
        calls it on exit, and a host that never served must call it itself.
        """
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        for sock in getattr(self, "sockets", []):
            with contextlib.suppress(Exception):
                sock.close()
        with contextlib.suppress(Exception):
            self.bridge_app.state.bridge.stop()
        srv = self.server
        with contextlib.suppress(Exception):
            srv.stop_monitor()
        # Let the monitor loop see its stop flag before the tasking loop goes
        # away, or asyncio logs "Task was destroyed but it is pending".
        fut = getattr(srv, "_monitor_task", None)
        if fut is not None:
            with contextlib.suppress(Exception):
                fut.result(timeout=3.0)
        with contextlib.suppress(Exception):
            srv.tasking.shutdown()
        with contextlib.suppress(Exception):
            self.store.close()
        if self.sim is not None:
            with contextlib.suppress(Exception):
                self.sim.stop()
        lock = getattr(self, "_store_lock", None)
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.close()

    # -- introspection ----------------------------------------------------
    def chat_summary(self) -> dict:
        """``chat`` block of ``/app/config``: available, model, reason?,
        provider? (``{id, label}`` only: the route has no auth, so no host,
        key flags or base URL; BYOK spec §8)."""
        model = self.config.model
        if self.chat is not None:
            try:
                st = dict(self.chat.status())
            except Exception as exc:  # noqa: BLE001 - config must not 500
                st = {"available": False, "reason": "error", "hint": str(exc)}
            out = {"available": bool(st.get("available")), "model": st.get("model", model)}
            if st.get("reason"):
                out["reason"] = st["reason"]
            ref = _provider_ref(st.get("provider"))
            if ref:
                out["provider"] = ref
            return out
        out = {"available": False, "reason": self.chat_reason or "error", "model": model}
        ref = _provider_ref(llm_provider_block(self.llm))
        if ref:
            out["provider"] = ref
        return out


def _bridge_ctx(bridge_app: FastAPI, adapter: Any, token: str) -> Any:
    """``bridge_app.state.godseye`` (contract §6), or an equivalent namespace
    when the bridge predates it.
    """
    ctx = getattr(bridge_app.state, "godseye", None)
    if ctx is not None:
        return ctx
    st = bridge_app.state

    def active_theater() -> dict:
        getter = getattr(st.bridge.feed, "active_theater", None)
        return getter() if callable(getter) else {}

    return SimpleNamespace(state=st.bridge, hub=st.hub, feed=st.feed, mcp=st.mcp,
                           adapter=adapter, active_theater=active_theater, token=token)


def _unavailable_router(prefix: str, error: str, auth: Callable[..., bool]) -> APIRouter:
    router = APIRouter()

    @router.api_route(prefix + "/{path:path}",
                      methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False)
    def _unavailable(path: str, _: bool = Depends(auth)) -> JSONResponse:
        return JSONResponse({"error": error}, status_code=503)

    return router


#: What ``/chat/status`` may say about the provider (never a key or a base URL).
_PROVIDER_BLOCK_KEYS = ("id", "label", "kind", "model_family", "host", "key_source",
                        "configured")


def llm_provider_block(llm: Any) -> dict | None:
    """The active provider as ``/chat/status`` shows it, straight from the
    settings (used when the analyst itself could not start). Asks the
    settings' keychain-free summary (``provider_status()``, else
    ``status()``); only ``_PROVIDER_BLOCK_KEYS`` survive. None without one.
    """
    if llm is None:
        return None
    raw = None
    for name in ("provider_status", "status"):
        fn = getattr(llm, name, None)
        if callable(fn):
            try:
                raw = fn()
            except Exception as exc:  # noqa: BLE001 - status must not 500
                log.warning("provider summary failed: %s", type(exc).__name__)
                return None
            break
    if inspect.isawaitable(raw):
        with contextlib.suppress(Exception):
            raw.close()
        return None
    if not isinstance(raw, dict):
        raw = {k: getattr(raw, k) for k in _PROVIDER_BLOCK_KEYS if hasattr(raw, k)}
    src = raw.get("provider") if isinstance(raw.get("provider"), dict) else raw
    block = {k: src.get(k, raw.get(k)) for k in _PROVIDER_BLOCK_KEYS}
    return block if block.get("id") else None


def _provider_ref(block: Any) -> dict | None:
    """``{id, label}`` of a provider block, or None."""
    if not isinstance(block, dict) or not block.get("id"):
        return None
    return {"id": str(block["id"]), "label": str(block.get("label") or "")}


def _chat_fallback_router(host: Host, auth: Callable[..., bool]) -> APIRouter:
    router = APIRouter()

    @router.get("/chat/status")
    def chat_status(_: bool = Depends(auth)) -> dict:
        out = {"available": False, "reason": host.chat_reason or "error",
               "hint": host.chat_error or "the analyst could not be started",
               "model": host.config.model}
        block = llm_provider_block(host.llm)
        if block:
            out["provider"] = block
        return out

    router.include_router(_unavailable_router("/chat", "chat_unavailable", auth))
    return router


def _chat_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError) and (exc.name or "").startswith("claude_agent_sdk"):
        return "sdk_missing"
    return "error"


def build_host(cfg: HostConfig) -> Host:
    """Wire sim + MCP + bridge + intel + chat + UI into ONE FastAPI app.

    Binds the listening socket(s) first (so a busy port fails before anything
    starts), then the sim, the MCP server and the bridge. Nothing listens
    until ``serve()``. On failure everything acquired so far is released.
    """
    bind_addr = LOOPBACK_BIND.get((cfg.host or "").strip().lower())
    if bind_addr is None:
        raise ValueError(
            f"host {cfg.host!r} is not a loopback address; the console page embeds "
            "the API token, so the app only binds 127.0.0.1, localhost or ::1")
    if cfg.sim_backend not in ("fake", "real"):
        raise ValueError(f"sim_backend must be 'fake' or 'real', not {cfg.sim_backend!r}")
    if cfg.sim_backend == "real" and not cfg.sim_port:
        raise ValueError("a real AirSim needs an explicit sim_port")
    url_host = "[::1]" if bind_addr == "::1" else "127.0.0.1"

    t = launch.resolve_theater(cfg.theater)         # KeyError names the known ids
    home = launch.home_geopoint(t)                  # MSL -> HAE, once (T1)
    token = cfg.token or secrets.token_urlsafe(24)
    store_dir = Path(cfg.store_dir or default_store_dir()).expanduser().resolve()
    ui_dir = Path(cfg.ui_dir).expanduser().resolve() if cfg.ui_dir else default_ui_dir()

    acquired: list[Callable[[], Any]] = []

    def unwind() -> None:
        for undo in reversed(acquired):
            with contextlib.suppress(Exception):
                undo()

    try:
        sockets = [_bind(bind_addr, cfg.port)]
        acquired.append(sockets[0].close)
        port = sockets[0].getsockname()[1]
        mcp_port = None
        if cfg.mcp_port is not None:
            if cfg.mcp_port == port:
                raise ValueError("mcp_port must differ from port")
            sockets.append(_bind(bind_addr, cfg.mcp_port))
            acquired.append(sockets[-1].close)
            mcp_port = sockets[-1].getsockname()[1]

        store_dir.mkdir(parents=True, exist_ok=True)
        store_lock = _lock_store(store_dir)
        if store_lock is not None:
            acquired.append(store_lock.close)

        sim = None
        sim_port = cfg.sim_port
        if cfg.sim_backend == "fake":
            sim, sim_port = _start_fake_sim(home, cfg.sim_port)
            acquired.append(sim.stop)

        import airsim  # vendored PythonClient; app.ensure_airsim_client() puts it on sys.path

        from .bridge import AirSimAdapter, create_app
        from .server import UavBackend
        from .store import Store

        client = airsim.MultirotorClient(port=sim_port)
        client.confirmConnection()
        backend = UavBackend(client, home, sim=sim)
        store = Store(store_dir)
        acquired.append(store.close)
        # The MCP auth metadata (401 WWW-Authenticate resource_metadata) names
        # THIS host's port, not the legacy stack's 8791.
        server = launch.build_server(t, backend, store, token=token,
                                     public_url=f"http://{url_host}:{port}")
        acquired.append(server.tasking.shutdown)
        if server.theater_mismatch is not None:
            raise RuntimeError(
                f"server resolved theater {server.theater.id!r} for a {t.id!r} "
                f"envelope: {server.theater_mismatch}")

        mcp_asgi = server.mcp.streamable_http_app(
            streamable_http_path=MCP_PATH, stateless_http=True, host="127.0.0.1")
        adapter = AirSimAdapter(port=sim_port, home=home)
        mcp_url = f"http://{url_host}:{port}{MCP_PATH}"
        app = create_app(adapter=adapter, token=token, start_loops=cfg.start_loops,
                         mcp_url=mcp_url, mcp_token=token)
        acquired.append(app.state.bridge.stop)
    except BaseException:
        unwind()
        raise

    host = Host(
        app=app, bridge_app=app, server=server, config=cfg, token=token,
        theater=t, home=home, sim=sim, sim_port=sim_port, store=store,
        store_dir=store_dir, backend=backend, adapter=adapter, sockets=sockets,
        port=port, mcp_port=mcp_port, url=f"http://{url_host}:{port}/",
        mcp_url=mcp_url, ui_dir=ui_dir, ui_built=(ui_dir / "index.html").is_file(),
        _store_lock=store_lock, _stop_requested=False,
    )
    try:
        _wire(host, mcp_asgi, adapter)
    except BaseException:
        host.close()
        raise
    return host


def _supported_kwargs(fn: Any, **kw: Any) -> dict:
    """The subset of ``kw`` that ``fn`` accepts (all of it for ``**kwargs``)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


def _load_llm_settings(host: Host) -> Any:
    """Import ``llm_settings`` and build ``LlmSettings(store_dir.parent,
    llm_env)`` (BYOK spec §12). Returns the module, or None after logging:
    the analyst then runs on the Claude login exactly as before the spec
    (the launch provider variables are gone from ``os.environ`` either way).
    """
    try:
        from . import llm_settings
        cls = llm_settings.LlmSettings
        cfg = host.config
        # --model/--effort lock those settings; the port is the app's own
        # host:port (a base URL may not point back at it).
        host.llm = cls(host.store_dir.parent, dict(cfg.llm_env or {}),
                       **_supported_kwargs(cls, store_dir=host.store_dir, model=cfg.model,
                                           effort=cfg.effort, app_port=host.port,
                                           cli_path=cfg.cli_path or frozen_cli_path()))
        return llm_settings
    except Exception as exc:  # noqa: BLE001 - the host boots without the settings
        host.llm = None
        host.llm_error = type(exc).__name__   # never the message: it could quote a value
        log.warning("analyst settings unavailable (%s); the analyst uses the Claude login",
                    host.llm_error)
        return None


def _settings_guard(llm_mod: Any, host: Host) -> tuple[type, dict] | None:
    """``(SettingsGuardMiddleware, kwargs)`` for this host's origins, checked
    by building one eagerly: Starlette builds middleware at the first
    request, where a bad signature would fail every route. None if unusable.
    """
    guard = getattr(llm_mod, "SettingsGuardMiddleware", None) if llm_mod else None
    if guard is None:
        return None
    ports = tuple(p for p in (host.port, host.mcp_port) if p)
    origins = tuple(f"http://{h}:{p}" for p in ports for h in ("127.0.0.1", "localhost", "[::1]"))
    kw = _supported_kwargs(guard, port=host.port, ports=ports, allowed_origins=origins,
                           origins=origins)

    async def _probe_app(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        return None

    try:
        guard(_probe_app, **kw)
    except Exception as exc:  # noqa: BLE001 - no guard, so no settings routes either
        log.warning("analyst settings guard unusable: %s", type(exc).__name__)
        return None
    return guard, kw


def _wire(host: Host, mcp_asgi: ASGIApp, adapter: Any) -> None:
    app, cfg, token = host.app, host.config, host.token
    auth, auth_sse = bearer_auth(token), sse_auth(token)
    llm_mod = _load_llm_settings(host)
    guard = _settings_guard(llm_mod, host)

    app.router.routes.append(Route(MCP_PATH, endpoint=mcp_asgi, name="mcp"))
    app.add_middleware(ShutdownDisconnectMiddleware, stopping=lambda: host._stopping,
                       loop=lambda: host._loop)
    app.add_middleware(TokenPageCorsGuardMiddleware)      # outside the bridge's CORS
    if guard is not None:
        # /settings/*: strips Origin before the bridge's CORS, refuses a
        # cross-origin caller (spec §7.5). Inside the Host check.
        app.add_middleware(guard[0], **guard[1])
    app.add_middleware(LoopbackHostMiddleware)            # outermost

    @app.get("/app/config", include_in_schema=False)
    def app_config() -> dict:
        return {"app": APP_NAME, "version": app_version(),
                "theater": {"id": host.theater.id, "label": host.theater.label},
                "chat": host.chat_summary(), "mcp_path": MCP_PATH,
                "ui": "built" if host.ui_built else "missing"}

    # -- intel ------------------------------------------------------------
    try:
        from . import intel_graph
        host.intel = intel_graph.IntelService(_bridge_ctx(app, adapter, token), host.server)
        app.include_router(intel_graph.intel_router(host.intel, auth))
    except Exception as exc:  # noqa: BLE001 - boot without the orb data, loudly
        host.intel = None
        host.intel_error = f"{type(exc).__name__}: {exc}"
        log.warning("intel graph unavailable: %s", host.intel_error)
        app.include_router(_unavailable_router("/intel", "intel_unavailable", auth))

    # -- analyst chat -------------------------------------------------------
    try:
        from . import chat
        cli_path = cfg.cli_path or frozen_cli_path()
        host.chat = chat.ChatService(
            server=host.server, intel=host.intel, store_dir=host.store_dir,
            model=cfg.model, effort=cfg.effort, cli_path=cli_path, enabled=cfg.chat,
            llm=host.llm)
        app.include_router(chat.chat_router(host.chat, auth, auth_sse))
    except Exception as exc:  # noqa: BLE001 - the console still works without the analyst
        host.chat = None
        host.chat_reason = _chat_failure_reason(exc)
        host.chat_error = f"{type(exc).__name__}: {exc}"
        log.warning("analyst chat unavailable: %s", host.chat_error)
        app.include_router(_chat_fallback_router(host, auth))

    # -- analyst settings (BYOK spec §7): header bearer only, never ?token= --
    settings_router = None
    if llm_mod is not None and guard is not None and host.llm is not None:
        try:
            make = llm_mod.llm_settings_router
            try:
                wants_chat = len(inspect.signature(make).parameters) >= 3
            except (TypeError, ValueError):
                wants_chat = False
            args = (host.llm, auth, host.chat) if wants_chat else (host.llm, auth)
            settings_router = make(*args)
        except Exception as exc:  # noqa: BLE001 - the rest of the app stays up
            host.llm_error = type(exc).__name__
            log.warning("analyst settings routes unavailable: %s", host.llm_error)
    app.include_router(settings_router
                       or _unavailable_router("/settings", "settings_unavailable", auth))

    # -- GEV node-only providers do not exist here ---------------------------
    @app.api_route("/api/{path:path}", include_in_schema=False,
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def api_not_available(path: str) -> JSONResponse:
        return JSONResponse({"error": "not_available_in_app_host"}, status_code=404)

    # -- the UI (static mount LAST: a root mount shadows later routes) --------
    if host.ui_built:
        index = _IndexCache(host.ui_dir / "index.html", {"bridgeUrl": "", "token": token})

        def index_page() -> HTMLResponse:
            return HTMLResponse(index.get(), headers=NO_STORE_HEADERS)

        app.add_api_route("/", index_page, methods=["GET"], include_in_schema=False)
        app.add_api_route("/index.html", index_page, methods=["GET"], include_in_schema=False)
        app.mount("/", StaticFiles(directory=str(host.ui_dir), html=True), name="ui")
    else:
        page = _not_built_page(host.ui_dir)

        def not_built() -> HTMLResponse:
            return HTMLResponse(page, headers=NO_STORE_HEADERS)

        app.add_api_route("/", not_built, methods=["GET"], include_in_schema=False)
        app.add_api_route("/index.html", not_built, methods=["GET"], include_in_schema=False)

    # -- lifespan: MCP session manager + safety monitor + chat teardown -------
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any):
        async with host.server.mcp.session_manager.run():
            host.server.start_monitor()
            try:
                async with inner(a) as state:
                    yield state
            finally:
                if host.chat is not None:
                    try:
                        await asyncio.wait_for(host.chat.shutdown(), timeout=10.0)
                    except Exception as exc:  # noqa: BLE001 - shutdown continues
                        log.warning("analyst shutdown: %s: %s", type(exc).__name__, exc)
                host.server.stop_monitor()

    app.router.lifespan_context = lifespan


async def serve(host: Host, *, ready: threading.Event | None = None) -> None:
    """Serve ``host.app`` on its socket(s) until cancelled or ``request_stop()``.

    Same app on the main port and the optional MCP compat port. At shutdown,
    open event streams are told the client went away, and anything still
    running after ``GRACEFUL_SHUTDOWN_S`` is cancelled. ``ready`` is set once
    the sockets accept connections. Releases the host's resources on exit.
    """
    config = uvicorn.Config(
        host.app, log_level="warning", access_log=False, lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
    server = uvicorn.Server(config)
    host._uvicorn = server
    if getattr(host, "_stop_requested", False):
        server.should_exit = True

    # The Event and the loop that owns it, set together: the middleware only
    # ever awaits the Event from this loop (see ShutdownDisconnectMiddleware).
    host._loop = asyncio.get_running_loop()
    host._stopping = asyncio.Event()

    async def watch() -> None:
        while not server.started:
            if server.should_exit:
                break
            await asyncio.sleep(0.02)
        else:
            if ready is not None:
                ready.set()
        while not server.should_exit:
            await asyncio.sleep(0.05)
        host._stopping.set()             # streams see http.disconnect and end

    task = asyncio.ensure_future(server.serve(sockets=list(host.sockets)))
    watcher = asyncio.ensure_future(watch())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        with contextlib.suppress(BaseException):
            await task
        raise
    finally:
        watcher.cancel()
        host.close()


def port_in_use(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.EADDRINUSE, errno.EACCES)
