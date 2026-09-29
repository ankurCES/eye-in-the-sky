"""After-action review and crash persistence for the simulated wargame (M14a, WG v2 §5.2.9).

The AAR is filed as `uav://reports/aar-<session id>` (`srv.reports["aar-…"]`)
and NEVER as `latest` (V21): the ISR INTREP keeps that slot. It holds only
designators, vehicle names, the theater id and AO-relative offsets (§3.1), and
every row is stamped simulated (D1).

Crash persistence is `<store>/wargame.json`, written atomically when a session
starts, on every spawn and on every new scenario track:

    {schema, session_id, num, started_at_ms, seed, theater_id, object_names, track_ids}

`WargameEngine.recover_on_boot` reads it after a restart, deletes the session's
tracks, and files a partial AAR (`incomplete: true`) whose timeline comes from
the `wargame_event` audit rows (at most the last `AUDIT_SCAN_ROWS`).

Pure apart from the three small file helpers; no clock except `zulu` formatting.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .wargame_tables import NOTIONAL_NOTE, TABLE_VERSION

SESSION_FILE = "wargame.json"
SESSION_SCHEMA = "godseye.wargame-session/v1"
REPORT_TYPE = "AAR"
AAR_TITLE = "After-action review (simulated)"
MARKDOWN_MAX_BYTES = 40_000
AUDIT_SCAN_ROWS = 5000
EVENT_AUDIT_KIND = "wargame_event"
FIXED_LINE = "Simulated wargame. Nothing real was fired; outcomes are notional adjudications."
INCOMPLETE_LINE = ("This review is incomplete: a restart interrupted the session, so its "
                   "timeline comes from the audit log.")
SIDES = ("blue", "red")
STATES = ("active", "suppressed", "damaged", "destroyed")
ENGAGEMENT_KINDS = ("blue_strike", "red_shot", "red_ground")
_EVENT_KEYS = ("t_ms", "sim_s", "kind", "side", "text", "outcome", "register", "simulated")
_FILE_LOCK = threading.Lock()
#: Bidi controls stripped from every cell (§0.2 untrusted text).
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")


def aar_id(session_id: str) -> str:
    return f"aar-{session_id}"


def resource_for(report_id: str) -> str:
    return f"uav://reports/{report_id}"


# ------------------------------------------------------------ persistence --

def session_path(store: Any) -> Path | None:
    """`<store>/wargame.json`, or None for an in-memory store."""
    root = getattr(store, "root", None)
    return None if root is None else Path(root) / SESSION_FILE


def session_record(*, session_id: str, num: int, started_at_ms: int, seed: Any,
                   theater_id: str, object_names: Iterable[str],
                   track_ids: Iterable[str]) -> dict[str, Any]:
    return {"schema": SESSION_SCHEMA, "session_id": session_id, "num": int(num),
            "started_at_ms": int(started_at_ms), "seed": seed, "theater_id": theater_id,
            "object_names": list(object_names), "track_ids": list(track_ids)}


def write_session(path: Path | None, record: Mapping[str, Any]) -> bool:
    """Write `record` atomically (tmp + `os.replace`). False for no path."""
    if path is None:
        return False
    with _FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(dict(record), sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    return True


def read_session(path: Path | None) -> dict[str, Any] | None:
    """The persisted record, None when absent, or `{"corrupt": reason}`."""
    if path is None or not path.exists():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"corrupt": f"{type(exc).__name__}: {exc}"}
    if not isinstance(rec, dict):
        return {"corrupt": "not a JSON object"}
    return rec


def remove_session(path: Path | None) -> bool:
    """Delete the file; True when one was removed."""
    if path is None:
        return False
    with _FILE_LOCK:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


# ------------------------------------------------------------- formatting --

def zulu(ms: float | None, *, date: bool = True) -> str:
    """'2026-09-28 14:03:07Z' (or '14:03:07Z'); '–' for no time."""
    if ms is None:
        return "–"
    dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%SZ" if date else "%H:%M:%SZ")


def _cell(value: Any) -> str:
    """One markdown table cell: no pipes, no line breaks, no bidi controls."""
    text = "–" if value is None or value == "" else str(value)
    text = _BIDI.sub("", text)
    return " ".join(text.replace("|", "\\|").split())


def public_event(e: Mapping[str, Any]) -> dict[str, Any]:
    """An event with only its public keys (`_*` engine fields dropped)."""
    out = {k: e.get(k) for k in _EVENT_KEYS}
    out["register"] = out.get("register") or "scenario"
    out["simulated"] = True
    return out


# ------------------------------------------------------------------- AAR ---

def count_units(units: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """`{blue|red: {units, active, suppressed, damaged, destroyed}}` from unit rows."""
    out = {s: {"units": 0, **{st: 0 for st in STATES}} for s in SIDES}
    for u in units:
        side = u.get("side")
        if side not in out:
            continue
        out[side]["units"] += 1
        if u.get("state") in STATES:
            out[side][u["state"]] += 1
    return out


def count_events(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Adjudicated engagements per kind, plus drones and packages lost, from events."""
    out = {"blue_strike": 0, "red_shot": 0, "red_ground": 0, "drones_lost": 0,
           "packages_lost": 0, "red_destroyed": 0, "blue_destroyed": 0}
    for e in events:
        kind, outcome = e.get("kind"), e.get("outcome")
        if kind == "blue_strike_executed":
            out["blue_strike"] += 1
            out["red_destroyed"] += outcome == "destroyed"
        elif kind == "red_shot":
            out["red_shot"] += 1
            out["drones_lost"] += outcome == "destroyed"
        elif kind == "red_ground":
            out["red_ground"] += 1
            out["blue_destroyed"] += outcome == "destroyed"
        elif kind == "package_lost":
            out["packages_lost"] += 1
            out["blue_destroyed"] += 1
    return out


