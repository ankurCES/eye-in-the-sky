import test from 'node:test';
import assert from 'node:assert/strict';

import { createCamera, orientationFacing } from './camera.js';
import { statusColor, statusKey } from './glyphs.js';
import { computeLayout, toVector } from './layout.js';
import { nodeRadius } from './orb.js';
import {
  EDGE_CAP,
  LABEL_SPACING_PX,
  LEADER_GAP_PX,
  MORE_LINE_EXTRA_PX,
  RING,
  TIER,
  bandCaptions,
  chooseLabels,
  createRenderer,
  frameDrawBudget,
  layoutMarginLabels,
  placeCaptions,
  placeColumn,
  routeLeaders,
  selectEdges,
} from './renderer.js';
import { FakePath2D, fakeCtx, makeGraph, rng } from './fixtures.test.mjs';

const W = 1000;
const H = 700;

function vmFor(node, n) {
  const status = statusKey(node.status);
  const type = node.type;
  let tier = TIER.OTHER;
  if (type === 'vehicle') tier = TIER.VEHICLE;
  else if (status === 'critical') tier = TIER.CRITICAL;
  return {
    id: node.id,
    type,
    status,
    spec: {
      type,
      status,
      phase: node.attrs?.phase,
      conf: node.attrs?.confidence || '',
      dup: false,
    },
    spriteKey: `${type}|${status}|${node.attrs?.phase || ''}|${node.attrs?.confidence || ''}`,
    color: statusColor(type, status),
    critical: status === 'critical',
    label: node.label || node.id,
    subtitle: node.subtitle || '',
    salience: node.salience || 0,
    fuel: node.attrs?.fuel_pct ?? null,
    bingo: node.attrs?.bingo_fuel_pct ?? null,
    r: nodeRadius(node, n),
    isNew: false,
    alpha: 1,
    ring: 0,
    tier,
    ink: 'film',
  };
}

function sceneFor(
  graph,
  { labelMode = 'inline', labelBudget = 12, spin = 0, extra = {} } = {},
) {
  const layout = computeLayout(graph);
  const camera = createCamera();
  camera.setViewport({ cx: W / 2, cy: 330, r: 280 });
  camera.spin(spin);
  const n = layout.n;
  const proj = camera.projectAll(layout.pos, n);
  const order = Uint32Array.from({ length: n }, (_, i) => i).sort(
    (a, b) => proj[a * 4 + 2] - proj[b * 4 + 2],
  );
  const nodes = layout.nodes.map((node) => vmFor(node, n));
  return {
    layout,
    scene: {
      width: W,
      height: H,
      dpr: 2,
      cx: W / 2,
      cy: 330,
      R: 280,
      camera,
      n,
      proj,
      order,
      nodes,
      edges: selectEdges(layout, { colorOf: (i) => nodes[i].color }),
      edgePts: layout.edgePts,
      beltDashed: false,
      beltEmpty: !layout.counts.track,
      captions: bandCaptions(layout.counts),
      sectorNames: true,
      labelMode,
      labelBudget,
      hovered: -1,
      ripples: [],
      ghosts: [],
      limbMarks: [],
      twinFocused: false,
      watermark: null,
      pointsBack: n >= 300,
      dropBackHalos: false,
      equipmentTicks: n > 150,
      ...extra,
    },
  };
}

function spriteEnv() {
  const made = [];
  return {
    made,
    env: {
      Path2D: FakePath2D,
      createCanvas: (w, h) => {
        const ctx = fakeCtx({ width: w, height: h });
        const canvas = { width: w, height: h, getContext: () => ctx, ctx };
        made.push(canvas);
        return canvas;
      },
    },
  };
}

const FORBIDDEN = ['shadowBlur', 'shadowColor', 'filter', 'letterSpacing'];

test('a frame draws against a recording context without crashing', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const { scene } = sceneFor(makeGraph(60));
  const ctx = fakeCtx({ width: W * 2, height: H * 2 });
  const stats = renderer.draw(ctx, scene);
  assert.ok(stats.drawn > 0);
  assert.ok(
    ctx.count('drawImage') >= stats.drawn,
    'one drawImage per painted node',
  );
  assert.equal(ctx.count('clearRect'), 1);
  assert.deepEqual(
    ctx.calls.find((c) => c[0] === 'setTransform'),
    ['setTransform', 2, 0, 0, 2, 0, 0],
  );
});

