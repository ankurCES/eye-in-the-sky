import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BANDS,
  BAND_ORDER,
  CAP_SPAN_DEG,
  GRATICULE_PARALLELS,
  EDGE_POINTS,
  EDGE_STRIDE,
  SECTORS,
  computeLayout,
  edgeArc,
  hashId,
  interleave,
  meanLongitude,
  sectorOf,
  toVector,
  wrapLon,
} from './layout.js';
import { makeGraph, shuffled } from './fixtures.test.mjs';
import {
  SITE_BAND,
  placeSectorRow,
  sectorRowLon,
  siteSectorKey,
} from './contextBands.js';

const at = (layout, id) => {
  const i = layout.index.get(id);
  assert.ok(i != null, `${id} is placed`);
  return { lat: layout.lat[i], lon: layout.lon[i], i };
};
const lonDelta = (a, b) => Math.abs(wrapLon(a - b));

test('the same graph gives the same layout regardless of node and edge order', () => {
  const graph = makeGraph(120);
  const a = computeLayout(graph);
  const b = computeLayout({
    ...graph,
    nodes: shuffled(graph.nodes),
    edges: shuffled(graph.edges, 9),
  });
  assert.deepEqual(a.ids, b.ids);
  assert.deepEqual([...a.pos], [...b.pos]);
  assert.deepEqual([...a.lat], [...b.lat]);
  // …and a second run is bit-identical.
  assert.deepEqual([...computeLayout(graph).pos], [...a.pos]);
});

test('existing nodes never move when others arrive', () => {
  const small = makeGraph(60, { seed: 11 });
  const first = computeLayout(small);
  const bigger = makeGraph(60, { seed: 11 });
  // New contacts in every sector, a new vehicle, a new POI and a new report.
  for (let k = 0; k < 12; k += 1) {
    bigger.nodes.push({
      id: `trk:NEW-${k}`,
      type: 'track',
      label: `New ${k}`,
      group: SECTORS[k % 10].key,
      salience: 0.5,
      status: 'ok',
      attrs: {},
    });
  }
  bigger.nodes.push({
    id: 'veh:Drone9',
    type: 'vehicle',
    label: 'Drone9',
    salience: 0.8,
    status: 'ok',
    attrs: {},
  });
  bigger.nodes.push({
    id: 'poi:default:West Field',
    type: 'poi',
    label: 'West Field',
    salience: 0.5,
    status: 'ok',
    attrs: {},
  });
  const second = computeLayout(bigger, first);
  for (const id of first.ids) {
    if (id.startsWith('alarm:')) continue; // the alarm cap is ordered by recency, by design
    const before = at(first, id);
    const after = at(second, id);
    assert.equal(after.lat, before.lat, `${id} latitude is stable`);
    assert.equal(after.lon, before.lon, `${id} longitude is stable`);
  }
});

test('existing nodes never move when others leave', () => {
  const graph = makeGraph(90, { seed: 5 });
  const first = computeLayout(graph);
  const keep = graph.nodes.filter(
    (node, k) => k % 3 !== 1 || node.type === 'theater',
  );
  const kept = new Set(keep.map((n) => n.id));
  const second = computeLayout(
    {
      nodes: keep,
      edges: graph.edges.filter((e) => kept.has(e.a) && kept.has(e.b)),
    },
    first,
  );
  for (const id of second.ids) {
    if (id.startsWith('alarm:')) continue;
    assert.equal(at(second, id).lon, at(first, id).lon, `${id} stays put`);
    assert.equal(at(second, id).lat, at(first, id).lat, `${id} stays put`);
  }
  // A node that leaves and comes back takes a free slot again without disturbing others.
  const third = computeLayout(graph, second);
  for (const id of second.ids) {
    if (id.startsWith('alarm:')) continue;
    assert.equal(at(third, id).lon, at(second, id).lon);
  }
});

