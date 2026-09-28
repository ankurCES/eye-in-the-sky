import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import {
  contextEntityOptions,
  readContextFeatures,
  representativePoint,
} from './contextEntities.js';
import {
  CONTEXT_PREFIX,
  DEPTH_TEST_DISTANCE_M,
  LABEL_RANGE_M,
  MAX_BILLBOARDS,
  PROTECTED_LABEL_RANGE_M,
  UNKNOWN_GREY,
  UNRECOGNISED_MAP_ITEM,
} from './contextPolicy.js';

const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';
const BIDI_CONTROLS = /[‪-‮⁦-⁩]/;

/** One §3.3 site feature. */
function siteFeature(overrides = {}, properties = {}) {
  const id = properties.id ?? 'sit:dyn-k:way/101';
  return {
    type: 'Feature',
    id,
    geometry: { type: 'Point', coordinates: [32.6, 46.64] },
    properties: {
      kind: 'site',
      id,
      label: 'Kherson International',
      category: 'airfield',
      protected: false,
      register: 'mapped',
      salience: 0.5,
      labelled: true,
      simulated: false,
      truth: false,
      ...properties,
    },
    ...overrides,
  };
}

test('a site reads as a drawn item with its id, category and safe label', () => {
  const { items, counts } = readContextFeatures({
    features: [siteFeature()],
  });
  assert.equal(items.length, 1);
  const [item] = items;
  assert.equal(item.id, 'sit:dyn-k:way/101');
  assert.equal(item.entityId, `${CONTEXT_PREFIX}sit:dyn-k:way/101`);
  assert.equal(item.kind, 'site');
  assert.equal(item.known, true);
  assert.equal(item.category, 'airfield');
  assert.equal(item.label, 'Kherson International');
  assert.equal(item.labelled, true);
  assert.deepEqual([item.lon, item.lat], [32.6, 46.64]);
  assert.deepEqual(counts.site, { served: 1, drawn: 1, capped: 0 });
});

test('a site entity is a clamped, depth-limited, distance-scaled billboard', () => {
  const [item] = readContextFeatures({ features: [siteFeature()] }).items;
  const icon = { tag: 'canvas' };
  const options = contextEntityOptions(item, { icon });
  assert.equal(options.id, item.entityId);
  assert.equal(options.billboard.image, icon);
  assert.equal(options.billboard.width, 32);
  assert.equal(options.billboard.height, 32);
  assert.equal(
    options.billboard.heightReference,
    Cesium.HeightReference.CLAMP_TO_GROUND,
  );
  assert.equal(
    options.billboard.disableDepthTestDistance,
    DEPTH_TEST_DISTANCE_M,
  );
  // The map frames the largest AO (50 km, padded 15 %) from about 250 km:
  // closer than this the depth test must be off, or the sites vanish.
  assert.ok(DEPTH_TEST_DISTANCE_M >= 250_000);
  const scale = options.billboard.scaleByDistance;
  assert.deepEqual(
    [scale.near, scale.nearValue, scale.far, scale.farValue],
    [2e3, 1, 5e4, 0.5],
  );
  assert.equal(options.label.text, 'Kherson International');
  assert.equal(options.label.distanceDisplayCondition.far, LABEL_RANGE_M);
  // Clamped markers stand ABOVE their ground point, labels above them, so
  // terrain never cuts the lower half off beyond the depth-test range.
  assert.equal(options.billboard.verticalOrigin, Cesium.VerticalOrigin.BOTTOM);
  assert.equal(options.label.verticalOrigin, Cesium.VerticalOrigin.BOTTOM);
  assert.ok(options.label.pixelOffset.y < -32);
  const cartographic = Cesium.Cartographic.fromCartesian(options.position);
  assert.ok(
    Math.abs(Cesium.Math.toDegrees(cartographic.latitude) - 46.64) < 1e-9,
  );

  // Without an icon canvas the site is still drawn, as a point.
  const plain = contextEntityOptions(item, { icon: null });
  assert.equal(plain.billboard, undefined);
  assert.equal(plain.point.pixelSize, 10);
});

test('only labelled sites carry a label; medical shows "Protected" to 30 km', () => {
  const quiet = readContextFeatures({
    features: [siteFeature({}, { labelled: false })],
  }).items[0];
  assert.equal(contextEntityOptions(quiet).label, undefined);

  const medical = readContextFeatures({
    features: [
      siteFeature(
        {},
        {
          id: 'sit:dyn-k:node/9',
          label: 'City hospital',
          category: 'medical',
          protected: true,
          labelled: false,
        },
      ),
    ],
  }).items[0];
  const options = contextEntityOptions(medical);
  assert.equal(options.label.text, 'City hospital\nProtected');
  assert.equal(
    options.label.distanceDisplayCondition.far,
    PROTECTED_LABEL_RANGE_M,
  );
});

