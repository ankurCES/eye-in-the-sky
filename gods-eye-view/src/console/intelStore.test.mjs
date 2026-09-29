import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AuthError, HttpError, OfflineError } from './api.js';
import { createBus } from './bus.js';
import {
  BACKOFF_MS,
  DEFAULT_WARGAME_VIEW,
  POLL_MS,
  WARGAME_VIEW_KEY,
  alarmFingerprint,
  alarmFromPayload,
  blueViewOf,
  createIntelStore,
  diffNodes,
  graphPath,
  theaterChangeBetween,
  wargameSessionOf,
} from './intelStore.js';
import { isTruthView, wargameOf } from './railWargame.js';
import { rankNodes } from './search.js';

const flush = () => new Promise((resolve) => setImmediate(resolve));

function fakeClock(start = 1_000_000) {
  let now = start;
  let seq = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) {
      const id = ++seq;
      timers.set(id, { at: now + Math.max(0, Number(ms) || 0), fn, ms });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    delays: () => [...timers.values()].map((t) => t.ms),
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) {
          if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        }
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

function fakeStorage() {
  const map = new Map();
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
  };
}

function fakeDoc(state = 'visible') {
  const listeners = {};
  return {
    visibilityState: state,
    addEventListener: (t, f) => (listeners[t] ||= []).push(f),
    removeEventListener: (t, f) => {
      listeners[t] = (listeners[t] || []).filter((x) => x !== f);
    },
    fire(t) {
      for (const f of listeners[t] || []) f();
    },
  };
}

function node(id, type, extra = {}) {
  return {
    id,
    type,
    label: id.split(':').pop(),
    subtitle: '',
    status: 'ok',
    salience: 0.5,
    ts_ms: null,
    attrs: {},
    ...extra,
  };
}

function graph(nodes, edges = [], extra = {}) {
  return {
    schema: 'godseye.intel-graph/v1',
    scope: 'theater',
    theater: { id: 'default', label: 'Redmond' },
    nodes,
    edges,
    meta: { counts: {}, caveats: [] },
    ...extra,
  };
}

/** api.get answers from a queue of graphs/errors; sse records its handlers. */
function fakeApi(replies = []) {
  const calls = [];
  const streams = [];
  return {
    calls,
    streams,
    replies,
    get(path) {
      calls.push(path);
      const next = replies.length ? replies.shift() : null;
      if (next instanceof Error) return Promise.reject(next);
      if (typeof next === 'function') return next(path);
      return Promise.resolve(next);
    },
    sse(path, handlers) {
      const s = {
        path,
        handlers,
        closed: false,
        close() {
          this.closed = true;
        },
      };
      streams.push(s);
      return s;
    },
  };
}

function setup({
  replies = [],
  doc = fakeDoc(),
  storage = fakeStorage(),
  bus = createBus(),
} = {}) {
  const clock = fakeClock();
  const api = fakeApi(replies);
  const store = createIntelStore({ api, bus, clock, doc, storage });
  const changes = [];
  store.on('change', (d) => changes.push(d));
  return { clock, api, store, changes, doc, storage, bus };
}

test('polls the theater graph, goes live, and reports every node as added', async () => {
  const g = graph([node('veh:Drone1', 'vehicle'), node('trk:T-1', 'track')]);
  const { api, store, changes, clock } = setup({ replies: [g] });
  assert.equal(store.get().status, 'loading');
  store.start();
  await flush();
  assert.deepEqual(api.calls, ['/intel/graph?scope=theater']);
  const st = store.get();
  assert.equal(st.status, 'live');
  assert.equal(st.lastLiveAt, clock.now());
  assert.equal(st.graph, g);
  assert.equal(st.byId.get('trk:T-1').type, 'track');
  assert.equal(st.byId['veh:Drone1'].type, 'vehicle');
  assert.equal(st.byId.size, 2);
  assert.deepEqual(changes.at(-1).added, ['veh:Drone1', 'trk:T-1']);
  assert.equal(changes.at(-1).first, true);
  assert.equal(changes.at(-1).status, true);
  assert.equal(changes.at(-1).statusFrom, 'loading');
  assert.equal(changes.at(-1).statusTo, 'live');
  assert.deepEqual(clock.delays(), [POLL_MS]);
  store.stop();
});

