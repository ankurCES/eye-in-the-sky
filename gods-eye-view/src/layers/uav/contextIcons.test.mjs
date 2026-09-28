import test from 'node:test';
import assert from 'node:assert/strict';

import { SITE_GLYPHS } from '../../console/orb/glyphPaths.js';
import { createContextIcons, drawSiteIcon } from './contextIcons.js';
import {
  ICON_PLATE_ALPHA,
  ICON_PX,
  ICON_SCALE,
  PENCIL,
  SLATE,
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
