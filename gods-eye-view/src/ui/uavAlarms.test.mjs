import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeAlarm } from '../sources/live/uav.js';
import {
  alarmBannerModel,
  createUavAlarmSurface,
  streamStatusNotice,
} from './uavAlarms.js';

function stubDocument() {
  const create = (tag) => ({
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    textContent: '',
    append(...kids) {
      this.children.push(...kids);
    },
    replaceChildren(...kids) {
      this.children = [...kids];
    },
    setAttribute(key, value) {
      this.attrs[key] = value;
    },
    removeAttribute(key) {
      delete this.attrs[key];
    },
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    remove() {},
  });
  return { createElement: create, body: create('body') };
}

/** A surface with timers the test drives by hand. */
function surface(nowRef) {
  globalThis.document = stubDocument();
  return createUavAlarmSurface({
    now: () => nowRef.value,
    startTimer: () => 1,
    stopTimer: () => {},
  });
}

const render = (el) =>
  JSON.stringify(el, (key, value) => (key === 'listeners' ? undefined : value));

test('the feed reports its own absence rather than going quiet', () => {
  assert.match(streamStatusNotice('unsupported').text, /no \/events channel/);
  assert.match(
    streamStatusNotice('offline', { retryInMs: 6000 }).text,
    /offline — retrying in 6s/,
  );
  assert.match(streamStatusNotice('connecting').text, /connecting/);
  assert.equal(streamStatusNotice('live'), null);
  assert.equal(streamStatusNotice('idle'), null);
});

test('at least three alarm kinds render with contract severity styling', () => {
  const nowRef = { value: 1_000_000 };
  const alarms = surface(nowRef);
  for (const raw of [
    { kind: 'bingo', message: 'BINGO fuel — forcing RTB' },
    { kind: 'geofence_proximity', message: 'inside the geofence margin' },
    { kind: 'lost_link', message: 'link lost — hold-orbit plan running' },
    { kind: 'detection', message: 'TRK-0003 promoted to a track' },
  ])
    alarms.push(normalizeAlarm(raw, nowRef.value));
  const text = render(alarms.element);
  assert.match(text, /BINGO/);
  assert.match(text, /GEOFENCE PROXIMITY/);
  assert.match(text, /LOST LINK/);
  assert.match(text, /DETECTION/);
  assert.equal(alarms.active().length, 4);
  const severities = alarms
    .active()
    .map((entry) => entry.alarm.severity)
    .sort();
  assert.deepEqual(severities, ['critical', 'critical', 'info', 'warning']);
  alarms.destroy();
});

test('the banner shows the worst live alarm, then falls back to feed state', () => {
  const nowRef = { value: 1_000_000 };
  const alarms = surface(nowRef);
  // A live, quiet feed says nothing at all.
  alarms.setStreamStatus('live');
  assert.equal(alarms.banner.attrs.hidden, '');

  alarms.push(
    normalizeAlarm({ kind: 'detection', message: 'new track' }, nowRef.value),
  );
  alarms.push(
    normalizeAlarm(
      { kind: 'geofence_breach', message: 'outside the AO' },
      nowRef.value,
    ),
  );
  const banner = alarmBannerModel(alarms.active(), 'live', {}, nowRef.value);
  assert.equal(banner.severity, 'critical');
  assert.equal(banner.text, 'outside the AO');
  assert.equal(alarms.banner.attrs['data-severity'], 'critical');

  // Info alarms age out; a critical alarm stays until dismissed.
  nowRef.value += 60_000;
  alarms.prune();
  assert.deepEqual(
    alarms.active().map((entry) => entry.alarm.kind),
    ['geofence_breach'],
  );
  alarms.clear();
  const idle = alarms.setStreamStatus('offline', { retryInMs: 3000 });
  assert.equal(idle.severity, 'feed');
  assert.match(idle.text, /offline/);
  alarms.destroy();
});

test('a dismissed toast leaves the surface and the banner', () => {
  const nowRef = { value: 1_000_000 };
  const alarms = surface(nowRef);
  const entry = alarms.push(
    normalizeAlarm({ kind: 'bingo', message: 'BINGO fuel' }, nowRef.value),
  );
  assert.equal(alarms.active().length, 1);
  alarms.dismiss(entry.id);
  assert.equal(alarms.active().length, 0);
  assert.equal(alarms.banner.attrs.hidden, '');
  alarms.destroy();
});

test('the toast stack is capped so a burst cannot fill the screen', () => {
  const nowRef = { value: 1_000_000 };
  const alarms = surface(nowRef);
  for (let i = 0; i < 12; i += 1)
    alarms.push(
      normalizeAlarm({ kind: 'detection', message: `t${i}` }, nowRef.value),
    );
  assert.equal(alarms.active().length, 5);
  assert.equal(alarms.active()[0].alarm.message, 't11');
  alarms.destroy();
});

test('an unusable alarm is ignored without breaking the surface', () => {
  const nowRef = { value: 1_000_000 };
  const alarms = surface(nowRef);
  assert.equal(alarms.push(null), null);
  assert.equal(alarms.push(normalizeAlarm('garbage')), null);
  assert.equal(alarms.active().length, 0);
  alarms.destroy();
});