test('bands follow the spec latitudes', () => {
  const layout = computeLayout(makeGraph(200, { seed: 2 }));
  for (let i = 0; i < layout.n; i += 1) {
    const { type } = layout.nodes[i];
    const lat = layout.lat[i];
    switch (type) {
      case 'theater':
        assert.equal(lat, 90);
        break;
      case 'feed':
      case 'poi':
      case 'vehicle':
      case 'mission':
      case 'unit':
      case 'equipment':
      case 'report':
        assert.ok(
          Math.abs(lat - BANDS[type].lat) < 1e-4,
          `${type} at ${BANDS[type].lat}, got ${lat}`,
        );
        break;
      case 'track':
        assert.ok(
          lat <= 38 && lat >= -26,
          `contact inside the belt, got ${lat}`,
        );
        break;
      case 'alarm':
        assert.ok(
          lat <= -68 + 1e-4 && lat >= -86 - 1e-4,
          `alarm inside the cap, got ${lat}`,
        );
        break;
      default:
        assert.fail(`unexpected type ${type}`);
    }
  }
});

test('contacts sit inside their fixed sector; unknown groups are unclassified', () => {
  const graph = makeGraph(150, { seed: 4 });
  graph.nodes.push({
    id: 'trk:odd',
    type: 'track',
    label: 'Odd',
    group: 'space-lasers',
    salience: 0.2,
    status: 'ok',
    attrs: {},
  });
  const layout = computeLayout(graph);
  for (let i = 0; i < layout.n; i += 1) {
    const node = layout.nodes[i];
    if (node.type !== 'track') continue;
    const sector = SECTORS[sectorOf(node.group)];
    const offset = wrapLon(layout.lon[i] - sector.lonStart);
    assert.ok(
      offset >= 2.9 && offset <= 33.1,
      `${node.id} (${node.group}) within ${sector.key}: offset ${offset}`,
    );
  }
  assert.equal(
    SECTORS[layout.sector[layout.index.get('trk:odd')]].key,
    'unclassified',
  );
  assert.equal(SECTORS.length, 10);
  assert.equal(SECTORS[0].key, 'air-defense');
  assert.equal(SECTORS[0].lonCenter, 0, 'air defense faces the camera at rest');
});

test('missions sit at their vehicle longitude; units, equipment and reports at their members', () => {
  const graph = makeGraph(120, { seed: 8 });
  const layout = computeLayout(graph);
  const mission = at(layout, 'msn:MSN-1a2b3c4d');
  const vehicle = at(layout, 'veh:Drone1');
  assert.ok(lonDelta(mission.lon, vehicle.lon) <= 5, 'flying edge stays short');

  for (const kind of ['member_of', 'is_a']) {
    const targets = new Set(
      graph.edges.filter((e) => e.kind === kind).map((e) => e.b),
    );
    for (const id of targets) {
      const members = graph.edges
        .filter((e) => e.kind === kind && e.b === id)
        .map((e) => at(layout, e.a).lon);
      const mean = meanLongitude(members);
      assert.ok(
        lonDelta(at(layout, id).lon, mean) <= 10,
        `${id} near its members' mean longitude`,
      );
    }
  }
  const reports = new Set(
    graph.edges.filter((e) => e.kind === 'reports_on').map((e) => e.a),
  );
  for (const id of reports) {
    const lons = graph.edges
      .filter((e) => e.kind === 'reports_on' && e.a === id)
      .map((e) => at(layout, e.b).lon);
    assert.ok(lonDelta(at(layout, id).lon, meanLongitude(lons)) <= 10);
  }
});

test('a unit placed before its members are known re-anchors once they arrive', () => {
  const lonely = {
    nodes: [{ id: 'unit:sam:x', type: 'unit', label: 'U', attrs: {} }],
    edges: [],
  };
  const first = computeLayout(lonely);
  assert.equal(first.assignments.get('unit:sam:x').provisional, true);
  const graph = {
    nodes: [
      ...lonely.nodes,
      { id: 'trk:a', type: 'track', group: 'naval', attrs: {} },
      { id: 'trk:b', type: 'track', group: 'naval', attrs: {} },
    ],
    edges: [
      { a: 'trk:a', b: 'unit:sam:x', kind: 'member_of' },
      { a: 'trk:b', b: 'unit:sam:x', kind: 'member_of' },
    ],
  };
  const second = computeLayout(graph, first);
  const mean = meanLongitude([
    at(second, 'trk:a').lon,
    at(second, 'trk:b').lon,
  ]);
  assert.ok(lonDelta(at(second, 'unit:sam:x').lon, mean) <= 5);
  assert.equal(second.assignments.get('unit:sam:x').provisional, false);
});