test('the diff names added, removed, updated and status-changed ids', async () => {
  const g1 = graph([
    node('a:1', 'track'),
    node('b:1', 'track'),
    node('c:1', 'track'),
  ]);
  const g2 = graph(
    [
      node('a:1', 'track'),
      node('b:1', 'track', { status: 'critical' }),
      node('d:1', 'track'),
    ],
    [{ a: 'a:1', b: 'b:1', kind: 'near' }],
  );
  const { store, changes, clock } = setup({ replies: [g1, g2, g2] });
  store.start();
  await flush();
  await clock.advance(POLL_MS);
  const d = changes.at(-1);
  assert.deepEqual(d.added, ['d:1']);
  assert.deepEqual(d.removed, ['c:1']);
  assert.deepEqual(d.updated, ['b:1']);
  assert.deepEqual(d.statusChanged, ['b:1']);
  assert.equal(d.edges, true);
  assert.equal(d.graph, true);
  const count = changes.length;
  await clock.advance(POLL_MS); // identical graph: no change event
  assert.equal(changes.length, count);
  store.stop();
});

test('errors back off to 5 s; stale keeps the last picture; recovery reports the transition', async () => {
  const g = graph([node('veh:Drone1', 'vehicle')]);
  const { store, changes, clock } = setup({
    replies: [g, new OfflineError(), g],
  });
  store.start();
  await flush();
  const liveAt = store.get().lastLiveAt;
  await clock.advance(POLL_MS);
  let st = store.get();
  assert.equal(st.status, 'stale');
  assert.equal(st.graph, g, 'the last picture is kept');
  assert.equal(st.lastLiveAt, liveAt);
  assert.equal(st.error.kind, 'offline');
  assert.equal(changes.at(-1).statusTo, 'stale');
  assert.deepEqual(clock.delays(), [BACKOFF_MS]);
  await clock.advance(BACKOFF_MS);
  st = store.get();
  assert.equal(st.status, 'live');
  assert.equal(st.error, null);
  assert.equal(changes.at(-1).statusFrom, 'stale');
  assert.deepEqual(clock.delays(), [POLL_MS]);
  store.stop();
});

test('never loaded: offline for network errors, typed http errors, unauthorized for 401', async () => {
  const a = setup({ replies: [new OfflineError()] });
  a.store.start();
  await flush();
  assert.equal(a.store.get().status, 'offline');
  assert.equal(a.store.get().graph, null);
  a.store.stop();

  const b = setup({ replies: [new HttpError('boom', { status: 500 })] });
  b.store.start();
  await flush();
  assert.equal(b.store.get().status, 'offline');
  assert.deepEqual(
    { kind: b.store.get().error.kind, status: b.store.get().error.status },
    { kind: 'http', status: 500 },
  );
  b.store.stop();

  const c = setup({ replies: [new AuthError()] });
  c.store.start();
  await flush();
  assert.equal(c.store.get().status, 'unauthorized');
  assert.deepEqual(c.clock.delays(), [BACKOFF_MS]);
  c.store.stop();
});

test('a 404 without the host JSON body means the service is not at this address', async () => {
  // e.g. a bare Vite dev server answering /intel/graph with its HTML page.
  const a = setup({
    replies: [
      new HttpError('HTTP 404 Not Found', {
        status: 404,
        body: '<!DOCTYPE html><html></html>',
      }),
    ],
  });
  a.store.start();
  await flush();
  assert.equal(a.store.get().status, 'offline');
  assert.equal(a.store.get().error.kind, 'offline');
  a.store.stop();

  // The host's own JSON 404 is still an http error with its message.
  const b = setup({
    replies: [
      new HttpError('unknown scope', {
        status: 404,
        body: { error: 'unknown scope' },
      }),
    ],
  });
  b.store.start();
  await flush();
  assert.equal(b.store.get().error.kind, 'http');
  b.store.stop();
});

test('polling pauses while the page is hidden and resumes when shown', async () => {
  const doc = fakeDoc('hidden');
  const g = graph([node('veh:Drone1', 'vehicle')]);
  const { store, clock, api } = setup({ doc, replies: [g, g] });
  store.start();
  await flush();
  assert.equal(api.calls.length, 1);
  assert.deepEqual(clock.delays(), [], 'nothing scheduled while hidden');
  doc.visibilityState = 'visible';
  doc.fire('visibilitychange');
  await flush();
  assert.equal(api.calls.length, 2);
  assert.deepEqual(clock.delays(), [POLL_MS]);
  store.stop();
});

