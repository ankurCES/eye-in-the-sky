import test from 'node:test';
import assert from 'node:assert/strict';

import {
  THEATERS,
  SEED_POIS,
  MISSIONS,
  createUavMissionPanel,
  trackRowAsContact,
  unwrapMcp,
} from './uavMissionPanel.js';
import { OFFLINE_THEATER_PAYLOAD } from './uavTheaters.js';
import { normalizeTheaterTable } from '../sources/live/uav.js';

/** A served table that shares no ids with the bundled fallback. */
const SERVED_THEATERS = {
  schema: 'godseye.theaters/v1',
  default: 'forward-ao',
  alt_datum: 'MSL',
  theaters: [
    {
      id: 'forward-ao',
      label: 'Forward AO',
      home: [10, 20, 300],
      ao: [
        [9.9, 19.9],
        [9.9, 20.1],
        [10.1, 20.1],
        [10.1, 19.9],
      ],
      pois: [{ name: 'Alpha', lat: 10, lon: 20 }],
      orbit_radius_m: 150,
      demo: { polygon: [], alt_m_agl: 60, speed_mps: 8 },
    },
  ],
};

test('seed POIs exist for every theater and lie inside its AO box', () => {
  for (const [key, t] of Object.entries(THEATERS)) {
    const seeds = SEED_POIS[key];
    assert.ok(seeds && seeds.length >= 3, `missing seeds for ${key}`);
    const lats = t.ao.map(([la]) => la);
    const lons = t.ao.map(([, lo]) => lo);
    const [laMin, laMax] = [Math.min(...lats), Math.max(...lats)];
    const [loMin, loMax] = [Math.min(...lons), Math.max(...lons)];
    for (const p of seeds) {
      assert.ok(p.lat > laMin && p.lat < laMax, `${key}/${p.name} lat in AO`);
      assert.ok(p.lon > loMin && p.lon < loMax, `${key}/${p.name} lon in AO`);
    }
  }
});

test('theaters include wartime/security AO presets with home + ao', () => {
  for (const key of [
    'indo-pak-loc',
    'iran-isfahan',
    'taiwan-strait',
    'ukraine-donbas',
    'red-sea-hormuz',
  ]) {
    const t = THEATERS[key];
    assert.ok(t, `missing theater ${key}`);
    assert.equal(t.home.length, 3);
    assert.ok(t.ao.length >= 4, 'ao polygon');
    for (const [lat, lon] of t.ao) {
      assert.ok(lat >= -90 && lat <= 90);
      assert.ok(lon >= -180 && lon <= 180);
    }
  }
});

test('missions are ISR-only (no kinetic kinds)', () => {
  for (const k of Object.keys(MISSIONS)) {
    assert.match(k, /recon|grid|orbit|track|assess/);
    assert.doesNotMatch(k, /strike|kinetic|attack|weapon/i);
  }
});

// The bundled table is only an offline fallback; it must agree with
// mcp/godseye_uav/theaters.py, which is the single source of truth. These are
// the anchors the three hand-typed copies had drifted on — "iran-isfahan"
// used to resolve at Natanz, ~118 km from the real Isfahan.
test('the offline fallback matches the python theater table anchors', () => {
  const by = Object.fromEntries(
    OFFLINE_THEATER_PAYLOAD.theaters.map((t) => [t.id, t]),
  );
  assert.deepEqual(by['iran-isfahan'].home, [32.6546, 51.668, 1570]);
  assert.deepEqual(by['iran-natanz'].home, [33.7243, 51.7286, 1580]);
  assert.deepEqual(by['iran-fordow'].home, [34.8849, 50.9958, 1550]);
  assert.deepEqual(by['taiwan-strait'].home, [24.5, 119.5, 0]);
  assert.equal(by['red-sea-hormuz'].label, 'Strait of Hormuz');
  assert.equal(OFFLINE_THEATER_PAYLOAD.schema, 'godseye.theaters/v1');
  assert.equal(OFFLINE_THEATER_PAYLOAD.alt_datum, 'MSL');
  // Every theater serves a demo box the geofence accepts (M4).
  for (const t of OFFLINE_THEATER_PAYLOAD.theaters) {
    const lats = t.ao.map(([la]) => la);
    const lons = t.ao.map(([, lo]) => lo);
    for (const [lat, lon] of t.demo.polygon) {
      assert.ok(lat > Math.min(...lats) && lat < Math.max(...lats));
      assert.ok(lon > Math.min(...lons) && lon < Math.max(...lons));
    }
  }
});

test('the MCP envelope unwraps to the flat tool result', () => {
  assert.deepEqual(unwrapMcp({ result: { structuredContent: { lat: 1 } } }), {
    lat: 1,
  });
  assert.deepEqual(
    unwrapMcp({ result: { content: [{ text: '{"fuel_pct":62.1}' }] } }),
    { fuel_pct: 62.1 },
  );
  assert.deepEqual(unwrapMcp({ task_id: 'abc' }), { task_id: 'abc' });
  assert.equal(unwrapMcp(null), null);
});