test('the renderer never uses shadowBlur, ctx.filter, letterSpacing or lighter compositing', () => {
  const { env, made } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = makeGraph(120);
  const { scene } = sceneFor(graph, { labelMode: 'margin' });
  scene.nodes.forEach((vm, i) => {
    if (i % 5 === 0) vm.ring = RING.SELECT | RING.FOCUS_ANALYST;
  });
  scene.ripples = [
    { x: 400, y: 300, z: 0.5, color: '#fff', radius: 10, alpha: 0.5 },
  ];
  scene.limbMarks = [
    { kind: 'arc', angle: 1, color: '#fff' },
    { kind: 'tick', angle: 2, color: '#8CC8FF' },
  ];
  scene.twinFocused = true;
  scene.watermark = 'Last picture 14:31:05Z, not live.';
  const ctx = fakeCtx();
  renderer.draw(ctx, scene);
  const everything = [ctx, ...made.map((c) => c.ctx)];
  for (const g of everything) {
    for (const [key, value] of g.sets) {
      assert.ok(!FORBIDDEN.includes(key), `never sets ${key}`);
      if (key === 'globalCompositeOperation') assert.notEqual(value, 'lighter');
    }
  }
});

test('the 300-node frame stays within the draw-call budget', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = makeGraph(300, { seed: 13 });
  const { scene } = sceneFor(graph, { labelMode: 'margin' });
  const ctx = fakeCtx();
  renderer.draw(ctx, scene); // warms the sprite atlas
  ctx.reset();
  renderer.draw(ctx, scene);
  const budget = frameDrawBudget(scene.n);
  assert.ok(ctx.draws() <= budget, `${ctx.draws()} paint calls ≤ ${budget}`);
  const back = [...scene.proj].filter((_, k) => k % 4 === 2 && _ < 0).length;
  const front = scene.n - back;
  assert.ok(
    ctx.count('drawImage') <= front * 2 + 5,
    'back nodes are batched points, not sprites',
  );
  // Back-hemisphere points: one fill per colour.
  const colours = new Set(scene.nodes.map((vm) => vm.color));
  assert.ok(ctx.count('fill') <= colours.size + 3);
});

test('sprites are built once and reused across frames', () => {
  const { env, made } = spriteEnv();
  const renderer = createRenderer(env);
  const { scene } = sceneFor(makeGraph(80));
  renderer.draw(fakeCtx(), scene);
  const built = made.length;
  assert.ok(built > 0 && built <= renderer.spriteCount + 0);
  renderer.draw(fakeCtx(), scene);
  assert.equal(made.length, built, 'no sprite is rebuilt on the second frame');
  renderer.draw(fakeCtx(), { ...scene, dpr: 1 });
  assert.ok(made.length > built, 'a DPR change rebuilds the atlas');
});

test('without canvas creation or Path2D the renderer paints nodes directly', () => {
  const renderer = createRenderer({ createCanvas: () => null, Path2D: null });
  const { scene } = sceneFor(makeGraph(30));
  const ctx = fakeCtx();
  const stats = renderer.draw(ctx, scene);
  assert.ok(stats.drawn > 0);
  assert.equal(ctx.count('drawImage'), 0);
  assert.ok(
    ctx.count('arc') > 0 && ctx.count('lineTo') > 0,
    'glyphs traced from parsed paths',
  );
});

test('label budget: 12 wide, 6 compact, 4 narrow; z > 0.2 only; all labelled when N ≤ 12', () => {
  const graph = makeGraph(200, { seed: 21 });
  for (const budget of [12, 6, 4]) {
    const { scene } = sceneFor(graph, { labelBudget: budget });
    const chosen = chooseLabels(scene);
    assert.equal(chosen.length, budget, `budget ${budget}`);
    for (const i of chosen) assert.ok(scene.proj[i * 4 + 2] > 0.2);
    const renderer = createRenderer(spriteEnv().env);
    const ctx = fakeCtx();
    const stats = renderer.draw(ctx, scene);
    assert.ok(
      stats.labels.inline.length <= budget,
      'inline labels within budget',
    );
  }
  const small = sceneFor(makeGraph(10, { tracks: 0 }), {
    labelBudget: 4,
  }).scene;
  const eligible = [...Array(small.n).keys()].filter(
    (i) => small.proj[i * 4 + 2] > 0.2,
  );
  assert.equal(
    chooseLabels(small).length,
    eligible.length,
    'N ≤ 12: every qualifying node',
  );
});

test('label priority: selected, analyst, hovered, search, vehicles, criticals, salience', () => {
  const { scene } = sceneFor(makeGraph(200, { seed: 3 }), { labelBudget: 4 });
  const front = [...Array(scene.n).keys()].filter(
    (i) => scene.proj[i * 4 + 2] > 0.3,
  );
  const [a, b, c] = front.slice(-3);
  scene.nodes[a].tier = TIER.SELECTED;
  scene.nodes[b].tier = TIER.ANALYST;
  scene.nodes[c].tier = TIER.HOVERED;
  const chosen = chooseLabels(scene);
  assert.deepEqual(chosen.slice(0, 3), [a, b, c]);
  // A dimmed non-match loses its label unless it is prioritised.
  const dim = front.find((i) => ![a, b, c].includes(i));
  scene.nodes[dim].alpha = 0.15;
  scene.nodes[dim].tier = TIER.CRITICAL;
  assert.ok(!chooseLabels({ ...scene, budget: 200 }).includes(dim));
});

