import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import {
  contextEntityList,
  contextEntityOptions,
  corridorRuns,
  readContextFeatures,
  representativePoint,
  wargameEntityOptions,
} from './contextEntities.js';
import {
  AXIS_WIDTH_PX,
  CONTEXT_PREFIX,
  CORRIDOR_ALPHA,
  DEPTH_TEST_DISTANCE_M,
  ENVELOPE_FILL_ALPHA,
  ICON_PX,
  LABEL_RANGE_M,
  MAX_BILLBOARDS,
  PROTECTED_LABEL_RANGE_M,
  RING_PX,
  UNKNOWN_GREY,
  UNRECOGNISED_MAP_ITEM,
  WARGAME_INK,
  WARGAME_LABEL_RANGE_M,
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
  // A wargame kind WITHOUT the scenario register and the simulated stamp is
  // not a scenario item (D1): it is never framed, only shown as unknown.
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
  const none = { served: 0, drawn: 0, capped: 0, hidden: 0 };
  assert.deepEqual(readContextFeatures(null), {
    items: [],
    counts: {
      site: { served: 0, drawn: 0, capped: 0 },
      unknown: { served: 0, drawn: 0 },
      invalid: 0,
      force: none,
      force_envelope: none,
      vector: none,
      engagement: none,
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

// ---- the simulated wargame (WG v2 §5.3.12, B16) --------------------------------

const REAL_SYSTEM_TOKENS = [
  /S-300/i,
  /\bSA-\d/i,
  /\bTor\b/i,
  /Pantsir/i,
  /ZSU/i,
  /\bZU-/i,
  /warhead/i,
  /\bmunition/i,
  /fuze/i,
  /blast radius/i,
  /aimpoint/i,
  /\bCEP\b/i,
  /\d+\s?(kg|mm)\b/i,
];

/** One §3.3 wargame feature, as the engine serves it (Umpire view). */
function wg(kind, id, geometry, properties = {}) {
  return {
    type: 'Feature',
    id,
    geometry,
    properties: {
      kind,
      id,
      register: 'scenario',
      simulated: true,
      truth: true,
      ...properties,
    },
  };
}

const at = (lon, lat) => ({ type: 'Point', coordinates: [lon, lat] });
const line = (...pairs) => ({ type: 'LineString', coordinates: pairs });
const square = (lon, lat, d = 0.01) => ({
  type: 'Polygon',
  coordinates: [
    [
      [lon, lat],
      [lon + d, lat],
      [lon + d, lat + d],
      [lon, lat + d],
      [lon, lat],
    ],
  ],
});

function force(id, side, extra = {}) {
  return wg('force', id, at(-122.1, 47.6), {
    label: side === 'red' ? 'Red SAM 1' : 'Blue artillery 1',
    side,
    wg_class: side === 'red' ? 'ad_short' : 'blue_artillery',
    state: 'active',
    status: 'ok',
    ...extra,
  });
}

function envelope(unit, ring, status = 'ok') {
  return wg('force_envelope', `env:${unit}:${ring}`, square(-122.1, 47.6), {
    label: 'Red SAM 1',
    side: 'red',
    force: `frc:${unit}`,
    ring,
    radius_m: 3000,
    status,
  });
}

function engagement(id, extra = {}) {
  return wg('engagement', id, at(-122.11, 47.61), {
    label: 'Simulated strike on Contact',
    side: 'blue',
    phase: 'adjudicated',
    kind_detail: 'blue_strike',
    outcome: 'destroyed',
    consequence: 'red_effect',
    p_notional: 0.62,
    bda_state: 'none',
    from: null,
    ...extra,
  });
}

function corridor(id, legs, extra = {}) {
  return wg(
    'vector',
    id,
    line([-122.13, 47.6], [-122.12, 47.605], [-122.11, 47.61], [-122.1, 47.62]),
    {
      label: 'Planned corridor',
      side: 'blue',
      kind_detail: 'corridor',
      corridor_m: 90,
      legs,
      ...extra,
    },
  );
}

const axis = (id = 'vec:axis-red-sam-1') =>
  wg('vector', id, line([-122.1, 47.6], [-122.12, 47.61]), {
    label: 'Red axis',
    side: 'red',
    kind_detail: 'axis',
    status: 'ok',
  });

/** Icon stand-ins: one tagged object per kind of canvas. */
const ICONS = {
  iconFor: () => ({ tag: 'site' }),
  frameIcon: (spec) => ({ tag: 'frame', key: spec.key }),
  burstIcon: (spec) => ({ tag: 'burst', key: spec.key }),
  ringIcon: (spec) => ({ tag: 'ring', key: spec.key }),
};

const SESSION = {
  features: [
    force('frc:red-sam-1', 'red', { status: 'critical' }),
    force('frc:blue-art-1', 'blue'),
    envelope('red-sam-1', 'threat', 'critical'),
    envelope('red-sam-1', 'detection', 'critical'),
    axis(),
    corridor('vec:cor-1', [
      { exposure: 'low', exposure_s: 0, length_m: 900 },
      { exposure: 'low', exposure_s: 0, length_m: 800 },
      { exposure: 'high', exposure_s: 40, length_m: 700 },
    ]),
    engagement('eng:bs-1'),
  ],
};

/** Every entity option a body draws, flattened. */
function entitiesOf(body, options = {}, icons = ICONS) {
  return readContextFeatures(body, options).items.flatMap((item) =>
    contextEntityList(item, { icons }).map((opts) => ({ item, opts })),
  );
}

test('each wargame kind builds (Umpire view)', () => {
  const { items, counts } = readContextFeatures(SESSION, { truth: true });
  assert.deepEqual(
    items.map((item) => item.id),
    [
      'frc:red-sam-1',
      'frc:blue-art-1',
      'env:red-sam-1:threat',
      'vec:axis-red-sam-1',
      'vec:cor-1',
      'eng:bs-1',
      'env:red-sam-1:detection',
    ],
  );
  assert.equal(counts.force.drawn, 2);
  assert.equal(counts.force_envelope.drawn, 2);
  assert.equal(counts.vector.drawn, 2);
  assert.equal(counts.engagement.drawn, 1);
  assert.equal(counts.unknown.served, 0, 'none of it is unrecognised');
  for (const item of items) {
    const list = contextEntityList(item, { icons: ICONS });
    assert.ok(list.length >= 1, item.id);
    for (const opts of list) assert.ok(opts.id.startsWith(CONTEXT_PREFIX));
  }
});

test('a force is a frame billboard with its designator; Film text', () => {
  const [red] = readContextFeatures(SESSION, { truth: true }).items;
  const [opts] = wargameEntityOptions(red, { icons: ICONS });
  assert.equal(opts.id, `${CONTEXT_PREFIX}frc:red-sam-1`);
  assert.deepEqual(opts.billboard.image, {
    tag: 'frame',
    key: 'red:active:critical',
  });
  assert.equal(opts.billboard.width, ICON_PX);
  assert.equal(opts.billboard.verticalOrigin, Cesium.VerticalOrigin.CENTER);
  assert.equal(
    opts.billboard.heightReference,
    Cesium.HeightReference.CLAMP_TO_GROUND,
  );
  assert.equal(opts.label.text, 'Red SAM 1');
  assert.equal(
    opts.label.distanceDisplayCondition.far,
    WARGAME_LABEL_RANGE_M,
    'designators read at every AO framing',
  );
  // Without a canvas: a point in the side hue with a Film outline.
  const [plain] = wargameEntityOptions(red, { icons: null });
  assert.equal(plain.billboard, undefined);
  assert.ok(
    Cesium.Color.equals(
      plain.point.color,
      Cesium.Color.fromCssColorString(WARGAME_INK.hostile),
    ),
  );
  assert.ok(
    Cesium.Color.equals(
      plain.point.outlineColor,
      Cesium.Color.fromCssColorString(WARGAME_INK.film),
    ),
  );
});

test('a threat envelope is a draped 6 % fill under a 2 px status stroke; detection is Pencil dashes', () => {
  const items = readContextFeatures(SESSION, { truth: true }).items;
  const threat = items.find((item) => item.id === 'env:red-sam-1:threat');
  assert.equal(threat.pickId, 'frc:red-sam-1', 'a click reports the force');
  const [opts] = wargameEntityOptions(threat);
  assert.ok(opts.polygon.hierarchy instanceof Cesium.PolygonHierarchy);
  assert.equal(opts.polygon.height, undefined, 'draped on the terrain');
  const fill = opts.polygon.material.color.getValue();
  assert.ok(
    Cesium.Color.equals(
      fill,
      Cesium.Color.fromCssColorString(WARGAME_INK.critical).withAlpha(
        ENVELOPE_FILL_ALPHA,
      ),
    ),
  );
  assert.equal(opts.polyline.width, 2);
  assert.equal(opts.polyline.clampToGround, true);
  assert.equal(opts.polyline.positions.length, 5, 'the outline closes');
  // Pencil when the unit is ok.
  const calm = readContextFeatures(
    { features: [envelope('red-sam-2', 'threat', 'ok')] },
    { truth: true },
  ).items[0];
  assert.ok(
    Cesium.Color.equals(
      wargameEntityOptions(calm)[0].polyline.material.color.getValue(),
      Cesium.Color.fromCssColorString(WARGAME_INK.pencil),
    ),
  );
  const detection = items.find((item) => item.id.endsWith(':detection'));
  const [ring] = wargameEntityOptions(detection);
  assert.equal(ring.polygon, undefined, 'no fill');
  assert.ok(
    ring.polyline.material instanceof Cesium.PolylineDashMaterialProperty,
  );
  assert.ok(
    Cesium.Color.equals(
      ring.polyline.material.color.getValue(),
      Cesium.Color.fromCssColorString(WARGAME_INK.pencil),
    ),
  );
});

test('a red axis is a 3 px ground-clamped arrow in the hostile hue', () => {
  const item = readContextFeatures({ features: [axis()] }, { truth: true })
    .items[0];
  const [opts] = wargameEntityOptions(item);
  assert.ok(
    opts.polyline.material instanceof Cesium.PolylineArrowMaterialProperty,
  );
  assert.ok(
    Cesium.Color.equals(
      opts.polyline.material.color.getValue(),
      Cesium.Color.fromCssColorString(WARGAME_INK.hostile),
    ),
  );
  assert.equal(opts.polyline.width, AXIS_WIDTH_PX);
  assert.equal(opts.polyline.clampToGround, true);
  assert.equal(opts.label.text, 'Red axis (simulated)');
});

test('a corridor: one draped corridor per exposure run, plus a dashed Film centreline', () => {
  const item = readContextFeatures(SESSION, { truth: true }).items.find(
    (entry) => entry.id === 'vec:cor-1',
  );
  const list = wargameEntityOptions(item);
  const legs = list.filter((opts) => opts.corridor);
  assert.equal(legs.length, 2, 'low, low | high');
  assert.deepEqual(
    legs.map((opts) => opts.id),
    [`${CONTEXT_PREFIX}vec:cor-1#leg0`, `${CONTEXT_PREFIX}vec:cor-1#leg1`],
  );
  assert.equal(legs[0].corridor.positions.length, 3);
  assert.equal(legs[1].corridor.positions.length, 2);
  assert.equal(legs[0].corridor.width, 90);
  assert.equal(legs[0].corridor.height, undefined, 'draped on the terrain');
  assert.ok(
    Cesium.Color.equals(
      legs[1].corridor.material.color.getValue(),
      Cesium.Color.fromCssColorString(WARGAME_INK.critical).withAlpha(
        CORRIDOR_ALPHA,
      ),
    ),
  );
  const centre = list.find((opts) => opts.polyline);
  assert.equal(centre.id, `${CONTEXT_PREFIX}vec:cor-1`);
  assert.ok(
    centre.polyline.material instanceof Cesium.PolylineDashMaterialProperty,
  );
  assert.ok(
    Cesium.Color.equals(
      centre.polyline.material.color.getValue(),
      Cesium.Color.fromCssColorString(WARGAME_INK.film),
    ),
  );
  assert.equal(centre.polyline.clampToGround, true);
  assert.equal(centre.label.text, 'Planned corridor (simulated)');
});

test('corridor runs: legs map to segments; a segment with no leg claims no exposure', () => {
  const path = [
    [0, 0],
    [1, 0],
    [2, 0],
    [3, 0],
    [4, 0],
  ];
  const runs = corridorRuns(path, [
    { exposure: 'moderate' },
    { exposure: 'moderate' },
    { exposure: 'surprise' },
  ]);
  assert.deepEqual(
    runs.map((run) => [run.exposure, run.positions.length]),
    [
      ['moderate', 3],
      ['unknown', 2],
    ],
  );
  assert.equal(runs[1].ink, WARGAME_INK.lilac, 'not recognised is lilac');
  assert.deepEqual(corridorRuns(path, null), []);
});

test('outcome rings are screen-space billboards, never ground ellipses', () => {
  const list = entitiesOf(
    {
      features: [
        engagement('eng:bs-1', { consequence: 'red_effect' }),
        engagement('eng:rs-1', {
          side: 'red',
          kind_detail: 'red_shot',
          outcome: 'destroyed',
          consequence: 'own_loss',
        }),
      ],
    },
    { truth: false },
  );
  const rings = list.filter(({ opts }) => opts.id.endsWith('#ring'));
  assert.equal(rings.length, 2);
  for (const { opts } of rings) {
    assert.equal(opts.billboard.image.tag, 'ring');
    assert.equal(opts.billboard.width, RING_PX);
    assert.equal(opts.billboard.height, RING_PX);
    assert.notEqual(opts.billboard.sizeInMeters, true, 'sized in pixels');
  }
  assert.equal(rings[1].opts.billboard.image.key, 'own_loss');
  for (const { opts } of list) {
    assert.equal(opts.ellipse, undefined, 'no ground ellipse, ever');
    assert.equal(opts.ellipsoid, undefined);
  }
  const burst = list.find(
    ({ opts }) => opts.id === `${CONTEXT_PREFIX}eng:bs-1`,
  );
  assert.equal(burst.opts.billboard.image.tag, 'burst');
  assert.equal(burst.opts.label.text, 'Destroyed (simulated)');
  assert.equal(
    burst.opts.label.pixelOffset.y,
    RING_PX / 2 + 3,
    'the label hangs below the ring, clear of the target designator',
  );
  assert.equal(burst.opts.label.verticalOrigin, Cesium.VerticalOrigin.TOP);
  // Without a canvas the ring is a point's outline: still screen space.
  const bare = entitiesOf(
    { features: [engagement('eng:bs-2')] },
    { truth: false },
    { iconFor: () => null },
  );
  const bareRing = bare.find(({ opts }) => opts.id.endsWith('#ring')).opts;
  assert.equal(bareRing.ellipse, undefined);
  assert.ok(
    Cesium.Color.equals(bareRing.point.color, Cesium.Color.TRANSPARENT),
  );
  assert.equal(bareRing.point.pixelSize, RING_PX - 6);
});

test('an engagement without a shown outcome has no ring', () => {
  const list = entitiesOf(
    {
      features: [
        engagement('eng:hidden', { outcome: null, outcome_hidden: true }),
        engagement('eng:waiting', { phase: 'proposed', outcome: null }),
      ],
    },
    { truth: false },
  );
  assert.equal(list.length, 2);
  assert.equal(
    list.some(({ opts }) => opts.id.endsWith('#ring')),
    false,
  );
  assert.deepEqual(
    list.map(({ opts }) => opts.label.text),
    ['Outcome hidden (simulated)', 'Simulated strike, waiting for you'],
  );
});

test('Blue view hides red forces, envelopes and axes; engagements stay', () => {
  const blue = readContextFeatures(SESSION, { truth: false });
  assert.deepEqual(
    blue.items.map((item) => item.id),
    ['frc:blue-art-1', 'vec:cor-1', 'eng:bs-1'],
  );
  assert.equal(blue.counts.force.hidden, 1);
  assert.equal(blue.counts.force_envelope.hidden, 2);
  assert.equal(blue.counts.vector.hidden, 1);
  assert.equal(blue.counts.engagement.hidden, 0);
  // The default is Blue view.
  assert.deepEqual(
    readContextFeatures(SESSION).items.map((item) => item.id),
    blue.items.map((item) => item.id),
  );
  // Umpire view asked, but the body was fetched for Blue view (a revealed
  // red unit, truth:false): red still does not show.
  const revealed = readContextFeatures(
    { features: [force('frc:red-sam-9', 'red', { truth: false })] },
    { truth: true },
  );
  assert.equal(revealed.items.length, 0);
  assert.equal(revealed.counts.force.hidden, 1);
});

test('a wargame kind without the scenario stamp is never framed', () => {
  for (const properties of [
    { register: 'mapped' },
    { simulated: false },
    { register: undefined },
  ]) {
    const feature = force('frc:red-1', 'red', properties);
    const [item] = readContextFeatures(
      { features: [feature] },
      { truth: true },
    ).items;
    assert.equal(item.known, false);
    assert.equal(item.wargame, undefined);
    const [opts] = contextEntityList(item, { icons: ICONS });
    assert.equal(opts.billboard, undefined, 'no frame');
    assert.equal(opts.label.text, UNRECOGNISED_MAP_ITEM);
  }
});

test('designators are untrusted: bidi-stripped, literal, capped (§3.11)', () => {
  const XSS_BIDI = `${XSS}${BIDI}`;
  const list = entitiesOf(
    {
      features: [
        force('frc:blue-1', 'blue', { label: XSS }),
        force('frc:blue-2', 'blue', { label: BIDI }),
        force('frc:blue-3', 'blue', { label: XSS_BIDI.repeat(3) }),
        force('frc:blue-4', 'blue', { label: '⁦⁩' }),
        corridor('vec:cor-9', [], { label: XSS }),
        engagement('eng:x', { label: XSS }),
      ],
    },
    { truth: false },
  );
  const texts = list.map(({ opts }) => opts.label?.text).filter(Boolean);
  assert.equal(texts[0], XSS, 'the literal angle-bracket text survives');
  assert.equal(texts[1], 'evil');
  assert.ok(texts[2].length <= 40 && texts[2].endsWith('…'));
  assert.equal(texts[3], 'Scenario unit', 'an empty designator falls back');
  assert.equal(texts[4], 'Planned corridor (simulated)', 'constants only');
  assert.equal(texts[5], 'Destroyed (simulated)', 'constants only');
  for (const text of texts) {
    assert.doesNotMatch(text, BIDI_CONTROLS);
    assert.doesNotMatch(text, /·|SIMULATED/);
  }
  assert.equal(typeof globalThis.document, 'undefined', 'no DOM node');
});

test('detection rings: the selected force first, then critical ones; at most 8', () => {
  const features = [];
  for (let i = 0; i < 10; i += 1) {
    features.push(envelope(`red-${i}`, 'detection', i < 9 ? 'critical' : 'ok'));
  }
  features.push(envelope('red-quiet', 'detection', 'ok'));
  const plain = readContextFeatures({ features }, { truth: true });
  assert.equal(plain.items.length, 8);
  assert.equal(plain.counts.force_envelope.capped, 1);
  assert.equal(plain.items[0].pickId, 'frc:red-0');
  const chosen = readContextFeatures(
    { features },
    { truth: true, selected: 'frc:red-quiet' },
  );
  assert.equal(chosen.items.length, 8);
  assert.ok(chosen.items.some((item) => item.pickId === 'frc:red-quiet'));
  assert.equal(chosen.counts.force_envelope.capped, 2);
  // A calm force nobody selected has no detection ring.
  assert.equal(
    readContextFeatures(
      { features: [envelope('red-q', 'detection')] },
      { truth: true },
    ).items.length,
    0,
  );
});

test('client caps hold if the server sends too much; bad geometry costs itself', () => {
  const forces = Array.from({ length: 61 }, (_, i) =>
    force(`frc:blue-${i}`, 'blue'),
  );
  const corridors = Array.from({ length: 7 }, (_, i) =>
    corridor(`vec:cor-${i}`, []),
  );
  const { items, counts } = readContextFeatures({
    features: [
      ...forces,
      ...corridors,
      wg('force', 'frc:blue-bad', line([0, 0], [1, 1]), { side: 'blue' }),
      wg('vector', 'vec:cor-bad', at(0, 0), {
        side: 'blue',
        kind_detail: 'corridor',
      }),
      wg('force_envelope', 'env:x:threat', square(0, 0, NaN), { side: 'blue' }),
      force('frc:blue-0', 'blue'),
    ],
  });
  assert.equal(counts.force.drawn, 60);
  assert.equal(counts.force.capped, 1);
  assert.equal(counts.vector.drawn, 6);
  assert.equal(counts.vector.capped, 1);
  assert.equal(counts.invalid, 4, 'three bad geometries and one duplicate id');
  assert.equal(items.length, 66);
});

test('no real-system or weaponeering token reaches a wargame label', () => {
  const texts = entitiesOf(SESSION, { truth: true })
    .map(({ opts }) => opts.label?.text)
    .filter(Boolean);
  assert.ok(texts.length >= 5);
  for (const text of texts)
    for (const token of REAL_SYSTEM_TOKENS) assert.doesNotMatch(text, token);
});
