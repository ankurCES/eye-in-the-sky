/**
 * Node glyphs and the status palette (UX spec §2.1–2.2, §4.3).
 *
 * One source for the canvas (`new Path2D(d)`) and for inline SVG in chips and
 * search results. Glyphs are SVG path strings in a 24×24 box whose nominal
 * node radius is 9 units. `glyphSvg()` builds markup ONLY from the constants
 * below — every caller-supplied value is looked up or clamped — so its output
 * is safe to assign to innerHTML. Icon fonts are never used for these.
 *
 * Fail-safe (WG spec §4.2.1): a node type the console does not know draws
 * as the lilac `unrecognised` glyph, whatever its status, and is never
 * green. Sites draw their category glyph stroked in Pencil (§4.2.6).
 *
 * The simulated wargame (§5.3.4, §5.3.6): `force`, `engagement` and
 * `vector` draw from wargameStyles.js, which reads their side, state, phase,
 * consequence and kind from the node's attrs (pass `attrs`). A frame is
 * drawn only on a scenario force; nothing here is ever green. Surfaces
 * that have not opted in to the wargame keep treating these types as
 * unknown: `isKnownType(type)` is false for them unless `{wargame: true}`.
 */

import {
  SITE_CATEGORIES,
  SITE_GLYPHS,
  UNRECOGNISED_GLYPH,
  siteCategoryKey,
} from './glyphPaths.js';
import {
  WARGAME_TYPES,
  WG_INK,
  isWargameType,
  wargameGlyphStyle,
} from './wargameStyles.js';

export const COLORS = Object.freeze({
  slate: '#1B2630',
  panel: '#22303B',
  raised: '#2A3945',
  bodyLight: '#243340',
  film: '#E6ECEF',
  pencil: '#A3B0BA',
  magenta: '#F27AD6',
  analyst: '#8CC8FF',
  graticule: '#3A4A56',
  edge: '#7A8B97',
  hairline: 'rgba(230,236,239,0.10)',
  film35: 'rgba(230,236,239,0.35)',
  limb: 'rgba(230,236,239,0.16)',
  ok: '#5DD39B',
  warn: '#F2B544',
  critical: '#FF7B7B',
  stale: '#8A98A2',
  unknown: '#BBA7E0',
});

export const STATUSES = Object.freeze([
  'ok',
  'warn',
  'critical',
  'stale',
  'unknown',
]);
const STATUS_SET = new Set(STATUSES);

/** Contacts and units: never green; an assessed-low contact is film white. */
const CONTACT_TYPES = new Set(['track', 'unit']);
/** Reference data and places: always Pencil, never status-coloured. */
const NEUTRAL_TYPES = new Set(['equipment', 'poi', 'site']);

/** Normalise a status string to one of STATUSES (unknown otherwise). */
export function statusKey(status) {
  return STATUS_SET.has(status) ? status : 'unknown';
}

/**
 * The ink for a node of `type` in `status` (spec §4.3 colour table). Green
 * is reserved for own systems (§2.2): an info-level alarm ("new contact:
 * sam") is Pencil, so it and its `about` edges never read as "fine".
 */
export function statusColor(type, status, { attrs } = {}) {
  if (isWargameType(type)) {
    if (attrs && typeof attrs === 'object')
      return wargameGlyphStyle(type, attrs, status).color;
    const table =
      type === 'force' ? WARGAME_STATUS_INK.force : WARGAME_STATUS_INK.other;
    return table[statusKey(status)] ?? COLORS.unknown;
  }
  if (!isKnownType(type)) return COLORS.unknown;
  if (NEUTRAL_TYPES.has(type)) return COLORS.pencil;
  const key = statusKey(status);
  if (key === 'ok') {
    if (CONTACT_TYPES.has(type)) return COLORS.film;
    return type === 'alarm' ? COLORS.pencil : COLORS.ok;
  }
  return COLORS[key];
}

export const GLYPH_BOX = 24;
export const GLYPH_NOMINAL_RADIUS = 9;

