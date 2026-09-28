"""Local stub of the Anthropic Messages API for the BYOK tests.

Adapted from the research's ``plumbing/stub_anthropic.py`` (stdlib here, so it
runs in-process with no extra dependency). Loopback only.

**It never stores a credential.** Each request records which header carried
one and a classification: ``test:<key>`` for the harness's fake keys,
``oauth`` (``sk-ant-oat...``), ``anthropic_key`` (``sk-ant-api...``),
``empty`` or ``other``; plus whether ``metadata.user_id`` names an account.

Behaviour comes from the server's ``mode`` or, per request, a path prefix
``/m/<mode>`` on the base URL (``stub.base("401")``):

``ok`` (default), ``tool`` (first turn calls the first tool whose name ends
with ``tool_suffix``), ``400`` ``401`` ``403`` ``404`` ``429`` ``500`` ``529``,
``reject_thinking`` (400 when ``thinking.type`` is adaptive), ``echo_key``
(401 echoing the credential), ``redirect`` (307 to ``/landing``, on this stub
or, with ``configure(redirect_to=<url>)``, on another origin), ``hang``,
``html`` (200 text/html).

``script`` (WG §4.5, A14): ``configure(mode="script", script=[[step, ...], ...])``.
Each inner list answers one operator message: the stub counts the user TEXT
messages in the conversation to pick the list, and the assistant ``tool_use``
turns since that message to pick the step. A step ``{"tool": "<bare name>",
"input": {...}, "text": "optional"}`` becomes ``tool_use`` ``mcp__godseye__<tool>``.
``{"$ref": "<tool>.<dotted.path>"}`` anywhere in ``input`` (or as the whole
input) resolves against the most recent ``tool_result`` of that tool; paths take
keys, numeric indices and ``[key=value]`` filters (``vehicles[name=Drone1].lat``).
An exhausted list replies "Done (scripted)."; a ref that does not resolve replies
"Script error: ..." and ends the turn. Requests that offer no ``mcp__`` tool
(the CLI's side calls) get "OK" and never consume a step. ``script_log()`` lists
what each scripted request did.

``messages()`` lists the engine's ``/v1/messages`` requests; the settings' own
HTTP checks (user agent ``eye-in-the-sky-...``) are in ``probes()``.
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

from . import pick_port

TEST_KEYS = frozenset({"test-key-123", "test-key-456"})
MODES = ("ok", "tool", "400", "401", "403", "404", "429", "500", "529", "reject_thinking",
         "echo_key", "redirect", "hang", "html", "script")
#: The analyst's in-process MCP server prefix (analyst_policy.TOOL_PREFIX).
SCRIPT_TOOL_PREFIX = "mcp__godseye__"
SCRIPT_DONE = "Done (scripted)."
_REF_TOKEN = re.compile(r"\[([^\]]*)\]|([^.\[\]]+)")
_REMINDER = "<system-reminder>"
THINKING_REJECTION = ("thinking.type: Input tag 'adaptive' found using 'type' does not match "
                      "any of the expected tags: 'disabled', 'enabled'")
_ERRORS = {
    "400": (400, "invalid_request_error", "stub: invalid request"),
    "401": (401, "authentication_error", "invalid x-api-key"),
    "403": (403, "permission_error", "stub: permission denied"),
    "429": (429, "rate_limit_error", "stub: rate limited"),
    "500": (500, "api_error", "stub: internal server error"),
    "529": (529, "overloaded_error", "Overloaded"),
}
_MODE_PREFIX = re.compile(r"^/m/([a-z0-9_]+)(/.*)?$")
_ACCOUNT_RX = re.compile(r"account_([0-9a-fA-F-]{8,})")


def classify_credential(value: str | None) -> str | None:
    """What kind of credential a header held (never the value itself, except
    for the harness's own fake keys)."""
    if value is None:
        return None
    if value in TEST_KEYS:
        return f"test:{value}"
    if not value:
        return "empty"
    if value.startswith("sk-ant-oat"):
        return "oauth"
    if value.startswith("sk-ant-api"):
        return "anthropic_key"
    return "other"


def _auth(headers) -> tuple[dict, str | None]:
    """(record, raw credential for echo mode)."""
    out: dict = {"authorization": None, "x_api_key": None}
    raw = None
    a = headers.get("authorization")
    if a is not None:
        scheme, _, token = a.partition(" ")
        out["authorization"] = {"scheme": scheme, "kind": classify_credential(token)}
        raw = token
    k = headers.get("x-api-key")
    if k is not None:
        out["x_api_key"] = classify_credential(k)
        raw = raw or k
    return out, raw


def _account_present(metadata) -> bool:
    uid = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(uid, str) or not uid:
        return False
    try:
        parsed = json.loads(uid)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return bool(parsed.get("account_uuid"))
    return bool(_ACCOUNT_RX.search(uid))


def _summary(body) -> dict:
    if not isinstance(body, dict):
        return {"parsed": False}
    msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    tool_results = 0
    for m in msgs:
        if isinstance(m.get("content"), list):
            tool_results += sum(1 for c in m["content"]
                                if isinstance(c, dict) and c.get("type") == "tool_result")
    return {
        "parsed": True, "model": body.get("model"), "max_tokens": body.get("max_tokens"),
        "stream": bool(body.get("stream")), "has_thinking": "thinking" in body,
        "thinking": body.get("thinking"),
        "tools": [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)],
        "messages": len(msgs), "roles": [m.get("role") for m in msgs],
        "tool_results": tool_results,
        "account_uuid_present": _account_present(body.get("metadata")),
    }


