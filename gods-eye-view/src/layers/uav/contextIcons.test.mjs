import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ENGAGEMENT_GLYPH,
  FRAME_BARS,
  FRAME_BARS_BROKEN,
  FRAME_OUTLINES,
  FRAME_SLASH,
  SITE_GLYPHS,
} from '../../console/orb/glyphPaths.js';
import {
  createContextIcons,
  drawBurstIcon,
  drawFrameIcon,
  drawRingIcon,
  drawSiteIcon,
} from './contextIcons.js';
import {
  FRAME_FILL_ALPHA,
  ICON_PLATE_ALPHA,
  ICON_PX,
  ICON_SCALE,
  PENCIL,
  RING_PX,
  SLATE,
  WARGAME_INK,
  burstSpec,
  frameSpec,
  ringSpec,
} from './contextPolicy.js';

/** A 2D context that records what was painted. */
function recordingContext() {
  const calls = [];
  const ctx = {
    calls,
    fills: [],
    strokes: [],
    save: () => calls.push('save'),
    restore: () => calls.push('restore'),
    clearRect: () => calls.push('clearRect'),
    beginPath: () => calls.push('beginPath'),
    closePath: () => calls.push('closePath'),
    roundRect: (...args) => calls.push(['roundRect', ...args]),
    rect: (...args) => calls.push(['rect', ...args]),
    translate: (...args) => calls.push(['translate', ...args]),
    scale: (...args) => calls.push(['scale', ...args]),
    fill() {
      ctx.fills.push({ fillStyle: ctx.fillStyle, alpha: ctx.globalAlpha });
    },
    stroke(path) {
      ctx.strokes.push({
        d: path?.d,
        strokeStyle: ctx.strokeStyle,
        lineWidth: ctx.lineWidth,
        lineJoin: ctx.lineJoin,
      });
    },
  };
  return ctx;
}

class FakePath2D {
  constructor(d) {
    this.d = d;
  }
}

function fakeCanvasFactory(made = []) {
  return () => {
    const ctx = recordingContext();
    const canvas = { width: 0, height: 0, ctx, getContext: () => ctx };
    made.push(canvas);
    return canvas;
  };
}

test('an icon is a slate plate at 80 % under the Pencil category glyph', () => {
  const ctx = recordingContext();
  assert.equal(
    drawSiteIcon(ctx, SITE_GLYPHS.airfield, { Path2DImpl: FakePath2D }),
    true,
  );
  assert.deepEqual(ctx.fills, [{ fillStyle: SLATE, alpha: ICON_PLATE_ALPHA }]);
  assert.equal(ctx.strokes.length, 1);
  assert.equal(ctx.strokes[0].d, SITE_GLYPHS.airfield);
  assert.equal(ctx.strokes[0].strokeStyle, PENCIL);
  assert.equal(ctx.strokes[0].lineWidth, 2);
  assert.equal(ctx.strokes[0].lineJoin, 'round');
  // The 24-unit glyph box is scaled into the plate, inside a margin.
  const scale = ctx.calls.find((call) => call[0] === 'scale');
  assert.ok(scale[1] > 0 && scale[1] * 24 < ICON_PX * ICON_SCALE);
});

test('one canvas per category, cached; unknown categories share `other`', () => {
  const made = [];
  const icons = createContextIcons({
    createCanvas: fakeCanvasFactory(made),
    Path2DImpl: FakePath2D,
  });
  const airfield = icons.iconFor('airfield');
  assert.equal(icons.iconFor('airfield'), airfield, 'cached');
  assert.equal(airfield.width, ICON_PX * ICON_SCALE);
  assert.equal(airfield.height, ICON_PX * ICON_SCALE);
  assert.equal(airfield.ctx.strokes[0].d, SITE_GLYPHS.airfield);

  const odd = icons.iconFor('weather_station');
  assert.equal(icons.iconFor('other'), odd, 'an unknown category is `other`');
  assert.equal(odd.ctx.strokes[0].d, SITE_GLYPHS.other);
  assert.equal(made.length, 2);
  assert.equal(icons.size, ICON_PX);
  icons.clear();
  assert.notEqual(icons.iconFor('airfield'), airfield);
});