test('a /tracks row reshapes into a contacts[] row', () => {
  const row = trackRowAsContact({
    track_id: 'TRK-0007',
    unit: 'unknown',
    activity: 'emplaced',
    location: { lat: 33.72, lon: 51.73, alt_m: 1548 },
  });
  assert.equal(row.track_id, 'TRK-0007');
  assert.equal(row.location.lat, 33.72);
  assert.equal(row.salute.activity, 'emplaced');
});

// ---- DOM stub -------------------------------------------------------------

function stubDoc() {
  const nodes = [];
  const mk = (tag) => {
    const el = {
      tag,
      children: [],
      attrs: {},
      listeners: {},
      className: '',
      textContent: '',
      value: undefined,
      append(...k) {
        this.children.push(...k);
      },
      replaceChildren(...k) {
        this.children = [...k];
      },
      setAttribute(k, v) {
        this.attrs[k] = v;
      },
      removeAttribute(k) {
        delete this.attrs[k];
      },
      getAttribute(k) {
        return this.attrs[k];
      },
      addEventListener(t, f) {
        (this.listeners[t] ||= []).push(f);
      },
      fire(t, event = {}) {
        for (const fn of this.listeners[t] || []) fn(event);
      },
      remove() {},
    };
    nodes.push(el);
    return el;
  };
  return {
    createElement: mk,
    getElementById: () => null,
    body: mk('body'),
    _nodes: nodes,
  };
}

function find(root, predicate) {
  if (!root || typeof root !== 'object') return null;
  if (predicate(root)) return root;
  for (const child of root.children || []) {
    const hit = find(child, predicate);
    if (hit) return hit;
  }
  return null;
}

const byId = (root, id) => find(root, (el) => el.attrs?.id === id);

const dump = (el) =>
  JSON.stringify(el, (key, value) => (key === 'listeners' ? undefined : value));

function fakeEventSource(made = []) {
  return (url) => {
    const es = {
      url,
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

const SNAPSHOT = {
  source: 'uav',
  status: 'ok',
  simState: 'up',
  stale: false,
  observedAtMs: 1_789_620_458_818,
  count: 1,
  records: [
    {
      id: 'Drone1',
      reference: 'Drone1',
      label: 'Drone1',
      kind: 'uav',
      position: {
        latitude: 33.72,
        longitude: 51.73,
        altitude: 1620,
        ellipsoidAltitude: 1620,
        mslAltitude: 1580,
        agl: 60,
      },
      velocity: { speed: 12.4, heading: 61, verticalRate: 0.4 },
      attitude: { pitch: 0, roll: 0 },
      status: {
        landedState: 2,
        armed: true,
        fuelPct: 62.1,
        bingoFuelPct: 24.8,
        etaToBingoS: 930,
        mission: 'msn-0007',
        trackId: '',
        datumDegraded: false,
      },
      observedAtMs: 1_789_620_458_818,
    },
  ],
  targets: [],
  missions: [
    {
      id: 'msn-0007',
      vehicle: 'Drone1',
      kind: 'grid_search',
      phase: 'executing',
      activeTool: 'uav_fly_route',
      progressPct: 43.5,
      waypoint: { index: 6, of: 14 },
      etaS: 480,
      fuelPct: 62.1,
      bingoFuelPct: 24.8,
      coveragePct: 41,
      safety: { geofence: 'ok', proximityM: 310, bingoLatched: false },
      incompleteReason: null,
    },
  ],
  contacts: [
    {
      id: 'TRK-0003',
      trackId: 'TRK-0003',
      category: 'sam_medium_range',
      confidence: 'probable',
      threatLevel: 'high',
      position: { latitude: 33.7241, longitude: 51.7238, altitude: 1548 },
      lastSeenAtMs: 1_789_620_450_000,
      salute: { size: '2 launchers', activity: 'emplaced', equipment: 'SA-6' },
    },
  ],
  sections: { vehicles: true, missions: true, contacts: true },
  complete: true,
  rejectedCount: 0,
};

function mountPanel(doc, options = {}) {
  globalThis.document = doc;
  globalThis.setInterval = () => 0;
  globalThis.clearInterval = () => {};
  return createUavMissionPanel({
    bridgeUrl: () => 'http://x',
    token: () => 't',
    eventSourceFactory: fakeEventSource(),
    source: { label: 'stub', getSnapshot: async () => SNAPSHOT },
    ...options,
  });
}

test('panel builds mission params per kind from the selected theater', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc);
  // default theater -> recon route uses the AO corners as waypoints
  const t = THEATERS.default;
  assert.ok(t.ao.length >= 4);
  assert.ok(panel._panel);
  assert.equal(byId(panel._panel, 'uav-theater').children.length, 8);
});

test('panel mount/destroy lifecycle does not throw', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  assert.ok(doc.body.children.length >= 1);
  panel.destroy();
});

test('the bundled theater table is labelled as an offline fallback', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc);
  const origin = find(panel._panel, (el) =>
    /OFFLINE FALLBACK/.test(el.textContent || ''),
  );
  assert.ok(origin, 'offline label rendered');
  assert.equal(panel._theaters.isOffline(), true);
  panel.destroy();
});

test('a served theater table replaces the bundled copy and drops the label', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: async () => normalizeTheaterTable(SERVED_THEATERS),
  });
  assert.equal(panel._theaters.isOffline(), true);
  await panel._theaters.refresh();
  assert.equal(panel._theaters.isOffline(), false);
  assert.match(panel._theaters.originLabel(), /bridge · godseye\.theaters\/v1/);
  assert.deepEqual(panel._theaters.get('forward-ao').home, [10, 20, 300]);
  panel.destroy();
});

