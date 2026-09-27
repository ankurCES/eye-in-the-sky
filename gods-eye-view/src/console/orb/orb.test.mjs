import test from 'node:test';
import assert from 'node:assert/strict';

import { meanLatLon, orientationFacing, pullWithin } from './camera.js';
import { computeLayout, toVector } from './layout.js';
import { COLORS } from './glyphs.js';
import { RING } from './renderer.js';
import {
  CAP_GLYPH_GAP_PX,
  CAP_NUDGE_MAX_PX,
  IDLE_DEG_PER_S,
  ORB_TIMING,
  ROTATION_STORAGE_KEY,
  createOrb,
  densityFactor,
  detectionsDown,
  effectiveDpr,
  framingOrientation,
  nodeRadius,
  pictureNotLive,
  separateCap,
} from './orb.js';
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

function mount({
  total = 60,
  env: extraEnv = {},
  graph,
  handlers = {},
  width = 1000,
  height = 700,
} = {}) {
  const canvas = fakeCanvas(width, height);
  const fe = fakeEnv(extraEnv);
  const host = stubElement('div');
  const events = { select: [], hover: [], action: [], notice: [], input: 0 };
  const orb = createOrb(canvas, {
    a11yHost: host,
    env: fe.env,
    onSelect: (id) => events.select.push(id),
    onHover: (id) => events.hover.push(id),
    onAction: (action) => events.action.push(action),
    onNotice: (notice) => events.notice.push(notice),
    onInput: () => {
      events.input += 1;
    },
    ...handlers,
  });
  const g = graph ?? makeGraph(total);
  orb.setGraph(g);
  fe.flush();
  return { orb, canvas, fe, host, events, graph: g };
}

const listbox = (host) => findNode(host, (el) => el.attrs?.role === 'listbox');

/** Turn the orb so `id` faces the camera (instantly). */
function face(orb, id) {
  const i = orb.layout.index.get(id);
  const p = [
    orb.layout.pos[i * 3],
    orb.layout.pos[i * 3 + 1],
    orb.layout.pos[i * 3 + 2],
  ];
  orb.camera.setOrientation(orientationFacing(p));
}

test('module helpers: density, radius, DPR cap, detections feed', () => {
  assert.equal(densityFactor(5), 1.4 - 5 / 400);
  assert.equal(densityFactor(300), 0.65);
  assert.equal(densityFactor(1000), 0.65);
  assert.equal(
    nodeRadius({ type: 'vehicle', salience: 0 }, 300),
    10,
    'vehicles are at least 10 px',
  );
  assert.ok(
    Math.abs(nodeRadius({ type: 'track', salience: 1 }, 0) - 16.8) < 1e-9,
  );
  assert.equal(effectiveDpr(2, 1000, 700), 2);
  assert.equal(effectiveDpr(2, 2560, 1440), 1.5, 'over 4.5 MP drops to 1.5');
  assert.equal(effectiveDpr(3, 100, 100), 2, 'capped at 2');
  assert.equal(effectiveDpr(undefined, 100, 100), 1);
  assert.equal(
    detectionsDown({
      meta: { feeds: { contacts: { ok: false, status: 'critical' } } },
    }),
    true,
  );
  assert.equal(
    detectionsDown({
      nodes: [{ id: 'feed:contacts', type: 'feed', status: 'critical' }],
    }),
    true,
  );
  assert.equal(detectionsDown(makeGraph(20)), false);
});

test('mounts under the stub DOM: aria-hidden canvas, listbox twin, label layer', () => {
  const { orb, canvas, host } = mount();
  assert.equal(canvas.attrs['aria-hidden'], 'true');
  assert.equal(canvas.attrs.tabindex, '-1');
  const box = listbox(host);
  assert.ok(box, 'listbox in the a11y host');
  assert.equal(box.attrs['aria-label'], 'Entities on the orb');
  assert.equal(box.attrs.tabindex, '0');
  const groups = findAll(box, (el) => el.attrs?.role === 'group');
  assert.ok(groups.length >= 6, 'one group per populated band');
  assert.ok(groups.some((g) => /^Contacts, \d+$/.test(g.attrs['aria-label'])));
  const options = findAll(box, (el) => el.attrs?.role === 'option');
  assert.equal(options.length, orb.layout.n);
  assert.ok(orb.labelLayer, 'margin label layer sits beside the canvas');
  assert.equal(orb.labelLayer.parentNode, canvas.parentNode);
  assert.equal(canvas.width, 2000, 'backing store at DPR 2');
});

test('render on demand: frames stop when nothing changes (no ambient animation)', () => {
  const { orb, fe } = mount();
  assert.equal(fe.pendingFrames, 0, 'no frame pending after the first draw');
  const frames = orb.stats().frames;
  fe.advance(10_000);
  assert.equal(
    orb.stats().frames,
    frames,
    'nothing redraws in 10 s of stillness',
  );
  orb.highlight(['veh:Drone1']);
  assert.equal(fe.pendingFrames, 1);
  fe.flush();
  assert.equal(fe.pendingFrames, 0);
  assert.equal(
    orb.stats().frames,
    frames + 1,
    'a state change costs exactly one frame',
  );
});

