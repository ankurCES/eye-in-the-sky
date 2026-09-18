/**
 * Contact roster (PLAN §3.1 feed row 5, acceptance 3 and 4).
 *
 * Renders `/snapshot.contacts[]` as the operator's persistent track list:
 * track id, classification, confidence, threat level, the last SALUTE and
 * time since last seen. Selecting a row focuses/tracks that contact.
 *
 * Read-only: selecting a contact moves the operator's view, never the drone.
 */
import {
  formatAge,
  h,
  label,
  replaceKids,
  setClass,
  setHidden,
} from './uavDom.js';

/** Threat ordering, worst first — the roster's sort key. */
const THREAT_RANK = Object.freeze({
  critical: 5,
  high: 4,
  medium: 3,
  moderate: 3,
  low: 2,
  none: 1,
  unknown: 0,
});

const CONFIDENCE_RANK = Object.freeze({
  confirmed: 3,
  probable: 2,
  possible: 1,
  unknown: 0,
});

const ROSTER_CSS = `
.uav-roster{margin-top:12px;padding-top:10px;border-top:1px solid #173a30;
  font:10px/1.45 "SF Mono",Menlo,monospace;color:#9fd9c8}
.uav-roster .uav-roster-head{display:flex;align-items:center;gap:6px;
  color:#1de9b6;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.uav-roster .uav-roster-count{margin-left:auto;color:#5f8c80}
.uav-roster .uav-roster-empty{color:#5f8c80}
.uav-roster .uav-roster-list{display:flex;flex-direction:column;gap:4px;
  max-height:190px;overflow-y:auto}
.uav-roster .uav-contact{text-align:left;width:100%;box-sizing:border-box;
  background:#0d1512;border:1px solid #234b40;border-radius:4px;padding:5px 7px;
  color:#9fd9c8;font:inherit;cursor:pointer;display:block;letter-spacing:0;
  text-transform:none}
.uav-roster .uav-contact:hover{border-color:#1de9b6}
.uav-roster .uav-contact.selected{border-color:#1de9b6;background:#10201a}
.uav-roster .uav-contact-top{display:flex;gap:6px;align-items:baseline}
.uav-roster .uav-contact-id{font-weight:700;color:#d7f5ec}
.uav-roster .uav-contact-age{margin-left:auto;color:#5f8c80}
.uav-roster .uav-contact-class{color:#7fb5a6}
.uav-roster .uav-chip{padding:0 4px;border-radius:3px;border:1px solid #234b40;
  color:#7fb5a6;font-size:9px;letter-spacing:.06em}
.uav-roster .uav-chip[data-threat="high"],
.uav-roster .uav-chip[data-threat="critical"]{border-color:#ff7a7a;color:#ff7a7a}
.uav-roster .uav-chip[data-threat="medium"],
.uav-roster .uav-chip[data-threat="moderate"]{border-color:#ffd166;color:#ffd166}
.uav-roster .uav-chip[data-threat="low"],
.uav-roster .uav-chip[data-threat="none"]{border-color:#1de9b6;color:#1de9b6}
.uav-roster .uav-contact-salute{margin-top:3px;color:#5f8c80;
  white-space:normal;word-break:break-word}
.uav-roster .uav-contact-nofix{color:#ffd166}
.uav-roster .is-hidden{display:none}
`;

/** One-line SALUTE digest; empty fields are dropped, never faked. */
export function saluteLine(salute = {}) {
  return [
    salute.size && `S:${salute.size}`,
    salute.activity && `A:${salute.activity}`,
    salute.location && `L:${salute.location}`,
    salute.unit && `U:${salute.unit}`,
    salute.time && `T:${salute.time}`,
    salute.equipment && `E:${salute.equipment}`,
  ]
    .filter(Boolean)
    .join(' · ');
}

/**
 * Sort and decorate contacts for display: worst threat first, then the most
 * confident, then the most recently seen. Pure, so the ordering is testable.
 * @param {Array<object>} contacts normalized contacts
 * @param {number} [nowMs] clock for the age column
 * @returns {Array<object>} display rows
 */
