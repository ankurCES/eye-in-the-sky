import test from 'node:test';
import assert from 'node:assert/strict';

// Contract-level test of the UAV source normalizer against a bridge payload.
import {
  alarmSeverityRank,
  createUavEventStream,
  createUavSource,
  fetchUavActiveTheater,
  fetchUavTheaters,
  normalizeActiveTheater,
  normalizeAlarm,
  normalizeContact,
  normalizeMission,
  normalizeTheaterTable,
} from './uav.js';

const BRIDGE_SNAPSHOT = {
  sim_state: 'up',
  observedAtMs: 1789620458818,
  count: 2,
  vehicles: [
    {
      name: 'Drone1',
      latitude: 47.6419,
      longitude: -122.1395,
      alt_hae: 93.2,
      alt_msl: 122.0,
      agl: 28.5,
      speed_ms: 12.4,
      heading_deg: 61.0,
      vz: -0.4,
      landed_state: 2,
      armed: true,
      fuel_pct: 87.0,
      bingo_fuel_pct: 24.8,
      eta_to_bingo_s: 930,
      mission: 'recon',
      track_id: 'T-001',
      datum_degraded: false,
      timestamp_ms: 1789620458818,
    },
    {
      name: 'bad-no-coords',
      alt_hae: 100,
      timestamp_ms: 1789620458818,
    },
  ],
  missions: [
    {
      mission_id: 'msn-0007',
      vehicle: 'Drone1',
      kind: 'grid_search',
      phase: 'executing',
      active_tool: 'uav_fly_route',
      progress_pct: 43.5,
      waypoint: { index: 6, of: 14 },
      eta_s: 480,
      fuel_pct: 62.1,
      bingo_fuel_pct: 24.8,
      coverage_pct: 41.0,
      safety: { geofence: 'ok', proximity_m: 310, bingo_latched: false },
      incomplete_reason: null,
    },
    { vehicle: 'Drone1' }, // no mission_id: dropped, never fatal
  ],
  contacts: [
    {
      track_id: 'TRK-0003',
      category: 'sam_medium_range',
      confidence: 'probable',
      location: { lat: 33.7241, lon: 51.7238, alt_m: 1548.0 },
      last_seen_ms: 1789620450000,
      threat_level: 'high',
      salute: {
        size: '2 launchers',
        activity: 'emplaced',
        location: '33.7241N 51.7238E',
        unit: 'unknown',
        time: '2026-09-17T06:00Z',
        equipment: 'SA-6',
      },
    },
  ],
};

/** Route a stubbed fetch by bridge path. */
function stubFetch(routes) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    for (const [suffix, handler] of Object.entries(routes)) {
      if (String(url).includes(suffix)) return handler();
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

const ok = (body) => () => ({ ok: true, json: async () => body });

test('uav source normalizes bridge snapshot to contract records', async () => {
  const source = createUavSource();
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.source, 'uav');
    assert.equal(snap.status, 'ok');
    assert.equal(snap.stale, false);
    assert.equal(snap.count, 1); // the coord-less row is rejected
    const rec = snap.records[0];
    assert.equal(rec.id, 'Drone1');
    assert.equal(rec.reference, 'Drone1');
    assert.equal(rec.kind, 'uav');
    assert.equal(rec.position.latitude, 47.6419);
    assert.equal(rec.position.ellipsoidAltitude, 93.2);
    assert.equal(rec.position.barometricAltitude, 122.0);
    assert.equal(rec.velocity.speed, 12.4);
    assert.equal(rec.status.fuelPct, 87.0);
    assert.equal(rec.status.mission, 'recon');
    assert.equal(rec.status.trackId, 'T-001');
  } finally {
    fetchStub.restore();
  }
});

