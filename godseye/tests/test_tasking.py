"""Per-vehicle queue + state machine tests (PLAN T2/T4a/T4b).

Wave-2 contracts pinned here:
  * the watchdog keys on LACK OF PROGRESS, not elapsed time — a long healthy
    leg survives, a stalled one is killed and recovered to hover;
  * a second command while the vehicle is busy is REJECTED with the blocking
    handle, never silently queued;
  * a task_id is pollable from the instant it is handed out;
  * an idempotency key never re-executes, including after completion and
    across a restart.
"""
import asyncio

import pytest
from godseye_uav.tasking import Task, TaskingService, TaskState, VehicleBusyError


async def ok_executor(task, ctx):
    await asyncio.sleep(0.01)
    return {"echo": task.params}


async def slow_executor(task, ctx):
    await asyncio.sleep(30)
    return {}


async def fail_executor(task, ctx):
    raise RuntimeError("boom")


async def progressing_executor(task, ctx):
    """Runs far longer than the watchdog window but keeps closing on target.

    Reports every ~0.05 s for ~2 s against the 0.5 s window the test sets, so
    the task outlives the window 4x over while tolerating the scheduler jitter
    of a fully loaded machine. The old 0.02 s / 0.4 s / 0.1 s figures proved the
    same thing with a 0.1 s margin, which a loaded test run could exceed — and
    a false watchdog kill reads exactly like a real regression.
    """
    for i in range(1, 41):
        await asyncio.sleep(0.05)
        await task.report_progress(i * 2.5, note=f"leg {i}")
    return {"legs": 40}


async def stalled_executor(task, ctx):
    """Reports the SAME percentage forever: a drone that stopped closing."""
    while True:
        await task.report_progress(12.0, note="stuck")
        await asyncio.sleep(0.01)


#: The mission-scale leg the watchdog used to kill. A 16.2 km recon route at
#: 12 m/s with the server's real 0.25 s telemetry cadence: 5 400 reports, each
#: worth 12*0.25/16200*100 = 0.0185 pp — well under PROGRESS_EPS_PCT.
LONG_ROUTE_M = 16_200.0
LONG_ROUTE_SPEED_MPS = 12.0
TELEMETRY_POLL_S = 0.25


def long_route_pcts(n_reports: int) -> list[float]:
    """Progress percentages a healthy `LONG_ROUTE_M` leg reports, in order."""
    step_m = LONG_ROUTE_SPEED_MPS * TELEMETRY_POLL_S
    return [100.0 * (i * step_m) / LONG_ROUTE_M for i in range(1, n_reports + 1)]


async def long_route_executor(task, ctx):
    """A perfectly healthy mission-scale leg, replayed fast.

    Same per-report step as the real thing (0.0185 pp); only the wall clock is
    compressed so the test can watch a whole watchdog window go by.
    """
    for pct in long_route_pcts(1200):
        await asyncio.sleep(0.001)
        await task.report_progress(pct, note="closing")
    return {"route_m": LONG_ROUTE_M}


