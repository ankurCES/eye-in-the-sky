"""Theater table tests: one source of truth, demo actually flies (T1, M4).

Regression guard for the register finding "Reconcile the two THEATER tables —
the default theater's demo mission is geofence-rejected".
"""
import json

import pytest

from godseye_uav import theaters
from godseye_uav.missions import plan_mission
from godseye_uav.safety import FuelModel, SafetyEnvelope, haversine_m


def _envelope(t) -> SafetyEnvelope:
    return SafetyEnvelope(**t.envelope_kwargs())


def test_table_is_self_consistent():
    assert theaters.validate() == []


def test_lookup_api():
    assert theaters.get(None).id == theaters.DEFAULT_THEATER_ID
    assert theaters.get("iran-isfahan").label == "Iran — Isfahan"
    assert theaters.ids() == [t.id for t in theaters.all_theaters()]
    try:
        theaters.get("atlantis")
    except KeyError as exc:
        assert "atlantis" in str(exc) and "iran-isfahan" in str(exc)
    else:
        raise AssertionError("unknown theater must raise KeyError")


def test_table_covers_both_drifted_tables():
    """Union of the old launch.py and GEV panel ids — nobody loses a theater."""
    launcher = {"indo-pak-loc", "iran-isfahan", "taiwan-strait", "ukraine-donbas",
                "red-sea-hormuz", "default"}
    panel = {"default", "iran-isfahan", "iran-fordow", "iran-natanz",
             "indo-pak-loc", "taiwan-strait", "ukraine-donbas"}
    assert launcher | panel <= set(theaters.ids())


def test_isfahan_is_isfahan_and_natanz_is_separate():
    """The old launcher 'iran-isfahan' was actually Natanz, 115 km away."""
    isfahan = theaters.get("iran-isfahan")
    natanz = theaters.get("iran-natanz")
    assert haversine_m(isfahan.home_lat, isfahan.home_lon, 32.6546, 51.6680) < 5_000
    assert haversine_m(natanz.home_lat, natanz.home_lon, 33.7243, 51.7286) < 5_000
    apart = haversine_m(isfahan.home_lat, isfahan.home_lon,
                        natanz.home_lat, natanz.home_lon)
    assert apart > 100_000  # they are genuinely different places


def test_home_altitude_is_msl_and_converts_once():
    """T1: the stored datum is MSL; HAE comes from the single conversion point."""
    t = theaters.get("iran-isfahan")
    assert t.home == (t.home_lat, t.home_lon, t.home_alt_msl_m)
    hae = t.home_alt_hae_m()
    assert hae != t.home_alt_msl_m  # geoid undulation applied
    assert abs(hae - t.home_alt_msl_m) < 120.0  # EGM96 range is +-107 m
    assert theaters.as_payload()["alt_datum"] == "MSL"


def test_home_and_pois_inside_own_ao():
    for t in theaters.all_theaters():
        assert t.contains(*t.center()), t.id
        assert t.contains(t.home_lat, t.home_lon), t.id
        for poi in t.pois:
            assert t.contains(poi.lat, poi.lon), f"{t.id}/{poi.name}"


def test_poi_orbit_ring_stays_inside_geofence():
    """The panel draws a 150 m orbit ring — it must not break the geofence."""
    for t in theaters.all_theaters():
        env = _envelope(t)
        for poi in t.pois:
            wps = plan_mission("orbit_poi", "Drone1", lat=poi.lat, lon=poi.lon,
                               alt_m=60.0, radius_m=t.orbit_radius_m).waypoints
            assert env.check_route(wps) == [], f"{t.id}/{poi.name}"


def test_demo_mission_passes_geofence_and_bingo_gate():
    """The headline regression: the one-command demo must actually fly (M1/M4)."""
    for t in theaters.all_theaters():
        demo = t.demo_mission()
        plan = plan_mission("grid_search", "Drone1", polygon=demo["polygon"],
                            alt_m=demo["alt_m"])
        assert len(plan.waypoints) >= 4, t.id
        assert _envelope(t).check_route(plan.waypoints) == [], t.id

        fm = FuelModel()
        fm.home = t.home
        gate = fm.preflight_gate(plan.waypoints, t.home[:2], demo["speed_mps"])
        assert gate["ok"], f"{t.id}: BINGO gate rejected the demo: {gate}"


