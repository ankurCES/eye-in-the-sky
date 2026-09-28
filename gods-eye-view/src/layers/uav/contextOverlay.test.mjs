import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import { createUavLayer } from './index.js';
import {
  fakeClickHandler,
  mutableSource,
  stubViewer,
  uavRecord,
} from './testSupport.mjs';
import { CONTEXT_PREFIX, UNRECOGNISED_MAP_ITEM } from './contextPolicy.js';

const T0 = 1789620000000;
const SITE_ID = 'sit:default:way/101';
const SITE_ENTITY = `${CONTEXT_PREFIX}${SITE_ID}`;

function site(id, extra = {}) {
  return {
    type: 'Feature',
    id,
    geometry: { type: 'Point', coordinates: [-122.13, 47.64] },
    properties: {
      kind: 'site',
      id,
      label: 'Redmond airstrip',
      category: 'airfield',
      protected: false,
      register: 'mapped',
      salience: 0.5,
      labelled: true,
      simulated: false,
      truth: false,
      ...extra,
    },
  };
}

/** A §3.3 body: one site, one kind this build does not know. */
function body(rev = '0:17:0:0', features = null, extra = {}) {
  return {
    type: 'FeatureCollection',
    rev,
    theater: { id: 'default', epoch: 0 },
    attribution: ['© OpenStreetMap contributors, ODbL'],
    counts: { site: 1 },
    omitted: {},
    features: features ?? [
      site(SITE_ID),
      {
        type: 'Feature',
        id: 'frc:red-1',
        geometry: { type: 'Point', coordinates: [-122.12, 47.65] },
        properties: { kind: 'force', id: 'frc:red-1', label: 'Red SAM 1' },
      },
    ],
    sites: {
      total: 41,
      served: 1,
      degraded: false,
      reason: null,
      fetched_at_ms: 17,
    },
    ...extra,
  };
}

/**
 * The in-app host: `/mission-overlay` answers an empty collection, and
 * `/intel/overlay` answers the queued replies (the last one repeats).
 */
function fakeHost(replies) {
  const calls = { overlay: [], mission: [] };
  const queue = [...replies];
  async function fetchImpl(url, init) {
    if (url.includes('/mission-overlay')) {
      calls.mission.push({ url, init });
      return {
        ok: true,
        status: 200,
        json: async () => ({ type: 'FeatureCollection', features: [] }),
      };
    }
    calls.overlay.push({ url, init });
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next instanceof Error) throw next;
    if (typeof next === 'number') {
      return { ok: false, status: next, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => structuredClone(next) };
  }
  return { calls, fetchImpl, queue };
}

const flush = async () => {
  for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r));
};

async function harness(replies = [body()], options = {}) {
  const clock = { nowMs: T0 };
  const source = mutableSource({
    observedAtMs: T0,
    theater: { id: 'default', epoch: 0 },
    records: [uavRecord({ observedAtMs: T0 })],
  });
  const host = fakeHost(replies);
  const handler = fakeClickHandler();
  const layer = createUavLayer({
    source,
    pollMs: 200,
    now: () => clock.nowMs,
    // Only the mission overlay's origin: the context overlay shares it.
    missionOverlay: {
      baseUrl: 'http://host',
      token: 'tok',
      fetchImpl: host.fetchImpl,
    },
    contextIcons: { iconFor: () => null, clear() {} },
    createContextClickHandler: () => handler,
    ...options,
  });
  const viewer = stubViewer();
  layer.init(viewer);
  await layer.update(viewer);
  return { clock, source, host, handler, layer, viewer };
}

async function tickAt(h, atMs, snapshot = {}) {
  h.clock.nowMs = atMs;
  h.source.snapshot = {
    ...h.source.snapshot,
    observedAtMs: atMs,
    records: [uavRecord({ observedAtMs: atMs })],
    ...snapshot,
  };
  const result = await h.layer.update(h.viewer);
  await flush();
  return result;
}

function contextSource(viewer) {
  for (let i = 0; i < viewer.dataSources.length; i += 1) {
    const ds = viewer.dataSources.get(i);
    if (ds.name === 'uav-context-overlay') return ds;
  }
  return null;
}

