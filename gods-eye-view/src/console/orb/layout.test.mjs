import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BANDS,
  CAP_SPAN_DEG,
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
