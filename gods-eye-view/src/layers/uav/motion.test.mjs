import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import { createUavLayer, MAX_POSITION_SAMPLES } from './index.js';
import {
  longitudeOf,
  mutableSource,
  stubViewer,
  uavRecord,
} from './testSupport.mjs';

const T0 = 1789620000000;
const LON_A = -122.14;
const LON_B = -122.13;

/** Layer wired to a controllable clock and a rewritable snapshot. */
function harness() {
  const clock = { nowMs: T0 };
  const source = mutableSource({
    observedAtMs: T0,
    records: [uavRecord({ observedAtMs: T0, position: { longitude: LON_A } })],
  });
  const layer = createUavLayer({
    source,
    pollMs: 200,
    now: () => clock.nowMs,
  });
  const viewer = stubViewer();
  layer.init(viewer);
  return { clock, source, layer, viewer };
}

/** Advance the feed one poll: a new fix at `atMs`, `longitude` degrees east. */
async function poll({ clock, source, layer, viewer }, atMs, longitude, extra) {
  clock.nowMs = atMs;
  source.snapshot = {
    ...source.snapshot,
    observedAtMs: atMs,
    records: [
      uavRecord({
        observedAtMs: atMs,
        position: { longitude },
        ...(extra || {}),
      }),
    ],
  };
  return layer.update(viewer);
}

test('a sampled position property interpolates between two 5 Hz fixes', async () => {
  const fixture = harness();
  await fixture.layer.update(fixture.viewer);
  await poll(fixture, T0 + 1000, LON_B);

  const sampled = fixture.layer.testing.motion.sampledProperty('Drone1');
  assert.ok(sampled instanceof Cesium.SampledPositionProperty);

  const start = Cesium.JulianDate.fromDate(new Date(T0));
  const middle = Cesium.JulianDate.addSeconds(
    start,
    0.5,
    new Cesium.JulianDate(),
  );
  const midpoint = longitudeOf(sampled.getValue(middle));

  // Genuine interpolation: strictly between the two fixes, at their midpoint.
  assert.ok(midpoint > LON_A, 'midpoint is past the first fix');
  assert.ok(midpoint < LON_B, 'midpoint is short of the second fix');
  assert.ok(
    Math.abs(midpoint - (LON_A + LON_B) / 2) < 1e-6,
    `midpoint ${midpoint} is halfway between the fixes`,
  );
  fixture.layer.destroy();
});

test('the entity position renders one poll interval behind the newest fix', async () => {
  const fixture = harness();
  await fixture.layer.update(fixture.viewer);
  await poll(fixture, T0 + 1000, LON_B);

  const entity = fixture.layer._collection.entities.getById('uav:Drone1');
  assert.ok(
    entity.position instanceof Cesium.CallbackPositionProperty,
    'position is a position property the tracked camera can resolve',
  );

  // Wall clock exactly one poll past the newest fix renders that fix.
  fixture.clock.nowMs = T0 + 1200;
  const atNewest = longitudeOf(
    entity.position.getValue(Cesium.JulianDate.now()),
  );
  assert.ok(Math.abs(atNewest - LON_B) < 1e-9);

  // Half a second earlier, the frame lands between the two fixes rather than
  // snapping to either one — the property a ConstantPositionProperty cannot have.
  fixture.clock.nowMs = T0 + 700;
  const between = longitudeOf(
    entity.position.getValue(Cesium.JulianDate.now()),
  );
  assert.ok(between > LON_A && between < LON_B, `${between} is between fixes`);
  assert.ok(Math.abs(between - (LON_A + (LON_B - LON_A) * 0.5)) < 1e-6);
  fixture.layer.destroy();
});

test('render delay never exceeds the 250 ms the plan budgets', () => {
  const source = mutableSource({});
  const fast = createUavLayer({ source, pollMs: 200 });
  assert.equal(fast.testing.motion.renderDelayMs, 200);
  const slow = createUavLayer({ source, pollMs: 5000 });
  assert.equal(slow.testing.motion.renderDelayMs, 250);
});

test('samples stay bounded over a long mission', async () => {
  const fixture = harness();
  for (let index = 0; index < 200; index += 1) {
    await poll(fixture, T0 + index * 200, LON_A + index * 0.0001);
  }
  assert.ok(
    fixture.layer.testing.motion.sampleCount('Drone1') <= MAX_POSITION_SAMPLES,
    'the interpolation table is trimmed',
  );
  // Trimming must not cost the current position.
  fixture.clock.nowMs = T0 + 199 * 200 + 200;
  const entity = fixture.layer._collection.entities.getById('uav:Drone1');
  assert.ok(entity.position.getValue(Cesium.JulianDate.now()));
  fixture.layer.destroy();
});

test('a repeated observation time never corrupts the interpolation table', async () => {
  const fixture = harness();
  await fixture.layer.update(fixture.viewer);
  await poll(fixture, T0 + 1000, LON_B);
  const before = fixture.layer.testing.motion.sampleCount('Drone1');
  // The bridge re-serves the same instant (a stalled sim republishes its last
  // observation): the fix is held, not appended out of order.
  await poll(fixture, T0 + 1000, LON_B);
  await poll(fixture, T0 + 500, LON_A);
  assert.equal(fixture.layer.testing.motion.sampleCount('Drone1'), before);
  fixture.layer.destroy();
});

test('scalar entity writes stay inside the 2-5 Hz cap', async () => {
  const fixture = harness();
  await fixture.layer.update(fixture.viewer);
  const entity = fixture.layer._collection.entities.getById('uav:Drone1');
  const first = entity.label.text.getValue();
  assert.match(first, /87%/);

  // Same millisecond, new fuel figure: the label write is suppressed.
  fixture.source.snapshot = {
    ...fixture.source.snapshot,
    records: [
      uavRecord({
        observedAtMs: T0 + 100,
        position: { longitude: LON_B },
        status: { fuelPct: 42 },
      }),
    ],
  };
  await fixture.layer.update(fixture.viewer);
  assert.equal(entity.label.text.getValue(), first, 'write suppressed');

  // A poll interval later it lands.
  fixture.clock.nowMs = T0 + 200;
  await fixture.layer.update(fixture.viewer);
  assert.match(entity.label.text.getValue(), /42%/);
  fixture.layer.destroy();
});

test('motion state is released with the layer', async () => {
  const fixture = harness();
  await fixture.layer.update(fixture.viewer);
  assert.ok(fixture.layer.testing.motion.sampleCount('Drone1') > 0);
  fixture.layer.destroy();
  assert.equal(fixture.layer.testing.motion.sampleCount('Drone1'), 0);
  assert.equal(fixture.layer.testing.motion.sampledProperty('Drone1'), null);
});