test('idle rotation: only after 30 s without input, 2°/s, never when selected or reduced motion', () => {
  const { orb, fe, canvas } = mount();
  fe.advance(ORB_TIMING.idleAfter - 5000);
  assert.equal(orb.stats().idle, false);
  fe.advance(6000, { frames: false });
  assert.equal(orb.stats().idle, true, 'idle after 30 s');
  const q0 = orb.camera.q;
  fe.flush(1);
  for (let k = 0; k < 30; k += 1) {
    fe.step(100);
    fe.flush(1);
  }
  const q1 = orb.camera.q;
  const angle =
    2 *
    Math.acos(
      Math.min(
        1,
        Math.abs(q0[0] * q1[0] + q0[1] * q1[1] + q0[2] * q1[2] + q0[3] * q1[3]),
      ),
    );
  const expected = IDLE_DEG_PER_S * 3 * (Math.PI / 180);
  assert.ok(
    Math.abs(angle - expected) < expected * 0.15,
    `≈ 6° in 3 s, got ${(angle * 180) / Math.PI}°`,
  );
  // Input stops it.
  canvas.fire('pointerenter', {});
  canvas.fire('pointermove', { offsetX: 5, offsetY: 5, pointerId: 1 });
  assert.equal(orb.stats().idle, false);
  fe.flush();
  assert.equal(fe.pendingFrames, 0, 'rotation stops');
  canvas.fire('pointerleave', {});
  // Selection blocks it.
  orb.select('veh:Drone1');
  fe.advance(ORB_TIMING.idleAfter + 100);
  assert.equal(orb.stats().idle, false);
  orb.select(null);
  orb.setOptions({ reducedMotion: true });
  fe.advance(ORB_TIMING.idleAfter + 100);
  assert.equal(orb.stats().idle, false, 'never under reduced motion');
  orb.setOptions({ reducedMotion: false, idle: false });
  fe.advance(ORB_TIMING.idleAfter + 100);
  assert.equal(
    orb.stats().idle,
    false,
    'the shell can switch it off (tracking)',
  );
});

test('Space toggles idle rotation and the choice is remembered', () => {
  const store = new Map();
  const storage = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
  };
  const { orb, host } = mount({ env: { storage } });
  const box = listbox(host);
  box.fire('keydown', { key: ' ' });
  assert.equal(store.get(ROTATION_STORAGE_KEY), 'off');
  assert.equal(orb.stats().rotation, false);
  const again = mount({ env: { storage } });
  assert.equal(
    again.orb.stats().rotation,
    false,
    'a new orb reads the stored choice',
  );
  const blocked = {
    getItem() {
      throw new Error('blocked');
    },
    setItem() {
      throw new Error('blocked');
    },
  };
  const safe = mount({ env: { storage: blocked } });
  listbox(safe.host).fire('keydown', { key: ' ' });
  assert.equal(
    safe.orb.stats().rotation,
    false,
    'blocked storage still toggles',
  );
});

test('filter: non-matches drop to 15 % and 0.8× size, criticals never below 40 %', () => {
  const { orb, graph } = mount({ total: 120 });
  const critical = graph.nodes.find(
    (n) => n.status === 'critical' && n.type === 'track',
  );
  const plain = graph.nodes.find(
    (n) => n.status === 'ok' && n.type === 'track',
  );
  const match = graph.nodes.find((n) => n.type === 'vehicle');
  const before = orb.visual(plain.id).r;
  const count = orb.filter((node) => node.id === match.id);
  assert.equal(count, 1);
  assert.equal(orb.visual(match.id).alpha, 1);
  assert.ok(orb.visual(match.id).ring & RING.MATCH);
  assert.equal(orb.visual(plain.id).alpha, 0.15);
  assert.ok(
    orb.visual(critical.id).alpha >= 0.4,
    'critical floor under a filter',
  );
  orb.renderNow();
  assert.ok(Math.abs(orb.visual(plain.id).r - before * 0.8) < 1e-6);
  // Focus dims others to 35 % — criticals still ≥ 40 %.
  orb.filter(null);
  orb.focus([match.id], { by: 'analyst' });
  assert.equal(orb.visual(plain.id).alpha, 0.35);
  assert.ok(orb.visual(critical.id).alpha >= 0.4);
  assert.equal(orb.visual(match.id).ink, 'analyst');
  assert.ok(orb.visual(match.id).ring & RING.FOCUS_ANALYST);
  // Chip highlight: others at 70 %; combined states take the minimum.
  orb.highlight([critical.id], { by: 'analyst' });
  assert.equal(orb.visual(plain.id).alpha, 0.35);
  orb.focus(null);
  assert.equal(orb.visual(plain.id).alpha, 0.7);
  // A throwing predicate is a non-match, never a crash.
  assert.equal(
    orb.filter(() => {
      throw new Error('bad predicate');
    }),
    0,
  );
});