/** Proper crossing of two segments (touching endpoints do not count). */
function segmentsCross(s, t) {
  const orient = (ax, ay, bx, by, px, py) =>
    (bx - ax) * (py - ay) - (by - ay) * (px - ax);
  const d1 = orient(t[0], t[1], t[2], t[3], s[0], s[1]);
  const d2 = orient(t[0], t[1], t[2], t[3], s[2], s[3]);
  const d3 = orient(s[0], s[1], s[2], s[3], t[0], t[1]);
  const d4 = orient(s[0], s[1], s[2], s[3], t[2], t[3]);
  const opposite = (a, b) => (a > 1e-9 && b < -1e-9) || (a < -1e-9 && b > 1e-9);
  return opposite(d1, d2) && opposite(d3, d4);
}

function segmentsOf(pts) {
  const out = [];
  for (let k = 0; k + 3 < pts.length; k += 2)
    out.push([pts[k], pts[k + 1], pts[k + 2], pts[k + 3]]);
  return out;
}

function crossings(items) {
  let count = 0;
  for (let a = 0; a < items.length; a += 1) {
    for (let b = a + 1; b < items.length; b += 1) {
      for (const s of segmentsOf(items[a].leader))
        for (const t of segmentsOf(items[b].leader))
          if (segmentsCross(s, t)) count += 1;
    }
  }
  return count;
}

test('margin labels: at most 12, per-side node order, spacing, leaders into the label', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const { scene } = sceneFor(makeGraph(200, { seed: 17 }), {
    labelMode: 'margin',
    labelBudget: 12,
  });
  const ctx = fakeCtx();
  const stats = renderer.draw(ctx, scene);
  const items = stats.labels.items;
  assert.ok(items.length > 0 && items.length <= 12);
  for (const side of ['left', 'right']) {
    const mine = items.filter((it) => it.side === side);
    const ys = mine.map((it) => it.y);
    for (let k = 1; k < ys.length; k += 1)
      assert.ok(ys[k] - ys[k - 1] >= LABEL_SPACING_PX - 1e-6, 'spaced');
    // Labels keep their nodes' projected-y order.
    const nodeYs = mine.map((it) => scene.proj[it.i * 4 + 1]);
    for (let k = 1; k < nodeYs.length; k += 1)
      assert.ok(nodeYs[k] >= nodeYs[k - 1] - 1e-6, 'node order');
    for (const item of mine) {
      const dir = side === 'right' ? 1 : -1;
      assert.ok(dir * (item.x - scene.cx) > scene.R, 'in the gutter');
      assert.ok(
        dir * (scene.proj[item.i * 4] - scene.cx) >= 0,
        'side by projected x',
      );
      const pts = item.leader;
      const [ex, ey] = pts.slice(-2);
      const [bx, by] = pts.slice(-4, -2);
      assert.equal(ey, item.y, 'the leader ends on the label line');
      assert.equal(by, item.y, 'with a horizontal shoulder');
      assert.ok(Math.abs(ex - (scene.cx + dir * (scene.R + 12))) < 1e-6);
      assert.ok(dir * (ex - bx) > 0);
      // It starts at its own node, just off the glyph.
      assert.ok(Math.abs(pts[1] - scene.proj[item.i * 4 + 1]) < 1e-6);
    }
  }
  assert.equal(crossings(items), 0, 'no two leaders cross');
  // A tight cluster is centred on its nodes, not pushed below them.
  const packed = layoutMarginLabels(
    [0, 1, 2],
    new Float32Array([700, 100, 0, 1, 700, 101, 0, 1, 700, 102, 0, 1]),
    {
      cx: 500,
      cy: 330,
      R: 280,
      width: W,
      height: H,
    },
  );
  assert.deepEqual(
    packed.map((p) => p.y),
    [81, 101, 121],
  );
});

