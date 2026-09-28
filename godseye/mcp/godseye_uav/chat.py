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
``<config dir>/projects/<encoded cwd>/<session>.jsonl`` whatever the options
say; the analyst's cwd is ``<store>/analyst``, so they land under that encoded
path. The config dir is ``~/.claude`` for the Claude login and
``<store>/analyst/claude-home`` for every other provider (BYOK spec §4.2).

Model provider (BYOK spec §4.4, §5, §8, §9): with ``llm=`` (an
``llm_settings.LlmSettings``) every turn resolves the active provider first.
Only ``model``, ``thinking``, ``effort`` and ``env`` come from it; every other
option is the same for every provider (``provider_options`` is the one place
those four are computed). A settings change bumps the provider generation: the
running turn finishes on the old provider, and the session reconnects on its
next message, fresh when the provider identity changed (``provider_changed``).
Every event goes through one redaction choke point (``_Session.emit``) so no
provider key can reach SSE; streamed text and thinking go through
``_Session.stream`` first, which redacts across deltas (a key split into short
chunks). A turn whose events or final blocks needed redacting also gets its
CLI transcript scrubbed. Before a CLI is spawned, ``llm.preflight(rp)`` may
refuse the provider (an endpoint that now redirects). Without ``llm`` the
service runs exactly as before this spec (Claude login, no ``env`` option).
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
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
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
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

# ------------------------------------------------------- model providers --

#: The provider kind that is today's default: the owner's Claude login.
LOGIN_KIND = "anthropic_login"
#: The login provider as events name it when no settings module is wired.
LOGIN_PROVIDER = {"id": "anthropic_login", "label": "Claude login (this Mac)"}
#: Kinds whose model precedence starts with ``ChatService(model=)`` and then
#: ``GODSEYE_CHAT_MODEL`` (spec §3.4); every other kind takes the settings.
FIRST_PARTY_KINDS = frozenset({"anthropic_login", "anthropic_key"})
#: Kinds that accept ``effort`` (spec §2): Claude on Anthropic or a cloud.
EFFORT_KINDS = frozenset({"anthropic_login", "anthropic_key", "bedrock", "vertex", "foundry"})
ADAPTIVE_THINKING = {"type": "adaptive", "display": "summarized"}
COST_LIST = "anthropic_list"
#: The CLI prices unknown models from its own table: the dollars are invented.
COST_UNRELIABLE = "unreliable"
#: ``ResolvedProvider.env`` names whose values are credentials (redacted everywhere).
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
                   "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
                   "ANTHROPIC_AWS_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
REDACTED = "[redacted key]"
#: A secret is redacted wherever any run of this many of its characters shows up.
REDACT_MIN_RUN = 12
#: Sent in place of a key to a keyless local server (``llm_settings``' list; a
#: test pins them equal). The only values of 8+ characters never redacted.
PLACEHOLDER_TOKENS = frozenset({"ollama", "lmstudio", "unused"})
#: ``status.reason`` values that mean "the provider settings need attention".
PROVIDER_REASONS = ("provider_not_configured", "provider_key_missing", "provider_auth",
                    "settings_error")
PROVIDER_HINTS = {
    "provider_not_configured": "Open analyst settings.",
    "provider_key_missing": "Open analyst settings.",
    "provider_auth": "Open analyst settings to replace the key.",
    "settings_error": "Open analyst settings.",
}
#: ``error.code`` values (spec §5).
ERROR_CODES = ("auth", "billing", "model", "rate_limit", "invalid_request", "server",
               "network", "config", "unknown")

_STATUS_IN_TEXT_RX = re.compile(r"(?i)\b(?:API Error|status(?: code)?)[:\s]+([1-5]\d\d)\b")
_NETWORK_RX = re.compile(r"(?i)ECONNREFUSED|Connection refused|ENOTFOUND|timed out|certificate")
_THINKING_RX = re.compile(r"(?i)thinking|adaptive")
_CODE_BY_STATUS = {401: "auth", 403: "auth", 402: "billing", 404: "model", 429: "rate_limit",
                   400: "invalid_request", 422: "invalid_request"}
_CODE_BY_ASSISTANT_ERROR = {
    "authentication_failed": "auth", "billing_error": "billing", "model_not_found": "model",
    "rate_limit": "rate_limit", "invalid_request": "invalid_request", "server_error": "server",
}


def classify_provider_error(status: int | None, assistant_error: str | None,
                            text: str | None) -> str:
    """The ``error.code`` for a failed turn (spec §5).

    ``ResultMessage.api_error_status`` wins, then ``AssistantMessage.error``,
    then the text: gateway-style 401s carry ``invalid_request`` as their
    error string, and ``model_not_found`` is outside the SDK's Literal.
    """
    text = text or ""
    if not isinstance(status, int) or isinstance(status, bool):
        status = None
    if status is None:
        m = _STATUS_IN_TEXT_RX.search(text)
        if m and assistant_error in (None, "unknown", "invalid_request"):
            status = int(m.group(1))
    if status is not None:
        if status in _CODE_BY_STATUS:
            return _CODE_BY_STATUS[status]
        if 500 <= status <= 599:
            return "server"
    # The CLI reports a refused connection as ``server_error`` with no
    # status; §5 counts ``server_error`` as a server error only with one.
    if (assistant_error == "server_error" and _NETWORK_RX.search(text)
            and not _STATUS_IN_TEXT_RX.search(text)):
        return "network"
    code = _CODE_BY_ASSISTANT_ERROR.get(assistant_error or "")
    if code:
        return code
    if _NETWORK_RX.search(text):
        return "network"
    if _AUTH_RX.search(text):
        return "auth"
    return "unknown"