test('select never resizes the backing store; setViewport moves the projection', () => {
  const { orb, canvas, fe } = mount();
  const size = [canvas.width, canvas.height];
  orb.select('trk:T-0000000');
  orb.select('veh:Drone2');
  orb.setViewport({ cx: 500, cy: 250, r: 200 });
  fe.advance(1000);
  assert.deepEqual([canvas.width, canvas.height], size);
  assert.deepEqual(orb.camera.viewport, { cx: 500, cy: 250, r: 200 });
  orb.setViewport(null);
  fe.advance(1000);
  assert.notDeepEqual(
    orb.camera.viewport,
    { cx: 500, cy: 250, r: 200 },
    'null restores the automatic viewport',
  );
  // resize() is the only thing that touches the backing store.
  canvas.clientWidth = 1200;
  orb.resize();
  assert.equal(canvas.width, 2400);
});

test('select eases a node to the front only when it is more than 50° off centre', () => {
  const { orb, fe, events } = mount();
  face(orb, 'veh:Drone1');
  fe.flush();
  const q = orb.camera.q;
  orb.select('veh:Drone1');
  fe.advance(ORB_TIMING.camera + 50);
  assert.deepEqual(orb.camera.q, q, 'a facing node does not move the camera');
  const far = orb.layout.ids.find(
    (id) => orb.camera.angleFromFront(posOf(orb, id)) > 120,
  );
  orb.select(far);
  assert.equal(fe.pendingFrames, 1);
  fe.advance(ORB_TIMING.camera + 50);
  assert.ok(
    orb.camera.angleFromFront(posOf(orb, far)) < 1,
    'eased to the front',
  );
  assert.equal(fe.pendingFrames, 0, 'and the loop stops afterwards');
  assert.deepEqual(
    events.select,
    [],
    'programmatic select does not call onSelect',
  );
});

function posOf(orb, id) {
  const i = orb.layout.index.get(id);
  return [
    orb.layout.pos[i * 3],
    orb.layout.pos[i * 3 + 1],
    orb.layout.pos[i * 3 + 2],
  ];
}

test('pointer: hover rings a node, click selects it, click on empty space clears', () => {
  const { orb, canvas, fe, events } = mount();
  face(orb, 'veh:Drone1');
  orb.renderNow();
  const p = orb.project('veh:Drone1');
  const x = p.x - 10;
  const y = p.y - 20; // client → canvas offset (the stub rect is at 10, 20)
  canvas.fire('pointermove', { offsetX: x, offsetY: y, pointerId: 1 });
  assert.equal(events.hover.at(-1), 'veh:Drone1');
  assert.ok(orb.visual('veh:Drone1').ring & RING.HOVER);
  canvas.fire('pointerdown', { offsetX: x, offsetY: y, pointerId: 1 });
  canvas.fire('pointerup', { offsetX: x, offsetY: y, pointerId: 1 });
  assert.equal(events.select.at(-1), 'veh:Drone1');
  assert.ok(orb.visual('veh:Drone1').ring & RING.SELECT);
  canvas.fire('pointerdown', { offsetX: 3, offsetY: 3, pointerId: 1 });
  canvas.fire('pointerup', { offsetX: 3, offsetY: 3, pointerId: 1 });
  assert.equal(events.select.at(-1), null);
  // A drag rotates and never selects.
  const q = orb.camera.q;
  canvas.fire('pointerdown', { offsetX: 500, offsetY: 300, pointerId: 2 });
  canvas.fire('pointermove', { offsetX: 560, offsetY: 310, pointerId: 2 });
  canvas.fire('pointerup', { offsetX: 560, offsetY: 310, pointerId: 2 });
  assert.notDeepEqual(orb.camera.q, q);
  assert.equal(events.select.length, 2);
  assert.ok(events.input > 0, 'stage input is reported');
  // Wheel zooms within 0.8–2.4.
  for (let k = 0; k < 40; k += 1) canvas.fire('wheel', { deltaY: -200 });
  assert.equal(orb.camera.zoom, 2.4);
  fe.flush();
});

test('keyboard on the listbox: step, band jump, inspect, actions and Esc', () => {
  const { orb, host, events } = mount();
  const box = listbox(host);
  box.fire('focus', {});
  const first = box.attrs['aria-activedescendant'];
  assert.ok(first, 'focusing the listbox activates an entity');
  let prevented = 0;
  const key = (k, extra = {}) =>
    box.fire('keydown', {
      key: k,
      preventDefault: () => (prevented += 1),
      stopPropagation() {},
      ...extra,
    });
  key('ArrowDown');
  assert.notEqual(box.attrs['aria-activedescendant'], first);
  key('PageDown');
  key('Enter');
  const picked = events.select.at(-1);
  assert.ok(picked, 'Enter inspects');
  assert.equal(orb.selected, picked);
  const option = findNode(box, (el) => el.attrs?.['aria-selected'] === 'true');
  assert.equal(option.attrs.id, box.attrs['aria-activedescendant']);
  key('a');
  assert.deepEqual(events.action.at(-1), { action: 'ask', id: picked });
  key('t');
  assert.deepEqual(events.action.at(-1), { action: 'track', id: picked });
  key('l');
  assert.deepEqual(events.action.at(-1), { action: 'list' });
  key('Escape');
  assert.equal(events.select.at(-1), null, 'Esc clears the selection');
  assert.equal(events.action.at(-1).cleared, 'selection');
  const zoom = orb.camera.zoom;
  key('+');
  assert.ok(orb.camera.zoom > zoom);
  key('ArrowLeft', { shiftKey: true });
  const handled = prevented;
  key('k', { metaKey: true });
  assert.equal(prevented, handled, 'modified keys are left to the shell (⌘K)');
  box.fire('blur', {});
});

