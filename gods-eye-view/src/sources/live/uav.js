/**
 * UAV live source: polls the godSeye telemetry bridge /snapshot REST and
 * normalizes it to the GEV live-source contract (WGS84 degrees, metres,
 * m/s, Unix ms). Loopback-only bridge, Bearer token from injected config.
 *
 * Sections follow BRIDGE_CONTRACT.md: `vehicles[]` -> records, `missions[]`
 * -> missions, `contacts[]` -> contacts, `/tracks` -> targets and the SSE
 * `/events` channel -> alarms. Every section beyond `vehicles[]` is optional:
 * a bridge that has not shipped it yet degrades the snapshot (the `sections`
 * flags say which feeds were actually served) and never throws. Optional
 * ROUTES additionally back off, so an absent one costs one request per backoff
 * window rather than one per poll.
 *
 * `/theaters` and `/health` also carry the RUNNING theater when the bridge
 * publishes it; `normalizeActiveTheater` reads it and reports its absence
 * rather than substituting the table's default (finding UI-1).
 *
 * Browser globals are forbidden here. Transport handles that only the browser
 * owns — the `EventSource` constructor above all — are injected by the caller.
 */
import {
  LiveSourceError,
  admitRecords,
  cleanText,
  coordinates,
  epoch,
  finite,
} from './contract.js';

const DEFAULT_BASE = 'http://localhost:8790';
const DEFAULT_TOKEN = 'dev-token';

/** First and maximum backoff for the OPTIONAL `/tracks` route. */
const TRACKS_RETRY_MS = 5000;
const TRACKS_MAX_RETRY_MS = 60000;

// Config is injected by the app layer (browser-global access is forbidden
// inside sources/live). Callers may pass baseUrl/token or a config provider.
let runtime = {
  baseUrl: DEFAULT_BASE,
  token: DEFAULT_TOKEN,
  eventSourceFactory: null,
};

/**
 * Configure the bridge transport.
 * @param {object} [config]
 * @param {string} [config.baseUrl] bridge origin, default http://localhost:8790
 * @param {string} [config.token] Bearer token for the bridge
 * @param {(url: string) => object} [config.eventSourceFactory] constructs the
 *   SSE client for `/events`; omitted means alarms are unsupported, never fatal
 */
export function configureUavSource({
  baseUrl,
  token,
  eventSourceFactory,
} = {}) {
  runtime = {
    baseUrl: baseUrl || DEFAULT_BASE,
    token: token || DEFAULT_TOKEN,
    eventSourceFactory:
      typeof eventSourceFactory === 'function' ? eventSourceFactory : null,
  };
}

/** Resolve an override that may be a literal or a late-bound getter. */
function resolve(override) {
  const value = typeof override === 'function' ? override() : override;
  return typeof value === 'string' && value ? value : null;
}

function baseUrl(override) {
  return (
    resolve(override) ||
    import.meta.env?.VITE_UAV_BRIDGE_URL ||
    runtime.baseUrl ||
    DEFAULT_BASE
  );
}

function token(override) {
  return (
    resolve(override) ||
    import.meta.env?.VITE_UAV_BRIDGE_TOKEN ||
    runtime.token ||
    DEFAULT_TOKEN
  );
}

async function getJson(path, { signal, origin, secret } = {}) {
  const base = baseUrl(origin);
  let response;
  try {
    response = await fetch(`${base}${path}`, {
      signal,
      headers: { Authorization: `Bearer ${token(secret)}` },
    });
  } catch (error) {
    // A cancelled poll is not an outage: let the abort through so the layer's
    // update loop can tell "we stopped asking" from "the bridge is down".
    if (error?.name === 'AbortError') throw error;
    throw new LiveSourceError(
      'unavailable',
      `UAV bridge unreachable at ${base}`,
      { source: 'uav', retryAfterMs: 5000 },
    );
  }
  if (!response.ok)
    throw new LiveSourceError('http', `UAV bridge HTTP ${response.status}`, {
      status: response.status,
      source: 'uav',
    });
  return response.json();
}

