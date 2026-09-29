import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COLORS,
  GLYPHS,
  GLYPH_UNRECOGNISED,
  NODE_TYPES,
  glyphFor,
  glyphStyle,
  glyphSvg,
  parsePath,
  statusColor,
  tracePath,
} from './glyphs.js';
import { fakeCtx } from './fixtures.test.mjs';
import {
  SITE_CATEGORIES,
  SITE_GLYPHS,
  UNRECOGNISED_GLYPH,
  siteCategoryKey,
  siteGlyphPath,
} from './glyphPaths.js';

const TYPES = [
  'vehicle',
  'mission',
  'track',
  'unit',
  'equipment',
  'report',
  'theater',
  'poi',
  'alarm',
  'feed',
];

test('every contract node type has a constant glyph in a 24×24 box', () => {
  for (const type of TYPES) {
    const glyph = GLYPHS[type];
    assert.ok(glyph, `${type} has a glyph`);
    assert.equal(typeof glyph.filled, 'boolean');
    assert.match(
      glyph.path,
      /^[MLHVAZ0-9. -]+$/,
      `${type} path uses only absolute commands`,
    );
    for (const c of parsePath(glyph.path)) {
      for (const v of c.slice(1))
        assert.ok(v >= 0 && v <= 24, `${type} stays inside the box`);
    }
  }
  assert.ok(Object.isFrozen(GLYPHS) && Object.isFrozen(GLYPHS.track));
});

test('contacts are never green; own systems are; references are always Pencil', () => {
  assert.equal(
    statusColor('track', 'ok'),
    COLORS.film,
    'assessed-low contact is film white',
  );
  assert.equal(statusColor('unit', 'ok'), COLORS.film);
  assert.equal(statusColor('vehicle', 'ok'), COLORS.ok);
  for (const status of ['ok', 'warn', 'critical', 'stale', 'unknown']) {
    assert.notEqual(
      statusColor('track', status),
      COLORS.ok,
      `a ${status} contact is not green`,
    );
    assert.equal(statusColor('equipment', status), COLORS.pencil);
    assert.equal(statusColor('poi', status), COLORS.pencil);
  }
  assert.equal(
    statusColor('track', 'nonsense'),
    COLORS.unknown,
    'unknown statuses read as not assessed',
  );
});

test('glyph styles encode status without colour alone', () => {
  const unknown = glyphStyle('track', 'unknown');
  assert.equal(unknown.fill, null, 'not assessed is hollow');
  assert.equal(unknown.stroke, COLORS.unknown);
  assert.ok(unknown.dash, 'and dashed');
  const stale = glyphStyle('track', 'stale');
  assert.equal(stale.fillAlpha, 0.5);
  assert.ok(stale.dash, 'stale is dashed');
  const feedDown = glyphStyle('feed', 'critical');
  assert.equal(feedDown.slash, true, 'a down feed is hollow with a slash');
  assert.equal(feedDown.fill, null);
  assert.equal(
    glyphStyle('alarm', 'critical').fill,
    COLORS.critical,
    'critical alarms are filled',
  );
  assert.equal(glyphStyle('alarm', 'warn').fill, null);
  assert.equal(
    glyphStyle('mission', 'ok', { phase: 'executing' }).fill,
    COLORS.magenta,
  );
  assert.equal(
    glyphStyle('mission', 'ok', { phase: 'planning' }).stroke,
    COLORS.magenta,
  );
  assert.equal(
    glyphStyle('mission', 'ok', { phase: 'complete' }).stroke,
    COLORS.pencil,
  );
  assert.equal(
    glyphStyle('mission', 'critical', { phase: 'rtb' }).outerRing,
    COLORS.critical,
  );
  assert.equal(
    glyphStyle('unit', 'warn').stroke,
    COLORS.warn,
    'units are hexagon outlines',
  );
});

test('glyphSvg builds markup only from constants (safe for innerHTML)', () => {
  const svg = glyphSvg('track', { status: 'critical', size: 12 });
  assert.match(
    svg,
    /^<svg class="ic-glyph"[^>]*aria-hidden="true"[^>]*>.*<\/svg>$/,
  );
  assert.match(svg, /width="12"/);
  assert.ok(svg.includes(GLYPHS.track.path));
  const hostile = '"><img src=x onerror=alert(1)>';
  const out = glyphSvg(hostile, {
    status: hostile,
    size: hostile,
    phase: hostile,
    category: hostile,
  });
  assert.ok(!out.includes('<img'), 'no injected markup');
  assert.ok(!out.includes('onerror'), 'no injected attributes');
  assert.ok(
    out.includes(GLYPH_UNRECOGNISED.path),
    'unknown types draw the unrecognised glyph (WG §4.2.1)',
  );
  assert.ok(out.includes(COLORS.unknown) && !out.includes(COLORS.ok));
  assert.match(
    glyphSvg('vehicle', { size: 9999 }),
    /width="64"/,
    'size is clamped',
  );
  const down = glyphSvg('feed', { status: 'critical' });
  assert.equal((down.match(/<path/g) || []).length, 2, 'the slash is drawn');
  assert.match(glyphSvg('track', { status: 'unknown' }), /stroke-dasharray/);
  assert.match(
    glyphSvg('mission', { status: 'warn', phase: 'executing' }),
    /<circle/,
  );
});