/** {[nodeType]: {path, filled}} — SVG path `d` in a 24×24 box. */
export const GLYPHS = Object.freeze({
  vehicle: Object.freeze({
    path: 'M12 2.5L20.5 20.5L12 15.5L3.5 20.5Z',
    filled: true,
  }),
  mission: Object.freeze({
    path: 'M12 2.5L21.5 12L12 21.5L2.5 12Z',
    filled: true,
  }),
  track: Object.freeze({
    path: 'M3 12A9 9 0 1 0 21 12A9 9 0 1 0 3 12Z',
    filled: true,
  }),
  unit: Object.freeze({
    path: 'M12 3L19.8 7.5L19.8 16.5L12 21L4.2 16.5L4.2 7.5Z',
    filled: false,
  }),
  equipment: Object.freeze({ path: 'M7.5 7.5H16.5V16.5H7.5Z', filled: true }),
  report: Object.freeze({
    path: 'M6 2.5H14.5L18.5 6.5V21.5H6ZM14.5 2.5V6.5H18.5',
    filled: false,
  }),
  theater: Object.freeze({
    path: 'M3 12A9 9 0 1 0 21 12A9 9 0 1 0 3 12ZM7 12A5 5 0 1 0 17 12A5 5 0 1 0 7 12Z',
    filled: false,
  }),
  poi: Object.freeze({
    path: 'M10.5 3.5H13.5V10.5H20.5V13.5H13.5V20.5H10.5V13.5H3.5V10.5H10.5Z',
    filled: true,
  }),
  alarm: Object.freeze({ path: 'M12 2.5L22 20.5H2Z', filled: false }),
  feed: Object.freeze({
    path: 'M4 14H8V21H4ZM10 9H14V21H10ZM16 4H20V21H16Z',
    filled: true,
  }),
});

/** The slash over a feed that is down ("No reading"). */
export const GLYPH_SLASH = 'M3 21.5L21 2.5';

/** The ISR node types: the contract's ten plus `site`. */
export const ISR_NODE_TYPES = Object.freeze([...Object.keys(GLYPHS), 'site']);
/** Every node type the orb draws: the ISR types plus the wargame's three. */
export const NODE_TYPES = Object.freeze([...ISR_NODE_TYPES, ...WARGAME_TYPES]);
const ISR_TYPES = new Set(ISR_NODE_TYPES);

/**
 * Whether a surface knows how to draw and word a node type. The wargame
 * types (`force`, `engagement`, `vector`) count only with `{wargame: true}`:
 * a surface opts in once it can show them properly (the orb does; the rail,
 * search, inspector and chips do when their Phase B units land), and until
 * then they fail safe to "Unrecognised", never to another type's look.
 * @param {string} type node type
 * @param {{wargame?: boolean}} [options]
 */
export function isKnownType(type, { wargame = false } = {}) {
  if (typeof type !== 'string') return false;
  return ISR_TYPES.has(type) || (wargame === true && isWargameType(type));
}

/** Whether the orb draws a type as itself (ISR and wargame types). */
export function isOrbType(type) {
  return isKnownType(type, { wargame: true });
}

/** A wargame status as ink when no attrs say more: never green. */
const WARGAME_STATUS_INK = Object.freeze({
  force: Object.freeze({
    ok: WG_INK.film,
    warn: WG_INK.warn,
    critical: WG_INK.critical,
    stale: WG_INK.stale,
  }),
  other: Object.freeze({
    ok: WG_INK.pencil,
    warn: WG_INK.warn,
    critical: WG_INK.critical,
    stale: WG_INK.stale,
  }),
});

/** The fail-safe glyph for a type the console does not know (§4.2.1). */
export const GLYPH_UNRECOGNISED = Object.freeze({
  path: UNRECOGNISED_GLYPH,
  filled: false,
});

/** {[category]: {path, filled}} for site nodes: outlines only. */
export const SITE_GLYPH_SPECS = Object.freeze(
  Object.fromEntries(
    SITE_CATEGORIES.map((key) => [
      key,
      Object.freeze({ path: SITE_GLYPHS[key], filled: false }),
    ]),
  ),
);

/**
 * Glyph spec for a type. Sites take their category's glyph (`other` when the
 * category is unknown); wargame types take their frame, burst or arrow from
 * their attrs (a force without scenario provenance gets no frame: the "?");
 * a type the console does not know takes the `unrecognised` glyph, never
 * another type's.
 * @param {string} type node type
 * @param {{category?: string, attrs?: object}} [options] a site's
 *   `attrs.category`; a wargame node's attrs
 * @returns {{path: string, filled: boolean, bar?: string|null}}
 */
