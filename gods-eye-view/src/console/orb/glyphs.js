/**
 * Node glyphs and the status palette (UX spec §2.1–2.2, §4.3).
 *
 * One source for the canvas (`new Path2D(d)`) and for inline SVG in chips and
 * search results. Glyphs are SVG path strings in a 24×24 box whose nominal
 * node radius is 9 units. `glyphSvg()` builds markup ONLY from the constants
 * below — every caller-supplied value is looked up or clamped — so its output
 * is safe to assign to innerHTML. Icon fonts are never used for these.
 */

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
const NEUTRAL_TYPES = new Set(['equipment', 'poi']);

/** Normalise a status string to one of STATUSES (unknown otherwise). */
export function statusKey(status) {
  return STATUS_SET.has(status) ? status : 'unknown';
}

/**
 * The ink for a node of `type` in `status` (spec §4.3 colour table). Green
 * is reserved for own systems (§2.2): an info-level alarm ("new contact:
 * sam") is Pencil, so it and its `about` edges never read as "fine".
 */
export function statusColor(type, status) {
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

/** Glyph spec for a type, falling back to the contact circle. */
export function glyphFor(type) {
  return Object.hasOwn(GLYPHS, type) ? GLYPHS[type] : GLYPHS.track;
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
export function glyphStyle(type, status, { phase } = {}) {
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
  if (NEUTRAL_TYPES.has(type)) {
    style.fill = COLORS.pencil;
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
 * Built from constants only: `type`, `status` and `phase` are looked up and
 * `size` is clamped, so the result is safe for innerHTML.
 * @param {string} type node type
 * @param {{status?: string, size?: number, phase?: string}} [options]
 * @returns {string}
 */
export function glyphSvg(type, { status, size = 16, phase } = {}) {
  const glyph = glyphFor(type);
  const px = Math.round(Math.min(64, Math.max(8, Number(size) || 16)));
  const style = glyphStyle(
    Object.hasOwn(GLYPHS, type) ? type : 'track',
    statusKey(status),
    {
      phase: PHASES.has(phase) ? phase : undefined,
    },
  );
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
  if (style.slash) {
    body += `<path d="${GLYPH_SLASH}" fill="none" stroke="${COLORS.critical}" stroke-width="2" stroke-linecap="round"/>`;
  }
  if (style.outerRing) {
    body =
      `<circle cx="12" cy="12" r="11" fill="none" stroke="${style.outerRing}" stroke-width="1.5"/>` +
      body;
  }
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
