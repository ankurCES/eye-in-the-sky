import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ATTR_REASON,
  TIER,
  TYPE_FILTERS,
  applyTypeFilter,
  attrReason,
  createSearch,
  filterCounts,
  filterKeyOf,
  isQuestion,
  matchNode,
  outsideWords,
  rankNodes,
  siteCategoryCounts,
  typeWordOf,
} from './search.js';

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
const hasCls = (el, cls) =>
  String(el.className || '')
    .split(/\s+/)
    .includes(cls);
const byCls = (root, cls) => findAll(root, (el) => hasCls(el, cls));
const isHidden = (el) => 'hidden' in (el?.attrs || {});

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

function fakeOrb() {
  const calls = [];
  return {
    calls,
    filter: (pred) => calls.push(['filter', pred]),
    highlight: (ids, opts) => calls.push(['highlight', ids, opts]),
    select: (id) => calls.push(['select', id]),
    lastFilter() {
      return [...calls].reverse().find(([k]) => k === 'filter')?.[1];
    },
  };
}

// ---- fixture ----------------------------------------------------------------------

function node(id, type, label, extra = {}) {
  return {
    id,
    type,
    label,
    subtitle: '',
    status: 'ok',
    salience: 0.5,
    ts_ms: null,
    attrs: {},
    ...extra,
  };
}

function graph() {
  return {
    scope: 'theater',
    generated_at_ms: Date.UTC(2026, 8, 27, 14, 31, 5),
    nodes: [
      node('trk:T-3fa9c1', 'track', 'SA-6 battery', {
        subtitle: '2K12 Kub · probable · 2 sightings',
        status: 'warn',
        salience: 0.9,
        attrs: {
          threat: 'high',
          category: 'sam',
          duplicates: ['trk:T-77d0e2'],
        },
      }),
      node('trk:T-50ff01', 'track', 'Mobile SA site', {
        subtitle: 'possible · 1 sighting',
        status: 'unknown',
        salience: 0.4,
        attrs: { threat: 'not assessed' },
      }),
      node('trk:T-aa0001', 'track', 'Radar vehicle', {
        subtitle: 'possible · 1 sighting',
        status: 'unknown',
        salience: 0.5,
        attrs: { threat: 'not assessed', category: 'radar' },
      }),
      node('trk:T-bb0002', 'track', 'Utility truck', {
        subtitle: 'probable · 3 sightings',
        status: 'ok',
        salience: 0.3,
        attrs: { threat: 'low', category: 'logistics' },
      }),
      node('unit:sam:T-3fa9c1', 'unit', '3 x SA-6 Gainful', {
        status: 'warn',
        attrs: { threat: 'high' },
      }),
      node('veh:Drone1', 'vehicle', 'Drone1', {
        subtitle: 'fuel 64% / BINGO 22% · airborne',
        salience: 0.8,
      }),
      node('msn:MSN-1a2b3c4d', 'mission', 'Grid search · Drone1', {
        subtitle: 'executing · 42%',
        attrs: { phase: 'executing', vehicle: 'Drone1', kind: 'grid_search' },
      }),
      node('poi:default:North Field', 'poi', 'North Field', {
        attrs: { theater: 'default' },
      }),
      node('rpt:R-1', 'report', 'INTREP R-1', {
        subtitle: '4 contacts · 2 gaps',
        status: 'warn',
      }),
      node('alarm:7', 'alarm', 'Bingo', {
        subtitle: 'Drone1 reached BINGO',
        status: 'critical',
        attrs: { severity: 'critical', kind: 'bingo', vehicle: 'Drone1' },
      }),
    ],
    edges: [
      { a: 'trk:T-aa0001', b: 'poi:default:North Field', kind: 'near' },
      { a: 'trk:T-bb0002', b: 'poi:default:North Field', kind: 'near' },
    ],
    meta: {},
  };
}

function mount({ state = {}, orb = fakeOrb(), setScope } = {}) {
  const doc = stubDom();
  globalThis.document = doc;
  const st = {
    graph: graph(),
    status: 'live',
    lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 5),
    ...state,
  };
  const listeners = new Set();
  const store = {
    get: () => st,
    on: (e, cb) => (listeners.add(cb), () => listeners.delete(cb)),
    change(next) {
      Object.assign(st, next);
      for (const cb of listeners) cb({});
    },
  };
  if (setScope) store.setScope = setScope;
  const bus = fakeBus();
  const host = doc.createElement('div');
  const search = createSearch(host, { store, bus, orb }, { debounceMs: 0 });
  return { doc, store, bus, orb, host, search, input: search.input };
}

function type(input, value) {
  input.value = value;
  input.fire('input');
}

const options = (search) =>
  findAll(search.element, (el) => el.attrs?.role === 'option');
const selected = (search) =>
  options(search).find((o) => o.attrs['aria-selected'] === 'true');

