/**
 * Umpire rows and the loss banner (WG spec §5.3.8; M14a).
 *
 * Umpire rows are the wargame's adjudications, the battle damage sensors
 * saw, and own losses, written into the transcript from the intel graph.
 * There is no "executing" row.
 * - Marker: a Sand square with the meta "Umpire  {Z}  Simulated".
 * - Rows are append-only and deduplicated by `eng:{id}:{phase}:{bda.state}`.
 * - While a slip is pending, rows that arrive fold into "{n} umpire events
 *   since this request." with Show, and the transcript doesn't autoscroll.
 * - Other rows within 10 s of each other fold together.
 * - Text uses designators and generic labels only. In Blue view a red
 *   attacker is always "Red air defence (not identified)" and no chance is
 *   shown; every row keeps a masked text, used whenever Blue view is on.
 *
 * The loss banner ("Simulated loss: {vehicle} destroyed by {attacker} at
 * {Z}.") comes from vehicle `attrs.wargame_state === "lost"`; it is UI-only
 * and not an alarm kind.
 *
 * Pure helpers first, then the DOM builders (text only, bidi-safe).
 */

import { h as domH } from '../../ui/uavDom.js';
import { bidiSafe, capitalize, stripBidi, truncate, zulu } from './format.js';
import { MASKED_ATTACKER } from './validateEngagement.js';

// Designators and generic labels, still rendered as text only.
const h = bidiSafe(domH);

/** Rows closer together than this fold into one group. */
export const UMPIRE_FOLD_MS = 10_000;
/** The log keeps at most this many rows (oldest dropped). */
export const UMPIRE_MAX_ROWS = 200;

export const UMPIRE_COPY = Object.freeze({
  who: 'Umpire',
  simulated: 'Simulated',
  since: (n) => `${n} umpire event${n === 1 ? '' : 's'} since this request.`,
  group: (n, z) => `${n} umpire events${z ? ` from ${z}` : ''}.`,
  show: 'Show',
  hide: 'Hide',
  engaged: (attacker, target, outcome) =>
    `${attacker} engaged ${target}. ${outcome}`,
  hidden: 'Outcome hidden in blue view.',
  unreported: 'Outcome not reported yet.',
  bda: (target, words) => `Battle damage on ${target}: ${words}.`,
  lost: (vehicle, attacker) =>
    `${vehicle} was destroyed by ${attacker}. Simulated loss.`,
  banner: (vehicle, attacker, z) =>
    `Simulated loss: ${vehicle} destroyed by ${attacker}${z ? ` at ${z}` : ''}.`,
});

const OUTCOME_WORD = Object.freeze({
  missed: 'Missed',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});

/** §5.3.9 battle damage words, lower case for mid-sentence use. */
const BDA_WORD = Object.freeze({
  no_change: 'no change seen',
  damaged: 'damage seen',
  destroyed_probable: 'probably destroyed',
});

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
const text = (v, max = 80) =>
  str(v) ? truncate(stripBidi(str(v)), max) : null;
const looks = (n) => `${n} look${n === 1 ? '' : 's'}`;

function nodesOf(graph) {
  return Array.isArray(graph?.nodes) ? graph.nodes.filter(isObj) : [];
}

/** Umpire view: the graph was requested with `truth=1` (§3.2 meta). */
export function isUmpireView(graph) {
  return graph?.meta?.wargame?.truth_view === true;
}

/** The dedupe key of an engagement node (§5.3.8). */
export function umpireKey(node) {
  const a = node?.attrs || {};
  const bda = isObj(a.bda) && str(a.bda.state) ? a.bda.state : 'none';
  return `${node?.id}:${a.phase ?? ''}:${bda}`;
}

/** The chance behind a row: `p_notional` as a number or `{effect}`. */
function chanceOf(a) {
  if (isNum(a.p_notional)) return a.p_notional;
  if (isObj(a.p_notional) && isNum(a.p_notional.effect))
    return a.p_notional.effect;
  return null;
}

const isRedKind = (kind) => kind === 'red_shot' || kind === 'red_ground';

