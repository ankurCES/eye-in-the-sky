"""The in-app AI analyst: chat sessions over the Claude Agent SDK (contract §3, §5).

One ``ChatService`` per host.  Each chat session gets:

* an **actor task** that owns its ``ClaudeSDKClient`` for the client's whole
  life -- ``connect``, ``query``, ``receive_response`` and ``disconnect`` all run
  in that one task.  HTTP handlers never touch the client: they enqueue turns
  and resolve approval futures (same event loop).  ``interrupt`` is sent by a
  short-lived watcher task the actor itself spawns for each turn, which is the
  SDK's documented cross-task pattern (a consumer iterating
  ``receive_response`` while another task calls ``interrupt()``);
* a bounded (500) **event log** with a monotonically increasing ``seq``, so an
  SSE client can reconnect with ``Last-Event-ID`` and replay what it missed;
* an **approval broker**: ``can_use_tool`` classifies every call
  (analyst_policy.classify); read/plan calls run at once, everything else parks
  on an ``asyncio.Future`` that the operator resolves from the UI.  Ten minutes
  without an answer -> ``expired`` (denied); an interrupt -> ``cancelled``.
  "Allow for session" is remembered per session PER TOOL, and only where
  ``Decision.allow_session`` is true (sensor tools; contract v1.1 §10.2).

The SDK is imported lazily (``claude_agent_sdk`` is an optional extra); tests
inject a fake module through ``sdk=``.

Transcripts: the Claude CLI writes each conversation to
``~/.claude/projects/<encoded cwd>/<session>.jsonl`` whatever the options say;
the analyst's cwd is ``<store>/analyst``, so they land under that encoded path.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import pathlib
import re
import shutil
import time
import uuid
import warnings
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Request
from pydantic import BaseModel, Field

from .analyst_policy import (
    COMMAND,
    DRY_RUN_TOOLS,
    SDK_SERVER_NAME,
    Decision,
    auto_track_vehicle,
    bare_name,
    classify,
    mission_kind,
    mission_vehicle,
)
from .analyst_toolbelt import build_toolbelt, dumps_compact, extract_entities, shrink

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
MODEL_ENV = "GODSEYE_CHAT_MODEL"
EFFORT_ENV = "GODSEYE_CHAT_EFFORT"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

EVENT_LOG_SIZE = 500
MAX_TEXT_CHARS = 8000
MAX_TURNS = 40
MAX_SESSIONS = 8
HEARTBEAT_S = 15.0
#: A session's Claude CLI (about 190 MB resident) is disconnected after this
#: long without a turn. The session, its event log and its grants stay; the
#: next message reconnects with ``resume`` (contract §3 is unchanged).
IDLE_DISCONNECT_S = 600.0
DISPLAY_ARGS_CHARS = 4000

SIGN_IN_HINT = ("Sign in with the claude CLI (`claude` then /login) or set "
                "ANTHROPIC_API_KEY")
SDK_MISSING_HINT = ("Install the analyst extra: pip install 'godseye-uav[app]' "
                    "(it brings claude-agent-sdk, which bundles the Claude CLI).")
CLI_MISSING_HINT = ("The Claude CLI was not found. Reinstall claude-agent-sdk (it bundles "
                    "the CLI) or install Claude Code so `claude` is on PATH.")
DISABLED_HINT = "The analyst is turned off for this launch."

#: What the CLI says when ``--resume`` names a conversation it has no transcript
#: for (the CLI died before writing it, or Claude Code cleaned it up).
_NO_CONVERSATION_RX = re.compile(r"(?i)no conversation found")
RESUME_LOST_MESSAGE = ("The analyst could not resume this conversation: Claude Code has no "
                       "transcript for it. Send the message again to start a fresh "
                       "conversation; the earlier context is lost.")

_AUTH_RX = re.compile(r"(?i)(authenticat|unauthori[sz]ed|invalid api key|not logged in|"
                      r"/login|oauth|\b401\b|credential)")
# POI ids carry the POI's name, which may contain spaces ("poi:default:North Field").
_FOCUS_ID_RX = re.compile(r"^(?:veh|msn|trk|unit|ob|rpt|thr|poi|alarm|feed):[^\r\n\[\]|]{1,160}$")

_ASSISTANT_ERRORS = {
    "authentication_failed": ("The analyst could not sign in to Claude.", SIGN_IN_HINT, False),
    "billing_error": ("The Claude account has a billing problem.",
                      "Check the account's plan or credits, or set ANTHROPIC_API_KEY.", False),
    "rate_limit": ("Claude usage limit reached.", "Wait for the limit to reset, then retry.",
                   True),
    "invalid_request": ("Claude rejected the request.", None, False),
    "server_error": ("Claude had a server error.", "Retry in a moment.", True),
    "unknown": ("The analyst hit an unknown error.", None, True),
}

_GATE_KEYS = ("ok", "required_pct", "available_pct", "plan_fuel_pct", "return_fuel_pct",
              "reserve_pct", "bingo_latched", "bingo_fuel_pct", "envelope_violations",
              "warnings", "est_time_s", "est_distance_m")

#: (payload key, singular) for list results summarised as "N <noun>s".
_LIST_NOUNS = (("vehicles", "vehicle"), ("results", "result"), ("tracks", "track"),
               ("contacts", "contact"), ("detections", "detection"),
               ("classes", "equipment class"), ("conflicts", "conflict"),
               ("assessments", "assessment"))

_CLOSE = object()


class ChatError(Exception):
    """Base for the service's caller-facing errors."""


class NotFound(ChatError):
    """Unknown (or closed) session, or unknown / already-resolved approval."""


class Busy(ChatError):
    """A turn is already running in this session (HTTP 409)."""

    def __init__(self, turn_id: str | None = None, message: str = "a turn is running"):
        super().__init__(message)
        self.turn_id = turn_id


class NotAllowed(ChatError):
    """``approve_session`` for a class that must be approved call by call (HTTP 422)."""