def _probe(rec: dict) -> bool:
    return str(rec.get("user_agent") or "").startswith("eye-in-the-sky-")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _message(model: str, blocks: list[dict], stop: str) -> dict:
    return {"id": "msg_stub" + uuid.uuid4().hex[:16], "type": "message", "role": "assistant",
            "model": model, "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 17, "output_tokens": 2, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}


def _stream(msg: dict) -> str:
    head = {**msg, "content": [], "stop_reason": None,
            "usage": {**msg["usage"], "output_tokens": 1}}
    out = [_sse("message_start", {"type": "message_start", "message": head}),
           _sse("ping", {"type": "ping"})]
    for i, blk in enumerate(msg["content"]):
        if blk["type"] == "text":
            out.append(_sse("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text",
                                                                      "text": ""}}))
            out.append(_sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                    "delta": {"type": "text_delta",
                                                              "text": blk["text"]}}))
        else:  # tool_use
            out.append(_sse("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": blk["id"], "name": blk["name"],
                                  "input": {}}}))
            out.append(_sse("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(blk["input"])}}))
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_sse("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": msg["stop_reason"],
                                                "stop_sequence": None},
                                      "usage": {"output_tokens": 2}}))
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(out)


class ScriptError(ValueError):
    """A scripted step could not be built (bad ref, missing result)."""


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
            and isinstance(c.get("text"), str)]