def provider_error_copy(code: str, *, label: str, host: str | None = None,
                        model: str | None = None, status: int | None = None,
                        detail: str | None = None) -> tuple[str, str | None, bool]:
    """(message, hint, retryable) for a provider other than the Claude login
    (spec §5). ``detail`` must already be redacted; it is cut to 300 chars.
    """
    detail = (detail or "").strip()[:300] or "no detail given"
    where = host or label
    if code == "auth" and status == 403:
        return (f"{label} refused access for this key.",
                f"Check the key's permissions or plan in your {label} account.", False)
    if code == "auth":
        return f"{label} rejected the key.", "Open analyst settings to replace the key.", False
    if code == "billing":
        return (f"{label} reports a billing problem.",
                f"Check the credits or plan in your {label} account.", False)
    if code == "model":
        return (f"{label} doesn't recognize the model {model or 'you picked'}.",
                "Pick another model in analyst settings.", False)
    if code == "rate_limit":
        return f"{label} is rate-limiting this key.", "Wait a minute, then retry.", True
    if code == "invalid_request":
        hint = ("Turn off extended thinking for this model in analyst settings."
                if _THINKING_RX.search(detail) else "Run a full check in analyst settings.")
        return f"{label} rejected the request: {detail}", hint, False
    if code == "server":
        return f"{label} had a server error.", "Retry in a moment.", True
    if code == "network":
        return f"Couldn't reach {where}.", "Check the endpoint URL and your connection.", True
    return f"The analyst hit an error with {label}: {detail}", None, True


def _config_copy(reason: str | None, label: str) -> tuple[str, str]:
    """(message, hint) for a turn refused because the provider is not ready."""
    if reason == "provider_key_missing":
        return f"The analyst has no key for {label}.", "Open analyst settings."
    if reason == "settings_error":
        return "The analyst's model settings could not be read.", "Open analyst settings."
    return f"The analyst isn't set up to use {label}.", "Open analyst settings."


class _Redactor:
    """Replaces every run of ``REDACT_MIN_RUN`` or more characters of a known
    secret with ``[redacted key]`` (spec §9.5). A secret shorter than that is
    redacted where it appears whole, whatever its characters (``validate_key``
    accepts ``abcdefghij``); only the placeholder tokens (``lmstudio``) are
    never added. Secrets are only ever added: a key replaced in settings may
    still be echoed by a turn that started with it.
    """

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._grams: set[str] = set()
        self._whole: set[str] = set()

    def add(self, secret: Any) -> None:
        if (not isinstance(secret, str) or len(secret) < 8 or secret in PLACEHOLDER_TOKENS
                or secret in self._secrets):
            return
        self._secrets.add(secret)
        # The raw form, and the form a JSON transcript stores it in.
        for form in {secret, json.dumps(secret)[1:-1]}:
            if len(form) >= REDACT_MIN_RUN:
                self._grams.update(form[i:i + REDACT_MIN_RUN]
                                   for i in range(len(form) - REDACT_MIN_RUN + 1))
            else:
                self._whole.add(form)

    @property
    def active(self) -> bool:
        return bool(self._grams or self._whole)

    def spans(self, value: str) -> list[tuple[int, int]]:
        """Sorted, merged ``(start, end)`` spans of ``value`` to redact."""
        if not isinstance(value, str) or not self.active or len(value) < 8:
            return []
        found: list[tuple[int, int]] = []
        for needles, width in ((self._grams, REDACT_MIN_RUN), (self._whole, 0)):
            for needle in needles:
                start = value.find(needle)
                while start >= 0:
                    found.append((start, start + (width or len(needle))))
                    start = value.find(needle, start + 1)
        found.sort()
        merged: list[tuple[int, int]] = []
        for a, b in found:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    @staticmethod
    def render(value: str, spans: list[tuple[int, int]]) -> str:
        """``value`` with each span replaced by ``[redacted key]``."""
        out, pos = [], 0
        for a, b in spans:
            out += [value[pos:a], REDACTED]
            pos = b
        out.append(value[pos:])
        return "".join(out)

    def text(self, value: str) -> str:
        spans = self.spans(value)
        return self.render(value, spans) if spans else value

    def value(self, value: Any) -> Any:
        """``value`` with every string inside it redacted (dicts, lists, tuples)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.value(v) for v in value)
        return value


#: One registry per process: the analyst's log records, events and stderr are
#: all redacted against every key any ChatService has handed to a CLI.
_REDACTOR = _Redactor()


class _RedactLogFilter(logging.Filter):
    """Redacts this module's log records (a logger's own filters only see
    records created on it, so it sits on ``godseye_uav.chat`` itself)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _REDACTOR.active:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 -- a bad format string: leave it to logging
                return True
            clean = _REDACTOR.text(message)
            if clean != message:
                record.msg, record.args = clean, None
        return True


log.addFilter(_RedactLogFilter())


