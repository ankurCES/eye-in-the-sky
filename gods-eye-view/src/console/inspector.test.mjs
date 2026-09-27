import test from 'node:test';
import assert from 'node:assert/strict';

import {
  actionVehicle,
  confidenceLine,
  coords,
  createInspector,
  entityMarkup,
  formatValue,
  groupRelated,
  patternOfLifeLine,
  provenanceLabel,
  relatedLabel,
  saluteText,
  threatWord,
  typeFromId,
} from './inspector.js';
import { _openConfirm } from './situation.js';

// ---- stub DOM (the GEV convention: no jsdom) ---------------------------------

function stubDom() {
  const doc = { activeElement: null };
  const detach = (c) => {
    const p = c?.parent;
    if (p) {
      p.children = p.children.filter((x) => x !== c);
      c.parent = null;
    }
  };
  const mk = (tag) => ({
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    textContent: '',
    value: undefined,
    parent: null,
    append(...kids) {
      for (const c of kids) {
        if (c && typeof c === 'object') {
          if (c.parent && c.parent !== this) detach(c);
          c.parent = this;
        }
        this.children.push(c);
      }
    },
    replaceChildren(...kids) {
      for (const c of this.children)
        if (c && typeof c === 'object') c.parent = null;
      this.children = [];
      this.append(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    getAttribute(k) {
      return k in this.attrs ? this.attrs[k] : null;
    },
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
    fire(t, ev = {}) {
      const e = {
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
        ...ev,
      };
      for (const fn of [...(this.listeners[t] || [])]) fn(e);
      return e;
    },
    remove() {
      detach(this);
    },
    focus() {
      doc.activeElement = this;
    },
    blur() {
      if (doc.activeElement === this) doc.activeElement = null;
    },
    contains(o) {
      for (let n = o; n; n = n.parent) if (n === this) return true;
      return false;
    },
  });
  doc.createElement = mk;
  doc.body = mk('body');
  return doc;
}

const text = (el) =>
  el == null
    ? ''
    : typeof el !== 'object'
      ? String(el)
      : (el.textContent || '') + (el.children || []).map(text).join('');

function findAll(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}
const find = (root, pred) => findAll(root, pred)[0] || null;
const hasCls = (el, cls) =>
  String(el.className || '')
    .split(/\s+/)
    .includes(cls);
const byCls = (root, cls) => findAll(root, (el) => hasCls(el, cls));
const byKey = (root, key) => find(root, (el) => el.attrs?.['data-key'] === key);
const isHidden = (el) => 'hidden' in (el?.attrs || {});
const flush = () => new Promise((r) => setImmediate(r));

/** The value cell of a labelled field (dl row or SALUTE cell). */
function fieldValue(root, label) {
  const term = find(root, (el) => el.tag === 'dt' && text(el) === label);
  assert.ok(term, `field ${label}`);
  const row = term.parent;
  return row.children.find((c) => c?.tag === 'dd');
}

function fakeBus() {
  const handlers = new Map();
  const emitted = [];
  return {
    emitted,
    on(event, cb) {
      if (!handlers.has(event)) handlers.set(event, new Set());
      handlers.get(event).add(cb);
      return () => handlers.get(event)?.delete(cb);
    },
    emit(event, payload) {
      emitted.push([event, payload]);
      for (const cb of [...(handlers.get(event) || [])]) cb(payload);
    },
    all(event) {
      return emitted.filter(([e]) => e === event).map(([, p]) => p);
    },
    last(event) {
      return [...emitted].reverse().find(([e]) => e === event)?.[1];
    },
  };
}

// 2026-09-27 14:07:37Z
const NOW = Date.UTC(2026, 8, 27, 14, 7, 37);
const SEEN = Date.UTC(2026, 8, 27, 14, 1, 37);

function nodes() {
  return [
    {
      id: 'trk:T-3fa9c1',
      type: 'track',
      label: 'SA-6 battery',
      subtitle: '2K12 Kub · probable · 2 sightings',
      status: 'warn',
      ts_ms: SEEN,
      lat: 47.6445,
      lon: -122.1402,
      attrs: { threat: 'high', confidence: 'probable', sightings: 2 },
    },
    {
      id: 'trk:T-aa0001',
      type: 'track',
      label: 'Radar vehicle',
      subtitle: 'possible · 1 sighting',
      status: 'unknown',
      ts_ms: SEEN,
      attrs: { threat: 'not assessed', confidence: 'possible', sightings: 1 },
    },
    {
      id: 'trk:T-bb0002',
      type: 'track',
      label: 'Utility truck',
      status: 'ok',
      attrs: { threat: 'low' },
    },
    {
      id: 'veh:Drone1',
      type: 'vehicle',
      label: 'Drone1',
      status: 'ok',
      ts_ms: NOW - 3000,
      attrs: {
        fuel_pct: 64,
        bingo_fuel_pct: 22,
        eta_to_bingo_s: 660,
        landed: false,
        link: 'up',
        agl_m: 60,
        agl_is_real: false,
        mission: 'MSN-1a2b3c4d',
      },
    },
    {
      id: 'veh:Drone2',
      type: 'vehicle',
      label: 'Drone2',
      status: 'unknown',
      attrs: { fuel_pct: 100, landed: true, link: 'up' },
    },
    {
      id: 'msn:MSN-1a2b3c4d',
      type: 'mission',
      label: 'Grid search · Drone1',
      status: 'ok',
      ts_ms: null,
      attrs: {
        mission_id: 'MSN-1a2b3c4d',
        kind: 'grid_search',
        phase: 'executing',
        vehicle: 'Drone1',
        progress_pct: 42,
        eta_s: 860,
      },
    },
    {
      id: 'feed:contacts',
      type: 'feed',
      label: 'Contacts',
      status: 'critical',
      ts_ms: Date.UTC(2026, 8, 27, 14, 0, 12),
      attrs: { ok: false, error: 'uav_list_tracks returned no tracks[]' },
    },
    {
      id: 'alarm:7',
      type: 'alarm',
      label: 'Bingo',
      subtitle: 'Drone1 reached BINGO',
      status: 'critical',
      ts_ms: Date.UTC(2026, 8, 27, 14, 3, 12),
      attrs: { seq: 7, kind: 'bingo', severity: 'critical', vehicle: 'Drone1' },
    },
    {
      id: 'unit:sam:T-3fa9c1',
      type: 'unit',
      label: '3 x SA-6 Gainful',
      status: 'warn',
      attrs: { threat: 'high', category: 'sam' },
    },
  ];
}

const ENTITIES = {
  'trk:T-3fa9c1': {
    id: 'trk:T-3fa9c1',
    type: 'track',
    label: 'SA-6 battery',
    status: 'warn',
    fields: {
      track_id: 'T-3fa9c1',
      platform: 'SA-6 battery',
      equipment_name: '2K12 Kub',
      confidence: 'probable',
      sightings: 2,
      threat: 'high',
      lat: 47.6445,
      lon: -122.1402,
      last_seen_ms: SEEN,
      duplicates: [
        { track_id: 'T-77d0e2', run: 'r2' },
        { track_id: 'T-88e1f3', run: 'r3' },
      ],
      salute: {
        size: { text: '3 x SA-6 Gainful' },
        activity: { text: 'emplaced' },
        location: {
          lat: 47.6445,
          lon: -122.1402,
          source: 'contact detection geo_point',
        },
        unit: { text: 'SA-6 Gainful — battery' },
        equipment: { text: 'SA-6 Gainful (2K12 Kub)' },
      },
    },
    provenance: {
      source: 'uav_list_tracks SALUTE row (M11 persistent track store)',
      position: 'last fix',
    },
    related: [
      {
        id: 'unit:sam:T-3fa9c1',
        type: 'unit',
        label: '3 x SA-6 Gainful',
        kind: 'member_of',
        dir: 'out',
      },
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        label: 'Drone1',
        kind: 'tracking',
        dir: 'in',
      },
      {
        id: 'rpt:R-gone',
        type: 'report',
        label: 'INTREP R-gone',
        kind: 'reports_on',
        dir: 'in',
      },
    ],
    raw: { track_id: 'T-3fa9c1', size: '3' },
  },
  'trk:T-aa0001': {
    id: 'trk:T-aa0001',
    type: 'track',
    label: 'Radar vehicle',
    fields: {
      confidence: 'possible',
      sightings: 1,
      threat: 'not assessed',
      last_seen_ms: SEEN,
      salute: {},
    },
    provenance: {},
    related: [],
  },
  'trk:T-bb0002': {
    id: 'trk:T-bb0002',
    type: 'track',
    fields: { threat: 'low', confidence: 'probable', salute: {} },
  },
  'veh:Drone1': {
    id: 'veh:Drone1',
    type: 'vehicle',
    fields: {
      name: 'Drone1',
      fuel: {
        fuel_pct: 64,
        bingo_fuel_pct: 22,
        eta_to_bingo_s: 660,
        bingo_latched: false,
      },
      agl: { alt_agl_m: 60, alt_agl_is_real: false },
      link: { state: 'up' },
      mission: 'MSN-1a2b3c4d',
    },
    provenance: {
      agl_is_measured: false,
      agl_note: 'AGL is height above the LAUNCH DATUM, not terrain clearance',
    },
    related: [
      {
        id: 'msn:MSN-1a2b3c4d',
        type: 'mission',
        label: 'Grid search · Drone1',
        kind: 'flying',
        dir: 'out',
      },
    ],
  },
  'veh:Drone2': {
    id: 'veh:Drone2',
    type: 'vehicle',
    fields: { fuel: { fuel_pct: 100, bingo_fuel_pct: null }, agl: {} },
  },
  'msn:MSN-1a2b3c4d': {
    id: 'msn:MSN-1a2b3c4d',
    type: 'mission',
    fields: {
      mission_id: 'MSN-1a2b3c4d',
      phase: 'executing',
      started_ms: null,
    },
  },
  'feed:contacts': {
    id: 'feed:contacts',
    type: 'feed',
    fields: {
      ok: false,
      error: 'uav_list_tracks returned no tracks[]',
      at_ms: Date.UTC(2026, 8, 27, 14, 0, 12),
    },
  },
  'alarm:7': {
    id: 'alarm:7',
    type: 'alarm',
    fields: {
      kind: 'bingo',
      severity: 'critical',
      message: 'Drone1 reached BINGO',
      atMs: Date.UTC(2026, 8, 27, 14, 3, 12),
      vehicle: 'Drone1',
    },
  },
  'rpt:R-1': {
    id: 'rpt:R-1',
    type: 'report',
    label: 'INTREP R-1',
    fields: {
      header: { format: 'INTREP', report_id: 'R-1' },
      gaps: ['No coverage east of the river'],
      mission_summary:
        'Two **contacts** near [[poi:default:North Field|North Field]].',
      contacts: [{ track_id: 'T-3fa9c1' }],
    },
  },
};

function mount({ entity, layout = 'wide', api } = {}) {
  const doc = stubDom();
  globalThis.document = doc;
  const state = {
    graph: { nodes: nodes(), edges: [] },
    byId: null,
    status: 'live',
  };
  const listeners = new Set();
  const viewed = [];
  const fetched = [];
  const store = {
    get: () => state,
    on: (e, cb) => (listeners.add(cb), () => listeners.delete(cb)),
    change(next) {
      Object.assign(state, next);
      for (const cb of listeners) cb({});
    },
    entity(id) {
      fetched.push(id);
      if (entity) return entity(id, fetched.length);
      return ENTITIES[id]
        ? Promise.resolve(structuredClone(ENTITIES[id]))
        : Promise.resolve(null);
    },
    markAlarmViewed: (id) => viewed.push(id),
  };
  const bus = fakeBus();
  const root = doc.createElement('div');
  const host = doc.createElement('div');
  root.append(host);
  const orbCalls = [];
  const orb = { highlight: (ids, opts) => orbCalls.push([ids, opts]) };
  const ctx = {
    store,
    bus,
    root,
    orb,
    api: api || { abortVehicle: () => Promise.resolve({ aborted: true }) },
  };
  const inspector = createInspector(host, ctx, { now: () => NOW, layout });
  return {
    doc,
    store,
    bus,
    root,
    host,
    ctx,
    inspector,
    viewed,
    fetched,
    orbCalls,
    el: inspector.element,
  };
}

async function showLoaded(m, id) {
  m.inspector.show(id);
  await flush();
  return m.el;
}

// ---- pure helpers -------------------------------------------------------------------

test('pure helpers: SALUTE text, coordinates, threat and confidence words', () => {
  assert.equal(saluteText({ text: 'emplaced' }), 'emplaced');
  assert.equal(saluteText('  moving '), 'moving');
  assert.equal(saluteText({}), null);
  assert.equal(saluteText(null), null);
  assert.equal(coords(47.6445, -122.1402), '47.64450, -122.14020');
  assert.equal(coords(null, 1), null);
  assert.equal(threatWord('high'), 'High');
  assert.equal(threatWord('not assessed'), null, 'missing is never a level');
  assert.equal(threatWord(''), null);
  assert.equal(confidenceLine('probable', 2), 'Probable, 2 sightings');
  assert.equal(confidenceLine('confirmed', 1), 'Confirmed, 1 sighting');
  assert.equal(confidenceLine(null, null), 'Unrated');
  assert.equal(formatValue(true), 'Yes');
  assert.equal(formatValue({ a_b: 1, c: null }), 'A b: 1');
  assert.equal(
    formatValue(['x'.repeat(400)]).length,
    240,
    'compound values are bounded',
  );
  assert.equal(
    formatValue('x'.repeat(400)).length,
    400,
    'server strings stay verbatim',
  );
});

test('pure helpers: related groups, action vehicle, entity markup', () => {
  const groups = groupRelated([
    { id: 'rpt:1', kind: 'reports_on' },
    { id: 'msn:1', kind: 'flying' },
    { id: 'msn:1', kind: 'flying' },
    { id: 'unit:1', kind: 'member_of' },
    { id: 'm:2', kind: 'target' },
    { id: 'm:3', kind: 'observes' },
  ]);
  assert.deepEqual(
    groups.map(([label, list]) => [label, list.length]),
    [
      ['Flying', 1],
      ['Member of', 1],
      ['Reported in', 1],
      ['Observed by mission', 2],
    ],
  );
  assert.equal(actionVehicle('vehicle', 'veh:Drone1'), 'Drone1');
  assert.equal(
    actionVehicle('mission', 'msn:X', { vehicle: 'Drone2' }),
    'Drone2',
  );
  assert.equal(actionVehicle('track', 'trk:X'), null);
  assert.equal(
    entityMarkup('trk:T-1', 'SA-6 [battery] | x'),
    '[[trk:T-1|SA-6 battery x]]',
  );
  assert.equal(entityMarkup('trk:T-1', ''), '[[trk:T-1]]');
  assert.equal(typeFromId('veh:Drone1'), 'vehicle');
  assert.equal(typeFromId('ob:sa6'), 'equipment');
  assert.equal(typeFromId('T-1'), 'track');
});

// ---- contacts --------------------------------------------------------------------------

test('a contact: header, SALUTE grid, threat as an estimate, confidence and merged sightings', async () => {
  const m = mount();
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  assert.ok(!isHidden(el));
  assert.equal(el.attrs.role, 'region');
  const head = byCls(el, 'ic-inspector__head')[0];
  assert.match(text(head), /SA-6 battery/);
  assert.match(text(head), /Contact/);
  assert.match(text(head), /High/);
  assert.match(text(head), /trk:T-3fa9c1/);
  assert.ok(find(head, (x) => x.attrs?.['aria-label'] === 'Copy id'));

  const salute = byCls(el, 'ic-inspector__salute')[0];
  assert.equal(text(fieldValue(salute, 'Size')), '3 x SA-6 Gainful');
  assert.equal(text(fieldValue(salute, 'Activity')), 'emplaced');
  assert.equal(text(fieldValue(salute, 'Location')), '47.64450, -122.14020');
  assert.ok(
    byCls(fieldValue(salute, 'Location'), 'ic-kit-mono').length === 1,
    'coordinates in mono',
  );
  assert.equal(
    text(fieldValue(salute, 'Time')),
    'Last seen 14:01:37Z, 6 min ago',
  );
  assert.equal(
    text(fieldValue(salute, 'Equipment')),
    'SA-6 Gainful (2K12 Kub)',
  );

  const threat = fieldValue(el, 'Threat');
  assert.match(text(threat), /^High ≈/);
  assert.match(text(threat), /Estimated/);
  assert.match(text(threat), /Model output\. Sensor-posture advice only\./);
  assert.equal(text(fieldValue(el, 'Confidence')), 'Probable, 2 sightings');
  assert.match(
    text(el),
    /3 sightings from separate runs merged into this contact/,
  );
  assert.match(text(el), /T-77d0e2/);
  assert.match(text(el), /How we know/);
  assert.match(text(el), /uav_list_tracks SALUTE row/);
});

test('"Not assessed" is distinct from an assessed low threat, never "none" or "Low"', async () => {
  const m = mount();
  let el = await showLoaded(m, 'trk:T-aa0001');
  const threat = fieldValue(el, 'Threat');
  assert.equal(text(byCls(threat, 'ic-kit-notassessed')[0]), 'Not assessed');
  assert.match(text(threat), /This is not the same as no threat\./);
  assert.doesNotMatch(text(threat), /Low|None|≈/);
  const headStatus = byCls(
    byCls(el, 'ic-inspector__head')[0],
    'ic-kit-notassessed',
  )[0];
  assert.equal(text(headStatus), 'Not assessed');

  m.inspector.hide();
  el = await showLoaded(m, 'trk:T-bb0002');
  const low = fieldValue(el, 'Threat');
  assert.match(text(low), /^Low ≈/);
  assert.equal(byCls(low, 'ic-kit-notassessed').length, 0);
  const status = byCls(
    byCls(el, 'ic-inspector__head')[0],
    'ic-inspector__status',
  )[0];
  assert.equal(
    status.attrs['data-tone'],
    'low',
    'assessed low is film white, never green',
  );
});

test('missing SALUTE values are "No reading" boxes with a reason, never blanks', async () => {
  const m = mount();
  const el = await showLoaded(m, 'trk:T-aa0001');
  const salute = byCls(el, 'ic-inspector__salute')[0];
  for (const label of ['Size', 'Activity', 'Unit', 'Equipment']) {
    const cell = fieldValue(salute, label);
    assert.equal(byCls(cell, 'ic-kit-noreading').length, 1, label);
    assert.match(text(cell), /No reading/);
    assert.match(text(cell), /Not in the track record/);
  }
  assert.match(
    text(fieldValue(salute, 'Location')),
    /No reading.*No position fix/,
  );
});

test('an assumed location carries the tag and the reason', async () => {
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'trk:T-3fa9c1',
        type: 'track',
        fields: {
          lat: 1,
          lon: 2,
          threat: 'high',
          salute: { location: { lat: 1, lon: 2, los_is_measured: false } },
        },
      }),
  });
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  const loc = fieldValue(byCls(el, 'ic-inspector__salute')[0], 'Location');
  assert.equal(byCls(loc, 'ic-kit-assumed').length, 1);
  const tag = byCls(loc, 'ic-kit-reg')[0];
  assert.equal(text(tag), 'Assumed');
  assert.equal(tag.attrs['data-register'], 'assumed');
});

