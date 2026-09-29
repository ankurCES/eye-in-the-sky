import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COLORS,
  NODE_TYPES,
  glyphFor,
  glyphStyle,
  glyphSvg,
  isKnownType,
  isOrbType,
  parsePath,
  statusColor,
} from './glyphs.js';
import {
  ENGAGEMENT_GLYPH,
  FRAME_BARS,
  FRAME_BARS_BROKEN,
  FRAME_GLYPHS,
  FRAME_OUTLINES,
  FRAME_SLASH,
  UNRECOGNISED_GLYPH,
  VECTOR_GLYPH,
  frameGlyphPath,
  frameSideKey,
} from './glyphPaths.js';
import {
  CONSEQUENCES,
  ENGAGEMENT_PHASES,
  FORCE_STATES,
  VECTOR_KINDS,
  WARGAME_TYPES,
  WG_INK,
  engagementStyle,
  filterForView,
  forceStyle,
  hiddenInBlueView,
  isScenarioForce,
  isWargameType,
  vectorStyle,
  wargameGlyphKeys,
  wargameGlyphStyle,
  wargameStyleKey,
} from './wargameStyles.js';

const SIDES = ['red', 'blue', 'green', '', null, undefined, '__proto__'];
const STATES = [...FORCE_STATES, 'routed', '', null];
const STATUSES = ['ok', 'warn', 'critical', 'stale', 'unknown', 'nonsense'];
const PHASES = [...ENGAGEMENT_PHASES, 'launched', null];
const OUTCOME_CONSEQUENCES = [...CONSEQUENCES, 'catastrophe', null];
const KINDS = [...VECTOR_KINDS, 'feint', null];
const XSS = '<img src=x onerror=alert(1)>';

const scenario = (side, state) => ({ provenance: 'scenario', side, state });

/** Every wargame (type, attrs, status) the tests sweep. */
function* everyWargameStyle() {
  for (const provenance of ['scenario', 'osm', undefined])
    for (const side of SIDES)
      for (const state of STATES)
        for (const status of STATUSES)
          yield ['force', { provenance, side, state }, status];
  for (const phase of PHASES)
    for (const consequence of OUTCOME_CONSEQUENCES)
      for (const status of STATUSES)
        yield ['engagement', { phase, consequence }, status];
  for (const kind of KINDS)
    for (const proposed of [true, false])
      for (const status of STATUSES)
        yield ['vector', { kind, proposed, side: 'red' }, status];
}

test('the palette the wargame repeats is the console palette, plus Sand', () => {
  for (const key of ['film', 'pencil', 'warn', 'critical', 'stale'])
    assert.equal(WG_INK[key], COLORS[key], key);
  assert.equal(WG_INK.lilac, COLORS.unknown);
  assert.equal(WG_INK.hairline, COLORS.hairline);
  assert.equal(WG_INK.film35, COLORS.film35);
  assert.equal(WG_INK.sand, '#CDBC8C', 'Sand on slate (§5.3.1)');
  assert.equal(WG_INK.sandSlip, '#5A4812', 'Sand on the slip');
  assert.ok(Object.isFrozen(WG_INK));
});

test('wargame glyph paths are Appendix A exactly, parse, and stay within 1–23', () => {
  assert.deepEqual(FRAME_GLYPHS, {
    blue: 'M2.5 6.5H21.5V17.5H2.5ZM7 12H17',
    red: 'M12 2L22 12L12 22L2 12ZM7 12H17',
    unknown:
      'M8 8A4 4 0 0 1 16 8A4 4 0 0 1 16 16A4 4 0 0 1 8 16A4 4 0 0 1 8 8ZM9 12H15',
  });
  assert.equal(
    ENGAGEMENT_GLYPH,
    'M12 2L14.2 9.8L22 12L14.2 14.2L12 22L9.8 14.2L2 12L9.8 9.8Z',
  );
  assert.equal(VECTOR_GLYPH, 'M3 12H17M11 6L17 12L11 18');
  const all = [
    ...Object.values(FRAME_GLYPHS),
    ...Object.values(FRAME_BARS_BROKEN),
    FRAME_SLASH,
    ENGAGEMENT_GLYPH,
    VECTOR_GLYPH,
  ];
  for (const d of all) {
    assert.match(d, /^M[MLHVAZ0-9. ]+$/, `${d}: absolute M L H V A Z only`);
    let x = 0;
    let y = 0;
    for (const c of parsePath(d)) {
      if (c[0] === 'A') {
        const [, x0, y0, r, , x1, y1] = c;
        assert.ok(
          Math.abs(Math.hypot(x1 - x0, y1 - y0) - 2 * r) < 1e-9,
          `${d}: every arc is a semicircle`,
        );
        // The bulge of a semicircle reaches r from its chord's middle.
        const cx = (x0 + x1) / 2;
        const cy = (y0 + y1) / 2;
        for (const v of [cx - r, cx + r, cy - r, cy + r])
          assert.ok(v >= 1 - 1e-9 && v <= 23 + 1e-9, `${d}: arc inside`);
        [x, y] = [x1, y1];
      } else if (c[0] !== 'Z') [, x, y] = c;
      assert.ok(x >= 1 && x <= 23 && y >= 1 && y <= 23, `${d} inside 1–23`);
    }
  }
  assert.equal(frameSideKey('blue'), 'blue');
  assert.equal(frameSideKey('Blue'), 'unknown');
  assert.equal(frameGlyphPath('__proto__'), FRAME_GLYPHS.unknown);
  for (const side of ['blue', 'red', 'unknown'])
    assert.equal(FRAME_GLYPHS[side], FRAME_OUTLINES[side] + FRAME_BARS[side]);
});

