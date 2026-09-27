import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FUEL_POINTS,
  MOVE_METERS,
  assess,
  blocksApprove,
  burnRate,
  demotesApprove,
  expectedFuel,
  haversineM,
  staleSummary,
  vehicleFacts,
} from './validate.js';

const T0 = Date.UTC(2026, 8, 27, 14, 2, 51);

function approval(extra = {}) {
  return {
    id: 'a1',
    tool: 'mission_grid_search',
    klass: 'command',
    args: { vehicle: 'Drone1' },
    dry_run: {
      ok: true,
      gate: { required_pct: 41.3, available_pct: 82 },
      at_ms: T0,
    },
    ...extra,
  };
}

const SNAP = Object.freeze({
  fuel_pct: 82,
  landed: true,
  lat: 47.6445,
  lon: -122.1402,
  bingo_latched: false,
  link: 'up',
  at: T0 + 100,
});

const same = (patch = {}) => ({ ...SNAP, at: T0 + 60_000, ...patch });

test('tools that cannot be dry-run are never judged', () => {
  for (const tool of [
    'uav_takeoff',
    'uav_land',
    'uav_abort',
    'mission_cancel',
    'uav_scan_targets',
    'sim_set_weather',
  ]) {
    const r = assess(
      { id: 'x', tool, klass: 'command', args: { vehicle: 'Drone1' } },
      SNAP,
      same(),
    );
    assert.equal(r.state, 'none', tool);
    assert.equal(r.reasons[0].code, 'not_dry_runnable');
  }
});

test('the server flag dry_runnable overrides the fallback list', () => {
  assert.equal(
    assess(approval({ dry_runnable: false }), SNAP, same()).state,
    'none',
  );
  const r = assess(
    {
      id: 'x',
      tool: 'uav_hover',
      klass: 'command',
      args: { vehicle: 'Drone1' },
      dry_runnable: true,
      dry_run: { ok: true, at_ms: T0 },
    },
    SNAP,
    same(),
  );
  assert.equal(r.state, 'current');
});

test('a dry-runnable call without a dry run is "none" (ask for one)', () => {
  const r = assess(approval({ dry_run: null }), SNAP, same());
  assert.deepEqual(r, { state: 'none', reasons: [{ code: 'no_dry_run' }] });
  assert.equal(assess(null, SNAP, same()).state, 'none');
});

test('nothing changed: current', () => {
  const r = assess(approval(), SNAP, same());
  assert.deepEqual(r, { state: 'current', reasons: [] });
  assert.equal(demotesApprove(r), false);
  assert.equal(blocksApprove(r), false);
});

test('fuel moving by 2 points or more is stale; less is not', () => {
  assert.equal(FUEL_POINTS, 2);
  assert.equal(
    assess(approval(), SNAP, same({ fuel_pct: 80.01 })).state,
    'current',
  );
  assert.equal(
    assess(approval(), SNAP, same({ fuel_pct: 83.99 })).state,
    'current',
  );
  const down = assess(approval(), SNAP, same({ fuel_pct: 80 }));
  assert.equal(down.state, 'stale');
  assert.deepEqual(down.reasons, [{ code: 'fuel', from: 82, to: 80 }]);
  assert.equal(
    assess(approval(), SNAP, same({ fuel_pct: 84 })).state,
    'stale',
    'up counts too (refuel)',
  );
});

