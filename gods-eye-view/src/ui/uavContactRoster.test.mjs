import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeContact } from '../sources/live/uav.js';
import {
  contactRosterModel,
  createUavContactRoster,
  saluteLine,
} from './uavContactRoster.js';

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

const NOW = 1_789_620_500_000;

const CONTACTS = [
  normalizeContact({
    track_id: 'TRK-0001',
    category: 'truck',
    confidence: 'possible',
    location: { lat: 33.72, lon: 51.73, alt_m: 1550 },
    last_seen_ms: NOW - 40_000,
    threat_level: 'low',
    salute: {
      size: '3 vehicles',
      activity: 'moving north',
      equipment: 'Ural-375',
    },
  }),
  normalizeContact({
    track_id: 'TRK-0003',
    category: 'sam_medium_range',
    confidence: 'probable',
    location: { lat: 33.7241, lon: 51.7238, alt_m: 1548 },
    last_seen_ms: NOW - 5_000,
    threat_level: 'high',
    salute: { size: '2 launchers', activity: 'emplaced', equipment: 'SA-6' },
  }),
  normalizeContact({
    track_id: 'TRK-0002',
    category: 'radar',
    confidence: 'confirmed',
    last_seen_ms: NOW - 900_000,
    threat_level: 'high',
  }),
];

test('a SALUTE line drops fields the report does not carry', () => {
  assert.equal(
    saluteLine({
      size: '2 launchers',
      activity: 'emplaced',
      equipment: 'SA-6',
    }),
    'S:2 launchers · A:emplaced · E:SA-6',
  );
  assert.equal(saluteLine({}), '');
  assert.equal(saluteLine(), '');
});

test('the roster orders worst threat, then confidence, then most recent', () => {
  const rows = contactRosterModel(CONTACTS, NOW);
  assert.deepEqual(
    rows.map((row) => row.trackId),
    ['TRK-0002', 'TRK-0003', 'TRK-0001'],
  );
  assert.equal(rows[0].confidence, 'confirmed');
  assert.equal(rows[1].classification, 'SAM MEDIUM RANGE');
  assert.equal(rows[2].age, '40s');
  assert.equal(rows[0].located, false); // no position reported
  assert.equal(rows[1].located, true);
});

test('the roster tolerates an absent or malformed contact feed', () => {
  assert.deepEqual(contactRosterModel(undefined, NOW), []);
  assert.deepEqual(contactRosterModel([null, {}, { trackId: '' }], NOW), []);
});

test('the roster renders track id, class, confidence, threat, SALUTE and age', () => {
  globalThis.document = stubDocument();
  const roster = createUavContactRoster({ now: () => NOW });
  const rows = roster.update(CONTACTS);
  assert.equal(rows.length, 3);
  const text = JSON.stringify(roster.element, (key, value) =>
    key === 'listeners' ? undefined : value,
  );
  assert.match(text, /TRK-0003/);
  assert.match(text, /SAM MEDIUM RANGE/);
  assert.match(text, /PROBABLE/);
  assert.match(text, /HIGH/);
  assert.match(text, /S:2 launchers · A:emplaced · E:SA-6/);
  assert.match(text, /5s/);
  assert.match(text, /NO FIX/); // TRK-0002 has no position
  roster.destroy();
});

test('clicking a contact focuses it and marks it selected', () => {
  globalThis.document = stubDocument();
  const focused = [];
  const roster = createUavContactRoster({
    now: () => NOW,
    onSelect: (contact) => focused.push(contact.trackId),
  });
  roster.update(CONTACTS);
  assert.equal(roster.select('TRK-0003'), true);
  assert.deepEqual(focused, ['TRK-0003']);
  assert.equal(roster.getSelected().trackId, 'TRK-0003');
  assert.equal(roster.select('TRK-9999'), false);
  roster.destroy();
});

test('a throwing focus handler never breaks the roster', () => {
  globalThis.document = stubDocument();
  const roster = createUavContactRoster({
    now: () => NOW,
    onSelect: () => {
      throw new Error('camera exploded');
    },
  });
  roster.update(CONTACTS);
  assert.equal(roster.select('TRK-0001'), true);
  assert.equal(roster.getSelected().trackId, 'TRK-0001');
  roster.destroy();
});

test('a contact that leaves the feed also leaves the selection', () => {
  globalThis.document = stubDocument();
  const roster = createUavContactRoster({ now: () => NOW });
  roster.update(CONTACTS);
  roster.select('TRK-0003');
  roster.update([CONTACTS[0]]);
  assert.equal(roster.getSelected(), null);
  assert.equal(roster.rows().length, 1);
  roster.destroy();
});
