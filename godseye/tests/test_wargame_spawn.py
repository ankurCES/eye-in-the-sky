"""Spawn rules, placement, the spiral and the real-site gate (PLAN §4.5a M14a;
WG v2 §5.2.2 "Placing a scenario", §5.2.6).

Pure: no server, no sim, no ports, no network. The engine-level checks (a red
spawn really spawned, `no_room` leaving nothing behind) are in
`test_wargame_engine.py`.
"""
from __future__ import annotations

import math

import pytest
from godseye_uav import sites, theaters
from godseye_uav import wargame_spawn as sp
from godseye_uav import wargame_tables as T
from godseye_uav.safety import haversine_m
from godseye_uav.targets import match_ob
from support.wg_tokens import assert_no_real_system_tokens

THEATER = theaters.get("default")
HOME = (THEATER.home_lat, THEATER.home_lon)
POIS = tuple((p.lat, p.lon) for p in THEATER.pois)


def ne(dn: float, de: float, ref: tuple[float, float] = HOME) -> tuple[float, float]:
    """`dn` m north and `de` m east of `ref` (flat earth, < 3 km)."""
    return (ref[0] + dn / 111_320.0,
            ref[1] + de / (111_320.0 * math.cos(math.radians(ref[0]))))


def excl(*rects, protected=(), complete=True) -> sites.Exclusion:
    return sites.Exclusion(rects=tuple(tuple(r) for r in rects),
                           protected=tuple(tuple(r) for r in protected),
                           complete=complete, reason=None, fetched_at_ms=0,
                           element_count=len(rects))


def world(**kw) -> sp.World:
    base = {"ao": tuple(THEATER.ao), "geofence": tuple(THEATER.ao), "exclusion": excl(),
            "pois": POIS, "home": HOME}
    base.update(kw)
    return sp.World(**base)


def rect_east_of(point: tuple[float, float], gap_m: float, size_m: float = 100.0):
    """An `[s, w, n, e]` footprint whose WEST edge is `gap_m` east of `point`."""
    s_, w_ = ne(-size_m / 2, gap_m, point)
    n_, e_ = ne(size_m / 2, gap_m + size_m, point)
    return (s_, w_, n_, e_)


RED_OK = ne(1600, 1000)            # >= 1 km from home, > 500 m from every POI


# ------------------------------------------------------------- the rules ----

def test_a_clear_point_passes_for_both_sides():
    w = world()
    assert sp.position_refusal(w, "red", *RED_OK) is None
    assert sp.position_refusal(w, "blue", *RED_OK) is None


def test_a_mapped_ways_bounds_edge_at_400_m_is_refused_and_at_600_m_accepted():
    near = world(exclusion=excl(rect_east_of(RED_OK, 400.0)))
    far = world(exclusion=excl(rect_east_of(RED_OK, 600.0)))
    assert sp.position_refusal(near, "red", *RED_OK) == "near_real_site"
    assert sp.position_refusal(far, "red", *RED_OK) is None
    # The whole way counts, not its centre: its centre is > 450 m away here.
    assert sp.position_refusal(near, "blue", *RED_OK) == "near_real_site"


def test_theater_points_home_spacing_and_object_clearance():
    poi = POIS[0]
    assert sp.position_refusal(world(), "blue", *ne(400, 0, poi)) == "near_theater_point"
    assert sp.position_refusal(world(pois=()), "blue", *ne(400, 0, poi)) is None
    # Red keeps 1 km from home; blue does not.
    w = world(pois=())
    assert sp.position_refusal(w, "red", *ne(900, 0)) == "too_close_home"
    assert sp.position_refusal(w, "blue", *ne(900, 0)) is None
    assert sp.position_refusal(w, "red", *ne(1100, 0)) is None
    # 200 m from other scenario units, 150 m from any other object or track.
    assert sp.position_refusal(world(units=(ne(150, 0, RED_OK),)), "red", *RED_OK) \
        == "too_close_unit"
    assert sp.position_refusal(world(units=(ne(250, 0, RED_OK),)), "red", *RED_OK) is None
    assert sp.position_refusal(world(objects=(ne(100, 0, RED_OK),)), "red", *RED_OK) \
        == "near_existing_object"
    assert sp.position_refusal(world(objects=(ne(200, 0, RED_OK),)), "red", *RED_OK) is None


