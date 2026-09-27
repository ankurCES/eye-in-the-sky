import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AuthError, HttpError, OfflineError } from './api.js';
import { createBus } from './bus.js';
import {
  BACKOFF_MS,
  POLL_MS,
  alarmFingerprint,
  alarmFromPayload,
  createIntelStore,
  diffNodes,
} from './intelStore.js';

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