test('parsePath and tracePath replay glyphs on a context without Path2D', () => {
  const commands = parsePath(GLYPHS.theater.path);
  assert.equal(
    commands.filter((c) => c[0] === 'A').length,
    4,
    'two circles of two semicircles',
  );
  const ctx = fakeCtx();
  tracePath(ctx, commands);
  assert.equal(ctx.count('arc'), 4);
  const square = parsePath('M7 7H17V17H7Z');
  assert.deepEqual(square, [
    ['M', 7, 7],
    ['L', 17, 7],
    ['L', 17, 17],
    ['L', 7, 17],
    ['Z'],
  ]);
  const report = parsePath(GLYPHS.report.path);
  assert.equal(
    report.filter((c) => c[0] === 'M').length,
    2,
    'the folded corner is a second subpath',
  );
});

test('an info-level alarm is Pencil, never own-systems green (review: SAM alarms read "fine")', () => {
  assert.equal(statusColor('alarm', 'ok'), COLORS.pencil);
  assert.equal(glyphStyle('alarm', 'ok').stroke, COLORS.pencil);
  assert.equal(glyphStyle('alarm', 'ok').fill, null);
  assert.doesNotMatch(
    glyphSvg('alarm', { status: 'ok' }),
    new RegExp(COLORS.ok),
  );
  // Warnings and criticals keep their severity ink; own systems stay green.
  assert.equal(statusColor('alarm', 'warn'), COLORS.warn);
  assert.equal(statusColor('alarm', 'critical'), COLORS.critical);
  assert.equal(statusColor('feed', 'ok'), COLORS.ok);
});

test('a stale mission (picture not live) drops its magenta for the stale grey', () => {
  const fill = glyphStyle('mission', 'stale', { phase: 'executing' });
  assert.equal(fill.fill, COLORS.stale);
  assert.deepEqual(fill.dash, [3, 2]);
  assert.equal(
    glyphStyle('mission', 'stale', { phase: 'planning' }).stroke,
    COLORS.stale,
  );
  assert.equal(
    glyphStyle('mission', 'ok', { phase: 'executing' }).fill,
    COLORS.magenta,
    'a live mission stays magenta',
  );
});

/** Independent tokenizer for the path checks: [{cmd, args}] with every A kept whole. */
function commandsOf(d) {
  const tokens = d.match(/[A-Za-z]|-?\d*\.?\d+/g) || [];
  const arity = { M: 2, L: 2, H: 1, V: 1, A: 7, Z: 0 };
  const out = [];
  let i = 0;
  let cmd = null;
  while (i < tokens.length) {
    if (/^[A-Za-z]$/.test(tokens[i])) cmd = tokens[i++];
    assert.ok(Object.hasOwn(arity, cmd), `command ${cmd} is allowed`);
    const args = tokens.slice(i, i + arity[cmd]).map(Number);
    assert.equal(args.length, arity[cmd], `${cmd} has all its numbers`);
    i += arity[cmd];
    out.push({ cmd, args });
    if (cmd === 'M') cmd = 'L';
  }
  return out;
}

/** Every point a path touches, arcs sampled along their semicircle. */
function extentOf(d) {
  const pts = [];
  let x = 0;
  let y = 0;
  let start = [0, 0];
  for (const { cmd, args } of commandsOf(d)) {
    if (cmd === 'M' || cmd === 'L') [x, y] = args;
    else if (cmd === 'H') [x] = args;
    else if (cmd === 'V') [y] = args;
    else if (cmd === 'Z') [x, y] = start;
    else if (cmd === 'A') {
      const [r, , , , sweep, x1, y1] = args;
      const cx = (x + x1) / 2;
      const cy = (y + y1) / 2;
      const a0 = Math.atan2(y - cy, x - cx);
      for (let k = 0; k <= 16; k += 1) {
        const a = a0 + (sweep ? 1 : -1) * Math.PI * (k / 16);
        pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
      }
      [x, y] = [x1, y1];
    }
    if (cmd === 'M') start = [x, y];
    pts.push([x, y]);
  }
  return pts;
}

const ALL_PATHS = [
  ...SITE_CATEGORIES.map((key) => [`site ${key}`, SITE_GLYPHS[key]]),
  ['unrecognised', UNRECOGNISED_GLYPH],
];

