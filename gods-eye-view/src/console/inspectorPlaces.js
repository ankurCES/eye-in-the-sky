/**
 * Inspector bodies for places and for items the console doesn't recognise:
 * - the theater (WG §4.2.5): Place, Centre, Area, Bounds, Home, Ground,
 *   Airframe, Source, Geocoder and query, Previous, Active;
 * - a mapped site (WG §4.2.6): context only. Category, Name, Coordinates,
 *   Tags, Source, How we know, Near. A site never gets an engagement, strike
 *   or control action: its actions are Ask about this, Focus, Show on map
 *   and Plan recce over this, like the theater's;
 * - an unrecognised node type (WG §4.2.1): its fields as text and a fixed
 *   line saying unknown is not safe.
 *
 * Every place string (OSM names and tags, geocoder labels, theater labels) is
 * untrusted (§0.2, §3.11): it is bidi-stripped and reaches the DOM only as
 * text through uavDom `h()`, never as markup.
 *
 * inspector.js owns the plate and hands its small builders in as `kit`:
 * `{field, fields, mono, caption, chip, chips, section, isOpen, toggle,
 * now}`. The rest comes from the rail's formatting kit (situation.js), so a
 * theater reads the same in the rail and here.
 */
import { h } from '../ui/uavDom.js';
import { siteCategoryKey } from './orb/glyphPaths.js';
import {
  HOME_AO_CENTRE_TEXT,
  PLACED_FROM_COORDINATES,
  SITE_CAVEAT,
  SITE_PROTECTED_TEXT,
  SITE_STATUS_TEXT,
  SITES_DEGRADED_TEXT,
  areaText,
  homeRegister,
  safeText,
  siteWord,
} from './orb/placeText.js';
import { UNRECOGNISED_ITEM_LINE, unrecognisedItemTitle } from './orb/text.js';
import {
  bboxOf,
  coordText,
  homeText,
  humanize,
  latLonOf,
  noReading,
  num,
  registerTag,
  showOnMapRequest,
  theaterSetLine,
  zulu,
} from './situation.js';

