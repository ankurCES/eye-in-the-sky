"""Runtime theater and sim-speed tools (WG v2 §3.7, §3.10, §4.1.2-§4.1.6; A7).

Five tools on `server.mcp`, all SIMULATION administration (M14: ISR only):

* `geo_lookup` (read): a place name or coordinates -> candidate places.
* `geo_sites` (read): the mapped strategic sites of the running theater
  (context only: mapped, not verified; never targets).
* `theater_propose` (plan): builds a theater proposal (`theater_plan`, A4)
  and returns the exact `set_args` for the next call.
* `sim_set_theater` (sim, never session-grantable): checks the args against
  the stored proposal, then runs `theater_switch.switch` on the tasking loop.
* `sim_set_time_scale` (sim): the fake simulator's speed, x1 to x10.

Plus the helpers other units read (§3.10):

* `approval_preview(srv, tool, args)`: `theater_preview` / `time_scale_preview`
  for the chat slip (§3.6). Caches only, never raises, `{}` on error.
* `theater_state(srv)`: the §3.2 theater block (the intel graph's source, R22).
* `time_scale_caveats(srv, scale)`: the §4.1.6 caveat lines, in order.
* `CALL_VIA`: `"console"` when the analyst toolbelt proxy made the call,
  otherwise `"mcp"`; it becomes the switch's `set_via`.

This module duck-types `srv` (the `GodseyeUavServer`) and never imports
`server.py` (§0.2). The A4 modules are imported lazily through `_plan()` and
`_switch()`, which is also the seam tests replace. The A4 interface used here:

* `theater_plan.ProposalBook(max=16, ttl_s=1800)` with `.get(proposal_id)`
  (None when unknown or expired);
* `theater_plan.propose(srv, args, *, fetch) -> dict` (blocking; the tool
  result of §4.1.2, or a `{rejected, error, message}` refusal); it stores
  the proposal in `srv.theater_proposals`;
* `theater_plan.args_match(p, args) -> list[str]` (mismatched field names);
* `theater_plan.preview(srv, p) -> dict` (§3.6 `theater_preview`), plus
  `GROUND_SOURCE_TEXT`, `GEOCODER_TEXT`, `ao_area_km2`, `bbox_half_extent_m`;
* `theater_switch.switch(srv, p, via) -> dict` (a coroutine for the tasking
  loop): the result, or a `switch_refused` / `theater_integrity` refusal;
  `SwitchRefused.as_result()`;
* `theater_switch.quick_checks(srv, p)`.

Blocking work (geocoding, a sites refresh, a proposal) runs in worker threads
(§0.2), and the geodata client is `srv.geo_fetch` (None = the real one), so
tests inject every request. Places, labels and names are untrusted text
(§3.11): `geocode`, `sites` and `theaters` strip them as they are parsed, and
`theater_state` passes every string it reads through `theaters.clean_text`.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import math
from collections.abc import Mapping
from typing import Any

from . import geo_http, geocode, safety, theaters
from . import sites as _sites

_LOG = logging.getLogger(__name__)

#: Who made the current tool call: "console" when the analyst toolbelt proxy
#: set it (A9), "mcp" otherwise. Read on the calling loop, before the switch
#: hops to the tasking loop (a ContextVar does not follow
#: `run_coroutine_threadsafe`).
CALL_VIA: contextvars.ContextVar[str] = contextvars.ContextVar(
    "godseye_theater_call_via", default="mcp")

#: The five tools this module registers (§3.7), in registration order.
TOOL_NAMES: tuple[str, ...] = ("geo_lookup", "geo_sites", "theater_propose",
                               "sim_set_theater", "sim_set_time_scale")

#: §3.7 titles; `analyst_policy._TITLES` and `format.js TOOL_TITLES` match.
TITLES: Mapping[str, str] = {
    "geo_lookup": "Look up a place",
    "geo_sites": "List mapped sites",
    "theater_propose": "Propose a theater",
    "sim_set_theater": "Set the theater",
    "sim_set_time_scale": "Set sim speed",
}

#: `(calls, window_s)` budgets (§3.7). Only calls that may reach the network
#: spend one: an uncached lookup, a sites refresh, a proposal with geodata on.
BUDGETS: Mapping[str, tuple[int, float]] = {
    "geo_lookup": (30, 600.0),
    "geo_sites": (6, 600.0),
    "theater_propose": (10, 600.0),
}

#: The proposal runs in a worker thread under this bound (§4.1.2).
PROPOSE_TIMEOUT_S = 60.0
#: `ProposalBook` size and lifetime (§4.1.2 step 9).
PROPOSAL_MAX, PROPOSAL_TTL_S = 16, 1800.0

#: Argument bounds (§3.7).
QUERY_MIN, QUERY_MAX = geocode.QUERY_MIN, geocode.QUERY_MAX
LOOKUP_LIMIT_MIN, LOOKUP_LIMIT_MAX = geocode.LIMIT_MIN, geocode.LIMIT_MAX
SITES_LIMIT_DEFAULT, SITES_LIMIT_MAX = 40, 60
LABEL_MAX = 60
AO_VERTICES_MIN, AO_VERTICES_MAX = 3, 12
SCALE_MIN, SCALE_MAX = 1.0, 10.0

#: The server's safety tick and its floor at speed (`server.DEFAULT_TICK_S`,
#: `server.MONITOR_MIN_SLEEP_S`), mirrored because new modules never import
#: `server.py`; a test pins them equal.
DEFAULT_TICK_S = 0.5
MONITOR_MIN_SLEEP_S = 0.05

#: `mission_recon_route` defaults behind the capture-gap caveat (§4.1.6 #5).
RECON_CAPTURE_RATE_HZ = 1.0
RECON_ALT_AGL_M = 60.0
RECON_FORWARD_OVERLAP = 0.20
RECON_SPEED_MPS = 10.0

#: §4.1.6 caveats 1-3, verbatim.
FIXED_TIME_SCALE_CAVEATS: tuple[str, ...] = (
    "Link-loss timers stay in wall-clock seconds.",
    "Detections and scans run in real time.",
    "The analyst's clock is wall time.",
)

#: The one check a sim-speed preview carries (§3.6).
FAKE_SIM_CHECK = "Fake simulator"
#: Appended to a sim-speed preview whose requested speed is out of range.
SCALE_RANGE_CHECK = "Speed between ×1 and ×10"
#: The theater preview check a stale or mismatched request fails.
PROPOSAL_CHECK = "Proposal still valid"

RATE_LIMITED_MESSAGE = ("Too many {what} in the last {minutes} minutes; "
                        "try again in a few minutes.")


# ---------------------------------------------------------------- public --

def register(srv: Any) -> None:
    """Register the five tools on `srv.mcp` and create `srv.theater_proposals`
    (plus `srv.theater_budgets` and, when absent, `srv.geo_fetch = None`)."""
    srv.theater_proposals = _plan().ProposalBook(max=PROPOSAL_MAX, ttl_s=PROPOSAL_TTL_S)
    srv.theater_budgets = new_budgets()
    if not hasattr(srv, "geo_fetch"):
        #: A test double for every geodata request these tools make.
        srv.geo_fetch = None
    mcp = srv.mcp

    def tool(name: str) -> Any:
        return mcp.tool(name=name, title=TITLES[name], description=DESCRIPTIONS[name])

    @tool("geo_lookup")
    async def _geo_lookup(query: str, limit: int = 5) -> dict:
        return await geo_lookup(srv, query, limit)

    @tool("geo_sites")
    async def _geo_sites(category: str | None = None, limit: int = SITES_LIMIT_DEFAULT,
                         refresh: bool = False, near_lat: float | None = None,
                         near_lon: float | None = None) -> dict:
        return await geo_sites(srv, category, limit, refresh, near_lat, near_lon)

    @tool("theater_propose")
    async def _theater_propose(theater_id: str | None = None, lat: float | None = None,
                               lon: float | None = None, place_id: str | None = None,
                               label: str | None = None, place: str | None = None,
                               bbox: list[float] | None = None,
                               half_extent_m: float | None = None,
                               airframe: str | None = None, home_lat: float | None = None,
                               home_lon: float | None = None,
                               ground_msl_m: float | None = None,
                               query: str | None = None) -> dict:
        return await theater_propose(
            srv, theater_id=theater_id, lat=lat, lon=lon, place_id=place_id, label=label,
            place=place, bbox=bbox, half_extent_m=half_extent_m, airframe=airframe,
            home_lat=home_lat, home_lon=home_lon, ground_msl_m=ground_msl_m, query=query)

    @tool("sim_set_theater")
    async def _sim_set_theater(proposal_id: str, theater_id: str, label: str,
                               ao: list[list[float]], home_lat: float, home_lon: float,
                               ground_msl_m: float, airframe: str,
                               idempotency_key: str | None = None) -> dict:
        return await sim_set_theater(srv, {
            "proposal_id": proposal_id, "theater_id": theater_id, "label": label,
            "ao": ao, "home_lat": home_lat, "home_lon": home_lon,
            "ground_msl_m": ground_msl_m, "airframe": airframe}, idempotency_key)

    @tool("sim_set_time_scale")
    async def _sim_set_time_scale(scale: float, idempotency_key: str | None = None) -> dict:
        return await sim_set_time_scale(srv, scale, idempotency_key)


#: Tool descriptions. Each says SIMULATION; the theater ones name the MSL
#: datum (TOOL_CONTRACT: every height states its datum).
DESCRIPTIONS: Mapping[str, str] = {
    "geo_lookup": (
        "SIMULATION setup: look up a real place by name, or parse coordinates "
        "('12.97160, 77.59460'), to put the simulated theater there. Returns up to "
        "`limit` (1-10) candidates {id, name, label, lat, lon, bbox:[s,w,n,e], size_km, "
        "geocoder} with provenance and the OpenStreetMap attribution. Order: "
        "coordinates, the theater table, Photon, then Nominatim; answers are cached. "
        "No heights come back: theater_propose measures the ground in metres above mean "
        "sea level (MSL). With map data off only coordinates are accepted. Uncached "
        "lookups are rate-limited (30 per 10 minutes). A real place is context, never "
        "a target (M14). Changes nothing."),
    "geo_sites": (
        "Mapped strategic sites inside the running SIMULATION theater, from "
        "OpenStreetMap (ODbL): context only, mapped and not verified, never targets "
        "(M14). A missing site is not an absent one. Filter by `category` (airfield, "
        "military_base, port, power, fuel, comms, bridge, rail_hub, hq_gov, "
        "border_crossing, medical, dam, other), cap with `limit` (1-60), or sort by "
        "distance from near_lat/near_lon. Each row carries its chip id "
        "sit:{theater}:{osm}; cite it as [[sit:...|name]]. Positions are lat/lon only. "
        "`refresh=true` fetches the area again (rate-limited, 6 per 10 minutes); a "
        "failed or empty refresh keeps the previous sites. Changes nothing else."),
    "theater_propose": (
        "SIMULATION setup: propose moving the simulated theater (AO, home and "
        "geofence) to a real place. Changes nothing. Give a preset `theater_id`, or "
        "`lat`/`lon` (from geo_lookup, with its `place_id` and `bbox` to size the AO), "
        "plus optional `label` (<=60 characters), `place`, `half_extent_m`, `airframe` "
        "(quad_suas_electric; group3_fixed_wing for areas wider than about 6 km), an "
        "operator home `home_lat`/`home_lon` inside the AO, and `ground_msl_m`: the "
        "ground at home in metres above mean sea level (MSL), and the geo_lookup "
        "`query` the place came from (shown to the operator). The AO half-extent is "
        "clamped to 0.4 x the airframe's reach (1.5 to 25 km). Home is named open "
        "ground near the centre, else the AO centre. Ground is measured and stored as "
        "MSL, or refused as ground_unknown. Returns the proposal, its caveats and "
        "`set_args`: pass set_args unchanged to sim_set_theater. Proposals expire "
        "after 30 minutes."),
    "sim_set_theater": (
        "SIMULATION: move the simulated theater to a proposal from theater_propose. "
        "Pass its `set_args` exactly: proposal_id, theater_id, label, ao (3-12 "
        "[lat, lon] vertices), home_lat, home_lon, ground_msl_m (ground at home, metres "
        "MSL) and airframe. Anything that differs is refused (proposal_mismatch); an "
        "unknown or expired proposal is refused (proposal_expired). Every drone is "
        "parked, landed, at the new home; the geofence, home and fuel homes move with "
        "it; contacts, reports and the audit trail are kept; fuel is kept unless the "
        "airframe changes. Refused while any drone is airborne, busy, BINGO-latched or "
        "has lost its link, under real AirSim, and outside the app host. The operator "
        "approves every change. Returns the new theater, its home in MSL and HAE, and "
        "what was reset and kept."),
    "sim_set_time_scale": (
        "SIMULATION: run the fake simulator `scale` times faster (1 to 10): physics, "
        "fuel and the sun. Link-loss timers, detections, scans, camera captures and "
        "the safety checks stay on real time, so they happen less often per simulated "
        "second; the returned caveats say by how much. Refused under real AirSim. "
        "Returns {scale, previous, caveats}."),
}


def approval_preview(srv: Any, tool: str, args: Mapping[str, Any] | None) -> dict:
    """The preview the chat service attaches to an approval (§3.6):
    `theater_preview` for `sim_set_theater`, `time_scale_preview` for
    `sim_set_time_scale`, `{}` for any other tool. Reads caches only, never
    raises; any failure gives `{}` (the console then shows a Deny-only slip)."""
    try:
        a = dict(args or {})
        if tool == "sim_set_time_scale":
            return time_scale_preview(srv, a.get("scale"))
        if tool == "sim_set_theater":
            return _theater_preview(srv, a)
    except Exception as exc:  # noqa: BLE001 — never raises (§3.6)
        _LOG.warning("approval preview for %s failed: %s: %s", tool, type(exc).__name__, exc)
    return {}


def _theater_preview(srv: Any, args: dict) -> dict:
    """`theater_plan.preview` for the stored proposal, with the proposal check
    failed when the request no longer matches it (so the slip is blocked)."""
    book = getattr(srv, "theater_proposals", None)
    p = book.get(str(args.get("proposal_id") or "")) if book is not None else None
    if p is None:
        return {}
    plan = _plan()
    out = dict(plan.preview(srv, p) or {})
    if not out:
        return {}
    if "checks" not in out:
        out["checks"] = list(_switch().quick_checks(srv, p))
    mismatch = _mismatch(plan, p, args)
    if mismatch:
        checks = [dict(c) for c in out["checks"] if c.get("text") != PROPOSAL_CHECK]
        checks.append({"text": PROPOSAL_CHECK, "ok": False})
        out["checks"] = checks
        out["mismatch"] = mismatch
    return out


def theater_state(srv: Any) -> dict:
    """The §3.2 theater block for the running theater. Never raises.

    Keys: `{id, label, place, known}` plus `epoch, dynamic, source, state,
    bbox, center, half_extent_m, area_km2, home, ground_msl_m, ground_source,
    airframe, time_scale, geocoder, query, set_at_ms, set_via, previous,
    integrity_error}`. A field that cannot be read is null, never guessed.
    """
    t = getattr(srv, "theater", None)
    out: dict[str, Any] = dict.fromkeys(STATE_KEYS)
    if t is None:
        out.update(known=False, state=_switch_state(srv), epoch=_epoch(srv))
        return out
    plan = _safe(_plan)
    prov = _mapping(getattr(t, "provenance", None))
    center, ao, home_p, ground = (_mapping(prov.get(k)) for k in
                                  ("center", "ao", "home", "ground"))
    dynamic = bool(getattr(t, "dynamic", False))
    ground_key = str(ground.get("source") or ("operator" if dynamic else "preset"))
    geocoder_key = str(center.get("source") or ("operator" if dynamic else "preset"))
    out.update(
        id=_text(getattr(t, "id", None)), label=_text(getattr(t, "label", None)),
        place=_text(getattr(t, "place", None)) or None,
        known=_safe(lambda: theaters.is_known(t.id), False),
        epoch=_epoch(srv), dynamic=dynamic, source="chat" if dynamic else "preset",
        state=_switch_state(srv),
        bbox=_safe(lambda: [round(float(v), 7) for v in t.bbox()]),
        center=_safe(lambda: [round(float(v), 7) for v in t.center()]),
        half_extent_m=_safe(lambda: _half_extent_m(plan, t, ao)),
        area_km2=_safe(lambda: round(plan.ao_area_km2(t.ao_list()), 2)),
        home=_safe(lambda: {
            "lat": round(float(t.home_lat), 7), "lon": round(float(t.home_lon), 7),
            "alt_msl_m": round(float(t.home_alt_msl_m), 2),
            "name": _text(home_p.get("name")) or None,
            "source": _text(home_p.get("source")) or ("ao-centre" if dynamic else "preset")}),
        ground_msl_m=_safe(lambda: round(float(t.home_alt_msl_m), 2)),
        ground_source=_safe(lambda: plan.GROUND_SOURCE_TEXT.get(ground_key)),
        airframe=_safe(lambda: airframe_block(getattr(srv, "airframe_id", None))),
        time_scale=_safe(lambda: float(getattr(srv, "time_scale", 1.0)), 1.0),
        geocoder=_safe(lambda: plan.GEOCODER_TEXT.get(geocoder_key)),
        query=_text(center.get("query")) or None,
        set_at_ms=getattr(srv, "theater_set_at_ms", None),
        set_via=getattr(srv, "theater_set_via", None),
        previous=_previous(getattr(srv, "theater_previous", None)),
        integrity_error=getattr(srv, "theater_integrity_error", None))
    return out


def time_scale_caveats(srv: Any, scale: float) -> list[str]:
    """The §4.1.6 caveat lines for running at `scale`, in order.

    Line 4 is `round(max(0.05, DEFAULT_TICK_S / scale) * scale, 2)` sim
    seconds; line 5 appears only above `capture_gap_scale()`.
    """
    s = float(scale)
    lines = list(FIXED_TIME_SCALE_CAVEATS)
    lines.append(f"Safety checks run every {safety_period_sim_s(s):g} sim-seconds.")
    k = capture_gap_scale()
    if s > k:
        lines.append("Camera captures are rate-limited in real time; coverage gaps "
                     f"are likely above ×{k}.")
    return lines


def time_scale_preview(srv: Any, scale: Any) -> dict:
    """`{from, to, checks:[{text, ok}], caveats}` (§3.6). An out-of-range or
    non-numeric `scale` adds a failed range check (the slip is then blocked)."""
    to = _scale_or_none(scale)
    checks = [{"text": FAKE_SIM_CHECK,
               "ok": getattr(getattr(srv, "backend", None), "sim", None) is not None}]
    if to is None:
        checks.append({"text": SCALE_RANGE_CHECK, "ok": False})
    return {"from": float(getattr(srv, "time_scale", 1.0)),
            "to": to if to is not None else _plain_number(scale),
            "checks": checks,
            "caveats": time_scale_caveats(srv, to) if to is not None else []}


def capture_gap_scale() -> int:
    """`k` of caveat 5: above this speed recon captures leave gaps (§4.1.6).

    `max(1, floor(rate_hz * along_m * (1 - forward_overlap) / speed_mps))` at
    the `mission_recon_route` defaults and the default camera (k = 3).
    """
    from . import missions

    cam = missions.camera(None)
    _, along_m = missions.footprint_m(RECON_ALT_AGL_M, cam.hfov_deg, cam.vfov_deg)
    return max(1, math.floor(RECON_CAPTURE_RATE_HZ * along_m
                             * (1.0 - RECON_FORWARD_OVERLAP) / RECON_SPEED_MPS))


def safety_period_sim_s(scale: float) -> float:
    """Sim seconds between two safety passes at `scale` (§4.1.6 caveat 4)."""
    s = float(scale)
    return round(max(MONITOR_MIN_SLEEP_S, DEFAULT_TICK_S / s) * s, 2)


def airframe_block(airframe_id: Any) -> dict:
    """`{id, label, reach_m}` for an airframe id (None = the default)."""
    af = safety.get_airframe(airframe_id)
    return {"id": af.id, "label": af.label, "reach_m": round(safety.reach_radius_m(af))}


# ------------------------------------------------------------- internals --

#: §3.2 theater block keys, in order.
STATE_KEYS: tuple[str, ...] = (
    "id", "label", "place", "known", "epoch", "dynamic", "source", "state", "bbox",
    "center", "half_extent_m", "area_km2", "home", "ground_msl_m", "ground_source",
    "airframe", "time_scale", "geocoder", "query", "set_at_ms", "set_via", "previous",
    "integrity_error")


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a state read never raises (R22)
        return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int | None = 200) -> str:
    """Untrusted text as one clean line (§3.11); '' for None."""
    return "" if value is None else theaters.clean_text(value, limit=limit)


def _epoch(srv: Any) -> int:
    return _safe(lambda: int(getattr(srv, "theater_epoch", 0)), 0)


def _switch_state(srv: Any) -> str:
    flag = getattr(srv, "_switching", None)
    return "switching" if flag is not None and _safe(flag.is_set, False) else "active"


def _previous(prev: Any) -> dict | None:
    if not isinstance(prev, Mapping):
        return None
    return {"id": _text(prev.get("id")) or None, "label": _text(prev.get("label")) or None}


def _half_extent_m(plan: Any, t: Any, ao_prov: Mapping[str, Any]) -> float:
    """The provenance half-extent of a chat theater, else min(half-width,
    half-height) of the AO bbox in metres (presets, §3.2)."""
    half = _plain_number(ao_prov.get("half_extent_m"))
    if half is not None:
        return round(half, 1)
    return round(plan.bbox_half_extent_m(t.bbox()), 1)


def _plain_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _scale_or_none(value: Any) -> float | None:
    """A sim speed in [1, 10], or None."""
    s = _plain_number(value)
    return s if s is not None and SCALE_MIN <= s <= SCALE_MAX else None


def _plan() -> Any:
    """`theater_plan` (A4), imported on use; tests replace this seam."""
    from . import theater_plan

    return theater_plan


def _switch() -> Any:
    """`theater_switch` (A4), imported on use; tests replace this seam."""
    from . import theater_switch

    return theater_switch


def refusal(code: str, message: str, **extra: Any) -> dict:
    """`{rejected: true, error, message}` (§3.1); the UI shows `message` as text."""
    return {"rejected": True, "error": code, "message": message, **extra}


def _budget(srv: Any, tool: str) -> Any:
    """This server's `CallBudget` for `tool` (created by `register`)."""
    budgets = getattr(srv, "theater_budgets", None)
    if budgets is None:
        budgets = srv.theater_budgets = new_budgets()
    return budgets[tool]