def _is_operator_message(msg: dict) -> bool:
    """A user message the operator typed: text that is not only CLI reminders,
    and no tool results."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return False
    return any(t.strip() and not t.lstrip().startswith(_REMINDER) for t in _texts(content))


def _bare(name: str) -> str:
    return name.removeprefix(SCRIPT_TOOL_PREFIX)


def _result_value(block: dict):
    """A tool_result's payload: its text parsed as JSON (whole, else the first
    text block that parses), else the raw text."""
    texts = _texts(block.get("content"))
    for candidate in ("".join(texts), *texts):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return "".join(texts)


def script_position(messages: list) -> tuple[int, int, dict]:
    """(operator message index, tool_use turns since it, latest result per tool).

    Index -1 means no operator message yet."""
    msgs = [m for m in messages if isinstance(m, dict)]
    names: dict[str, str] = {}
    results: dict[str, object] = {}
    op, steps = -1, 0
    for m in msgs:
        content = m.get("content") if isinstance(m.get("content"), list) else []
        if m.get("role") == "assistant":
            uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
            for c in uses:
                names[str(c.get("id"))] = _bare(str(c.get("name", "")))
            steps += 1 if uses else 0
        elif _is_operator_message(m):
            op, steps = op + 1, 0
        elif m.get("role") == "user":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tool = names.get(str(c.get("tool_use_id")))
                    if tool:
                        results[tool] = _result_value(c)
    return op, steps, results


def resolve_path(value, path: str):
    """Walk ``a.b.0.c`` / ``a[key=value].b`` / ``a[0]`` through ``value``."""
    cur = value
    for m in _REF_TOKEN.finditer(path):
        bracket, key = m.group(1), m.group(2)
        if bracket is not None and "=" in bracket:
            k, _, want = bracket.partition("=")
            if not isinstance(cur, list):
                raise ScriptError(f"[{bracket}] needs a list")
            hit = next((el for el in cur if isinstance(el, dict)
                        and str(el.get(k.strip())) == want.strip()), None)
            if hit is None:
                raise ScriptError(f"no element with {k.strip()}={want.strip()}")
            cur = hit
            continue
        token = (bracket if bracket is not None else key).strip()
        if isinstance(cur, list):
            if not token.lstrip("-").isdigit():
                raise ScriptError(f"{token!r} is not an index")
            try:
                cur = cur[int(token)]
            except IndexError:
                raise ScriptError(f"index {token} out of range") from None
        elif isinstance(cur, dict):
            if token not in cur:
                raise ScriptError(f"no key {token!r}")
            cur = cur[token]
        else:
            raise ScriptError(f"cannot read {token!r} from a {type(cur).__name__}")
    return cur


def resolve_refs(value, results: dict):
    """Replace every ``{"$ref": "<tool>.<path>"}`` in ``value``."""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref = str(value["$ref"])
            tool, _, path = ref.partition(".")
            if tool not in results:
                raise ScriptError(f"{ref}: no result from {tool} yet")
            return resolve_path(results[tool], path) if path else results[tool]
        return {k: resolve_refs(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, results) for v in value]
    return value


def script_reply(script, messages: list, tool_names: list[str]) -> tuple[list[dict], str, dict]:
    """(blocks, stop_reason, log entry) for one scripted request."""
    if not any(str(n).startswith("mcp__") for n in tool_names):
        return [{"type": "text", "text": "OK"}], "end_turn", {"side_call": True}
    op, steps, results = script_position(messages)
    lists = script if isinstance(script, list) else []
    log = {"message": op, "step": steps}
    if op < 0 or op >= len(lists) or steps >= len(lists[op] or []):
        return [{"type": "text", "text": SCRIPT_DONE}], "end_turn", {**log, "done": True}
    step = lists[op][steps]
    try:
        tool = str(step["tool"])
        args = resolve_refs(step.get("input", {}), results)
        if not isinstance(args, dict):
            raise ScriptError(f"{tool}: the input resolved to a {type(args).__name__}")
    except (KeyError, TypeError, ScriptError) as exc:
        text = f"Script error: {exc}"
        return [{"type": "text", "text": text}], "end_turn", {**log, "error": str(exc)}
    blocks = [{"type": "text", "text": str(step.get("text") or f"Calling {tool}.")},
              {"type": "tool_use", "id": "toolu_stub" + uuid.uuid4().hex[:16],
               "name": SCRIPT_TOOL_PREFIX + tool, "input": args}]
    return blocks, "tool_use", {**log, "tool": tool, "input": args}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, fmt: str, *args: object) -> None:  # quiet
        return

    def _body(self) -> bytes:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            raw = b"".join(chunks)
        else:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.headers.get("content-encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw

    def _send(self, status: int, body: str | bytes, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        data = body.encode() if isinstance(body, str) else body
        self._record(status)  # before the reply: a client may look right after it
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", "req_stub_" + uuid.uuid4().hex[:12])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, etype: str, message: str, extra: dict | None = None) -> None:
        self._send(status, json.dumps({"type": "error", "error": {"type": etype,
                                                                  "message": message}}),
                   extra=extra)

    def _handle(self) -> None:
        raw = self._body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        path = self.path.split("?", 1)[0]
        mode = self.server.mode
        m = _MODE_PREFIX.match(path)
        if m:
            mode, path = m.group(1), m.group(2) or "/"
        auth, credential = _auth(self.headers)
        rec = {"method": self.command, "path": path, "mode": mode, "auth": auth,
               "anthropic_beta": self.headers.get("anthropic-beta"),
               "user_agent": self.headers.get("user-agent"), "body": _summary(body)}
        self._rec = rec
        status = self._route(mode, path, body, credential)
        self._record(status)

    def _record(self, status: int) -> None:
        rec, self._rec = getattr(self, "_rec", None), None
        if rec is not None:
            rec["status"] = status
            self.server.record(rec)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def _route(self, mode: str, path: str, body, credential: str | None) -> int:
        if path.startswith("/landing"):
            self._send(200, json.dumps(_message("landing", [{"type": "text", "text": "x"}],
                                                "end_turn")))
            return 200
        if path == "/v1/models" and self.command == "GET":
            data = [{"type": "model", "id": "stub-model", "display_name": "Stub"}]
            self._send(200, json.dumps({"data": data, "has_more": False}))
            return 200
        if path == "/v1/key" and self.command == "GET":
            if mode == "401":
                self._error(401, "authentication_error", "invalid key")
                return 401
            self._send(200, json.dumps({"data": {"label": "stub", "usage": 0, "limit": None}}))
            return 200
        if path == "/v1/messages/count_tokens":
            self._send(200, json.dumps({"input_tokens": 42}))
            return 200
        if path != "/v1/messages" or self.command != "POST":
            self._error(404, "not_found_error", f"stub: no route {self.command} {path}")
            return 404
        return self._messages(mode, body if isinstance(body, dict) else {}, credential)

    def _messages(self, mode: str, body: dict, credential: str | None) -> int:
        model = str(body.get("model") or "")
        if mode in _ERRORS:
            status, etype, msg = _ERRORS[mode]
            extra = {"retry-after": self.server.retry_after} if status in (429, 529) else None
            self._error(status, etype, msg, extra)
            return status
        if mode == "404":
            self._error(404, "not_found_error", f"model: {model}")
            return 404
        if mode == "echo_key":
            self._error(401, "authentication_error", f"invalid api key: {credential}")
            return 401
        if mode == "redirect":
            target = self.server.redirect_to or self.server.url
            self._send(307, "", extra={"location": f"{target}/landing/v1/messages"})
            return 307
        if mode == "html":
            self._send(200, "<html><body>not an API</body></html>", ctype="text/html")
            return 200
        if mode == "hang":
            time.sleep(self.server.hang_s)
        thinking = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
        if mode == "reject_thinking" and thinking.get("type") == "adaptive":
            self._error(400, "invalid_request_error", THINKING_REJECTION)
            return 400
        blocks, stop = self._reply(mode, body, model)
        msg = _message(model, blocks, stop)
        if body.get("stream"):
            self._send(200, _stream(msg), ctype="text/event-stream")
        else:
            self._send(200, json.dumps(msg))
        return 200

    def _reply(self, mode: str, body: dict, model: str) -> tuple[list[dict], str]:
        msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        if mode == "script":
            names = [t.get("name", "") for t in body.get("tools") or [] if isinstance(t, dict)]
            blocks, stop, entry = script_reply(self.server.script, msgs, names)
            self.server.log_script(entry)
            return blocks, stop
        # The CLI may put a role:"system" message after the tool results:
        # the turn's state is in the last USER message.
        users = [m for m in msgs if m.get("role") == "user"]
        last = users[-1] if users else {}
        content = last.get("content")
        results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"
                   ] if isinstance(content, list) else []
        if mode == "tool" and not results:
            names = [t.get("name", "") for t in body.get("tools") or [] if isinstance(t, dict)]
            pick = next((n for n in names if n.endswith(self.server.tool_suffix)), None)
            if pick:
                return ([{"type": "text", "text": "Calling a tool."},
                         {"type": "tool_use", "id": "toolu_stub" + uuid.uuid4().hex[:16],
                          "name": pick, "input": dict(self.server.tool_input)}], "tool_use")
        if results:
            return [{"type": "text", "text": "FINAL: the tool answered."}], "end_turn"
        return [{"type": "text", "text": "OK"}], "end_turn"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int]):
        super().__init__(addr, _Handler)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.mode = "ok"
        self.retry_after = "1"
        self.hang_s = 30.0
        self.tool_suffix = ""
        self.tool_input: dict = {}
        self.redirect_to = ""
        self.url = ""
        self.script: list = []
        self.script_entries: list[dict] = []

    def record(self, rec: dict) -> None:
        with self.lock:
            self.requests.append(rec)

    def log_script(self, entry: dict) -> None:
        with self.lock:
            self.script_entries.append(entry)


class StubAnthropic:
    """``with StubAnthropic() as stub: stub.base("401") ... stub.requests``"""

    def __init__(self, host: str = "127.0.0.1", port: int | None = None, *, mode: str = "ok"):
        self._server = _Server((host, pick_port(host) if port is None else port))
        self.host, self.port = self._server.server_address[:2]
        self._server.url = self.url
        self._server.mode = mode
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stub-anthropic", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def base(self, mode: str | None = None) -> str:
        """The API root to configure (``/m/<mode>`` selects a behaviour)."""
        return f"{self.url}/m/{mode}" if mode else self.url

    def configure(self, **kw: object) -> None:
        """``mode``, ``retry_after``, ``hang_s``, ``tool_suffix``, ``tool_input``,
        ``redirect_to`` (the origin ``redirect`` mode sends the client to),
        ``script`` (a list of step lists, one per operator message)."""
        for k, v in kw.items():
            if not hasattr(self._server, k) or k in ("lock", "requests", "url",
                                                      "script_entries"):
                raise AttributeError(k)
            if k == "script" and not (isinstance(v, list) and all(
                    isinstance(steps, list) for steps in v)):
                raise TypeError("script is a list of step lists")
            setattr(self._server, k, v)

    def script_log(self) -> list[dict]:
        """One entry per scripted request: ``{message, step, tool, input}``,
        ``{..., done}``, ``{..., error}`` or ``{side_call}``."""
        with self._server.lock:
            return list(self._server.script_entries)

    @property
    def requests(self) -> list[dict]:
        with self._server.lock:
            return list(self._server.requests)

    def messages(self) -> list[dict]:
        """The engine's ``/v1/messages`` requests (not the settings' probes)."""
        return [r for r in self.requests if r["path"] == "/v1/messages" and not _probe(r)]

    def probes(self) -> list[dict]:
        """Requests from the settings' own HTTP checks (quick check, redirect probe)."""
        return [r for r in self.requests if _probe(r)]

    def reset(self) -> None:
        with self._server.lock:
            self._server.requests.clear()
            self._server.script_entries.clear()

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
