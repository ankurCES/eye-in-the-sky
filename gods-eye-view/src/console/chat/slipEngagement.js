/**
 * The engagement slip body (WG spec §5.3.7; M14a). slip.js owns the frame:
 * the Sand band and hatch, the title, the acknowledgement box, the 1600 ms
 * arm, the buttons and the Deny-only decision. This module builds the info
 * nodes from the server's `engagement` preview, and the words the frame
 * needs (title, fixed line, acknowledgement, filed outcome).
 *
 * Labels come from the wargame's generic tables and designators, never from
 * real places; they still reach the DOM only as text through the bidi-safe
 * factory, and every value cell is wrapped in its own span so a label that
 * reads "Measured" is never drawn as a register tag.
 */

import { h as domH } from '../../ui/uavDom.js';
import { bidiSafe, stripBidi, truncate, zulu } from './format.js';
import {
  engagementPreviewOf,
  staleLine,
  targetLabel,
} from './validateEngagement.js';

// Generic labels and server strings: no bidi control reaches the slip.
const h = bidiSafe(domH);

/** Approve arms this long after the acknowledgement is ticked (§5.3.1). */
export const ENGAGE_ARM_MS = 1600;

/** The Scenario register's tooltip (§5.3.1). */
export const SCENARIO_TIP = 'Set by the wargame, not seen by a sensor.';

export const ENGAGEMENT_SLIP_COPY = Object.freeze({
  fixed: (target) =>
    target
      ? `Simulated. Nothing real is fired. ${target} is a scenario unit, not a real place.`
      : 'Simulated. Nothing real is fired.',
  cantUndoHead: "What can't be undone",
  cantUndo:
    'The outcome stands for the rest of this wargame. Only ending the wargame clears it.',
  ack: (target) =>
    `I understand this rolls a simulated outcome against ${target} that can't be undone.`,
  freshPlan: 'Ask for a fresh plan',
  freshPlanNote:
    'Conditions changed since this engagement was proposed. Propose it again and show me the new slip.',
  readyIn: 'Ready in 2 s',
  outcomeHidden: 'Outcome hidden in blue view.',
  outcomeAt: (z, word) => `Outcome${z ? ` at ${z}` : ''}: ${word} (simulated).`,
  noEffects: 'The server sent no description of the effects.',
});

/** Outcome words (§3.9 vocabulary); anything else is "not recognised". */
const OUTCOME_WORDS = Object.freeze({
  missed: 'missed',
  suppressed: 'suppressed',
  damaged: 'damaged',
  destroyed: 'destroyed',
});

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
/** Server label text, bidi-free and short. */
const label = (v, max = 80) =>
  str(v) ? truncate(stripBidi(str(v)), max) : null;

/** "strike" for a simulated air strike, otherwise "engagement". */
export function verbKindOf(preview) {
  return preview?.verb_kind === 'strike' ? 'strike' : 'engagement';
}

/**
 * The slip title: "Simulated {verb_kind} on {target}" from the preview, or
 * the server's title when the preview is missing.
 */
export function engagementTitle(approval) {
  const p = engagementPreviewOf(approval);
  if (!p || !str(p.target?.label))
    return str(approval?.title) || 'Execute a simulated engagement';
  return `Simulated ${verbKindOf(p)} on ${targetLabel(p)}`;
}

/** The acknowledgement sentence for an engagement slip. */
export function engagementAck(approval) {
  return ENGAGEMENT_SLIP_COPY.ack(targetLabel(engagementPreviewOf(approval)));
}

/** A lower-case outcome word, or "outcome not recognised". */
export function outcomeWord(outcome) {
  return (
    (typeof outcome === 'string' &&
      Object.hasOwn(OUTCOME_WORDS, outcome) &&
      OUTCOME_WORDS[outcome]) ||
    'outcome not recognised'
  );
}

/**
 * The filed slip's live outcome line, read from the graph's `eng:` node
 * (§5.3.7): "Outcome at {Z}: {outcome} (simulated).", or "Outcome hidden in
 * blue view." before battle damage assessment. Null before adjudication.
 * @returns {{text:string, hidden:boolean, tone:'unknown'|null}|null}
 */