test('arrivals: a new node behind gives a limb notice; a burst gives one batch notice', () => {
  const graph = makeGraph(60, { seed: 4 });
  const { orb, fe, events } = mount({ graph });
  face(orb, 'veh:Drone1');
  const behindLon =
    ((orb.layout.lon[orb.layout.index.get('veh:Drone1')] + 180 + 540) % 360) -
    180;
  const next = { ...graph, nodes: [...graph.nodes] };
  // Place a contact on the far side by choosing its sector.
  const sector = [
    'air-defense',
    'radar-ew',
    'c2',
    'ground-forces',
    'logistics',
    'infrastructure',
    'naval',
    'air',
    'civilian',
    'unclassified',
  ];
  const group = sector[Math.round(((behindLon + 360) % 360) / 36) % 10];
  next.nodes.push({
    id: 'trk:NEW-behind',
    type: 'track',
    label: 'New one',
    group,
    salience: 0.5,
    status: 'warn',
    attrs: {},
  });
  orb.setGraph(next, { added: ['trk:NEW-behind'], removed: [], updated: [] });
  const notice = events.notice.at(-1);
  assert.equal(notice?.kind, 'new_behind');
  assert.equal(notice.text, '1 new behind');
  assert.equal(orb.visual('trk:NEW-behind').isNew, true);
  assert.ok(fe.pendingFrames > 0, 'the ripple animates');
  fe.advance(ORB_TIMING.ripple + 100);
  fe.advance(ORB_TIMING.ripple + 100);
  assert.equal(fe.pendingFrames, 0, 'and then stops');
  const burst = { ...next, nodes: [...next.nodes] };
  const added = [];
  for (let k = 0; k < 6; k += 1) {
    burst.nodes.push({
      id: `trk:BURST-${k}`,
      type: 'track',
      label: `B${k}`,
      group: sector[k],
      salience: 0.3,
      status: 'ok',
      attrs: {},
    });
    added.push(`trk:BURST-${k}`);
  }
  orb.setGraph(burst);
  assert.equal(events.notice.at(-1).kind, 'batch');
  assert.equal(events.notice.at(-1).text, '7 new contacts');
  // "New" expires after 60 s.
  fe.advance(ORB_TIMING.newTag + 100);
  assert.equal(orb.visual('trk:BURST-0').isNew, false);
  // A first picture never counts as arrivals.
  const fresh = mount({ graph: makeGraph(40) });
  assert.equal(fresh.events.notice.length, 0);
});

test('frame-budget ladder: three slow frames step down, fast frames recover', () => {
  let clock = 0;
  const { orb, fe } = mount({ env: { now: () => (clock += 3) } });
  assert.equal(orb.stats().ladder, 0);
  for (let k = 0; k < 3; k += 1) orb.renderNow();
  assert.equal(
    orb.stats().ladder,
    1,
    'slow frames (> 6 ms) drop back-hemisphere detail',
  );
  for (let k = 0; k < 3; k += 1) orb.renderNow();
  assert.equal(orb.stats().ladder, 2, 'then cut the label budget');
  fe.flush();
});

test('paused (tracking), hidden or off screen: no frames until resumed', () => {
  const { orb, fe } = mount();
  orb.setOptions({ paused: true });
  fe.flush();
  orb.highlight(['veh:Drone1']);
  assert.equal(fe.pendingFrames, 0);
  orb.setOptions({ paused: false });
  assert.equal(fe.pendingFrames, 1);
  fe.flush();
  let observer;
  const io = class {
    constructor(cb) {
      observer = cb;
    }
    observe() {}
    disconnect() {}
  };
  const watched = mount({ env: { IntersectionObserver: io } });
  observer([{ isIntersecting: false }]);
  watched.orb.highlight(['veh:Drone2']);
  assert.equal(watched.fe.pendingFrames, 0);
  observer([{ isIntersecting: true }]);
  assert.equal(watched.fe.pendingFrames, 1);
});

test('project(), snapshot(), onFrame() and the offline watermark', () => {
  const { orb, canvas, fe } = mount();
  face(orb, 'veh:Drone1');
  const p = orb.project('veh:Drone1');
  assert.equal(p.front, true);
  assert.ok(
    Math.abs(p.x - (10 + orb.camera.viewport.cx)) < 1e-6,
    'client coordinates include the canvas rect',
  );
  assert.equal(orb.project('nope'), null);
  let frames = 0;
  const off = orb.onFrame(() => (frames += 1));
  orb.renderNow();
  assert.equal(frames, 1);
  off();
  orb.renderNow();
  assert.equal(frames, 1);
  const shot = orb.snapshot(64);
  assert.ok(shot && shot.width === 128, '64 px at DPR 2');
  assert.equal(shot.ctx.count('drawImage'), 1);
  orb.setOptions({
    pictureStatus: 'offline',
    lastLiveAt: Date.UTC(2026, 0, 1, 14, 31, 5),
  });
  canvas.ctx.reset();
  orb.renderNow();
  const texts = canvas.ctx.calls
    .filter((c) => c[0] === 'fillText')
    .map((c) => c[1]);
  assert.ok(texts.includes('Last picture 14:31:05Z, not live.'));
  assert.equal(
    orb.visual('veh:Drone1').status,
    'stale',
    'every node takes the stale styling',
  );
  fe.flush();
});