test('inactive, nothing is fetched; active, it fetches at once into its own source', async () => {
  const h = await harness();
  await tickAt(h, T0 + 3000);
  assert.equal(h.host.calls.overlay.length, 0, 'no poll while inactive');

  assert.equal(h.layer.setContextActive(true), true);
  await flush();
  assert.equal(h.host.calls.overlay.length, 1);
  const [call] = h.host.calls.overlay;
  assert.equal(call.url, 'http://host/intel/overlay?truth=0');
  assert.equal(call.init.headers.Authorization, 'Bearer tok');

  const ds = contextSource(h.viewer);
  assert.ok(ds, 'the uav-context-overlay data source exists');
  assert.notEqual(ds, h.layer._collection);
  assert.notEqual(ds, h.layer._overlayCollection);
  assert.equal(h.layer._contextCollection, ds);
  assert.equal(ds.show, true);
  assert.equal(ds.entities.values.length, 2);
  assert.ok(ds.entities.getById(SITE_ENTITY));
  const unknown = ds.entities.getById(`${CONTEXT_PREFIX}frc:red-1`);
  assert.equal(unknown.label.text.getValue(), UNRECOGNISED_MAP_ITEM);
  assert.ok(unknown.point, 'an unknown kind is a point');
  h.layer.destroy();
});

test('it polls every 3 s with the last rev; unchanged keeps the picture', async () => {
  const h = await harness([
    body('0:17:0:0'),
    { rev: '0:17:0:0', unchanged: true },
    body('0:18:0:0', [site('sit:default:node/7', { label: 'Fuel depot' })]),
  ]);
  h.layer.setContextActive(true);
  await flush();
  const first = contextSource(h.viewer).entities.getById(SITE_ENTITY);

  await tickAt(h, T0 + 1000);
  assert.equal(h.host.calls.overlay.length, 1, 'not due yet');
  await tickAt(h, T0 + 3000);
  assert.equal(h.host.calls.overlay.length, 2);
  assert.match(h.host.calls.overlay[1].url, /rev=0%3A17%3A0%3A0$/);
  assert.equal(
    contextSource(h.viewer).entities.getById(SITE_ENTITY),
    first,
    'an unchanged answer keeps the drawn entities',
  );

  await tickAt(h, T0 + 6000);
  assert.equal(h.host.calls.overlay.length, 3);
  const ds = contextSource(h.viewer);
  assert.equal(ds.entities.getById(SITE_ENTITY), undefined, 'rebuilt');
  assert.ok(ds.entities.getById(`${CONTEXT_PREFIX}sit:default:node/7`));
  assert.equal(h.layer.getContextStatus().rev, '0:18:0:0');
  h.layer.destroy();
});

test('a failed fetch backs off 5 s, then asks again', async () => {
  const h = await harness([new Error('host down'), 503, body()]);
  h.layer.setContextActive(true);
  await flush();
  assert.equal(h.layer.getContextStatus().status, 'error');
  assert.equal(h.layer.getContextStatus().lastError, 'host down');
  await tickAt(h, T0 + 3000);
  assert.equal(h.host.calls.overlay.length, 1, 'held off by the backoff');
  await tickAt(h, T0 + 5000);
  assert.equal(h.host.calls.overlay.length, 2);
  assert.match(h.layer.getContextStatus().lastError, /HTTP 503/);
  await tickAt(h, T0 + 10000);
  assert.equal(h.layer.getContextStatus().status, 'ok');
  h.layer.destroy();
});

test('without a host origin the overlay is unsupported and never fetches', async () => {
  const h = await harness([body()], { missionOverlay: {} });
  h.layer.setContextActive(true);
  await flush();
  assert.equal(h.layer.getContextStatus().status, 'unsupported');
  assert.equal(h.host.calls.overlay.length, 0);
  h.layer.destroy();
});

