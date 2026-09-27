/**
 * The console's picture: polls `/intel/graph` (every 2 s while the page is
 * visible, backing off to 5 s on error), follows the bridge alarm lane
 * (`/events`), and publishes one state object plus a `change` diff so the orb
 * can animate arrivals instead of redrawing blindly.
 *
 * State (`get()`):
 * - `graph` — the last graph the host returned (never fabricated);
 * - `byId` — id → node; works as `byId[id]` and as `byId.get(id)`;
 * - `alarms` — newest first: `{id, seq, kind, severity, message, atMs,
 *   vehicle?, track_id?, mission_id?, detail?, source, critical}`;
 * - `status` — `loading|live|stale|offline|unauthorized`;
 * - `lastLiveAt` — when the last graph arrived (ms), or null;
 * - `error` — `{kind:'auth'|'offline'|'timeout'|'http', message, status?, atMs}` or null;
 * - `scope` — `theater|all`.
 *
 * Alarm ids: graph alarm nodes are `alarm:{seq}` (the hub's publish number).
 * A live SSE alarm has no seq yet, so it is `alarm:live:{fingerprint}` until
 * the next graph carries it; the viewed set is keyed by fingerprint, so
 * "viewed" survives that hand-over.
 */

export const POLL_MS = 2000;
export const BACKOFF_MS = 5000;
export const GRAPH_TIMEOUT_MS = 8000;
/** Alarm kinds that raise the critical banner (UX spec §7.3). */
export const CRITICAL_ALARM_KINDS = Object.freeze([
  'bingo',
  'geofence_breach',
  'lost_link',
]);
const VIEWED_KEY = 'ic.alarms.viewed.v1';
const VIEWED_MAX = 200;
const LIVE_ALARMS_MAX = 50;

const CRITICAL_SET = new Set(CRITICAL_ALARM_KINDS);

/** id → node lookup usable both as a plain object and like a Map. */
class NodeIndex {
  get(id) {
    return Object.prototype.hasOwnProperty.call(this, id)
      ? this[id]
      : undefined;
  }

  has(id) {
    return Object.prototype.hasOwnProperty.call(this, id);
  }

  get size() {
    return Object.keys(this).length;
  }

  keys() {
    return Object.keys(this)[Symbol.iterator]();
  }

  values() {
    return Object.values(this)[Symbol.iterator]();
  }

  entries() {
    return Object.entries(this)[Symbol.iterator]();
  }

  forEach(fn) {
    for (const [id, node] of Object.entries(this)) fn(node, id, this);
  }

  [Symbol.iterator]() {
    return this.entries();
  }
}

function indexNodes(nodes) {
  const byId = new NodeIndex();
  for (const node of nodes) {
    if (node && typeof node.id === 'string') byId[node.id] = node;
  }
  return byId;
}

function str(value) {
  return typeof value === 'string' ? value : value == null ? '' : String(value);
}

/** Stable identity of one alarm across the SSE lane and the graph. */
export function alarmFingerprint(alarm) {
  const a = alarm || {};
  return [
    str(a.kind),
    str(a.vehicle),
    str(a.track_id),
    str(a.mission_id),
    Number.isFinite(Number(a.atMs)) ? String(Number(a.atMs)) : '',
  ].join('|');
}

/** An alarm record from a graph `alarm` node. */
export function alarmFromNode(node) {
  const attrs = node?.attrs || {};
  const alarm = {
    id: node.id,
    seq: Number.isFinite(Number(attrs.seq)) ? Number(attrs.seq) : null,
    kind: str(attrs.kind) || 'alarm',
    severity: str(attrs.severity) || null,
    message: str(node.subtitle),
    atMs: Number.isFinite(Number(node.ts_ms)) ? Number(node.ts_ms) : null,
    vehicle: attrs.vehicle || null,
    track_id: attrs.track_id || null,
    mission_id: attrs.mission_id || null,
    detail: null,
    source: 'graph',
  };
  alarm.critical = CRITICAL_SET.has(alarm.kind);
  alarm.fingerprint = alarmFingerprint(alarm);
  return alarm;
}