// ---- ranking (pure) -------------------------------------------------------------------

test('an exact id outranks everything, bare or prefixed, including a merged duplicate', () => {
  const g = graph();
  for (const q of ['T-3fa9c1', 'trk:T-3fa9c1', 't-3FA9C1']) {
    const [top] = rankNodes(g, q);
    assert.equal(top.id, 'trk:T-3fa9c1', q);
    assert.equal(top.tier, TIER.exactId);
    assert.equal(top.reason, 'ID match');
  }
  assert.equal(
    rankNodes(g, 'T-77d0e2')[0].id,
    'trk:T-3fa9c1',
    'a duplicate id finds the contact it merged into',
  );
});

test('label prefix beats label word start beats partial id beats subtitle beats attributes', () => {
  const g = graph();
  const sa = rankNodes(g, 'sa');
  assert.equal(sa[0].id, 'trk:T-3fa9c1');
  assert.equal(sa[0].tier, TIER.labelPrefix);
  assert.deepEqual(sa[0].ranges, [[0, 2]]);
  const word = sa.find((m) => m.id === 'trk:T-50ff01');
  assert.equal(word.tier, TIER.labelWord);
  assert.deepEqual(word.ranges, [[7, 9]]);
  assert.ok(sa.indexOf(word) > 0);

  const partial = rankNodes(g, '3fa9');
  assert.equal(partial[0].tier, TIER.partialId);
  assert.equal(partial[0].reason, 'Partial ID');

  const kub = rankNodes(g, 'kub');
  assert.equal(kub[0].id, 'trk:T-3fa9c1');
  assert.equal(kub[0].tier, TIER.subtitle);
  assert.equal(
    kub[0].reason,
    'Equipment',
    'the first subtitle segment of a contact is its equipment',
  );

  assert.equal(rankNodes(g, 'sightings')[0].reason, 'Subtitle');
  const high = rankNodes(g, 'high');
  assert.ok(high.length >= 2);
  assert.ok(
    high.every((m) => m.tier === TIER.attribute && m.reason === 'Threat'),
  );
});

test('every query token at a word start is a label match with each token highlighted', () => {
  const [top] = rankNodes(graph(), 'sa battery');
  assert.equal(top.id, 'trk:T-3fa9c1');
  assert.equal(top.tier, TIER.labelWord);
  assert.deepEqual(top.ranges, [
    [0, 2],
    [5, 12],
  ]);
  const [truck] = rankNodes(graph(), 'truck util');
  assert.equal(truck.id, 'trk:T-bb0002');
  assert.equal(truck.tier, TIER.labelWord);
  assert.deepEqual(truck.ranges, [
    [8, 13],
    [0, 4],
  ]);
});

test('contacts near a matching place carry the reason "Near North Field"', () => {
  const ranked = rankNodes(graph(), 'north field');
  assert.equal(ranked[0].id, 'poi:default:North Field');
  const near = ranked
    .filter((m) => m.reason === 'Near North Field')
    .map((m) => m.id);
  assert.deepEqual(near.sort(), ['trk:T-aa0001', 'trk:T-bb0002']);
});

test('ties go to critical first, then salience, then recency', () => {
  const g = {
    nodes: [
      node('a:1', 'track', 'Alpha one', { status: 'warn', salience: 0.9 }),
      node('a:2', 'track', 'Alpha two', { status: 'critical', salience: 0.1 }),
      node('a:3', 'track', 'Alpha three', {
        status: 'warn',
        salience: 0.9,
        ts_ms: 5,
      }),
    ],
    edges: [],
  };
  assert.deepEqual(
    rankNodes(g, 'alpha').map((m) => m.id),
    ['a:2', 'a:3', 'a:1'],
  );
});

test('single characters only match ids and label starts; unknown graphs rank nothing', () => {
  assert.equal(matchNode(node('x:1', 'track', 'Kilo'), 'i'), null);
  assert.ok(matchNode(node('x:1', 'track', 'Kilo'), 'k'));
  assert.deepEqual(rankNodes(null, 'sa'), []);
  assert.deepEqual(rankNodes(graph(), '   '), []);
});

test('questions and taskings are recognised', () => {
  assert.ok(isQuestion('which contacts near North Field are unassessed?'));
  assert.ok(isQuestion('Plan a grid search over North Field'));
  assert.ok(isQuestion('drone1 fuel?'));
  assert.ok(isQuestion('Summarize the situation'));
  assert.ok(!isQuestion('sa-6'));
  assert.ok(!isQuestion('Drone1'));
  assert.ok(!isQuestion(''));
});