/** The attacker words: masked for red in Blue view, whatever the graph says. */
function attackerWords(a, umpire) {
  if (isRedKind(a.kind) && !umpire) return MASKED_ATTACKER;
  return (
    text(a.attacker_label) ||
    (isRedKind(a.kind) ? MASKED_ATTACKER : 'A blue scenario unit')
  );
}

/** "Missed (≈ 0.18)." / "Outcome hidden in blue view." */
function outcomeWords(a, umpire) {
  if (a.outcome_hidden === true) return UMPIRE_COPY.hidden;
  if (a.outcome == null) return UMPIRE_COPY.unreported;
  const word = Object.hasOwn(OUTCOME_WORD, a.outcome)
    ? OUTCOME_WORD[a.outcome]
    : 'Outcome not recognised';
  const p = chanceOf(a);
  return umpire && p != null ? `${word} (≈ ${p.toFixed(2)}).` : `${word}.`;
}

/**
 * The adjudication row text of an engagement node. `umpire:false` gives the
 * Blue-view text: the red attacker masked and no chance.
 */
export function umpireRowText(node, { umpire = false } = {}) {
  const a = node?.attrs || {};
  const target = text(a.target_label) || 'an unidentified target';
  return UMPIRE_COPY.engaged(
    attackerWords(a, umpire),
    target,
    outcomeWords(a, umpire),
  );
}

/** The battle damage row text (§5.3.9 words), or null for `none`. */
export function bdaRowText(node) {
  const a = node?.attrs || {};
  const bda = isObj(a.bda) ? a.bda : null;
  const state = str(bda?.state);
  if (!state || state === 'none') return null;
  const n = isNum(bda.looks) ? bda.looks : null;
  const target = text(a.target_label) || 'an unidentified target';
  let words;
  if (state === 'destroyed_confirmed')
    words = `destroyed, confirmed by ${n != null ? looks(n) : 'repeated looks'}`;
  else if (Object.hasOwn(BDA_WORD, state))
    words = `${BDA_WORD[state]}${n != null ? ` (${looks(n)})` : ''}`;
  else words = 'battle damage state not recognised';
  return UMPIRE_COPY.bda(target, words);
}

/** The row tone from an engagement's consequence. */
function toneOf(a) {
  if (a.consequence === 'own_loss') return 'critical';
  if (a.consequence === 'own_damage') return 'warn';
  return null;
}

/** Who downed a lost vehicle: masked in Blue view (§3.2 fog). */
export function lossAttacker(graph, vehicleNode, { umpire = false } = {}) {
  if (!umpire) return MASKED_ATTACKER;
  const id = vehicleNode?.id;
  const hits = nodesOf(graph)
    .filter(
      (n) =>
        n.type === 'engagement' &&
        n.attrs?.target === id &&
        n.attrs?.consequence === 'own_loss',
    )
    .sort(
      (x, y) =>
        (y.attrs?.adjudicated_at_ms ?? 0) - (x.attrs?.adjudicated_at_ms ?? 0),
    );
  const fromEngagement = text(hits[0]?.attrs?.attacker_label);
  if (fromEngagement) return fromEngagement;
  const by = str(vehicleNode?.attrs?.wargame_lost_by);
  const byNode = by ? nodesOf(graph).find((n) => n.id === by) : null;
  return text(byNode?.label) || MASKED_ATTACKER;
}

const vehicleName = (n) =>
  text(n?.label) || text(String(n?.id || '').replace(/^veh:/, '')) || 'A drone';

/**
 * Every umpire event in a graph, oldest first. Each is
 * `{key, kind:'adjudicated'|'bda'|'loss', at, text, masked, tone, id}`:
 * `text` in the graph's own view, `masked` always the Blue-view text.
 */
