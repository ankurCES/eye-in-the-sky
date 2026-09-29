/**
 * The simulated wargame in the console's panels (WG §5.3, M14a):
 * - the words every panel uses for scenario forces, simulated engagements
 *   and vectors (sides, states, phases, outcomes, battle damage);
 * - their glyphs (the §5.3.4 frames, the engagement burst, the vector
 *   arrow), built from constants only (Appendix A);
 * - `wargameOf(graph)`, the normalised `meta.wargame` block;
 * - the rail's Wargame section (§5.3.10), between Missions and Alarms;
 * - the composer's session prompts (§5.3.8), for chat/view.js.
 *
 * Everything here is a simulation: the words say "simulated" and name no
 * real system, place or munition. Real places (sites, POIs) are context and
 * never appear as a target. Labels are untrusted text: they are
 * bidi-stripped and reach the DOM only as text through uavDom `h()`.
 *
 * This module imports nothing from situation.js (which imports it): the
 * rail hands its DOM kit in (`button`, `icon`, `glyph`, `zulu`, …).
 */
import { h } from '../ui/uavDom.js';
import { DEFAULT_WARGAME_VIEW, WARGAME_VIEWS } from './intelStore.js';
import { COLORS } from './orb/glyphs.js';
import { safeText } from './orb/placeText.js';

// ---------------------------------------------------------------------------
// Vocabulary (§3.9 closed vocabularies, Appendix B copy)
// ---------------------------------------------------------------------------

/** The Phase B node types (§3.2). */
export const WARGAME_TYPES = Object.freeze(['force', 'engagement', 'vector']);
const WARGAME_SET = new Set(WARGAME_TYPES);

/** Whether a node type is one of the wargame's own (force, engagement, vector). */
export function isWargameType(type) {
  return typeof type === 'string' && WARGAME_SET.has(type);
}

/** The umpire's ink (§5.3.1). Red and blue are never hues in the console. */
export const SAND = '#CDBC8C';

export const SIMULATED_WORD = 'Simulated';
export const SCENARIO_WORD = 'Scenario';
export const SCENARIO_TAG_TITLE = 'Set by the wargame, not seen by a sensor.';
export const SIDE_WORD = Object.freeze({ red: 'Red', blue: 'Blue' });
export const SIDE_UNSET_TEXT = 'Side not set';
export const FORCE_STATE_WORD = Object.freeze({
  active: 'Active',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});
export const STATE_UNKNOWN_TEXT = 'State not recognised';
export const ENGAGEMENT_PHASE_WORD = Object.freeze({
  proposed: 'Waiting for you',
  authorized: 'Authorized',
  adjudicated: 'Adjudicated',
  denied: 'Denied',
  expired: 'Expired',
});
export const PHASE_UNKNOWN_TEXT = 'Phase not recognised';
export const OUTCOME_WORD = Object.freeze({
  missed: 'Missed',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});
export const OUTCOME_UNKNOWN_TEXT = 'Outcome not recognised';
export const CONSEQUENCE_WORD = Object.freeze({
  own_loss: 'Own loss',
  own_damage: 'Own damage',
  red_effect: 'Effect on red',
  none: 'None',
});
/** Engagement kind -> the noun in "Simulated {noun} on {target}" (§5.3.6). */
export const ENGAGEMENT_KIND_WORD = Object.freeze({
  blue_strike: 'strike',
  red_shot: 'shot',
  red_ground: 'ground fire',
});
/** §5.3.9 battle damage words (the confirmed one names its looks). */
export const BDA_WORD = Object.freeze({
  none: 'Not assessed yet',
  no_change: 'No change seen',
  damaged: 'Damage seen',
  destroyed_probable: 'Probably destroyed',
  destroyed_confirmed: 'Destroyed, confirmed',
});
export const HIDDEN_ATTACKER_TEXT = 'Red air defence (not identified)';
export { WARGAME_VIEWS };
export const DEFAULT_VIEW = DEFAULT_WARGAME_VIEW;
export const VIEW_WORD = Object.freeze({
  umpire: 'Umpire view',
  blue: 'Blue view',
});
export const VIEW_SENTENCE = Object.freeze({
  umpire:
    'Umpire view: red units are where the scenario put them, not where a sensor saw them.',
  blue: 'Blue view: red units appear only as contacts your sensors reported.',
});
export const OUTCOMES_NOTIONAL_TEXT =
  'Outcomes are simulated adjudications; probabilities are notional.';