test('frames only on force: only a scenario force wears one, and no other type ever does', () => {
  const framePaths = new Set([
    ...Object.values(FRAME_OUTLINES),
    ...Object.values(FRAME_GLYPHS),
  ]);
  for (const type of NODE_TYPES.filter((t) => t !== 'force')) {
    for (const attrs of [
      undefined,
      scenario('red', 'active'),
      { side: 'blue' },
    ])
      assert.ok(
        !framePaths.has(glyphFor(type, { attrs }).path),
        `${type} never draws a frame`,
      );
  }
  for (const provenance of [undefined, 'osm', 'sensor', 'SCENARIO', XSS]) {
    const attrs = { provenance, side: 'red', state: 'active' };
    assert.equal(glyphFor('force', { attrs }).path, UNRECOGNISED_GLYPH);
    assert.equal(forceStyle(attrs, 'critical').frame, null);
    assert.equal(forceStyle(attrs, 'critical').halo, false);
    assert.equal(isScenarioForce({ type: 'force', attrs }), false);
  }
  assert.equal(
    glyphFor('force').path,
    UNRECOGNISED_GLYPH,
    'no attrs, no frame',
  );
  for (const side of ['blue', 'red']) {
    const glyph = glyphFor('force', { attrs: scenario(side, 'active') });
    assert.equal(glyph.path, FRAME_OUTLINES[side]);
    assert.equal(glyph.bar, FRAME_BARS[side]);
  }
  assert.equal(
    glyphFor('force', { attrs: scenario('green', 'active') }).path,
    FRAME_OUTLINES.unknown,
  );
  assert.equal(
    isScenarioForce({ type: 'track', attrs: { provenance: 'scenario' } }),
    false,
    'a scenario track is a contact, not a framed force',
  );
});

test('no wargame style is ever own-systems green, and sides are never hues', () => {
  const allowed = new Set([
    WG_INK.film,
    WG_INK.pencil,
    WG_INK.warn,
    WG_INK.critical,
    WG_INK.stale,
    WG_INK.lilac,
    WG_INK.sand,
  ]);
  let n = 0;
  for (const [type, attrs, status] of everyWargameStyle()) {
    n += 1;
    const style = wargameGlyphStyle(type, attrs, status);
    for (const ink of [style.color, style.stroke, style.fill].filter(Boolean)) {
      assert.notEqual(ink, COLORS.ok, `${type} ${JSON.stringify(attrs)}`);
      assert.ok(allowed.has(ink), `${type}: ${ink} is a console ink`);
    }
    assert.notEqual(statusColor(type, status, { attrs }), COLORS.ok);
    assert.notEqual(statusColor(type, status), COLORS.ok);
    const g = glyphStyle(type, status, { attrs });
    assert.notEqual(g.fill, COLORS.ok);
    assert.notEqual(g.stroke, COLORS.ok);
    const svg = glyphSvg(type, { status, attrs });
    assert.ok(!svg.includes(COLORS.ok), `${type} svg is never green`);
    assert.ok(
      !/#FF8080|#80E0FF/i.test(svg),
      'the map hues never reach the orb',
    );
  }
  assert.ok(n > 900, `swept ${n} combinations`);
});