test('related entities are chips grouped by edge kind; unknown ids are dashed with a tooltip', async () => {
  const m = mount();
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  const labels = byCls(el, 'ic-inspector__related-label').map(text);
  assert.deepEqual(labels, ['Tracked by', 'Member of', 'Reported in']);
  const gone = byKey(el, 'chip:rpt:R-gone');
  assert.equal(gone.attrs['data-known'], 'false');
  assert.equal(
    gone.attrs.title,
    'Not in the current picture (outside this theater or aged out).',
  );
  const drone = byKey(el, 'chip:veh:Drone1');
  drone.fire('mouseenter');
  assert.deepEqual(m.orbCalls.at(-1), [['veh:Drone1'], { by: 'operator' }]);
  drone.fire('click');
  await flush();
  assert.equal(m.inspector.current(), 'veh:Drone1');
  const back = byKey(el, 'back');
  assert.match(text(back), /Back to SA-6 battery/);
  back.fire('click');
  await flush();
  assert.equal(m.inspector.current(), 'trk:T-3fa9c1');
  assert.equal(byKey(el, 'back'), null, 'one level of history');
});

test('Show raw record discloses the raw row as text', async () => {
  const m = mount();
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  const toggle = byKey(el, 'raw');
  assert.equal(toggle.attrs['aria-expanded'], 'false');
  toggle.fire('click');
  const again = byKey(el, 'raw');
  assert.equal(again.attrs['aria-expanded'], 'true');
  assert.match(
    text(byCls(el, 'ic-inspector__rawtext')[0]),
    /"track_id": "T-3fa9c1"/,
  );
});

