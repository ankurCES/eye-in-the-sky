/**
 * Words for orb nodes: type words, status words, reading registers, Zulu
 * times and server-string segments (UX spec §2.6, §4.3, §9, §11.2).
 *
 * The console never writes `·`; server strings are split on " · " into
 * segments and rendered with spacing. Everything here is pure.
 *
 * Fail-safe (WG spec §4.2.1): a type the console does not know reads
 * "Unrecognised" ("Unrecognised (force)" in List view), its status is not
 * read at all, and its label is shown verbatim as text, bidi-safe.
 *
 * The simulated wargame (§5.3.4–§5.3.6): the node-level words here (label,
 * subtitle, status word, register, accessible name) know `force`,
 * `engagement` and `vector` and take them from wargameText.js. The
 * type-only words (`typeWord`, `typeLabel`) are shared with surfaces that
 * have not opted in yet, so they know the wargame types only with
 * `{wargame: true}`.
 */

import { isKnownType } from './glyphs.js';
import { isWargameType } from './wargameStyles.js';
import {
  WARGAME_TYPE_WORDS,
  wargameNodeLabel,
  wargameNodeSubtitle,
  wargameOptionText,
  wargameRegister,
  wargameStatusWord,
} from './wargameText.js';
import {
  SITE_REGISTER,
  SITE_STATUS_TEXT,
  safeText,
  siteCategory,
  siteLabel,
  siteSubtitle,
  siteWord,
  stripBidi,
} from './placeText.js';

export const TYPE_WORDS = Object.freeze({
  theater: 'Theater',
  feed: 'Feed',
  poi: 'Place',
  vehicle: 'Vehicle',
  mission: 'Mission',
  track: 'Contact',
  unit: 'Unit',
  equipment: 'Equipment',
  report: 'Report',
  alarm: 'Alarm',
  site: 'Site',
  ...WARGAME_TYPE_WORDS,
});

/** The type word for a type the console does not know. */
export const UNRECOGNISED_WORD = 'Unrecognised';
/** The inspector's fixed line for an unrecognised item (§4.2.1). */
export const UNRECOGNISED_ITEM_LINE =
  "The console doesn't recognise this kind of item, so it shows it as unknown. That isn't a statement that it's safe.";

const PHASE_WORDS = Object.freeze({
  planning: 'Planning',
  executing: 'Executing',
  rtb: 'Returning home',
  complete: 'Complete',
  aborted: 'Aborted',
});

const CONTACT_TYPES = new Set(['track', 'unit']);

/**
 * The type word ("Contact"), or "Unrecognised". Wargame types count only
 * with `{wargame: true}` (see isKnownType).
 */
export function typeWord(type, { wargame = false } = {}) {
  return isKnownType(type, { wargame }) ? TYPE_WORDS[type] : UNRECOGNISED_WORD;
}

/** A type as untrusted text (≤ 40 characters, bidi-safe). */
function typeText(type) {
  return safeText(type, 40) || 'no type';
}

/**
 * A type cell: "Contact", or "Unrecognised (force)" where the surface has
 * not opted in to the wargame; with `{wargame: true}`, "Force".
 */
export function typeLabel(type, { wargame = false } = {}) {
  return isKnownType(type, { wargame })
    ? TYPE_WORDS[type]
    : `${UNRECOGNISED_WORD} (${typeText(type)})`;
}

/** The inspector header for an unknown type: "Unrecognised item (force)". */
export function unrecognisedItemTitle(type) {
  return `Unrecognised item (${typeText(type)})`;
}

/** Sentence-case first letter. */
export function capitalize(text) {
  const value = String(text ?? '').trim();
  return value ? value[0].toUpperCase() + value.slice(1) : '';
}

/** Split a server string on " · " into trimmed, non-empty segments. */
export function splitSegments(text) {
  if (typeof text !== 'string' || !text.trim()) return [];
  return text
    .split(' · ')
    .map((part) => part.trim())
    .filter(Boolean);
}

/**
 * Plain names for the graph's feed nodes (`feed:{name}`). The server labels
 * feeds with their internal names ("Contacts", "Loop c"), which collide with
 * band captions or mean nothing to an operator.
 */
export const FEED_LABELS = Object.freeze({
  contacts: 'Contacts feed',
  detections: 'Detections',
  mission_state: 'Mission state',
  loop_c: 'Picture poll',
  real_data: 'Real data',
  sim: 'Sim',
  theater: 'Theater',
});

