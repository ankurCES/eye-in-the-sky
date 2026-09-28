/**
 * Slip bodies for the theater change and the sim-speed change (WG spec
 * §4.2.2). Everything under the summary comes from the server's preview
 * (`theater_preview`, `time_scale_preview`), and a missing field hides its
 * row. Place names, labels and home names are third-party text (geocoder,
 * OSM): they reach the DOM only as text through the bidi-safe factory. The
 * footprint drawing is built from numbers only.
 *
 * slip.js owns the frame (band, title, arming, buttons, Deny-only); this
 * module only builds the info nodes.
 */

import { h as domH } from '../../ui/uavDom.js';
import {
  bidiSafe,
  coord,
  segments,
  spanText,
  stripBidi,
  truncate,
} from './format.js';
import { previewOf } from './validateTheater.js';
import {
  HOME_AO_CENTRE_TEXT,
  PLACED_FROM_COORDINATES,
  homeRegister,
} from '../orb/placeText.js';

// Place names, labels and home names are geocoder and OSM text: no bidi
// control reaches the slip, and nothing here is parsed as markup.
const h = bidiSafe(domH);

const EARTH_R_M = 6371008.8;
const RAD = Math.PI / 180;
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
// Half-up to 0.1 km (7350 m is "7.4", not toFixed's "7.3").
const km1 = (m) => (Math.round(m / 100) / 10).toFixed(1);

export const THEATER_COPY = Object.freeze({
  notAMap: (vehicle) => `Not a map. Shows the area against ${vehicle}'s reach.`,
  cantReach: (vehicle) => `${vehicle} can't reach the far corners and return.`,
  undo: (label) =>
    label
      ? `Set the theater back to ${label} with another change, which also needs your approval.`
      : 'Set the theater back with another change, which also needs your approval.',
  speedUndo: (from) =>
    `Set the speed back to ×${from} with another simulation change, which also needs your approval.`,
});

/** Width and height of a `[s,w,n,e]` bbox in metres (spherical). */
export function bboxSizeM(bbox) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(isNum))
    return null;
  const [s, w, n, e] = bbox;
  const east = e < w ? e + 360 : e;
  const lat = ((s + n) / 2) * RAD;
  return {
    w: Math.abs(east - w) * RAD * EARTH_R_M * Math.cos(lat),
    h: Math.abs(n - s) * RAD * EARTH_R_M,
  };
}

/** "5.0 × 5.0 km (25.0 km²)" from a preview, or '' when it can't be said. */
export function areaText(preview) {
  const size = bboxSizeM(preview?.bbox);
  if (!size) return '';
  const area = isNum(preview?.area_km2)
    ? preview.area_km2
    : (size.w * size.h) / 1e6;
  return `${km1(size.w)} × ${km1(size.h)} km (${area.toFixed(1)} km²)`;
}

/** The place words for a theater approval: preview place, label, or args. */
export function theaterPlace(approval) {
  const p = approval?.theaterPreview;
  const text =
    str(p?.place) || str(p?.label) || str(approval?.args?.label) || '';
  return truncate(stripBidi(text), 120);
}

/** Metres east and north of `center` for a lat/lon (equirectangular). */
function offsetM(center, lat, lon) {
  return {
    x: (lon - center[1]) * RAD * EARTH_R_M * Math.cos(center[0] * RAD),
    y: (lat - center[0]) * RAD * EARTH_R_M,
  };
}

/** A number for SVG markup: one decimal, never text. */
const svgNum = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) throw new TypeError('not a number');
  return n.toFixed(1);
};

/** Only digits, dots, minus, spaces and the fixed tags below may appear. */
export const FOOTPRINT_SAFE =
  /^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" focusable="false">(?:<(?:rect|circle) (?:[a-z-]+="[-0-9. ]*" ?)+(?:class="ic-fp-(?:area|reach|home)")\/>)+<\/svg>$/;

/**
 * The footprint SVG markup (72 px): the area square, the home dot and the
 * dashed reach circle, from numbers only. Returns '' when the numbers are
 * missing. `warn` is true when the reach is less than the half-diagonal.
 * @returns {{svg: string, warn: boolean}}
 */