def new_budgets() -> dict[str, geo_http.CallBudget]:
    """Fresh per-server budgets (§3.7)."""
    return {tool: geo_http.CallBudget(calls, window_s)
            for tool, (calls, window_s) in BUDGETS.items()}


def _rate_limited(tool: str, what: str, **extra: Any) -> dict:
    calls, window_s = BUDGETS[tool]
    return refusal("rate_limited", RATE_LIMITED_MESSAGE.format(
        what=what, minutes=int(window_s // 60)), budget={"calls": calls, "window_s": window_s},
        **extra)


def _mismatch(plan: Any, p: Any, args: Mapping[str, Any]) -> list[str]:
    """`args_match` as a list of field names (a raising compare is a mismatch)."""
    try:
        return [str(f) for f in (plan.args_match(p, dict(args)) or [])]
    except Exception as exc:  # noqa: BLE001 — a malformed request never matches
        return [f"unreadable request ({type(exc).__name__})"]


def _geo_fetch(srv: Any) -> Any:
    """The injected geodata client (tests), or None for the real one."""
    return getattr(srv, "geo_fetch", None)


def _geodata_on(srv: Any) -> bool:
    return bool(getattr(srv, "geodata_enabled", False))


# ------------------------------------------------------------ tool bodies --

async def geo_lookup(srv: Any, query: Any, limit: Any = 5) -> dict:
    """`geo_lookup` (§3.7): `{candidates:[Place.as_dict], provenance}`."""
    q = query if isinstance(query, str) else ""
    if not QUERY_MIN <= len(q.strip()) <= QUERY_MAX:
        return refusal("invalid_parameter",
                       f"query must be {QUERY_MIN} to {QUERY_MAX} characters.")
    lim = _int_in(limit, LOOKUP_LIMIT_MIN, LOOKUP_LIMIT_MAX)
    if lim is None:
        return refusal("invalid_parameter",
                       f"limit must be a whole number from {LOOKUP_LIMIT_MIN} to "
                       f"{LOOKUP_LIMIT_MAX}.")
    out = await asyncio.to_thread(
        geocode.lookup, q, limit=lim, cache=getattr(srv, "geo_cache", None),
        fetch=_geo_fetch(srv), enabled=_geodata_on(srv), budget=_budget(srv, "geo_lookup"))
    prov = _mapping(out.get("provenance"))
    if prov.get("rate_limited"):
        return _rate_limited("geo_lookup", "new place lookups",
                             candidates=[], provenance=dict(prov))
    return out


async def geo_sites(srv: Any, category: Any = None, limit: Any = SITES_LIMIT_DEFAULT,
                    refresh: Any = False, near_lat: Any = None,
                    near_lon: Any = None) -> dict:
    """`geo_sites` (§3.7): `SiteSet.as_dict(...)` of the running theater."""
    if category is not None and category not in _sites.ALL_CATEGORIES:
        return refusal("invalid_parameter",
                       f"category must be one of: {', '.join(_sites.ALL_CATEGORIES)}.")
    lim = _int_in(limit, 1, SITES_LIMIT_MAX)
    if lim is None:
        return refusal("invalid_parameter",
                       f"limit must be a whole number from 1 to {SITES_LIMIT_MAX}.")
    near = _near(near_lat, near_lon)
    if near is False:
        return refusal("invalid_parameter",
                       "near_lat and near_lon go together and must be a valid position.")
    refreshed: dict[str, Any] | None = None
    if refresh is True:
        refreshed = await _refresh_sites(srv)
        if refreshed.get("rejected"):
            return refreshed
    t = srv.theater
    body = srv.sites.as_dict(category=category, limit=lim,
                             near_lat=near[0] if near else None,
                             near_lon=near[1] if near else None, theater_id=t.id)
    body.update(theater={"id": t.id, "epoch": _epoch(srv)}, simulated_world=True)
    if refreshed is not None:
        body["refresh"] = refreshed
    elif body.get("degraded") and _geodata_on(srv):
        body["hint"] = "Call geo_sites with refresh=true to fetch this area's sites."
    return body


async def _refresh_sites(srv: Any) -> dict:
    """Fetch the running theater's sites again in a worker thread. A degraded
    or empty answer never replaces a non-degraded set of the same epoch."""
    if not _geodata_on(srv):
        return {"done": False, "reason": _sites.GEODATA_OFF_REASON}
    if not _budget(srv, "geo_sites").take():
        return _rate_limited("geo_sites", "site refreshes")
    epoch, bbox = _epoch(srv), srv.theater.bbox()
    fresh = await asyncio.to_thread(
        _sites.fetch_sites, bbox, cache=getattr(srv, "geo_cache", None),
        fetch=_geo_fetch(srv), refresh=True)
    # A switch sets `_switching` under `_mode_lock` before it touches
    # `srv.sites`, so under the lock "not switching, same epoch" holds until
    # the assignment is done.
    with getattr(srv, "_mode_lock", None) or contextlib.nullcontext():
        if _epoch(srv) != epoch or _switch_state(srv) == "switching":
            return {"done": False, "reason": "the theater changed during the refresh; "
                                             "the answer was dropped"}
        current = srv.sites
        if current.degraded or (not fresh.degraded and fresh.total > 0):
            srv.sites = fresh
    return {"done": True, "replaced": srv.sites is fresh, "real": fresh.real,
            "total": fresh.total, "reason": fresh.reason}


async def theater_propose(srv: Any, **kw: Any) -> dict:
    """`theater_propose` (§3.7, §4.1.2): `theater_plan.propose` in a worker
    thread under `wait_for(..., 60)`. It changes nothing."""
    args = {k: v for k, v in kw.items() if v is not None}
    label = args.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > LABEL_MAX):
        return refusal("invalid_parameter", f"label must be text of at most {LABEL_MAX} "
                                            "characters.")
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > QUERY_MAX):
        return refusal("invalid_parameter", f"query must be text of at most {QUERY_MAX} "
                                            "characters.")
    bbox = args.get("bbox")
    if bbox is not None and (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                             or any(_plain_number(v) is None for v in bbox)):
        return refusal("invalid_parameter", "bbox must be four numbers [s, w, n, e].")
    if _geodata_on(srv) and not _budget(srv, "theater_propose").take():
        return _rate_limited("theater_propose", "theater proposals")
    fn = functools.partial(_plan().propose, srv, args, fetch=_geo_fetch(srv))
    try:
        out = await asyncio.wait_for(asyncio.to_thread(fn), PROPOSE_TIMEOUT_S)
    except TimeoutError:
        return refusal("proposal_timeout", "Building the proposal took longer than "
                                           f"{PROPOSE_TIMEOUT_S:g} s; try again.")
    except (TypeError, ValueError) as exc:
        return refusal("theater_invalid", _text(f"{exc}", 400) or "The proposal is invalid.")
    return out


