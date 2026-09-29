"""The analyst's toolbelt: an in-process Agent SDK MCP server named ``godseye``.

Built per chat session (contract §5.1):

* One SDK tool per REAL godseye MCP tool, generated from
  ``await server.mcp.list_tools()`` (same name, description and input schema).
  The handler calls ``await server.mcp.call_tool(name, args)`` in-process, on
  the host's event loop -- no HTTP, and no bearer token on the CLI's argv.
* EXCLUDED, not exposed at all: ``uav_list_tracks`` (uncapped; the intel tools
  replace it), ``sim_set_environment`` (legacy; it zeroes the wind) and
  ``uav_handoff_target`` (the GEV panel's alias of ``mission_handoff_track``:
  same implementation, minus ``dry_run`` and ``alt_agl_m``, so the analyst
  loses nothing and stops paying for, and choosing between, two
  definitions of one action).  Sim ground truth (``uav://targets``) and camera PNGs are unreachable because
  ``read_intel_resource`` only admits an allowlist of URIs.
* Curated tools: ``intel_overview``, ``intel_search``, ``intel_entity``,
  ``read_intel_resource`` and the ``ui_*`` directives, which call ``emit_ui``
  (``ui_show_map`` frames an area on the map: WG v2 §3.6, §3.7).
* Every proxied call runs with ``theater_tools.CALL_VIA`` set to ``"console"``,
  so a theater switch the analyst makes is recorded as ``set_via: "console"``
  (WG v2 §4.1.3 step 6); ``/mcp`` callers keep the default ``"mcp"``.
* Simulated wargame tools (M14a; WG v2 §5.2.11, unit B8) come only from the
  server's own never-mounted registry ``server.wargame_mcp``; any ``wg_*`` on
  ``server.mcp`` (``--wargame-mcp``) is skipped. ``mode="isr"`` (the default)
  carries only ``WG_ENTRY_TOOLS``; ``mode="wargame"`` carries every ``wg_*``
  tool. Each ``wg_*`` proxy calls ``server.wargame_mcp.call_tool`` inside
  ``wargame.console_call(session_id)``: that is the only way an engagement
  the console authorized for this chat session can execute (§3.8).

Result shaping (every result, proxied or curated): compact JSON; tools that
take ``detail`` / ``top_n`` get ``detail="summary"`` / ``top_n=10`` when the
model did not set them; anything still over 24,000 characters is cut down with
explicit ``{"_truncated": true, "_omitted": N, "_note": ...}`` markers and a
top-level ``_truncated`` note.  Nothing is ever dropped silently: the CLI spills
large results to a file the analyst has no tool to read, so the cap is ours.

``claude_agent_sdk`` is passed in as ``sdk`` (lazy import lives in chat.py), so
this module imports without it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import analyst_policy, theater_tools
from .analyst_policy import (
    DRY_RUN_TOOLS,
    SDK_SERVER_NAME,
    STATIC_AUTO_TOOLS,
    TOOL_PREFIX,
    classify,
)

if TYPE_CHECKING:  # pragma: no cover
    from .intel_graph import IntelService

#: Server tools never exposed to the analyst (module docstring says why).
EXCLUDED_TOOLS = ("uav_list_tracks", "sim_set_environment", "uav_handoff_target")

#: The simulated wargame's tool namespace (M14a, D3).
WG_PREFIX = "wg_"
#: Toolbelt modes: ISR (the default) and a simulated wargame session.
MODE_ISR, MODE_WARGAME = "isr", "wargame"
#: The only ``wg_*`` tools the ISR toolbelt carries (WG v2 §3.7).
#: ``analyst_policy.WG_ENTRY_TOOLS`` owns the set; the literal is the same set.
WG_ENTRY_TOOLS: frozenset[str] = frozenset(
    getattr(analyst_policy, "WG_ENTRY_TOOLS", None)
    or {"wg_session_start", "wg_session_status", "wg_list_classes"})

#: Hard cap on the text of any tool result handed to the model.
RESULT_CHAR_LIMIT = 24_000
#: ``intel_entity`` is capped tighter than the HTTP route (60 KB).
ENTITY_CHAR_LIMIT = 20_000
#: ``intel_overview`` is specified at <= 6 KB; shaped with headroom.
OVERVIEW_CHAR_LIMIT = 8_000

#: Defaults forced onto heavy report tools when the model leaves them unset.
FORCED_DEFAULTS = {"detail": "summary", "top_n": 10}

#: The only server resources the analyst may read (contract §5.1).
RESOURCE_ALLOWLIST = (
    re.compile(r"^uav://mission/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
    re.compile(r"^uav://reports/(?:latest|[A-Za-z0-9][A-Za-z0-9_.-]{0,63})$"),
    re.compile(r"^uav://pattern-of-life/(?:all|[A-Za-z0-9][A-Za-z0-9 _.'-]{0,79})$"),
    re.compile(r"^uav://safety/geofence$"),
    re.compile(r"^uav://[A-Za-z0-9][A-Za-z0-9_-]{0,39}/telemetry$"),
)

#: Results longer than this are parsed and shaped on a worker thread.
_OFFLOAD_CHARS = 200_000

#: An intel-graph id: one of the graph's type prefixes (``sit`` is a mapped
#: site, WG v2 §3.1, R26; ``frc``/``eng``/``vec`` are the simulated wargame's
#: forces, engagements and vectors, R26), then the rest of the chip grammar
#: ``[[prefix:id|label]]``.
_ENTITY_ID = re.compile(
    r"^(?:veh|msn|trk|unit|ob|rpt|thr|poi|sit|frc|eng|vec|alarm|feed):[^\r\n\[\]|]{1,160}$")
_MAX_ENTITIES = 20
#: ``ui_show_map`` takes 1 to this many graph ids (WG v2 §3.7, R12).
MAP_IDS_MAX = 50


@dataclass
class Toolbelt:
    server_config: Any
    tool_names: list[str]
    allowed_tools: list[str]
    excluded: list[str]
    #: The SdkMcpTool objects, in registration order (handlers are reachable
    #: for tests and diagnostics).
    tools: list[Any] = field(default_factory=list)
    #: tool -> {param: default} from each proxied tool's input schema. The
    #: approval card compares a dry run with the live call on EFFECTIVE plans,
    #: i.e. with each path's own defaults filled in.
    defaults: dict[str, dict] = field(default_factory=dict)
    #: ``"isr"`` or ``"wargame"``: which ``wg_*`` tools the belt carries.
    mode: str = MODE_ISR

    @property
    def disallowed_tools(self) -> list[str]:
        return [f"{TOOL_PREFIX}{name}" for name in self.excluded]


# ------------------------------------------------------------------ shaping --

def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _is_marker(x: Any) -> bool:
    return isinstance(x, dict) and x.get("_truncated") is True and "_omitted" in x


def _measure(obj: Any, path: tuple, lists: list, strings: list) -> int:
    """Serialized size of ``obj`` (compact JSON), collecting shrink candidates."""
    if isinstance(obj, dict):
        size = 2 + max(0, len(obj) - 1)
        for k, v in obj.items():
            size += len(dumps_compact(str(k))) + 1
            size += _measure(v, path + (k,), lists, strings)
        return size
    if isinstance(obj, list):
        size = 2 + max(0, len(obj) - 1)
        for i, v in enumerate(obj):
            size += _measure(v, path + (i,), lists, strings)
        real = len(obj) - (1 if obj and _is_marker(obj[-1]) else 0)
        if real >= 2:
            lists.append((size, path))
        return size
    text = dumps_compact(obj)
    if isinstance(obj, str) and len(obj) > 1000:
        strings.append((len(obj), path))
    return len(text)


def _get(obj: Any, path: tuple) -> Any:
    for key in path:
        obj = obj[key]
    return obj


def _set(obj: Any, path: tuple, value: Any) -> Any:
    if not path:
        return value
    parent = _get(obj, path[:-1])
    parent[path[-1]] = value
    return obj


def _shrink_list(lst: list, cap: int, list_size: int, excess: int) -> list:
    """Drop at least half of ``lst`` -- more when that is plainly not enough."""
    marker = lst[-1] if lst and _is_marker(lst[-1]) else None
    items = lst[:-1] if marker is not None else lst
    already = int(marker["_omitted"]) if marker is not None else 0
    avg = max(1.0, (list_size - 2) / max(1, len(lst)))
    needed = int(excess / avg) + 1
    keep = max(1, min(len(items) // 2, len(items) - needed))
    omitted = already + (len(items) - keep)
    return items[:keep] + [{
        "_truncated": True, "_omitted": omitted,
        "_note": (f"{omitted} more item(s) omitted to fit the {cap:,}-character "
                  "result cap; narrow the request (a specific id, detail='summary', "
                  "a smaller top_n) to see them."),
    }]


def shrink(obj: Any, limit: int = RESULT_CHAR_LIMIT) -> tuple[Any, bool]:
    """Return ``(obj', truncated)`` with ``dumps_compact(obj') <= limit``.

    Works on a deep copy.  Lists are halved (largest first) and end with an
    explicit marker; very long strings are cut with an inline note; as a last
    resort trailing dict keys are dropped and named in ``_omitted_keys``.
    """
    text = dumps_compact(obj)
    if len(text) <= limit:
        return obj, False
    work = json.loads(text)
    budget = max(200, limit - 450)  # headroom for the top-level note
    for _ in range(200):
        lists: list = []
        strings: list = []
        size = _measure(work, (), lists, strings)
        if size <= budget:
            break
        if lists:
            list_size, path = max(lists, key=lambda item: item[0])
            work = _set(work, path, _shrink_list(_get(work, path), limit, list_size,
                                                 size - budget))
            continue
        if strings:
            length, path = max(strings, key=lambda item: item[0])
            excess = size - budget
            keep = max(200, length - excess - 64)
            if keep >= length:
                keep = length // 2
            value = _get(work, path)
            work = _set(work, path,
                        value[:keep] + f"…[truncated {length - keep} chars]")
            continue
        break
    if len(dumps_compact(work)) > budget and isinstance(work, dict):
        kept: dict = {}
        dropped: list[str] = []
        running = 2
        for k, v in work.items():
            piece = len(dumps_compact({k: v})) - 1
            if running + piece <= budget - 800:  # room for the omitted-key list below
                kept[k] = v
                running += piece
            else:
                dropped.append(str(k)[:40])
        kept["_omitted_keys"] = dropped[:15]
        kept["_omitted_key_count"] = len(dropped)
        work = kept
    if len(dumps_compact(work)) > budget:
        preview = dumps_compact(work)[: max(100, budget - 300)]
        work = {"_truncated": True, "_note": "The result was too large to structure; "
                "this is a raw prefix of it.", "preview": preview}
        return work, True
    note = (f"This result was cut to fit the {limit:,}-character cap. Lists that were "
            "shortened end with a marker giving the number of omitted items.")
    if isinstance(work, dict):
        # Our flag must win: a payload's own `_truncated` (IntelService reports
        # {"dropped": [...]} there) used to overwrite it through `**work`, so a
        # cut result read as not truncated. The payload's report is kept.
        prior = work.pop("_truncated", None)
        prior_note = work.pop("_note", None)
        head: dict = {"_truncated": True,
                      "_note": f"{note} {prior_note}" if isinstance(prior_note, str) else note}
        if prior not in (None, True, False) and "_truncation" not in work:
            head["_truncation"] = prior
        work = {**head, **work}
    else:
        work = {"_truncated": True, "_note": note, "result": work}
    return work, True


def shape_text(payload: Any, limit: int = RESULT_CHAR_LIMIT) -> tuple[str, bool]:
    shaped, truncated = shrink(payload, limit)
    return dumps_compact(shaped), truncated


def extract_entities(payload: Any) -> list[str]:
    """Intel-graph ids mentioned in a tool payload (for UI chips)."""
    found: list[str] = []

    def add(eid: str) -> None:
        if eid not in found and len(found) < _MAX_ENTITIES:
            found.append(eid)

    def walk(node: Any, depth: int = 0) -> None:
        if len(found) >= _MAX_ENTITIES or depth > 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and v:
                    if k in ("vehicle", "from_vehicle", "to_vehicle", "observer_vehicle"):
                        if len(v) <= 64:
                            add(f"veh:{v}")
                    elif k in ("mission_id", "mission_handle") and v.startswith("MSN-"):
                        add(f"msn:{v}")
                    elif k == "track_id" and len(v) <= 80:
                        add(f"trk:{v}")
                    elif k in ("id", "entity_id") and _ENTITY_ID.match(v):
                        add(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node[:200]:
                walk(item, depth + 1)

    walk(payload)
    return found


def result_text(result: dict) -> str:
    """Join the text blocks of an SDK tool result dict."""
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _ok(payload: Any, *, limit: int = RESULT_CHAR_LIMIT) -> dict:
    text, _ = shape_text(payload, limit)
    return {"content": [{"type": "text", "text": text}]}


def _err(code: str, message: str, **extra: Any) -> dict:
    body = {"error": {"code": code, "message": message, **extra}}
    return {"content": [{"type": "text", "text": dumps_compact(body)}], "is_error": True}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _accepts_kw(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


async def _intel_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call an intel reader without blocking the event loop.

    ``IntelService`` is synchronous: it snapshots the bridge and builds the
    graph (tens of ms on a busy picture). The HTTP routes run it in the
    threadpool; the toolbelt runs on the host's loop, which also serves the
    SSE streams and the in-process MCP server, so it offloads the same way.
    Coroutine functions (async readers, test doubles) are awaited directly.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await _maybe_await(await asyncio.to_thread(fn, *args, **kwargs))


# ---------------------------------------------------------------- proxying --

def _exc_message(exc: BaseException) -> str:
    msg = str(exc) or type(exc).__name__
    cause = exc.__cause__
    if cause is not None and str(cause) and str(cause) not in msg:
        msg = f"{msg}: {type(cause).__name__}: {cause}"
    return msg[:2000]


def _call_result_text(result: Any) -> tuple[str, bool]:
    """(text, is_error) from an mcp CallToolResult (or an input-required result)."""
    content = getattr(result, "content", None)
    if content is None:
        return dumps_compact({"error": {"code": "unsupported_result",
                                        "message": type(result).__name__}}), True
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)
        else:
            texts.append(dumps_compact({"_omitted_block": getattr(block, "type", "?")}))
    return "".join(texts), bool(getattr(result, "is_error", False))


def _parse_and_shape(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"text": text}
    return shape_text(payload)[0]


def _approval_hint(name: str) -> str:
    probe = classify(name, {})
    if probe.auto:
        return ""
    if name in DRY_RUN_TOOLS:
        return (" [Console: dry_run=true runs without approval; a live call waits for "
                "the operator to approve it.]")
    return " [Console: waits for the operator to approve it.]"


def _proxy_tool(sdk: Any, server: Any, info: Any, *, registry: Any = None,
                session_id: str | None = None) -> Any:
    """One SDK tool that calls ``info.name`` on ``registry`` (default
    ``server.mcp``) in-process. A ``wg_*`` tool runs inside
    ``wargame.console_call(session_id)`` (WG v2 §3.8 step 5)."""
    name = info.name
    wargame = name.startswith(WG_PREFIX)
    schema = dict(getattr(info, "input_schema", None) or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    props = schema.get("properties") or {}
    forced = {k: v for k, v in FORCED_DEFAULTS.items() if k in props}
    description = (getattr(info, "description", None) or name).strip()
    if forced:
        description += (" [Console: detail defaults to 'summary' and top_n to 10; results "
                        f"over {RESULT_CHAR_LIMIT:,} characters are truncated with markers.]")
    description += _approval_hint(name)

    async def handler(args: dict) -> dict:
        call_args = dict(args or {})
        for key, value in forced.items():
            call_args.setdefault(key, value)
        # The call source for the theater switch's `set_via` (WG v2 §4.1.3):
        # set around the in-process call only, so nothing else inherits it.
        token = theater_tools.CALL_VIA.set("console")
        target = server.mcp if registry is None else registry
        try:
            if wargame:
                # The console's approval path (§3.8): the engine executes an
                # engagement only for the chat session that authorized it.
                from .wargame import console_call

                with console_call(session_id or ""):
                    result = await target.call_tool(name, call_args)
            else:
                result = await target.call_tool(name, call_args)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the model, never raised
            return _err("tool_failed", _exc_message(exc), tool=name)
        finally:
            theater_tools.CALL_VIA.reset(token)
        text, is_error = _call_result_text(result)
        if len(text) > _OFFLOAD_CHARS:  # keep a multi-MB parse off the event loop
            shaped = await asyncio.to_thread(_parse_and_shape, text)
        else:
            shaped = _parse_and_shape(text)
        out: dict = {"content": [{"type": "text", "text": shaped}]}
        if is_error:
            out["is_error"] = True
        return out

    return sdk.tool(name, description, schema)(handler)


# ---------------------------------------------------------------- curated --

def _curated_tools(sdk: Any, server: Any, intel: Any, emit_ui: Callable[[dict], Any]) -> list:
    tool = sdk.tool

    def _no_intel() -> dict:
        return _err("intel_unavailable", "The intel service is not available in this host.")

    @tool("intel_overview",
          "Compact situation picture: theater, vehicles (fuel vs BINGO, state, link), running "
          "missions, contact counts by category / confidence / threat, the latest alarms and "
          "the data caveats. Start here.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def intel_overview(args: dict) -> dict:
        if intel is None:
            return _no_intel()
        try:
            return _ok(await _intel_call(intel.overview), limit=OVERVIEW_CHAR_LIMIT)
        except Exception as exc:  # noqa: BLE001
            return _err("intel_failed", _exc_message(exc))

    @tool("intel_search",
          "Search the intel graph (vehicles, missions, contacts, units, equipment classes, "
          "reports, theater, mapped sites, POIs, alarms, feeds). Returns ranked nodes: id, "
          "type, label, subtitle, status. Use the ids with intel_entity and in "
          "[[type:id|label]] markup. Sites are mapped OpenStreetMap data: context only, "
          "not verified.",
          {"type": "object",
           "properties": {
               "query": {"type": "string", "minLength": 1, "maxLength": 200,
                         "description": "Free text: a name, id, class, category or place."},
               "types": {"type": "array", "maxItems": 12,
                         "items": {"type": "string", "maxLength": 32},
                         "description": "Optional node types: vehicle, mission, track "
                                        "(contact), unit, equipment (ob), report, theater, "
                                        "site (mapped strategic site), poi, alarm, feed."},
               "limit": {"type": "integer", "minimum": 1, "maximum": 25}},
           "required": ["query"], "additionalProperties": False})
    async def intel_search(args: dict) -> dict:
        if intel is None:
            return _no_intel()
        query = str(args.get("query") or "")[:200]
        types = args.get("types") or None
        limit = min(25, max(1, int(args.get("limit") or 10)))
        try:
            results = await _intel_call(intel.search, query, types, limit)
        except Exception as exc:  # noqa: BLE001
            return _err("intel_failed", _exc_message(exc))
        results = list(results or [])[:limit]
        return _ok({"query": query, "count": len(results), "results": results})

    @tool("intel_entity",
          "Full details of one intel entity by graph id (e.g. veh:Drone1, trk:TRK-…, "
          "msn:MSN-…): fields, provenance (measured vs assumed) and related entities. "
          f"Capped at {ENTITY_CHAR_LIMIT:,} characters.",
          {"type": "object",
           "properties": {"id": {"type": "string", "minLength": 3, "maxLength": 200}},
           "required": ["id"], "additionalProperties": False})
    async def intel_entity(args: dict) -> dict:
        if intel is None:
            return _no_intel()
        eid = str(args.get("id") or "")
        # IntelService trims to a byte budget with explicit `_truncated`
        # markers (UTF-8 bytes >= characters, so it also fits the char cap);
        # `_ok` below stays as the backstop for readers without `max_bytes`.
        kw = {"max_bytes": ENTITY_CHAR_LIMIT} if _accepts_kw(intel.entity, "max_bytes") else {}
        try:
            ent = await _intel_call(intel.entity, eid, **kw)
        except Exception as exc:  # noqa: BLE001
            return _err("intel_failed", _exc_message(exc))
        if ent is None:
            return _err("unknown_entity", f"No entity {eid!r}. Use intel_search to find ids.")
        return _ok(ent, limit=ENTITY_CHAR_LIMIT)

    @tool("read_intel_resource",
          "Read one server resource. Allowed: uav://mission/{id}, uav://reports/{id|latest}, "
          "uav://pattern-of-life/{poi|all}, uav://safety/geofence, uav://{vehicle}/telemetry. "
          "Nothing else is readable.",
          {"type": "object",
           "properties": {"uri": {"type": "string", "minLength": 6, "maxLength": 200}},
           "required": ["uri"], "additionalProperties": False})
    async def read_intel_resource(args: dict) -> dict:
        uri = str(args.get("uri") or "").strip()
        if not any(rx.match(uri) for rx in RESOURCE_ALLOWLIST):
            return _err("resource_not_allowed",
                        f"{uri!r} is not readable from the console. Allowed: "
                        "uav://mission/{id}, uav://reports/{id|latest}, "
                        "uav://pattern-of-life/{poi|all}, uav://safety/geofence, "
                        "uav://{vehicle}/telemetry.")
        if server is None:
            return _err("server_unavailable", "The godseye server is not available.")
        try:
            contents = list(await server.mcp.read_resource(uri))
        except Exception as exc:  # noqa: BLE001
            return _err("resource_failed", _exc_message(exc), uri=uri)
        if not contents:
            return _err("resource_empty", f"{uri} returned no content.")
        body = getattr(contents[0], "content", None)
        if not isinstance(body, str):
            return _err("resource_binary", f"{uri} is binary and cannot be read here.")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"text": body}
        return _ok(payload)

    def _emit(directive: dict) -> dict:
        try:
            emit_ui(directive)
        except Exception as exc:  # noqa: BLE001
            return _err("ui_failed", _exc_message(exc))
        return {"content": [{"type": "text", "text": dumps_compact({"ok": True})}]}

    def _ids(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(x)[:200] for x in value if isinstance(x, str) and x][:50]

    @tool("ui_focus",
          "Highlight entities in the operator's intel orb (graph ids such as veh:Drone1, "
          "trk:TRK-…). Call it whenever you point the operator at specific entities.",
          {"type": "object",
           "properties": {"ids": {"type": "array", "minItems": 1, "maxItems": 50,
                                  "items": {"type": "string", "maxLength": 200}},
                          "note": {"type": "string", "maxLength": 500}},
           "required": ["ids"], "additionalProperties": False})
    async def ui_focus(args: dict) -> dict:
        directive: dict = {"action": "focus", "ids": _ids(args.get("ids"))}
        if isinstance(args.get("note"), str) and args["note"]:
            directive["note"] = args["note"][:500]
        return _emit(directive)

    @tool("ui_track",
          "Switch the console to tracking mode: the map opens and the camera follows the "
          "drone. Use it when the operator wants to watch a drone.",
          {"type": "object",
           "properties": {"vehicle": {"type": "string", "minLength": 1, "maxLength": 64},
                          "reason": {"type": "string", "maxLength": 200}},
           "required": ["vehicle", "reason"], "additionalProperties": False})
    async def ui_track(args: dict) -> dict:
        return _emit({"action": "track", "vehicle": str(args.get("vehicle") or "")[:64],
                      "reason": str(args.get("reason") or "")[:200]})

    @tool("ui_show_orb", "Leave tracking mode and return the console to the intel orb.",
          {"type": "object", "properties": {}, "additionalProperties": False})
    async def ui_show_orb(args: dict) -> dict:
        return _emit({"action": "orb"})

    @tool("ui_inspect", "Open the entity inspector on one graph id.",
          {"type": "object",
           "properties": {"id": {"type": "string", "minLength": 3, "maxLength": 200}},
           "required": ["id"], "additionalProperties": False})
    async def ui_inspect(args: dict) -> dict:
        return _emit({"action": "inspect", "id": str(args.get("id") or "")[:200]})

    @tool("ui_show_map",
          "Show an area on the operator's map: the map frames every listed graph id "
          "(thr:… for the theater, sit:…, veh:…, trk:…, poi:…). Use it when the operator "
          "wants to see an area; ui_track follows one drone instead.",
          {"type": "object",
           "properties": {"ids": {"type": "array", "minItems": 1, "maxItems": MAP_IDS_MAX,
                                  "items": {"type": "string", "maxLength": 200}},
                          "reason": {"type": "string", "maxLength": 200}},
           "required": ["ids", "reason"], "additionalProperties": False})
    async def ui_show_map(args: dict) -> dict:
        problem, ids = map_ids(args.get("ids"))
        if problem:
            return _err("invalid_ids", problem)
        return _emit({"action": "map", "ids": ids,
                      "reason": str(args.get("reason") or "")[:200]})

    return [intel_overview, intel_search, intel_entity, read_intel_resource,
            ui_focus, ui_track, ui_show_orb, ui_inspect, ui_show_map]


def map_ids(value: Any) -> tuple[str | None, list[str]]:
    """``(problem, ids)`` for ``ui_show_map``'s ``ids`` (WG v2 §3.7, R12): 1 to
    ``MAP_IDS_MAX`` intel-graph ids in the chip grammar, de-duplicated in
    order. ``problem`` is one sentence for the model, or None."""
    if not isinstance(value, list) or not value:
        return "ids must list 1 to 50 graph ids, e.g. thr:<theater id>.", []
    if len(value) > MAP_IDS_MAX:
        return f"ids lists {len(value)} ids; the map takes at most {MAP_IDS_MAX}.", []
    bad = [x for x in value if not (isinstance(x, str) and _ENTITY_ID.match(x))]
    if bad:
        return (f"{len(bad)} of the ids are not graph ids (prefix:id, such as thr:…, sit:…, "
                "veh:… or trk:…); take them from intel_search or intel_entity."), []
    return None, list(dict.fromkeys(value))


async def build_toolbelt(server: Any, intel: IntelService | None,
                         emit_ui: Callable[[dict], Any], sdk: Any, *,
                         session_id: str | None = None, mode: str = MODE_ISR) -> Toolbelt:
    """Generate the ``godseye`` SDK server for one chat session.

    ``session_id`` is the chat session the ``wg_*`` proxies speak for
    (``wargame.console_call``); ``mode`` is ``"isr"`` (only ``WG_ENTRY_TOOLS``)
    or ``"wargame"`` (every ``wg_*`` tool). Anything else is ISR.
    """
    mode = MODE_WARGAME if mode == MODE_WARGAME else MODE_ISR
    curated = _curated_tools(sdk, server, intel, emit_ui)
    curated_names = {t.name for t in curated}
    proxied: list = []
    excluded: list[str] = list(EXCLUDED_TOOLS)
    defaults: dict[str, dict] = {}
    if server is not None:
        for info in await server.mcp.list_tools():
            if info.name in EXCLUDED_TOOLS:
                continue
            if info.name.startswith(WG_PREFIX):
                continue  # `--wargame-mcp` copies: the wargame registry supplies them
            if info.name in curated_names:
                excluded.append(info.name)  # the curated tool of that name wins
                continue
            proxied.append(_proxy_tool(sdk, server, info))
            defaults[info.name] = schema_defaults(getattr(info, "input_schema", None))
        registry = getattr(server, "wargame_mcp", None)
        for info in (await registry.list_tools()) if registry is not None else ():
            if not info.name.startswith(WG_PREFIX) or info.name in curated_names:
                continue
            if mode == MODE_ISR and info.name not in WG_ENTRY_TOOLS:
                continue  # only a wargame session carries it
            proxied.append(_proxy_tool(sdk, server, info, registry=registry,
                                       session_id=session_id))
            defaults[info.name] = schema_defaults(getattr(info, "input_schema", None))
    tools = proxied + curated
    names = [t.name for t in tools]
    allowed = [f"{TOOL_PREFIX}{n}" for n in names if n in STATIC_AUTO_TOOLS]
    config = sdk.create_sdk_mcp_server(name=SDK_SERVER_NAME, version="1.0.0", tools=tools)
    return Toolbelt(server_config=config, tool_names=names, allowed_tools=allowed,
                    excluded=sorted(set(excluded)), tools=tools, defaults=defaults,
                    mode=mode)


def schema_defaults(schema: Any) -> dict:
    """``{param: default}`` for the properties of a JSON-Schema object."""
    props = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    return {k: v["default"] for k, v in props.items() if isinstance(v, dict) and "default" in v}


__all__ = [
    "ENTITY_CHAR_LIMIT",
    "EXCLUDED_TOOLS",
    "MAP_IDS_MAX",
    "RESOURCE_ALLOWLIST",
    "RESULT_CHAR_LIMIT",
    "WG_ENTRY_TOOLS",
    "Toolbelt",
    "build_toolbelt",
    "dumps_compact",
    "extract_entities",
    "map_ids",
    "result_text",
    "schema_defaults",
    "shape_text",
    "shrink",
]