test('Ask about this drafts a reference without sending; Focus is an operator focus', async () => {
  const m = mount();
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  byKey(el, 'act:ask').fire('click');
  assert.deepEqual(m.bus.last('ask'), {
    text: 'Tell me about [[trk:T-3fa9c1|SA-6 battery]]',
    focused_ids: ['trk:T-3fa9c1'],
    draft: true,
  });
  byKey(el, 'act:focus').fire('click');
  const focus = m.bus.last('focus:entities');
  assert.equal(focus.by, 'operator');
  assert.deepEqual(
    focus.ids,
    ['trk:T-3fa9c1', 'unit:sam:T-3fa9c1', 'veh:Drone1'],
    'only ids in the picture',
  );
  assert.equal(byKey(el, 'act:track'), null, 'contacts are not tracked');
  assert.equal(byKey(el, 'act:abort'), null);
});

// ---- vehicles, missions, feeds, alarms, reports ----------------------------------------------

test('a vehicle: state, fuel vs BINGO with margin, time to BINGO, assumed AGL, link, mission chip', async () => {
  const m = mount();
  const el = await showLoaded(m, 'veh:Drone1');
  assert.equal(text(fieldValue(el, 'State')), 'Airborne');
  const fuel = fieldValue(el, 'Fuel');
  assert.equal(byCls(fuel, 'ic-kit-fuel__tick').length, 1);
  assert.equal(
    text(byCls(fuel, 'ic-kit-fuelline')[0]),
    '64.0%, BINGO 22.0%, 42.0 points above',
  );
  assert.match(
    text(fieldValue(el, 'To BINGO')),
    /≈ 11 min to BINGO.*Estimated/,
  );
  const agl = fieldValue(el, 'AGL');
  assert.match(text(agl), /60 m/);
  assert.equal(text(byCls(agl, 'ic-kit-reg')[0]), 'Assumed');
  assert.equal(
    byCls(agl, 'ic-kit-assumed')[0].attrs.title,
    'AGL is height above the LAUNCH DATUM, not terrain clearance',
  );
  assert.equal(text(fieldValue(el, 'Link')), 'Link up');
  assert.equal(
    text(fieldValue(el, 'Telemetry')),
    'Last update 14:07:34Z, 3 s ago',
  );
  assert.ok(byKey(fieldValue(el, 'Mission'), 'chip:msn:MSN-1a2b3c4d'));
  assert.match(text(el), /AGL measured.*No.*Assumed/);
});

