"""JSONL persistence: tasks, missions, audit, fuel, tracks, pattern-of-life.

Append-only JSONL journals under a run directory. One file per stream:
    tasks.jsonl            — every task state transition (+ params, for replay)
    missions.jsonl         — mission lifecycle events
    audit.jsonl            — safety + auth + command audit (BINGO, geofence, LOAL)
    fuel.jsonl             — fuel integrator ticks (T5; PLAN §4.5 "persist (JSONL)")
    tracks.jsonl           — track store, log-structured (M11)
    pattern_of_life.jsonl  — pattern-of-life store, log-structured (M12)

Design:
  - Append-only, write-through, fsync on safety events.
  - Crash-safe reload (T4c): a torn final line from a crash mid-write is
    truncated when the journal is reopened, so a later append can never be
    glued onto half a record and corrupt it. Earlier records are never
    rewritten in place.
  - Restart = replay -> resume-or-abort-and-RTH (T4c): `Store.replay()`
    reconstructs interrupted tasks/missions and the fuel clock from the log
    and returns a per-item decision with a cited reason.
  - `reset()` clears per-run sim state but deliberately PRESERVES the track
    store and pattern-of-life (M12), and never wipes the audit trail.
  - Thread-safe: the bridge and the MCP server both write here.
  - `root=":memory:"` is a real in-memory store (no files touched).

The intel stores are generic on purpose: a record is anything serializable
carrying an id, so `targets.py` / `threat.py` can evolve their Track and
pattern-of-life models without this layer knowing their field names.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

MEMORY = ":memory:"

# Task/mission events that end a unit of work. Anything else left as the last
# event for an id means the process died with that work in flight (T4c).
TERMINAL_TASK_EVENTS = frozenset({"completed", "failed", "cancelled", "done", "aborted"})
# The `state` values `tasking.TaskState` calls terminal. A row whose EVENT says
# the task ended but whose STATE still says "executing" is the exact shape of a
# journal that lies: it is what `.godseye/store/tasks.jsonl` looked like for
# every task on disk while the outcome was written from inside the executor,
# before the queue had decided it. `replay()` counts those rather than picking
# one of the two to believe.
TERMINAL_TASK_STATES = frozenset({"done", "failed", "cancelled"})
TERMINAL_MISSION_EVENTS = frozenset({"completed", "failed", "cancelled", "aborted", "rejected"})
# Audit kinds that are in-flight safety transitions: seeing one of these after
# a task's last transition forces abort-and-RTH on restart.
# NOTE (verification): this set must stay a superset of the `kind=` strings the
# producers actually emit. `server.py` today emits none of these — its safety-
# adjacent kinds are "preflight_reject", "abort" and "task_failed" — so those
# are included: a gate rejection or an abort left in the log at restart is not
# something to resume through. Adding a kind here can only make replay more
# conservative (RESUME -> ABORT_RTH), never less.
SAFETY_AUDIT_KINDS = frozenset({"bingo", "force_rtb", "geofence", "geofence_breach",
                                "lost_link", "link_lost", "loal", "envelope_breach",
                                "abort", "preflight_reject", "task_failed"})

# Recovery decisions (T4c).
RESUME = "resume"
ABORT_RTH = "abort_and_rth"

# Mirrors safety.RESERVE_PCT. Kept as a local constant so the persistence
# layer stays stdlib-only and cannot import-cycle with the safety envelope.
DEFAULT_BINGO_PCT = 20.0
# A log older than this is not a live mission any more: recover to RTH (T4c).
STALE_AFTER_S = 900.0


def _jsonable(obj: Any) -> Any:
    """Last-resort JSON coercion for record fields (enums, sets, dataclasses)."""
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def to_record(obj: Any) -> dict[str, Any]:
    """Coerce a serializable record (dict / dataclass / object) to a dict.

    Generic by design (M11/M12): the intel modules own their models, this
    layer only needs a dict it can write.
    """
    if isinstance(obj, Mapping):
        return dict(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    for attr in ("to_dict", "as_dict", "_asdict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            out = fn()
            if isinstance(out, Mapping):
                return dict(out)
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if not k.startswith("_")}
    raise TypeError(f"cannot serialize record of type {type(obj).__name__}")


class JsonlJournal:
    """Append-only JSONL file (or in-memory buffer when path is ":memory:").

    Thread-safe: append/close/rotate take a lock, so the bridge thread and
    the MCP server thread can both write to the same journal.
    """

    def __init__(self, path: Path | str | None, fsync: bool = False):
        self._lock = threading.RLock()
        self._fsync = fsync
        self._mem: list[str] | None = None
        self.torn_bytes = 0
        # Records dropped by `read_all` because they would not parse. A torn
        # tail is repaired on open; a record damaged anywhere ELSE in the file
        # can only be skipped, and skipping it silently is how a "completed"
        # task comes back as in-flight work on replay (T4c). Counted so the
        # caller can see that the log it is reasoning over is incomplete.
        self.corrupt_records = 0
        if path is None or str(path) == MEMORY:
            self.path = None
            self._mem = []
            self._fh = None
            return
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.torn_bytes = self._repair_tail()
        self._fh = open(self.path, "a", encoding="utf-8")

    @property
    def in_memory(self) -> bool:
        return self._mem is not None

    def _repair_tail(self) -> int:
        """Drop a torn final line left by a crash mid-write (T4c).

        Without this, the next append is concatenated onto the half-written
        record and destroys it too; truncating to the last newline bounds the
        damage to the single record that was in flight.
        """
        if self.path is None or not self.path.exists():
            return 0
        size = self.path.stat().st_size
        if size == 0:
            return 0
        with open(self.path, "rb+") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return 0
            pos, last_nl, chunk = size, -1, 8192
            while pos > 0 and last_nl < 0:
                start = max(0, pos - chunk)
                fh.seek(start)
                idx = fh.read(pos - start).rfind(b"\n")
                if idx >= 0:
                    last_nl = start + idx
                pos = start
            keep = last_nl + 1 if last_nl >= 0 else 0
            fh.truncate(keep)
            fh.flush()
            os.fsync(fh.fileno())
            return size - keep

    def append(self, record: dict[str, Any], sync: bool = False) -> dict[str, Any]:
        """Append one record (a `ts` is stamped unless the caller supplies one)."""
        record = {"ts": time.time(), **record}
        line = json.dumps(record, separators=(",", ":"), default=_jsonable) + "\n"
        with self._lock:
            if self._mem is not None:
                self._mem.append(line)
                return record
            self._fh.write(line)
            self._fh.flush()
            if sync or self._fsync:
                os.fsync(self._fh.fileno())
        return record

    def _snapshot(self) -> list[str]:
        """Lines as of now, taken under the lock (T4c).

        Reading the file outside the lock races `rotate()`: the reader could
        see the file mid-replace and come back empty — silent data loss with
        no exception — or hit the instant between `replace` and the reopen and
        raise FileNotFoundError. Both are worse than holding the lock for the
        length of a read.
        """
        with self._lock:
            if self._mem is not None:
                return list(self._mem)
            # `not closed`: a debrief reads the journals after the Store's
            # `with` block has exited. Flushing a closed handle raised
            # ValueError, so `close()` used to make replay/audit_tail crash.
            if self._fh is not None and not self._fh.closed:
                self._fh.flush()
            if self.path is None:
                return []
            try:
                return self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []

    def read_all(self) -> Iterator[dict[str, Any]]:
        """Yield every intact record in append order; count a corrupt line."""
        seen = 0
        for line in self._snapshot():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                seen += 1
                # high-water mark, not a running total: a second read of the
                # same file must not double-count the same damaged record.
                self.corrupt_records = max(self.corrupt_records, seen)

    def sync(self) -> None:
        """Force the OS to flush this journal to disk (safety checkpoints)."""
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                os.fsync(self._fh.fileno())

    def rotate(self, archive_dir: Path | None = None) -> Path | None:
        """Move the journal aside and start an empty one (sim_reset, M12).

        On a file-backed journal nothing is deleted: the old records land in
        `archive_dir` so a debrief replay of the previous run is still
        possible, and the destination is returned. An in-memory journal has
        nowhere to archive to, so its rows are DROPPED and `None` is returned
        — the caller sees an empty `archived` list, not a silent archive.
        """
        with self._lock:
            self.corrupt_records = 0
            self.torn_bytes = 0
            if self._mem is not None:
                self._mem.clear()
                return None
            self._fh.close()
            dest = None
            if self.path.exists() and self.path.stat().st_size > 0:
                archive_dir = archive_dir or (self.path.parent / "archive")
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / f"{self.path.stem}.{int(time.time())}{self.path.suffix}"
                self.path.replace(dest)
            self._fh = open(self.path, "a", encoding="utf-8")
            return dest

    def close(self) -> None:
        with self._lock:
            try:
                if self._fh is not None:
                    self._fh.close()
            except Exception:
                pass


class RecordStore:
    """Log-structured store of serializable records keyed by an id.

    Backs the track store (M11) and the pattern-of-life store (M12). Writes
    are appends (last write per id wins on load), so it inherits the journal's
    crash-safety; `compact()` rewrites the live set when the log gets long.

    Deliberately generic: the record may be a dataclass, a mapping, or any
    object with `to_dict()`, and the id field is auto-detected from
    `ID_CANDIDATES` unless pinned — so `targets.py`/`threat.py` can change
    their models without touching persistence.
    """

    # Identity fields only. `name` is deliberately NOT here: it is a display
    # label (two SA-6 sites are both "SA-6_site"), so auto-keying on it merges
    # distinct contacts under one id. A record with no identity field raises
    # instead — a loud failure beats a silently mis-keyed track (M11).
    ID_CANDIDATES = ("id", "track_id", "poi_id", "poi", "key")

    def __init__(self, journal: JsonlJournal, id_field: str | None = None):
        self.journal = journal
        self.id_field = id_field
        self._lock = threading.RLock()
        self._cache: dict[str, dict] | None = None

    # ---- id handling ----
    def _id_of(self, record: Mapping[str, Any], explicit: str | None = None) -> str:
        if explicit is not None:
            return str(explicit)
        if self.id_field is not None:
            if self.id_field not in record:
                raise KeyError(f"record has no id field {self.id_field!r}")
            return str(record[self.id_field])
        for cand in self.ID_CANDIDATES:
            if record.get(cand) is not None:
                return str(record[cand])
        raise KeyError(f"record has no id field (tried {', '.join(self.ID_CANDIDATES)})")

    def _load(self) -> dict[str, dict]:
        if self._cache is None:
            cache: dict[str, dict] = {}
            for row in self.journal.read_all():
                rid, op = row.get("id"), row.get("op", "put")
                if rid is None:
                    continue
                if op == "delete":
                    cache.pop(rid, None)
                else:
                    cache[rid] = row.get("rec", {})
            self._cache = cache
        return self._cache

    # ---- writes ----
    def put(self, record: Any, record_id: str | None = None) -> str:
        """Persist one record; returns the id it was stored under.

        An explicit `record_id` is written into the record as well, so the
        same record put again (without the explicit id) resolves to the same
        id instead of landing under a second key.
        """
        rec = to_record(record)
        rid = self._id_of(rec, record_id)
        if record_id is not None:
            rec.setdefault(self.id_field or "id", rid)
        with self._lock:
            self.journal.append({"op": "put", "id": rid, "rec": rec})
            self._load()[rid] = rec
        return rid

    def put_many(self, records: Iterable[Any]) -> list[str]:
        """Persist a batch (e.g. every track after an ingest frame)."""
        return [self.put(r) for r in records]

    def delete(self, record_id: str) -> None:
        """Tombstone a record — the log stays append-only."""
        rid = str(record_id)
        with self._lock:
            self.journal.append({"op": "delete", "id": rid})
            self._load().pop(rid, None)

    def compact(self) -> int:
        """Rewrite the journal with only live records; returns the count kept."""
        with self._lock:
            live = dict(self._load())
            self.journal.rotate()
            for rid, rec in live.items():
                self.journal.append({"op": "put", "id": rid, "rec": rec})
            self._cache = live
            return len(live)

    def clear(self) -> None:
        """Drop every record (only for an explicit, non-M12 wipe)."""
        with self._lock:
            self.journal.rotate()
            self._cache = {}

    # ---- reads ----
    def get(self, record_id: str) -> dict | None:
        with self._lock:
            return self._load().get(str(record_id))

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._load())

    def values(self) -> list[dict]:
        with self._lock:
            return list(self._load().values())

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._load().keys())

    def reload(self) -> dict[str, dict]:
        """Drop the cache and re-read the journal (restart replay, M11/M12)."""
        with self._lock:
            self._cache = None
            return dict(self._load())

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, record_id: object) -> bool:
        return self.get(str(record_id)) is not None

    def __iter__(self) -> Iterator[dict]:
        return iter(self.values())


@dataclasses.dataclass
class Recovered:
    """One interrupted unit of work and what to do with it on restart (T4c)."""

    kind: str                 # "task" | "mission"
    id: str
    vehicle: str | None
    decision: str             # RESUME | ABORT_RTH
    reason: str
    last_event: str | None
    last_ts: float
    tool: str | None = None
    params: dict | None = None
    fields: dict = dataclasses.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class ReplayReport:
    """Result of replaying the log after a restart (PLAN §4.8, T4c)."""

    tasks: list[Recovered] = dataclasses.field(default_factory=list)
    missions: list[Recovered] = dataclasses.field(default_factory=list)
    fuel: dict[str, dict] = dataclasses.field(default_factory=dict)
    tracks: int = 0
    pattern_of_life: int = 0
    torn_journals: int = 0      # journals whose torn final line was repaired
    corrupt_records: int = 0    # records that could not be parsed at all
    #: Tasks whose closing row's `event` and `state` contradict each other.
    #: Each entry is {task_id, event, state}. Non-empty means the log cannot be
    #: trusted to say how those tasks ended, so it is REPORTED — it is not
    #: resolved by quietly preferring one field over the other.
    state_event_mismatches: list[dict] = dataclasses.field(default_factory=list)

    @property
    def interrupted(self) -> list[Recovered]:
        return self.tasks + self.missions

    def vehicles(self) -> list[str]:
        """Vehicles with work left in flight when the process died."""
        seen: list[str] = []
        for r in self.interrupted:
            if r.vehicle and r.vehicle not in seen:
                seen.append(r.vehicle)
        return seen

    def decision_for(self, vehicle: str) -> str:
        """Per-vehicle verdict. Any abort dominates: safety is not majority-vote.

        A vehicle with nothing recovered returns RESUME, which means "no
        interrupted work to recover" — NOT "this vehicle was checked and is
        safe". Callers must not read RESUME for an unknown vehicle as
        clearance; use `vehicles()` to see who the log actually knows about.
        """
        found = [r for r in self.interrupted if r.vehicle == vehicle]
        if not found:
            return RESUME
        return ABORT_RTH if any(r.decision == ABORT_RTH for r in found) else RESUME

    def reasons_for(self, vehicle: str) -> list[str]:
        return [r.reason for r in self.interrupted if r.vehicle == vehicle]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": [r.as_dict() for r in self.tasks],
            "missions": [r.as_dict() for r in self.missions],
            "fuel": self.fuel,
            "tracks": self.tracks,
            "pattern_of_life": self.pattern_of_life,
            "torn_journals": self.torn_journals,
            "corrupt_records": self.corrupt_records,
            "state_event_mismatches": list(self.state_event_mismatches),
            "vehicles": {v: self.decision_for(v) for v in self.vehicles()},
        }


class Store:
    """Run-scoped persistence rooted at a directory (or ":memory:")."""

    def __init__(self, root: str | Path, fsync_fuel: bool = False):
        self.in_memory = root is None or str(root) == MEMORY
        self.root: Path | None
        if self.in_memory:
            self.root = None
            paths = {k: None for k in
                     ("tasks", "missions", "audit", "fuel", "tracks", "pattern_of_life")}
        else:
            self.root = Path(root)
            self.root.mkdir(parents=True, exist_ok=True)
            paths = {k: self.root / f"{k}.jsonl" for k in
                     ("tasks", "missions", "audit", "fuel", "tracks", "pattern_of_life")}
        self._lock = threading.RLock()
        self.tasks = JsonlJournal(paths["tasks"])
        self.missions = JsonlJournal(paths["missions"])
        self.audit = JsonlJournal(paths["audit"], fsync=True)
        self.fuel = JsonlJournal(paths["fuel"], fsync=fsync_fuel)
        # Intel stores — these survive sim_reset (M12).
        self.tracks = RecordStore(JsonlJournal(paths["tracks"]))
        self.pattern_of_life = RecordStore(JsonlJournal(paths["pattern_of_life"]))
        self._journals = (self.tasks, self.missions, self.audit, self.fuel,
                          self.tracks.journal, self.pattern_of_life.journal)

    # ---- writes ----
    def log_task(self, task_handle: dict, event: str, **fields: Any) -> None:
        """Journal a task transition. Pass `params=` on submit so a restart can
        actually reconstruct the command (T4c) — a handle alone cannot, because
        `tasking.Task.handle()` has no params field. Without it every task in
        the log replays as ABORT_RTH.

        The CLOSING transition must be logged with the task's real terminal
        state in the handle, i.e. after the queue has set it — `replay()`
        reports any row whose event and state disagree as a
        `state_event_mismatch` rather than choosing which one to believe.

        `event` is written last: a handle dict that happens to carry an
        "event" key must not silently relabel the transition that replay's
        terminal-state detection reads.
        """
        self.tasks.append({**task_handle, **fields, "event": event})

    def log_mission(self, mission_id: str, event: str, **fields: Any) -> None:
        self.missions.append({"mission_id": mission_id, "event": event, **fields})

    def log_audit(self, kind: str, message: str, **fields: Any) -> None:
        self.audit.append({"kind": kind, "message": message, **fields})

    def log_link_event(self, vehicle: str, state: str, **fields: Any) -> None:
        """Lost-link / LOAL transition (M9) — PLAN §4.8 names it in the log."""
        self.audit.append({"kind": "loal", "message": f"link {state}",
                           "vehicle": vehicle, "link_state": state, **fields})

    def log_fuel(self, vehicle: str, fuel_pct: float, phase: str, **fields: Any) -> None:
        """Persist one fuel integrator tick (T5; PLAN §4.5 requires this).

        Pass `bingo_fuel_pct=` so a restart can compare the recovered fuel
        clock against the BINGO line instead of guessing the reserve (M4).
        """
        self.fuel.append({"vehicle": vehicle, "fuel_pct": round(float(fuel_pct), 3),
                          "phase": phase, **fields})

    def sync(self) -> None:
        """fsync every journal — call at safety transitions (BINGO, breach)."""
        for j in self._journals:
            j.sync()

    # ---- reads ----
    def mission_events(self, mission_id: str) -> list[dict]:
        return [r for r in self.missions.read_all() if r.get("mission_id") == mission_id]

    def audit_tail(self, limit: int = 50) -> list[dict]:
        rows = list(self.audit.read_all())
        return rows[-limit:]

    def fuel_state(self, vehicle: str | None = None) -> dict:
        """Last persisted fuel record per vehicle (the recovered fuel clock, T5)."""
        latest: dict[str, dict] = {}
        for row in self.fuel.read_all():
            v = row.get("vehicle")
            if v is not None:
                latest[v] = row
        if vehicle is not None:
            return latest.get(vehicle, {})
        return latest

    # ---- restart recovery (T4c) ----
    def _fold(self, journal: JsonlJournal, id_key: str,
              terminal: frozenset) -> dict[str, dict]:
        """Fold a journal into one merged record per id, newest fields last.

        Merging (rather than keeping only the last row) is what lets a restart
        see the `params` logged on "submitted" together with the state from a
        later "started" row.
        """
        merged: dict[str, dict] = {}
        for row in journal.read_all():
            rid = row.get(id_key)
            if rid is None:
                continue
            cur = merged.setdefault(str(rid), {})
            cur.update(row)
            cur["_last_event"] = row.get("event")
            cur["_last_ts"] = float(row.get("ts", 0.0))
            cur["_terminal"] = row.get("event") in terminal
        return merged

    def _safety_events(self) -> tuple[dict[str, dict], dict | None]:
        """Latest in-flight safety transition per vehicle, plus unattributed ones.

        A safety row logged without a `vehicle` field used to be dropped, so a
        BINGO in the audit trail could still replay as RESUME. An unattributed
        safety event now applies to every vehicle: we do not know which drone
        it was about, and guessing "not this one" is the unsafe guess (M4).
        """
        latest: dict[str, dict] = {}
        unattributed: dict | None = None
        for row in self.audit.read_all():
            if row.get("kind") in SAFETY_AUDIT_KINDS:
                v = row.get("vehicle")
                if v is None:
                    unattributed = row
                else:
                    latest[str(v)] = row
        return latest, unattributed

    def _decide(self, vehicle: str | None, last_ts: float, params: dict | None,
                fuel: dict[str, dict], safety: dict[str, dict], now: float,
                stale_after_s: float, bingo_default_pct: float,
                need_params: bool, unattributed: dict | None = None) -> tuple[str, str]:
        """resume-or-abort-and-RTH for one interrupted item (PLAN §4.8, T4c).

        Doctrine: only resume when the reserve is provably intact and the
        command can actually be reconstructed. Everything unknown recovers to
        RTH, because the vehicle is airborne with nobody flying it.
        """
        ev = safety.get(vehicle or "")
        if ev is None or (unattributed is not None
                          and float(unattributed.get("ts", 0.0)) > float(ev.get("ts", 0.0))):
            ev = unattributed if unattributed is not None else ev
        if ev is not None and float(ev.get("ts", 0.0)) >= last_ts - 1e-9:
            return ABORT_RTH, f"safety event {ev.get('kind')} logged at/after the last transition"
        row = fuel.get(vehicle or "") or {}
        if not row:
            return ABORT_RTH, "no persisted fuel integral — reserve unverifiable (PLAN §4.5)"
        # A fuel integral is only evidence of the reserve while it is fresh:
        # an hours-old reading says nothing about a vehicle that kept flying.
        fuel_age = now - float(row.get("ts", 0.0))
        if fuel_age > stale_after_s:
            return ABORT_RTH, (f"fuel clock stale: {fuel_age:.0f}s since the last "
                               f"persisted integral (T5)")
        fuel_pct = float(row.get("fuel_pct") or 0.0)
        bingo_raw = row.get("bingo_fuel_pct")
        bingo = float(bingo_default_pct if bingo_raw is None else bingo_raw)
        if fuel_pct <= bingo:
            return ABORT_RTH, f"fuel {fuel_pct:.1f}% at/below BINGO {bingo:.1f}% (M4)"
        age = now - last_ts
        if age > stale_after_s:
            return ABORT_RTH, f"log stale: {age:.0f}s since the last transition"
        if need_params and not params:
            return ABORT_RTH, "task record carries no params — command not reconstructable"
        return RESUME, (f"fuel {fuel_pct:.1f}% above BINGO {bingo:.1f}%, "
                        f"task log {age:.0f}s old, fuel clock {fuel_age:.0f}s old")

    def replay(self, now: float | None = None, stale_after_s: float = STALE_AFTER_S,
               bingo_default_pct: float = DEFAULT_BINGO_PCT,
               record: bool = False) -> ReplayReport:
        """Reconstruct state from the log on restart (PLAN §4.8: "restart =
        replay -> resume-or-abort-and-RTH", T4c).

        Pure by default: pass `record=True` to also write the recovery decision
        into the audit trail. Tracks and pattern-of-life are reloaded too, so
        track ids stay unique across restarts (M11).
        """
        now = time.time() if now is None else now
        fuel = self.fuel_state()
        safety, unattributed = self._safety_events()
        report = ReplayReport(fuel=fuel)

        for tid, rec in self._fold(self.tasks, "task_id", TERMINAL_TASK_EVENTS).items():
            if rec.get("_terminal"):
                state = rec.get("state")
                if state is not None and state not in TERMINAL_TASK_STATES:
                    # The closing row says the task ended, its own `state` says
                    # it is still running. Surface the contradiction: a reader
                    # that trusts `state` cannot tell a finished mission from an
                    # abandoned one, which is what defeated restart recovery.
                    report.state_event_mismatches.append(
                        {"task_id": tid, "event": rec.get("_last_event"),
                         "state": state})
                continue
            params = rec.get("params")
            vehicle = rec.get("vehicle")
            decision, reason = self._decide(
                vehicle, rec["_last_ts"], params, fuel, safety, now,
                stale_after_s, bingo_default_pct, need_params=True,
                unattributed=unattributed)
            report.tasks.append(Recovered(
                kind="task", id=tid, vehicle=vehicle, decision=decision, reason=reason,
                last_event=rec.get("_last_event"), last_ts=rec["_last_ts"],
                tool=rec.get("tool"), params=params,
                fields={"state": rec.get("state"), "mission_id": rec.get("mission_id")}))

        for mid, rec in self._fold(self.missions, "mission_id", TERMINAL_MISSION_EVENTS).items():
            if rec.get("_terminal"):
                continue
            vehicle = rec.get("vehicle")
            decision, reason = self._decide(
                vehicle, rec["_last_ts"], rec.get("params"), fuel, safety, now,
                stale_after_s, bingo_default_pct, need_params=False,
                unattributed=unattributed)
            report.missions.append(Recovered(
                kind="mission", id=mid, vehicle=vehicle, decision=decision, reason=reason,
                last_event=rec.get("_last_event"), last_ts=rec["_last_ts"],
                tool=rec.get("kind"), params=rec.get("params"),
                fields={k: v for k, v in rec.items() if not k.startswith("_")}))

        report.tracks = len(self.tracks.reload())
        report.pattern_of_life = len(self.pattern_of_life.reload())
        report.torn_journals = sum(1 for j in self._journals if j.torn_bytes)
        report.corrupt_records = sum(j.corrupt_records for j in self._journals)
        if record:
            self.log_audit("replay", "restart replay complete", **report.as_dict())
        return report

    # ---- sim_reset (M12) ----
    def reset(self, preserve_intel: bool = True, reason: str = "sim_reset") -> dict:
        """Clear per-run sim state; the intel stores survive (M12).

        Cleared: tasks, missions, fuel — the per-run journals a replay would
        otherwise try to resume. Preserved: the track store and pattern-of-life
        (PLAN §4.4 "sim_reset does NOT wipe track store / pattern-of-life DB"),
        and the audit trail, which is never wiped.
        """
        with self._lock:
            cleared, archived = [], []
            for name in ("tasks", "missions", "fuel"):
                dest = getattr(self, name).rotate()
                cleared.append(name)
                if dest is not None:
                    archived.append(str(dest))
            preserved = ["audit", "tracks", "pattern_of_life"]
            if not preserve_intel:
                self.tracks.clear()
                self.pattern_of_life.clear()
                cleared += ["tracks", "pattern_of_life"]
                preserved = ["audit"]
            out = {"cleared": cleared, "preserved": preserved, "archived": archived,
                   "tracks": len(self.tracks), "pattern_of_life": len(self.pattern_of_life)}
            self.log_audit("sim_reset", reason, **out)
            return out

    def close(self) -> None:
        for j in self._journals:
            j.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