def test_default_theater_demo_is_not_geofence_rejected():
    t = theaters.get("default")
    assert all(t.contains(lat, lon) for lat, lon in t.demo_box())


def test_demo_targets_sit_inside_the_ao_at_ground_level():
    for t in theaters.all_theaters():
        targets = t.demo_targets()
        assert len(targets) == 3, t.id
        for tgt in targets:
            assert t.contains(tgt["lat"], tgt["lon"]), f"{t.id}/{tgt['name']}"
            assert tgt["alt_m"] == t.home_alt_msl_m  # ground, MSL datum (T1)


def test_point_at_offsets_in_metres():
    t = theaters.get("default")
    lat, lon = t.point_at(1000.0, 0.0)
    assert 990 < haversine_m(*t.center(), lat, lon) < 1010


def test_envelope_kwargs_build_a_working_envelope():
    t = theaters.get("ukraine-donbas")
    env = _envelope(t)
    assert env.home == t.home
    assert env.geofence == t.ao_list()
    assert env.check_point(*t.center(), 60.0) == []


def test_json_export_is_ui_consumable(tmp_path):
    payload = json.loads(theaters.to_json())
    assert payload["schema"] == theaters.SCHEMA
    assert payload["default"] == theaters.DEFAULT_THEATER_ID
    ids = [row["id"] for row in payload["theaters"]]
    assert ids == theaters.ids()
    row = payload["theaters"][0]
    assert len(row["home"]) == 3 and row["home_alt_datum"] == "MSL"
    assert all(len(v) == 2 for v in row["ao"])
    assert row["demo"]["alt_m_agl"] == theaters.DEMO_ALT_M_AGL
    assert row["place"] and row["description"]

    out = theaters.export_json(tmp_path / "ui" / "theaters.json")
    assert out.exists()
    assert json.loads(out.read_text())["theaters"][0]["id"] == ids[0]
    assert theaters.export_json(tmp_path).name == theaters.DEFAULT_EXPORT_NAME


# ------------------------------------------- adversarial verification (T1/M4)

# Independently sourced coordinates for the place each entry names, and how far
# the entry is allowed to sit from it. Guards the "anchored to a real place"
# claim itself: two comments were 2-3x out (Taichung 60 -> 125 km, Bandar Abbas
# 20 -> 70 km) while the table looked internally consistent.
REAL_PLACES = {
    "default": ("Microsoft campus, Redmond", 47.6396, -122.1283, 5_000),
    "iran-isfahan": ("Naqsh-e Jahan, Isfahan", 32.6575, 51.6776, 10_000),
    "iran-natanz": ("Natanz enrichment site", 33.7225, 51.7269, 5_000),
    "iran-fordow": ("Fordow enrichment site", 34.8847, 50.9961, 5_000),
    "indo-pak-loc": ("Srinagar, Kashmir", 34.0837, 74.7973, 15_000),
    "taiwan-strait": ("Taichung, Taiwan", 24.1477, 120.6736, 200_000),
    "ukraine-donbas": ("Bakhmut, Donetsk oblast", 48.5956, 37.9994, 20_000),
    "red-sea-hormuz": ("Bandar Abbas, Iran", 27.1833, 56.2667, 90_000),
}


def test_every_home_is_near_the_place_it_claims():
    for tid, (place, lat, lon, tol_m) in REAL_PLACES.items():
        t = theaters.get(tid)
        d = haversine_m(t.home_lat, t.home_lon, lat, lon)
        assert d <= tol_m, f"{tid}: {d/1000:.1f} km from {place}"


def test_home_alt_converts_to_hae_for_every_theater():
    """T1: one conversion point — but it has to work for all 8, not just one."""
    for t in theaters.all_theaters():
        hae = t.home_alt_hae_m()
        assert abs(hae - t.home_alt_msl_m) < 110.0, t.id   # EGM96 range