test('margin leaders never cross, even for crowded caps and captions in the way', () => {
  const cx = 500;
  const cy = 400;
  const R = 330;
  let checked = 0;
  for (let seed = 1; seed <= 400; seed += 1) {
    const rand = rng(seed);
    const n = 1 + Math.floor(rand() * 12);
    const proj = new Float32Array(n * 4);
    const radii = new Float32Array(n);
    const cluster = rand() < 0.5;
    const ccx = cx + (rand() - 0.5) * R;
    const ccy = cy + (rand() - 0.5) * R;
    for (let i = 0; i < n; i += 1) {
      let x;
      let y;
      do {
        if (cluster) {
          x = ccx + (rand() - 0.5) * 160;
          y = ccy + (rand() - 0.5) * 160;
        } else {
          const a = rand() * Math.PI * 2;
          const r = Math.sqrt(rand()) * R;
          x = cx + Math.cos(a) * r;
          y = cy + Math.sin(a) * r;
        }
      } while (Math.hypot(x - cx, y - cy) > R * 0.97);
      proj.set([x, y, 0.5, 1], i * 4);
      radii[i] = 4 + rand() * 14;
    }
    const captions =
      seed % 2
        ? [
            { y: cy - 220, left: cx - 250, right: cx - 170 },
            { y: cy - 60, left: cx - R - 90, right: cx - R - 8 },
            { y: cy + 10, left: cx - 60, right: cx + 20 },
          ]
        : [];
    const items = layoutMarginLabels([...Array(n).keys()], proj, {
      cx,
      cy,
      R,
      width: 1000,
      height: 800,
      radii,
      captions,
      twoLine: (i) => i % 2 === 0,
    });
    assert.equal(items.length, n);
    assert.equal(crossings(items), 0, `seed ${seed}`);
    for (const side of ['left', 'right']) {
      const mine = items.filter((it) => it.side === side);
      for (let k = 1; k < mine.length; k += 1)
        assert.ok(mine[k].y > mine[k - 1].y, 'label order is top to bottom');
    }
    checked += 1;
  }
  assert.equal(checked, 400);
});

test('routeLeaders: earlier labels pass above a glyph, later ones below', () => {
  // Three nodes on a row; the middle one sits in the far ones' way.
  const list = [
    { i: 0, x: 560, y: 300, ty: 250 },
    { i: 1, x: 650, y: 305, ty: 290 },
    { i: 2, x: 600, y: 310, ty: 330 },
  ];
  const radii = new Float32Array([8, 8, 8]);
  const paths = routeLeaders(list, 'right', { cx: 500, R: 280, radii });
  const yAt = (pts, x) => {
    for (let k = 0; k + 3 < pts.length; k += 2) {
      const [x0, y0, x1, y1] = pts.slice(k, k + 4);
      if (x >= x0 && x <= x1)
        return x1 === x0 ? y0 : y0 + ((y1 - y0) * (x - x0)) / (x1 - x0);
    }
    return null;
  };
  // At node 1's x (650), leader 0 is above it and leader 2 below it.
  assert.ok(yAt(paths[0], 650) <= 305 - 8 - LEADER_GAP_PX + 1e-6);
  assert.ok(yAt(paths[2], 650) >= 305 + 8 + LEADER_GAP_PX - 1e-6);
  // At node 2's x (600), leader 0 is above it.
  assert.ok(yAt(paths[0], 600) <= 310 - 8 - LEADER_GAP_PX + 1e-6);
  // A caption in the way: a leader already above it stays above it.
  const around = routeLeaders([{ i: 0, x: 420, y: 200, ty: 240 }], 'left', {
    cx: 500,
    R: 280,
    radii: new Float32Array([6]),
    obstacles: [{ s0: 150, s1: 230, y: 215, half: 9 }],
  });
  const pts = around[0];
  for (let k = 0; k < pts.length; k += 2) {
    const x = pts[k];
    const s = 500 - x;
    if (s >= 150 && s <= 230)
      assert.ok(pts[k + 1] <= 215 - 9 - LEADER_GAP_PX + 1e-6, 'above it');
  }
});

test('placeColumn: centred clusters, bounds, and captions stepped around', () => {
  const gap = () => 20;
  const extra = () => 0;
  const list = [{ y: 100 }, { y: 101 }, { y: 400 }];
  placeColumn(list, { top: 10, bottom: 790, gap, extra });
  assert.deepEqual(
    list.map((it) => it.ty),
    [90.5, 110.5, 400],
  );
  const low = [{ y: 780 }, { y: 785 }, { y: 790 }];
  placeColumn(low, { top: 10, bottom: 790, gap, extra });
  assert.deepEqual(
    low.map((it) => it.ty),
    [750, 770, 790],
    'the bottom bound pushes the cluster up',
  );
  const blocked = [{ y: 300 }, { y: 310 }];
  placeColumn(blocked, {
    top: 10,
    bottom: 790,
    gap,
    extra,
    obstacles: [305],
  });
  for (const it of blocked)
    assert.ok(Math.abs(it.ty - 305) >= 17, `clear of the caption: ${it.ty}`);
  assert.ok(blocked[1].ty - blocked[0].ty >= 20);
});

