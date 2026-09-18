"""Per-vehicle command queue + state machine (PLAN T2).

One in-flight command per vehicle. State machine:
    idle → executing → cancelling → aborting → idle

**Busy (T2)**: a command submitted while the vehicle already has non-terminal
work raises `VehicleBusyError` carrying the blocking task, which the server
turns into `{"status": "busy", "current": <handle>}`. The harness decides
whether to wait, cancel or abort — nothing silently queues behind an unbounded
backlog. Deliberate pipelining is still possible, but only by asking for it
(`allow_queue=True`), and the safety layer pre-empts with `privileged=True`.

**Watchdog (T2)**: the timeout keys on *lack of progress*, not total duration.
A task is killed only when nothing has reported forward movement for
`watchdog_s` seconds; a 1 100 s AO crossing that keeps closing on its target
survives, while a genuinely stuck task is caught and handed to the recovery
hook (the server hovers the vehicle). The previous flat 120 s cap on total
duration failed every realistic mission leg in the shipped theaters.

"Forward movement" is measured against the last watchdog RESET
(`Task._progress_mark`), never against the last report — see
`PROGRESS_EPS_PCT`. A SAFETY/un-cancellable task is watchdogged on exactly the
same terms, deliberately: once the clock only fires on a vehicle that has
genuinely stopped, exempting the force-RTB would remove the only detector of a
wedged RTB, and `_force_rtb` re-commits a fresh one when a previous safety task
ends in any terminal state.

**Progress (T4a)**: `Task.report_progress()` is the single place progress is
written. The executor derives it server-side from telemetry (distance-to-target
vs plan) — never from AirSim futures — and registered hooks forward it to the
caller's `notifications/progress` channel.

**Idempotency (T4b)**: a replayed key returns the ORIGINAL task and never
re-executes, including after that task has finished and including across a
restart (the server re-seeds keys from the JSONL journal via
`seed_idempotency`).

**Terminal states (T4c)**: every task ends exactly once, in `_finalize`, which
stamps `finished_at`, files the task in the history and fires the terminal
hooks with the task's TRUE terminal state already set. That single funnel is
what lets the server journal a `done`/`failed`/`cancelled` row the restart
replay can read: before it existed, a task cancelled by `abort()` or pre-empted
by a safety task never reached an executor at all, so the log's last word on it
stayed "queued" forever and `Store.replay()` could not tell a finished mission
from an abandoned one.

Executors are async callables `executor(task, ctx) -> dict` (a result payload).
The queue owns sequencing, the watchdog, idempotency dedup and cancellation —
never AirSim futures directly (T2).
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: A task has to move forward by at least this much SINCE THE LAST WATCHDOG
#: RESET to count as *progress*. Re-reporting the same percentage (a drone
#: hovering 400 m short of its waypoint) must NOT keep the watchdog alive —
#: that is precisely the stuck task the watchdog exists to catch (T2).
#:
#: Measured against `Task._progress_mark`, NEVER against the previous report.
#: Comparing consecutive reports made the threshold scale with route length:
#: telemetry arrives every 0.25 s, so one report advances
#: `speed*0.25/route_m*100` pp, which drops below this epsilon on any route
#: longer than `500*speed_mps` metres — 6 km at 12 m/s. Past that the clock
#: NEVER reset and every task died at exactly `watchdog_s`, however well it was
#: flying. It killed a 16.2 km recon route at 8.9 % after 1 445 m at its
#: commanded speed, and it killed the un-cancellable BINGO force-RTB 8 km from
#: home — the one task whose whole purpose is to get the aircraft back.
PROGRESS_EPS_PCT = 0.05

#: Default no-progress window. Not a cap on how long a leg may take.
DEFAULT_NO_PROGRESS_S = 120.0


class TaskState(str, Enum):
    QUEUED = "queued"
    EXECUTING = "executing"
    CANCELLING = "cancelling"
    ABORTING = "aborting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED}


ProgressHook = Callable[["Task"], Awaitable[None]]


@dataclass
class Task:
    tool: str
    params: dict
    vehicle: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    idempotency_key: str | None = None
    state: TaskState = TaskState.QUEUED
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress_pct: float = 0.0
    progress_note: str | None = None
    waypoint: int | None = None
    waypoints_total: int | None = None
    eta_s: float | None = None
    mission_id: str | None = None
    #: Submitted by the server's own safety layer (BINGO force-RTB, lost-link),
    #: not by the harness.
    safety: bool = False
    #: Harness cancel/abort must REFUSE this task (M4 un-cancellable RTB).
    uncancellable: bool = False
    #: Per-task no-progress window; falls back to the queue's.
    watchdog_s: float | None = None
    result: dict | None = None
    error: str | None = None
    #: What the watchdog's recovery hook did after a timeout (T2 hover-recovery).
    recovery: str | None = None
    #: How many times this task was returned again for a replayed key (T4b).
    replays: int = 0
    #: Monotonic clock of the last *forward* progress; the watchdog reads it.
    progress_at: float = field(default_factory=time.monotonic)
    #: `progress_pct` as it stood at the last watchdog reset — the baseline the
    #: `PROGRESS_EPS_PCT` step is measured from. Progress is only forward
    #: motion if it beats the last RESET; measuring it against the last REPORT
    #: makes a long route (where every individual report is tiny) look stalled.
    #: `VehicleQueue._execute_one` re-seeds it when the task starts.
    _progress_mark: float = field(default=0.0, repr=False)
    progress_hooks: list[ProgressHook] = field(default_factory=list, repr=False)
    #: Hooks that raised are dropped and recorded here — never swallowed.
    progress_errors: list[str] = field(default_factory=list)
    #: True once the queue has run this task through `_finalize` (T4c). Guards
    #: the terminal hooks against firing twice for one task.
    finalized: bool = False
    #: Terminal hooks that raised, recorded rather than swallowed. A journal
    #: write that failed silently is a log that lies about what happened.
    terminal_errors: list[str] = field(default_factory=list)

    # ---- progress (T4a) ----
    async def report_progress(self, pct: float, *, note: str | None = None,
                              waypoint: int | None = None,
                              waypoints_total: int | None = None,
                              eta_s: float | None = None,
                              now: float | None = None) -> float:
        """Record server-derived progress and fan it out to the hooks.

        Progress is monotonic (a re-plan must not make the bar go backwards),
        and the watchdog clock resets when the task has advanced more than
        `PROGRESS_EPS_PCT` SINCE THE LAST RESET — not since the last report.

        The distinction is the whole watchdog. Small steps ACCUMULATE against
        `_progress_mark`, so a 16 km route whose every telemetry report is
        worth 0.02 pp still resets the clock every second or so, while a
        vehicle that has genuinely stopped never crosses the epsilon at all and
        is caught exactly as before.
        """
        now = time.monotonic() if now is None else float(now)
        pct = max(0.0, min(100.0, float(pct)))
        if pct > self._progress_mark + PROGRESS_EPS_PCT:
            self.progress_at = now
            self._progress_mark = pct
        self.progress_pct = max(self.progress_pct, pct)
        if note is not None:
            self.progress_note = note
        if waypoint is not None:
            self.waypoint = int(waypoint)
        if waypoints_total is not None:
            self.waypoints_total = int(waypoints_total)
        if eta_s is not None:
            self.eta_s = float(eta_s)
        for hook in list(self.progress_hooks):
            try:
                await hook(self)
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                self.progress_errors.append(f"{type(exc).__name__}: {exc}")
                try:
                    self.progress_hooks.remove(hook)
                except ValueError:  # pragma: no cover - concurrent removal
                    pass
        return self.progress_pct

    def add_progress_hook(self, hook: ProgressHook) -> None:
        self.progress_hooks.append(hook)

    def stalled_s(self, now: float | None = None) -> float:
        """Seconds since this task last moved forward (watchdog input)."""
        now = time.monotonic() if now is None else float(now)
        return now - self.progress_at

    def handle(self) -> dict:
        return {
            "task_id": self.id,
            "tool": self.tool,
            "vehicle": self.vehicle,
            "state": self.state.value,
            "progress_pct": round(self.progress_pct, 1),
            "waypoint": self.waypoint,
            "waypoints_total": self.waypoints_total,
            "eta_s": (round(self.eta_s, 1) if self.eta_s is not None else None),
            "mission_id": self.mission_id,
            "idempotency_key": self.idempotency_key,
            "uncancellable": self.uncancellable,
            "safety": self.safety,
            "submitted_at": self.submitted_at,
            "error": self.error,
        }


Executor = Callable[[Task, dict], Awaitable[dict]]
#: recovery(task, reason) — called after a watchdog timeout so the server can
#: put the vehicle somewhere safe (T2: "fail handle → hover-recovery").
Recovery = Callable[[Task, str], Awaitable[Any]]
#: terminal(task) — fired ONCE per task, the instant it reaches a terminal
#: state, with that state already written to `task.state` (T4c). Deliberately
#: SYNCHRONOUS: tasks are finalized from `submit()` (safety pre-emption) and
#: from `abort()` as well as from the worker, i.e. from threads that have no
#: running event loop, and a journal append is a sync call anyway.
TerminalHook = Callable[["Task"], None]


class VehicleBusyError(Exception):
    """A command arrived while the vehicle already had non-terminal work (T2)."""

    def __init__(self, current: Task):
        self.current = current
        super().__init__(f"vehicle busy executing {current.tool} ({current.id})")


class WatchdogTimeout(Exception):
    pass


class VehicleQueue:
    """One-in-flight command runner for a single vehicle.

    The worker always runs on the TaskingService's dedicated background loop,
    so submit() is safe from sync callers (tests, bridges), from the MCP
    request loop, and from the server's telemetry monitor alike — all three
    touch the same queue from different threads.
    """

    def __init__(self, vehicle: str, watchdog_s: float = DEFAULT_NO_PROGRESS_S,
                 loop: asyncio.AbstractEventLoop | None = None,
                 max_duration_s: float | None = None, poll_s: float = 0.05):
        self.vehicle = vehicle
        #: No-progress window in seconds (NOT a cap on total duration, T2).
        self.watchdog_s = watchdog_s
        #: Optional absolute ceiling. None = a healthy long leg is unbounded.
        self.max_duration_s = max_duration_s
        self.poll_s = poll_s
        self._loop = loop
        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._lock = threading.RLock()
        self._pending: list[Task] = []
        self._by_id: dict[str, Task] = {}
        self._current: Task | None = None
        self._current_runner: asyncio.Task | None = None
        self._history: list[Task] = []
        self._idem: dict[str, Task] = {}
        self._worker: Any = None
        self._executor: Executor | None = None
        self._recovery: Recovery | None = None
        self._terminal_hooks: list[TerminalHook] = []
        self._ctx: dict = {}
        self._abort_evt = threading.Event()

    def set_executor(self, executor: Executor, ctx: dict | None = None) -> None:
        self._executor = executor
        self._ctx = ctx or {}

    def set_recovery(self, recovery: Recovery | None) -> None:
        """Hook called after a watchdog timeout (T2 hover-recovery)."""
        self._recovery = recovery

    def add_terminal_hook(self, hook: TerminalHook) -> None:
        """Observe every task the instant it reaches a terminal state (T4c)."""
        self._terminal_hooks.append(hook)

    def set_terminal_hooks(self, hooks: list[TerminalHook]) -> None:
        self._terminal_hooks = list(hooks)

    def _finalize(self, task: Task) -> None:
        """End a task exactly once: stamp, file, then announce (T4c).

        The ONLY place a task leaves the live set. `task.state` is already the
        terminal state when the hooks run, so a journal row written here can be
        trusted to say `done`/`failed`/`cancelled` — which is precisely what the
        old code could not do, because it journalled the outcome from inside the
        executor, before the queue had decided the state, and never journalled
        at all for a task cancelled before it started.
        """
        with self._lock:
            if task.finalized:
                return
            task.finalized = True
            if task.finished_at is None:
                task.finished_at = time.time()
            self._history.append(task)
            hooks = list(self._terminal_hooks)
        for hook in hooks:
            try:
                hook(task)
            except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
                task.terminal_errors.append(f"{type(exc).__name__}: {exc}")

    @property
    def current(self) -> Task | None:
        return self._current

    @property
    def state(self) -> str:
        if self._current is None:
            return "idle"
        return self._current.state.value

    def pending(self) -> int:
        with self._lock:
            return sum(1 for t in self._pending if t.state not in TERMINAL)

    def pending_tasks(self) -> list[Task]:
        with self._lock:
            return [t for t in self._pending if t.state not in TERMINAL]

    def active(self) -> Task | None:
        """The task blocking a new submission: in-flight first, else queued."""
        with self._lock:
            cur = self._current
            if cur is not None and cur.state not in TERMINAL:
                return cur
            for t in self._pending:
                if t.state not in TERMINAL:
                    return t
            return None

    # ---- submission ----
    def submit(self, tool: str, params: dict, idempotency_key: str | None = None,
               *, privileged: bool = False, allow_queue: bool = False,
               uncancellable: bool = False, mission_id: str | None = None,
               watchdog_s: float | None = None) -> Task:
        """Enqueue a command.

        - A repeated `idempotency_key` returns the ORIGINAL task and queues
          nothing (T4b), whatever state that task is in.
        - Otherwise a vehicle with non-terminal work raises `VehicleBusyError`
          (T2) unless the caller explicitly asked to pipeline (`allow_queue`)
          or this is a safety pre-emption (`privileged`).
        - `privileged` cancels whatever the harness had running (unless that
          is itself an un-cancellable safety task) and runs next.
        """
        dropped: list[Task] = []
        with self._lock:
            if idempotency_key and idempotency_key in self._idem:
                existing = self._idem[idempotency_key]
                existing.replays += 1
                return existing
            if not privileged and not allow_queue:
                blocking = self.active()
                if blocking is not None:
                    raise VehicleBusyError(blocking)
            task = Task(tool=tool, params=params, vehicle=self.vehicle,
                        idempotency_key=idempotency_key, mission_id=mission_id,
                        watchdog_s=watchdog_s,
                        safety=privileged, uncancellable=bool(uncancellable))
            if idempotency_key:
                self._idem[idempotency_key] = task
            self._by_id[task.id] = task
            if privileged:
                dropped = self._preempt(f"pre-empted by safety task {task.id} ({tool})")
            self._pending.append(task)
        for t in dropped:
            if t.state in TERMINAL:
                self._finalize(t)
        self._enqueue(task)
        self._ensure_worker()
        return task

    def seed_idempotency(self, key: str, handle: dict) -> Task:
        """Re-register a finished task's key from the journal (T4b across restart).

        A replayed key then returns the ORIGINAL task_id instead of flying the
        command a second time after a crash.
        """
        with self._lock:
            if key in self._idem:
                return self._idem[key]
            t = Task(tool=str(handle.get("tool") or "unknown"),
                     params=dict(handle.get("params") or {}),
                     vehicle=self.vehicle,
                     id=str(handle.get("task_id") or uuid.uuid4().hex[:12]),
                     idempotency_key=key,
                     state=TaskState.DONE,
                     submitted_at=float(handle.get("submitted_at") or time.time()))
            t.progress_pct = float(handle.get("progress_pct") or 0.0)
            t.finished_at = float(handle.get("ts") or time.time())
            t.result = {"restored_from_journal": True}
            # Already terminal when it was restored: it is a record of a task
            # that ended in a PREVIOUS process, not a transition happening now,
            # so the terminal hooks must not re-journal it.
            t.finalized = True
            self._idem[key] = t
            self._by_id[t.id] = t
            self._history.append(t)
            return t

    # ---- loop plumbing ----
    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _target_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "VehicleQueue needs an event loop (submit inside async code or pass loop=)"
            ) from exc

    def _enqueue(self, task: Task) -> None:
        loop = self._target_loop()
        if self._loop is not None and not self._on_loop():
            loop.call_soon_threadsafe(self._queue.put_nowait, task)
        else:
            self._queue.put_nowait(task)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        loop = self._target_loop()
        if self._on_loop() or self._loop is None:
            self._worker = loop.create_task(self._run())
        else:
            self._worker = asyncio.run_coroutine_threadsafe(self._run(), loop)

    def _cancel_runner(self) -> bool:
        runner = self._current_runner
        if runner is None or runner.done():
            return False
        loop = self._loop
        if loop is None or self._on_loop():
            runner.cancel()
        else:
            loop.call_soon_threadsafe(runner.cancel)
        return True

    def _preempt(self, reason: str) -> list[Task]:
        """Cancel queued work and the in-flight task for a safety transition.

        Filing and announcing the cancellations is left to `_finalize`, which
        the caller runs once it has dropped the submission lock — a terminal
        hook (the server's journal writer) must never run inside it.
        """
        dropped = []
        for t in self._pending:
            if t.state not in TERMINAL:
                t.state = TaskState.CANCELLED
                t.error = reason
                dropped.append(t)
        self._pending = [t for t in self._pending if t.state not in TERMINAL]
        cur = self._current
        if cur is not None and cur.state not in TERMINAL and not cur.uncancellable:
            cur.state = TaskState.CANCELLING
            cur.error = reason
            self._cancel_runner()
            dropped.append(cur)
        return dropped

    # ---- worker ----
    async def _run(self) -> None:
        while True:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except TimeoutError:
                return  # idle: let the worker die; submit() respawns it
            with self._lock:
                if task in self._pending:
                    self._pending.remove(task)
            if task.state in TERMINAL:
                # Drained by abort/pre-emption before it ever started. It is
                # already final; `_finalize` is idempotent and guarantees it
                # was filed and announced even on a path that skipped it.
                self._finalize(task)
                continue
            await self._execute_one(task)

    async def _execute_one(self, task: Task) -> None:
        assert self._executor is not None, "no executor configured"
        self._current = task
        task.state = TaskState.EXECUTING
        task.started_at = time.time()
        task.progress_at = time.monotonic()
        # The epsilon baseline starts where the task actually is, so a task
        # that was pre-empted and re-run does not get a free window, and a
        # fresh one measures its first step from 0.
        task._progress_mark = task.progress_pct
        self._abort_evt.clear()
        started_mono = time.monotonic()
        watchdog_s = task.watchdog_s if task.watchdog_s is not None else self.watchdog_s
        runner = asyncio.create_task(self._executor(task, self._ctx))
        self._current_runner = runner
        timeout_reason: str | None = None
        try:
            while True:
                done, _ = await asyncio.wait({runner}, timeout=self.poll_s)
                if done:
                    break
                stalled = task.stalled_s()
                if watchdog_s and stalled > watchdog_s:
                    timeout_reason = (
                        f"watchdog timeout: no progress for {stalled:.1f}s "
                        f"(limit {watchdog_s:.0f}s, progress {task.progress_pct:.1f}%)"
                    )
                elif (self.max_duration_s
                      and time.monotonic() - started_mono > self.max_duration_s):
                    timeout_reason = (
                        f"watchdog timeout: exceeded max duration "
                        f"{self.max_duration_s:.0f}s"
                    )
                if timeout_reason:
                    runner.cancel()
                    await asyncio.wait({runner}, timeout=10.0)
                    break
            if timeout_reason:
                task.state = TaskState.FAILED
                task.error = timeout_reason
                await self._recover(task, timeout_reason)
            elif runner.cancelled():
                task.state = TaskState.CANCELLED
                task.error = task.error or ("aborted" if self._abort_evt.is_set()
                                            else "cancelled")
            else:
                exc = runner.exception()
                if exc is not None:
                    task.state = TaskState.FAILED
                    task.error = str(exc) or type(exc).__name__
                else:
                    task.result = runner.result()
                    task.state = TaskState.DONE
                    await task.report_progress(100.0, note="complete")
        except asyncio.CancelledError:
            # The worker itself was cancelled (service shutdown).
            if not runner.done():
                runner.cancel()
            task.state = TaskState.CANCELLED
            task.error = task.error or "queue shutdown"
            raise
        finally:
            # The state above is final before anything is announced, so the
            # terminal hooks (and the journal row they write) see the truth.
            self._finalize(task)
            self._current = None
            self._current_runner = None

    async def _recover(self, task: Task, reason: str) -> None:
        """Put the vehicle somewhere safe after a watchdog timeout (T2)."""
        if self._recovery is None:
            task.recovery = "none: no recovery hook configured"
            return
        try:
            out = await self._recovery(task, reason)
            task.recovery = str(out) if out is not None else "recovered"
        except Exception as exc:  # noqa: BLE001 — surfaced on the handle
            task.recovery = f"recovery failed: {type(exc).__name__}: {exc}"

    # ---- harness control ----
    async def cancel_current(self, *, operator_override: bool = False) -> Task | None:
        """Cancel the in-flight task (harness-initiated).

        An un-cancellable safety task (BINGO force-RTB, M4) is REFUSED unless
        the caller is an operator override.
        """
        cur = self._current
        if cur is None:
            return None
        if cur.uncancellable and not operator_override:
            raise PermissionError(
                f"task {cur.id} ({cur.tool}) is an un-cancellable safety transition"
            )
        cur.state = TaskState.CANCELLING
        self._cancel_runner()
        return cur

    async def abort(self, *, operator_override: bool = False,
                    reason: str = "aborted before start") -> dict:
        """Abort = cancel + clear queued tasks (uav_abort semantics, T2).

        Refuses while an un-cancellable safety task is flying: the harness
        cannot abort its way out of a BINGO RTB (M4/T5).
        """
        cur = self._current
        if (cur is not None and cur.uncancellable and cur.state not in TERMINAL
                and not operator_override):
            return {"aborted": False, "refused": True,
                    "reason": f"{cur.tool} is an un-cancellable safety transition",
                    "current": cur.handle(), "cancelled": []}
        self._abort_evt.set()
        dropped: list[Task] = []
        with self._lock:
            for t in self._pending:
                if t.state not in TERMINAL:
                    t.state = TaskState.CANCELLED
                    t.error = reason
                    dropped.append(t)
            self._pending = [t for t in self._pending if t.state not in TERMINAL]
        # Outside the lock, and only now: a task cancelled before it ever
        # started still has to reach a terminal row in the journal, or the
        # restart replay sees it as work that is still in flight (T4c).
        for t in dropped:
            self._finalize(t)
        cancelled: list[dict] = [t.handle() for t in dropped]
        current = await self.cancel_current(operator_override=operator_override)
        return {"aborted": True, "refused": False,
                "current": current.handle() if current else None,
                "cancelled": cancelled}

    # ---- reads ----
    def history(self, limit: int = 20) -> list[dict]:
        return [t.handle() for t in self._history[-limit:]]

    def get(self, task_id: str) -> Task | None:
        """Resolve a task id for its WHOLE lifetime, queued window included."""
        with self._lock:
            t = self._by_id.get(task_id)
        if t is not None:
            return t
        if self._current and self._current.id == task_id:
            return self._current
        for h in self._history:
            if h.id == task_id:
                return h
        return None


class TaskingService:
    """Owns one VehicleQueue per vehicle name, plus a shared background
    event loop so sync callers can submit without a running loop."""

    def __init__(self, watchdog_s: float = DEFAULT_NO_PROGRESS_S,
                 max_duration_s: float | None = None):
        #: No-progress window (T2), not a cap on total task duration.
        self.watchdog_s = watchdog_s
        self.max_duration_s = max_duration_s
        self._queues: dict[str, VehicleQueue] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._default_executor: Executor | None = None
        self._default_ctx: dict = {}
        self._default_recovery: Recovery | None = None
        self._terminal_hooks: list[TerminalHook] = []

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_forever, name="godseye-tasking", daemon=True)
        t.start()
        self._loop = loop
        self._loop_thread = t
        return loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._ensure_loop()

    def queue_for(self, vehicle: str) -> VehicleQueue:
        if vehicle not in self._queues:
            q = VehicleQueue(vehicle, watchdog_s=self.watchdog_s,
                             loop=self._ensure_loop(),
                             max_duration_s=self.max_duration_s)
            if self._default_executor is not None:
                q.set_executor(self._default_executor, self._default_ctx)
            q.set_recovery(self._default_recovery)
            q.set_terminal_hooks(self._terminal_hooks)
            self._queues[vehicle] = q
        return self._queues[vehicle]

    def shutdown(self, timeout_s: float = 3.0) -> None:
        """Stop the shared background loop and join its thread.

        Without this the tasking thread (spawned lazily by the first sync
        submit) is never joined, so a test/process teardown hangs waiting
        on it. Cancels any in-flight runner first.
        """
        loop = self._loop
        if loop is None:
            return

        def _cancel_all():
            for q in self._queues.values():
                if q._current_runner and not q._current_runner.done():
                    q._current_runner.cancel()
                if q._worker is not None and not q._worker.done():
                    try:
                        q._worker.cancel()
                    except Exception:
                        pass
        try:
            loop.call_soon_threadsafe(_cancel_all)
            # Let the loop actually deliver those cancellations before it is
            # stopped; otherwise a suspended worker unwinds against a closed
            # loop and the teardown spews "Event loop is closed".
            time.sleep(0.05)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=timeout_s)
        try:
            loop.close()
        except Exception:
            pass
        self._loop = None
        self._loop_thread = None

    def set_executor(self, executor: Executor, ctx: dict | None = None) -> None:
        for q in self._queues.values():
            q.set_executor(executor, ctx)
        self._default_executor = executor
        self._default_ctx = ctx or {}

    def set_recovery(self, recovery: Recovery | None) -> None:
        for q in self._queues.values():
            q.set_recovery(recovery)
        self._default_recovery = recovery

    def add_terminal_hook(self, hook: TerminalHook) -> None:
        """Observe every task on every vehicle as it reaches a terminal state.

        Registered on the queues that already exist AND remembered for the ones
        created later, so a vehicle that first appears mid-run is not silently
        exempt from the journal (T4c).
        """
        self._terminal_hooks.append(hook)
        for q in self._queues.values():
            q.add_terminal_hook(hook)

    def submit(self, vehicle: str, tool: str, params: dict,
               idempotency_key: str | None = None, **kw) -> Task:
        q = self.queue_for(vehicle)
        if q._executor is None and self._default_executor is not None:
            q.set_executor(self._default_executor, self._default_ctx)
        return q.submit(tool, params, idempotency_key=idempotency_key, **kw)

    def seed_idempotency(self, vehicle: str, key: str, handle: dict) -> Task:
        return self.queue_for(vehicle).seed_idempotency(key, handle)

    def get(self, vehicle: str, task_id: str) -> Task | None:
        return self.queue_for(vehicle).get(task_id)

    def status(self, vehicle: str) -> dict:
        q = self.queue_for(vehicle)
        return {
            "vehicle": vehicle,
            "state": q.state,
            "current": q.current.handle() if q.current else None,
            "queued": q.pending(),
            "pending": [t.handle() for t in q.pending_tasks()],
        }

    def vehicles(self) -> list[str]:
        return list(self._queues)