def test_demo_box_is_only_guaranteed_at_the_default_half_size():
    """demo_box() is never silently clamped: a caller-supplied half_m can leave
    the AO, and nothing warns.

    Sized off each AO rather than off one magic constant, so the property is
    still tested after an AO is resized (the `default` AO was enlarged from
    1.1 x 0.75 km, which made a fixed 500 m half-box fit everywhere and would
    have quietly turned this assertion into a tautology).
    """
    for t in theaters.all_theaters():
        assert all(t.contains(la, lo) for la, lo in t.demo_box()), t.id
        min_lat, _min_lon, max_lat, _max_lon = t.bounds()
        # Twice the AO's own half-height: a box no AO can hold.
        oversize = (max_lat - min_lat) * 111_320.0
        assert not all(t.contains(la, lo) for la, lo in t.demo_box(oversize)), t.id


def test_the_launcher_theater_id_the_demo_script_uses_is_documented():
    """scripts/demo_mission.py calls the Redmond theater "redmond"; this table
    calls it "default". A mechanical port of that file would KeyError."""
    assert "redmond" not in theaters.ids()
    assert theaters.get("default").place.startswith("Redmond")


# --------------------------------------------- declared ground vs real terrain

#: Terrain measured at each theater's EXACT home coordinate, offline.
#:
#: `hae_m` is the ellipsoidal height served by the God's Eye View terrain proxy
#: (`GET /api/terrain/heights`, upstream Re:Earth / Mapterhorn), recorded from
#: its own on-disk cache (`gods-eye-view/.gev-cache/terrain-heights.json`) so
#: this test needs no network and no dev server. It is the RAW upstream number:
#: the MSL the assertion compares against is derived from it by
#: `realdata.TerrainProvider` through `geo.canonical_altitude` (EGM96, T1) —
#: the same one conversion point the running stack uses.
#:
#: `crosscheck_msl_m` is the same point from an INDEPENDENT DEM (Copernicus
#: GLO-90 via the Open-Meteo elevation API, orthometric). It is here so the
#: fixture can be audited rather than trusted: two unrelated DEMs agreeing to a
#: few metres is what makes "the table is wrong" the only remaining reading.
MEASURED_TERRAIN = {
    "default":        {"hae_m":   89.350, "crosscheck_msl_m":  119.0},
    "iran-isfahan":   {"hae_m": 1579.434, "crosscheck_msl_m": 1579.0},
    "iran-natanz":    {"hae_m": 1294.984, "crosscheck_msl_m": 1298.0},
    "iran-fordow":    {"hae_m":  904.141, "crosscheck_msl_m":  906.0},
    "indo-pak-loc":   {"hae_m": 1555.956, "crosscheck_msl_m": 1587.0},
    "taiwan-strait":  {"hae_m":   15.012, "crosscheck_msl_m":    0.0},
    "ukraine-donbas": {"hae_m":  223.389, "crosscheck_msl_m":  203.0},
    "red-sea-hormuz": {"hae_m":  -30.696, "crosscheck_msl_m":    0.0},
}

#: How far a declared `home_alt_msl_m` may sit from measured ground.
#:
#: The table's own comment calls these "approximate terrain elevation ... good
#: enough for an AirSim OriginGeopoint, not a survey", and every row that has
#: ever been measured honestly lands inside 41.1 m (ukraine-donbas, the widest).
#: 50 m keeps that slack and still catches the two rows this guard was written
#: for: iran-natanz was out by 286.5 m and iran-fordow by 647.2 m.
GROUND_TOLERANCE_M = 50.0


def _recorded_terrain_fetch(points):
    """A `realdata` fetch that serves ONLY the recorded points, offline.

    Every other point comes back with no height, so the provider takes its
    flagged synthetic fallback there. Nothing in this fixture can invent a
    measurement for a coordinate that was never recorded.
    """
    import json
    from urllib.parse import parse_qs, urlparse

    from godseye_uav.realdata import HttpResponse

    keyed = {(round(lat, 5), round(lon, 5)): hae for (lat, lon), hae in points.items()}

    def fetch(url, timeout_s):
        parsed = urlparse(url)
        if parsed.path != "/api/terrain/heights":
            return HttpResponse(404, json.dumps({"error": "not recorded"}), {})
        results = []
        for pair in parse_qs(parsed.query)["points"][0].split(";"):
            lon, lat = (float(v) for v in pair.split(","))
            hae = keyed.get((round(lat, 5), round(lon, 5)))
            results.append({"lon": lon, "lat": lat, "ellipsoid": hae,
                            "geoid": None, "elevation": hae})
        return HttpResponse(200, json.dumps({"results": results}), {})

    return fetch


