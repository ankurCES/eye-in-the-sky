import test from 'node:test';
import assert from 'node:assert/strict';

import {
  OFFLINE_THEATERS,
  OFFLINE_THEATER_PAYLOAD,
  SEED_POIS,
  THEATERS,
  createTheaterRegistry,
  theaterList,
} from './uavTheaters.js';

const SERVED = {
  schema: 'godseye.theaters/v1',
  default: 'forward-ao',
  alt_datum: 'MSL',
  theaters: [
    {
      id: 'forward-ao',
      label: 'Forward AO',
      home: [10, 20, 300],
      ao: [
        [9.9, 19.9],
        [9.9, 20.1],
        [10.1, 20.1],
        [10.1, 19.9],
      ],
      pois: [{ name: 'Alpha', lat: 10, lon: 20 }],
      orbit_radius_m: 150,
      demo: { polygon: [], alt_m_agl: 60, speed_mps: 8 },
    },
  ],
};

async function servedTable() {
  const { normalizeTheaterTable } = await import('../sources/live/uav.js');
  return normalizeTheaterTable(SERVED);
}

test('the bundled fallback normalizes into a complete table', () => {
  assert.equal(OFFLINE_THEATERS.schema, 'godseye.theaters/v1');
  assert.equal(OFFLINE_THEATERS.defaultId, 'default');
  assert.equal(OFFLINE_THEATERS.order.length, 8);
  assert.equal(theaterList(OFFLINE_THEATERS).length, 8);
  for (const theater of theaterList(OFFLINE_THEATERS)) {
    assert.equal(theater.homeAltDatum, 'MSL');
    assert.ok(theater.ao.length >= 3, `${theater.id} AO`);
    assert.ok(theater.pois.length >= 3, `${theater.id} POIs`);
    assert.equal(theater.demo.polygon.length, 4, `${theater.id} demo box`);
    assert.equal(theater.demo.altMAgl, 60);
    assert.ok(theater.place, `${theater.id} names a real place`);
  }
});

test('the legacy THEATERS/SEED_POIS views mirror the fallback rows', () => {
  assert.deepEqual(Object.keys(THEATERS), OFFLINE_THEATERS.order);
  assert.deepEqual(Object.keys(SEED_POIS), OFFLINE_THEATERS.order);
  assert.deepEqual(THEATERS['iran-natanz'].home, [33.7243, 51.7286, 1580]);
  assert.equal(SEED_POIS['iran-natanz'][0].name, 'Natanz North');
});

test('the registry starts on the labelled offline fallback', () => {
  const registry = createTheaterRegistry({ load: async () => null });
  assert.equal(registry.isOffline(), true);
  assert.match(registry.originLabel(), /OFFLINE FALLBACK/);
  assert.equal(registry.table(), OFFLINE_THEATERS);
});

test('a served table wins and is labelled as coming from the bridge', async () => {
  const registry = createTheaterRegistry({ load: servedTable });
  await registry.refresh();
  assert.equal(registry.isOffline(), false);
  assert.match(registry.originLabel(), /bridge · godseye\.theaters\/v1/);
  assert.equal(registry.list().length, 1);
  assert.deepEqual(registry.get('forward-ao').home, [10, 20, 300]);
});

test('a failing or empty /theaters keeps the fallback and never throws', async () => {
  const thrown = createTheaterRegistry({
    load: async () => {
      throw new Error('bridge has no /theaters');
    },
  });
  await thrown.refresh();
  assert.equal(thrown.isOffline(), true);

  const empty = createTheaterRegistry({ load: async () => ({ order: [] }) });
  await empty.refresh();
  assert.equal(empty.isOffline(), true);
});

test('an unknown theater id resolves to the table default, never to nothing', () => {
  const registry = createTheaterRegistry({ load: async () => null });
  assert.equal(registry.get('does-not-exist').id, 'default');
  assert.equal(registry.get(undefined).id, 'default');
});

// The whole point of consuming the served table is that the bundled copy is
// no longer an independent hand-typed list. Keep the payload in the shape the
// python `theaters.as_payload()` emits so both go through one normalizer.
test('the fallback payload keeps the served payload contract', () => {
  assert.deepEqual(Object.keys(OFFLINE_THEATER_PAYLOAD).sort(), [
    'alt_datum',
    'alt_datum_note',
    'default',
    'schema',
    'theaters',
  ]);
  for (const row of OFFLINE_THEATER_PAYLOAD.theaters) {
    assert.deepEqual(Object.keys(row).sort(), [
      'ao',
      'demo',
      'description',
      'home',
      'home_alt_datum',
      'id',
      'label',
      'orbit_radius_m',
      'place',
      'pois',
    ]);
  }
});