/** Percentages are reported 0-100; anything else is an unknown, not a zero. */
function percent(value) {
  const number = finite(value);
  if (number == null) return null;
  return Math.min(100, Math.max(0, number));
}

function oneOf(value, allowed, fallback = '') {
  const text = cleanText(value).toLowerCase();
  return allowed.includes(text) ? text : fallback;
}

/**
 * Admit an optional snapshot section. Unlike `admitRecords` an all-invalid
 * section is empty rather than fatal: mission and contact rows must never be
 * able to blank the vehicle telemetry the operator is flying on.
 */
function admitSection(rows, normalize) {
  if (!Array.isArray(rows)) return { items: [], served: false, rejected: 0 };
  const items = [];
  const ids = new Set();
  for (const row of rows) {
    const item = normalize(row);
    if (item && !ids.has(item.id)) {
      items.push(item);
      ids.add(item.id);
    }
  }
  return { items, served: true, rejected: rows.length - items.length };
}

function normalizeVehicle(row) {
  const latitude = finite(row?.latitude);
  const longitude = finite(row?.longitude);
  if (!coordinates(latitude, longitude)) return null;
  const altHae = finite(row?.alt_hae);
  const reference = String(row?.name ?? 'uav');
  return {
    id: reference,
    reference,
    label: String(row?.name ?? 'UAV'),
    kind: 'uav',
    position: {
      latitude,
      longitude,
      // Contract separates barometric and ellipsoid altitude; we carry altHae.
      altitude: altHae,
      ellipsoidAltitude: altHae,
      barometricAltitude: finite(row?.alt_msl),
      // Orthometric height is its own observation: the contract's barometric
      // slot keeps the legacy mapping, mslAltitude names what alt_msl is.
      mslAltitude: finite(row?.alt_msl),
      agl: finite(row?.agl),
    },
    velocity: {
      speed: finite(row?.speed_ms),
      heading: finite(row?.heading_deg),
      verticalRate: finite(row?.vz) != null ? -finite(row?.vz) : null,
    },
    attitude: {
      pitch: finite(row?.pitch_deg),
      roll: finite(row?.roll_deg),
    },
    status: {
      landedState: finite(row?.landed_state),
      armed: row?.armed === true,
      fuelPct: percent(row?.fuel_pct),
      bingoFuelPct: percent(row?.bingo_fuel_pct),
      etaToBingoS: finite(row?.eta_to_bingo_s),
      mission: typeof row?.mission === 'string' ? row.mission : '',
      trackId: typeof row?.track_id === 'string' ? row.track_id : '',
      // Never hidden: a degraded geoid source is operator-visible state.
      datumDegraded: row?.datum_degraded === true,
    },
    observedAtMs: epoch(row?.timestamp_ms) ?? Date.now(),
  };
}

/** Mission phases the contract defines; anything else reports as unknown. */
export const UAV_MISSION_PHASES = Object.freeze([
  'planning',
  'executing',
  'rtb',
  'complete',
  'aborted',
]);

/** Contact confidence levels the contract defines (M13 evidence grading). */
export const UAV_CONTACT_CONFIDENCE = Object.freeze([
  'confirmed',
  'probable',
  'possible',
]);

/**
 * Normalize one `/snapshot.missions[]` row (BRIDGE_CONTRACT missions[]).
 * @param {object} row raw bridge row
 * @returns {object|null} normalized mission, or null when unidentifiable
 */
export function normalizeMission(row) {
  const id = cleanText(row?.mission_id);
  if (!id) return null;
  const waypoint = row?.waypoint ?? {};
  const safety = row?.safety ?? {};
  return {
    id,
    missionId: id,
    vehicle: cleanText(row?.vehicle),
    kind: cleanText(row?.kind),
    phase: oneOf(row?.phase, UAV_MISSION_PHASES, 'unknown'),
    activeTool: cleanText(row?.active_tool),
    progressPct: percent(row?.progress_pct),
    waypoint: {
      index: finite(waypoint.index),
      of: finite(waypoint.of),
    },
    etaS: finite(row?.eta_s),
    fuelPct: percent(row?.fuel_pct),
    bingoFuelPct: percent(row?.bingo_fuel_pct),
    // Coverage is flown ground, never planned ground (M1).
    coveragePct: percent(row?.coverage_pct),
    safety: {
      geofence: cleanText(safety.geofence) || 'unknown',
      proximityM: finite(safety.proximity_m),
      bingoLatched: safety.bingo_latched === true,
    },
    incompleteReason: cleanText(row?.incomplete_reason) || null,
  };
}

