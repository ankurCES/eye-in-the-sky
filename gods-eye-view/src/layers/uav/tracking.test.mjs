import test from 'node:test';
import assert from 'node:assert/strict';

import {
  isOwnedByOtherLayer,
  registerPickOwner,
  resolvePickId,
  unregisterPickOwner,
} from '../../data/pickRegistry.js';
import { createUavLayer } from './index.js';
import {
  fakeClickHandler,
  mutableSource,
  stubViewer,
  uavRecord,
} from './testSupport.mjs';

const T0 = 1789620000000;
const PICK_AT = { x: 120, y: 240 };

const picking = {
  registerPickOwner,
  unregisterPickOwner,
  resolvePickId,
  isOwnedByOtherLayer,
};

/** A layer wired to the real pick registry and a replayable input handler. */
async function harness({ contextSpy = null } = {}) {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    missions: [
      {
        missionId: 'msn-0007',
        vehicle: 'Drone1',
        phase: 'executing',
        progressPct: 43.5,
      },
    ],
  });
  const handler = fakeClickHandler();
  const viewer = stubViewer();
  const layer = createUavLayer({
    source,
    now: () => T0,
    services: {
      picking,
      context: contextSpy || {},
    },
    createClickHandler: () => handler,
  });
  layer.init(viewer);
  await layer.update(viewer);
  return { layer, viewer, handler, source };
}

test('the layer registers a uav pick owner while it is initialized', async () => {
  const { layer } = await harness();
  // A sibling layer asking about a UAV pick must be told it belongs to us,
  // otherwise it clears the operator's track as if empty space were clicked.
  assert.equal(isOwnedByOtherLayer('military', 'uav:Drone1'), true);
  assert.equal(isOwnedByOtherLayer('military', 'uav-trail:Drone1'), true);
  assert.equal(isOwnedByOtherLayer('military', 'icao:abc123'), false);
  layer.destroy();
  assert.equal(
    isOwnedByOtherLayer('military', 'uav:Drone1'),
    false,
    'ownership is released on destroy',
  );
});

test('clicking a drone tracks that drone', async () => {
  const { layer, viewer, handler } = await harness();
  const entity = layer._collection.entities.getById('uav:Drone1');
  viewer.scene.pick = () => ({ id: entity });

  assert.equal(
    layer.getTrackedInfo(),
    null,
    'nothing tracked before the click',
  );
  handler.click(PICK_AT);

  assert.equal(
    viewer.trackedEntity,
    entity,
    'camera follows the clicked drone',
  );
  const info = layer.getTrackedInfo();
  assert.equal(info.layerId, 'uav');
  assert.equal(info.icao24, 'Drone1');
  assert.equal(info.callsign, 'Drone1');
  assert.equal(info.fuelPct, 87);
  layer.destroy();
});

test('clicking a drone selects it through a billboard-style primitive pick', async () => {
  const { layer, viewer, handler } = await harness();
  // Some Cesium versions surface the id on the primitive rather than the pick.
  viewer.scene.pick = () => ({ primitive: { id: 'uav:Drone1' } });
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo()?.icao24, 'Drone1');
  layer.destroy();
});

test('clicking empty space releases the tracked drone', async () => {
  const { layer, viewer, handler } = await harness();
  const entity = layer._collection.entities.getById('uav:Drone1');
  viewer.scene.pick = () => ({ id: entity });
  handler.click(PICK_AT);
  assert.ok(layer.getTrackedInfo());

  viewer.scene.pick = () => undefined;
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo(), null);
  assert.equal(viewer.trackedEntity, undefined);
  layer.destroy();
});

test("a sibling layer's contact never clears the uav track", async () => {
  const { layer, viewer, handler } = await harness();
  const entity = layer._collection.entities.getById('uav:Drone1');
  viewer.scene.pick = () => ({ id: entity });
  handler.click(PICK_AT);
  assert.ok(layer.getTrackedInfo());

  registerPickOwner('military', (id) => id === 'abc123');
  viewer.scene.pick = () => ({ id: 'abc123' });
  handler.click(PICK_AT);
  assert.equal(
    layer.getTrackedInfo()?.icao24,
    'Drone1',
    'the military pick is not empty space',
  );
  unregisterPickOwner('military');
  layer.destroy();
});