test('labels: equipment classes only when pointed at; alarms after entities', () => {
  const { scene } = sceneFor(makeGraph(200, { seed: 21 }), {
    labelBudget: 200,
  });
  const chosen = chooseLabels({ ...scene, budget: 200 });
  const types = chosen.map((i) => scene.nodes[i].type);
  assert.ok(!types.includes('equipment'), 'reference data is not labelled');
  const firstAlarm = types.indexOf('alarm');
  if (firstAlarm >= 0) {
    assert.ok(
      types.slice(firstAlarm).every((t) => t === 'alarm' || t === 'vehicle'),
      'alarms come after the entities',
    );
  }
  const eq = [...Array(scene.n).keys()].find(
    (i) => scene.nodes[i].type === 'equipment' && scene.proj[i * 4 + 2] > 0.3,
  );
  if (eq != null) {
    scene.nodes[eq].tier = TIER.HOVERED;
    assert.ok(chooseLabels(scene).includes(eq), 'unless it is pointed at');
  }
  // Small pictures still label every front node.
  const small = sceneFor(makeGraph(10, { tracks: 0 })).scene;
  const eligible = [...Array(small.n).keys()].filter(
    (i) => small.proj[i * 4 + 2] > 0.2,
  );
  assert.equal(chooseLabels(small).length, eligible.length);
});

test('inline labels keep off labelled glyphs: blocked on the right, they go left', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = {
    nodes: [
      { id: 'veh:A', type: 'vehicle', label: 'Alpha', salience: 0.9 },
      { id: 'veh:B', type: 'vehicle', label: 'Bravo', salience: 0.9 },
    ],
    edges: [],
  };
  const { scene } = sceneFor(graph, {
    labelMode: 'inline',
    extra: { captions: [], sectorNames: false },
  });
  // Put A just left of B, on the same row.
  scene.proj = new Float32Array([480, 300, 0.9, 1, 530, 300, 0.9, 1]);
  scene.order = Uint32Array.from([0, 1]);
  const ctx = fakeCtx();
  renderer.draw(ctx, scene);
  const texts = ctx.calls.filter((c) => c[0] === 'fillText');
  const alpha = texts.find((c) => c[1] === 'Alpha');
  const bravo = texts.find((c) => c[1] === 'Bravo');
  assert.ok(alpha && bravo, 'both labelled');
  assert.ok(alpha[2] < 480, 'Alpha goes left of its node, off Bravo');
  assert.ok(bravo[2] > 530, 'Bravo keeps the right slot');
});

test('edges: magenta always, neighbour kinds on hover/select/focus, context kinds on select, cap 200', () => {
  const graph = makeGraph(80);
  const layout = computeLayout(graph);
  const idle = selectEdges(layout);
  assert.ok(idle.length > 0);
  assert.ok(
    idle.every((e) => e.kind === 'flying' || e.kind === 'tracking'),
    'only magenta edges at rest',
  );
  const target = layout.edges.find((e) => e.kind === 'target');
  const hovered = selectEdges(layout, { hovered: target.ai });
  assert.ok(hovered.some((e) => e.kind === 'target'));
  assert.ok(
    !hovered.some((e) => e.kind === 'in_theater'),
    'context edges wait for selection',
  );
  const theater = layout.index.get('thr:default');
  const selected = selectEdges(layout, { selected: theater });
  assert.ok(selected.some((e) => e.kind === 'in_theater'));
  const alarm = layout.edges.find((e) => e.kind === 'about');
  const about = selectEdges(layout, {
    focus: [alarm.ai],
    colorOf: () => '#FF7B7B',
  }).find((e) => e.kind === 'about');
  assert.equal(
    about.color,
    '#FF7B7B',
    'about edges take the alarm severity colour',
  );
  const busy = computeLayout(makeGraph(400, { seed: 2 }));
  const all = selectEdges(busy, { selected: busy.index.get('thr:default') });
  assert.ok(all.length <= EDGE_CAP);
});

test('band captions: empty bands still speak; a down detections feed says so', () => {
  const captions = bandCaptions(
    { theater: 1, feed: 4, vehicle: 1, track: 0 },
    { recent: { feed: 1 } },
  );
  const text = captions.map((c) => c.text);
  assert.ok(text.includes('Contacts: none reported yet'));
  assert.ok(text.includes('Feeds 4 (+1)'));
  assert.ok(text.includes('Alarms 0'));
  const down = bandCaptions({ track: 12 }, { detectionsDown: true }).find(
    (c) => c.key === 'track',
  );
  assert.equal(
    down.text,
    'Detection feed down. Contacts may be missing, not absent.',
  );
});