test('per-kind visibility hides sites; an unrecognised item still shows', async () => {
  const h = await harness();
  h.layer.setContextActive(true);
  await flush();
  const ds = contextSource(h.viewer);
  const out = h.layer.setContextVisibility({ sites: false });
  assert.equal(out.sites, false);
  assert.equal(ds.entities.getById(SITE_ENTITY).show, false);
  assert.equal(ds.entities.getById(`${CONTEXT_PREFIX}frc:red-1`).show, true);
  h.layer.setContextVisibility({ sites: true });
  assert.equal(ds.entities.getById(SITE_ENTITY).show, true);
  h.layer.destroy();
});

test('a click reports a site id or veh:{name} while active, nothing after', async () => {
  const h = await harness();
  const picks = [];
  const off = h.layer.onContextPick((pick) => picks.push(pick));
  h.layer.setContextActive(true);
  await flush();
  const ds = contextSource(h.viewer);
  const vehicle = h.layer._collection.entities.getById('uav:Drone1');
  let under = null;
  h.viewer.scene.pick = () => (under ? { id: under } : undefined);

  under = ds.entities.getById(SITE_ENTITY);
  h.handler.click(new Cesium.Cartesian2(10, 10));
  under = vehicle;
  h.handler.click(new Cesium.Cartesian2(20, 20));
  under = null;
  h.handler.click(new Cesium.Cartesian2(30, 30));
  assert.deepEqual(picks, [{ id: SITE_ID }, { id: 'veh:Drone1' }]);

  // Context entities are this layer's picks: clicking one is not empty space
  // for the tracking handler (a tracked drone is kept).
  assert.ok(h.layer.testing.state.ownedIds.has(SITE_ENTITY));

  h.layer.setContextActive(false);
  assert.equal(h.handler.destroyed, true, 'the click handler goes with it');
  assert.equal(ds.show, false, 'hidden when inactive');
  off();
  h.layer.destroy();
});

test('inactive, the overlay stops polling', async () => {
  const h = await harness();
  h.layer.setContextActive(true);
  await flush();
  h.layer.setContextActive(false);
  await tickAt(h, T0 + 9000);
  assert.equal(h.host.calls.overlay.length, 1);
  h.layer.destroy();
});

test('a theater change clears trails and interpolation and refetches both overlays', async () => {
  const h = await harness([body('0:17:0:0'), body('1:99:0:0')]);
  h.layer.setContextActive(true);
  await flush();
  await tickAt(h, T0 + 200);
  const { state, motion } = h.layer.testing;
  const missionBefore = h.host.calls.mission.length;
  assert.ok(state.trails.get('Drone1').length >= 1);

  // The switch: epoch 1, the fleet re-parked 13,000 km away.
  await tickAt(h, T0 + 400, {
    theater: { id: 'dyn-kherson', epoch: 1 },
    records: [
      uavRecord({
        observedAtMs: T0 + 400,
        position: { latitude: 46.64, longitude: 32.6 },
      }),
    ],
  });
  assert.equal(state.trails.get('Drone1').length, 1, 'a fresh trail');
  assert.equal(motion.sampleCount('Drone1'), 1, 'fresh interpolation');
  assert.equal(
    h.host.calls.mission.length,
    missionBefore + 1,
    'the mission overlay was force-refetched',
  );
  const last = h.host.calls.overlay.at(-1);
  assert.doesNotMatch(last.url, /rev=/, 'the context refetch asks in full');
  assert.equal(h.layer.getContextStatus().rev, '1:99:0:0');
  assert.deepEqual(h.layer.getTheater(), { id: 'dyn-kherson', epoch: 1 });
  h.layer.destroy();
});

test('one switch resets once, even when its epoch arrives a poll late', async () => {
  const h = await harness();
  const changes = [];
  h.layer.onTheaterChange((change) => changes.push(change));
  await tickAt(h, T0 + 200, { theater: { id: null, epoch: null } });
  await tickAt(h, T0 + 400, { theater: { id: 'dyn-k', epoch: null } });
  await tickAt(h, T0 + 600, { theater: { id: 'dyn-k', epoch: 1 } });
  await tickAt(h, T0 + 800, { theater: { id: 'dyn-k', epoch: 1 } });
  assert.equal(changes.length, 1);
  assert.deepEqual(changes[0].from, { id: 'default', epoch: 0 });
  // No theater block at all (an older bridge): nothing happens.
  await tickAt(h, T0 + 1000, { theater: undefined });
  assert.equal(changes.length, 1);
  h.layer.destroy();
});