test('vehicle actions: Track, Open mission panel and Abort go to the operator paths', async () => {
  const calls = [];
  const m = mount({
    api: {
      abortVehicle: (v) => (calls.push(v), Promise.resolve({ aborted: true })),
    },
  });
  const el = await showLoaded(m, 'veh:Drone1');
  byKey(el, 'act:track').fire('click');
  assert.deepEqual(m.bus.last('track:request'), {
    vehicle: 'Drone1',
    source: 'operator',
  });
  byKey(el, 'act:mission').fire('click');
  assert.deepEqual(m.bus.last('track:request'), {
    vehicle: 'Drone1',
    source: 'operator',
    reason: 'Open mission panel',
    openMissionPanel: true,
  });
  byKey(el, 'act:abort').fire('click');
  const dialog = byCls(m.root, 'ic-abort-confirm')[0];
  assert.ok(dialog, 'Abort goes through the shared confirm');
  byKey(dialog, 'abort:go').fire('click');
  await flush();
  assert.deepEqual(calls, ['Drone1']);
  assert.equal(m.bus.all('ask').length, 0, 'never through the analyst');
  _openConfirm()?.close();
});

test('a landed vehicle with no task has no Abort; no BINGO line says so', async () => {
  const m = mount();
  const el = await showLoaded(m, 'veh:Drone2');
  assert.equal(byKey(el, 'act:abort'), null);
  assert.ok(byKey(el, 'act:track'));
  assert.equal(byKey(el, 'act:mission'), null);
  assert.equal(text(fieldValue(el, 'State')), 'Landed');
  assert.match(text(fieldValue(el, 'Fuel')), /BINGO not computed yet/);
  assert.equal(byCls(fieldValue(el, 'Fuel'), 'ic-kit-fuel__tick').length, 0);
  assert.match(text(fieldValue(el, 'AGL')), /No reading/);
});