export function footprintSvg(preview) {
  const size = bboxSizeM(preview?.bbox);
  const center = preview?.center;
  if (!size || !Array.isArray(center) || !center.every(isNum))
    return { svg: '', warn: false };
  const halfW = size.w / 2;
  const halfH = size.h / 2;
  const halfDiag = Math.hypot(halfW, halfH);
  const reach = isNum(preview?.airframe?.reach_m)
    ? preview.airframe.reach_m
    : null;
  const home =
    isNum(preview?.home?.lat) && isNum(preview?.home?.lon)
      ? offsetM(center, preview.home.lat, preview.home.lon)
      : { x: 0, y: 0 };
  let extent = Math.max(halfW, halfH, 1);
  if (reach != null) {
    extent = Math.max(
      extent,
      Math.abs(home.x) + reach,
      Math.abs(home.y) + reach,
    );
  }
  const k = 32 / extent; // 4 px margin in a 72 px box
  const px = (m) => 36 + m * k;
  const py = (m) => 36 - m * k;
  try {
    const parts = [
      `<rect x="${svgNum(px(-halfW))}" y="${svgNum(py(halfH))}" width="${svgNum(2 * halfW * k)}" height="${svgNum(2 * halfH * k)}" class="ic-fp-area"/>`,
    ];
    if (reach != null) {
      parts.push(
        `<circle cx="${svgNum(px(home.x))}" cy="${svgNum(py(home.y))}" r="${svgNum(reach * k)}" stroke-dasharray="3 3" class="ic-fp-reach"/>`,
      );
    }
    parts.push(
      `<circle cx="${svgNum(px(home.x))}" cy="${svgNum(py(home.y))}" r="2.5" class="ic-fp-home"/>`,
    );
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" focusable="false">' +
      parts.join('') +
      '</svg>';
    if (!FOOTPRINT_SAFE.test(svg)) return { svg: '', warn: false };
    return { svg, warn: reach != null && reach < halfDiag };
  } catch {
    return { svg: '', warn: false };
  }
}

// ---- shared blocks ---------------------------------------------------------

/** The first vehicle's name for reach captions, or a generic word. */
function reachVehicle(fleet) {
  const name = Array.isArray(fleet) ? fleet.find((v) => v?.name)?.name : null;
  return name || 'the aircraft';
}

function summaryLine(parts, tag) {
  const kids = [];
  parts.filter(Boolean).forEach((part, i) => {
    if (i) kids.push(h('span', { class: 'ic-vh' }, ', '));
    kids.push(h('span', { class: 'ic-seg' }, part));
  });
  if (kids.length) kids.push(h('span', { class: 'ic-vh' }, ', '));
  return h('p', { class: 'ic-slip__summary' }, ...kids, tag('Requested'));
}

/** Lower-case, single-spaced, trimmed: how two sentences are compared. */
const sentenceKey = (text) => text.trim().replace(/\s+/g, ' ').toLowerCase();

/** An item's sentences, compared by sentenceKey. */
const sentencesOf = (text) =>
  sentenceKey(text)
    .replace(/([.!?]) /g, '$1\n')
    .split('\n')
    .filter(Boolean);

/**
 * Items without repeats: one is dropped when every sentence it has was
 * already said by an earlier kept item. The speed slip joins A8's
 * consequences ("… 4× faster (physics, fuel, sun). Link-loss timers stay in
 * wall-clock seconds.") with A7's caveats, which repeat that last sentence.
 * @param {unknown[]} items
 * @returns {string[]}
 */
export function uniqueSentences(items) {
  const kept = [];
  const said = new Set();
  for (const c of Array.isArray(items) ? items : []) {
    if (typeof c !== 'string') continue;
    const parts = sentencesOf(c);
    if (!parts.length || parts.every((part) => said.has(part))) continue;
    kept.push(c);
    for (const part of parts) said.add(part);
  }
  return kept;
}

/**
 * "What happens": the approval's consequences (A8) and, for the speed slip,
 * the preview's caveats (A7), each said once.
 */
function whatHappens(items) {
  const nodes = [h('h4', { class: 'ic-slip__head' }, 'What happens')];
  const list = uniqueSentences(items);
  nodes.push(
    list.length
      ? h('ul', { class: 'ic-slip__list' }, ...list.map((c) => h('li', {}, c)))
      : h(
          'p',
          { class: 'ic-slip__muted' },
          'The server sent no description of the effects.',
        ),
  );
  return nodes;
}