def test_declared_ground_matches_measured_terrain():
    """`home_alt_msl_m` is the datum the whole sim runs on — the AirSim
    OriginGeopoint, `home_alt_hae_m()`, the envelope's ceiling and min-AGL
    tests, and `sim_spawn_target`'s default altitude. A row that is hundreds of
    metres out is not a cosmetic error: it buries or floats the entire order of
    battle and moves the geofence's vertical reference under a flying mission.

    Measured against real recorded terrain at each home point, through the
    project's own hydration path (`Theater.hydrate()` ->
    `TheaterRealData.terrain_delta_m()`), which is what `realdata.py` was built
    to report and what nothing was reading.

    FAILS ON THE PRE-FIX TABLE by 286.5 m (iran-natanz, which carried the
    elevation of Natanz TOWN, 30 km away and 357 m higher) and 647.2 m
    (iran-fordow).
    """
    from godseye_uav.realdata import RealWorldData

    assert set(MEASURED_TERRAIN) == set(theaters.ids()), (
        "every theater needs a recorded ground truth, or a new row can be "
        "added with an unmeasured altitude and this guard would not notice")

    theaters.clear_hydration()
    try:
        deltas = {}
        for tid, row in MEASURED_TERRAIN.items():
            t = theaters.get(tid)
            fetch = _recorded_terrain_fetch({(t.home_lat, t.home_lon): row["hae_m"]})
            client = RealWorldData(origin="http://recorded", fetch=fetch)
            hydrated = t.hydrate(client)
            assert hydrated.ground.real is True, (
                f"{tid}: the fixture did not actually measure the home point")
            measured_msl = hydrated.ground.msl_m

            # The fixture is auditable: an unrelated DEM agrees with the one
            # recorded here, so a delta below can only be the TABLE's error.
            assert abs(measured_msl - row["crosscheck_msl_m"]) < 15.0, (
                f"{tid}: the two DEMs disagree ({measured_msl:.1f} m vs "
                f"{row['crosscheck_msl_m']:.1f} m) — re-record the fixture "
                "before trusting any verdict it gives about the table")

            delta = hydrated.terrain_delta_m()
            assert delta is not None, f"{tid}: nothing was measured"
            deltas[tid] = delta
            assert abs(delta) <= GROUND_TOLERANCE_M, (
                f"{tid}: declared home_alt_msl_m={t.home_alt_msl_m} m is "
                f"{delta:+.1f} m from terrain measured at its own home point "
                f"({measured_msl:.1f} m MSL); tolerance is "
                f"{GROUND_TOLERANCE_M} m")
        # The two sea-level theaters are the sanity check on the whole method:
        # if THEY drifted, the terrain model would be the suspect, not the table.
        assert abs(deltas["taiwan-strait"]) < 1.0
        assert abs(deltas["red-sea-hormuz"]) < 1.0
    finally:
        theaters.clear_hydration()


# -------------------------------------------- the demo laydown is observable

def _identify_plan_for(target):
    """The real `identify_plan` for a demo contact, classified as the server
    would classify it (`targets.match_ob` on the spawned NAME).

    LOS is stubbed clear on purpose: this is a geometry question about the AO,
    and a terrain-blocked arc would confound it.
    """
    import time

    from godseye_uav.missions import identify_plan
    from godseye_uav.targets import Track, match_ob

    ob, _evidence = match_ob(target["name"])
    now = time.time()
    track = Track(track_id="TRK-probe", name=target["name"], category=ob.category,
                  lat=target["lat"], lon=target["lon"], alt_m=target["alt_m"],
                  first_seen=now, last_seen=now, ob_class=ob.key)
    return ob, identify_plan("Drone1", track, alt_agl_m=theaters.DEMO_ALT_M_AGL,
                             los_check=lambda *_a: {"los": True,
                                                    "first_obstacle": None,
                                                    "model": "test-stub"})


