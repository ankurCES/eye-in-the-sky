/**
 * The orb in a simulated wargame session (WG spec §5.3.3–§5.3.6): session
 * bands, frames on the canvas, halos, Blue and Umpire view, captions,
 * arrowheads, and the untrusted-text fixture in designators.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { orientationFacing } from './camera.js';
import { COLORS } from './glyphs.js';
import { FRAME_OUTLINES, UNRECOGNISED_GLYPH } from './glyphPaths.js';
import {
  SESSION_REPORT_LAT,
  WARGAME_BANDS,
  forceSlotsFor,
  sessionProfile,
} from './contextBands.js';
import {
  BANDS,
  SECTORS,
  bandOfNode,
  bandOfType,
  computeLayout,
} from './layout.js';
import { bandCaptions } from './renderer.js';
import { createOrbListView, groupByBand } from './a11y.js';
import { VECTOR_RADIUS_FACTOR, createOrb, nodeRadius } from './orb.js';
import { WG_INK } from './wargameStyles.js';
import {
  fakeCanvas,
  fakeEnv,
  findAll,
  findNode,
  installStubDocument,
  makeGraph,
  stubElement,
} from './fixtures.test.mjs';

let dom;
test.beforeEach(() => {
  dom = installStubDocument();
});
test.afterEach(() => {
  dom.restore();
});

const scenario = (side, state = 'active', extra = {}) => ({
  provenance: 'scenario',
  side,
  state,
  kind_label: side === 'blue' ? 'Artillery battery' : 'Short-range air defence',
  ...extra,
});

/** A session picture: contacts, red and blue forces, engagements, vectors. */
function wargameGraph({ withOwn = true } = {}) {
  const nodes = [];
  const edges = [];
  const add = (node) =>
    nodes.push({
      subtitle: '',
      salience: 0.5,
      status: 'ok',
      attrs: {},
      ...node,
    });
  if (withOwn) {
    add({
      id: 'thr:default',
      type: 'theater',
      label: 'Redmond',
      attrs: { active: true },
    });
    add({ id: 'veh:Drone1', type: 'vehicle', label: 'Drone1', salience: 0.8 });
    add({ id: 'rpt:1', type: 'report', label: 'Report 1' });
  }
  add({
    id: 'trk:T-sam',
    type: 'track',
    label: 'Short-range air defence',
    group: 'air-defense',
    attrs: { confidence: 'probable', scenario: true },
  });
  add({
    id: 'trk:T-gun',
    type: 'track',
    label: 'Towed anti-aircraft gun',
    group: 'air-defense',
    attrs: { confidence: 'probable', scenario: true },
  });
  add({
    id: 'frc:red-sam-1',
    type: 'force',
    label: 'Red SAM 1',
    group: 'air-defense',
    status: 'critical',
    salience: 0.6,
    attrs: scenario('red'),
  });
  add({
    id: 'frc:red-gun-1',
    type: 'force',
    label: 'Red gun 1',
    group: 'air-defense',
    status: 'warn',
    salience: 0.6,
    attrs: scenario('red', 'suppressed'),
  });
  add({
    id: 'frc:blue-art-1',
    type: 'force',
    label: 'Blue artillery 1',
    group: 'ground-forces',
    attrs: scenario('blue'),
  });
  add({
    id: 'frc:blue-art-2',
    type: 'force',
    label: 'Blue artillery 2',
    group: 'ground-forces',
    status: 'critical',
    attrs: scenario('blue', 'destroyed'),
  });
  add({
    id: 'eng:E1',
    type: 'engagement',
    status: 'warn',
    salience: 0.8,
    attrs: {
      kind: 'blue_strike',
      phase: 'proposed',
      target: 'trk:T-gun',
      target_label: 'Towed anti-aircraft gun',
      proposed_at_ms: 2,
    },
  });
  add({
    id: 'eng:E2',
    type: 'engagement',
    status: 'critical',
    attrs: {
      kind: 'red_shot',
      phase: 'adjudicated',
      outcome: 'destroyed',
      consequence: 'own_loss',
      target: withOwn ? 'veh:Drone1' : 'trk:T-sam',
      target_label: 'Drone1',
    },
  });
  add({
    id: 'vec:axis-red-sam-1',
    type: 'vector',
    status: 'critical',
    salience: 0.4,
    attrs: {
      kind: 'axis',
      side: 'red',
      from: 'frc:red-sam-1',
      to: 'frc:blue-art-1',
      length_m: 2100,
    },
  });
  add({
    id: 'vec:cor-1',
    type: 'vector',
    salience: 0.4,
    attrs: {
      kind: 'corridor',
      side: 'blue',
      from: withOwn ? 'veh:Drone1' : 'trk:T-gun',
      to: 'trk:T-gun',
      length_m: 3300,
      proposed: true,
    },
  });
  edges.push(
    { a: 'trk:T-sam', b: 'frc:red-sam-1', kind: 'correlates' },
    { a: 'trk:T-gun', b: 'frc:red-gun-1', kind: 'correlates' },
    { a: 'frc:red-sam-1', b: 'frc:blue-art-1', kind: 'axis' },
    { a: 'eng:E1', b: 'trk:T-gun', kind: 'attacks' },
    { a: 'eng:E1', b: 'frc:blue-art-1', kind: 'launched_by' },
    { a: 'eng:E1', b: 'vec:cor-1', kind: 'along' },
  );
  if (withOwn) {
    edges.push(
      { a: 'eng:E2', b: 'veh:Drone1', kind: 'attacks' },
      { a: 'frc:red-sam-1', b: 'veh:Drone1', kind: 'threatens' },
      { a: 'veh:Drone1', b: 'trk:T-gun', kind: 'ingress' },
      { a: 'rpt:1', b: 'trk:T-sam', kind: 'reports_on' },
    );
  }
  return {
    theater: { id: 'default', epoch: 0 },
    meta: { wargame: { active: true } },
    nodes,
    edges,
  };
}