export function glyphFor(type, { category, attrs } = {}) {
  if (type === 'site') return SITE_GLYPH_SPECS[siteCategoryKey(category)];
  if (isWargameType(type)) {
    const style = wargameGlyphStyle(type, attrs, 'unknown');
    return { path: style.path, filled: style.filled, bar: style.bar };
  }
  return isKnownType(type) ? GLYPHS[type] : GLYPH_UNRECOGNISED;
}

/** Mission phase → how the diamond is painted. */
export function missionPhaseClass(phase) {
  if (phase === 'executing' || phase === 'rtb') return 'fill';
  if (phase === 'planning') return 'outline';
  return 'done';
}

/**
 * How to paint a glyph: shared by the canvas sprites and the SVG chips.
 * @returns {{fill: string|null, stroke: string|null, dash: number[]|null,
 *   fillAlpha: number, slash: boolean, outerRing: string|null, color: string}}
 */
export function glyphStyle(type, status, { phase, attrs } = {}) {
  if (isWargameType(type)) {
    const w = wargameGlyphStyle(type, attrs, statusKey(status));
    return {
      fill: w.fill,
      stroke: w.stroke,
      dash: w.dash,
      fillAlpha: w.fillAlpha,
      slash: w.slash,
      outerRing: w.outerRing,
      color: w.color,
      bar: w.bar,
      barDash: w.barDash,
      alpha: w.alpha,
      slashPath: w.slashPath,
      halo: w.halo,
    };
  }
  const key = statusKey(status);
  const color = statusColor(type, key);
  const style = {
    fill: null,
    stroke: null,
    dash: null,
    fillAlpha: 1,
    slash: false,
    outerRing: null,
    color,
  };
  // Fail-safe: status is ignored for a type the console does not know.
  if (!isKnownType(type)) {
    style.stroke = COLORS.unknown;
    return style;
  }
  if (NEUTRAL_TYPES.has(type)) {
    if (glyphFor(type).filled) style.fill = COLORS.pencil;
    else style.stroke = COLORS.pencil;
    return style;
  }
  if (type === 'mission') {
    const paint = missionPhaseClass(phase);
    // Missions are never stale on the server; stale here means the picture
    // is not live, and the canvas desaturates (spec §4.6).
    const ink = key === 'stale' ? COLORS.stale : COLORS.magenta;
    if (paint === 'fill') style.fill = ink;
    else style.stroke = paint === 'outline' ? ink : COLORS.pencil;
    if (key === 'warn' || key === 'critical') style.outerRing = color;
    if (key === 'stale') style.dash = [3, 2];
    style.color = style.fill || style.stroke;
    return style;
  }
  const filled = glyphFor(type).filled;
  if (key === 'unknown') {
    style.stroke = COLORS.unknown;
    style.dash = [3, 2.5];
    return style;
  }
  if (key === 'stale') {
    if (filled) {
      style.fill = COLORS.stale;
      style.fillAlpha = 0.5;
    }
    style.stroke = COLORS.stale;
    style.dash = [3, 2];
    return style;
  }
  if (type === 'feed' && key === 'critical') {
    style.stroke = COLORS.critical;
    style.slash = true;
    return style;
  }
  if (type === 'alarm') {
    if (key === 'critical') style.fill = color;
    else style.stroke = color;
    return style;
  }
  if (filled) style.fill = color;
  else style.stroke = color;
  return style;
}

const PHASES = new Set([
  'planning',
  'executing',
  'rtb',
  'complete',
  'aborted',
  'unknown',
]);

/**
 * Inline SVG markup for a node glyph (chips, search rows, the inspector).
 * Built from constants only: `type`, `status`, `phase` and `category` are
 * looked up, a wargame node's `attrs` are read only through the closed
 * vocabularies of wargameStyles.js, and `size` is clamped, so the result is
 * safe for innerHTML. An unknown type draws the lilac `unrecognised` glyph
 * (§4.2.1); a force draws its frame only with scenario provenance (§5.3.4).
 * @param {string} type node type
 * @param {{status?: string, size?: number, phase?: string, category?: string,
 *   attrs?: object}} [options]
 * @returns {string}
 */