test('one tick drives the HUD from missions[] and the roster from contacts[]', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc);
  const snap = await panel.tick();
  assert.equal(snap.count, 1);
  const hud = dump(panel._hud.element);
  assert.match(hud, /EXECUTING/);
  assert.match(hud, /uav_fly_route/);
  assert.match(hud, /43\.5%/);
  assert.match(hud, /bingo 24\.8%/);
  assert.match(hud, /15:30/); // ETA to BINGO
  const roster = dump(panel._roster.element);
  assert.match(roster, /TRK-0003/);
  assert.match(roster, /SAM MEDIUM RANGE/);
  // The telemetry readout now comes from the normalized snapshot, so the
  // fuel/BINGO figures actually render instead of staying at "telemetry: —".
  const tele = find(panel._panel, (el) =>
    /fuel 62\.1%/.test(el.textContent || ''),
  );
  assert.ok(tele, 'fuel/bingo readout rendered');
  assert.match(tele.textContent, /bingo 24\.8%/);
  panel.destroy();
});

test('the roster falls back to /tracks targets until contacts[] ships', async () => {
  const doc = stubDoc();
  const legacy = {
    ...SNAPSHOT,
    missions: [],
    contacts: [],
    sections: { vehicles: true, missions: false, contacts: false },
    targets: [
      {
        track_id: 'TRK-0009',
        unit: 'unknown',
        activity: 'moving',
        location: { lat: 33.71, lon: 51.72, alt_m: 1500 },
      },
    ],
  };
  const panel = mountPanel(doc, {
    source: { label: 'stub', getSnapshot: async () => legacy },
  });
  await panel.tick();
  assert.deepEqual(
    panel._roster.rows().map((row) => row.trackId),
    ['TRK-0009'],
  );
  // The HUD says the mission feed is absent rather than inventing an idle drone.
  assert.match(dump(panel._hud.element), /mission feed not served/);
  panel.destroy();
});

test('a bridge outage leaves the readouts degraded, not broken', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    source: {
      label: 'stub',
      getSnapshot: async () => {
        throw new Error('UAV bridge unreachable');
      },
    },
  });
  assert.equal(await panel.tick(), null);
  assert.ok(
    find(panel._panel, (el) => el.textContent === 'telemetry: offline'),
  );
  panel.destroy();
});

test('an absent /events channel shows the feed banner and never throws', () => {
  const doc = stubDoc();
  globalThis.document = doc;
  globalThis.setInterval = () => 0;
  globalThis.clearInterval = () => {};
  const priorEventSource = globalThis.EventSource;
  delete globalThis.EventSource;
  try {
    const panel = createUavMissionPanel({
      bridgeUrl: () => 'http://x',
      token: () => 't',
      source: { label: 'stub', getSnapshot: async () => SNAPSHOT },
    }).mount(doc.body);
    assert.equal(panel._events.getStatus(), 'unsupported');
    assert.match(dump(panel._alarms.banner), /no \/events channel/);
    panel.destroy();
  } finally {
    if (priorEventSource) globalThis.EventSource = priorEventSource;
  }
});

test('an SSE alarm reaches the toast surface and the HUD banner', () => {
  const doc = stubDoc();
  const made = [];
  const panel = mountPanel(doc, {
    eventSourceFactory: fakeEventSource(made),
  }).mount(doc.body);
  assert.equal(made.length, 1);
  made[0].onopen();
  made[0].handlers.alarm({
    data: JSON.stringify({
      kind: 'bingo',
      severity: 'critical',
      vehicle: 'Drone1',
      message: 'BINGO fuel — forcing RTB',
      atMs: Date.now(),
    }),
  });
  assert.equal(panel._alarms.active().length, 1);
  assert.match(dump(panel._alarms.element), /BINGO fuel/);
  assert.equal(panel._alarms.banner.attrs['data-severity'], 'critical');
  panel.destroy();
});

test('grid search uses the served demo box, which the geofence accepts', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc);
  const missionSel = byId(panel._panel, 'uav-mission');
  missionSel.value = 'grid_search';
  const theater = panel._theaters.get('iran-natanz');
  assert.equal(theater.demo.polygon.length, 4);
  const lats = theater.ao.map(([la]) => la);
  for (const [lat] of theater.demo.polygon)
    assert.ok(lat > Math.min(...lats) && lat < Math.max(...lats));
  panel.destroy();
});

// ---- finding UI-1: the panel must adopt the RUNNING theater ----------------

/**
 * The bundled rows, served, with the bridge's own `active` block on top.
 * Shape per BRIDGE_CONTRACT "THE ACTIVE THEATER" / theaters.active_from_server.
 */
const runningBlock = (id) => ({
  known: true,
  id,
  label: '',
  ground_elevation_msl_m: 1570.0,
  ao: null,
  in_table: true,
  theater_mismatch: null,
  source: 'uav://safety/geofence',
  at_ms: 1789620458818,
  reason: '',
});