test('filter counts and type filters', () => {
  const ranked = rankNodes(graph(), 'drone1');
  const counts = filterCounts(ranked);
  assert.equal(counts.all, ranked.length);
  assert.equal(counts.vehicles, 1);
  assert.equal(counts.missions, 1);
  assert.equal(counts.alarms, 1);
  assert.deepEqual(
    applyTypeFilter(ranked, 'vehicles').map((m) => m.id),
    ['veh:Drone1'],
  );
});

// ---- component --------------------------------------------------------------------------

test('focused and empty: type filters and the hint, no results, no filter', () => {
  const { search, bus, input } = mount();
  input.fire('focus');
  const pop = byCls(search.element, 'ic-search__pop')[0];
  assert.ok(!isHidden(pop));
  assert.equal(input.attrs['aria-expanded'], 'true');
  const chips = byCls(search.element, 'ic-search__chip').map(text);
  assert.deepEqual(chips, [
    'All',
    'Contacts',
    'Vehicles',
    'Missions',
    'Places',
    'Reports',
    'Alarms',
  ]);
  assert.match(text(pop), /Try a callsign, a track id, or a question\./);
  assert.equal(options(search).length, 0);
  assert.equal(
    input.attrs.placeholder,
    'Search the picture, or ask a question',
  );
  assert.equal(
    bus.all('search:filter').length,
    0,
    'an empty query filters nothing',
  );
});