async def sim_set_theater(srv: Any, args: Mapping[str, Any],
                          idempotency_key: str | None = None) -> dict:
    """`sim_set_theater` (§3.7, §4.1.3 handler). Runs on the calling loop; the
    switch itself runs on the tasking loop."""
    replay = srv._idem_replay("sim_set_theater", idempotency_key)
    if replay is not None:
        return replay
    via = CALL_VIA.get()                        # before the hop to another loop
    a = {k: args.get(k) for k in SET_ARG_KEYS}
    problem = _set_args_problem(a)
    if problem:
        return refusal("invalid_parameter", problem)
    plan = _plan()
    book = getattr(srv, "theater_proposals", None)
    p = book.get(a["proposal_id"]) if book is not None else None
    if p is None:
        return refusal("proposal_expired",
                       f"Proposal {_text(a['proposal_id'], 40)} is unknown or has expired; "
                       "call theater_propose again.")
    bad = _mismatch(plan, p, a)
    if bad:
        return refusal("proposal_mismatch",
                       "These arguments differ from the proposal: " + ", ".join(bad)
                       + ". Pass theater_propose's set_args unchanged.", fields=bad)
    # The whole row, not just the id: a dynamic id hashes only the label and
    # the AO, so a corrected home or ground over the same area keeps its id
    # (review A). `Theater.__eq__` is the geometry (provenance excluded).
    if p.theater == srv.theater and a["airframe"] == srv.airframe_id:
        return srv._idem_record("sim_set_theater", idempotency_key, _unchanged(srv))
    sw = _switch()
    future = asyncio.run_coroutine_threadsafe(sw.switch(srv, p, via), srv.tasking.loop)
    try:
        # Shielded: Stop or a closed chat cancels this call, and that must
        # never reach the switch half-way through moving the origin
        # (review A, critical). The switch runs on to its end regardless.
        out = await asyncio.shield(asyncio.wrap_future(future))
    except sw.SwitchRefused as exc:             # `switch` returns refusals; a raised one too
        return exc.as_result()
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        _LOG.exception("theater switch failed")
        return refusal("theater_switch_failed",
                       f"The theater switch failed: {type(exc).__name__}: {_text(exc, 300)}")
    if isinstance(out, Mapping) and out.get("rejected"):
        return dict(out)
    return srv._idem_record("sim_set_theater", idempotency_key, dict(out))


