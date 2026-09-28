import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CONTEXT_DATA_SOURCE,
  CONTEXT_ERROR_BACKOFF_MS,
  CONTEXT_POLL_MS,
  DEFAULT_VISIBILITY,
  MAX_BILLBOARDS,
  contextOverlayUrl,
  createTheaterWatcher,
  kindVisible,
  mergeVisibility,
  readTheaterRef,
  visibilitySwitch,
} from './contextPolicy.js';

test('the context overlay contract numbers are the spec values (§4.2.7)', () => {
  assert.equal(CONTEXT_DATA_SOURCE, 'uav-context-overlay');
  assert.equal(CONTEXT_POLL_MS, 3000);
  assert.equal(CONTEXT_ERROR_BACKOFF_MS, 5000);
  assert.equal(MAX_BILLBOARDS, 150);
});

test('each overlay kind answers to one switch; an unknown kind to none', () => {
  assert.equal(visibilitySwitch('site'), 'sites');
  assert.equal(visibilitySwitch('force'), 'forces');
  assert.equal(visibilitySwitch('force_envelope'), 'forces');
  assert.equal(visibilitySwitch('engagement'), 'engagements');
  assert.equal(visibilitySwitch('vector'), 'vectors');
  assert.equal(visibilitySwitch('mystery'), null);
  assert.equal(visibilitySwitch(undefined), null);
});

test('switches merge partially and an unknown kind is never hidden', () => {
  const off = mergeVisibility(DEFAULT_VISIBILITY, {
    sites: false,
    bogus: false,
    forces: 'no',
  });
  assert.deepEqual(off, { ...DEFAULT_VISIBILITY, sites: false });
  assert.equal(kindVisible('site', off), false);
  assert.equal(kindVisible('force', off), true);
  const allOff = mergeVisibility(off, {
    forces: false,
    engagements: false,
    vectors: false,
  });
  assert.equal(kindVisible('mystery', allOff), true);
  assert.deepEqual(mergeVisibility(off, null), off);
});

test('the overlay URL carries truth and the last rev, encoded', () => {
  assert.equal(
    contextOverlayUrl('http://127.0.0.1:8780/'),
    'http://127.0.0.1:8780/intel/overlay?truth=0',
  );
  assert.equal(
    contextOverlayUrl('http://h', { truth: true, rev: '3:17:0:1' }),
    'http://h/intel/overlay?truth=1&rev=3%3A17%3A0%3A1',
  );
  assert.equal(
    contextOverlayUrl('', { rev: '' }),
    '/intel/overlay?truth=0',
    'an empty base is the page origin; an empty rev is omitted',
  );
});

test('a snapshot theater block reads only a plain integer epoch', () => {
  assert.deepEqual(readTheaterRef({ id: ' dyn-a ', epoch: 3 }), {
    id: 'dyn-a',
    epoch: 3,
  });
  assert.deepEqual(readTheaterRef({ id: 'default', epoch: '3' }), {
    id: 'default',
    epoch: null,
  });
  assert.deepEqual(readTheaterRef({ id: 'default', epoch: true }), {
    id: 'default',
    epoch: null,
  });
  assert.equal(readTheaterRef({ id: null, epoch: null }), null);
  assert.equal(readTheaterRef(null), null);
  assert.equal(readTheaterRef('default'), null);
});

test('the first theater seen is a baseline, not a change', () => {
  const watch = createTheaterWatcher();
  assert.equal(watch.observe({ id: 'default', epoch: 0 }), null);
  assert.equal(watch.observe({ id: 'default', epoch: 0 }), null);
  assert.deepEqual(watch.current(), { id: 'default', epoch: 0 });
});

test('a switch is one change whichever order the id and epoch arrive in', () => {
  // id and epoch together
  let watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.deepEqual(watch.observe({ id: 'dyn-k', epoch: 1 }), {
    from: { id: 'default', epoch: 0 },
    to: { id: 'dyn-k', epoch: 1 },
  });
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // unknown for one poll, then the new theater
  watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.equal(watch.observe({ id: null, epoch: null }), null);
  assert.ok(watch.observe({ id: 'dyn-k', epoch: 1 }));
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // the id first with a null epoch, the epoch a poll later: still one change
  watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.ok(watch.observe({ id: 'dyn-k', epoch: null }));
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // the same id, a transient null, then a new epoch (airframe-only switch)
  watch = createTheaterWatcher();
  watch.observe({ id: 'dyn-k', epoch: 1 });
  assert.equal(watch.observe({ id: 'dyn-k', epoch: null }), null);
  assert.deepEqual(watch.observe({ id: 'dyn-k', epoch: 2 }), {
    from: { id: 'dyn-k', epoch: 1 },
    to: { id: 'dyn-k', epoch: 2 },
  });
});

test('an older server (never an epoch) still reports an id change', () => {
  const watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: null });
  assert.equal(watch.observe({ id: 'default', epoch: null }), null);
  assert.ok(watch.observe({ id: 'iran-isfahan', epoch: null }));
  watch.reset();
  assert.equal(watch.current(), null);
});
