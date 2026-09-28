/**
 * Words for places on the console: sites (WG spec §4.2.6, Appendix B), the
 * theater-change caption (§4.2.4) and area sizes, plus the untrusted-text
 * rule (§0.2, §3.11) every place string passes through.
 *
 * OSM names, geocoder labels, theater labels and tags are untrusted: callers
 * render the results only as DOM text or canvas text, never as markup. The
 * helpers here strip bidi controls and control characters so no reordering
 * mark reaches a label. Pure; the console never writes "·".
 */

import { siteCategoryKey } from './glyphPaths.js';

/** Bidi embedding, override and isolate controls, plus the directional marks. */
const BIDI = /[‪-‮⁦-⁩‎‏؜]/g;
const CONTROL = /[\u0000-\u001F\u007F-\u009F]/g;

/** `text` without bidi controls (§0.2). Non-strings become strings. */
export function stripBidi(text) {
  return String(text ?? '').replace(BIDI, '');
}

/**
 * An untrusted string made safe to show as text: bidi controls removed,
 * control characters turned into spaces, whitespace collapsed, and cut to
 * `max` characters with an ellipsis.
 * @param {unknown} text
 * @param {number} [max]
 * @returns {string}
 */
export function safeText(text, max = 80) {
  const value = stripBidi(text)
    .replace(CONTROL, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const limit = Math.max(1, Math.floor(Number(max) || 80));
  return value.length > limit
    ? `${value.slice(0, limit - 1).trimEnd()}…`
    : value;
}

/** Site words, singular and plural (Appendix B). */
export const SITE_WORDS = Object.freeze({
  airfield: Object.freeze(['Airfield', 'Airfields']),
  military_base: Object.freeze(['Military site', 'Military sites']),
  port: Object.freeze(['Port', 'Ports']),
  power: Object.freeze(['Power', 'Power']),
  fuel: Object.freeze(['Fuel', 'Fuel']),
  comms: Object.freeze(['Comms', 'Comms']),
  bridge: Object.freeze(['Bridge', 'Bridges']),
  rail_hub: Object.freeze(['Rail hub', 'Rail hubs']),
  hq_gov: Object.freeze(['Government', 'Government']),
  border_crossing: Object.freeze(['Border crossing', 'Border crossings']),
  medical: Object.freeze(['Medical, protected', 'Medical']),
  dam: Object.freeze(['Dam', 'Dams']),
  other: Object.freeze(['Mapped site', 'Mapped sites']),
});

/** Nouns for an unnamed site ("Unnamed power site"). */
const UNNAMED_NOUN = Object.freeze({
  military_base: 'military site',
  power: 'power site',
  fuel: 'fuel site',
  comms: 'comms site',
  hq_gov: 'government site',
  medical: 'medical site',
  other: 'mapped site',
});

export const SITE_STATUS_TEXT = 'Mapped, not verified';
export const SITE_PROTECTED_TEXT = 'Protected';
export const SITE_REGISTER = 'Mapped';
export const SITE_CAVEAT =
  'Mapped, not verified. Mapping may be incomplete, out of date or wrong; a missing site is not an absent one.';
export const MAP_ATTRIBUTION = 'Map data: © OpenStreetMap contributors, ODbL.';
export const SITES_DEGRADED_TEXT =
  'Map data feed down. Sites may be missing, not absent.';
export const SITES_CAPTION = 'Sites';

/** "Airfield", "Mapped site" for an unknown category. */
export function siteWord(category) {
  return SITE_WORDS[siteCategoryKey(category)][0];
}

/** "Airfields", "Mapped sites" for an unknown category. */
export function sitePlural(category) {
  return SITE_WORDS[siteCategoryKey(category)][1];
}

/** A search category chip: "Airfields 3". */
export function siteCountLabel(category, n) {
  const count = Math.max(0, Math.floor(Number(n) || 0));
  return `${sitePlural(category)} ${count}`;
}

/** A site node's category key (unknown → `other`). */
export function siteCategory(node) {
  return siteCategoryKey(node?.attrs?.category);
}

/** Whether a site node is marked protected (medical). */
export function siteProtected(node) {
  return node?.attrs?.protected === true || siteCategory(node) === 'medical';
}

/** A site's name as text: the OSM name, bidi-safe, ≤ 80 characters. */
export function siteLabel(node) {
  const name = safeText(node?.label, 80);
  if (name) return name;
  const key = siteCategory(node);
  return `Unnamed ${UNNAMED_NOUN[key] ?? siteWord(key).toLowerCase()}`;
}

/** A site's orb subtitle, " · "-joined for splitSegments(). */
export function siteSubtitle(node) {
  return [siteWord(siteCategory(node)), SITE_STATUS_TEXT].join(' · ');
}

/**
 * The site band caption: "Sites 41 (12 more on the map)", "Sites 41", or
 * null for an empty band (which draws no caption).
 * @param {{count?: number, omitted?: number}} counts
 */
export function siteBandCaption({ count = 0, omitted = 0 } = {}) {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  const more = Math.max(0, Math.floor(Number(omitted) || 0));
  if (!n) return null;
  return more
    ? `${SITES_CAPTION} ${n} (${more} more on the map)`
    : `${SITES_CAPTION} ${n}`;
}

/** The map dock's overflow line: "12 more sites not drawn." */
export function sitesNotDrawnText(n) {
  const count = Math.max(0, Math.floor(Number(n) || 0));
  return `${count} more ${count === 1 ? 'site' : 'sites'} not drawn.`;
}

/**
 * The orb caption after a theater change (§4.2.4): "Theater changed to
 * Kherson, Ukraine. 41 items arrived." The label is untrusted text.
 * @param {unknown} label theater label
 * @param {number} n items that arrived with the change
 */
export function theaterChangedText(label, n) {
  const name = safeText(label, 80) || 'the new theater';
  const count = Math.max(0, Math.floor(Number(n) || 0));
  return `Theater changed to ${name}. ${count} ${count === 1 ? 'item' : 'items'} arrived.`;
}

const KM_PER_DEG = (Math.PI * 6371.0088) / 180;

/**
 * Width and height in km of a `[s, w, n, e]` bbox (§3.1), east-west measured
 * at the middle latitude; null for anything that is not four finite numbers
 * with s < n.
 * @returns {{w: number, h: number}|null}
 */
export function bboxSizeKm(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4) return null;
  const [s, w, n, e] = bbox.map(Number);
  if (![s, w, n, e].every(Number.isFinite) || !(n > s)) return null;
  const span = (((e - w) % 360) + 360) % 360 || (e !== w ? 360 : 0);
  const mid = ((s + n) / 2) * (Math.PI / 180);
  return {
    w: span * KM_PER_DEG * Math.cos(mid),
    h: (n - s) * KM_PER_DEG,
  };
}