test('status carries what the dock needs: drawn, not drawn, degraded', async () => {
  const h = await harness([
    body('0:17:0:0', null, {
      sites: {
        total: 41,
        served: 1,
        degraded: true,
        reason: 'overpass timeout',
        fetched_at_ms: 17,
      },
    }),
  ]);
  h.layer.setContextActive(true);
  await flush();
  const status = h.layer.getStats().contextOverlay;
  assert.equal(status.active, true);
  assert.equal(status.sites.total, 41);
  assert.equal(status.sites.drawn, 1);
  assert.equal(status.sites.notDrawn, 40);
  assert.equal(status.sites.degraded, true);
  assert.equal(status.sites.reason, 'overpass timeout');
  assert.deepEqual(status.attribution, ['© OpenStreetMap contributors, ODbL']);
  assert.deepEqual(status.unknown, { served: 1, drawn: 1 });
  assert.deepEqual(status.theater, { id: 'default', epoch: 0 });
  h.layer.destroy();
});

test('disable hides the context source; destroy removes it and its ids', async () => {
  const h = await harness();
  h.layer.setContextActive(true);
  await flush();
  const ds = contextSource(h.viewer);
  await h.layer.disable();
  assert.equal(ds.show, false);
  await h.layer.enable(h.viewer);
  assert.equal(ds.show, true);
  h.layer.destroy();
  assert.equal(contextSource(h.viewer), null);
  assert.equal(h.layer.testing.state.ownedIds.has(SITE_ENTITY), false);
  assert.equal(h.layer.getContextStatus().active, false);
});

test('an epoch change with no jump (airframe-only switch) still resets trails', async () => {
  const h = await harness();
  await tickAt(h, T0 + 200, {
    records: [
      uavRecord({
        observedAtMs: T0 + 200,
        position: { longitude: -122.1385 },
      }),
    ],
  });
  const { state, motion } = h.layer.testing;
  assert.equal(state.trails.get('Drone1').length, 2);
  await tickAt(h, T0 + 400, {
    theater: { id: 'default', epoch: 1 },
    records: [
      uavRecord({
        observedAtMs: T0 + 400,
        position: { longitude: -122.1385 },
      }),
    ],
  });
  assert.equal(state.trails.get('Drone1').length, 1);
  assert.equal(motion.sampleCount('Drone1'), 1);
  h.layer.destroy();
});

test('with an icon canvas a site is a billboard; at most 150 are drawn', async () => {
  const canvas = { width: 64, height: 64, tag: 'canvas' };
  const many = Array.from({ length: 180 }, (_, i) =>
    site(`sit:default:node/${i}`, { labelled: i < 40 }),
  );
  const h = await harness([body('0:17:0:0', many)], {
    contextIcons: { iconFor: () => canvas, clear() {} },
  });
  h.layer.setContextActive(true);
  await flush();
  const ds = contextSource(h.viewer);
  assert.equal(ds.entities.values.length, 150);
  const first = ds.entities.getById(`${CONTEXT_PREFIX}sit:default:node/0`);
  assert.equal(first.billboard.image.getValue(), canvas);
  assert.equal(first.label.text.getValue(), 'Redmond airstrip');
  const quiet = ds.entities.getById(`${CONTEXT_PREFIX}sit:default:node/40`);
  assert.equal(quiet.label, undefined, 'only the top 40 are labelled');
  assert.equal(
    ds.entities.getById(`${CONTEXT_PREFIX}sit:default:node/150`),
    undefined,
  );
  const status = h.layer.getContextStatus();
  assert.equal(status.sites.drawn, 150);
  assert.equal(status.sites.capped, 30);
  h.layer.destroy();
});
