/**
 * Theater table for the UAV command center.
 *
 * `mcp/godseye_uav/theaters.py` is the single source of truth and serves the
 * same rows over the bridge (`GET /theaters`, the bytes of
 * `theaters.as_payload()`). This module consumes that payload and keeps a
 * bundled copy ONLY as an offline fallback — labelled as such everywhere it
 * surfaces, because a bundled copy is exactly what drifted before: the same
 * theater id used to resolve ~118 km apart between the hand-typed copies.
 *
 * The fallback is written in the served payload's own shape and goes through
 * the same normalizer, so there is one parsing path and the offline rows can
 * never take a shape the bridge would not produce.
 */
import {
  fetchUavTheaters,
  normalizeTheaterTable,
} from '../sources/live/uav.js';

/** Demo/default flight profile, mirrored from theaters.py. */
const DEMO_ALT_M_AGL = 60;
const DEMO_SPEED_MPS = 8;
const DEMO_BOX_HALF_M = 150;
const DEFAULT_ORBIT_RADIUS_M = 150;
const SCHEMA = 'godseye.theaters/v1';
const M_PER_DEG_LAT = 111320;

/** Axis-aligned AO box, CCW from the SW corner (theaters.py `_box`). */
function box(minLat, minLon, maxLat, maxLon) {
  return [
    [minLat, minLon],
    [minLat, maxLon],
    [maxLat, maxLon],
    [maxLat, minLon],
  ];
}

function centroid(ao) {
  return [
    ao.reduce((sum, [lat]) => sum + lat, 0) / ao.length,
    ao.reduce((sum, [, lon]) => sum + lon, 0) / ao.length,
  ];
}

/** Offset (north, east) metres from a reference point (theaters.py `point_at`). */
function pointAt(ref, northM, eastM) {
  const [lat0, lon0] = ref;
  const mPerDegLon = M_PER_DEG_LAT * Math.cos((lat0 * Math.PI) / 180);
  return [lat0 + northM / M_PER_DEG_LAT, lon0 + eastM / mPerDegLon];
}

/**
 * The demo search polygon: a square centred on the AO centroid, not on home,
 * so it is inside the geofence for every theater (theaters.py `demo_box`).
 */
function demoBox(ao, halfM = DEMO_BOX_HALF_M) {
  const ref = centroid(ao);
  return [
    [-1, -1],
    [-1, 1],
    [1, 1],
    [1, -1],
  ].map(([dn, de]) => pointAt(ref, dn * halfM, de * halfM));
}

function row({ id, label, place, description, home, ao, pois }) {
  return {
    id,
    label,
    place,
    description,
    home,
    home_alt_datum: 'MSL',
    ao,
    pois: pois.map(([name, lat, lon]) => ({ name, lat, lon })),
    orbit_radius_m: DEFAULT_ORBIT_RADIUS_M,
    demo: {
      polygon: demoBox(ao),
      alt_m_agl: DEMO_ALT_M_AGL,
      speed_mps: DEMO_SPEED_MPS,
    },
  };
}

/**
 * Bundled fallback in the served payload's shape. Reconciled against
 * `mcp/godseye_uav/theaters.py`; home altitudes are metres MSL (T1).
 */
