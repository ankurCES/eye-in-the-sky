import test from 'node:test';
import assert from 'node:assert/strict';

import { COLORS } from './glyphs.js';
import { computeLayout } from './layout.js';
import { EDGE_CAP, selectEdges } from './renderer.js';
import {
  AXIS_CAP,
  PENDING_ATTACKS_CAP,
  WARGAME_EDGE_KINDS,
  WARGAME_EDGE_STYLES,
  isWargameEdge,
  wargameEdgePlan,
  wargameEdgeStyle,
} from './wargameEdges.js';
import { ENGAGEMENT_PHASES, CONSEQUENCES, WG_INK } from './wargameStyles.js';

const scenario = (side, state = 'active') => ({
  provenance: 'scenario',
  side,
  state,
});

test('no wargame edge is ever magenta, in any view or phase', () => {
  for (const style of Object.values(WARGAME_EDGE_STYLES)) {
    assert.notEqual(style.color, COLORS.magenta);
    assert.notEqual(style.color, COLORS.ok, 'nor green');
    assert.ok(Object.isFrozen(style));
  }
  const ends = [];
  for (const phase of [...ENGAGEMENT_PHASES, 'odd', null])
    for (const consequence of [...CONSEQUENCES, 'odd', null])
      ends.push({ type: 'engagement', attrs: { phase, consequence } });
  for (const status of ['ok', 'warn', 'critical', 'stale', 'unknown'])
    ends.push({ type: 'force', status, attrs: scenario('red') });
  for (const kind of WARGAME_EDGE_KINDS)
    for (const a of [...ends, undefined])
      for (const umpire of [true, false]) {
        const style = wargameEdgeStyle(kind, { a, umpire });
        assert.ok(style, `${kind} has a style`);
        assert.notEqual(style.color, COLORS.magenta, `${kind} is not magenta`);
      }
  assert.equal(
    wargameEdgeStyle('flying', {}),
    null,
    'mission edges are not ours',
  );
  assert.equal(isWargameEdge('attacks'), true);
  assert.equal(isWargameEdge('tracking'), false);
});

test('edge styles follow the §5.3.5 table', () => {
  const pending = wargameEdgeStyle('attacks', {
    a: { type: 'engagement', attrs: { phase: 'authorized' } },
  });
  assert.equal(pending.color, WG_INK.sand);
  assert.deepEqual(pending.dash, [6, 3]);
  assert.equal(pending.reveal, 'always');
  assert.equal(pending.cap, 'pending');
  const done = (consequence) =>
    wargameEdgeStyle('attacks', {
      a: { type: 'engagement', attrs: { phase: 'adjudicated', consequence } },
    });
  assert.equal(done('own_loss').color, WG_INK.critical);
  assert.equal(done('own_damage').color, WG_INK.warn);
  assert.equal(done('red_effect').color, WG_INK.pencil);
  assert.equal(done('none').reveal, 'neighbour', 'hover or select');
  assert.equal(done('none').width, 1);
  const launched = wargameEdgeStyle('launched_by');
  assert.equal(launched.color, COLORS.hairline);
  assert.ok(launched.dash, 'dotted');
  assert.equal(wargameEdgeStyle('along').color, WG_INK.pencil35);
  const axis = wargameEdgeStyle('axis', {
    a: { type: 'force', status: 'ok' },
    umpire: true,
  });
  assert.equal(axis.color, WG_INK.pencil);
  assert.equal(axis.width, 1.5);
  assert.equal(axis.arrow, true, 'arrowhead at the target');
  assert.equal(axis.reveal, 'always');
  for (const status of ['warn', 'critical'])
    assert.equal(
      wargameEdgeStyle('axis', { a: { status }, umpire: true }).color,
      WG_INK.warn,
    );
  assert.equal(wargameEdgeStyle('axis', { umpire: false }).reveal, 'never');
  const ingress = wargameEdgeStyle('ingress');
  assert.equal(ingress.color, WG_INK.film35);
  assert.ok(ingress.dash);
  const threatens = wargameEdgeStyle('threatens');
  assert.equal(threatens.color, WG_INK.warn);
  assert.equal(threatens.reveal, 'neighbour');
  assert.equal(
    wargameEdgeStyle('correlates', { umpire: true }).reveal,
    'neighbour',
  );
  assert.equal(
    wargameEdgeStyle('correlates', { umpire: false }).reveal,
    'never',
  );
});

