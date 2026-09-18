"""Theater table — the ONE source of truth for home, AO and demo geometry.

The theater definitions lived in three hand-typed copies (`launch.py`
THEATERS, `scripts/demo_mission.py` THEATERS and the GEV panel
`src/ui/uavMissionPanel.js` THEATERS) which had already drifted: the two
"iran-isfahan" entries were ~115 km apart with fully disjoint AOs, the demo
box and the launcher AO disagreed, and the out-of-the-box demo mission was
therefore geofence-rejected by `SafetyEnvelope.check_route`. This table is
the replacement, and the browser UI consumes the same rows as JSON (see
`as_payload` / `export_json`).

WHAT IS ACTUALLY WIRED TO THIS TABLE (re-verified, not inherited)
-----------------------------------------------------------------
* `launch.py` — `--theater` choices are `theaters.ids()`, the row is resolved
  with `theaters.get()` and handed to `build_server(theater=t)`; the envelope
  is `envelope_kwargs()`. It keeps no table of its own.
* `server.py` — `GodseyeUavServer.theater` IS a row from here; the geofence
  resource, the INTREP area name, the POI seed and `sim_spawn_target`'s
  default altitude all read it.
* `scripts/demo_mission.py` — `demo_mission()` / `demo_targets()`.
* `bridge.py` — serves this table at `GET /theaters`, and (see ACTIVE THEATER
  below) publishes which row the running server actually chose.
* The GEV panel fetches `GET /theaters` and prints its provenance
  ("theaters: bridge · godseye.theaters/v1"). It still ships a copy in
  `src/ui/uavTheaters.js`, but as a *labelled offline fallback* in the served
  payload's own shape, not a fourth independent table.

Note that the old demo id "redmond" is "default" here.

ACTIVE THEATER. `as_payload()["default"]` is the **table's** default row
("default" / Redmond). It is NOT a statement about what is flying: a stack
started with `--theater iran-isfahan` still has `default: "default"` here,
because this module cannot see a running server. The bridge answers that
question instead — `as_payload(active=...)` fills an `active` block, and
`bridge.MissionFeed.active_theater()` derives it from the server's own
`uav://safety/geofence`. A consumer that reads `default` as "the current
theater" is one click from seeding a target 10,000 km from the aircraft,
which is exactly what INTEGRATION_FINDINGS.md UI-1 recorded; `active` exists
so that consumer has a truthful field to read, and says `known: false` with a
reason rather than falling back to the table default.

REAL-WORLD HYDRATION. Every number in the table below is a static,
hand-entered anchor and nothing requires the network. A theater can
additionally *hydrate* from real data — measured terrain elevation, mapped
military sites as a plausible order of battle, live air traffic in the AO, and
the actual current weather and wind at that place — via `Theater.hydrate()` /
`hydrate_all()`, which delegate to `realdata.py`. Three rules hold there:

* Hydration is a **startup / background-thread** call. It blocks on the network,
  so nothing on a mission or telemetry path may call it; `Theater.real_data()`
  is the non-blocking read of whatever was last learned, and returns `None`
  before the first hydration rather than inventing a value.
* **STILL UNWIRED, and deliberately said out loud.** Nothing in `server.py`,
  `bridge.py`, `launch.py` or `scripts/` calls `hydrate()`, so on a live stack
  every theater row serves `real_data: {"hydrated": false, "source":
  "static-table"}` and the mission flies on flat-world quantities:
  `uav_get_telemetry` reports height above the launch datum, `uav_los_check`
  runs `fake_airsim.line_of_sight` (a geometric earth-curvature horizon, no
  terrain), the safety envelope has no terrain floor, and the fuel model's wind
  comes from `UavBackend.wind_ne()` — the sim, or an operator's `set_wind` —
  never from the theater's actual current weather. The ingestion layer below is
  real and exercised — see `python -m godseye_uav.theaters --hydrate` and
  `tests/test_realdata.py` — but it is a *source with no consumer*. Read "real
  terrain" here as "available", not "in use".
* Hydration **never rewrites this table**. `TheaterRealData.terrain_delta_m()`
  reports how far the static `home_alt_msl_m` sits from the measured ground, and
  that is all it does: silently moving a running geofence's datum underneath it
  would be worse than the discrepancy. Every hydrated value carries a
  `Provenance` saying whether it is real and, if not, why.

  That reporting is only useful if somebody reads it, so
  `tests/test_theaters.py::test_declared_ground_matches_measured_terrain`
  pins every declared `home_alt_msl_m` against terrain measured at that exact
  home point, offline, from a recorded fixture. It caught two rows that were
  hundreds of metres out (see `iran-natanz` and `iran-fordow` below).

ALTITUDE DATUM (T1): `home_alt_msl_m` is **metres above mean sea level
(MSL)** — the same datum AirSim's `settings.json` OriginGeopoint is entered
in. It is NOT height above the WGS84 ellipsoid. Convert exactly once, at the
bridge, with `geo.msl_to_hae()` (or `Theater.home_alt_hae_m()`), and publish
only altHae downstream (T1). Waypoint altitudes in this module are metres
**AGL**, which is what the safety envelope's ceiling/min-AGL test expects.

Every theater is anchored to a real place; the comment on each entry says
which. ISR-only (M14): a theater is an area to observe — nothing here
describes or supports a strike.
"""
from __future__ import annotations

import json
import math
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .safety import point_in_polygon

if TYPE_CHECKING:  # import-time cost and cycle risk stay out of the hot path
    from .realdata import RealWorldData, TheaterRealData

