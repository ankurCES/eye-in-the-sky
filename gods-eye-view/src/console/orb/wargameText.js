/**
 * Words for the simulated wargame on the orb (WG spec §5.3.1, §5.3.4–§5.3.6,
 * Appendix B): force, engagement and vector labels and subtitles, state and
 * phase words, band captions and the listbox twin's accessible names.
 *
 * Every string a node carries (designators, `kind_label`, `target_label`,
 * `attacker_label`) is untrusted server text: it passes through `safeText`,
 * which strips bidi and control characters, and callers render the result
 * only as DOM text or canvas text (§3.11). Outputs never contain a place
 * name the node does not carry. Sentence case; "Simulated" is never
 * capitalised as a stamp; the console never writes "·" (subtitles are
 * " · "-joined only so `splitSegments()` can split them for display).
 */

import { safeText } from './placeText.js';
import {
  consequenceKey,
  engagementKindKey,
  engagementPhaseKey,
  forceStateKey,
  isScenarioForce,
  outcomeKey,
  sideKey,
  vectorKindKey,
} from './wargameStyles.js';

export const WARGAME_TYPE_WORDS = Object.freeze({
  force: 'Force',
  engagement: 'Engagement',
  vector: 'Vector',
});

export const SIMULATED_WORD = 'Simulated';
export const SCENARIO_REGISTER = 'Scenario';
/** The Scenario register's tooltip (§5.3.1). */
export const SCENARIO_TOOLTIP = 'Set by the wargame, not seen by a sensor.';
/** The force inspector's fixed line (§5.3.4). */
export const FORCE_FIXED_LINE =
  'Simulated scenario unit. Placed by the wargame, not observed.';
/** A red attacker in Blue view (§3.2 fog). */
export const RED_NOT_IDENTIFIED = 'Red air defence (not identified)';

export const SIDE_WORDS = Object.freeze({ red: 'Red', blue: 'Blue' });
export const SIDE_NOT_SET = 'Side not set';
export const STATE_WORDS = Object.freeze({
  active: 'Active',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});
export const STATE_NOT_RECOGNISED = 'State not recognised';
/** A `force` node without scenario provenance: no frame, no state read. */
export const NOT_SCENARIO_WORD = 'Not a scenario unit';

export const PHASE_WORDS = Object.freeze({
  proposed: 'Waiting for you',
  authorized: 'Authorized',
  adjudicated: 'Adjudicated',
  denied: 'Denied',
  expired: 'Expired',
});
export const PHASE_NOT_RECOGNISED = 'Phase not recognised';
export const OUTCOME_WORDS = Object.freeze({
  missed: 'Missed',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});
export const OUTCOME_HIDDEN = 'Outcome hidden';
export const OUTCOME_NOT_RECOGNISED = 'Outcome not recognised';
export const CONSEQUENCE_WORDS = Object.freeze({
  own_loss: 'Own loss',
  own_damage: 'Own damage',
  red_effect: 'Effect on red',
  none: 'No effect on own side',
});
/** "Simulated {strike|shot|ground fire} on {target}" (§5.3.6). */
export const ENGAGEMENT_KIND_WORDS = Object.freeze({
  blue_strike: 'strike',
  red_shot: 'shot',
  red_ground: 'ground fire',
});
export const VECTOR_KIND_WORDS = Object.freeze({
  axis: 'Red axis',
  corridor: 'Planned corridor',
});
export const VECTOR_NOT_RECOGNISED = 'Vector kind not recognised';

const attrsOf = (node) =>
  node?.attrs && typeof node.attrs === 'object' ? node.attrs : {};

/** A force's side word: "Red", "Blue" or "Side not set". */
export function forceSideWord(node) {
  const side = sideKey(attrsOf(node).side);
  return side === 'unknown' ? SIDE_NOT_SET : SIDE_WORDS[side];
}

/**
 * A force's state word (its status word everywhere): "Active", …,
 * "State not recognised", "Side not set" for a force of no known side, and
 * "Not a scenario unit" for a force that is not the scenario's.
 */
export function forceStateWord(node) {
  if (!isScenarioForce(node)) return NOT_SCENARIO_WORD;
  const attrs = attrsOf(node);
  if (sideKey(attrs.side) === 'unknown') return SIDE_NOT_SET;
  const state = forceStateKey(attrs.state);
  return state === 'unknown' ? STATE_NOT_RECOGNISED : STATE_WORDS[state];
}

/** A force's label: its designator ("Red SAM 1"), as text. */
export function forceLabel(node) {
  return safeText(node?.label, 80) || safeText(node?.id, 80) || 'Force';
}

/**
 * A force's subtitle segments: "{Blue|Red}  {kind_label}  {state word}
 * Scenario" (§5.3.4). The kind label is the wargame table's generic label.
 */
export function forceSubtitle(node) {
  const kind = safeText(attrsOf(node).kind_label, 48);
  const parts = [forceSideWord(node), kind, forceStateWord(node)];
  if (isScenarioForce(node)) parts.push(SCENARIO_REGISTER);
  return parts.filter(Boolean).join(' · ');
}