async def route_executor(task, ctx):
    """Flies `params['route_m']` at the real speed and telemetry cadence."""
    route_m = float(task.params["route_m"])
    step_m = LONG_ROUTE_SPEED_MPS * TELEMETRY_POLL_S
    n = int(route_m // step_m)
    for i in range(1, n + 1):
        await asyncio.sleep(0.001)
        await task.report_progress(100.0 * (i * step_m) / route_m,
                                   note=f"{route_m - i * step_m:.0f} m to home")
    return {"route_m": route_m, "reports": n}


async def long_route_then_stall_executor(task, ctx):
    """A mission-scale leg that flies a while and then genuinely STOPS."""
    last = 0.0
    for pct in long_route_pcts(120):
        await asyncio.sleep(0.001)
        last = pct
        await task.report_progress(pct, note="closing")
    while True:  # the aircraft stopped closing: the watchdog must catch this
        await task.report_progress(last, note="stuck")
        await asyncio.sleep(0.01)


def run(coro):
    return asyncio.run(coro)


def test_fifo_executes_in_order():
    """Explicit pipelining still runs FIFO (the busy contract is opt-out)."""
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        t1 = svc.submit("Drone1", "uav_takeoff", {"alt_m": 30}, allow_queue=True)
        t2 = svc.submit("Drone1", "uav_goto_gps", {"lat": 1, "lon": 2}, allow_queue=True)
        await asyncio.sleep(0.3)
        assert t1.state == TaskState.DONE
        assert t2.state == TaskState.DONE
        assert t1.started_at <= t2.started_at
        assert t1.result == {"echo": {"alt_m": 30}}
        hist = svc.queue_for("Drone1").history()
        assert [h["tool"] for h in hist] == ["uav_takeoff", "uav_goto_gps"]
        svc.shutdown()
    run(main())


def test_second_command_while_busy_is_rejected_with_current_handle():
    """T2: 'busy' + the blocking handle — never a silent second queue slot."""
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        first = svc.submit("Drone1", "uav_goto_gps", {"lat": 1})
        await asyncio.sleep(0.15)
        assert first.state == TaskState.EXECUTING
        with pytest.raises(VehicleBusyError) as exc:
            svc.submit("Drone1", "uav_land", {})
        assert exc.value.current.id == first.id
        assert exc.value.current.tool == "uav_goto_gps"
        # and nothing was enqueued behind it
        assert svc.queue_for("Drone1").pending() == 0
        await svc.queue_for("Drone1").abort()
        svc.shutdown()
    run(main())


def test_queued_task_is_pollable_immediately():
    """T4a: an id handed to the harness resolves for its WHOLE lifetime.

    Deliberately polls with NO sleep — before the worker has dequeued it.
    """
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        t = svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        assert svc.queue_for("Drone1").get(t.id) is not None
        assert svc.get("Drone1", t.id).state == TaskState.QUEUED
        queued = svc.submit("Drone1", "uav_land", {}, allow_queue=True)
        assert svc.queue_for("Drone1").get(queued.id) is not None
        assert queued.handle()["state"] == "queued"
        await svc.queue_for("Drone1").abort()
        svc.shutdown()
    run(main())


def test_idempotency_key_dedupes():
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        t1 = svc.submit("Drone1", "uav_takeoff", {}, idempotency_key="k-1")
        t2 = svc.submit("Drone1", "uav_takeoff", {}, idempotency_key="k-1")
        assert t1 is t2
        await asyncio.sleep(0.2)
        assert len(svc.queue_for("Drone1").history()) == 1
        svc.shutdown()
    run(main())


def test_idempotent_replay_after_completion_does_not_re_execute():
    """T4b: replaying a key returns the ORIGINAL handle, even once it is done."""
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        first = svc.submit("Drone1", "uav_takeoff", {"alt_m": 20}, idempotency_key="k-9")
        await asyncio.sleep(0.25)
        assert first.state == TaskState.DONE
        again = svc.submit("Drone1", "uav_takeoff", {"alt_m": 20}, idempotency_key="k-9")
        await asyncio.sleep(0.15)
        assert again is first
        assert again.replays == 1
        assert len(svc.queue_for("Drone1").history()) == 1  # nothing re-flew
        svc.shutdown()
    run(main())


def test_seed_idempotency_survives_restart():
    """T4b across a restart: the journal re-seeds the key, so it never re-flies."""
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        svc.seed_idempotency("Drone1", "k-restart",
                             {"task_id": "abc123", "tool": "uav_fly_route",
                              "params": {"waypoints": []}})
        t = svc.submit("Drone1", "uav_fly_route", {"waypoints": []},
                       idempotency_key="k-restart")
        assert t.id == "abc123"
        assert t.state == TaskState.DONE
        await asyncio.sleep(0.1)
        assert svc.queue_for("Drone1").pending() == 0
        svc.shutdown()
    run(main())


def test_failure_surfaces_in_handle():
    async def main():
        svc = TaskingService()
        svc.set_executor(fail_executor)
        t = svc.submit("Drone1", "uav_land", {})
        await asyncio.sleep(0.2)
        assert t.state == TaskState.FAILED
        assert t.error == "boom"
        assert svc.status("Drone1")["state"] == "idle"
        svc.shutdown()
    run(main())


def test_watchdog_timeout_fails_task():
    async def main():
        svc = TaskingService(watchdog_s=0.05)
        svc.set_executor(slow_executor)
        t = svc.submit("Drone1", "uav_goto_gps", {})
        await asyncio.sleep(0.5)
        assert t.state == TaskState.FAILED
        assert "watchdog" in t.error
        svc.shutdown()
    run(main())


def test_watchdog_does_not_kill_a_long_task_that_keeps_progressing():
    """T2: the watchdog bounds ABSENCE OF PROGRESS, not total duration.

    The task runs ~2 s against a 0.5 s window — it would have died on the old
    flat total-duration watchdog after the first 0.5 s.
    """
    async def main():
        svc = TaskingService(watchdog_s=0.5)
        svc.set_executor(progressing_executor)
        t = svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        for _ in range(200):
            await asyncio.sleep(0.05)
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
        assert t.state == TaskState.DONE, t.error
        # 4x the no-progress window: total duration is plainly not the bound
        assert t.finished_at - t.started_at > 2.0
        assert t.progress_pct == 100.0
        svc.shutdown()
    run(main())


def test_watchdog_still_kills_a_stalled_task_that_re_reports_the_same_progress():
    """Re-reporting the same percentage must NOT keep the watchdog alive."""
    async def main():
        svc = TaskingService(watchdog_s=0.15)
        svc.set_executor(stalled_executor)
        t = svc.submit("Drone1", "uav_goto_gps", {})
        for _ in range(60):
            await asyncio.sleep(0.05)
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
        assert t.state == TaskState.FAILED
        assert "no progress" in t.error
        assert t.progress_pct == 12.0
        svc.shutdown()
    run(main())


def test_the_watchdog_clock_resets_on_a_mission_scale_leg_of_tiny_reports():
    """THE watchdog defect: the epsilon was measured per REPORT, not per RESET.

    A 16.2 km recon route at 12 m/s reports every 0.25 s, so one report is
    worth 0.0185 pp — under PROGRESS_EPS_PCT. The clock therefore never reset
    and the task was killed at exactly `watchdog_s` however well it was flying.
    Fed 600 s of that route, the OLD code reported `stalled_s == 600.0` while
    `progress_pct` had climbed to 44.4 %.

    The same feed on a 900 m route resets on every single report — which is
    why every shipped test (all short routes) passed on the broken code.
    """
    async def main():
        n = int(600.0 / TELEMETRY_POLL_S)     # 600 s of the leg
        t = Task(tool="uav_fly_route", params={}, vehicle="Drone1")
        t.progress_at = 0.0
        t._progress_mark = 0.0
        now = 0.0
        for i, pct in enumerate(long_route_pcts(n), start=1):
            now = i * TELEMETRY_POLL_S
            await t.report_progress(pct, now=now)
        assert t.progress_pct == pytest.approx(44.4, abs=0.2)
        # 3 reports of 0.0185 pp clear the 0.05 pp epsilon, so the clock can
        # never be more than 3 polls stale. It read 600.0 s before the fix.
        assert t.stalled_s(now=now) <= 4 * TELEMETRY_POLL_S, (
            f"a healthy 16.2 km leg at {t.progress_pct:.1f}% reports "
            f"stalled_s {t.stalled_s(now=now):.1f}s: the watchdog kills it")

        # control: the short route every earlier test used
        short = Task(tool="uav_fly_route", params={}, vehicle="Drone2")
        short.progress_at = 0.0
        short._progress_mark = 0.0
        step_m = LONG_ROUTE_SPEED_MPS * TELEMETRY_POLL_S
        now = 0.0
        for i in range(1, 61):
            now = i * TELEMETRY_POLL_S
            await short.report_progress(100.0 * (i * step_m) / 900.0, now=now)
        assert short.stalled_s(now=now) == 0.0
    run(main())


def test_the_queue_flies_a_mission_scale_leg_to_completion():
    """End of the same defect, at the queue: the task must not be killed.

    1 200 reports of 0.0185 pp against a window it outlives many times over.
    On the old code this failed with "watchdog timeout: no progress".
    """
    async def main():
        svc = TaskingService(watchdog_s=0.3)
        svc.set_executor(long_route_executor)
        fired = []

        async def recover(task, reason):
            fired.append(reason)
            return "hover"

        svc.set_recovery(recover)
        t = svc.submit("Drone1", "uav_fly_route", {"route_m": LONG_ROUTE_M})
        for _ in range(600):
            await asyncio.sleep(0.02)
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
        assert t.state == TaskState.DONE, t.error
        assert fired == [], f"watchdog recovered a healthy leg: {fired}"
        # it really did outlive the window rather than finishing inside it
        assert t.finished_at - t.started_at > 3 * 0.3
        assert t.progress_pct == 100.0
        svc.shutdown()
    run(main())


@pytest.mark.parametrize("route_m", [8100.0, 7000.0])
def test_the_uncancellable_bingo_rtb_is_not_killed_on_its_way_home(route_m):
    """SAFETY: the watchdog was killing the force-RTB, and losing aircraft.

    The BINGO force-RTB is submitted privileged + un-cancellable — the harness
    may not touch it — and the watchdog killed it anyway 8 km out. The safety
    layer re-committed, the replacement restarted at 0.1 % and re-armed the
    same bomb. Whether an RTB survived depended on nothing but the arithmetic
    of route length against report size, which is not a safety property: both
    of these distances are flown here and neither may be recovered away.
    """
    async def main():
        svc = TaskingService(watchdog_s=0.2)
        svc.set_executor(route_executor)
        fired = []

        async def recover(task, reason):
            fired.append(reason)
            return "hover"

        svc.set_recovery(recover)
        t = svc.submit("Drone1", "uav_return_to_home",
                       {"route_m": route_m, "reason": "bingo"},
                       privileged=True, uncancellable=True)
        assert t.uncancellable and t.safety
        for _ in range(900):
            await asyncio.sleep(0.02)
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
        assert t.state == TaskState.DONE, (
            f"the un-cancellable {route_m / 1000:.1f} km RTB was killed: {t.error}")
        assert fired == [], (
            f"the watchdog recovered a healthy force-RTB to hover: {fired}")
        assert t.finished_at - t.started_at > 3 * 0.2
        svc.shutdown()
    run(main())


def test_a_mission_scale_leg_that_stops_closing_is_still_killed():
    """The fix must not disable the watchdog it fixes.

    Same 16.2 km leg, flown for a while and then genuinely stuck: the clock
    has to run out on the accumulated mark, not be reset by the re-reports.
    """
    async def main():
        svc = TaskingService(watchdog_s=0.25)
        svc.set_executor(long_route_then_stall_executor)
        fired = []

        async def recover(task, reason):
            fired.append(reason)
            return "hover"

        svc.set_recovery(recover)
        t = svc.submit("Drone1", "uav_fly_route", {"route_m": LONG_ROUTE_M})
        for _ in range(300):
            await asyncio.sleep(0.02)
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
        assert t.state == TaskState.FAILED, t.state
        assert "no progress" in (t.error or "")
        assert fired and t.recovery == "hover"
        svc.shutdown()
    run(main())


def test_watchdog_timeout_runs_the_recovery_hook():
    """T2: 'fail handle -> hover-recovery'."""
    async def main():
        svc = TaskingService(watchdog_s=0.05)
        svc.set_executor(slow_executor)
        seen = []

        async def recover(task, reason):
            seen.append((task.id, reason))
            return "hover"

        svc.set_recovery(recover)
        t = svc.submit("Drone1", "uav_goto_gps", {})
        await asyncio.sleep(0.5)
        assert t.state == TaskState.FAILED
        assert seen and seen[0][0] == t.id
        assert t.recovery == "hover"
        svc.shutdown()
    run(main())


def test_cancel_current():
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        t = svc.submit("Drone1", "uav_orbit_poi", {})
        await asyncio.sleep(0.2)
        assert t.state == TaskState.EXECUTING
        await svc.queue_for("Drone1").cancel_current()
        await asyncio.sleep(0.2)
        assert t.state == TaskState.CANCELLED
        svc.shutdown()
    run(main())


def test_abort_clears_queue_and_current():
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        t1 = svc.submit("Drone1", "uav_orbit_poi", {})
        t2 = svc.submit("Drone1", "uav_goto_gps", {}, allow_queue=True)
        t3 = svc.submit("Drone1", "uav_land", {}, allow_queue=True)
        await asyncio.sleep(0.2)
        out = await svc.queue_for("Drone1").abort()
        await asyncio.sleep(0.2)
        assert out["aborted"] is True
        assert t1.state == TaskState.CANCELLED
        assert t2.state == TaskState.CANCELLED
        assert t3.state == TaskState.CANCELLED
        assert svc.queue_for("Drone1").pending() == 0
        svc.shutdown()
    run(main())


def test_privileged_safety_task_preempts_and_refuses_cancellation():
    """M4/T5: the safety layer pre-empts, and the harness cannot undo it."""
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        harness = svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        queued = svc.submit("Drone1", "uav_goto_gps", {}, allow_queue=True)
        await asyncio.sleep(0.2)
        rtb = svc.submit("Drone1", "uav_return_to_home", {"reason": "bingo"},
                         privileged=True, uncancellable=True)
        await asyncio.sleep(0.3)
        assert harness.state == TaskState.CANCELLED
        assert queued.state == TaskState.CANCELLED
        assert rtb.state == TaskState.EXECUTING
        assert rtb.uncancellable is True and rtb.safety is True
        # the harness cannot abort or cancel it
        out = await svc.queue_for("Drone1").abort()
        assert out["refused"] is True
        assert out["current"]["task_id"] == rtb.id
        with pytest.raises(PermissionError):
            await svc.queue_for("Drone1").cancel_current()
        assert rtb.state == TaskState.EXECUTING
        # only an operator override may
        await svc.queue_for("Drone1").cancel_current(operator_override=True)
        await asyncio.sleep(0.2)
        assert rtb.state == TaskState.CANCELLED
        svc.shutdown()
    run(main())


def test_progress_hooks_receive_updates_and_a_broken_hook_is_recorded():
    """T4a: progress fans out; a hook that dies is dropped LOUDLY, not swallowed."""
    async def main():
        seen = []

        async def good(task):
            seen.append(task.progress_pct)

        async def bad(task):
            raise RuntimeError("stream closed")

        t = Task(tool="uav_fly_route", params={}, vehicle="Drone1")
        t.add_progress_hook(good)
        t.add_progress_hook(bad)
        await t.report_progress(10.0)
        await t.report_progress(40.0, waypoint=2, waypoints_total=5, eta_s=12.0)
        assert seen == [10.0, 40.0]
        assert len(t.progress_errors) == 1
        assert "stream closed" in t.progress_errors[0]
        assert len(t.progress_hooks) == 1  # the broken one was dropped
        h = t.handle()
        assert h["progress_pct"] == 40.0
        assert h["waypoint"] == 2 and h["waypoints_total"] == 5 and h["eta_s"] == 12.0
    run(main())


def test_progress_is_monotonic_and_only_forward_steps_reset_the_watchdog():
    async def main():
        t = Task(tool="uav_goto_gps", params={}, vehicle="Drone1")
        await t.report_progress(30.0, now=100.0)
        assert t.progress_at == 100.0
        await t.report_progress(30.0, now=140.0)      # no movement
        assert t.progress_at == 100.0
        assert t.stalled_s(now=140.0) == 40.0
        await t.report_progress(10.0, now=150.0)      # a re-plan cannot rewind
        assert t.progress_pct == 30.0
        assert t.progress_at == 100.0
        await t.report_progress(31.0, now=160.0)
        assert t.progress_at == 160.0
    run(main())


def test_vehicles_are_independent():
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        a = svc.submit("Drone1", "uav_orbit_poi", {})
        b = svc.submit("Drone2", "uav_orbit_poi", {})
        await asyncio.sleep(0.2)
        assert a.state == TaskState.EXECUTING
        assert b.state == TaskState.EXECUTING  # not blocked behind Drone1
        await svc.queue_for("Drone1").abort()
        await svc.queue_for("Drone2").abort()
        svc.shutdown()
    run(main())


def test_status_shape():
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        svc.submit("Drone1", "uav_takeoff", {"alt_m": 10})
        st = svc.status("Drone1")
        assert st["vehicle"] == "Drone1"
        assert st["state"] in ("idle", "executing")
        assert "queued" in st and "pending" in st
        await asyncio.sleep(0.2)
        svc.shutdown()
    run(main())


# ------------------------------------------- T4c: terminal-state observability


def _recorder():
    """A terminal hook that records (task_id, state) the instant it fires."""
    seen: list[tuple[str, str]] = []

    def hook(task):
        # The state must ALREADY be terminal when the hook runs — that is the
        # whole point: a journal row written before the queue decided the
        # outcome says "executing" forever.
        seen.append((task.id, task.state.value))

    return seen, hook


def test_a_finished_task_announces_its_real_terminal_state():
    """T4c: the hook fires once, after the state is final, with DONE."""
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        seen, hook = _recorder()
        svc.add_terminal_hook(hook)
        t = svc.submit("Drone1", "uav_takeoff", {"alt_m": 10})
        for _ in range(100):
            if t.state in (TaskState.DONE, TaskState.FAILED):
                break
            await asyncio.sleep(0.02)
        assert t.state == TaskState.DONE
        assert seen == [(t.id, "done")], seen
        assert t.finalized is True and t.finished_at is not None
        assert t.terminal_errors == []
        svc.shutdown()
    run(main())


def test_a_failed_task_announces_failed_not_executing():
    async def main():
        svc = TaskingService()
        svc.set_executor(fail_executor)
        seen, hook = _recorder()
        svc.add_terminal_hook(hook)
        t = svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        for _ in range(100):
            if t.state == TaskState.FAILED:
                break
            await asyncio.sleep(0.02)
        assert seen == [(t.id, "failed")], seen
        svc.shutdown()
    run(main())


def test_a_task_aborted_before_it_ever_ran_still_reaches_a_terminal_hook():
    """The gap that made restart replay useless (T4c).

    A task cancelled while still queued never entered an executor, so nothing
    ever wrote its closing row: the log's last word on it stayed "queued" and
    `Store.replay()` read it as work still in flight, forever.
    """
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        seen, hook = _recorder()
        svc.add_terminal_hook(hook)
        running = svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        queued = svc.submit("Drone1", "uav_hover", {}, allow_queue=True)
        await asyncio.sleep(0.1)
        assert queued.state == TaskState.QUEUED, queued.state
        await svc.queue_for("Drone1").abort()
        await asyncio.sleep(0.2)
        assert (queued.id, "cancelled") in seen, seen
        assert queued.finished_at is not None
        # and the one that WAS running is closed exactly once too
        assert [s for s in seen if s[0] == running.id] == [(running.id, "cancelled")]
        svc.shutdown()
    run(main())


def test_a_safety_preemption_closes_the_work_it_displaced():
    """A privileged submit drops queued harness work; that work must be closed."""
    async def main():
        svc = TaskingService()
        svc.set_executor(slow_executor)
        seen, hook = _recorder()
        svc.add_terminal_hook(hook)
        svc.submit("Drone1", "uav_fly_route", {"waypoints": []})
        displaced = svc.submit("Drone1", "uav_hover", {}, allow_queue=True)
        await asyncio.sleep(0.1)
        svc.submit("Drone1", "uav_return_to_home", {}, privileged=True,
                   uncancellable=True)
        await asyncio.sleep(0.2)
        assert (displaced.id, "cancelled") in seen, seen
        await svc.queue_for("Drone1").abort(operator_override=True)
        svc.shutdown()
    run(main())


def test_a_terminal_hook_that_raises_is_recorded_not_swallowed():
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)

        def boom(task):
            raise RuntimeError("journal is full")

        svc.add_terminal_hook(boom)
        t = svc.submit("Drone1", "uav_takeoff", {"alt_m": 10})
        for _ in range(100):
            if t.state == TaskState.DONE:
                break
            await asyncio.sleep(0.02)
        assert t.state == TaskState.DONE
        assert t.terminal_errors == ["RuntimeError: journal is full"]
        svc.shutdown()
    run(main())


def test_a_key_reseeded_from_the_journal_is_not_reannounced_as_terminal():
    """A restored row is a record of a PREVIOUS process, not a transition now."""
    async def main():
        svc = TaskingService()
        svc.set_executor(ok_executor)
        seen, hook = _recorder()
        svc.add_terminal_hook(hook)
        svc.seed_idempotency("Drone1", "k-9",
                             {"task_id": "abc123", "tool": "uav_takeoff"})
        await asyncio.sleep(0.1)
        assert seen == []
        svc.shutdown()
    run(main())