test('force frames follow the §5.3.4 table: blue by state, red by the contact status rules', () => {
  const blue = (state) => forceStyle(scenario('blue', state), 'ok');
  assert.equal(blue('active').stroke, WG_INK.film, 'Film, never green');
  assert.equal(blue('active').bar, FRAME_BARS.blue, 'solid bar');
  assert.equal(blue('active').dash, null);
  assert.equal(blue('suppressed').stroke, WG_INK.warn);
  assert.ok(blue('suppressed').dash, 'dashed frame');
  assert.equal(blue('suppressed').bar, FRAME_BARS.blue, 'the bar stays solid');
  assert.equal(blue('suppressed').barDash, null);
  assert.equal(blue('damaged').stroke, WG_INK.warn);
  assert.equal(
    blue('damaged').bar,
    FRAME_BARS_BROKEN.blue,
    'bar broken in two',
  );
  assert.equal(blue('damaged').dash, null);
  const lost = forceStyle(scenario('blue', 'destroyed'), 'critical');
  assert.equal(lost.stroke, WG_INK.stale);
  assert.equal(lost.slash, true);
  assert.equal(lost.alpha, 0.5);
  assert.equal(lost.halo, false, 'a lost blue unit is slashed, not haloed');
  const red = (state, status) => forceStyle(scenario('red', state), status);
  assert.equal(red('active', 'ok').stroke, WG_INK.film);
  assert.equal(red('active', 'warn').stroke, WG_INK.warn);
  assert.equal(red('active', 'critical').stroke, WG_INK.critical);
  assert.equal(red('active', 'critical').halo, true, 'a steady halo');
  assert.equal(red('active', 'warn').halo, false);
  assert.equal(red('suppressed', 'warn').stroke, WG_INK.warn);
  assert.ok(red('suppressed', 'warn').dash);
  assert.equal(red('damaged', 'critical').bar, FRAME_BARS_BROKEN.red);
  assert.equal(red('destroyed', 'stale').stroke, WG_INK.stale);
  assert.equal(red('destroyed', 'stale').slash, true);
  assert.equal(red('destroyed', 'critical').halo, false);
  const odd = forceStyle(scenario('green', 'active'), 'ok');
  assert.equal(odd.frame, 'unknown', 'quatrefoil');
  assert.equal(odd.stroke, WG_INK.lilac);
  assert.ok(odd.dash, 'lilac dashed');
  const noState = forceStyle(scenario('red', 'routed'), 'critical');
  assert.equal(noState.frame, 'red', 'the side keeps its frame');
  assert.equal(noState.stroke, WG_INK.lilac);
  assert.equal(noState.halo, false);
  // Not live: stale grey like every other glyph.
  assert.equal(blue('active').stroke, WG_INK.film);
  assert.equal(
    wargameGlyphStyle('force', scenario('blue', 'active'), 'stale').stroke,
    WG_INK.stale,
  );
});

test('engagements and vectors follow §5.3.6', () => {
  const eng = (phase, consequence) => engagementStyle({ phase, consequence });
  for (const phase of ['proposed', 'authorized']) {
    assert.equal(eng(phase).stroke, WG_INK.sand, `${phase}: Sand`);
    assert.ok(eng(phase).dash, 'dashed outline');
    assert.equal(eng(phase).fill, null);
  }
  assert.equal(eng('adjudicated', 'own_loss').fill, WG_INK.critical);
  assert.equal(eng('adjudicated', 'own_loss').halo, true);
  assert.equal(eng('adjudicated', 'own_damage').fill, WG_INK.warn);
  assert.equal(eng('adjudicated', 'red_effect').fill, WG_INK.pencil);
  assert.equal(eng('adjudicated', 'none').fill, WG_INK.pencil);
  assert.equal(eng('adjudicated', 'none').halo, false);
  for (const phase of ['denied', 'expired']) {
    assert.equal(eng(phase).stroke, WG_INK.pencil);
    assert.ok(eng(phase).dash);
    assert.equal(eng(phase).alpha, 0.5);
  }
  for (const [phase, consequence] of [
    ['launched', 'none'],
    ['adjudicated', 'catastrophe'],
    [null, null],
  ]) {
    assert.equal(eng(phase, consequence).stroke, WG_INK.lilac);
    assert.ok(eng(phase, consequence).dash, 'lilac dashed');
  }
  assert.equal(vectorStyle({ kind: 'axis' }, 'ok').stroke, WG_INK.pencil);
  assert.equal(vectorStyle({ kind: 'axis' }, 'warn').stroke, WG_INK.warn);
  assert.equal(
    vectorStyle({ kind: 'axis' }, 'critical').stroke,
    WG_INK.critical,
  );
  assert.equal(vectorStyle({ kind: 'corridor' }, 'ok').dash, null);
  assert.ok(vectorStyle({ kind: 'corridor', proposed: true }, 'ok').dash);
  assert.equal(vectorStyle({ kind: 'feint' }, 'ok').stroke, WG_INK.lilac);
  assert.equal(
    glyphFor('vector', { attrs: { kind: 'axis' } }).path,
    VECTOR_GLYPH,
  );
  assert.equal(glyphFor('engagement').path, ENGAGEMENT_GLYPH);
});