def test_every_demo_contact_can_be_identified_from_inside_its_own_ao():
    """A laydown that can be spawned but never identified is not a demo.

    `demo_targets()` names the contacts, the name fixes the order-of-battle
    class, the class fixes the M5 standoff ring, and `identify_plan` puts every
    waypoint on it. Two separate defects made that impossible:

      * "SA-6_site_1" classifies as `sam_medium_range` — a 24 km engagement
        envelope, so a 26.4 km standoff. NO AO in this table is within an order
        of magnitude of that (the widest reaches ~9 km), so the flagship demo
        contact could not be identified in ANY theater.
      * the `default` AO reached only ~800 m from its own contacts — under the
        1290 m DETECT ring of any vehicle-sized object, never mind a threat
        ring — so all three of its demo contacts were rejected at every
        waypoint.

    Asserts the OUTCOME the operator gets: `SafetyEnvelope.check_route`, the
    same gate the server runs, reports no geofence violation. An earlier draft
    of this test compared the plan's widest radius against the furthest AO
    VERTEX, which is the metric `missions.standoff_vs_ao` uses to EXPLAIN a
    rejection — it is satisfied by a ring that still pokes out through an
    edge, and a live dry-run on the real stack duly still returned
    `wp4:geofence`. The polygon test is the real answer.
    """
    for t in theaters.all_theaters():
        env = _envelope(t)
        for target in t.demo_targets():
            ob, plan = _identify_plan_for(target)
            violations = [v for v in env.check_route(plan.waypoints)
                          if "geofence" in v]
            assert violations == [], (
                f"{t.id}/{target['name']}: classified {ob.key}; its identify "
                f"pass leaves the AO at {violations} — M14 forbids closing "
                "inside the ring to fix it, so the demo simply cannot be flown")


def test_no_demo_contact_is_standing_off_beyond_what_it_can_be_seen_at():
    """Standing off from something the camera then cannot resolve is not an
    ISR pass, it is an expensive orbit. `identify_plan` already detects it and
    warns `detect_marginal` / `id_not_achievable`; the shipped laydown must not
    trip either. A 4.6 m towed AAA piece at its own 2.2 km threat ring is
    1.2 px in the wide field, under the 3 px floor — which is why the first
    replacement for the SA-6 was rejected too."""
    for t in theaters.all_theaters():
        for target in t.demo_targets():
            _ob, plan = _identify_plan_for(target)
            bad = [w for w in (plan.warnings or [])
                   if w.startswith(("detect_marginal", "id_not_achievable"))]
            assert bad == [], f"{t.id}/{target['name']}: {bad}"
            assert plan.meta["id_achievable"] is True, f"{t.id}/{target['name']}"


#: Metres of route an identify pass may fly for a demo contact.
#:
#: The pass flies two rings, so its length is ~4*pi*standoff and grows fast:
#: the old T-72 row was a 20.8 km sortie and the towed-AAA row 26.6 km.
#: Measured live against the shipped stack (`mission_identify_target
#: dry_run=True`, default theater), the three rows below need 61-81% of the
#: tank against 99% available, while the T-72 needed 128% and the AAA 158% —
#: i.e. BINGO, which is real now, ends them before they finish.
#:
#: Stated as ROUTE METRES rather than fuel percent on purpose: the fuel model
#: is tuned elsewhere, and this guard is about the laydown's geometry, which is
#: what `demo_targets()` actually controls. 14 km leaves room for the fuel
#: model to move either way without this test voting on it.
DEMO_IDENTIFY_ROUTE_MAX_M = 14_000.0