/** An engagement's phase word: "Waiting for you", "Adjudicated", … */
export function engagementPhaseWord(node) {
  const phase = engagementPhaseKey(attrsOf(node).phase);
  return phase === 'unknown' ? PHASE_NOT_RECOGNISED : PHASE_WORDS[phase];
}

/**
 * An engagement's outcome as text: "Destroyed (simulated)", "Outcome
 * hidden" (blue view, before battle damage is assessed), or null while there
 * is none yet.
 */
export function engagementOutcomeText(node) {
  const attrs = attrsOf(node);
  if (attrs.outcome_hidden === true) return OUTCOME_HIDDEN;
  if (attrs.outcome == null) return null;
  const outcome = outcomeKey(attrs.outcome);
  return outcome === 'unknown'
    ? OUTCOME_NOT_RECOGNISED
    : `${OUTCOME_WORDS[outcome]} (simulated)`;
}

/**
 * An engagement's label: "Simulated strike on Towed anti-aircraft gun"
 * (§5.3.6), from the kind and the generic target label; the server label
 * when either is missing.
 */
export function engagementLabel(node) {
  const attrs = attrsOf(node);
  const kind = engagementKindKey(attrs.kind);
  const target = safeText(attrs.target_label, 60);
  if (kind !== 'unknown' && target)
    return `${SIMULATED_WORD} ${ENGAGEMENT_KIND_WORDS[kind]} on ${target}`;
  return safeText(node?.label, 80) || `${SIMULATED_WORD} engagement`;
}