const unknownBlock = (reason) => ({
  known: false,
  id: null,
  label: null,
  ground_elevation_msl_m: null,
  ao: null,
  in_table: false,
  theater_mismatch: null,
  source: 'uav://safety/geofence',
  at_ms: 0,
  reason,
});

/** The bundled rows, served, with the bridge declaring what it is flying. */
const runningTable = (active) => async () =>
  normalizeTheaterTable({ ...OFFLINE_THEATER_PAYLOAD, active });

const optionText = (el) =>
  (el?.children || []).map((option) => option.children?.[0] || '');

/** The Launch button, found the way the operator finds it: by its label. */
const launchButton = (panel) =>
  find(
    panel._panel,
    (el) => el.tag === 'button' && (el.children || []).includes('Launch'),
  );

// The stack was running --theater iran-isfahan and the drone was over Isfahan,
// but the panel initialised its selector to "default" and listed Redmond POIs.
// The outcome that matters is not the selector: it is the coordinate the
// Launch button actually submits, ~10,000 km from the aircraft.
test('the panel adopts the theater the bridge is RUNNING, not the table default', async () => {
  const doc = stubDoc();
  const original = globalThis.fetch;
  let submitted = null;
  const posted = new Promise((resolve) => {
    globalThis.fetch = async (url, init) => {
      if (String(url).includes('/control/mission')) {
        submitted = JSON.parse(init.body);
        resolve(submitted);
      }
      return { ok: true, json: async () => ({ task_id: 'abcdef012345' }) };
    };
  });
  try {
    const panel = mountPanel(doc, {
      theaterLoader: runningTable(runningBlock('iran-isfahan')),
    });
    await panel.adoptRunningTheater();

    assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
    const seeds = optionText(byId(panel._panel, 'uav-poi-seed'));
    assert.ok(
      seeds.some((text) => /Isfahan North/.test(text)),
      `seeded the running theater: ${seeds}`,
    );
    assert.ok(
      !seeds.some((text) => /North Field/.test(text)),
      'no Redmond seeds while the bridge flies Isfahan',
    );

    byId(panel._panel, 'uav-mission').value = 'orbit_poi';
    launchButton(panel).fire('click');
    const mission = await posted;
    assert.equal(mission.kind, 'orbit_poi');
    assert.ok(
      Math.abs(mission.params.lat - 32.6546) < 0.001 &&
        Math.abs(mission.params.lon - 51.668) < 0.001,
      `the mission must be flown at the aircraft: ${JSON.stringify(mission.params)}`,
    );
    panel.destroy();
  } finally {
    globalThis.fetch = original;
    assert.ok(submitted, 'a mission was submitted');
  }
});

test('a selector that disagrees with the running theater says so, loudly', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable(runningBlock('iran-isfahan')),
  });
  await panel.adoptRunningTheater();
  assert.match(panel._running.textContent, /running: Iran — Isfahan/);
  assert.ok(!/mismatch/.test(panel._running.className));

  const selector = byId(panel._panel, 'uav-theater');
  selector.value = 'default';
  selector.fire('change');
  assert.match(panel._running.textContent, /SELECTOR ≠ RUNNING THEATER/);
  assert.match(panel._running.textContent, /iran-isfahan/);
  assert.ok(/mismatch/.test(panel._running.className), 'rendered as a warning');

  // ...and the operator's own choice is not taken away from them again.
  await panel.adoptRunningTheater();
  assert.equal(selector.value, 'default');
  assert.match(panel._running.textContent, /SELECTOR ≠ RUNNING THEATER/);
  panel.destroy();
});

// Today's bridge has no theater key at all. That must cost the operator a
// label, not a broken panel or a console full of retries.
test('a bridge that publishes no running theater degrades quietly', async () => {
  const doc = stubDoc();
  const noise = [];
  const restore = ['error', 'warn', 'log'].map((level) => {
    const previous = console[level];
    console[level] = (...args) => noise.push([level, ...args]);
    return () => {
      console[level] = previous;
    };
  });
  try {
    const panel = mountPanel(doc, {
      theaterLoader: runningTable(undefined),
      activeTheaterLoader: async () => null,
    });
    await panel.adoptRunningTheater();
    assert.equal(panel._theaters.active(), null);
    assert.match(panel._running.textContent, /not published by this bridge/);
    assert.ok(!/mismatch/.test(panel._running.className));
    assert.equal(byId(panel._panel, 'uav-theater').value, 'default');
    assert.deepEqual(noise, []);
    panel.destroy();
  } finally {
    for (const undo of restore) undo();
  }
});

test('the running theater is read from /health when /theaters omits it', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable(undefined),
    // Exactly what fetchUavActiveTheater() resolves from /health.theater.
    activeTheaterLoader: async () => ({
      ...runningBlock('iran-natanz'),
      label: '',
      known: true,
      atMs: 1789620458818,
      feed: 'health',
    }),
  });
  await panel.adoptRunningTheater();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-natanz');
  // The label is filled in from the table, so the line names a place.
  assert.match(panel._running.textContent, /running: Iran — Natanz/);
  panel.destroy();
});