def scrub_transcripts(config_dir: pathlib.Path, *, session_ids: Iterable[str],
                      redact: Callable[[str], str]) -> list[pathlib.Path]:
    """Rewrite one session's CLI transcripts with ``redact`` applied (spec §9.6).

    Rewrites ``<config_dir>/projects/*/<session id>.jsonl`` (and any
    ``*.jsonl`` under a ``<session id>/`` folder beside it) for each of
    ``session_ids``, and nothing else: another analyst session's CLI may be
    appending to its own file in the same folder, and a replace under it would
    lose its writes. Atomic (same-directory temp file, mode 0600,
    ``os.replace``). Returns the files rewritten. The session's CLI must not
    be running.
    """
    projects = pathlib.Path(config_dir) / "projects"
    if not projects.is_dir():
        return []
    ids = {sid for sid in session_ids if isinstance(sid, str) and re.fullmatch(r"[\w.-]+", sid)}
    files: set[pathlib.Path] = set()
    for folder in projects.iterdir():
        if not folder.is_dir():
            continue
        for sid in ids:
            own = folder / f"{sid}.jsonl"
            if own.is_file():
                files.add(own)
            side = folder / sid
            if side.is_dir():
                files.update(q for q in side.rglob("*.jsonl") if q.is_file())
    rewritten = []
    for path in sorted(files):
        try:
            raw = path.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            continue
        clean = redact(raw)
        if clean == raw:
            continue
        tmp = path.with_name(f".{path.name}.{os.getpid()}.scrub")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fd = -1
                fh.write(clean)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fd != -1:
                os.close(fd)
        os.replace(tmp, path)
        rewritten.append(path)
    return rewritten


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """A ResolvedProvider attribute (or mapping key), else ``default``."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(frozen=True)
class _LegacyProvider:
    """The provider without a settings module: today's Claude login.

    ``env`` is None, so the options carry no ``env`` at all (exactly the
    option set from before the BYOK spec).
    """

    model: str
    effort: str | None
    id: str = LOGIN_PROVIDER["id"]
    label: str = LOGIN_PROVIDER["label"]
    kind: str = LOGIN_KIND
    model_family: str = "claude"
    host: str | None = None
    thinking: dict = field(default_factory=lambda: dict(ADAPTIVE_THINKING))
    env: Mapping[str, str] | None = None
    cost_basis: str = COST_LIST
    identity: str = "legacy-login"
    generation: int = 0
    ready: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class _UnresolvedProvider:
    """Stands in for a provider ``resolve()`` could not produce."""

    label: str = "the model provider"
    id: str = "unknown"
    kind: str = "unknown"
    model: str = ""
    ready: bool = False
    reason: str = "settings_error"
    generation: int = -1
    identity: str = ""
    env: Mapping[str, str] | None = None


def provider_options(rp: Any, *, model_override: str | None = None,
                     effort_override: str | None = None) -> dict:
    """The four ``ClaudeAgentOptions`` fields a provider decides (spec §4.4).

    ``model``, ``thinking``, ``effort`` (only for kinds that allow it) and
    ``env`` (only when the provider has one: the legacy login has none). The
    model precedence of spec §3.4: ``model_override`` (``ChatService(model=)``,
    then ``GODSEYE_CHAT_MODEL``) wins only for the first-party Claude kinds.
    Every other option is the same for every provider and is never set here.
    """
    kind = _field(rp, "kind", LOGIN_KIND)
    model = _field(rp, "model") or None
    if kind in FIRST_PARTY_KINDS:
        model = model_override or model or DEFAULT_MODEL
    thinking = _field(rp, "thinking")
    out: dict[str, Any] = {
        "model": model,
        "thinking": dict(thinking) if isinstance(thinking, Mapping) else dict(ADAPTIVE_THINKING),
    }
    if kind in EFFORT_KINDS:
        effort = effort_override or _field(rp, "effort")
        if effort in EFFORT_LEVELS:
            out["effort"] = effort
    env = _field(rp, "env")
    if isinstance(env, Mapping):
        out["env"] = {str(k): str(v) for k, v in env.items()}
    return out


def _ready(rp: Any) -> bool:
    return bool(_field(rp, "ready", True))


def _conn_key(rp: Any) -> tuple:
    """What a connected CLI was built from: a change means reconnect (spec §8)."""
    return (_field(rp, "generation"), _field(rp, "identity"))


def _provider_ref(rp: Any) -> dict:
    """``{id, label}``: all an event or ``/app/config`` may say about a provider."""
    return {"id": str(_field(rp, "id", LOGIN_PROVIDER["id"])),
            "label": str(_field(rp, "label", LOGIN_PROVIDER["label"]))}


#: Keys ``status()`` may copy out of a provider summary (a whitelist: nothing
#: else, and never ``env``, can reach ``/chat/status``).
_SUMMARY_KEYS = ("id", "label", "kind", "model_family", "host", "key_source", "configured",
                 "cost_basis", "settings_rev", "rev", "ready", "reason", "model", "generation",
                 "identity", "effort")


def _summary_fields(raw: Any) -> dict:
    """A provider summary (a mapping, possibly with a nested ``provider``
    block, or a ``ResolvedProvider``) flattened to ``_SUMMARY_KEYS``."""
    out: dict = {}
    if raw is None:
        return out
    if isinstance(raw, Mapping):
        nested = raw.get("provider")
        for src in (nested if isinstance(nested, Mapping) else {}, raw):
            for key in _SUMMARY_KEYS:
                if key in src and not isinstance(src[key], Mapping):
                    out.setdefault(key, src[key])
        return out
    for key in _SUMMARY_KEYS:
        value = getattr(raw, key, None)
        if value is not None:
            out[key] = value
    return out


_GATE_KEYS =("ok", "required_pct", "available_pct", "plan_fuel_pct", "return_fuel_pct",
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
    #: An AssistantMessage error waiting for the ResultMessage's HTTP status:
    #: ``{"error": str, "text": str}`` (spec §5: the status decides the code).
    pending_error: dict | None = None
    started_at: float = field(default_factory=time.time)


class _Session:
    def __init__(self, sid: str, redactor: _Redactor | None = None):
        self.sid = sid
        self._redactor = redactor
        #: Set when the redaction choke point replaced something during a turn:
        #: the CLI transcript then holds the key and gets scrubbed (spec §9.6).
        self.needs_scrub = False
        #: Streamed text not sent yet, per kind ("text" / "thinking"): its
        #: tail could be the start of a key the next delta completes.
        self.held: dict[str, str] = {}
        #: Provider generation / identity the session last ran a turn on, and
        #: ``{id, label, model}`` for ``provider_changed.from`` (spec §8).
        self.gen: int | None = None
        self.identity: str | None = None
        self.provider: dict | None = None
        #: kind / endpoint host of that provider (error copy, spec §5).
        self.kind: str = LOGIN_KIND
        self.host: str | None = None
        self.cost_basis = COST_LIST
        #: The CLI config dir of the connected client (for the transcript scrub).
        self.config_dir: pathlib.Path | None = None
        self.seen_session_ids: set[str] = set()
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

    def redact(self, value: Any) -> Any:
        """``value`` with every known key redacted; flags a transcript scrub
        (done after the turn) when anything was replaced."""
        red = self._redactor
        if red is None or not red.active:
            return value
        clean = red.value(value)
        if clean != value:
            self.needs_scrub = True
        return clean

    def stream(self, kind: str, delta: str) -> str:
        """The settled, redacted part of the ``kind`` stream after ``delta``.

        ``emit`` redacts each event on its own, so a key split over deltas
        shorter than ``REDACT_MIN_RUN`` would pass it. Redaction runs on the
        held text plus the delta instead; the last ``REDACT_MIN_RUN - 1``
        characters (and a match that reaches into them) wait for the next
        delta or ``flush_stream``. Nothing is held while no key is known.
        """
        raw = self.held.pop(kind, "") + delta
        red = self._redactor
        if red is None or not red.active:
            return raw
        spans = red.spans(raw)
        cut = len(raw) - (REDACT_MIN_RUN - 1)
        for a, b in spans:
            if a < cut < b:
                cut = a  # the match may grow: keep all of it for the next round
                break
        if cut <= 0:
            self.held[kind] = raw
            return ""
        self.held[kind] = raw[cut:]
        done = [(a, b) for a, b in spans if b <= cut]
        if done:
            self.needs_scrub = True
        return red.render(raw[:cut], done)

    def flush_stream(self, kind: str) -> str:
        """Whatever the ``kind`` stream still holds, redacted."""
        raw = self.held.pop(kind, "")
        return self.redact(raw) if raw else ""

    def stderr_line(self, line: str) -> None:
        """The CLI's stderr sink: redacted before it is kept."""
        self.stderr_tail.append(self.redact(line))

    def emit(self, name: str, data: dict) -> int:
        # The one choke point every event passes: no string in any event may
        # carry a provider key (a provider that echoes the key in a 401 puts
        # it into the error text; spec §9.5).
        data = self.redact(data)
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
                 approval_timeout_s: float = 600.0, enabled: bool = True,
                 llm: Any = None):
        self._server = server
        self._intel = intel
        self._store_dir = pathlib.Path(store_dir)
        #: ``llm_settings.LlmSettings`` (or None: today's Claude login, no ``env``).
        self._llm = llm
        self._redactor = _REDACTOR
        #: The last provider ``resolve()`` produced (never logged: its env holds the key).
        self._last_rp: Any = None
        #: Provider generation at which a non-login provider rejected its key.
        self._auth_failure: int | None = None
        #: ChatService(model=) then GODSEYE_CHAT_MODEL: wins for the Claude kinds only.
        self._model_override = model or os.environ.get(MODEL_ENV) or None
        self.model = self._model_override or DEFAULT_MODEL
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
        """``GET /chat/status``. Never touches the keychain and never carries
        a secret: the provider block is built from a whitelist (spec §8)."""
        ok, reason, hint = self._availability()
        out: dict = {"available": ok, "model": self.model}
        if self._llm is None:
            if reason:
                out["reason"] = reason
            if hint:
                out["hint"] = hint
            if self.effort:
                out["effort"] = self.effort
            return out
        info = self._provider_summary()
        kind = info.get("kind") or LOGIN_KIND
        model = info.get("model") or None
        if kind in FIRST_PARTY_KINDS:
            model = self._model_override or model or DEFAULT_MODEL
        out["model"] = model
        ready = bool(info.get("ready", True))
        out["provider"] = {
            "id": str(info.get("id") or "unknown"), "label": str(info.get("label") or ""),
            "kind": kind, "model_family": info.get("model_family"),
            "host": info.get("host"),
            "key_source": info.get("key_source") or ("login" if kind == LOGIN_KIND else None),
            "configured": bool(info.get("configured", ready)),
        }
        out["cost_basis"] = info.get("cost_basis") or (
            COST_LIST if kind in EFFORT_KINDS else COST_UNRELIABLE)
        rev = info.get("settings_rev", info.get("rev"))
        if isinstance(rev, int) and not isinstance(rev, bool):
            out["settings_rev"] = rev
        if ok and not ready:
            ok, reason = False, str(info.get("reason") or "provider_not_configured")
            hint = PROVIDER_HINTS.get(reason, PROVIDER_HINTS["provider_not_configured"])
        elif ok and self._auth_failure is not None and (
                self._auth_failure == self._current_generation(info)):
            # the key was rejected, and the settings haven't changed since
            ok, reason, hint = False, "provider_auth", PROVIDER_HINTS["provider_auth"]
        out["available"] = ok
        if reason:
            out["reason"] = reason
        if hint:
            out["hint"] = hint
        effort = self.effort or info.get("effort")
        if kind in EFFORT_KINDS and effort in EFFORT_LEVELS:
            out["effort"] = effort
        return out

    def _current_generation(self, info: Mapping) -> Any:
        """The settings' provider generation now (the summary's, else
        ``llm.generation``), without resolving."""
        gen = info.get("generation")
        if gen is None:
            gen = getattr(self._llm, "generation", None)
        return gen

    def _provider_summary(self) -> dict:
        """The active provider without touching the keychain: the settings'
        own summary (``provider_status()``, else ``status()``), else the last
        resolved provider. Only ``_SUMMARY_KEYS`` survive."""
        raw = None
        for name in ("provider_status", "status"):
            fn = getattr(self._llm, name, None)
            if callable(fn):
                try:
                    raw = fn()
                except Exception as exc:  # noqa: BLE001 -- status must not 500
                    log.warning("analyst: the provider summary failed: %s", type(exc).__name__)
                    return {"id": "unknown", "label": "the model provider", "ready": False,
                            "reason": "settings_error"}
                break
        if inspect.isawaitable(raw):  # an async summary cannot serve a sync status
            with contextlib.suppress(Exception):
                raw.close()
            raw = None
        info = _summary_fields(raw)
        if not info and self._last_rp is not None:
            info = _summary_fields(self._last_rp)
        return info

    async def _resolve_provider(self) -> Any:
        """The provider for the next turn (``llm.resolve()``, sync or async).
        Registers its credentials with the redactor. Never raises."""
        if self._llm is None:
            return _LegacyProvider(model=self._model_override or DEFAULT_MODEL,
                                   effort=self.effort)
        try:
            # aresolve reads the keychain off the event loop (spec §3.2).
            fn = getattr(self._llm, "aresolve", None)
            rp = fn() if callable(fn) else self._llm.resolve()
            if inspect.isawaitable(rp):
                rp = await rp
        except Exception as exc:  # noqa: BLE001 -- the turn fails with settings_error
            log.warning("analyst: could not resolve the model provider: %s",
                        type(exc).__name__)
            rp = _UnresolvedProvider()
        env = _field(rp, "env")
        if isinstance(env, Mapping):
            for name in SECRET_ENV_VARS:
                self._redactor.add(env.get(name))
        self._last_rp = rp
        if _ready(rp):
            self.model = provider_options(rp, model_override=self._model_override)["model"]
        return rp

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
        self._sessions[sid] = _Session(sid, self._redactor)
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
        s.turn = _Turn(turn_id)  # reserved before the await: a second post is Busy
        s.last_turn_id = turn_id
        s.interrupt_evt = asyncio.Event()
        rp = await self._resolve_provider()
        if s.closed:
            s.turn = None
            raise NotFound(f"no chat session {sid!r}")
        if _ready(rp):
            self._note_provider(s, rp)  # provider_changed goes out before turn_start
        s.emit("turn_start", {"turn_id": turn_id, "text": text})
        prompt = self._compose_prompt(text, context)
        if s.actor is None or s.actor.done():
            s.actor = asyncio.create_task(self._actor(s), name=f"godseye-analyst-{sid[:8]}")
        s.inbox.put_nowait((turn_id, prompt, rp))
        return turn_id

    def _note_provider(self, s: _Session, rp: Any) -> None:
        """Generation / identity bookkeeping before a turn (spec §8).

        A change since the session's last turn emits ``provider_changed``.
        A different identity (provider id, kind, endpoint, fields) also drops
        the CLI conversation: no resume across providers (another config dir,
        another provider's thinking signatures). The actor reconnects the CLI
        itself, because it compares each turn's provider with its client's.
        """
        gen, identity = _field(rp, "generation"), _field(rp, "identity")
        opts = provider_options(rp, model_override=self._model_override)
        info = {**_provider_ref(rp), "model": opts["model"]}
        if s.gen is not None and (gen != s.gen or identity != s.identity):
            memory = "kept" if identity == s.identity else "cleared"
            if memory == "cleared":
                s.claude_session_id = None
            s.emit("provider_changed", {"from": s.provider, "to": info, "memory": memory,
                                        "at_ms": _now_ms()})
        s.gen, s.identity, s.provider = gen, identity, info
        s.kind = str(_field(rp, "kind", LOGIN_KIND))
        s.host = _field(rp, "host")
        s.cost_basis = _field(rp, "cost_basis") or COST_LIST

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
        st = self.status()
        provider = st.get("provider") or LOGIN_PROVIDER
        yield (0, "session", self._redactor.value({
            "session_id": s.sid, "model": st.get("model", self.model),
            "available": st["available"],
            "provider": {"id": provider.get("id"), "label": provider.get("label")},
            "last_seq": s.seq, "history_truncated": cursor < oldest - 1,
        }))
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
        client_key: tuple | None = None  # the provider the connected CLI was built from
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
                turn_id, prompt, rp = item
                try:
                    if s.interrupt_evt.is_set():
                        if s.turn is not None:
                            s.turn.stop = "interrupted"
                        continue
                    if client is not None and client_key != _conn_key(rp):
                        # The settings changed since this CLI started: the
                        # running turn finished on the old provider; this one
                        # gets a CLI built from the new one (spec §8).
                        await self._disconnect(client)
                        client = None
                    opts = provider_options(rp, model_override=self._model_override)
                    if not _ready(rp) or not opts.get("model"):
                        self._fail_config(s, rp)  # never spawn a CLI without a provider
                        continue
                    if client is None:
                        refusal = await self._preflight(rp)
                        if refusal:
                            self._fail_preflight(s, rp, refusal)  # no CLI is spawned
                            continue
                        client = await self._connect(s, rp)
                        client_key = _conn_key(rp)
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
                if s.needs_scrub:
                    # A key showed up in this turn's events: the CLI wrote it
                    # into its transcript too. Stop the CLI, scrub, resume
                    # from the clean transcript on the next turn (spec §9.6).
                    if client is not None:
                        await self._disconnect(client)
                        client = None
                    await self._scrub(s)
        finally:
            for approval_id in list(s.pending):
                self._resolve(s, approval_id, "cancelled")
            if client is not None:
                await self._disconnect(client)
            if s.needs_scrub:
                await self._scrub(s)

    async def _preflight(self, rp: Any) -> dict | None:
        """The settings' last check before a CLI is spawned for ``rp``
        (``LlmSettings.preflight``: an endpoint that now redirects, where the
        CLI would re-send the key to the new host). A refusal dict, or None.
        A preflight that raises refuses too (fail closed)."""
        fn = getattr(self._llm, "preflight", None)
        if not callable(fn):
            return None
        try:
            out = fn(rp)
            if inspect.isawaitable(out):
                out = await out
        except Exception as exc:  # noqa: BLE001 -- refused, never a crash
            log.warning("analyst: the provider preflight failed: %s", type(exc).__name__)
            message, hint = _config_copy("settings_error", str(_field(rp, "label", "")))
            return {"code": "config", "message": message, "hint": hint}
        if isinstance(out, Mapping) and out.get("message"):
            return dict(out)
        return None

    def _fail_preflight(self, s: _Session, rp: Any, refusal: Mapping) -> None:
        message = str(refusal["message"])
        self._emit_error(s, message, refusal.get("hint"), False, key="config",
                         code=str(refusal.get("code") or "config"), provider=_provider_ref(rp))
        if s.turn is not None:
            s.turn.stop = "error"
            s.turn.error = message

    async def _connect(self, s: _Session, rp: Any = None) -> Any:
        sdk = self._sdk
        if rp is None:
            rp = await self._resolve_provider()
        toolbelt = await build_toolbelt(self._server, self._intel,
                                        lambda directive: self._emit_ui(s, directive), sdk)
        s.tool_defaults = dict(getattr(toolbelt, "defaults", None) or {})
        s.stderr_tail.clear()  # a failure below must be judged on THIS process's stderr
        options = self._options(s, toolbelt, rp)  # never logged: its env holds the key
        s.config_dir = self._config_dir(rp)
        client = sdk.ClaudeSDKClient(options=options)
        await client.connect()
        s.cost_base = 0.0  # a new CLI process starts its cumulative cost at zero
        return client

    @staticmethod
    def _config_dir(rp: Any) -> pathlib.Path:
        """The CLI config dir a provider's CLI writes its transcripts under:
        the env's ``CLAUDE_CONFIG_DIR`` (created 0700 for a BYOK provider),
        else the login's ``~/.claude``."""
        env = _field(rp, "env")
        raw = env.get("CLAUDE_CONFIG_DIR") if isinstance(env, Mapping) else None
        if not raw:
            raw = os.environ.get("CLAUDE_CONFIG_DIR") or str(pathlib.Path.home() / ".claude")
        path = pathlib.Path(raw)
        if _field(rp, "kind", LOGIN_KIND) != LOGIN_KIND:
            with contextlib.suppress(OSError):
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _base_options(self, *, allowed_tools: list, disallowed_tools: list,
                      mcp_servers: dict, can_use_tool: Callable, max_turns: int,
                      stderr: Callable[[str], None]) -> dict[str, Any]:
        """Every option that is the SAME for every provider (spec §0.1, §9.9).
        The BYOK code never touches these: only ``provider_options`` varies."""
        kw: dict[str, Any] = {
            "system_prompt": self._prompt_text(),
            "tools": [],
            "allowed_tools": list(allowed_tools),
            "disallowed_tools": list(disallowed_tools),
            "mcp_servers": dict(mcp_servers),
            "strict_mcp_config": True,
            "setting_sources": [],
            "verbatim_prompts": True,
            "permission_mode": "default",
            "can_use_tool": can_use_tool,
            "include_partial_messages": True,
            "max_turns": max_turns,
            "cwd": str(self._analyst_dir()),
            "stderr": stderr,
        }
        if self._cli_path:
            kw["cli_path"] = self._cli_path
        return kw

    def _options(self, s: _Session, toolbelt: Any, rp: Any = None) -> Any:
        if rp is None:
            rp = _LegacyProvider(model=self._model_override or DEFAULT_MODEL, effort=self.effort)
        kw = self._base_options(
            allowed_tools=toolbelt.allowed_tools, disallowed_tools=toolbelt.disallowed_tools,
            mcp_servers={SDK_SERVER_NAME: toolbelt.server_config},
            can_use_tool=self._can_use_tool_for(s), max_turns=MAX_TURNS,
            stderr=s.stderr_line)
        kw.update(provider_options(rp, model_override=self._model_override,
                                   effort_override=self.effort))
        if s.claude_session_id:
            kw["resume"] = s.claude_session_id
        return self._sdk.ClaudeAgentOptions(**kw)

    def sdk_module(self) -> Any:
        """The Agent SDK module (or None when it cannot be imported)."""
        return self._ensure_sdk()

    def check_options(self, rp: Any, *, env_extra: Mapping[str, str] | None = None,
                      mcp_servers: dict | None = None, allowed_tools: Iterable[str] = (),
                      max_turns: int = 1, stderr: Callable[[str], None] | None = None) -> Any:
        """``ClaudeAgentOptions`` for the settings' full check (spec §6.2).

        The analyst's own option builder (``_base_options`` +
        ``provider_options``) with the check's changes only: ``env_extra``
        merged over the provider's env (``CLAUDE_CODE_MAX_RETRIES=0``, the
        check's config dir, ...), the caller's dummy tool server instead of
        the toolbelt, ``max_turns=1`` and a ``can_use_tool`` that refuses
        everything (the dummy tool is pre-allowed). The real ``thinking``.
        """
        sdk = self._ensure_sdk()
        if sdk is None:
            raise Unavailable("sdk_missing", SDK_MISSING_HINT)
        env = _field(rp, "env")
        if isinstance(env, Mapping):
            for name in SECRET_ENV_VARS:
                self._redactor.add(env.get(name))

        async def refuse(tool_name: str, input_data: dict, ctx: Any) -> Any:
            # No interrupt: the turn then ends normally at max_turns=1.
            return sdk.PermissionResultDeny(message="Not available in a connection check.",
                                            interrupt=False)

        tail: deque = deque(maxlen=30)
        kw = self._base_options(
            allowed_tools=list(allowed_tools), disallowed_tools=[],
            mcp_servers=mcp_servers or {}, can_use_tool=refuse, max_turns=max_turns,
            stderr=stderr or (lambda line: tail.append(self._redactor.text(line))))
        kw.update(provider_options(rp, model_override=self._model_override,
                                   effort_override=self.effort))
        if env_extra:
            kw["env"] = {**kw.get("env", {}), **{str(k): str(v) for k, v in env_extra.items()}}
        return sdk.ClaudeAgentOptions(**kw)

    async def _scrub(self, s: _Session) -> None:
        """Rewrite the session's CLI transcripts without the keys (spec §9.6)."""
        s.needs_scrub = False
        if s.config_dir is None:
            return
        ids = set(s.seen_session_ids)
        if s.claude_session_id:
            ids.add(s.claude_session_id)
        try:
            files = await asyncio.to_thread(scrub_transcripts, s.config_dir, session_ids=ids,
                                            redact=self._redactor.text)
        except Exception as exc:  # noqa: BLE001 -- best effort, never fatal
            log.warning("analyst transcript scrub failed: %s", type(exc).__name__)
            return
        if files:
            log.info("analyst: removed a provider key from %d transcript file(s)", len(files))

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
        if turn is not None:
            self._flush_pending_error(s, turn, None)  # no ResultMessage came: no status
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
                    key: str | None = None, *, code: str = "unknown",
                    provider: dict | None = None) -> None:
        """One ``error`` event: ``{message, hint?, retryable, code, provider}``
        (spec §5). ``provider`` defaults to the session's current one."""
        turn = s.turn
        if turn is not None and key is not None:
            if key in turn.errors_emitted:
                return
            turn.errors_emitted.add(key)
        ref = provider or ({"id": s.provider["id"], "label": s.provider["label"]}
                           if s.provider else dict(LOGIN_PROVIDER))
        s.emit("error", _drop_none({"message": message, "hint": hint,
                                    "retryable": bool(retryable),
                                    "code": code if code in ERROR_CODES else "unknown",
                                    "provider": ref}))

    def _provider_copy(self, s: _Session, code: str, status: int | None,
                       detail: str) -> tuple[str, str | None, bool]:
        """Copy for a provider other than the Claude login (spec §5); an auth
        failure also marks the provider for ``status.reason=provider_auth``."""
        if code == "auth":
            self._auth_failure = s.gen  # cleared by the next settings change
        label = (s.provider or LOGIN_PROVIDER)["label"]
        model = (s.provider or {}).get("model")
        return provider_error_copy(code, label=label, host=s.host, model=model,
                                   status=status, detail=detail)

    def _flush_pending_error(self, s: _Session, turn: _Turn, status: Any) -> None:
        """Emit the AssistantMessage error held for the ResultMessage's HTTP
        status (``api_error_status`` decides the code first, spec §5)."""
        pending, turn.pending_error = turn.pending_error, None
        if not pending:
            return
        error, text = pending["error"], pending["text"]
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        code = classify_provider_error(status, error, text)
        if s.kind == LOGIN_KIND:
            if code == "auth":
                message, hint, retryable = _ASSISTANT_ERRORS["authentication_failed"]
            else:
                message, hint, retryable = _ASSISTANT_ERRORS.get(error,
                                                                 _ASSISTANT_ERRORS["unknown"])
        else:
            message, hint, retryable = self._provider_copy(s, code, status, text[:300])
        self._emit_error(s, message, hint, retryable, key=f"assistant:{error}", code=code)

    def _fail_config(self, s: _Session, rp: Any) -> None:
        """A turn refused before any CLI starts: the provider isn't ready."""
        reason = str(_field(rp, "reason", None) or "provider_not_configured")
        label = str(_field(rp, "label", None) or "the model provider")
        message, hint = _config_copy(reason, label)
        self._emit_error(s, message, hint, False, key="config", code="config",
                         provider=_provider_ref(rp))
        if s.turn is not None:
            s.turn.stop = "error"
            s.turn.error = reason

    def _fail_turn(self, s: _Session, exc: BaseException) -> None:
        if s.turn is not None:
            self._flush_streams(s, s.turn)
            self._flush_pending_error(s, s.turn, None)
        sdk = self._sdk
        not_found = getattr(sdk, "CLINotFoundError", None)
        tail = " ".join(s.stderr_tail)
        what = s.redact(f"{type(exc).__name__}: {exc}")
        code = "unknown"
        if isinstance(not_found, type) and isinstance(exc, not_found):
            message, hint, retryable = "The Claude CLI was not found.", CLI_MISSING_HINT, False
        elif s.claude_session_id and _NO_CONVERSATION_RX.search(f"{exc} {tail}"):
            s.claude_session_id = None  # never resume it again: start fresh next turn
            message, hint, retryable = RESUME_LOST_MESSAGE, None, True
        elif s.kind != LOGIN_KIND:
            code = classify_provider_error(None, None, f"{exc} {tail}")
            message, hint, retryable = self._provider_copy(s, code, None, what[:300])
            message = message[:400]
        elif _AUTH_RX.search(f"{exc} {tail}"):
            message = "The analyst could not sign in to Claude."
            hint, retryable, code = SIGN_IN_HINT, False, "auth"
        else:
            message = f"The analyst failed: {what}"[:400]
            hint, retryable = None, True
            code = classify_provider_error(None, None, f"{exc} {tail}")
        log.warning("analyst turn failed: %s", what)
        self._emit_error(s, message, hint, retryable, key="fail", code=code)
        if s.turn is not None:
            s.turn.stop = "error"
            s.turn.error = message

    def _finish_turn(self, s: _Session, turn_id: str) -> None:
        turn = s.turn
        if turn is None or turn.turn_id != turn_id or turn.ended:
            return
        turn.ended = True
        self._flush_streams(s, turn)
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

    #: stream kind -> the event its text goes out as.
    _STREAM_EVENTS = (("text", "text_delta"), ("thinking", "thinking"))

    def _flush_streams(self, s: _Session, turn: _Turn) -> None:
        """Send the streamed text ``_Session.stream`` still holds (a block
        ended, another event follows, or the turn ends)."""
        for kind, event in self._STREAM_EVENTS:
            rest = s.flush_stream(kind)
            if rest:
                s.emit(event, {"turn_id": turn.turn_id, "text": rest})

    def _on_stream_event(self, s: _Session, turn: _Turn, msg: Any) -> None:
        if getattr(msg, "parent_tool_use_id", None):
            return
        ev = getattr(msg, "event", None) or {}
        kind = ev.get("type")
        if kind == "content_block_start":
            self._flush_streams(s, turn)
            block = ev.get("content_block") or {}
            if block.get("type") == "text":
                turn.text_chars = 0
                if turn.any_text:  # a new text block: keep paragraphs apart
                    s.emit("text_delta", {"turn_id": turn.turn_id, "text": "\n\n"})
            elif block.get("type") == "thinking":
                turn.thinking_chars = 0
        elif kind == "content_block_delta":
            # Through the session's stream redactor: a key split over short
            # deltas is caught across them (spec §9.5; secrets review).
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                turn.text_chars += len(delta["text"])
                turn.any_text = True
                out = s.stream("text", delta["text"])
                if out:
                    s.emit("text_delta", {"turn_id": turn.turn_id, "text": out})
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                turn.thinking_chars += len(delta["thinking"])
                out = s.stream("thinking", delta["thinking"])
                if out:
                    s.emit("thinking", {"turn_id": turn.turn_id, "text": out})
        elif kind in ("content_block_stop", "message_stop"):
            self._flush_streams(s, turn)

    def _on_assistant(self, s: _Session, turn: _Turn, msg: Any) -> None:
        if getattr(msg, "parent_tool_use_id", None):
            return
        self._flush_streams(s, turn)
        error = getattr(msg, "error", None)
        if error:
            # Held until the ResultMessage: its api_error_status decides the
            # code (a gateway 401 arrives as "invalid_request"; spec §5).
            text = " ".join(str(getattr(b, "text", "") or "")
                            for b in getattr(msg, "content", None) or []).strip()
            if turn.pending_error is None:
                turn.pending_error = {"error": str(error), "text": s.redact(text)}
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
                else:
                    s.redact(block.text)  # a key in it flags the transcript scrub
                turn.text_chars = 0
            elif self._is(block, "ThinkingBlock"):
                if turn.thinking_chars == 0 and block.thinking:
                    s.emit("thinking", {"turn_id": turn.turn_id, "text": block.thinking})
                else:
                    s.redact(block.thinking)
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
        if isinstance(data.get("session_id"), str):
            s.seen_session_ids.add(data["session_id"])  # the scrub's target, never resumed
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
        self._flush_streams(s, turn)
        turn.got_result = True
        status = getattr(msg, "api_error_status", None)
        self._flush_pending_error(s, turn, status)
        if isinstance(getattr(msg, "session_id", None), str):
            s.claude_session_id = msg.session_id
            s.seen_session_ids.add(msg.session_id)
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
        # The CLI prices a model it doesn't know from its own table: for any
        # provider but Claude's the dollars would be invented (spec §8).
        priced = s.cost_basis != COST_UNRELIABLE
        s.emit("usage", _drop_none({
            "turn_id": turn.turn_id,
            "cost_usd": cost if priced else None,
            "session_cost_usd": round(s.cost_total, 6) if priced and cost is not None else None,
            "cost_basis": s.cost_basis,
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
            detail = s.redact(str(detail))[:400]  # redacted BEFORE the cut
            if not isinstance(status, int) or isinstance(status, bool):
                status = None
            code = classify_provider_error(status, None, detail)
            if _NO_CONVERSATION_RX.search(detail):
                s.claude_session_id = None  # the resumed transcript is gone
                self._emit_error(s, RESUME_LOST_MESSAGE, None, True, key="fail")
            elif s.kind != LOGIN_KIND:
                message, hint, retryable = self._provider_copy(s, code, status, detail[:300])
                self._emit_error(s, message, hint, retryable, key="result", code=code)
            elif _AUTH_RX.search(detail) or status == 401:
                self._emit_error(s, "The analyst could not sign in to Claude.", SIGN_IN_HINT,
                                 False, key="assistant:authentication_failed", code="auth")
            else:
                self._emit_error(s, f"The analyst turn failed: {detail}", None,
                                 status in (429, 500, 502, 503, 529), key="result", code=code)
            turn.stop = "error"
            turn.error = detail
        else:
            turn.stop = "end"
            if self._auth_failure is not None and self._auth_failure == s.gen:
                self._auth_failure = None  # the provider took the key after all

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
    "Unavailable", "chat_router", "classify_provider_error", "provider_error_copy",
    "provider_options", "scrub_transcripts",
]