/**
 * Normalize one `/snapshot.contacts[]` row (BRIDGE_CONTRACT contacts[]).
 * `location` is the contact's own position, never the observer's.
 * @param {object} row raw bridge row
 * @returns {object|null} normalized contact, or null without a track id
 */
export function normalizeContact(row) {
  const trackId = cleanText(row?.track_id);
  if (!trackId) return null;
  const location = row?.location ?? {};
  const latitude = finite(location.lat);
  const longitude = finite(location.lon);
  const salute = row?.salute ?? {};
  return {
    id: trackId,
    trackId,
    category: cleanText(row?.category),
    confidence: oneOf(row?.confidence, UAV_CONTACT_CONFIDENCE, 'unknown'),
    threatLevel: cleanText(row?.threat_level).toLowerCase() || 'unknown',
    position: coordinates(latitude, longitude)
      ? { latitude, longitude, altitude: finite(location.alt_m) }
      : null,
    lastSeenAtMs: epoch(row?.last_seen_ms),
    salute: {
      size: cleanText(salute.size),
      activity: cleanText(salute.activity),
      location: cleanText(salute.location),
      unit: cleanText(salute.unit),
      time: cleanText(salute.time),
      equipment: cleanText(salute.equipment),
    },
  };
}

/**
 * Create the UAV live source.
 *
 * A caller that already knows which bridge it is talking to (the mission panel
 * resolves one from operator settings) passes its own origin here, so the
 * telemetry it renders and the `/control/*` calls it makes can never end up
 * pointing at two different bridges. Omit both and the module-level
 * configuration applies, which is what the globe layer uses.
 *
 * @param {object} [options]
 * @param {string|(() => string)} [options.baseUrl] bridge origin, or a getter
 * @param {string|(() => string)} [options.token] bearer token, or a getter
 * @param {() => number} [options.now] clock, injected by tests
 * @param {number} [options.tracksRetryMs] first `/tracks` backoff delay
 * @param {number} [options.tracksMaxRetryMs] `/tracks` backoff ceiling
 * @returns {{label: string, getSnapshot: Function}} the live source
 */