test('a mission that ended after the details loaded: no Mission row, no Abort once landed', async () => {
  const m = mount();
  const el = await showLoaded(m, 'veh:Drone1');
  assert.ok(byKey(el, 'act:abort'), 'flying a mission: Abort offered');
  assert.ok(byKey(fieldValue(el, 'Mission'), 'chip:msn:MSN-1a2b3c4d'));
  // The poll says the mission ended and the drone landed; the entity fetched
  // on open still names the mission.
  const nodes = m.store.get().graph.nodes.map((n) =>
    n.id === 'veh:Drone1'
      ? {
          ...n,
          attrs: { ...n.attrs, landed: true, mission: undefined },
        }
      : n,
  );
  m.store.change({ graph: { nodes, edges: [] } });
  assert.equal(byKey(el, 'act:abort'), null, 'the rail hides it too');
  assert.equal(byKey(el, 'act:mission'), null);
  assert.ok(!find(el, (e) => e.tag === 'dt' && text(e) === 'Mission'));
  assert.deepEqual(m.fetched, ['veh:Drone1'], 'no refetch needed');
});

test('a mission: phase word, magenta progress, ETA estimate, no invented start time', async () => {
  const m = mount();
  const el = await showLoaded(m, 'msn:MSN-1a2b3c4d');
  assert.equal(text(fieldValue(el, 'Kind')), 'Grid search');
  assert.equal(text(fieldValue(el, 'Phase')), 'Executing');
  assert.equal(byCls(fieldValue(el, 'Progress'), 'ic-kit-progress').length, 1);
  assert.match(text(fieldValue(el, 'Progress')), /42%/);
  assert.match(text(fieldValue(el, 'ETA')), /≈ 14 min 20 s/);
  assert.equal(text(fieldValue(el, 'Started')), 'Start time not recorded');
  assert.ok(byKey(fieldValue(el, 'Vehicle'), 'chip:veh:Drone1'));
  assert.ok(byKey(el, 'act:track'), 'missions track through their vehicle');
  assert.equal(byKey(el, 'act:abort'), null);
});

test('a feed that is down is a No reading box, never a clear picture', async () => {
  const m = mount();
  const el = await showLoaded(m, 'feed:contacts');
  const box = byCls(el, 'ic-kit-noreading')[0];
  assert.match(
    text(box),
    /Down since 14:00:12Z\. Missing data here is not a clear picture\./,
  );
  assert.match(text(el), /uav_list_tracks returned no tracks\[\]/);
});