/**
 * Alarm kind -> label (UX spec §7.2), shared by the orb, search, inspector,
 * rail and banner so one alarm has one name everywhere. The server labels
 * alarm nodes with the humanized kind ("Bingo"); unknown kinds keep it.
 */
export const ALARM_KIND_LABEL = Object.freeze({
  bingo: 'BINGO fuel',
  geofence_proximity: 'Near geofence',
  geofence_breach: 'Geofence breach',
  lost_link: 'Lost link',
  link_restored: 'Link restored',
  detection: 'Detection',
  mission_phase: 'Mission phase',
  datum_degraded: 'Altitude datum degraded',
});

/** The detections feed row in `graph.meta.feeds`, whatever the host calls
 *  it: `contacts` (the server's key), `detections`, or any `*detect*` key. */
export function detectionsFeedOf(feeds) {
  if (!feeds || typeof feeds !== 'object') return null;
  for (const name of ['contacts', 'detections']) {
    const feed = feeds[name];
    if (feed && typeof feed === 'object') return feed;
  }
  for (const [name, feed] of Object.entries(feeds)) {
    if (/detect/i.test(name) && feed && typeof feed === 'object') return feed;
  }
  return null;
}

/** Longest subtitle segment the orb shows; the inspector has the rest. */
export const SEGMENT_MAX = 48;

/**
 * Segments that are plumbing, not intelligence: source strings
 * ("mcp:uav_list_tracks", "… @ http://…"), exception class names,
 * tracebacks and bare machine identifiers ("aaa_self_propelled_9"). The orb
 * drops them; the inspector shows the full record.
 */