export function engagementOutcome(approval, graph) {
  const p = engagementPreviewOf(approval);
  const id = str(p?.id) ? `eng:${p.id}` : null;
  if (!id || !Array.isArray(graph?.nodes)) return null;
  const node = graph.nodes.find((n) => n && n.id === id);
  const a = node?.attrs;
  if (!isObj(a) || a.phase !== 'adjudicated') return null;
  if (a.outcome_hidden === true)
    return {
      text: ENGAGEMENT_SLIP_COPY.outcomeHidden,
      hidden: true,
      tone: null,
    };
  if (a.outcome == null) return null;
  const known = Object.hasOwn(OUTCOME_WORDS, a.outcome);
  return {
    text: ENGAGEMENT_SLIP_COPY.outcomeAt(
      zulu(a.adjudicated_at_ms, { seconds: true }),
      outcomeWord(a.outcome),
    ),
    hidden: false,
    tone: known ? null : 'unknown',
  };
}

/** The Sand "Scenario" register tag with its tooltip. */
export function scenarioTag() {
  return h(
    'span',
    { class: 'ic-tag', 'data-register': 'scenario', title: SCENARIO_TIP },
    'Scenario',
  );
}

// ---- number words ------------------------------------------------------------

/** "≈ 0.62" (two decimals), or '' for a non-number. */
export function approxP(p) {
  return isNum(p) ? `≈ ${p.toFixed(2)}` : '';
}

function metres(m) {
  if (!isNum(m)) return '';
  const a = Math.abs(m);
  return a >= 1000 ? `${(a / 1000).toFixed(1)} km` : `${Math.round(a)} m`;
}

/** "≈ 42 s exposed; survival ≈ 0.91" */
export function exposureText(vector) {
  if (!isObj(vector)) return '';
  const parts = [];
  if (isNum(vector.exposure_s))
    parts.push(`≈ ${Math.round(vector.exposure_s)} s exposed`);
  if (isNum(vector.p_survive))
    parts.push(`survival ${approxP(vector.p_survive)}`);
  return parts.join('; ');
}

/** "≈ 118 s less exposure, 1.4 km longer" */
export function straightText(vector) {
  if (!isObj(vector)) return '';
  const parts = [];
  const ds = vector.delta_exposure_s;
  if (isNum(ds) && Math.round(ds) !== 0)
    parts.push(
      `≈ ${Math.round(Math.abs(ds))} s ${ds > 0 ? 'less' : 'more'} exposure`,
    );
  const dm = vector.delta_length_m;
  if (isNum(dm) && Math.round(dm) !== 0)
    parts.push(`${metres(dm)} ${dm > 0 ? 'longer' : 'shorter'}`);
  return parts.join(', ');
}

/** "Seen as Air-defence guns, probable, 2 sightings, 14:01:02Z" */
export function seenText(target) {
  if (!isObj(target)) return '';
  const parts = [`Seen as ${label(target.label) || 'an unnamed contact'}`];
  const conf = label(target.confidence, 40);
  if (conf) parts.push(conf);
  if (isNum(target.sightings))
    parts.push(
      `${target.sightings} sighting${target.sightings === 1 ? '' : 's'}`,
    );
  const z = zulu(target.last_seen_ms, { seconds: true });
  if (z) parts.push(z);
  return parts.join(', ');
}

// ---- the body -----------------------------------------------------------------

/** Text inside its own span: never read back as a register word. */
const cell = (text, cls = null) => h('span', cls ? { class: cls } : {}, text);

/** Spaced segments with hidden commas (the console never writes a dot). */
function spaced(parts) {
  const kids = [];
  parts.filter(Boolean).forEach((part, i) => {
    if (i) kids.push(h('span', { class: 'ic-vh' }, ', '));
    kids.push(
      typeof part === 'string' ? h('span', { class: 'ic-seg' }, part) : part,
    );
  });
  return kids;
}

/** "{attacker}   {target} {track id}   corridor   Requested" */
function summaryNodes(p, tag) {
  const attacker = label(p.attacker?.label);
  const target = label(p.target?.label);
  const trackId = label(p.target?.track_id, 60);
  const targetSeg = target
    ? h(
        'span',
        { class: 'ic-seg' },
        target,
        trackId ? ' ' : null,
        trackId ? h('span', { class: 'ic-mono' }, trackId) : null,
      )
    : null;
  const kids = spaced([
    attacker,
    targetSeg,
    isObj(p.vector) ? 'corridor' : null,
  ]);
  if (kids.length) kids.push(h('span', { class: 'ic-vh' }, ', '));
  return h('p', { class: 'ic-slip__summary' }, ...kids, tag('Requested'));
}

/** The preview's checks as ✓/✕ lines (any ✕ makes the slip Deny-only). */
function ruleList(checks) {
  const list = Array.isArray(checks) ? checks : [];
  if (!list.length) return cell('No rules were sent.', 'ic-slip__muted');
  return h(
    'ul',
    { class: 'ic-slip__checks' },
    ...list.map((c) => {
      const ok = isObj(c) && c.ok === true;
      return h(
        'li',
        { 'data-ok': ok ? 'true' : 'false' },
        h(
          'span',
          { class: 'ic-slip__mark', 'aria-hidden': 'true' },
          ok ? '✓' : '✕',
        ),
        h('span', { class: 'ic-vh' }, ok ? 'Passed: ' : 'Failed: '),
        label(c?.text, 200) || 'Unnamed check',
      );
    }),
  );
}