def test_the_demo_identify_pass_is_short_enough_for_the_aircraft_to_finish():
    """Geofence-legal is not the same as flyable. An identify pass is two
    orbits at the standoff, so a doctrinally-correct ring can still be a sortie
    the airframe cannot complete — and a mission that BINGOs out halfway is not
    a demo either."""
    from godseye_uav.missions import haversine_m

    for t in theaters.all_theaters():
        for target in t.demo_targets():
            ob, plan = _identify_plan_for(target)
            wps = plan.waypoints
            route_m = sum(haversine_m(wps[i]["lat"], wps[i]["lon"],
                                      wps[i + 1]["lat"], wps[i + 1]["lon"])
                          for i in range(len(wps) - 1))
            assert route_m <= DEMO_IDENTIFY_ROUTE_MAX_M, (
                f"{t.id}/{target['name']}: classified {ob.key}, whose identify "
                f"pass is a {route_m / 1000:.1f} km route — past the "
                f"{DEMO_IDENTIFY_ROUTE_MAX_M / 1000:.0f} km the demo airframe "
                "can be expected to finish")


def test_the_demo_laydown_still_exercises_a_real_threat_ring():
    """The cheap way to pass the tests above is a laydown of contacts with no
    engagement envelope at all, which quietly retires the M5 standoff machinery
    from the demo. At least one demo contact's standoff must come from its OWN
    envelope rather than from the `MIN_STANDOFF_M` floor."""
    from godseye_uav.targets import match_ob
    from godseye_uav.threat import MIN_STANDOFF_M, standoff_m

    t = theaters.get("default")
    rings = {}
    for target in t.demo_targets():
        ob, _evidence = match_ob(target["name"])
        rings[ob.key] = ob.weapon_range_m
    driven = [k for k, r in rings.items() if r > 0.0 and
              max(MIN_STANDOFF_M, r * 1.1) > MIN_STANDOFF_M]
    assert driven, (
        f"every demo contact falls back to the {MIN_STANDOFF_M} m floor, so "
        f"the M5 standoff derivation is never exercised: {rings}")
    # ...and it really does move the geometry, not just exist as a number.
    for target in t.demo_targets():
        ob, _evidence = match_ob(target["name"])
        if ob.key in driven:
            assert standoff_m(_track_like(target, ob)) > MIN_STANDOFF_M


def _track_like(target, ob):
    import time

    from godseye_uav.targets import Track

    now = time.time()
    return Track(track_id="TRK-probe", name=target["name"], category=ob.category,
                 lat=target["lat"], lon=target["lon"], alt_m=target["alt_m"],
                 first_seen=now, last_seen=now, ob_class=ob.key)


# --------------------------------------------------- the ACTIVE theater block

def test_as_payload_never_lets_the_table_default_pass_for_the_active_theater():
    """INTEGRATION_FINDINGS UI-1. `default` is the TABLE default; a consumer
    reading it as "the theater that is flying" showed Redmond POIs for an
    aircraft over Isfahan. The payload now always carries a separate `active`
    block, and with nothing to consult it says so instead of being absent."""
    payload = theaters.as_payload()
    active = payload["active"]
    assert sorted(active) == sorted(theaters.ACTIVE_KEYS)
    assert active["known"] is False
    assert active["id"] is None           # NOT theaters.DEFAULT_THEATER_ID
    assert active["reason"]
    assert payload["default"] == theaters.DEFAULT_THEATER_ID
    assert payload["default_note"]

    supplied = theaters.active_from_server(
        {"id": "iran-isfahan", "label": "Iran — Isfahan",
         "ground_elevation_msl_m": 1570.0,
         "ao": theaters.get("iran-isfahan").ao_list()},
        source="mcp:uav://safety/geofence", at_ms=1_700_000_000_000)
    carried = theaters.as_payload(active=supplied)["active"]
    assert carried["known"] is True
    assert carried["id"] == "iran-isfahan"
    assert carried["in_table"] is True
    assert carried["ground_elevation_msl_m"] == 1570.0
    # the whole point: what is FLYING is not what the table defaults to
    assert carried["id"] != theaters.as_payload()["default"]