test('refresh() polls at once and never runs two requests together', async () => {
  let release;
  const g = graph([]);
  const { store, api } = setup({
    replies: [() => new Promise((r) => (release = () => r(g))), g],
  });
  store.start();
  const again = store.refresh();
  await flush();
  assert.equal(api.calls.length, 1);
  release();
  await again;
  await flush();
  assert.equal(store.get().status, 'live');
  store.stop();
});

test('setScope asks for all theaters and drops an in-flight answer for the old scope', async () => {
  let release;
  const old = graph([node('trk:old', 'track')]);
  const all = graph([node('trk:all', 'track')], [], { scope: 'all' });
  const { store, api, changes } = setup({
    replies: [() => new Promise((r) => (release = () => r(old))), all],
  });
  store.start();
  store.setScope('all');
  assert.equal(changes.at(-1).scope, true);
  await flush();
  assert.deepEqual(api.calls, [
    '/intel/graph?scope=theater',
    '/intel/graph?scope=all',
  ]);
  release();
  await flush();
  assert.equal(store.get().scope, 'all');
  assert.ok(store.get().byId.has('trk:all'));
  assert.ok(!store.get().byId.has('trk:old'));
  store.stop();
});

// ---- alarms ---------------------------------------------------------------------

function alarmNode(seq, kind, severity, atMs, extra = {}) {
  return node(`alarm:${seq}`, 'alarm', {
    label: kind,
    subtitle: `${kind} message`,
    ts_ms: atMs,
    status: severity === 'critical' ? 'critical' : 'warn',
    attrs: { seq, kind, severity, vehicle: 'Drone1', ...extra },
  });
}

test('graph alarm nodes become alarms, newest first; criticals wait to be viewed', async () => {
  const g = graph([
    alarmNode(1, 'bingo', 'critical', 1000),
    alarmNode(2, 'geofence_proximity', 'warning', 2000),
    alarmNode(3, 'lost_link', 'critical', 3000),
  ]);
  const bus = createBus();
  const viewedEvents = [];
  bus.on('alarm:viewed', (p) => viewedEvents.push(p.id));
  const storage = fakeStorage();
  const { store, changes } = setup({ replies: [g], bus, storage });
  store.start();
  await flush();
  const st = store.get();
  assert.deepEqual(
    st.alarms.map((a) => a.id),
    ['alarm:3', 'alarm:2', 'alarm:1'],
  );
  assert.equal(st.alarms[0].message, 'lost_link message');
  assert.equal(st.alarms[0].critical, true);
  assert.equal(st.alarms[1].critical, false);
  assert.deepEqual(
    store.unviewedCritical().map((a) => a.id),
    ['alarm:1', 'alarm:3'],
  );
  // History present at the first load is not announced as new.
  assert.deepEqual(changes.at(-1).newAlarms, []);

  assert.equal(store.markAlarmViewed('alarm:1'), true);
  assert.equal(store.markAlarmViewed('alarm:1'), false, 'idempotent');
  assert.deepEqual(
    store.unviewedCritical().map((a) => a.id),
    ['alarm:3'],
  );
  assert.deepEqual(viewedEvents, ['alarm:1']);
  assert.equal(changes.at(-1).alarms, true);
  assert.equal(store.isAlarmViewed('alarm:1'), true);
  store.stop();

  // The viewed set survives a reload of the page.
  const again = setup({ replies: [g], storage });
  again.store.start();
  await flush();
  assert.deepEqual(
    again.store.unviewedCritical().map((a) => a.id),
    ['alarm:3'],
  );
  again.store.stop();
});