class Unavailable(ChatError):
    """The analyst cannot run (SDK missing, CLI missing, disabled)."""

    def __init__(self, reason: str, hint: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


# ----------------------------------------------------------------- session --

@dataclass
class _Pending:
    approval_id: str
    call_id: str
    tool: str
    decision: Decision
    future: asyncio.Future
    expires_at_ms: int


@dataclass
class _Turn:
    turn_id: str
    text_chars: int = 0
    thinking_chars: int = 0
    any_text: bool = False
    stop: str | None = None
    error: str | None = None
    errors_emitted: set = field(default_factory=set)
    got_result: bool = False
    ended: bool = False


class _Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.log: deque = deque(maxlen=EVENT_LOG_SIZE)
        self.seq = 0
        self.changed = asyncio.Event()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.actor: asyncio.Task | None = None
        self.turn: _Turn | None = None
        self.last_turn_id: str | None = None
        self.pending: dict[str, _Pending] = {}
        #: tool -> since_ms: sensor tools the operator allowed for this session.
        self.grants: dict[str, int] = {}
        self.calls: dict[str, dict] = {}
        self.approved_calls: dict[str, bool] = {}
        #: call ids the operator denied / let expire / interrupted (never ran).
        self.not_run: dict[str, bool] = {}
        #: (mission kind, vehicle) -> (summary, EFFECTIVE plan args)
        self.dry_runs: dict[tuple[str, str], tuple[dict, dict]] = {}
        self.interrupt_evt = asyncio.Event()
        self.closed = False
        self.last_active = time.monotonic()
        self.claude_session_id: str | None = None
        #: tool -> {param: default} from the server's input schemas (toolbelt).
        self.tool_defaults: dict[str, dict] = {}
        self.rate_limit: dict | None = None
        self.stderr_tail: deque = deque(maxlen=30)
        # ResultMessage.total_cost_usd is cumulative per CLI process (measured:
        # 0.289 -> 0.322 -> 0.338 over three turns), so per-turn cost is a delta.
        self.cost_base = 0.0
        self.cost_total = 0.0

    def emit(self, name: str, data: dict) -> int:
        self.seq += 1
        self.log.append((self.seq, name, data))
        self.last_active = time.monotonic()
        self.notify()
        return self.seq

    def notify(self) -> None:
        ev, self.changed = self.changed, asyncio.Event()
        ev.set()

    def remember_call(self, call_id: str, record: dict) -> None:
        self.calls[call_id] = record
        while len(self.calls) > 300:
            self.calls.pop(next(iter(self.calls)))

    def mark_approved(self, call_id: str) -> None:
        self.approved_calls[call_id] = True
        while len(self.approved_calls) > 300:
            self.approved_calls.pop(next(iter(self.approved_calls)))

    def mark_not_run(self, call_id: str) -> None:
        self.not_run[call_id] = True
        while len(self.not_run) > 300:
            self.not_run.pop(next(iter(self.not_run)))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _display_args(args: Any) -> Any:
    shaped, _ = shrink(args if isinstance(args, dict) else {}, DISPLAY_ARGS_CHARS)
    return shaped


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{block.get('type', 'block')}]")
        else:
            parts.append(str(block))
    return "".join(parts)


def _error_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "error")[:300]
    if isinstance(err, str) and err:
        return err[:300]
    return None