test("this layer's own contact marker never clears its own track", async () => {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    contacts: [
      {
        trackId: 'TRK-0003',
        position: { latitude: 33.7, longitude: 51.7, altitude: 10 },
        threatLevel: 'high',
        confidence: 'confirmed',
      },
    ],
  });
  source.getMissionOverlay = async () => ({
    type: 'FeatureCollection',
    features: [
      {
        id: 'wp-6',
        geometry: { type: 'Point', coordinates: [51.73, 33.73, 1500] },
        properties: { kind: 'waypoint', index: 6 },
      },
    ],
  });
  const handler = fakeClickHandler();
  const viewer = stubViewer();
  const layer = createUavLayer({
    source,
    now: () => T0,
    services: { picking },
    createClickHandler: () => handler,
  });
  layer.init(viewer);
  await layer.update(viewer);

  const drone = layer._collection.entities.getById('uav:Drone1');
  viewer.scene.pick = () => ({ id: drone });
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo()?.icao24, 'Drone1');

  // Reading a numbered contact is not a request to stop flying with the drone.
  const contact = layer._collection.entities.getById('uav-target:TRK-0003');
  assert.ok(contact, 'the contact marker exists');
  viewer.scene.pick = () => ({ id: contact });
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo()?.icao24, 'Drone1');

  const caption = layer._collection.entities.getById(
    'uav-target-caption:TRK-0003',
  );
  viewer.scene.pick = () => ({ id: caption });
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo()?.icao24, 'Drone1');

  // …and neither is clicking the mission geometry the drone is flying.
  const waypoint = layer._overlayCollection.entities.getById(
    'uav-overlay:wp-6',
  );
  assert.ok(waypoint, 'the overlay waypoint exists');
  viewer.scene.pick = () => ({ id: waypoint });
  handler.click(PICK_AT);
  assert.equal(layer.getTrackedInfo()?.icao24, 'Drone1');

  layer.destroy();
});

test('mission-overlay features answer to the shared pick registry', async () => {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
  });
  source.getMissionOverlay = async () => ({
    type: 'FeatureCollection',
    features: [
      {
        id: 'wp-6',
        geometry: { type: 'Point', coordinates: [51.73, 33.73, 1500] },
        properties: { kind: 'waypoint', index: 6 },
      },
    ],
  });
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0, services: { picking } });
  layer.init(viewer);
  await layer.update(viewer);
  // A sibling layer must not read a waypoint click as empty space and drop its
  // own track.
  assert.equal(isOwnedByOtherLayer('military', 'uav-overlay:wp-6'), true);

  // A rebuilt overlay releases the ids it no longer draws.
  source.getMissionOverlay = async () => ({
    type: 'FeatureCollection',
    features: [
      {
        id: 'wp-7',
        geometry: { type: 'Point', coordinates: [51.74, 33.74, 1500] },
        properties: { kind: 'waypoint', index: 7 },
      },
    ],
  });
  source.snapshot = {
    ...source.snapshot,
    records: [
      uavRecord({ observedAtMs: T0, status: { mission: 'msn-2', fuelPct: 87 } }),
    ],
  };
  await layer.update(viewer);
  assert.equal(isOwnedByOtherLayer('military', 'uav-overlay:wp-7'), true);
  assert.equal(isOwnedByOtherLayer('military', 'uav-overlay:wp-6'), false);

  layer.destroy();
  assert.equal(isOwnedByOtherLayer('military', 'uav-overlay:wp-7'), false);
});

test('tracking publishes the drone into the shared subject context', async () => {
  const selected = [];
  const cleared = [];
  const { layer, viewer, handler } = await harness({
    contextSpy: {
      selectTrackedSubjectContext: (metadata) => selected.push(metadata),
      clearTrackedSubjectContext: (layerId) => cleared.push(layerId),
    },
  });
  const entity = layer._collection.entities.getById('uav:Drone1');
  viewer.scene.pick = () => ({ id: entity });
  handler.click(PICK_AT);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].layerId, 'uav');
  assert.equal(selected[0].id, 'Drone1');

  layer.untrack();
  assert.deepEqual(cleared, ['uav']);
  layer.destroy();
});

test('getTrackedInfo carries the mission figures the HUD needs', async () => {
  const { layer } = await harness();
  layer.track('Drone1');
  const info = layer.getTrackedInfo();
  assert.equal(info.missionPhase, 'executing');
  assert.equal(info.missionProgressPct, 43.5);
  assert.equal(info.aglM, 28);
  assert.equal(info.pitchDeg, 2);
  assert.equal(info.rollDeg, -1);
  layer.destroy();
});

test('track() before the entity exists still tracks on the first render', async () => {
  const source = mutableSource({ observedAtMs: T0, records: [] });
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0 });
  layer.init(viewer);
  layer.track('Drone1');
  assert.equal(viewer.trackedEntity, undefined);

  source.snapshot = {
    ...source.snapshot,
    records: [uavRecord({ observedAtMs: T0 })],
  };
  await layer.update(viewer);
  assert.equal(
    viewer.trackedEntity,
    layer._collection.entities.getById('uav:Drone1'),
  );
  layer.destroy();
});

test('the input handler is removed on disable and destroy', async () => {
  const { layer, handler } = await harness();
  await layer.disable();
  assert.equal(handler.destroyed, true);
  assert.equal(isOwnedByOtherLayer('military', 'uav:Drone1'), false);
  layer.destroy();
});