test('airborne: fuel is judged against the measured burn, so normal flight never stales a slip', () => {
  // Review (craft): an airborne slip went stale in ~30 s from normal burn
  // (0.06–0.08 %/s against a raw 2-point rule), training "Approve anyway".
  const BINGO = 26.4;
  const rate = (79.7 - BINGO) / 760; // ≈ 0.070 %/s, the server's projection
  const snap = {
    ...SNAP,
    landed: false,
    fuel_pct: 79.7,
    bingo_fuel_pct: BINGO,
    eta_to_bingo_s: 760,
    read_at: T0,
    at: T0 + 100,
  };
  const later = (seconds, fuel) => ({
    ...snap,
    fuel_pct: fuel,
    eta_to_bingo_s: (fuel - BINGO) / rate,
    read_at: T0 + seconds * 1000,
    at: T0 + seconds * 1000 + 250,
  });
  assert.ok(Math.abs(burnRate(snap) - rate) < 1e-9);
  // 30 s and 90 s of normal burn: 2.1 and 6.3 points down, still current.
  assert.equal(assess(approval(), snap, later(30, 77.6)).state, 'current');
  assert.equal(assess(approval(), snap, later(90, 73.4)).state, 'current');
  assert.ok(Math.abs(expectedFuel(snap, later(90, 73.4)) - 73.39) < 0.01);
  // Burning 2+ points faster than predicted is stale, and says what the burn
  // predicted.
  const fast = assess(approval(), snap, later(30, 75));
  assert.equal(fast.state, 'stale');
  assert.equal(fast.reasons[0].code, 'fuel');
  assert.equal(fast.reasons[0].from, 79.7);
  assert.equal(fast.reasons[0].to, 75);
  assert.ok(Math.abs(fast.reasons[0].expected - 77.6) < 0.01);
  assert.equal(
    staleSummary(fast.reasons, 'Drone1'),
    'fuel 79.7% → 75.0% (its burn predicts ≈ 77.6%)',
  );
  // A refuel in the air (fuel above the prediction) is stale too.
  assert.equal(assess(approval(), snap, later(30, 80.5)).state, 'stale');
  // Telemetry stamps win over capture times when both readings carry one;
  // otherwise both ends use the capture times.
  const unstamped = {
    ...later(30, 77.6),
    read_at: null,
    at: T0 + 100 + 30_000,
  };
  assert.equal(assess(approval(), snap, unstamped).state, 'current');
  assert.equal(
    assess(approval(), snap, { ...unstamped, at: T0 + 100 }).state,
    'stale',
    'no time passed: 2.1 points is a real change',
  );
});

test('without a burn rate the raw 2-point fuel rule applies', () => {
  const air = { ...SNAP, landed: false, fuel_pct: 79.7, read_at: T0 };
  const now = { ...air, fuel_pct: 77.6, read_at: T0 + 30_000 };
  assert.equal(burnRate(air), null, 'no time to BINGO: no burn');
  assert.equal(assess(approval(), air, now).state, 'stale');
  assert.deepEqual(assess(approval(), air, now).reasons, [
    { code: 'fuel', from: 79.7, to: 77.6 },
  ]);
  // On the ground, or past BINGO, nothing is burning toward the line.
  const ground = {
    ...air,
    landed: true,
    bingo_fuel_pct: 26.4,
    eta_to_bingo_s: 700,
  };
  assert.equal(burnRate(ground), null);
  assert.equal(
    burnRate({ ...ground, landed: false, fuel_pct: 20 }),
    null,
    'past BINGO',
  );
  assert.equal(burnRate({ ...ground, landed: false, eta_to_bingo_s: 0 }), null);
  assert.equal(expectedFuel(null, now), null);
});

test('landed, BINGO latch and link changes are stale', () => {
  assert.deepEqual(assess(approval(), SNAP, same({ landed: false })).reasons, [
    { code: 'landed', from: true, to: false },
  ]);
  assert.deepEqual(
    assess(approval(), SNAP, same({ bingo_latched: true })).reasons,
    [{ code: 'bingo', from: false, to: true }],
  );
  assert.deepEqual(
    assess(approval(), SNAP, same({ link: 'degraded' })).reasons,
    [{ code: 'link', from: 'up', to: 'degraded' }],
  );
});

test('moving more than 50 m is stale; 50 m or less is not', () => {
  assert.equal(MOVE_METERS, 50);
  // ~0.00045 deg latitude ≈ 50 m.
  const near = assess(approval(), SNAP, same({ lat: SNAP.lat + 0.0004 }));
  assert.equal(near.state, 'current');
  const far = assess(approval(), SNAP, same({ lat: SNAP.lat + 0.0006 }));
  assert.equal(far.state, 'stale');
  assert.equal(far.reasons[0].code, 'moved');
  assert.ok(far.reasons[0].meters > 50 && far.reasons[0].meters < 80);
});

test('unknown readings never count as a change', () => {
  const r = assess(
    approval(),
    SNAP,
    same({
      fuel_pct: null,
      landed: null,
      lat: null,
      lon: undefined,
      bingo_latched: null,
      link: null,
    }),
  );
  assert.equal(r.state, 'current');
  const snapUnknown = { at: T0, fuel_pct: null, landed: null };
  assert.equal(
    assess(approval(), snapUnknown, same({ fuel_pct: 10 })).state,
    'current',
  );
});