def _count_items(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    n = 0
    for item in value:
        if isinstance(item, dict) and item.get("_truncated") is True and "_omitted" in item:
            n += int(item.get("_omitted") or 0)
        else:
            n += 1
    return n


def _result_summary(tool: str, payload: Any, *, ok: bool, rejected: bool, error: str | None,
                    nbytes: int) -> str:
    if error:
        return f"Failed: {error}"[:200]
    if not isinstance(payload, dict):
        return f"Returned {nbytes:,} bytes"
    if rejected:
        gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
        viol = gate.get("envelope_violations") or []
        bits = ["Rejected by the server gate"]
        if gate.get("bingo_latched"):
            bits.append("BINGO latched")
        req, avail = gate.get("required_pct"), gate.get("available_pct")
        if isinstance(req, (int, float)) and isinstance(avail, (int, float)) and req > avail:
            bits.append(f"needs {req:.0f}% fuel, has {avail:.0f}%")
        if viol:
            bits.append(", ".join(str(v) for v in viol[:3]))
        if isinstance(payload.get("error"), str):
            bits.append(payload["error"][:120])
        return " · ".join(bits)[:200]
    if payload.get("status") == "busy":
        return "Vehicle busy with another task"
    if payload.get("refused") is True:
        return f"Refused: {payload.get('reason', 'safety transition in force')}"[:200]
    if payload.get("accepted") is False:
        return f"Not accepted: {payload.get('reason', 'no reason given')}"[:200]
    bits = []
    if payload.get("dry_run") is True or payload.get("executed") is False:
        gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
        bits.append("Gate passed" if gate.get("ok") else "Gate would reject")
        eta = payload.get("est_time_s")
        if isinstance(eta, (int, float)):
            bits.append(f"about {eta / 60:.0f} min")
        fuel = payload.get("est_fuel_pct")
        if isinstance(fuel, (int, float)):
            bits.append(f"{fuel:.0f}% fuel")
    else:
        mid = payload.get("mission_id") or payload.get("mission_handle")
        if isinstance(mid, str):
            bits.append(mid)
        if isinstance(payload.get("state"), str):
            bits.append(payload["state"])
        if isinstance(payload.get("status"), str) and payload["status"] not in bits:
            bits.append(payload["status"])
        for key, noun in _LIST_NOUNS:
            n = _count_items(payload.get(key))
            if n is not None:
                plural = noun if n == 1 else (noun + "es" if noun.endswith("s") else noun + "s")
                bits.append(f"{n} {plural}")
                break
        else:
            n = payload.get("count")
            if isinstance(n, int) and not isinstance(n, bool):
                bits.append(f"{n} result{'' if n == 1 else 's'}")
    if not bits:
        bits.append("OK" if ok else "Done")
    if payload.get("_truncated"):  # True, or a {dropped, ...} report from IntelService
        bits.append("truncated")
    bits.append(f"{nbytes / 1024:.1f} KB" if nbytes >= 1024 else f"{nbytes} B")
    return " · ".join(bits)[:200]


#: Arguments that do not change a plan's geometry, fuel or gate verdict.
_NON_PLAN_ARGS = ("dry_run", "idempotency_key", "kind", "lost_link_plan", "detail", "top_n")
#: Tools that route through the server's mission dispatcher (per-kind defaults).
_DISPATCH_TOOLS = frozenset({"uav_mission", "mission_dry_run"})


def _as_dict(value: Any) -> dict:
    """``params`` as the server will see it: a dict, or a JSON-object string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _plan_args(args: Any) -> dict:
    """The planning inputs of a mission call as supplied, flattened
    (``params`` merged in, top-level values win, unset/None values dropped)."""
    a = dict(args) if isinstance(args, dict) else {}
    params = _as_dict(a.pop("params", None))
    merged = {**params, **{k: v for k, v in a.items() if v is not None}}
    merged = {k: v for k, v in merged.items() if v is not None}
    for key in _NON_PLAN_ARGS:
        merged.pop(key, None)
    return json.loads(dumps_compact(merged))  # canonical JSON types for comparison


def _effective_plan(tool: str, args: Any, *, schema_defaults: dict | None = None,
                    kind_defaults: dict | None = None) -> dict:
    """The plan a mission call will ACTUALLY fly: the supplied planning inputs
    over the defaults of the code path that call takes.

    A dry run and the live call it previews often take different paths
    (``mission_dry_run(kind=orbit_poi)`` goes through the server's mission
    dispatcher, ``uav_orbit_poi`` is a discrete tool), and each path fills
    omitted parameters with its own defaults. Comparing only what the model
    typed claimed a match while the two paths planned different orbits (the
    dispatcher defaulted radius_m to 80 m, the tool to 150 m). Defaults come
    from the server: ``kind_defaults`` = ``GodseyeUavServer.MISSION_PARAM_DEFAULTS``
    for the dispatcher, ``schema_defaults`` = the discrete tools' input-schema
    defaults. A path whose defaults are unknown contributes none, so a
    comparison with one side known and the other not comes out unequal.
    """
    name = bare_name(tool)
    supplied = _plan_args(args)
    if name in _DISPATCH_TOOLS:
        kind = mission_kind(name, args if isinstance(args, dict) else {})
        # the dispatcher's legacy spellings (server._dispatch_mission)
        legacy_alt = supplied.pop("alt_m", None)
        if "alt_agl_m" not in supplied and legacy_alt is not None:
            supplied["alt_agl_m"] = legacy_alt
        overlap = supplied.pop("overlap", None)
        if overlap is not None and kind == "grid_search":
            supplied.setdefault("overlap_pct", overlap)
        elif overlap is not None and kind == "recon_route":
            supplied.setdefault("forward_overlap_pct", overlap)
        defaults = (kind_defaults or {}).get(kind or "") or {}
    else:
        defaults = (schema_defaults or {}).get(name) or {}
    out = {k: v for k, v in defaults.items() if v is not None and k not in _NON_PLAN_ARGS}
    out.update(supplied)
    return json.loads(dumps_compact(out))


def _load_prompt() -> str:
    try:
        from importlib.resources import files

        return files("godseye_uav").joinpath("analyst_prompt.md").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 -- fall back to the source tree, then a stub
        path = pathlib.Path(__file__).with_name("analyst_prompt.md")
        if path.is_file():
            return path.read_text(encoding="utf-8")
        log.warning("analyst_prompt.md is missing; the analyst runs with a stub prompt")
        return ("You are the ISR analyst inside the Eye in the Sky console. ISR only: observe, "
                "classify and report; never plan or recommend engagement. The operator approves "
                "every command in the UI; never claim a command ran until its result says so.")


# ----------------------------------------------------------------- service --

class ChatService:
    def __init__(self, *, server: Any, intel: Any, store_dir: pathlib.Path,
                 model: str | None = None, effort: str | None = None,
                 cli_path: str | None = None, sdk: Any = None,
                 approval_timeout_s: float = 600.0, enabled: bool = True):
        self._server = server
        self._intel = intel
        self._store_dir = pathlib.Path(store_dir)
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        raw_effort = effort or os.environ.get(EFFORT_ENV) or None
        self.effort = raw_effort if raw_effort in EFFORT_LEVELS else None
        if raw_effort and self.effort is None:
            log.warning("ignoring unknown analyst effort %r (use one of %s)",
                        raw_effort, ", ".join(EFFORT_LEVELS))
        self._cli_path = cli_path
        self._sdk = sdk
        self._sdk_error: str | None = None
        self._sdk_checked = sdk is not None
        self._approval_timeout_s = float(approval_timeout_s)
        self._enabled = bool(enabled)
        self._sessions: dict[str, _Session] = {}
        self._prompt: str | None = None
        self.heartbeat_s = HEARTBEAT_S
        #: Seconds without a turn before a session's CLI is disconnected (0 = never).
        self.idle_disconnect_s = IDLE_DISCONNECT_S
        if sdk is not None:
            self._configure_sdk(sdk)

    # ------------------------------------------------------------ plumbing --
    @staticmethod
    def _configure_sdk(sdk: Any) -> None:
        # Our read tools are deliberately in allowed_tools (auto-approved, never
        # routed to can_use_tool); the SDK warns about exactly that.
        category = getattr(sdk, "CanUseToolShadowedWarning", None)
        if isinstance(category, type) and issubclass(category, Warning):
            warnings.filterwarnings("ignore", category=category)

    def _ensure_sdk(self) -> Any:
        if not self._sdk_checked:
            self._sdk_checked = True
            try:
                self._sdk = importlib.import_module("claude_agent_sdk")
                self._configure_sdk(self._sdk)
            except Exception as exc:  # noqa: BLE001 -- ImportError or a broken install
                self._sdk = None
                self._sdk_error = f"{type(exc).__name__}: {exc}"
        return self._sdk

    def _cli_available(self) -> bool:
        if self._cli_path:
            p = pathlib.Path(self._cli_path)
            return p.is_file() and os.access(p, os.X_OK)
        sdk_file = getattr(self._sdk, "__file__", None)
        if not sdk_file:
            return True  # an injected SDK manages its own transport
        name = "claude.exe" if os.name == "nt" else "claude"
        if (pathlib.Path(sdk_file).parent / "_bundled" / name).is_file():
            return True
        if shutil.which("claude"):
            return True
        home = pathlib.Path.home()
        for loc in (home / ".npm-global/bin/claude", pathlib.Path("/usr/local/bin/claude"),
                    home / ".local/bin/claude", home / "node_modules/.bin/claude",
                    home / ".yarn/bin/claude", home / ".claude/local/claude"):
            if loc.is_file():
                return True
        return False

    def _availability(self) -> tuple[bool, str | None, str | None]:
        if not self._enabled:
            return False, "disabled", DISABLED_HINT
        if self._ensure_sdk() is None:
            return False, "sdk_missing", SDK_MISSING_HINT
        if not self._cli_available():
            return False, "cli_missing", CLI_MISSING_HINT
        return True, None, None

    def status(self) -> dict:
        ok, reason, hint = self._availability()
        out: dict = {"available": ok, "model": self.model}
        if reason:
            out["reason"] = reason
        if hint:
            out["hint"] = hint
        if self.effort:
            out["effort"] = self.effort
        return out

    def _get(self, sid: str) -> _Session:
        s = self._sessions.get(sid)
        if s is None or s.closed:
            raise NotFound(f"no chat session {sid!r}")
        return s

    def _prompt_text(self) -> str:
        if self._prompt is None:
            self._prompt = _load_prompt()
        return self._prompt

    def _analyst_dir(self) -> pathlib.Path:
        path = self._store_dir / "analyst"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------- sessions --
    async def create_session(self) -> str:
        if len(self._sessions) >= MAX_SESSIONS:
            idle = sorted((s for s in self._sessions.values() if s.turn is None),
                          key=lambda s: s.last_active)
            if not idle:
                raise Busy(None, "too many analyst sessions are running")
            await self.close_session(idle[0].sid)
        sid = uuid.uuid4().hex
        self._sessions[sid] = _Session(sid)
        return sid

    async def post_message(self, sid: str, text: str, context: dict | None = None) -> str:
        s = self._get(sid)
        ok, reason, hint = self._availability()
        if not ok:
            raise Unavailable(reason or "unavailable", hint)
        text = (text or "").strip()
        if not text:
            raise ValueError("text is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text is longer than {MAX_TEXT_CHARS} characters")
        if s.turn is not None:
            raise Busy(s.turn.turn_id)
        turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        s.turn = _Turn(turn_id)
        s.last_turn_id = turn_id
        s.interrupt_evt = asyncio.Event()
        s.emit("turn_start", {"turn_id": turn_id, "text": text})
        prompt = self._compose_prompt(text, context)
        if s.actor is None or s.actor.done():
            s.actor = asyncio.create_task(self._actor(s), name=f"godseye-analyst-{sid[:8]}")
        s.inbox.put_nowait((turn_id, prompt))
        return turn_id

    @staticmethod
    def _compose_prompt(text: str, context: dict | None) -> str:
        ids: list[str] = []
        if isinstance(context, dict) and isinstance(context.get("focused_ids"), list):
            for raw in context["focused_ids"]:
                if isinstance(raw, str) and _FOCUS_ID_RX.match(raw) and raw not in ids:
                    ids.append(raw)
                if len(ids) >= 20:
                    break
        if not ids:
            return text
        refs = ", ".join(f"[[{i}]]" for i in ids)
        return f"[Console context: the operator has these entities focused: {refs}]\n\n{text}"

    async def resolve_approval(self, sid: str, approval_id: str, decision: str,
                               note: str | None = None) -> None:
        s = self._get(sid)
        if decision not in ("approve", "deny", "approve_session"):
            raise ValueError(f"unknown decision {decision!r}")
        p = s.pending.get(approval_id)
        if p is None or p.future.done():
            raise NotFound(f"no pending approval {approval_id!r}")
        if decision == "approve_session" and not p.decision.allow_session:
            raise NotAllowed(f"{p.decision.klass} calls must be approved one at a time")
        self._resolve(s, approval_id, decision, note)

    def grants(self, sid: str) -> list[dict]:
        """Sensor tools allowed for this session: ``[{tool, since_ms}]``."""
        s = self._get(sid)
        return [{"tool": tool, "since_ms": since} for tool, since in s.grants.items()]

    async def revoke_grant(self, sid: str, tool: str) -> None:
        """Withdraw an "allow for session" grant (idempotent)."""
        s = self._get(sid)
        s.grants.pop(bare_name(tool), None)

    async def interrupt(self, sid: str) -> None:
        s = self._get(sid)
        if s.turn is None:
            return
        s.interrupt_evt.set()
        for approval_id in list(s.pending):
            self._resolve(s, approval_id, "cancelled")

    async def close_session(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if s is None:
            raise NotFound(f"no chat session {sid!r}")
        s.interrupt_evt.set()
        for approval_id in list(s.pending):
            self._resolve(s, approval_id, "cancelled")
        actor = s.actor
        if actor is not None and not actor.done():
            s.inbox.put_nowait(_CLOSE)
            actor.cancel()
            await asyncio.wait({actor}, timeout=20.0)
        # Only now: subscribers drain the turn_end the actor emitted, then stop.
        s.closed = True
        s.notify()

    async def shutdown(self) -> None:
        """Close every session IN PARALLEL: a busy CLI can take seconds to go
        away, and the host gives the whole analyst shutdown a fixed budget."""
        async def close(sid: str) -> None:
            with contextlib.suppress(NotFound):
                await self.close_session(sid)

        sids = list(self._sessions)
        if sids:
            await asyncio.gather(*(close(sid) for sid in sids))

    # ----------------------------------------------------------- streaming --
    def subscribe(self, sid: str, last_event_id: int | None = None
                  ) -> AsyncIterator[tuple[int, str, dict]]:
        """Replay the log after ``last_event_id`` (all of it when None), then follow.

        The first item is always ``(0, "session", {...})``; its seq is 0 so an
        SSE writer can leave its ``id:`` line out and not disturb the client's
        Last-Event-ID.  Raises ``NotFound`` immediately for an unknown session.
        The iterator ends when the session is closed.
        """
        s = self._get(sid)

        async def only_events() -> AsyncIterator[tuple[int, str, dict]]:
            async for item in self._follow(s, last_event_id, None):
                if item is not None:
                    yield item

        return only_events()

    async def _follow(self, s: _Session, last_event_id: int | None,
                      heartbeat_s: float | None) -> AsyncIterator[tuple[int, str, dict] | None]:
        cursor = last_event_id if isinstance(last_event_id, int) and last_event_id > 0 else 0
        oldest = s.log[0][0] if s.log else s.seq + 1
        yield (0, "session", {
            "session_id": s.sid, "model": self.model, "available": self.status()["available"],
            "last_seq": s.seq, "history_truncated": cursor < oldest - 1,
        })
        while True:
            waiter = s.changed  # grabbed BEFORE the scan: an emit during a yield wakes us
            for seq, name, data in list(s.log):
                if seq > cursor:
                    cursor = seq
                    yield (seq, name, data)
            if s.closed:
                return
            if heartbeat_s is None:
                await waiter.wait()
            else:
                try:
                    await asyncio.wait_for(waiter.wait(), heartbeat_s)
                except TimeoutError:
                    yield None

    async def sse_frames(self, sid: str, last_event_id: int | None = None,
                         heartbeat_s: float | None = None) -> AsyncIterator[str]:
        """``text/event-stream`` frames for one subscriber (with comment heartbeats)."""
        s = self._get(sid)
        beat = self.heartbeat_s if heartbeat_s is None else heartbeat_s
        yield "retry: 3000\n\n"
        async for item in self._follow(s, last_event_id, beat):
            if item is None:
                yield f": ping {int(time.time())}\n\n"
                continue
            seq, name, data = item
            head = f"id: {seq}\n" if seq > 0 else ""
            yield f"{head}event: {name}\ndata: {dumps_compact(data)}\n\n"

    # ---------------------------------------------------------------- actor --
    async def _actor(self, s: _Session) -> None:
        client = None
        try:
            while True:
                if client is not None and self.idle_disconnect_s > 0:
                    try:
                        item = await asyncio.wait_for(s.inbox.get(), self.idle_disconnect_s)
                    except TimeoutError:
                        # Idle: release the CLI process. The session stays; the
                        # next message reconnects and resumes the conversation.
                        await self._disconnect(client)
                        client = None
                        log.info("analyst session %s idle; its CLI was disconnected",
                                 s.sid[:8])
                        continue
                else:
                    item = await s.inbox.get()
                if item is _CLOSE:
                    return
                turn_id, prompt = item
                try:
                    if s.interrupt_evt.is_set():
                        if s.turn is not None:
                            s.turn.stop = "interrupted"
                        continue
                    if client is None:
                        client = await self._connect(s)
                    await self._run_turn(s, client, prompt)
                except asyncio.CancelledError:
                    if s.turn is not None and s.turn.stop is None:
                        s.turn.stop = "interrupted"
                    raise
                except Exception as exc:  # noqa: BLE001 -- surfaced as an event
                    self._fail_turn(s, exc)
                    if client is not None:
                        await self._disconnect(client)
                        client = None
                finally:
                    self._finish_turn(s, turn_id)
        finally:
            for approval_id in list(s.pending):
                self._resolve(s, approval_id, "cancelled")
            if client is not None:
                await self._disconnect(client)

    async def _connect(self, s: _Session) -> Any:
        sdk = self._sdk
        toolbelt = await build_toolbelt(self._server, self._intel,
                                        lambda directive: self._emit_ui(s, directive), sdk)
        s.tool_defaults = dict(getattr(toolbelt, "defaults", None) or {})
        s.stderr_tail.clear()  # a failure below must be judged on THIS process's stderr
        options = self._options(s, toolbelt)
        client = sdk.ClaudeSDKClient(options=options)
        await client.connect()
        s.cost_base = 0.0  # a new CLI process starts its cumulative cost at zero
        return client

    def _options(self, s: _Session, toolbelt: Any) -> Any:
        kw: dict[str, Any] = {
            "model": self.model,
            "system_prompt": self._prompt_text(),
            "tools": [],
            "allowed_tools": list(toolbelt.allowed_tools),
            "disallowed_tools": list(toolbelt.disallowed_tools),
            "mcp_servers": {SDK_SERVER_NAME: toolbelt.server_config},
            "strict_mcp_config": True,
            "setting_sources": [],
            "verbatim_prompts": True,
            "permission_mode": "default",
            "can_use_tool": self._can_use_tool_for(s),
            "include_partial_messages": True,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "max_turns": MAX_TURNS,
            "cwd": str(self._analyst_dir()),
            "stderr": s.stderr_tail.append,
        }
        if self.effort:
            kw["effort"] = self.effort
        if self._cli_path:
            kw["cli_path"] = self._cli_path
        if s.claude_session_id:
            kw["resume"] = s.claude_session_id
        return self._sdk.ClaudeAgentOptions(**kw)

    @staticmethod
    async def _disconnect(client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), 20.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("analyst client disconnect failed: %s", exc)

    async def _run_turn(self, s: _Session, client: Any, prompt: str) -> None:
        turn = s.turn
        if s.interrupt_evt.is_set():  # interrupted while the client was connecting
            if turn is not None and turn.stop is None:
                turn.stop = "interrupted"
            return
        watcher = asyncio.create_task(self._interrupt_watcher(s, client))
        try:
            await client.query(prompt)
            async for msg in client.receive_response():
                try:
                    self._on_message(s, turn, msg)
                except Exception:  # a mapping bug must not kill the turn
                    log.exception("analyst: could not map %s", type(msg).__name__)
        finally:
            watcher.cancel()
        if turn is not None and not turn.got_result and turn.stop is None:
            raise RuntimeError("the analyst process ended the turn without a result")

    @staticmethod
    async def _interrupt_watcher(s: _Session, client: Any) -> None:
        await s.interrupt_evt.wait()
        try:
            await client.interrupt()
        except Exception as exc:  # noqa: BLE001
            log.warning("analyst interrupt failed: %s", exc)

    def _emit_error(self, s: _Session, message: str, hint: str | None, retryable: bool,
                    key: str | None = None) -> None:
        turn = s.turn
        if turn is not None and key is not None:
            if key in turn.errors_emitted:
                return
            turn.errors_emitted.add(key)
        s.emit("error", _drop_none({"message": message, "hint": hint,
                                    "retryable": bool(retryable)}))

    def _fail_turn(self, s: _Session, exc: BaseException) -> None:
        sdk = self._sdk
        not_found = getattr(sdk, "CLINotFoundError", None)
        tail = " ".join(s.stderr_tail)
        if isinstance(not_found, type) and isinstance(exc, not_found):
            message, hint, retryable = "The Claude CLI was not found.", CLI_MISSING_HINT, False
        elif s.claude_session_id and _NO_CONVERSATION_RX.search(f"{exc} {tail}"):
            s.claude_session_id = None  # never resume it again: start fresh next turn
            message, hint, retryable = RESUME_LOST_MESSAGE, None, True
        elif _AUTH_RX.search(f"{exc} {tail}"):
            message = "The analyst could not sign in to Claude."
            hint, retryable = SIGN_IN_HINT, False
        else:
            message = f"The analyst failed: {type(exc).__name__}: {exc}"[:400]
            hint, retryable = None, True
        log.warning("analyst turn failed: %s: %s", type(exc).__name__, exc)
        self._emit_error(s, message, hint, retryable, key="fail")
        if s.turn is not None:
            s.turn.stop = "error"
            s.turn.error = message

    def _finish_turn(self, s: _Session, turn_id: str) -> None:
        turn = s.turn
        if turn is None or turn.turn_id != turn_id or turn.ended:
            return
        turn.ended = True
        stop = turn.stop or ("end" if turn.got_result else "error")
        data: dict = {"turn_id": turn_id, "stop": stop}
        if stop == "error":
            data["error"] = turn.error or "the turn ended without a result"
        s.turn = None
        s.emit("turn_end", data)

    # ------------------------------------------------------- message mapping --
    def _is(self, msg: Any, name: str) -> bool:
        cls = getattr(self._sdk, name, None)
        return isinstance(cls, type) and isinstance(msg, cls)

    def _on_message(self, s: _Session, turn: _Turn | None, msg: Any) -> None:
        if turn is None:
            return
        if self._is(msg, "StreamEvent"):
            self._on_stream_event(s, turn, msg)
        elif self._is(msg, "AssistantMessage"):
            self._on_assistant(s, turn, msg)
        elif self._is(msg, "UserMessage"):
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if self._is(block, "ToolResultBlock"):
                        self._on_tool_result(s, turn, block)
        elif self._is(msg, "ResultMessage"):
            self._on_result(s, turn, msg)
        elif self._is(msg, "RateLimitEvent"):
            info = getattr(msg, "rate_limit_info", None)
            if info is not None:
                s.rate_limit = _drop_none({
                    "status": getattr(info, "status", None),
                    "resets_at": getattr(info, "resets_at", None),
                    "type": getattr(info, "rate_limit_type", None),
                })
                s.emit("usage", {"turn_id": turn.turn_id, "rate_limit": s.rate_limit})
        elif self._is(msg, "SystemMessage"):
            self._on_system(s, turn, msg)

    def _on_stream_event(self, s: _Session, turn: _Turn, msg: Any) -> None:
        if getattr(msg, "parent_tool_use_id", None):
            return
        ev = getattr(msg, "event", None) or {}
        kind = ev.get("type")
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "text":
                turn.text_chars = 0
                if turn.any_text:  # a new text block: keep paragraphs apart
                    s.emit("text_delta", {"turn_id": turn.turn_id, "text": "\n\n"})
            elif block.get("type") == "thinking":
                turn.thinking_chars = 0
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                turn.text_chars += len(delta["text"])
                turn.any_text = True
                s.emit("text_delta", {"turn_id": turn.turn_id, "text": delta["text"]})
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                turn.thinking_chars += len(delta["thinking"])
                s.emit("thinking", {"turn_id": turn.turn_id, "text": delta["thinking"]})

    def _on_assistant(self, s: _Session, turn: _Turn, msg: Any) -> None:
        if getattr(msg, "parent_tool_use_id", None):
            return
        error = getattr(msg, "error", None)
        if error:
            message, hint, retryable = _ASSISTANT_ERRORS.get(error, _ASSISTANT_ERRORS["unknown"])
            self._emit_error(s, message, hint, retryable, key=f"assistant:{error}")
            turn.stop = "error"
            turn.error = str(error)
            return  # the blocks are the CLI's own error text; the error event replaces them
        for block in getattr(msg, "content", None) or []:
            if self._is(block, "TextBlock"):
                if turn.text_chars == 0 and block.text:
                    # No deltas arrived for this block: send it whole.
                    text = f"\n\n{block.text}" if turn.any_text else block.text
                    s.emit("text_delta", {"turn_id": turn.turn_id, "text": text})
                    turn.any_text = True
                turn.text_chars = 0
            elif self._is(block, "ThinkingBlock"):
                if turn.thinking_chars == 0 and block.thinking:
                    s.emit("thinking", {"turn_id": turn.turn_id, "text": block.thinking})
                turn.thinking_chars = 0
            elif self._is(block, "ToolUseBlock"):
                self._on_tool_use(s, turn, block)

    def _on_tool_use(self, s: _Session, turn: _Turn, block: Any) -> None:
        args = block.input if isinstance(block.input, dict) else {}
        decision = classify(block.name, args)
        tool = bare_name(block.name)
        s.remember_call(block.id, {"tool": tool, "args": args, "decision": decision,
                                   "turn_id": turn.turn_id})
        s.emit("tool_call", {
            "turn_id": turn.turn_id, "call_id": block.id, "tool": tool,
            "title": decision.title, "class": decision.klass,
            "args": _display_args(args), "summary": decision.summary,
        })

    def _on_tool_result(self, s: _Session, turn: _Turn, block: Any) -> None:
        call_id = block.tool_use_id
        call = s.calls.get(call_id) or {}
        text = _tool_result_text(getattr(block, "content", None))
        nbytes = len(text.encode("utf-8"))
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = None
        is_error = bool(getattr(block, "is_error", False))
        rejected = isinstance(payload, dict) and payload.get("rejected") is True
        error = None
        if is_error:
            error = (_error_text(payload) or text.removeprefix("Error: ") or "tool error")[:300]
        elif not rejected:
            error = _error_text(payload)
        busy = isinstance(payload, dict) and payload.get("status") == "busy"
        refused = isinstance(payload, dict) and (
            payload.get("refused") is True or payload.get("accepted") is False)
        ok = not is_error and not rejected and error is None and not busy and not refused
        if call_id in s.not_run:
            outcome = "not_run"
        elif ok:
            outcome = "ok"
        elif busy:
            outcome = "busy"
        elif rejected or refused:
            outcome = "rejected"
        else:
            outcome = "error"
        data: dict = {"call_id": call_id, "ok": ok, "outcome": outcome}
        if rejected:
            data["rejected"] = True
        if error:
            data["error"] = error
        if busy:
            current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
            data["busy_with"] = _drop_none({
                "task_id": current.get("task_id"), "mission_id": current.get("mission_id"),
                "tool": current.get("tool")})
        data.update({
            "summary": _result_summary(call.get("tool", ""), payload, ok=ok, rejected=rejected,
                                       error=error, nbytes=nbytes),
            "bytes": nbytes,
            # Any truthy marker: the toolbelt's True, or IntelService's
            # {"dropped": [...]} report of what it cut.
            "truncated": isinstance(payload, dict) and bool(payload.get("_truncated")),
            "entities": extract_entities(payload) if payload is not None else [],
        })
        s.emit("tool_result", data)
        if not call or not isinstance(payload, dict):
            return
        tool, args = call["tool"], call["args"]
        if ok and (tool == "mission_dry_run" or args.get("dry_run") is True):
            self._record_dry_run(s, tool, args, payload)
        if ok and call_id in s.approved_calls and call["decision"].klass == COMMAND:
            vehicle = auto_track_vehicle(tool, args)
            if vehicle:
                self._emit_ui(s, {"action": "track", "vehicle": vehicle,
                                  "reason": "mission launched"})

    def _on_system(self, s: _Session, turn: _Turn, msg: Any) -> None:
        if getattr(msg, "subtype", None) != "init":
            return
        data = getattr(msg, "data", None) or {}
        # The session id is NOT taken from init: the CLI writes its transcript
        # only after init, so a CLI that dies in between left an id that every
        # later turn tried (and failed) to resume. ResultMessage carries it
        # once the transcript exists (see _on_result).
        servers = data.get("mcp_servers")
        status = None
        if isinstance(servers, list):
            for entry in servers:
                if isinstance(entry, dict) and entry.get("name") == SDK_SERVER_NAME:
                    status = entry.get("status")
        if status != "connected":
            self._emit_error(
                s, f"The analyst's tool server is not connected (status: {status or 'missing'}).",
                "Restart the app. If it keeps happening, check the host log.", True,
                key="mcp")
            turn.stop = "error"
            turn.error = "mcp_unavailable"
            s.interrupt_evt.set()

    def _on_result(self, s: _Session, turn: _Turn, msg: Any) -> None:
        turn.got_result = True
        if isinstance(getattr(msg, "session_id", None), str):
            s.claude_session_id = msg.session_id
        usage = getattr(msg, "usage", None) or {}
        input_tokens = None
        if isinstance(usage, dict):
            parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                            "cache_read_input_tokens")]
            nums = [p for p in parts if isinstance(p, int)]
            input_tokens = sum(nums) if nums else None
        total = getattr(msg, "total_cost_usd", None)
        cost = None
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            cost = total - s.cost_base if total >= s.cost_base else float(total)
            s.cost_base = float(total)
            s.cost_total += cost
            cost = round(cost, 6)
        s.emit("usage", _drop_none({
            "turn_id": turn.turn_id,
            "cost_usd": cost,
            "session_cost_usd": round(s.cost_total, 6) if cost is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": usage.get("output_tokens") if isinstance(usage, dict) else None,
            "rate_limit": s.rate_limit,
        }))
        if turn.stop == "error":
            return
        terminal = getattr(msg, "terminal_reason", None)
        subtype = getattr(msg, "subtype", "") or ""
        if s.interrupt_evt.is_set() or terminal in ("aborted_tools", "aborted_streaming"):
            turn.stop = "interrupted"
        elif subtype == "error_max_turns" or terminal == "max_turns":
            turn.stop = "max_turns"
        elif getattr(msg, "is_error", False) or subtype != "success":
            errors = getattr(msg, "errors", None) or []
            detail = "; ".join(str(e) for e in errors) or getattr(msg, "result", None) or subtype
            detail = str(detail)[:400]
            status = getattr(msg, "api_error_status", None)
            if _NO_CONVERSATION_RX.search(detail):
                s.claude_session_id = None  # the resumed transcript is gone
                self._emit_error(s, RESUME_LOST_MESSAGE, None, True, key="fail")
            elif _AUTH_RX.search(detail) or status == 401:
                self._emit_error(s, "The analyst could not sign in to Claude.", SIGN_IN_HINT,
                                 False, key="assistant:authentication_failed")
            else:
                self._emit_error(s, f"The analyst turn failed: {detail}", None,
                                 status in (429, 500, 502, 503, 529), key="result")
            turn.stop = "error"
            turn.error = detail
        else:
            turn.stop = "end"

    # ------------------------------------------------------------- approvals --
    def _can_use_tool_for(self, s: _Session):
        async def can_use_tool(tool_name: str, input_data: dict, ctx: Any):
            return await self._decide(s, tool_name, input_data, ctx)
        return can_use_tool

    async def _decide(self, s: _Session, tool_name: str, input_data: dict, ctx: Any) -> Any:
        sdk = self._sdk
        allow, deny = sdk.PermissionResultAllow, sdk.PermissionResultDeny
        args = input_data if isinstance(input_data, dict) else {}
        decision = classify(tool_name, args)
        call_id = getattr(ctx, "tool_use_id", None) or f"call-{uuid.uuid4().hex[:12]}"
        tool = bare_name(tool_name)
        if decision.auto:
            return allow(updated_input=input_data)
        if s.closed or s.interrupt_evt.is_set():
            s.mark_not_run(call_id)
            return deny(message="The operator interrupted the turn; the call was not run.",
                        interrupt=True)
        if decision.allow_session and tool in s.grants:
            s.mark_approved(call_id)
            return allow(updated_input=input_data)
        approval_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        pending = _Pending(approval_id=approval_id, call_id=call_id, tool=tool,
                           decision=decision, future=loop.create_future(),
                           expires_at_ms=_now_ms() + int(self._approval_timeout_s * 1000))
        s.pending[approval_id] = pending
        request: dict = {
            "approval_id": approval_id, "call_id": call_id, "tool": pending.tool,
            "class": decision.klass, "title": decision.title, "summary": decision.summary,
            "args": _display_args(args), "consequences": list(decision.consequences),
            "allow_session": decision.allow_session, "expires_at_ms": pending.expires_at_ms,
            # contract v1.1 §10.1
            "vehicle": mission_vehicle(tool, args) or (
                args["vehicle"] if isinstance(args.get("vehicle"), str) else None),
            "dry_runnable": tool in DRY_RUN_TOOLS,
            "grant_scope": [tool] if decision.allow_session else None,
        }
        dry = self._dry_run_for(s, tool_name, args)
        if dry is not None:
            request["dry_run"] = dry
        s.emit("approval_request", request)
        try:
            done, _ = await asyncio.wait({pending.future}, timeout=self._approval_timeout_s)
            if not done:
                self._resolve(s, approval_id, "expired")
            verdict, note = pending.future.result()
        except asyncio.CancelledError:
            # client.interrupt() cancels a parked callback (control_cancel_request).
            self._resolve(s, approval_id, "cancelled")
            raise
        finally:
            s.pending.pop(approval_id, None)
        if verdict in ("approve", "approve_session"):
            if verdict == "approve_session" and decision.allow_session:
                s.grants.setdefault(tool, _now_ms())
            s.mark_approved(call_id)
            return allow(updated_input=input_data)
        s.mark_not_run(call_id)
        if verdict == "deny":
            message = ("The operator denied this call in the approval panel. Do not retry it; "
                       "ask the operator how they want to proceed.")
            if note:
                message += f" Operator note: {note[:500]}"
            return deny(message=message, interrupt=False)
        if verdict == "expired":
            minutes = max(1, round(self._approval_timeout_s / 60))
            return deny(message=(f"The operator did not answer the approval request within "
                                 f"{minutes} minute{'s' if minutes != 1 else ''}, so the call "
                                 "was not run. Do not retry it unless the operator asks."),
                        interrupt=False)
        return deny(message="The operator interrupted the turn; the call was not run.",
                    interrupt=True)

    def _resolve(self, s: _Session, approval_id: str, verdict: str,
                 note: str | None = None) -> bool:
        p = s.pending.get(approval_id)
        if p is None or p.future.done():
            return False
        p.future.set_result((verdict, note))
        word = {"approve": "approved", "approve_session": "approved", "deny": "denied",
                "expired": "expired", "cancelled": "cancelled"}[verdict]
        data: dict = {"approval_id": approval_id, "call_id": p.call_id, "decision": word,
                      "tool": p.tool,
                      "scope": "session" if verdict == "approve_session" else "once"}
        if note:
            data["note"] = note[:500]
        s.emit("approval_resolved", data)
        return True

    # ------------------------------------------------------------- dry runs --
    def _record_dry_run(self, s: _Session, tool: str, args: dict, payload: dict) -> None:
        kind = mission_kind(tool, args)
        vehicle = mission_vehicle(tool, args)
        gate = payload.get("gate")
        survey = payload.get("survey")
        if not isinstance(gate, dict) and isinstance(survey, dict):
            gate = survey.get("gate")
            payload = survey
        if not kind or not vehicle or not isinstance(gate, dict):
            return
        summary: dict = {"ok": bool(gate.get("ok")),
                         "gate": {k: gate[k] for k in _GATE_KEYS if k in gate},
                         "at_ms": _now_ms(), "tool": tool}
        for key in ("envelope_violations", "warnings"):
            if isinstance(summary["gate"].get(key), list):
                summary["gate"][key] = summary["gate"][key][:8]
        eta = payload.get("est_time_s", gate.get("est_time_s"))
        if isinstance(eta, (int, float)):
            summary["eta_s"] = eta
        fuel_now, burn = payload.get("fuel_pct"), payload.get("est_fuel_pct")
        if isinstance(fuel_now, (int, float)) and isinstance(burn, (int, float)):
            summary["fuel_pct_after"] = round(fuel_now - burn, 1)
        elif isinstance(gate.get("available_pct"), (int, float)) and isinstance(
                gate.get("plan_fuel_pct"), (int, float)):
            summary["fuel_pct_after"] = round(gate["available_pct"] - gate["plan_fuel_pct"], 1)
        count = payload.get("waypoint_count")
        if not isinstance(count, int):
            count = _count_items(payload.get("waypoints"))
        if isinstance(count, int):
            summary["waypoints"] = count
        s.dry_runs[(kind, vehicle)] = (summary, self._effective_plan_for(s, tool, args))

    def _dry_run_for(self, s: _Session, tool: str, args: dict) -> dict | None:
        kind = mission_kind(tool, args)
        vehicle = mission_vehicle(tool, args)
        if not kind or not vehicle:
            return None
        found = s.dry_runs.get((kind, vehicle))
        if not found:
            return None
        summary, planned = found
        out = dict(summary)
        out["gate"] = dict(summary.get("gate") or {})
        # Compared on the EFFECTIVE plans (each path's own defaults applied),
        # never on the raw arguments the model typed.
        out["matches_args"] = planned == self._effective_plan_for(s, tool, args)
        envelope = self._envelope_summary()
        if envelope:
            out["gate"]["envelope"] = envelope
        return out

    def _effective_plan_for(self, s: _Session, tool: str, args: Any) -> dict:
        kind_defaults = getattr(self._server, "MISSION_PARAM_DEFAULTS", None)
        return _effective_plan(
            tool, args, schema_defaults=s.tool_defaults,
            kind_defaults=kind_defaults if isinstance(kind_defaults, dict) else None)

    def _envelope_summary(self) -> dict | None:
        env = getattr(self._server, "envelope", None)
        if env is None:
            return None
        try:
            return {"ceiling_m_agl": float(env.ceiling_m_agl),
                    "min_agl_m": float(env.min_agl_m),
                    "max_speed_mps": float(env.max_speed_mps),
                    "geofence": "enforced" if getattr(env, "geofence", None) else "none"}
        except (AttributeError, TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- ui --
    def _emit_ui(self, s: _Session, directive: dict) -> None:
        if not isinstance(directive, dict) or directive.get("action") not in (
                "focus", "track", "orb", "inspect"):
            raise ValueError("unknown ui directive")
        s.emit("ui", dict(directive))


# ------------------------------------------------------------------ router --

class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    context: dict | None = None


class ApprovalBody(BaseModel):
    decision: Literal["approve", "deny", "approve_session"]
    note: str | None = Field(default=None, max_length=2000)


def chat_router(service: ChatService, auth: Any, sse_auth: Any):
    """Routes per contract §3 (+ the v1.1 grant routes, §10.2).

    ``auth`` / ``sse_auth`` are FastAPI dependencies.  Beyond the table: an
    unavailable analyst answers ``POST .../messages`` with 503
    ``{error:"unavailable", reason, hint?}``.
    """
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse, StreamingResponse

    router = APIRouter()
    api = APIRouter(dependencies=[Depends(auth)])
    sse = APIRouter(dependencies=[Depends(sse_auth)])

    def _missing(sid: str) -> JSONResponse:
        return JSONResponse({"error": "unknown_session", "session_id": sid}, status_code=404)

    @api.get("/chat/status")
    async def chat_status():
        return service.status()

    @api.post("/chat/sessions")
    async def chat_create():
        try:
            sid = await service.create_session()
        except Busy as exc:
            return JSONResponse({"error": "busy", "message": str(exc)}, status_code=409)
        return {"session_id": sid}

    @api.post("/chat/sessions/{sid}/messages", status_code=202)
    async def chat_message(sid: str, body: MessageBody):
        try:
            turn_id = await service.post_message(sid, body.text, body.context)
        except NotFound:
            return _missing(sid)
        except Busy as exc:
            return JSONResponse({"error": "busy", "turn_id": exc.turn_id}, status_code=409)
        except Unavailable as exc:
            return JSONResponse(_drop_none({"error": "unavailable", "reason": exc.reason,
                                            "hint": exc.hint}), status_code=503)
        except ValueError as exc:
            return JSONResponse({"error": "invalid", "message": str(exc)}, status_code=422)
        return {"turn_id": turn_id}

    @sse.get("/chat/sessions/{sid}/stream")
    async def chat_stream(sid: str, request: Request):
        raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        try:
            last = int(raw) if raw not in (None, "") else None
        except ValueError:
            last = None
        try:
            service._get(sid)
        except NotFound:
            return _missing(sid)
        return StreamingResponse(
            service.sse_frames(sid, last),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    @api.post("/chat/sessions/{sid}/approvals/{approval_id}")
    async def chat_approval(sid: str, approval_id: str, body: ApprovalBody):
        try:
            await service.resolve_approval(sid, approval_id, body.decision, body.note)
        except NotFound:
            return JSONResponse({"error": "unknown_approval", "approval_id": approval_id},
                                status_code=404)
        except NotAllowed as exc:
            return JSONResponse({"error": "not_allowed", "message": str(exc)}, status_code=422)
        except ValueError as exc:
            return JSONResponse({"error": "invalid", "message": str(exc)}, status_code=422)
        return {"ok": True}

    @api.get("/chat/sessions/{sid}/grants")
    async def chat_grants(sid: str):
        try:
            return {"grants": service.grants(sid)}
        except NotFound:
            return _missing(sid)

    @api.delete("/chat/sessions/{sid}/grants/{tool}")
    async def chat_revoke_grant(sid: str, tool: str):
        try:
            await service.revoke_grant(sid, tool)
        except NotFound:
            return _missing(sid)
        return {"ok": True}

    @api.post("/chat/sessions/{sid}/interrupt")
    async def chat_interrupt(sid: str):
        try:
            await service.interrupt(sid)
        except NotFound:
            return _missing(sid)
        return {"ok": True}

    @api.delete("/chat/sessions/{sid}")
    async def chat_delete(sid: str):
        try:
            await service.close_session(sid)
        except NotFound:
            return _missing(sid)
        return {"ok": True}

    router.include_router(api)
    router.include_router(sse)
    return router


__all__ = [
    "DEFAULT_MODEL", "Busy", "ChatError", "ChatService", "NotAllowed", "NotFound",
    "Unavailable", "chat_router",
]