const latLon = (layout, id) => {
  const i = layout.index.get(id);
  assert.ok(i != null, `${id} is placed`);
  return { lat: layout.lat[i], lon: layout.lon[i], band: layout.band[i] };
};
const lonGap = (a, b) => Math.abs(((a - b + 540) % 360) - 180);
const inSector = (lon, key) => {
  const sector = SECTORS.find((s) => s.key === key);
  return lonGap(lon, sector.lonCenter) <= 18 + 1e-6;
};

test('session bands: red −41°, blue −47°, reports −54°, engagements and vectors −61° (§5.3.5)', () => {
  const layout = computeLayout(wargameGraph());
  assert.equal(layout.profile, 'session');
  for (const id of ['frc:red-sam-1', 'frc:red-gun-1']) {
    const at = latLon(layout, id);
    assert.equal(at.band, 'force_red');
    assert.ok(Math.abs(at.lat - -41) < 1e-4);
    assert.ok(inSector(at.lon, 'air-defense'), `${id}: its category's sector`);
  }
  for (const id of ['frc:blue-art-1', 'frc:blue-art-2']) {
    const at = latLon(layout, id);
    assert.equal(at.band, 'force_blue');
    assert.ok(Math.abs(at.lat - -47) < 1e-4);
    assert.ok(inSector(at.lon, 'ground-forces'), `${id}: its group's sector`);
  }
  // correlates edges run (nearly) vertically: contact and force share a sector.
  assert.ok(
    lonGap(
      latLon(layout, 'trk:T-sam').lon,
      latLon(layout, 'frc:red-sam-1').lon,
    ) < 36,
  );
  assert.ok(Math.abs(latLon(layout, 'rpt:1').lat - SESSION_REPORT_LAT) < 1e-4);
  for (const id of ['eng:E1', 'eng:E2', 'vec:axis-red-sam-1', 'vec:cor-1']) {
    assert.equal(latLon(layout, id).band, 'engagement');
    assert.ok(Math.abs(latLon(layout, id).lat - -61) < 1e-4);
  }
  // An engagement sits at its target's longitude; a vector at its origin's.
  assert.ok(
    lonGap(latLon(layout, 'eng:E1').lon, latLon(layout, 'trk:T-gun').lon) <= 10,
  );
  assert.ok(
    lonGap(latLon(layout, 'eng:E2').lon, latLon(layout, 'veh:Drone1').lon) <=
      10,
  );
  assert.ok(
    lonGap(
      latLon(layout, 'vec:axis-red-sam-1').lon,
      latLon(layout, 'frc:red-sam-1').lon,
    ) <= 10,
  );
  assert.deepEqual(layout.wargame, { vectors: 2, sideNotSet: 0 });
  assert.equal(bandOfType('force'), 'force_red');
  assert.equal(bandOfType('vector'), 'engagement');
  assert.equal(bandOfType('force_blue'), 'other', 'a band key is not a type');
  assert.equal(
    bandOfNode({ type: 'force', attrs: { side: 'blue' } }),
    'force_blue',
  );
  assert.equal(bandOfNode({ type: 'force', attrs: {} }), 'force_red');
  assert.equal(WARGAME_BANDS.force_red.lat, -41);
  assert.equal(BANDS.engagement, WARGAME_BANDS.engagement);
});

