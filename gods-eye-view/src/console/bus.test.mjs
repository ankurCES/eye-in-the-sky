import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BUS_EVENTS, createBus } from './bus.js';

test('emit delivers the payload to every listener in subscription order', () => {
  const bus = createBus();
  const seen = [];
  bus.on('inspect', (p) => seen.push(['a', p.id]));
  bus.on('inspect', (p) => seen.push(['b', p.id]));
  assert.equal(bus.emit('inspect', { id: 'trk:T-1' }), 2);
  assert.deepEqual(seen, [
    ['a', 'trk:T-1'],
    ['b', 'trk:T-1'],
  ]);
  assert.equal(bus.emit('nobody-listens', {}), 0);
});

test('unsubscribe is idempotent and removes only its own subscription', () => {
  const bus = createBus();
  const seen = [];
  const cb = (p) => seen.push(p);
  const off1 = bus.on('mode', cb);
  bus.on('mode', cb);
  off1();
  off1();
  bus.emit('mode', 'orb');
  assert.deepEqual(seen, ['orb']);
  bus.off('mode', cb);
  bus.emit('mode', 'tracking');
  assert.deepEqual(seen, ['orb']);
});

test('a throwing listener is reported and never stops the others', () => {
  const errors = [];
  const bus = createBus({
    onError: (err, event) => errors.push([err.message, event]),
  });
  const seen = [];
  bus.on('layout', () => {
    throw new Error('panel broke');
  });
  bus.on('layout', (p) => seen.push(p.layout));
  bus.emit('layout', { layout: 'compact' });
  assert.deepEqual(seen, ['compact']);
  assert.deepEqual(errors, [['panel broke', 'layout']]);
});

test('a listener removed during an emit is skipped; one added is not called', () => {
  const bus = createBus();
  const seen = [];
  let offB = null;
  bus.on('ask', () => {
    seen.push('a');
    offB();
    bus.on('ask', () => seen.push('late'));
  });
  offB = bus.on('ask', () => seen.push('b'));
  bus.emit('ask', {});
  assert.deepEqual(seen, ['a']);
});

test('non-function listeners are ignored and clear() drops everything', () => {
  const bus = createBus();
  const off = bus.on('ask', null);
  assert.equal(typeof off, 'function');
  bus.on('ask', () => {});
  bus.clear();
  assert.equal(bus.emit('ask', {}), 0);
  assert.ok(BUS_EVENTS.includes('track:request'));
  assert.ok(Object.isFrozen(BUS_EVENTS));
});
