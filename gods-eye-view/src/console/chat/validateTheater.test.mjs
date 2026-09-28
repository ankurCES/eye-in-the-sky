import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PREVIEW_MISSING,
  REQUIRED_PREVIEW_KEYS,
  assessTheater,
  blockedLine,
  failedChecks,
  fleetOf,
  isPreviewTool,
  previewProblem,
} from './validateTheater.js';

const THEATER_PREVIEW = Object.freeze({
  label: 'Bengaluru centre',
  place: 'Bengaluru, Karnataka, India',
  geocoder: 'Coordinates',
  center: [12.9716, 77.5946],
  bbox: [12.94912, 77.57156, 12.99408, 77.61764],
  half_extent_m: 2500,
  area_km2: 25.0,
  clamped_from_km: null,
  home: {
    lat: 12.974,
    lon: 77.596,
    name: 'Cubbon Park',
    source: 'overpass-open-ground',
    distance_m: 300,
  },
  ground_msl_m: 920,
  ground_source: 'Set by the operator.',
  airframe: {
    from: 'quad_suas_electric',
    to: 'quad_suas_electric',
    label: 'Quad, small electric',
    reach_m: 7350,
  },
  previous: { id: 'default', label: 'Redmond (AirSim default)' },
  checks: [
    { text: 'Drone1 on the ground', ok: true },
    { text: 'Fake simulator', ok: true },
  ],
});

function theaterApproval(extra = {}) {
  return {
    id: 'a1',
    tool: 'sim_set_theater',
    klass: 'sim',
    args: { label: 'Bengaluru centre' },
    theaterPreview: { ...THEATER_PREVIEW },
    ...extra,
  };
}

function vehicle(name, attrs = {}) {
  return {
    id: `veh:${name}`,
    type: 'vehicle',
    label: name,
    attrs: {
      landed: true,
      bingo_latched: false,
      link: 'up',
      fuel_pct: 94,
      ...attrs,
    },
  };
}

function graph(nodes, theater = { id: 'default', epoch: 0 }) {
  return { theater, nodes };
}

test('preview tools and the required keys (WG spec §3.6)', () => {
  assert.equal(isPreviewTool('sim_set_theater'), true);
  assert.equal(isPreviewTool('sim_set_time_scale'), true);
  assert.equal(isPreviewTool('sim_set_weather'), false);
  assert.equal(isPreviewTool('toString'), false);
  assert.deepEqual(
    [...REQUIRED_PREVIEW_KEYS.sim_set_theater],
    ['checks', 'center', 'bbox', 'home', 'airframe', 'ground_msl_m'],
  );
  assert.deepEqual(
    [...REQUIRED_PREVIEW_KEYS.sim_set_time_scale],
    ['checks', 'from', 'to'],
  );
  assert.equal(
    PREVIEW_MISSING,
    "The console couldn't build this preview, so it can't be approved.",
  );
});

test('previewProblem: missing preview, each missing key, malformed numbers', () => {
  assert.equal(previewProblem(theaterApproval()), null);
  assert.deepEqual(previewProblem(theaterApproval({ theaterPreview: null })), {
    code: 'missing',
  });
  for (const key of REQUIRED_PREVIEW_KEYS.sim_set_theater) {
    const p = { ...THEATER_PREVIEW };
    delete p[key];
    assert.deepEqual(
      previewProblem(theaterApproval({ theaterPreview: p })),
      { code: 'key', key },
      key,
    );
    assert.deepEqual(
      previewProblem(
        theaterApproval({
          theaterPreview: { ...THEATER_PREVIEW, [key]: null },
        }),
      ),
      { code: 'key', key },
      `${key} null`,
    );
  }
  for (const [key, bad] of [
    ['center', [12.9, 'x']],
    ['bbox', [1, 2, 3]],
    ['checks', 'all good'],
    ['ground_msl_m', '920'],
    ['home', 'Cubbon Park'],
  ]) {
    assert.deepEqual(
      previewProblem(
        theaterApproval({ theaterPreview: { ...THEATER_PREVIEW, [key]: bad } }),
      ),
      { code: 'key', key },
      key,
    );
  }
  // The server's "{} on error" is a missing preview in effect.
  assert.equal(
    previewProblem(theaterApproval({ theaterPreview: {} })).code,
    'key',
  );
  const speed = (p) => ({ tool: 'sim_set_time_scale', timeScalePreview: p });
  assert.equal(previewProblem(speed({ from: 1, to: 4, checks: [] })), null);
  assert.deepEqual(previewProblem(speed({ from: 1, checks: [] })), {
    code: 'key',
    key: 'to',
  });
  assert.deepEqual(previewProblem(speed(undefined)), { code: 'missing' });
  assert.equal(previewProblem({ tool: 'uav_land' }), null);
});