async def sim_set_time_scale(srv: Any, scale: Any,
                             idempotency_key: str | None = None) -> dict:
    """`sim_set_time_scale` (§3.7): `{scale, previous, caveats}`."""
    replay = srv._idem_replay("sim_set_time_scale", idempotency_key)
    if replay is not None:
        return replay
    s = _scale_or_none(scale)
    if s is None:
        return refusal("invalid_parameter",
                       f"scale must be a number from {SCALE_MIN:g} to {SCALE_MAX:g}.")
    caveats = time_scale_caveats(srv, s)
    try:
        changed = srv.set_time_scale(s)
    except ValueError as exc:
        return refusal("time_scale_refused", _text(exc, 300) or "Sim speed can't be set.")
    out = {"ok": True, "status": "accepted", "scale": changed["scale"],
           "previous": changed["previous"], "caveats": caveats, "simulated_world": True}
    return srv._idem_record("sim_set_time_scale", idempotency_key, out)


def _unchanged(srv: Any) -> dict:
    t = srv.theater
    return {"ok": True, "status": "unchanged",
            "theater": {"id": t.id, "label": _text(t.label), "epoch": _epoch(srv),
                        "dynamic": bool(getattr(t, "dynamic", False))},
            "airframe": {"id": srv.airframe_id, "changed": False},
            "message": "The simulation is already in this theater with this airframe; "
                       "nothing changed."}