test('vehicle records carry BINGO, ETA-to-BINGO, MSL/AGL and the datum flag', async () => {
  const source = createUavSource();
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  try {
    const rec = (await source.getSnapshot({})).records[0];
    assert.equal(rec.status.bingoFuelPct, 24.8);
    assert.equal(rec.status.etaToBingoS, 930);
    assert.equal(rec.position.mslAltitude, 122.0);
    assert.equal(rec.position.agl, 28.5);
    assert.equal(rec.status.datumDegraded, false);
  } finally {
    fetchStub.restore();
  }
});

test('a degraded datum is surfaced on the record and the snapshot', async () => {
  const source = createUavSource();
  const degraded = {
    ...BRIDGE_SNAPSHOT,
    vehicles: [{ ...BRIDGE_SNAPSHOT.vehicles[0], datum_degraded: true }],
  };
  const fetchStub = stubFetch({ '/snapshot': ok(degraded) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.records[0].status.datumDegraded, true);
    assert.equal(snap.datumDegraded, true);
  } finally {
    fetchStub.restore();
  }
});

test('missions[] normalizes phase, tool, progress, coverage and BINGO', async () => {
  const source = createUavSource();
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.sections.missions, true);
    assert.equal(snap.missions.length, 1); // the id-less row is dropped
    const mission = snap.missions[0];
    assert.equal(mission.id, 'msn-0007');
    assert.equal(mission.vehicle, 'Drone1');
    assert.equal(mission.phase, 'executing');
    assert.equal(mission.activeTool, 'uav_fly_route');
    assert.equal(mission.progressPct, 43.5);
    assert.deepEqual(mission.waypoint, { index: 6, of: 14 });
    assert.equal(mission.etaS, 480);
    assert.equal(mission.fuelPct, 62.1);
    assert.equal(mission.bingoFuelPct, 24.8);
    assert.equal(mission.coveragePct, 41.0);
    assert.deepEqual(mission.safety, {
      geofence: 'ok',
      proximityM: 310,
      bingoLatched: false,
    });
    assert.equal(mission.incompleteReason, null);
  } finally {
    fetchStub.restore();
  }
});

test('contacts[] normalizes the contact position, threat and SALUTE', async () => {
  const source = createUavSource();
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.sections.contacts, true);
    const contact = snap.contacts[0];
    assert.equal(contact.trackId, 'TRK-0003');
    assert.equal(contact.category, 'sam_medium_range');
    assert.equal(contact.confidence, 'probable');
    assert.equal(contact.threatLevel, 'high');
    // The contact's own position, never the observer's.
    assert.equal(contact.position.latitude, 33.7241);
    assert.equal(contact.position.longitude, 51.7238);
    assert.equal(contact.position.altitude, 1548.0);
    assert.equal(contact.lastSeenAtMs, 1789620450000);
    assert.equal(contact.salute.equipment, 'SA-6');
  } finally {
    fetchStub.restore();
  }
});

test('a bridge without missions[]/contacts[] degrades instead of throwing', async () => {
  const source = createUavSource();
  const legacy = {
    sim_state: 'up',
    observedAtMs: 1789620458818,
    count: 1,
    vehicles: [BRIDGE_SNAPSHOT.vehicles[0]],
  };
  const fetchStub = stubFetch({ '/snapshot': ok(legacy) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.count, 1);
    assert.deepEqual(snap.missions, []);
    assert.deepEqual(snap.contacts, []);
    assert.equal(snap.sections.missions, false);
    assert.equal(snap.sections.contacts, false);
  } finally {
    fetchStub.restore();
  }
});

test('an all-invalid mission section never blanks the vehicle telemetry', async () => {
  const source = createUavSource();
  const broken = {
    ...BRIDGE_SNAPSHOT,
    missions: [{}, { vehicle: 'Drone1' }],
    contacts: [{ category: 'truck' }],
  };
  const fetchStub = stubFetch({ '/snapshot': ok(broken) });
  try {
    const snap = await source.getSnapshot({});
    assert.equal(snap.records.length, 1);
    assert.deepEqual(snap.missions, []);
    assert.deepEqual(snap.contacts, []);
    assert.equal(snap.sections.missions, true);
  } finally {
    fetchStub.restore();
  }
});

