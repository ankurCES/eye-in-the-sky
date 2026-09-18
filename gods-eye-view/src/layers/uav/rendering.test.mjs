import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import {
  contactNumber,
  createUavLayer,
  MODEL_SWAP_DISTANCE_M,
  readContact,
  UAV_MODEL_URL,
} from './index.js';
import { mutableSource, stubViewer, uavRecord, value } from './testSupport.mjs';

const T0 = 1789620000000;

function contactRow(overrides = {}) {
  return {
    trackId: 'TRK-A-0003',
    category: 'sam_medium_range',
    confidence: 'probable',
    threatLevel: 'high',
    position: { latitude: 33.7241, longitude: 51.7238, altitude: 1548 },
    salute: { unit: '3rd Bty', activity: 'emitting' },
    ...overrides,
  };
}

async function harness(snapshot = {}, options = {}) {
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0 })],
    ...snapshot,
  });
  const viewer = stubViewer();
  const layer = createUavLayer({ source, now: () => T0, ...options });
  layer.init(viewer);
  await layer.update(viewer);
  return { layer, viewer, source };
}

test('a vehicle draws a point far out and the MQ-9 up close', async () => {
  const { layer } = await harness(
    {},
    { resolveAsset: (url) => `/assets${url}` },
  );
  const entity = layer._collection.entities.getById('uav:Drone1');

  assert.equal(value(entity.model.uri), `/assets${UAV_MODEL_URL}`);
  const modelWhen = value(entity.model.distanceDisplayCondition);
  const pointWhen = value(entity.point.distanceDisplayCondition);

  // The GLB owns everything closer than the swap distance and the point owns
  // everything beyond it, so the drone is drawn exactly once at every range.
  assert.equal(modelWhen.near, 0);
  assert.equal(modelWhen.far, MODEL_SWAP_DISTANCE_M);
  assert.equal(pointWhen.near, MODEL_SWAP_DISTANCE_M);
  assert.ok(pointWhen.far > MODEL_SWAP_DISTANCE_M);

  // The airframe is posed from telemetry, not left nose-north.
  const orientation = value(entity.orientation);
  assert.ok(orientation instanceof Cesium.Quaternion);
  layer.destroy();
});

test('the trail survives the move to interpolated positions', async () => {
  const fixture = await harness();
  const trail = fixture.layer._collection.entities.getById('uav-trail:Drone1');
  assert.ok(trail, 'trail entity exists');
  assert.equal(value(trail.polyline.positions).length, 1);

  fixture.source.snapshot = {
    ...fixture.source.snapshot,
    observedAtMs: T0 + 200,
    records: [
      uavRecord({ observedAtMs: T0 + 200, position: { longitude: -122.1 } }),
    ],
  };
  await fixture.layer.update(fixture.viewer);
  assert.equal(value(trail.polyline.positions).length, 2);
  fixture.layer.destroy();
});

test('contacts render as numbered markers styled by threat and confidence', async () => {
  const { layer } = await harness({ contacts: [contactRow()] });
  const marker = layer._collection.entities.getById('uav-target:TRK-A-0003');
  const caption = layer._collection.entities.getById(
    'uav-target-caption:TRK-A-0003',
  );
  assert.ok(marker && caption);

  // The number the bridge already put in the track id rides the marker.
  assert.equal(value(marker.label.text), '3');
  // High threat reads red; probable confidence sets the marker's weight.
  assert.equal(
    value(marker.point.color).toCssHexString().toLowerCase().slice(0, 7),
    '#ff6b3d',
  );
  assert.equal(value(marker.point.pixelSize), 13);
  assert.equal(value(marker.point.outlineWidth), 2);

  const text = value(caption.label.text);
  assert.match(text, /TRK-A-0003/);
  assert.match(text, /sam_medium_range/);
  assert.match(text, /probable/);
  assert.match(text, /3rd Bty/);
  assert.match(text, /emitting/);
  layer.destroy();
});

test('a confirmed contact draws heavier than a possible one', async () => {
  const { layer } = await harness({
    contacts: [
      contactRow({ trackId: 'TRK-A-0001', confidence: 'confirmed' }),
      contactRow({
        trackId: 'TRK-A-0002',
        confidence: 'possible',
        threatLevel: 'low',
      }),
    ],
  });
  const confirmed = layer._collection.entities.getById('uav-target:TRK-A-0001');
  const possible = layer._collection.entities.getById('uav-target:TRK-A-0002');
  assert.ok(
    value(confirmed.point.pixelSize) > value(possible.point.pixelSize),
    'confidence drives marker weight',
  );
  assert.ok(
    value(confirmed.point.color).alpha > value(possible.point.color).alpha,
    'a possible contact is drawn faintly',
  );
  assert.equal(
    value(possible.point.color).toCssHexString().toLowerCase().slice(0, 7),
    '#7ad46c',
    'low threat reads cool',
  );
  layer.destroy();
});

