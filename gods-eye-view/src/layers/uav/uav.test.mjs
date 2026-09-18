import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import { createUavLayer } from './index.js';

function makeSource(records) {
  return {
    label: 'godSeye UAV (AirSim)',
    async getSnapshot() {
      return {
        source: 'uav',
        status: 'ok',
        simState: 'up',
        observedAtMs: 1789620000000,
        records,
      };
    },
  };
}

const REC = {
  id: 'Drone1',
  reference: 'Drone1',
  label: 'Drone1',
  kind: 'uav',
  position: {
    latitude: 47.6419,
    longitude: -122.1395,
    ellipsoidAltitude: 93.2,
    agl: 28,
  },
  velocity: { speed: 12, heading: 61 },
  status: { fuelPct: 87, armed: true, landedState: 2 },
  observedAtMs: 1789620000000,
};

/** Stub viewer with a real CustomDataSource-backed entities collection. */
function stubViewer() {
  const dataSources = new Cesium.DataSourceCollection();
  return {
    dataSources,
    scene: { requestRender: () => {} },
    trackedEntity: undefined,
  };
}

test('uav layer requires a snapshot source', () => {
  assert.throws(() => createUavLayer({}), /snapshot source is required/);
  assert.throws(
    () => createUavLayer({ source: {} }),
    /snapshot source is required/,
  );
});

test('init adds a data source and update renders one entity per record', async () => {
  const layer = createUavLayer({ source: makeSource([REC]) });
  const viewer = stubViewer();
  layer.init(viewer);
  // Cesium DataSourceCollection.add() resolves asynchronously.
  await Promise.resolve();
  assert.equal(viewer.dataSources.length, 1);

  const result = await layer.update(viewer);
  assert.equal(result.status, 'ok');
  assert.equal(result.ids.has('Drone1'), true);
  assert.equal(result.simState, 'up');

  const collection = layer._collection;
  const entity = collection.entities.getById('uav:Drone1');
  assert.ok(entity, 'drone entity exists');
  const labelText = entity.label.text?.getValue
    ? entity.label.text.getValue()
    : entity.label.text;
  assert.equal(typeof labelText, 'string');
  assert.match(labelText, /Drone1/);
  assert.match(labelText, /87%/); // fuel HUD
  assert.match(labelText, /12m\/s/); // speed HUD

  // trail polyline entity also present
  assert.ok(collection.entities.getById('uav-trail:Drone1'));

  const stats = layer.getStats();
  assert.equal(stats.vehicles, 1);
  assert.equal(stats.observedAtMs, 1789620000000);

  layer.destroy();
  assert.equal(viewer.dataSources.length, 0);
});

test('update without init returns not-initialized', async () => {
  const layer = createUavLayer({ source: makeSource([REC]) });
  const result = await layer.update(undefined);
  assert.equal(result.status, 'not-initialized');
});

test('track/ untrack sets the viewer tracked entity', async () => {
  const layer = createUavLayer({ source: makeSource([REC]) });
  const viewer = stubViewer();
  layer.init(viewer);
  await layer.update(viewer);
  layer.track('Drone1');
  assert.ok(viewer.trackedEntity, 'tracked entity set');
  layer.untrack();
  assert.equal(viewer.trackedEntity, undefined);
  layer.destroy();
});

test('update degrades gracefully when the bridge is down', async () => {
  const down = {
    label: 'uav',
    async getSnapshot() {
      throw new Error('UAV bridge unreachable');
    },
  };
  const layer = createUavLayer({ source: down });
  const viewer = stubViewer();
  layer.init(viewer);
  const result = await layer.update(viewer);
  assert.equal(result.status, 'error');
  assert.match(result.message, /unreachable/);
  layer.destroy();
});