// `/tracks` is optional and the poll loop runs at up to 5 Hz. Asking on every
// snapshot meant a bridge without the route took a 404 five times a second for
// as long as the layer stayed on.
test('an absent /tracks route backs off instead of firing on every poll', async () => {
  let clock = 1_000_000;
  const source = createUavSource({
    now: () => clock,
    tracksRetryMs: 5000,
    tracksMaxRetryMs: 60000,
  });
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  const probes = () =>
    fetchStub.calls.filter((url) => url.endsWith('/tracks')).length;
  try {
    // Ten polls at the 5 Hz rate the layer actually uses.
    for (let i = 0; i < 10; i += 1) {
      await source.getSnapshot({});
      clock += 200;
    }
    assert.equal(probes(), 1, 'one probe per backoff window, not one per poll');
    const waiting = await source.getSnapshot({});
    assert.equal(waiting.tracksFeed.served, false);
    assert.equal(waiting.tracksFeed.polled, false);
    assert.ok(waiting.tracksFeed.retryInMs > 0);
    // The window expires, it asks once more, and the next window is longer.
    clock += 5000;
    await source.getSnapshot({});
    assert.equal(probes(), 2);
    assert.equal((await source.getSnapshot({})).tracksFeed.attempts, 2);
    clock += 5000;
    await source.getSnapshot({});
    assert.equal(probes(), 2, 'the second window is 10s, not 5s');
  } finally {
    fetchStub.restore();
  }
});

// A poll we chose not to make is not evidence that the tracks went away: the
// roster reads `targets` directly, so blanking it during a backoff would erase
// every contact the operator is working.
test('/tracks rows are held through a backoff, never blanked by a skipped poll', async () => {
  let clock = 2_000_000;
  let failing = false;
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (String(url).endsWith('/tracks')) {
      if (failing) return { ok: false, status: 503, json: async () => ({}) };
      return {
        ok: true,
        json: async () => ({ tracks: [{ track_id: 'T-9' }] }),
      };
    }
    return { ok: true, json: async () => BRIDGE_SNAPSHOT };
  };
  const ids = (snap) => snap.targets.map((row) => row.track_id);
  try {
    const source = createUavSource({ now: () => clock });
    let snap = await source.getSnapshot({});
    assert.deepEqual(ids(snap), ['T-9']);
    assert.equal(snap.tracksFeed.served, true);
    assert.equal(snap.tracksFeed.stale, false);
    failing = true;
    clock += 10_000;
    snap = await source.getSnapshot({});
    assert.deepEqual(
      ids(snap),
      ['T-9'],
      'a failed probe does not empty tracks',
    );
    assert.equal(snap.tracksFeed.stale, true);
    clock += 100;
    snap = await source.getSnapshot({});
    assert.equal(snap.tracksFeed.polled, false);
    assert.deepEqual(ids(snap), ['T-9'], 'nor does a poll inside the window');
  } finally {
    globalThis.fetch = original;
  }
});

test('a 404 on /tracks leaves the vehicle snapshot intact', async () => {
  const source = createUavSource();
  const fetchStub = stubFetch({ '/snapshot': ok(BRIDGE_SNAPSHOT) });
  try {
    const snap = await source.getSnapshot({});
    assert.deepEqual(snap.targets, []);
    assert.equal(snap.records.length, 1);
  } finally {
    fetchStub.restore();
  }
});