test('an unknown kind draws as a grey point labelled "Unrecognised map item"', () => {
  const { items, counts } = readContextFeatures({
    features: [
      {
        type: 'Feature',
        id: 'frc:red-1',
        geometry: { type: 'Point', coordinates: [32.61, 46.65] },
        properties: { kind: 'force', id: 'frc:red-1', label: XSS },
      },
      {
        type: 'Feature',
        geometry: {
          type: 'LineString',
          coordinates: [
            [32.0, 46.0],
            [33.0, 47.0],
          ],
        },
        properties: { kind: 'vector', id: 'vec:cor-1' },
      },
    ],
  });
  assert.equal(items.length, 2);
  assert.deepEqual(counts.unknown, { served: 2, drawn: 2 });
  for (const item of items) {
    assert.equal(item.known, false);
    assert.equal(item.label, UNRECOGNISED_MAP_ITEM);
    const options = contextEntityOptions(item);
    assert.equal(options.billboard, undefined);
    assert.ok(
      Cesium.Color.equals(
        options.point.color,
        Cesium.Color.fromCssColorString(UNKNOWN_GREY),
      ),
    );
    assert.equal(options.label.text, UNRECOGNISED_MAP_ITEM);
  }
  // A line is drawn at the mean of its vertices.
  assert.deepEqual([items[1].lon, items[1].lat], [32.5, 46.5]);
});

test('at most 150 billboards, in the order served (most salient first)', () => {
  const features = Array.from({ length: 200 }, (_, index) =>
    siteFeature({}, { id: `sit:dyn-k:node/${index}`, label: `Site ${index}` }),
  );
  const { items, counts } = readContextFeatures({ features });
  assert.equal(items.length, MAX_BILLBOARDS);
  assert.equal(items[0].label, 'Site 0');
  assert.equal(items.at(-1).label, 'Site 149');
  assert.deepEqual(counts.site, { served: 200, drawn: 150, capped: 50 });
});

test('unusable features cost only themselves; duplicate ids draw once', () => {
  const { items, counts } = readContextFeatures({
    features: [
      siteFeature({ geometry: { type: 'Point', coordinates: ['x', 1] } }),
      siteFeature({ geometry: null }),
      siteFeature({ geometry: { type: 'Point', coordinates: [500, 1] } }),
      siteFeature({}, { id: 'sit:dup' }),
      siteFeature({}, { id: 'sit:dup' }),
      null,
    ],
  });
  assert.equal(items.length, 1);
  assert.equal(counts.invalid, 5);
  assert.deepEqual(readContextFeatures(null), {
    items: [],
    counts: {
      site: { served: 0, drawn: 0, capped: 0 },
      unknown: { served: 0, drawn: 0 },
      invalid: 0,
    },
  });
});

test('representative points: a Point, a polygon mean, nothing usable', () => {
  assert.deepEqual(
    representativePoint({ type: 'Point', coordinates: [1, 2] }),
    [1, 2],
  );
  assert.deepEqual(
    representativePoint({
      type: 'Polygon',
      coordinates: [
        [
          [0, 0],
          [2, 0],
          [2, 2],
          [0, 2],
        ],
      ],
    }),
    [1, 1],
  );
  assert.equal(representativePoint({ type: 'Polygon', coordinates: [] }), null);
  assert.equal(representativePoint(undefined), null);
});

test('untrusted names reach a Cesium label as text, bidi-stripped (§3.11)', () => {
  const { items } = readContextFeatures({
    features: [
      siteFeature({}, { id: 'sit:a', label: XSS }),
      siteFeature({}, { id: 'sit:b', label: BIDI }),
      siteFeature(
        {},
        {
          id: 'sit:c',
          label: `${BIDI} ward`,
          category: 'medical',
          protected: true,
        },
      ),
      siteFeature({}, { id: 'sit:d', label: '⁦⁩', category: 'port' }),
    ],
  });
  const texts = items.map((item) => contextEntityOptions(item).label.text);
  assert.equal(texts[0], XSS, 'the literal angle-bracket text survives');
  assert.equal(texts[1], 'evil');
  assert.equal(texts[2], 'evil ward\nProtected');
  assert.equal(
    texts[3],
    'Port',
    'an empty name falls back to the category word',
  );
  for (const text of texts) assert.doesNotMatch(text, BIDI_CONTROLS);
  // Cesium labels paint canvas text: no DOM node, so no <img>, is created.
  assert.equal(typeof globalThis.document, 'undefined');
});

test('a label is cut to 80 characters', () => {
  const [item] = readContextFeatures({
    features: [siteFeature({}, { label: 'x'.repeat(200) })],
  }).items;
  assert.ok(item.label.length <= 80);
  assert.ok(item.label.endsWith('…'));
});