export const OFFLINE_THEATER_PAYLOAD = Object.freeze({
  schema: SCHEMA,
  default: 'default',
  alt_datum: 'MSL',
  alt_datum_note:
    'home altitudes are metres above mean sea level (T1); waypoint altitudes are metres AGL',
  theaters: [
    row({
      id: 'default',
      label: 'Redmond (AirSim default)',
      place: 'Redmond, Washington, USA',
      description:
        'Stock AirSim origin. Smoke-test AO for the one-command demo.',
      home: [47.641468, -122.140165, 122],
      ao: box(47.636468, -122.145165, 47.646468, -122.135165),
      pois: [
        ['North Field', 47.6445, -122.1402],
        ['South Field', 47.6385, -122.1402],
        ['East Field', 47.6415, -122.1372],
      ],
    }),
    row({
      id: 'iran-isfahan',
      label: 'Iran — Isfahan',
      place: 'Isfahan, Iran',
      description:
        'Urban-industrial AO: wide-area recon and pattern-of-life build-up.',
      home: [32.6546, 51.668, 1570],
      ao: box(32.63, 51.63, 32.68, 51.71),
      pois: [
        ['Isfahan North', 32.67, 51.66],
        ['Isfahan Center', 32.655, 51.67],
        ['Isfahan South', 32.64, 51.68],
      ],
    }),
    row({
      id: 'iran-natanz',
      label: 'Iran — Natanz',
      place: 'Natanz, Isfahan province, Iran',
      description:
        'Declared-facility monitoring: periodic re-look and change detection.',
      home: [33.7243, 51.7286, 1580],
      ao: box(33.705, 51.7, 33.745, 51.76),
      pois: [
        ['Natanz North', 33.735, 51.72],
        ['Natanz Center', 33.725, 51.73],
        ['Natanz South', 33.715, 51.74],
      ],
    }),
    row({
      id: 'iran-fordow',
      label: 'Iran — Fordow',
      place: 'Fordow, near Qom, Iran',
      description:
        'Hard-terrain AO: LOS-limited observation of a hillside facility.',
      home: [34.8849, 50.9958, 1550],
      ao: box(34.865, 50.965, 34.905, 51.025),
      pois: [
        ['Fordow North', 34.892, 50.99],
        ['Fordow Center', 34.885, 50.996],
        ['Fordow South', 34.878, 51.005],
      ],
    }),
    row({
      id: 'indo-pak-loc',
      label: 'Indo-Pak — Line of Control',
      place: 'Kashmir valley near Srinagar, India',
      description:
        'Mountain-valley AO: route recon and border-watch pattern-of-life.',
      home: [34.08, 74.82, 1600],
      ao: box(34.04, 74.78, 34.12, 74.86),
      pois: [
        ['LoC North', 34.105, 74.815],
        ['LoC Center', 34.08, 74.82],
        ['LoC South', 34.055, 74.825],
      ],
    }),
    row({
      id: 'taiwan-strait',
      label: 'Taiwan Strait',
      place: 'Taiwan Strait (open water west of Taichung)',
      description:
        'Maritime AO: vessel search, classification and track handoff.',
      home: [24.5, 119.5, 0],
      ao: box(24.44, 119.44, 24.56, 119.56),
      pois: [
        ['Strait North', 24.54, 119.5],
        ['Strait Center', 24.5, 119.5],
        ['Strait South', 24.46, 119.5],
      ],
    }),
    row({
      id: 'ukraine-donbas',
      label: 'Ukraine — Donbas',
      place: 'Donetsk oblast, Ukraine (Donets Ridge)',
      description:
        'Open-terrain AO: convoy detection, movement tracking, BDA re-look.',
      home: [48.6, 37.9, 250],
      ao: box(48.54, 37.84, 48.66, 37.96),
      pois: [
        ['Donbas North', 48.64, 37.9],
        ['Donbas Center', 48.6, 37.9],
        ['Donbas South', 48.56, 37.9],
      ],
    }),
    row({
      id: 'red-sea-hormuz',
      label: 'Strait of Hormuz',
      place: 'Strait of Hormuz, south of Bandar Abbas, Iran',
      description:
        'Choke-point AO: shipping-lane watch and vessel pattern-of-life.',
      home: [26.55, 56.25, 0],
      ao: box(26.5, 56.19, 26.6, 56.31),
      pois: [
        ['Hormuz North', 26.58, 56.25],
        ['Hormuz Center', 26.55, 56.25],
        ['Hormuz South', 26.52, 56.25],
      ],
    }),
  ],
});

/** The bundled fallback, normalized exactly like a served payload. */
export const OFFLINE_THEATERS = Object.freeze(
  normalizeTheaterTable(OFFLINE_THEATER_PAYLOAD),
);

/** Ordered theater rows of a normalized table. */
export function theaterList(table) {
  return (table?.order || []).map((id) => table.theaters[id]).filter(Boolean);
}

/**
 * Back-compat view of the bundled fallback in the panel's original shape.
 * Prefer `createTheaterRegistry()` — these are the offline rows only.
 */
export const THEATERS = Object.freeze(
  Object.fromEntries(
    theaterList(OFFLINE_THEATERS).map((theater) => [
      theater.id,
      Object.freeze({
        label: theater.label,
        home: theater.home,
        ao: theater.ao,
      }),
    ]),
  ),
);

/** Back-compat seed-POI view of the bundled fallback. */
export const SEED_POIS = Object.freeze(
  Object.fromEntries(
    theaterList(OFFLINE_THEATERS).map((theater) => [theater.id, theater.pois]),
  ),
);

