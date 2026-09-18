"""Adversarial-verification regressions for the harness entrypoints.

Each test here fails on the code as it stood before this pass. They guard four
defects found by driving the shipped stack over the REAL MCP Streamable HTTP
transport rather than by reading the code:

1. `scripts/demo_mission.py` monitored a flying grid for a FIXED 120 s while
   the server costed the shipped plan at ~346 s, so the one-command demo always
   abandoned its own recon leg at roughly a third of coverage, aborted it, and
   printed "demo complete". (Measured: with the window sized from the server's
   own `est_time_s` the same grid reaches `state=done progress=100.0%`.)
2. `_spawn_order_of_battle` converted MSL -> HAE before sending the LEGACY
   `alt_m` spelling, on the stated belief that `alt_m` is "an ABSOLUTE geodetic
   (HAE) altitude". The shipped `sim_spawn_target` documents the opposite:
   "`alt_m` is the legacy spelling of the same datum" as `alt_msl_m`. Sending
   HAE displaces the whole order of battle by the undulation (-22.2 m at the
   default theater, -33.0 m at indo-pak-loc).
3. `_sensor_extras` preferred the bare `alt_m` over `alt_msl_m` on
   `uav_los_check`, which TOOL_CONTRACT forbids ("Never a bare alt_m"), and
   would have filled an `alt_agl_m`/`alt_hae_m` parameter with an MSL number if
   those were the only spellings on offer.
4. `scripts/demo_laptop.sh` hardcoded `THEATER="${THEATER:-default}"` — a fresh
   copy of exactly the theater knowledge `theaters.py` exists to hold, in the
   script whose whole claim is that the duplicate tables are retired.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from godseye_uav import theaters
from godseye_uav.geo import canonical_altitude

REPO = Path(__file__).resolve().parents[1]
DEMO_PY = REPO / "scripts" / "demo_mission.py"
DEMO_SH = REPO / "scripts" / "demo_laptop.sh"


def _load_demo():
    spec = importlib.util.spec_from_file_location("godseye_demo_mission_verify", DEMO_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load_demo()

#: The dry-run / mission_grid_search product the shipped server actually
#: returns for the default theater's demo box, trimmed to the fields the
#: window sizing reads. Measured over the wire on 2026-09-17.
MEASURED_PLAN = {
    "est_time_s": 346.2,
    "waypoint_count": 14,
    "gate": {"ok": True, "est_time_s": 346.2, "required_pct": 20.81,
             "envelope_violations": []},
}


class FakeHarness(demo.Harness):
    """A Harness with a fixed tool registry and no transport.

    Keeps the real `Harness.arg` / `first` / `require` / `skip` logic — the
    point of these tests is which parameter the demo CHOOSES and what value it
    puts in it, so that resolution must not be stubbed out.
    """

    def __init__(self, schemas, out, returns=None):
        super().__init__(None, out=out)
        self.schemas = schemas
        self.returns = returns or {}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool, *, _timeout_s: float = 60.0, **args):
        undeclared = [k for k in args if k not in self.schemas.get(tool, {})]
        assert not undeclared, f"{tool} was sent undeclared parameters {undeclared}"
        self.calls.append((tool, dict(args)))
        return dict(self.returns.get(tool, {}))


# ---------------------------------------------------------------------------
# 1. the monitor window must cover the flight the demo exists to show
# ---------------------------------------------------------------------------


def test_monitor_window_auto_covers_the_servers_own_plan():
    window, note = demo._monitor_window(None, MEASURED_PLAN)
    est = MEASURED_PLAN["est_time_s"]
    assert window >= est, (
        f"auto window {window}s does not even cover the server's own "
        f"est_time_s of {est}s — the grid is abandoned mid-flight")
    assert window <= demo.MAX_AUTO_MONITOR_S
    assert "est_time_s" in note and "auto-sized" in note


def test_monitor_window_reads_est_time_from_the_gate_when_the_top_level_lacks_it():
    """The M4 gate is the pre-flight product; read it first."""
    gate_only = {"gate": {"est_time_s": 500.0}}
    window, _ = demo._monitor_window(None, gate_only)
    assert window >= 500.0


def test_monitor_window_honours_an_explicit_request():
    """`--monitor-s` / MONITOR_S still wins — an operator asking for a short
    look must get a short look, not an auto-sized one."""
    window, note = demo._monitor_window(4.0, MEASURED_PLAN)
    assert window == 4.0
    assert "as asked" in note


def test_monitor_window_without_any_est_time_says_it_may_not_cover_the_leg():
    """No silent fallback: if the plan carried no cost, say the window is a
    guess rather than implying it is sized."""
    window, note = demo._monitor_window(None, {}, {"gate": {}})
    assert window == demo.FALLBACK_MONITOR_S
    assert "no est_time_s" in note and "may NOT cover" in note


def test_monitor_window_reports_the_ceiling_instead_of_hiding_it():
    huge = {"est_time_s": demo.MAX_AUTO_MONITOR_S * 10}
    window, note = demo._monitor_window(None, huge)
    assert window == demo.MAX_AUTO_MONITOR_S
    assert "unfinished" in note, "a capped window must say the grid will not finish"


def test_plan_seconds_ignores_a_nonsense_cost():
    assert demo._plan_seconds({"est_time_s": 0}) is None
    assert demo._plan_seconds({"est_time_s": "soon"}) is None
    assert demo._plan_seconds(None, "not a dict", MEASURED_PLAN) == 346.2


def test_cli_default_monitor_s_is_auto_not_a_fixed_120():
    """A fixed default is what abandoned the grid; the default must be 'auto'."""
    args = demo.build_parser().parse_args([])
    assert args.monitor_s is None, (
        "a fixed --monitor-s default cuts the recon leg off regardless of how "
        "long the server says the plan takes")
    assert demo.build_parser().parse_args(["--monitor-s", "7"]).monitor_s == 7.0


# ---------------------------------------------------------------------------
# 2. the legacy spawn altitude is MSL, not HAE
# ---------------------------------------------------------------------------


def _spawn_calls(schemas):
    lines: list[str] = []
    h = FakeHarness(schemas, lines.append)
    t = theaters.get("default")
    asyncio.run(demo._spawn_order_of_battle(h, t, lines.append))
    return h, t, lines


def test_legacy_spawn_altitude_is_sent_as_msl_not_converted_to_hae():
    """`sim_spawn_target.alt_m` is the legacy spelling of alt_msl_m."""
    schemas = {"sim_spawn_target": {"name": {}, "mesh": {}, "lat": {}, "lon": {},
                                    "alt_m": {}}}
    h, t, lines = _spawn_calls(schemas)
    assert h.calls, "nothing was spawned"
    hae = canonical_altitude(t.home_alt_msl_m, t.home_lat, t.home_lon,
                             datum="msl").alt_hae
    assert abs(hae - t.home_alt_msl_m) > 10.0, "pick a theater where the datums differ"
    for tool, args in h.calls:
        assert tool == "sim_spawn_target"
        assert args["alt_m"] == pytest.approx(t.home_alt_msl_m, abs=1e-6), (
            "the legacy alt_m is MSL; sending HAE buries or floats every "
            "target by the geoid undulation")
        assert args["alt_m"] != pytest.approx(hae, abs=1e-6)
    text = "\n".join(lines)
    assert "MSL" in text and "egm96" in text, (
        "the datum and its provenance must still be stated on every spawn")


def test_contract_spawn_altitude_uses_alt_msl_m_when_offered():
    schemas = {"sim_spawn_target": {"name": {}, "mesh": {}, "lat": {}, "lon": {},
                                    "alt_msl_m": {}, "alt_m": {}}}
    h, t, _ = _spawn_calls(schemas)
    for _tool, args in h.calls:
        assert "alt_msl_m" in args and "alt_m" not in args
        assert args["alt_msl_m"] == pytest.approx(t.home_alt_msl_m, abs=1e-6)


# ---------------------------------------------------------------------------
# 3. uav_los_check must not be handed an MSL number in another datum
# ---------------------------------------------------------------------------


def _los_run(props):
    lines: list[str] = []
    h = FakeHarness({"uav_los_check": props}, lines.append,
                    returns={"uav_los_check": {"los": True, "model": "test"}})
    t = theaters.get("default")
    asyncio.run(demo._sensor_extras(h, "Drone1", t, [], lines.append))
    return h, t, lines


def test_los_check_prefers_the_datumed_msl_spelling_over_bare_alt_m():
    """TOOL_CONTRACT: 'Never a bare alt_m'."""
    h, t, _ = _los_run({"vehicle": {}, "lat": {}, "lon": {}, "alt_m": {},
                        "alt_msl_m": {}, "alt_agl_m": {}, "alt_hae_m": {}})
    assert h.calls, "uav_los_check was never called"
    _tool, args = h.calls[-1]
    assert "alt_msl_m" in args, f"picked a bare/ambiguous spelling: {sorted(args)}"
    assert "alt_m" not in args
    assert args["alt_msl_m"] == pytest.approx(t.home_alt_msl_m)


def test_los_check_accepts_legacy_alt_m_only_when_it_is_the_only_msl_spelling():
    h, t, _ = _los_run({"vehicle": {}, "lat": {}, "lon": {}, "alt_m": {}})
    _tool, args = h.calls[-1]
    assert args["alt_m"] == pytest.approx(t.home_alt_msl_m)


def test_los_check_refuses_to_put_an_msl_number_in_an_agl_parameter():
    """An MSL ground elevation in an AGL field is a silent datum swap of the
    whole ground elevation (122 m at the default theater, 1570 m at Isfahan).
    Skip loudly instead."""
    h, _t, lines = _los_run({"vehicle": {}, "lat": {}, "lon": {}, "alt_agl_m": {},
                             "alt_hae_m": {}})
    assert not h.calls, (
        f"uav_los_check was called anyway, with {h.calls} — an MSL value was "
        f"sent down a different datum")
    assert any("uav_los_check altitude" in s for s in h.skipped), h.skipped
    assert "SKIP" in "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. demo_laptop.sh keeps no theater knowledge of its own
# ---------------------------------------------------------------------------


def test_demo_laptop_takes_its_default_theater_from_the_table():
    src = DEMO_SH.read_text(encoding="utf-8")
    assert "THEATER:-default" not in src, (
        "demo_laptop.sh hardcodes a default theater id — that is another copy "
        "of the knowledge theaters.py is supposed to own")
    assert "DEFAULT_THEATER_ID" in src
    assert "godseye_uav import theaters" in src


def test_demo_laptop_only_passes_monitor_s_when_the_operator_set_it():
    """A hardcoded MONITOR_S here would re-impose the fixed window the driver
    now sizes from the server's plan."""
    src = DEMO_SH.read_text(encoding="utf-8")
    assert "MONITOR_S:-120" not in src
    assert 'MONITOR_S="${MONITOR_S:-}"' in src
    assert "MONITOR_ARGS" in src
