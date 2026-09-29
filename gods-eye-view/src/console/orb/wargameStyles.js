/**
 * How the orb paints the simulated wargame (WG spec §5.3.1, §5.3.4, §5.3.6):
 * force frames by side and state, engagement bursts by phase and
 * consequence, vector arrows by kind, and the Blue/Umpire view filter.
 *
 * Rules held here (and pinned by wargameStyles.test.mjs):
 * - A frame is drawn only on a `force` node with `provenance: "scenario"`.
 *   Anything else that claims to be a force draws the lilac "?", so no real
 *   object ever wears a frame (D1).
 * - Nothing here is ever own-systems green (`COLORS.ok`): a blue unit that
 *   is fine is Film white, like an assessed-low contact.
 * - Red and blue are never hues: sides read by frame shape and by word.
 * - Every value that reaches a style or a sprite key is looked up in a closed
 *   vocabulary (§3.9), so styles and keys are built from constants only.
 *
 * Pure and imports only glyphPaths.js: glyphs.js imports this module, so it
 * must not import glyphs.js back. `WG_INK` repeats the few palette values it
 * needs; a test holds them equal to `COLORS`.
 */

import {
  ENGAGEMENT_GLYPH,
  FRAME_BARS,
  FRAME_BARS_BROKEN,
  FRAME_OUTLINES,
  FRAME_SLASH,
  UNRECOGNISED_GLYPH,
  VECTOR_GLYPH,
  frameSideKey,
} from './glyphPaths.js';

/** Inks the wargame uses. Sand is the umpire's ink (§5.3.1). */
export const WG_INK = Object.freeze({
  film: '#E6ECEF',
  pencil: '#A3B0BA',
  warn: '#F2B544',
  critical: '#FF7B7B',
  stale: '#8A98A2',
  lilac: '#BBA7E0',
  sand: '#CDBC8C',
  sandSlip: '#5A4812',
  hairline: 'rgba(230,236,239,0.10)',
  film35: 'rgba(230,236,239,0.35)',
  pencil35: 'rgba(163,176,186,0.35)',
});

/** The three wargame node types (§3.2). */
export const WARGAME_TYPES = Object.freeze(['force', 'engagement', 'vector']);
const WARGAME_SET = new Set(WARGAME_TYPES);

/** Whether a node type belongs to the simulated wargame. */
export function isWargameType(type) {
  return typeof type === 'string' && WARGAME_SET.has(type);
}

/** Closed vocabularies (§3.9). */
export const FORCE_SIDES = Object.freeze(['red', 'blue']);
export const FORCE_STATES = Object.freeze([
  'active',
  'suppressed',
  'damaged',
  'destroyed',
]);
export const ENGAGEMENT_KINDS = Object.freeze([
  'blue_strike',
  'red_shot',
  'red_ground',
]);
export const ENGAGEMENT_PHASES = Object.freeze([
  'proposed',
  'authorized',
  'adjudicated',
  'denied',
  'expired',
]);
export const OUTCOMES = Object.freeze([
  'missed',
  'suppressed',
  'damaged',
  'destroyed',
]);
export const CONSEQUENCES = Object.freeze([
  'own_loss',
  'own_damage',
  'red_effect',
  'none',
]);
export const BDA_STATES = Object.freeze([
  'none',
  'no_change',
  'damaged',
  'destroyed_probable',
  'destroyed_confirmed',
]);
export const VECTOR_KINDS = Object.freeze(['axis', 'corridor']);
/** Engagements still waiting on the operator (§5.3.5 "pending"). */
export const PENDING_PHASES = Object.freeze(['proposed', 'authorized']);

const vocabulary = (list) => {
  const set = new Set(list);
  return (value) =>
    typeof value === 'string' && set.has(value) ? value : 'unknown';
};

/** A force side: `red`, `blue` or `unknown`. */
export const sideKey = frameSideKey;
/** A force state from §3.9, else `unknown`. */
export const forceStateKey = vocabulary(FORCE_STATES);
/** An engagement kind from §3.9, else `unknown`. */
export const engagementKindKey = vocabulary(ENGAGEMENT_KINDS);
/** An engagement phase from §3.9, else `unknown`. */
export const engagementPhaseKey = vocabulary(ENGAGEMENT_PHASES);
/** An outcome from §3.9, else `unknown`. */
export const outcomeKey = vocabulary(OUTCOMES);
/** A consequence from §3.9, else `unknown`. */
export const consequenceKey = vocabulary(CONSEQUENCES);
/** A BDA state from §3.9, else `unknown`. */
export const bdaStateKey = vocabulary(BDA_STATES);
/** A vector kind from §3.9, else `unknown`. */
export const vectorKindKey = vocabulary(VECTOR_KINDS);

const STATUS_SET = new Set(['ok', 'warn', 'critical', 'stale', 'unknown']);
const statusOf = (status) => (STATUS_SET.has(status) ? status : 'unknown');