test('a live SSE alarm shows at once, is announced, and hands over to its graph node', async () => {
  const g1 = graph([node('veh:Drone1', 'vehicle')]);
  const payload = {
    kind: 'bingo',
    severity: 'critical',
    message:
      'Drone1 reached BINGO at 14:03:12Z (fuel 21.8%, BINGO 22.0%); returning home',
    atMs: 5000,
    vehicle: 'Drone1',
    detail: { fuel_pct: 21.8, bingo_fuel_pct: 22 },
  };
  const g2 = graph([
    node('veh:Drone1', 'vehicle'),
    alarmNode(7, 'bingo', 'critical', 5000, { vehicle: 'Drone1' }),
  ]);
  g2.nodes[1].subtitle = 'Drone1 reached BINGO at 14:03:12Z (truncated…';
  const { store, api, changes, clock } = setup({ replies: [g1, g2] });
  store.start();
  await flush();
  assert.equal(api.streams[0].path, '/events');
  assert.deepEqual(api.streams[0].handlers.events, ['alarm']);

  api.streams[0].handlers.onEvent('alarm', payload, null);
  let st = store.get();
  const live = st.alarms[0];
  assert.equal(live.id, `alarm:live:${alarmFingerprint(payload)}`);
  assert.equal(live.source, 'live');
  assert.deepEqual(
    changes.at(-1).newAlarms.map((a) => a.kind),
    ['bingo'],
  );
  assert.deepEqual(
    store.unviewedCritical().map((a) => a.id),
    [live.id],
  );
  store.markAlarmViewed(live.id);
  assert.deepEqual(store.unviewedCritical(), []);

  await clock.advance(POLL_MS);
  st = store.get();
  assert.deepEqual(
    st.alarms.map((a) => a.id),
    ['alarm:7'],
  );
  assert.equal(
    st.alarms[0].message,
    payload.message,
    'the full SSE message wins over the truncated subtitle',
  );
  assert.deepEqual(st.alarms[0].detail, payload.detail);
  assert.deepEqual(
    store.unviewedCritical(),
    [],
    'viewed carries over by fingerprint',
  );
  assert.deepEqual(changes.at(-1).newAlarms, [], 'announced once');
  // The same alarm arriving again on the lane is not duplicated.
  api.streams[0].handlers.onEvent('alarm', payload, null);
  assert.equal(store.get().alarms.length, 1);
  store.stop();
  assert.equal(api.streams[0].closed, true);
});

test('alarmFromPayload uses the hub seq when the payload carries one', () => {
  const a = alarmFromPayload({
    seq: 12,
    kind: 'lost_link',
    severity: 'critical',
    message: 'x',
    atMs: 1,
  });
  assert.equal(a.id, 'alarm:12');
  assert.equal(a.critical, true);
  const b = alarmFromPayload({
    kind: 'detection',
    severity: 'info',
    message: 'y',
    atMs: 2,
    track_id: 'T-1',
  });
  assert.equal(b.id, 'alarm:live:detection||T-1||2');
  assert.equal(b.critical, false);
});

test('vehicle() returns the node attrs and the mission it is flying', async () => {
  const g = graph(
    [
      node('veh:Drone1', 'vehicle', {
        status: 'warn',
        lat: 47.6,
        lon: -122.1,
        attrs: { fuel_pct: 64, bingo_fuel_pct: 22, link: 'up', landed: false },
      }),
      node('msn:MSN-1', 'mission', {
        label: 'Grid search · Drone1',
        attrs: { kind: 'grid_search', progress_pct: 42, vehicle: 'Drone1' },
      }),
    ],
    [{ a: 'veh:Drone1', b: 'msn:MSN-1', kind: 'flying' }],
  );
  const { store } = setup({ replies: [g] });
  store.start();
  await flush();
  const v = store.vehicle('Drone1');
  assert.equal(v.fuel_pct, 64);
  assert.equal(v.bingo_fuel_pct, 22);
  assert.equal(v.status, 'warn');
  assert.equal(v.name, 'Drone1');
  assert.equal(v.missionNode.id, 'msn:MSN-1');
  assert.equal(store.vehicle('veh:Drone1').id, 'veh:Drone1');
  assert.equal(store.vehicle('Nope'), null);
  assert.equal(store.node('msn:MSN-1').type, 'mission');
  store.stop();
});

test('entity() fetches details once for concurrent callers', async () => {
  let release;
  const { store, api } = setup({
    replies: [
      () =>
        new Promise((r) => (release = () => r({ id: 'trk:T 1', fields: {} }))),
    ],
  });
  const a = store.entity('trk:T 1');
  const b = store.entity('trk:T 1');
  assert.equal(a, b);
  await flush();
  assert.deepEqual(api.calls, ['/intel/entity/trk%3AT%201']);
  release();
  assert.equal((await a).id, 'trk:T 1');
  await assert.rejects(store.entity(''), TypeError);
});

test('noteStageInput records the time of the last stage input', () => {
  const { store, clock } = setup();
  assert.equal(store.lastStageInputAt, 0);
  store.noteStageInput();
  assert.equal(store.lastStageInputAt, clock.now());
  assert.equal(store.get().lastStageInputAt, clock.now());
});

test('diffNodes is a pure id diff', () => {
  const first = diffNodes(null, new Map(), [node('a:1', 'track')]);
  assert.deepEqual(first.added, ['a:1']);
  assert.deepEqual(first.removed, []);
});