test('an alarm: kind label, severity, message verbatim, Zulu time, About chip; inspecting marks it viewed', async () => {
  const m = mount();
  const el = await showLoaded(m, 'alarm:7');
  assert.equal(text(fieldValue(el, 'Kind')), 'BINGO fuel');
  assert.equal(text(fieldValue(el, 'Severity')), 'Critical');
  assert.equal(text(fieldValue(el, 'Message')), 'Drone1 reached BINGO');
  assert.equal(text(fieldValue(el, 'Time')), '14:03:12Z');
  assert.ok(byKey(fieldValue(el, 'About'), 'chip:veh:Drone1'));
  assert.match(text(el), /The BINGO return can't be cancelled\./);
  assert.deepEqual(
    m.viewed,
    ['alarm:7'],
    'the store marks it (and emits alarm:viewed)',
  );
});

test('a report renders its gaps and Markdown summary safely, with contact chips', async () => {
  const m = mount();
  const el = await showLoaded(m, 'rpt:R-1');
  assert.match(text(fieldValue(el, 'Format')), /INTREP.*R-1/);
  assert.match(text(el), /Gaps/);
  assert.match(text(el), /No coverage east of the river/);
  const md = byCls(el, 'ic-md')[0];
  assert.ok(md, 'summary through chat/markdown.js');
  assert.match(text(md), /Two contacts near North Field\./);
  assert.ok(
    byKey(md, 'chip:poi:default:North Field'),
    'entity markup becomes a chip',
  );
  assert.ok(byKey(el, 'chip:trk:T-3fa9c1'));
});

// ---- load states ----------------------------------------------------------------------------

test('loading shows the graph node immediately, then the details', async () => {
  let release;
  const m = mount({ entity: () => new Promise((r) => (release = r)) });
  m.inspector.show('trk:T-3fa9c1');
  const el = m.el;
  assert.equal(text(byCls(el, 'ic-inspector__loading')[0]), 'Loading details…');
  assert.match(text(byCls(el, 'ic-inspector__head')[0]), /SA-6 battery/);
  assert.match(
    text(fieldValue(el, 'Threat')),
    /High ≈/,
    'node-based body while loading',
  );
  release(structuredClone(ENTITIES['trk:T-3fa9c1']));
  await flush();
  assert.ok(isHidden(byCls(el, 'ic-inspector__loading')[0]));
  assert.match(text(el), /How we know/);
});

test('an error says what failed and Retry fetches again', async () => {
  const m = mount({
    entity: (id, n) =>
      n === 1
        ? Promise.reject(new Error('boom.'))
        : Promise.resolve(structuredClone(ENTITIES[id])),
  });
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  assert.match(text(el), /Couldn't load details for trk:T-3fa9c1: boom\./);
  byKey(el, 'retry').fire('click');
  await flush();
  assert.equal(m.fetched.length, 2);
  assert.doesNotMatch(text(el), /Couldn't load details/);
  assert.match(text(el), /How we know/);
});

test('a 404 or a node that leaves the picture says so, with the last-seen time', async () => {
  const m = mount({
    entity: () => {
      const err = new Error('not found');
      err.status = 404;
      return Promise.reject(err);
    },
  });
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  assert.match(
    text(el),
    /This entity left the picture\. Last seen 14:01:37Z\./,
  );

  const m2 = mount();
  await showLoaded(m2, 'trk:T-aa0001');
  m2.store.change({
    graph: { nodes: nodes().filter((n) => n.id !== 'trk:T-aa0001'), edges: [] },
  });
  assert.match(
    text(m2.el),
    /This entity left the picture\. Last seen 14:01:37Z\./,
  );
});

test('truncated details and merged ids are stated', async () => {
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'trk:T-3fa9c1',
        type: 'track',
        requested_id: 'T-77d0e2',
        fields: { threat: 'high' },
        _truncated: { dropped: ['raw'] },
      }),
  });
  const el = await showLoaded(m, 'T-77d0e2');
  assert.equal(m.inspector.current(), 'trk:T-3fa9c1');
  assert.match(text(el), /Merged into T-3fa9c1\./);
  assert.match(
    text(el),
    /Details are larger than shown\. Ask the analyst for the full record\./,
  );
});

// ---- placement and wiring ---------------------------------------------------------------------

test('placement follows the layout: compact heads with "Back to analyst", narrow with "Back"', async () => {
  const m = mount();
  await showLoaded(m, 'trk:T-3fa9c1');
  assert.equal(m.el.attrs['data-layout'], 'wide');
  m.bus.emit('layout', { layout: 'compact' });
  assert.equal(m.el.attrs['data-layout'], 'compact');
  assert.match(text(byKey(m.el, 'back')), /Back to analyst/);
  byKey(m.el, 'back').fire('click');
  assert.ok(isHidden(m.el));
  m.inspector.setLayout('narrow');
  await showLoaded(m, 'trk:T-3fa9c1');
  assert.equal(text(byKey(m.el, 'back')).trim().endsWith('Back'), true);
});

test('the bus opens it; Escape and Close hide it and report the state', async () => {
  const m = mount();
  m.bus.emit('inspect', { id: 'veh:Drone1' });
  await flush();
  assert.equal(m.inspector.current(), 'veh:Drone1');
  assert.deepEqual(m.bus.all('inspector:state')[0], {
    open: true,
    id: 'veh:Drone1',
  });
  m.bus.emit('inspect', { id: 'veh:Drone1' });
  assert.equal(m.fetched.length, 1, 'the same id is not fetched twice');
  m.el.fire('keydown', { key: 'Escape' });
  assert.ok(isHidden(m.el));
  assert.deepEqual(m.bus.last('inspector:state'), {
    open: false,
    id: 'veh:Drone1',
  });
  await showLoaded(m, 'trk:T-3fa9c1');
  byKey(m.el, 'act:close').fire('click');
  assert.ok(isHidden(m.el));
  assert.equal(m.inspector.current(), null);
});

test("the analyst's inspect opens the plate at wide but never covers the chat at compact or narrow", async () => {
  const wide = mount({ layout: 'wide' });
  wide.bus.emit('inspect', { id: 'veh:Drone1', by: 'analyst' });
  await flush();
  assert.equal(wide.inspector.current(), 'veh:Drone1');
  for (const layout of ['compact', 'narrow']) {
    const m = mount({ layout });
    m.bus.emit('inspect', { id: 'veh:Drone1', by: 'analyst' });
    await flush();
    assert.equal(m.inspector.current(), null, layout);
    // The operator's own inspect (the shell's "Inspect") still opens it.
    m.bus.emit('inspect', { id: 'veh:Drone1' });
    await flush();
    assert.equal(m.inspector.current(), 'veh:Drone1', layout);
  }
});