/** A session graph with `pending` waiting engagements and `axes` red axes. */
function busyGraph({ pending = 14, axes = 25 } = {}) {
  const nodes = [{ id: 'veh:Drone1', type: 'vehicle' }];
  const edges = [];
  nodes.push({
    id: 'frc:blue-1',
    type: 'force',
    group: 'ground-forces',
    attrs: scenario('blue'),
  });
  for (let k = 0; k < axes; k += 1) {
    const id = `frc:red-${k}`;
    nodes.push({
      id,
      type: 'force',
      group: 'air-defense',
      status: k === axes - 1 ? 'warn' : 'ok',
      attrs: scenario('red'),
    });
    edges.push({ a: id, b: 'frc:blue-1', kind: 'axis' });
  }
  for (let k = 0; k < pending; k += 1) {
    const id = `eng:${k}`;
    nodes.push({
      id,
      type: 'engagement',
      attrs: {
        phase: 'proposed',
        proposed_at_ms: 1000 + k,
        target: 'frc:red-0',
      },
    });
    edges.push({ a: id, b: 'frc:red-0', kind: 'attacks' });
  }
  return { meta: { wargame: { active: true } }, nodes, edges };
}

test('caps: the newest 10 pending attacks and 20 axes (warn first) stay on; the rest wait for hover', () => {
  const layout = computeLayout(busyGraph());
  const plan = wargameEdgePlan(layout, { umpire: true });
  const always = (kind) =>
    layout.edges
      .map((edge, e) => ({ edge, style: plan[e] }))
      .filter(
        ({ edge, style }) => edge.kind === kind && style.reveal === 'always',
      );
  const pending = always('attacks');
  assert.equal(pending.length, PENDING_ATTACKS_CAP);
  assert.deepEqual(
    pending.map(({ edge }) => edge.a).sort(),
    [
      'eng:10',
      'eng:11',
      'eng:12',
      'eng:13',
      'eng:4',
      'eng:5',
      'eng:6',
      'eng:7',
      'eng:8',
      'eng:9',
    ],
    'the newest ten by proposed time',
  );
  const axes = always('axis');
  assert.equal(axes.length, AXIS_CAP);
  assert.ok(
    axes.some(({ edge }) => edge.a === 'frc:red-24'),
    'the warn axis stays on',
  );
  assert.equal(
    wargameEdgePlan(layout, { umpire: true }),
    plan,
    'cached per layout',
  );
  const idle = selectEdges(layout, { umpire: true });
  assert.equal(
    idle.filter((e) => e.kind === 'attacks').length,
    PENDING_ATTACKS_CAP,
  );
  assert.equal(idle.filter((e) => e.kind === 'axis').length, AXIS_CAP);
  assert.ok(
    idle.filter((e) => e.kind === 'axis').every((e) => e.arrow === true),
  );
  assert.ok(idle.every((e) => e.color !== COLORS.magenta));
  // Hovering the target reveals the demoted pending edges too.
  const target = layout.index.get('frc:red-0');
  const hovered = selectEdges(layout, { umpire: true, hovered: target });
  assert.equal(hovered.filter((e) => e.kind === 'attacks').length, 14);
  // Blue view: no axis at all, even when a red unit is selected.
  const blue = selectEdges(layout, { umpire: false, selected: target });
  assert.equal(blue.filter((e) => e.kind === 'axis').length, 0);
  assert.ok(
    selectEdges(layout, { umpire: true, selected: target }).length <= EDGE_CAP,
  );
});

test('ISR edges keep their styles: no arrow flag, no wargame plan entries', () => {
  const layout = computeLayout({
    nodes: [
      { id: 'veh:Drone1', type: 'vehicle' },
      { id: 'msn:1', type: 'mission', attrs: { vehicle: 'Drone1' } },
      { id: 'trk:1', type: 'track' },
    ],
    edges: [
      { a: 'veh:Drone1', b: 'msn:1', kind: 'flying' },
      { a: 'veh:Drone1', b: 'trk:1', kind: 'tracking' },
    ],
  });
  assert.deepEqual(wargameEdgePlan(layout), [null, null]);
  const idle = selectEdges(layout);
  assert.equal(idle.length, 2);
  for (const edge of idle) {
    assert.equal(edge.color, COLORS.magenta, 'own missions stay magenta');
    assert.equal('arrow' in edge, false);
  }
});
