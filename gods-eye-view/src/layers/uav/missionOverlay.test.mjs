import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import { createUavLayer, missionSignature } from './index.js';
import { mutableSource, stubViewer, uavRecord, value } from './testSupport.mjs';

const T0 = 1789620000000;

/** One feature of every kind BRIDGE_CONTRACT lists for /mission-overlay. */
function overlayPayload() {
  return {
    type: 'FeatureCollection',
    features: [
      {
        id: 'route-1',
        geometry: {
          type: 'LineString',
          coordinates: [
            [51.72, 33.72],
            [51.73, 33.73],
          ],
        },
        properties: { kind: 'route' },
      },
      {
        id: 'flown-1',
        geometry: {
          type: 'LineString',
          coordinates: [
            [51.72, 33.72, 1500],
            [51.725, 33.725, 1520],
          ],
        },
        properties: { kind: 'flown' },
      },
      {
        id: 'wp-6',
        geometry: { type: 'Point', coordinates: [51.73, 33.73, 1500] },
        properties: { kind: 'waypoint', index: 6, reached: true },
      },
      {
        id: 'grid-1',
        geometry: {
          type: 'LineString',
          coordinates: [
            [51.7, 33.7],
            [51.75, 33.7],
          ],
        },
        properties: { kind: 'grid' },
      },
      {
        id: 'coverage-1',
        geometry: {
          type: 'Polygon',
          coordinates: [
            [
              [51.7, 33.7],
              [51.75, 33.7],
              [51.75, 33.75],
              [51.7, 33.7],
            ],
          ],
        },
        properties: { kind: 'coverage' },
      },
      {
        id: 'ao',
        geometry: {
          type: 'Polygon',
          coordinates: [
            [
              [51.6, 33.6],
              [51.8, 33.6],
              [51.8, 33.8],
              [51.6, 33.6],
            ],
          ],
        },
        properties: { kind: 'geofence' },
      },
      {
        id: 'ring-engage',
        geometry: {
          type: 'Polygon',
          coordinates: [
            [
              [51.71, 33.71],
              [51.72, 33.71],
              [51.72, 33.72],
              [51.71, 33.71],
            ],
          ],
        },
        properties: {
          kind: 'threat_ring',
          ring: 'engagement',
          track_id: 'TRK-0003',
          radius_m: 4000,
        },
      },
      {
        id: 'target-3',
        geometry: { type: 'Point', coordinates: [51.7238, 33.7241, 1548] },
        properties: { kind: 'target', track_id: 'TRK-0003' },
      },
    ],
  };
}

function missionRow(overrides = {}) {
  return {
    missionId: 'msn-0007',
    vehicle: 'Drone1',
    phase: 'executing',
    activeTool: 'uav_fly_route',
    progressPct: 43.5,
    waypoint: { index: 6, of: 14 },
    ...overrides,
  };
}

/** Layer whose source also serves the overlay, counting overlay fetches. */
async function harness({ missions = [missionRow()] } = {}) {
  const served = { calls: 0, payload: overlayPayload() };
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    missions,
  });
  source.getMissionOverlay = async () => {
    served.calls += 1;
    return served.payload;
  };
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0 });
  layer.init(viewer);
  await layer.update(viewer);
  return { layer, viewer, source, served };
}

test('mission overlay features land in their own data source', async () => {
  const { layer, viewer } = await harness();

  const overlay = layer._overlayCollection;
  assert.ok(overlay, 'a second data source exists');
  assert.equal(overlay.name, 'uav-mission-overlay');
  assert.equal(overlay.entities.values.length, 8, 'every feature rendered');
  assert.equal(
    viewer.dataSources.length,
    2,
    'entities and overlay, separately',
  );

  // T6: nothing from the overlay may ride the entity snapshot.
  const snapshotIds = layer._collection.entities.values.map(
    (entity) => entity.id,
  );
  assert.equal(
    snapshotIds.some((id) => id.startsWith('uav-overlay:')),
    false,
    'no overlay feature reached the entity collection',
  );
  assert.deepEqual(snapshotIds, ['uav:Drone1', 'uav-trail:Drone1']);
  assert.equal(
    overlay.entities.values.every((entity) =>
      entity.id.startsWith('uav-overlay:'),
    ),
    true,
  );

  layer.destroy();
  assert.equal(viewer.dataSources.length, 0, 'both data sources are released');
});

test('each contract kind is styled rather than dropped', async () => {
  const { layer } = await harness();
  const overlay = layer._overlayCollection;
  const byId = (id) => overlay.entities.getById(`uav-overlay:${id}`);

  // A planned route is dashed, a flown track is solid, so plan reads against
  // reality without a legend.
  assert.ok(
    byId('route-1').polyline.material instanceof
      Cesium.PolylineDashMaterialProperty,
  );
  assert.ok(
    byId('flown-1').polyline.material instanceof Cesium.ColorMaterialProperty,
  );
  // A route without heights is draped; a flown track with heights is not.
  assert.equal(value(byId('route-1').polyline.clampToGround), true);
  assert.equal(value(byId('flown-1').polyline.clampToGround), false);

  const waypoint = byId('wp-6');
  assert.equal(value(waypoint.label.text), 'W6');
  assert.ok(value(waypoint.point.color).alpha < 1, 'a reached waypoint dims');

  // The AO boundary outlines and never fills; imaged ground fills.
  assert.equal(value(byId('ao').polygon.fill), false);
  assert.equal(value(byId('coverage-1').polygon.fill), true);
  assert.ok(value(byId('coverage-1').polygon.material.color).alpha < 0.5);

  // An engagement ring reads hotter than an acquisition ring.
  const engagement = value(byId('ring-engage').polygon.outlineColor);
  assert.equal(
    engagement.toCssHexString().toLowerCase().startsWith('#ff3b30'),
    true,
  );

  assert.equal(value(byId('target-3').label.text), 'TRK-0003');
  layer.destroy();
});