# Demo/default flight profile. 60 m AGL sits under the 120 m AGL default
# ceiling (M4/safety envelope) and keeps the grid-search swath sane (M1).
DEMO_ALT_M_AGL = 60.0
DEMO_SPEED_MPS = 8.0
DEMO_BOX_HALF_M = 150.0          # ~300 m square: always inside every AO below
DEFAULT_ORBIT_RADIUS_M = 150.0   # POI orbit ring the GEV panel draws
SCHEMA = "godseye.theaters/v1"

#: Terrain samples per AO edge when building a geofence floor. 5x5 over the
#: bounding box plus the AO vertices is 29 points — one terrain request, well
#: inside the proxy's per-request cap.
FLOOR_GRID = 5

#: Default terrain clearance for the geofence FLOOR, metres AGL. Matches the
#: demo profile's altitude so a hydrated floor never invalidates the demo.
FLOOR_CLEARANCE_M_AGL = 60.0

#: Radius around home searched for real air traffic to deconflict against.
TRAFFIC_RADIUS_M = 50_000.0

#: Said on every un-hydrated theater row, so a consumer can never mistake the
#: static table for measured data.
STATIC_TABLE_NOTE = (
    "offline default: home altitude and AO are hand-entered anchors, not measured "
    "terrain; call Theater.hydrate() to ingest real data"
)

_M_PER_DEG_LAT = 111_320.0


def _m_per_deg_lon(lat_deg: float) -> float:
    return 111_320.0 * math.cos(math.radians(lat_deg))


@dataclass(frozen=True)
class Poi:
    """A named point inside the AO the panel can orbit (PLAN §4.1 orbit_poi)."""

    name: str
    lat: float
    lon: float

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "lat": self.lat, "lon": self.lon}


