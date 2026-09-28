import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SITE_TAGS_SHOWN,
  airframeText,
  geocoderText,
  isPlaceType,
  placeMapRequest,
  placeMarkup,
  polygonBbox,
  recceText,
  siteBody,
  siteCategoryLine,
  siteHeader,
  siteIsProtected,
  siteKeyTag,
  siteSourceLine,
  siteTagPairs,
  theaterFacts,
  unknownBody,
  unknownTitle,
} from './inspectorPlaces.js';

const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';
const BIDI_RE = /[‪-‮⁦-⁩]/;

// A minimal stub document and kit: the bodies only need h() and the builders.
function stubDoc() {
  const mk = (tag) => ({
    tag,
    attrs: {},
    children: [],
    listeners: {},
    className: '',
    append(...kids) {
      this.children.push(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
  });
  return { createElement: mk };
}

const text = (el) =>
  el == null
    ? ''
    : typeof el !== 'object'
      ? String(el)
      : (el.children || []).map(text).join('');

function kit() {
  const rows = [];
  return {
    rows,
    field: (label, ...value) => {
      const row = { tag: 'row', label, children: value.flat().filter(Boolean) };
      rows.push(row);
      return row;
    },
    fields: (...r) => ({ tag: 'dl', children: r.filter(Boolean) }),
    mono: (t) => ({ tag: 'mono', children: [t] }),
    caption: (t) => ({ tag: 'caption', children: [t] }),
    chip: (id, label) => ({ tag: 'chip', id, children: [label || id] }),
    chips: (list) => ({ tag: 'chips', children: list }),
    section: (title, ...kids) => ({
      tag: 'section',
      title,
      children: kids.flat(),
    }),
    isOpen: () => false,
    toggle: () => {},
  };
}

test('pure words: markup, recce prefill, category, source, key tag, protected', () => {
  assert.equal(placeMarkup('sit:a', 'A [b] | c'), '[[sit:a|A b c]]');
  assert.equal(placeMarkup('sit:a', BIDI), '[[sit:a|evil]]');
  assert.equal(placeMarkup('sit:a', ''), '[[sit:a]]');
  assert.equal(
    recceText('thr:dyn-k', 'Kherson'),
    'Plan a route recon over [[thr:dyn-k|Kherson]] and show me the dry run.',
  );
  assert.equal(isPlaceType('site'), true);
  assert.equal(isPlaceType('theater'), true);
  assert.equal(isPlaceType('poi'), false);
  assert.equal(
    siteCategoryLine('airfield', 'aeroway=aerodrome'),
    'Airfield (aeroway=aerodrome)',
  );
  assert.equal(siteCategoryLine('volcano', ''), 'Mapped site');
  assert.equal(
    siteSourceLine(Date.UTC(2026, 8, 27, 9, 5)),
    'OpenStreetMap contributors, ODbL. Fetched 09:05Z via Overpass.',
  );
  assert.equal(
    siteSourceLine(null),
    'OpenStreetMap contributors, ODbL. Fetched via Overpass.',
  );
  assert.equal(
    siteKeyTag({ attrs: { tags: { iata: 'KHE', icao: 'UKOH' } } }),
    'ICAO UKOH',
  );
  assert.equal(siteKeyTag({ attrs: { tags: { iata: 'KHE' } } }), 'IATA KHE');
  assert.equal(siteKeyTag({ attrs: {} }), '');
  assert.equal(siteIsProtected({ attrs: { category: 'medical' } }), true);
  assert.equal(
    siteIsProtected({ attrs: { category: 'airfield', protected: true } }),
    true,
  );
  assert.equal(siteIsProtected({ attrs: { category: 'airfield' } }), false);
  assert.deepEqual(siteHeader({ attrs: { category: 'medical' } }, null), {
    category: 'medical',
    categoryWord: 'Medical, protected',
    status: 'Mapped, not verified',
    protected: true,
  });
  assert.equal(unknownTitle('force'), 'Unrecognised item (force)');
  assert.equal(unknownTitle(BIDI), 'Unrecognised item (evil)');
});

test('siteTagPairs: the entity wins over the trimmed graph node, name never repeats, text is bidi-safe', () => {
  const node = { attrs: { tags: { icao: 'UKOH' } } };
  assert.deepEqual(siteTagPairs(node, null), [['icao', 'UKOH']]);
  assert.deepEqual(
    siteTagPairs(node, {
      fields: { tags: { name: 'X', operator: BIDI, nested: { a: 1 } } },
    }),
    [['operator', 'evil']],
  );
  assert.deepEqual(siteTagPairs({ attrs: {} }, null), []);
});

test('theaterFacts: the active block wins; an inactive preset reads its table row', () => {
  const block = {
    id: 'dyn-k',
    label: 'Kherson',
    source: 'chat',
    dynamic: true,
    bbox: [1, 2, 3, 4],
    center: [2, 3],
    home: { lat: 2, lon: 3, name: 'H', source: 'operator' },
    ground_msl_m: 45,
    epoch: 3,
  };
  const active = theaterFacts(
    { id: 'thr:dyn-k', attrs: { active: true, label: 'old' } },
    null,
    { theater: block },
  );
  assert.equal(active.active, true);
  assert.equal(active.label, 'Kherson');
  assert.equal(active.source, 'chat');
  assert.deepEqual(active.bbox, [1, 2, 3, 4]);
  assert.equal(active.epoch, 3);
  const preset = theaterFacts(
    { id: 'thr:default', lat: 47.64, lon: -122.14, attrs: { active: false } },
    {
      fields: {
        place: 'Redmond',
        home: [47.64, -122.14, 122],
        ao: [
          [47.62, -122.17],
          [47.62, -122.11],
          [47.66, -122.11],
          [47.66, -122.17],
        ],
        dynamic: false,
      },
    },
    { theater: block },
  );
  assert.equal(preset.active, false);
  assert.equal(preset.source, 'preset');
  assert.deepEqual(preset.bbox, [47.62, -122.17, 47.66, -122.11]);
  assert.deepEqual(preset.center, [47.64, -122.14]);
  assert.equal(preset.home.source, 'preset');
  assert.equal(preset.ground_msl_m, 122);
  assert.equal(polygonBbox([[1, 2]]), null);
  assert.equal(
    polygonBbox([
      [1, 2],
      [3, 'x'],
      [4, 5],
    ]),
    null,
  );
});

test('airframe, geocoder and Show on map words', () => {
  assert.equal(
    airframeText({
      id: 'group3_fixed_wing',
      label: 'Fixed-wing, group 3',
      reach_m: 62_050,
    }),
    'Fixed-wing, group 3, reach 62.1 km',
  );
  assert.equal(airframeText({ id: 'x_y' }), 'X y');
  assert.equal(airframeText(null), '');
  assert.equal(
    geocoderText({ geocoder: 'Photon (OpenStreetMap)', query: XSS }),
    `Photon (OpenStreetMap), query "${XSS}"`,
  );
  assert.equal(geocoderText({ query: 'Kherson' }), 'Query "Kherson"');
  assert.equal(geocoderText({}), '');
  const site = placeMapRequest(
    'sit:a',
    {
      type: 'site',
      label: 'A',
      lat: 1,
      lon: 2,
      attrs: { bounds: [0.9, 1.9, 1.1, 2.1] },
    },
    null,
    null,
  );
  assert.deepEqual(site.bbox, [0.9, 1.9, 1.1, 2.1]);
  assert.equal(site.source, 'operator');
  assert.equal(
    placeMapRequest('trk:a', { type: 'track', lat: 1, lon: 2 }),
    null,
  );
  assert.equal(
    placeMapRequest('sit:a', { type: 'site', attrs: {} }, null, null),
    null,
  );
});

test('siteBody: category, six tags then "Show all", source and caveat; no engagement field', () => {
  globalThis.document = stubDoc();
  const k = kit();
  const tags = Object.fromEntries(
    Array.from({ length: 9 }, (_, i) => [`k${i}`, `v${i}`]),
  );
  const out = siteBody(
    k,
    { id: 'sit:a', type: 'site', label: XSS, attrs: { category: 'port' } },
    {
      fields: {
        category: 'port',
        subtype: 'harbour=yes',
        name: XSS,
        tags,
        lat: 1,
        lon: 2,
        fetched_at_ms: null,
      },
      provenance: {},
    },
  );
  const labels = k.rows.map((r) => r.label);
  assert.deepEqual(labels, [
    'Category',
    'Name',
    'Coordinates',
    'Tags',
    'Source',
    'How we know',
  ]);
  const tagRow = k.rows.find((r) => r.label === 'Tags');
  const [list, more] = tagRow.children[0].children;
  assert.equal(list.children.length, SITE_TAGS_SHOWN);
  assert.equal(text(more), 'Show all 9 tags');
  assert.ok(text(k.rows[1]).includes(XSS));
  assert.doesNotMatch(
    JSON.stringify(labels) + text({ children: out }),
    /engage|strike|damage|control/i,
  );
});

test('unknownBody: the fixed line, then every field as text', () => {
  globalThis.document = stubDoc();
  const k = kit();
  const out = unknownBody(
    k,
    { type: 'force', lat: 1, lon: 2, attrs: { side: 'red', label: BIDI } },
    { fields: { detail: { a: 1 }, empty: null } },
  );
  assert.match(text(out[0]), /That isn't a statement that it's safe\./);
  assert.deepEqual(
    k.rows.map((r) => r.label),
    ['Coordinates', 'side', 'label', 'detail'],
  );
  const all = text({ children: out });
  assert.doesNotMatch(all, BIDI_RE);
  assert.match(all, /\{"a":1\}/);
});