export function createUavSource({
  baseUrl: origin,
  token: secret,
  now = Date.now,
  tracksRetryMs = TRACKS_RETRY_MS,
  tracksMaxRetryMs = TRACKS_MAX_RETRY_MS,
} = {}) {
  // `/tracks` (M11) is an OPTIONAL route, and the poll loop runs at up to 5 Hz:
  // asking on every snapshot meant a bridge without the route was hammered with
  // 404s forever, exactly like the other feeds before they were given backoff.
  // Request state belongs to this factory instance, never to the module.
  const tracks = {
    served: false, // has this bridge ever answered /tracks?
    rows: [], // the last rows it served
    observedAtMs: null,
    attempts: 0,
    nextAtMs: 0,
    stale: false, // rows are held, not refreshed
  };

  return {
    label: 'godSeye UAV (AirSim)',
    async getSnapshot(query = {}, { signal } = {}) {
      const body = await getJson('/snapshot', { signal, origin, secret });
      const observedAtMs = epoch(body?.observedAtMs) ?? Date.now();
      const admitted = admitRecords(
        body?.vehicles ?? [],
        normalizeVehicle,
        'uav',
      );
      // Mission state (§3.1 feed row 3) and the contact roster (feed row 5)
      // ship after the vehicle feed; an absent section is a degraded snapshot.
      const missions = admitSection(body?.missions, normalizeMission);
      const contacts = admitSection(body?.contacts, normalizeContact);
      // Persistent target tracks (M11) — best-effort, backed off, and NEVER
      // blanked by a poll we chose not to make: an empty `targets` has to mean
      // "the bridge served no tracks", not "we are waiting out a backoff".
      const at = now();
      const polled = at >= tracks.nextAtMs;
      if (polled) {
        try {
          const tk = await getJson('/tracks', { signal, origin, secret });
          tracks.rows = Array.isArray(tk?.tracks) ? tk.tracks : [];
          tracks.observedAtMs = at;
          tracks.served = true;
          tracks.attempts = 0;
          tracks.nextAtMs = 0;
          tracks.stale = false;
        } catch (error) {
          if (error?.name === 'AbortError') throw error;
          tracks.nextAtMs =
            at +
            Math.min(tracksMaxRetryMs, tracksRetryMs * 2 ** tracks.attempts);
          tracks.attempts += 1;
          tracks.stale = true;
        }
      }
      const datumDegraded =
        body?.datum_degraded === true ||
        admitted.records.some(
          (record) => record.status?.datumDegraded === true,
        );
      return {
        source: 'uav',
        label: 'godSeye UAV (AirSim)',
        status: body?.sim_state?.startsWith('up') ? 'ok' : 'degraded',
        simState: body?.sim_state ?? 'unknown',
        stale: body?.sim_state?.startsWith('up') !== true,
        observedAtMs,
        freshness: Date.now() - observedAtMs,
        count: admitted.records.length,
        records: admitted.records,
        // A copy: the held rows outlive this snapshot, and a consumer that
        // sorts or splices its `targets` must not edit the source's cache.
        targets: tracks.rows.slice(),
        missions: missions.items,
        contacts: contacts.items,
        datumDegraded,
        // State of the optional `/tracks` route, so a consumer can tell rows
        // it is still being served from rows it is only still holding.
        tracksFeed: {
          served: tracks.served,
          polled,
          stale: tracks.stale,
          attempts: tracks.attempts,
          observedAtMs: tracks.observedAtMs,
          retryInMs: tracks.nextAtMs ? Math.max(0, tracks.nextAtMs - at) : 0,
        },
        // Which contract sections this bridge actually served, so the UI can
        // say "not shipped yet" instead of "nothing is happening".
        sections: {
          vehicles: Array.isArray(body?.vehicles),
          missions: missions.served,
          contacts: contacts.served,
        },
        complete: admitted.complete,
        rejectedCount:
          admitted.rejectedCount + missions.rejected + contacts.rejected,
      };
    },
  };
}

/* ------------------------------------------------------------------ *
 * Theater table (served): mcp/godseye_uav/theaters.py is the single
 * source of truth and exports the same rows over the bridge. The UI
 * consumes these rows; its bundled copy is only an offline fallback.
 * ------------------------------------------------------------------ */

function normalizeTheater(row) {
  const id = cleanText(row?.id);
  if (!id) return null;
  const home = Array.isArray(row?.home) ? row.home : [];
  const latitude = finite(home[0]);
  const longitude = finite(home[1]);
  if (!coordinates(latitude, longitude)) return null;
  const ao = (Array.isArray(row?.ao) ? row.ao : [])
    .map((vertex) => [finite(vertex?.[0]), finite(vertex?.[1])])
    .filter(([lat, lon]) => coordinates(lat, lon));
  if (ao.length < 3) return null;
  const pois = (Array.isArray(row?.pois) ? row.pois : [])
    .map((poi) => ({
      name: cleanText(poi?.name),
      lat: finite(poi?.lat),
      lon: finite(poi?.lon),
    }))
    .filter((poi) => poi.name && coordinates(poi.lat, poi.lon));
  const demo = row?.demo ?? {};
  return {
    id,
    label: cleanText(row?.label) || id,
    place: cleanText(row?.place),
    description: cleanText(row?.description),
    // [lat, lon, altitude] in the datum the payload declares (MSL, T1).
    home: [latitude, longitude, finite(home[2]) ?? 0],
    homeAltDatum: cleanText(row?.home_alt_datum) || 'MSL',
    ao,
    pois,
    orbitRadiusM: finite(row?.orbit_radius_m) ?? 150,
    demo: {
      polygon: (Array.isArray(demo.polygon) ? demo.polygon : [])
        .map((vertex) => [finite(vertex?.[0]), finite(vertex?.[1])])
        .filter(([lat, lon]) => coordinates(lat, lon)),
      altMAgl: finite(demo.alt_m_agl),
      speedMps: finite(demo.speed_mps),
    },
  };
}

