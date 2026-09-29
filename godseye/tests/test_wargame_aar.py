"""After-action review, crash persistence, BDA and restart recovery of the
simulated wargame (PLAN §4.5a M14a; WG v2 §5.2.9).

Pure tests for `wargame_aar` and `wargame_bda`, then `recover_on_boot` against
a planted `<store>/wargame.json` on a real server (fake AirSim, no network;
B3's ports 53350-53369).
"""
from __future__ import annotations

import itertools
import json
import os
import time
from types import SimpleNamespace

import airsim
import pytest
from godseye_uav import theaters
from godseye_uav import wargame_aar as A
from godseye_uav import wargame_bda as B
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint, canonical_altitude
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store
from godseye_uav.targets import Track
from godseye_uav.wargame_tables import NOTIONAL_NOTE
from support.wg_tokens import assert_no_real_system_tokens

SID = "WG-a1b2c3"
T0 = 1_759_072_800_000


def event(i: int, kind: str = "red_shot", outcome: str | None = "missed",
          side: str = "red", text: str | None = None) -> dict:
    return {"t_ms": T0 + 1000 * i, "sim_s": float(i), "kind": kind, "side": side,
            "text": text or f"Red SAM 1 fired at Drone1: {outcome} (simulated).",
            "outcome": outcome, "register": "scenario", "simulated": True,
            "_blue_text": "masked", "_blue_hidden": False}


def aar(**kw) -> dict:
    base = {
        "session_id": SID, "theater_id": "default", "started_at_ms": T0,
        "ended_at_ms": T0 + 60_000, "seed": 4417, "time_scale": 2.0, "red_engages": True,
        "reveal_red": False,
        "units": [{"side": "red", "state": "destroyed"}, {"side": "red", "state": "active"},
                  {"side": "blue", "state": "damaged"}],
        "events": [event(0), event(1, "red_shot", "destroyed"),
                   event(2, "blue_strike_executed", "destroyed", "blue",
                         "Blue artillery 1 struck Red SAM 1: destroyed (simulated).")],
        "sorties": [{"vehicle": "Drone1", "missions_flown": 2, "lost": True,
                     "lost_by": "Red SAM 1"}],
        "bda_accuracy": [B.accuracy_row("WG-a1b2c3-E3", "Air-defence contact",
                                        {"state": "destroyed_probable", "looks": 1},
                                        "destroyed", "destroyed", False)],
        "track_ids": ["TRK-a-0001"]}
    base.update(kw)
    return A.build_aar(**base)


# ----------------------------------------------------------------- AAR ------

def test_the_aar_carries_the_contract_fields_and_is_simulated():
    r = aar()
    assert r["report_type"] == "AAR" and r["id"] == r["report_id"] == f"aar-{SID}"
    assert r["resource"] == f"uav://reports/aar-{SID}" and r["incomplete"] is False
    assert r["engine"] == r["table_version"] == "wg-notional/1"
    assert r["simulated"] is True and r["note"] == NOTIONAL_NOTE
    assert r["counts"]["red"] == {"units": 2, "active": 1, "suppressed": 0, "damaged": 0,
                                  "destroyed": 1}
    assert r["counts"]["engagements"] == {"blue_strike": 1, "red_shot": 2, "red_ground": 0}
    assert r["counts"]["drones_lost"] == 1
    assert r["loss_exchange"] == {"red_destroyed": 1, "blue_destroyed": 0, "drones_lost": 1,
                                  "blue_losses": 1, "ratio": 1.0, "simulated": True}
    # The timeline keeps public keys only: no engine fog fields leak.
    assert all(set(e) == {"t_ms", "sim_s", "kind", "side", "text", "outcome", "register",
                          "simulated"} for e in r["timeline"])
    assert r["bda_accuracy"][0]["agrees"] is True and r["track_ids"] == ["TRK-a-0001"]
    assert_no_real_system_tokens(r)


def test_the_markdown_has_the_title_sections_and_names_no_place():
    md = aar()["markdown"]
    assert md.startswith("# After-action review (simulated)\n")
    heads = [line for line in md.splitlines() if line.startswith("## ")]
    assert heads == ["## Summary", "## Timeline", "## Sorties", "## Battle damage accuracy",
                     "## Replay"]
    assert "| Time (Z) | Event | Side | Outcome | Register |" in md
    assert "| 15:20:01Z | Red SAM 1 fired at Drone1: destroyed (simulated). | red | " \
           "destroyed | Scenario |" in md
    assert "- Seed 4417; engine wg-notional/1; sim speed x2" in md
    assert "1 red destroyed for 1 blue loss (0 units, 1 drone)" in md
    assert "| Drone1 | 2 | yes (Red SAM 1) |" in md
    assert "Redmond" not in md and "·" not in md and "SIMULATED" not in md


