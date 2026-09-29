"""Runtime (dynamic) theaters and the test egress guard — WG spec §4.1.1, §4.1.10.

Unit A1's acceptance list (§4.3):
  * id determinism and chip-grammar length;
  * register/validate refusal;
  * `get()` resolves dynamic ids;
  * `as_payload` rows;
  * `from_dict(as_dict(t)) == t`;
  * `in_table`;
  * `ACTIVE_KEYS` unchanged;
  * the egress guard blocks `socket.create_connection(("93.184.216.34", 80))`
    and allows loopback.
Offline: nothing here reaches the network (the guard would refuse it).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import math
import os
import pickle
import re
import socket

import pytest
from conftest import (
    EGRESS_BLOCKED,
    LIVE_NET_ENV,
    NO_EGRESS_ENV,
    _own_addresses,
    is_local_host,
)
from godseye_uav import theaters
from godseye_uav.analyst_toolbelt import _ENTITY_ID

#: gods-eye-view/src/console/chat/markdown.js CHIP_SOURCE, verbatim.
JS_CHIP_RE = re.compile(
    r"\[\[([a-z]{2,8}):([^\]|\n]{1,200}?)(?:\|([^\]\n]{1,200}?))?\]\]")

#: The id rest the Python toolbelt accepts (analyst_toolbelt._ENTITY_ID).
PY_ID_REST_RE = re.compile(r"^[^\r\n\[\]|]{1,160}$")

XSS = "<img src=x onerror=alert(1)>"
BIDI = "\u202eevil\u202c"
BIDI_CHARS = set(map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A)]))

CENTRE = (12.9716, 77.5946)
PROVENANCE = {
    "proposal_id": "TP-0000abcd",
    "center": {"lat": CENTRE[0], "lon": CENTRE[1], "source": "coordinates",
               "place_id": None, "query": None},
    "ao": {"half_extent_m": 2500, "requested_m": None, "geocoded_half_m": None,
           "clamped": False, "airframe": "quad_suas_electric", "reach_m": 7350},
    "home": {"source": "ao-centre", "name": None, "osm": None, "distance_m": 0},
    "ground": {"msl_m": 920.0, "source": "operator", "hae_m": None, "fetched_at_ms": 0},
    "pois": [{"name": "Cubbon Park", "source": "open-ground", "osm": "way/1"}],
    "sites": {"total": 0, "degraded": True, "reason": "map data is off",
              "fetched_at_ms": None, "attribution": "© OpenStreetMap contributors, ODbL"},
}


def _make(label: str = "Bengaluru centre", *, center=CENTRE, half: float = 2500.0,
          home=None, pois=(), provenance=PROVENANCE, **kw) -> theaters.Theater:
    return theaters.make_dynamic(
        label=label, place=kw.pop("place", "Bengaluru, Karnataka, India"),
        center=center, half_extent_m=half, home=home or center,
        home_alt_msl_m=kw.pop("home_alt_msl_m", 920.0), pois=pois,
        provenance=provenance)


def _span_m(t: theaters.Theater) -> tuple[float, float]:
    s, w, n, e = t.bbox()
    lat0 = (s + n) / 2
    return ((n - s) * 111_320.0, (e - w) * 111_320.0 * math.cos(math.radians(lat0)))


# ------------------------------------------------------------------ ids

def test_dynamic_id_is_deterministic_and_keyed_on_label_and_ao():
    a, b = _make(), _make()
    assert a.id == b.id == theaters.dynamic_id("Bengaluru centre", a.ao)
    assert a.id.startswith(theaters.DYNAMIC_PREFIX)
    assert a.id.startswith("dyn-bengaluru-centre-")
    # The hash is over the AO at 5 decimals: another area, another id...
    moved = _make(center=(CENTRE[0] + 0.01, CENTRE[1]))
    assert moved.id != a.id and moved.id.startswith("dyn-bengaluru-centre-")
    # ...and another name over the same AO, another slug.
    assert _make("Bangalore").id.startswith("dyn-bangalore-")
    assert _make("Bangalore").id.rsplit("-", 1)[1] == a.id.rsplit("-", 1)[1]
    # Sub-5-decimal noise in the vertices does not change the id.
    jitter = tuple((lat + 1e-7, lon - 1e-7) for lat, lon in a.ao)
    assert theaters.dynamic_id(a.label, jitter) == a.id


def test_make_dynamic_builds_the_square_ao_and_description():
    t = _make()
    north_m, east_m = _span_m(t)
    assert north_m == pytest.approx(5000.0, abs=0.5)
    assert east_m == pytest.approx(5000.0, abs=0.5)
    assert t.center() == pytest.approx(CENTRE, abs=1e-6)
    assert t.dynamic is True
    assert t.description == theaters.DYNAMIC_DESCRIPTION == (
        "Chat-defined AO (simulation). A real place: mapped data is context only (M14).")
    assert t.home == (CENTRE[0], CENTRE[1], 920.0)
    assert theaters.validate([t]) == []


@pytest.mark.parametrize("label", [
    "Bengaluru centre",
    "Kherson, Kherson Oblast, Ukraine — riverside industrial district and port",
    XSS,
    BIDI + " town",
    "Київ",                                    # no ASCII after folding -> "area"
    "São Tomé / Príncipe",
    "x" * 500,
    "a]]|b[[c\nd\re",
])
def test_ids_fit_the_chip_grammar(label):
    t = _make(label)
    assert theaters.DYNAMIC_ID_RE.match(t.id), t.id
    assert len(t.id) <= 35                     # "dyn-" + 24 + "-" + 6
    assert re.fullmatch(r"dyn-[a-z0-9-]+", t.id)
    # Python: the analyst toolbelt's entity-id grammar.
    assert _ENTITY_ID.match(f"thr:{t.id}")
    # Sites keyed under the theater still fit (sit:{theater}:{osm_type}/{id}).
    sit = f"{t.id}:relation/99999999999"
    assert PY_ID_REST_RE.match(sit) and len(sit) <= 160
    # JS: a chip with the (cleaned) label parses back to exactly this id.
    m = JS_CHIP_RE.fullmatch(f"[[thr:{t.id}|{t.label.replace(']', '')}]]")
    assert m and m.group(1) == "thr" and m.group(2) == t.id
    # The label kept for display is one clean line, capped, bidi-free.
    assert len(t.label) <= theaters.LABEL_MAX
    assert not (set(t.label) & BIDI_CHARS)
    assert "\n" not in t.label and "\r" not in t.label


def test_untrusted_label_is_kept_as_text_not_escaped():
    t = _make(XSS)
    assert t.label == XSS                       # rendering escapes, not storage
    assert t.id.startswith("dyn-img-src-x-onerror-ale")
    assert _make(BIDI).label == "evil"
    assert _make("Київ").id.startswith("dyn-area-")
    assert theaters.clean_text("  a\u202e\tb\n\nc  ") == "a b c"
    once = theaters.clean_text(BIDI * 40 + " " + XSS * 10, limit=80)
    assert theaters.clean_text(once, limit=80) == once


# ------------------------------------------------------------------ register / validate

def test_register_refuses_a_row_validate_rejects():
    outside = _make(home=(CENTRE[0] + 0.5, CENTRE[1]))          # home 55 km away
    with pytest.raises(ValueError) as err:
        theaters.register_dynamic(outside)
    problems = err.value.args[0]
    assert isinstance(problems, list) and problems == theaters.validate([outside])
    assert any("home" in p and "outside its AO" in p for p in problems)
    # An AO smaller than the demo box: the demo mission would be geofenced.
    tiny = _make(half=100.0)
    with pytest.raises(ValueError) as err:
        theaters.register_dynamic(tiny)
    assert any("demo_box" in p for p in err.value.args[0])
    # A POI whose orbit ring leaves the AO.
    s, w, _, _ = _make().bbox()
    edge = _make(pois=[{"name": "Edge", "lat": s + 1e-4, "lon": w + 1e-4}])
    with pytest.raises(ValueError, match="Edge"):
        theaters.register_dynamic(edge)
    assert theaters.dynamic_theaters() == []


@pytest.mark.parametrize("bad_id", ["default", "Dyn-x", "dyn-", "dyn-X", "dyn-x]]|y",
                                    "dyn-a b", "thr:dyn-x", "dyn-" + "a" * 61])
def test_register_refuses_ids_outside_the_dyn_shape(bad_id):
    t = dataclasses.replace(_make(), id=bad_id)
    with pytest.raises(ValueError) as err:
        theaters.register_dynamic(t)
    assert isinstance(err.value.args[0], list)
    assert not theaters.is_known(bad_id) or bad_id in theaters.THEATERS


def test_register_refuses_a_non_theater():
    with pytest.raises(TypeError):
        theaters.register_dynamic(_make().as_dict())            # type: ignore[arg-type]


@pytest.mark.parametrize("kwargs, match", [
    ({"label": "  \u202e\u202c "}, "label"),
    ({"center": (math.nan, 77.0)}, "centre lat"),
    ({"center": (12.0, 181.0)}, "centre"),
    ({"half": 0.0}, "positive"),
    ({"half": math.inf}, "finite"),
    ({"home_alt_msl_m": True}, "home_alt_msl_m"),
    ({"home_alt_msl_m": "high"}, "home_alt_msl_m"),
    ({"center": (89.99, 0.0)}, "pole"),
    ({"center": (0.0, 179.99)}, "antimeridian"),
    ({"pois": [{"name": "", "lat": 12.97, "lon": 77.59}]}, "POI"),
    ({"pois": [{"name": "P", "lat": None, "lon": 77.59}]}, "POI"),
])
def test_make_dynamic_refuses_unusable_input(kwargs, match):
    label = kwargs.pop("label", "Somewhere")
    with pytest.raises(ValueError, match=match):
        _make(label, **kwargs)


def test_registered_row_is_always_dynamic_and_replaces_by_id():
    t = _make()
    stored = theaters.register_dynamic(dataclasses.replace(t, dynamic=False))
    assert stored.dynamic is True and stored == t
    again = theaters.register_dynamic(_make(home_alt_msl_m=921.0))
    assert [x.id for x in theaters.dynamic_theaters()] == [t.id]
    assert theaters.get(t.id) is again and theaters.get(t.id).home_alt_msl_m == 921.0


# ------------------------------------------------------------------ get / ids / is_known

def test_get_resolves_table_then_dynamic_and_the_error_lists_both():
    t = _make()
    with pytest.raises(KeyError):
        theaters.get(t.id)
    assert not theaters.is_known(t.id)
    theaters.register_dynamic(t)
    assert theaters.get(t.id) == t and theaters.get(t.id).dynamic
    assert theaters.is_known(t.id) and theaters.is_known("default")
    assert not theaters.is_known("dyn-nope") and not theaters.is_known(None)
    assert theaters.get(None).id == theaters.DEFAULT_THEATER_ID
    assert theaters.get("iran-natanz") is theaters.THEATERS["iran-natanz"]
    # ids() stays the static table: it is argparse's `choices`.
    assert theaters.ids() == [row.id for row in theaters.all_theaters()]
    assert t.id not in theaters.ids()
    with pytest.raises(KeyError) as err:
        theaters.get("dyn-nope-000000")
    text = str(err.value)
    assert "default" in text and "red-sea-hormuz" in text and t.id in text
    theaters.clear_dynamic()
    with pytest.raises(KeyError, match="none registered"):
        theaters.get(t.id)
    assert theaters.dynamic_theaters() == []


def test_dynamic_theaters_keeps_registration_order():
    a = theaters.register_dynamic(_make("Alpha"))
    b = theaters.register_dynamic(_make("Bravo", center=(48.6, 37.9)))
    assert theaters.dynamic_theaters() == [a, b]


# ------------------------------------------------------------------ payload

def test_as_payload_lists_static_rows_then_dynamic_rows():
    a = theaters.register_dynamic(_make("Alpha"))
    b = theaters.register_dynamic(_make("Bravo", center=(48.6, 37.9), provenance=None))
    payload = json.loads(json.dumps(theaters.as_payload()))
    rows = payload["theaters"]
    assert [r["id"] for r in rows] == theaters.ids() + [a.id, b.id]
    static, dynamic = rows[:len(theaters.ids())], rows[len(theaters.ids()):]
    assert all(r["dynamic"] is False and "provenance" not in r for r in static)
    assert all(r["dynamic"] is True for r in dynamic)
    assert dynamic[0]["provenance"] == PROVENANCE
    assert "provenance" not in dynamic[1]
    row = dynamic[0]
    assert row["home"] == [CENTRE[0], CENTRE[1], 920.0] and row["home_alt_datum"] == "MSL"
    assert len(row["ao"]) == 4 and all(len(v) == 2 for v in row["ao"])
    assert row["description"] == theaters.DYNAMIC_DESCRIPTION
    assert row["real_data"]["hydrated"] is False
    assert row["demo"]["polygon"] and all(a.contains(*v) for v in row["demo"]["polygon"])
    assert payload["default"] == theaters.DEFAULT_THEATER_ID     # still the TABLE default


# ------------------------------------------------------------------ from_dict

@pytest.mark.parametrize("t", [*theaters.all_theaters(), "dynamic", "dynamic-pois"],
                         ids=lambda t: t if isinstance(t, str) else t.id)
def test_from_dict_inverts_as_dict(t):
    if t == "dynamic":
        t = _make()
    elif t == "dynamic-pois":
        t = _make(pois=[{"name": "Cubbon Park", "lat": 12.9763, "lon": 77.5929},
                        theaters.Poi("Lalbagh", 12.9507, 77.5848)], provenance=None)
    row = t.as_dict()
    back = theaters.from_dict(row)
    assert back == t
    assert back.dynamic is t.dynamic is t.id.startswith("dyn-")
    assert back.as_dict() == row                   # provenance included
    # ...and through the bytes theater.json holds.
    again = theaters.from_dict(json.loads(json.dumps(row)))
    assert again == t and again.as_dict() == row
    assert hash(again) == hash(t)


def test_from_dict_reads_a_dynamic_row_it_can_register():
    t = _make()
    back = theaters.from_dict(json.loads(json.dumps(t.as_dict())))
    assert theaters.register_dynamic(back) is theaters.get(t.id)
    assert theaters.get(t.id).provenance["ao"]["airframe"] == "quad_suas_electric"


def test_from_dict_cleans_hostile_text_from_disk():
    row = _make().as_dict()
    row.update(label=BIDI + XSS + "\n" + "y" * 200, place="p\u2066lace\r\n",
               pois=[{"name": "\u202epark", "lat": CENTRE[0], "lon": CENTRE[1]}])
    t = theaters.from_dict(row)
    assert t.label.startswith("evil" + XSS + " y") and len(t.label) == theaters.LABEL_MAX
    assert t.place == "place" and t.pois[0].name == "park"


@pytest.mark.parametrize("mutate, exc", [
    (lambda r: r.pop("home"), ValueError),
    (lambda r: r.update(home=[1.0, 2.0]), ValueError),
    (lambda r: r.update(home="12,77,920"), ValueError),
    (lambda r: r.update(home=[12.0, 77.0, None]), ValueError),
    (lambda r: r.update(home=[12.0, 77.0, True]), ValueError),
    (lambda r: r.pop("ao"), ValueError),
    (lambda r: r.update(ao=[[12.0], [13.0]]), ValueError),
    (lambda r: r.update(ao=[{"lat": 1, "lon": 2}] * 4), ValueError),
    (lambda r: r.update(ao=[[95.0, 0.0]] * 4), ValueError),
    (lambda r: r.update(id=""), ValueError),
    (lambda r: r.update(home_alt_datum="HAE"), ValueError),
    (lambda r: r.update(provenance=["not", "a", "mapping"]), ValueError),
    (lambda r: r.update(provenance={"bad": math.nan}), ValueError),
    (lambda r: r.update(orbit_radius_m="wide"), ValueError),
    (lambda r: r.update(pois=[{"name": "P"}]), ValueError),
])
def test_from_dict_refuses_a_malformed_row(mutate, exc):
    row = json.loads(json.dumps(_make().as_dict()))
    mutate(row)
    with pytest.raises(exc):
        theaters.from_dict(row)


def test_from_dict_refuses_a_non_mapping():
    with pytest.raises(TypeError):
        theaters.from_dict(["dyn-x"])                              # type: ignore[arg-type]


# ------------------------------------------------------------------ provenance

def test_provenance_is_deep_frozen_json_ready_and_outside_equality():
    source = json.loads(json.dumps(PROVENANCE))
    t = _make(provenance=source)
    source["ao"]["half_extent_m"] = 1                          # caller's copy moves...
    source["pois"].append({"name": "late"})
    assert t.provenance["ao"]["half_extent_m"] == 2500         # ...the row does not
    assert len(t.provenance["pois"]) == 1
    with pytest.raises(TypeError):
        t.provenance["proposal_id"] = "TP-ffffffff"             # type: ignore[index]
    with pytest.raises(TypeError):
        t.provenance["ao"]["clamped"] = True                    # type: ignore[index]
    assert t.as_dict()["provenance"] == PROVENANCE
    assert _make(provenance=None) == t and hash(_make(provenance=None)) == hash(t)
    assert "provenance" not in repr(t)
    with pytest.raises(ValueError, match="JSON"):
        _make(provenance={"when": object()})
    with pytest.raises(TypeError):
        _make(provenance=["TP-1"])                             # type: ignore[arg-type]


def test_a_dynamic_row_copies_pickles_and_serialises():
    t = _make()
    for method in ("update", "pop", "clear", "setdefault", "popitem"):
        with pytest.raises(TypeError, match="read-only"):
            getattr(t.provenance, method)({"x": 1} if method == "update" else "x")
    assert json.loads(json.dumps(t.provenance)) == PROVENANCE
    clone = copy.deepcopy(t)
    assert clone == t and clone.dynamic and clone.as_dict() == t.as_dict()
    back = pickle.loads(pickle.dumps(t))
    assert back == t and back.as_dict() == t.as_dict()
    with pytest.raises(TypeError):
        back.provenance["ao"]["half_extent_m"] = 1              # type: ignore[index]
    flat = dataclasses.asdict(t)
    assert json.loads(json.dumps(flat["provenance"])) == PROVENANCE
    assert flat["dynamic"] is True


# ------------------------------------------------------------------ active block

def test_in_table_covers_the_dynamic_registry():
    t = _make()
    block = {"id": t.id, "label": t.label, "ao": [list(v) for v in t.ao],
             "ground_elevation_msl_m": t.home_alt_msl_m}
    before = theaters.active_from_server(block, source="test", at_ms=1)
    assert before["known"] is True and before["in_table"] is False
    theaters.register_dynamic(t)
    after = theaters.active_from_server(block, source="test", at_ms=2)
    assert after["in_table"] is True and after["reason"] == ""
    static = theaters.active_from_server({"id": "iran-fordow"}, source="test", at_ms=3)
    assert static["in_table"] is True
    unknown = theaters.active_from_server({"id": "dyn-elsewhere-123456"},
                                          source="test", at_ms=4)
    assert unknown["in_table"] is False
    # The payload's active block passes check_active either way.
    assert theaters.as_payload(active=after)["active"]["in_table"] is True


def test_active_keys_unchanged():
    assert theaters.ACTIVE_KEYS == (
        "known", "id", "label", "ground_elevation_msl_m", "ao", "in_table",
        "theater_mismatch", "source", "at_ms", "reason")
    assert tuple(theaters.active_unknown("x")) == theaters.ACTIVE_KEYS


# ------------------------------------------------------------------ conftest isolation

def test_isolation_a_registers_a_theater():
    theaters.register_dynamic(_make("Leaky"))
    assert len(theaters.dynamic_theaters()) == 1


def test_isolation_b_starts_with_an_empty_registry():
    # Runs after the test above (file order): the conftest dropped its row.
    assert theaters.dynamic_theaters() == []


# ------------------------------------------------------------------ egress guard (§4.1.10)

EXAMPLE_IP = ("93.184.216.34", 80)


@contextlib.contextmanager
def _loopback_listener(family=socket.AF_INET, host="127.0.0.1"):
    """An ephemeral-port listener (port 0: no fixed test port is used)."""
    with socket.socket(family, socket.SOCK_STREAM) as srv:
        srv.bind((host, 0))
        srv.listen(4)
        yield srv.getsockname()[1]


def test_the_session_sets_no_egress_for_subprocesses():
    assert os.environ.get(NO_EGRESS_ENV) == "1"


def test_guard_blocks_create_connection_to_a_public_address(egress_guard):
    with pytest.raises(OSError, match=EGRESS_BLOCKED):
        socket.create_connection(EXAMPLE_IP, timeout=1)
    assert egress_guard.blocked[-1] == "93.184.216.34:80"


def test_guard_blocks_a_raw_connect_and_connect_ex():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        with pytest.raises(OSError, match=EGRESS_BLOCKED):
            s.connect(EXAMPLE_IP)
        with pytest.raises(OSError, match=EGRESS_BLOCKED):
            s.connect_ex(("8.8.8.8", 53))


def test_guard_refuses_a_hostname_without_resolving_it(monkeypatch):
    def no_dns(*_a, **_k):
        raise AssertionError("the guard must refuse before any DNS lookup")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(OSError, match=EGRESS_BLOCKED):
        socket.create_connection(("example.com", 443), timeout=1)


def test_guard_covers_asyncio_and_http_clients():
    async def dial():
        await asyncio.open_connection(*EXAMPLE_IP)

    with pytest.raises(OSError, match=EGRESS_BLOCKED):
        asyncio.run(dial())
    httpx = pytest.importorskip("httpx")
    with pytest.raises(httpx.ConnectError, match=EGRESS_BLOCKED):
        httpx.get("http://93.184.216.34/", timeout=1)


def test_guard_allows_loopback():
    with _loopback_listener() as port:
        with contextlib.closing(socket.create_connection(("127.0.0.1", port), 2)):
            pass
        with contextlib.closing(socket.create_connection(("localhost", port), 2)):
            pass
        with socket.socket() as s:
            s.settimeout(2)
            s.connect(("127.0.0.1", port))
    if socket.has_ipv6:
        try:
            ctx = _loopback_listener(socket.AF_INET6, "::1")
            port6 = ctx.__enter__()
        except OSError:
            return                                   # no IPv6 loopback on this box
        try:
            with contextlib.closing(socket.create_connection(("::1", port6), 2)):
                pass
        finally:
            ctx.__exit__(None, None, None)


def test_guard_allows_a_udp_route_pick_and_unix_sockets(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))              # picks a route; sends nothing
        except OSError as exc:                       # no route is fine; the guard is not
            assert EGRESS_BLOCKED not in str(exc)
    path = str(tmp_path / "s.sock")
    if len(path) < 100 and hasattr(socket, "AF_UNIX"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(path)
            srv.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.connect(path)


@pytest.mark.parametrize("host, local", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("[::1]", True),
    ("localhost", True), ("LOCALHOST", True), ("0.0.0.0", True), ("::", True),
    ("::ffff:127.0.0.1", True), ("", True),
    ("93.184.216.34", False), ("8.8.8.8", False), ("10.0.0.1", False),
    ("192.168.1.1", False), ("2001:4860:4860::8888", False),
    ("example.com", False), ("localhost.example.com", False), ("127.0.0.1.nip.io", False),
    (None, False), (b"127.0.0.1", True),
])
def test_is_local_host(host, local):
    if not local and host in _own_addresses():
        pytest.skip(f"{host} is this machine's own address here")
    assert is_local_host(host) is local


def test_this_machines_own_address_is_local():
    # test_host.py black-box-probes the sim on the LAN address; that connect
    # never leaves the machine, so the guard must not turn it into a no-op.
    for addr in _own_addresses():
        assert is_local_host(addr)


@pytest.mark.live_net
def test_live_net_tests_are_skipped_unless_opted_in():
    # Only ever runs under GODSEYE_LIVE_NET=1, and then with the guard lifted.
    assert os.environ.get(LIVE_NET_ENV) == "1"
    assert NO_EGRESS_ENV not in os.environ


# ------------------------------------------------------------------ wargame_ok (M14a, B3b)

#: WG spec §5.2.4: which rows a simulated wargame session may start in.
WARGAME_OK = {
    "default": True,          # AirSim's stock synthetic origin
    "iran-isfahan": False,    # urban AO beside declared facilities
    "iran-natanz": False,     # the home coordinates name the facility
    "iran-fordow": False,     # the home coordinates name the facility
    "indo-pak-loc": False,    # a live line of control
    "ukraine-donbas": False,  # an active front line
    "taiwan-strait": False,   # maritime: ground templates can't be placed
    "red-sea-hormuz": False,  # maritime: ground templates can't be placed
}


def test_wargame_ok_table_is_exact_and_exhaustive():
    # A new preset row has to take a position here, not inherit the default.
    assert set(theaters.ids()) == set(WARGAME_OK)
    assert {t.id: t.wargame_ok for t in theaters.all_theaters()} == WARGAME_OK
    for tid, ok in WARGAME_OK.items():
        assert theaters.get(tid).wargame_ok is ok


def test_a_dynamic_theater_is_cleared_and_stays_cleared_through_registration():
    t = _make()
    assert t.wargame_ok is True
    assert theaters.register_dynamic(t).wargame_ok is True
    assert theaters.get(t.id).wargame_ok is True
    # register_dynamic's own replace(dynamic=True) keeps the flag
    stored = theaters.register_dynamic(dataclasses.replace(t, dynamic=False))
    assert stored.dynamic is True and stored.wargame_ok is True


def test_wargame_ok_is_outside_the_row_equality_and_hash():
    for t in [*theaters.all_theaters(), _make()]:
        assert "wargame_ok" not in t.as_dict()
        assert "wargame_ok" not in json.dumps(t.as_dict())
        flipped = dataclasses.replace(t, wargame_ok=not t.wargame_ok)
        assert flipped == t and hash(flipped) == hash(t)
        assert flipped.wargame_ok is (not t.wargame_ok)


@pytest.mark.parametrize("tid", sorted(WARGAME_OK))
def test_from_dict_keeps_a_preset_rows_table_value(tid):
    # The row carries no flag, so a static row read back from disk (or any
    # export) can never clear a preset the table refuses.
    row = json.loads(json.dumps(theaters.get(tid).as_dict()))
    assert theaters.from_dict(row).wargame_ok is WARGAME_OK[tid]


def test_from_dict_clears_a_dynamic_row_and_fails_closed_on_any_other_id():
    row = json.loads(json.dumps(_make().as_dict()))
    assert theaters.from_dict(row).wargame_ok is True
    stray = {**theaters.get("default").as_dict(), "id": "somewhere-else"}
    assert theaters.from_dict(stray).wargame_ok is False
    # ...even when the row tries to say otherwise
    assert theaters.from_dict({**stray, "wargame_ok": True}).wargame_ok is False


@pytest.mark.parametrize("value", [1, "true", "yes", None, [True]])
def test_only_the_literal_true_clears_a_theater(value):
    t = dataclasses.replace(_make(), wargame_ok=value)
    assert t.wargame_ok is False
    assert dataclasses.replace(_make(), wargame_ok=True).wargame_ok is True


# -------------------------------------- wargame_ok near refused presets (review fix)

#: Preset rows the table refuses (WG §5.2.4); a runtime AO over any of them is too.
_REFUSED = sorted(tid for tid, ok in WARGAME_OK.items() if not ok)


@pytest.mark.parametrize("tid", _REFUSED)
def test_a_dynamic_ao_on_a_refused_presets_home_is_not_cleared(tid):
    pre = theaters.get(tid)
    t = _make("Field AO", center=(pre.home_lat, pre.home_lon), half=2200.0,
              home_alt_msl_m=pre.home_alt_msl_m)
    assert t.wargame_ok is False
    assert theaters.register_dynamic(t).wargame_ok is False
    assert theaters.get(t.id).wargame_ok is False
    # read back from disk (theater.json) it stays refused
    row = json.loads(json.dumps(t.as_dict()))
    assert theaters.from_dict(row).wargame_ok is False
    # every POI of the preset, too
    for poi in pre.pois:
        assert _make("POI AO", center=(poi.lat, poi.lon), half=1000.0).wargame_ok is False


def test_the_preset_margin_is_a_few_km_beyond_the_preset_box():
    nat = theaters.get("iran-natanz")
    north = max(lat for lat, _ in nat.ao)
    half = 1000.0

    def ao_north_of_the_box(gap_m: float) -> theaters.Theater:
        clat = north + (gap_m + half) / 111_320.0
        return _make("North AO", center=(clat, nat.home_lon), half=half)

    margin = theaters.WARGAME_PRESET_MARGIN_M
    assert ao_north_of_the_box(margin - 1000.0).wargame_ok is False
    assert ao_north_of_the_box(margin + 1000.0).wargame_ok is True
    assert theaters.wargame_clear_of_presets(nat.ao) is False
    assert theaters.wargame_clear_of_presets(theaters.get("default").ao) is True
    for bad in ((), None, [("x", 1.0)]):
        assert theaters.wargame_clear_of_presets(bad) is False       # fails closed