test('raw /tracks rows render until the bridge ships contacts[]', async () => {
  const { layer } = await harness({
    targets: [
      {
        track_id: 'TRK-B-0007',
        class: 'radar',
        threat_level: 'medium',
        confidence: 'confirmed',
        location: { lat: 33.72, lon: 51.72, alt_m: 1500 },
        unit: 'EW site',
      },
    ],
  });
  const marker = layer._collection.entities.getById('uav-target:TRK-B-0007');
  assert.ok(marker, 'the legacy track shape still renders');
  assert.equal(value(marker.label.text), '7');
  layer.destroy();
});

test('a contact the bridge drops is removed with its caption', async () => {
  const fixture = await harness({ contacts: [contactRow()] });
  assert.equal(fixture.layer.getStats().contacts, 1);

  fixture.source.snapshot = { ...fixture.source.snapshot, contacts: [] };
  await fixture.layer.update(fixture.viewer);
  assert.equal(fixture.layer.getStats().contacts, 0);
  assert.equal(
    fixture.layer._collection.entities.getById('uav-target:TRK-A-0003'),
    undefined,
  );
  assert.equal(
    fixture.layer._collection.entities.getById('uav-target-caption:TRK-A-0003'),
    undefined,
  );
  fixture.layer.destroy();
});

test('a vehicle that stops reporting is evicted after the missed-poll grace', async () => {
  const fixture = await harness();
  fixture.source.snapshot = { ...fixture.source.snapshot, records: [] };

  await fixture.layer.update(fixture.viewer);
  assert.equal(
    fixture.layer.getStats().vehicles,
    1,
    'one dropout is tolerated',
  );
  await fixture.layer.update(fixture.viewer);
  await fixture.layer.update(fixture.viewer);
  assert.equal(fixture.layer.getStats().vehicles, 0);
  assert.equal(
    fixture.layer._collection.entities.getById('uav:Drone1'),
    undefined,
  );
  assert.equal(
    fixture.layer._collection.entities.getById('uav-trail:Drone1'),
    undefined,
  );
  fixture.layer.destroy();
});

test('contact numbering is stable and never collides', () => {
  const assigned = new Map();
  assert.equal(contactNumber(assigned, 'TRK-2026-0012'), 12);
  assert.equal(contactNumber(assigned, 'TRK-2026-0012'), 12, 'stable');
  // An id without digits takes the lowest free number.
  assert.equal(contactNumber(assigned, 'contact-alpha'), 1);
  assert.equal(contactNumber(assigned, 'contact-bravo'), 2);
  // A numbered id whose number is already worn moves up instead of doubling.
  assert.equal(contactNumber(assigned, 'TRK-2026-0002'), 3);
});

test('a contact without a usable position is not rendered', () => {
  assert.equal(readContact({ trackId: 'TRK-1' }), null);
  assert.equal(readContact({ position: { lat: 1, lon: 2 } }), null);
  assert.equal(
    readContact({ track_id: 'TRK-1', location: { lat: 1, lon: 2 } }).trackId,
    'TRK-1',
  );
});

test('a vehicle the bridge cannot place costs only itself', async () => {
  // Cartesian3.fromDegrees throws on a non-numeric coordinate. One malformed
  // row used to abort the whole tick: every other drone stopped updating and
  // the layer took an error backoff over a single bad vehicle.
  const { layer } = await harness({
    records: [
      uavRecord({ observedAtMs: T0 }),
      { reference: 'Ghost', label: 'Ghost' },
      uavRecord({
        id: 'Drone2',
        reference: 'Drone2',
        label: 'Drone2',
        observedAtMs: T0,
        position: { latitude: 47.65, longitude: -122.13, ellipsoidAltitude: 90 },
      }),
    ],
  });
  const stats = layer.getStats();
  assert.equal(stats.lastError, null, 'the tick is healthy');
  assert.equal(stats.vehicles, 2, 'both placeable drones render');
  assert.ok(layer._collection.entities.getById('uav:Drone1'));
  assert.ok(layer._collection.entities.getById('uav:Drone2'));
  assert.equal(layer._collection.entities.getById('uav:Ghost'), undefined);
  layer.destroy();
});