export function umpireEvents(graph) {
  const umpire = isUmpireView(graph);
  const nodes = nodesOf(graph);
  const out = [];
  const lostByShot = new Set();
  for (const n of nodes) {
    if (n.type !== 'engagement') continue;
    const a = n.attrs || {};
    if (a.phase !== 'adjudicated') continue;
    if (a.consequence === 'own_loss' && str(a.target)) lostByShot.add(a.target);
    const tone = toneOf(a);
    const at = isNum(a.adjudicated_at_ms) ? a.adjudicated_at_ms : null;
    out.push({
      key: `${n.id}:adjudicated:none`,
      kind: 'adjudicated',
      id: n.id,
      at,
      text: umpireRowText(n, { umpire }),
      masked: umpireRowText(n, { umpire: false }),
      tone,
    });
    const bda = bdaRowText(n);
    if (bda) {
      out.push({
        key: umpireKey(n),
        kind: 'bda',
        id: n.id,
        at: isNum(a.bda?.last_look_ms) ? a.bda.last_look_ms : at,
        text: bda,
        masked: bda,
        tone: null,
      });
    }
  }
  for (const n of nodes) {
    if (n.type !== 'vehicle' || n.attrs?.wargame_state !== 'lost') continue;
    // A loss already told by a red shot's row isn't told twice.
    if (lostByShot.has(n.id)) continue;
    const at = isNum(n.attrs.wargame_lost_at_ms)
      ? n.attrs.wargame_lost_at_ms
      : null;
    const name = vehicleName(n);
    out.push({
      key: `${n.id}:lost:${at ?? ''}`,
      kind: 'loss',
      id: n.id,
      at,
      text: UMPIRE_COPY.lost(name, lossAttacker(graph, n, { umpire })),
      masked: UMPIRE_COPY.lost(name, MASKED_ATTACKER),
      tone: 'critical',
    });
  }
  return out.sort((x, y) => (x.at ?? Infinity) - (y.at ?? Infinity));
}

/**
 * The append-only umpire log. The first graph it sees is a baseline (what
 * already happened isn't replayed into the transcript); later graphs append
 * only keys it hasn't seen.
 * `observe(graph, {now, anchor, pendingId})` → the rows it appended, each
 * with `anchor` (where in the transcript it belongs) and `pendingId` (the
 * slip that was waiting when it arrived, or null).
 */
export function createUmpireLog({ max = UMPIRE_MAX_ROWS } = {}) {
  const seen = new Set();
  let rows = [];
  let primed = false;
  return {
    observe(graph, { now = null, anchor = 0, pendingId = null } = {}) {
      if (!graph || !Array.isArray(graph.nodes)) return [];
      const events = umpireEvents(graph);
      if (!primed) {
        primed = true;
        for (const e of events) seen.add(e.key);
        return [];
      }
      const added = [];
      for (const e of events) {
        if (seen.has(e.key)) continue;
        seen.add(e.key);
        added.push({ ...e, at: e.at ?? now, anchor, pendingId });
      }
      if (added.length) rows = rows.concat(added).slice(-max);
      return added;
    },
    get rows() {
      return rows;
    },
    get primed() {
      return primed;
    },
  };
}

/**
 * Groups for one transcript position, in order:
 * - `since`: consecutive rows that arrived while the same slip was pending
 *   (and it still is), shown as "{n} umpire events since this request.";
 * - `fold`: two or more rows each within `foldMs` of the one before;
 * - `row`: a single row.
 * @param {Array<object>} rows log rows at one anchor, oldest first
 * @param {{isPending?: (id:string) => boolean, foldMs?: number}} [opts]
 */
export function groupUmpireRows(
  rows,
  { isPending = () => false, foldMs = UMPIRE_FOLD_MS } = {},
) {
  const list = Array.isArray(rows) ? rows : [];
  const out = [];
  let i = 0;
  while (i < list.length) {
    const first = list[i];
    const group = [first];
    i += 1;
    if (first.pendingId && isPending(first.pendingId)) {
      while (i < list.length && list[i].pendingId === first.pendingId) {
        group.push(list[i]);
        i += 1;
      }
      out.push({ kind: 'since', key: `since:${first.key}`, rows: group });
      continue;
    }
    while (
      i < list.length &&
      !(list[i].pendingId && isPending(list[i].pendingId)) &&
      isNum(list[i].at) &&
      isNum(group[group.length - 1].at) &&
      list[i].at - group[group.length - 1].at <= foldMs
    ) {
      group.push(list[i]);
      i += 1;
    }
    out.push(
      group.length > 1
        ? { kind: 'fold', key: `fold:${first.key}`, rows: group }
        : { kind: 'row', key: `row:${first.key}`, rows: group },
    );
  }
  return out;
}