/** Site tags shown before "Show all {n} tags" (WG §4.2.6). */
export const SITE_TAGS_SHOWN = 6;
/** How far "Near" looks around a site (intel_sites.NEAR_SITE_M). */
export const SITE_NEAR_M = 1000;
export const SITE_WORD = 'Site';

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/** `[[id|label]]` chip markup with markup-breaking characters removed. */
export function placeMarkup(id, label) {
  const clean = safeText(label, 80)
    .replace(/[[\]|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return clean ? `[[${id}|${clean}]]` : `[[${id}]]`;
}

/**
 * The "Plan recce over this" prefill (WG §4.2.5): drafted into the
 * composer, never sent.
 */
export function recceText(id, label) {
  return `Plan a route recon over ${placeMarkup(id, label)} and show me the dry run.`;
}

/** Whether an id or node is a place with the recce and map actions. */
export function isPlaceType(type) {
  return type === 'theater' || type === 'site';
}

/** "Airfield (aeroway=aerodrome)"; the word alone without a subtype. */
export function siteCategoryLine(category, subtype) {
  const word = siteWord(category);
  const tag = safeText(subtype, 80);
  return tag ? `${word} (${tag})` : word;
}

/** "OpenStreetMap contributors, ODbL. Fetched 14:01Z via Overpass." */
export function siteSourceLine(fetchedAtMs) {
  const at = num(fetchedAtMs);
  return at != null
    ? `OpenStreetMap contributors, ODbL. Fetched ${zulu(at)} via Overpass.`
    : 'OpenStreetMap contributors, ODbL. Fetched via Overpass.';
}

/**
 * A site's tags as `[key, value]` text pairs: the entity's full whitelist
 * when it has loaded, else the graph node's (which may be trimmed), never
 * repeating `name`. Keys and values are bidi-safe.
 */
export function siteTagPairs(node, entity) {
  const full = entity?.fields?.tags;
  const src =
    full && typeof full === 'object'
      ? full
      : node?.attrs?.tags && typeof node.attrs.tags === 'object'
        ? node.attrs.tags
        : {};
  const out = [];
  for (const [k, v] of Object.entries(src)) {
    if (v == null || typeof v === 'object') continue;
    const key = safeText(k, 40);
    if (!key || key === 'name') continue;
    out.push([key, safeText(v, 160)]);
  }
  return out;
}

/**
 * "OpenStreetMap has 14 tags for this site; the console keeps 3." when the
 * server kept fewer tags than OSM has (it keeps a whitelist and sends
 * `tags_total`), else null. Only from loaded details: a graph node's tags
 * may have been trimmed for size.
 */
export function siteTagsCutLine(entity) {
  const f = entity?.fields;
  const tags = f?.tags;
  const total = num(f?.tags_total);
  if (!tags || typeof tags !== 'object' || total == null) return null;
  const kept = Object.values(tags).filter(
    (v) => v != null && typeof v !== 'object',
  ).length;
  return total > kept
    ? `OpenStreetMap has ${total} tags for this site; the console keeps ${kept}.`
    : null;
}

/** The short tag a search row shows for a site ("ICAO OIFM"), or ''. */
export function siteKeyTag(node) {
  const tags = node?.attrs?.tags;
  if (!tags || typeof tags !== 'object') return '';
  for (const key of ['icao', 'iata']) {
    const value = safeText(tags[key], 12);
    if (value) return `${key.toUpperCase()} ${value}`;
  }
  return '';
}

/** Whether a site is protected (medical), from the node or its details. */
export function siteIsProtected(node, entity) {
  const cat = siteCategoryKey(
    node?.attrs?.category ?? entity?.fields?.category,
  );
  return (
    node?.attrs?.protected === true ||
    entity?.fields?.protected === true ||
    cat === 'medical'
  );
}

/**
 * The header words for a site: the category word and whether "Protected"
 * shows (WG §4.2.6). The status is always "Mapped, not verified".
 */
export function siteHeader(node, entity) {
  const category = node?.attrs?.category ?? entity?.fields?.category;
  return {
    category: siteCategoryKey(category),
    categoryWord: siteWord(category),
    status: SITE_STATUS_TEXT,
    protected: siteIsProtected(node, entity),
  };
}

/**
 * The notice for a site the map draws but the graph leaves out (the orb
 * keeps its most salient sites; the overlay serves them all): "On the map
 * only: not among the {n} sites the orb shows." Null for anything else,
 * including a site of another theater, which keeps "Not in the current
 * picture". Reads `fields.in_graph` and `fields.theater` (intel_sites).
 * @param {object|null} entity the loaded details
 * @param {object|null} graph the intel graph
 * @returns {string|null}
 */
export function siteOffGraphLine(entity, graph) {
  const f = entity?.fields;
  const isSite =
    entity?.type === 'site' || String(entity?.id ?? '').startsWith('sit:');
  if (!isSite || !f || f.in_graph !== false) return null;
  const active = graph?.theater?.id;
  if (f.theater != null && active != null && f.theater !== active) return null;
  const n = num(graph?.meta?.sites?.in_graph);
  return n != null && n > 0
    ? `On the map only: not among the ${n} sites the orb shows.`
    : 'On the map only: not among the sites the orb shows.';
}

/** The bbox of an AO polygon `[[lat, lon], ...]`, or null. */
export function polygonBbox(ao) {
  if (!Array.isArray(ao) || ao.length < 3) return null;
  let [s, w, n, e] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const p of ao) {
    const pt = latLonOf(p);
    if (!pt) return null;
    s = Math.min(s, pt[0]);
    n = Math.max(n, pt[0]);
    w = Math.min(w, pt[1]);
    e = Math.max(e, pt[1]);
  }
  return bboxOf([s, w, n, e]);
}

/** Great-circle metres between two `[lat, lon]` pairs. */
function metresBetween(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad;
  const dLon = (b[1] - a[1]) * rad;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(s)));
}

/**
 * One theater's facts, whatever the source: the active theater's §3.2 block
 * (`graph.theater`, whose keys the active `thr:` node also carries), else
 * the static table row an inactive theater's details carry (`fields.place`,
 * `fields.home` as `[lat, lon, alt_msl]`, `fields.ao`). A missing fact is
 * null, and its row is hidden.
 */