test('the overlay refreshes on mission-state change, not on every tick', async () => {
  const fixture = await harness();
  assert.equal(fixture.served.calls, 1, 'fetched once for the first snapshot');

  await fixture.layer.update(fixture.viewer);
  await fixture.layer.update(fixture.viewer);
  assert.equal(
    fixture.served.calls,
    1,
    'an unchanged mission does not refetch',
  );

  fixture.source.snapshot = {
    ...fixture.source.snapshot,
    missions: [missionRow({ phase: 'rtb', waypoint: { index: 9, of: 14 } })],
  };
  await fixture.layer.update(fixture.viewer);
  assert.equal(fixture.served.calls, 2, 'a phase change refetches');
  fixture.layer.destroy();
});

test('mission signature falls back to the vehicles when missions[] is absent', () => {
  const withMissions = missionSignature({ missions: [missionRow()] });
  assert.match(withMissions, /msn-0007:executing:uav_fly_route:6:14:44/);

  const base = {
    records: [
      { reference: 'Drone1', status: { mission: 'msn-1', trackId: '' } },
    ],
  };
  const before = missionSignature(base);
  const after = missionSignature({
    records: [
      { reference: 'Drone1', status: { mission: 'msn-2', trackId: '' } },
    ],
  });
  assert.notEqual(before, after, 'a vehicle mission swap still refreshes');
});

test('the overlay is fetched over HTTP when the source does not serve it', async () => {
  const requests = [];
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    missions: [missionRow()],
  });
  const viewer = stubViewer();
  const layer = createUavLayer({
    source,
    now: () => T0,
    missionOverlay: {
      baseUrl: 'http://localhost:8790',
      token: 'dev-token',
      fetchImpl: async (url, init) => {
        requests.push({ url, init });
        return { ok: true, json: async () => overlayPayload() };
      },
    },
  });
  layer.init(viewer);
  await layer.update(viewer);

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, 'http://localhost:8790/mission-overlay');
  assert.equal(
    requests[0].init.headers.Authorization,
    'Bearer dev-token',
    'the bridge requires a bearer token',
  );
  assert.equal(layer._overlayCollection.entities.values.length, 8);
  layer.destroy();
});

test('an overlay failure degrades to status and never blanks the vehicles', async () => {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
  });
  source.getMissionOverlay = async () => {
    throw new Error('mission overlay HTTP 404');
  };
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0 });
  layer.init(viewer);
  const result = await layer.update(viewer);

  assert.equal(result.status, 'ok', 'the vehicle feed is unaffected');
  assert.ok(layer._collection.entities.getById('uav:Drone1'));
  const stats = layer.getStats();
  assert.equal(stats.missionOverlay.status, 'error');
  assert.match(stats.missionOverlay.lastError, /404/);
  assert.equal(stats.lastError, null, 'the layer itself is healthy');
  layer.destroy();
});

test('a bridge without /mission-overlay is retried on a backoff, not every poll', async () => {
  // The route 404s today. A failed attempt does not advance the mission
  // signature, so without a backoff clock the layer would put one failed
  // request per 200 ms poll on the wire and in the operator's console.
  let now = T0;
  let attempts = 0;
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    missions: [missionRow()],
  });
  const viewer = stubViewer();
  const layer = createUavLayer({
    source,
    now: () => now,
    missionOverlay: {
      baseUrl: 'http://localhost:8790',
      fetchImpl: async () => {
        attempts += 1;
        return { ok: false, status: 404, json: async () => ({}) };
      },
    },
  });
  layer.init(viewer);

  // Twenty-five polls — four seconds of a 5 Hz feed.
  for (let i = 0; i < 25; i += 1) {
    now = T0 + i * 200;
    await layer.update(viewer);
  }
  assert.equal(attempts, 1, 'one attempt, then silence');
  assert.equal(layer.getStats().missionOverlay.status, 'error');

  // Past the backoff the layer tries again, so the overlay appears by itself
  // once the bridge ships the route.
  now = T0 + 5200;
  await layer.update(viewer);
  assert.equal(attempts, 2, 'retried after the backoff window');
  layer.destroy();
});

test('no overlay transport at all reports unsupported rather than failing', async () => {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
  });
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0 });
  layer.init(viewer);
  await layer.update(viewer);
  assert.equal(layer.getStats().missionOverlay.status, 'unsupported');
  assert.equal(layer._overlayCollection, null, 'no empty data source is added');
  assert.equal(viewer.dataSources.length, 1);
  layer.destroy();
});

test('a malformed feature costs only itself', async () => {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
  });
  source.getMissionOverlay = async () => ({
    type: 'FeatureCollection',
    features: [
      {
        geometry: { type: 'LineString', coordinates: [[1, 2]] },
        properties: {},
      },
      { geometry: null, properties: { kind: 'route' } },
      {
        id: 'ok',
        geometry: { type: 'Point', coordinates: [51.7, 33.7] },
        properties: { kind: 'target', track_id: 'TRK-9' },
      },
    ],
  });
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0 });
  layer.init(viewer);
  await layer.update(viewer);
  assert.equal(layer._overlayCollection.entities.values.length, 1);
  assert.ok(layer._overlayCollection.entities.getById('uav-overlay:ok'));
  layer.destroy();
});
