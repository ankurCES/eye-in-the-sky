/**
 * @module uav/contextIcons
 * @description Cached canvas icons for the map's site billboards
 * (WG v2 §4.2.7): a slate plate at 80 % with the category glyph stroked in
 * Pencil, one canvas per category, drawn once and reused by every billboard.
 *
 * The glyph paths are the console's own (`console/orb/glyphPaths.js`, R24), so
 * the orb, the chips and the map draw the same airfield. Canvas and Path2D are
 * injected: headless (unit tests, a page without 2D canvas) the factory
 * answers null and the entity builder falls back to a plain point, so a
 * missing canvas costs the icon, never the site.
 */
import {
  GLYPH_PATH_BOX,
  siteCategoryKey,
  siteGlyphPath,
} from '../../console/orb/glyphPaths.js';
import {
  GLYPH_STROKE,
  ICON_PLATE_ALPHA,
  ICON_PX,
  ICON_SCALE,
  PENCIL,
  SLATE,
} from './contextPolicy.js';

/** Glyph box inset inside the plate (CSS px each side). */
const GLYPH_INSET_PX = 4;
/** Plate corner radius (CSS px). */
const PLATE_RADIUS_PX = 7;

function defaultCreateCanvas() {
  const doc = globalThis.document;
  if (!doc?.createElement) return null;
  try {
    return doc.createElement('canvas');
  } catch {
    return null;
  }
}

/** A rounded rectangle path, with a plain-rectangle fallback. */
function platePath(ctx, size, radius) {
  ctx.beginPath();
  if (typeof ctx.roundRect === 'function') {
    ctx.roundRect(0, 0, size, size, radius);
  } else {
    ctx.rect(0, 0, size, size);
  }
  ctx.closePath();
}

/**
 * Paint one icon into a 2D context sized `ICON_PX * scale` square.
 * @param {CanvasRenderingContext2D} ctx Target context.
 * @param {string} d Glyph path (24-unit box).
 * @param {{scale?: number, Path2DImpl?: Function}} [options] Backing scale and
 *   the Path2D constructor.
 * @returns {boolean} True when the glyph was stroked.
 */
export function drawSiteIcon(ctx, d, { scale = ICON_SCALE, Path2DImpl } = {}) {
  if (!ctx) return false;
  const size = ICON_PX * scale;
  ctx.save?.();
  ctx.clearRect?.(0, 0, size, size);
  ctx.globalAlpha = ICON_PLATE_ALPHA;
  ctx.fillStyle = SLATE;
  platePath(ctx, size, PLATE_RADIUS_PX * scale);
  ctx.fill();
  ctx.globalAlpha = 1;
  const box = (ICON_PX - GLYPH_INSET_PX * 2) * scale;
  const unit = box / GLYPH_PATH_BOX;
  ctx.translate(GLYPH_INSET_PX * scale, GLYPH_INSET_PX * scale);
  ctx.scale(unit, unit);
  ctx.strokeStyle = PENCIL;
  ctx.lineWidth = GLYPH_STROKE;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  let stroked = false;
  if (typeof Path2DImpl === 'function') {
    try {
      ctx.stroke(new Path2DImpl(d));
      stroked = true;
    } catch {
      stroked = false;
    }
  }
  ctx.restore?.();
  return stroked;
}

/**
 * Create the per-category icon cache.
 * @param {object} [options]
 * @param {() => object|null} [options.createCanvas] Canvas factory.
 * @param {Function} [options.Path2DImpl] Path2D constructor.
 * @param {number} [options.scale] Backing scale.
 * @returns {{iconFor: Function, size: number, clear: Function}} Icon cache.
 */
export function createContextIcons({
  createCanvas = defaultCreateCanvas,
  Path2DImpl = globalThis.Path2D,
  scale = ICON_SCALE,
} = {}) {
  const cache = new Map();

  /**
   * The icon canvas for a site category (unknown categories get `other`), or
   * null when no 2D canvas is available.
   * @param {string} category Site category.
   * @returns {object|null} Canvas.
   */
  function iconFor(category) {
    const key = siteCategoryKey(category);
    if (cache.has(key)) return cache.get(key);
    let canvas = null;
    try {
      canvas = createCanvas?.() ?? null;
      const ctx = canvas?.getContext?.('2d') ?? null;
      if (!ctx) {
        canvas = null;
      } else {
        canvas.width = ICON_PX * scale;
        canvas.height = ICON_PX * scale;
        drawSiteIcon(ctx, siteGlyphPath(key), { scale, Path2DImpl });
      }
    } catch {
      canvas = null;
    }
    cache.set(key, canvas);
    return canvas;
  }

  return {
    iconFor,
    /** On-screen size every icon is drawn at (CSS px). */
    size: ICON_PX,
    /** Drop every cached canvas (teardown). */
    clear: () => cache.clear(),
  };
}