export const FORCE_FIXED_LINE =
  'Simulated scenario unit. Placed by the wargame, not observed.';
export const ENGAGEMENT_FIXED_LINE = 'Simulated. Nothing real was fired.';
export const VECTOR_FIXED_LINE = 'Simulated. Computed by the wargame.';
export const OUTCOME_HIDDEN_TEXT =
  'Hidden in blue view. Look again to assess damage.';
export const SITE_CONTEXT_ONLY_TEXT =
  "Context only. Real places can't be engaged in the wargame.";
export const AAR_TITLE = 'After-action review (simulated)';
export const STRIKE_ACTION_TEXT = 'Plan a simulated strike';
export const WARGAME_TITLE = 'Wargame';

const num = (value) => {
  if (value == null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const str = (value) => (typeof value === 'string' && value ? value : null);

// ---------------------------------------------------------------------------
// Pure reads
// ---------------------------------------------------------------------------

/**
 * The graph's `meta.wargame` block, normalised (§3.2 meta additions), or
 * null when the host doesn't report one (ISR mode before Phase B).
 * - inactive: `{active:false, last}`;
 * - active: `{active:true, session_id, started_at_ms, seed, time_scale,
 *   red_engages, reveal_red, truth_view, pending:[eng ids], counts:{blue,
 *   red}, caveats, last}`.
 */
export function wargameOf(graph) {
  const w = graph?.meta?.wargame;
  if (!w || typeof w !== 'object') return null;
  const l = w.last && typeof w.last === 'object' ? w.last : null;
  const last = l
    ? {
        session_id: str(l.session_id),
        aar_id: str(l.aar_id),
        ended_at_ms: num(l.ended_at_ms),
      }
    : null;
  if (w.active !== true) return { active: false, last };
  const counts = w.counts && typeof w.counts === 'object' ? w.counts : {};
  const side = (v) => (v && typeof v === 'object' ? v : {});
  return {
    active: true,
    session_id: str(w.session_id),
    started_at_ms: num(w.started_at_ms),
    seed: num(w.seed),
    time_scale: num(w.time_scale),
    red_engages: w.red_engages === true,
    reveal_red: w.reveal_red === true,
    truth_view: w.truth_view === true,
    pending: (Array.isArray(w.pending) ? w.pending : []).filter(
      (id) => typeof id === 'string' && id,
    ),
    counts: { blue: side(counts.blue), red: side(counts.red) },
    caveats: (Array.isArray(w.caveats) ? w.caveats : [])
      .map((c) => safeText(c, 240))
      .filter(Boolean),
    last,
  };
}

/** Whether a simulated wargame session is running in this graph. */
export function wargameActive(graph) {
  return wargameOf(graph)?.active === true;
}

/**
 * Whether this graph shows the umpire's truth: the host says so
 * (`meta.wargame.truth_view`), or the console asked for it (`truth`).
 */
export function isTruthView(graph, truth = false) {
  const w = wargameOf(graph);
  return Boolean(w?.active && (w.truth_view || truth === true));
}

/** A force's side, `'red'|'blue'`, or null when it isn't set (§3.9). */
export function sideOf(node) {
  const s = node?.attrs?.side;
  return s === 'red' || s === 'blue' ? s : null;
}

/** A force's state when it is one of the four (§3.9), else null. */
export function forceStateOf(node) {
  const s = node?.attrs?.state;
  return typeof s === 'string' && Object.hasOwn(FORCE_STATE_WORD, s) ? s : null;
}

/** "Red" / "Blue" / "Side not set". */
export function sideWord(side) {
  return Object.hasOwn(SIDE_WORD, side ?? '')
    ? SIDE_WORD[side]
    : SIDE_UNSET_TEXT;
}

/** "Active" … "Destroyed", or "State not recognised". */
export function forceStateWord(state) {
  return Object.hasOwn(FORCE_STATE_WORD, state ?? '')
    ? FORCE_STATE_WORD[state]
    : STATE_UNKNOWN_TEXT;
}

/** "Waiting for you" … "Expired", or "Phase not recognised". */
export function engagementPhaseWord(phase) {
  return Object.hasOwn(ENGAGEMENT_PHASE_WORD, phase ?? '')
    ? ENGAGEMENT_PHASE_WORD[phase]
    : PHASE_UNKNOWN_TEXT;
}

/** "Missed" … "Destroyed", or "Outcome not recognised". */
export function outcomeWord(outcome) {
  return Object.hasOwn(OUTCOME_WORD, outcome ?? '')
    ? OUTCOME_WORD[outcome]
    : OUTCOME_UNKNOWN_TEXT;
}

/**
 * The colour tone of a wargame node, for the panels' CSS (`data-tone`).
 * Never `ok` (green): an active force is film white (`low`), pending
 * engagements are Sand, and anything the console doesn't recognise is
 * lilac `unknown`.
 */
export function wargameTone(node) {
  const type = node?.type;
  const a = node?.attrs || {};
  if (type === 'force') {
    const side = sideOf(node);
    const state = forceStateOf(node);
    if (!side || !state) return 'unknown';
    if (state === 'destroyed') return 'stale';
    if (side === 'blue') return state === 'active' ? 'low' : 'warn';
    return (
      { ok: 'low', warn: 'warn', critical: 'critical', stale: 'stale' }[
        node?.status
      ] || 'unknown'
    );
  }
  if (type === 'engagement') {
    const phase = a.phase;
    if (phase === 'proposed' || phase === 'authorized') return 'sand';
    if (phase === 'adjudicated') {
      if (a.consequence === 'own_loss') return 'critical';
      if (a.consequence === 'own_damage') return 'warn';
      return 'neutral';
    }
    if (phase === 'denied' || phase === 'expired') return 'stale';
    return 'unknown';
  }
  if (type === 'vector') {
    return (
      { ok: 'neutral', warn: 'warn', critical: 'critical', stale: 'stale' }[
        node?.status
      ] || 'unknown'
    );
  }
  return 'unknown';
}

/** The status word a wargame node shows beside its colour. */
export function wargameStatusWord(node) {
  const type = node?.type;
  if (type === 'force') return forceStateWord(node?.attrs?.state);
  if (type === 'engagement') return engagementPhaseWord(node?.attrs?.phase);
  if (type === 'vector')
    return (
      { warn: 'Warning', critical: 'Critical', stale: 'Stale' }[node?.status] ||
      ''
    );
  return '';
}

// ---------------------------------------------------------------------------
// Glyphs (Appendix A; §5.3.4 frames, §5.3.6 paint). Constants only.
// ---------------------------------------------------------------------------

const FRAMES = Object.freeze({
  blue: Object.freeze({
    frame: 'M2.5 6.5H21.5V17.5H2.5Z',
    bar: 'M7 12H17',
    broken: 'M7 12H10.5M13.5 12H17',
  }),
  red: Object.freeze({
    frame: 'M12 2L22 12L12 22L2 12Z',
    bar: 'M7 12H17',
    broken: 'M7 12H10.5M13.5 12H17',
  }),
  unknown: Object.freeze({
    frame: 'M8 8A4 4 0 0 1 16 8A4 4 0 0 1 16 16A4 4 0 0 1 8 16A4 4 0 0 1 8 8Z',
    bar: 'M9 12H15',
    broken: 'M9 12H11M13 12H15',
  }),
});
export const ENGAGEMENT_GLYPH =
  'M12 2L14.2 9.8L22 12L14.2 14.2L12 22L9.8 14.2L2 12L9.8 9.8Z';
export const VECTOR_GLYPH = 'M3 12H17M11 6L17 12L11 18';
const SLASH = 'M3 21.5L21 2.5';
const INK = Object.freeze({
  film: COLORS.film,
  pencil: COLORS.pencil,
  warn: COLORS.warn,
  critical: COLORS.critical,
  stale: COLORS.stale,
  unknown: COLORS.unknown,
  sand: SAND,
});

function stroke(ink, dash) {
  const d = dash ? ` stroke-dasharray="${dash}"` : '';
  return `fill="none" stroke="${ink}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"${d}`;
}

function forceBody(side, state, status) {
  const f = FRAMES[side || 'unknown'];
  if (!side)
    return `<path d="${f.frame}" ${stroke(INK.unknown, '3 2.5')}/><path d="${f.bar}" ${stroke(INK.unknown)}/>`;
  if (!state)
    return `<path d="${f.frame}" ${stroke(INK.unknown)}/><path d="${f.bar}" ${stroke(INK.unknown)}/>`;
  if (state === 'destroyed')
    return (
      `<g opacity="0.5"><path d="${f.frame}" ${stroke(INK.stale)}/><path d="${f.bar}" ${stroke(INK.stale)}/></g>` +
      `<path d="${SLASH}" ${stroke(INK.stale)}/>`
    );
  let ink = INK.film;
  let halo = '';
  if (side === 'blue') {
    if (state !== 'active') ink = INK.warn;
  } else {
    ink =
      {
        ok: INK.film,
        warn: INK.warn,
        critical: INK.critical,
        stale: INK.stale,
      }[status] || INK.unknown;
    if (status === 'critical')
      halo = `<circle cx="12" cy="12" r="11" fill="none" stroke="${INK.critical}" stroke-width="1.5"/>`;
  }
  const frameDash = state === 'suppressed' ? '3 2' : null;
  const bar = state === 'damaged' ? f.broken : f.bar;
  return `${halo}<path d="${f.frame}" ${stroke(ink, frameDash)}/><path d="${bar}" ${stroke(ink)}/>`;
}

function engagementBody(phase, consequence) {
  const d = ENGAGEMENT_GLYPH;
  if (phase === 'proposed' || phase === 'authorized')
    return `<path d="${d}" ${stroke(INK.sand, '3 2')}/>`;
  if (phase === 'adjudicated') {
    const ink =
      consequence === 'own_loss'
        ? INK.critical
        : consequence === 'own_damage'
          ? INK.warn
          : INK.pencil;
    return `<path d="${d}" fill="${ink}" stroke="${ink}" stroke-width="1" stroke-linejoin="round"/>`;
  }
  if (phase === 'denied' || phase === 'expired')
    return `<path d="${d}" ${stroke(INK.pencil, '3 2')} opacity="0.5"/>`;
  return `<path d="${d}" ${stroke(INK.unknown, '3 2.5')}/>`;
}

function vectorBody(status) {
  const ink =
    { warn: INK.warn, critical: INK.critical, stale: INK.stale }[status] ||
    (status === 'ok' ? INK.pencil : INK.unknown);
  return `<path d="${VECTOR_GLYPH}" ${stroke(ink)}/>`;
}

/**
 * Inline SVG for a wargame node (force frame, engagement burst, vector
 * arrow). Every input is looked up or clamped, so the markup comes from
 * constants alone and is safe for innerHTML. A force is never green; a
 * frame appears only on a `force` (§5.3.4). Empty for other types.
 * @param {string} type 'force'|'engagement'|'vector'
 * @param {{side?:string, state?:string, status?:string, phase?:string,
 *   consequence?:string, size?:number}} [options]
 */
export function wargameGlyphSvg(type, options = {}) {
  if (!isWargameType(type)) return '';
  const px = Math.round(Math.min(64, Math.max(8, Number(options.size) || 16)));
  const side =
    options.side === 'red' || options.side === 'blue' ? options.side : null;
  const state = Object.hasOwn(FORCE_STATE_WORD, options.state ?? '')
    ? options.state
    : null;
  const status = ['ok', 'warn', 'critical', 'stale'].includes(options.status)
    ? options.status
    : 'unknown';
  const phase = Object.hasOwn(ENGAGEMENT_PHASE_WORD, options.phase ?? '')
    ? options.phase
    : null;
  const consequence = Object.hasOwn(CONSEQUENCE_WORD, options.consequence ?? '')
    ? options.consequence
    : null;
  const body =
    type === 'force'
      ? forceBody(side, state, status)
      : type === 'engagement'
        ? engagementBody(phase, consequence)
        : vectorBody(status);
  return (
    `<svg class="ic-glyph" xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" ` +
    `viewBox="0 0 24 24" aria-hidden="true" focusable="false">${body}</svg>`
  );
}

// ---------------------------------------------------------------------------
// Engagements, battle damage, counts (pure)
// ---------------------------------------------------------------------------

/** The notional chance of effect: a number, or a `{effect}` object (§5.2.7). */
export function pNotional(value) {
  const p = num(value && typeof value === 'object' ? value.effect : value);
  return p == null ? null : Math.min(1, Math.max(0, p));
}

/** "≈ 0.62", or '' when unknown. Always an estimate, never a measurement. */
export function pText(value) {
  const p = pNotional(value);
  return p == null ? '' : `≈ ${p.toFixed(2)}`;
}

/** "strike" / "shot" / "ground fire" (a kind the console doesn't know: "engagement"). */
export function engagementNoun(kind) {
  return Object.hasOwn(ENGAGEMENT_KIND_WORD, kind ?? '')
    ? ENGAGEMENT_KIND_WORD[kind]
    : 'engagement';
}

/** The attacker as the fog allows: its designator, or "Red air defence (not identified)". */
export function attackerText(node) {
  const a = node?.attrs || {};
  const label = safeText(a.attacker_label, 80);
  if (label) return label;
  return a.kind === 'red_shot' || a.kind === 'red_ground'
    ? HIDDEN_ATTACKER_TEXT
    : 'Attacker not reported';
}

/** The target's generic label (never a real place), or "the target". */
export function targetText(node) {
  return safeText(node?.attrs?.target_label, 80) || 'the target';
}

/** "Simulated strike on Air-defence guns" (§5.3.6 title, §5.3.10 rows). */
export function engagementTitle(node) {
  return `Simulated ${engagementNoun(node?.attrs?.kind)} on ${targetText(node)}`;
}

/**
 * §5.3.9 battle damage: `{state, word, looks, lastMs, tone}`. The confirmed
 * state names its looks ("Destroyed, confirmed by 2 looks"); `none` is the
 * italic lilac "Not assessed yet"; an unknown state reads "not recognised".
 */
export function bdaFacts(bda) {
  const b = bda && typeof bda === 'object' ? bda : {};
  const known = Object.hasOwn(BDA_WORD, b.state ?? '');
  const state = known ? b.state : null;
  const looks = num(b.looks) ?? 0;
  let word = known ? BDA_WORD[state] : 'Battle damage not recognised';
  if (state === 'destroyed_confirmed') word = `${word} by ${looks} looks`;
  const tone =
    !state || state === 'none'
      ? 'unknown'
      : state === 'damaged'
        ? 'warn'
        : 'neutral';
  return { state, word, looks, lastMs: num(b.last_look_ms), tone };
}

/** Whether the sensors' battle damage agrees with the umpire's outcome. */
export function bdaAgrees(outcome, state) {
  if (!Object.hasOwn(OUTCOME_WORD, outcome ?? '')) return true;
  if (outcome === 'destroyed')
    return state === 'destroyed_probable' || state === 'destroyed_confirmed';
  if (outcome === 'damaged') return state === 'damaged';
  return state === 'no_change';
}

/** Graph nodes of one type. */
function nodesOf(graph, type) {
  return (Array.isArray(graph?.nodes) ? graph.nodes : []).filter(
    (n) => n && n.type === type,
  );
}

/**
 * Engagements waiting for the operator (§5.3.10): the ids in
 * `meta.wargame.pending`, else every proposed or authorized engagement.
 */
export function pendingEngagements(graph) {
  const w = wargameOf(graph);
  const all = nodesOf(graph, 'engagement');
  if (w?.pending?.length) {
    const byId = new Map(all.map((n) => [n.id, n]));
    return w.pending.map((id) => byId.get(id)).filter(Boolean);
  }
  return all.filter((n) => ['proposed', 'authorized'].includes(n.attrs?.phase));
}

/** The newest adjudicated engagements, newest first. */
export function recentEngagements(graph, limit = 3) {
  const at = (n) => num(n.attrs?.adjudicated_at_ms) ?? num(n.ts_ms) ?? 0;
  return nodesOf(graph, 'engagement')
    .filter((n) => n.attrs?.phase === 'adjudicated')
    .sort((a, b) => at(b) - at(a))
    .slice(0, limit);
}

/** A recent row's outcome: its word, or "Outcome hidden" before battle damage. */
export function recentOutcomeText(node) {
  const a = node?.attrs || {};
  if (a.outcome_hidden === true || a.outcome == null) return 'Outcome hidden';
  return outcomeWord(a.outcome);
}

const COUNT_WORDS = [
  ['active', 'active'],
  ['suppressed', 'suppressed'],
  ['damaged', 'damaged'],
  ['destroyed', 'destroyed'],
];

/**
 * One side line of `meta.wargame.counts` as segments (§5.3.10), zero
 * segments omitted: `['Blue', '3 units', '3 active']`. Red counts without
 * `units` are Blue view: `['Red', '2 seen']`.
 */
export function sideSegments(side, counts) {
  const c = counts && typeof counts === 'object' ? counts : {};
  const out = [side === 'red' ? 'Red' : 'Blue'];
  const units = num(c.units);
  if (side === 'red' && units == null) {
    out.push(`${num(c.seen) ?? 0} seen`);
    return out;
  }
  out.push(`${units ?? 0} ${units === 1 ? 'unit' : 'units'}`);
  for (const [key, word] of COUNT_WORDS) {
    const n = num(c[key]);
    if (n) out.push(`${n} ${word}`);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Prompts (§5.3.4 strike prefill, §5.3.8 session prompts)
// ---------------------------------------------------------------------------

/** A label safe inside `[[id|label]]` chip markup. */
function chipLabel(text) {
  return safeText(text, 80)
    .replace(/[[\]|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * The generic label a scenario contact is cited by (R20): the graph's
 * label for a `scenario` track (the host writes `label_for_ob`), else
 * "Contact". Never a real system or place name.
 */
export function genericTrackLabel(trackNode) {
  const a = trackNode?.attrs || {};
  const label = a.scenario === true ? chipLabel(trackNode?.label) : '';
  return label || 'Contact';
}

/**
 * The strike prefill for a red force (§5.3.4): only in a running session,
 * only for a red force with `correlated[]` contacts (Umpire view), citing
 * the first contact by its generic label. Null otherwise. Never sent.
 */
export function strikePrefill(forceNode, graph) {
  if (!wargameActive(graph)) return null;
  if (forceNode?.type !== 'force' || sideOf(forceNode) !== 'red') return null;
  const ids = (
    Array.isArray(forceNode.attrs?.correlated) ? forceNode.attrs.correlated : []
  ).filter((id) => typeof id === 'string' && id);
  if (!ids.length) return null;
  const byId = new Map(nodesOf(graph, 'track').map((n) => [n.id, n]));
  const id = ids.find((x) => byId.has(x)) || ids[0];
  const ref = String(id).startsWith('trk:') ? id : `trk:${id}`;
  const label = genericTrackLabel(byId.get(id) || byId.get(ref));
  return `Plan a simulated strike on contact [[${ref}|${label}]] with the least exposure and show me the dry run.`;
}

/**
 * The composer's suggested prompts during a session (§5.3.8), in place of
 * the ISR ones; `[]` outside a session. The strike prompt appears only
 * when a scenario contact exists, cited by its generic label.
 */
export function wargameSessionPrompts(graph) {
  if (!wargameActive(graph)) return [];
  const out = [
    'Generate a medium air-defence scenario here.',
    'Recce the far half of the area and scan for contacts.',
  ];
  const track = nodesOf(graph, 'track').find((n) => n.attrs?.scenario === true);
  if (track) {
    const ref = String(track.id).startsWith('trk:')
      ? track.id
      : `trk:${track.id}`;
    out.push(
      `Plan a simulated strike on [[${ref}|${genericTrackLabel(track)}]] and show me the dry run.`,
    );
  }
  out.push(
    'Plan a low-exposure re-look of the last strike.',
    'End the wargame and show the after-action review.',
  );
  return out.slice(0, 5);
}

/** The Sand "Simulated" tag. */
export function simulatedTag() {
  return h(
    'span',
    { class: 'ic-kit-reg', 'data-register': 'simulated' },
    SIMULATED_WORD,
  );
}

// ---------------------------------------------------------------------------
// Rail section (§5.3.10)
// ---------------------------------------------------------------------------

/** "What's assumed" lines during a session: the view sentence, then notional outcomes. */
export function wargameAssumedLines(graph, view) {
  if (!wargameActive(graph)) return [];
  const v = WARGAME_VIEWS.includes(view)
    ? view
    : isTruthView(graph)
      ? 'umpire'
      : 'blue';
  return [VIEW_SENTENCE[v], OUTCOMES_NOTIONAL_TEXT];
}

/** "1 engagement waiting for you", "2 engagements waiting for you". */
export function pendingText(n) {
  return `${n} ${n === 1 ? 'engagement' : 'engagements'} waiting for you`;
}

/** Spaced segments with visually hidden ", " between them (a list to a screen reader). */
function parts(list, cls = '') {
  const kids = [];
  list.filter(Boolean).forEach((part, i) => {
    if (i) kids.push(h('span', { class: 'ic-kit-vh' }, ', '));
    kids.push(h('span', { class: 'ic-kit-part' }, part));
  });
  return h('span', { class: `ic-kit-parts ${cls}`.trim() }, ...kids);
}

const line = (text, attrs = {}) =>
  h('p', { class: 'ic-rail__line', ...attrs }, text);

function viewControl(store, view) {
  const mk = (value) => {
    const b = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail__scope-btn',
        'aria-pressed': view === value ? 'true' : 'false',
        'data-key': `wargame:view:${value}`,
      },
      VIEW_WORD[value],
    );
    b.addEventListener('click', () => store.setView(value));
    return b;
  };
  return h(
    'div',
    {
      class: 'ic-rail__scope ic-rail__wg-view',
      role: 'group',
      'aria-label': 'Wargame view',
    },
    mk('blue'),
    mk('umpire'),
  );
}

/**
 * The rail's Wargame section, or null when no session runs and none has
 * ended (ISR mode draws nothing new). `env` is the rail's kit and wiring:
 * `{kit:{button, icon, glyph, zulu, ICON}, emit, store, orb, layout,
 * open, onToggle}`. Compact shows the side lines and the pending count;
 * narrow collapses (a waiting engagement stays visible).
 */
export function wargameSection(st, env = {}) {
  const graph = st?.graph;
  const w = wargameOf(graph);
  if (!w || (!w.active && !w.last?.aar_id)) return null;
  const { kit = {}, emit = () => {}, store = null, layout = 'wide' } = env;
  const inspect = (id) => id && emit('inspect', { id });
  const collapsible = layout === 'narrow';
  const open = !collapsible || env.open === true;
  const head = h(
    'div',
    { class: 'ic-rail__head' },
    h('h3', { class: 'ic-rail__title' }, WARGAME_TITLE),
    simulatedTag(),
  );
  if (collapsible) {
    const toggle = h(
      'button',
      {
        type: 'button',
        class: 'ic-kit-btn ic-kit-btn--quiet ic-rail__wg-toggle',
        'aria-expanded': open ? 'true' : 'false',
        'aria-label': open ? 'Collapse wargame' : 'Expand wargame',
        'data-key': 'wargame:toggle',
      },
      kit.icon ? kit.icon(kit.ICON?.expand || 'expand_more') : null,
    );
    toggle.addEventListener('click', () => env.onToggle?.());
    head.append(toggle);
  }
  const kids = [head];
  const pending = w.active ? pendingEngagements(graph) : [];
  if (!w.active) {
    kids.push(line('No wargame running.'));
  } else if (layout === 'compact') {
    for (const side of ['blue', 'red'])
      kids.push(
        line(parts(sideSegments(side, w.counts[side])), {
          class: 'ic-rail__line ic-rail__wg-side',
          'data-side': side,
        }),
      );
    kids.push(
      line(pending.length ? pendingText(pending.length) : 'Nothing waiting', {
        class: 'ic-rail__line ic-rail__wg-pending',
        'data-status': pending.length ? 'sand' : undefined,
      }),
    );
  } else {
    if (open && typeof store?.setView === 'function')
      kids.push(viewControl(store, st.view || DEFAULT_VIEW));
    if (open)
      for (const side of ['blue', 'red'])
        kids.push(
          line(parts(sideSegments(side, w.counts[side])), {
            class: 'ic-rail__line ic-rail__wg-side',
            'data-side': side,
          }),
        );
    if (pending.length) kids.push(pendingBlock(pending, kit, emit, inspect));
    if (open) kids.push(...recentBlock(graph, kit, emit, inspect, env.orb));
  }
  if (open && w.last?.aar_id && layout !== 'compact') {
    const chip = h(
      'button',
      {
        type: 'button',
        class: 'ic-kit-chip ic-rail__wg-aar',
        'data-key': 'wargame:aar',
        'aria-label': `Inspect the ${AAR_TITLE.toLowerCase()}`,
      },
      kit.glyph ? kit.glyph('report', 'ok', 10) : null,
      h('span', { class: 'ic-kit-chip__label' }, AAR_TITLE),
    );
    chip.addEventListener('click', () => inspect(`rpt:${w.last.aar_id}`));
    kids.push(h('p', { class: 'ic-rail__line' }, chip));
  }
  return h(
    'section',
    {
      class: 'ic-rail__section ic-rail__wargame',
      'data-section': 'wargame',
      'data-active': w.active ? 'true' : 'false',
      'data-open': open ? 'true' : 'false',
    },
    ...kids,
  );
}

function engagementGlyph(kit, node, size = 12) {
  if (!kit.glyph) return null;
  const a = node?.attrs || {};
  return kit.glyph('engagement', node?.status, size, a.phase, {
    consequence: a.consequence,
  });
}

/** "Waiting for you": each pending engagement with Review (scrolls to its slip). */
function pendingBlock(pending, kit, emit, inspect) {
  const rows = pending.map((node) => {
    const name = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail__name ic-rail__wg-name',
        'data-key': `wargame:pending:${node.id}`,
        'aria-label': `Inspect ${engagementTitle(node)}`,
      },
      engagementGlyph(kit, node),
      h('span', {}, engagementTitle(node)),
    );
    name.addEventListener('click', () => inspect(node.id));
    const review = kit.button
      ? kit.button('Review', {
          key: `wargame:review:${node.id}`,
          label: `Review ${engagementTitle(node)}`,
          onClick: () =>
            emit('approval:review', {
              id: node.id,
              approval_id: node.attrs?.approval_id ?? null,
            }),
        })
      : null;
    return h(
      'div',
      { class: 'ic-rail__wg-row', 'data-phase': node.attrs?.phase || '' },
      name,
      review,
    );
  });
  return h(
    'div',
    { class: 'ic-rail__wg-block', 'data-block': 'pending' },
    h('h4', { class: 'ic-rail__wg-subtitle' }, 'Waiting for you'),
    ...rows,
  );
}

/** "Recent": the last three adjudicated, then Show all (the List view, Engagements). */
function recentBlock(graph, kit, emit, inspect, orb) {
  const recent = recentEngagements(graph, 3);
  if (!recent.length) return [];
  const rows = recent.map((node) => {
    const a = node.attrs || {};
    const at = num(a.adjudicated_at_ms) ?? num(node.ts_ms);
    const row = h(
      'button',
      {
        type: 'button',
        class: 'ic-rail__wg-recent',
        'data-key': `wargame:eng:${node.id}`,
        'data-tone': wargameTone(node),
        'aria-label': `Inspect ${engagementTitle(node)}`,
      },
      engagementGlyph(kit, node),
      parts([
        `${attackerText(node)} engaged ${targetText(node)}`,
        at != null && kit.zulu ? kit.zulu(at) : '',
        recentOutcomeText(node),
        pText(a.p_notional),
      ]),
    );
    row.addEventListener('click', () => inspect(node.id));
    return row;
  });
  const ids = nodesOf(graph, 'engagement').map((n) => n.id);
  const all = kit.button
    ? kit.button('Show all', {
        cls: 'ic-kit-btn--link',
        key: 'wargame:all',
        label: `Show all ${ids.length} engagements`,
        onClick: () => {
          const set = new Set(ids);
          orb?.filter?.((n) => set.has(typeof n === 'string' ? n : n?.id));
          emit('search:filter', {
            ids,
            query: 'Engagements',
            source: 'situation',
          });
          emit('view:request', { view: 'list', source: 'situation' });
        },
      })
    : null;
  return [
    h(
      'div',
      { class: 'ic-rail__wg-block', 'data-block': 'recent' },
      h('h4', { class: 'ic-rail__wg-subtitle' }, 'Recent'),
      ...rows,
      all,
    ),
  ];
}

/**
 * The compact strip's wargame count (§5.3.10 compact variant): the pending
 * number and a sentence for the strip's label, or null outside a session.
 */
export function wargameStripFacts(graph) {
  const w = wargameOf(graph);
  if (!w?.active) return null;
  const pending = pendingEngagements(graph).length;
  const sides = ['blue', 'red']
    .map((s) => sideSegments(s, w.counts[s]).join(' '))
    .join(', ');
  return {
    pending,
    summary: `Simulated wargame: ${sides}, ${pendingText(pending)}`,
  };
}