test('a stale picture (failed poll after a load) restyles like offline, then recovers', () => {
  // Review: intelStore reports "stale", never "offline", once a picture has
  // loaded, so the orb kept drawing live criticals under "not live" copy.
  assert.equal(pictureNotLive('stale'), true);
  assert.equal(pictureNotLive('offline'), true);
  assert.equal(pictureNotLive('unauthorized'), true);
  assert.equal(pictureNotLive('live'), false);
  assert.equal(pictureNotLive('loading'), false);
  const { orb, canvas, graph, fe } = mount();
  const critical = graph.nodes.find((n) => n.status === 'critical');
  assert.ok(critical, 'fixture has a critical node');
  face(orb, 'veh:Drone1');
  orb.renderNow();
  const inks = () => canvas.ctx.sets.filter((c) => /Style$/.test(c[0]));
  assert.ok(
    inks().some((c) => c[1] === COLORS.magenta),
    'live: the flying edge is magenta',
  );
  assert.equal(orb.visual(critical.id).status, 'critical');
  orb.setOptions({
    pictureStatus: 'stale',
    lastLiveAt: Date.UTC(2026, 0, 1, 12, 36, 47),
  });
  canvas.ctx.reset();
  orb.renderNow();
  const texts = canvas.ctx.calls
    .filter((c) => c[0] === 'fillText')
    .map((c) => c[1]);
  assert.ok(texts.includes('Last picture 12:36:47Z, not live.'));
  for (const node of graph.nodes)
    assert.equal(orb.visual(node.id).status, 'stale', `${node.id} is stale`);
  assert.ok(
    !inks().some((c) => c[1] === COLORS.magenta || c[1] === COLORS.ok),
    'not live: no magenta or green ink on the canvas',
  );
  orb.setOptions({ pictureStatus: 'live' });
  canvas.ctx.reset();
  orb.renderNow();
  assert.equal(orb.visual(critical.id).status, 'critical', 'live again');
  assert.ok(
    !canvas.ctx.calls.some(
      (c) => c[0] === 'fillText' && /not live/.test(String(c[1])),
    ),
  );
  fe.flush();
});

test('separateCap: crowded polar-cap glyphs are pushed apart; other bands stay put', () => {
  // Near the pole a band ring projects smaller than its glyphs (review:
  // feed bars stacked, two place plus signs merged into "#").
  const band = ['theater', 'feed', 'feed', 'poi', 'vehicle', 'track', 'feed'];
  const r = [10, 8, 8, 9, 12, 8, 8];
  const proj = new Float32Array([
    ...[500, 100, 0.5, 1], // theater on the pole
    ...[503, 101, 0.5, 1], // feed
    ...[500, 100, 0.5, 1], // feed, coincident with the theater
    ...[497, 104, 0.5, 1], // place
    ...[500, 124, 0.5, 1], // own force: an obstacle, never moved
    ...[501, 100, 0.5, 1], // a contact: not a cap band, never moved
    ...[500, 100, -0.5, 1], // a feed on the back hemisphere: never moved
  ]);
  const before = Float32Array.from(proj);
  const nudge = new Float32Array(band.length * 2).fill(99);
  const moved = separateCap(proj, band.length, band, (i) => r[i], nudge);
  assert.equal(moved, 4, 'the theater, both feeds and the place move');
  for (const i of [4, 5, 6]) {
    assert.equal(proj[i * 4], before[i * 4], `node ${i} x kept`);
    assert.equal(proj[i * 4 + 1], before[i * 4 + 1], `node ${i} y kept`);
    assert.equal(nudge[i * 2], 0);
    assert.equal(nudge[i * 2 + 1], 0);
  }
  const cap = [0, 1, 2, 3, 4];
  for (const a of cap)
    for (const b of cap) {
      if (a >= b) continue;
      const d = Math.hypot(
        proj[a * 4] - proj[b * 4],
        proj[a * 4 + 1] - proj[b * 4 + 1],
      );
      assert.ok(
        d - r[a] - r[b] >= CAP_GLYPH_GAP_PX - 0.01,
        `glyphs ${a} and ${b} are ${(d - r[a] - r[b]).toFixed(2)} px clear`,
      );
    }
  for (const i of [0, 1, 2, 3]) {
    const ox = nudge[i * 2];
    const oy = nudge[i * 2 + 1];
    assert.ok(Math.hypot(ox, oy) <= CAP_NUDGE_MAX_PX + 1e-4, 'bounded');
    assert.ok(Math.abs(proj[i * 4] - (before[i * 4] + ox)) < 1e-3);
    assert.ok(Math.abs(proj[i * 4 + 1] - (before[i * 4 + 1] + oy)) < 1e-3);
  }
  // Deterministic: the same frame gives the same offsets.
  const again = new Float32Array(band.length * 2);
  separateCap(Float32Array.from(before), band.length, band, (i) => r[i], again);
  assert.deepEqual([...again], [...nudge]);
  // Glyphs already clear stay exactly where they project; old offsets reset.
  const clear = new Float32Array([
    ...[400, 100, 0.5, 1],
    ...[440, 100, 0.5, 1],
    ...[480, 100, 0.5, 1],
  ]);
  const kept = Float32Array.from(clear);
  const reset = new Float32Array(6).fill(7);
  assert.equal(
    separateCap(clear, 3, ['feed', 'poi', 'feed'], () => 10, reset),
    0,
  );
  assert.deepEqual([...clear], [...kept]);
  assert.deepEqual([...reset], [0, 0, 0, 0, 0, 0]);
});