/** An alarm record from an SSE `alarm` payload (or `/intel/events/recent`). */
export function alarmFromPayload(payload) {
  const p = payload || {};
  const alarm = {
    id: null,
    seq: Number.isFinite(Number(p.seq)) ? Number(p.seq) : null,
    kind: str(p.kind) || 'alarm',
    severity: str(p.severity) || null,
    message: str(p.message),
    atMs: Number.isFinite(Number(p.atMs)) ? Number(p.atMs) : null,
    vehicle: p.vehicle || null,
    track_id: p.track_id || null,
    mission_id: p.mission_id || null,
    detail: p.detail && typeof p.detail === 'object' ? p.detail : null,
    source: 'live',
  };
  alarm.critical = CRITICAL_SET.has(alarm.kind);
  alarm.fingerprint = alarmFingerprint(alarm);
  alarm.id =
    alarm.seq != null
      ? `alarm:${alarm.seq}`
      : `alarm:live:${alarm.fingerprint}`;
  return alarm;
}

/**
 * Diff two node sets by id. `updated` = same id, different content;
 * `statusChanged` = the subset whose `status` changed.
 */
export function diffNodes(prevIndex, prevPrints, nodes) {
  const added = [];
  const updated = [];
  const statusChanged = [];
  const prints = new Map();
  const seen = new Set();
  for (const node of nodes) {
    if (!node || typeof node.id !== 'string') continue;
    seen.add(node.id);
    const print = JSON.stringify(node);
    prints.set(node.id, print);
    const prev = prevIndex?.get?.(node.id);
    if (!prev) added.push(node.id);
    else if (prevPrints.get(node.id) !== print) {
      updated.push(node.id);
      if (prev.status !== node.status) statusChanged.push(node.id);
    }
  }
  const removed = [];
  if (prevIndex)
    for (const id of Object.keys(prevIndex))
      if (!seen.has(id)) removed.push(id);
  return { added, removed, updated, statusChanged, prints };
}

function errorOf(error, now) {
  const name = error?.name;
  const message = str(error?.message) || 'unknown error';
  if (name === 'AuthError')
    return { kind: 'auth', message, status: 401, atMs: now };
  if (name === 'TimeoutError') return { kind: 'timeout', message, atMs: now };
  if (name === 'OfflineError') return { kind: 'offline', message, atMs: now };
  // A 404 without the host's JSON body means nothing at this address serves
  // the intel routes (a bare dev server, a stale page): the service is not
  // reachable here, which is not the same as the picture failing to load.
  const body = error?.body;
  if (error?.status === 404 && (body == null || typeof body !== 'object'))
    return { kind: 'offline', message, status: 404, atMs: now };
  return { kind: 'http', message, status: error?.status ?? null, atMs: now };
}

function defaultStorage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

function defaultClock() {
  return {
    now: () => Date.now(),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (id) => globalThis.clearTimeout(id),
  };
}

/**
 * Create the store.
 * @param {object} options
 * @param {object} options.api createApi() client
 * @param {object} [options.bus] createBus(); `alarm:viewed` is emitted on it
 * @param {number} [options.intervalMs]
 * @param {number} [options.backoffMs]
 * @param {'theater'|'all'} [options.scope]
 * @param {object} [options.clock] {now, setTimeout, clearTimeout}
 * @param {object|null} [options.doc] document (visibility); null disables
 * @param {object|null} [options.storage] persists the viewed-alarm set
 * @param {boolean} [options.alarmStream] follow `/events` (default true)
 */