// WG §4.2.4: the store names a theater change so the orb runs its transition.
function withTheater(theater, nodes = [node('veh:Drone1', 'vehicle')]) {
  return graph(nodes, [], { theater });
}

test('theaterChanged is null on the first graph and when nothing moved', async () => {
  const t = { id: 'default', label: 'Redmond', epoch: 0 };
  const { store, changes, clock } = setup({
    replies: [
      withTheater(t),
      withTheater(t, [node('veh:Drone1', 'vehicle'), node('trk:T-1', 'track')]),
    ],
  });
  store.start();
  await flush();
  assert.equal(changes.at(-1).first, true);
  assert.equal(changes.at(-1).theaterChanged, null);
  await clock.advance(POLL_MS);
  assert.deepEqual(changes.at(-1).added, ['trk:T-1']);
  assert.equal(changes.at(-1).theaterChanged, null);
  store.stop();
});

test('theaterChanged names from and to when the theater id changes', async () => {
  const a = { id: 'default', label: 'Redmond', place: 'Redmond, WA', epoch: 0 };
  const b = {
    id: 'dyn-kherson-1',
    label: 'Kherson',
    place: 'Kherson, Ukraine',
    epoch: 1,
  };
  const { store, changes, clock } = setup({
    replies: [withTheater(a), withTheater(b)],
  });
  store.start();
  await flush();
  await clock.advance(POLL_MS);
  const d = changes.at(-1);
  assert.equal(d.graph, true);
  assert.deepEqual(d.theaterChanged, {
    from: { id: 'default', epoch: 0, label: 'Redmond', place: 'Redmond, WA' },
    to: {
      id: 'dyn-kherson-1',
      epoch: 1,
      label: 'Kherson',
      place: 'Kherson, Ukraine',
    },
  });
  store.stop();
});

test('theaterChanged fires on an epoch change with the same id', async () => {
  const a = { id: 'dyn-x', label: 'X', epoch: 2 };
  const b = { id: 'dyn-x', label: 'X', epoch: 3 };
  const { store, changes, clock } = setup({
    replies: [withTheater(a), withTheater(b), withTheater(b)],
  });
  store.start();
  await flush();
  await clock.advance(POLL_MS);
  assert.equal(changes.at(-1).theaterChanged.from.epoch, 2);
  assert.equal(changes.at(-1).theaterChanged.to.epoch, 3);
  const count = changes.length;
  await clock.advance(POLL_MS);
  assert.equal(changes.length, count, 'identical graph: no change event');
  store.stop();
});

test('non-graph diffs carry theaterChanged: null so the orb trusts the store', async () => {
  const { store, changes, api } = setup({
    replies: [withTheater({ id: 'default', label: 'Redmond', epoch: 0 })],
  });
  store.start();
  await flush();
  api.streams[0].handlers.onEvent('alarm', {
    kind: 'bingo',
    vehicle: 'Drone1',
    message: 'BINGO',
    atMs: 5,
  });
  const d = changes.at(-1);
  assert.equal(d.alarms, true);
  assert.ok('theaterChanged' in d);
  assert.equal(d.theaterChanged, null);
  store.stop();
});

test('theaterChangeBetween: id or both epochs; a missing epoch is not a change', () => {
  assert.equal(theaterChangeBetween(null, { id: 'a' }), null);
  assert.equal(theaterChangeBetween({ id: 'a' }, null), null);
  assert.equal(theaterChangeBetween({ id: 'a' }, { id: 'a' }), null);
  assert.equal(
    theaterChangeBetween({ id: 'a' }, { id: 'a', epoch: 4 }),
    null,
    'an older server starting to report epochs is not a switch',
  );
  assert.equal(
    theaterChangeBetween({ id: 'a', epoch: 4 }, { id: 'a', epoch: null }),
    null,
  );
  assert.equal(
    theaterChangeBetween({ id: 'a', epoch: '4' }, { id: 'a', epoch: 5 }),
    null,
    'a non-integer epoch is ignored',
  );
  assert.deepEqual(theaterChangeBetween({ id: 'a' }, { id: 'b' }), {
    from: { id: 'a', epoch: null, label: null, place: null },
    to: { id: 'b', epoch: null, label: null, place: null },
  });
  assert.equal(
    theaterChangeBetween({ id: 'a', epoch: 1 }, { id: 'a', epoch: 2 }).to.epoch,
    2,
  );
  assert.equal(theaterChangeBetween({ id: '' }, { id: 'b' }), null);
});

// ---- the simulated wargame's view (WG §5.3.3) ---------------------------------------