def test_active_block_refuses_to_be_unknown_without_a_reason():
    """An unexplained "theater: unknown" on a HUD is indistinguishable from
    "this field was never wired up", and the operator cannot act on either."""
    with pytest.raises(ValueError, match="reason"):
        theaters.active_unknown("")
    with pytest.raises(ValueError, match="reason"):
        theaters.active_unknown("   ")


def test_a_server_theater_with_no_id_is_unknown_not_the_default():
    """The silent-fallback trap: a malformed theater block must NOT resolve to
    the table default, which would be a confident wrong place."""
    out = theaters.active_from_server({"label": "somewhere"},
                                      source="mcp:test", at_ms=5)
    assert out["known"] is False and out["id"] is None
    assert "theater id" in out["reason"]
    assert out["source"] == "mcp:test"


def test_a_theater_this_table_does_not_have_is_published_and_flagged():
    """A server running a theater the bridge has never heard of is still
    published — the operator needs to know WHERE the aircraft is — but
    `in_table` says the local table cannot draw it."""
    out = theaters.active_from_server(
        {"id": "mars-olympus", "label": "Mars", "ground_elevation_msl_m": 21_000.0},
        source="mcp:test", at_ms=7)
    assert out["known"] is True and out["id"] == "mars-olympus"
    assert out["in_table"] is False
    assert out["ao"] is None          # no polygon offered, none invented


def test_the_active_block_cannot_be_mutated_through_the_payload():
    """`as_payload` deep-copies the block in. A caller editing what it got back
    must not be editing the publisher's own state — including through the
    NESTED members, which a shallow copy would still share."""
    supplied = theaters.active_from_server(
        {"id": "iran-natanz", "label": "Natanz",
         "ground_elevation_msl_m": 1293.0,
         "ao": theaters.get("iran-natanz").ao_list()},
        source="mcp:test", at_ms=1,
        theater_mismatch={"theater_id": "default"})
    payload = theaters.as_payload(active=supplied)
    payload["active"]["id"] = "tampered"
    payload["active"]["ao"][0][0] = 0.0
    payload["active"]["theater_mismatch"]["theater_id"] = "tampered"
    assert supplied["id"] == "iran-natanz"
    assert supplied["ao"][0][0] != 0.0
    assert supplied["theater_mismatch"]["theater_id"] == "default"


def test_a_partly_unusable_theater_block_says_what_it_dropped():
    """The id is good, so WHERE the aircraft is stays publishable — but a field
    the server did send and the bridge could not use must not come back as a
    bare null, which reads as 'the server never sent it'."""
    out = theaters.active_from_server(
        {"id": "iran-natanz", "ground_elevation_msl_m": "not a number",
         "ao": [[1.0, 2.0], "broken", [3.0, 4.0]]},
        source="mcp:test", at_ms=9)
    assert out["known"] is True and out["id"] == "iran-natanz"
    assert out["ground_elevation_msl_m"] is None
    assert out["ao"] is None
    assert "ground_elevation_msl_m" in out["reason"]
    assert "ao polygon" in out["reason"]


def test_a_field_the_server_never_sent_is_not_reported_as_complete():
    """The sibling of the test above, and the hole it left.

    `active_from_server` flagged a field that arrived BROKEN but said nothing
    about one that never arrived: a block with no `ao` came back
    `ao: null, reason: ""` — a block vouching for its own completeness while
    carrying no polygon. `null` is the same published value either way, so
    `reason` is the only thing that can separate "the server never sent it"
    from "the server sent it and it was junk", and a consumer deciding whether
    to redraw a geofence or to alarm needs to know which.

    FAILS ON THE PRE-FIX CODE: `reason` was the empty string.
    """
    out = theaters.active_from_server({"id": "iran-natanz"},
                                      source="mcp:test", at_ms=9)
    assert out["known"] is True and out["id"] == "iran-natanz"
    assert out["ao"] is None and out["ground_elevation_msl_m"] is None
    assert out["reason"] != "", (
        "a block with neither an AO nor a ground elevation declared itself "
        "complete")
    assert "ao" in out["reason"] and "ground_elevation_msl_m" in out["reason"]
    assert "did not send" in out["reason"]

    # ...and a complete block still says nothing, or the field is just noise.
    full = theaters.active_from_server(
        {"id": "iran-natanz", "label": "Natanz", "ground_elevation_msl_m": 1293.0,
         "ao": theaters.get("iran-natanz").ao_list()},
        source="mcp:test", at_ms=9)
    assert full["reason"] == ""


