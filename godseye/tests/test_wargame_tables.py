"""Notional wargame class tables — WG spec §5.2.2, unit B1 (M14a, PLAN.md §4.5a).

B1's acceptance (§5.4): the class table is exhaustive (16); every label and note
passes `assert_no_real_system_tokens`. Also pinned here: the spec's numbers,
designators, generic labels, hardness estimates, AO fit and the templates.
Pure: no server, no ports, no network.
"""
from __future__ import annotations

import dataclasses
import json
import math

import pytest
from godseye_uav import theater_plan, theaters
from godseye_uav import wargame_tables as T
from godseye_uav.safety import bearing_deg, haversine_m
from godseye_uav.targets import OB_LIBRARY
from support.wg_tokens import assert_no_real_system_tokens, find_real_system_tokens

RED_KEYS = {"ad_long", "ad_medium", "ad_short", "ad_gun", "ad_manportable",
            "radar_early_warning", "armour_company", "mech_infantry",
            "artillery_battery", "command_post", "supply_depot"}
BLUE_KEYS = {"blue_artillery", "blue_rocket", "blue_strike_air", "blue_mech",
             "blue_defended_point"}

# key: (ob_key, prefix, label, role, threat, ceiling, detect, p_det, pk_air,
#       ground, pk_ground, cycle, ammo, suppress, optical, speed, hardness)
RED_TABLE = {
    "ad_long": ("sam_long_range", "sam", "Surface-to-air, long range", "air",
                40000, 15000, 80000, .90, .60, 0, 0, 30, 8, 300, False, 0, .85),
    "ad_medium": ("sam_medium_range", "sam", "Surface-to-air, medium range", "air",
                  20000, 8000, 40000, .85, .55, 0, 0, 20, 12, 240, False, 0, .85),
    "ad_short": ("sam_short_range", "sam", "Surface-to-air, short range", "air",
                 8000, 4000, 15000, .80, .50, 0, 0, 12, 8, 180, False, 6, .85),
    "ad_gun": ("aaa_towed", "aaa", "Air-defence guns", "air",
               2000, 1000, 3000, .70, .30, 0, 0, 5, 40, 120, True, 0, .85),
    "ad_manportable": ("manpads", "manpads", "Portable surface-to-air", "air",
                       4000, 2500, 5000, .60, .40, 0, 0, 60, 4, 120, True, 1.5, .9),
    "radar_early_warning": ("radar_acquisition", "radar", "Early-warning radar", "sensor",
                            0, 0, 60000, .90, 0, 0, 0, 0, 0, 300, False, 0, .9),
    "armour_company": ("mbt", "armour", "Armour company", "ground",
                       0, 0, 5000, .80, 0, 3000, .35, 60, 20, 180, True, 8, .7),
    "mech_infantry": ("ifv", "infantry", "Mechanised infantry", "ground",
                      0, 0, 4000, .80, 0, 2000, .25, 60, 20, 180, True, 6, .8),
    "artillery_battery": ("spg", "artillery", "Artillery battery", "indirect",
                          0, 0, 2000, .70, 0, 15000, .30, 120, 12, 240, True, 0, .85),
    "command_post": ("c2_node", "cp", "Command post", "none",
                     0, 0, 2000, .70, 0, 0, 0, 0, 0, 300, True, 0, .9),
    "supply_depot": ("depot_ammo", "depot", "Supply depot", "none",
                     0, 0, 0, 0, 0, 0, 0, 0, 0, 300, False, 0, 1.0),
}
RED_FIELDS = ("ob_key", "prefix_slug", "label", "role", "threat_range_m",
              "threat_ceiling_m", "detection_range_m", "p_detect_ref", "pk_air",
              "ground_range_m", "pk_ground", "cycle_s", "ammo", "suppress_s",
              "optical", "speed_mps", "hardness")

# key: (prefix, label, role, strike, pk_strike, cycle, ammo, hardness, speed)
BLUE_TABLE = {
    "blue_artillery": ("artillery", "Artillery battery", "shooter", 20000, .50, 120, 12, 1.0, 0),
    "blue_rocket": ("rockets", "Rocket battery", "shooter", 40000, .60, 300, 4, 1.0, 0),
    "blue_strike_air": ("strike", "Strike package (virtual)", "shooter", None, .65, 600, 2,
                        1.0, 0),
    "blue_mech": ("mech", "Mechanised company", "shooter", 2500, .35, 60, 20, .7, 6),
    "blue_defended_point": ("depot", "Defended depot", "objective", 0, 0, 0, 0, .8, 0),
}
BLUE_FIELDS = ("prefix_slug", "label", "role", "strike_range_m", "pk_strike", "cycle_s",
               "ammo", "hardness", "speed_mps")