@dataclass(frozen=True)
class Theater:
    """One AO: home, geofence polygon and the demo geometry derived from it.

    `ao` is `[(lat, lon), ...]` — the format `safety.point_in_polygon` and
    `SafetyEnvelope.geofence` take. `home_alt_msl_m` is MSL (T1, see module
    docstring).
    """

    id: str
    label: str
    place: str            # the real-world location this is anchored to
    description: str      # what the theater is for (ISR framing, M14)
    home_lat: float
    home_lon: float
    home_alt_msl_m: float
    ao: tuple[tuple[float, float], ...]
    pois: tuple[Poi, ...] = ()
    orbit_radius_m: float = DEFAULT_ORBIT_RADIUS_M

    # ---- home ----
    @property
    def home(self) -> tuple[float, float, float]:
        """(lat, lon, alt_MSL_m) — drop-in for `SafetyEnvelope.home` (T1)."""
        return (self.home_lat, self.home_lon, self.home_alt_msl_m)

    def home_alt_hae_m(self) -> float:
        """Home altitude converted to HAE at the single conversion point (T1)."""
        from .geo import msl_to_hae

        return msl_to_hae(self.home_alt_msl_m, self.home_lat, self.home_lon)

    # ---- geometry ----
    def ao_list(self) -> list[tuple[float, float]]:
        """Geofence polygon as a mutable list (SafetyEnvelope wants a list)."""
        return [tuple(v) for v in self.ao]

    def center(self) -> tuple[float, float]:
        """Centroid of the AO vertices — the most geofence-clear point."""
        return (sum(v[0] for v in self.ao) / len(self.ao),
                sum(v[1] for v in self.ao) / len(self.ao))

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_lat, min_lon, max_lat, max_lon) of the AO."""
        lats = [v[0] for v in self.ao]
        lons = [v[1] for v in self.ao]
        return (min(lats), min(lons), max(lats), max(lons))

    def contains(self, lat: float, lon: float) -> bool:
        """Geofence test using the same ray-cast the server enforces."""
        return point_in_polygon(lat, lon, self.ao_list())

    def point_at(self, north_m: float, east_m: float,
                 ref: tuple[float, float] | None = None) -> tuple[float, float]:
        """Offset (north, east) metres from `ref` (default: the AO centre)."""
        lat0, lon0 = ref if ref is not None else self.center()
        return (lat0 + north_m / _M_PER_DEG_LAT,
                lon0 + east_m / _m_per_deg_lon(lat0))

    def ring(self, lat: float, lon: float, radius_m: float,
             points: int = 8) -> list[tuple[float, float]]:
        """`points` samples on a circle — used to prove an orbit stays inside."""
        return [self.point_at(radius_m * math.cos(2 * math.pi * i / points),
                              radius_m * math.sin(2 * math.pi * i / points),
                              ref=(lat, lon))
                for i in range(points)]

    # ---- demo mission (must pass geofence + BINGO, M4) ----
    def demo_box(self, half_m: float = DEMO_BOX_HALF_M) -> list[tuple[float, float]]:
        """Square search polygon centred on the AO — the demo's own box.

        Centred on the centroid rather than on home so it is inside the
        geofence for every theater in the table (the register finding
        "the default theater's demo mission is geofence-rejected").

        Only the default `half_m` is guaranteed: `validate()` checks that box
        and nothing else. A caller passing its own `half_m` must re-check with
        `contains()` — the box is never silently clamped to the AO, so a
        half-box wider than the AO simply comes back outside it.
        """
        return [self.point_at(dn * half_m, de * half_m)
                for dn, de in ((-1, -1), (-1, 1), (1, 1), (1, -1))]

    def demo_mission(self, alt_m_agl: float = DEMO_ALT_M_AGL,
                     half_m: float = DEMO_BOX_HALF_M) -> dict[str, Any]:
        """The one-command demo's grid search, consistent with this AO (M1/M4)."""
        return {
            "kind": "grid_search",
            "polygon": self.demo_box(half_m),
            "alt_m": alt_m_agl,
            "speed_mps": DEMO_SPEED_MPS,
        }

    def demo_targets(self) -> list[dict[str, Any]]:
        """Scenario laydown for the demo: three observable objects in the AO.

        Placed AO-relative (not home-relative) so they are always inside the
        geofence and inside the demo box's sensor footprint. Ground altitude
        is the theater's MSL datum (T1). ISR-only (M14): these exist to be
        detected, classified and reported — never engaged.

        THE LAYDOWN HAS TO BE FLYABLE, not just present. `name` is what
        `targets.match_ob()` classifies on; the class fixes the M5 standoff
        ring AND the contact's size; `missions.identify_plan` then puts every
        waypoint on the wider of that ring and the pixel-density detect ring.
        Three independent limits fall out, and the old laydown broke all three:

          * GEOFENCE. "SA-6_site_1" classifies as `sam_medium_range` — a 24 km
            engagement envelope, so a 26.4 km standoff. No AO in this table is
            within an order of magnitude of that (the widest reaches ~9 km), so
            the flagship demo contact could not be identified in ANY theater.
            M14 correctly forbids closing inside the ring to make it fit.
          * SENSOR. A ring the doctrine forces can be wider than the range the
            camera can detect that target at: a 4.6 m towed AAA piece at its
            own 2.2 km ring is 1.2 px in the wide field, under the 3 px floor,
            and `identify_plan` says so (`detect_marginal`). Standing off from
            something you then cannot see is not an ISR pass.
          * FUEL. The identify pass flies two rings, so its route is roughly
            4*pi*standoff. Measured live against the shipped fuel model, a
            1.65 km ring (T-72) is a 20.8 km sortie needing 128% of the tank
            and a 2.2 km ring needs 158%. BINGO is real now; a mission the
            aircraft cannot finish is not a demo.

        So the laydown is scaled to the aircraft that actually flies it — a
        slow short-endurance quadrotor — rather than to a theater-level
        strategic target set it would never be tasked against. A battery
        position, its command post and its comms relay: one small unit, which
        is what a tactical ISR quad is sent to find and report. Measured at the
        demo's 60 m AGL, all three are detectable at their own standoff, no
        waypoint leaves any AO in this table, and the widest route is 10.6 km:

            command_post_1  c2_node         ring 550 m  furthest wp 1290 m
            D30_howitzer_1  towed_howitzer  ring 300 m  furthest wp 1290 m
            comms_relay_1   comms_relay     ring 300 m  furthest wp  921 m

        `command_post_1` is the one whose standoff comes from its OWN
        engagement envelope rather than the 300 m floor, which is what keeps
        the M5 machinery exercised by the shipped demo.
        """
        alt = self.home_alt_msl_m
        laydown = (("command_post_1", "c2", 90.0, 90.0),
                   ("D30_howitzer_1", "arty", -80.0, 110.0),
                   ("comms_relay_1", "relay", 110.0, -70.0))
        out = []
        for name, mesh, north_m, east_m in laydown:
            lat, lon = self.point_at(north_m, east_m)
            out.append({"name": name, "mesh": mesh, "lat": lat, "lon": lon,
                        "alt_m": alt})
        return out

    # ---- real-world hydration (realdata.py) ----
    def bbox(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) — the order every GEV bbox proxy wants."""
        min_lat, min_lon, max_lat, max_lon = self.bounds()
        return (min_lat, min_lon, max_lat, max_lon)

    def terrain_sample_points(self, grid: int = FLOOR_GRID) -> list[tuple[float, float]]:
        """AO vertices plus a `grid` x `grid` lattice over its bounding box.

        The set a geofence floor is built from: vertices catch a ridge on the
        boundary, the lattice catches one in the middle. Points outside the
        polygon are dropped so a concave AO does not import a neighbouring peak.
        """
        south, west, north, east = self.bbox()
        points = list(self.ao_list())
        n = max(2, int(grid))
        for i in range(n):
            for j in range(n):
                lat = south + (north - south) * i / (n - 1)
                lon = west + (east - west) * j / (n - 1)
                if self.contains(lat, lon):
                    points.append((lat, lon))
        return points

    def hydrate(self, client: "RealWorldData | None" = None, *,
                allow_network: bool = True,
                floor_clearance_agl_m: float = FLOOR_CLEARANCE_M_AGL,
                traffic_radius_m: float = TRAFFIC_RADIUS_M,
                ob_limit: int | None = 40) -> "TheaterRealData":
        """Ingest real terrain / sites / traffic / weather for this theater.

        BLOCKS on the network — call it at startup or from a background thread,
        never from a mission or telemetry path. The result is stored so
        `real_data()` can serve it without blocking, and is returned.

        Nothing in the static table is modified: a hydrated theater keeps its
        hand-entered `home_alt_msl_m` and AO, and `TheaterRealData` reports the
        discrepancy instead of applying it. Each feed degrades independently and
        says so in its `Provenance`, so a dead upstream is visible rather than
        papered over.
        """
        from . import realdata

        client = client or realdata.default_client()
        result = client.hydrate_theater(
            theater_id=self.id, home_lat=self.home_lat, home_lon=self.home_lon,
            bbox=self.bbox(), static_home_msl_m=self.home_alt_msl_m,
            traffic_radius_m=traffic_radius_m,
            floor_points=self.terrain_sample_points(),
            floor_clearance_agl_m=floor_clearance_agl_m,
            ob_limit=ob_limit, allow_network=allow_network)
        set_real_data(self.id, result)
        return result

    def hydrate_async(self, client: "RealWorldData | None" = None,
                      **kwargs: Any) -> threading.Thread:
        """`hydrate()` on a daemon thread — the non-blocking entry point."""
        thread = threading.Thread(
            target=lambda: self.hydrate(client, **kwargs),
            name=f"godseye-hydrate-{self.id}", daemon=True)
        thread.start()
        return thread

    def real_data(self) -> "TheaterRealData | None":
        """Last hydration for this theater, or None. Never touches the network."""
        return real_data(self.id)

    def ground_msl_m(self) -> tuple[float, bool]:
        """(ground elevation MSL, is_real) — measured terrain if hydrated.

        Falls back to the static `home_alt_msl_m` with `is_real=False`, which is
        exactly the pre-existing "AGL means height above takeoff" behaviour —
        but now the caller can SEE that it is the fallback.
        """
        hydrated = self.real_data()
        if hydrated is not None and hydrated.ground.msl_m is not None:
            return (hydrated.ground.msl_m, hydrated.ground.real)
        return (self.home_alt_msl_m, False)

    def real_data_dict(self) -> dict[str, Any]:
        """The `real_data` block of `as_dict()` — hydrated payload or the honest
        "this row is the static table" marker."""
        hydrated = self.real_data()
        if hydrated is None:
            return {"hydrated": False, "source": "static-table", "real": False,
                    "note": STATIC_TABLE_NOTE}
        payload = hydrated.as_dict()
        payload["hydrated"] = True
        payload["source"] = "realdata"
        return payload

    # ---- wiring helpers ----
    def envelope_kwargs(self) -> dict[str, Any]:
        """`SafetyEnvelope(**theater.envelope_kwargs())` — geofence + home."""
        return {"geofence": self.ao_list(), "home": self.home}

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready row. `home`/`ao` keep the shape the GEV panel already uses.

        `real_data` is always present and always says whether the row is the
        static offline default or measured — it is never omitted, because an
        absent flag reads as "fine" and that is the failure mode this project
        keeps being bitten by.
        """
        return {
            "id": self.id,
            "label": self.label,
            "place": self.place,
            "description": self.description,
            "home": [self.home_lat, self.home_lon, self.home_alt_msl_m],
            "home_alt_datum": "MSL",
            "ao": [[lat, lon] for lat, lon in self.ao],
            "pois": [p.as_dict() for p in self.pois],
            "orbit_radius_m": self.orbit_radius_m,
            "demo": {
                "polygon": [[lat, lon] for lat, lon in self.demo_box()],
                "alt_m_agl": DEMO_ALT_M_AGL,
                "speed_mps": DEMO_SPEED_MPS,
            },
            "real_data": self.real_data_dict(),
        }


