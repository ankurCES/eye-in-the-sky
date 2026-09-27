import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COLORS,
  GLYPHS,
  glyphStyle,
  glyphSvg,
  parsePath,
  statusColor,
  tracePath,
} from './glyphs.js';
import { fakeCtx } from './fixtures.test.mjs';

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
  });
  assert.ok(!out.includes('<img'), 'no injected markup');
  assert.ok(!out.includes('onerror'), 'no injected attributes');
  assert.ok(
    out.includes(GLYPHS.track.path),
    'unknown types fall back to the contact circle',
  );
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