def _square_geo(half_m: float) -> dict[str, float]:
    return {"half_extent_m": half_m, "half_diagonal_m": half_m * math.sqrt(2.0)}


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _box_around(lat: float, lon: float, half_m: float) -> tuple[float, float, float, float]:
    dlat = half_m / 111_320.0
    dlon = half_m / (111_320.0 * math.cos(math.radians(lat)))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


# ---------------------------------------------------------------- class table
def test_class_table_is_exhaustive():
    assert len(T.CLASSES) == 16
    assert set(T.CLASSES) == RED_KEYS | BLUE_KEYS
    assert {k for k, c in T.CLASSES.items() if c.side == "red"} == RED_KEYS
    assert {k for k, c in T.CLASSES.items() if c.side == "blue"} == BLUE_KEYS
    for key, cls in T.CLASSES.items():
        assert cls.key == key
        assert cls.side in T.SIDES and cls.role in T.ROLES
        assert cls.prefix_slug in T.WORD
        if cls.side == "red":
            assert cls.ob_key in OB_LIBRARY, key
        else:
            assert cls.ob_key is None, key


def test_red_rows_match_the_spec_table():
    for key, row in RED_TABLE.items():
        cls = T.CLASSES[key]
        assert tuple(getattr(cls, f) for f in RED_FIELDS) == row, key
        assert cls.strike_range_m == 0.0 and cls.pk_strike == 0.0


def test_blue_rows_match_the_spec_table():
    for key, row in BLUE_TABLE.items():
        cls = T.CLASSES[key]
        assert tuple(getattr(cls, f) for f in BLUE_FIELDS) == row, key
        assert cls.threat_range_m == 0.0 and cls.detection_range_m == 0.0
        assert cls.pk_air == 0.0 and cls.pk_ground == 0.0
    # Blue suppression is set here (the spec's blue table has no column for it).
    assert [T.CLASSES[k].suppress_s for k in sorted(BLUE_KEYS)] == [180, 300, 180, 180, 0]


def test_constants():
    assert T.TABLE_VERSION == "wg-notional/1"
    assert T.NOTIONAL_NOTE == (
        "Notional simulation parameters chosen for play balance. Not weapon data.")
    assert T.P_CAP == 0.95
    assert (T.DAMAGED_PK_FACTOR, T.DAMAGED_CYCLE_FACTOR, T.DAMAGED_RANGE_FACTOR) == (
        0.5, 2.0, 0.8)
    assert T.RADAR_CUE_BONUS == 0.2
    assert T.CONF_FACTOR == {"confirmed": 1.0, "probable": 0.85}
    assert T.EFFECT_SPLIT == {"destroyed": 0.32, "damaged": 0.40, "suppressed": 0.28}
    assert math.isclose(sum(T.EFFECT_SPLIT.values()), 1.0)
    assert (T.PACKAGE_ALT_AGL_M, T.PACKAGE_SPEED_MPS) == (150.0, 100.0)