export function theaterFacts(node, entity, graph) {
  const id = String(node?.id ?? entity?.id ?? '').replace(/^thr:/, '');
  const f = entity?.fields || {};
  const attrs = node?.attrs || {};
  const active = graph?.theater?.id === id ? graph.theater : null;
  const b = { ...attrs, ...(active || {}) };
  const dynamic =
    typeof b.dynamic === 'boolean'
      ? b.dynamic
      : typeof f.dynamic === 'boolean'
        ? f.dynamic
        : null;
  const row = Array.isArray(f.home) ? f.home : null;
  const tableHome =
    row && num(row[0]) != null && num(row[1]) != null
      ? {
          lat: num(row[0]),
          lon: num(row[1]),
          alt_msl_m: num(row[2]),
          name: null,
          source: dynamic ? null : 'preset',
        }
      : null;
  const home = b.home && typeof b.home === 'object' ? b.home : tableHome;
  const bbox = bboxOf(b.bbox) ?? polygonBbox(f.ao);
  const nodePoint = latLonOf([node?.lat, node?.lon]);
  const center =
    latLonOf(b.center) ??
    nodePoint ??
    (bbox ? [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2] : null);
  return {
    id,
    label: b.label ?? node?.label ?? entity?.label ?? f.label ?? null,
    place: b.place ?? f.place ?? null,
    active: Boolean(active) || attrs.active === true || f.active === true,
    dynamic,
    source:
      b.source === 'chat' || b.source === 'preset'
        ? b.source
        : dynamic === true
          ? 'chat'
          : dynamic === false
            ? 'preset'
            : null,
    bbox,
    center,
    half_extent_m: num(b.half_extent_m),
    area_km2: num(b.area_km2),
    home,
    ground_msl_m: num(b.ground_msl_m) ?? num(home?.alt_msl_m),
    ground_source: typeof b.ground_source === 'string' ? b.ground_source : null,
    airframe: b.airframe && typeof b.airframe === 'object' ? b.airframe : null,
    geocoder: typeof b.geocoder === 'string' ? b.geocoder : null,
    query: typeof b.query === 'string' ? b.query : null,
    set_at_ms: num(b.set_at_ms),
    set_via: typeof b.set_via === 'string' ? b.set_via : null,
    previous: b.previous && typeof b.previous === 'object' ? b.previous : null,
    integrity_error: b.integrity_error || null,
    epoch: Number.isInteger(b.epoch) ? b.epoch : null,
  };
}

/** "Kherson grass strip  46.63512, 32.61670  0.3 km from the centre". */
function homeKids(kit, t) {
  const home = t.home;
  const where = coordText(home?.lat, home?.lon);
  // An AO-centre home is no place of its own: say what it is.
  const name =
    safeText(home?.name, 80) ||
    (home?.source === 'ao-centre' ? HOME_AO_CENTRE_TEXT : '');
  if (!where && !name) return null;
  const kids = [];
  if (name) kids.push(h('span', { class: 'ic-inspector__home-name' }, name));
  if (where) kids.push(' ', kit.mono(where));
  const pt = latLonOf([home?.lat, home?.lon]);
  if (pt && t.center) {
    const km = metresBetween(pt, t.center) / 1000;
    kids.push(' ', `${km.toFixed(1)} km from the centre`);
  }
  const register = homeRegister(home?.source);
  if (register) kids.push(registerTag(register));
  return kids;
}

/** "Quad, small electric, reach 7.4 km". */
export function airframeText(airframe) {
  if (!airframe || typeof airframe !== 'object') return '';
  const label = safeText(airframe.label, 60) || humanize(airframe.id);
  const reach = num(airframe.reach_m);
  return reach != null && reach > 0
    ? `${label}, reach ${(Math.round(reach / 100) / 10).toFixed(1)} km`
    : label;
}

/**
 * `Photon (OpenStreetMap), query "Kherson"`. Typed coordinates were never
 * geocoded: "Placed from coordinates, not geocoded", as the slip and the
 * rail say.
 */
export function geocoderText(t) {
  const name = safeText(t?.geocoder, 60);
  const query = safeText(t?.query, 120);
  if (name === 'Coordinates') return PLACED_FROM_COORDINATES;
  if (!name && !query) return '';
  if (!query) return name;
  return name ? `${name}, query "${query}"` : `Query "${query}"`;
}

/** Whether the geocoder row is a lookup on the map (Mapped), not a placement. */
function geocoded(t) {
  const name = safeText(t?.geocoder, 60);
  return Boolean(name) && name !== 'Coordinates' && name !== 'Theater table';
}

/**
 * The theater inspector body (WG §4.2.5 table). Registers: Centre is
 * Requested for a chat theater and Measured for a preset; Area and Bounds
 * are Requested; Home by its source (placeText.HOME_SOURCE_REGISTER, shared
 * with the slip); Ground is Estimated; a geocoder lookup is Mapped, typed
 * coordinates have no register.
 */