def test_untrusted_text_cannot_break_a_table_row():
    evil = "Drone|1\n<img src=x onerror=alert(1)>\u202eevil\u202c"
    md = aar(events=[event(0, text=evil)], sorties=[{"vehicle": evil}])["markdown"]
    row = next(line for line in md.splitlines() if "onerror" in line and "15:20:00Z" in line)
    assert row.count(" | ") == 4 and "Drone\\|1" in row
    assert "\u202e" not in md and "\u202c" not in md


def test_the_markdown_stays_under_40_kb_and_says_what_it_dropped():
    many = [event(i, text="Red SAM 1 fired at Drone1: missed (simulated). " + "x" * 60)
            for i in range(A.MARKDOWN_MAX_BYTES // 40)]
    r = aar(events=many)
    assert len(r["markdown"].encode()) <= A.MARKDOWN_MAX_BYTES
    assert "earlier events are omitted here" in r["markdown"]
    assert r["markdown"].rstrip().endswith("events")          # the replay section survives
    assert len(r["timeline"]) == len(many)                     # the report keeps them all


# ---------------------------------------------------------- persistence -----

def test_the_session_file_round_trips_atomically(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    path = A.session_path(store)
    assert path == tmp_path / "wargame.json" and A.session_path(SimpleNamespace(root=None)) \
        is None
    rec = A.session_record(session_id=SID, num=T0, started_at_ms=T0, seed=4417,
                           theater_id="default", object_names=["aaa_towed_1001"],
                           track_ids=["TRK-a-0001"])
    assert set(rec) == {"schema", "session_id", "num", "started_at_ms", "seed", "theater_id",
                        "object_names", "track_ids"}
    assert rec["schema"] == "godseye.wargame-session/v1"
    assert A.read_session(path) is None
    assert A.write_session(path, rec) is True
    assert A.read_session(path) == rec
    assert [p.name for p in tmp_path.iterdir()] == ["wargame.json"]     # no tmp left
    path.write_text("{not json")
    assert "corrupt" in A.read_session(path)
    assert A.remove_session(path) is True and A.remove_session(path) is False
    assert A.write_session(None, rec) is False


def test_a_partial_aar_reads_only_this_sessions_events_from_the_audit_tail():
    rows = ([{"kind": "wargame_event", "session_id": "WG-other", "event": event(0)}]
            + [{"kind": "spawn_target", "message": "x"}] * 3
            + [{"kind": "wargame_event", "session_id": SID, "event": event(i, outcome=o)}
               for i, o in enumerate(["missed", "destroyed"], start=1)])
    rec = {"session_id": SID, "seed": 7, "theater_id": "default", "started_at_ms": T0,
           "track_ids": ["TRK-a-0001"]}
    r = A.partial_aar(rec, rows, ended_at_ms=T0 + 5000)
    assert r["incomplete"] is True and r["reason"] == "restart" and r["id"] == f"aar-{SID}"
    assert [e["outcome"] for e in r["timeline"]] == ["missed", "destroyed"]
    assert r["counts"]["blue"] is None and r["counts"]["drones_lost"] == 1
    assert A.INCOMPLETE_LINE in r["markdown"] and "Red engages: not known" in r["markdown"]
    # Only the last AUDIT_SCAN_ROWS rows are scanned.
    old = [{"kind": "wargame_event", "session_id": SID, "event": event(0)}]
    filler = [{"kind": "tick"}] * A.AUDIT_SCAN_ROWS
    assert A.events_from_audit(old + filler, SID) == []


# ------------------------------------------------------------------ BDA -----

def obs(*ts: float):
    return SimpleNamespace(observations=[SimpleNamespace(ts=t) for t in ts])


def test_looks_count_only_observations_after_the_strike():
    fired = 1000.0 * 1000
    assert B.looks_since([obs(999.0, 1000.0)], fired) == (0, None)
    assert B.looks_since([obs(999.0, 1000.5), obs(1002.0)], fired) == (2, 1_002_000)
    assert B.looks_since([SimpleNamespace(observations=[{"ts": 1001.0}, {"ts": "bad"}])],
                         fired) == (1, 1_001_000)


@pytest.mark.parametrize(("truth", "damaged", "looks", "state"), [
    ("destroyed", True, 0, "none"),
    ("destroyed", True, 1, "destroyed_probable"),
    ("destroyed", True, 2, "destroyed_confirmed"),
    ("damaged", True, 1, "damaged"),
    ("suppressed", True, 3, "damaged"),
    ("suppressed", False, 1, "no_change"),
    ("active", False, 2, "no_change"),
])
def test_bda_tiers(truth, damaged, looks, state):
    tracks = [obs(*(2000.0 + i for i in range(looks)))]
    got = B.assess(truth, damaged, tracks, 1_000_000)
    assert got["state"] == state and got["looks"] == looks
    assert B.outcome_visible(got) is (looks >= 1)


def test_bda_agreement_words_and_tier_changes():
    assert B.agrees("none", "destroyed", True) is None
    assert B.agrees("destroyed_probable", "destroyed", True) is True
    assert B.agrees("damaged", "destroyed", True) is False
    assert B.agrees("damaged", "damaged", True) is True
    assert B.agrees("no_change", "active", False) is True
    assert B.tier_changed(None, {"state": "none"}) is False
    assert B.tier_changed({"state": "none"}, {"state": "damaged"}) is True
    assert B.bda_text("Air-defence contact", {"state": "destroyed_confirmed", "looks": 2}) == \
        "Battle damage assessment on Air-defence contact: destroyed (confirmed) (2 looks)."
    assert_no_real_system_tokens(B.BDA_WORDS)


# ------------------------------------------------- restart recovery (server) --

_PORTS = list(range(53350, 53370))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):]
                        + _PORTS[:os.getpid() % len(_PORTS)])
