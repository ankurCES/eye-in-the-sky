"""Threat assessment + multi-UAV coordination (PLAN §4.6, Phase 5).

Deterministic, order-of-battle-driven assessment (M13) — no LLM scores. The
pipeline is exactly the plan's four stages:

  (a) match the track against the order-of-battle library (targets.OB_LIBRARY:
      type -> capabilities, weapon ranges, mobility);
  (b) evaluate FOUR intent indicators — posture, movement toward a defended
      asset, pattern-of-life deviation (M12 store) and emissions;
  (c) confidence = confirmed/probable/possible with cited evidence per element
      (targets.assess_confidence);
  (d) score = capability x intent, every component traceable: each sub-score is
      returned as a number with the evidence list that produced it, so a human
      can see which observation drove which term.

ISR-ONLY (M14). This module reports; command authority stays with the
operator. The only advisory it emits is `sensor_posture` — where to put the
SENSOR and how to keep the aircraft alive (standoff, do not overfly, break
contact). There is no engagement, targeting-for-strike, weaponeering or
prosecution logic anywhere in godSeye, and none may be added here. The weapon
ranges consumed from the OB library describe what a contact can do TO the UAV.

Coordination (M10): hand a track off from one UAV to another so the first can
return to home / refuel while the track stays under observation. Custody
transfer requires a positive ID — confidence at least 'probable'.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .safety import point_in_polygon
from .targets import (
    UNCLASSIFIED_STANDOFF_M,
    MIN_STANDOFF_M,
    ObClass,
    PatternOfLife,
    Track,
    _cite,
    _clamp,
    assess_confidence,
    confidence_at_least,
    OB_LIBRARY,
    ob_for_category,
)

THREAT_LEVELS = ["none", "low", "moderate", "high", "critical"]
_LEVEL_THRESHOLDS = ((0.70, "critical"), (0.45, "high"), (0.25, "moderate"),
                     (0.05, "low"), (0.0, "none"))

#: Capability floor when the observer is far outside (or there is no) envelope.
_GEOMETRY_FLOOR = 0.05
#: Intrinsic share of capability that geometry cannot remove: a rocket battery
#: is dangerous to the force even though it cannot reach the UAV.
_INTRINSIC_SHARE = 0.35

#: Categories whose static posture means "emplaced and at readiness".
_AIR_DEFENCE_CATEGORIES = ("sam", "aaa", "radar")
#: Categories that are known emitters worth an ESM-style indicator.
_EMITTER_CATEGORIES = ("sam", "aaa", "radar", "c2", "aircraft", "naval")

#: Standing statement of authority, echoed into every assessment (M14).
ISR_AUTHORITY_NOTE = (
    "ISR-only: this is a sensor-posture and self-protection advisory. "
    "godSeye has no engagement capability and confers no engagement authority; "
    "command decisions remain with the operator."
)

def _derive_oob() -> dict[str, dict]:
    """Collapse the OB library into the legacy coarse-category view."""
    out: dict[str, dict] = {}
    for entry in OB_LIBRARY.values():
        row = out.setdefault(entry.category, {"weight": 0.0, "envelope_m": 0.0})
        row["weight"] = max(row["weight"], entry.threat_weight)
    for cat, row in out.items():
        rep = ob_for_category(cat)
        row["envelope_m"] = rep.weapon_range_m or UNCLASSIFIED_STANDOFF_M
        row["ob_class"] = rep.key
    out.setdefault("tel", {"weight": 0.90, "envelope_m": 12000.0,
                           "ob_class": "sam_short_range"})
    out["unknown"] = {"weight": 0.10, "envelope_m": UNCLASSIFIED_STANDOFF_M,
                      "ob_class": "unclassified"}
    return out


#: Legacy compatibility view: coarse category -> {weight, envelope_m,
#: ob_class}. Derived from OB_LIBRARY, never hand-maintained. New code should
#: use targets.OB_LIBRARY (rows carry capabilities, a separate acquisition
#: range, mobility and unit size) and `standoff_m()` for M5 standoff.
_OOB: dict[str, dict] = _derive_oob()


def _dist_m(a_lat, a_lon, b_lat, b_lon) -> float:
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(a_lat))
    return math.hypot((b_lat - a_lat) * m_lat, (b_lon - a_lon) * m_lon)


def _bearing_deg(a_lat, a_lon, b_lat, b_lon) -> float:
    m_lon = 111320.0 * math.cos(math.radians(a_lat))
    return math.degrees(math.atan2((b_lon - a_lon) * m_lon,
                                   (b_lat - a_lat) * 111320.0)) % 360.0


def standoff_m(track: Track) -> float:
    """Server-derived standoff for observing a track (M5, PLAN §4.3).

    Outside the contact's engagement envelope with a 10% margin. An
    unclassified contact gets a prudent default that is explicitly NOT a claim
    about its capability.
    """
    ob = track.ob
    if ob.weapon_range_m <= 0.0:
        return UNCLASSIFIED_STANDOFF_M if ob.key == "unclassified" else MIN_STANDOFF_M
    return max(MIN_STANDOFF_M, ob.weapon_range_m * 1.1)


def _level_for(score: float) -> str:
    for threshold, name in _LEVEL_THRESHOLDS:
        if score >= threshold:
            return name
    return "none"


# --------------------------------------------------------------------------
# §4.6(a)+(d) — capability
# --------------------------------------------------------------------------
def assess_capability(track: Track, observer: dict | None = None) -> dict:
    """Capability term of the score: what this contact can do, and to whom.

    capability = ob.threat_weight x (intrinsic share + geometry share), where
    geometry is the observer's position relative to the contact's engagement
    envelope and ceiling. Every input is returned so the caller can recompute
    the number without parsing prose (PLAN §4.6(d)).
    """
    ob: ObClass = track.ob
    ev: list[dict] = [
        _cite("ob_class", ob.key,
              f"order-of-battle library row '{ob.key}' ({ob.name}) matched for "
              f"track {track.track_id}"),
        _cite("capability_weight", ob.threat_weight,
              f"library weight for {ob.name}: {ob.role}; capabilities="
              f"{'; '.join(ob.capabilities)}"),
        _cite("weapon_range_m", ob.weapon_range_m,
              f"engagement envelope of {ob.name} against an airborne observer "
              f"(ceiling {ob.weapon_ceiling_m:.0f} m)", unit="m"),
        _cite("acquisition_range_m", ob.acquisition_range_m,
              f"sensor/acquisition range of {ob.name} — detection reaches "
              "further than engagement", unit="m"),
        _cite("mobility", ob.mobility,
              f"{ob.mobility} platform, road speed {ob.mobility_speed_mps} m/s"),
    ]

    geometry = _GEOMETRY_FLOOR
    in_envelope = False
    observer_range_m = None
    above_ceiling = False
    if observer and observer.get("lat") is not None:
        obs_alt = float(observer.get("alt_m") or 0.0)
        ground = _dist_m(track.lat, track.lon, float(observer["lat"]),
                         float(observer["lon"]))
        dz = obs_alt - float(track.alt_m or 0.0)
        observer_range_m = math.hypot(ground, dz)
        if ob.weapon_range_m > 0.0:
            above_ceiling = (ob.weapon_ceiling_m > 0.0
                             and obs_alt > ob.weapon_ceiling_m)
            if observer_range_m <= ob.weapon_range_m and not above_ceiling:
                geometry, in_envelope = 1.0, True
            else:
                # geometry is a FRACTION of the in-envelope case and must never
                # exceed 1.0: for a row whose weapon_range/ceiling ratio is > 4
                # (ifv, apc, patrol_boat) the unclamped ratio made the
                # above-ceiling "penalty" larger than being inside the
                # envelope, i.e. climbing above the weapon ceiling RAISED the
                # assessed threat (M13d regression).
                geometry = _clamp(ob.weapon_range_m / max(1.0, observer_range_m),
                                  _GEOMETRY_FLOOR, 1.0)
                if above_ceiling:
                    geometry = max(_GEOMETRY_FLOOR, geometry * 0.25)
            ev.append(_cite(
                "observer_range_m", round(observer_range_m, 1),
                f"observer slant range to track {track.track_id}; envelope "
                f"{ob.weapon_range_m:.0f} m -> "
                f"{'INSIDE' if in_envelope else 'outside'} the engagement envelope",
                score=geometry, unit="m"))
            if above_ceiling:
                ev.append(_cite("above_weapon_ceiling", True,
                                f"observer at {obs_alt:.0f} m is above the "
                                f"{ob.weapon_ceiling_m:.0f} m engagement ceiling of {ob.name}",
                                score=0.25))
        else:
            ev.append(_cite(
                "observer_range_m", round(observer_range_m, 1),
                f"{ob.name} asserts no engagement envelope against air; capability "
                "reduced to its intrinsic share", score=geometry, unit="m"))
    else:
        ev.append(_cite("observer_range_m", None,
                        "no observer position supplied — engagement geometry "
                        "unresolved, intrinsic capability only", score=geometry,
                        inferred=True))

    value = _clamp(ob.threat_weight * (_INTRINSIC_SHARE
                                       + (1.0 - _INTRINSIC_SHARE) * geometry))
    return {
        "value": round(value, 4),
        "formula": f"threat_weight x ({_INTRINSIC_SHARE} + "
                   f"{round(1 - _INTRINSIC_SHARE, 2)} x engagement_geometry)",
        "capability_weight": ob.threat_weight,
        "engagement_geometry": round(geometry, 4),
        "in_envelope": in_envelope,
        "envelope_m": ob.weapon_range_m,
        "weapon_ceiling_m": ob.weapon_ceiling_m,
        "acquisition_range_m": ob.acquisition_range_m,
        "observer_range_m": round(observer_range_m, 1) if observer_range_m is not None else None,
        "above_weapon_ceiling": above_ceiling,
        "mobility": ob.mobility,
        "envelope_asserted": ob.weapon_range_m > 0.0,
        "prudent_standoff_m": round(standoff_m(track), 1),
        "evidence": ev,
    }


# --------------------------------------------------------------------------
# §4.6(b) — the four intent indicators
# --------------------------------------------------------------------------
_POSTURE_VALUES = {
    "emplaced_ready": 0.70,   # air-defence sited and able to radiate
    "emplaced": 0.50,         # weapon system static in a firing position
    "displacing": 0.50,       # relocating after/before an engagement
    "on_march": 0.40,         # moving on a route
    "manoeuvring": 0.35,
    "halted": 0.20,
    "static_installation": 0.25,
    "unknown": 0.15,
}


def _indicator(name: str, value: float, weight: float, state, evidence: list[dict]) -> dict:
    value = _clamp(value)
    return {
        "indicator": name, "value": round(value, 3), "weight": round(weight, 3),
        "contribution": round(value * weight, 4), "state": state,
        "evidence": evidence,
    }


def indicator_posture(track: Track, now: float) -> dict:
    """Intent indicator 1/4: posture (PLAN §4.6(b))."""
    ob = track.ob
    speed = track.speed_mps
    dwell = track.dwell_s(now)
    ev = [_cite("speed_mps", speed,
                f"track {track.track_id} velocity from the last fix pair "
                f"({track.sightings} fix(es) held)", unit="m/s"),
          _cite("mobility_class", ob.mobility,
                f"{ob.name} is {ob.mobility}, road speed {ob.mobility_speed_mps} m/s")]

    if ob.mobility == "fixed":
        state = "static_installation"
        ev.append(_cite("posture_rule", state,
                        f"{ob.name} is a fixed installation — it cannot reposition"))
    elif speed is None or speed < 0.5:
        ev.append(_cite("dwell_s", round(dwell, 1),
                        f"track {track.track_id} has held its position for "
                        f"{dwell:.0f} s", unit="s"))
        if ob.category in _AIR_DEFENCE_CATEGORIES and ob.emitter:
            state = "emplaced_ready"
            ev.append(_cite("posture_rule", state,
                            f"{ob.name} is a static air-defence emitter — emplaced "
                            "systems are at readiness by definition"))
        elif ob.engages_air:
            state = "emplaced"
            ev.append(_cite("posture_rule", state,
                            f"{ob.name} is static and holds an air engagement "
                            f"envelope of {ob.weapon_range_m:.0f} m"))
        else:
            state = "halted"
            ev.append(_cite("posture_rule", state,
                            f"{ob.name} is static and asserts no air engagement envelope"))
    elif ob.category in _AIR_DEFENCE_CATEGORIES:
        state = "displacing"
        ev.append(_cite("posture_rule", state,
                        f"{ob.name} is an air-defence system on the move — displacement "
                        "brackets an engagement"))
    elif ob.mobility_speed_mps and speed >= 0.6 * ob.mobility_speed_mps:
        state = "on_march"
        ev.append(_cite("posture_rule", state,
                        f"{speed} m/s is >=60% of the {ob.mobility} road speed "
                        f"({ob.mobility_speed_mps} m/s)"))
    else:
        state = "manoeuvring"
        ev.append(_cite("posture_rule", state,
                        f"{speed} m/s is below the {ob.mobility} road speed — "
                        "tactical movement, not road march"))
    return _indicator("posture", _POSTURE_VALUES[state], 0.90, state, ev)


def indicator_movement(track: Track, observer: dict | None,
                       defended: list[dict] | None) -> dict:
    """Intent indicator 2/4: movement toward a defended asset (PLAN §4.6(b))."""
    ev: list[dict] = []
    assets = list(defended or [])
    if observer and observer.get("lat") is not None:
        assets.append({"lat": observer["lat"], "lon": observer["lon"],
                       "name": "observing UAV (self-protection)"})
    if not assets:
        ev.append(_cite("defended_assets", 0,
                        "no defended assets supplied and no observer position"))
        return _indicator("movement_toward_asset", 0.0, 1.0, "no_assets", ev)
    if not track.speed_mps or track.speed_mps <= 0.5 or track.heading_deg is None:
        ev.append(_cite("speed_mps", track.speed_mps,
                        f"track {track.track_id} is not moving — approach cannot be "
                        "assessed", unit="m/s"))
        return _indicator("movement_toward_asset", 0.0, 1.0, "static", ev)

    best_value, best_state = 0.0, "no_approach"
    prev = track.history[-2] if len(track.history) >= 2 else None
    for asset in assets:
        name = asset.get("name", "asset")
        rng = _dist_m(track.lat, track.lon, float(asset["lat"]), float(asset["lon"]))
        bearing = _bearing_deg(track.lat, track.lon, float(asset["lat"]),
                               float(asset["lon"]))
        delta = abs((bearing - track.heading_deg + 180) % 360 - 180)
        closure = None
        if prev is not None:
            prev_rng = _dist_m(prev[1], prev[2], float(asset["lat"]), float(asset["lon"]))
            dt = max(1e-3, track.last_seen - prev[0])
            closure = (prev_rng - rng) / dt
        if delta >= 30.0:
            continue
        cone = 1.0 - delta / 30.0
        proximity = _clamp(1.0 - rng / 10000.0)
        value = cone * (0.5 + 0.5 * proximity)
        if value > best_value:
            best_value, best_state = value, f"closing on {name}"
            ev = [
                _cite("asset", name, f"defended asset at {asset['lat']:.5f},"
                                     f"{asset['lon']:.5f}"),
                _cite("range_to_asset_m", round(rng, 1),
                      f"track {track.track_id} is {rng:.0f} m from {name}",
                      score=proximity, unit="m"),
                _cite("heading_offset_deg", round(delta, 1),
                      f"track heading {track.heading_deg} deg vs bearing-to-asset "
                      f"{bearing:.1f} deg — inside the 30 deg approach cone",
                      score=cone, unit="deg"),
            ]
            if closure is not None:
                ev.append(_cite("closure_rate_mps", round(closure, 2),
                                f"range to {name} changed between the fixes at "
                                f"t={prev[0]:.0f} and t={track.last_seen:.0f}",
                                unit="m/s"))
    if best_value == 0.0:
        ev = [_cite("heading_deg", track.heading_deg,
                    f"track {track.track_id} heading is outside the 30 deg approach "
                    f"cone of all {len(assets)} asset(s)", unit="deg")]
    return _indicator("movement_toward_asset", best_value, 1.0, best_state, ev)


def indicator_pattern_of_life(track: Track, pol: PatternOfLife | None,
                              now: float) -> dict:
    """Intent indicator 3/4: pattern-of-life deviation, from the M12 store."""
    if pol is None:
        return _indicator(
            "pattern_of_life_deviation", 0.0, 0.85, "no_store",
            [_cite("pattern_of_life", None,
                   "no pattern-of-life store supplied to the assessment (M12)",
                   inferred=True)])
    dev = pol.deviation_for_track(track, now)
    state = (f"deviation at {dev['poi']}" if dev.get("poi")
             else "outside every pattern-of-life POI")
    return _indicator("pattern_of_life_deviation", dev["deviation"], 0.85, state,
                      dev.get("evidence", []))


def indicator_emissions(track: Track) -> dict:
    """Intent indicator 4/4: emissions (PLAN §4.6(b), 'if modeled').

    An observed emission is strong evidence. With no ESM observation the
    indicator falls back to the OB library's emitter flag and is explicitly
    marked inferred, so nothing pretends to be a measurement.
    """
    ob = track.ob
    if track.emitter_observed():
        active = [o for o in track.observations if o.emitter_active]
        return _indicator(
            "emissions", 0.90, 0.70, "emission observed",
            [_cite("emitter_active", True,
                   f"{len(active)} observation(s) of track {track.track_id} reported "
                   f"an active emitter (latest frame "
                   f"{active[-1].frame_id or f't={active[-1].ts:.0f}'})")])
    if ob.emitter and ob.category in _EMITTER_CATEGORIES:
        return _indicator(
            "emissions", 0.45, 0.70, "emitter class, no ESM observation",
            [_cite("emitter_class", ob.key,
                   f"{ob.name} is a known emitter ({ob.role}); no ESM observation "
                   "is available to confirm it is radiating", inferred=True)])
    return _indicator(
        "emissions", 0.0, 0.70, "non-emitter",
        [_cite("emitter_class", ob.key,
               f"{ob.name} carries no emitter in the order-of-battle library")])


def assess_intent(track: Track, observer: dict | None = None,
                  defended: list[dict] | None = None,
                  pol: PatternOfLife | None = None,
                  now: float | None = None) -> dict:
    """All four §4.6(b) intent indicators, combined and fully traceable.

    Combination is noisy-OR over each indicator's weighted value: one strong
    indicator raises intent, several raise it further, and no indicator can
    mask another. Every indicator returns its own evidence list.
    """
    now = time.time() if now is None else now
    indicators = [
        indicator_posture(track, now),
        indicator_movement(track, observer, defended),
        indicator_pattern_of_life(track, pol, now),
        indicator_emissions(track),
    ]
    residual = 1.0
    for ind in indicators:
        residual *= (1.0 - _clamp(ind["contribution"]))
    value = _clamp(1.0 - residual)
    return {
        "value": round(value, 4),
        "method": "noisy-OR over weighted indicators (PLAN §4.6(b))",
        "indicators": indicators,
        "indicators_present": sum(1 for i in indicators if i["value"] > 0.0),
        "indicator_count": len(indicators),
    }


# --------------------------------------------------------------------------
# §4.6(d) — score = capability x intent
# --------------------------------------------------------------------------
def assess_track(track: Track, observer: dict | None = None,
                 defended: list[dict] | None = None,
                 pol: PatternOfLife | None = None,
                 now: float | None = None) -> dict:
    """Deterministic threat assessment for one track (M13, PLAN §4.6).

    observer: {lat, lon, alt_m} of the UAV assessing (engagement geometry).
    defended: [{lat, lon, name}] assets whose approach raises intent.
    pol:      pattern-of-life store (M12) for the deviation indicator.

    Returns capability, intent and confidence as structured, individually
    cited sub-assessments plus the flat numbers, so a human can see exactly
    which evidence drove which sub-score. ISR-only (M14): the sole advisory is
    sensor posture and self-protection.
    """
    now = time.time() if now is None else now
    ob = track.ob
    capability = assess_capability(track, observer)
    intent = assess_intent(track, observer, defended, pol, now)
    confidence = assess_confidence(track, now)

    score = _clamp(capability["value"] * intent["value"])
    level = _level_for(score)
    posture = _sensor_posture(level, capability, track)

    evidence: list[dict] = []
    for item in capability["evidence"]:
        evidence.append({"component": "capability", **item})
    for ind in intent["indicators"]:
        for item in ind["evidence"]:
            evidence.append({"component": f"intent.{ind['indicator']}", **item})
    for item in confidence["evidence"]:
        evidence.append({"component": "confidence", **item})

    return {
        "format": "THREAT_ASSESSMENT",
        "track_id": track.track_id,
        "category": track.category,
        "ob_class": ob.key,
        "ob_name": ob.name,
        # ---- flat, machine-traceable score components (PLAN §4.6(d)) ----
        "threat_score": round(score, 3),
        "threat_level": level,
        "capability": capability["value"],
        "capability_weight": capability["capability_weight"],
        "envelope_factor": capability["engagement_geometry"],
        "intent": intent["value"],
        "confidence_level": confidence["level"],
        "confidence_score": confidence["score"],
        "in_envelope": capability["in_envelope"],
        "envelope_m": capability["envelope_m"],
        "envelope_asserted": capability["envelope_asserted"],
        "observer_range_m": capability["observer_range_m"],
        # ---- structured sub-assessments ----
        "assessment": {
            "capability": capability,
            "intent": intent,
            "confidence": confidence,
            "score": {
                "value": round(score, 3),
                "formula": "capability x intent (PLAN §4.6(d))",
                "capability": capability["value"],
                "intent": intent["value"],
                "level_thresholds": {name: threshold
                                     for threshold, name in _LEVEL_THRESHOLDS},
            },
        },
        "evidence": evidence,
        "rationale": _rationale(track, capability, intent, confidence),
        # ---- ISR-only advisory (M14) ----
        "sensor_posture": posture,
        "isr_only": True,
        "authority": ISR_AUTHORITY_NOTE,
        # deprecated alias of sensor_posture.advisory, kept for the GEV panel
        # and demo script; new callers should read sensor_posture.
        "recommendation": posture["advisory"],
    }


def _rationale(track: Track, capability: dict, intent: dict, confidence: dict) -> str:
    """Human-readable summary. The structured `evidence` list is authoritative."""
    ob = track.ob
    parts = [f"{ob.name} [{ob.key}] capability {capability['value']:.2f} "
             f"(weight {capability['capability_weight']:.2f})"]
    if capability["in_envelope"]:
        parts.append(f"observer INSIDE the {capability['envelope_m']:.0f} m engagement "
                     f"envelope at {capability['observer_range_m']:.0f} m")
    elif capability["envelope_asserted"]:
        parts.append(f"outside the {capability['envelope_m']:.0f} m envelope "
                     f"(geometry {capability['engagement_geometry']:.2f})")
    else:
        parts.append("no engagement envelope asserted against air")
    driving = sorted(intent["indicators"], key=lambda i: i["contribution"], reverse=True)
    top = [f"{i['indicator']}={i['value']:.2f} ({i['state']})"
           for i in driving if i["value"] > 0.0][:3]
    parts.append(f"intent {intent['value']:.2f} from " + (", ".join(top) if top
                 else "no positive indicator"))
    parts.append(f"identification {confidence['level']} "
                 f"({confidence['score']:.2f}, {track.sightings} sighting(s))")
    return "; ".join(parts)


def _sensor_posture(level: str, capability: dict, track: Track) -> dict:
    """Sensor-posture / self-protection advisory only (M14).

    Says where to put the SENSOR and how to keep the aircraft alive. It never
    speaks to engagement, prosecution or effects — godSeye has no such
    capability and confers no such authority.
    """
    standoff = standoff_m(track)
    if level in ("critical", "high") or capability["in_envelope"]:
        code = "increase_standoff"
        advisory = (f"SENSOR POSTURE: open to at least {standoff:.0f} m standoff, hold "
                    "the contact on the narrow-FOV sensor, do not overfly")
    elif level == "moderate":
        code = "maintain_standoff"
        advisory = (f"SENSOR POSTURE: maintain standoff observation at {standoff:.0f} m; "
                    "keep the contact in sensor coverage")
    else:
        code = "routine_isr"
        advisory = "SENSOR POSTURE: continue routine ISR collection"
    return {
        "code": code,
        "advisory": advisory,
        "standoff_m": round(standoff, 1),
        "basis": (f"derived from the {capability['envelope_m']:.0f} m engagement "
                  f"envelope of {track.ob.name}" if capability["envelope_asserted"]
                  else "prudent default — contact asserts no engagement envelope"),
        "scope": "sensor employment and aircraft self-protection only",
        "authority": ISR_AUTHORITY_NOTE,
    }


#: Default number of contacts a roll-up reports in detail. The rest are still
#: listed, compactly — see `assess_area`.
SUMMARY_TOP_N = 10

DETAIL_LEVELS = ("summary", "full")

#: Dropped from a summary-level assessment. Every flat, machine-traceable score
#: component (capability, intent, confidence, envelope, observer_range) SURVIVES
#: — only the expandable sub-objects go, and `evidence` is itself a flattened
#: duplicate of what lives inside `assessment`. `authority`/`isr_only` are
#: report-level facts that were being repeated on every single contact.
_SUMMARY_DROP = ("assessment", "evidence", "authority", "isr_only")

#: The compact row used for contacts past `top_n`, so a truncated roll-up still
#: names every contact it did not expand.
_ROW_KEYS = ("track_id", "category", "ob_class", "threat_level", "threat_score",
             "confidence_level", "in_envelope")


def _summarize(assessment: dict) -> dict:
    return {k: v for k, v in assessment.items() if k not in _SUMMARY_DROP}


def _row(assessment: dict) -> dict:
    return {k: assessment[k] for k in _ROW_KEYS if k in assessment}


def assess_area(tracks: list[Track], observer: dict | None = None,
                defended: list[dict] | None = None,
                pol: PatternOfLife | None = None,
                area_polygon: list | None = None,
                now: float | None = None,
                detail: str = "summary",
                top_n: int | None = SUMMARY_TOP_N) -> dict:
    """Roll-up: the most dangerous contacts in the AO (M13, PLAN §4.3).

    `area_polygon` ([(lat, lon), ...]) scopes the report to one sector so
    successive sorties over different sectors do not blend into one
    highest_threat figure.

    SIZE (why this is not simply "return everything"): the consumer is an LLM
    harness, and this roll-up grows with the track store, which persists across
    runs. Measured at 36 tracks, the full form was 1.1 MB — past what the MCP
    python client will carry, and far past what a harness can read. So:

      detail="summary" (default) keeps every flat, traceable score component and
        drops the expandable `assessment` sub-objects and the `evidence` list
        (a flattened duplicate of them). Per-contact `authority` is hoisted to
        the report, where it belonged.
      detail="full" restores them, for a human or a single-contact look.
      top_n caps how many contacts are expanded; pass None for no cap.

    Contacts past `top_n` are NOT dropped — they appear in `omitted` as compact
    rows, and `truncation` says so in words. Silently shortening an ISR report
    is the same defect class as reporting planned coverage as flown.
    """
    if detail not in DETAIL_LEVELS:
        raise ValueError(
            f"detail={detail!r} is not one of {DETAIL_LEVELS}; refusing rather "
            "than guessing, because the wrong guess silently changes what an "
            "ISR report contains")
    if top_n is not None and top_n < 0:
        raise ValueError(f"top_n={top_n} must be >= 0 or None (no cap)")
    now = time.time() if now is None else now
    scoped = list(tracks)
    # safety.point_in_polygon treats a <3-vertex polygon as "no geofence
    # configured" and returns True for every point. Honour that here instead of
    # silently reporting a sector-scoped THREATREP that scoped nothing.
    usable = bool(area_polygon) and len(area_polygon) >= 3
    if usable:
        scoped = [t for t in scoped if point_in_polygon(t.lat, t.lon, area_polygon)]
    assessed = [assess_track(t, observer, defended, pol, now) for t in scoped]
    assessed.sort(key=lambda a: a["threat_score"], reverse=True)
    top = assessed[0] if assessed else None

    shown = assessed if top_n is None else assessed[:top_n]
    rest = [] if top_n is None else assessed[top_n:]
    body = shown if detail == "full" else [_summarize(a) for a in shown]

    return {
        "format": "THREATREP",
        "count": len(assessed),
        "scoped_by_polygon": usable,
        "scoping_error": (None if usable or not area_polygon else
                          f"area_polygon has {len(area_polygon)} vertices; at least 3 "
                          "are required — the roll-up covers every track"),
        "area_polygon": [list(p) for p in area_polygon] if area_polygon else None,
        "tracks_out_of_area": len(tracks) - len(scoped),
        "highest_threat": top["threat_level"] if top else "none",
        "highest_threat_track": top["track_id"] if top else None,
        "assessments": body,
        # ---- what this report did and did not expand (never silent) ----
        "detail": detail,
        "detailed_count": len(body),
        "omitted_count": len(rest),
        "omitted": [_row(a) for a in rest],
        "truncation": (
            None if not rest else
            f"{len(rest)} of {len(assessed)} contacts are listed in 'omitted' as "
            f"id/class/level/score rows only. Raise top_n, or call the "
            f"single-contact assessment, to expand them."),
        "detail_note": (
            "summary: flat score components only. Pass detail='full' for the "
            "capability/intent/confidence sub-assessments and their cited "
            "evidence (much larger)." if detail == "summary" else
            "full: every sub-assessment and its cited evidence."),
        "isr_only": True,
        "authority": ISR_AUTHORITY_NOTE,
    }


# ---- coordination (M10) ----

@dataclass
class Handoff:
    track_id: str
    from_vehicle: str
    to_vehicle: str
    accepted: bool
    reason: str = ""
    standoff_m: float = 0.0
    confidence: str = "possible"
    evidence: list = field(default_factory=list)


def plan_handoff(track: Track, from_vehicle: str, to_vehicle: str,
                 to_fuel_pct: float, to_pos: dict | None = None,
                 require_confidence: str = "probable",
                 now: float | None = None) -> Handoff:
    """Decide whether to_vehicle can take over observation of a track (M10).

    Three gates, all cited on the returned Handoff:
      1. Positive ID — identification confidence at least `require_confidence`
         (M10: custody transfers an identified contact, not a maybe).
      2. Receiver fuel — enough to reach a standoff point and hold.
      3. Receiver range — inside a practical transit of the standoff ring.

    Standoff is derived from the track's engagement envelope (M5), never
    chosen by the harness. ISR-only (M14): this is custody of a SENSOR track.
    """
    now = time.time() if now is None else now
    stand = standoff_m(track)
    conf = assess_confidence(track, now)
    ev = [_cite("identification_confidence", conf["level"],
                f"track {track.track_id}: {track.sightings} sighting(s), "
                f"confidence score {conf['score']:.2f}"),
          _cite("standoff_m", round(stand, 1),
                f"derived from the {track.ob.weapon_range_m:.0f} m engagement "
                f"envelope of {track.ob.name} (M5)", unit="m"),
          _cite("receiver_fuel_pct", round(float(to_fuel_pct), 1),
                f"{to_vehicle} fuel at handoff planning", unit="%")]

    if not confidence_at_least(conf["level"], require_confidence):
        return Handoff(track.track_id, from_vehicle, to_vehicle, False,
                       f"positive ID required: confidence '{conf['level']}' is below "
                       f"'{require_confidence}' — re-look before custody transfer",
                       stand, conf["level"], ev)
    if to_fuel_pct < 30.0:
        return Handoff(track.track_id, from_vehicle, to_vehicle, False,
                       "receiver below 30% fuel — cannot accept", stand,
                       conf["level"], ev)
    if to_pos and to_pos.get("lat") is not None:
        d = _dist_m(track.lat, track.lon, float(to_pos["lat"]), float(to_pos["lon"]))
        ev.append(_cite("receiver_range_m", round(d, 1),
                        f"{to_vehicle} is {d:.0f} m from track {track.track_id}",
                        unit="m"))
        if d > stand * 4:
            return Handoff(track.track_id, from_vehicle, to_vehicle, False,
                           f"receiver {d:.0f} m out — beyond handoff range", stand,
                           conf["level"], ev)
    return Handoff(track.track_id, from_vehicle, to_vehicle, True,
                   f"observe from {stand:.0f} m standoff; identification "
                   f"{conf['level']}", stand, conf["level"], ev)