export function theaterBody(kit, node, entity, graph) {
  const t = theaterFacts(node, entity, graph);
  const chat = t.source === 'chat' || t.dynamic === true;
  const out = [];
  if (t.integrity_error)
    out.push(
      h(
        'p',
        { class: 'ic-inspector__line', 'data-status': 'critical' },
        safeText(t.integrity_error, 240),
      ),
    );
  const rows = [];
  const place = safeText(t.place, 160);
  if (place) rows.push(kit.field('Place', place));
  if (t.center)
    rows.push(
      kit.field(
        'Centre',
        kit.mono(coordText(t.center[0], t.center[1])),
        registerTag(chat ? 'requested' : 'measured'),
      ),
    );
  const area = areaText(t, { withArea: true });
  if (area) rows.push(kit.field('Area', area, registerTag('requested')));
  if (t.bbox)
    rows.push(
      kit.field(
        'Bounds',
        kit.mono(t.bbox.map((v) => v.toFixed(5)).join(', ')),
        registerTag('requested'),
      ),
    );
  const home = homeKids(kit, t);
  if (home) rows.push(kit.field('Home', ...home));
  if (t.ground_msl_m != null)
    rows.push(
      kit.field(
        'Ground',
        `≈ ${Math.round(t.ground_msl_m)} m above sea level`,
        registerTag('estimated'),
        t.ground_source ? kit.caption(safeText(t.ground_source, 200)) : null,
      ),
    );
  const af = airframeText(t.airframe);
  if (af) rows.push(kit.field('Airframe', af));
  const set = theaterSetLine(t);
  if (set) rows.push(kit.field('Source', set));
  const geo = chat ? geocoderText(t) : '';
  if (geo)
    rows.push(
      kit.field(
        'Geocoder and query',
        geo,
        geocoded(t) ? registerTag('mapped') : null,
      ),
    );
  const prevId =
    typeof t.previous?.id === 'string' && t.previous.id ? t.previous.id : null;
  if (prevId)
    rows.push(
      kit.field(
        'Previous',
        kit.chips([
          kit.chip(
            `thr:${prevId}`,
            safeText(t.previous.label, 80) || prevId,
            'theater',
          ),
        ]),
      ),
    );
  rows.push(kit.field('Active', t.active ? 'Yes' : 'No'));
  out.push(kit.fields(...rows));
  return out;
}

/** A near row's chips: `[{id, label, distance_m}]` → id chips with distances. */
function nearChips(kit, rows, type) {
  const list = (Array.isArray(rows) ? rows : [])
    .filter((r) => r && typeof r.id === 'string' && r.id)
    .slice(0, 20);
  return list.length
    ? kit.chips(list.map((r) => kit.chip(r.id, safeText(r.label, 80), type)))
    : null;
}

/**
 * The site inspector body (WG §4.2.6): Category, Name (+ `name:en`),
 * Coordinates, Tags (six, then "Show all {n} tags", and how many OSM tags
 * the server did not keep), Source, How we know, and Near. Context only: nothing here offers an engagement, and nothing
 * reads a damage or control field.
 */