test('failedChecks: anything not ok:true fails, unnamed ones included', () => {
  assert.deepEqual(
    failedChecks({
      checks: [
        { text: 'A', ok: true },
        { text: 'B', ok: false },
        { text: 'C' },
        null,
      ],
    }),
    [{ text: 'B' }, { text: 'C' }, { text: 'an unnamed check' }],
  );
  assert.deepEqual(failedChecks(null), []);
});

test('fleetOf: busy from the vehicle mission or a running mission node', () => {
  const fleet = fleetOf(
    graph([
      vehicle('Drone1'),
      vehicle('Drone2', { mission: 'm-7' }),
      vehicle('Drone3'),
      {
        id: 'msn:m-9',
        type: 'mission',
        attrs: { vehicle: 'Drone3', phase: 'executing' },
      },
      {
        id: 'msn:m-1',
        type: 'mission',
        attrs: { vehicle: 'Drone1', phase: 'complete' },
      },
    ]),
  );
  assert.deepEqual(
    fleet.map((v) => [v.name, v.busy, v.landed]),
    [
      ['Drone1', false, true],
      ['Drone2', true, true],
      ['Drone3', true, true],
    ],
  );
  assert.deepEqual(fleetOf(null), []);
});

test('assessTheater: every refusal the console can see, and the epoch', () => {
  assert.equal(
    assessTheater(theaterApproval(), graph([vehicle('Drone1')])).ok,
    true,
  );
  const bad = assessTheater(
    theaterApproval(),
    graph([
      vehicle('Drone1', { landed: false }),
      vehicle('Drone2', { mission: 'm-1' }),
      vehicle('Drone3', { bingo_latched: true }),
      vehicle('Drone4', { link: 'lost' }),
    ]),
  );
  assert.equal(bad.ok, false);
  assert.deepEqual(
    bad.reasons.map((r) => r.code),
    ['airborne', 'busy', 'bingo', 'link'],
  );
  assert.deepEqual(bad.airborne, ['Drone1']);
  assert.equal(
    bad.line,
    "This can't be approved now: Drone1 is airborne, Drone2 has a task running, Drone3 has BINGO latched and Drone4 has lost its link. Land Drone1 first, then ask again.",
  );
  // No "Land …" clause unless something is airborne.
  const busy = assessTheater(
    theaterApproval(),
    graph([vehicle('Drone1', { mission: 'm-1' })]),
  );
  assert.equal(
    busy.line,
    "This can't be approved now: Drone1 has a task running.",
  );
  // The theater epoch moved on since the request.
  const moved = assessTheater(
    theaterApproval(),
    graph([vehicle('Drone1')], { id: 'dyn-x', epoch: 2 }),
    { requestEpoch: 1 },
  );
  assert.deepEqual(
    moved.reasons.map((r) => r.code),
    ['epoch'],
  );
  assert.equal(
    assessTheater(
      theaterApproval(),
      graph([vehicle('Drone1')], { id: 'dyn-x', epoch: 2 }),
      { requestEpoch: null },
    ).ok,
    true,
    'unknown request epoch (a replay): the server decides',
  );
  assert.deepEqual(
    assessTheater(
      theaterApproval(),
      graph([], { id: 'x', epoch: 1, state: 'switching' }),
    ).reasons.map((r) => r.code),
    ['switching'],
  );
  // Other tools are never assessed here.
  assert.equal(
    assessTheater(
      { tool: 'sim_set_time_scale' },
      graph([vehicle('Drone1', { landed: false })]),
    ).ok,
    true,
  );
});

test('blockedLine: failing checks and the graph assessment, or null', () => {
  const a = theaterApproval();
  assert.equal(blockedLine(a, { state: 'none' }), null);
  const failing = theaterApproval({
    theaterPreview: {
      ...THEATER_PREVIEW,
      checks: [{ text: 'Drone1 link up', ok: false }],
    },
  });
  assert.equal(
    blockedLine(failing, null),
    "This can't be approved now: a check failed: Drone1 link up.",
  );
  const theater = assessTheater(
    a,
    graph([vehicle('Drone1', { landed: false })]),
  );
  assert.equal(
    blockedLine(a, { state: 'blocked', theater }),
    "This can't be approved now: Drone1 is airborne. Land Drone1 first, then ask again.",
  );
  const speed = {
    tool: 'sim_set_time_scale',
    timeScalePreview: {
      from: 1,
      to: 4,
      checks: [
        { text: 'Fake simulator', ok: false },
        { text: 'Other', ok: false },
      ],
    },
  };
  assert.equal(
    blockedLine(speed, { theater }),
    "This can't be approved now: checks failed: Fake simulator; Other.",
    'the speed slip ignores the theater assessment',
  );
  assert.equal(blockedLine({ tool: 'uav_land' }, null), null);
});