test('alarms: newest nearest -68°, each older one about the same subject 3° poleward', () => {
  const nodes = [
    { id: 'veh:Drone1', type: 'vehicle', label: 'Drone1', attrs: {} },
  ];
  const edges = [];
  for (let seq = 1; seq <= 4; seq += 1) {
    nodes.push({
      id: `alarm:${seq}`,
      type: 'alarm',
      label: 'Bingo',
      attrs: { seq },
    });
    edges.push({ a: `alarm:${seq}`, b: 'veh:Drone1', kind: 'about' });
  }
  nodes.push({
    id: 'alarm:99',
    type: 'alarm',
    label: 'Orphan',
    attrs: { seq: 99 },
  });
  const layout = computeLayout({ nodes, edges });
  assert.ok(Math.abs(at(layout, 'alarm:4').lat - -68) < 1e-4);
  assert.ok(Math.abs(at(layout, 'alarm:3').lat - -71) < 1e-4);
  assert.ok(Math.abs(at(layout, 'alarm:1').lat - -77) < 1e-4);
  const vehicle = at(layout, 'veh:Drone1');
  assert.ok(
    lonDelta(at(layout, 'alarm:4').lon, vehicle.lon) <= 5,
    'at the subject longitude',
  );
  assert.ok(
    Math.abs(at(layout, 'alarm:99').lat - -68) < 1e-4,
    'an alarm without a subject is hashed',
  );
});

test('a single theater sits on the pole; others ring it', () => {
  const layout = computeLayout({
    nodes: [
      { id: 'thr:b', type: 'theater', attrs: { active: false } },
      { id: 'thr:z', type: 'theater', attrs: { active: true } },
      { id: 'thr:a', type: 'theater', attrs: {} },
    ],
    edges: [],
  });
  assert.equal(
    at(layout, 'thr:z').lat,
    90,
    'the active theater takes the pole',
  );
  assert.ok(Math.abs(at(layout, 'thr:a').lat - 85) < 1e-4);
  assert.ok(Math.abs(at(layout, 'thr:b').lat - 85) < 1e-4);
});

test('the polar cap spreads feeds and places over an arc centred on own force (review: first-run pile-up)', () => {
  // The first-run picture: 5 feeds hashed into neighbouring +80° slots and
  // 3 places at +72° collapsed into one cluster over the pole.
  const feeds = ['sim', 'real_data', 'theater', 'contacts', 'mission_state'];
  const places = ['North Field', 'East Field', 'South Field'];
  const graph = {
    nodes: [
      { id: 'thr:default', type: 'theater', attrs: { active: true } },
      ...feeds.map((f) => ({ id: `feed:${f}`, type: 'feed' })),
      ...places.map((p) => ({ id: `poi:default:${p}`, type: 'poi' })),
      { id: 'veh:Drone1', type: 'vehicle' },
    ],
    edges: [],
  };
  const layout = computeLayout(graph);
  const own = at(layout, 'veh:Drone1').lon;
  const cap = [
    ...feeds.map((f) => `feed:${f}`),
    ...places.map((p) => `poi:default:${p}`),
  ]
    .map((id) => ({ id, ...at(layout, id) }))
    .sort((a, b) => wrapLon(a.lon - own) - wrapLon(b.lon - own));
  // Latitudes stay the spec's bands.
  for (const node of cap)
    assert.equal(
      node.lat,
      node.id.startsWith('feed:') ? BANDS.feed.lat : BANDS.poi.lat,
    );
  // Every cap node sits inside the arc (plus half a slot), on the own-force side.
  const halfSlot = 360 / layout.pools.cap.capacity / 2;
  for (const node of cap)
    assert.ok(
      lonDelta(node.lon, own) <= CAP_SPAN_DEG / 2 + halfSlot + 1e-6,
      `${node.id} at ${node.lon}, own force at ${own}`,
    );
  // Neighbours are an even share of the arc apart (no two in one slot), and
  // they alternate rings where they can, so no ring runs glyph on glyph.
  const share = CAP_SPAN_DEG / cap.length;
  for (let k = 1; k < cap.length; k += 1)
    assert.ok(
      lonDelta(cap[k].lon, cap[k - 1].lon) >= share - 2 * halfSlot - 1e-6,
      `${cap[k - 1].id} and ${cap[k].id} are ${lonDelta(cap[k].lon, cap[k - 1].lon)}° apart`,
    );
  const rings = cap.map((node) => node.id.slice(0, 4)).join(' ');
  assert.ok(!/poi: poi:/.test(rings), `places never adjacent: ${rings}`);
  // A new feed later on never moves the ones already placed.
  const grown = computeLayout(
    {
      ...graph,
      nodes: [...graph.nodes, { id: 'feed:weather', type: 'feed' }],
    },
    layout,
  );
  for (const node of cap) {
    const i = grown.index.get(node.id);
    assert.equal(grown.lon[i], node.lon, `${node.id} kept its slot`);
  }
});