def loss_exchange(*, red_destroyed: int, blue_destroyed: int, drones_lost: int) -> dict:
    """Red units destroyed against blue units and drones lost (notional)."""
    blue_losses = int(blue_destroyed) + int(drones_lost)
    return {"red_destroyed": int(red_destroyed), "blue_destroyed": int(blue_destroyed),
            "drones_lost": int(drones_lost), "blue_losses": blue_losses,
            "ratio": round(red_destroyed / blue_losses, 2) if blue_losses else None,
            "simulated": True}


def build_aar(*, session_id: str, theater_id: str, started_at_ms: int, ended_at_ms: int,
              seed: Any, time_scale: float | None, red_engages: bool | None,
              reveal_red: bool | None,
              units: Sequence[Mapping[str, Any]] | None, events: Sequence[Mapping[str, Any]],
              sorties: Sequence[Mapping[str, Any]], bda_accuracy: Sequence[Mapping[str, Any]],
              track_ids: Sequence[str], incomplete: bool = False,
              reason: str | None = None) -> dict[str, Any]:
    """The AAR report (§5.2.9). `units` None means unknown (a partial AAR)."""
    rid = aar_id(session_id)
    timeline = [public_event(e) for e in events]
    ev = count_events(timeline)
    counts: dict[str, Any] = {"blue": None, "red": None} if units is None else count_units(units)
    counts["engagements"] = {k: ev[k] for k in ENGAGEMENT_KINDS}
    counts["drones_lost"] = ev["drones_lost"]
    counts["packages_lost"] = ev["packages_lost"]
    counts["events"] = len(timeline)
    if units is None:
        red_d, blue_d = ev["red_destroyed"], ev["blue_destroyed"]
    else:
        red_d, blue_d = counts["red"]["destroyed"], counts["blue"]["destroyed"]
    aar = {"report_type": REPORT_TYPE, "id": rid, "report_id": rid, "resource": resource_for(rid),
           "title": AAR_TITLE, "session_id": session_id, "theater_id": theater_id,
           "started_at_ms": started_at_ms, "ended_at_ms": ended_at_ms, "seed": seed,
           "engine": TABLE_VERSION, "table_version": TABLE_VERSION, "time_scale": time_scale,
           "red_engages": _flag(red_engages), "reveal_red": _flag(reveal_red),
           "incomplete": bool(incomplete), "reason": reason, "counts": counts,
           "loss_exchange": loss_exchange(red_destroyed=red_d, blue_destroyed=blue_d,
                                          drones_lost=ev["drones_lost"]),
           "timeline": timeline, "sorties": [dict(s) for s in sorties],
           "bda_accuracy": [dict(b) for b in bda_accuracy], "track_ids": list(track_ids),
           "simulated": True, "note": NOTIONAL_NOTE}
    aar["markdown"] = render_markdown(aar)
    return aar


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _yes_no(value: Any) -> str:
    return "not known" if value is None else ("yes" if value else "no")


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{int(count)} {one if int(count) == 1 else (many or one + 's')}"


def _count_line(label: str, c: Mapping[str, int] | None) -> str:
    if not c:
        return f"- {label}: not known (the session was interrupted)"
    return (f"- {label}: {c['units']} ({c['active']} active, {c['suppressed']} suppressed, "
            f"{c['damaged']} damaged, {c['destroyed']} destroyed)")


def _header(aar: Mapping[str, Any]) -> list[str]:
    c, lx = aar["counts"], aar["loss_exchange"]
    e = c["engagements"]
    lines = [f"# {AAR_TITLE}", "", FIXED_LINE + " " + NOTIONAL_NOTE, ""]
    if aar.get("incomplete"):
        lines += [INCOMPLETE_LINE, ""]
    lines += ["## Summary", "",
              f"- Session: {_cell(aar['session_id'])}, theater `{_cell(aar['theater_id'])}`",
              f"- Started {zulu(aar['started_at_ms'])}, ended {zulu(aar['ended_at_ms'])}",
              (f"- Seed {_cell(aar['seed'])}; engine {TABLE_VERSION}; sim speed "
               + (f"x{float(aar['time_scale']):g}" if aar.get("time_scale") else "not known")),
              (f"- Red engages: {_yes_no(aar['red_engages'])}; "
               f"red revealed: {_yes_no(aar['reveal_red'])}"),
              _count_line("Blue units", c.get("blue")), _count_line("Red units", c.get("red")),
              (f"- Engagements: {_n(e['blue_strike'], 'blue strike')}, "
               f"{_n(e['red_shot'], 'red shot')} at drones, "
               f"{_n(e['red_ground'], 'red ground fire', 'red ground fires')}"),
              (f"- Loss exchange: {lx['red_destroyed']} red destroyed for "
               f"{_n(lx['blue_losses'], 'blue loss', 'blue losses')} "
               f"({_n(lx['blue_destroyed'], 'unit')}, {_n(lx['drones_lost'], 'drone')})"), ""]
    return lines