export function siteBody(kit, node, entity) {
  const f = entity?.fields || {};
  const a = node?.attrs || {};
  const prov = entity?.provenance || {};
  const out = [];
  const rows = [
    kit.field(
      'Category',
      siteCategoryLine(a.category ?? f.category, f.subtype ?? a.subtype),
      registerTag('mapped'),
    ),
  ];
  // `fields.name` is the OSM name; before the details load, the node label
  // (which reads "Unnamed …" for a site with no name) stands in.
  const name = safeText(entity ? f.name : node?.label, 160);
  const nameEn = safeText(f.name_en ?? f.tags?.['name:en'], 160);
  if (name || nameEn) {
    const kids = [name || nameEn];
    if (name && nameEn && nameEn !== name)
      kids.push(
        ' ',
        h('span', { class: 'ic-inspector__alt-name' }, `(${nameEn})`),
      );
    rows.push(kit.field('Name', ...kids));
  }
  const where = coordText(f.lat ?? node?.lat, f.lon ?? node?.lon);
  if (where) rows.push(kit.field('Coordinates', kit.mono(where)));
  const tags = siteTagPairs(node, entity);
  // The server keeps a tag whitelist: say when OSM has more than it kept.
  const cut = siteTagsCutLine(entity);
  if (tags.length || cut) {
    const open = kit.isOpen?.('site:tags') === true;
    const shown = open ? tags : tags.slice(0, SITE_TAGS_SHOWN);
    const kids = [
      h(
        'ul',
        { class: 'ic-kit-list ic-inspector__tags', hidden: !tags.length },
        ...shown.map(([k, v]) =>
          h(
            'li',
            { class: 'ic-inspector__tag' },
            h('span', { class: 'ic-kit-mono ic-inspector__tag-key' }, k),
            ' ',
            h('span', { class: 'ic-inspector__tag-value' }, v),
          ),
        ),
      ),
    ];
    if (tags.length > SITE_TAGS_SHOWN) {
      const btn = h(
        'button',
        {
          type: 'button',
          class: 'ic-kit-btn ic-kit-btn--link',
          'aria-expanded': open ? 'true' : 'false',
          'data-key': 'site:tags',
        },
        open ? 'Show fewer tags' : `Show all ${tags.length} tags`,
      );
      btn.addEventListener('click', () => kit.toggle?.('site:tags'));
      kids.push(btn);
    }
    if (cut) kids.push(kit.caption(cut));
    rows.push(
      kit.field('Tags', h('div', { class: 'ic-inspector__stack' }, ...kids)),
    );
  }
  rows.push(
    kit.field(
      'Source',
      siteSourceLine(f.fetched_at_ms ?? a.fetched_at_ms ?? prov.fetched_at_ms),
    ),
  );
  const how = [h('span', {}, SITE_CAVEAT)];
  if (prov.degraded === true) {
    how.push(
      h(
        'span',
        { class: 'ic-inspector__line', 'data-status': 'warn' },
        SITES_DEGRADED_TEXT,
      ),
    );
    const why = safeText(prov.reason, 200);
    if (why) how.push(kit.caption(why));
  }
  rows.push(
    kit.field(
      'How we know',
      h('div', { class: 'ic-inspector__stack' }, ...how),
    ),
  );
  out.push(kit.fields(...rows));
  const near = f.near && typeof f.near === 'object' ? f.near : null;
  if (near) {
    const radius = num(near.radius_m) ?? SITE_NEAR_M;
    const contacts = nearChips(kit, near.contacts, 'track');
    const pois = nearChips(kit, near.pois, 'poi');
    const km = Number((radius / 1000).toFixed(1));
    out.push(
      kit.section(
        'Near',
        contacts
          ? [kit.caption(`Contacts within ${km} km`), contacts]
          : h(
              'p',
              { class: 'ic-inspector__line' },
              `No contacts within ${km} km.`,
            ),
        pois ? [kit.caption('Places'), pois] : null,
      ),
    );
  }
  return out;
}

/** A bounded text form of any field value, for the unrecognised body. */
function valueText(value) {
  if (value == null) return '';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number')
    return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'string') return safeText(value, 240);
  let text = '';
  try {
    text = JSON.stringify(value);
  } catch {
    text = '';
  }
  return safeText(text, 240);
}

/** The header word for a type the console doesn't know: "Unrecognised item (force)". */
export function unknownTitle(type) {
  return unrecognisedItemTitle(type);
}

/**
 * The body for a node type the console doesn't know (WG §4.2.1): the fixed
 * line, then every field it carries as a definition list, as text.
 */
export function unknownBody(kit, node, entity) {
  const merged = {
    ...(node?.attrs && typeof node.attrs === 'object' ? node.attrs : {}),
    ...(entity?.fields && typeof entity.fields === 'object'
      ? entity.fields
      : {}),
  };
  const where = coordText(node?.lat, node?.lon);
  const rows = [];
  if (where) rows.push(kit.field('Coordinates', kit.mono(where)));
  for (const [key, value] of Object.entries(merged).slice(0, 40)) {
    const text = valueText(value);
    if (!text) continue;
    rows.push(kit.field(safeText(key, 40) || 'Field', text));
  }
  return [
    h(
      'p',
      {
        class: 'ic-inspector__line ic-inspector__unrecognised',
        'data-status': 'unknown',
      },
      UNRECOGNISED_ITEM_LINE,
    ),
    rows.length ? kit.fields(...rows) : noReading('No fields reported.'),
  ];
}

/**
 * The operator's Show on map for a theater (its bbox) or a site (its bounds,
 * else a 1 km box on its point), as a `map:request` payload; null without a
 * location.
 */
export function placeMapRequest(id, node, entity, graph) {
  const type = node?.type ?? entity?.type;
  if (type === 'theater') {
    const t = theaterFacts(node, entity, graph);
    return showOnMapRequest(id, {
      bbox: t.bbox,
      lat: t.center?.[0],
      lon: t.center?.[1],
      label: t.label,
    });
  }
  if (type === 'site') {
    const f = entity?.fields || {};
    return showOnMapRequest(id, {
      bbox: node?.attrs?.bounds ?? f.bounds,
      lat: f.lat ?? node?.lat,
      lon: f.lon ?? node?.lon,
      label: node?.label ?? entity?.label,
    });
  }
  return null;
}

export { SITE_PROTECTED_TEXT, SITE_STATUS_TEXT };