/** A status as ink, never green: `ok` is Film (§5.3.4 "contact status rules"). */
function statusInk(status) {
  switch (statusOf(status)) {
    case 'ok':
      return WG_INK.film;
    case 'warn':
      return WG_INK.warn;
    case 'critical':
      return WG_INK.critical;
    case 'stale':
      return WG_INK.stale;
    default:
      return WG_INK.lilac;
  }
}

/** Whether a node is a force the scenario placed (the only kind with a frame). */
export function isScenarioForce(node) {
  return node?.type === 'force' && node?.attrs?.provenance === 'scenario';
}

/** Whether an engagement node is still waiting (proposed or authorized). */
export function isPendingEngagement(node) {
  return (
    node?.type === 'engagement' &&
    PENDING_PHASES.includes(engagementPhaseKey(node?.attrs?.phase))
  );
}

/**
 * The whitelisted wargame fields a glyph depends on, as closed-vocabulary
 * constants. Nothing else from the node reaches a style or a sprite key.
 * @param {string} type node type
 * @param {object} [attrs] node attrs
 */
export function wargameGlyphKeys(type, attrs = {}) {
  const a = attrs && typeof attrs === 'object' ? attrs : {};
  if (type === 'force') {
    return {
      scenario: a.provenance === 'scenario',
      side: sideKey(a.side),
      state: forceStateKey(a.state),
    };
  }
  if (type === 'engagement') {
    return {
      phase: engagementPhaseKey(a.phase),
      consequence: consequenceKey(a.consequence),
    };
  }
  if (type === 'vector') {
    return { kind: vectorKindKey(a.kind), proposed: a.proposed === true };
  }
  return {};
}

/** A blank style: the shape every wargame style fills in. */
function baseStyle(path, color) {
  return {
    path,
    bar: null,
    filled: false,
    fill: null,
    stroke: color,
    dash: null,
    barDash: null,
    fillAlpha: 1,
    alpha: 1,
    slash: false,
    slashPath: FRAME_SLASH,
    outerRing: null,
    halo: false,
    color,
    frame: null,
  };
}

/** Dash patterns: a dashed frame means "suppressed"; lilac dashes mean "not recognised". */
const DASH_FRAME = Object.freeze([4, 3]);
const DASH_UNKNOWN = Object.freeze([3, 2.5]);
const DASH_PENDING = Object.freeze([3, 2]);

/**
 * A force frame (§5.3.4). Blue is a rectangle, red a diamond, an unknown
 * side a lilac dashed quatrefoil. State: suppressed dashes the frame,
 * damaged breaks the bar, destroyed is stale with a slash (blue also at
 * 50 %). Blue inks by state (Film, warn, stale); red by the contact status
 * rules (Film if ok, warn, critical with a steady halo). A known side with
 * an unknown state keeps its frame in lilac. A force whose provenance is not
 * `scenario` gets no frame at all: the lilac "?".
 * @param {object} [attrs] the force node's attrs
 * @param {string} [status] the force node's status
 */
export function forceStyle(attrs = {}, status) {
  const keys = wargameGlyphKeys('force', attrs);
  if (!keys.scenario) {
    const style = baseStyle(UNRECOGNISED_GLYPH, WG_INK.lilac);
    return style;
  }
  const side = keys.side;
  const state = keys.state;
  const style = baseStyle(FRAME_OUTLINES[side], WG_INK.film);
  style.frame = side;
  style.bar = FRAME_BARS[side];
  if (side === 'unknown') {
    style.stroke = WG_INK.lilac;
    style.dash = DASH_UNKNOWN;
    style.color = WG_INK.lilac;
    return style;
  }
  if (state === 'unknown') {
    style.stroke = WG_INK.lilac;
    style.color = WG_INK.lilac;
    return style;
  }
  let ink;
  if (state === 'destroyed') ink = WG_INK.stale;
  else if (side === 'blue')
    ink = state === 'active' ? WG_INK.film : WG_INK.warn;
  else ink = statusInk(status);
  style.stroke = ink;
  style.color = ink;
  if (side === 'red' && statusOf(status) === 'unknown' && state !== 'destroyed')
    style.dash = DASH_UNKNOWN;
  if (state === 'suppressed') style.dash = DASH_FRAME;
  if (state === 'damaged') style.bar = FRAME_BARS_BROKEN[side];
  if (state === 'destroyed') {
    style.slash = true;
    if (side === 'blue') style.alpha = 0.5;
  }
  style.halo =
    side === 'red' && state !== 'destroyed' && statusOf(status) === 'critical';
  return style;
}

/**
 * An engagement burst (§5.3.6): Sand dashed outline while proposed or
 * authorized; filled once adjudicated (own loss critical, own damage warn,
 * anything else Pencil); Pencil dashed at 50 % when denied or expired; lilac
 * dashed for a phase or consequence the console does not know.
 * @param {object} [attrs] the engagement node's attrs
 */