test('first-run picture: theater, feed and place glyphs never overlap, and picking follows the drawn point', () => {
  // Review: in the first-run picture the theater, 5 feeds and 3 places
  // collapsed into one small cluster at the north cap.
  const feeds = ['sim', 'real_data', 'theater', 'contacts', 'mission_state'];
  const places = ['North Field', 'East Field', 'South Field'];
  const graph = {
    nodes: [
      {
        id: 'thr:default',
        type: 'theater',
        label: 'Redmond (AirSim default)',
        salience: 0.6,
        status: 'ok',
        attrs: { active: true },
      },
      ...feeds.map((f) => ({
        id: `feed:${f}`,
        type: 'feed',
        label: f,
        status: f === 'real_data' ? 'warn' : 'ok',
        salience: f === 'real_data' ? 0.7 : 0.2,
      })),
      ...places.map((p) => ({
        id: `poi:default:${p}`,
        type: 'poi',
        label: p,
        subtitle: 'Redmond (AirSim default)',
        salience: 0.5,
      })),
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        label: 'Drone1',
        status: 'ok',
        salience: 0.8,
        attrs: { fuel_pct: 100 },
      },
    ],
    edges: [],
    meta: { feeds: {} },
  };
  // Wide (orb column at 1440×900), compact and phone widths.
  for (const [width, height] of [
    [740, 840],
    [560, 700],
    [375, 520],
  ]) {
    const { orb, canvas, fe, events } = mount({ graph, width, height });
    orb.renderNow();
    const glyphs = graph.nodes.map((node) => {
      const i = orb.layout.index.get(node.id);
      const scale = orb.camera.project([
        orb.layout.pos[i * 3],
        orb.layout.pos[i * 3 + 1],
        orb.layout.pos[i * 3 + 2],
      ]).scale;
      const r = orb.visual(node.id).r * scale;
      return {
        id: node.id,
        ...orb.project(node.id),
        r: node.type === 'vehicle' ? r * 1.45 + 2 : r,
      };
    });
    const front = glyphs.filter((g) => g.front);
    assert.equal(front.length, graph.nodes.length, 'the cap faces the camera');
    for (const a of front)
      for (const b of front) {
        if (a.id >= b.id) continue;
        const clearance = Math.hypot(a.x - b.x, a.y - b.y) - a.r - b.r;
        assert.ok(
          clearance >= CAP_GLYPH_GAP_PX - 0.05,
          `${width}×${height}: ${a.id} and ${b.id} are ${clearance.toFixed(1)} px clear`,
        );
      }
    // A click on a place's drawn (nudged) point selects that place.
    const place = glyphs.find((g) => g.id === 'poi:default:North Field');
    const x = place.x - 10;
    const y = place.y - 20;
    canvas.fire('pointerdown', { offsetX: x, offsetY: y, pointerId: 1 });
    canvas.fire('pointerup', { offsetX: x, offsetY: y, pointerId: 1 });
    assert.equal(events.select.at(-1), 'poi:default:North Field');
    fe.flush();
    orb.destroy();
  }
});

test('margin labels are pooled DOM; a filter overflow reads "N more match"', () => {
  const { orb, fe } = mount({ total: 200 });
  orb.filter((node) => node.type === 'track');
  fe.flush();
  const labels = findAll(
    orb.labelLayer,
    (el) => el.className === 'ic-orb-label' && el.attrs.hidden === undefined,
  );
  assert.ok(labels.length > 0 && labels.length <= 12);
  assert.ok(labels.every((el) => el.children[0].textContent));
  const more = findNode(orb.labelLayer, (el) => el.className === 'ic-orb-more');
  assert.match(more.textContent, /^\d+ more match$/);
  orb.setOptions({ labelMode: 'inline', labelBudget: 6 });
  fe.flush();
  const shown = findAll(
    orb.labelLayer,
    (el) => el.className === 'ic-orb-label' && el.attrs.hidden === undefined,
  );
  assert.equal(shown.length, 0, 'inline mode draws labels on the canvas');
});

test('empty picture: captions still speak and nothing crashes', () => {
  const { orb, canvas, fe } = mount({ graph: { nodes: [], edges: [] } });
  canvas.ctx.reset();
  orb.renderNow();
  const texts = canvas.ctx.calls
    .filter((c) => c[0] === 'fillText')
    .map((c) => c[1]);
  assert.ok(texts.includes('Contacts: none reported yet'));
  orb.setGraph(null);
  fe.flush();
  assert.equal(orb.layout.n, 0);
});

