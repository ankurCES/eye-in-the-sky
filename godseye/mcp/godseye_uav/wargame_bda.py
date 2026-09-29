"""Battle-damage assessment for simulated blue strikes (M14a, PLAN.md §4.5a; WG v2 §5.2.9).

Drones never deliver effects; they only LOOK again. A look is one observation,
taken after the strike fired, on a track that passes the provenance gate for
the struck scenario unit (the engine filters the tracks before calling here, so
no mapped site, real traffic or other sim object can ever count as a look).

`state = bda_state(unit.state, unit.damaged, looks)` (R8): `none` with no look,
`destroyed_probable` after one look at a destroyed unit and
`destroyed_confirmed` after two, else `damaged` or `no_change`. In blue view a
blue strike's outcome stays hidden until the first look (§3.2 fog).

Pure: no clock, no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .wargame_adjudicate import bda_state

#: Human words for a BDA tier (event text, AAR). Sentence case (§0.2).
BDA_WORDS: dict[str, str] = {
    "none": "not assessed yet",
    "no_change": "no change seen",
    "damaged": "damaged",
    "destroyed_probable": "destroyed (probable)",
    "destroyed_confirmed": "destroyed (confirmed)",
}
#: The empty assessment a fresh blue strike starts with.
NO_BDA: dict[str, Any] = {"state": "none", "looks": 0, "last_look_ms": None}


def looks_since(tracks: Iterable[Any], fired_at_ms: float) -> tuple[int, int | None]:
    """`(looks, last_look_ms)`: observations with `ts > fired_at_ms / 1000` on `tracks`."""
    cutoff = float(fired_at_ms) / 1000.0
    looks, last = 0, None
    for t in tracks:
        for o in list(getattr(t, "observations", None) or ()):
            raw = o.get("ts") if isinstance(o, Mapping) else getattr(o, "ts", None)
            try:
                ts = float(raw)
            except (TypeError, ValueError):
                continue
            if ts > cutoff:
                looks += 1
                last = ts if last is None else max(last, ts)
    return looks, (None if last is None else round(last * 1000.0))


def assess(truth_state: str, damaged: bool, tracks: Iterable[Any],
           fired_at_ms: float) -> dict[str, Any]:
    """`{state, looks, last_look_ms}` for one fired blue strike."""
    looks, last = looks_since(tracks, fired_at_ms)
    return {"state": bda_state(truth_state, bool(damaged), looks), "looks": looks,
            "last_look_ms": last}


def tier_changed(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    """True when the BDA tier moved (the engine emits `bda_assessed` then)."""
    return (old or NO_BDA).get("state") != new.get("state")


def outcome_visible(bda: Mapping[str, Any] | None) -> bool:
    """A blue strike's outcome shows in blue view once it has been looked at."""
    return bool(bda) and int(bda.get("looks") or 0) >= 1


def bda_text(target_label: str, bda: Mapping[str, Any]) -> str:
    """Event text, e.g. 'Battle damage assessment on Air-defence contact: damaged (2 looks).'"""
    n = int(bda.get("looks") or 0)
    word = BDA_WORDS.get(str(bda.get("state")), str(bda.get("state")))
    return (f"Battle damage assessment on {target_label}: {word} "
            f"({n} look{'s' if n != 1 else ''}).")


def agrees(bda_state_: str, truth_state: str, damaged: bool) -> bool | None:
    """Did the assessment match the umpire's truth? None before any look."""
    if bda_state_ == "none":
        return None
    if truth_state == "destroyed":
        return bda_state_ in ("destroyed_probable", "destroyed_confirmed")
    if damaged:
        return bda_state_ == "damaged"
    return bda_state_ == "no_change"


def accuracy_row(engagement_id: str, target_label: str, bda: Mapping[str, Any] | None,
                 outcome: str | None, truth_state: str, damaged: bool) -> dict[str, Any]:
    """One AAR 'battle damage accuracy' row: the assessment against the umpire."""
    b = dict(bda or NO_BDA)
    return {"engagement_id": engagement_id, "target_label": target_label,
            "bda_state": b.get("state"), "looks": int(b.get("looks") or 0),
            "umpire_outcome": outcome, "truth_state": truth_state,
            "agrees": agrees(str(b.get("state")), truth_state, damaged), "simulated": True}