test('ISR pictures lay out exactly as before: no session profile, reports at −58°', () => {
  const graph = makeGraph(80);
  const isr = computeLayout(graph);
  assert.equal(isr.profile, 'isr');
  assert.equal(sessionProfile(graph), false);
  for (const key of Object.keys(WARGAME_BANDS))
    assert.equal(isr.counts[key], 0);
  const report = isr.ids.find((id) => id.startsWith('rpt:'));
  assert.ok(Math.abs(latLon(isr, report).lat - -58) < 1e-4);
  const inactive = computeLayout({
    ...graph,
    meta: { ...graph.meta, wargame: { active: false, last: null } },
  });
  assert.deepEqual(
    [...inactive.pos],
    [...isr.pos],
    'an inactive meta block moves nothing',
  );
  // A session with no wargame nodes yet already makes room (§5.3.5).
  const started = computeLayout({
    ...graph,
    meta: { wargame: { active: true } },
  });
  assert.equal(started.profile, 'session');
  assert.ok(Math.abs(latLon(started, report).lat - SESSION_REPORT_LAT) < 1e-4);
  assert.equal(sessionProfile({ meta: { wargame: { active: true } } }), true);
});

test('a crowded sector packs its forces tighter and never hides one; forces keep their slots', () => {
  const nodes = [];
  for (let k = 0; k < 30; k += 1)
    nodes.push({
      id: `frc:red-ad-${k}`,
      type: 'force',
      group: 'air-defense',
      salience: 0.6,
      attrs: scenario('red'),
    });
  const layout = computeLayout({ nodes, edges: [] });
  assert.equal(forceSlotsFor(30), 48);
  assert.equal(forceSlotsFor(12), 12);
  const lons = new Set();
  for (const node of nodes) {
    const at = latLon(layout, node.id);
    assert.ok(Math.abs(at.lat - -41) < 1e-4);
    assert.ok(inSector(at.lon, 'air-defense'));
    lons.add(at.lon.toFixed(3));
    assert.equal(layout.hidden[layout.index.get(node.id)], 0, 'never hidden');
  }
  assert.equal(lons.size, 30, 'one slot each');
  const next = computeLayout(
    {
      nodes: [
        ...nodes,
        {
          id: 'frc:red-radar-1',
          type: 'force',
          group: 'radar-ew',
          attrs: scenario('red'),
        },
      ],
      edges: [],
    },
    layout,
  );
  for (const node of nodes)
    assert.equal(
      latLon(next, node.id).lon,
      latLon(layout, node.id).lon,
      `${node.id} stays`,
    );
});

function mount(graph, { canvas = fakeCanvas(1000, 700) } = {}) {
  const fe = fakeEnv();
  const host = stubElement('div');
  const events = { notice: [] };
  const orb = createOrb(canvas, {
    a11yHost: host,
    env: fe.env,
    onNotice: (notice) => events.notice.push(notice),
  });
  orb.setGraph(graph);
  fe.flush();
  return { orb, fe, host, canvas, events };
}