def test_a_bool_is_not_accepted_as_a_ground_elevation():
    """`bool` is an `int` subclass, so the `float()` cast turned `True` into a
    finite 1.0 and published it with an empty `reason` vouching for it — a
    theater at 1570 m MSL served to the HUD as 1 m. A cast that cannot fail is
    not a validator.

    FAILS ON THE PRE-FIX CODE: ground came back 1.0 with reason "".
    """
    out = theaters.active_from_server({"id": "iran-isfahan",
                                       "ground_elevation_msl_m": True},
                                      source="mcp:test", at_ms=9)
    assert out["ground_elevation_msl_m"] is None, (
        f"a bool was laundered into {out['ground_elevation_msl_m']!r} m MSL")
    assert "bool" in out["reason"]


def test_active_from_server_does_not_alias_the_callers_theater_mismatch():
    """`bridge.MissionFeed.active_theater()` passes the `theater_mismatch` out
    of its OWN cached `uav://safety/geofence` document, and `GET /health`
    returns the resulting block directly — it never goes through
    `as_payload`'s copy. Storing the reference handed a route's return value a
    live alias into the feed's state, which is precisely the trap
    `as_payload` was already deep-copying to avoid, one level further up.

    FAILS ON THE PRE-FIX CODE: the caller's dict was mutated.
    """
    cached = {"theater_id": "default", "note": "as the server said it"}
    out = theaters.active_from_server({"id": "iran-natanz"}, source="mcp:test",
                                      at_ms=1, theater_mismatch=cached)
    out["theater_mismatch"]["note"] = "tampered"
    out["theater_mismatch"]["theater_id"] = "tampered"
    assert cached == {"theater_id": "default", "note": "as the server said it"}, (
        "editing the published block edited the bridge's cached geofence doc")


def test_as_payload_refuses_a_partial_active_block():
    """`as_payload` deep-copied whatever it was handed and published it. An
    empty or half-built block therefore went out as the `active` key, and every
    consumer resolves the theater the same way — `active.get("id") or
    payload["default"]` — so it lands back on the TABLE default: UI-1, wearing
    the shape of the fix for it. A validator that accepts the value which
    disables the feature it validates is not a validator.

    FAILS ON THE PRE-FIX CODE: each of these was published verbatim.
    """
    import pytest

    for bad, why in (
            ({}, "an empty block"),
            ({"known": True}, "known with nothing under it"),
            ({**theaters.active_unknown("x"), "id": None, "known": True},
             "known=True with a null id"),
    ):
        with pytest.raises((ValueError, TypeError)):
            theaters.as_payload(active=bad)
        # and the consumer's own reach is what makes it dangerous
        assert (bad.get("id") or theaters.DEFAULT_THEATER_ID) == \
            theaters.DEFAULT_THEATER_ID, why

    # A block that says unknown without saying why is equally unpublishable.
    silent = {**theaters.active_unknown("placeholder"), "reason": ""}
    with pytest.raises(ValueError):
        theaters.as_payload(active=silent)

    # ...while both well-formed blocks still go through untouched.
    assert theaters.as_payload(
        active=theaters.active_unknown("no server"))["active"]["known"] is False
    good = theaters.active_from_server({"id": "iran-natanz"}, source="s", at_ms=1)
    assert theaters.as_payload(active=good)["active"]["id"] == "iran-natanz"


def test_validate_catches_drift():
    """A hand-edited theater whose AO no longer contains its home is rejected."""
    good = theaters.get("default")
    drifted = theaters.Theater(
        id="drifted", label="Drifted", place="nowhere", description="test",
        home_lat=good.home_lat + 1.0, home_lon=good.home_lon, home_alt_msl_m=0.0,
        ao=good.ao, pois=good.pois,
    )
    problems = theaters.validate([drifted])
    assert any("outside its AO" in p for p in problems)