test('typing ranks live, filters the orb and emits search:filter with counts on the chips', () => {
  const { search, bus, orb, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  const payload = bus.last('search:filter');
  assert.equal(payload.source, 'search');
  assert.equal(payload.query, 'sa');
  assert.ok(payload.ids.includes('trk:T-3fa9c1'));
  assert.ok(payload.ids.includes('unit:sam:T-3fa9c1'));
  const pred = orb.lastFilter();
  assert.equal(pred({ id: 'trk:T-3fa9c1' }), true);
  assert.equal(pred({ id: 'veh:Drone1' }), false);
  assert.equal(pred('trk:T-50ff01'), true, 'the predicate accepts an id too');

  const chips = byCls(search.element, 'ic-search__chip').map(text);
  assert.ok(chips[0].startsWith('All '));
  assert.ok(chips.some((c) => /^Contacts \d+$/.test(c)));
  assert.ok(
    !chips.some((c) => c.startsWith('Alarms')),
    'empty types are not offered',
  );

  const rows = options(search);
  const first = rows[0];
  assert.equal(first.attrs['data-id'], 'trk:T-3fa9c1');
  assert.equal(
    first.attrs['aria-selected'],
    'true',
    'the top hit is preselected',
  );
  assert.equal(input.attrs['aria-activedescendant'], first.attrs.id);
  assert.match(text(first), /SA-6 battery/);
  assert.match(text(first), /Contact/);
  assert.match(text(first), /2K12 Kub, probable, 2 sightings/);
  assert.match(text(first), /High/);
  assert.match(text(first), /Label/);
  assert.match(text(first), /trk:T-3fa9c1/);
  assert.equal(
    text(byCls(first, 'ic-search__hit')[0]),
    'SA',
    'matched characters in weight 700',
  );
  const unassessed = rows.find((r) => r.attrs['data-id'] === 'trk:T-50ff01');
  assert.equal(
    text(byCls(unassessed, 'ic-kit-notassessed')[0]),
    'Not assessed',
  );
  assert.equal(unassessed.attrs['data-tone'], 'unknown');
  assert.equal(rows.at(-1).attrs['data-kind'], 'ask', 'the ask row is last');
  assert.match(text(rows.at(-1)), /Ask the analyst: "sa"/);
  assert.match(
    text(byCls(search.element, 'ic-search__foot')[0]),
    /Show all \d+ on the orb/,
  );
});

test('arrow keys move the selection; Enter selects and inspects, then clears the live filter', () => {
  const { search, bus, orb, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  input.fire('keydown', { key: 'ArrowDown' });
  const second = selected(search);
  assert.equal(options(search).indexOf(second), 1);
  input.fire('keydown', { key: 'ArrowUp' });
  input.fire('keydown', { key: 'ArrowUp' });
  assert.equal(
    selected(search).attrs['data-kind'],
    'ask',
    'wraps to the last option',
  );
  input.fire('keydown', { key: 'ArrowDown' });
  const id = selected(search).attrs['data-id'];
  assert.equal(id, 'trk:T-3fa9c1');
  input.fire('keydown', { key: 'Enter' });
  assert.deepEqual(bus.last('inspect'), { id });
  assert.ok(orb.calls.some(([k, v]) => k === 'select' && v === id));
  assert.ok(isHidden(byCls(search.element, 'ic-search__pop')[0]));
  assert.deepEqual(bus.last('search:filter'), {
    ids: null,
    query: '',
    source: 'search',
  });
  assert.equal(orb.lastFilter(), null);
});

test('a question puts the ask row first and preselected; Enter asks with the top matches in view', () => {
  const { search, bus, input } = mount();
  input.fire('focus');
  type(input, 'which contacts near North Field are unassessed?');
  const rows = options(search);
  assert.equal(rows[0].attrs['data-kind'], 'ask');
  assert.equal(rows[0].attrs['aria-selected'], 'true');
  input.fire('keydown', { key: 'Enter' });
  const ask = bus.last('ask');
  assert.equal(ask.text, 'which contacts near North Field are unassessed?');
  assert.equal(
    ask.focused_ids,
    undefined,
    'no matches, so no context is invented',
  );
  assert.ok(
    isHidden(byCls(search.element, 'ic-search__pop')[0]),
    'asking closes the bar',
  );
});

test('asking sends at most the top ten matches as context', () => {
  const { bus, input, store } = mount();
  const g = graph();
  for (let i = 0; i < 14; i += 1)
    g.nodes.push(node(`trk:T-k${i}`, 'track', `Kilo ${i}`));
  store.change({ graph: g });
  input.fire('focus');
  type(input, 'kilo');
  input.fire('keydown', { key: 'Enter', ctrlKey: true });
  const ask = bus.last('ask');
  assert.equal(ask.focused_ids.length, 10);
  assert.equal(ask.focused_ids[0], 'trk:T-k0');
});

test('⌘Enter or Shift+Enter asks even when the top row is a result', () => {
  const { bus, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  input.fire('keydown', { key: 'Enter', metaKey: true });
  const ask = bus.last('ask');
  assert.equal(ask.text, 'sa');
  assert.equal(ask.focused_ids[0], 'trk:T-3fa9c1');
  assert.equal(bus.all('inspect').length, 0);

  const second = mount();
  second.input.fire('focus');
  type(second.input, 'drone1');
  second.input.fire('keydown', { key: 'Enter', shiftKey: true });
  assert.equal(second.bus.last('ask').text, 'drone1');
});

test('no match: the copy deck line, the ask row first, and "Search all theaters" when scope can widen', () => {
  let scope = null;
  const { search, input } = mount({ setScope: (v) => (scope = v) });
  input.fire('focus');
  type(input, 'sa-11');
  const pop = byCls(search.element, 'ic-search__pop')[0];
  assert.match(text(pop), /Nothing on the picture matches "sa-11"\./);
  const rows = options(search);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].attrs['data-kind'], 'ask');
  assert.equal(rows[0].attrs['aria-selected'], 'true');
  const all = findAll(
    pop,
    (el) => el.attrs?.['data-key'] === 'search:all-theaters',
  )[0];
  assert.match(text(all), /Search all theaters/);
  all.fire('click');
  assert.equal(scope, 'all');
});

test('graph never loaded: explains, and the ask row still works', () => {
  const { search, bus, input } = mount({
    state: { graph: null, status: 'loading', lastLiveAt: null },
  });
  input.fire('focus');
  const pop = byCls(search.element, 'ic-search__pop')[0];
  assert.match(
    text(pop),
    /Search needs the intel picture, which hasn't loaded yet\./,
  );
  type(input, 'what is up?');
  assert.equal(options(search)[0].attrs['data-kind'], 'ask');
  input.fire('keydown', { key: 'Enter' });
  assert.deepEqual(bus.last('ask'), { text: 'what is up?' });
});

test('a stale or offline picture captions results with its time', () => {
  const { search, input } = mount({ state: { status: 'offline' } });
  input.fire('focus');
  type(input, 'sa');
  assert.match(
    text(search.element),
    /From the picture at 14:31:05Z, not live\./,
  );
  assert.ok(options(search).length > 1, 'results still show');
});

test('analyst unavailable disables the ask row with the reason, and asking does nothing', () => {
  const { search, bus, input } = mount();
  bus.emit('analyst:availability', { available: false, reason: 'auth' });
  input.fire('focus');
  type(input, 'where is drone1?');
  const ask = options(search)[0];
  assert.equal(ask.attrs['aria-disabled'], 'true');
  assert.match(text(ask), /The analyst isn't signed in\./);
  input.fire('keydown', { key: 'Enter' });
  assert.equal(bus.all('ask').length, 0);
  bus.emit('analyst:availability', { available: true });
  type(input, 'where is drone1?');
  assert.equal(options(search)[0].attrs['aria-disabled'], undefined);
});

test('type filter chips narrow the results and the live filter', () => {
  const { search, bus, input } = mount();
  input.fire('focus');
  type(input, 'drone1');
  const chip = byCls(search.element, 'ic-search__chip').find((c) =>
    text(c).startsWith('Missions'),
  );
  chip.fire('click');
  assert.deepEqual(bus.last('search:filter').ids, ['msn:MSN-1a2b3c4d']);
  const pressed = byCls(search.element, 'ic-search__chip').find(
    (c) => c.attrs['aria-pressed'] === 'true',
  );
  assert.match(text(pressed), /^Missions 1$/);
  assert.equal(options(search)[0].attrs['data-id'], 'msn:MSN-1a2b3c4d');
});

test('"Show all N on the orb" keeps the filter as a pill; × clears it', () => {
  const { search, bus, orb, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  const count = bus.last('search:filter').ids.length;
  const show = findAll(
    search.element,
    (el) => el.attrs?.['data-key'] === 'search:show-all',
  )[0];
  assert.equal(text(show), `Show all ${count} on the orb`);
  show.fire('click');
  assert.ok(isHidden(byCls(search.element, 'ic-search__pop')[0]));
  const pill = byCls(search.element, 'ic-search__pill')[0];
  assert.ok(!isHidden(pill));
  assert.equal(
    text(byCls(pill, 'ic-search__pill-text')[0]),
    `Filtered: sa, ${count} shown`,
  );
  assert.equal(
    bus.last('search:filter').ids.length,
    count,
    'the filter persists after closing',
  );
  assert.equal(typeof orb.lastFilter(), 'function');

  byCls(pill, 'ic-search__pill-clear')[0].fire('click');
  assert.ok(isHidden(pill));
  assert.deepEqual(bus.last('search:filter'), {
    ids: null,
    query: '',
    source: 'search',
  });
});

test('Escape clears the query first, then closes', () => {
  const { search, bus, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  input.fire('keydown', { key: 'Escape' });
  assert.equal(input.value, '');
  assert.equal(search.query(), '');
  assert.ok(
    !isHidden(byCls(search.element, 'ic-search__pop')[0]),
    'still open after the first Escape',
  );
  assert.equal(bus.last('search:filter').ids, null);
  input.fire('keydown', { key: 'Escape' });
  assert.ok(isHidden(byCls(search.element, 'ic-search__pop')[0]));
});

test('a filter persisted elsewhere (the rail) shows as a pill; its clear reaches the orb', () => {
  const { search, bus, orb } = mount();
  bus.emit('search:filter', {
    ids: ['alarm:7'],
    query: 'Alarms',
    source: 'situation',
  });
  const pill = byCls(search.element, 'ic-search__pill')[0];
  assert.equal(
    text(byCls(pill, 'ic-search__pill-text')[0]),
    'Filtered: Alarms, 1 shown',
  );
  byCls(pill, 'ic-search__pill-clear')[0].fire('click');
  assert.equal(orb.lastFilter(), null);
  assert.ok(isHidden(pill));
});

test('hovering a result highlights it in operator ink without re-rendering the row', () => {
  const { search, orb, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  const rows = options(search);
  rows[1].fire('mouseenter');
  assert.equal(
    options(search)[1],
    rows[1],
    'the same element stays under the pointer',
  );
  assert.equal(rows[1].attrs['aria-selected'], 'true');
  const hl = orb.calls.filter(([k]) => k === 'highlight').at(-1);
  assert.deepEqual(hl, [
    'highlight',
    [rows[1].attrs['data-id']],
    { by: 'operator' },
  ]);
  const md = rows[1].fire('mousedown');
  assert.ok(md.defaultPrevented, 'mousedown keeps focus in the input');
  rows[1].fire('click');
  assert.ok(isHidden(byCls(search.element, 'ic-search__pop')[0]));
});

test('graph changes re-rank an open query', () => {
  const { search, store, input } = mount();
  input.fire('focus');
  type(input, 'sa-7');
  assert.equal(options(search).length, 1, 'only the ask row');
  const g = graph();
  g.nodes.push(
    node('trk:T-new', 'track', 'SA-7 launcher', { status: 'critical' }),
  );
  store.change({ graph: g });
  assert.equal(options(search)[0].attrs['data-id'], 'trk:T-new');
});

test('destroy detaches and stops listening', () => {
  const { search, host, bus } = mount();
  search.destroy();
  assert.equal(host.children.length, 0);
  bus.emit('search:filter', { ids: ['x'], query: 'x', source: 'situation' });
  assert.ok(isHidden(byCls(search.element, 'ic-search__pill')[0]));
});

// ---- JS-PANELS-POLISH ----------------------------------------------------------

test("feed rows use the orb's name and short state, never the server plumbing", () => {
  const g = graph();
  g.nodes.push(
    node('feed:contacts', 'feed', 'Contacts', {
      subtitle: 'mcp:uav_list_tracks',
      ts_ms: Date.now() - 1000,
      attrs: { ok: true },
    }),
  );
  const { search, input } = mount({ state: { graph: g } });
  type(input, 'contacts feed');
  const rows = options(search).filter((o) => o.attrs['data-kind'] === 'result');
  const row = rows.find((r) => r.attrs['data-id'] === 'feed:contacts');
  assert.ok(row, 'the feed matches its console name');
  assert.match(text(row), /Contacts feed/);
  assert.match(text(row), /Up/);
  assert.doesNotMatch(text(row), /mcp:|uav_list_tracks/);
});

test('matches outside the active theater carry a word, and plumbing segments are dropped', () => {
  assert.equal(
    outsideWords({ attrs: { out_of_theater: true } }),
    'Outside this theater',
  );
  assert.equal(
    outsideWords({ attrs: { outside_ao: true } }),
    'Just outside the AO',
  );
  assert.equal(outsideWords({ attrs: {} }), '');
  const g = graph();
  g.nodes.push(
    node('poi:ukraine-donbas:Donbas Center', 'poi', 'Donbas Center', {
      subtitle: 'Ukraine — Donbas',
      attrs: { theater: 'ukraine-donbas', out_of_theater: true },
    }),
  );
  const { search, input } = mount({ state: { graph: g } });
  type(input, 'donbas');
  const row = options(search).find(
    (o) => o.attrs['data-id'] === 'poi:ukraine-donbas:Donbas Center',
  );
  assert.match(text(row), /Outside this theater/);
});

// ---- focus inside the popover survives re-renders (a11y review) -------------------

/** Chrome fires focusout (relatedTarget null) while replaceChildren drops the
 *  focused control, before it is detached: the stub does the same. */
function chromeLikeFocusout(search, doc, cls) {
  const el = byCls(search.element, cls)[0];
  const orig = el.replaceChildren.bind(el);
  el.replaceChildren = (...kids) => {
    if (el.contains(doc.activeElement) && doc.activeElement !== el)
      search.element.fire('focusout', {
        target: doc.activeElement,
        relatedTarget: null,
      });
    orig(...kids);
  };
}

test('a type chip keeps the search open, the query and focus on the same chip, through clicks and polls', () => {
  const { search, doc, input, store } = mount();
  chromeLikeFocusout(search, doc, 'ic-search__chips');
  chromeLikeFocusout(search, doc, 'ic-search__foot');
  input.fire('focus');
  type(input, 'drone1');
  const pop = byCls(search.element, 'ic-search__pop')[0];
  const chip = () =>
    byCls(search.element, 'ic-search__chip').find(
      (c) => c.attrs['data-filter'] === 'missions',
    );
  const first = chip();
  first.focus();
  first.fire('click');
  assert.ok(!isHidden(pop), 'still open after pressing the chip');
  assert.equal(search.query(), 'drone1');
  assert.equal(input.value, 'drone1');
  assert.notEqual(chip(), first, 'the chips were rebuilt');
  assert.equal(doc.activeElement, chip(), 'focus is on the rebuilt chip');
  assert.equal(chip().attrs['aria-pressed'], 'true');

  // The 2 s poll re-renders with focus parked on the chip.
  store.change({ lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 7) });
  assert.ok(!isHidden(pop), 'a poll never closes it');
  assert.equal(search.query(), 'drone1');
  assert.equal(doc.activeElement?.attrs['data-filter'], 'missions');

  // Same for "Show all N on the orb".
  const showAll = () =>
    findAll(
      search.element,
      (el) => el.attrs?.['data-key'] === 'search:show-all',
    )[0];
  showAll().focus();
  store.change({ lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 9) });
  assert.ok(!isHidden(pop));
  assert.equal(doc.activeElement, showAll());

  // A real exit (focus to another control outside) still closes it.
  search.element.fire('focusout', { relatedTarget: doc.body });
  assert.ok(isHidden(pop));
});

test('"Show all" by keyboard moves focus to the kept filter\'s clear button', () => {
  const { search, doc, input } = mount();
  input.fire('focus');
  type(input, 'sa');
  const show = findAll(
    search.element,
    (el) => el.attrs?.['data-key'] === 'search:show-all',
  )[0];
  show.focus();
  show.fire('click');
  assert.equal(doc.activeElement?.attrs['data-key'], 'search:pill-clear');
});

test("the kept filter's clear keeps focus through a poll that rebuilds the pill", () => {
  const { search, doc, input, store } = mount();
  input.fire('focus');
  type(input, 'sa');
  findAll(
    search.element,
    (el) => el.attrs?.['data-key'] === 'search:show-all',
  )[0].fire('click');
  const clear = () => byCls(search.element, 'ic-search__pill-clear')[0];
  const first = clear();
  first.focus();
  store.change({ lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 7) });
  assert.notEqual(clear(), first, 'the poll rebuilt the pill');
  assert.equal(doc.activeElement, clear(), 'focus is on the rebuilt clear');
  assert.ok(isHidden(byCls(search.element, 'ic-search__pop')[0]));
});

test('the match count is announced when it changes, not on every poll', () => {
  const { search, input, store } = mount();
  const live = findAll(search.element, (el) => el.attrs?.role === 'status')[0];
  const said = [];
  const orig = live.replaceChildren.bind(live);
  live.replaceChildren = (...kids) => {
    said.push(kids.map(text).join(''));
    orig(...kids);
  };
  input.fire('focus');
  type(input, 'drone1');
  assert.equal(said.length, 1);
  store.change({ lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 7) });
  store.change({ lastLiveAt: Date.UTC(2026, 8, 27, 14, 31, 9) });
  assert.equal(said.length, 1, 'polls with the same answer stay quiet');
  // A new query is a new answer, even with the same count.
  type(input, 'drone');
  assert.equal(said.length, 2);
  assert.equal(said[1], said[0]);
});

test('an alarm result reads by its kind label, as the orb and the rail do', () => {
  const { search, input } = mount();
  input.fire('focus');
  type(input, 'alarm:7');
  const row = options(search).find((o) => o.attrs['data-id'] === 'alarm:7');
  assert.match(text(byCls(row, 'ic-search__label')[0]), /^BINGO fuel$/);
});

// ---- WG §4.2.6: sites in search; §4.2.1: unrecognised types ---------------------------------

const XSS = '<img src=x onerror=alert(1)>';

function siteNode(id, label, category, extra = {}) {
  return node(`sit:dyn-k:${id}`, 'site', label, {
    subtitle: 'Mapped, not verified',
    salience: 0.4,
    attrs: { category, source: 'osm', register: 'mapped', tags: {} },
    ...extra,
  });
}

function placesGraph() {
  const g = graph();
  g.nodes.push(
    siteNode('way/1', 'Kherson International Airport', 'airfield', {
      attrs: {
        category: 'airfield',
        subtype: 'aeroway=aerodrome',
        tags: { icao: 'UKOH', aeroway: 'aerodrome' },
        tags_total: 14,
        fetched_at_ms: 1727500000123,
        source: 'osm',
      },
    }),
    siteNode('way/2', 'Chornobaivka airfield', 'airfield'),
    siteNode('node/3', 'City hospital', 'medical', {
      attrs: { category: 'medical', protected: true, tags: {} },
    }),
    siteNode('way/4', 'Kherson port', 'port'),
    node('thr:dyn-k', 'theater', 'Kherson', {
      attrs: { place: 'Kherson, Ukraine', active: true },
    }),
    node('frc:red-sam-1', 'force', 'Kherson red SAM', { status: 'ok' }),
  );
  return g;
}

test('Places covers theaters, POIs and sites; an unknown type is under All only', () => {
  const places = TYPE_FILTERS.find((f) => f.key === 'places');
  assert.deepEqual(places.types, ['theater', 'poi', 'site']);
  assert.equal(filterKeyOf('site'), 'places');
  assert.equal(filterKeyOf('force'), null);
  const matches = rankNodes(placesGraph(), 'kherson');
  const counts = filterCounts(matches);
  assert.equal(counts.places, 3, 'two sites and the theater');
  assert.equal(counts.all, 4, 'the unknown force counts under All');
  assert.equal(typeWordOf('site'), 'Site');
  assert.equal(typeWordOf('force'), 'Unrecognised (force)');
});

test('site matches: category words and tags, with type-keyed reasons', () => {
  const g = placesGraph();
  const byId = (id) => g.nodes.find((n) => n.id === id);
  const airport = byId('sit:dyn-k:way/1');
  assert.deepEqual(matchNode(airport, 'ukoh'), {
    tier: TIER.attribute,
    reason: 'Tags',
    ranges: [],
  });
  assert.equal(matchNode(airport, 'airfield').reason, 'Category');
  assert.equal(
    matchNode(byId('sit:dyn-k:node/3'), 'medical').reason,
    'Category',
  );
  assert.equal(matchNode(airport, 'aerodrome').reason, 'Category');
  // Plumbing never matches: source "osm", the fetch time, the tag count.
  assert.equal(matchNode(airport, 'osm'), null);
  // Its theater's id inside the site id is not a partial-id match.
  assert.equal(matchNode(byId('sit:dyn-k:way/2'), 'dyn-k'), null);
  assert.equal(
    matchNode(byId('sit:dyn-k:way/2'), 'way/2').reason,
    'Partial ID',
  );
  assert.equal(matchNode(airport, '1727500000'), null);
  // A unit's category still reads "Equipment".
  assert.equal(attrReason('unit', 'category'), 'Equipment');
  assert.equal(attrReason('site', 'category'), 'Category');
  assert.equal(attrReason('site', 'kind'), 'Kind', 'shared reasons apply');
  assert.equal(attrReason('constructor', 'toString'), 'Details');
  assert.ok(Object.isFrozen(ATTR_REASON.site));
});

test('new question words read as taskings: set, go, recce, move, change', () => {
  for (const q of [
    'set the theater to Kherson',
    'go to Odesa',
    'recce the port',
    'move the AO north',
    'change sim speed',
  ])
    assert.equal(isQuestion(q), true, q);
  assert.equal(isQuestion('settlement'), false, 'whole first word only');
});

test('siteCategoryCounts and the category filter within Places', () => {
  const matches = rankNodes(placesGraph(), 'k');
  const all = rankNodes(placesGraph(), 'kherson');
  assert.deepEqual(siteCategoryCounts(all), [
    ['airfield', 1],
    ['port', 1],
  ]);
  assert.deepEqual(siteCategoryCounts(matches.slice(0, 0)), []);
  const onlyPorts = applyTypeFilter(all, 'places', 'port');
  assert.deepEqual(
    onlyPorts.map((m) => m.id),
    ['sit:dyn-k:way/4'],
  );
  assert.equal(
    applyTypeFilter(all, 'contacts', 'port').length,
    0,
    'the category applies only under Places',
  );
});

test('a site row reads "Site  Airfield  Mapped, not verified  ICAO UKOH"', () => {
  const { search, input } = mount({ state: { graph: placesGraph() } });
  input.fire('focus');
  type(input, 'kherson international');
  const row = options(search).find(
    (o) => o.attrs['data-id'] === 'sit:dyn-k:way/1',
  );
  assert.ok(row);
  assert.equal(row.attrs['data-type'], 'site');
  assert.equal(row.attrs['data-tone'], 'neutral');
  const meta = byCls(row, 'ic-search__meta')[0];
  assert.equal(
    meta.children.filter(Boolean).map(text).join('  '),
    'Site  Airfield  Mapped, not verified  ICAO UKOH',
  );
  const g = findAll(row, (e) => hasCls(e, 'ic-kit-glyph'))[0];
  assert.equal(g.attrs['data-type'], 'site');
});

test('Places shows category chips when sites match; a chip narrows to that category', () => {
  const { search, input, bus } = mount({ state: { graph: placesGraph() } });
  input.fire('focus');
  type(input, 'kherson');
  const siteChips = () =>
    byCls(search.element, 'ic-search__chip--site').map((c) => text(c));
  assert.deepEqual(siteChips(), [], 'not under All');
  byCls(search.element, 'ic-search__chip')
    .find((c) => text(c).startsWith('Places'))
    .fire('click');
  assert.deepEqual(siteChips(), ['Airfields 1', 'Ports 1']);
  byCls(search.element, 'ic-search__chip--site')
    .find((c) => text(c) === 'Ports 1')
    .fire('click');
  assert.deepEqual(bus.last('search:filter').ids, ['sit:dyn-k:way/4']);
  const pressed = byCls(search.element, 'ic-search__chip--site').find(
    (c) => c.attrs['aria-pressed'] === 'true',
  );
  assert.equal(text(pressed), 'Ports 1');
  // Clicking it again lets go.
  pressed.fire('click');
  assert.equal(bus.last('search:filter').ids.length, 3);
});

test('an unrecognised type lists under All as "Unrecognised (force)", lilac, never green', () => {
  const { search, input } = mount({ state: { graph: placesGraph() } });
  input.fire('focus');
  type(input, 'red sam');
  const row = options(search).find(
    (o) => o.attrs['data-id'] === 'frc:red-sam-1',
  );
  assert.ok(row);
  assert.equal(row.attrs['data-type'], 'unknown');
  assert.equal(row.attrs['data-tone'], 'unknown');
  assert.equal(text(byCls(row, 'ic-search__type')[0]), 'Unrecognised (force)');
  assert.match(text(row), /Not assessed/);
  const g = findAll(row, (e) => hasCls(e, 'ic-kit-glyph'))[0];
  assert.doesNotMatch(String(g.innerHTML), /#5DD39B/i);
});

test('XSS and bidi fixtures in a site name and tags render as text in search results (§3.11)', () => {
  const g = placesGraph();
  g.nodes.push(
    siteNode('way/9', `‮${XSS}‬`, 'airfield', {
      attrs: { category: 'airfield', tags: { icao: XSS } },
    }),
  );
  const { search, input } = mount({ state: { graph: g } });
  input.fire('focus');
  type(input, 'img');
  const row = options(search).find(
    (o) => o.attrs['data-id'] === 'sit:dyn-k:way/9',
  );
  assert.ok(row, 'the hostile site is listed');
  assert.equal(findAll(search.element, (e) => e.tag === 'img').length, 0);
  assert.equal(
    findAll(search.element, (e) =>
      Object.keys(e.attrs || {}).some((k) => /^on/i.test(k)),
    ).length,
    0,
  );
  const label = text(byCls(row, 'ic-search__label')[0]);
  assert.ok(label.includes(XSS));
  assert.doesNotMatch(label, /[‪-‮⁦-⁩]/);
  assert.match(text(row), /ICAO <img src=x…/);
});