function face(orb, id) {
  const i = orb.layout.index.get(id);
  const p = [
    orb.layout.pos[i * 3],
    orb.layout.pos[i * 3 + 1],
    orb.layout.pos[i * 3 + 2],
  ];
  orb.camera.setOrientation(orientationFacing(p));
}

const strokedWith = (fe, d) =>
  fe.sprites.filter((c) =>
    c.ctx.calls.some((k) => k[0] === 'stroke' && k[1]?.d === d),
  );
const setsOf = (ctx) => ctx.sets.map(([, value]) => value);

test('on the canvas: frames only on scenario forces, never green, never magenta', () => {
  const graph = wargameGraph({ withOwn: false });
  graph.nodes.push({
    id: 'frc:real',
    type: 'force',
    label: 'Not ours',
    group: 'air-defense',
    status: 'ok',
    attrs: { side: 'red', state: 'active', provenance: 'osm' },
  });
  const { orb, fe, canvas } = mount(graph);
  for (const id of graph.nodes.map((n) => n.id)) {
    face(orb, id);
    orb.select(id);
    orb.renderNow();
  }
  assert.ok(strokedWith(fe, FRAME_OUTLINES.red).length >= 2, 'red diamonds');
  assert.ok(
    strokedWith(fe, FRAME_OUTLINES.blue).length >= 2,
    'blue rectangles',
  );
  assert.ok(
    strokedWith(fe, UNRECOGNISED_GLYPH).length >= 1,
    'no frame on a non-scenario force',
  );
  for (const sprite of strokedWith(fe, UNRECOGNISED_GLYPH))
    assert.ok(setsOf(sprite.ctx).includes(COLORS.unknown), 'lilac');
  for (const sprite of fe.sprites)
    assert.ok(!setsOf(sprite.ctx).includes(COLORS.ok), 'no sprite is green');
  const inks = setsOf(canvas.ctx);
  assert.ok(!inks.includes(COLORS.ok), 'nothing on the stage is green');
  assert.ok(!inks.includes(COLORS.magenta), 'no wargame edge is magenta');
  assert.ok(inks.includes(WG_INK.sand), 'the pending attacks edge is Sand');
  assert.equal(
    orb.visual('frc:real').status,
    'ok',
    'a known type, read through its style',
  );
});

test('halos: a critical red force wears one; a lost blue unit is slashed at 50 % instead', () => {
  const halos = (graph) => {
    const { fe, orb } = mount(graph);
    face(orb, graph.nodes[0].id);
    orb.renderNow();
    return fe.sprites.filter((c) =>
      c.ctx.calls.some((k) => k[0] === 'createRadialGradient'),
    ).length;
  };
  const one = (status, attrs) => ({
    nodes: [
      {
        id: 'frc:x',
        type: 'force',
        label: 'X',
        group: 'air-defense',
        status,
        salience: 0.6,
        attrs,
      },
    ],
    edges: [],
  });
  assert.equal(halos(one('critical', scenario('red'))), 1);
  assert.equal(halos(one('critical', scenario('blue', 'destroyed'))), 0);
  const { fe, orb } = mount(one('critical', scenario('blue', 'destroyed')));
  face(orb, 'frc:x');
  orb.renderNow();
  const frame = strokedWith(fe, FRAME_OUTLINES.blue)[0];
  assert.ok(
    frame.ctx.sets.some(([k, v]) => k === 'globalAlpha' && v === 0.5),
    '50 %',
  );
  assert.ok(
    frame.ctx.sets.some(([k, v]) => k === 'strokeStyle' && v === COLORS.stale),
  );
});

test('vectors are drawn smaller', () => {
  const vector = nodeRadius({ type: 'vector', salience: 0.4 }, 10);
  const track = nodeRadius({ type: 'track', salience: 0.4 }, 10);
  assert.ok(Math.abs(vector - track * VECTOR_RADIUS_FACTOR) < 1e-9);
  assert.ok(VECTOR_RADIUS_FACTOR < 1);
});