test('the facing hemisphere is drawn over the back one', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = makeGraph(40);
  const { scene, layout } = sceneFor(graph);
  scene.camera.setOrientation(orientationFacing(toVector(6, 0)));
  const proj = scene.camera.projectAll(layout.pos, scene.n);
  scene.proj = proj;
  scene.order = Uint32Array.from({ length: scene.n }, (_, i) => i).sort(
    (a, b) => proj[a * 4 + 2] - proj[b * 4 + 2],
  );
  const ctx = fakeCtx();
  renderer.draw(ctx, scene);
  const images = ctx.calls.filter((c) => c[0] === 'drawImage');
  const alphas = [];
  let alpha = 1;
  for (const [key, value] of ctx.sets)
    if (key === 'globalAlpha') alphas.push((alpha = value));
  assert.ok(images.length > 0 && alpha === 1);
  assert.ok(
    alphas.includes(0.3) || alphas.some((a) => a > 0 && a < 0.35),
    'back nodes at 30 %',
  );
});

test('a boxed-in high-priority label still shows, over a lesser glyph', () => {
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const around = [
    [530, 300],
    [470, 300],
    [500, 272],
    [500, 328],
  ];
  const graph = {
    nodes: [
      { id: 'veh:A', type: 'vehicle', label: 'Alpha', salience: 0.9 },
      ...around.map((_, k) => ({
        id: `poi:default:P${k}`,
        type: 'poi',
        label: `P${k}`,
        salience: 0.2,
      })),
    ],
    edges: [],
  };
  const { scene, layout } = sceneFor(graph, {
    labelMode: 'inline',
    extra: { captions: [], sectorNames: false },
  });
  const proj = new Float32Array(layout.n * 4);
  layout.ids.forEach((id, i) => {
    const [x, y] = id === 'veh:A' ? [500, 300] : around[Number(id.slice(-1))];
    proj.set([x, y, 0.9, 1], i * 4);
  });
  scene.proj = proj;
  scene.order = Uint32Array.from(layout.ids.map((_, i) => i));
  const ctx = fakeCtx();
  renderer.draw(ctx, scene);
  const texts = ctx.calls.filter((c) => c[0] === 'fillText').map((c) => c[1]);
  assert.ok(texts.includes('Alpha'), 'the vehicle keeps its label');
});

test('an ordinary label boxed in by glyphs stays hidden instead of printing over them', () => {
  // Review: at phone width the first-run cap drew "Sim" and "East Field" on
  // top of neighbouring feed bars. Only labels that outrank ordinary nodes
  // may fall back to a slot over a lesser glyph.
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const around = [
    [530, 300],
    [470, 300],
    [500, 272],
    [500, 328],
  ];
  const graph = {
    nodes: [
      {
        id: 'feed:sim',
        type: 'feed',
        label: 'Sim',
        status: 'ok',
        salience: 0.9,
      },
      ...around.map((_, k) => ({
        id: `poi:default:P${k}`,
        type: 'poi',
        label: `P${k}`,
        salience: 0.2,
      })),
    ],
    edges: [],
  };
  const drawn = (tier) => {
    const { scene, layout } = sceneFor(graph, {
      labelMode: 'inline',
      extra: { captions: [], sectorNames: false },
    });
    const proj = new Float32Array(layout.n * 4);
    layout.ids.forEach((id, i) => {
      const [x, y] =
        id === 'feed:sim' ? [500, 300] : around[Number(id.slice(-1))];
      proj.set([x, y, 0.9, 1], i * 4);
    });
    scene.proj = proj;
    scene.order = Uint32Array.from(layout.ids.map((_, i) => i));
    scene.nodes[layout.index.get('feed:sim')].tier = tier;
    const ctx = fakeCtx();
    renderer.draw(ctx, scene);
    return ctx.calls.filter((c) => c[0] === 'fillText').map((c) => c[1]);
  };
  assert.ok(!drawn(TIER.OTHER).includes('Sim'), 'no free slot: hidden');
  assert.ok(drawn(TIER.HOVERED).includes('Sim'), 'hovered: it still shows');
});

/** A label box [top, bottom] for line y (two-line labels carry a subtitle). */
const labelSpan = (y, tall) => [y - 8, y + 8 + (tall ? 16 : 0)];
const overlaps = ([a0, a1], [b0, b1]) => a0 < b1 && b0 < a1;