export function glyphSvg(
  type,
  { status, size = 16, phase, category, attrs: nodeAttrs } = {},
) {
  const safeType = isOrbType(type) ? type : '';
  const wargame = isWargameType(safeType);
  const wgAttrs = wargame ? nodeAttrs : undefined;
  const glyph = glyphFor(safeType, { category, attrs: wgAttrs });
  const px = Math.round(Math.min(64, Math.max(8, Number(size) || 16)));
  const style = glyphStyle(safeType, statusKey(status), {
    phase: PHASES.has(phase) ? phase : undefined,
    attrs: wgAttrs,
  });
  const attrs = [];
  attrs.push(`fill="${style.fill || 'none'}"`);
  if (style.fill && style.fillAlpha < 1)
    attrs.push(`fill-opacity="${style.fillAlpha}"`);
  if (style.stroke) {
    attrs.push(
      `stroke="${style.stroke}"`,
      'stroke-width="2"',
      'stroke-linejoin="round"',
    );
    if (style.dash) attrs.push(`stroke-dasharray="${style.dash.join(' ')}"`);
  }
  let body = `<path d="${glyph.path}" ${attrs.join(' ')}/>`;
  if (glyph.bar && style.stroke) {
    body += `<path d="${glyph.bar}" fill="none" stroke="${style.stroke}" stroke-width="2" stroke-linecap="round"/>`;
  }
  if (style.slash) {
    const ink = wargame ? style.stroke || style.color : COLORS.critical;
    body += `<path d="${style.slashPath || GLYPH_SLASH}" fill="none" stroke="${ink}" stroke-width="2" stroke-linecap="round"/>`;
  }
  if (style.outerRing) {
    body =
      `<circle cx="12" cy="12" r="11" fill="none" stroke="${style.outerRing}" stroke-width="1.5"/>` +
      body;
  }
  if (wargame && style.alpha < 1)
    body = `<g opacity="${style.alpha}">${body}</g>`;
  return (
    `<svg class="ic-glyph" xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" ` +
    `viewBox="0 0 24 24" aria-hidden="true" focusable="false">${body}</svg>`
  );
}

/**
 * Parse the constant glyph paths (absolute M, L, H, V, A, Z) into commands,
 * so the canvas can trace them where Path2D is unavailable.
 * @returns {Array<Array<string|number>>}
 */
export function parsePath(d) {
  const tokens = String(d).match(/[MLHVAZ]|-?\d*\.?\d+(?:e-?\d+)?/gi) || [];
  const out = [];
  let i = 0;
  let cmd = null;
  let x = 0;
  let y = 0;
  const num = () => Number(tokens[i++]);
  while (i < tokens.length) {
    if (/^[MLHVAZ]$/i.test(tokens[i])) cmd = tokens[i++].toUpperCase();
    if (cmd === 'Z') {
      out.push(['Z']);
      cmd = null;
      continue;
    }
    if (cmd === 'M' || cmd === 'L') {
      x = num();
      y = num();
      out.push([cmd, x, y]);
      if (cmd === 'M') cmd = 'L';
    } else if (cmd === 'H') {
      x = num();
      out.push(['L', x, y]);
    } else if (cmd === 'V') {
      y = num();
      out.push(['L', x, y]);
    } else if (cmd === 'A') {
      const rx = num();
      num(); // ry (glyph arcs are circular)
      num(); // x-axis rotation
      num(); // large-arc flag
      const sweep = num();
      const x1 = num();
      const y1 = num();
      out.push(['A', x, y, rx, sweep, x1, y1]);
      x = x1;
      y = y1;
    } else {
      i += 1;
    }
  }
  return out;
}

/** Replay parsed glyph commands onto a 2D context (semicircular arcs only). */
export function tracePath(ctx, commands) {
  for (const c of commands) {
    if (c[0] === 'M') ctx.moveTo(c[1], c[2]);
    else if (c[0] === 'L') ctx.lineTo(c[1], c[2]);
    else if (c[0] === 'Z') ctx.closePath();
    else if (c[0] === 'A') {
      const [, x0, y0, r, sweep, x1, y1] = c;
      const cx = (x0 + x1) / 2;
      const cy = (y0 + y1) / 2;
      const start = Math.atan2(y0 - cy, x0 - cx);
      const end = Math.atan2(y1 - cy, x1 - cx);
      ctx.arc(cx, cy, r, start, end, !sweep);
    }
  }
}