export function engagementStyle(attrs = {}) {
  const { phase, consequence } = wargameGlyphKeys('engagement', attrs);
  const style = baseStyle(ENGAGEMENT_GLYPH, WG_INK.sand);
  if (phase === 'proposed' || phase === 'authorized') {
    style.dash = DASH_PENDING;
    return style;
  }
  if (phase === 'denied' || phase === 'expired') {
    style.stroke = WG_INK.pencil;
    style.color = WG_INK.pencil;
    style.dash = DASH_PENDING;
    style.alpha = 0.5;
    return style;
  }
  if (phase === 'adjudicated' && consequence !== 'unknown') {
    const ink =
      consequence === 'own_loss'
        ? WG_INK.critical
        : consequence === 'own_damage'
          ? WG_INK.warn
          : WG_INK.pencil;
    style.filled = true;
    style.fill = ink;
    style.stroke = null;
    style.color = ink;
    style.halo = consequence === 'own_loss';
    return style;
  }
  style.stroke = WG_INK.lilac;
  style.color = WG_INK.lilac;
  style.dash = DASH_UNKNOWN;
  return style;
}

/**
 * A vector arrow (§5.3.6, drawn smaller than other glyphs): a red axis inks
 * by its unit's status (warn or critical, else Pencil); a corridor is
 * Pencil, dashed while only proposed; an unknown kind is lilac dashed.
 * @param {object} [attrs] the vector node's attrs
 * @param {string} [status] the vector node's status
 */
export function vectorStyle(attrs = {}, status) {
  const { kind, proposed } = wargameGlyphKeys('vector', attrs);
  const style = baseStyle(VECTOR_GLYPH, WG_INK.pencil);
  if (kind === 'unknown') {
    style.stroke = WG_INK.lilac;
    style.color = WG_INK.lilac;
    style.dash = DASH_UNKNOWN;
    return style;
  }
  const s = statusOf(status);
  if (kind === 'axis' && (s === 'warn' || s === 'critical')) {
    style.stroke = statusInk(s);
    style.color = style.stroke;
  }
  if (kind === 'corridor' && proposed) style.dash = DASH_PENDING;
  return style;
}

/**
 * The style for any wargame node type, or null for other types.
 * @param {string} type node type
 * @param {object} [attrs] node attrs
 * @param {string} [status] node status
 */
export function wargameGlyphStyle(type, attrs, status) {
  let style;
  if (type === 'force') style = forceStyle(attrs, status);
  else if (type === 'engagement') style = engagementStyle(attrs);
  else if (type === 'vector') style = vectorStyle(attrs, status);
  else return null;
  // A stale node (the picture is not live, or a red unit is destroyed)
  // reads stale grey like every other glyph; lilac "not recognised" stays.
  if (statusOf(status) === 'stale' && style.color !== WG_INK.lilac) {
    if (style.fill) style.fill = WG_INK.stale;
    if (style.stroke) style.stroke = WG_INK.stale;
    style.color = WG_INK.stale;
    style.halo = false;
  }
  return style;
}

/**
 * A sprite-cache key for a wargame glyph, from closed-vocabulary constants
 * only (so equal keys always paint the same pixels).
 */
export function wargameStyleKey(type, attrs, status) {
  const k = wargameGlyphKeys(type, attrs);
  const s = statusOf(status);
  if (type === 'force')
    return `force:${k.scenario ? 'scn' : 'no'}:${k.side}:${k.state}:${s}`;
  if (type === 'engagement') return `eng:${k.phase}:${k.consequence}`;
  if (type === 'vector') return `vec:${k.kind}:${k.proposed ? 'p' : ''}:${s}`;
  return '';
}

/** Edge kinds the orb draws only in Umpire view (§5.3.5). */
const UMPIRE_EDGE_KINDS = new Set(['axis', 'correlates']);

/**
 * Whether Blue view hides a node (§5.3.3, §5.3.4): every force that is not
 * provably blue (red truth, or a side the console does not know) and every
 * red axis. A presentation filter, not a secrecy boundary: the server
 * already leaves red truth out of a blue-view graph.
 */
export function hiddenInBlueView(node) {
  if (node?.type === 'force') return sideKey(node?.attrs?.side) !== 'blue';
  if (node?.type === 'vector') {
    const a = node?.attrs || {};
    return a.side === 'red' || vectorKindKey(a.kind) === 'axis';
  }
  return false;
}

/**
 * The graph a view shows. Umpire view shows everything; Blue view drops the
 * nodes `hiddenInBlueView` names, every edge that touched them, and the
 * Umpire-only edge kinds. Returns the same object when nothing is dropped.
 * @param {{nodes?: object[], edges?: object[]}|null} graph
 * @param {{umpire?: boolean}} [view]
 */
export function filterForView(graph, { umpire = true } = {}) {
  if (umpire || !graph || typeof graph !== 'object') return graph;
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const hidden = new Set();
  for (const node of nodes)
    if (node && hiddenInBlueView(node)) hidden.add(node.id);
  const keptEdges = edges.filter(
    (edge) =>
      !hidden.has(edge?.a) &&
      !hidden.has(edge?.b) &&
      !UMPIRE_EDGE_KINDS.has(edge?.kind),
  );
  if (!hidden.size && keptEdges.length === edges.length) return graph;
  return {
    ...graph,
    nodes: nodes.filter((node) => !(node && hidden.has(node.id))),
    edges: keptEdges,
  };
}