test('placeColumn: labels never land on a caption row, even after keeping order', () => {
  // Review: "Sim / Up" over "Missions 0". The old last pass restored order by
  // pushing labels down, back into the rows they had just stepped around.
  const regress = [
    { y: 552, tall: true },
    { y: 565, tall: true },
  ];
  placeColumn(regress, {
    top: 10,
    bottom: 890,
    gap: () => 36,
    extra: () => 16,
    obstacles: [602, 603, 641],
  });
  for (const it of regress)
    for (const oy of [602, 603, 641])
      assert.ok(
        !overlaps(labelSpan(it.ty, true), [oy - 9, oy + 9]),
        `${it.ty}`,
      );
  let checked = 0;
  for (let seed = 1; seed <= 3000; seed += 1) {
    const rand = rng(seed);
    const height = 300 + Math.floor(rand() * 600);
    const n = 1 + Math.floor(rand() * 12);
    const tall = Array.from({ length: n }, () => rand() < 0.5);
    const list = Array.from({ length: n }, () => ({
      y: 10 + rand() * (height - 20),
    })).sort((a, b) => a.y - b.y);
    const rows = Array.from(
      { length: Math.floor(rand() * 7) },
      () => 20 + rand() * (height - 40),
    ).sort((a, b) => a - b);
    const gap = (k) => (tall[k] ? 36 : 20);
    const extra = (k) => (tall[k] ? 16 : 0);
    placeColumn(list, {
      top: 10,
      bottom: height - 10,
      gap,
      extra,
      obstacles: rows,
    });
    list.forEach((it, k) => {
      for (const oy of rows)
        assert.ok(
          !overlaps(labelSpan(it.ty, tall[k]), [oy - 9, oy + 9]),
          `seed ${seed}: label ${k} on the caption at ${oy}`,
        );
      if (k)
        assert.ok(it.ty >= list[k - 1].ty + gap(k - 1) - 1e-6, `seed ${seed}`);
      if (!it.overflow) {
        assert.ok(it.ty >= 10 - 1e-6 && it.ty + extra(k) <= height - 10 + 1e-6);
      }
    });
    checked += 1;
  }
  assert.equal(checked, 3000);
});

test('margin labels: a crowded gutter drops its lowest-priority labels; "more" keeps its row', () => {
  const cx = 500;
  const R = 200;
  const height = 300;
  // Twelve two-line labels on the right cannot fit in 280 px.
  const n = 12;
  const proj = new Float32Array(n * 4);
  for (let i = 0; i < n; i += 1) proj.set([600, 60 + i * 15, 0.5, 1], i * 4);
  const indices = [...Array(n).keys()].reverse(); // priority: 11 first
  const items = layoutMarginLabels(indices, proj, {
    cx,
    R,
    width: 1000,
    height,
    twoLine: () => true,
  });
  assert.ok(items.length < n && items.length >= 5, `${items.length} kept`);
  const kept = new Set(items.map((it) => it.i));
  for (let k = 0; k < items.length; k += 1)
    assert.ok(kept.has(indices[k]), 'the highest-priority labels stay');
  const ys = items.map((it) => it.y).sort((a, b) => a - b);
  for (let k = 1; k < ys.length; k += 1) assert.ok(ys[k] - ys[k - 1] >= 36);
  assert.ok(ys.at(-1) + 16 <= height - 10);
  // With a filter on, the gutter's last label keeps a free row under its
  // subtitle for "N more match", clear of captions and of the bottom.
  const caption = { y: 250, left: cx - R - 90, right: cx - R - 8 };
  const left = new Float32Array([420, 222, 0.5, 1, 430, 230, 0.5, 1]);
  const withMore = layoutMarginLabels([0, 1], left, {
    cx,
    R,
    width: 1000,
    height: 600,
    twoLine: () => true,
    captions: [caption],
    moreRow: true,
  });
  const last = Math.max(...withMore.map((it) => it.y));
  const moreRow = [last + 8 + 16, last + 8 + 16 + MORE_LINE_EXTRA_PX];
  assert.ok(!overlaps(moreRow, [caption.y - 9, caption.y + 9]), `${last}`);
  for (const it of withMore)
    assert.ok(!overlaps(labelSpan(it.y, true), [caption.y - 9, caption.y + 9]));
});