/**
 * Keys a bridge may publish the RUNNING theater under.
 *
 * The table's `default` is deliberately NOT one of them: `default` is what the
 * table would pick, `active` is what the running server chose, and reading one
 * as the other is the whole of finding UI-1 — a stack running
 * `--theater iran-isfahan` presented Redmond POIs to the operator while the
 * aircraft flew 10,000 km away.
 *
 * The bridge publishes ONE block under `GET /theaters -> active` and
 * `GET /health -> theater` (BRIDGE_CONTRACT "THE ACTIVE THEATER"):
 * `{known, id, label, ground_elevation_msl_m, ao, in_table, theater_mismatch,
 * source, at_ms, reason}`. A bare id string is also accepted.
 */
const ACTIVE_THEATER_KEYS = Object.freeze([
  'active_theater',
  'active',
  'theater',
]);

/**
 * Read the running theater out of a `/theaters` or `/health` body.
 *
 * Three answers, never two: `null` when the bridge says nothing about a
 * theater at all, `known: false` (with the bridge's own `reason`) when it says
 * it does not know, and `known: true` with the id when a server really
 * answered. `known: false` is the producer's flag and it WINS — an id sitting
 * next to it is not something to fly on, and neither unknown state may be
 * resolved to the table default.
 *
 * @param {object} payload raw `/theaters` or `/health` body
 * @returns {{known: boolean, id: string, label: string, reason: string,
 *   mismatch: object|null, source: string, atMs: number|null}|null}
 */
export function normalizeActiveTheater(payload) {
  if (!payload || typeof payload !== 'object') return null;
  for (const key of ACTIVE_THEATER_KEYS) {
    const raw = payload[key];
    if (typeof raw === 'string') {
      const id = cleanText(raw);
      if (id)
        return {
          known: true,
          id,
          label: '',
          reason: '',
          mismatch: null,
          source: '',
          atMs: null,
        };
      continue;
    }
    if (!raw || typeof raw !== 'object') continue;
    const id = cleanText(raw.id ?? raw.theater_id ?? raw.theater);
    const declared = typeof raw.known === 'boolean' ? raw.known : null;
    const known = declared === null ? Boolean(id) : declared && Boolean(id);
    // Says nothing either way — try the next spelling rather than reporting
    // an unknown the bridge never claimed.
    if (!known && declared === null) continue;
    return {
      known,
      id: known ? id : '',
      label: known ? cleanText(raw.label) : '',
      reason: cleanText(raw.reason),
      // The server's own report that the envelope it ENFORCES was built for a
      // different theater than the row everything else is derived from.
      mismatch: raw.theater_mismatch ?? null,
      source: cleanText(raw.source),
      atMs: epoch(raw.at_ms),
    };
  }
  return null;
}

/**
 * Normalize the served theater payload (`theaters.as_payload()`).
 * @param {object} payload raw `/theaters` body
 * @returns {object|null} `{schema, defaultId, altDatum, active, order,
 *   theaters}` or null when the payload carries no usable theater
 */
export function normalizeTheaterTable(payload) {
  const rows = Array.isArray(payload?.theaters) ? payload.theaters : [];
  const theaters = {};
  const order = [];
  for (const row of rows) {
    const theater = normalizeTheater(row);
    if (!theater || theaters[theater.id]) continue;
    theaters[theater.id] = theater;
    order.push(theater.id);
  }
  if (!order.length) return null;
  const declared = cleanText(payload?.default);
  const running = normalizeActiveTheater(payload);
  return {
    schema: cleanText(payload?.schema) || 'unknown',
    defaultId: theaters[declared] ? declared : order[0],
    altDatum: cleanText(payload?.alt_datum) || 'MSL',
    altDatumNote: cleanText(payload?.alt_datum_note),
    // The theater the bridge is actually flying, when it says. An id this
    // table does not carry stays here marked `inTable: false` — silently
    // resolving it to the default would hide the very mismatch it proves.
    active: running
      ? {
          ...running,
          label: running.label || theaters[running.id]?.label || '',
          inTable: Boolean(theaters[running.id]),
          feed: 'theaters',
        }
      : null,
    order,
    theaters,
  };
}