test('destroy removes listeners, the twin and the label layer, and stops the loop', () => {
  const { orb, canvas, host, fe } = mount();
  orb.highlight(['veh:Drone1']);
  orb.destroy();
  assert.equal(Object.values(canvas.listeners).flat().length, 0);
  assert.equal(listbox(host), null);
  assert.equal(orb.labelLayer.parentNode, null);
  fe.flush();
  orb.setGraph(makeGraph(10));
  orb.highlight(['veh:Drone2']);
  assert.equal(fe.pendingFrames, 0, 'a destroyed orb never schedules');
});

/** View elevation (deg) and horizontal offset of the framing target. */
function framedAt(orb) {
  const { layout } = orb;
  const points = [];
  const front = ['vehicle', 'mission', 'track'];
  const keep = layout.nodes.some((n) => front.includes(n.type))
    ? (n) => front.includes(n.type)
    : () => true;
  layout.nodes.forEach((node, i) => {
    if (keep(node))
      points.push({
        lat: layout.lat[i],
        lon: layout.lon[i],
        weight: node.salience,
      });
  });
  const mean = meanLatLon(points);
  const lead = orb.layout.ids
    .filter((id) => id.startsWith('veh:'))
    .sort(
      (a, b) =>
        orb.layout.nodes[orb.layout.index.get(b)].salience -
        orb.layout.nodes[orb.layout.index.get(a)].salience,
    )[0];
  let target = toVector(mean.lat, mean.lon);
  if (lead) target = pullWithin(target, posOf(orb, lead), 60);
  const v = orb.camera.toView(target);
  return { elevation: Math.asin(v[1]) / (Math.PI / 180), x: v[0], lead };
}

const sparse = () => {
  const graph = makeGraph(14, { tracks: 0 });
  graph.nodes = graph.nodes.filter(
    (n) => n.type !== 'mission' && n.id !== 'veh:Drone2',
  );
  graph.edges = graph.edges.filter((e) =>
    [e.a, e.b].every((id) => graph.nodes.some((n) => n.id === id)),
  );
  return graph;
};

test('the first picture opens on its populated latitudes, 12° above centre', () => {
  // Sparse: theater, feeds, places, one vehicle. The vehicle is framed.
  const { orb } = mount({ graph: sparse() });
  const vehicle = orb.camera.project(posOf(orb, 'veh:Drone1'));
  const { cx, cy, r } = orb.camera.viewport;
  assert.ok(Math.abs(vehicle.x - cx) < 1, 'vehicle centred horizontally');
  assert.ok(vehicle.y < cy && vehicle.y > cy - r * 0.4, 'just above centre');
  const at = framedAt(orb);
  assert.ok(Math.abs(at.elevation - 12) < 1e-6);
  // Everything in the north cap now faces the viewer, well inside the limb.
  for (const id of orb.layout.ids) {
    if (!/^(thr|feed|poi|veh):/.test(id)) continue;
    const p = orb.camera.project(posOf(orb, id));
    assert.ok(p.z > 0.4, `${id} faces the viewer`);
    assert.ok(Math.hypot(p.x - cx, p.y - cy) < r * 0.95, `${id} off the rim`);
  }
  // A rich picture frames its contacts (salience-weighted), not the pole.
  const rich = mount({ graph: makeGraph(160, { seed: 5 }) });
  const richAt = framedAt(rich.orb);
  assert.ok(Math.abs(richAt.elevation - 12) < 1e-6);
  assert.ok(Math.abs(richAt.x) < 1e-6);
  const tracks = rich.orb.layout.ids.filter((id) => id.startsWith('trk:'));
  const facing = tracks.filter(
    (id) => rich.orb.camera.project(posOf(rich.orb, id)).z > 0,
  );
  assert.ok(
    facing.length >= tracks.length * 0.4,
    `${facing.length} of ${tracks.length} contacts face the viewer`,
  );
  // The lead vehicle stays on the labelled side, however the contacts fall.
  const lead = rich.orb.camera.project(posOf(rich.orb, richAt.lead));
  assert.ok(lead.z >= 0.3, `lead vehicle z ${lead.z}`);
  const north = rich.orb.camera.toView([0, 1, 0]);
  assert.ok(Math.abs(north[0]) < 1e-6 && north[1] > 0, 'north stays up');
});

test('framingOrientation: front-worthy nodes first, else everything, else rest', () => {
  const rest = [1, 0, 0, 0];
  assert.deepEqual(framingOrientation(computeLayout(null), rest), rest);
  const capOnly = computeLayout({
    nodes: [
      { id: 'thr:default', type: 'theater', salience: 0.9 },
      { id: 'poi:default:A', type: 'poi', salience: 0.5 },
    ],
  });
  assert.notDeepEqual(framingOrientation(capOnly, rest), rest);
  // Adding an alarm does not move the frame once a vehicle is present.
  const base = sparse();
  const q1 = framingOrientation(computeLayout(base), rest);
  const withAlarm = {
    ...base,
    nodes: [
      ...base.nodes,
      { id: 'alarm:99', type: 'alarm', salience: 1, status: 'critical' },
    ],
  };
  assert.deepEqual(framingOrientation(computeLayout(withAlarm), rest), q1);
});