/** The worst tone in a group: a folded loss is still marked critical. */
export function groupTone(rows) {
  const tones = (Array.isArray(rows) ? rows : []).map((r) => r?.tone);
  if (tones.includes('critical')) return 'critical';
  if (tones.includes('warn')) return 'warn';
  return null;
}

/** One loss banner per lost vehicle (§5.3.8), newest last. */
export function lossBanners(graph) {
  const umpire = isUmpireView(graph);
  return nodesOf(graph)
    .filter((n) => n.type === 'vehicle' && n.attrs?.wargame_state === 'lost')
    .map((n) => {
      const at = n.attrs.wargame_lost_at_ms;
      return {
        key: `${n.id}:${isNum(at) ? at : ''}`,
        vehicle: vehicleName(n),
        at: isNum(at) ? at : null,
        text: UMPIRE_COPY.banner(
          vehicleName(n),
          lossAttacker(graph, n, { umpire }),
          zulu(at, { seconds: true }),
        ),
      };
    })
    .sort((x, y) => (x.at ?? 0) - (y.at ?? 0));
}

// ---- DOM (text only) ------------------------------------------------------------

/** The words a row shows in the current view. */
export function rowText(row, { umpire = false } = {}) {
  return umpire ? row.text : row.masked;
}

/** "Umpire  {Z}  Simulated" with the Sand square marker. */
function metaNode(at) {
  const z = zulu(at, { seconds: true });
  return h(
    'p',
    { class: 'ic-umpire__meta' },
    h('span', { class: 'ic-umpire__mark', 'aria-hidden': 'true' }),
    h('span', { class: 'ic-umpire__who' }, UMPIRE_COPY.who),
    z ? h('span', { class: 'ic-vh' }, ', ') : null,
    z ? h('time', { class: 'ic-umpire__time' }, z) : null,
    h('span', { class: 'ic-vh' }, ', '),
    h('span', { class: 'ic-umpire__sim' }, UMPIRE_COPY.simulated),
  );
}

function lineNode(row, umpire) {
  return h(
    'p',
    {
      class: 'ic-umpire__text',
      'data-tone': row.tone || null,
      'data-kind': row.kind,
    },
    rowText(row, { umpire }),
  );
}

/** One umpire row. */
export function umpireRowNode(row, { umpire = false } = {}) {
  return h(
    'div',
    {
      class: 'ic-umpire',
      'data-kind': 'row',
      'data-tone': row.tone || null,
      'data-key': row.key,
    },
    metaNode(row.at),
    lineNode(row, umpire),
  );
}

/**
 * A folded group: a `since` group ("{n} umpire events since this request.")
 * or a `fold` group ("{n} umpire events from {Z}."), with Show/Hide.
 * @param {{kind:string, key:string, rows:Array}} group
 * @param {{umpire?: boolean, open?: boolean, onToggle?: Function}} [opts]
 */
export function umpireGroupNode(
  group,
  { umpire = false, open = false, onToggle = null } = {},
) {
  const rows = group.rows || [];
  const first = rows[0] || {};
  const label =
    group.kind === 'since'
      ? UMPIRE_COPY.since(rows.length)
      : UMPIRE_COPY.group(rows.length, zulu(first.at, { seconds: true }));
  const toggle = h(
    'button',
    {
      type: 'button',
      class: 'ic-btn ic-umpire__toggle',
      'data-variant': 'link',
      'aria-expanded': open ? 'true' : 'false',
    },
    open ? UMPIRE_COPY.hide : UMPIRE_COPY.show,
  );
  if (typeof onToggle === 'function')
    toggle.addEventListener('click', () => onToggle(group.key));
  const tone = groupTone(rows);
  return h(
    'div',
    {
      class: 'ic-umpire',
      'data-kind': group.kind,
      'data-tone': tone,
      'data-key': group.key,
    },
    metaNode(first.at),
    h(
      'p',
      { class: 'ic-umpire__text', 'data-tone': tone },
      capitalize(label),
      ' ',
      toggle,
    ),
    open
      ? h(
          'div',
          { class: 'ic-umpire__rows' },
          ...rows.map((r) => umpireRowNode(r, { umpire })),
        )
      : null,
  );
}