def _box(min_lat: float, min_lon: float, max_lat: float,
         max_lon: float) -> tuple[tuple[float, float], ...]:
    """Axis-aligned AO box, CCW from the SW corner."""
    return ((min_lat, min_lon), (min_lat, max_lon),
            (max_lat, max_lon), (max_lat, min_lon))


# Real-world anchors. Altitudes are approximate terrain elevation in metres
# MSL (T1) — good enough for an AirSim OriginGeopoint, not a survey, but they
# are now CHECKED against terrain measured at each home point: see
# `tests/test_theaters.py::test_declared_ground_matches_measured_terrain`,
# which holds every row to 50 m and caught two that were 286 m and 647 m out.
# "Approximate" is not licence to guess — it is the tolerance of that guard.
_TABLE: tuple[Theater, ...] = (
    Theater(
        id="default",
        label="Redmond (AirSim default)",
        # Microsoft campus, Redmond, Washington, USA — the stock AirSim
        # Blocks OriginGeopoint (47.641468, -122.140165, 122 m MSL).
        place="Redmond, Washington, USA",
        description="Stock AirSim origin. Smoke-test AO for the one-command demo.",
        home_lat=47.641468, home_lon=-122.140165, home_alt_msl_m=122.0,
        # AO ENLARGED. It was +/-0.005 deg — 1113 m N-S x 751 m E-W, so the
        # furthest geofence vertex from a contact near its centre was ~800 m,
        # while every other AO in this table is 4.4-12 km across. 800 m is
        # under the pixel-density DETECT ring of any vehicle-sized contact
        # (1290 m for a 7 m target at 60 m AGL), never mind an M5 threat ring,
        # so `default` was the one theater where an identify pass was
        # geofence-rejected at every waypoint — and M14 rightly forbids closing
        # inside the ring to make it fit. Now +/-0.02 deg lat x +/-0.03 deg lon
        # (4453 x 4501 m — the same degree extents as iran-natanz), half-extents
        # ~2226 x 2250 m. The demo laydown's widest waypoint sits 1290 m from
        # its contact, which is ~1426 m from the AO centre once the contact's
        # own offset is counted: ~800 m of margin, kept deliberately so a
        # laydown change does not immediately re-break the geofence. The demo
        # MISSION is unchanged — `demo_box()` is still a 300 m square on the
        # same centre, and the BINGO gate still passes.
        ao=_box(47.621468, -122.170165, 47.661468, -122.110165),
        pois=(Poi("North Field", 47.6445, -122.1402),
              Poi("South Field", 47.6385, -122.1402),
              Poi("East Field", 47.6415, -122.1372)),
    ),
    Theater(
        id="iran-isfahan",
        label="Iran — Isfahan",
        # Isfahan city, Iran (Naqsh-e Jahan ~32.6546N 51.6680E, ~1570 m MSL).
        # The old launch.py "iran-isfahan" (33.72, 51.72) was in fact Natanz,
        # 115 km away — that entry now lives below as `iran-natanz`.
        place="Isfahan, Iran",
        description="Urban-industrial AO: wide-area recon and pattern-of-life build-up.",
        home_lat=32.6546, home_lon=51.6680, home_alt_msl_m=1570.0,
        ao=_box(32.63, 51.63, 32.68, 51.71),
        pois=(Poi("Isfahan North", 32.670, 51.660),
              Poi("Isfahan Center", 32.655, 51.670),
              Poi("Isfahan South", 32.640, 51.680)),
    ),
    Theater(
        id="iran-natanz",
        label="Iran — Natanz",
        # Natanz fuel-enrichment site, Isfahan province, Iran
        # (~33.7243N 51.7286E) on the central Iranian plateau, ~1580 m MSL.
        # GROUND ELEVATION CORRECTED (was 1580 m, i.e. 286.5 m too high). 1580 m
        # is the elevation of Natanz TOWN (33.5086N 51.9161E, measured 1650 m),
        # 30 km SE and up in the Karkas range. The enrichment site these
        # coordinates name is out on the Kashan plain: 1293.5 m measured at this
        # exact point by the project's own pipeline, 1298 m by an independent
        # DEM. The coordinates were right and the altitude was wrong, so the
        # altitude moved and the coordinates did not.
        place="Natanz, Isfahan province, Iran",
        description="Declared-facility monitoring: periodic re-look and change detection.",
        home_lat=33.7243, home_lon=51.7286, home_alt_msl_m=1293.0,
        ao=_box(33.705, 51.700, 33.745, 51.760),
        pois=(Poi("Natanz North", 33.735, 51.720),
              Poi("Natanz Center", 33.725, 51.730),
              Poi("Natanz South", 33.715, 51.740)),
    ),
    Theater(
        id="iran-fordow",
        label="Iran — Fordow",
        # Fordow enrichment site, hills NE of Qom, Iran (~34.8849N 50.9958E).
        # GROUND ELEVATION CORRECTED (was 1550 m, i.e. 647.2 m too high —
        # by far the worst row in the table). Measured 902.8 m here by the
        # project's own pipeline and 906 m by an independent DEM; the published
        # site coordinate (34.8846N 50.9987E) measures 909 m, 3 m and 270 m
        # away, so the coordinates already name the real facility. Nothing
        # within 2 km of here reaches even 1025 m, and Qom itself is 931 m:
        # 1550 m was not a nearby ridge, it was simply not measured.
        place="Fordow, near Qom, Iran",
        description="Hard-terrain AO: LOS-limited observation of a hillside facility.",
        home_lat=34.8849, home_lon=50.9958, home_alt_msl_m=903.0,
        ao=_box(34.865, 50.965, 34.905, 51.025),
        pois=(Poi("Fordow North", 34.892, 50.990),
              Poi("Fordow Center", 34.885, 50.996),
              Poi("Fordow South", 34.878, 51.005)),
    ),
    Theater(
        id="indo-pak-loc",
        label="Indo-Pak — Line of Control",
        # Kashmir valley near Srinagar, India (~34.08N 74.82E, ~1600 m MSL).
        # Stand-in for Line-of-Control terrain; the LoC itself is ~70 km NW.
        place="Kashmir valley near Srinagar, India",
        description="Mountain-valley AO: route recon and border-watch pattern-of-life.",
        home_lat=34.08, home_lon=74.82, home_alt_msl_m=1600.0,
        ao=_box(34.04, 74.78, 34.12, 74.86),
        pois=(Poi("LoC North", 34.105, 74.815),
              Poi("LoC Center", 34.080, 74.820),
              Poi("LoC South", 34.055, 74.825)),
    ),
    Theater(
        id="taiwan-strait",
        label="Taiwan Strait",
        # Open water mid-strait at 24.50N 119.50E — 125 km WNW of Taichung,
        # Taiwan (24.1477N 120.6736E), ~90 km off the Fujian coast.
        # Sea-level datum: home MSL = 0 m.
        place="Taiwan Strait (open water west of Taichung)",
        description="Maritime AO: vessel search, classification and track handoff.",
        home_lat=24.50, home_lon=119.50, home_alt_msl_m=0.0,
        ao=_box(24.44, 119.44, 24.56, 119.56),
        pois=(Poi("Strait North", 24.540, 119.500),
              Poi("Strait Center", 24.500, 119.500),
              Poi("Strait South", 24.460, 119.500)),
    ),
    Theater(
        id="ukraine-donbas",
        label="Ukraine — Donbas",
        # Donets Ridge between Kostiantynivka and Bakhmut, Donetsk oblast,
        # Ukraine (~48.60N 37.90E, ~250 m MSL).
        place="Donetsk oblast, Ukraine (Donets Ridge)",
        description="Open-terrain AO: convoy detection, movement tracking, BDA re-look.",
        home_lat=48.60, home_lon=37.90, home_alt_msl_m=250.0,
        ao=_box(48.54, 37.84, 48.66, 37.96),
        pois=(Poi("Donbas North", 48.640, 37.900),
              Poi("Donbas Center", 48.600, 37.900),
              Poi("Donbas South", 48.560, 37.900)),
    ),
    Theater(
        id="red-sea-hormuz",
        label="Strait of Hormuz",
        # Strait of Hormuz at 26.55N 56.25E — 70 km S of Bandar Abbas, Iran
        # (27.1833N 56.2667E), south of Qeshm island in the outbound lane.
        # Shipping lane; sea-level datum: home MSL = 0 m.
        place="Strait of Hormuz, south of Bandar Abbas, Iran",
        description="Choke-point AO: shipping-lane watch and vessel pattern-of-life.",
        home_lat=26.55, home_lon=56.25, home_alt_msl_m=0.0,
        ao=_box(26.50, 56.19, 26.60, 56.31),
        pois=(Poi("Hormuz North", 26.580, 56.250),
              Poi("Hormuz Center", 26.550, 56.250),
              Poi("Hormuz South", 26.520, 56.250)),
    ),
)