test('the orb never re-frames itself; reset view (double-click empty, Home) does', () => {
  const graph = sparse();
  const { orb, canvas, fe, host } = mount({ graph });
  const q0 = orb.camera.q;
  // A richer picture arrives: no camera move.
  orb.setGraph(makeGraph(120, { seed: 9 }));
  fe.advance(2000);
  assert.deepEqual(orb.camera.q, q0, 'no auto-rotation on new data');
  // The operator turns the orb, then double-clicks empty space.
  canvas.fire('pointerdown', { offsetX: 500, offsetY: 300, pointerId: 1 });
  canvas.fire('pointermove', { offsetX: 600, offsetY: 360, pointerId: 1 });
  canvas.fire('pointerup', { offsetX: 600, offsetY: 360, pointerId: 1 });
  assert.notDeepEqual(orb.camera.q, q0);
  canvas.fire('dblclick', { offsetX: 3, offsetY: 3 });
  assert.equal(fe.pendingFrames, 1, 'it eases');
  fe.advance(ORB_TIMING.camera + 50);
  const home = framingOrientation(orb.layout, orb.camera.rest());
  orb.camera.q.forEach((v, k) =>
    assert.ok(Math.abs(v - home[k]) < 1e-6, 'back to the framed view'),
  );
  assert.ok(Math.abs(framedAt(orb).elevation - 12) < 1e-6);
  // Home does the same from the keyboard.
  listbox(host).fire('keydown', { key: 'ArrowRight' });
  listbox(host).fire('keydown', { key: 'Home' });
  fe.advance(ORB_TIMING.camera + 50);
  orb.camera.q.forEach((v, k) => assert.ok(Math.abs(v - home[k]) < 1e-6));
});

test('an operator who turned the orb before the first picture keeps the view', () => {
  const { orb, canvas } = mount({ graph: { nodes: [], edges: [] } });
  canvas.fire('pointerdown', { offsetX: 500, offsetY: 300, pointerId: 1 });
  canvas.fire('pointermove', { offsetX: 560, offsetY: 330, pointerId: 1 });
  canvas.fire('pointerup', { offsetX: 560, offsetY: 330, pointerId: 1 });
  const q = orb.camera.q;
  orb.setGraph(sparse());
  assert.deepEqual(orb.camera.q, q);
});

test('feeds read as plain words on labels, the twin and the list; errors stay out', () => {
  const graph = sparse();
  const set = (name, patch) => {
    const node = graph.nodes.find((n) => n.id === `feed:${name}`);
    Object.assign(node, patch);
  };
  set('contacts', { label: 'Contacts', subtitle: 'mcp:uav_list_tracks' });
  set('mission_state', {
    label: 'Mission state',
    subtitle: 'mcp:uav_task_status+mission_status @ http://127.0.0.1:1/mcp',
  });
  set('sim', {
    label: 'Sim',
    status: 'critical',
    subtitle: 'down: RPCError: datalink lost: no response',
    ts_ms: Date.UTC(2026, 8, 27, 13, 0),
  });
  set('real_data', {
    label: 'Real data',
    status: 'warn',
    subtitle: 'off: AGL is height above the launch datum, LOS is geometric',
  });
  const { orb, fe, host } = mount({ graph });
  const shown = () =>
    Object.fromEntries(
      findAll(
        orb.labelLayer,
        (el) =>
          el.className === 'ic-orb-label' && el.attrs.hidden === undefined,
      ).map((el) => [
        el.children[0].textContent,
        el.children[2].children.map((c) => c.children.join('')).join(' · '),
      ]),
    );
  const labels = shown();
  assert.equal(labels['Contacts feed'], 'Up');
  assert.equal(labels['Mission state'], 'Up');
  assert.equal(labels.Sim, 'Down', 'already down at load: no invented start');
  assert.equal(labels['Real data'], 'Off');
  const all = JSON.stringify(labels);
  assert.ok(!/mcp:|RPCError|http/.test(all), all);
  const options = findAll(
    listbox(host),
    (el) => el.attrs?.role === 'option',
  ).map((el) => el.textContent);
  assert.ok(options.includes('Contacts feed, feed, up'));
  assert.ok(!options.some((t) => /mcp:|RPCError/.test(t)));
  // The contacts feed goes down while we watch: "Down since" its first failure.
  const next = structuredClone(graph);
  const contacts = next.nodes.find((n) => n.id === 'feed:contacts');
  contacts.status = 'critical';
  contacts.subtitle = 'TimeoutError: uav_list_tracks timed out after 10 s';
  contacts.ts_ms = Date.UTC(2026, 8, 27, 14, 0, 30);
  orb.setGraph(next);
  fe.flush();
  assert.equal(shown()['Contacts feed'], 'Down since 14:00Z');
  // A later failure keeps the first time; recovery clears it.
  const later = structuredClone(next);
  later.nodes.find((n) => n.id === 'feed:contacts').ts_ms = Date.UTC(
    2026,
    8,
    27,
    14,
    9,
  );
  orb.setGraph(later);
  fe.flush();
  assert.equal(shown()['Contacts feed'], 'Down since 14:00Z');
  const back = structuredClone(later);
  Object.assign(
    back.nodes.find((n) => n.id === 'feed:contacts'),
    { status: 'ok', subtitle: 'mcp:uav_list_tracks' },
  );
  orb.setGraph(back);
  fe.flush();
  assert.equal(shown()['Contacts feed'], 'Up');
});