/**
 * The console's own refusal codes (validateTheater.assessTheater) and the
 * per-vehicle check each one contradicts (theater_switch.quick_checks words
 * it "{vehicle} on the ground", …).
 */
const CHECK_FOR_REASON = Object.freeze({
  airborne: 'on the ground',
  busy: 'has no task running',
  bingo: 'BINGO not latched',
  link: 'link up',
});

export const CHECKS_CHANGED =
  'Measured when the analyst asked. Marked ✕ where that has changed since.';

/**
 * Checks with ✓/✕, Measured (any ✕ makes the slip Deny-only). The server
 * measured them at the request; when the console's own assessment now finds
 * a problem (`now`: assessTheater's result), the check it contradicts turns
 * ✕ and a line says when the rest were measured, so the list never shows
 * "✓ Drone1 on the ground" beside "Drone1 is airborne".
 * @param {Array<{text: string, ok: boolean}>} checks the preview's checks
 * @param {Function} tag register tag factory
 * @param {{now?: {ok: boolean, reasons: Array}|null}} [opts]
 */
export function checkNodes(checks, tag, { now = null } = {}) {
  if (!Array.isArray(checks) || !checks.length) return [];
  const reasons =
    now && now.ok === false && Array.isArray(now.reasons) ? now.reasons : [];
  const failingNow = new Set();
  for (const r of reasons) {
    const code = typeof r?.code === 'string' ? r.code : '';
    const words = Object.hasOwn(CHECK_FOR_REASON, code)
      ? CHECK_FOR_REASON[code]
      : null;
    if (words && typeof r.vehicle === 'string' && r.vehicle)
      failingNow.add(`${r.vehicle} ${words}`);
  }
  const nodes = [
    h('h4', { class: 'ic-slip__head' }, 'Checks ', tag('Measured')),
  ];
  if (reasons.length)
    nodes.push(h('p', { class: 'ic-slip__muted' }, CHECKS_CHANGED));
  nodes.push(
    h(
      'ul',
      { class: 'ic-slip__checks' },
      ...checks.map((c) => {
        const text =
          typeof c?.text === 'string' && c.text ? c.text : 'Unnamed check';
        const changed = c?.ok === true && failingNow.has(text);
        const ok = c?.ok === true && !changed;
        return h(
          'li',
          {
            'data-ok': ok ? 'true' : 'false',
            'data-changed': changed ? 'true' : null,
          },
          h(
            'span',
            { class: 'ic-slip__mark', 'aria-hidden': 'true' },
            ok ? '✓' : '✕',
          ),
          h(
            'span',
            { class: 'ic-vh' },
            ok ? 'Passed: ' : changed ? 'Failed now: ' : 'Failed: ',
          ),
          text,
        );
      }),
    ),
  );
  return nodes;
}

function textList(head, items) {
  const list = Array.isArray(items)
    ? items.filter((t) => typeof t === 'string' && t)
    : [];
  if (!list.length) return [];
  return [
    h('h4', { class: 'ic-slip__head' }, head),
    h('ul', { class: 'ic-slip__list' }, ...list.map((t) => h('li', {}, t))),
  ];
}

function undoNodes(line) {
  return [
    h('h4', { class: 'ic-slip__head' }, 'How to undo it'),
    h('p', { class: 'ic-slip__console' }, line),
  ];
}

// ---- theater -----------------------------------------------------------------

/** Register words for the home's source (shared with the inspector). */
const REGISTER_WORD = Object.freeze({
  mapped: 'Mapped',
  measured: 'Measured',
  requested: 'Requested',
  assumed: 'Assumed',
});