test('interleave takes each list at its even share', () => {
  assert.deepEqual(interleave(['a', 'b', 'c', 'd', 'e'], [1, 2, 3]), [
    'a',
    1,
    'b',
    'c',
    2,
    'd',
    3,
    'e',
  ]);
  assert.deepEqual(interleave([], [1, 2]), [1, 2]);
  assert.deepEqual(interleave(['a'], []), ['a']);
});

test('pools grow past two-thirds occupancy and stay deterministic', () => {
  const nodes = [];
  for (let k = 0; k < 60; k += 1)
    nodes.push({
      id: `trk:${k}`,
      type: 'track',
      group: 'air-defense',
      attrs: {},
    });
  const layout = computeLayout({ nodes, edges: [] });
  assert.ok(
    layout.pools['track:air-defense'].capacity >= 180,
    'capacity is 3× the count',
  );
  const slots = new Set([...layout.assignments.values()].map((a) => a.slot));
  assert.equal(slots.size, 60, 'every node has its own slot');
  const again = computeLayout({ nodes: shuffled(nodes), edges: [] });
  assert.deepEqual([...again.pos], [...layout.pos]);
});

test('edges are 12-segment great-circle polylines lifted by arc length', () => {
  const layout = computeLayout(makeGraph(60));
  assert.equal(layout.edgePts.length, layout.edges.length * EDGE_STRIDE);
  const p = toVector(0, 0);
  const q = toVector(0, 90);
  const pts = edgeArc(p, q);
  assert.equal(pts.length, EDGE_POINTS * 3);
  assert.deepEqual(
    [pts[0], pts[1], pts[2]].map((v) => +v.toFixed(5)),
    p.map((v) => +v.toFixed(5)),
  );
  const end = (EDGE_POINTS - 1) * 3;
  assert.deepEqual(
    [pts[end], pts[end + 1], pts[end + 2]].map((v) => +v.toFixed(5)),
    q.map((v) => +v.toFixed(5)),
  );
  const mid = 6 * 3;
  const midLen = Math.hypot(pts[mid], pts[mid + 1], pts[mid + 2]);
  assert.ok(
    midLen > 1.08 && midLen < 1.1,
    `a 90° arc lifts ~9 %, got ${midLen}`,
  );
  const short = edgeArc(toVector(0, 0), toVector(0, 10));
  const shortLen = Math.hypot(short[mid], short[mid + 1], short[mid + 2]);
  assert.ok(shortLen < midLen, 'shorter arcs lift less');
  // Antipodal and degenerate arcs stay finite.
  for (const arc of [
    edgeArc(toVector(0, 0), toVector(0, 180)),
    edgeArc(p, p),
  ]) {
    assert.ok([...arc].every(Number.isFinite));
  }
});

test('self loops and dangling edges are dropped; junk nodes are ignored', () => {
  const layout = computeLayout({
    nodes: [
      { id: 'veh:A', type: 'vehicle' },
      { id: 'veh:A', type: 'vehicle' },
      null,
      { id: '', type: 'track' },
      { type: 'track' },
    ],
    edges: [
      { a: 'veh:A', b: 'veh:A', kind: 'flying' },
      { a: 'veh:A', b: 'msn:gone', kind: 'flying' },
    ],
  });
  assert.equal(layout.n, 1);
  assert.equal(layout.edges.length, 0);
  assert.equal(computeLayout(null).n, 0);
  assert.equal(computeLayout({}).n, 0);
});