def test_the_ao_and_its_150_m_margin():
    _s, w_, n_, e_ = THEATER.bbox()
    mid_lon = (w_ + e_) / 2
    assert sp.position_refusal(world(), "red", n_ + 0.01, mid_lon) == "outside_ao"
    inside_100 = (n_ - 100 / 111_320.0, mid_lon)
    inside_200 = (n_ - 200 / 111_320.0, mid_lon)
    assert sp.position_refusal(world(), "red", *inside_100) == "outside_ao"
    assert sp.position_refusal(world(), "red", *inside_200) is None
    assert sp.position_refusal(world(), "red", float("nan"), mid_lon) == "outside_ao"
    assert sp.edge_distance_m(*inside_200, THEATER.ao) == pytest.approx(200.0, abs=1.0)


def test_rules_are_checked_in_order():
    # On a mapped footprint AND next to home AND on a unit: the site wins.
    w = world(exclusion=excl(rect_east_of(ne(700, 0), -50.0)), units=(ne(700, 0),))
    assert sp.position_refusal(w, "red", *ne(700, 0)) == "near_real_site"
    assert sp.POSITION_CODES == ("outside_ao", "near_real_site", "near_theater_point",
                                 "too_close_home", "too_close_unit", "near_existing_object")


# --------------------------------------------------------------- spiral -----

def test_the_spiral_is_deterministic_rings_of_150_m_in_30_degree_steps():
    pts = list(sp.spiral(*RED_OK))
    assert len(pts) == 8 * 12
    assert pts == list(sp.spiral(*RED_OK))
    for i, p in enumerate(pts):
        k = i // 12 + 1
        assert haversine_m(*RED_OK, *p) == pytest.approx(150.0 * k, abs=0.5)
    first = pts[0]
    assert first[0] > RED_OK[0] and first[1] == pytest.approx(RED_OK[1], abs=1e-9)


def test_place_moves_a_refused_point_to_the_first_valid_spiral_point():
    blocked = world(objects=(RED_OK,))
    p, code = sp.place(blocked, "red", *RED_OK)
    assert code is None
    expected = next(q for q in sp.spiral(*RED_OK)
                    if sp.position_refusal(blocked, "red", *q) is None)
    assert p == expected
    assert haversine_m(*RED_OK, *p) >= sp.OBJECT_CLEARANCE_M
    # Without `move` the caller's point itself must be valid.
    assert sp.place(blocked, "red", *RED_OK, move=False) == (None, "near_existing_object")


def test_plan_positions_is_all_or_nothing_and_keeps_spacing():
    reqs = [("red", *RED_OK)] * 3
    pos, code = sp.plan_positions(world(), reqs, move_first=False)
    assert code is None and len(pos) == 3 and pos[0] == RED_OK
    for i, a in enumerate(pos):
        for b in pos[i + 1:]:
            assert haversine_m(*a, *b) >= sp.UNIT_SPACING_M
    assert sp.moved_count(reqs, pos) == 2
    # One unit that fits nowhere refuses the whole request.
    everything = excl(tuple(THEATER.bbox()))
    assert sp.plan_positions(world(exclusion=everything), reqs) == (None, "no_room")
    # The first unit's own refusal when it may not move.
    assert sp.plan_positions(world(objects=(RED_OK,)), reqs, move_first=False) == \
        (None, "near_existing_object")
    assert sp.plan_positions(world(n_units=59), reqs) == (None, "max_units")
    assert sp.MAX_UNITS == 60


def test_no_room_in_a_dense_fixture_is_the_spec_sentence():
    ring = tuple(rect_east_of(ne(dn, de), 0.0, 400.0)
                 for dn in range(-2000, 2001, 450) for de in range(-2300, 2301, 450))
    pos, code = sp.plan_positions(world(exclusion=excl(*ring)), [("red", *RED_OK)])
    assert pos is None and code == "no_room"
    assert sp.refusal("no_room")["message"] == (
        "There isn't room here for this scenario away from mapped places, theater points "
        "and home; try a smaller intensity or a larger AO.")


# ------------------------------------------------------ templates / fit -----