test('uav source throws LiveSourceError when bridge unreachable', async () => {
  const source = createUavSource();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('ECONNREFUSED');
  };
  try {
    await assert.rejects(
      () => source.getSnapshot({}),
      /UAV bridge unreachable/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('mission and contact normalizers reject unidentifiable rows', () => {
  assert.equal(normalizeMission({ phase: 'executing' }), null);
  assert.equal(normalizeContact({ category: 'truck' }), null);
  // Out-of-contract enums report as unknown rather than being invented.
  assert.equal(
    normalizeMission({ mission_id: 'm', phase: 'dancing' }).phase,
    'unknown',
  );
  assert.equal(
    normalizeContact({ track_id: 't', confidence: 'certain' }).confidence,
    'unknown',
  );
  // A contact without a fix is still a roster row, with no position.
  assert.equal(normalizeContact({ track_id: 't' }).position, null);
});

/* ---------------- theater table ---------------- */

const THEATER_PAYLOAD = {
  schema: 'godseye.theaters/v1',
  default: 'iran-natanz',
  alt_datum: 'MSL',
  theaters: [
    {
      id: 'iran-natanz',
      label: 'Iran — Natanz',
      place: 'Natanz, Isfahan province, Iran',
      description: 'Declared-facility monitoring.',
      home: [33.7243, 51.7286, 1580.0],
      home_alt_datum: 'MSL',
      ao: [
        [33.705, 51.7],
        [33.705, 51.76],
        [33.745, 51.76],
        [33.745, 51.7],
      ],
      pois: [{ name: 'Natanz Center', lat: 33.725, lon: 51.73 }],
      orbit_radius_m: 150,
      demo: {
        polygon: [
          [33.72365, 51.72838],
          [33.72365, 51.73162],
          [33.72635, 51.73162],
          [33.72635, 51.72838],
        ],
        alt_m_agl: 60,
        speed_mps: 8,
      },
    },
    { id: 'broken', home: [0], ao: [] },
  ],
};

test('the served theater table normalizes and drops unusable rows', () => {
  const table = normalizeTheaterTable(THEATER_PAYLOAD);
  assert.equal(table.schema, 'godseye.theaters/v1');
  assert.equal(table.defaultId, 'iran-natanz');
  assert.deepEqual(table.order, ['iran-natanz']);
  const natanz = table.theaters['iran-natanz'];
  assert.deepEqual(natanz.home, [33.7243, 51.7286, 1580]);
  assert.equal(natanz.homeAltDatum, 'MSL');
  assert.equal(natanz.ao.length, 4);
  assert.equal(natanz.pois[0].name, 'Natanz Center');
  assert.equal(natanz.demo.polygon.length, 4);
  assert.equal(natanz.demo.altMAgl, 60);
});

test('an empty or missing theater payload normalizes to null', () => {
  assert.equal(normalizeTheaterTable(null), null);
  assert.equal(normalizeTheaterTable({ theaters: [] }), null);
  assert.equal(normalizeTheaterTable({ theaters: [{ id: 'x' }] }), null);
});

// Finding UI-1: the table's `default` is what the table would pick; `active`
// is what the server was LAUNCHED with. Reading one as the other is how a
// stack running --theater iran-isfahan showed Redmond POIs to its operator.
/** The block the bridge republishes on /theaters.active and /health.theater. */
const RUNNING_BLOCK = {
  known: true,
  id: 'iran-natanz',
  label: 'Iran — Natanz',
  ground_elevation_msl_m: 1580.0,
  ao: [
    [33.705, 51.7],
    [33.705, 51.76],
    [33.745, 51.76],
  ],
  in_table: true,
  theater_mismatch: null,
  source: 'uav://safety/geofence',
  at_ms: 1789620458818,
  reason: '',
};

/** The same block when no server has answered — `reason` is mandatory there. */
const UNKNOWN_BLOCK = {
  known: false,
  id: null,
  label: null,
  ground_elevation_msl_m: null,
  ao: null,
  in_table: false,
  theater_mismatch: null,
  source: 'uav://safety/geofence',
  at_ms: 0,
  reason: 'uav://safety/geofence has not been read yet — MCP server not up',
};

test('the served table publishes the RUNNING theater, distinct from its default', () => {
  const served = normalizeTheaterTable({
    ...THEATER_PAYLOAD,
    default: 'iran-natanz',
    active: RUNNING_BLOCK,
  });
  assert.equal(served.active.known, true);
  assert.equal(served.active.id, 'iran-natanz');
  assert.equal(served.active.label, 'Iran — Natanz');
  assert.equal(served.active.inTable, true);
  assert.equal(served.active.source, 'uav://safety/geofence');
  assert.equal(served.active.atMs, 1789620458818);
  // A bare id string is accepted too.
  assert.equal(
    normalizeTheaterTable({ ...THEATER_PAYLOAD, active: 'iran-natanz' }).active
      .id,
    'iran-natanz',
  );
  // An id this table does not carry is REPORTED, never resolved to the
  // default — that resolution is exactly what hides the mismatch.
  const ghost = normalizeTheaterTable({
    ...THEATER_PAYLOAD,
    active: { ...RUNNING_BLOCK, id: 'ghost', label: 'Ghost AO' },
  });
  assert.equal(ghost.active.id, 'ghost');
  assert.equal(ghost.active.inTable, false);
  assert.equal(ghost.defaultId, 'iran-natanz');
  // An explicit unknown keeps its reason and never becomes a theater.
  const unknown = normalizeTheaterTable({
    ...THEATER_PAYLOAD,
    active: UNKNOWN_BLOCK,
  });
  assert.equal(unknown.active.known, false);
  assert.equal(unknown.active.id, '');
  assert.match(unknown.active.reason, /MCP server not up/);
  // A bridge that publishes nothing at all: absent stays absent.
  assert.equal(normalizeTheaterTable(THEATER_PAYLOAD).active, null);
});

test('an unusable active-theater field reads as absent, not as a theater', () => {
  assert.equal(normalizeActiveTheater(null), null);
  assert.equal(normalizeActiveTheater({ active: 42 }), null);
  assert.equal(normalizeActiveTheater({ active: '' }), null);
  assert.equal(normalizeActiveTheater({ theater: {} }), null);
  assert.equal(normalizeActiveTheater({ default: 'default' }), null);
  assert.equal(normalizeActiveTheater({ theater: { id: ' x ' } }).id, 'x');
  // `known: false` is the producer's flag and it wins over a stray id: an
  // unknown theater must never be adopted, however plausible it looks.
  const lying = normalizeActiveTheater({
    theater: { known: false, id: 'default', reason: 'MCP down' },
  });
  assert.equal(lying.known, false);
  assert.equal(lying.id, '');
  assert.equal(lying.reason, 'MCP down');
});

test('fetchUavActiveTheater reads the running theater from /health', async () => {
  const serving = stubFetch({
    '/health': ok({ ok: true, sim_state: 'up', theater: RUNNING_BLOCK }),
  });
  try {
    const running = await fetchUavActiveTheater({
      baseUrl: 'http://pinned.test',
      token: 'pinned-token',
    });
    assert.equal(running.known, true);
    assert.equal(running.id, 'iran-natanz');
    assert.equal(running.label, 'Iran — Natanz');
    assert.equal(running.feed, 'health');
    assert.equal(serving.calls[0], 'http://pinned.test/health');
  } finally {
    serving.restore();
  }
  // A bridge that answers /health with no theater key at all (finding UI-1).
  const silent = stubFetch({ '/health': ok({ ok: true, sim_state: 'up' }) });
  try {
    assert.equal(await fetchUavActiveTheater(), null);
  } finally {
    silent.restore();
  }
  // And a bridge that is not there at all is not an error either.
  const down = stubFetch({});
  try {
    assert.equal(await fetchUavActiveTheater(), null);
  } finally {
    down.restore();
  }
});

test('fetchUavTheaters resolves null when the bridge has no /theaters', async () => {
  const fetchStub = stubFetch({});
  try {
    assert.equal(await fetchUavTheaters(), null);
  } finally {
    fetchStub.restore();
  }
});

test('fetchUavTheaters returns the served table when the bridge has one', async () => {
  const fetchStub = stubFetch({ '/theaters': ok(THEATER_PAYLOAD) });
  try {
    const table = await fetchUavTheaters();
    assert.equal(table.order.length, 1);
  } finally {
    fetchStub.restore();
  }
});

/* ---------------- alarms ---------------- */

test('alarm severity defaults follow the contract, explicit severity wins', () => {
  assert.equal(normalizeAlarm({ kind: 'bingo' }).severity, 'critical');
  assert.equal(
    normalizeAlarm({ kind: 'geofence_proximity' }).severity,
    'warning',
  );
  assert.equal(
    normalizeAlarm({ kind: 'geofence_breach' }).severity,
    'critical',
  );
  assert.equal(normalizeAlarm({ kind: 'lost_link' }).severity, 'critical');
  assert.equal(normalizeAlarm({ kind: 'link_restored' }).severity, 'info');
  assert.equal(normalizeAlarm({ kind: 'detection' }).severity, 'info');
  assert.equal(normalizeAlarm({ kind: 'mission_phase' }).severity, 'info');
  assert.equal(normalizeAlarm({ kind: 'datum_degraded' }).severity, 'warning');
  assert.equal(
    normalizeAlarm({ kind: 'detection', severity: 'critical' }).severity,
    'critical',
  );
  assert.equal(normalizeAlarm({ kind: 'weird' }).severity, 'info');
  assert.ok(alarmSeverityRank('critical') > alarmSeverityRank('warning'));
  assert.ok(alarmSeverityRank('warning') > alarmSeverityRank('info'));
});

test('alarm payloads parse from SSE text and survive garbage', () => {
  const alarm = normalizeAlarm(
    '{"kind":"bingo","severity":"critical","vehicle":"Drone1","message":"BINGO fuel — forcing RTB","atMs":1789658270821}',
  );
  assert.equal(alarm.kind, 'bingo');
  assert.equal(alarm.vehicle, 'Drone1');
  assert.equal(alarm.atMs, 1789658270821);
  assert.equal(normalizeAlarm('not json'), null);
  assert.equal(normalizeAlarm(null), null);
  // A message-less alarm still reads as something in the banner.
  assert.equal(normalizeAlarm({ kind: 'lost_link' }).message, 'lost link');
});

function fakeEventSource(made) {
  return (url) => {
    const es = {
      url,
      closed: false,
      handlers: {},
      close() {
        this.closed = true;
      },
      addEventListener(type, fn) {
        this.handlers[type] = fn;
      },
    };
    made.push(es);
    return es;
  };
}

test('an absent EventSource reports unsupported and never throws', () => {
  const seen = [];
  const stream = createUavEventStream({
    eventSourceFactory: null,
    onStatus: (status) => seen.push(status),
  });
  assert.equal(stream.start(), false);
  assert.equal(stream.getStatus(), 'unsupported');
  assert.deepEqual(seen, ['unsupported']);
  stream.stop();
});

test('the alarm stream delivers events and reconnects with backoff', async () => {
  const made = [];
  const alarms = [];
  const statuses = [];
  const stream = createUavEventStream({
    eventSourceFactory: fakeEventSource(made),
    baseUrl: 'http://bridge',
    token: 'dev-token',
    retryMs: 1,
    maxRetryMs: 2,
    onAlarm: (alarm) => alarms.push(alarm),
    onStatus: (status) => statuses.push(status),
  });
  try {
    stream.start();
    assert.equal(made.length, 1);
    assert.equal(made[0].url, 'http://bridge/events?token=dev-token');
    made[0].onopen();
    assert.equal(stream.getStatus(), 'live');
    made[0].handlers.alarm({
      data: JSON.stringify({ kind: 'bingo', message: 'BINGO fuel' }),
    });
    made[0].onmessage({ data: JSON.stringify({ kind: 'detection' }) });
    assert.deepEqual(
      alarms.map((alarm) => alarm.kind),
      ['bingo', 'detection'],
    );
    // A 404 or a dropped stream reconnects; nothing is logged or thrown.
    made[0].onerror();
    assert.equal(stream.getStatus(), 'offline');
    assert.equal(made[0].closed, true);
    await new Promise((resolve) => setTimeout(resolve, 25));
    assert.ok(made.length >= 2, 'reconnected');
    assert.ok(statuses.includes('offline'));
  } finally {
    stream.stop();
  }
  assert.equal(stream.getStatus(), 'stopped');
});

test('the alarm stream can omit the token query parameter', () => {
  const made = [];
  const stream = createUavEventStream({
    eventSourceFactory: fakeEventSource(made),
    baseUrl: 'http://bridge',
    token: 'dev-token',
    tokenQueryParam: null,
  });
  assert.equal(stream.url(), 'http://bridge/events');
  stream.stop();
});

test('a throwing alarm listener never takes the stream down', () => {
  const made = [];
  const stream = createUavEventStream({
    eventSourceFactory: fakeEventSource(made),
    onAlarm: () => {
      throw new Error('listener exploded');
    },
  });
  stream.start();
  made[0].onopen();
  made[0].onmessage({ data: '{"kind":"bingo"}' });
  assert.equal(stream.getStatus(), 'live');
  stream.stop();
});

test('a cancelled poll surfaces as an abort, not as a bridge outage', async () => {
  const source = createUavSource();
  const original = globalThis.fetch;
  globalThis.fetch = async () => {
    const error = new Error('The operation was aborted');
    error.name = 'AbortError';
    throw error;
  };
  try {
    await assert.rejects(() => source.getSnapshot({}), {
      name: 'AbortError',
    });
  } finally {
    globalThis.fetch = original;
  }
});

// A caller that already knows which bridge it is talking to must be able to
// say so. Without this the mission panel's readouts resolved the bridge origin
// from module configuration while its /control calls resolved it from operator
// settings, so telemetry and commands could address two different bridges.
test('a source can be pinned to one bridge origin and bearer', async () => {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), auth: init?.headers?.Authorization });
    return { ok: true, json: async () => BRIDGE_SNAPSHOT };
  };
  try {
    const source = createUavSource({
      baseUrl: 'http://pinned.test',
      token: 'pinned-token',
    });
    await source.getSnapshot({});
    assert.equal(seen[0].url, 'http://pinned.test/snapshot');
    assert.equal(seen[0].auth, 'Bearer pinned-token');
    assert.equal(seen[1].url, 'http://pinned.test/tracks');
    assert.equal(seen[1].auth, 'Bearer pinned-token');
  } finally {
    globalThis.fetch = original;
  }
});

test('a pinned origin may be a getter, re-read on every call', async () => {
  const seen = [];
  const original = globalThis.fetch;
  let origin = 'http://first.test';
  globalThis.fetch = async (url) => {
    seen.push(String(url));
    return { ok: true, json: async () => BRIDGE_SNAPSHOT };
  };
  try {
    const source = createUavSource({ baseUrl: () => origin, token: () => 't' });
    await source.getSnapshot({});
    origin = 'http://second.test';
    await source.getSnapshot({});
    assert.match(seen[0], /^http:\/\/first\.test\//);
    assert.match(seen[2], /^http:\/\/second\.test\//);
  } finally {
    globalThis.fetch = original;
  }
});

test('the theater fetch can be pinned to the same bridge', async () => {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), auth: init?.headers?.Authorization });
    return { ok: false, status: 404, json: async () => ({}) };
  };
  try {
    const table = await fetchUavTheaters({
      baseUrl: 'http://pinned.test',
      token: 'pinned-token',
    });
    assert.equal(table, null); // a 404 still degrades to the bundled copy
    assert.equal(seen[0].url, 'http://pinned.test/theaters');
    assert.equal(seen[0].auth, 'Bearer pinned-token');
  } finally {
    globalThis.fetch = original;
  }
});