test('hashId spreads ids and is stable', () => {
  assert.equal(hashId('trk:T-3fa9c1'), hashId('trk:T-3fa9c1'));
  const buckets = new Array(10).fill(0);
  for (let k = 0; k < 1000; k += 1) buckets[hashId(`trk:T-${k}`) % 10] += 1;
  for (const count of buckets)
    assert.ok(count > 60 && count < 140, `bucket ${count}`);
  assert.equal(wrapLon(190), -170);
  assert.equal(wrapLon(-180), -180);
  assert.equal(meanLongitude([170, -170]), -180);
  assert.equal(meanLongitude([]), null);
});

test('the graph fixture is contract-shaped and sized as asked', () => {
  for (const total of [5, 40, 300]) {
    const graph = makeGraph(total);
    const ids = new Set(graph.nodes.map((n) => n.id));
    assert.equal(ids.size, graph.nodes.length, 'ids are unique');
    if (total >= 40) assert.equal(graph.nodes.length, total);
    for (const edge of graph.edges) {
      assert.ok(
        ids.has(edge.a) && ids.has(edge.b),
        `edge ${edge.a} → ${edge.b} resolves`,
      );
    }
  }
});

test('band latitudes are pinned: the ISR bands as before, plus the site band, the lower belt top and the session bands (WG §4.2.6, §5.3.5)', () => {
  const lat = Object.fromEntries(
    Object.entries(BANDS).map(([key, band]) => [key, band.lat]),
  );
  assert.deepEqual(lat, {
    theater: 90,
    feed: 80,
    poi: 72,
    vehicle: 60,
    mission: 50,
    site: 42,
    track: 6,
    other: -31,
    unit: -36,
    force_red: -41,
    equipment: -46,
    force_blue: -47,
    report: -58,
    engagement: -61,
    alarm: -74,
  });
  assert.equal(BANDS.theater.ringLat, 85);
  assert.equal(BANDS.track.latTop, 35, 'the contact belt top moves from 38');
  assert.equal(BANDS.track.latBottom, -26);
  assert.equal(BANDS.alarm.latTop, -68);
  assert.equal(BANDS.alarm.latBottom, -86);
  assert.equal(BANDS.site.slotsPerSector, 12);
  assert.equal(BANDS.site.slotDeg, 3);
  assert.equal(BANDS.site, SITE_BAND);
  assert.deepEqual(BAND_ORDER, [
    'theater',
    'feed',
    'poi',
    'vehicle',
    'mission',
    'site',
    'track',
    'other',
    'unit',
    'force_red',
    'equipment',
    'force_blue',
    'report',
    'engagement',
    'alarm',
  ]);
  assert.equal(GRATICULE_PARALLELS.beltTop, 38.5, 'moved from 44');
  assert.equal(GRATICULE_PARALLELS.beltBottom, -28.5);
  assert.equal(GRATICULE_PARALLELS.alarmCap, -63);
  assert.equal(GRATICULE_PARALLELS.emptyRow, 6);
  assert.ok(
    GRATICULE_PARALLELS.sectorNames < GRATICULE_PARALLELS.beltTop &&
      GRATICULE_PARALLELS.sectorNames > BANDS.track.latTop,
    'sector names sit between the belt line and the contacts',
  );
  assert.ok(
    BANDS.site.lat > GRATICULE_PARALLELS.beltTop &&
      BANDS.site.lat < BANDS.mission.lat,
  );
});

test('an unknown node type goes to the Other band (fail-safe, WG §4.2.1)', () => {
  const layout = computeLayout({
    nodes: [
      { id: 'stk:1', type: 'strike_package', status: 'critical' },
      { id: 'band:1', type: 'force_red' },
      { id: 'zzz:1', type: 'zzz' },
      { id: 'x:proto', type: '__proto__' },
    ],
    edges: [],
  });
  for (const id of layout.ids) {
    assert.equal(layout.band[layout.index.get(id)], 'other');
    assert.ok(Math.abs(at(layout, id).lat - BANDS.other.lat) < 1e-4);
  }
  assert.equal(layout.counts.other, 4, 'a band key is not a node type');
  assert.equal(layout.profile, 'isr');
});

const theaterGraph = (active, all = true) => ({
  nodes: [
    ...(all || active === 'default'
      ? [
          {
            id: 'thr:default',
            type: 'theater',
            attrs: { active: active === 'default' },
          },
        ]
      : []),
    ...(all || active === 'dyn-x'
      ? [
          {
            id: 'thr:dyn-x',
            type: 'theater',
            attrs: { active: active === 'dyn-x' },
          },
        ]
      : []),
    ...(all
      ? [{ id: 'thr:baghdad', type: 'theater', attrs: { active: false } }]
      : []),
    { id: 'veh:Drone1', type: 'vehicle' },
  ],
  edges: [],
});