test('a stale response never overwrites a newer selection', async () => {
  const pending = {};
  const m = mount({
    entity: (id) =>
      new Promise(
        (r) => (pending[id] = () => r(structuredClone(ENTITIES[id]))),
      ),
  });
  m.inspector.show('trk:T-3fa9c1');
  m.inspector.show('veh:Drone1');
  pending['veh:Drone1']();
  await flush();
  pending['trk:T-3fa9c1']();
  await flush();
  assert.equal(m.inspector.current(), 'veh:Drone1');
  assert.match(text(fieldValue(m.el, 'State')), /Airborne/);
});

test('destroy detaches and unsubscribes', () => {
  const m = mount();
  m.inspector.destroy();
  assert.equal(m.host.children.length, 0);
  m.bus.emit('inspect', { id: 'veh:Drone1' });
  assert.equal(m.fetched.length, 0);
});

// ---- JS-PANELS-POLISH: v1.1 graph and entity fields ---------------------------

test('related groups read the edge direction: a mission is flown by its vehicle', () => {
  assert.equal(relatedLabel('flying', 'out'), 'Flying');
  assert.equal(relatedLabel('flying', 'in'), 'Flown by');
  assert.equal(relatedLabel('member_of', 'in'), 'Members');
  assert.equal(relatedLabel('tracking', 'in'), 'Tracked by');
  assert.equal(relatedLabel('tracking', 'out'), 'Tracking');
  assert.equal(relatedLabel('near', undefined), 'Near');
  const groups = groupRelated([
    { id: 'veh:Drone1', kind: 'flying', dir: 'in' },
    { id: 'trk:T-1', kind: 'observes', dir: 'out' },
  ]);
  assert.deepEqual(
    groups.map(([label]) => label),
    ['Flown by', 'Observes'],
  );
});

test('merged duplicates count from duplicate_count, not the shortened id list', async () => {
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'trk:T-3fa9c1',
        type: 'track',
        fields: { threat: 'high', salute: {} },
      }),
  });
  const node = m.store.get().graph.nodes[0];
  node.attrs = {
    ...node.attrs,
    duplicates: ['trk:T-1', 'trk:T-2', 'trk:T-3', 'trk:T-4', 'trk:T-5'],
    duplicate_count: 12,
  };
  const el = await showLoaded(m, 'trk:T-3fa9c1');
  const t = text(el);
  assert.match(t, /13 sightings from separate runs merged into this contact/);
  assert.match(t, /7 more not listed\./);
  assert.equal(byCls(el, 'ic-kit-idtag').length, 5);
});

test('a contact outside the active theater or just outside the AO says so', async () => {
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'trk:T-aa0001',
        type: 'track',
        fields: {
          threat: 'not assessed',
          theater: 'ukraine-donbas',
          salute: {},
        },
      }),
  });
  m.store.get().graph.nodes[1].attrs.out_of_theater = true;
  const el = await showLoaded(m, 'trk:T-aa0001');
  assert.equal(
    text(fieldValue(el, 'Theater')),
    'Outside the active theater (ukraine-donbas)',
  );

  const m2 = mount({
    entity: () =>
      Promise.resolve({
        id: 'trk:T-aa0001',
        type: 'track',
        fields: { threat: 'not assessed', salute: {} },
      }),
  });
  m2.store.get().graph.meta = { ao_margin_m: 10000 };
  m2.store.get().graph.nodes[1].attrs.outside_ao = true;
  const el2 = await showLoaded(m2, 'trk:T-aa0001');
  assert.equal(
    text(fieldValue(el2, 'Theater')),
    'Just outside the AO, within the 10 km margin',
  );
});

test('the lost-link plan is a sentence from data, and a server default says so', async () => {
  const m = mount();
  m.store.get().graph.nodes[3].attrs.lost_link = {
    behaviour: 'rtb',
    declare_after_s: 5,
    escalate_to_rtb_after_s: 300,
    source: 'server default (uav://safety/geofence, as of boot)',
  };
  const el = await showLoaded(m, 'veh:Drone1');
  const t = text(fieldValue(el, 'Lost-link plan'));
  assert.match(t, /^If the link drops for 5 s, Drone1 returns to base\./);
  assert.match(t, /The server's default plan, not read from this aircraft\./);
  assert.doesNotMatch(t, /Rtb/);
});

test('entity caveats appear under How we know with the Assumed tag; provenance keys read as words', async () => {
  const caveat =
    'Real-data layer is off: AGL is height above the launch datum (not terrain clearance).';
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'veh:Drone1',
        type: 'vehicle',
        fields: { fuel: { fuel_pct: 64, bingo_fuel_pct: 22 }, agl: {} },
        provenance: { stale_ms: 42000, datum_source: 'egm96' },
        caveats: [caveat],
      }),
  });
  const el = await showLoaded(m, 'veh:Drone1');
  const how = find(
    el,
    (x) => x.tag === 'section' && /How we know/.test(text(x)),
  );
  assert.ok(how);
  assert.match(text(how), new RegExp(caveat.replace(/[()]/g, '\\$&')));
  assert.equal(text(byCls(how, 'ic-kit-reg').at(-1)), 'Assumed');
  assert.equal(text(fieldValue(el, 'Telemetry age')), '42 s');
  assert.equal(text(fieldValue(el, 'Altitude datum')), 'egm96');
  assert.equal(provenanceLabel('agl_is_measured'), 'AGL measured');
});

test('pattern of life is one honest line, never a dump of the baseline', () => {
  assert.equal(
    patternOfLifeLine({
      baseline: { total_obs: 0, visits: 0, hourly: new Array(24).fill(0) },
      deviation: { mature: false, baseline_samples: 0, deviation: 0 },
    }),
    "No observations here yet. The baseline isn't mature yet (0 of 24 observations), so no deviation is scored.",
  );
  assert.equal(
    patternOfLifeLine({
      baseline: { total_obs: 31, visits: 4 },
      deviation: { mature: true, deviation: 0.4213 },
    }),
    '31 observations, 4 visits. Deviation from the baseline now ≈ 0.42.',
  );
  assert.equal(patternOfLifeLine(null), null);
});