function row(head, value, register) {
  return h(
    'tr',
    {},
    h('th', { scope: 'row' }, head),
    h('td', {}, typeof value === 'string' ? cell(value) : value),
    h('td', {}, register || ''),
  );
}

/** The "Checks" table (§5.3.7): chance, inputs, exposure, rules, seen, replay. */
function checksTable(p, tag) {
  const rows = [];
  const effect = p.p_notional?.effect;
  if (isNum(effect))
    rows.push(
      row('Chance of effect', `${approxP(effect)}, notional`, tag('Estimated')),
    );
  const inputs = Array.isArray(p.inputs)
    ? p.inputs.map((t) => label(t, 200)).filter(Boolean)
    : [];
  if (inputs.length)
    rows.push(
      row(
        'Inputs',
        h(
          'ul',
          { class: 'ic-slip__list' },
          ...inputs.map((t) => h('li', {}, t)),
        ),
        tag('Estimated'),
      ),
    );
  if (isObj(p.vector)) {
    const exposure = exposureText(p.vector);
    if (exposure) rows.push(row('Exposure', exposure, tag('Estimated')));
    const straight = straightText(p.vector);
    if (straight)
      rows.push(row('Against a straight route', straight, tag('Estimated')));
  }
  rows.push(row('Rules', ruleList(p.checks), scenarioTag()));
  const seen = seenText(p.target);
  if (seen) rows.push(row('Target seen', seen, tag('Measured')));
  const replay = [];
  if (isNum(p.seed)) replay.push(`Seed ${p.seed}`);
  if (label(p.engine, 40)) replay.push(`Engine ${label(p.engine, 40)}`);
  if (replay.length)
    rows.push(
      row('Replay', h('span', { class: 'ic-segs' }, ...spaced(replay)), ''),
    );
  return h('table', { class: 'ic-slip__table' }, h('tbody', {}, ...rows));
}

function caveatNodes(p, tag) {
  const list = Array.isArray(p.caveats)
    ? p.caveats.map((t) => label(t, 300)).filter(Boolean)
    : [];
  return list.map((c) =>
    h(
      'p',
      { class: 'ic-slip__caveat' },
      h('span', { class: 'ic-assumed' }, c),
      ' ',
      tag('Assumed'),
    ),
  );
}

/**
 * The info nodes of an engagement slip (everything between the title and
 * the acknowledgement box). With no usable preview only the server's
 * summary and consequences are shown; the slip is Deny-only then.
 * @param {{approval: object, assessment: object|null, tag: Function}} ctx
 */
export function engagementInfoNodes({ approval, assessment, tag }) {
  const a = approval || {};
  const p = engagementPreviewOf(a);
  const nodes = [];
  if (p) nodes.push(summaryNodes(p, tag));
  nodes.push(
    h(
      'p',
      { class: 'ic-slip__fixed' },
      // Without a preview the console can't say what the target is.
      ENGAGEMENT_SLIP_COPY.fixed(p ? targetLabel(p) : null),
    ),
  );
  const consequences = Array.isArray(a.consequences) ? a.consequences : [];
  nodes.push(h('h4', { class: 'ic-slip__head' }, 'What happens'));
  nodes.push(
    consequences.length
      ? h(
          'ul',
          { class: 'ic-slip__list' },
          ...consequences.map((c) => h('li', {}, String(c))),
        )
      : h('p', { class: 'ic-slip__muted' }, ENGAGEMENT_SLIP_COPY.noEffects),
  );
  if (p) {
    nodes.push(h('h4', { class: 'ic-slip__head' }, 'Checks'));
    nodes.push(checksTable(p, tag));
    nodes.push(...caveatNodes(p, tag));
  }
  if (assessment?.state === 'stale') {
    const line =
      assessment.line || staleLine(assessment.stale || assessment.reasons);
    if (line)
      nodes.push(h('p', { class: 'ic-slip__stale', role: 'note' }, line));
  }
  nodes.push(
    h('h4', { class: 'ic-slip__head' }, ENGAGEMENT_SLIP_COPY.cantUndoHead),
  );
  nodes.push(
    h('p', { class: 'ic-slip__console' }, ENGAGEMENT_SLIP_COPY.cantUndo),
  );
  return nodes;
}