test('pole fix: a newly active theater takes the pole in All scope, and the old one rings it (WG §4.2.4)', () => {
  const before = computeLayout(theaterGraph('default'));
  assert.equal(at(before, 'thr:default').lat, 90);
  const ringBefore = at(before, 'thr:baghdad');
  const after = computeLayout(theaterGraph('dyn-x'), before);
  assert.equal(
    at(after, 'thr:dyn-x').lat,
    90,
    'the active theater has the pole',
  );
  assert.ok(Math.abs(at(after, 'thr:default').lat - 85) < 1e-4);
  const ringAfter = at(after, 'thr:baghdad');
  assert.equal(ringAfter.lat, ringBefore.lat, 'bystanders keep their slot');
  assert.equal(ringAfter.lon, ringBefore.lon);
  // …and back again.
  const back = computeLayout(theaterGraph('default'), after);
  assert.equal(at(back, 'thr:default').lat, 90);
  assert.ok(Math.abs(at(back, 'thr:dyn-x').lat - 85) < 1e-4);
  // Deterministic: a fresh layout agrees on the pole.
  assert.equal(at(computeLayout(theaterGraph('dyn-x')), 'thr:dyn-x').lat, 90);
});

test('pole fix: in theater scope the new theater replaces the old one on the pole', () => {
  const before = computeLayout(theaterGraph('default', false));
  const after = computeLayout(theaterGraph('dyn-x', false), before);
  assert.equal(after.counts.theater, 1);
  assert.equal(at(after, 'thr:dyn-x').lat, 90);
  // No active flag at all: the pole holder keeps the pole.
  const none = computeLayout({
    nodes: [
      { id: 'thr:a', type: 'theater', attrs: {} },
      { id: 'thr:b', type: 'theater', attrs: {} },
    ],
    edges: [],
  });
  const again = computeLayout(
    { nodes: [...none.nodes, { id: 'thr:0', type: 'theater' }], edges: [] },
    none,
  );
  assert.equal(at(again, 'thr:a').lat, 90);
});

const site = (n, category, { group, salience = 0.4 } = {}) => ({
  id: `sit:dyn-x:node/${n}`,
  type: 'site',
  label: `Site ${n}`,
  group,
  salience,
  status: 'ok',
  attrs: { category },
});

test('sites: one row at +42°, inside their sector, 3° slots, sparse sectors spread out', () => {
  const graph = {
    nodes: [
      site(1, 'airfield', { group: 'air' }),
      site(2, 'airfield', { group: 'air' }),
      site(3, 'airfield', { group: 'air' }),
      site(4, 'power', { group: 'infrastructure' }),
      site(5, 'medical'), // no group: its category's sector (civilian)
      site(6, 'volcano', { group: 'space-lasers' }), // → unclassified
    ],
    edges: [],
  };
  const layout = computeLayout(graph);
  const sectorIndex = (key) => SECTORS.find((s) => s.key === key).index;
  const expect = {
    1: 'air',
    2: 'air',
    3: 'air',
    4: 'infrastructure',
    5: 'civilian',
    6: 'unclassified',
  };
  const slotsSeen = new Set();
  for (const [n, key] of Object.entries(expect)) {
    const id = `sit:dyn-x:node/${n}`;
    const { lat, lon, i } = at(layout, id);
    assert.equal(layout.band[i], 'site');
    assert.equal(lat, 42);
    const sec = SECTORS[sectorIndex(key)];
    assert.equal(layout.sector[i], sec.index, `${id} in ${key}`);
    assert.ok(lonDelta(lon, sec.lonCenter) <= 18 - 1.5 + 1e-4);
    // On the 3° grid: centre ± (k + 0.5) × 3.
    const offset = wrapLon(lon - sec.lonCenter) + 18 - 1.5;
    assert.ok(Math.abs(offset / 3 - Math.round(offset / 3)) < 1e-4);
    slotsSeen.add(`${sec.index}:${Math.round(offset / 3)}`);
    assert.equal(layout.hidden[i], 0);
  }
  assert.equal(slotsSeen.size, 6, 'no two sites share a slot');
  // Three airfields spread over the sector (12° apart), not piled together.
  const air = [1, 2, 3]
    .map((n) => at(layout, `sit:dyn-x:node/${n}`).lon)
    .sort((a, b) => a - b);
  assert.ok(
    air[1] - air[0] >= 9 - 1e-4 && air[2] - air[1] >= 9 - 1e-4,
    `${air}`,
  );
  assert.equal(layout.counts.site, 6);
  assert.equal(layout.overflow.site, 0);
  assert.equal(
    siteSectorKey(
      { attrs: { category: 'port' } },
      SECTORS.map((s) => s.key),
    ),
    'naval',
  );
});

