"""JSONL persistence tests (PLAN §4.8, T4c, T5, M11/M12)."""
import json
import threading
import time
from dataclasses import dataclass

from godseye_uav.store import (ABORT_RTH, RESUME, JsonlJournal, RecordStore,
                               Store, to_record)


def test_journal_roundtrip(tmp_path):
    j = JsonlJournal(tmp_path / "a.jsonl")
    j.append({"event": "one", "n": 1})
    j.append({"event": "two", "n": 2})
    j.close()
    rows = list(JsonlJournal(tmp_path / "a.jsonl").read_all())
    assert [r["event"] for r in rows] == ["one", "two"]
    assert all("ts" in r for r in rows)


def test_journal_skips_corrupt_tail(tmp_path):
    p = tmp_path / "b.jsonl"
    j = JsonlJournal(p)
    j.append({"ok": True})
    j.close()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"broken": tru')  # crash mid-write
    rows = list(JsonlJournal(p).read_all())
    assert len(rows) == 1
    assert rows[0]["ok"] is True


def test_journal_read_missing_file(tmp_path):
    assert list(JsonlJournal(tmp_path / "nope.jsonl").read_all()) == []


def test_store_streams_and_filters(tmp_path):
    with Store(tmp_path) as store:
        store.log_task({"task_id": "t1", "tool": "uav_takeoff"}, "submitted")
        store.log_mission("m1", "started", vehicle="Drone1")
        store.log_mission("m2", "started", vehicle="Drone2")
        store.log_mission("m1", "completed", progress_pct=100)
        store.log_audit("bingo", "BINGO reached, force RTB", vehicle="Drone1")
        store.log_fuel("Drone1", 87.5, "cruise")

        m1 = store.mission_events("m1")
        assert [r["event"] for r in m1] == ["started", "completed"]
        assert store.audit_tail(1)[0]["kind"] == "bingo"

        fuel_rows = list(store.fuel.read_all())
        assert fuel_rows[0]["fuel_pct"] == 87.5
        assert fuel_rows[0]["phase"] == "cruise"

        task_rows = list(store.tasks.read_all())
        assert task_rows[0]["event"] == "submitted"
        assert task_rows[0]["tool"] == "uav_takeoff"


def test_store_files_created(tmp_path):
    with Store(tmp_path) as store:
        store.log_audit("test", "hello")
    for name in ("tasks.jsonl", "missions.jsonl", "audit.jsonl", "fuel.jsonl"):
        assert (tmp_path / name).exists()
    # audit is fsync'd and readable as plain JSONL
    line = (tmp_path / "audit.jsonl").read_text().strip().splitlines()[0]
    assert json.loads(line)["message"] == "hello"


# ---------------------------------------------------------------- durability


def test_memory_store_touches_no_files(tmp_path, monkeypatch):
    """":memory:" is a real in-memory store, not a directory of that name."""
    monkeypatch.chdir(tmp_path)
    with Store(":memory:") as store:
        assert store.in_memory and store.root is None
        store.log_audit("boot", "hello")
        store.log_fuel("Drone1", 99.0, "cruise")
        assert store.audit_tail(1)[0]["message"] == "hello"
        assert store.fuel_state("Drone1")["fuel_pct"] == 99.0
    assert list(tmp_path.iterdir()) == []


def test_torn_write_cannot_corrupt_earlier_records(tmp_path):
    """A crash mid-write loses only the record in flight (T4c)."""
    p = tmp_path / "c.jsonl"
    j = JsonlJournal(p)
    j.append({"n": 1})
    j.append({"n": 2})
    j.close()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"n": 3, "partia')  # power cut here

    j2 = JsonlJournal(p)  # restart: the torn tail is truncated on open
    assert j2.torn_bytes > 0
    j2.append({"n": 4})   # must NOT be glued onto the torn line
    j2.close()
    rows = [r["n"] for r in JsonlJournal(p).read_all()]
    assert rows == [1, 2, 4]