THEATERS: Mapping[str, Theater] = {t.id: t for t in _TABLE}
DEFAULT_THEATER_ID = "default"
DEFAULT_EXPORT_NAME = "theaters.json"

# ---------------------------------------------------------------------------
# Hydration registry. `Theater` is frozen — the static table must stay the
# offline default and stay immutable — so what a theater has *learned* about
# the real world lives beside it, keyed by id.
# ---------------------------------------------------------------------------

_HYDRATION: dict[str, "TheaterRealData"] = {}
_HYDRATION_LOCK = threading.Lock()


def set_real_data(theater_id: str, data: "TheaterRealData") -> None:
    """Publish a hydration result. Called by `Theater.hydrate()` and refreshers."""
    with _HYDRATION_LOCK:
        _HYDRATION[theater_id] = data


def real_data(theater_id: str) -> "TheaterRealData | None":
    """Last hydration for a theater, or None. Non-blocking: memory only."""
    with _HYDRATION_LOCK:
        return _HYDRATION.get(theater_id)


def clear_hydration(theater_id: str | None = None) -> None:
    """Forget hydration for one theater, or all of them.

    Called by the tests. NOT yet called by `sim_reset` — nothing in `server.py`
    hydrates a theater, so there is no hydration for a reset to clear (see the
    UNWIRED note in this module's docstring). Say so rather than claim a
    wiring that does not exist.
    """
    with _HYDRATION_LOCK:
        if theater_id is None:
            _HYDRATION.clear()
        else:
            _HYDRATION.pop(theater_id, None)


