import test from 'node:test';
import assert from 'node:assert/strict';

import { missionViewRows } from './uavMissionViews.js';

// The panel commands one vehicle, but a theater can have several missions in
// the air. These pin the join between missions[] and the vehicles flying them,
// and the FIELD CONTRACT -- which is where this first broke: the live source
// normalizes to camelCase (sources/live/uav.js normalizeMission) while the
// bridge's own /snapshot is snake_case, and reading the wrong one made every
// healthy mission report "no telemetry yet".

test('reads the normalized camelCase contract the live source publishes', () => {
  const [row] = missionViewRows({
    missions: [
      {
        missionId: 'MSN-1',
        vehicle: 'Drone1',
        kind: 'grid_search',
        phase: 'executing',
        progressPct: 61,
        coveragePct: 62,
      },
    ],
    records: [{ reference: 'Drone1' }],
  });
  assert.equal(row.missionId, 'MSN-1');
  assert.equal(row.progressPct, 61);
  assert.equal(row.coveragePct, 62);
  assert.equal(row.lead, 'Drone1');
  assert.equal(row.live, true);
});

test('still reads the raw bridge snake_case shape', () => {
  const [row] = missionViewRows({
    missions: [
      {
        mission_id: 'MSN-2',
        vehicle: 'Drone1',
        kind: 'recon_route',
        phase: 'rtb',
        progress_pct: 40,
        coverage_pct: 30,
        incomplete_reason: 'incomplete - fuel',
      },
    ],
    records: [],
  });
  assert.equal(row.missionId, 'MSN-2');
  assert.equal(row.progressPct, 40);
  assert.equal(row.incompleteReason, 'incomplete - fuel');
  assert.equal(row.live, true, 'rtb is still worth watching');
});

test('a fleet is every drone on the mission, lead first', () => {
  const [row] = missionViewRows({
    missions: [{ missionId: 'MSN-3', vehicle: 'Drone1', phase: 'executing' }],
    records: [
      { reference: 'Drone2', status: { mission: 'MSN-3' } },
      { reference: 'Drone1', status: { mission: 'MSN-3' } },
      { reference: 'Drone9', status: { mission: 'MSN-OTHER' } },
    ],
  });
  assert.equal(row.lead, 'Drone1');
  assert.deepEqual(row.fleet, ['Drone1', 'Drone2']);
});

test('a lead with no telemetry yet is still listed as the lead', () => {
  // The mission exists before its drone has reported a position; dropping the
  // row here would hide a mission that is genuinely being flown.
  const [row] = missionViewRows({
    missions: [{ missionId: 'MSN-4', vehicle: 'Drone7', phase: 'planning' }],
    records: [],
  });
  assert.equal(row.lead, 'Drone7');
  assert.deepEqual(row.fleet, ['Drone7']);
  assert.equal(row.progressPct, null);
});

test('a finished mission is kept but no longer counted as live', () => {
  const rows = missionViewRows({
    missions: [
      { missionId: 'A', vehicle: 'D1', phase: 'complete', progressPct: 100 },
      { missionId: 'B', vehicle: 'D2', phase: 'executing', progressPct: 10 },
    ],
    records: [],
  });
  assert.deepEqual(
    rows.map((r) => r.live),
    [false, true],
  );
});

test('an empty or absent missions section yields no rows, never a throw', () => {
  assert.deepEqual(missionViewRows({}), []);
  assert.deepEqual(missionViewRows({ missions: null, records: null }), []);
  assert.deepEqual(missionViewRows(), []);
});

test('progress is clamped to a sane percentage', () => {
  const [a, b] = missionViewRows({
    missions: [
      { missionId: 'A', vehicle: 'D', phase: 'executing', progressPct: 140 },
      { missionId: 'B', vehicle: 'D', phase: 'executing', progressPct: -5 },
    ],
    records: [],
  });
  assert.equal(a.progressPct, 100);
  assert.equal(b.progressPct, 0);
});