function wgGraph(active, sessionId = 'WG-3fa9c1', extra = {}) {
  return graph([node('veh:Drone1', 'vehicle')], [], {
    meta: {
      counts: {},
      caveats: [],
      wargame: active
        ? { active: true, session_id: sessionId, truth_view: false }
        : { active: false, last: null },
      ...extra,
    },
  });
}

function brokenStorage() {
  return {
    getItem() {
      throw new Error('SecurityError: storage is blocked');
    },
    setItem() {
      throw new Error('QuotaExceededError');
    },
  };
}

test('graphPath: without truth it is the ISR request, byte for byte', () => {
  assert.equal(graphPath('theater'), '/intel/graph?scope=theater');
  assert.equal(graphPath('all', false), '/intel/graph?scope=all');
  assert.equal(
    graphPath('theater', true),
    '/intel/graph?scope=theater&truth=1',
  );
  assert.equal(wargameSessionOf(wgGraph(true)), 'WG-3fa9c1');
  assert.equal(wargameSessionOf(wgGraph(false)), null);
  assert.equal(wargameSessionOf(graph([])), null);
});

test('ISR mode: no session, no truth, no view; the request never changes', async () => {
  const { store, api, clock } = setup({
    replies: [wgGraph(false), graph([])],
  });
  store.start();
  await flush();
  await clock.advance(POLL_MS);
  assert.deepEqual(api.calls, [
    '/intel/graph?scope=theater',
    '/intel/graph?scope=theater',
  ]);
  assert.equal(store.get().view, null);
  assert.equal(store.get().truth, false);
  assert.equal(store.view, null);
  store.stop();
});

test('a session starts in Umpire view: the store asks again at once with truth=1', async () => {
  const bus = createBus();
  const views = [];
  bus.on('wargame:view', (p) => views.push(p));
  const { store, api, clock, changes } = setup({
    replies: [wgGraph(true), wgGraph(true)],
    bus,
  });
  store.start();
  await flush();
  assert.equal(store.get().view, DEFAULT_WARGAME_VIEW);
  assert.equal(changes.at(-1).view, true);
  assert.deepEqual(views[0], {
    view: 'umpire',
    truth: true,
    session_id: 'WG-3fa9c1',
  });
  await clock.advance(0);
  assert.deepEqual(api.calls, [
    '/intel/graph?scope=theater',
    '/intel/graph?scope=theater&truth=1',
  ]);
  assert.equal(store.get().truth, true);
  assert.deepEqual(clock.delays(), [POLL_MS], 'then the usual cadence');
  store.stop();
});

test('Blue view drops truth, is kept per session, and a new session starts in Umpire', async () => {
  const storage = fakeStorage();
  const { store, api, clock } = setup({
    storage,
    replies: [
      wgGraph(true, 'WG-1'),
      wgGraph(true, 'WG-1'),
      wgGraph(true, 'WG-1'),
      wgGraph(true, 'WG-2'),
      wgGraph(true, 'WG-2'),
    ],
  });
  store.start();
  await flush();
  await clock.advance(0);
  assert.equal(store.setView('blue'), true);
  await flush();
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater');
  assert.equal(store.get().view, 'blue');
  assert.equal(store.get().truth, false);
  assert.deepEqual(JSON.parse(storage.map.get(WARGAME_VIEW_KEY)), {
    'WG-1': 'blue',
  });
  assert.equal(store.setView('blue'), false, 'no change, no poll');
  assert.equal(store.setView('red'), false, 'not a view');
  await clock.advance(POLL_MS);
  assert.equal(store.get().wargameSession, 'WG-2');
  assert.equal(store.get().view, 'umpire', 'a new session starts in Umpire');
  await clock.advance(0);
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater&truth=1');
  store.stop();

  // A reload in the same session restores its view from storage.
  const again = setup({ storage, replies: [wgGraph(true, 'WG-1')] });
  again.store.start();
  await flush();
  assert.equal(again.store.get().view, 'blue');
  await again.clock.advance(0);
  assert.deepEqual(again.api.calls, ['/intel/graph?scope=theater']);
  again.store.stop();
});