def test_classes_are_frozen_and_json_ready():
    cls = T.CLASSES["ad_short"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cls.pk_air = 1.0  # type: ignore[misc]
    rows = T.class_rows(_square_geo(2940.0))
    assert len(rows) == 16
    json.dumps(rows, allow_nan=False)
    assert cls.mobile and not T.CLASSES["ad_gun"].mobile


# ---------------------------------------------------------------- designators, labels
def test_designators_and_unit_ids():
    assert T.designator("red", "sam", 1) == "Red SAM 1"
    assert T.designator("blue", "artillery", 1) == "Blue artillery 1"
    assert T.designator("red", "aaa", 2) == "Red AD guns 2"
    assert T.designator("red", "manpads", 3) == "Red portable SAM 3"
    assert T.designator("blue", "strike", 1) == "Blue strike package 1"
    assert T.designator("blue", "mech", 2) == "Blue mech company 2"
    assert T.unit_id("red", "sam", 1) == "red-sam-1"
    assert T.unit_id("blue", "rockets", 2) == "blue-rockets-2"
    with pytest.raises(KeyError):
        T.designator("red", "tank", 1)


def test_label_for_ob_is_generic():
    assert T.label_for_ob("aaa_towed") == "Air-defence guns"          # red class match
    assert T.label_for_ob("sam_short_range") == "Surface-to-air, short range"
    assert T.label_for_ob("radar_acquisition") == "Early-warning radar"
    assert T.label_for_ob("spaag_missile") == "Air-defence contact"   # category aaa
    assert T.label_for_ob("aaa_self_propelled") == "Air-defence contact"
    assert T.label_for_ob("radar_counterbattery") == "Radar contact"
    assert T.label_for_ob("apc") == "Armour contact"
    assert T.label_for_ob("mlrs") == "Artillery contact"
    assert T.label_for_ob("comms_relay") == "Command contact"
    assert T.label_for_ob("supply_truck") == "Logistics or structure"
    assert T.label_for_ob("bridge") == "Logistics or structure"
    for other in ("civilian_vehicle", "fixed_wing", "patrol_boat", "unclassified",
                  "no_such_class", "", None):
        assert T.label_for_ob(other) == "Contact"
    for key, ob in OB_LIBRARY.items():
        label = T.label_for_ob(key)
        assert ob.name not in label and label != key


def test_estimate_hardness_by_perceived_category():
    assert T.estimate_hardness("sam_medium_range") == .85
    assert T.estimate_hardness("aaa_towed") == .85
    assert T.estimate_hardness("radar_acquisition") == .9
    assert T.estimate_hardness("mbt") == .7
    assert T.estimate_hardness("spg") == .85
    assert T.estimate_hardness("c2_node") == .9
    for other in ("depot_ammo", "supply_truck", "civilian_vehicle", "nope", None):
        assert T.estimate_hardness(other) == 1.0


def test_no_real_system_tokens_in_any_label_or_note():
    designators = [T.designator(c.side, c.prefix_slug, n)
                   for c in T.CLASSES.values() for n in (1, 2, 12)]
    assert_no_real_system_tokens({
        "classes": T.class_rows(_square_geo(1000.0)),
        "labels": [T.label_for_ob(k) for k in [*OB_LIBRARY, None, "x"]],
        "words": T.WORD, "category_labels": T.CATEGORY_LABEL,
        "designators": designators, "unit_ids": [T.unit_id("red", "sam", 1)],
        "notes": [T.NOTIONAL_NOTE, T.COVERS_CAVEAT, T.TABLE_VERSION],
        "templates": T.TEMPLATES,
    })


def test_token_helper_catches_every_token_and_passes_clean_text():
    leaks = ["S-300 battery", "an SA-6 site", "a Tor unit", "pantsir", "ZSU-23-4",
             "a ZU-23 mount", "the warhead", "munitions list", "proximity fuze",
             "fuzing options", "blast radius 50", "the aimpoint", "CEP 10",
             "a 500 kg item", "twin 23 mm cannon", "30mm"]
    for text in leaks:
        assert find_real_system_tokens({"k": [text]}), text
        with pytest.raises(AssertionError):
            assert_no_real_system_tokens(text)
    clean = ["Shooter active with ammunition", "Range 3.2 km of 20.0 km", "Red SAM 1",
             "aaa_towed_1759072800123001", "WG-3fa9c1-E7", "Air-defence guns",
             "torque", "Toronto", "a 2.5 km ring", "exception"]
    assert_no_real_system_tokens({"clean": clean, "set": set(clean), "tuple": tuple(clean)})
    # The ISR library's own names are exactly what must never leak into the wargame.
    for key in ("sam_long_range", "sam_medium_range", "sam_short_range", "spaag_missile",
                "aaa_self_propelled", "aaa_towed"):
        assert find_real_system_tokens(OB_LIBRARY[key].name), key
    # Keys and dataclass fields are walked too.
    assert find_real_system_tokens({"Pantsir": 1})
    bad = dataclasses.replace(T.CLASSES["ad_gun"], label="ZU-23 guns")
    assert find_real_system_tokens([bad])


# ---------------------------------------------------------------- AO fit
def test_ao_geometry_from_bbox_and_srv():
    lat, lon = 46.64, 32.61
    geo = T.ao_geometry_bbox(_box_around(lat, lon, 2940.0))
    assert geo["half_extent_m"] == pytest.approx(2940.0, rel=2e-3)
    assert geo["half_diagonal_m"] == pytest.approx(2940.0 * math.sqrt(2.0), rel=2e-3)

    class _Srv:
        theater = theaters.get("default")

    got = T.ao_geometry(_Srv())
    assert got["half_extent_m"] == pytest.approx(
        theater_plan.bbox_half_extent_m(_Srv.theater.bbox()), abs=0.1)
    assert got["half_diagonal_m"] > got["half_extent_m"]


def test_fit_and_cover_definitions():
    geo = _square_geo(2940.0)                    # quad AO, half-diagonal ~4158 m
    assert T.fits_ao(T.CLASSES["ad_manportable"], geo)
    assert T.fits_ao(T.CLASSES["ad_gun"], geo)
    assert not T.fits_ao(T.CLASSES["ad_short"], geo)
    assert T.covers_ao(T.CLASSES["ad_short"], geo)
    assert not T.fits_ao(T.CLASSES["radar_early_warning"], geo)     # no air envelope
    assert not T.covers_ao(T.CLASSES["radar_early_warning"], geo)
    assert T.class_caveats(T.CLASSES["ad_short"], geo) == [T.COVERS_CAVEAT]
    assert T.class_caveats(T.CLASSES["ad_gun"], geo) == []
    assert T.classes_that_fit(geo) == ["ad_gun", "ad_manportable"]
    rows = {r["key"]: r for r in T.class_rows(geo)}
    assert rows["ad_long"]["covers_ao"] and rows["ad_long"]["caveats"] == [T.COVERS_CAVEAT]


def test_class_fit_picks_manportable_in_a_quad_ao_and_medium_in_a_large_ao():
    assert T.choose_ad_class(_square_geo(2950.0)) == "ad_manportable"   # 5.9 km AO
    assert T.choose_ad_class(_square_geo(25000.0)) == "ad_medium"       # 50 km AO
    assert T.choose_ad_class(_square_geo(25000.0), T.AD_PREF[1:]) == "ad_short"
    tiny = _square_geo(1000.0)
    assert T.choose_ad_class(tiny) == T.AD_FALLBACK == "ad_gun"
    assert T.class_caveats(T.CLASSES["ad_gun"], tiny) == [T.COVERS_CAVEAT]
    assert T.choose_ad_class(tiny, ad_class="ad_long") == "ad_long"
    for bad in ("armour_company", "blue_artillery", "radar_early_warning", "nope"):
        with pytest.raises(ValueError):
            T.choose_ad_class(tiny, ad_class=bad)


# ---------------------------------------------------------------- templates
SPEC_COUNTS = {   # template: {slot: (side, low, medium, high)}
    "air_defence_belt": {"ad": ("red", 1, 2, 3), "radar": ("red", 0, 1, 1),
                         "guns": ("red", 1, 1, 2), "fires": ("blue", 1, 1, 1)},
    "mech_advance": {"armour": ("red", 1, 2, 3), "infantry": ("red", 1, 1, 2),
                     "cover": ("red", 1, 1, 1), "point": ("blue", 1, 1, 1),
                     "mech": ("blue", 1, 1, 2), "fires": ("blue", 1, 1, 1)},
    "strike_exercise": {"sam": ("red", 1, 1, 2), "radar": ("red", 1, 1, 1),
                        "cp": ("red", 1, 1, 1), "depot": ("red", 1, 1, 2),
                        "fires": ("blue", 1, 1, 1), "rockets": ("blue", 0, 1, 1),
                        "strike": ("blue", 1, 1, 1)},
}


def test_templates_match_the_spec_counts_and_classes():
    assert set(T.TEMPLATES) == set(SPEC_COUNTS)
    geo = _square_geo(25000.0)
    for name, slots in SPEC_COUNTS.items():
        assert [s.slot for s in T.TEMPLATES[name]] == list(slots)
        for level, intensity in enumerate(T.INTENSITIES, start=1):
            units = T.expand_template(name, intensity, geo)
            for slot, (side, *counts) in slots.items():
                mine = [u for u in units if u.slot == slot]
                assert len(mine) == counts[level - 1], (name, intensity, slot)
                assert all(u.side == side == T.CLASSES[u.wg_class].side for u in mine)
                assert [u.index for u in mine] == list(range(len(mine)))
    for slots in T.TEMPLATES.values():
        names = {s.slot for s in slots}
        for s in slots:
            assert s.anchor in T.ANCHORS
            assert all(c in T.CLASSES for c in s.classes)
            if s.objective is not None:
                assert s.objective in names and s.side == "red"
                target = next(o for o in slots if o.slot == s.objective)
                assert target.side == "blue" and target.classes == ("blue_defended_point",)
    with pytest.raises(ValueError):
        T.expand_template("mech_advance", "extreme", geo)
    with pytest.raises(KeyError):
        T.expand_template("no_such_template", "low", geo)


def test_template_ad_choice_follows_the_ao_and_the_override():
    small, large = _square_geo(2950.0), _square_geo(25000.0)
    ad = [u.wg_class for u in T.expand_template("air_defence_belt", "high", small)
          if u.slot == "ad"]
    assert ad == ["ad_manportable"] * 3
    assert {u.wg_class for u in T.expand_template("strike_exercise", "high", large)
            if u.slot == "sam"} == {"ad_medium"}
    cover = [u for u in T.expand_template("mech_advance", "low", large) if u.slot == "cover"]
    assert [u.wg_class for u in cover] == ["ad_short"]              # AD_PREF[1:]
    forced = T.expand_template("air_defence_belt", "medium", large, ad_class="ad_gun")
    assert {u.wg_class for u in forced if u.slot == "ad"} == {"ad_gun"}
    with pytest.raises(ValueError):
        T.expand_template("air_defence_belt", "low", large, ad_class="blue_rocket")


def test_offsets_cycle_and_each_full_cycle_adds_a_tenth(monkeypatch):
    slot = T.TemplateSlot("x", "red", ("ad_gun",), (5, 5, 5), "C", .30, (-90.0, 90.0))
    monkeypatch.setitem(T.TEMPLATES, "synthetic", (slot,))
    units = T.expand_template("synthetic", "low", _square_geo(5000.0))
    assert [(u.offset_deg, u.r) for u in units] == [
        (-90.0, .30), (90.0, .30), (-90.0, .40), (90.0, .40), (-90.0, .50)]


def test_destination_round_trips_with_haversine():
    for lat, lon, d, b in ((47.64, -122.14, 1617.0, 33.0), (-33.9, 151.2, 25000.0, 270.0),
                           (0.0, 179.99, 5000.0, 90.0)):
        lat2, lon2 = T.destination(lat, lon, d, b)
        assert haversine_m(lat, lon, lat2, lon2) == pytest.approx(d, abs=0.01)
        assert _angle_diff(bearing_deg(lat, lon, lat2, lon2), b) < 1e-6
        assert -180.0 <= lon2 < 180.0


def test_template_positions_are_relative_to_centre_and_home_only():
    centre = (46.64, 32.61)
    home = T.destination(*centre, 1500.0, 180.0)          # home 1.5 km south: beta = 0
    geo = T.ao_geometry_bbox(_box_around(*centre, 2940.0))
    half = geo["half_extent_m"]
    frame = T.template_frame(centre, home, half)
    assert _angle_diff(frame["beta"], 0.0) < 1e-6
    assert haversine_m(*centre, *frame["far"]) == pytest.approx(0.55 * half, abs=0.01)
    assert haversine_m(*home, *frame["near"]) == pytest.approx(0.35 * half, abs=0.01)
    assert frame["far"][0] > centre[0] and frame["near"][0] > home[0]
    rows = T.template_positions("strike_exercise", "high", centre, home, geo)
    assert len(rows) == len(T.expand_template("strike_exercise", "high", geo))
    for row, unit in zip(rows, T.expand_template("strike_exercise", "high", geo)):
        anchor = frame[unit.anchor]
        assert haversine_m(*anchor, row["lat"], row["lon"]) == pytest.approx(
            unit.r * half, abs=0.05)
        if unit.r:
            assert _angle_diff(bearing_deg(*anchor, row["lat"], row["lon"]),
                               frame["beta"] + unit.offset_deg) < 1e-4
    strike = next(r for r in rows if r["slot"] == "strike")
    assert (strike["lat"], strike["lon"]) == pytest.approx(home)
    json.dumps(rows, allow_nan=False)
    assert_no_real_system_tokens(rows)


def test_beta_follows_home_to_centre_and_is_zero_when_they_coincide():
    centre = (46.64, 32.61)
    west_home = T.destination(*centre, 2000.0, 270.0)       # home west: beta ~ 90
    assert _angle_diff(T.template_frame(centre, west_home, 2940.0)["beta"], 90.0) < 0.05
    near_home = T.destination(*centre, 150.0, 45.0)          # < 200 m: beta = 0
    assert T.template_frame(centre, near_home, 2940.0)["beta"] == 0.0
