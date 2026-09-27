import test from 'node:test';
import assert from 'node:assert/strict';

import { createUavAutoStart, leadVehiclePosition } from './uavAutoStart.js';

const SNAPSHOT = {
  records: [
    { reference: 'Ghost', position: { latitude: null, longitude: 12 } },
    {
      reference: 'Drone1',
      position: {
        latitude: 32.6546,
        longitude: 51.668,
        ellipsoidAltitude: 1620,
      },
    },
  ],
};

function start(overrides = {}) {
  const calls = { enable: 0, snapshot: 0, fly: [] };
  const autoStart = createUavAutoStart({
    enableLayer: async () => {
      calls.enable += 1;
    },
    getSnapshot: async () => {
      calls.snapshot += 1;
      return SNAPSHOT;
    },
    flyTo: (lead) => calls.fly.push(lead),
    ...overrides,
  });
  return { autoStart, calls };
}

test('the lead drone is the first record with a position fix', () => {
  assert.deepEqual(leadVehiclePosition(SNAPSHOT), {
    reference: 'Drone1',
    latitude: 32.6546,
    longitude: 51.668,
    altitude: 1620,
  });
  assert.equal(leadVehiclePosition({ records: [] }), null);
  assert.equal(leadVehiclePosition(null), null);
});

test('it enables the layer and flies to the lead drone exactly once', async () => {
  const { autoStart, calls } = start();
  // The ObraMaestra version could run twice: the share-restore event AND a
  // 3.5 s safety timeout both invoked it. Every call now shares one run.
  const [first, second] = await Promise.all([
    autoStart.start(),
    autoStart.start(),
  ]);
  assert.equal(first, second);
  assert.deepEqual(first, { enabled: true, flew: true, reason: '' });
  await autoStart.start();
  assert.equal(calls.enable, 1);
  assert.equal(calls.snapshot, 1);
  assert.equal(calls.fly.length, 1);
  assert.equal(calls.fly[0].reference, 'Drone1');
});

test('a share link keeps its view; the layer still comes on', async () => {
  const { autoStart, calls } = start({ shouldFly: () => false });
  assert.deepEqual(await autoStart.start(), {
    enabled: true,
    flew: false,
    reason: 'view',
  });
  assert.equal(calls.snapshot, 0);
  assert.equal(calls.fly.length, 0);
});

test('a bridge that is down leaves the camera alone and never throws', async () => {
  const { autoStart, calls } = start({
    getSnapshot: async () => {
      throw new Error('UAV bridge unreachable');
    },
  });
  assert.deepEqual(await autoStart.start(), {
    enabled: true,
    flew: false,
    reason: 'bridge',
  });
  assert.equal(calls.fly.length, 0);
});

test('no fix yet, a failed enable, or a teardown all stop short', async () => {
  let run = start({ getSnapshot: async () => ({ records: [{}] }) });
  assert.equal((await run.autoStart.start()).reason, 'position');

  run = start({
    enableLayer: async () => {
      throw new Error('no data manager');
    },
  });
  assert.deepEqual(await run.autoStart.start(), {
    enabled: false,
    flew: false,
    reason: 'layer',
  });
  assert.equal(run.calls.snapshot, 0);

  const controller = new AbortController();
  run = start({
    signal: controller.signal,
    getSnapshot: async () => {
      controller.abort();
      return SNAPSHOT;
    },
  });
  assert.equal((await run.autoStart.start()).reason, 'aborted');
  assert.equal(run.calls.fly.length, 0);
});