test('styles, keys and SVG are built from constants: hostile attrs never leak', () => {
  const hostile = {
    provenance: 'scenario',
    side: XSS,
    state: '"><script>',
    phase: XSS,
    consequence: XSS,
    kind: XSS,
    proposed: XSS,
  };
  for (const type of WARGAME_TYPES) {
    const key = wargameStyleKey(type, hostile, XSS);
    assert.match(key, /^[a-z:_-]*$/, `${type}: key is constants only`);
    const svg = glyphSvg(type, { status: XSS, attrs: hostile, size: XSS });
    assert.ok(!svg.includes('<img') && !svg.includes('script'), svg);
    assert.deepEqual(
      Object.values(wargameGlyphKeys(type, hostile)).filter(
        (v) => typeof v === 'string' && v.includes('<'),
      ),
      [],
    );
  }
  assert.equal(
    wargameStyleKey('force', scenario('red', 'active'), 'critical'),
    'force:scn:red:active:critical',
  );
  assert.notEqual(
    wargameStyleKey('force', scenario('red', 'active'), 'critical'),
    wargameStyleKey('force', { side: 'red', state: 'active' }, 'critical'),
    'provenance is part of the key',
  );
  // A frame in SVG: outline, bar, and a slash in the frame's own ink.
  const svg = glyphSvg('force', {
    status: 'critical',
    attrs: scenario('blue', 'destroyed'),
  });
  assert.ok(svg.includes(FRAME_OUTLINES.blue) && svg.includes(FRAME_BARS.blue));
  assert.ok(
    svg.includes(`d="${FRAME_SLASH}" fill="none" stroke="${WG_INK.stale}"`),
  );
  assert.ok(svg.includes('opacity="0.5"'));
  assert.ok(!svg.includes(COLORS.critical), 'a lost unit is stale, not red');
});

test('surfaces opt in to the wargame types; the orb has', () => {
  for (const type of WARGAME_TYPES) {
    assert.equal(isWargameType(type), true);
    assert.equal(isKnownType(type), false, `${type}: fail-safe by default`);
    assert.equal(isKnownType(type, { wargame: true }), true);
    assert.equal(isOrbType(type), true);
    assert.ok(NODE_TYPES.includes(type));
  }
  for (const odd of ['force_red', 'engagement_band', '__proto__', undefined]) {
    assert.equal(isOrbType(odd), false);
    assert.equal(isWargameType(odd), false);
  }
  assert.equal(isKnownType('track'), true);
});

const graph = () => ({
  theater: { id: 'default', epoch: 0 },
  meta: { wargame: { active: true } },
  nodes: [
    { id: 'frc:red-sam-1', type: 'force', attrs: scenario('red', 'active') },
    { id: 'frc:blue-art-1', type: 'force', attrs: scenario('blue', 'active') },
    { id: 'frc:odd', type: 'force', attrs: scenario(undefined, 'active') },
    {
      id: 'vec:axis-red-sam-1',
      type: 'vector',
      attrs: { kind: 'axis', side: 'red' },
    },
    {
      id: 'vec:cor-1',
      type: 'vector',
      attrs: { kind: 'corridor', side: 'blue' },
    },
    { id: 'trk:1', type: 'track' },
    { id: 'eng:1', type: 'engagement', attrs: { phase: 'proposed' } },
  ],
  edges: [
    { a: 'trk:1', b: 'frc:red-sam-1', kind: 'correlates' },
    { a: 'frc:red-sam-1', b: 'frc:blue-art-1', kind: 'axis' },
    { a: 'eng:1', b: 'trk:1', kind: 'attacks' },
    { a: 'trk:1', b: 'frc:blue-art-1', kind: 'correlates' },
  ],
});

test('Blue view hides red: every force not provably blue, red axes, and the Umpire-only edges', () => {
  const g = graph();
  assert.equal(filterForView(g, { umpire: true }), g, 'Umpire view: as given');
  assert.equal(filterForView(g), g, 'the default is Umpire view');
  const blue = filterForView(g, { umpire: false });
  assert.deepEqual(
    blue.nodes.map((n) => n.id),
    ['frc:blue-art-1', 'vec:cor-1', 'trk:1', 'eng:1'],
  );
  assert.deepEqual(blue.edges, [{ a: 'eng:1', b: 'trk:1', kind: 'attacks' }]);
  assert.equal(blue.theater, g.theater, 'the rest of the graph is kept');
  assert.equal(blue.meta, g.meta);
  assert.equal(g.nodes.length, 7, 'the input is not mutated');
  const isr = { nodes: [{ id: 'trk:1', type: 'track' }], edges: [] };
  assert.equal(filterForView(isr, { umpire: false }), isr, 'nothing to hide');
  assert.equal(filterForView(null, { umpire: false }), null);
  assert.equal(
    hiddenInBlueView({ type: 'force', attrs: { side: 'blue' } }),
    false,
  );
  assert.equal(hiddenInBlueView({ type: 'force', attrs: {} }), true);
  assert.equal(
    hiddenInBlueView({ type: 'track', attrs: { side: 'red' } }),
    false,
  );
});