/**
 * Describe the running theater against the operator's current selection.
 *
 * Pure, so the panel has one place to render and one place to test. Every
 * state except `ok` is something the operator must be able to SEE — a mismatch
 * that is only true in memory is how finding UI-1 reached a browser.
 *
 * The three "we do not know" states are kept apart on purpose, because the
 * operator acts differently on each: nothing published at all (an old bridge),
 * published-but-unknown with the bridge's own reason (MCP down, resource
 * unregistered — wait or restart), and a known theater this table cannot draw.
 *
 * @param {object|null} active running-theater block or null
 * @param {string} selectedId the theater id the selector currently shows
 * @returns {{state: string, text: string, warn: boolean}} one of
 *   `unpublished` | `unknown` | `not-in-table` | `mismatch` | `envelope` |
 *   `ok`, the line to render, and whether it must read as a warning
 */
export function activeTheaterNote(active, selectedId) {
  if (!active)
    return {
      state: 'unpublished',
      text: 'running theater: not published by this bridge',
      warn: false,
    };
  if (!active.known || !active.id)
    return {
      state: 'unknown',
      // The bridge is required to say WHY; without a reason an unknown theater
      // is indistinguishable from a feed nobody wired up.
      text: `running theater: unknown${active.reason ? ` — ${active.reason}` : ''}`,
      warn: false,
    };
  const name =
    active.label && active.label !== active.id
      ? `${active.label} (${active.id})`
      : active.id;
  if (!active.inTable)
    return {
      state: 'not-in-table',
      text: `RUNNING THEATER ${name} IS NOT IN THIS TABLE`,
      warn: true,
    };
  if (selectedId !== active.id)
    return {
      state: 'mismatch',
      text: `SELECTOR ≠ RUNNING THEATER — the bridge is flying ${name}`,
      warn: true,
    };
  // The server's own envelope disagreement outranks a matching selector: the
  // AO being drawn is not the AO being enforced.
  if (active.mismatch)
    return {
      state: 'envelope',
      text: `running: ${name} — SERVER REPORTS A THEATER MISMATCH: its enforced envelope belongs to another theater`,
      warn: true,
    };
  return { state: 'ok', text: `running: ${name}`, warn: false };
}

/**
 * Registry that prefers the served table and falls back to the bundled copy.
 *
 * It also learns which theater the bridge is RUNNING, which is a different
 * question from which theater the table defaults to. `loadActive` is the
 * secondary probe (`/health`) used only when the served table published no
 * active theater; pass `null` and the served table becomes the only source,
 * which `active()` then reports as "not published" rather than guessing.
 *
 * @param {object} [options]
 * @param {(opts: object) => Promise<object|null>} [options.load] table fetcher
 * @param {(opts: object) => Promise<object|null>} [options.loadActive] running
 *   theater fetcher, used only when the table itself publishes none
 * @returns {object} registry with `table`, `origin`, `originLabel`, `active`,
 *   `refresh`
 */
export function createTheaterRegistry({
  load = fetchUavTheaters,
  loadActive = null,
} = {}) {
  let table = OFFLINE_THEATERS;
  let origin = 'offline';
  let active = null;
  let asked = false;
  return {
    table: () => table,
    origin: () => origin,
    isOffline: () => origin !== 'bridge',
    list: () => theaterList(table),
    get: (id) => table.theaters[id] || table.theaters[table.defaultId] || null,
    /** The theater the bridge is flying, or null when it publishes none. */
    active: () => active,
    /** Whether the bridge has been asked yet — "not asked" is not "nothing". */
    asked: () => asked,
    originLabel: () =>
      origin === 'bridge'
        ? `theaters: bridge · ${table.schema}`
        : 'theaters: OFFLINE FALLBACK — bundled copy, may drift from the bridge',
    /** Try the served table once; a failure silently keeps the fallback. */
    async refresh({ signal } = {}) {
      let served = null;
      try {
        served = await load({ signal });
      } catch {
        served = null;
      }
      asked = true;
      if (served?.order?.length) {
        table = served;
        origin = 'bridge';
      }
      // The table first, `/health` only if the table said NOTHING. An explicit
      // `known: false` is an answer — both routes republish one block, so
      // re-asking `/health` would only fetch the same unknown twice.
      let running = served?.active ?? null;
      if (!running && typeof loadActive === 'function') {
        try {
          running = await loadActive({ signal });
        } catch {
          running = null;
        }
      }
      // `inTable` is re-decided against the table in force, which may not be
      // the one the block was graded against.
      const grade = (block) => ({
        ...block,
        label: block.label || table.theaters[block.id]?.label || block.id,
        inTable: Boolean(block.known && table.theaters[block.id]),
      });
      if (running) active = grade(running);
      else if (active) active = grade(active);
      // ...and when nothing new was learned, what we already knew STANDS. A
      // bridge blip must not blank a theater we were told about, exactly as a
      // failed table load keeps the table we already have.
      return { table, origin, active };
    },
  };
}