test('without a 2D canvas the factory answers null (a point is drawn instead)', () => {
  const none = createContextIcons({ createCanvas: () => null });
  assert.equal(none.iconFor('port'), null);
  const no2d = createContextIcons({
    createCanvas: () => ({ getContext: () => null }),
  });
  assert.equal(no2d.iconFor('port'), null);
  const throws = createContextIcons({
    createCanvas: () => {
      throw new Error('no canvas here');
    },
  });
  assert.equal(throws.iconFor('port'), null);
});

test('a missing Path2D still paints the plate and never throws', () => {
  const ctx = recordingContext();
  assert.equal(
    drawSiteIcon(ctx, SITE_GLYPHS.port, { Path2DImpl: undefined }),
    false,
  );
  assert.equal(ctx.fills.length, 1);
  assert.equal(drawSiteIcon(null, SITE_GLYPHS.port), false);
});

// ---- the simulated wargame (WG v2 §5.3.12, B16) --------------------------------

/** A 2D context that records every paint op with the state it used. */
function paintLog() {
  const ops = [];
  let dash = [];
  const ctx = {
    ops,
    save() {},
    restore() {},
    clearRect() {},
    translate() {},
    scale() {},
    beginPath() {},
    arc: (...args) => ops.push({ op: 'arc', args }),
    setLineDash: (value) => {
      dash = [...value];
    },
    fill(path) {
      ops.push({
        op: 'fill',
        d: path?.d,
        style: ctx.fillStyle,
        alpha: ctx.globalAlpha,
      });
    },
    stroke(path) {
      ops.push({
        op: 'stroke',
        d: path?.d,
        style: ctx.strokeStyle,
        width: ctx.lineWidth,
        dash: [...dash],
        alpha: ctx.globalAlpha,
      });
    },
  };
  return ctx;
}

const strokesOf = (ctx, d) =>
  ctx.ops.filter((op) => op.op === 'stroke' && op.d === d);

test('a blue frame: the rectangle filled with the friendly hue at 70 % under a Film stroke', () => {
  const ctx = paintLog();
  const spec = frameSpec({ side: 'blue', state: 'active', status: 'ok' });
  assert.equal(drawFrameIcon(ctx, spec, { Path2DImpl: FakePath2D }), true);
  const fill = ctx.ops.find((op) => op.op === 'fill');
  assert.equal(fill.d, FRAME_OUTLINES.blue);
  assert.equal(fill.style, WARGAME_INK.friendly);
  assert.equal(fill.alpha, FRAME_FILL_ALPHA);
  const outline = strokesOf(ctx, FRAME_OUTLINES.blue).at(-1);
  assert.equal(outline.style, WARGAME_INK.film, 'never relies on its hue');
  assert.deepEqual(outline.dash, []);
  assert.equal(strokesOf(ctx, FRAME_BARS.blue).length, 1);
  assert.equal(strokesOf(ctx, FRAME_SLASH).length, 0);
  assert.equal(ctx.ops.filter((op) => op.op === 'arc').length, 0);
});

test('red frames: a diamond, a halo when critical, the state as a pattern', () => {
  const critical = paintLog();
  drawFrameIcon(
    critical,
    frameSpec({ side: 'red', state: 'damaged', status: 'critical' }),
    { Path2DImpl: FakePath2D },
  );
  assert.equal(
    critical.ops.find((op) => op.op === 'fill').style,
    WARGAME_INK.hostile,
  );
  assert.ok(strokesOf(critical, FRAME_OUTLINES.red).length >= 1);
  const bar = strokesOf(critical, FRAME_BARS_BROKEN.red)[0];
  assert.equal(bar.style, WARGAME_INK.critical, 'damaged breaks the bar');
  const halo = critical.ops.findIndex((op) => op.op === 'arc');
  assert.ok(halo >= 0, 'a steady halo');

  const suppressed = paintLog();
  drawFrameIcon(
    suppressed,
    frameSpec({ side: 'red', state: 'suppressed', status: 'warn' }),
    { Path2DImpl: FakePath2D },
  );
  const outline = strokesOf(suppressed, FRAME_OUTLINES.red).at(-1);
  assert.ok(outline.dash.length > 0, 'suppressed dashes the frame');

  const destroyed = paintLog();
  drawFrameIcon(
    destroyed,
    frameSpec({ side: 'blue', state: 'destroyed', status: 'ok' }),
    { Path2DImpl: FakePath2D },
  );
  const slash = strokesOf(destroyed, FRAME_SLASH)[0];
  assert.equal(slash.style, WARGAME_INK.stale);
  assert.equal(slash.alpha, 0.5, 'a destroyed blue unit at 50 %');
});