def _tail(aar: Mapping[str, Any]) -> list[str]:
    lines = ["## Sorties", "", "| Drone | Missions flown | Lost |", "|---|---|---|"]
    for s in aar["sorties"] or [{"vehicle": None, "missions_flown": 0, "lost": False}]:
        lost = (f"yes ({_cell(s.get('lost_by'))})" if s.get("lost_by") else "yes") \
            if s.get("lost") else "no"
        lines.append(f"| {_cell(s.get('vehicle'))} | {int(s.get('missions_flown') or 0)} "
                     f"| {lost} |")
    lines += ["", "## Battle damage accuracy", "",
              "| Engagement | Target | Assessment | Umpire outcome | Agrees |",
              "|---|---|---|---|---|"]
    for b in aar["bda_accuracy"]:
        agree = {True: "yes", False: "no", None: "not assessed"}[b.get("agrees")]
        lines.append(f"| {_cell(b.get('engagement_id'))} | {_cell(b.get('target_label'))} | "
                     f"{_cell(b.get('bda_state'))} | {_cell(b.get('umpire_outcome'))} | "
                     f"{agree} |")
    if not aar["bda_accuracy"]:
        lines.append("| – | – | – | – | – |")
    lines += ["", "## Replay", "",
              (f"- Seed {_cell(aar['seed'])}, engine {TABLE_VERSION}, table version "
               f"{TABLE_VERSION}, {_n(len(aar['timeline']), 'event')}"), ""]
    return lines


def _timeline_rows(timeline: Sequence[Mapping[str, Any]]) -> list[str]:
    return [f"| {zulu(e.get('t_ms'), date=False)} | {_cell(e.get('text'))} | "
            f"{_cell(e.get('side'))} | {_cell(e.get('outcome'))} | Scenario |"
            for e in timeline]


def render_markdown(aar: Mapping[str, Any]) -> str:
    """The AAR as markdown, at most `MARKDOWN_MAX_BYTES`: the oldest timeline rows
    are dropped (and counted) when it would not fit."""
    head, tail = _header(aar), _tail(aar)
    rows = _timeline_rows(aar["timeline"])
    table = ["## Timeline", "", "| Time (Z) | Event | Side | Outcome | Register |",
             "|---|---|---|---|---|"]
    dropped = 0
    while True:
        note = ([(f"{dropped} earlier events are omitted here; the report's timeline "
                  "holds them all."), ""] if dropped else [])
        body = rows[dropped:] or ["| – | No events | – | – | Scenario |"]
        text = "\n".join(head + table + body + [""] + note + tail)
        if len(text.encode("utf-8")) <= MARKDOWN_MAX_BYTES or dropped >= len(rows):
            return text[:MARKDOWN_MAX_BYTES] if len(text.encode("utf-8")) > \
                MARKDOWN_MAX_BYTES else text
        dropped += max(1, (len(rows) - dropped) // 10)


# ------------------------------------------------------ crash recovery -----

def events_from_audit(rows: Iterable[Mapping[str, Any]], session_id: str, *,
                      limit: int = AUDIT_SCAN_ROWS) -> list[dict[str, Any]]:
    """The session's events from `wargame_event` audit rows, scanning at most the
    last `limit` rows, oldest first."""
    tail = list(rows)[-int(limit):] if limit else list(rows)
    out: list[dict[str, Any]] = []
    for row in tail:
        if row.get("kind") != EVENT_AUDIT_KIND or row.get("session_id") != session_id:
            continue
        ev = row.get("event")
        if isinstance(ev, Mapping):
            out.append(public_event(ev))
        else:
            out.append(public_event({"t_ms": int(float(row.get("ts") or 0) * 1000),
                                     "kind": row.get("event_kind") or "event",
                                     "text": row.get("message"), "side": row.get("side")}))
    return out


def partial_aar(record: Mapping[str, Any], audit_rows: Iterable[Mapping[str, Any]], *,
                ended_at_ms: int) -> dict[str, Any]:
    """The AAR of a session a restart interrupted (`incomplete: true`)."""
    sid = str(record.get("session_id") or "unknown")
    events = events_from_audit(audit_rows, sid)
    return build_aar(
        session_id=sid, theater_id=str(record.get("theater_id") or "unknown"),
        started_at_ms=int(record.get("started_at_ms") or 0), ended_at_ms=int(ended_at_ms),
        seed=record.get("seed"), time_scale=None, red_engages=None, reveal_red=None,
        units=None, events=events, sorties=[], bda_accuracy=[],
        track_ids=[str(t) for t in record.get("track_ids") or ()], incomplete=True,
        reason="restart")
