/**
 * Wargame edges on the orb (WG spec §5.3.5, the session profile).
 *
 * | Edge          | Style                                              | Drawn                          |
 * |---------------|----------------------------------------------------|--------------------------------|
 * | `attacks`     | Sand dashed [6,3] while proposed or authorized;    | pending always (cap 10), else  |
 * |               | then 1 px in the consequence colour                | hover or select                |
 * | `launched_by` | hairline dotted                                    | hover or select                |
 * | `along`       | Pencil 35 %                                        | hover or select                |
 * | `axis`        | Pencil 1.5 px, arrowhead at the target; warn when  | Umpire view (cap 20)           |
 * |               | its unit is warn or critical                       |                                |
 * | `ingress`     | Film 35 % dashed                                   | hover or select                |
 * | `threatens`   | dotted warn                                        | neighbours only                |
 * | `correlates`  | dotted Film 35 %                                   | neighbours only, Umpire view   |
 *
 * No wargame edge is ever magenta: magenta belongs to own missions. Every
 * style is a frozen constant, so a frame allocates nothing per edge.
 * Pure; imports only wargameStyles.js.
 */

import {
  WG_INK,
  consequenceKey,
  engagementPhaseKey,
  isPendingEngagement,
} from './wargameStyles.js';

export const WARGAME_EDGE_KINDS = Object.freeze([
  'attacks',
  'launched_by',
  'along',
  'axis',
  'ingress',
  'threatens',
  'correlates',
]);
const WARGAME_EDGE_SET = new Set(WARGAME_EDGE_KINDS);

/** At most this many pending `attacks` edges are always drawn. */
export const PENDING_ATTACKS_CAP = 10;
/** At most this many `axis` edges are always drawn (Umpire view). */
export const AXIS_CAP = 20;

/** Whether an edge kind belongs to the simulated wargame. */
export function isWargameEdge(kind) {
  return typeof kind === 'string' && WARGAME_EDGE_SET.has(kind);
}

const DOTTED = Object.freeze([1, 3]);
const style = (color, width, dash, reveal, extra = {}) =>
  Object.freeze({
    color,
    width,
    dash,
    reveal,
    cap: null,
    arrow: false,
    ...extra,
  });

/** Every wargame edge style (frozen constants). */
export const WARGAME_EDGE_STYLES = Object.freeze({
  attacksPending: style(WG_INK.sand, 1, Object.freeze([6, 3]), 'always', {
    cap: 'pending',
  }),
  attacksOwnLoss: style(WG_INK.critical, 1, null, 'neighbour'),
  attacksOwnDamage: style(WG_INK.warn, 1, null, 'neighbour'),
  attacksDone: style(WG_INK.pencil, 1, null, 'neighbour'),
  launchedBy: style(WG_INK.hairline, 1, DOTTED, 'neighbour'),
  along: style(WG_INK.pencil35, 1, null, 'neighbour'),
  axis: style(WG_INK.pencil, 1.5, null, 'always', {
    cap: 'axis',
    arrow: true,
  }),
  axisWarn: style(WG_INK.warn, 1.5, null, 'always', {
    cap: 'axis',
    arrow: true,
  }),
  ingress: style(WG_INK.film35, 1, Object.freeze([4, 3]), 'neighbour'),
  threatens: style(WG_INK.warn, 1, DOTTED, 'neighbour'),
  correlates: style(WG_INK.film35, 1, DOTTED, 'neighbour'),
  never: style(WG_INK.pencil, 1, null, 'never'),
});

const S = WARGAME_EDGE_STYLES;

/**
 * The style of one wargame edge, or null for any other kind.
 * @param {string} kind edge kind
 * @param {{a?: object, b?: object, umpire?: boolean}} [ends] the edge's end
 *   nodes (graph nodes: `a` is the engagement of `attacks`, the red unit of
 *   `axis`) and the view
 */
export function wargameEdgeStyle(kind, { a, umpire = true } = {}) {
  switch (kind) {
    case 'attacks': {
      if (isPendingEngagement(a)) return S.attacksPending;
      const phase = engagementPhaseKey(a?.attrs?.phase);
      const consequence =
        phase === 'adjudicated' ? consequenceKey(a?.attrs?.consequence) : '';
      if (consequence === 'own_loss') return S.attacksOwnLoss;
      if (consequence === 'own_damage') return S.attacksOwnDamage;
      return S.attacksDone;
    }
    case 'launched_by':
      return S.launchedBy;
    case 'along':
      return S.along;
    case 'axis':
      if (!umpire) return S.never;
      return a?.status === 'warn' || a?.status === 'critical'
        ? S.axisWarn
        : S.axis;
    case 'ingress':
      return S.ingress;
    case 'threatens':
      return S.threatens;
    case 'correlates':
      return umpire ? S.correlates : S.never;
    default:
      return null;
  }
}

const proposedAt = (node) => {
  const ms = Number(node?.attrs?.proposed_at_ms);
  return Number.isFinite(ms) ? ms : -Infinity;
};

const plans = new WeakMap();

/**
 * Styles for every edge of a layout (null where the kind is not a wargame
 * one), with the caps applied: the newest 10 pending `attacks` edges and 20
 * `axis` edges (warn ones first) stay always-on; the rest are demoted to
 * hover or select. Cached per layout and view.
 * @param {{edges: object[], nodes: object[]}} layout computeLayout() result
 * @param {{umpire?: boolean}} [view]
 * @returns {Array<object|null>}
 */
export function wargameEdgePlan(layout, { umpire = true } = {}) {
  const cached = plans.get(layout);
  if (cached && cached.umpire === umpire) return cached.plan;
  const edges = Array.isArray(layout?.edges) ? layout.edges : [];
  const nodes = Array.isArray(layout?.nodes) ? layout.nodes : [];
  const plan = new Array(edges.length).fill(null);
  const capped = { pending: [], axis: [] };
  edges.forEach((edge, e) => {
    if (!isWargameEdge(edge?.kind)) return;
    const a = nodes[edge.ai];
    const s = wargameEdgeStyle(edge.kind, { a, b: nodes[edge.bi], umpire });
    plan[e] = s;
    if (s.cap) capped[s.cap].push({ e, a, warn: s === S.axisWarn });
  });
  capped.pending.sort((x, y) => proposedAt(y.a) - proposedAt(x.a) || x.e - y.e);
  capped.axis.sort((x, y) => Number(y.warn) - Number(x.warn) || x.e - y.e);
  const demote = (list, limit) => {
    for (const { e } of list.slice(limit))
      plan[e] = Object.freeze({ ...plan[e], reveal: 'neighbour' });
  };
  demote(capped.pending, PENDING_ATTACKS_CAP);
  demote(capped.axis, AXIS_CAP);
  plans.set(layout, { umpire, plan });
  return plan;
}