def hydrate_all(client: "RealWorldData | None" = None,
                theater_ids: Iterable[str] | None = None,
                **kwargs: Any) -> dict[str, "TheaterRealData"]:
    """Hydrate several theaters through one client. BLOCKS — startup only.

    One theater failing does not stop the rest: a failure is already a flagged
    `TheaterRealData`, not an exception, so the returned map is complete.
    """
    ids_ = list(theater_ids) if theater_ids is not None else ids()
    if client is None:
        from . import realdata

        client = realdata.default_client()
    return {tid: get(tid).hydrate(client, **kwargs) for tid in ids_}


def hydration_status() -> dict[str, Any]:
    """Which theaters are hydrated and which feeds are degraded — diagnostics."""
    with _HYDRATION_LOCK:
        snapshot = dict(_HYDRATION)
    return {
        "hydrated": sorted(snapshot),
        "static_only": sorted(t for t in ids() if t not in snapshot),
        "degraded_feeds": {tid: data.degraded_feeds
                           for tid, data in sorted(snapshot.items())
                           if data.degraded_feeds},
    }


def ids() -> list[str]:
    """Theater ids, table order — for `argparse(choices=...)` and UI menus."""
    return [t.id for t in _TABLE]


def get(theater_id: str | None) -> Theater:
    """Look up a theater; `None` returns the default. Raises KeyError."""
    key = theater_id or DEFAULT_THEATER_ID
    try:
        return THEATERS[key]
    except KeyError:
        raise KeyError(f"unknown theater {key!r}; known: {', '.join(ids())}") from None


def all_theaters() -> list[Theater]:
    """Every theater, table order."""
    return list(_TABLE)


# ---------------------------------------------------------------------------
# ACTIVE theater — which row a RUNNING server chose (INTEGRATION_FINDINGS UI-1)
#
# This module cannot know the answer: it is a static table, and `DEFAULT_
# THEATER_ID` is only which row a caller gets when it asks for none. The
# publisher is `bridge.MissionFeed.active_theater()`, which reads the MCP
# server's own `uav://safety/geofence`. The SHAPE lives here so there is one
# definition of it for the bridge, the panel and the tests.
#
# The one invariant: `known` is true only when a server really answered.
# Unknown never degrades to `DEFAULT_THEATER_ID` — a UI that showed Redmond
# POIs for an aircraft over Isfahan is the defect this block exists to end.
# ---------------------------------------------------------------------------

#: Every key an `active` block carries, present whether or not it is known.
ACTIVE_KEYS: tuple[str, ...] = (
    "known", "id", "label", "ground_elevation_msl_m", "ao", "in_table",
    "theater_mismatch", "source", "at_ms", "reason")