test('Blue view hides red on the orb and its twin; a switch is not an arrival; Umpire view restores', () => {
  const graph = wargameGraph();
  const { orb, fe, host, events } = mount(graph);
  assert.deepEqual(orb.view, { umpire: true }, 'Umpire view by default');
  const bluePos = [
    ...['frc:blue-art-1'].map((id) => orb.layout.index.get(id)),
  ].map((i) => [orb.layout.lat[i], orb.layout.lon[i]]);
  const notices = events.notice.length;
  assert.deepEqual(orb.setView({ umpire: false }), { umpire: false });
  fe.flush(60);
  assert.equal(fe.pendingFrames, 0, 'no ripple for a view switch');
  assert.equal(events.notice.length, notices, 'no arrival notice');
  for (const id of ['frc:red-sam-1', 'frc:red-gun-1', 'vec:axis-red-sam-1'])
    assert.equal(orb.layout.index.has(id), false, `${id} hidden`);
  for (const id of [
    'frc:blue-art-1',
    'frc:blue-art-2',
    'eng:E1',
    'vec:cor-1',
    'trk:T-sam',
  ])
    assert.equal(orb.layout.index.has(id), true, `${id} kept`);
  assert.ok(
    !orb.layout.edges.some((e) => e.kind === 'axis' || e.kind === 'correlates'),
  );
  const twinText = findAll(host, (el) => el.attrs?.role === 'option')
    .map((el) => el.textContent)
    .join('\n');
  assert.ok(
    !twinText.includes('Red SAM 1') && twinText.includes('Blue artillery 1'),
  );
  const captions = bandCaptions(orb.layout.counts, {
    wargame: orb.layout.wargame,
    profile: orb.layout.profile,
  });
  assert.ok(
    !captions.some((c) => c.key === 'force_red'),
    'an empty band draws no caption',
  );
  // A poll in Blue view stays filtered.
  orb.setGraph(structuredClone(graph));
  assert.equal(orb.layout.index.has('frc:red-sam-1'), false);
  // Anything but a boolean is ignored.
  assert.deepEqual(orb.setView({ umpire: 'yes' }), { umpire: false });
  assert.deepEqual(orb.setView(), { umpire: false });
  assert.deepEqual(orb.setView({ umpire: true }), { umpire: true });
  assert.equal(orb.layout.index.has('frc:red-sam-1'), true);
  const i = orb.layout.index.get('frc:blue-art-1');
  assert.deepEqual(
    [orb.layout.lat[i], orb.layout.lon[i]],
    bluePos[0],
    'blue kept its slot',
  );
});

test('List view has Blue view too, with the Scenario register tag', () => {
  const host = stubElement('div');
  const view = createOrbListView(host);
  view.setGraph(wargameGraph());
  const rowIds = () =>
    findAll(host, (el) => el.tag === 'tr' && el.attrs?.['data-id']).map(
      (el) => el.attrs['data-id'],
    );
  assert.ok(rowIds().includes('frc:red-sam-1'));
  const row = findNode(
    host,
    (el) => el.tag === 'tr' && el.attrs?.['data-id'] === 'frc:red-sam-1',
  );
  const text = (el) =>
    [
      el.textContent,
      ...(el.children || []).map((k) => (typeof k === 'string' ? k : text(k))),
    ].join('');
  assert.equal(text(row.children[0]), 'Force');
  assert.equal(text(row.children[2]), 'Active');
  assert.equal(row.children[2].attrs['data-status'], 'critical');
  assert.equal(text(row.children[3]), 'Scenario');
  assert.equal(row.children[3].attrs['data-register'], 'scenario');
  assert.equal(
    row.children[3].attrs.title,
    'Set by the wargame, not seen by a sensor.',
  );
  const pending = findNode(
    host,
    (el) => el.tag === 'tr' && el.attrs?.['data-id'] === 'eng:E1',
  );
  assert.equal(pending.children[2].attrs['data-status'], 'sand');
  assert.deepEqual(view.setView({ umpire: false }), { umpire: false });
  assert.ok(!rowIds().includes('frc:red-sam-1'));
  assert.ok(rowIds().includes('frc:blue-art-1'));
  view.setGraph(wargameGraph());
  assert.ok(!rowIds().includes('frc:red-sam-1'), 'a poll stays filtered');
  view.setView({ umpire: true });
  assert.ok(rowIds().includes('frc:red-sam-1'));
  // The twin groups by session band, pole to pole.
  const keys = groupByBand(wargameGraph().nodes).map((g) => g.key);
  assert.deepEqual(keys.slice(-4), [
    'force_red',
    'force_blue',
    'report',
    'engagement',
  ]);
});