THEATER = theaters.get("default")
HOME = GeoPoint(THEATER.home_lat, THEATER.home_lon,
                canonical_altitude(THEATER.home_alt_msl_m, THEATER.home_lat,
                                   THEATER.home_lon, datum="msl").alt_hae)


def boot(store_dir):
    """A server booted on `store_dir` (its restart replay runs in __init__)."""
    for _ in range(len(_PORTS)):
        sim = FakeAirSim(home=HOME, port=next(_PORT))
        try:
            sim.start()
            break
        except OSError:
            sim.stop()
    store = Store(store_dir)
    client = airsim.MultirotorClient(port=sim.port)
    client.confirmConnection()
    srv = GodseyeUavServer(UavBackend(client, HOME, sim=sim), store, theater=THEATER)
    return srv, store, sim


def track(tid: str, name: str) -> Track:
    now = time.time()
    return Track(track_id=tid, name=name, category="aaa", lat=HOME.latitude + 0.01,
                 lon=HOME.longitude, alt_m=HOME.altitude, first_seen=now, last_seen=now,
                 sightings=3, ob_class="aaa_towed", scenario=True)


def test_recover_on_boot_deletes_the_tracks_files_a_partial_aar_and_the_file(tmp_path):
    name = "aaa_towed_1759072800123001"
    # The crashed run: a session file, two scenario tracks (one never noted in
    # the file), an ISR track, and the session's events in the audit.
    first = Store(tmp_path)
    for t in (track("TRK-old-0001", name), track("TRK-old-0002", name),
              track("TRK-isr-0003", "supply_truck_7")):
        first.tracks.put(t.to_dict())
    first.log_audit("wargame_event", "x", session_id=SID, event=event(1, outcome="destroyed"))
    first.log_audit("wargame_event", "y", session_id="WG-other", event=event(2))
    first.close()
    A.write_session(tmp_path / "wargame.json", A.session_record(
        session_id=SID, num=T0, started_at_ms=T0, seed=4417, theater_id="default",
        object_names=[name], track_ids=["TRK-old-0001"]))
    srv, store, sim = boot(tmp_path)
    try:
        got = srv.wargame_recovery
        assert got["aborted"] == SID and got["aar_id"] == f"aar-{SID}"
        assert sorted(got["track_ids"]) == ["TRK-old-0001", "TRK-old-0002"]
        assert srv.tracks.get("TRK-old-0001") is None and srv.tracks.get("TRK-old-0002") is None
        assert srv.tracks.get("TRK-isr-0003") is not None              # ISR untouched
        assert store.tracks.get("TRK-old-0001") is None and store.tracks.get("TRK-isr-0003")
        report = srv.reports[f"aar-{SID}"]
        assert report["incomplete"] is True and report["simulated"] is True
        assert [e["outcome"] for e in report["timeline"]] == ["destroyed"]
        assert "latest" not in srv.reports
        assert not (tmp_path / "wargame.json").exists()
        rows = [r for r in store.audit.read_all()
                if r.get("kind") == "wargame_session_aborted_by_restart"]
        assert len(rows) == 1 and rows[0]["session_id"] == SID
        assert srv.wargame.last["aar_id"] == f"aar-{SID}" and srv.wargame.active is False
        assert srv.wargame._last_num >= T0                       # the next num differs
    finally:
        srv.wargame.close()
        srv.stop_monitor()
        srv.tasking.shutdown()
        store.close()
        sim.stop()
    # A clean restart has nothing to recover.
    srv, store, sim = boot(tmp_path)
    try:
        assert srv.wargame_recovery is None
    finally:
        srv.stop_monitor()
        srv.tasking.shutdown()
        store.close()
        sim.stop()


def test_a_corrupt_session_file_is_still_cleaned_up(tmp_path):
    (tmp_path / "wargame.json").write_text("{nope")
    srv, store, sim = boot(tmp_path)
    try:
        assert srv.wargame_recovery["aborted"] == "unknown"
        assert not (tmp_path / "wargame.json").exists()
        [row] = [r for r in store.audit.read_all()
                 if r.get("kind") == "wargame_session_aborted_by_restart"]
        assert row["corrupt"].startswith("JSONDecodeError")
    finally:
        srv.stop_monitor()
        srv.tasking.shutdown()
        store.close()
        sim.stop()
    assert json.dumps(A.public_event(event(0)))                     # JSON-ready