// `known: false` carries the bridge's own reason, and the operator has to act
// on it (wait vs restart the stack). It is also not a settled answer: the MCP
// server coming up later must still be picked up.
test('an explicit "unknown theater" shows the bridge reason and keeps asking', async () => {
  const doc = stubDoc();
  let block = unknownBlock(
    'uav://safety/geofence has not been read yet — the MCP server is not up',
  );
  let healthCalls = 0;
  const panel = mountPanel(doc, {
    theaterLoader: async () =>
      normalizeTheaterTable({ ...OFFLINE_THEATER_PAYLOAD, active: block }),
    activeTheaterLoader: async () => {
      healthCalls += 1;
      return null;
    },
  });
  await panel.adoptRunningTheater();
  assert.equal(panel._theaters.active().known, false);
  assert.match(
    panel._running.textContent,
    /running theater: unknown — .*MCP server is not up/,
  );
  assert.ok(!/mismatch/.test(panel._running.className));
  assert.equal(byId(panel._panel, 'uav-theater').value, 'default');
  assert.equal(
    healthCalls,
    0,
    '/health is not re-asked for a block /theaters already served',
  );
  // The MCP server comes up: an unknown is a reason to ask again, not to stop.
  block = runningBlock('iran-isfahan');
  for (let i = 0; i < 15; i += 1) await panel.tick();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
  assert.match(panel._running.textContent, /running: Iran — Isfahan/);
  panel.destroy();
});

// The worst case the block can report: the selector, the POIs and the drawn AO
// all agree, and the envelope actually being ENFORCED belongs to another
// theater. A matching selector must not read as "all clear".
test('a server-side envelope mismatch warns even when the selector matches', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable({
      ...runningBlock('iran-isfahan'),
      theater_mismatch: { enforced: 'default', derived: 'iran-isfahan' },
    }),
  });
  await panel.adoptRunningTheater();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
  assert.match(panel._running.textContent, /SERVER REPORTS A THEATER MISMATCH/);
  assert.ok(/mismatch/.test(panel._running.className));
  panel.destroy();
});

test('before the bridge has been asked, the panel says so', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable(runningBlock('iran-isfahan')),
  });
  // Constructed, not yet refreshed: "not published" would be a claim about a
  // bridge nobody has asked.
  assert.match(panel._running.textContent, /asking the bridge/);
  await panel.adoptRunningTheater();
  assert.match(panel._running.textContent, /running: Iran — Isfahan/);
  panel.destroy();
});

test('a bridge blip does not blank a running theater already learned', async () => {
  const doc = stubDoc();
  let up = true;
  const panel = mountPanel(doc, {
    theaterLoader: async () => {
      if (!up) throw new Error('bridge went away');
      return normalizeTheaterTable({
        ...OFFLINE_THEATER_PAYLOAD,
        active: runningBlock('iran-isfahan'),
      });
    },
    activeTheaterLoader: async () => null,
  });
  await panel.adoptRunningTheater();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
  up = false;
  await panel.adoptRunningTheater();
  assert.equal(panel._theaters.active()?.id, 'iran-isfahan');
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
  assert.match(panel._running.textContent, /running: Iran — Isfahan/);
  panel.destroy();
});

test('a running theater the table does not carry is called out, not defaulted', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable(runningBlock('ghost-ao')),
  });
  await panel.adoptRunningTheater();
  // Nothing to adopt — but the operator is told, rather than being quietly
  // handed the table default as if it were the running theater.
  assert.equal(byId(panel._panel, 'uav-theater').value, 'default');
  assert.match(
    panel._running.textContent,
    /RUNNING THEATER ghost-ao IS NOT IN THIS TABLE/,
  );
  assert.ok(/mismatch/.test(panel._running.className));
  panel.destroy();
});

// The panel mounts with the page; the bridge is routinely started afterwards.
test('a bridge that comes up after the panel is adopted on a later tick', async () => {
  const doc = stubDoc();
  let running = null;
  let loads = 0;
  const panel = mountPanel(doc, {
    theaterLoader: async () => {
      loads += 1;
      return running
        ? normalizeTheaterTable({
            ...OFFLINE_THEATER_PAYLOAD,
            active: runningBlock(running),
          })
        : null;
    },
    activeTheaterLoader: async () => null,
  });
  await panel.adoptRunningTheater();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'default');
  running = 'iran-natanz';
  for (let i = 0; i < 15; i += 1) await panel.tick();
  assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-natanz');
  assert.match(panel._running.textContent, /running: Iran — Natanz/);
  panel.destroy();
  assert.equal(loads, 2, 'one load at mount, one on the retry tick');
});