const RAW_SEGMENT =
  /(^|[\s(;])mcp:|https?:\/\/|\b[A-Z][A-Za-z]*(Error|Exception)\b|Traceback|\bat 0x[0-9a-f]+|^[a-z][a-z0-9]*(_[a-z0-9]+)+$/;

function feedKey(node) {
  const id = String(node?.id ?? '');
  if (id.startsWith('feed:')) return id.slice(5);
  return String(node?.label ?? '')
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_');
}

/** "Contacts feed", "Mission state", … for a feed node. */
export function feedLabel(node) {
  const key = feedKey(node);
  if (Object.hasOwn(FEED_LABELS, key)) return FEED_LABELS[key];
  return capitalize(key.replace(/[_-]+/g, ' ')) || 'Feed';
}

/**
 * A feed's short state: "Up", "Degraded", "Off", "Not loaded", "Unknown",
 * "Down since 14:00Z" (only when the console saw it go down) or "Down".
 * Raw error text and source strings never appear here.
 * @param {object} node a `feed` graph node
 * @param {{downSince?: number|null}} [options] epoch ms the feed was first seen down
 */
export function feedState(node, { downSince = null } = {}) {
  const key = feedKey(node);
  const status = node?.status;
  const detail = String(node?.subtitle ?? '')
    .trim()
    .toLowerCase();
  if (key === 'real_data') {
    if (detail.startsWith('off')) return 'Off';
    if (detail.includes('not hydrated')) return 'Not loaded';
    if (detail.includes('degraded')) return 'Degraded';
    if (status === 'ok') return 'Up';
  }
  if (key === 'theater' && status === 'warn') return 'Unknown';
  switch (status) {
    case 'ok':
      return 'Up';
    case 'warn':
      return 'Degraded';
    case 'critical': {
      const at = formatZ(downSince, { seconds: false });
      return at ? `Down since ${at}` : 'Down';
    }
    case 'stale':
      return 'Stale';
    default:
      return 'No reading';
  }
}

/**
 * A server subtitle with plumbing segments removed and long segments cut,
 * re-joined with " · " so splitSegments() still works on it.
 */
export function cleanSubtitle(text) {
  return splitSegments(typeof text === 'string' ? stripBidi(text) : text)
    .filter((segment) => !RAW_SEGMENT.test(segment))
    .map((segment) =>
      segment.length > SEGMENT_MAX
        ? `${segment.slice(0, SEGMENT_MAX - 1).trimEnd()}…`
        : segment,
    )
    .join(' · ');
}

/**
 * The name the orb shows for a node (feeds get plain names). Always text:
 * bidi controls and control characters are removed (§3.11).
 */
export function nodeLabel(node) {
  if (isWargameType(node?.type)) return wargameNodeLabel(node);
  if (node?.type === 'feed') return feedLabel(node);
  if (node?.type === 'site') return siteLabel(node);
  const alarm = alarmNodeLabel(node);
  if (alarm) return alarm;
  return safeText(node?.label || node?.id || '', 400);
}

/** An alarm node's spec label by kind ("BINGO fuel"), or null. */
export function alarmNodeLabel(node) {
  if (node?.type !== 'alarm') return null;
  const kind = node?.attrs?.kind;
  return typeof kind === 'string' && Object.hasOwn(ALARM_KIND_LABEL, kind)
    ? ALARM_KIND_LABEL[kind]
    : null;
}

/**
 * The subtitle the orb shows for a node: a feed's short state, otherwise the
 * server subtitle without plumbing segments.
 * @param {object} node
 * @param {{downSince?: number|null}} [options]
 */
export function nodeSubtitle(node, options = {}) {
  if (isWargameType(node?.type)) return wargameNodeSubtitle(node);
  if (node?.type === 'feed') return feedState(node, options);
  if (node?.type === 'site') return siteSubtitle(node);
  if (!isKnownType(node?.type)) {
    const rest = cleanSubtitle(node?.subtitle);
    return [typeLabel(node?.type), rest].filter(Boolean).join(' · ');
  }
  return cleanSubtitle(node?.subtitle);
}

/**
 * A margin label's first line when the full name does not fit the ~76 px
 * gutter (review: two SA-6 batteries both read "medium-ran…"). The part that
 * tells entities apart is kept instead of the head the ellipsis would keep:
 * "medium-range SAM battery (SA-6/2K12 class)" -> "SA-6/2K12 class" or
 * "SA-6/2K12"; "command post / C2 node" -> "C2 node" when that fits; a unit's
 * "3 x …" count stays in front. A simulated engagement ("Simulated strike on
 * Towed anti-aircraft gun", WG §5.3.6) keeps what was engaged: "Strike on
 * Towed anti-aircraft gun" (its subtitle still says "Simulated"). Anything
 * else is returned whole (CSS ends it with "…").
 * @param {string} label the full node label
 * @param {(text:string)=>boolean} [fits] whether a text fits the gutter
 */
export function marginTitle(label, fits = () => true) {
  const text = String(label ?? '').trim();
  if (!text || fits(text)) return text;
  const engaged = /^Simulated (strike|shot|ground fire) on (.+)$/.exec(text);
  if (engaged) return `${capitalize(engaged[1])} on ${engaged[2]}`;
  const count = /^(\d+ x )(.+)$/.exec(text);
  if (count) {
    const rest = marginTitle(count[2], (t) => fits(count[1] + t));
    return count[1] + rest;
  }
  const cls = /\(([^()]+?)\s+class\)$/i.exec(text);
  if (cls) {
    const designation = cls[1].trim();
    return fits(`${designation} class`) ? `${designation} class` : designation;
  }
  const tail = text.split(' / ').pop().trim();
  return tail !== text && fits(tail) ? tail : text;
}

/**
 * A margin label's second line: a contact's confidence word is dropped (its
 * glyph ring already shows it, and the gutter is narrow), so
 * "probable · 6 sightings" reads "6 sightings".
 */
export function marginSubtitle(node, subtitle = nodeSubtitle(node)) {
  if (node?.type !== 'track') return subtitle;
  const word = String(node?.attrs?.confidence || 'unrated')
    .trim()
    .toLowerCase();
  return splitSegments(subtitle)
    .filter((segment) => segment.toLowerCase() !== word)
    .join(' · ');
}

/** "14:02:51Z" (seconds) or "14:02Z" from epoch ms; null when unknown. */
export function formatZ(ms, { seconds = true } = {}) {
  if (!Number.isFinite(ms)) return null;
  const date = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  return seconds ? `${hm}:${pad(date.getUTCSeconds())}Z` : `${hm}Z`;
}

/** The threat word for a contact or unit: "High", "Not assessed", … */
export function threatWord(node) {
  const threat = node?.attrs?.threat;
  if (typeof threat === 'string' && threat.trim()) {
    return threat.trim().toLowerCase() === 'not assessed'
      ? 'Not assessed'
      : capitalize(threat);
  }
  switch (node?.status) {
    case 'critical':
      return 'Critical';
    case 'warn':
      return 'High';
    case 'ok':
      return 'Low';
    case 'stale':
      return 'Stale';
    default:
      return 'Not assessed';
  }
}

/** One status word per node, always paired with colour (spec §2.2). */
export function statusWord(node) {
  const type = node?.type;
  const status = node?.status;
  if (isWargameType(type)) return wargameStatusWord(node);
  // Fail-safe: an unknown type's status is never read (§4.2.1).
  if (!isKnownType(type)) return 'Not assessed';
  if (type === 'site') return SITE_STATUS_TEXT;
  if (CONTACT_TYPES.has(type)) {
    if (status === 'stale') return 'Stale';
    return threatWord(node);
  }
  if (type === 'mission') {
    const phase = node?.attrs?.phase;
    return Object.hasOwn(PHASE_WORDS, phase)
      ? PHASE_WORDS[phase]
      : 'Phase unknown';
  }
  if (type === 'feed') {
    if (status === 'ok') return 'Up';
    if (status === 'warn') return 'Degraded';
    if (status === 'critical') return 'Down';
  }
  if (type === 'alarm') {
    if (status === 'critical') return 'Critical';
    if (status === 'warn') return 'Warning';
    if (status === 'ok') return 'Info';
  }
  if (type === 'equipment') return 'Reference';
  if (type === 'poi') return 'Place';
  switch (status) {
    case 'ok':
      return 'Normal';
    case 'warn':
      return 'Warning';
    case 'critical':
      return 'Critical';
    case 'stale':
      return 'Stale';
    default:
      return 'Unknown';
  }
}

/**
 * The reading register for a node's headline value (spec §2.6), fixed per
 * type: threat levels are model estimates; missing threat is "Not assessed";
 * height readings are "Assumed" only when the server says `agl_is_real:false`.
 */
export function registerOf(node) {
  const attrs = node?.attrs || {};
  if (isWargameType(node?.type)) return wargameRegister(node);
  if (!isKnownType(node?.type)) return 'Not assessed';
  if (node?.type === 'site') return SITE_REGISTER;
  if (CONTACT_TYPES.has(node?.type)) {
    return threatWord(node) === 'Not assessed' ? 'Not assessed' : 'Estimated';
  }
  if (node?.type === 'vehicle') {
    if (attrs.agl_is_real === false) return 'Assumed';
    return Number.isFinite(attrs.fuel_pct) ? 'Measured' : 'No reading';
  }
  if (node?.type === 'feed' && node?.status === 'critical') return 'No reading';
  if (node?.type === 'equipment') return 'Reference';
  return 'Measured';
}

/**
 * The accessible name for a listbox option, e.g.
 * "SA-6 battery, contact, probable, threat high, estimated, 2 sightings" or
 * "Contacts feed, feed, down since 14:00Z".
 */
export function optionText(node, { isNew = false, downSince = null } = {}) {
  if (isWargameType(node?.type)) return wargameOptionText(node, { isNew });
  const parts = [nodeLabel(node)];
  parts.push(typeLabel(node?.type).toLowerCase());
  const segments = splitSegments(nodeSubtitle(node, { downSince }));
  if (!isKnownType(node?.type)) {
    // "X, unrecognised (force), not assessed, …": the type is not repeated.
    parts.push('not assessed', ...segments.slice(1));
  } else if (node?.type === 'site') {
    parts.push(
      siteWord(siteCategory(node)).toLowerCase(),
      SITE_STATUS_TEXT.toLowerCase(),
    );
  } else if (node?.type === 'feed') {
    const state = segments[0] || '';
    parts.push(state ? state[0].toLowerCase() + state.slice(1) : '');
  } else if (CONTACT_TYPES.has(node?.type)) {
    const confidence =
      typeof node?.attrs?.confidence === 'string'
        ? node.attrs.confidence.trim()
        : '';
    if (confidence) parts.push(confidence.toLowerCase());
    const threat = threatWord(node);
    if (threat === 'Not assessed') parts.push('threat not assessed');
    else if (threat === 'Stale') parts.push('stale');
    else parts.push(`threat ${threat.toLowerCase()}`, 'estimated');
    parts.push(
      ...segments.filter(
        (part) => part.toLowerCase() !== confidence.toLowerCase(),
      ),
    );
  } else {
    // "East Field, place, place": a status word that only repeats the type
    // word ("Place", "Reference" for equipment) says nothing new.
    const sw = statusWord(node).toLowerCase();
    parts.push(sw === parts[1] ? '' : sw, ...segments);
  }
  if (isNew) parts.push('new');
  return parts.filter(Boolean).join(', ');
}