/** An engagement's subtitle segments: phase, outcome, "Simulated". */
export function engagementSubtitle(node) {
  return [
    engagementPhaseWord(node),
    engagementOutcomeText(node),
    SIMULATED_WORD,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** A consequence word, or null when the engagement has none. */
export function consequenceWord(node) {
  const value = attrsOf(node).consequence;
  if (value == null) return null;
  const key = consequenceKey(value);
  return key === 'unknown' ? null : CONSEQUENCE_WORDS[key];
}

/** A vector's kind word: "Red axis", "Planned corridor". */
export function vectorKindWord(node) {
  const kind = vectorKindKey(attrsOf(node).kind);
  return kind === 'unknown' ? VECTOR_NOT_RECOGNISED : VECTOR_KIND_WORDS[kind];
}

/** A vector's label: the server label as text, else its kind word. */
export function vectorLabel(node) {
  return safeText(node?.label, 80) || vectorKindWord(node);
}

/**
 * A vector's subtitle segments: kind, length, "Simulated". The kind is left
 * out when it is already the label (a vector the server did not name).
 */
export function vectorSubtitle(node) {
  const km = Number(attrsOf(node).length_m) / 1000;
  const length = Number.isFinite(km) && km > 0 ? `${km.toFixed(1)} km` : '';
  const kind = vectorKindWord(node);
  return [vectorLabel(node) === kind ? '' : kind, length, SIMULATED_WORD]
    .filter(Boolean)
    .join(' · ');
}

/** The orb label for any wargame node (always text, bidi-safe). */
export function wargameNodeLabel(node) {
  if (node?.type === 'force') return forceLabel(node);
  if (node?.type === 'engagement') return engagementLabel(node);
  if (node?.type === 'vector') return vectorLabel(node);
  return safeText(node?.label || node?.id, 80);
}

/** The orb subtitle (" · "-joined segments) for any wargame node. */
export function wargameNodeSubtitle(node) {
  if (node?.type === 'force') return forceSubtitle(node);
  if (node?.type === 'engagement') return engagementSubtitle(node);
  if (node?.type === 'vector') return vectorSubtitle(node);
  return '';
}

/** One status word per wargame node, always paired with its colour. */
export function wargameStatusWord(node) {
  if (node?.type === 'force') return forceStateWord(node);
  if (node?.type === 'engagement') return engagementPhaseWord(node);
  if (node?.type === 'vector') {
    return vectorKindKey(attrsOf(node).kind) === 'unknown'
      ? VECTOR_NOT_RECOGNISED
      : SIMULATED_WORD;
  }
  return 'Not assessed';
}

/** The register a wargame node reads in: set by the wargame (§5.3.1). */
export function wargameRegister() {
  return SCENARIO_REGISTER;
}

/**
 * The List view colour key for a wargame node's status word (the word
 * carries the meaning): lilac `unknown` for anything not recognised, `sand`
 * for an engagement waiting on the operator, `stale` for destroyed, denied
 * or expired, and the warn/critical rules of §5.3.4 and §5.3.6. Never a key
 * the CSS paints green.
 */
export function wargameDisplayStatus(node) {
  const attrs = attrsOf(node);
  const status = node?.status;
  if (node?.type === 'force') {
    if (!isScenarioForce(node)) return 'unknown';
    const side = sideKey(attrs.side);
    const state = forceStateKey(attrs.state);
    if (side === 'unknown' || state === 'unknown') return 'unknown';
    if (state === 'destroyed') return 'stale';
    if (side === 'blue') return state === 'active' ? 'ok' : 'warn';
    return ['ok', 'warn', 'critical', 'stale'].includes(status)
      ? status
      : 'unknown';
  }
  if (node?.type === 'engagement') {
    const phase = engagementPhaseKey(attrs.phase);
    if (phase === 'proposed' || phase === 'authorized') return 'sand';
    if (phase === 'denied' || phase === 'expired') return 'stale';
    if (phase === 'adjudicated') {
      const consequence = consequenceKey(attrs.consequence);
      if (consequence === 'own_loss') return 'critical';
      if (consequence === 'own_damage') return 'warn';
      return consequence === 'unknown' ? 'unknown' : 'ok';
    }
    return 'unknown';
  }
  if (node?.type === 'vector') {
    const kind = vectorKindKey(attrs.kind);
    if (kind === 'unknown') return 'unknown';
    return kind === 'axis' && (status === 'warn' || status === 'critical')
      ? status
      : 'ok';
  }
  return 'unknown';
}

/**
 * The accessible name for a wargame node in the listbox twin, e.g.
 * "Red SAM 1, force, red, Short-range air defence, active, scenario,
 * simulated". Server strings keep their case; the console's words are
 * lowercased mid-sentence.
 */
export function wargameOptionText(node, { isNew = false } = {}) {
  const attrs = attrsOf(node);
  const parts = [wargameNodeLabel(node)];
  if (node?.type === 'force') {
    parts.push('force');
    if (isScenarioForce(node)) {
      parts.push(
        forceSideWord(node).toLowerCase(),
        safeText(attrs.kind_label, 48),
        forceStateWord(node).toLowerCase(),
        'scenario',
      );
    } else {
      parts.push(NOT_SCENARIO_WORD.toLowerCase());
    }
  } else if (node?.type === 'engagement') {
    const outcome = engagementOutcomeText(node);
    const consequence = consequenceWord(node);
    parts.push(
      'engagement',
      engagementPhaseWord(node).toLowerCase(),
      outcome ? outcome.toLowerCase() : '',
      consequence ? consequence.toLowerCase() : '',
      'scenario',
    );
  } else if (node?.type === 'vector') {
    const segments = vectorSubtitle(node).split(' · ');
    const kind = vectorKindWord(node);
    parts.push(
      'vector',
      ...(segments.includes(kind) ? [] : [kind.toLowerCase()]),
      ...segments
        .filter((s) => s !== SIMULATED_WORD)
        .map((s) => s.toLowerCase()),
      'scenario',
    );
  }
  parts.push('simulated');
  if (isNew) parts.push('new');
  return parts.filter(Boolean).join(', ');
}

/** Band captions for the session profile (§5.3.5); empty bands draw none. */
export const WARGAME_BAND_CAPTIONS = Object.freeze({
  force_red: 'Simulated red forces',
  force_blue: 'Simulated blue forces',
  engagement: 'Simulated engagements',
});

const count = (n) => Math.max(0, Math.floor(Number(n) || 0));

/**
 * One wargame band's caption, or null when it is empty: "Simulated red
 * forces 3 (1 side not set)", "Simulated blue forces 2", "Simulated
 * engagements 4, vectors 2", "Simulated vectors 2", each with "(+n)" for
 * recent arrivals. With `short`, the form the orb falls back to where the
 * full one does not fit the margin: "Red forces 3", "Blue forces 2",
 * "Engagements 4", "Vectors 2" (the band's nodes still say "Simulated").
 * @param {'force_red'|'force_blue'|'engagement'} key band key
 * @param {{count?: number, engagements?: number, vectors?: number,
 *   sideNotSet?: number, recent?: number}} [counts]
 * @param {{short?: boolean}} [options]
 */
export function wargameBandCaption(key, counts = {}, { short = false } = {}) {
  const total = count(counts.count);
  if (!total || !Object.hasOwn(WARGAME_BAND_CAPTIONS, key)) return null;
  let text;
  if (key === 'force_red') {
    const unset = Math.min(total, count(counts.sideNotSet));
    const red = total - unset;
    if (short) return red ? `Red forces ${red}` : `Forces ${unset}`;
    text = red ? `${WARGAME_BAND_CAPTIONS.force_red} ${red}` : '';
    if (unset)
      text = text
        ? `${text} (${unset} ${SIDE_NOT_SET.toLowerCase()})`
        : `Simulated forces, ${SIDE_NOT_SET.toLowerCase()} ${unset}`;
  } else if (key === 'engagement') {
    const vectors = Math.min(total, count(counts.vectors));
    const engagements = total - vectors;
    if (short)
      return engagements ? `Engagements ${engagements}` : `Vectors ${vectors}`;
    if (engagements && vectors)
      text = `${WARGAME_BAND_CAPTIONS.engagement} ${engagements}, vectors ${vectors}`;
    else if (engagements)
      text = `${WARGAME_BAND_CAPTIONS.engagement} ${engagements}`;
    else text = `Simulated vectors ${vectors}`;
  } else {
    if (short) return `Blue forces ${total}`;
    text = `${WARGAME_BAND_CAPTIONS[key]} ${total}`;
  }
  const recent = count(counts.recent);
  return recent ? `${text} (+${recent})` : text;
}
