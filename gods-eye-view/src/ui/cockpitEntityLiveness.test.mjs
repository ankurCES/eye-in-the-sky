import test from 'node:test';
import assert from 'node:assert/strict';

import { viewerHasEntity } from './cockpitTrackingController.js';

/** Minimal Cesium-shaped fakes: an EntityCollection knows its owner. */
function collection(owner = null) {
  const items = new Set();
  return {
    owner,
    add(e) {
      items.add(e);
      e.entityCollection = this;
      return e;
    },
    remove(e) {
      items.delete(e);
    },
    contains: (e) => items.has(e),
  };
}

function viewer() {
  const sources = new Set();
  return {
    entities: collection(),
    dataSources: {
      add: (ds) => sources.add(ds),
      remove: (ds) => sources.delete(ds),
      contains: (ds) => sources.has(ds),
    },
  };
}

test('an entity in the viewer collection is live', () => {
  const v = viewer();
  const e = v.entities.add({});
  assert.equal(viewerHasEntity(v, e), true);
});

test('a drone in a CustomDataSource the viewer holds is live', () => {
  // The UAV layer keeps its entities in its own data source. Checking only
  // viewer.entities made the cockpit exit 13 ms after entering.
  const v = viewer();
  const ds = { name: 'uav' };
  ds.entities = collection(ds);
  v.dataSources.add(ds);
  const drone = ds.entities.add({});
  assert.equal(viewerHasEntity(v, drone), true);
});

test('removed from its source, or its source removed, is not live', () => {
  const v = viewer();
  const ds = { name: 'uav' };
  ds.entities = collection(ds);
  v.dataSources.add(ds);
  const drone = ds.entities.add({});
  ds.entities.remove(drone);
  assert.equal(viewerHasEntity(v, drone), false);
  const other = ds.entities.add({});
  v.dataSources.remove(ds);
  assert.equal(viewerHasEntity(v, other), false);
});

test('missing viewer, entity or dataSources never throws', () => {
  assert.equal(viewerHasEntity(null, {}), false);
  assert.equal(viewerHasEntity(viewer(), null), false);
  assert.equal(
    viewerHasEntity({ entities: { contains: () => false } }, {}),
    false,
  );
});