/**
 * Fetch the served theater table. Fail-soft: a bridge without `/theaters`
 * resolves null so the caller can fall back to its bundled offline copy.
 * @param {object} [options]
 * @param {AbortSignal} [options.signal]
 * @param {string|(() => string)} [options.baseUrl] bridge origin, or a getter
 * @param {string|(() => string)} [options.token] bearer token, or a getter
 * @returns {Promise<object|null>} normalized table, or null when unavailable
 */
export async function fetchUavTheaters({ signal, baseUrl, token } = {}) {
  try {
    return normalizeTheaterTable(
      await getJson('/theaters', { signal, origin: baseUrl, secret: token }),
    );
  } catch {
    return null;
  }
}

/**
 * Ask `/health` which theater the bridge is RUNNING.
 *
 * Secondary to `/theaters`: use this only when the table itself published no
 * active theater. Fail-soft in both directions — an unreachable bridge and a
 * bridge whose `/health` carries no theater key both resolve null, and neither
 * is an error the operator should see as a failure.
 *
 * @param {object} [options]
 * @param {AbortSignal} [options.signal]
 * @param {string|(() => string)} [options.baseUrl] bridge origin, or a getter
 * @param {string|(() => string)} [options.token] bearer token, or a getter
 * @returns {Promise<object|null>} the running-theater block, or null
 */
