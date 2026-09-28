import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  MAP_SITES_DRAWN_MAX,
  createMapDock,
  dockSiteCounts,
  dockVehicles,
  mapDockModel,
} from './mapDock.js';

// ---- a stub document (uavDom's h() needs only createElement) ----------------------

function stubDom() {
  const doc = { activeElement: null };
  const mk = (tag) => {
    const el = {
      tag,
      tagName: String(tag).toUpperCase(),
      children: [],
      attrs: {},
      listeners: {},
      className: '',
      textContent: '',
      parent: null,
      append(...kids) {
        for (const k of kids) {
          if (k && typeof k === 'object') k.parent = this;
          this.children.push(k);
        }
      },
      replaceChildren(...kids) {
        this.children = [];
        this.append(...kids);
      },
      remove() {
        if (this.parent)
          this.parent.children = this.parent.children.filter((c) => c !== this);
        this.parent = null;
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
      fire(t) {
        for (const f of this.listeners[t] || []) f({ target: this });
      },
      focus() {
        doc.activeElement = this;
      },
    };
    return el;
  };
  doc.createElement = mk;
  return doc;
}

function withDom(fn) {
  const saved = globalThis.document;
  globalThis.document = stubDom();
  try {
    return fn();
  } finally {
    globalThis.document = saved;
  }
}

function textOf(el) {
  if (el == null || el === false) return '';
  if (typeof el !== 'object') return String(el);
  if (el.attrs && 'hidden' in el.attrs) return '';
  return [el.textContent || '', ...(el.children || []).map(textOf)]
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function all(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const kid of root.children || []) all(kid, pred, out);
  return out;
}

const hasClass = (cls) => (el) =>
  String(el.className || '')
    .split(/\s+/)
    .includes(cls);
const byClass = (root, cls) => all(root, hasClass(cls))[0] || null;
const isHidden = (el) => Boolean(el && 'hidden' in el.attrs);

const BBOX_5KM = [12.9491, 77.5715, 12.9941, 77.6176];
const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';

function graph({ sites = 41, siteNodes = 0, degraded = false } = {}) {
  const nodes = [
    { id: 'veh:Drone1', type: 'vehicle', label: 'Drone1' },
    { id: 'thr:dyn-blr', type: 'theater', label: 'Bengaluru centre' },
  ];
  for (let i = 0; i < siteNodes; i += 1)
    nodes.push({ id: `sit:x:node/${i}`, type: 'site' });
  return {
    nodes,
    meta: { sites: { total: sites, degraded, in_graph: siteNodes } },
  };
}

// ---- model --------------------------------------------------------------------------

test('the dock line reads "Map  {label}  {W × H} km" from the target', () => {
  const m = mapDockModel({
    target: { label: 'Bengaluru centre', bbox: BBOX_5KM },
    graph: graph(),
  });
  assert.deepEqual(m.line, ['Map', 'Bengaluru centre', '5.0 × 5.0 km']);
  assert.deepEqual(m.vehicles, ['Drone1']);
  assert.deepEqual(
    mapDockModel({ target: { label: null, bbox: null } }).line,
    ['Map'],
    'missing parts are hidden, never invented',
  );
});

test('site numbers: fetched from meta, drawn capped at the 150 billboards, or the overlay says', () => {
  assert.deepEqual(dockSiteCounts(graph({ sites: 41 })), {
    total: 41,
    drawn: 41,
    notDrawn: 0,
    degraded: false,
  });
  const many = dockSiteCounts(graph({ sites: 212 }));
  assert.equal(many.drawn, MAP_SITES_DRAWN_MAX);
  assert.equal(many.notDrawn, 62);
  assert.equal(
    dockSiteCounts(graph({ sites: 3, siteNodes: 5 })).total,
    5,
    'never fewer than the graph holds',
  );
  const reported = dockSiteCounts(graph({ sites: 41 }), {
    sites: { drawn: 30, total: 41 },
  });
  assert.equal(reported.drawn, 30);
  assert.equal(reported.notDrawn, 11);
  assert.equal(dockSiteCounts(null).total, 0);
});

test('the Sites row, the overflow line and the ODbL line follow what is drawn', () => {
  const on = mapDockModel({ target: {}, graph: graph({ sites: 212 }) });
  assert.equal(on.sitesRow, true);
  assert.equal(on.sitesLabel, 'Sites 150');
  assert.equal(on.notDrawnText, '62 more sites not drawn.');
  assert.equal(on.attribution, 'Map data: © OpenStreetMap contributors, ODbL.');
  const off = mapDockModel({
    target: {},
    graph: graph({ sites: 212 }),
    sitesOn: false,
  });
  assert.equal(off.attribution, null, 'no sites drawn, no attribution');
  assert.equal(off.notDrawnText, null);
  assert.equal(off.sitesLabel, 'Sites 212');
  const none = mapDockModel({ target: {}, graph: graph({ sites: 0 }) });
  assert.equal(none.sitesRow, false);
  assert.equal(none.attribution, null);
  assert.equal(
    mapDockModel({ target: {}, graph: graph({ degraded: true }) }).degradedText,
    'Map data feed down. Sites may be missing, not absent.',
  );
});

test('vehicle names are safe text, deduplicated and capped', () => {
  const g = {
    nodes: [
      { id: 'veh:A', type: 'vehicle', label: `${BIDI}A` },
      { id: 'veh:B', type: 'vehicle' },
      { id: 'veh:A2', type: 'vehicle', label: `${BIDI}A` },
      ...['C', 'D', 'E'].map((n) => ({
        id: `veh:${n}`,
        type: 'vehicle',
        label: n,
      })),
    ],
  };
  assert.deepEqual(dockVehicles(g), ['evilA', 'B', 'C', 'D']);
});

// ---- DOM ------------------------------------------------------------------------------

test('the dock renders the model as text, with Back, Track, Sites, Key and the ODbL line', () =>
  withDom(() => {
    const calls = [];
    const dock = createMapDock({
      onBack: () => calls.push(['back']),
      onTrack: (v) => calls.push(['track', v]),
      onSites: (on) => calls.push(['sites', on]),
    });
    assert.equal(dock.element.getAttribute('aria-label'), 'Map');
    assert.equal(isHidden(dock.element), true, 'hidden until the map opens');
    dock.element.removeAttribute('hidden'); // the shell shows it in the map
    dock.update(
      mapDockModel({
        target: { label: 'Bengaluru centre', bbox: BBOX_5KM },
        graph: graph({ sites: 212 }),
      }),
    );
    const line = byClass(dock.element, 'ic-mapdock__line');
    assert.equal(textOf(line), 'Map Bengaluru centre 5.0 × 5.0 km');
    assert.deepEqual(
      line.children.filter((c) => typeof c === 'string'),
      ['  ', '  '],
      'textContent reads "Map  Bengaluru centre  5.0 × 5.0 km"',
    );
    assert.equal(
      textOf(byClass(dock.element, 'ic-dock__backlabel')),
      'Back to console',
    );
    const text = textOf(dock.element);
    assert.match(text, /Track Drone1/);
    assert.match(text, /Show Sites 150/);
    assert.match(text, /62 more sites not drawn\./);
    assert.match(text, /Map data: © OpenStreetMap contributors, ODbL\./);
    assert.doesNotMatch(text, /·/, 'the console never writes a middle dot');

    dock.back.fire('click');
    const track = all(
      dock.element,
      (el) => el.tag === 'button' && /Track Drone1/.test(textOf(el)),
    )[0];
    track.fire('click');
    const box = byClass(dock.element, 'ic-mapdock__check');
    assert.equal(box.checked, true);
    box.checked = false;
    box.fire('change');
    assert.deepEqual(calls, [['back'], ['track', 'Drone1'], ['sites', false]]);
  }));

test('the Key lists every site glyph and word, then the unrecognised map item', () =>
  withDom(() => {
    const dock = createMapDock();
    const key = byClass(dock.element, 'ic-mapdock__key');
    assert.equal(key.tag, 'details');
    const rows = all(key, hasClass('ic-mapdock__keyrow'));
    assert.equal(rows.length, 14);
    assert.equal(rows[0].attrs['data-category'], 'airfield');
    assert.match(textOf(rows[10]), /Medical, protected/);
    assert.equal(textOf(rows[13]), 'Unrecognised map item');
    for (const row of rows.slice(0, 13)) {
      const glyph = byClass(row, 'ic-mapdock__glyph');
      assert.match(glyph.innerHTML, /^<svg class="ic-glyph"/);
    }
    assert.match(textOf(key), /a missing site is not an absent one/);
  }));

test('untrusted text: the dock label renders as text, with no element and no bidi control', () =>
  withDom(() => {
    const dock = createMapDock();
    dock.element.removeAttribute('hidden');
    dock.update(
      mapDockModel({
        target: { label: `${BIDI} ${XSS}`, bbox: BBOX_5KM },
        graph: {
          nodes: [{ id: 'veh:x', type: 'vehicle', label: XSS }],
          meta: {},
        },
      }),
    );
    const text = textOf(dock.element);
    assert.ok(text.includes(`evil ${XSS}`), 'the literal angle brackets show');
    assert.ok(text.includes(`Track ${XSS}`));
    assert.doesNotMatch(text, /[‪-‮⁦-⁩]/);
    assert.equal(
      all(dock.element, (el) => el.tag === 'img').length,
      0,
      'no img element is created',
    );
    assert.equal(
      all(dock.element, (el) => 'onerror' in (el.attrs || {})).length,
      0,
    );
    const label = byClass(dock.element, 'ic-mapdock__label');
    assert.equal(label.innerHTML, undefined, 'never set as markup');
  }));

test('rows without data hide; the Track buttons are rebuilt only when the fleet changes', () =>
  withDom(() => {
    const dock = createMapDock();
    const model = mapDockModel({
      target: { label: 'X', bbox: BBOX_5KM },
      graph: graph({ sites: 0 }),
    });
    dock.update(model);
    assert.equal(isHidden(byClass(dock.element, 'ic-mapdock__toggle')), true);
    assert.equal(
      isHidden(byClass(dock.element, 'ic-mapdock__attribution')),
      true,
    );
    assert.equal(isHidden(byClass(dock.element, 'ic-mapdock__note')), true);
    assert.equal(isHidden(byClass(dock.element, 'ic-mapdock__warn')), true);
    const actions = byClass(dock.element, 'ic-mapdock__actions');
    const first = actions.children[0];
    dock.update(
      mapDockModel({
        target: { label: 'Y', bbox: BBOX_5KM },
        graph: graph({ sites: 5, degraded: true }),
      }),
    );
    assert.equal(actions.children[0], first, 'same fleet, same button');
    assert.equal(isHidden(byClass(dock.element, 'ic-mapdock__warn')), false);
    assert.equal(dock.model.line[1], 'Y');
    dock.destroy();
    dock.update(model);
    assert.equal(dock.model.line[1], 'Y', 'a destroyed dock ignores updates');
  }));
