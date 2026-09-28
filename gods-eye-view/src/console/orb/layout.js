/**
 * Pure, deterministic placement of intel-graph nodes on the unit sphere
 * (UX spec §4.1–4.2).
 *
 * The orb is a categorical sphere, not a map: latitude is the node type (a
 * band), and inside the contact belt longitude is the equipment sector. Every
 * band is a slot pool. A node's slot starts at `hash(id)` (or at its anchor's
 * longitude for missions, units, equipment and reports) and probes linearly to
 * the next free slot. Passing the previous layout back in keeps every existing
 * assignment, so a node never moves when others arrive or leave. The one
 * deliberate exception is the alarm cap, which the spec orders by recency
 * (newest nearest −68°, each older alarm about the same subject 3° poleward).
 *
 * Coordinates: y is the polar axis (north = +y); longitude 0 faces +z, which
 * is the camera at rest, and +90° is +x.
 *
 * WG spec §4.2.4 and §4.2.6: the active theater always takes the pole, even
 * when another theater held it in the previous layout (either scope); mapped
 * sites sit in their own sector-anchored row at +42°, with the contact belt
 * topped at +35°. A type the contract lacks goes to `other`.
 */

import {
  SITE_BAND,
  placeSectorRow,
  sectorRowLon,
  siteSectorKey,
} from './contextBands.js';

const DEG = Math.PI / 180;
const GOLDEN = 0.6180339887498949;

/** Contact sectors, in fixed order, 36° each. Sector 0 is centred on lon 0. */
export const SECTORS = Object.freeze(
  [
    ['air-defense', 'Air defense'],
    ['radar-ew', 'Radar and EW'],
    ['c2', 'Command and control'],
    ['ground-forces', 'Ground forces'],
    ['logistics', 'Logistics'],
    ['infrastructure', 'Infrastructure'],
    ['naval', 'Naval'],
    ['air', 'Air'],
    ['civilian', 'Civilian'],
    ['unclassified', 'Unclassified'],
  ].map(([key, label], index) =>
    Object.freeze({
      key,
      label,
      index,
      lonStart: -18 + index * 36,
      lonCenter: wrapLon(index * 36),
    }),
  ),
);

export const SECTOR_WIDTH_DEG = 36;
const SECTOR_MARGIN_DEG = 3;
const SECTOR_INDEX = new Map(
  SECTORS.map((sector) => [sector.key, sector.index]),
);

/** Bands by latitude (spec §4.2). `other` holds node types the contract lacks. */
export const BANDS = Object.freeze({
  theater: Object.freeze({ caption: 'Theater', lat: 90, ringLat: 85, min: 12 }),
  feed: Object.freeze({ caption: 'Feeds', lat: 80, min: 18 }),
  poi: Object.freeze({ caption: 'Places', lat: 72, min: 24 }),
  vehicle: Object.freeze({ caption: 'Own force', lat: 60, min: 12 }),
  mission: Object.freeze({
    caption: 'Missions',
    lat: 50,
    min: 72,
    anchored: true,
  }),
  site: SITE_BAND,
  track: Object.freeze({
    caption: 'Contacts',
    latTop: 35,
    latBottom: -26,
    lat: 6,
    min: 30,
  }),
  other: Object.freeze({ caption: 'Other', lat: -31, min: 36 }),
  unit: Object.freeze({ caption: 'Units', lat: -36, min: 72, anchored: true }),
  equipment: Object.freeze({
    caption: 'Equipment',
    lat: -46,
    min: 72,
    anchored: true,
  }),
  report: Object.freeze({
    caption: 'Reports',
    lat: -58,
    min: 48,
    anchored: true,
  }),
  alarm: Object.freeze({
    caption: 'Alarms',
    latTop: -68,
    latBottom: -86,
    lat: -74,
    step: 3,
    columns: 72,
  }),
});

/**
 * Feeds and places spread over this arc of longitude, centred on own force,
 * so the default view (which frames own force) sees the whole cap.
 */
export const CAP_SPAN_DEG = 240;

/** Band keys from the north pole to the south pole. */
export const BAND_ORDER = Object.freeze([
  'theater',
  'feed',
  'poi',
  'vehicle',
  'mission',
  'site',
  'track',
  'other',
  'unit',
  'equipment',
  'report',
  'alarm',
]);