export function contactRosterModel(contacts, nowMs = Date.now()) {
  return (Array.isArray(contacts) ? contacts : [])
    .filter((contact) => contact?.trackId)
    .map((contact) => ({
      id: contact.id || contact.trackId,
      contact,
      trackId: contact.trackId,
      classification: label(contact.category, 'UNCLASSIFIED'),
      confidence: contact.confidence || 'unknown',
      threatLevel: contact.threatLevel || 'unknown',
      threatRank: THREAT_RANK[contact.threatLevel] ?? 0,
      confidenceRank: CONFIDENCE_RANK[contact.confidence] ?? 0,
      salute: saluteLine(contact.salute),
      lastSeenAtMs: contact.lastSeenAtMs ?? null,
      age: formatAge(contact.lastSeenAtMs, nowMs),
      located: Boolean(contact.position),
    }))
    .sort(
      (a, b) =>
        b.threatRank - a.threatRank ||
        b.confidenceRank - a.confidenceRank ||
        (b.lastSeenAtMs ?? 0) - (a.lastSeenAtMs ?? 0) ||
        a.trackId.localeCompare(b.trackId),
    );
}

/**
 * Create the contact roster surface.
 * @param {object} [options]
 * @param {(contact: object) => void} [options.onSelect] focus/track handler
 * @param {() => number} [options.now] clock, for tests
 * @param {boolean} [options.withStyle] emit the scoped stylesheet element
 * @returns {object} `{element, style, update, select, getSelected, destroy}`
 */
export function createUavContactRoster({
  onSelect = null,
  now = () => Date.now(),
  withStyle = true,
} = {}) {
  const style = withStyle ? h('style') : null;
  if (style) style.textContent = ROSTER_CSS;

  const count = h('span', { class: 'uav-roster-count' }, '0');
  const list = h('div', { class: 'uav-roster-list' });
  const empty = h('div', { class: 'uav-roster-empty' }, 'no contacts');
  const element = h(
    'div',
    { class: 'uav-roster' },
    h('div', { class: 'uav-roster-head' }, h('span', {}, 'Contacts'), count),
    empty,
    list,
  );

  let rows = [];
  let selectedId = null;

  function choose(row) {
    selectedId = row.id;
    paint();
    if (!onSelect) return;
    try {
      onSelect(row.contact);
    } catch {
      /* a focus handler must never break the roster */
    }
  }

  function card(row) {
    const button = h(
      'button',
      {
        class: 'uav-contact',
        type: 'button',
        'data-track': row.trackId,
        title: row.located
          ? 'Focus this contact'
          : 'No position reported for this contact',
      },
      h(
        'div',
        { class: 'uav-contact-top' },
        h('span', { class: 'uav-contact-id' }, row.trackId),
        h(
          'span',
          { class: 'uav-chip', 'data-threat': row.threatLevel },
          label(row.threatLevel),
        ),
        h('span', { class: 'uav-chip' }, label(row.confidence)),
        h('span', { class: 'uav-contact-age' }, row.age),
      ),
      h(
        'div',
        { class: 'uav-contact-class' },
        row.classification,
        row.located
          ? ''
          : h('span', { class: 'uav-contact-nofix' }, ' · NO FIX'),
      ),
      row.salute
        ? h('div', { class: 'uav-contact-salute' }, row.salute)
        : h('div', { class: 'uav-contact-salute' }, 'no SALUTE reported'),
    );
    setClass(button, 'selected', row.id === selectedId);
    button.addEventListener('click', () => choose(row));
    return button;
  }

  function paint() {
    replaceKids(list, rows.map(card));
    count.textContent = String(rows.length);
    setHidden(empty, rows.length > 0);
    setHidden(list, rows.length === 0);
  }

  paint();

  return {
    element,
    style,
    /**
     * Repaint the roster.
     * @param {Array<object>} contacts normalized contacts
     * @returns {Array<object>} the rows that were rendered
     */
    update(contacts) {
      rows = contactRosterModel(contacts, now());
      if (selectedId && !rows.some((row) => row.id === selectedId))
        selectedId = null;
      paint();
      return rows;
    },
    /** Programmatically select a track id, firing the focus handler. */
    select(id) {
      const row = rows.find((candidate) => candidate.id === id);
      if (row) choose(row);
      return Boolean(row);
    },
    getSelected() {
      return rows.find((row) => row.id === selectedId)?.contact ?? null;
    },
    rows() {
      return rows.slice();
    },
    destroy() {
      replaceKids(list, []);
      element.remove?.();
      style?.remove?.();
    },
  };
}