function homeCell(home) {
  if (!home || typeof home !== 'object') return null;
  const parts = [];
  // An AO-centre home is no place of its own: say what it is.
  const name =
    str(home.name) ||
    (home.source === 'ao-centre' ? HOME_AO_CENTRE_TEXT : null);
  if (name) parts.push(h('span', {}, truncate(name, 80)), ' ');
  if (isNum(home.lat) && isNum(home.lon))
    parts.push(
      h('span', { class: 'ic-mono' }, `${coord(home.lat)}, ${coord(home.lon)}`),
    );
  if (isNum(home.distance_m))
    parts.push(`, ${km1(home.distance_m)} km from the centre`);
  if (!parts.length) return null;
  // The same register the theater inspector gives this home; an unknown
  // source claims none (it used to read "Mapped" for the AO centre).
  const register = REGISTER_WORD[homeRegister(home.source)];
  if (register) parts.push(' ', register);
  return parts;
}

function theaterRows(p, vehicle) {
  const rows = [];
  const place = str(p.place);
  if (place) rows.push(['Place', [h('span', {}, truncate(place, 120))]]);
  if (Array.isArray(p.center) && p.center.length === 2 && p.center.every(isNum))
    rows.push([
      'Centre',
      [
        h(
          'span',
          { class: 'ic-mono' },
          `${coord(p.center[0])}, ${coord(p.center[1])}`,
        ),
      ],
    ]);
  const area = areaText(p);
  if (area) {
    const cell = [`${area} `, 'Requested'];
    const from = p.clamped_from_km;
    if (Array.isArray(from) && from.length === 2 && from.every(isNum)) {
      cell.push(
        h('br', {}),
        `Clamped from ${from[0].toFixed(1)} × ${from[1].toFixed(1)} km to fit ${vehicle}'s reach. `,
        'Estimated',
      );
    }
    rows.push(['Area', cell]);
  }
  const home = homeCell(p.home);
  if (home) rows.push(['Home', home]);
  if (isNum(p.ground_msl_m)) {
    const cell = [
      `≈ ${Math.round(p.ground_msl_m)} m above sea level `,
      'Estimated',
    ];
    if (str(p.ground_source)) cell.push(h('br', {}), str(p.ground_source));
    rows.push(['Ground', cell]);
  }
  const af = p.airframe;
  if (af && typeof af === 'object') {
    const label = str(af.label) || str(af.to);
    if (label) {
      let text = label;
      if (isNum(af.reach_m)) text += `, reach ≈ ${km1(af.reach_m)} km`;
      if (str(af.from) && str(af.to) && af.from !== af.to)
        text += `. Changes from ${str(af.from)}`;
      rows.push(['Airframe', [h('span', {}, text)]]);
    }
  }
  if (p.sites && typeof p.sites === 'object' && isNum(p.sites.total)) {
    const cell = [`${p.sites.total} mapped sites `, 'Mapped'];
    if (p.sites.degraded === true)
      cell.push(
        h('br', {}),
        'Map data feed down. Sites may be missing, not absent.',
      );
    rows.push(['Sites', cell]);
  }
  return rows;
}

function footprintNodes(p, vehicle) {
  const { svg, warn } = footprintSvg(p);
  if (!svg) return [];
  const pic = h('span', {
    class: 'ic-slip__footprint',
    'aria-hidden': 'true',
    'data-warn': warn ? 'true' : 'false',
  });
  // Numbers-only markup (FOOTPRINT_SAFE); no text from the preview is in it.
  pic.innerHTML = svg;
  const captions = [
    h('p', { class: 'ic-slip__caption' }, THEATER_COPY.notAMap(vehicle)),
  ];
  if (warn)
    captions.push(
      h(
        'p',
        { class: 'ic-slip__warnline', role: 'note' },
        THEATER_COPY.cantReach(vehicle),
      ),
    );
  return [
    h('div', { class: 'ic-slip__figure' }, pic, h('div', {}, ...captions)),
  ];
}

/**
 * The summary's geocoder segment. A centre typed as coordinates was placed,
 * not geocoded, and a table theater is a preset: saying "geocoded by
 * Coordinates" would claim a lookup that never ran (the rail says the same,
 * A17b; E2E A1, A14).
 * @param {string|null} geocoder `theater_preview.geocoder`
 * @returns {string|null} Segment text.
 */
export function geocoderSegment(geocoder) {
  if (!geocoder) return null;
  if (geocoder === 'Coordinates')
    return PLACED_FROM_COORDINATES.replace(/^P/, 'p');
  if (geocoder === 'Theater table') return 'preset theater';
  return `geocoded by ${geocoder}`;
}