test('an unsided force is a lilac dashed quatrefoil with no hue', () => {
  const ctx = paintLog();
  drawFrameIcon(ctx, frameSpec({ side: '?', state: 'active' }), {
    Path2DImpl: FakePath2D,
  });
  assert.equal(ctx.ops.filter((op) => op.op === 'fill').length, 0);
  const outline = strokesOf(ctx, FRAME_OUTLINES.unknown).at(-1);
  assert.equal(outline.style, WARGAME_INK.lilac);
  assert.ok(outline.dash.length > 0);
});

test('the Sand burst dashes while waiting and fills once adjudicated', () => {
  const waiting = paintLog();
  drawBurstIcon(waiting, burstSpec({ phase: 'proposed' }), {
    Path2DImpl: FakePath2D,
  });
  const line = strokesOf(waiting, ENGAGEMENT_GLYPH).at(-1);
  assert.equal(line.style, WARGAME_INK.sand);
  assert.ok(line.dash.length > 0);
  assert.equal(waiting.ops.filter((op) => op.op === 'fill').length, 0);

  const done = paintLog();
  drawBurstIcon(done, burstSpec({ phase: 'adjudicated' }), {
    Path2DImpl: FakePath2D,
  });
  assert.equal(done.ops.find((op) => op.op === 'fill').style, WARGAME_INK.sand);
  assert.deepEqual(strokesOf(done, ENGAGEMENT_GLYPH).at(-1).dash, []);
});

test('an outcome ring is a stroked circle, never filled', () => {
  const ctx = paintLog();
  assert.equal(drawRingIcon(ctx, ringSpec('own_loss')), true);
  assert.equal(ctx.ops.filter((op) => op.op === 'fill').length, 0);
  const arcs = ctx.ops.filter((op) => op.op === 'arc');
  assert.equal(arcs.length, 2, 'a dark under-stroke, then the ring');
  const [cx, cy, r] = arcs[1].args;
  assert.equal(cx, (RING_PX * ICON_SCALE) / 2);
  assert.equal(cy, cx);
  assert.ok(r > 0 && r < cx);
  assert.equal(ctx.ops.at(-1).style, WARGAME_INK.critical);
});

test('frames, bursts and rings are cached per closed-vocabulary key', () => {
  const made = [];
  const icons = createContextIcons({
    createCanvas: fakeCanvasFactory(made),
    Path2DImpl: FakePath2D,
  });
  const spec = frameSpec({ side: 'red', state: 'active', status: 'ok' });
  const frame = icons.frameIcon(spec);
  assert.equal(frame.width, ICON_PX * ICON_SCALE);
  assert.equal(icons.frameIcon({ ...spec }), frame, 'cached by key');
  assert.notEqual(
    icons.frameIcon(frameSpec({ side: 'blue', state: 'active' })),
    frame,
  );
  const ring = icons.ringIcon(ringSpec('none'));
  assert.equal(ring.width, RING_PX * ICON_SCALE);
  assert.equal(
    icons.ringIcon(ringSpec('red_effect')),
    icons.ringIcon(ringSpec('red_effect')),
  );
  assert.ok(icons.burstIcon(burstSpec({ phase: 'denied' })));
  assert.equal(icons.ringSize, RING_PX);
  assert.equal(icons.frameIcon(null), null);
  assert.equal(made.length, 5, 'two frames, two rings, one burst');
  // Sites and wargame icons never share a cache slot.
  assert.notEqual(icons.iconFor('other'), frame);

  const none = createContextIcons({ createCanvas: () => null });
  assert.equal(none.frameIcon(spec), null);
  assert.equal(none.ringIcon(ringSpec('none')), null);
  assert.equal(none.burstIcon(burstSpec({ phase: 'x' })), null);
  assert.equal(drawFrameIcon(null, spec), false);
  assert.equal(drawRingIcon(null, ringSpec('none')), false);
  assert.equal(PENCIL, WARGAME_INK.pencil);
});