test('the view flag survives storage failure: it holds in memory and still steers truth', async () => {
  const { store, api, clock } = setup({
    storage: brokenStorage(),
    replies: [wgGraph(true), wgGraph(true), wgGraph(true), wgGraph(true)],
  });
  store.start();
  await flush();
  assert.equal(store.get().view, 'umpire', 'the default, with no storage');
  await clock.advance(0);
  assert.doesNotThrow(() => store.setView('blue'));
  await flush();
  assert.equal(store.get().view, 'blue');
  assert.equal(store.view, 'blue');
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater');
  await clock.advance(POLL_MS);
  assert.equal(store.get().view, 'blue', 'kept across polls');
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater');
  store.stop();
});

test('setView drops an in-flight answer for the old view', async () => {
  let release;
  const stale = wgGraph(true);
  stale.nodes.push(node('frc:red-sam-1', 'force'));
  const { store, api, clock } = setup({
    replies: [
      wgGraph(true),
      () => new Promise((r) => (release = () => r(stale))),
      wgGraph(true),
    ],
  });
  store.start();
  await flush();
  await clock.advance(0);
  store.setView('blue');
  await flush();
  release();
  await flush();
  assert.deepEqual(api.calls, [
    '/intel/graph?scope=theater',
    '/intel/graph?scope=theater&truth=1',
    '/intel/graph?scope=theater',
  ]);
  assert.ok(
    !store.get().byId.has('frc:red-sam-1'),
    'the umpire answer never landed',
  );
  store.stop();
});

test('when the session ends the store returns to the ISR request', async () => {
  const { store, api, clock } = setup({
    replies: [wgGraph(true), wgGraph(true), wgGraph(false), wgGraph(false)],
  });
  store.start();
  await flush();
  await clock.advance(0);
  await clock.advance(POLL_MS);
  assert.equal(store.get().view, null);
  await clock.advance(0);
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater');
  assert.equal(store.get().truth, false);
  store.stop();
});

test('entity() asks for truth only in Umpire view during a session (B17)', async () => {
  const { store, api, clock } = setup({
    replies: [
      wgGraph(true),
      wgGraph(true),
      { id: 'frc:red-sam-1', fields: {} },
      { id: 'frc:red-sam-1', fields: {} },
      wgGraph(true),
      { id: 'frc:blue-artillery-1', fields: {} },
    ],
  });
  store.start();
  await flush();
  await clock.advance(0);
  assert.equal(store.get().view, 'umpire');
  await store.entity('frc:red-sam-1');
  assert.equal(api.calls.at(-1), '/intel/entity/frc%3Ared-sam-1?truth=1');
  assert.equal(store.setView('blue'), true);
  await store.entity('frc:red-sam-1');
  assert.equal(api.calls.at(-1), '/intel/entity/frc%3Ared-sam-1');
  store.stop();
});

// ---- leaving Umpire drops the truth in hand at once (review B, ui) ------------------

/** An Umpire (`truth=1`) graph: red truth, truth-only edges, full red counts. */
function umpireGraph({ reveal = false } = {}) {
  const force = (id, side, label, extra = {}) =>
    node(id, 'force', {
      label,
      attrs: { side, provenance: 'scenario', simulated: true, ...extra },
    });
  return graph(
    [
      node('veh:Drone1', 'vehicle', {
        attrs: { wargame_state: 'lost', wargame_lost_by: 'frc:r1' },
      }),
      node('trk:T-1', 'track', { label: 'Contact T-1' }),
      force('frc:r1', 'red', 'Red SAM 2', { correlated: ['trk:T-1'] }),
      force('frc:b1', 'blue', 'Blue strike 1'),
      node('vec:ax1', 'vector', {
        label: 'Red axis from Red SAM 2',
        attrs: { kind: 'axis', side: 'red' },
      }),
      node('vec:c1', 'vector', {
        attrs: { kind: 'corridor', side: 'blue' },
      }),
      node('eng:e1', 'engagement', {
        attrs: {
          kind: 'red_shot',
          attacker: 'frc:r1',
          attacker_label: 'Red SAM 2',
          p_notional: { kill: 0.4 },
          inputs: ['range'],
          outcome: 'hit',
        },
      }),
      node('eng:e2', 'engagement', {
        attrs: { kind: 'blue_strike', outcome: 'destroyed', bda: { looks: 0 } },
      }),
    ],
    [
      { a: 'frc:r1', b: 'trk:T-1', kind: 'correlates' },
      { a: 'frc:r1', b: 'veh:Drone1', kind: 'threatens' },
      { a: 'vec:ax1', b: 'frc:r1', kind: 'axis' },
      { a: 'eng:e1', b: 'frc:r1', kind: 'launched_by' },
      { a: 'eng:e1', b: 'veh:Drone1', kind: 'attacks' },
    ],
    {
      meta: {
        counts: { track: 1 },
        caveats: [],
        wargame: {
          active: true,
          session_id: 'WG-1',
          reveal_red: reveal,
          truth_view: true,
          counts: { blue: { total: 1 }, red: { total: 1, active: 1, seen: 1 } },
        },
      },
    },
  );
}