// Adoption fixes the SELECTOR, and the selector is not the only thing the
// Launch button reads. A coordinate in the POI box was entered under the
// theater adoption just replaced, and the operator did not ask for that move.
// Verified in a real browser against the bridge's own /theaters bytes: the
// selector and the running line both read "iran-isfahan" while the submitted
// mission carried Redmond, 10,000 km from the aircraft, and the status line
// said "Orbit POI queued".
test('a target set before adoption is dropped, not flown at the new theater', async () => {
  const doc = stubDoc();
  const original = globalThis.fetch;
  let submitted = null;
  const posted = new Promise((resolve) => {
    globalThis.fetch = async (url, init) => {
      if (String(url).includes('/control/mission')) {
        submitted = JSON.parse(init.body);
        resolve(submitted);
      }
      return { ok: true, json: async () => ({ task_id: 'abcdef012345' }) };
    };
  });
  try {
    let active;
    const panel = mountPanel(doc, {
      theaterLoader: async () =>
        normalizeTheaterTable({ ...OFFLINE_THEATER_PAYLOAD, active }),
      activeTheaterLoader: async () => null,
    });
    await panel.adoptRunningTheater(); // the bridge publishes nothing yet
    assert.equal(byId(panel._panel, 'uav-theater').value, 'default');
    // The operator lines up a Redmond target while the bridge is still silent.
    byId(panel._panel, 'uav-poi').value = '47.6445,-122.1402';
    // ...and only then does the MCP server come up, flying Isfahan.
    active = runningBlock('iran-isfahan');
    await panel.adoptRunningTheater();
    assert.equal(byId(panel._panel, 'uav-theater').value, 'iran-isfahan');
    assert.equal(byId(panel._panel, 'uav-poi').value, '');
    // ...and the drop is stated, never silent: an emptied box the operator
    // cannot account for is its own way of losing their target. Read before
    // the launch, which writes its own status over it.
    const status = find(panel._panel, (el) =>
      /dropped/.test(el.textContent || ''),
    );
    assert.ok(status, 'the dropped target is reported to the operator');
    assert.match(status.textContent, /47\.6445,-122\.1402/);
    assert.match(status.textContent, /default/);

    byId(panel._panel, 'uav-mission').value = 'orbit_poi';
    launchButton(panel).fire('click');
    const mission = await posted;
    assert.ok(
      Math.abs(mission.params.lat - 32.6546) < 0.001 &&
        Math.abs(mission.params.lon - 51.668) < 0.001,
      `a target must not survive the theater it was set for: ${JSON.stringify(mission.params)}`,
    );
    panel.destroy();
  } finally {
    globalThis.fetch = original;
    assert.ok(submitted, 'a mission was submitted');
  }
});

// The converse: adoption that does not move the selector must leave the
// operator's target alone. A "safety" clear that fires on every pass would
// silently erase a target they set on purpose.
test('an adoption pass that changes nothing keeps the operator target', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    theaterLoader: runningTable(runningBlock('iran-isfahan')),
  });
  await panel.adoptRunningTheater();
  byId(panel._panel, 'uav-poi').value = '32.6700,51.6600';
  await panel.adoptRunningTheater();
  assert.equal(byId(panel._panel, 'uav-poi').value, '32.6700,51.6600');
  panel.destroy();
});

test('the adoption retry is bounded, not an endless probe', async () => {
  const doc = stubDoc();
  let loads = 0;
  const panel = mountPanel(doc, {
    theaterLoader: async () => {
      loads += 1;
      return null;
    },
    activeTheaterLoader: async () => null,
  });
  for (let i = 0; i < 100; i += 1) await panel.tick();
  assert.equal(loads, 5, 'five attempts, then it stops asking');
  panel.destroy();
});

// ---- regressions -----------------------------------------------------------

// /tracks proxies uav_list_tracks, i.e. targets.salute_report(): its
// size/activity/unit/time/equipment/confidence are OBJECTS and the timestamp is
// seconds. Reading them as flat strings rendered every SALUTE field as
// "[object Object]" and left the threat chip uncoloured and bottom-ranked.
const SALUTE_REPORT_ROW = {
  format: 'SALUTE',
  track_id: 'TRK-0003',
  size: { count: 2, element: 'section', text: '2 x SA-6 Gainful' },
  activity: { code: 'emplaced', text: 'emplaced, engine off' },
  location: { lat: 33.7241, lon: 51.7238, alt_m: 1548 },
  unit: {
    category: 'sam_medium_range',
    assessment: 'section',
    text: 'SA-6 Gainful — section',
  },
  time: {
    epoch: 1789620450,
    iso: '2026-09-17T06:07:30Z',
    last_seen: 1789620450,
  },
  equipment: { platform: 'SA-6 Gainful', text: 'SA-6 Gainful (SA-6 TEL)' },
  confidence: { level: 'probable', score: 0.82 },
  category: 'sam_medium_range',
  confidence_level: 'probable',
  threat_level: 'High',
};

test('a salute_report /tracks row reads as text, not [object Object]', () => {
  const row = trackRowAsContact(SALUTE_REPORT_ROW);
  assert.equal(row.category, 'sam_medium_range');
  assert.equal(row.confidence, 'probable');
  assert.equal(row.last_seen_ms, 1789620450000);
  for (const value of Object.values(row.salute))
    assert.doesNotMatch(String(value), /\[object Object\]/);
  assert.equal(row.salute.size, '2 x SA-6 Gainful');
  assert.equal(row.salute.activity, 'emplaced, engine off');
  assert.equal(row.salute.unit, 'SA-6 Gainful — section');
  assert.equal(row.salute.equipment, 'SA-6 Gainful (SA-6 TEL)');
});

test('the /tracks roster grades threat and confidence like contacts[] does', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    source: {
      label: 'stub',
      getSnapshot: async () => ({
        ...SNAPSHOT,
        contacts: [],
        sections: { vehicles: true, missions: false, contacts: false },
        targets: [SALUTE_REPORT_ROW],
      }),
    },
  });
  await panel.tick();
  const [row] = panel._roster.rows();
  // "High" must fold to the roster's own vocabulary, or the worst contact
  // ranks 0 and matches none of the threat colour rules.
  assert.equal(row.threatLevel, 'high');
  assert.equal(row.threatRank, 4);
  assert.equal(row.confidence, 'probable');
  assert.equal(row.confidenceRank, 2);
  assert.doesNotMatch(row.salute, /\[object Object\]/);
  assert.notEqual(row.age, '—');
  panel.destroy();
});