/** Parallels the graticule draws: band-group boundaries only (spec §4.1). */
export const GRATICULE_PARALLELS = Object.freeze({
  beltTop: 38.5,
  /** Sector names sit between the belt line and the top row of contacts. */
  sectorNames: 36.75,
  beltBottom: -28.5,
  alarmCap: -63,
  emptyRow: 6,
});

/** Edge polylines: 12 segments, 13 points, 3 floats each. */
export const EDGE_SEGMENTS = 12;
export const EDGE_POINTS = EDGE_SEGMENTS + 1;
export const EDGE_STRIDE = EDGE_POINTS * 3;
const EDGE_LIFT = 0.18;

/** The band a node type lives in. */
export function bandOfType(type) {
  return Object.hasOwn(BANDS, type) && type !== 'other' ? type : 'other';
}

/** The contact sector for a node group; unknown groups are unclassified. */
export function sectorOf(group) {
  return SECTOR_INDEX.get(group) ?? SECTOR_INDEX.get('unclassified');
}

/**
 * 32-bit string hash: FNV-1a over UTF-16 code units, then the murmur3
 * finaliser so short ids with shared prefixes still spread.
 * @param {string} id
 * @returns {number} unsigned 32-bit integer
 */
export function hashId(id) {
  const text = String(id);
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x85ebca6b);
  hash ^= hash >>> 13;
  hash = Math.imul(hash, 0xc2b2ae35);
  hash ^= hash >>> 16;
  return hash >>> 0;
}

/** Longitude folded into [-180, 180). */
export function wrapLon(lon) {
  const wrapped = ((((lon + 180) % 360) + 360) % 360) - 180;
  return Object.is(wrapped, -0) ? 0 : wrapped;
}

/** Unit vector for a latitude/longitude in degrees. */
export function toVector(latDeg, lonDeg, out = [0, 0, 0], offset = 0) {
  const lat = latDeg * DEG;
  const lon = lonDeg * DEG;
  const c = Math.cos(lat);
  out[offset] = c * Math.sin(lon);
  out[offset + 1] = Math.sin(lat);
  out[offset + 2] = c * Math.cos(lon);
  return out;
}

/** Circular mean of longitudes in degrees; null for an empty list. */
export function meanLongitude(lons) {
  let s = 0;
  let c = 0;
  for (const lon of lons) {
    s += Math.sin(lon * DEG);
    c += Math.cos(lon * DEG);
  }
  if (!lons.length || (Math.abs(s) < 1e-9 && Math.abs(c) < 1e-9)) return null;
  return wrapLon(Math.atan2(s, c) / DEG);
}

const compareIds = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/**
 * Merge two sorted id lists so each is spread evenly through the result
 * ([a0, b0, a1, a2, b1, …]); deterministic for the same inputs.
 */
export function interleave(a, b) {
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    // Take from whichever list is further behind its share.
    const takeA =
      j >= b.length ||
      (i < a.length && (i + 0.5) / a.length <= (j + 0.5) / b.length);
    out.push(takeA ? a[i++] : b[j++]);
  }
  return out;
}

function initialCapacity(min, count) {
  let capacity = min;
  while (count * 3 > capacity) capacity *= 2;
  return capacity;
}

function ringSlotLon(slot, capacity) {
  return wrapLon(-180 + ((slot + 0.5) * 360) / capacity);
}

function ringSlotFor(lon, capacity) {
  const step = 360 / capacity;
  return (
    ((Math.floor((wrapLon(lon) + 180) / step) % capacity) + capacity) % capacity
  );
}

/** Fibonacci-lattice slot inside a contact sector patch (area-uniform). */
function sectorSlot(sector, slot, capacity) {
  const top = Math.sin(BANDS.track.latTop * DEG);
  const bottom = Math.sin(BANDS.track.latBottom * DEG);
  const u = (slot + 0.5) / capacity;
  const lat = Math.asin(top + (bottom - top) * u) / DEG;
  const frac = (slot * GOLDEN) % 1;
  const width = SECTOR_WIDTH_DEG - 2 * SECTOR_MARGIN_DEG;
  const lon = wrapLon(
    SECTORS[sector].lonStart + SECTOR_MARGIN_DEG + frac * width,
  );
  return [lat, lon];
}