test('captions keep off limb back glyphs; sector names keep off every glyph', () => {
  // Review: "Contacts 16" sat under a limb contact disc and "Command and
  // control" printed over a contact at compact width.
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = {
    nodes: ['a', 'b', 'c'].map((k) => ({
      id: `trk:${k}`,
      type: 'track',
      label: `T${k}`,
      group: 'air-defense',
      salience: 0.3,
    })),
    edges: [],
  };
  const draw = (placeAt) => {
    const { scene, layout } = sceneFor(graph, { labelMode: 'inline' });
    const proj = new Float32Array(layout.n * 4);
    // Deep behind the sphere, far from the limb: never an obstacle.
    for (let i = 0; i < layout.n; i += 1) proj.set([500, 330, -0.9, 1], i * 4);
    if (placeAt) proj.set(placeAt, 0);
    scene.proj = proj;
    scene.order = Uint32Array.from(layout.ids.map((_, i) => i)).sort(
      (a, b) => proj[a * 4 + 2] - proj[b * 4 + 2],
    );
    const ctx = fakeCtx();
    renderer.draw(ctx, scene);
    return ctx.calls.filter((c) => c[0] === 'fillText');
  };
  const textAt = (calls, text) => calls.find((c) => c[1] === text) ?? null;
  const base = draw(null);
  const home = textAt(base, 'Contacts 3');
  const sector = textAt(base, 'Air defense');
  assert.ok(home && sector, 'baseline draws the caption and the sector name');
  // A back glyph at the limb, right under the caption: it steps aside.
  const limb = textAt(draw([home[2] + 10, home[3], -0.1, 1]), 'Contacts 3');
  assert.ok(
    !limb || Math.abs(limb[3] - home[3]) === 18,
    'the caption leaves the limb disc',
  );
  // The same glyph deep behind the sphere does not move it.
  const deep = textAt(draw([home[2] + 10, home[3], -0.9, 1]), 'Contacts 3');
  assert.equal(deep?.[3], home[3]);
  // A front glyph (too near the limb to be labelled) on the sector name:
  // the name is left out.
  assert.equal(
    textAt(draw([sector[2], sector[3], 0.1, 1]), 'Air defense'),
    null,
    'no sector name over a glyph',
  );
});

test('the not-live watermark is an obstacle: no caption or label prints into it', () => {
  // Review (offline, phone width): "Units 4" ran straight into "Last
  // picture …, not live." on the same line.
  const { env } = spriteEnv();
  const renderer = createRenderer(env);
  const graph = makeGraph(60, { seed: 11 });
  const { scene } = sceneFor(graph, { labelMode: 'inline' });
  // A long watermark spans the lower orb, as a short one does on a phone.
  scene.watermark = `Last picture 12:36:47Z, not live. ${'x'.repeat(50)}`;
  const ctx = fakeCtx();
  const calls = [];
  // Record each text with the alignment it was drawn with.
  ctx.fillText = (text, x, y) => calls.push([text, text, x, y, ctx.textAlign]);
  renderer.draw(ctx, scene);
  const mark = calls.find((c) => c[1] === scene.watermark);
  assert.ok(mark, 'the watermark is drawn');
  const w = scene.watermark.length * 6.5;
  const markBox = [
    mark[2] - w / 2 - 2,
    mark[3] - 9,
    mark[2] + w / 2 + 2,
    mark[3] + 9,
  ];
  assert.ok(calls.length > 3, 'captions and labels are drawn too');
  for (const c of calls) {
    const align = c[4];
    if (c === mark) continue;
    const tw = String(c[1]).length * 6.5;
    const left =
      align === 'right' ? c[2] - tw : align === 'center' ? c[2] - tw / 2 : c[2];
    const box = [left - 2, c[3] - 9, left + tw + 2, c[3] + 9];
    assert.ok(
      !(
        box[0] < markBox[2] &&
        markBox[0] < box[2] &&
        box[1] < markBox[3] &&
        markBox[1] < box[3]
      ),
      `"${c[1]}" at ${Math.round(left)}, ${Math.round(c[3])} is off the watermark`,
    );
  }
});

test('band captions step off glyphs, or stay out, and never crowd each other', () => {
  const camera = createCamera();
  camera.setViewport({ cx: 500, cy: 330, r: 280 });
  const captions = bandCaptions({ track: 16, alarm: 17, vehicle: 1 });
  const opts = {
    cx: 500,
    cy: 330,
    R: 280,
    mode: 'inline',
    measure: (text) => text.length * 6.5,
  };
  const free = placeCaptions(captions, camera, opts);
  assert.ok(free.length >= 2);
  // A glyph right on each caption: every caption moves 18 px or is left out.
  const avoid = free.map((row) => [row.left + 4, row.y - 6, 12, 12]);
  const moved = placeCaptions(captions, camera, { ...opts, avoid });
  for (const row of moved) {
    const home = free.find((f) => f.key === row.key);
    assert.equal(Math.abs(row.y - home.y), 18, `${row.text} stepped aside`);
    for (const b of avoid)
      assert.ok(
        !(
          b[0] < row.right + 2 &&
          row.left - 2 < b[0] + b[2] &&
          b[1] < row.y + 9 &&
          row.y - 9 < b[1] + b[3]
        ),
        `${row.text} is off every glyph`,
      );
  }
  for (let k = 1; k < moved.length; k += 1)
    assert.ok(moved[k].y - moved[k - 1].y >= 16);
  // Boxed in above and below too: the caption is left out, not overprinted.
  const walled = free.flatMap((row) =>
    [-6, -24, 12].map((dy) => [row.left - 40, row.y + dy, 120, 12]),
  );
  const shown = captions.filter((c) => free.some((row) => row.key === c.key));
  assert.equal(
    placeCaptions(shown, camera, { ...opts, avoid: walled }).length,
    0,
  );
});