test('sim weather, time or fuel approved since the dry run makes it stale', () => {
  for (const tool of ['sim_set_weather', 'sim_set_time', 'sim_set_fuel']) {
    const r = assess(approval(), SNAP, same(), [
      { id: 'w', tool, klass: 'sim', vehicle: null, at: T0 + 5000 },
    ]);
    assert.equal(r.state, 'stale', tool);
    assert.deepEqual(r.reasons, [{ code: 'sim_change', tool }]);
  }
  // Approved BEFORE the snapshot: not a change since the dry run.
  const before = assess(approval(), SNAP, same(), [
    { id: 'w', tool: 'sim_set_weather', klass: 'sim', at: T0 - 5000 },
  ]);
  assert.equal(before.state, 'current');
  // Other sim changes (spawn a target) don't price the dry run.
  const spawn = assess(approval(), SNAP, same(), [
    { id: 's', tool: 'sim_spawn_target', klass: 'sim', at: T0 + 5000 },
  ]);
  assert.equal(spawn.state, 'current');
});

test('another command approved for the same vehicle makes it stale; other vehicles do not', () => {
  const r = assess(approval(), SNAP, same(), [
    {
      id: 'g',
      tool: 'uav_goto_gps',
      klass: 'command',
      vehicle: 'Drone1',
      title: 'Fly to point',
      at: T0 + 5000,
    },
    {
      id: 'h',
      tool: 'uav_goto_gps',
      klass: 'command',
      vehicle: 'Drone2',
      at: T0 + 5000,
    },
    {
      id: 'a1',
      tool: 'mission_grid_search',
      klass: 'command',
      vehicle: 'Drone1',
      at: T0 + 5000,
    },
  ]);
  assert.equal(r.state, 'stale');
  assert.deepEqual(r.reasons, [
    { code: 'other_command', tool: 'uav_goto_gps', title: 'Fly to point' },
  ]);
});

test('matches_args false is stale; true is current; absent is not a claim either way', () => {
  const f = assess(
    approval({ dry_run: { ok: true, at_ms: T0, matches_args: false } }),
    SNAP,
    same(),
  );
  assert.equal(f.state, 'stale');
  assert.deepEqual(f.reasons, [{ code: 'args_differ' }]);
  assert.equal(
    assess(
      approval({ dry_run: { ok: true, at_ms: T0, matches_args: true } }),
      SNAP,
      same(),
    ).state,
    'current',
  );
});

test('a failed gate on a current dry run is "failed"; failed and stale is "stale"', () => {
  const failed = assess(
    approval({ dry_run: { ok: false, at_ms: T0 } }),
    SNAP,
    same(),
  );
  assert.deepEqual(failed, {
    state: 'failed',
    reasons: [{ code: 'gate_failed' }],
  });
  assert.equal(blocksApprove(failed), true);
  const both = assess(
    approval({ dry_run: { ok: false, at_ms: T0 } }),
    SNAP,
    same({ fuel_pct: 70 }),
  );
  assert.equal(both.state, 'stale');
  assert.equal(both.failed, true);
  assert.equal(demotesApprove(both), true);
  // A failed gate with no snapshot is still failed (the gate numbers speak).
  assert.equal(
    assess(approval({ dry_run: { ok: false } }), null, null).state,
    'failed',
  );
});

test('no snapshot or no vehicle now: unverified (shown, not demoted)', () => {
  const noSnap = assess(approval(), null, same());
  assert.deepEqual(noSnap, {
    state: 'unverified',
    reasons: [{ code: 'no_snapshot' }],
  });
  assert.equal(demotesApprove(noSnap), false);
  assert.deepEqual(assess(approval(), SNAP, null), {
    state: 'unverified',
    reasons: [{ code: 'no_vehicle_now' }],
  });
  // Without a snapshot the dry run's own time orders the approvals.
  const r = assess(approval(), null, same(), [
    { id: 'w', tool: 'sim_set_time', klass: 'sim', at: T0 + 1 },
  ]);
  assert.equal(r.state, 'stale');
});