export async function fetchUavActiveTheater({ signal, baseUrl, token } = {}) {
  try {
    const running = normalizeActiveTheater(
      await getJson('/health', { signal, origin: baseUrl, secret: token }),
    );
    return running ? { ...running, feed: 'health' } : null;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ *
 * Alarm stream (SSE /events)
 * ------------------------------------------------------------------ */

/** Default severity per alarm kind (BRIDGE_CONTRACT `/events` table). */
export const UAV_ALARM_SEVERITY = Object.freeze({
  bingo: 'critical',
  geofence_proximity: 'warning',
  geofence_breach: 'critical',
  lost_link: 'critical',
  link_restored: 'info',
  detection: 'info',
  mission_phase: 'info',
  datum_degraded: 'warning',
});

const SEVERITIES = Object.freeze(['info', 'warning', 'critical']);

/** Rank a severity so a surface can keep the worst alarm on top. */
export function alarmSeverityRank(severity) {
  const index = SEVERITIES.indexOf(cleanText(severity).toLowerCase());
  return index < 0 ? 0 : index;
}

/**
 * Normalize one `/events` alarm payload.
 * @param {object|string} payload event `data`, parsed or raw JSON text
 * @param {number} [receivedAtMs] fallback timestamp when the event omits one
 * @returns {object|null} normalized alarm, or null when unparseable
 */
export function normalizeAlarm(payload, receivedAtMs = Date.now()) {
  let row = payload;
  if (typeof payload === 'string') {
    try {
      row = JSON.parse(payload);
    } catch {
      return null;
    }
  }
  if (!row || typeof row !== 'object') return null;
  const kind = cleanText(row.kind) || 'unknown';
  const declared = cleanText(row.severity).toLowerCase();
  const severity = SEVERITIES.includes(declared)
    ? declared
    : UAV_ALARM_SEVERITY[kind] || 'info';
  return {
    kind,
    severity,
    vehicle: cleanText(row.vehicle),
    trackId: cleanText(row.track_id),
    missionId: cleanText(row.mission_id),
    message: cleanText(row.message) || kind.replace(/_/g, ' '),
    atMs: epoch(row.atMs) ?? epoch(row.at_ms) ?? receivedAtMs,
    detail: row.detail && typeof row.detail === 'object' ? row.detail : null,
  };
}

/**
 * Client for the bridge's one-way alarm channel (`GET /events`, SSE).
 *
 * Absence is the normal case until the bridge ships the endpoint, so a 404,
 * a refused connection or a missing `EventSource` all report as status and
 * nothing else: no throw, no console output, no effect on the poll loop.
 * Reconnection backs off exponentially and is capped.
 *
 * `EventSource` cannot set an Authorization header, so the loopback bridge
 * takes the bearer as a query parameter; pass `tokenQueryParam: null` to omit
 * it when the endpoint is unauthenticated.
 *
 * @param {object} [options]
 * @param {(url: string) => object} [options.eventSourceFactory] injected
 *   EventSource constructor wrapper; without one the stream is `unsupported`
 * @param {(alarm: object) => void} [options.onAlarm]
 * @param {(status: string, detail: object) => void} [options.onStatus]
 * @param {string|(() => string)} [options.baseUrl] overrides the configured
 *   bridge origin; a getter is re-read on every reconnect
 * @param {string|(() => string)} [options.token] overrides the bearer token
 * @param {string|null} [options.tokenQueryParam] query key for the token
 * @param {number} [options.retryMs] first reconnect delay
 * @param {number} [options.maxRetryMs] reconnect delay ceiling
 * @returns {{start: Function, stop: Function, getStatus: Function, url: Function}}
 */
export function createUavEventStream({
  eventSourceFactory = null,
  onAlarm = null,
  onStatus = null,
  baseUrl: overrideBase = null,
  token: overrideToken = null,
  tokenQueryParam = 'token',
  retryMs = 3000,
  maxRetryMs = 30000,
} = {}) {
  const factory =
    typeof eventSourceFactory === 'function'
      ? eventSourceFactory
      : runtime.eventSourceFactory;
  let stream = null;
  let timer = null;
  let attempts = 0;
  let stopped = false;
  let status = 'idle';

  function url() {
    const origin = baseUrl(overrideBase);
    const secret = token(overrideToken);
    const query =
      tokenQueryParam && secret
        ? `?${tokenQueryParam}=${encodeURIComponent(secret)}`
        : '';
    return `${origin}/events${query}`;
  }

  function report(next, detail = {}) {
    status = next;
    try {
      onStatus?.(next, detail);
    } catch {
      /* a listener must never take the stream down */
    }
  }

  function deliver(event) {
    const alarm = normalizeAlarm(event?.data, Date.now());
    if (!alarm) return;
    try {
      onAlarm?.(alarm);
    } catch {
      /* a listener must never take the stream down */
    }
  }

  function close() {
    if (!stream) return;
    try {
      stream.close?.();
    } catch {
      /* already gone */
    }
    stream = null;
  }

  function fail(reason) {
    close();
    if (stopped) return;
    const delay = Math.min(maxRetryMs, retryMs * 2 ** attempts);
    attempts += 1;
    report('offline', { reason, attempts, retryInMs: delay });
    timer = setTimeout(connect, delay);
  }

  function connect() {
    timer = null;
    if (stopped) return;
    let opened = null;
    try {
      opened = factory(url());
    } catch {
      opened = null;
    }
    if (!opened) {
      fail('unavailable');
      return;
    }
    stream = opened;
    report('connecting', { attempts });
    stream.onopen = () => {
      attempts = 0;
      report('live', {});
    };
    stream.onerror = () => fail('disconnected');
    stream.onmessage = deliver;
    // The contract names the event `alarm`; unnamed events fall through to
    // onmessage above so an untyped emitter still reaches the operator.
    stream.addEventListener?.('alarm', deliver);
  }

  return {
    /** Open the stream. Safe to call repeatedly; never throws. */
    start() {
      if (typeof factory !== 'function') {
        report('unsupported', { reason: 'no EventSource' });
        return false;
      }
      stopped = false;
      if (stream || timer) return true;
      connect();
      return true;
    },
    /** Close the stream and cancel any pending reconnect. */
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      timer = null;
      close();
      report('stopped', {});
    },
    getStatus() {
      return status;
    },
    url,
  };
}
