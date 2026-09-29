/**
 * @module uav/contextIcons
 * @description Cached canvas icons for the map's site billboards
 * (WG v2 §4.2.7): a slate plate at 80 % with the category glyph stroked in
 * Pencil, one canvas per category, drawn once and reused by every billboard.
 * Phase B (§5.3.12) adds the simulated wargame's force frames, the Sand
 * engagement burst and the outcome ring, each cached by a key built from
 * closed-vocabulary constants (`contextPolicy.frameSpec` and friends).
 *
 * The glyph paths are the console's own (`console/orb/glyphPaths.js`, R24), so
 * the orb, the chips and the map draw the same airfield. Canvas and Path2D are
 * injected: headless (unit tests, a page without 2D canvas) the factory
 * answers null and the entity builder falls back to a plain point, so a
 * missing canvas costs the icon, never the site.
 */
import {
  ENGAGEMENT_GLYPH,
  FRAME_BARS,
  FRAME_BARS_BROKEN,
  FRAME_OUTLINES,
  FRAME_SLASH,
  GLYPH_PATH_BOX,
  siteCategoryKey,
  siteGlyphPath,
} from '../../console/orb/glyphPaths.js';
import {
  FRAME_FILL_ALPHA,
  GLYPH_STROKE,
  ICON_PLATE_ALPHA,
  ICON_PX,
  ICON_SCALE,
  PENCIL,
  RING_PX,
  SLATE,
  WARGAME_INK,
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

// ---- the simulated wargame (§5.3.4, §5.3.6, §5.3.12) -------------------------

/** Dash patterns (glyph units): suppressed frames, unknown sides, waiting bursts. */
const FRAME_DASHES = Object.freeze({ frame: [4, 3], unknown: [3, 2.5] });
const BURST_DASH = Object.freeze([3, 2]);
/** A dark under-stroke keeps a light frame legible over bright imagery. */
const UNDER_STROKE = 'rgba(0,0,0,0.55)';
/** The halo of a red unit whose threat reaches us (glyph units). */
const HALO_RADIUS = 13.5;
/** The burst's Sand fill once adjudicated. */
const BURST_FILL_ALPHA = 0.35;

function pathOp(ctx, Path2DImpl, d, op) {
  if (typeof Path2DImpl !== 'function') return false;
  try {
    ctx[op](new Path2DImpl(d));
    return true;
  } catch {
    return false;
  }
}

/** Clear a `px`-square icon and map the 24-unit glyph box into its middle. */
function glyphSpace(ctx, px, scale) {
  const size = px * scale;
  ctx.save?.();
  ctx.clearRect?.(0, 0, size, size);
  const inset = (px - (ICON_PX - GLYPH_INSET_PX * 2)) / 2;
  const unit = ((ICON_PX - GLYPH_INSET_PX * 2) * scale) / GLYPH_PATH_BOX;
  ctx.translate(inset * scale, inset * scale);
  ctx.scale(unit, unit);
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
}

/**
 * Paint a force frame (`contextPolicy.frameSpec`): the side's outline filled
 * with its hue at 70 % under a Film stroke, the state bar in the status ink,
 * a slash when destroyed, and a halo for a red unit whose threat reaches us.
 * @param {CanvasRenderingContext2D} ctx Target context (ICON_PX * scale).
 * @param {object} spec Frame spec.
 * @param {{scale?: number, Path2DImpl?: Function}} [options]
 * @returns {boolean} True when the outline was stroked.
 */
export function drawFrameIcon(
  ctx,
  spec,
  { scale = ICON_SCALE, Path2DImpl } = {},
) {
  if (!ctx || !spec) return false;
  const side = Object.hasOwn(FRAME_OUTLINES, spec.side) ? spec.side : 'unknown';
  const outline = FRAME_OUTLINES[side];
  const alpha = Number.isFinite(spec.alpha) ? spec.alpha : 1;
  glyphSpace(ctx, ICON_PX, scale);
  ctx.globalAlpha = alpha;
  if (spec.halo) {
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = WARGAME_INK.critical;
    ctx.beginPath?.();
    ctx.arc?.(12, 12, HALO_RADIUS, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (spec.fill) {
    ctx.globalAlpha = alpha * FRAME_FILL_ALPHA;
    ctx.fillStyle = spec.fill;
    pathOp(ctx, Path2DImpl, outline, 'fill');
    ctx.globalAlpha = alpha;
  }
  ctx.lineWidth = GLYPH_STROKE + 1.5;
  ctx.strokeStyle = UNDER_STROKE;
  pathOp(ctx, Path2DImpl, outline, 'stroke');
  ctx.lineWidth = GLYPH_STROKE;
  ctx.strokeStyle = spec.stroke;
  ctx.setLineDash?.(spec.dash ? (FRAME_DASHES[spec.dash] ?? []) : []);
  const stroked = pathOp(ctx, Path2DImpl, outline, 'stroke');
  ctx.setLineDash?.([]);
  ctx.strokeStyle = spec.bar;
  const bars = spec.barBroken ? FRAME_BARS_BROKEN : FRAME_BARS;
  pathOp(ctx, Path2DImpl, bars[side], 'stroke');
  if (spec.slash) {
    ctx.strokeStyle = WARGAME_INK.stale;
    pathOp(ctx, Path2DImpl, FRAME_SLASH, 'stroke');
  }
  ctx.restore?.();
  return stroked;
}

/**
 * Paint an engagement's Sand burst (`contextPolicy.burstSpec`).
 * @param {CanvasRenderingContext2D} ctx Target context (ICON_PX * scale).
 * @param {object} spec Burst spec.
 * @param {{scale?: number, Path2DImpl?: Function}} [options]
 * @returns {boolean} True when the burst was stroked.
 */
export function drawBurstIcon(
  ctx,
  spec,
  { scale = ICON_SCALE, Path2DImpl } = {},
) {
  if (!ctx || !spec) return false;
  const alpha = Number.isFinite(spec.alpha) ? spec.alpha : 1;
  glyphSpace(ctx, ICON_PX, scale);
  ctx.globalAlpha = alpha;
  if (spec.fill) {
    ctx.globalAlpha = alpha * BURST_FILL_ALPHA;
    ctx.fillStyle = spec.fill;
    pathOp(ctx, Path2DImpl, ENGAGEMENT_GLYPH, 'fill');
    ctx.globalAlpha = alpha;
  }
  ctx.lineWidth = GLYPH_STROKE + 1.5;
  ctx.strokeStyle = UNDER_STROKE;
  pathOp(ctx, Path2DImpl, ENGAGEMENT_GLYPH, 'stroke');
  ctx.lineWidth = GLYPH_STROKE;
  ctx.strokeStyle = spec.stroke;
  ctx.setLineDash?.(spec.dash ? BURST_DASH : []);
  const stroked = pathOp(ctx, Path2DImpl, ENGAGEMENT_GLYPH, 'stroke');
  ctx.setLineDash?.([]);
  ctx.restore?.();
  return stroked;
}

/**
 * Paint an outcome ring (`contextPolicy.ringSpec`): a circle in screen
 * space. It marks an outcome; it is not an effect area, so it never scales
 * with the ground.
 * @param {CanvasRenderingContext2D} ctx Target context (RING_PX * scale).
 * @param {{stroke: string}} spec Ring spec.
 * @param {{scale?: number}} [options]
 * @returns {boolean} True when the ring was stroked.
 */
export function drawRingIcon(ctx, spec, { scale = ICON_SCALE } = {}) {
  if (!ctx || !spec) return false;
  const size = RING_PX * scale;
  const centre = size / 2;
  const radius = centre - 3 * scale;
  ctx.save?.();
  ctx.clearRect?.(0, 0, size, size);
  for (const [width, ink] of [
    [5 * scale, UNDER_STROKE],
    [3 * scale, spec.stroke],
  ]) {
    ctx.lineWidth = width;
    ctx.strokeStyle = ink;
    ctx.beginPath?.();
    ctx.arc?.(centre, centre, radius, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore?.();
  return true;
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

  /**
   * One cached canvas of `px` CSS px, painted once by `paint(ctx)`; null
   * when no 2D canvas is available (the entity falls back to a point).
   */
  function painted(key, px, paint) {
    if (cache.has(key)) return cache.get(key);
    let canvas = null;
    try {
      canvas = createCanvas?.() ?? null;
      const ctx = canvas?.getContext?.('2d') ?? null;
      if (!ctx) {
        canvas = null;
      } else {
        canvas.width = px * scale;
        canvas.height = px * scale;
        paint(ctx);
      }
    } catch {
      canvas = null;
    }
    cache.set(key, canvas);
    return canvas;
  }

  return {
    iconFor,
    /**
     * A force frame canvas for a `contextPolicy.frameSpec` (cached by its
     * closed-vocabulary key), or null.
     */
    frameIcon: (spec) =>
      spec?.key
        ? painted(`frame:${spec.key}`, ICON_PX, (ctx) =>
            drawFrameIcon(ctx, spec, { scale, Path2DImpl }),
          )
        : null,
    /** An engagement burst canvas for a `contextPolicy.burstSpec`, or null. */
    burstIcon: (spec) =>
      spec?.key
        ? painted(`burst:${spec.key}`, ICON_PX, (ctx) =>
            drawBurstIcon(ctx, spec, { scale, Path2DImpl }),
          )
        : null,
    /** An outcome ring canvas for a `contextPolicy.ringSpec`, or null. */
    ringIcon: (spec) =>
      spec?.key
        ? painted(`ring:${spec.key}`, RING_PX, (ctx) =>
            drawRingIcon(ctx, spec, { scale }),
          )
        : null,
    /** On-screen size every icon is drawn at (CSS px). */
    size: ICON_PX,
    /** On-screen size of an outcome ring (CSS px). */
    ringSize: RING_PX,
    /** Drop every cached canvas (teardown). */
    clear: () => cache.clear(),
  };
}