test('captions: the session bands say "Simulated"; reports follow their band to −54°', () => {
  const layout = computeLayout(wargameGraph());
  const captions = bandCaptions(layout.counts, {
    wargame: layout.wargame,
    profile: layout.profile,
  });
  const text = (key) => captions.find((c) => c.key === key)?.text;
  assert.equal(text('force_red'), 'Simulated red forces 2');
  assert.equal(text('force_blue'), 'Simulated blue forces 2');
  assert.equal(text('engagement'), 'Simulated engagements 2, vectors 2');
  assert.equal(
    captions.find((c) => c.key === 'report').lat,
    SESSION_REPORT_LAT,
  );
  const isr = computeLayout(makeGraph(60));
  const plain = bandCaptions(isr.counts, {
    wargame: isr.wargame,
    profile: isr.profile,
  });
  assert.ok(!plain.some((c) => Object.hasOwn(WARGAME_BANDS, c.key)));
  assert.equal(plain.find((c) => c.key === 'report').lat, -58);
  // Equipment collapses to ticks between the force rows in a session, and
  // its caption steps aside; an ISR picture keeps it.
  assert.equal(text('equipment'), undefined);
  assert.equal(
    bandCaptions({ equipment: 4 }).find((c) => c.key === 'equipment').text,
    'Equipment 4',
  );
  for (const c of captions)
    assert.ok(!c.text.includes('SIMULATED') && !c.text.includes('·'));
});

/** A canvas whose context logs calls and property writes in one order. */
function orderedCanvas(width = 1000, height = 700) {
  const log = [];
  const state = {};
  const target = {
    canvas: { width, height },
    measureText: (t) => ({ width: String(t).length * 6.5 }),
    createRadialGradient: () => ({ addColorStop() {} }),
    getLineDash: () => [],
  };
  const ctx = new Proxy(target, {
    get(t, key) {
      if (key === 'log') return log;
      if (key in t)
        return typeof t[key] === 'function'
          ? (...a) => (log.push([key, ...a]), t[key](...a))
          : t[key];
      if (key in state) return state[key];
      if (typeof key === 'symbol') return undefined;
      return (...a) => {
        log.push([key, ...a]);
      };
    },
    set(_t, key, value) {
      state[key] = value;
      log.push(['set', key, value]);
      return true;
    },
  });
  const canvas = fakeCanvas(width, height);
  canvas.ctx = ctx;
  canvas.getContext = () => ctx;
  return canvas;
}

/** Stroked sub-paths as {ink, width, points}, read back from the ordered log. */
function strokedPaths(log) {
  const out = [];
  let ink = null;
  let width = 1;
  let sub = [];
  let path = [];
  for (const [name, ...args] of log) {
    if (name === 'set' && args[0] === 'strokeStyle') ink = args[1];
    else if (name === 'set' && args[0] === 'lineWidth') width = args[1];
    else if (name === 'beginPath') path = [];
    else if (name === 'moveTo') path.push((sub = [[args[0], args[1]]]));
    else if (name === 'lineTo') sub.push([args[0], args[1]]);
    else if (name === 'stroke' && !args.length)
      for (const points of path) out.push({ ink, width, points });
  }
  return out;
}