test('a served but empty contacts[] does not resurrect /tracks rows', async () => {
  const doc = stubDoc();
  const panel = mountPanel(doc, {
    source: {
      label: 'stub',
      getSnapshot: async () => ({
        ...SNAPSHOT,
        contacts: [],
        sections: { vehicles: true, missions: true, contacts: true },
        targets: [SALUTE_REPORT_ROW],
      }),
    },
  });
  await panel.tick();
  assert.deepEqual(panel._roster.rows(), []);
  panel.destroy();
});

// The /control/status fetch is rate-limited; the READING it produces is not.
// Applying it only on the fetch tick made the BINGO %, the fuel bar's BINGO
// tick and the fuel warning state blink on for one tick in five.
test('the BINGO enrichment holds between /control/status polls', async () => {
  const doc = stubDoc();
  globalThis.document = doc;
  globalThis.setInterval = () => 0;
  globalThis.clearInterval = () => {};
  const original = globalThis.fetch;
  let statusCalls = 0;
  globalThis.fetch = async (url) => {
    if (String(url).includes('/control/status')) {
      statusCalls += 1;
      return {
        ok: true,
        json: async () => ({
          result: {
            content: [{ text: '{"fuel_pct":62.1,"bingo_fuel_pct":24.8}' }],
          },
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  // A vehicle whose snapshot carries no BINGO line — today's bridge.
  const bare = {
    ...SNAPSHOT.records[0],
    status: {
      ...SNAPSHOT.records[0].status,
      bingoFuelPct: null,
      etaToBingoS: null,
    },
  };
  try {
    const panel = createUavMissionPanel({
      bridgeUrl: () => 'http://x',
      token: () => 't',
      eventSourceFactory: fakeEventSource(),
      source: {
        label: 'stub',
        getSnapshot: async () => ({
          ...SNAPSHOT,
          records: [{ ...bare, status: { ...bare.status } }],
          missions: [],
          sections: { vehicles: true, missions: false, contacts: false },
        }),
      },
    });
    const seen = [];
    for (let i = 0; i < 11; i++) {
      await panel.tick();
      const tele = find(panel._panel, (el) =>
        /^lat /.test(el.textContent || ''),
      );
      seen.push(/bingo 24\.8%/.test(tele?.textContent || ''));
    }
    assert.deepEqual(
      seen.filter(Boolean).length,
      11,
      'BINGO must read on every tick, not one tick in five',
    );
    assert.ok(statusCalls < 11, 'the fetch itself stays rate-limited');
    panel.destroy();
  } finally {
    globalThis.fetch = original;
  }
});

// An id-carrying '#uav-mission-panel button' rule outranks every class selector
// a child surface can write, so it repainted the contact-roster cards in the
// Launch button's fill and uppercased their SALUTE lines. The action-row
// buttons must be selected by their row, not by tag.
test('the panel styles its action buttons by row, not every button it contains', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  const style = doc.body.children.find(
    (el) =>
      el.tag === 'style' && /uav-mission-panel/.test(el.textContent || ''),
  );
  assert.ok(style, 'panel stylesheet mounted');
  assert.doesNotMatch(style.textContent, /#uav-mission-panel button\s*[{:[.]/);
  assert.match(style.textContent, /#uav-mission-panel \.row button\{/);
  panel.destroy();
});

// The panel mounts collapsed, and the mission controls (theater, vehicle,
// mission, LAUNCH) live in its body. Clicking the green "UAV (AirSim)" toggle
// in the layer rail enables the layer but does NOT touch this panel, so the
// operator saw the layer go green and nothing open -- there was no way to fly
// anything without first finding the panel and expanding it by hand.
// src/app/controls.js wraps the uav layer's enable() and calls expand(); these
// pin the contract that wiring depends on.
test('expand() opens the panel body and collapse() closes it', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  assert.equal(panel.isExpanded(), false, 'mounts collapsed');
  panel.expand();
  assert.equal(panel.isExpanded(), true, 'expand() opens it');
  assert.ok(!/(^|\s)collapsed(\s|$)/.test(panel._panel.className));
  panel.collapse();
  assert.equal(panel.isExpanded(), false, 'collapse() closes it again');
  panel.destroy();
});

test('expand() reopens a panel the operator had collapsed by hand', () => {
  // Enabling the layer is an explicit "I want to use this", so it must win over
  // an earlier manual collapse -- otherwise the toggle silently does nothing
  // for anyone who ever closed the panel.
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  panel.expand();
  panel.collapse();
  assert.equal(panel.isExpanded(), false);
  panel.expand();
  assert.equal(
    panel.isExpanded(),
    true,
    'a manual collapse must not latch expand() off',
  );
  panel.destroy();
});

// The panel mounts into #left-panel-stack, which style.css sets to
// pointer-events:none so the globe stays draggable behind the rail. Clicks are
// re-enabled there only for an explicit ALLOWLIST of panel ids
// (#data-panel, #cctv-panel, #global-context-panel, #scene-panel) — and this
// panel is not on it. Without its own rule the whole panel was INERT: the
// theater select, LAUNCH, and even the expand caret all fell through to the
// Cesium canvas. Nothing caught it because every test drives the DOM directly.
test('the panel re-enables pointer events on itself', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  const style = doc.body.children.find(
    (el) =>
      el.tag === 'style' && /uav-mission-panel/.test(el.textContent || ''),
  );
  assert.ok(style, 'panel stylesheet mounted');
  assert.match(
    style.textContent,
    /#uav-mission-panel\{[^}]*pointer-events:\s*auto/,
    'the panel must declare pointer-events:auto — it inherits none from the rail',
  );
  panel.destroy();
});

// Stacked as a vertical column in the rail this panel reached ~1900px -- taller
// than any viewport, so everything from the contact roster down was unreachable
// at normal zoom. It now opens as a HORIZONTAL drawer: wider than the rail, two
// columns, and bounded by the rail's own --left-panel-allocated-height budget
// with the body doing the scrolling.
test('the open panel is a horizontal drawer, not a tall column', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  const style = doc.body.children.find(
    (el) =>
      el.tag === 'style' && /uav-mission-panel/.test(el.textContent || ''),
  );
  const css = style.textContent;
  assert.match(
    css,
    /#uav-mission-panel:not\(\.collapsed\)\{width:680px/,
    'open drawer must be wider than the 360px rail',
  );
  assert.match(
    css,
    /max-height:var\(--left-panel-allocated-height/,
    'height must come from the rail budget, not a guessed vh figure',
  );
  assert.match(
    css,
    /#uav-mission-panel \.uav-body\{display:grid;[\s\S]*?grid-template-columns:repeat\(2/,
    'the body lays out in two columns',
  );
  assert.match(
    css,
    /flex:1 1 auto;min-height:0;overflow-y:auto/,
    'the body scrolls inside the budget instead of growing past it',
  );
  panel.destroy();
});

test('the drawer carries a mission-views surface', () => {
  const doc = stubDoc();
  const panel = mountPanel(doc).mount(doc.body);
  assert.ok(panel._missionViews, 'panel exposes its mission views');
  const rows = panel._missionViews.update({
    missions: [
      {
        missionId: 'MSN-9',
        vehicle: 'Drone1',
        kind: 'grid_search',
        phase: 'executing',
        progressPct: 42,
      },
    ],
    records: [{ reference: 'Drone1' }],
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].lead, 'Drone1');
  panel.destroy();
});

test('a launched mission only stops chasing the cockpit once it really enters', () => {
  const doc = stubDoc();
  // The real onEnterCockpit fails until the UAV layer's poll loop has created
  // the Cesium entity, which is never true at LAUNCH+0. A callback that
  // reports success optimistically disarms the retry on that first failure and
  // the view never switches -- the bug this test exists for.
  const calls = [];
  let result = false;
  const panel = mountPanel(doc, {
    onEnterCockpit: (ref) => {
      calls.push(ref);
      return result;
    },
  }).mount(doc.body);

  panel.armCockpitFollow('Drone1');
  assert.equal(panel.isCockpitFollowArmed(), true);

  // No position fix yet: do not even attempt an entry.
  panel._followLaunchedMission([{ reference: 'Drone1' }]);
  assert.deepEqual(calls, [], 'no entry attempt before the drone has a fix');
  assert.equal(panel.isCockpitFollowArmed(), true);

  const withFix = [
    { reference: 'Drone1', position: { latitude: 47.6, longitude: -122.1 } },
  ];
  panel._followLaunchedMission(withFix);
  assert.deepEqual(calls, ['Drone1'], 'attempts once there is a fix');
  assert.equal(
    panel.isCockpitFollowArmed(),
    true,
    'a failed entry must keep the retry armed',
  );

  // Anything that is not a literal `true` is a failure, including the
  // undefined a callback returns when it forgets to report at all. Treating
  // "not false" as success is what made this break in the first place.
  result = undefined;
  panel._followLaunchedMission(withFix);
  assert.equal(calls.length, 2);
  assert.equal(
    panel.isCockpitFollowArmed(),
    true,
    'a non-boolean answer is not success',
  );

  result = true;
  panel._followLaunchedMission(withFix);
  assert.equal(calls.length, 3);
  assert.equal(panel.isCockpitFollowArmed(), false, 'a real entry disarms it');
  panel.destroy();
});

test('the cockpit chase gives up loudly instead of retrying forever', () => {
  const doc = stubDoc();
  let calls = 0;
  const panel = mountPanel(doc, {
    onEnterCockpit: () => {
      calls += 1;
      return false;
    },
  }).mount(doc.body);

  const withFix = [
    { reference: 'Drone1', position: { latitude: 1, longitude: 2 } },
  ];
  panel.armCockpitFollow('Drone1');
  for (let i = 0; i < 200 && panel.isCockpitFollowArmed(); i += 1) {
    panel._followLaunchedMission(withFix);
  }
  assert.equal(panel.isCockpitFollowArmed(), false, 'bounded, not infinite');
  assert.ok(calls <= 40, `gave up after ${calls} attempts`);
  panel.destroy();
});