def test_journal_appends_are_thread_safe(tmp_path):
    """The bridge and the MCP server write to the same journals."""
    j = JsonlJournal(tmp_path / "t.jsonl")

    def worker(base: int) -> None:
        for i in range(40):
            j.append({"n": base + i})

    threads = [threading.Thread(target=worker, args=(t * 100,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    j.close()
    rows = list(JsonlJournal(tmp_path / "t.jsonl").read_all())
    assert len(rows) == 320
    assert len({r["n"] for r in rows}) == 320  # no interleaved/lost writes


# ------------------------------------------------- generic intel record store


@dataclass
class _FakeTrack:
    track_id: str
    category: str
    history: list


class _FakePoi:
    def __init__(self, poi_id: str, hits: int):
        self.poi_id = poi_id
        self.hits = hits
        self._private = "not serialized"

    def to_dict(self) -> dict:
        return {"poi_id": self.poi_id, "hits": self.hits}


def test_record_store_is_generic_over_the_record_shape(tmp_path):
    """M11/M12: dataclass, mapping or to_dict() — ids are auto-detected."""
    with Store(tmp_path) as store:
        store.tracks.put(_FakeTrack("TRK-001", "armor", [(1.0, 2.0, 3.0)]))
        store.tracks.put({"track_id": "TRK-002", "category": "sam"})
        store.pattern_of_life.put(_FakePoi("POI-A", 3))
        store.pattern_of_life.put({"id": "POI-B", "hits": 1})

        assert sorted(store.tracks.ids()) == ["TRK-001", "TRK-002"]
        assert store.tracks.get("TRK-001")["category"] == "armor"
        assert store.pattern_of_life.get("POI-A") == {"poi_id": "POI-A", "hits": 3}
        assert "POI-B" in store.pattern_of_life
        assert len(store.tracks) == 2


def test_record_store_last_write_wins_delete_and_compact(tmp_path):
    with Store(tmp_path) as store:
        rs = store.tracks
        rs.put({"track_id": "TRK-001", "sightings": 1})
        rs.put({"track_id": "TRK-001", "sightings": 9})
        rs.put({"track_id": "TRK-002", "sightings": 1})
        assert rs.get("TRK-001")["sightings"] == 9

        rs.delete("TRK-002")
        assert rs.get("TRK-002") is None
        assert rs.compact() == 1
        assert list(rs.all()) == ["TRK-001"]
        assert len(list(rs.journal.read_all())) == 1  # log rewritten to live set


def test_intel_stores_survive_a_restart(tmp_path):
    """Track ids must not restart at TRK-001 on every launch (M11)."""
    with Store(tmp_path) as store:
        store.tracks.put({"track_id": "TRK-007", "category": "radar"})
        store.pattern_of_life.put({"poi_id": "POI-A", "by_hour": {"03": 2}})

    with Store(tmp_path) as reopened:
        assert reopened.tracks.get("TRK-007")["category"] == "radar"
        assert reopened.pattern_of_life.get("POI-A")["by_hour"] == {"03": 2}
        report = reopened.replay()
        assert report.tracks == 1 and report.pattern_of_life == 1


def test_to_record_rejects_a_non_serializable_record():
    try:
        to_record(object())
    except TypeError as exc:
        assert "cannot serialize" in str(exc)
    else:
        raise AssertionError("expected TypeError")


# ----------------------------------------------------- fuel integrals (T5/M4)


def test_fuel_integrals_are_persisted_and_recovered(tmp_path):
    """PLAN §4.5 requires the integrator to persist; replay reads it back."""
    with Store(tmp_path) as store:
        store.log_fuel("Drone1", 88.0, "cruise", bingo_fuel_pct=24.0)
        store.log_fuel("Drone1", 71.5, "cruise", bingo_fuel_pct=24.5)
        store.log_fuel("Drone2", 55.0, "hover")
    with Store(tmp_path) as reopened:
        state = reopened.fuel_state()
        assert state["Drone1"]["fuel_pct"] == 71.5
        assert state["Drone1"]["bingo_fuel_pct"] == 24.5
        assert state["Drone2"]["phase"] == "hover"
        assert reopened.fuel_state("nobody") == {}


# ------------------------------------------- restart replay (T4c, PLAN §4.8)


def _interrupt(store, vehicle="Drone1", task_id="t1", params=None, ts=None,
               tool="uav_fly_route"):
    """Journal a task that was still in flight when the process died."""
    ts = time.time() if ts is None else ts
    handle = {"task_id": task_id, "tool": tool, "vehicle": vehicle, "state": "queued"}
    store.log_task(handle, "submitted", ts=ts,
                   params={"waypoints": [{"lat": 1.0, "lon": 2.0, "alt_m": 60.0}],
                           "speed_mps": 8.0} if params is None else params)
    store.log_task({**handle, "state": "executing"}, "started", ts=ts)
    return handle


def test_replay_resumes_a_healthy_interrupted_task(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 70.0, "cruise", bingo_fuel_pct=25.0)
    with Store(tmp_path) as reopened:
        report = reopened.replay()
        assert len(report.tasks) == 1
        rec = report.tasks[0]
        assert rec.decision == RESUME and rec.tool == "uav_fly_route"
        assert rec.params["speed_mps"] == 8.0  # command is reconstructable
        assert report.decision_for("Drone1") == RESUME
        assert report.vehicles() == ["Drone1"]


def test_replay_aborts_and_rths_at_bingo(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 21.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "BINGO" in report.tasks[0].reason


def test_replay_aborts_when_the_fuel_clock_is_unknown(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)  # no fuel record at all
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "fuel" in report.tasks[0].reason


def test_replay_aborts_on_a_stale_log(tmp_path):
    old = time.time() - 4000.0
    with Store(tmp_path) as store:
        _interrupt(store, ts=old)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0, ts=old)
        report = store.replay(stale_after_s=900.0)
    assert report.tasks[0].decision == ABORT_RTH
    assert "stale" in report.tasks[0].reason


def test_replay_aborts_when_params_were_never_journaled(tmp_path):
    """A handle without params cannot be resumed — recover to RTH instead."""
    with Store(tmp_path) as store:
        store.log_task({"task_id": "t9", "tool": "uav_goto_gps", "vehicle": "Drone1",
                        "state": "executing"}, "started")
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "params" in report.tasks[0].reason


def test_replay_aborts_after_a_safety_event(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        store.log_audit("geofence", "breach, forcing RTB", vehicle="Drone1")
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "geofence" in report.tasks[0].reason


def test_replay_ignores_completed_work(tmp_path):
    with Store(tmp_path) as store:
        handle = _interrupt(store, task_id="done-1")
        store.log_task({**handle, "state": "done"}, "completed")
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
    assert report.tasks == [] and report.vehicles() == []
    assert report.decision_for("Drone1") == RESUME


def test_replay_recovers_interrupted_missions(tmp_path):
    with Store(tmp_path) as store:
        store.log_mission("m1", "started", vehicle="Drone1", kind="grid_search")
        store.log_mission("m2", "started", vehicle="Drone2", kind="recon_route")
        store.log_mission("m2", "completed", vehicle="Drone2")
        store.log_fuel("Drone1", 80.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
    assert [m.id for m in report.missions] == ["m1"]
    assert report.missions[0].decision == RESUME
    assert report.missions[0].fields["kind"] == "grid_search"


def test_replay_any_abort_dominates_for_a_vehicle(tmp_path):
    """Safety is not a majority vote: one abort RTHs the whole vehicle."""
    with Store(tmp_path) as store:
        _interrupt(store, task_id="ok-1")
        _interrupt(store, task_id="bad-1", params={})  # unreconstructable
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
    assert {r.decision for r in report.tasks} == {RESUME, ABORT_RTH}
    assert report.decision_for("Drone1") == ABORT_RTH
    assert report.as_dict()["vehicles"]["Drone1"] == ABORT_RTH


def test_replay_can_record_its_decision_in_the_audit_trail(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)
        store.replay(record=True)
        assert store.audit_tail(1)[0]["kind"] == "replay"


# --------------------------------------------------------- sim_reset (M12)


def test_reset_preserves_tracks_and_pattern_of_life(tmp_path):
    """PLAN §4.4: sim_reset does NOT wipe the track store / pattern-of-life."""
    with Store(tmp_path) as store:
        store.tracks.put({"track_id": "TRK-001", "category": "armor"})
        store.pattern_of_life.put({"poi_id": "POI-A", "by_hour": {"07": 4}})
        _interrupt(store)
        store.log_fuel("Drone1", 60.0, "cruise")

        out = store.reset()
        assert set(out["cleared"]) == {"tasks", "missions", "fuel"}
        assert "tracks" in out["preserved"] and "pattern_of_life" in out["preserved"]

        # per-run state is gone: nothing to resume after a reset
        assert store.replay().tasks == []
        assert store.fuel_state() == {}
        # intel survived
        assert store.tracks.get("TRK-001")["category"] == "armor"
        assert store.pattern_of_life.get("POI-A")["by_hour"] == {"07": 4}
        # and the audit trail is never wiped
        assert any(r["kind"] == "sim_reset" for r in store.audit_tail())


def test_reset_archives_rather_than_deletes(tmp_path):
    with Store(tmp_path) as store:
        _interrupt(store)
        out = store.reset()
    assert out["archived"], "per-run journals must be archived for debrief"
    archived = list((tmp_path / "archive").glob("tasks.*.jsonl"))
    assert len(archived) == 1
    assert any(r.get("event") == "started" for r in JsonlJournal(archived[0]).read_all())


def test_reset_wipes_intel_only_when_explicitly_asked(tmp_path):
    with Store(tmp_path) as store:
        store.tracks.put({"track_id": "TRK-001"})
        out = store.reset(preserve_intel=False)
    assert "tracks" in out["cleared"]
    with Store(tmp_path) as reopened:
        assert reopened.tracks.all() == {}


def test_memory_store_supports_reset_and_replay():
    with Store(":memory:") as store:
        store.tracks.put({"track_id": "TRK-001"})
        _interrupt(store)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        assert store.replay().tasks[0].decision == RESUME
        store.reset()
        assert store.replay().tasks == []
        assert store.tracks.get("TRK-001") is not None


# ------------------------------------------- adversarial verification (T4c/M4)


def test_corrupt_record_mid_journal_is_counted_not_hidden(tmp_path):
    """A damaged record anywhere but the tail can only be skipped — but skipping
    it silently turned a COMPLETED task back into in-flight work (T4c)."""
    p = tmp_path / "tasks.jsonl"
    j = JsonlJournal(p)
    j.append({"task_id": "t1", "vehicle": "Drone1", "event": "submitted",
              "params": {"waypoints": []}})
    j.append({"task_id": "t1", "vehicle": "Drone1", "event": "started"})
    j.append({"task_id": "t1", "vehicle": "Drone1", "event": "completed"})
    j.close()
    lines = p.read_text().splitlines()
    lines[2] = lines[2][:30]                       # damaged, but NOT the tail
    p.write_text("\n".join(lines) + "\n")

    with Store(tmp_path) as store:
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        report = store.replay()
        assert store.tasks.torn_bytes == 0         # repair-on-open cannot help here
        assert store.tasks.corrupt_records == 1
        assert report.corrupt_records >= 1         # the log is known-incomplete
        # reading twice must not double-count the same damaged record
        list(store.tasks.read_all())
        assert store.tasks.corrupt_records == 1


def test_replay_aborts_on_a_safety_event_with_no_vehicle_field(tmp_path):
    """An unattributed BINGO used to be dropped and the task resumed (M4)."""
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        store.log_audit("bingo", "BINGO reached, force RTB")   # no vehicle=
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "bingo" in report.tasks[0].reason
    assert report.decision_for("Drone1") == ABORT_RTH


def test_replay_aborts_when_the_fuel_clock_is_stale(tmp_path):
    """A 6 h old integral is not proof the reserve is intact (T5)."""
    with Store(tmp_path) as store:
        store.log_fuel("Drone1", 95.0, "cruise", bingo_fuel_pct=25.0,
                       ts=time.time() - 6 * 3600)
        _interrupt(store)                       # the task itself is current
        report = store.replay(stale_after_s=900.0)
    assert report.tasks[0].decision == ABORT_RTH
    assert "fuel clock stale" in report.tasks[0].reason


def test_replay_survives_a_null_bingo_field(tmp_path):
    """`bingo_fuel_pct=None` used to crash replay with a TypeError."""
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=None)
        report = store.replay()
    assert report.tasks[0].decision == RESUME
    assert "BINGO 20.0%" in report.tasks[0].reason      # fell back to the default


def test_read_all_snapshot_is_not_torn_by_a_concurrent_rotate(tmp_path):
    """reset() on another thread must not empty a read that is already running."""
    j = JsonlJournal(tmp_path / "a.jsonl")
    for n in range(5):
        j.append({"n": n})
    stream = j.read_all()
    first = next(stream)                 # snapshot taken here, under the lock
    j.rotate(archive_dir=tmp_path / "archive")
    rest = [r["n"] for r in stream]
    assert first["n"] == 0 and rest == [1, 2, 3, 4]
    assert list(j.read_all()) == []      # the rotate did happen
    j.close()


def test_record_store_refuses_to_key_a_record_on_its_display_name(tmp_path):
    """`name` is a label, not an identity: two SA-6 sites share one (M11)."""
    with Store(tmp_path) as store:
        try:
            store.tracks.put({"name": "SA-6_site", "category": "sam"})
        except KeyError as exc:
            assert "id field" in str(exc)
        else:
            raise AssertionError("a record with no identity field must raise")


def test_explicit_record_id_is_written_into_the_record(tmp_path):
    """Otherwise the same record put twice lands under two different ids."""
    with Store(tmp_path) as store:
        store.tracks.put({"category": "sam"}, record_id="TRK-042")
        assert store.tracks.get("TRK-042")["id"] == "TRK-042"
        store.tracks.put(store.tracks.get("TRK-042"))      # no explicit id
        assert store.tracks.ids() == ["TRK-042"]


def test_replay_of_the_journal_the_server_actually_writes_aborts(tmp_path):
    """Wiring debt, pinned: server.py logs `task.handle()` with no params and
    never calls log_fuel, so every real restart recovers to RTH (T4c)."""
    with Store(tmp_path) as store:
        handle = {"task_id": "T1", "tool": "uav_fly_route", "vehicle": "Drone1",
                  "state": "queued", "progress_pct": 0.0,
                  "submitted_at": time.time(), "error": None}
        store.log_task(handle, "submitted")                 # exactly server.py
        store.log_task({**handle, "state": "executing"}, "started")
        report = store.replay()
    assert report.tasks[0].decision == ABORT_RTH
    assert "no persisted fuel integral" in report.tasks[0].reason


def test_journals_are_still_readable_after_close(tmp_path):
    """A debrief replays the log after the `with Store(...)` block exits."""
    with Store(tmp_path) as store:
        _interrupt(store)
        store.log_fuel("Drone1", 90.0, "cruise", bingo_fuel_pct=25.0)
        store.log_audit("bingo", "BINGO", vehicle="Drone1")
    assert store.audit_tail(1)[0]["kind"] == "bingo"      # used to raise ValueError
    assert store.replay().tasks[0].decision == ABORT_RTH
    assert store.fuel_state("Drone1")["fuel_pct"] == 90.0


def test_memory_reset_drops_rows_and_reports_no_archive():
    """In memory there is nowhere to archive to; the caller must see that."""
    with Store(":memory:") as store:
        _interrupt(store)
        out = store.reset()
    assert out["archived"] == []
    assert out["cleared"] == ["tasks", "missions", "fuel"]


def test_record_store_can_pin_its_id_field(tmp_path):
    rs = RecordStore(JsonlJournal(tmp_path / "r.jsonl"), id_field="poi_id")
    rs.put({"poi_id": "P1", "id": "ignored"})
    assert rs.ids() == ["P1"]
    try:
        rs.put({"name": "no-poi-id"})
    except KeyError as exc:
        assert "poi_id" in str(exc)
    else:
        raise AssertionError("pinned id field must be required")


# ----------- T4c: a closing row whose event and state contradict each other

def test_replay_flags_a_closing_row_that_still_claims_to_be_executing(tmp_path):
    """The shape of a journal that lies about how a task ended.

    Every task in the shipped `.godseye/store/tasks.jsonl` looked like this:
    the `completed` row carried `state: "executing"`, because the outcome was
    written from inside the executor before the queue had decided it. A reader
    that trusts `state` cannot tell a finished mission from an abandoned one,
    so the contradiction is REPORTED — never resolved by quietly believing one
    field over the other.
    """
    with Store(tmp_path) as store:
        handle = {"task_id": "liar-1", "tool": "uav_fly_route",
                  "vehicle": "Drone1", "state": "queued"}
        store.log_task(handle, "submitted", params={"speed_mps": 8.0})
        store.log_task({**handle, "state": "executing"}, "started")
        # the defect: terminal EVENT, non-terminal STATE
        store.log_task({**handle, "state": "executing"}, "completed",
                       result={"ok": True})
        store.log_fuel("Drone1", 70.0, "cruise", bingo_fuel_pct=25.0)
    with Store(tmp_path) as reopened:
        report = reopened.replay()
        # it is still treated as finished (the event is the transition record)
        assert [r.id for r in report.tasks] == []
        assert report.state_event_mismatches == [
            {"task_id": "liar-1", "event": "completed", "state": "executing"}]
        assert report.as_dict()["state_event_mismatches"]


def test_replay_reports_no_mismatch_when_the_closing_row_is_honest(tmp_path):
    with Store(tmp_path) as store:
        handle = {"task_id": "ok-1", "tool": "uav_takeoff",
                  "vehicle": "Drone1", "state": "queued"}
        store.log_task(handle, "submitted", params={"alt_agl_m": 30.0})
        store.log_task({**handle, "state": "executing"}, "started")
        store.log_task({**handle, "state": "done"}, "completed",
                       result={"ok": True})
        store.log_task({"task_id": "ok-2", "tool": "uav_hover",
                        "vehicle": "Drone1", "state": "cancelled"}, "cancelled")
        store.log_fuel("Drone1", 70.0, "cruise", bingo_fuel_pct=25.0)
    with Store(tmp_path) as reopened:
        report = reopened.replay()
        assert report.tasks == []
        assert report.state_event_mismatches == []