test('an axis edge ends in an arrowhead at its target, in Umpire view only', () => {
  const graph = {
    meta: { wargame: { active: true } },
    nodes: [
      {
        id: 'frc:red-1',
        type: 'force',
        label: 'Red 1',
        group: 'air-defense',
        status: 'warn',
        salience: 0.6,
        attrs: scenario('red'),
      },
      {
        id: 'frc:blue-1',
        type: 'force',
        label: 'Blue 1',
        group: 'ground-forces',
        salience: 0.5,
        attrs: scenario('blue'),
      },
    ],
    edges: [{ a: 'frc:red-1', b: 'frc:blue-1', kind: 'axis' }],
  };
  const canvas = orderedCanvas();
  const { orb } = mount(graph, { canvas });
  face(orb, 'frc:blue-1');
  canvas.ctx.log.length = 0;
  orb.renderNow();
  const warn = strokedPaths(canvas.ctx.log).filter(
    (p) => p.ink === WG_INK.warn,
  );
  const edge = warn.find((p) => p.points.length > 3);
  const arrow = warn.find((p) => p.points.length === 3);
  assert.ok(edge, 'the axis edge is drawn in warn (its unit is warn)');
  assert.equal(edge.width, 1.5);
  assert.ok(arrow, 'with an arrowhead: two wings meeting at a tip');
  const tip = arrow.points[1];
  const target = orb.project('frc:blue-1');
  const d = Math.hypot(tip[0] - target.x, tip[1] - target.y);
  assert.ok(
    d > 2 && d < 30,
    `the tip sits just clear of the target glyph (${d.toFixed(1)} px)`,
  );
  orb.setView({ umpire: false });
  canvas.ctx.log.length = 0;
  orb.renderNow();
  assert.equal(
    strokedPaths(canvas.ctx.log).filter((p) => p.ink === WG_INK.warn).length,
    0,
    'Blue view: no axis, no arrowhead (the red unit is hidden too)',
  );
});

test('untrusted designators render as text: margin, inline, twin and List view (§3.11)', () => {
  const XSS = '<img src=x onerror=alert(1)>';
  const BIDI = '‮evil‬';
  const BIDI_CHARS = /[‪-‮⁦-⁩]/;
  const tags = [];
  const make = dom.doc.createElement;
  dom.doc.createElement = (tag) => {
    tags.push(String(tag).toLowerCase());
    return make(tag);
  };
  const graph = wargameGraph();
  const hostile = (id, patch) =>
    Object.assign(
      graph.nodes.find((n) => n.id === id),
      patch,
    );
  hostile('frc:red-sam-1', {
    label: `${XSS}${BIDI}`,
    attrs: scenario('red', 'active', { kind_label: `${BIDI}${XSS}` }),
  });
  hostile('frc:blue-art-1', { label: `${BIDI}${XSS}` });
  hostile('eng:E1', {
    attrs: {
      ...graph.nodes.find((n) => n.id === 'eng:E1').attrs,
      target_label: `${XSS}${BIDI}`,
    },
  });
  hostile('vec:cor-1', { label: `${XSS}${BIDI}` });
  const { orb, fe, canvas, host } = mount(graph);
  const collect = (el, out = []) => {
    if (!el || typeof el !== 'object') return out;
    if (el.textContent) out.push(el.textContent);
    for (const kid of el.children || [])
      if (typeof kid === 'string') out.push(kid);
      else collect(kid, out);
    return out;
  };
  for (const id of ['frc:red-sam-1', 'frc:blue-art-1', 'eng:E1', 'vec:cor-1']) {
    face(orb, id);
    orb.select(id);
    orb.setOptions({ labelMode: 'margin' });
    fe.flush();
    orb.renderNow();
    const margin = collect(orb.labelLayer).join('\n');
    assert.ok(
      margin.includes(XSS),
      `${id}: literal angle brackets in the margin label`,
    );
    assert.ok(
      !BIDI_CHARS.test(margin),
      `${id}: no bidi control in the margin label`,
    );
    orb.setOptions({ labelMode: 'inline' });
    canvas.ctx.reset();
    orb.renderNow();
    const drawn = canvas.ctx.calls
      .filter((c) => c[0] === 'fillText')
      .map((c) => String(c[1]));
    assert.ok(
      drawn.some((t) => t.includes('<img')),
      `${id}: drawn as text`,
    );
    assert.ok(
      !drawn.some((t) => BIDI_CHARS.test(t)),
      `${id}: canvas text is bidi-safe`,
    );
  }
  const twin = collect(
    findNode(host, (el) => el.attrs?.role === 'listbox'),
  ).join('\n');
  assert.ok(twin.includes(XSS) && !BIDI_CHARS.test(twin));
  const list = stubElement('div');
  createOrbListView(list).setGraph(graph);
  const listText = collect(list).join('\n');
  assert.ok(listText.includes(XSS) && !BIDI_CHARS.test(listText));
  assert.ok(!tags.includes('img'), 'no img element is ever created');
  for (const root of [orb.labelLayer, host, list])
    assert.equal(
      findNode(root, (el) => 'innerHTML' in el),
      null,
      'nothing is set as markup',
    );
  dom.doc.createElement = make;
});