test('a feed is named and stated as the orb states it, with no plumbing in the header', async () => {
  const m = mount({
    entity: () =>
      Promise.resolve({
        id: 'feed:contacts',
        type: 'feed',
        label: 'Contacts',
        subtitle: 'mcp:uav_list_tracks',
        status: 'ok',
        fields: { ok: true, at_ms: NOW - 2000, detail: 'mcp:uav_list_tracks' },
      }),
  });
  const node = m.store.get().graph.nodes.find((n) => n.id === 'feed:contacts');
  Object.assign(node, {
    status: 'ok',
    subtitle: 'mcp:uav_list_tracks',
    ts_ms: NOW - 2000,
    attrs: { ok: true },
  });
  const el = await showLoaded(m, 'feed:contacts');
  const head = byCls(el, 'ic-inspector__head')[0];
  assert.match(text(head), /Contacts feed/);
  assert.match(text(head), /Up/);
  assert.doesNotMatch(text(el), /mcp:/);
  assert.match(text(el), /Up, last update 2 s ago/);
});

test('the body follows the graph live: a new fuel reading re-renders it', async () => {
  const m = mount();
  const el = await showLoaded(m, 'veh:Drone1');
  assert.match(text(fieldValue(el, 'Fuel')), /64\.0%/);
  const g = m.store.get().graph;
  const nodes2 = g.nodes.map((n) =>
    n.id === 'veh:Drone1' ? { ...n, attrs: { ...n.attrs, fuel_pct: 51.5 } } : n,
  );
  // The entity (fetched once) still says 64: the live node wins.
  m.store.change({ graph: { ...g, nodes: nodes2 } });
  assert.match(text(fieldValue(el, 'Fuel')), /51\.5%/);
  assert.match(text(fieldValue(el, 'Fuel')), /29\.5 points above/);
});

test('a mission shows its waypoint from graph v1.1 attrs.waypoint', async () => {
  const m = mount();
  m.store.get().graph.nodes[5].attrs.waypoint = { index: 11, of: 26 };
  const el = await showLoaded(m, 'msn:MSN-1a2b3c4d');
  assert.equal(text(fieldValue(el, 'Waypoint')), 'Waypoint 11 of 26');
});

test('the compact sheet keeps a waiting approval pinned at its top, with Review', async () => {
  const m = mount({ layout: 'compact' });
  m.bus.emit('approval:pending', {
    count: 1,
    oldest: { id: 'a1', title: 'Take off', vehicle: 'Drone1' },
  });
  const el = await showLoaded(m, 'veh:Drone1');
  const banner = byCls(el, 'ic-inspector__approval')[0];
  assert.ok(banner, 'pinned banner');
  assert.match(text(banner), /Approval waiting: Take off, Drone1\./);
  byKey(banner, 'approval:review').fire('click');
  assert.deepEqual(m.bus.last('approval:review'), {});
  assert.equal(isHidden(m.el), true, 'the sheet gives way to the slip');

  for (const layout of ['wide', 'narrow']) {
    const other = mount({ layout });
    other.bus.emit('approval:pending', { count: 2, oldest: { title: 'x' } });
    const sheet = await showLoaded(other, 'veh:Drone1');
    assert.equal(
      byCls(sheet, 'ic-inspector__approval').length,
      0,
      `${layout}: the plate never covers the analyst; narrow's bar pins it`,
    );
  }
});

// ---- review fixes -------------------------------------------------------------------

test('focus stays on the Related chip when the same entity also appears as a field chip', async () => {
  const m = mount();
  const el = await showLoaded(m, 'veh:Drone1');
  const chips = () =>
    findAll(el, (x) => x.attrs?.['data-key'] === 'chip:msn:MSN-1a2b3c4d');
  assert.equal(chips().length, 2, 'the mission is a field chip and Related');
  // The next poll brings a new fuel reading, so the body is rebuilt.
  const poll = (fuel) => {
    const g = m.store.get().graph;
    m.store.change({
      graph: {
        ...g,
        nodes: g.nodes.map((n) =>
          n.id === 'veh:Drone1'
            ? { ...n, attrs: { ...n.attrs, fuel_pct: fuel } }
            : n,
        ),
      },
    });
  };
  const related = chips()[1];
  related.focus();
  poll(63);
  assert.notEqual(chips()[1], related, 'the body was rebuilt');
  assert.equal(globalThis.document.activeElement, chips()[1]);
  chips()[0].focus();
  poll(62);
  assert.equal(globalThis.document.activeElement, chips()[0]);
});

test('a BINGO latched after the return has landed says Landed, not returning home', async () => {
  const m = mount();
  const g = m.store.get().graph;
  m.store.change({
    graph: {
      ...g,
      nodes: g.nodes.map((n) =>
        n.id === 'veh:Drone1'
          ? {
              ...n,
              subtitle: 'fuel 18% / BINGO 20% · BINGO latched · landed',
              attrs: {
                ...n.attrs,
                fuel_pct: 18,
                bingo_fuel_pct: 20,
                bingo_latched: true,
                landed: true,
                agl_m: 0,
              },
            }
          : n,
      ),
    },
  });
  const el = await showLoaded(m, 'veh:Drone1');
  assert.equal(text(fieldValue(el, 'State')), 'Landed');
  const fuel = text(fieldValue(el, 'Fuel'));
  assert.match(fuel, /Below BINGO\. Landed; BINGO latched\./);
  assert.doesNotMatch(fuel, /Returning home/);
});

test('the inspector region takes focus (F6 lands on it)', () => {
  const m = mount();
  assert.equal(m.el.attrs.tabindex, '-1');
});
