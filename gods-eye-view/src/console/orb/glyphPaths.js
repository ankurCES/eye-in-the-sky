/**
 * Glyph path data shared by the orb and the map's context icons (WG spec
 * Appendix A, R24): `console/orb/glyphs.js` paints them on the canvas and in
 * chips; `layers/uav/contextIcons.js` paints them on map billboards.
 *
 * Pure constants, no imports, so both bundles can take them. Every path is
 * SVG `d` in a 24×24 box using only absolute M, L, H, V, A and Z; every A is
 * a semicircle (the canvas fallback in glyphs.js traces arcs that way); they
 * are drawn stroked, width 2, round joins. Site glyphs and `unrecognised` are
 * Phase A; the wargame frames, engagement and vector glyphs are Phase B.
 */

export const GLYPH_PATH_BOX = 24;

/** The closed site vocabulary (§3.9); anything else draws as `other`. */
export const SITE_CATEGORIES = Object.freeze([
  'airfield',
  'military_base',
  'port',
  'power',
  'fuel',
  'comms',
  'bridge',
  'rail_hub',
  'hq_gov',
  'border_crossing',
  'medical',
  'dam',
  'other',
]);

/** {[category]: d} — stroked outlines, always Pencil (§4.2.6). */
export const SITE_GLYPHS = Object.freeze({
  airfield:
    'M12 2.5L13.5 4V9.5L21.5 13.5V15.5L13.5 13V18.5L16 20.5V21.5L12 20.5L8 21.5V20.5L10.5 18.5V13L2.5 15.5V13.5L10.5 9.5V4Z',
  military_base: 'M3.5 20.5V8.5H7V11.5H10.5V8.5H13.5V11.5H17V8.5H20.5V20.5Z',
  port: 'M10 4.5A2 2 0 1 0 14 4.5A2 2 0 1 0 10 4.5ZM12 6.5V20M8 10H16M5 13A7 7 0 0 0 19 13',
  power: 'M13.5 2.5L5.5 13.5H11L9.5 21.5L18.5 10H12.5Z',
  fuel: 'M5 21V4H13V21ZM7 7H11V10.5H7ZM13 9H16V17A1.5 1.5 0 0 0 19 17V8L16.5 5.5M3.5 21H14.5',
  comms:
    'M12 7V21M8.5 21L12 9.5L15.5 21M6.5 3.5A3 3 0 0 0 6.5 9.5M17.5 3.5A3 3 0 0 1 17.5 9.5',
  bridge: 'M2.5 8.5H21.5M4.5 8.5V20.5M19.5 8.5V20.5M5 20.5A7 7 0 0 1 19 20.5',
  rail_hub:
    'M8.5 3V21M15.5 3V21M8.5 6.5H15.5M8.5 10.5H15.5M8.5 14.5H15.5M8.5 18.5H15.5',
  hq_gov:
    'M3 9.5L12 3.5L21 9.5ZM5.5 12V18.5M10 12V18.5M14 12V18.5M18.5 12V18.5M3 21H21',
  border_crossing:
    'M5 21V8M3 21H7M5 10.5H21.5V14H5M10 10.5L8 14M14.5 10.5L12.5 14M19 10.5L17 14',
  medical:
    'M12 2.5L20 5.5V12L12 21.5L4 12V5.5ZM10.75 7.5H13.25V10.75H16.5V13.25H13.25V16.5H10.75V13.25H7.5V10.75H10.75Z',
  dam: 'M13 3.5H16.5L21 20.5H13ZM2.5 9A1.75 1.75 0 0 1 6 9A1.75 1.75 0 0 0 9.5 9M2.5 14A1.75 1.75 0 0 1 6 14A1.75 1.75 0 0 0 9.5 14',
  other:
    'M7 9A5 5 0 0 1 17 9L12 20.5ZM10.5 9A1.5 1.5 0 0 1 13.5 9A1.5 1.5 0 0 1 10.5 9Z',
});

/**
 * The fail-safe glyph (§4.2.1): a ring with a question mark, for any node
 * type the console does not know. Lilac, never a status colour.
 */
export const UNRECOGNISED_GLYPH =
  'M3 12A9 9 0 1 0 21 12A9 9 0 1 0 3 12ZM9 9A3 3 0 0 1 15 9L12 12.5V14.5M12 16.5V18';

const SITE_SET = new Set(SITE_CATEGORIES);

/** A site category from the closed vocabulary, else `other`. */
export function siteCategoryKey(category) {
  return typeof category === 'string' && SITE_SET.has(category)
    ? category
    : 'other';
}

/** The glyph `d` for a site category (unknown categories get `other`). */
export function siteGlyphPath(category) {
  return SITE_GLYPHS[siteCategoryKey(category)];
}