def active_unknown(reason: str, *, source: str = "") -> dict[str, Any]:
    """An `active` block that says the running theater is NOT known, and why.

    `reason` is mandatory and must be non-empty: "unknown" with no cause is
    indistinguishable on a HUD from "not wired up", and the operator cannot
    tell whether to wait or to restart the stack.
    """
    if not str(reason).strip():
        raise ValueError(
            "active_unknown needs a reason; an unexplained 'unknown theater' "
            "is what the operator has to act on")
    return {"known": False, "id": None, "label": None,
            "ground_elevation_msl_m": None, "ao": None, "in_table": False,
            "theater_mismatch": None, "source": source, "at_ms": 0,
            "reason": str(reason)}


def active_from_server(block: Mapping[str, Any], *, source: str, at_ms: int,
                       theater_mismatch: Any = None) -> dict[str, Any]:
    """An `active` block from a server's own theater description.

    `block` is the `theater` object of `uav://safety/geofence`
    (`{id, label, ao, ground_elevation_msl_m}`). A block with no usable `id` is
    NOT known — a nameless theater cannot be adopted by a selector, and
    inventing the default here would re-create UI-1.

    `in_table` says whether this bridge's own table has that row, so a consumer
    can tell "the server is somewhere I can draw" from "the server is running a
    theater I have never heard of" instead of silently showing neither.

    `reason` is empty ONLY when the block came through complete, and "complete"
    counts a field the server never sent as well as one that arrived broken.
    Both end up publishing `null`, and a consumer cannot tell those apart from
    the value alone — an absent `ao` used to come back `ao: null, reason: ""`,
    i.e. a block that declared itself complete while carrying no polygon. A
    block whose id is good but whose ground or AO is unusable is still `known`
    — the operator needs the place — but `reason` now names every field that is
    null and says which of the two happened to it.
    """
    tid = str(block.get("id") or "").strip() if isinstance(block, Mapping) else ""
    if not tid:
        return active_unknown(
            "the server's uav://safety/geofence carried no theater id",
            source=source)
    dropped: list[str] = []
    missing: list[str] = []

    raw_ground = block.get("ground_elevation_msl_m")
    ground: float | None
    if raw_ground is None:
        ground = None
        missing.append("ground_elevation_msl_m")
    elif isinstance(raw_ground, bool):
        # `bool` is an `int` subclass, so `float(True)` is a perfectly finite
        # 1.0 — a theater at 1570 m published as 1 m above sea level, with an
        # empty `reason` vouching for it. The cast must not launder a flag into
        # a plausible elevation.
        ground = None
        dropped.append(f"ground_elevation_msl_m was a bool ({raw_ground!r})")
    else:
        try:
            ground = float(raw_ground)
        except (TypeError, ValueError):
            ground, _ = None, dropped.append(
                f"ground_elevation_msl_m was not a number ({raw_ground!r})")
        else:
            if not math.isfinite(ground):
                ground = None
                dropped.append(f"ground_elevation_msl_m was {raw_ground!r}")

    raw_ao = block.get("ao")
    ao: list[list[float]] | None = None
    if raw_ao is None:
        missing.append("ao")
    elif isinstance(raw_ao, list) and len(raw_ao) >= 3:
        try:
            ao = [[float(p[0]), float(p[1])] for p in raw_ao]
        except (TypeError, ValueError, IndexError, KeyError):
            # A malformed polygon must not 500 an open /health route, and must
            # not be quietly indistinguishable from "no polygon was offered".
            ao = None
            dropped.append("the ao polygon was not a list of [lat, lon] pairs")
    elif isinstance(raw_ao, list):
        dropped.append("the ao polygon had fewer than 3 vertices")
    else:
        dropped.append(
            f"the ao polygon was not a list (it was {type(raw_ao).__name__})")

    notes: list[str] = []
    if dropped:
        notes.append("; ".join(dropped) + " — dropped from this block")
    if missing:
        notes.append(", ".join(missing) + " — the server did not send "
                     + ("this field" if len(missing) == 1 else "these fields"))

    return {
        "known": True,
        "id": tid,
        "label": str(block.get("label") or tid),
        "ground_elevation_msl_m": ground,
        "ao": ao,
        "in_table": tid in THEATERS,
        # DEEP-copied: the caller's `theater_mismatch` is a nested object out of
        # the bridge's own cached `uav://safety/geofence` document, and
        # `GET /health` returns this block straight to the responder without
        # going through `as_payload`'s copy. Storing the reference handed a
        # route's return value a live alias into `MissionFeed`'s state — the
        # "frozen row, still-mutable dict field" trap, one level up.
        "theater_mismatch": deepcopy(theater_mismatch),
        "source": source,
        "at_ms": int(at_ms),
        "reason": "; ".join(notes),
    }