test('the vehicle comes from approval.vehicle, then the args (handoff receiver)', () => {
  const r = assess(
    {
      id: 'h',
      tool: 'mission_handoff_track',
      klass: 'command',
      args: { vehicle: 'Drone1', to_vehicle: 'Drone2' },
      dry_run: { ok: true, at_ms: T0 },
    },
    SNAP,
    same(),
    [
      {
        id: 'x',
        tool: 'uav_hover',
        klass: 'command',
        vehicle: 'Drone2',
        at: T0 + 1000,
      },
    ],
  );
  assert.equal(r.state, 'stale');
  const v = assess(approval({ vehicle: 'Drone3', args: {} }), SNAP, same(), [
    {
      id: 'x',
      tool: 'uav_hover',
      klass: 'command',
      vehicle: 'Drone3',
      at: T0 + 1000,
    },
  ]);
  assert.equal(v.state, 'stale');
});

test('several reasons are all reported, in a stable order', () => {
  const r = assess(
    approval({ dry_run: { ok: true, at_ms: T0, matches_args: false } }),
    SNAP,
    same({ fuel_pct: 74.5, landed: false }),
    [{ id: 'w', tool: 'sim_set_weather', klass: 'sim', at: T0 + 5000 }],
  );
  assert.deepEqual(
    r.reasons.map((x) => x.code),
    ['args_differ', 'sim_change', 'fuel', 'landed'],
  );
});

test('stale summary copy for the warn strip', () => {
  assert.equal(
    staleSummary(
      [
        { code: 'fuel', from: 82, to: 74.5 },
        { code: 'landed', from: true, to: false },
      ],
      'Drone1',
    ),
    'fuel 82.0% → 74.5%; Drone1 is now airborne',
  );
  assert.equal(
    staleSummary([{ code: 'landed', from: false, to: true }], 'Drone1'),
    'Drone1 has landed',
  );
  assert.equal(
    staleSummary(
      [
        { code: 'moved', meters: 73.4 },
        { code: 'bingo', from: false, to: true },
      ],
      'Drone1',
    ),
    'Drone1 moved 73 m; Drone1 reached BINGO',
  );
  assert.equal(
    staleSummary([{ code: 'link', from: 'up', to: 'loal' }], 'Drone1'),
    'link up → lost',
  );
  assert.equal(
    staleSummary([{ code: 'sim_change', tool: 'sim_set_weather' }]),
    'Set sim weather was approved',
  );
  assert.equal(
    staleSummary([{ code: 'other_command', tool: 'uav_goto_gps' }], 'Drone1'),
    'another command for Drone1 was approved (Fly to point)',
  );
  assert.equal(
    staleSummary([{ code: 'args_differ' }]),
    'the dry run used different settings',
  );
  assert.equal(staleSummary(null), '');
});

test('vehicleFacts normalizes a graph node', () => {
  const facts = vehicleFacts(
    {
      id: 'veh:Drone1',
      type: 'vehicle',
      label: 'Drone1',
      status: 'ok',
      lat: 47.1,
      lon: -122.2,
      attrs: {
        fuel_pct: 64,
        bingo_fuel_pct: 22,
        landed: false,
        bingo_latched: false,
        link: 'up',
        agl_m: 60,
        agl_is_real: false,
        lost_link: { behaviour: 'rtb', declare_after_s: 5 },
      },
    },
    T0,
  );
  assert.equal(facts.name, 'Drone1');
  assert.equal(facts.fuel_pct, 64);
  assert.equal(facts.landed, false);
  assert.equal(facts.lat, 47.1);
  assert.equal(facts.agl_is_real, false);
  assert.deepEqual(facts.lost_link, { behaviour: 'rtb', declare_after_s: 5 });
  assert.equal(facts.at, T0);
  assert.equal(facts.eta_to_bingo_s, null);
  assert.equal(facts.read_at, null);
  const timed = vehicleFacts({
    id: 'veh:Drone1',
    ts_ms: T0 - 500,
    attrs: { fuel_pct: 64, eta_to_bingo_s: 600 },
  });
  assert.equal(timed.eta_to_bingo_s, 600);
  assert.equal(timed.read_at, T0 - 500, 'the telemetry stamp of the reading');
  assert.equal(vehicleFacts(null), null);
  const sparse = vehicleFacts({
    id: 'veh:X',
    attrs: { fuel_pct: 'lots', landed: 'no' },
  });
  assert.equal(sparse.fuel_pct, null, 'non-numbers are unknown, never coerced');
  assert.equal(sparse.landed, null);
});

test('haversine is sane', () => {
  assert.equal(haversineM(0, 0, 0, 0), 0);
  const oneDegLat = haversineM(0, 0, 1, 0);
  assert.ok(Math.abs(oneDegLat - 111_195) < 50);
});