def quad_ao() -> tuple[float, ...]:
    t = theaters.make_dynamic(label="Quad test", place="Test", center=(47.0, 8.0),
                              half_extent_m=2940.0, home=(47.0, 8.0), home_alt_msl_m=400.0,
                              provenance=None)
    return t.bbox()


def big_ao() -> tuple[float, ...]:
    t = theaters.make_dynamic(label="Big test", place="Test", center=(47.0, 8.0),
                              half_extent_m=25_000.0, home=(47.0, 8.0), home_alt_msl_m=400.0,
                              provenance=None)
    return t.bbox()


def test_class_fit_picks_portable_in_a_5_9_km_ao_and_medium_in_a_50_km_ao():
    quad = T.ao_geometry_bbox(quad_ao())
    big = T.ao_geometry_bbox(big_ao())
    assert 5800 <= 2 * quad["half_extent_m"] <= 5900
    small = T.template_positions("air_defence_belt", "medium", (47.0, 8.0), (47.0, 8.0), quad)
    large = T.template_positions("air_defence_belt", "medium", (47.0, 8.0), (47.0, 8.0), big)
    assert {p["wg_class"] for p in small if p["slot"] == "ad"} == {"ad_manportable"}
    assert {p["wg_class"] for p in large if p["slot"] == "ad"} == {"ad_medium"}
    # The fallback and its caveat when nothing fits (the stock 3.2 km half-diagonal
    # fits only the guns; a 1 km AO fits nothing).
    tiny = {"half_extent_m": 500.0, "half_diagonal_m": 700.0}
    assert T.choose_ad_class(tiny) == "ad_gun"
    assert T.class_caveats(T.CLASSES["ad_gun"], tiny) == [T.COVERS_CAVEAT]


# --------------------------------------------------------- site gate --------

def test_site_gate_on_footprints_theater_points_and_protected_places():
    e = excl(rect_east_of(RED_OK, 400.0))
    assert sp.site_gate(e, (), *RED_OK) == "target_near_real_site"
    assert sp.site_gate(excl(rect_east_of(RED_OK, 600.0)), (), *RED_OK) is None
    assert sp.site_gate(excl(), (ne(450, 0, RED_OK),), *RED_OK) == "target_near_real_site"
    prot = excl(protected=(rect_east_of(RED_OK, 900.0),))
    assert sp.site_gate(prot, (), *RED_OK) == "target_protected"
    assert sp.site_gate(excl(protected=(rect_east_of(RED_OK, 1100.0),)), (), *RED_OK) is None
    assert sp.site_gate(None, (), *RED_OK) is None
    for code in ("target_near_real_site", "target_protected"):
        assert sp.refusal(code) == {"rejected": True, "error": code,
                                    "message": sp.MESSAGES[code], "simulated": True}


# ------------------------------------------------------- names and text -----

@pytest.mark.parametrize("wg_class", [k for k, c in T.CLASSES.items() if c.side == "red"])
def test_session_unique_object_names_classify_back_to_their_class(wg_class):
    ob = T.CLASSES[wg_class].ob_key
    name = sp.object_name(ob, 1759072800123, 7)
    assert name == f"{ob}_1759072800123007"
    assert match_ob(name)[0].key == ob
    assert sp.object_name(ob, 1759072800123, 7) != sp.object_name(ob, 1759072800124, 7)


def test_ao_relative_positions_name_no_place():
    c = HOME
    assert sp.relative_position(c, *c) == "at the AO centre"
    assert sp.relative_position(c, *ne(2100, 2100)) == "3.0 km north-east of the AO centre"
    assert sp.relative_position(c, *T.destination(*c, 350, 180)) == \
        "350 m south of the AO centre"
    assert sp.relative_position(c, *T.destination(*c, 800, 270)) == \
        "800 m west of the AO centre"
    assert [sp.compass_word(b) for b in (0, 44, 46, 180, 315, 359)] == [
        "north", "north-east", "north-east", "south", "north-west", "north"]


def test_every_message_is_one_sentence_without_real_system_tokens():
    assert_no_real_system_tokens(sp.MESSAGES)
    for code, msg in sp.MESSAGES.items():
        assert msg.endswith(".") and msg[0].isupper(), code
        assert "Redmond" not in msg and THEATER.place not in msg