#: Exactly what `sim_set_theater` takes (`theater_plan.SET_ARG_KEYS`).
SET_ARG_KEYS: tuple[str, ...] = ("proposal_id", "theater_id", "label", "ao", "home_lat",
                                 "home_lon", "ground_msl_m", "airframe")


def _set_args_problem(a: Mapping[str, Any]) -> str | None:
    """A one-sentence reason `sim_set_theater`'s args are malformed, or None."""
    for key in ("proposal_id", "theater_id", "label", "airframe"):
        if not isinstance(a.get(key), str) or not a[key]:
            return f"{key} must be the text theater_propose returned in set_args."
    ao = a.get("ao")
    if (not isinstance(ao, (list, tuple))
            or not AO_VERTICES_MIN <= len(ao) <= AO_VERTICES_MAX
            or any(not isinstance(v, (list, tuple)) or len(v) != 2
                   or _plain_number(v[0]) is None or _plain_number(v[1]) is None
                   for v in ao)):
        return (f"ao must be {AO_VERTICES_MIN} to {AO_VERTICES_MAX} [lat, lon] pairs, "
                "exactly as theater_propose returned them.")
    for key in ("home_lat", "home_lon", "ground_msl_m"):
        if _plain_number(a.get(key)) is None:
            return f"{key} must be a number."
    return None


def _int_in(value: Any, lo: int, hi: int) -> int | None:
    """A whole number in [lo, hi] (bools and fractions refused), or None."""
    n = _plain_number(value)
    if n is None or n != int(n) or not lo <= n <= hi:
        return None
    return int(n)


def _near(lat: Any, lon: Any) -> tuple[float, float] | None | bool:
    """`(lat, lon)`, None when neither is given, False when malformed."""
    if lat is None and lon is None:
        return None
    la, lo = _plain_number(lat), _plain_number(lon)
    if la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return False
    return (la, lo)