/**
 * Info nodes for a theater slip.
 * @param {{approval, fleet, tag, table, now, rightNow?, assessment?}} ctx
 */
export function theaterInfoNodes(ctx) {
  const { approval: a, fleet = [], tag, table } = ctx;
  const p = previewOf(a) || {};
  const vehicle = reachVehicle(fleet);
  const nodes = [];
  const place = theaterPlace(a);
  const geocoder = str(p.geocoder);
  nodes.push(
    place || geocoder
      ? summaryLine([place, geocoderSegment(geocoder)], tag)
      : summaryLine(segments(a.summary), tag),
  );
  nodes.push(...whatHappens(a.consequences || []));
  nodes.push(
    h(
      'p',
      { class: 'ic-slip__console' },
      'Dry runs made before this change will be marked out of date.',
    ),
  );
  if (typeof ctx.rightNow === 'function') {
    for (const v of fleet) {
      const line = v?.name ? ctx.rightNow(v.name, v) : '';
      if (line) nodes.push(h('p', { class: 'ic-slip__console' }, line));
    }
  }
  const rows = theaterRows(p, vehicle);
  const figure = footprintNodes(p, vehicle);
  if (rows.length || figure.length) {
    nodes.push(h('h4', { class: 'ic-slip__head' }, 'The new theater'));
    nodes.push(...figure);
    if (rows.length) nodes.push(table(rows));
  }
  const na = Array.isArray(p.now_after)
    ? p.now_after.filter((r) => r && typeof r.row === 'string')
    : [];
  if (na.length) {
    nodes.push(
      table(
        na.map((r) => [
          r.row,
          [h('span', {}, String(r.now ?? ''))],
          [h('span', {}, String(r.after ?? ''))],
        ]),
        ['', 'Now', 'After'],
      ),
    );
  }
  nodes.push(...textList('Resets', p.resets));
  nodes.push(...textList('Stays', p.keeps));
  for (const c of Array.isArray(p.caveats) ? p.caveats : []) {
    if (typeof c === 'string' && c)
      nodes.push(h('p', { class: 'ic-slip__caveat' }, c));
  }
  // The console's own recheck (slip.js hands it in as `assessment.theater`).
  nodes.push(
    ...checkNodes(p.checks, tag, { now: ctx.assessment?.theater ?? null }),
  );
  nodes.push(...undoNodes(THEATER_COPY.undo(str(p.previous?.label))));
  return nodes;
}

// ---- sim speed -----------------------------------------------------------------

/**
 * "Right now ×1. Drone1 has ≈ 11 min to BINGO in sim time; at ×4 that is
 * ≈ 2 min 45 s of real time." One line per vehicle with a BINGO time.
 */
export function speedLines(fleet, from, to) {
  if (!isNum(to) || to <= 0) return [];
  const out = [];
  for (const v of Array.isArray(fleet) ? fleet : []) {
    if (!v?.name || !isNum(v.eta_to_bingo_s) || v.eta_to_bingo_s < 0) continue;
    const head = out.length || !isNum(from) ? '' : `Right now ×${from}. `;
    out.push(
      `${head}${v.name} has ≈ ${spanText(v.eta_to_bingo_s)} to BINGO in sim time; at ×${to} that is ≈ ${spanText(v.eta_to_bingo_s / to)} of real time.`,
    );
  }
  return out;
}

/**
 * Info nodes for a sim-speed slip.
 * @param {{approval, fleet, tag, table}} ctx
 */
export function timeScaleInfoNodes(ctx) {
  const { approval: a, fleet = [], tag } = ctx;
  const p = previewOf(a) || {};
  const to = isNum(a.args?.scale) ? a.args.scale : p.to;
  const from = isNum(p.from) ? p.from : null;
  const nodes = [summaryLine([isNum(to) ? `×${to}` : null], tag)];
  const caveats = Array.isArray(p.caveats) ? p.caveats : [];
  nodes.push(...whatHappens([...(a.consequences || []), ...caveats]));
  for (const line of speedLines(fleet, from, to))
    nodes.push(h('p', { class: 'ic-slip__console' }, line));
  nodes.push(...checkNodes(p.checks, tag));
  nodes.push(...undoNodes(THEATER_COPY.speedUndo(from ?? 1)));
  return nodes;
}