test('sites: a sector draws its 12 most salient; the rest overflow, hidden and counted', () => {
  const nodes = [];
  for (let k = 0; k < 15; k += 1)
    nodes.push(
      site(k, 'power', { group: 'infrastructure', salience: k / 100 }),
    );
  const layout = computeLayout({ nodes, edges: [] });
  assert.equal(layout.overflow.site, 3);
  const hidden = layout.ids.filter((id) => layout.hidden[layout.index.get(id)]);
  assert.deepEqual(
    hidden.sort(),
    ['sit:dyn-x:node/0', 'sit:dyn-x:node/1', 'sit:dyn-x:node/2'],
    'the least salient overflow',
  );
  const lons = layout.ids
    .filter((id) => !layout.hidden[layout.index.get(id)])
    .map((id) => at(layout, id).lon);
  assert.equal(new Set(lons.map((l) => l.toFixed(3))).size, 12);
  // Overflow sits at its sector's centre (so a pick can still show it there).
  const infra = SECTORS.find((s) => s.key === 'infrastructure');
  assert.ok(
    lonDelta(at(layout, 'sit:dyn-x:node/0').lon, infra.lonCenter) < 1e-4,
  );
});

test('sites never move when others arrive or leave, and ignore node order', () => {
  const first = computeLayout({
    nodes: [
      site(1, 'airfield', { group: 'air' }),
      site(2, 'airfield', { group: 'air', salience: 0.2 }),
      site(3, 'port', { group: 'naval' }),
    ],
    edges: [],
  });
  const more = {
    nodes: [
      site(4, 'airfield', { group: 'air', salience: 0.5 }),
      site(1, 'airfield', { group: 'air' }),
      site(2, 'airfield', { group: 'air', salience: 0.2 }),
      site(5, 'airfield', { group: 'air', salience: 0.1 }),
      site(3, 'port', { group: 'naval' }),
    ],
    edges: [],
  };
  const second = computeLayout(more, first);
  for (const n of [1, 2, 3])
    assert.equal(
      at(second, `sit:dyn-x:node/${n}`).lon,
      at(first, `sit:dyn-x:node/${n}`).lon,
    );
  const third = computeLayout(
    { nodes: more.nodes.filter((node) => !node.id.endsWith('/4')), edges: [] },
    second,
  );
  for (const n of [1, 2, 3, 5])
    assert.equal(
      at(third, `sit:dyn-x:node/${n}`).lon,
      at(second, `sit:dyn-x:node/${n}`).lon,
    );
  const shuffledLayout = computeLayout({
    ...more,
    nodes: shuffled(more.nodes),
  });
  assert.deepEqual([...shuffledLayout.pos], [...computeLayout(more).pos]);
});

test('placeSectorRow and sectorRowLon are pure helpers', () => {
  assert.equal(sectorRowLon(0, 0), -16.5);
  assert.equal(sectorRowLon(0, 11), 16.5);
  assert.equal(sectorRowLon(5, 6), wrapLon(180 + 1.5));
  const row = placeSectorRow(
    [
      { id: 'a', sector: 2, salience: 0.5 },
      { id: 'b', sector: 2, salience: 0.5 },
      { id: 'c', sector: 2, salience: 0.9 },
    ],
    {
      slots: 2,
      previous: (id) => (id === 'a' ? { sector: 2, slot: 1 } : null),
    },
  );
  assert.deepEqual(row.overflow, ['b'], 'ties break by id');
  assert.deepEqual(row.slots.get('a'), { sector: 2, slot: 1 }, 'kept its slot');
  assert.deepEqual(row.slots.get('c'), { sector: 2, slot: 0 });
});