/**
 * Spherical linear interpolation of a great-circle arc, lifted off the surface
 * in proportion to its length so long edges clear the nodes they pass.
 * @returns {Float32Array} EDGE_POINTS × (x, y, z)
 */
export function edgeArc(p, q, out = new Float32Array(EDGE_STRIDE), offset = 0) {
  const dot = Math.max(
    -1,
    Math.min(1, p[0] * q[0] + p[1] * q[1] + p[2] * q[2]),
  );
  const theta = Math.acos(dot);
  const lift = EDGE_LIFT * (theta / Math.PI);
  let mid = null;
  if (Math.PI - theta < 1e-3) {
    // Antipodal: any great circle works; pick one through a perpendicular.
    const axis = Math.abs(p[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
    mid = normalize(cross(p, axis));
  }
  for (let k = 0; k < EDGE_POINTS; k += 1) {
    const t = k / EDGE_SEGMENTS;
    let x;
    let y;
    let z;
    if (theta < 1e-6) {
      [x, y, z] = p;
    } else if (mid) {
      const [a, b, tt] = t < 0.5 ? [p, mid, t * 2] : [mid, q, (t - 0.5) * 2];
      const half = Math.PI / 2;
      const wa = Math.sin((1 - tt) * half);
      const wb = Math.sin(tt * half);
      x = wa * a[0] + wb * b[0];
      y = wa * a[1] + wb * b[1];
      z = wa * a[2] + wb * b[2];
    } else {
      const s = Math.sin(theta);
      const wa = Math.sin((1 - t) * theta) / s;
      const wb = Math.sin(t * theta) / s;
      x = wa * p[0] + wb * q[0];
      y = wa * p[1] + wb * q[1];
      z = wa * p[2] + wb * q[2];
    }
    const scale = 1 + lift * Math.sin(Math.PI * t);
    const o = offset + k * 3;
    out[o] = x * scale;
    out[o + 1] = y * scale;
    out[o + 2] = z * scale;
  }
  return out;
}

function cross(a, b) {
  return [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
}

function normalize(v) {
  const len = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / len, v[1] / len, v[2] / len];
}

function alarmSeq(node) {
  const seq = Number(node?.attrs?.seq);
  if (Number.isFinite(seq)) return seq;
  const tail = Number(String(node.id).split(':').pop());
  return Number.isFinite(tail) ? tail : 0;
}

/**
 * Place every node of an intel graph.
 *
 * Deterministic: the same graph (in any node order) gives the same layout.
 * Stable: with `previous` (the last result) every surviving node keeps its
 * slot unless its pool had to grow past two-thirds occupancy, a newly active
 * theater claimed the pole, or a more salient site pushed it into its
 * sector's overflow. `hidden[i]` marks placed nodes the orb does not draw
 * (site overflow); `overflow.site` counts them.
 *
 * @param {{nodes?: object[], edges?: object[]}|null} graph
 * @param {object|null} [previous] the previous computeLayout() result
 * @returns {object} layout (see the returned object's fields)
 */
export function computeLayout(graph, previous = null) {
  const byId = new Map();
  for (const node of Array.isArray(graph?.nodes) ? graph.nodes : []) {
    if (!node || typeof node.id !== 'string' || !node.id || byId.has(node.id))
      continue;
    byId.set(node.id, node);
  }
  const ids = [...byId.keys()].sort(compareIds);
  const index = new Map(ids.map((id, i) => [id, i]));
  const n = ids.length;
  const lat = new Float32Array(n);
  const lon = new Float32Array(n);
  const pos = new Float32Array(n * 3);
  const band = new Array(n);
  const sector = new Int8Array(n).fill(-1);
  const placed = new Uint8Array(n);
  // Placed but not drawn: site-band overflow (counted, sent to the map).
  const hidden = new Uint8Array(n);
  const assignments = new Map();
  const pools = {};
  const prevAssign =
    previous?.assignments instanceof Map ? previous.assignments : null;
  const prevPools = previous?.pools || {};

  // Edges (both ends present, no self loops), in graph order.
  const edges = [];
  for (const edge of Array.isArray(graph?.edges) ? graph.edges : []) {
    const ai = index.get(edge?.a);
    const bi = index.get(edge?.b);
    if (ai == null || bi == null || ai === bi) continue;
    edges.push({ a: edge.a, b: edge.b, ai, bi, kind: String(edge.kind || '') });
  }
  const inbound = new Map();
  const outbound = new Map();
  const push = (map, key, value) => {
    const list = map.get(key);
    if (list) list.push(value);
    else map.set(key, [value]);
  };
  for (const edge of edges) {
    push(inbound, `${edge.kind}|${edge.b}`, edge.ai);
    push(outbound, `${edge.kind}|${edge.a}`, edge.bi);
  }
  const incoming = (kind, target) => inbound.get(`${kind}|${target}`) || [];
  const outgoing = (kind, source) => outbound.get(`${kind}|${source}`) || [];

  const members = {};
  for (const key of BAND_ORDER) members[key] = [];
  for (const id of ids) {
    const key = bandOfType(byId.get(id).type);
    band[index.get(id)] = key;
    members[key].push(id);
  }

  const set = (i, la, lo) => {
    lat[i] = la;
    lon[i] = wrapLon(lo);
    toVector(la, lon[i], pos, i * 3);
    placed[i] = 1;
  };

  /**
   * Fill one slot pool.
   * @param {string} key pool key
   * @param {string[]} list member ids, sorted
   * @param {number} min minimum capacity
   * @param {(slot:number, capacity:number, id:string)=>[number,number]} where slot → [lat, lon]
   * @param {(id:string, capacity:number)=>({slot:number, anchored:boolean}|null)} [prefer]
   * @param {Map<string, number>|null} [claims] ids that take this slot whoever
   *   held it before (the displaced holder probes like a newcomer)
   */
  const fillPool = (key, list, min, where, prefer, claims = null) => {
    if (!list.length) {
      if (prevPools[key]) pools[key] = { capacity: prevPools[key].capacity };
      return;
    }
    let capacity =
      prevPools[key]?.capacity ?? initialCapacity(min, list.length);
    let keepPrevious = true;
    if (list.length * 3 > capacity * 2) {
      capacity = initialCapacity(Math.max(min, capacity), list.length);
      keepPrevious = false;
    }
    pools[key] = { capacity };
    const taken = new Map();
    const pending = [];
    const inList = new Set(list);
    for (const [id, slot] of claims || []) {
      if (!inList.has(id) || taken.has(slot)) continue;
      taken.set(slot, id);
      assignments.set(id, { pool: key, slot, capacity, provisional: false });
    }
    for (const id of list) {
      if (claims?.has(id) && taken.get(claims.get(id)) === id) continue;
      const was = keepPrevious ? prevAssign?.get(id) : null;
      const choice = prefer?.(id, capacity) ?? null;
      const reanchor = was?.provisional && choice?.anchored;
      if (
        was &&
        was.pool === key &&
        was.capacity === capacity &&
        !reanchor &&
        !taken.has(was.slot)
      ) {
        taken.set(was.slot, id);
        assignments.set(id, { ...was });
      } else {
        pending.push([id, choice]);
      }
    }
    for (const [id, choice] of pending) {
      const start = choice ? choice.slot : hashId(id) % capacity;
      let slot = start;
      for (let step = 0; step < capacity; step += 1) {
        slot = choice?.anchored
          ? (((start + (step % 2 ? (step + 1) / 2 : -step / 2)) % capacity) +
              capacity) %
            capacity
          : (start + step) % capacity;
        if (!taken.has(slot)) break;
      }
      taken.set(slot, id);
      assignments.set(id, {
        pool: key,
        slot,
        capacity,
        provisional: Boolean(prefer) && choice == null,
      });
    }
    for (const id of list) {
      const { slot } = assignments.get(id);
      const [la, lo] = where(slot, capacity, id);
      set(index.get(id), la, lo);
    }
  };

  const ring = (latDeg) => (slot, capacity) => [
    latDeg,
    ringSlotLon(slot, capacity),
  ];
  const anchorAt = (lons) => (_id, capacity) => {
    const mean = meanLongitude(lons);
    return mean == null
      ? null
      : { slot: ringSlotFor(mean, capacity), anchored: true };
  };
  /** Start each node of `list` at an even share of an arc, in list order. */
  const spreadOver = (list, centre, span) => {
    const rank = new Map(list.map((id, k) => [id, k]));
    return (id, capacity) => ({
      slot: ringSlotFor(
        centre - span / 2 + (span * (rank.get(id) + 0.5)) / list.length,
        capacity,
      ),
      anchored: false,
    });
  };
  const placedLons = (indices) =>
    indices.filter((i) => placed[i]).map((i) => lon[i]);

  // Theater: the active one takes the pole in either scope, even when another
  // theater held it last time (WG §4.2.4 pole fix); the rest ring it at +85°.
  // With none active, the pole holder keeps it (or the first by id takes it).
  const theaters = [...members.theater].sort((a, b) => {
    const aa = byId.get(a)?.attrs?.active === true ? 0 : 1;
    const bb = byId.get(b)?.attrs?.active === true ? 0 : 1;
    return aa - bb || compareIds(a, b);
  });
  const activeTheater = theaters.find(
    (id) => byId.get(id)?.attrs?.active === true,
  );
  fillPool(
    'theater',
    theaters,
    BANDS.theater.min,
    (slot, capacity) =>
      slot === 0
        ? [90, 0]
        : [
            BANDS.theater.ringLat,
            wrapLon(-180 + ((slot - 0.5) * 360) / (capacity - 1)),
          ],
    () => ({ slot: 0, anchored: false }),
    activeTheater ? new Map([[activeTheater, 0]]) : null,
  );
  fillPool(
    'vehicle',
    members.vehicle,
    BANDS.vehicle.min,
    ring(BANDS.vehicle.lat),
  );
  // The polar cap (feeds +80°, places +72°) is small on screen: five feeds
  // hashed into neighbouring slots piled into one cluster (review). Feeds and
  // places share one longitude pool, interleaved and spread evenly over the
  // CAP_SPAN_DEG arc centred on own force (which the default view frames), so
  // neighbours alternate rings and none sits on the far rim; each keeps its
  // band's latitude. Placed nodes keep their slots.
  const cap = interleave(members.feed, members.poi);
  const capCentre =
    meanLongitude(placedLons(members.vehicle.map((id) => index.get(id)))) ?? 0;
  fillPool(
    'cap',
    cap,
    Math.max(BANDS.feed.min, BANDS.poi.min),
    (slot, capacity, id) => [
      bandOfType(byId.get(id).type) === 'feed' ? BANDS.feed.lat : BANDS.poi.lat,
      ringSlotLon(slot, capacity),
    ],
    spreadOver(cap, capCentre, CAP_SPAN_DEG),
  );

  // Contacts: one Fibonacci pool per sector.
  const bySector = SECTORS.map(() => []);
  for (const id of members.track) {
    const s = sectorOf(byId.get(id).group);
    sector[index.get(id)] = s;
    bySector[s].push(id);
  }
  SECTORS.forEach((sec, s) =>
    fillPool(
      `track:${sec.key}`,
      bySector[s],
      BANDS.track.min,
      (slot, capacity) => sectorSlot(s, slot, capacity),
    ),
  );

  // Missions sit at their vehicle's longitude.
  fillPool(
    'mission',
    members.mission,
    BANDS.mission.min,
    ring(BANDS.mission.lat),
    (id, capacity) => {
      const vehicle = byId.get(id)?.attrs?.vehicle;
      const direct =
        typeof vehicle === 'string' ? index.get(`veh:${vehicle}`) : undefined;
      const flying = direct != null ? [direct] : incoming('flying', id);
      return anchorAt(placedLons(flying))(id, capacity);
    },
  );

  // Sites: one row at +42°, anchored to their sector, 12 slots of 3° each;
  // a sector's overflow sits at its centre, hidden and counted (§4.2.6).
  const sectorKeys = new Set(SECTORS.map((sec) => sec.key));
  const siteItems = members.site.map((id) => {
    const s = sectorOf(siteSectorKey(byId.get(id), sectorKeys));
    sector[index.get(id)] = s;
    return { id, sector: s, salience: Number(byId.get(id)?.salience) || 0 };
  });
  const siteRow = placeSectorRow(siteItems, {
    slots: BANDS.site.slotsPerSector,
    previous: (id) => {
      const was = prevAssign?.get(id);
      return was?.pool === 'site' ? was : null;
    },
  });
  for (const item of siteItems) {
    const i = index.get(item.id);
    const at = siteRow.slots.get(item.id);
    if (at) {
      set(i, BANDS.site.lat, sectorRowLon(item.sector, at.slot));
      assignments.set(item.id, {
        pool: 'site',
        sector: item.sector,
        slot: at.slot,
        capacity: BANDS.site.slotsPerSector,
      });
    } else {
      set(i, BANDS.site.lat, SECTORS[item.sector].lonCenter);
      hidden[i] = 1;
      assignments.set(item.id, {
        pool: 'site',
        sector: item.sector,
        slot: -1,
        capacity: BANDS.site.slotsPerSector,
        overflow: true,
      });
    }
  }

  fillPool('other', members.other, BANDS.other.min, ring(BANDS.other.lat));
  fillPool(
    'unit',
    members.unit,
    BANDS.unit.min,
    ring(BANDS.unit.lat),
    (id, capacity) =>
      anchorAt(placedLons(incoming('member_of', id)))(id, capacity),
  );
  fillPool(
    'equipment',
    members.equipment,
    BANDS.equipment.min,
    ring(BANDS.equipment.lat),
    (id, capacity) => anchorAt(placedLons(incoming('is_a', id)))(id, capacity),
  );
  fillPool(
    'report',
    members.report,
    BANDS.report.min,
    ring(BANDS.report.lat),
    (id, capacity) =>
      anchorAt(placedLons(outgoing('reports_on', id)))(id, capacity),
  );

  // Alarms: columns by subject longitude, newest nearest −68°.
  const columns = new Map();
  const colWidth = 360 / BANDS.alarm.columns;
  for (const id of members.alarm) {
    const subject = outgoing('about', id).find((i) => placed[i]);
    const subjectLon =
      subject != null
        ? lon[subject]
        : -180 + ((hashId(id) % BANDS.alarm.columns) + 0.5) * colWidth;
    const column = ringSlotFor(subjectLon, BANDS.alarm.columns);
    if (!columns.has(column)) columns.set(column, []);
    columns.get(column).push(id);
  }
  for (const [column, list] of columns) {
    list.sort(
      (a, b) =>
        alarmSeq(byId.get(b)) - alarmSeq(byId.get(a)) || compareIds(a, b),
    );
    const depth = Math.round(
      (BANDS.alarm.latTop - BANDS.alarm.latBottom) / BANDS.alarm.step,
    );
    list.forEach((id, k) => {
      const la = BANDS.alarm.latTop - BANDS.alarm.step * Math.min(k, depth);
      const lo =
        ringSlotLon(column, BANDS.alarm.columns) +
        (k > depth ? (k - depth) * 1.5 : 0);
      set(index.get(id), la, lo);
      assignments.set(id, {
        pool: 'alarm',
        slot: column,
        capacity: BANDS.alarm.columns,
        rank: k,
      });
    });
  }

  // Edge polylines, precomputed once per layout.
  const edgePts = new Float32Array(edges.length * EDGE_STRIDE);
  const a = [0, 0, 0];
  const b = [0, 0, 0];
  edges.forEach((edge, e) => {
    a[0] = pos[edge.ai * 3];
    a[1] = pos[edge.ai * 3 + 1];
    a[2] = pos[edge.ai * 3 + 2];
    b[0] = pos[edge.bi * 3];
    b[1] = pos[edge.bi * 3 + 1];
    b[2] = pos[edge.bi * 3 + 2];
    edgeArc(a, b, edgePts, e * EDGE_STRIDE);
  });

  const counts = {};
  for (const key of BAND_ORDER) counts[key] = members[key].length;
  const sectorCounts = bySector.map((list) => list.length);
  const overflow = { site: siteRow.overflow.length };

  return {
    n,
    ids,
    index,
    nodes: ids.map((id) => byId.get(id)),
    pos,
    lat,
    lon,
    band,
    sector,
    edges,
    edgePts,
    assignments,
    pools,
    counts,
    sectorCounts,
    hidden,
    overflow,
  };
}