def check_active(active: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an `active` block and return a private deep copy of it.

    A PARTIAL block is worse than no block at all. Every consumer of this
    payload reaches for the theater the same way — `active.get("id") or
    payload["default"]` — so a block missing `id`, or carrying `known: true`
    with nothing under it, resolves straight back to the TABLE default and
    re-creates UI-1 while looking like the fix for it. `as_payload` therefore
    refuses to publish one rather than passing it through: this is the
    "validator that accepts the value which disables the feature it validates"
    failure, and the only safe answer is to fail loudly at the publisher.
    """
    if not isinstance(active, Mapping):
        raise TypeError(
            f"active must be a mapping built by active_from_server() or "
            f"active_unknown(), not {type(active).__name__}")
    absent = [k for k in ACTIVE_KEYS if k not in active]
    if absent:
        raise ValueError(
            f"active block is missing {', '.join(absent)}; build it with "
            "active_from_server() or active_unknown(). A partial block lets a "
            "consumer fall through to the table default, which is UI-1")
    if active["known"]:
        if not str(active["id"] or "").strip():
            raise ValueError(
                "active block says known=True with no id; `known` means a "
                "server really answered with a theater id")
    elif not str(active["reason"] or "").strip():
        raise ValueError(
            "active block says known=False with no reason; an unexplained "
            "'unknown theater' is what the operator has to act on "
            "(see active_unknown)")
    return deepcopy(dict(active))


def as_payload(active: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The whole table as a JSON-ready payload (bridge `/theaters`, GEV UI).

    `active` is the running theater (see above). Omitted, the payload still
    carries an `active` block — one that says it is unknown and why — because
    an absent key is the thing consumers read as "just use `default`".

    The block is CHECKED and DEEP-copied in (`check_active`): its `ao` list and
    `theater_mismatch` dict are mutable, and handing a caller a live reference
    to the publisher's own state is the "frozen dataclass with a mutable dict
    field" trap one level up. A block that is not the published shape raises
    rather than being served — see `check_active` for why a partial one is
    worse than none.
    """
    return {
        "schema": SCHEMA,
        "default": DEFAULT_THEATER_ID,
        "default_note": "the TABLE default: which row `theaters.get(None)` "
                        "returns. NOT what the server is flying — read `active` "
                        "for that, and show a mismatch rather than defaulting.",
        "active": check_active(active) if active is not None else active_unknown(
            "this payload was built from the static table alone; no running "
            "server was consulted"),
        "alt_datum": "MSL",
        "alt_datum_note": "home altitudes are metres above mean sea level (T1); "
                          "waypoint altitudes are metres AGL",
        "real_data": hydration_status(),
        "real_data_note": "each theater row carries a real_data block saying whether "
                          "it is the static offline default or hydrated from real "
                          "terrain / mapped sites / live traffic / current weather",
        "theaters": [t.as_dict() for t in _TABLE],
    }


def to_json(indent: int | None = 2) -> str:
    """Serialized table — the exact bytes the browser UI should consume."""
    return json.dumps(as_payload(), indent=indent, sort_keys=False)


def export_json(path: str | Path) -> Path:
    """Write the table to `path` (parents created). Returns the path written."""
    p = Path(path)
    if p.is_dir():
        p = p / DEFAULT_EXPORT_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(to_json() + "\n", encoding="utf-8")
    return p


def validate(theaters: Iterable[Theater] | None = None) -> list[str]:
    """Self-check the table; returns a list of problems (empty = consistent).

    Guards exactly the drift that broke the one-command demo: home outside
    its own AO, a demo box that the geofence would reject (M4), or a POI
    whose orbit ring leaves the AO.
    """
    problems: list[str] = []
    for t in theaters if theaters is not None else _TABLE:
        if len(t.ao) < 3:
            problems.append(f"{t.id}: AO needs >=3 vertices, has {len(t.ao)}")
            continue
        if not t.contains(t.home_lat, t.home_lon):
            problems.append(f"{t.id}: home {t.home_lat},{t.home_lon} outside its AO")
        for i, (lat, lon) in enumerate(t.demo_box()):
            if not t.contains(lat, lon):
                problems.append(f"{t.id}: demo_box vertex {i} outside the AO")
        for poi in t.pois:
            if not t.contains(poi.lat, poi.lon):
                problems.append(f"{t.id}: POI {poi.name!r} outside the AO")
                continue
            for j, (lat, lon) in enumerate(t.ring(poi.lat, poi.lon, t.orbit_radius_m)):
                if not t.contains(lat, lon):
                    problems.append(
                        f"{t.id}: POI {poi.name!r} orbit ring point {j} outside the AO")
                    break
    return problems


def main() -> int:
    """CLI: `python -m godseye_uav.theaters [--out path]` — export or verify."""
    import argparse

    ap = argparse.ArgumentParser(description="godSeye theater table (single source of truth)")
    ap.add_argument("--out", help="write the JSON table here (default: stdout)")
    ap.add_argument("--check", action="store_true", help="validate the table and exit")
    ap.add_argument("--hydrate", metavar="ID", nargs="?", const="*",
                    help="ingest real terrain/sites/traffic/weather before exporting "
                         "(one theater id, or no value for all). Needs the God's Eye "
                         "View dev server; without it every feed degrades visibly "
                         "rather than failing.")
    ap.add_argument("--gev-origin", help="God's Eye View origin for --hydrate")
    args = ap.parse_args()

    problems = validate()
    if problems:
        for p in problems:
            print(f"[theaters] INCONSISTENT: {p}")
        return 1
    if args.hydrate:
        from . import realdata

        client = realdata.default_client(origin=args.gev_origin)
        wanted = None if args.hydrate == "*" else [args.hydrate]
        for tid, data in hydrate_all(client, wanted).items():
            degraded = ", ".join(data.degraded_feeds) or "none"
            delta = data.terrain_delta_m()
            print(f"[theaters] {tid}: degraded feeds = {degraded}; "
                  f"terrain delta vs static table = "
                  f"{'unknown' if delta is None else format(delta, '+.1f') + ' m'}")
    if args.check:
        print(f"[theaters] ok: {len(_TABLE)} theaters consistent")
        return 0
    if args.out:
        print(f"[theaters] wrote {export_json(args.out)}")
    else:
        print(to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