test('glyphPaths: every path parses, every arc is a semicircle, and stays within 1–23', () => {
  assert.equal(SITE_CATEGORIES.length, 13, 'thirteen site glyphs');
  assert.ok(Object.isFrozen(SITE_GLYPHS) && Object.isFrozen(SITE_CATEGORIES));
  for (const [name, d] of ALL_PATHS) {
    assert.match(d, /^M[MLHVAZ0-9. ]+$/, `${name}: absolute M L H V A Z only`);
    const parsed = parsePath(d);
    assert.ok(parsed.length > 1, `${name} parses`);
    for (const c of parsed)
      for (const v of c.slice(1))
        assert.ok(Number.isFinite(v), `${name}: finite numbers`);
    const commands = commandsOf(d);
    assert.equal(
      parsed.length,
      commands.length,
      `${name}: parsePath consumes every command`,
    );
    for (const { cmd, args } of commands) {
      if (cmd !== 'A') continue;
      const [rx, ry, rot] = args;
      assert.equal(rx, ry, `${name}: circular arcs`);
      assert.equal(rot, 0, `${name}: no rotation`);
    }
    // Endpoint distance = diameter: checked against the previous point.
    let x = 0;
    let y = 0;
    for (const { cmd, args } of commands) {
      if (cmd === 'A') {
        const [r, , , , , x1, y1] = args;
        assert.ok(
          Math.abs(Math.hypot(x1 - x, y1 - y) - 2 * r) < 1e-9,
          `${name}: A from ${x},${y} to ${x1},${y1} is a semicircle`,
        );
        [x, y] = [x1, y1];
      } else if (cmd === 'M' || cmd === 'L') [x, y] = args;
      else if (cmd === 'H') [x] = args;
      else if (cmd === 'V') [y] = args;
    }
    for (const [px, py] of extentOf(d)) {
      assert.ok(
        px >= 1 - 1e-9 && px <= 23 + 1e-9 && py >= 1 - 1e-9 && py <= 23 + 1e-9,
        `${name}: (${px.toFixed(2)}, ${py.toFixed(2)}) inside 1–23`,
      );
    }
  }
  // The canvas fallback traces the port's semicircles as arcs.
  const ctx = fakeCtx();
  tracePath(ctx, parsePath(SITE_GLYPHS.port));
  assert.equal(ctx.count('arc'), 3);
});

test('an unknown site category draws the "other" pin', () => {
  for (const odd of ['volcano', '', null, undefined, '__proto__', 42]) {
    assert.equal(siteCategoryKey(odd), 'other');
    assert.equal(siteGlyphPath(odd), SITE_GLYPHS.other);
    assert.equal(glyphFor('site', { category: odd }).path, SITE_GLYPHS.other);
  }
  assert.equal(
    glyphFor('site', { category: 'airfield' }).path,
    SITE_GLYPHS.airfield,
  );
  assert.equal(glyphFor('site').path, SITE_GLYPHS.other);
});

test('fail-safe: an unknown node type is lilac "unrecognised", never green (WG §4.2.1)', () => {
  assert.ok(NODE_TYPES.includes('site') && NODE_TYPES.length === 14);
  for (const type of [
    'force_red',
    'engagement_band',
    'strike_package',
    'mystery',
    '',
    '__proto__',
    'constructor',
    undefined,
  ]) {
    assert.equal(
      glyphFor(type),
      GLYPH_UNRECOGNISED,
      `${type} draws the unrecognised glyph`,
    );
    for (const status of [
      'ok',
      'warn',
      'critical',
      'stale',
      'unknown',
      'nonsense',
    ]) {
      assert.notEqual(
        statusColor(type, status),
        COLORS.ok,
        `${type}/${status} is never green`,
      );
      assert.equal(
        statusColor(type, status),
        COLORS.unknown,
        'status is ignored',
      );
      const style = glyphStyle(type, status);
      assert.equal(style.stroke, COLORS.unknown);
      assert.equal(style.fill, null);
      assert.equal(style.outerRing, null);
      assert.equal(style.slash, false);
    }
  }
  const svg = glyphSvg('strike_package', { status: 'ok' });
  assert.ok(svg.includes(UNRECOGNISED_GLYPH) && svg.includes(COLORS.unknown));
  assert.ok(!svg.includes(COLORS.ok));
  // A force without scenario provenance is not framed: it keeps the "?"
  // (WG §5.3.4; wargameStyles.test.mjs covers the frames).
  const bare = glyphSvg('force', { status: 'ok' });
  assert.ok(bare.includes(UNRECOGNISED_GLYPH) && !bare.includes(COLORS.ok));
});

test('sites are Pencil outlines whatever their status (context, never status-coloured)', () => {
  for (const status of ['ok', 'warn', 'critical', 'stale', 'unknown']) {
    assert.equal(statusColor('site', status), COLORS.pencil);
    const style = glyphStyle('site', status);
    assert.equal(
      style.stroke,
      COLORS.pencil,
      `${status} site is stroked Pencil`,
    );
    assert.equal(style.fill, null);
  }
  const svg = glyphSvg('site', { status: 'critical', category: 'medical' });
  assert.ok(svg.includes(SITE_GLYPHS.medical));
  assert.ok(
    svg.includes(`stroke="${COLORS.pencil}"`) && svg.includes('fill="none"'),
  );
  assert.ok(!svg.includes(COLORS.critical));
});