export function createIntelStore({
  api,
  bus = null,
  intervalMs = POLL_MS,
  backoffMs = BACKOFF_MS,
  scope: initialScope = 'theater',
  clock = defaultClock(),
  doc = globalThis.document ?? null,
  storage = defaultStorage(),
  alarmStream = true,
} = {}) {
  let graph = null;
  let byId = new NodeIndex();
  let prints = new Map();
  let edgesPrint = '';
  let status = 'loading';
  let lastLiveAt = null;
  let error = null;
  let scope = initialScope === 'all' ? 'all' : 'theater';
  let graphAlarms = [];
  const liveAlarms = new Map(); // fingerprint -> alarm (not yet in the graph)
  const liveInfo = new Map(); // fingerprint -> {message, detail} from the SSE lane
  let alarms = [];
  const announced = new Set(); // fingerprints already reported as new
  let primed = false; // first graph seen: alarms before it are history
  let lastStageInputAt = 0;
  let timer = null;
  let inflight = null;
  let started = false;
  let failing = false;
  let pausedHidden = false;
  let stream = null;
  let pollSeq = 0;
  const listeners = new Set();
  const entityInflight = new Map();
  const viewed = loadViewed();

  function loadViewed() {
    try {
      const raw = storage?.getItem?.(VIEWED_KEY);
      const list = raw ? JSON.parse(raw) : [];
      return new Set(
        Array.isArray(list) ? list.filter((x) => typeof x === 'string') : [],
      );
    } catch {
      return new Set();
    }
  }

  function saveViewed() {
    try {
      storage?.setItem?.(
        VIEWED_KEY,
        JSON.stringify([...viewed].slice(-VIEWED_MAX)),
      );
    } catch {
      /* private window or quota: the set still works for this page */
    }
  }

  function snapshot() {
    return {
      graph,
      byId,
      alarms,
      status,
      lastLiveAt,
      error,
      scope,
      lastStageInputAt,
    };
  }

  function emitChange(diff) {
    const full = {
      added: [],
      removed: [],
      updated: [],
      statusChanged: [],
      edges: false,
      graph: false,
      alarms: false,
      newAlarms: [],
      status: false,
      statusFrom: null,
      statusTo: status,
      scope: false,
      ...diff,
    };
    for (const cb of [...listeners]) {
      try {
        cb(full);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
  }

  function setStatus(next) {
    if (next === status) return null;
    const from = status;
    status = next;
    return { status: true, statusFrom: from, statusTo: next };
  }

  function rebuildAlarms() {
    const inGraph = new Set(graphAlarms.map((a) => a.fingerprint));
    for (const fp of [...liveAlarms.keys()])
      if (inGraph.has(fp)) liveAlarms.delete(fp);
    const merged = [...graphAlarms, ...liveAlarms.values()];
    merged.sort((a, b) => {
      const t = (b.atMs ?? -Infinity) - (a.atMs ?? -Infinity);
      if (t) return t;
      return (b.seq ?? -Infinity) - (a.seq ?? -Infinity);
    });
    alarms = merged;
  }

  function freshAlarms(candidates) {
    if (!primed) {
      for (const a of candidates) announced.add(a.fingerprint);
      return [];
    }
    const out = [];
    for (const a of candidates) {
      if (announced.has(a.fingerprint)) continue;
      announced.add(a.fingerprint);
      out.push(a);
    }
    return out;
  }

  function applyGraph(next) {
    const nodes = Array.isArray(next?.nodes) ? next.nodes : [];
    const edges = Array.isArray(next?.edges) ? next.edges : [];
    const first = graph == null;
    const d = diffNodes(first ? null : byId, prints, nodes);
    const nextEdgesPrint = JSON.stringify(edges);
    const edgesChanged = nextEdgesPrint !== edgesPrint;
    const scopeChanged = graph != null && graph.scope !== next?.scope;
    const metaChanged =
      JSON.stringify(graph?.meta ?? null) !==
        JSON.stringify(next?.meta ?? null) ||
      JSON.stringify(graph?.theater ?? null) !==
        JSON.stringify(next?.theater ?? null);
    graph = next;
    byId = indexNodes(nodes);
    prints = d.prints;
    edgesPrint = nextEdgesPrint;

    graphAlarms = nodes
      .filter((n) => n?.type === 'alarm')
      .map((n) => {
        const a = alarmFromNode(n);
        // The graph subtitle is truncated; the SSE message is not.
        const live = liveInfo.get(a.fingerprint);
        if (live) {
          if (live.message.length > a.message.length) a.message = live.message;
          a.detail = live.detail;
        }
        return a;
      });
    const before = new Set(alarms.map((a) => a.fingerprint));
    rebuildAlarms();
    const alarmsChanged =
      alarms.length !== before.size ||
      alarms.some((a) => !before.has(a.fingerprint));
    const newAlarms = freshAlarms(graphAlarms);
    primed = true;

    const changedGraph =
      first ||
      d.added.length ||
      d.removed.length ||
      d.updated.length ||
      edgesChanged ||
      metaChanged;
    return {
      added: d.added,
      removed: d.removed,
      updated: d.updated,
      statusChanged: d.statusChanged,
      edges: edgesChanged,
      graph: Boolean(changedGraph),
      first,
      alarms: alarmsChanged,
      newAlarms,
      scope: scopeChanged,
    };
  }

  function hidden() {
    return Boolean(doc && doc.visibilityState === 'hidden');
  }

  function schedule() {
    if (!started || timer != null) return;
    if (hidden()) {
      pausedHidden = true;
      return;
    }
    timer = clock.setTimeout(
      () => {
        timer = null;
        poll();
      },
      failing ? backoffMs : intervalMs,
    );
  }

  /** Fetch the graph now (Retry now). Resolves when this poll settles. */
  function poll() {
    if (timer != null) {
      clock.clearTimeout(timer);
      timer = null;
    }
    if (inflight) return inflight;
    const seq = ++pollSeq;
    const wanted = scope;
    const request = Promise.resolve()
      .then(() =>
        api.get(`/intel/graph?scope=${encodeURIComponent(wanted)}`, {
          timeoutMs: GRAPH_TIMEOUT_MS,
        }),
      )
      .then(
        (next) => {
          if (seq !== pollSeq) return;
          failing = false;
          error = null;
          lastLiveAt = clock.now();
          const diff = applyGraph(next && typeof next === 'object' ? next : {});
          const st = setStatus('live');
          if (diff.graph || diff.alarms || st)
            emitChange({ ...diff, ...(st || {}) });
        },
        (err) => {
          if (seq !== pollSeq) return;
          failing = true;
          error = errorOf(err, clock.now());
          const next =
            error.kind === 'auth'
              ? 'unauthorized'
              : graph
                ? 'stale'
                : 'offline';
          const st = setStatus(next);
          emitChange({ ...(st || { status: false }), error: true });
        },
      )
      .catch((err) => globalThis.console?.error?.(err))
      .finally(() => {
        if (inflight === request) inflight = null;
        if (seq === pollSeq) schedule();
      });
    inflight = request;
    return request;
  }

  function onVisibility() {
    if (!started || hidden()) return;
    if (pausedHidden || timer == null) {
      pausedHidden = false;
      poll();
    }
  }

  function onAlarm(name, data) {
    if (name !== 'alarm' || !data || typeof data !== 'object') return;
    const alarm = alarmFromPayload(data);
    liveInfo.set(alarm.fingerprint, {
      message: alarm.message,
      detail: alarm.detail,
    });
    while (liveInfo.size > VIEWED_MAX)
      liveInfo.delete(liveInfo.keys().next().value);
    if (graphAlarms.some((a) => a.fingerprint === alarm.fingerprint)) return;
    if (liveAlarms.has(alarm.fingerprint)) return;
    liveAlarms.set(alarm.fingerprint, alarm);
    while (liveAlarms.size > LIVE_ALARMS_MAX) {
      liveAlarms.delete(liveAlarms.keys().next().value);
    }
    rebuildAlarms();
    const newAlarms = freshAlarms([alarm]);
    emitChange({ alarms: true, newAlarms });
  }

  function start() {
    if (started) return;
    started = true;
    doc?.addEventListener?.('visibilitychange', onVisibility);
    if (alarmStream && typeof api?.sse === 'function') {
      stream = api.sse('/events', { events: ['alarm'], onEvent: onAlarm });
    }
    poll();
  }

  function stop() {
    started = false;
    pollSeq += 1;
    if (timer != null) clock.clearTimeout(timer);
    timer = null;
    doc?.removeEventListener?.('visibilitychange', onVisibility);
    stream?.close?.();
    stream = null;
  }

  function on(event, cb) {
    if (event !== 'change' || typeof cb !== 'function') return () => {};
    const entry = (diff) => cb(diff);
    listeners.add(entry);
    return () => listeners.delete(entry);
  }

  function alarmById(id) {
    return alarms.find((a) => a.id === id || a.fingerprint === id) || null;
  }

  function viewedAlarm(alarm) {
    return viewed.has(alarm.fingerprint) || viewed.has(alarm.id);
  }

  function markAlarmViewed(id) {
    const alarm = alarmById(id);
    const key = alarm
      ? alarm.fingerprint
      : typeof id === 'string' && id
        ? id
        : null;
    if (!key || (alarm ? viewedAlarm(alarm) : viewed.has(key))) return false;
    viewed.add(key);
    saveViewed();
    emitChange({ alarms: true, viewed: [alarm ? alarm.id : id] });
    bus?.emit?.('alarm:viewed', { id: alarm ? alarm.id : id });
    return true;
  }

  function isAlarmViewed(id) {
    const alarm = alarmById(id);
    return alarm ? viewedAlarm(alarm) : viewed.has(id);
  }

  /** Unviewed critical alarms (bingo, geofence breach, lost link), oldest first. */
  function unviewedCritical() {
    return alarms.filter((a) => a.critical && !viewedAlarm(a)).reverse();
  }

  /** Vehicle snapshot from its graph node, or null. */
  function vehicle(name) {
    const key = str(name);
    if (!key) return null;
    const node = byId.get(key.startsWith('veh:') ? key : `veh:${key}`);
    if (!node) return null;
    const attrs = node.attrs || {};
    let missionNode = null;
    const edges = Array.isArray(graph?.edges) ? graph.edges : [];
    const flying = edges.find((e) => e?.kind === 'flying' && e.a === node.id);
    if (flying) missionNode = byId.get(flying.b) || null;
    if (!missionNode && attrs.mission)
      missionNode = byId.get(`msn:${attrs.mission}`) || null;
    return {
      ...attrs,
      id: node.id,
      name: node.label || key.replace(/^veh:/, ''),
      label: node.label,
      subtitle: node.subtitle,
      status: node.status,
      lat: node.lat ?? null,
      lon: node.lon ?? null,
      ts_ms: node.ts_ms ?? null,
      missionNode,
    };
  }

  /** Full details for one entity (`GET /intel/entity/{id}`); concurrent calls share one request. */
  function entity(id) {
    const key = str(id);
    if (!key) return Promise.reject(new TypeError('entity needs an id'));
    if (entityInflight.has(key)) return entityInflight.get(key);
    const p = Promise.resolve()
      .then(() => api.get(`/intel/entity/${encodeURIComponent(key)}`))
      .finally(() => entityInflight.delete(key));
    entityInflight.set(key, p);
    return p;
  }

  function setScope(next) {
    const value = next === 'all' ? 'all' : 'theater';
    if (value === scope) return;
    scope = value;
    pollSeq += 1; // a poll for the old scope must not land
    inflight = null;
    emitChange({ scope: true });
    if (started) poll();
  }

  function noteStageInput() {
    lastStageInputAt = clock.now();
  }

  return {
    start,
    stop,
    get: snapshot,
    on,
    refresh: poll,
    vehicle,
    entity,
    node: (id) => byId.get(id) || null,
    unviewedCritical,
    markAlarmViewed,
    isAlarmViewed,
    setScope,
    noteStageInput,
    get lastStageInputAt() {
      return lastStageInputAt;
    },
    get status() {
      return status;
    },
  };
}
