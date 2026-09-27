import test from 'node:test';
import assert from 'node:assert/strict';

import {
  abortFailure,
  abortOutcome,
  ago,
  anchorPlacement,
  canAbort,
  confirmAbort,
  createSituation,
  duration,
  displayLabel,
  feedLines,
  feedState,
  feedSummary,
  formatPoints,
  fuelSentence,
  fuelState,
  fuelTone,
  honestyLines,
  humanize,
  linkLines,
  missionKindTitle,
  pickAlarms,
  recallFocus,
  rememberFocus,
  segments,
  splitSegments,
  statusWord,
  toneOf,
  vehicleStateWord,
  zulu,
  _openConfirm,
} from './situation.js';

// ---- stub DOM (the GEV convention: no jsdom) ---------------------------------

function stubDom() {
  const doc = { activeElement: null };
  const detach = (c) => {
    const p = c?.parent;
    if (p) {
      p.children = p.children.filter((x) => x !== c);
      c.parent = null;
    }
  };
  const mk = (tag) => ({
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    textContent: '',
    value: undefined,
    parent: null,
    append(...kids) {
      for (const c of kids) {
        if (c && typeof c === 'object') {
          if (c.parent && c.parent !== this) detach(c);
          c.parent = this;
        }
        this.children.push(c);
      }
    },
    replaceChildren(...kids) {
      for (const c of this.children)
        if (c && typeof c === 'object') c.parent = null;
      this.children = [];
      this.append(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    getAttribute(k) {
      return k in this.attrs ? this.attrs[k] : null;
    },
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
    fire(t, ev = {}) {
      const e = {
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
        ...ev,
      };
      for (const fn of [...(this.listeners[t] || [])]) fn(e);
      return e;
    },
    remove() {
      detach(this);
    },
    focus() {
      doc.activeElement = this;
    },
    blur() {
      if (doc.activeElement === this) doc.activeElement = null;
    },
    contains(o) {
      for (let n = o; n; n = n.parent) if (n === this) return true;
      return false;
    },
  });
  doc.createElement = mk;
  doc.body = mk('body');
  return doc;
}

const text = (el) =>
  el == null
    ? ''
    : typeof el !== 'object'
      ? String(el)
      : (el.textContent || '') + (el.children || []).map(text).join('');

function findAll(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}
const find = (root, pred) => findAll(root, pred)[0] || null;
const hasCls = (el, cls) =>
  String(el.className || '')
    .split(/\s+/)
    .includes(cls);
const byCls = (root, cls) => findAll(root, (el) => hasCls(el, cls));
const byKey = (root, key) => find(root, (el) => el.attrs?.['data-key'] === key);
const isHidden = (el) => 'hidden' in (el?.attrs || {});
const flush = () => new Promise((r) => setImmediate(r));

function fakeBus() {
  const handlers = new Map();
  const emitted = [];
  return {
    emitted,
    on(event, cb) {
      if (!handlers.has(event)) handlers.set(event, new Set());
      handlers.get(event).add(cb);
      return () => handlers.get(event)?.delete(cb);
    },
    emit(event, payload) {
      emitted.push([event, payload]);
      for (const cb of [...(handlers.get(event) || [])]) cb(payload);
    },
    last(event) {
      return [...emitted].reverse().find(([e]) => e === event)?.[1];
    },
  };
}

function fakeStore(state) {
  const listeners = new Set();
  const viewed = [];
  return {
    state,
    viewed,
    get: () => state,
    on(event, cb) {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    change(next) {
      Object.assign(state, next);
      for (const cb of listeners) cb({});
    },
    unviewedCritical: () =>
      (state.alarms || []).filter((a) => a.critical && !viewed.includes(a.id)),
    markAlarmViewed(id) {
      viewed.push(id);
    },
  };
}

// 2026-09-27 14:06:10Z
const NOW = Date.UTC(2026, 8, 27, 14, 6, 10);

function graph() {
  return {
    schema: 'godseye.intel-graph/v1',
    generated_at_ms: NOW - 2000,
    scope: 'theater',
    theater: {
      id: 'default',
      label: 'Redmond (AirSim default)',
      place: 'Redmond, Washington, USA',
      known: true,
    },
    nodes: [
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        label: 'Drone1',
        status: 'ok',
        ts_ms: NOW - 1000,
        attrs: {
          fuel_pct: 64.2,
          bingo_fuel_pct: 22.2,
          margin_pct: 42,
          landed: false,
          link: 'up',
          mission: 'MSN-1a2b3c4d',
        },
      },
      {
        id: 'veh:Drone2',
        type: 'vehicle',
        label: 'Drone2',
        status: 'unknown',
        attrs: { fuel_pct: 100, landed: true, link: 'up' },
      },
      {
        id: 'msn:MSN-1a2b3c4d',
        type: 'mission',
        label: 'Grid search · Drone1',
        status: 'ok',
        ts_ms: null,
        attrs: {
          mission_id: 'MSN-1a2b3c4d',
          kind: 'grid_search',
          phase: 'executing',
          vehicle: 'Drone1',
          progress_pct: 42.4,
          eta_s: 110,
        },
      },
      {
        id: 'msn:MSN-old',
        type: 'mission',
        label: 'Recon route · Drone1',
        status: 'ok',
        attrs: {
          mission_id: 'MSN-old',
          kind: 'recon_route',
          phase: 'complete',
        },
      },
      {
        id: 'feed:sim',
        type: 'feed',
        label: 'Sim',
        status: 'ok',
        attrs: { ok: true },
      },
      {
        id: 'alarm:7',
        type: 'alarm',
        label: 'Bingo',
        status: 'critical',
        subtitle: 'Drone1 reached BINGO',
        ts_ms: NOW - 183_000,
        attrs: {
          seq: 7,
          kind: 'bingo',
          severity: 'critical',
          vehicle: 'Drone1',
        },
      },
    ],
    edges: [{ a: 'veh:Drone1', b: 'msn:MSN-1a2b3c4d', kind: 'flying' }],
    meta: {
      counts: {},
      out_of_theater_contacts: 27,
      duplicates_collapsed: 20,
      threat_assessed: 10,
      threat_unassessed: 2,
      caveats: [
        'Real-data layer is off: AGL is height above the launch datum (not terrain clearance), LOS is geometric with no terrain, and there is no live air traffic.',
      ],
      feeds: {
        contacts: {
          ok: false,
          status: 'critical',
          error: 'uav_list_tracks returned no tracks[]',
          at_ms: Date.UTC(2026, 8, 27, 14, 0, 12),
        },
        mission_state: { ok: true, status: 'ok', at_ms: NOW - 1000 },
      },
    },
  };
}

function alarms() {
  return [
    {
      id: 'alarm:9',
      seq: 9,
      kind: 'detection',
      severity: 'info',
      message: 'New contact',
      atMs: NOW - 10_000,
      critical: false,
    },
    {
      id: 'alarm:8',
      seq: 8,
      kind: 'mission_phase',
      severity: 'info',
      message: 'Executing',
      atMs: NOW - 60_000,
      critical: false,
    },
    {
      id: 'alarm:7',
      seq: 7,
      kind: 'bingo',
      severity: 'critical',
      message: 'Drone1 reached BINGO',
      atMs: Date.UTC(2026, 8, 27, 14, 3, 12),
      critical: true,
    },
    {
      id: 'alarm:6',
      seq: 6,
      kind: 'link_restored',
      severity: 'info',
      message: 'Link restored',
      atMs: NOW - 400_000,
      critical: false,
    },
  ];
}

function mount({ state = {}, layout = 'wide', api } = {}) {
  const doc = stubDom();
  globalThis.document = doc;
  const store = fakeStore({
    graph: graph(),
    alarms: alarms(),
    status: 'live',
    lastLiveAt: NOW - 2000,
    ...state,
  });
  const bus = fakeBus();
  const root = doc.createElement('div');
  const host = doc.createElement('div');
  root.append(host);
  const ctx = { store, bus, root, api: api || fakeApi(), orb: fakeOrb() };
  const rail = createSituation(host, ctx, {
    now: () => NOW,
    tickMs: 0,
    layout,
  });
  return { doc, store, bus, root, host, ctx, rail };
}

function fakeApi(result = { aborted: true, cancelled: [] }) {
  const calls = [];
  return {
    calls,
    abortVehicle(vehicle) {
      calls.push(vehicle);
      return typeof result === 'function'
        ? result(vehicle, calls.length)
        : Promise.resolve(result);
    },
  };
}

function fakeOrb() {
  const calls = [];
  return {
    calls,
    filter: (pred) => calls.push(['filter', pred]),
    highlight: (ids, opts) => calls.push(['highlight', ids, opts]),
  };
}

// ---- pure: fuel versus BINGO -------------------------------------------------------

test('fuel versus BINGO: states, margin and bar geometry', () => {
  const above = fuelState({ fuel_pct: 64, bingo_fuel_pct: 22 });
  assert.equal(above.state, 'above');
  assert.equal(above.margin, 42);
  assert.equal(above.fillPct, 64);
  assert.equal(above.tickPct, 22);

  assert.equal(fuelState({ fuel_pct: 30, bingo_fuel_pct: 22 }).state, 'near');
  assert.equal(fuelState({ fuel_pct: 22, bingo_fuel_pct: 22 }).state, 'below');
  assert.equal(
    fuelState({ fuel_pct: 17.2, bingo_fuel_pct: 22, bingo_latched: true })
      .state,
    'latched',
  );

  const noBingo = fuelState({ fuel_pct: 100, bingo_fuel_pct: null });
  assert.equal(noBingo.state, 'no-bingo');
  assert.equal(
    noBingo.tickPct,
    null,
    'no BINGO tick until the line is computed',
  );

  const unknown = fuelState({});
  assert.equal(unknown.state, 'unknown');
  assert.equal(unknown.fuel, null, 'a missing reading is never a zero');

  assert.equal(fuelState({ fuel_pct: 140, bingo_fuel_pct: -5 }).fillPct, 100);
  assert.equal(fuelState({ fuel_pct: 140, bingo_fuel_pct: -5 }).tickPct, 0);
  assert.equal(
    fuelState({ fuel_pct: '64.5', bingo_fuel_pct: '22' }).margin,
    42.5,
  );
});

test('fuel sentences say only what the state supports', () => {
  assert.equal(
    fuelSentence(fuelState({ fuel_pct: 64, bingo_fuel_pct: 22 })),
    '42 points above BINGO',
  );
  assert.equal(
    fuelSentence(fuelState({ fuel_pct: 26.6, bingo_fuel_pct: 22 })),
    '4.6 points above BINGO',
  );
  assert.equal(
    fuelSentence(fuelState({ fuel_pct: 23, bingo_fuel_pct: 22 })),
    '1.0 point above BINGO',
  );
  assert.equal(formatPoints(1, 0), '1 point');
  assert.equal(formatPoints(42.04, 1), '42.0 points');
  assert.equal(
    fuelSentence(fuelState({ fuel_pct: 100 })),
    'BINGO not computed yet',
  );
  assert.equal(
    fuelSentence(
      fuelState({ fuel_pct: 17.2, bingo_fuel_pct: 22, bingo_latched: true }),
    ),
    "Below BINGO. Returning home; that can't be cancelled.",
  );
  // Below the line but not latched: no claim that it is returning.
  assert.equal(
    fuelSentence(fuelState({ fuel_pct: 20, bingo_fuel_pct: 22 })),
    'Below BINGO.',
  );
  assert.equal(fuelSentence(fuelState({})), 'No fuel reading yet');
});

test('a latched BINGO after the return has landed reads Landed, not returning home', () => {
  // Graph attrs after a BINGO RTB completes: the latch stays set.
  const attrs = {
    fuel_pct: 18,
    bingo_fuel_pct: 20,
    bingo_latched: true,
    landed: true,
    agl_m: 0,
  };
  assert.equal(vehicleStateWord(attrs), 'Landed');
  const fs = fuelState(attrs);
  assert.equal(fs.state, 'latched');
  assert.equal(fs.landed, true);
  assert.equal(fuelSentence(fs), 'Below BINGO. Landed; BINGO latched.');
  assert.doesNotMatch(fuelSentence(fs), /Returning home/);
  assert.equal(
    fuelSentence(fuelState({ ...attrs, fuel_pct: 25 })),
    'Landed; BINGO latched.',
  );
  // Still airborne (or unknown): the return is still stated.
  assert.equal(
    vehicleStateWord({ ...attrs, landed: false }),
    'Returning home on BINGO',
  );
  assert.equal(
    fuelSentence(fuelState({ ...attrs, landed: undefined })),
    "Below BINGO. Returning home; that can't be cancelled.",
  );
});

test('vehicle state words, link lines and abort availability', () => {
  assert.equal(
    vehicleStateWord({ bingo_latched: true, landed: false }),
    'Returning home on BINGO',
  );
  assert.equal(vehicleStateWord({ link: 'loal', landed: false }), 'Link lost');
  assert.equal(vehicleStateWord({ landed: true }), 'Landed');
  assert.equal(vehicleStateWord({ landed: false }), 'Airborne');
  assert.equal(vehicleStateWord({}), 'State unknown');

  assert.deepEqual(linkLines({ link: 'up' }), [
    { text: 'Link up', status: 'ok' },
  ]);
  assert.deepEqual(linkLines({ link: { state: 'loal' } }), [
    { text: 'Link lost', status: 'critical' },
  ]);
  assert.equal(
    linkLines({ link: 'up', stale_ms: 42_000 })[0].text,
    'Telemetry stale, last 42 s ago',
  );
  assert.equal(linkLines({})[0].text, 'Link: no reading');

  assert.equal(canAbort({ landed: false }), true);
  assert.equal(canAbort({ landed: true }), false);
  assert.equal(canAbort({ landed: true, mission: 'MSN-1' }), true);
  assert.equal(canAbort({}), true, 'unknown state still offers the e-stop');
});

test('formatting kit: Zulu, ages, durations, segments, status words', () => {
  assert.equal(zulu(NOW), '14:06Z');
  assert.equal(zulu(NOW, { seconds: true }), '14:06:10Z');
  assert.equal(zulu(null), '');
  assert.equal(ago(NOW - 2000, NOW), '2 s');
  assert.equal(ago(NOW - 6 * 60_000, NOW), '6 min');
  assert.equal(duration(860), '14 min 20 s');
  assert.equal(duration(110), '1 min 50 s');
  assert.equal(duration(45), '45 s');
  assert.equal(duration(null), '');
  assert.deepEqual(splitSegments('2K12 Kub · probable · 2 sightings'), [
    '2K12 Kub',
    'probable',
    '2 sightings',
  ]);

  globalThis.document = stubDom();
  const el = segments('a · b');
  assert.equal(text(el), 'a, b');
  assert.equal(byCls(el, 'ic-kit-part').length, 2);
  assert.ok(!text(el).includes('·'), 'the console never writes the middle dot');

  assert.equal(
    statusWord({
      type: 'track',
      status: 'unknown',
      attrs: { threat: 'not assessed' },
    }),
    'Not assessed',
  );
  assert.equal(
    statusWord({ type: 'track', status: 'ok', attrs: { threat: 'low' } }),
    'Low',
  );
  assert.equal(
    statusWord({ type: 'track', status: 'warn', attrs: { threat: 'high' } }),
    'High',
  );
  assert.equal(
    statusWord({ type: 'unit', status: 'ok', attrs: {} }),
    'Not assessed',
  );
  assert.equal(statusWord({ type: 'feed', status: 'critical' }), 'Down');
  assert.equal(statusWord({ type: 'vehicle', status: 'ok' }), '');
  assert.equal(toneOf('track', 'ok'), 'low', 'contacts are never green');
  assert.equal(toneOf('vehicle', 'ok'), 'ok');
  assert.equal(toneOf('poi', 'critical'), 'neutral');
  // An info-level alarm is Pencil grey, as the orb draws it (search imports
  // toneOf), never green; a critical alarm keeps its tone.
  assert.equal(toneOf('alarm', 'ok'), 'neutral');
  assert.equal(toneOf('alarm', 'critical'), 'critical');
});

test('humanize upper-cases military and system acronyms', () => {
  assert.equal(humanize('grid_search'), 'Grid search');
  assert.equal(humanize('sam'), 'SAM');
  assert.equal(humanize('sam_element'), 'SAM element');
  assert.equal(humanize('c2_node'), 'C2 node');
  assert.equal(humanize('mobile_aaa'), 'Mobile AAA');
  assert.equal(humanize('gps_denied'), 'GPS denied');
  assert.equal(humanize('sample'), 'Sample', 'only whole words');
  assert.equal(humanize(''), '');
});

test('honesty and feed lines come from meta, verbatim where the server wrote them', () => {
  const meta = graph().meta;
  assert.deepEqual(honestyLines(meta), [
    'Threat assessed for 10 of 12 contacts. 2 not assessed.',
    '20 duplicate sightings merged.',
    '27 contacts outside this theater are hidden.',
  ]);
  const lines = feedLines(meta, NOW);
  // Feeds are named as the orb names them (orb/text.js), never by plumbing.
  assert.deepEqual(lines[0], {
    text: 'Contacts feed down since 14:00:12Z. No reading, not a clear picture.',
    status: 'critical',
  });
  assert.deepEqual(lines[1], {
    text: 'Mission state up, 1 s ago',
    status: 'ok',
  });
});

test('critical alarms are pinned first, unviewed before viewed', () => {
  const list = alarms();
  const picked = pickAlarms(list, new Set(['alarm:7']));
  assert.equal(picked[0].id, 'alarm:7');
  assert.equal(picked[1].id, 'alarm:9', 'then newest first');
  assert.equal(pickAlarms(list, new Set(), 2).length, 2);
});

// ---- rail rendering -----------------------------------------------------------

test('the rail renders theater, sim status and update age', () => {
  const { rail } = mount();
  const t = text(rail.element);
  assert.match(t, /Redmond \(AirSim default\)/);
  assert.match(t, /Redmond, Washington, USA/);
  assert.match(t, /Sim running/);
  assert.match(t, /Updated 2 s ago/);
});

test('the rail stops saying "Sim running" while the picture is not live', () => {
  const { rail, store } = mount();
  assert.match(text(rail.element), /Sim running/);
  for (const status of ['stale', 'offline', 'unauthorized']) {
    store.change({ status });
    const t = text(rail.element);
    assert.doesNotMatch(t, /Sim running/, status);
    assert.match(t, /Sim host not responding/, status);
  }
  store.change({ status: 'live' });
  assert.match(text(rail.element), /Sim running/, 'recovers when live again');
});

test('feed lines that say "up" take the stale tone while the picture is not live', () => {
  const { rail, store } = mount();
  const tones = () =>
    findAll(
      rail.element,
      (el) =>
        el.attrs?.['data-status'] != null && /^Mission state up/.test(text(el)),
    ).map((el) => el.attrs['data-status']);
  assert.deepEqual(tones(), ['ok']);
  store.change({ status: 'offline' });
  assert.deepEqual(tones(), ['stale']);
  store.change({ status: 'live' });
  assert.deepEqual(tones(), ['ok']);
});

test('fleet rows show fuel vs BINGO with a tick, the margin, link and actions', () => {
  const { rail } = mount();
  const row = find(
    rail.element,
    (el) => el.attrs?.['data-vehicle'] === 'Drone1',
  );
  assert.ok(row, 'Drone1 row');
  const t = text(row);
  assert.match(t, /Airborne/);
  assert.match(t, /64%/);
  assert.match(t, /BINGO 22%/);
  assert.match(t, /42 points above BINGO/);
  assert.match(t, /Link up/);
  const bar = byCls(row, 'ic-kit-fuel')[0];
  assert.equal(bar.attrs['aria-label'], 'Fuel 64%, BINGO 22%');
  const fill = byCls(bar, 'ic-kit-fuel__fill')[0];
  assert.equal(fill.attrs['data-ic-fill'], '64.2%');
  const tick = byCls(bar, 'ic-kit-fuel__tick')[0];
  assert.equal(tick.attrs['data-ic-tick'], '22.2%');
  assert.ok(byKey(row, 'track:Drone1'));
  assert.ok(byKey(row, 'abort:Drone1'), 'airborne vehicles offer Abort');
});

test('a vehicle with no BINGO line says so and draws no tick; landed without a task has no Abort', () => {
  const { rail } = mount();
  const row = find(
    rail.element,
    (el) => el.attrs?.['data-vehicle'] === 'Drone2',
  );
  assert.match(text(row), /BINGO not computed yet/);
  assert.match(text(row), /Landed/);
  assert.equal(byCls(row, 'ic-kit-fuel__tick').length, 0);
  assert.equal(byKey(row, 'abort:Drone2'), null);
});

test('a latched BINGO and a missing fuel reading are stated, never zeroed', () => {
  const g = graph();
  g.nodes[0].attrs = {
    fuel_pct: 17.2,
    bingo_fuel_pct: 22,
    bingo_latched: true,
    landed: false,
    link: 'up',
  };
  g.nodes[0].status = 'critical';
  g.nodes[1].attrs = { landed: true };
  const { rail } = mount({ state: { graph: g } });
  const t = text(rail.element);
  assert.match(t, /Returning home on BINGO/);
  assert.match(t, /Below BINGO\. Returning home; that can't be cancelled\./);
  const drone2 = find(
    rail.element,
    (el) => el.attrs?.['data-vehicle'] === 'Drone2',
  );
  assert.match(text(drone2), /No reading/);
  assert.doesNotMatch(text(drone2), /0%/);
});

test('Track emits an operator track request', () => {
  const { rail, bus } = mount();
  byKey(rail.element, 'track:Drone1').fire('click');
  assert.deepEqual(bus.last('track:request'), {
    vehicle: 'Drone1',
    source: 'operator',
  });
});

test('running missions come first with progress, time left and Track', () => {
  const { rail, bus } = mount();
  const section = find(
    rail.element,
    (el) => el.attrs?.['data-section'] === 'missions',
  );
  const t = text(section);
  assert.match(t, /1 running/);
  assert.match(t, /Grid search/);
  assert.match(t, /MSN-1a2b3c4d/);
  assert.match(t, /Executing/);
  assert.match(t, /42%/);
  assert.match(t, /≈ 1 min 50 s left/);
  assert.ok(
    t.indexOf('Grid search') < t.indexOf('Route recon'),
    'running first',
  );
  byKey(section, 'track-msn:MSN-1a2b3c4d').fire('click');
  assert.deepEqual(bus.last('track:request'), {
    vehicle: 'Drone1',
    source: 'operator',
  });
  assert.equal(
    byKey(section, 'track-msn:MSN-old'),
    null,
    'finished missions have no Track',
  );
});

test('mission kinds read as words, never raw snake case (live: "Orbit poi")', () => {
  assert.equal(missionKindTitle('orbit_poi'), 'Orbit');
  assert.equal(missionKindTitle('grid_search'), 'Grid search');
  assert.equal(missionKindTitle('recon_route'), 'Route recon');
  assert.equal(missionKindTitle('track'), 'Contact track');
  assert.equal(missionKindTitle('brand_new_kind'), 'Brand new kind');
  assert.equal(missionKindTitle(null), '');
  const g = graph();
  const m = g.nodes.find((n) => n.id === 'msn:MSN-1a2b3c4d');
  m.label = 'Orbit poi · Drone1';
  m.attrs.kind = 'orbit_poi';
  const { rail } = mount({ state: { graph: g } });
  const t = text(
    find(rail.element, (el) => el.attrs?.['data-section'] === 'missions'),
  );
  assert.match(t, /Orbit/);
  assert.doesNotMatch(t, /Orbit poi/);
});

test('once a mission has been seen, an empty mission list never says "yet"', () => {
  const { rail, store } = mount({
    state: { alarms: alarms().filter((a) => a.kind !== 'mission_phase') },
  });
  const section = () =>
    text(find(rail.element, (el) => el.attrs?.['data-section'] === 'missions'));
  assert.match(section(), /Grid search/);
  // The server drops finished missions from the picture a little later.
  const g = graph();
  g.nodes = g.nodes.filter((n) => n.type !== 'mission');
  store.change({ graph: g });
  assert.doesNotMatch(section(), /No missions yet/);
  assert.match(
    section(),
    /No missions running\. Finished missions leave this list when the server drops them\./,
  );
});

test('a mission-phase alarm alone (e.g. after a reload) also rules out "No missions yet"', () => {
  const g = graph();
  g.nodes = g.nodes.filter((n) => n.type !== 'mission');
  const { rail } = mount({ state: { graph: g } });
  const t = text(
    find(rail.element, (el) => el.attrs?.['data-section'] === 'missions'),
  );
  assert.ok(
    alarms().some((a) => a.kind === 'mission_phase'),
    'fixture has one',
  );
  assert.doesNotMatch(t, /No missions yet/);
  assert.match(t, /No missions running\./);
});

test('alarms: counts, severity glyph and word, Zulu time, New until viewed', () => {
  const { rail, store, bus } = mount();
  const section = find(
    rail.element,
    (el) => el.attrs?.['data-section'] === 'alarms',
  );
  const t = text(section);
  assert.match(t, /1 critical, 3 info/);
  const rows = byCls(section, 'ic-rail-alarm');
  assert.equal(
    rows[0].attrs['data-severity'],
    'critical',
    'critical pinned first',
  );
  assert.match(text(rows[0]), /Critical/);
  assert.match(text(rows[0]), /BINGO fuel/);
  assert.match(text(rows[0]), /14:03Z/);
  assert.match(text(rows[0]), /New/);
  assert.equal(rows[0].attrs['data-viewed'], 'false');
  assert.equal(rows[0].attrs.title, '14:03:12Z, 2 min ago');
  const glyph = byCls(rows[0], 'ic-kit-sym')[0];
  assert.equal(text(glyph), 'error');

  rows[0].fire('click');
  assert.deepEqual(store.viewed, ['alarm:7'], 'viewing goes through the store');
  assert.deepEqual(
    bus.last('inspect'),
    { id: 'alarm:7' },
    'the alarm node opens',
  );
  const after = byCls(
    find(rail.element, (el) => el.attrs?.['data-section'] === 'alarms'),
    'ic-rail-alarm',
  );
  assert.equal(after[0].attrs['data-viewed'], 'true');
  assert.doesNotMatch(text(after[0]), /New/);
});

test("What's assumed shows caveats verbatim, counts, feeds and the legend", () => {
  const { rail, bus } = mount();
  const section = find(
    rail.element,
    (el) => el.attrs?.['data-section'] === 'assumed',
  );
  const t = text(section);
  assert.ok(t.includes(graph().meta.caveats[0]), 'caveat verbatim');
  assert.match(t, /Threat assessed for 10 of 12 contacts\. 2 not assessed\./);
  assert.match(
    t,
    /Contacts feed down since 14:00:12Z\. No reading, not a clear picture\./,
  );
  assert.match(t, /≈ estimated/);
  assert.match(t, /dotted underline: assumed/);
  byKey(section, 'ask:real-data').fire('click');
  assert.deepEqual(bus.last('ask'), {
    text: 'Load real-world data for this theater.',
    draft: true,
  });
});

test('empty and loading states use the copy deck', () => {
  const g = graph();
  g.nodes = [];
  const { rail } = mount({ state: { graph: g, alarms: [] } });
  const t = text(rail.element);
  assert.match(t, /No aircraft reported\. The sim may still be starting\./);
  assert.match(
    t,
    /No missions yet\. Ask the analyst to plan one; it dry-runs first and asks before anything flies\./,
  );
  assert.match(t, /No alarms this session\./);

  const loading = mount({
    state: { graph: null, alarms: [], status: 'loading', lastLiveAt: null },
  });
  assert.match(text(loading.rail.element), /Loading the intel picture…/);

  const offline = mount({
    state: { status: 'offline', lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 5) },
  });
  assert.match(
    text(offline.rail.element),
    /Last picture 14:31:05Z, not live\./,
  );
});

test('updated age turns warn after 10 s', () => {
  const { rail } = mount({ state: { lastLiveAt: NOW - 38_000 } });
  const line = byCls(rail.element, 'ic-rail__updated')[0];
  assert.equal(text(line), 'Updated 38 s ago');
  assert.equal(line.attrs['data-status'], 'warn');
});

test('scope control appears when the store can switch scope', () => {
  const { store, bus, root } = mount();
  store.setScope = (v) => (store.scopeSet = v);
  const host = globalThis.document.createElement('div');
  root.append(host);
  const rail = createSituation(
    host,
    { store, bus, root },
    { now: () => NOW, tickMs: 0 },
  );
  const all = byKey(rail.element, 'scope:all');
  assert.match(text(all), /All theaters \(27 more contacts\)/);
  assert.equal(
    byKey(rail.element, 'scope:theater').attrs['aria-pressed'],
    'true',
  );
  all.fire('click');
  assert.equal(store.scopeSet, 'all');
});

test('compact: a 64 px strip with per-vehicle bars; click opens the drawer, Escape closes it', () => {
  const { rail, bus } = mount();
  bus.emit('layout', { layout: 'compact' });
  const strip = byCls(rail.element, 'ic-rail-strip')[0];
  assert.ok(strip, 'strip rendered');
  assert.equal(rail.element.attrs['data-layout'], 'compact');
  const bars = byCls(strip, 'ic-kit-fuel');
  assert.equal(bars.length, 2);
  assert.equal(bars[0].attrs['data-orient'], 'vertical');
  assert.ok(
    byCls(bars[0], 'ic-kit-fuel__tick').length === 1,
    'BINGO tick on the strip bar',
  );
  assert.match(
    strip.attrs['aria-label'],
    /Drone1 64%, BINGO 22%.*1 mission running.*1 critical alarm.*some readings assumed/,
  );
  const drawer = byCls(rail.element, 'ic-rail-drawer')[0];
  assert.ok(isHidden(drawer));
  strip.fire('click');
  assert.ok(!isHidden(drawer));
  assert.equal(strip.attrs['aria-expanded'], 'true');
  assert.match(text(drawer), /Fleet/);
  drawer.fire('keydown', { key: 'Escape' });
  assert.ok(isHidden(drawer));
  assert.equal(
    globalThis.document.activeElement,
    strip,
    'focus returns to the strip',
  );
  rail.setLayout('wide');
  assert.equal(byCls(rail.element, 'ic-rail-strip').length, 0);
});

test('re-render on store change keeps focus on the same control', () => {
  const { rail, store } = mount();
  byKey(rail.element, 'track:Drone1').focus();
  store.change({ lastLiveAt: NOW - 1000 });
  const again = byKey(rail.element, 'track:Drone1');
  assert.equal(globalThis.document.activeElement, again);
});

test('destroy unsubscribes and detaches', () => {
  const { rail, host, bus } = mount();
  rail.destroy();
  assert.equal(host.children.length, 0);
  bus.emit('layout', { layout: 'compact' });
  assert.equal(rail.element.attrs['data-layout'], 'wide');
});

// ---- operator Abort ------------------------------------------------------------------

function mountConfirm(api) {
  const doc = stubDom();
  globalThis.document = doc;
  const root = doc.createElement('div');
  const opener = doc.createElement('button');
  root.append(opener);
  opener.focus();
  const ctx = { api, root, bus: fakeBus() };
  const promise = confirmAbort(ctx, 'Drone1', { now: () => NOW });
  const dialog = byCls(root, 'ic-abort-confirm')[0];
  return { doc, root, opener, ctx, promise, dialog };
}

test('abort confirm: copy, safe focus, Keep flying sends nothing', async () => {
  const api = fakeApi();
  const { dialog, promise, root, opener } = mountConfirm(api);
  assert.equal(dialog.attrs.role, 'alertdialog');
  assert.match(text(dialog), /Abort Drone1\?/);
  assert.match(
    text(dialog),
    /It cancels Drone1's current task, clears its queue and holds position\. A BINGO return can't be cancelled\./,
  );
  assert.equal(
    globalThis.document.activeElement,
    byKey(dialog, 'abort:keep'),
    'focus starts on Keep flying',
  );
  assert.match(text(byKey(dialog, 'abort:go')), /Abort Drone1/);
  byKey(dialog, 'abort:keep').fire('click');
  assert.equal(await promise, false);
  assert.deepEqual(api.calls, []);
  assert.equal(byCls(root, 'ic-abort-confirm').length, 0, 'dialog removed');
  assert.equal(
    globalThis.document.activeElement,
    opener,
    'focus returns to the opener',
  );
});

test('abort confirm: success calls the command path once and reports the time', async () => {
  const api = fakeApi({ task_id: 't1', aborted: true, cancelled: [] });
  const { dialog, promise } = mountConfirm(api);
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.deepEqual(api.calls, ['Drone1']);
  assert.equal(dialog.attrs['data-outcome'], 'aborted');
  const result = byCls(dialog, 'ic-abort-confirm__result')[0];
  assert.equal(text(result), 'Drone1 aborted at 14:06:10Z. Holding position.');
  assert.equal(result.attrs.role, 'status');
  assert.equal(byKey(dialog, 'abort:retry'), null);
  byKey(dialog, 'abort:close').fire('click');
  assert.equal(await promise, true);
});

test('abort confirm: a server refusal is shown verbatim, with no retry', async () => {
  const api = fakeApi({
    aborted: false,
    refused: true,
    reason: 'uav_return_to_home is an un-cancellable safety transition',
  });
  const { dialog, promise } = mountConfirm(api);
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  const result = byCls(dialog, 'ic-abort-confirm__result')[0];
  assert.equal(
    text(result),
    'Abort refused: uav_return_to_home is an un-cancellable safety transition',
  );
  assert.equal(result.attrs.role, 'alert');
  assert.equal(byKey(dialog, 'abort:retry'), null);
  dialog.fire('keydown', { key: 'Escape' });
  assert.equal(await promise, false);
});

test('abort confirm: busy is its own outcome and can be retried', async () => {
  const api = fakeApi({
    status: 'busy',
    current: { tool: 'uav_fly_route' },
    rejected_tool: 'uav_hover',
  });
  const { dialog } = mountConfirm(api);
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.equal(dialog.attrs['data-outcome'], 'busy');
  assert.equal(
    text(byCls(dialog, 'ic-abort-confirm__result')[0]),
    "Abort wasn't run: Drone1 is busy with uav_fly_route.",
  );
  assert.ok(byKey(dialog, 'abort:retry'));
  _openConfirm()?.close();
});

test('abort confirm: a transport error says it did not reach, then Retry succeeds', async () => {
  class OfflineError extends Error {}
  const api = fakeApi((vehicle, n) =>
    n === 1
      ? Promise.reject(new OfflineError("Can't reach the local service."))
      : Promise.resolve({ aborted: true }),
  );
  const { dialog, promise } = mountConfirm(api);
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.equal(
    text(byCls(dialog, 'ic-abort-confirm__result')[0]),
    "Abort didn't reach Drone1: Can't reach the local service.",
  );
  byKey(dialog, 'abort:retry').fire('click');
  await flush();
  assert.deepEqual(api.calls, ['Drone1', 'Drone1']);
  assert.equal(dialog.attrs['data-outcome'], 'aborted');
  byKey(dialog, 'abort:close').fire('click');
  assert.equal(await promise, true);
});

test('abort outcomes and failures are classified honestly', () => {
  assert.equal(
    abortOutcome({ aborted: true }, 'Drone1', NOW).outcome,
    'aborted',
  );
  assert.deepEqual(
    abortOutcome({ rejected: true, error: 'gate' }, 'Drone1', NOW),
    {
      outcome: 'refused',
      text: 'Abort refused: gate',
      retry: false,
    },
  );
  assert.equal(
    abortOutcome({ error: 'boom', isError: true }, 'Drone1').text,
    'Abort failed on Drone1: boom.',
  );
  assert.equal(abortOutcome({ task_id: 'x' }, 'Drone1').outcome, 'unconfirmed');
  assert.equal(abortOutcome(null, 'Drone1').outcome, 'unconfirmed');

  class TimeoutError extends Error {}
  class AuthError extends Error {}
  assert.match(
    abortFailure(new TimeoutError('slow'), 'Drone1').text,
    /may still have reached Drone1/,
  );
  assert.equal(abortFailure(new AuthError('no'), 'Drone1').retry, false);
  const named = new Error('x');
  named.name = 'TimeoutError';
  assert.match(
    abortFailure(named, 'Drone1').text,
    /No answer about the abort in time/,
  );
});

test('abort confirm: one dialog per vehicle; a missing command path is an error, not a success', async () => {
  const { ctx, promise, root } = mountConfirm({});
  assert.equal(
    confirmAbort(ctx, 'Drone1'),
    promise,
    'same vehicle reuses the open confirm',
  );
  const dialog = byCls(root, 'ic-abort-confirm')[0];
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.match(
    text(dialog),
    /Abort didn't reach Drone1: the console has no command path\./,
  );
  byKey(dialog, 'abort:close').fire('click');
  assert.equal(await promise, false);
  assert.equal(await confirmAbort(ctx, ''), false);
});

test('the rail Abort button opens the confirm and aborts through the api', async () => {
  const api = fakeApi({ aborted: true });
  const { rail, root } = mount({ api });
  byKey(rail.element, 'abort:Drone1').fire('click');
  const dialog = byCls(root, 'ic-abort-confirm')[0];
  assert.ok(dialog, 'confirm mounted in ctx.root');
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.deepEqual(api.calls, ['Drone1']);
  byKey(dialog, 'abort:close').fire('click');
});

// ---- JS-PANELS-POLISH: fuel colour, feeds, link loss, honest no-graph -------

test('fuelTone colours by the fuel margin alone (§7.2 thresholds)', () => {
  assert.equal(fuelTone(fuelState({ fuel_pct: 98, bingo_fuel_pct: 20 })), 'ok');
  assert.equal(
    fuelTone(fuelState({ fuel_pct: 29, bingo_fuel_pct: 20 })),
    'warn',
    'under 10 points above',
  );
  assert.equal(
    fuelTone(fuelState({ fuel_pct: 20, bingo_fuel_pct: 20 })),
    'critical',
    'at the line',
  );
  assert.equal(
    fuelTone(
      fuelState({ fuel_pct: 60, bingo_fuel_pct: 20, bingo_latched: true }),
    ),
    'critical',
    'latched',
  );
  assert.equal(fuelTone(fuelState({ fuel_pct: 60 })), 'neutral');
  assert.equal(fuelTone(fuelState({})), 'unknown');
});

test('a vehicle critical for another reason (link lost) keeps an honest fuel colour', () => {
  const g = graph();
  g.nodes[0].status = 'critical';
  g.nodes[0].attrs = {
    fuel_pct: 98,
    bingo_fuel_pct: 20,
    landed: true,
    link: 'loal',
  };
  const { rail } = mount({ state: { graph: g } });
  const row = find(
    rail.element,
    (el) => el.attrs?.['data-vehicle'] === 'Drone1',
  );
  assert.match(text(row), /78 points above BINGO/);
  assert.match(text(row), /Link lost/);
  const margin = byCls(row, 'ic-rail__margin')[0];
  assert.equal(margin.attrs['data-status'], 'ok', 'not red');
  assert.equal(byCls(row, 'ic-kit-fuel')[0].attrs['data-status'], 'ok');

  g.nodes[0].attrs = { ...g.nodes[0].attrs, fuel_pct: 25 };
  const near = mount({ state: { graph: g } });
  const nearRow = find(
    near.rail.element,
    (el) => el.attrs?.['data-vehicle'] === 'Drone1',
  );
  assert.equal(
    byCls(nearRow, 'ic-rail__margin')[0].attrs['data-status'],
    'warn',
  );
});

test('a lost link is dated when the graph dates it (link_lost_since_ms)', () => {
  assert.deepEqual(
    linkLines({
      link: 'loal',
      link_lost_since_ms: Date.UTC(2026, 8, 27, 14, 3, 12),
    }),
    [{ text: 'Link lost since 14:03:12Z', status: 'critical' }],
  );
  assert.equal(vehicleStateWord({ link: 'lost' }), 'Link lost');
});

test('feeds are worded as the orb words them, never by server plumbing', () => {
  const contacts = {
    id: 'feed:contacts',
    type: 'feed',
    label: 'Contacts',
    status: 'ok',
    subtitle: 'mcp:uav_list_tracks',
    ts_ms: NOW - 1000,
    attrs: { ok: true },
  };
  const fs = feedState('contacts', { node: contacts, now: NOW });
  assert.equal(fs.label, 'Contacts feed');
  assert.equal(fs.word, 'Up');
  assert.equal(fs.text, 'Contacts feed up, 1 s ago');
  assert.equal(feedSummary(contacts, NOW), 'Up, 1 s ago');
  assert.equal(displayLabel(contacts), 'Contacts feed');

  const real = {
    id: 'feed:real_data',
    type: 'feed',
    label: 'Real data',
    status: 'warn',
    subtitle: 'off: AGL is height above the launch datum, LOS is geometric',
    attrs: { ok: false },
  };
  assert.deepEqual(
    (({ word, text: t, status }) => ({ word, text: t, status }))(
      feedState('real_data', { node: real, now: NOW }),
    ),
    { word: 'Off', text: 'Real data off', status: 'warn' },
  );
  const sim = feedState('sim', {
    node: { id: 'feed:sim', type: 'feed', status: 'critical', attrs: {} },
    now: NOW,
  });
  assert.equal(sim.status, 'critical');
  assert.match(sim.text, /^Sim down\. No reading, not a clear picture\.$/);

  const g = graph();
  g.nodes.push(contacts, real);
  g.meta.feeds = {
    contacts: { ok: true, status: 'ok', at_ms: NOW - 1000 },
    real_data: { ok: false, status: 'warn' },
  };
  const lines = feedLines(g.meta, NOW, g);
  assert.deepEqual(
    lines.map((l) => l.text),
    ['Contacts feed up, 1 s ago', 'Real data off'],
  );
  for (const l of lines) assert.doesNotMatch(l.text, /mcp:|uav_/);
});

test('before the first picture the rail says "no reading", never "no missions" or "no alarms"', () => {
  const offline = mount({
    state: { graph: null, alarms: [], status: 'offline', lastLiveAt: null },
  });
  const t = text(offline.rail.element);
  assert.match(t, /Theater not known yet/);
  assert.match(t, /No picture yet: the local service isn't answering\./);
  assert.match(t, /No reading until the local service answers\./);
  assert.doesNotMatch(t, /No missions yet/);
  assert.doesNotMatch(t, /No alarms this session/);
  assert.equal(byKey(offline.rail.element, 'ask:real-data'), null);
});

test("What's assumed reads caveats first, then feeds, then counts (§7.2 order)", () => {
  const { rail } = mount();
  const section = find(
    rail.element,
    (el) => el.attrs?.['data-section'] === 'assumed',
  );
  const t = text(section);
  const caveat = t.indexOf('Real-data layer is off');
  const feed = t.indexOf('Contacts feed down');
  const count = t.indexOf('Threat assessed for 10 of 12');
  assert.ok(caveat >= 0 && feed > caveat && count > feed, t);
});

test('an info alarm row keeps its severity word for screen readers only', () => {
  const { rail } = mount();
  const rows = byCls(
    find(rail.element, (el) => el.attrs?.['data-section'] === 'alarms'),
    'ic-rail-alarm',
  );
  const info = rows.find((r) => r.attrs['data-severity'] === 'info');
  const sev = byCls(info, 'ic-rail-alarm__sev')[0];
  assert.ok(hasCls(sev, 'ic-kit-vh'));
  assert.equal(byCls(info, 'ic-rail-alarm__head').length, 1);
  const crit = rows.find((r) => r.attrs['data-severity'] === 'critical');
  assert.ok(!hasCls(byCls(crit, 'ic-rail-alarm__sev')[0], 'ic-kit-vh'));
});

test('a running mission shows "Waypoint N of M" from graph v1.1 attrs.waypoint', () => {
  const g = graph();
  g.nodes[2].attrs = { ...g.nodes[2].attrs, waypoint: { index: 11, of: 26 } };
  const { rail } = mount({ state: { graph: g } });
  const section = find(
    rail.element,
    (el) => el.attrs?.['data-section'] === 'missions',
  );
  assert.match(text(section), /Waypoint 11 of 26/);
  const plain = mount();
  assert.doesNotMatch(
    text(
      find(
        plain.rail.element,
        (el) => el.attrs?.['data-section'] === 'missions',
      ),
    ),
    /Waypoint/,
    'never invented',
  );
});

// ---- review fixes: focus return, anchored confirm, one alarm name -------------------

test('abort confirm: Esc and Keep flying return focus to the rail Abort, even after the rail re-rendered', async () => {
  const api = fakeApi();
  const { rail, root, store, doc } = mount({ api });
  doc.body.append(root);
  for (const how of ['esc', 'keep']) {
    const first = byKey(rail.element, 'abort:Drone1');
    first.focus();
    first.fire('click', { currentTarget: first });
    const dialog = byCls(root, 'ic-abort-confirm')[0];
    assert.equal(doc.activeElement, byKey(dialog, 'abort:keep'));
    // The 2 s poll rebuilds the rail under the open confirm.
    store.change({ lastLiveAt: NOW - 1000 });
    const again = byKey(rail.element, 'abort:Drone1');
    assert.notEqual(again, first, 'the rail rebuilt its buttons');
    first.isConnected = false; // what a browser reports for the old node
    if (how === 'esc') dialog.fire('keydown', { key: 'Escape' });
    else byKey(dialog, 'abort:keep').fire('click');
    assert.equal(doc.activeElement, again, `${how}: focus is on Abort again`);
  }
  assert.deepEqual(api.calls, []);
});

test('recallFocus skips a control focus cannot reach (hidden or inert tab panel, not rendered)', () => {
  const doc = stubDom();
  globalThis.document = doc;
  const rail = doc.createElement('div');
  doc.body.append(rail);
  const opener = doc.createElement('button');
  opener.setAttribute('data-key', 'track:Drone1');
  rail.append(opener);
  const mem = rememberFocus(opener);
  // Narrow: back on the Orb tab, the Situation panel is hidden and inert.
  let panelHidden = true;
  opener.closest = (sel) =>
    panelHidden && /\[hidden\]|\[inert\]/.test(sel) ? rail : null;
  const sheet = doc.createElement('div');
  doc.body.append(sheet);
  const inspectorTrack = doc.createElement('button');
  inspectorTrack.setAttribute('data-key', 'act:track');
  sheet.append(inspectorTrack);
  assert.equal(
    recallFocus(mem, { fallbackKeys: ['act:track'] }),
    inspectorTrack,
    "the reopened inspector's Track, not the hidden rail button",
  );
  assert.equal(recallFocus(mem), null, 'never a control focus cannot reach');
  panelHidden = false;
  assert.equal(recallFocus(mem), opener, 'visible again: the opener');
  // display:none (no boxes) is unreachable too.
  opener.getClientRects = () => [];
  assert.equal(
    recallFocus(mem, { fallbackKeys: ['act:track'] }),
    inspectorTrack,
  );
});

test('rememberFocus/recallFocus find a re-rendered control by its key, else a fallback key', () => {
  const doc = stubDom();
  globalThis.document = doc;
  const panel = doc.createElement('div');
  doc.body.append(panel);
  const old = doc.createElement('button');
  old.setAttribute('data-key', 'track:Drone1');
  panel.append(old);
  const mem = rememberFocus(old);
  assert.equal(recallFocus(mem), old, 'still connected: the same element');
  panel.replaceChildren();
  old.isConnected = false;
  const fresh = doc.createElement('button');
  fresh.setAttribute('data-key', 'track:Drone1');
  panel.append(fresh);
  assert.equal(recallFocus(mem), fresh);
  panel.replaceChildren();
  const inspectorTrack = doc.createElement('button');
  inspectorTrack.setAttribute('data-key', 'act:track');
  panel.append(inspectorTrack);
  assert.equal(
    recallFocus(mem, { fallbackKeys: ['act:track'] }),
    inspectorTrack,
  );
  assert.equal(recallFocus(mem), null);
  assert.equal(rememberFocus(doc.body), null, 'the body is not an opener');
});

test('the abort confirm opens next to the Abort that opened it, flipped and clamped to the viewport', () => {
  const vp = { width: 1440, height: 900 };
  const box = { width: 420, height: 158 };
  // Rail Abort at x=113, y=370: under it, left-aligned.
  assert.deepEqual(
    anchorPlacement({ left: 113, top: 370, bottom: 402 }, box, vp),
    { left: 113, top: 410, flipped: false },
  );
  // Near the bottom: above it.
  assert.deepEqual(
    anchorPlacement({ left: 1300, top: 820, bottom: 852 }, box, vp),
    { left: 1004, top: 654, flipped: true },
  );
  // No room either way: pinned inside the bottom gutter; never left of 16.
  assert.deepEqual(
    anchorPlacement({ left: -40, top: 100, bottom: 132 }, box, {
      width: 420,
      height: 300,
    }),
    { left: 16, top: 126, flipped: false },
  );

  const doc = stubDom();
  globalThis.document = doc;
  const mk = doc.createElement;
  doc.createElement = (tag) =>
    Object.assign(mk(tag), {
      style: {},
      getBoundingClientRect: () => ({ width: 420, height: 158 }),
    });
  const root = doc.createElement('div');
  const anchor = doc.createElement('button');
  anchor.getBoundingClientRect = () => ({
    left: 113,
    top: 370,
    bottom: 402,
    width: 72,
    height: 32,
  });
  root.append(anchor);
  const saved = [globalThis.innerWidth, globalThis.innerHeight];
  globalThis.innerWidth = 1440;
  globalThis.innerHeight = 900;
  try {
    confirmAbort({ api: fakeApi(), root }, 'Drone1', { anchor });
    const dialog = byCls(root, 'ic-abort-confirm')[0];
    assert.equal(dialog.style.left, '113px');
    assert.equal(dialog.style.top, '410px');
    assert.equal(dialog.attrs['data-anchored'], 'below');
    _openConfirm()?.close();
    // No anchor (a shortcut, the analyst): the stylesheet's centred place.
    confirmAbort({ api: fakeApi(), root }, 'Drone1', {});
    const centred = byCls(root, 'ic-abort-confirm')[0];
    assert.equal(centred.style.left, undefined);
    assert.equal(centred.attrs['data-anchored'], undefined);
    _openConfirm()?.close();
  } finally {
    [globalThis.innerWidth, globalThis.innerHeight] = saved;
  }
});

test('an alarm node is named by its kind label everywhere (displayLabel), unknown kinds keep the server label', () => {
  assert.equal(
    displayLabel({
      id: 'alarm:7',
      type: 'alarm',
      label: 'Bingo',
      attrs: { kind: 'bingo' },
    }),
    'BINGO fuel',
  );
  assert.equal(
    displayLabel({
      id: 'alarm:8',
      type: 'alarm',
      label: 'Odd thing',
      attrs: { kind: 'odd_thing' },
    }),
    'Odd thing',
  );
});