/**
 * An area's size as text: "5.0 × 5.0 km", or with `withArea`
 * "5.0 × 5.0 km (25.0 km²)". Takes a theater block (`bbox`, else
 * `half_extent_m` as a square) or a bare bbox; null when neither is usable.
 */
export function areaText(theaterOrBbox, { withArea = false } = {}) {
  const block = Array.isArray(theaterOrBbox)
    ? { bbox: theaterOrBbox }
    : theaterOrBbox || {};
  let size = bboxSizeKm(block.bbox);
  const half = Number(block.half_extent_m);
  if (!size && Number.isFinite(half) && half > 0)
    size = { w: (2 * half) / 1000, h: (2 * half) / 1000 };
  if (!size) return null;
  const text = `${size.w.toFixed(1)} × ${size.h.toFixed(1)} km`;
  if (!withArea) return text;
  const area = Number(block.area_km2);
  const km2 = Number.isFinite(area) && area > 0 ? area : size.w * size.h;
  return `${text} (${km2.toFixed(1)} km²)`;
}

/**
 * A theater home's `source` → the register its position is read in, shared
 * by the theater slip and the theater inspector (WG §4.2.2, §4.2.5): open
 * ground found on the map is Mapped, a table preset Measured, the operator's
 * pick Requested, and the AO centre (no mapped open ground there, or map
 * data off) Assumed. Anything else has no register.
 */
export const HOME_SOURCE_REGISTER = Object.freeze({
  'overpass-open-ground': 'mapped',
  preset: 'measured',
  operator: 'requested',
  'ao-centre': 'assumed',
});

/** What an `ao-centre` home is, where it has no name of its own. */
export const HOME_AO_CENTRE_TEXT = 'AO centre (no mapped open ground)';

/** The register key for a home `source`, or null. */
export function homeRegister(source) {
  return typeof source === 'string' &&
    Object.hasOwn(HOME_SOURCE_REGISTER, source)
    ? HOME_SOURCE_REGISTER[source]
    : null;
}

/** A theater placed from typed coordinates was never looked up. */
export const PLACED_FROM_COORDINATES = 'Placed from coordinates, not geocoded';