test('Blue view drops the truth in hand at once and keeps it dropped when the Blue poll fails', async () => {
  const bus = createBus();
  const order = [];
  bus.on('wargame:view', (p) => order.push(`bus:${p.view}`));
  const { store, api, clock, changes } = setup({
    bus,
    replies: [umpireGraph(), umpireGraph(), new OfflineError()],
  });
  store.on('change', (d) => d.view && order.push('change'));
  store.start();
  await flush();
  await clock.advance(0);
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater&truth=1');
  assert.equal(store.node('frc:r1')?.label, 'Red SAM 2', 'Umpire shows red');
  order.length = 0;

  assert.equal(store.setView('blue'), true);
  assert.deepEqual(order, ['bus:blue', 'change'], 'the orb hides red first');
  const diff = changes.at(-1);
  assert.equal(diff.graph, true);
  assert.ok(diff.removed.includes('frc:r1'));
  await flush();
  assert.equal(api.calls.at(-1), '/intel/graph?scope=theater');

  const st = store.get();
  assert.equal(st.status, 'stale', 'the Blue poll failed');
  assert.equal(st.view, 'blue');
  assert.equal(st.truth, false);
  assert.equal(store.node('frc:r1'), null);
  assert.equal(store.node('vec:ax1'), null);
  assert.deepEqual(rankNodes(st.graph, 'sam'), [], 'search finds no red');
  assert.equal(isTruthView(st.graph, st.truth), false, 'inspector, rail');
  assert.deepEqual(wargameOf(st.graph).counts.red, { seen: 1 });
  assert.equal(store.node('frc:b1').label, 'Blue strike 1');
  assert.equal(store.node('vec:c1').attrs.kind, 'corridor');
  assert.deepEqual(
    st.graph.edges.map((e) => e.kind),
    ['attacks'],
    'no truth-only edge, nothing at a dropped node',
  );
  const e1 = store.node('eng:e1').attrs;
  assert.equal(e1.attacker, null);
  assert.equal(e1.attacker_label, 'Red air defence (not identified)');
  assert.equal(e1.p_notional, null);
  assert.deepEqual(e1.inputs, []);
  const e2 = store.node('eng:e2').attrs;
  assert.equal(e2.outcome, null);
  assert.equal(e2.outcome_hidden, true);
  assert.equal(store.node('veh:Drone1').attrs.wargame_lost_by, null);
  assert.equal(st.graph.meta.counts.track, 1, 'the ISR picture is kept');
  store.stop();
});

test('blueViewOf: a revealed session keeps red but never truth-only data; outside a session it is the graph', () => {
  const g = umpireGraph({ reveal: true });
  const blue = blueViewOf(g);
  assert.ok(
    blue.nodes.some((n) => n.id === 'frc:r1'),
    'revealed red stays',
  );
  const r1 = blue.nodes.find((n) => n.id === 'frc:r1');
  assert.equal(Object.hasOwn(r1.attrs, 'correlated'), false);
  assert.deepEqual(blue.edges.map((e) => e.kind).sort(), [
    'attacks',
    'launched_by',
  ]);
  assert.equal(blue.meta.wargame.truth_view, true);
  assert.deepEqual(blue.meta.wargame.counts, g.meta.wargame.counts);
  assert.ok(g.nodes.find((n) => n.id === 'frc:r1').attrs.correlated, 'pure');
  const isr = wgGraph(false);
  assert.equal(blueViewOf(isr), isr);
  assert.equal(blueViewOf(null), null);
});

test('Umpire from Blue keeps the Blue graph until the truth graph lands', async () => {
  const blue = wgGraph(true, 'WG-1');
  const { store, clock, changes } = setup({
    storage: fakeStorage(),
    replies: [blue, umpireGraph(), blue],
  });
  store.start();
  await flush();
  await clock.advance(0);
  store.setView('blue');
  await flush();
  const before = store.get().graph;
  const n = changes.length;
  store.setView('umpire');
  assert.equal(store.get().graph, before, 'nothing to drop');
  assert.equal(changes.length, n + 1);
  assert.equal(changes.at(-1).graph, false);
  store.stop();
});