test('not live: wargame glyphs go stale like everything else, and stay never-green', () => {
  const { orb, fe } = mount(wargameGraph({ withOwn: false }));
  orb.setOptions({ pictureStatus: 'offline' });
  for (const id of [
    'frc:red-sam-1',
    'frc:blue-art-1',
    'eng:E2',
    'vec:axis-red-sam-1',
  ]) {
    assert.equal(orb.visual(id).status, 'stale');
    face(orb, id);
    orb.renderNow();
  }
  for (const sprite of fe.sprites)
    assert.ok(!setsOf(sprite.ctx).includes(COLORS.ok));
  assert.ok(
    strokedWith(fe, FRAME_OUTLINES.blue).some((s) =>
      setsOf(s.ctx).includes(COLORS.stale),
    ),
  );
});

test('a session caption too long for the margin takes its short form there, never inside on its glyphs', async () => {
  const { createCamera } = await import('./camera.js');
  const { placeCaptions } = await import('./renderer.js');
  const layout = computeLayout(wargameGraph());
  const captions = bandCaptions(layout.counts, {
    wargame: layout.wargame,
    profile: layout.profile,
  }).filter((c) => c.key === 'force_red' || c.key === 'force_blue');
  assert.deepEqual(
    captions.map((c) => [c.text, c.short]),
    [
      ['Simulated red forces 2', 'Red forces 2'],
      ['Simulated blue forces 2', 'Blue forces 2'],
    ],
  );
  const camera = createCamera();
  const place = (cx) => {
    camera.setViewport({ cx, cy: 330, r: 280 });
    return placeCaptions(captions, camera, {
      cx,
      cy: 330,
      R: 280,
      mode: 'margin',
      measure: (text) => text.length * 6.5,
    });
  };
  // A wide margin: the full caption, outside the limb.
  const wide = place(500);
  assert.ok(wide.length > 0);
  for (const row of wide) {
    assert.equal(row.align, 'right');
    assert.ok(row.text.startsWith('Simulated'), row.text);
  }
  // A 60 px gutter: at −41° and −47° the full captions no longer fit
  // outside the limb, so they take the short form, still in the margin.
  const narrow = place(60 + 280);
  assert.ok(narrow.length > 0);
  for (const row of narrow) {
    assert.equal(row.align, 'right', `${row.text} sits in the margin`);
    assert.ok(['Red forces 2', 'Blue forces 2'].includes(row.text), row.text);
  }
  // Short forms are counts, not new claims.
  const counts = { count: 3, sideNotSet: 1, vectors: 0 };
  const { wargameBandCaption } = await import('./wargameText.js');
  assert.equal(
    wargameBandCaption('force_red', counts, { short: true }),
    'Red forces 2',
  );
  assert.equal(
    wargameBandCaption('engagement', { count: 3, vectors: 3 }, { short: true }),
    'Vectors 3',
  );
  assert.equal(
    wargameBandCaption('force_blue', { count: 0 }, { short: true }),
    null,
  );
});
