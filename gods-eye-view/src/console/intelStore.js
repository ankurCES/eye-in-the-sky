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
 * - `scope` — `theater|all`;
 * - `view` — the simulated wargame view, `umpire|blue`, while a session runs
 *   (`graph.meta.wargame.active`), else null (WG §5.3.3);
 * - `truth` — whether the last graph was asked for with `truth=1`;
 * - `wargameSession` — the running session's id, or null.
 *
 * Wargame view (WG §5.3.3): Umpire is the default when a session starts;
 * the choice is kept per session in storage (try/catch: a failing storage
 * keeps the flag in memory). Umpire view asks for `/intel/graph?truth=1`.
 * It is a presentation filter, not a secrecy boundary. Outside a session
 * the request is exactly the ISR one (`/intel/graph?scope=…`).
 *
 * Every `change` diff carries `theaterChanged`: `{from, to}` (each
 * `{id, epoch, label, place}`) when `graph.theater.id` or its epoch changed
 * (WG §4.2.4), else null. The orb runs its theater transition on it.
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
/** Wargame views (WG §5.3.3); Umpire is the default at a session's start. */
export const WARGAME_VIEWS = Object.freeze(['umpire', 'blue']);
export const DEFAULT_WARGAME_VIEW = 'umpire';
/** Storage key for the per-session view choice: `{[session_id]: view}`. */
export const WARGAME_VIEW_KEY = 'ic.wargame.view.v1';
const VIEW_SESSIONS_MAX = 20;

/** The running wargame session id in a graph (`meta.wargame`), or null. */
export function wargameSessionOf(graph) {
  const w = graph?.meta?.wargame;
  return w && typeof w === 'object' && w.active === true
    ? typeof w.session_id === 'string' && w.session_id
      ? w.session_id
      : 'session'
    : null;
}

/**
 * The graph request path. Without `truth` it is exactly the ISR request;
 * Umpire view adds `&truth=1` (WG §3.2).
 */
export function graphPath(scope, truth = false) {
  const base = `/intel/graph?scope=${encodeURIComponent(scope)}`;
  return truth ? `${base}&truth=1` : base;
}

/** Edge kinds only an Umpire (`truth=1`) graph carries (WG §3.2). */
const TRUTH_EDGE_KINDS = new Set(['axis', 'threatens', 'correlates']);
/** The attacker a Blue view names for a red engagement (§3.2 Fog). */
const MASKED_ATTACKER = Object.freeze({
  red_shot: 'Red air defence (not identified)',
  red_ground: 'Red ground forces (not identified)',
});

function blueNode(node, showRed) {
  const a = node?.attrs && typeof node.attrs === 'object' ? node.attrs : null;
  if (node?.type === 'force') {
    if (!showRed && a?.side !== 'blue') return null;
    if (!a || !Object.hasOwn(a, 'correlated')) return node;
    const attrs = { ...a };
    delete attrs.correlated; // truth only (§3.2)
    return { ...node, attrs };
  }
  if (node?.type === 'vector')
    return showRed || (a?.kind === 'corridor' && a?.side !== 'red')
      ? node
      : null;
  if (showRed || !a) return node;
  if (node.type === 'engagement') {
    let next = a;
    if (Object.hasOwn(MASKED_ATTACKER, a.kind))
      next = {
        ...next,
        attacker: null,
        attacker_label: MASKED_ATTACKER[a.kind],
        p_notional: null,
        inputs: [],
      };
    if (a.kind === 'blue_strike' && !(Number(a.bda?.looks) >= 1))
      next = { ...next, outcome_hidden: true };
    if (next.outcome_hidden === true && next.outcome != null)
      next = { ...next, outcome: null };
    return next === a ? node : { ...node, attrs: next };
  }
  if (node.type === 'vehicle' && a.wargame_lost_by != null)
    return { ...node, attrs: { ...a, wargame_lost_by: null } };
  return node;
}

/**
 * The Blue view of an Umpire graph, fogged as the host fogs it (WG §3.2):
 * no `correlated[]` and no truth-only edges; unless the session reveals
 * red, no force that is not blue, only non-red corridors, a red attacker
 * masked, an unconfirmed strike outcome hidden, no downing unit named, and
 * red counts cut to `seen`. A switch to Blue drops truth with it at once,
 * not when (or if) the Blue graph lands. Outside a session: the graph.
 */
export function blueViewOf(graph) {
  const wg = graph?.meta?.wargame;
  if (!wg || typeof wg !== 'object' || wg.active !== true) return graph;
  const showRed = wg.reveal_red === true;
  const nodes = [];
  const dropped = new Set();
  for (const node of Array.isArray(graph.nodes) ? graph.nodes : []) {
    const kept = blueNode(node, showRed);
    if (kept) nodes.push(kept);
    else if (node) dropped.add(node.id);
  }
  const edges = (Array.isArray(graph.edges) ? graph.edges : []).filter(
    (e) =>
      !TRUTH_EDGE_KINDS.has(e?.kind) &&
      !dropped.has(e?.a) &&
      !dropped.has(e?.b),
  );
  const counts = wg.counts && typeof wg.counts === 'object' ? wg.counts : {};
  const seen = Number(counts.red?.seen);
  const wargame = {
    ...wg,
    truth_view: showRed,
    counts: showRed
      ? counts
      : { ...counts, red: { seen: Number.isFinite(seen) ? seen : 0 } },
  };
  return { ...graph, nodes, edges, meta: { ...graph.meta, wargame } };
}

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

/** `{id, epoch, label, place}` of a graph theater block, or null. */
function theaterRefOf(theater) {
  if (!theater || typeof theater !== 'object') return null;
  if (typeof theater.id !== 'string' || !theater.id) return null;
  return {
    id: theater.id,
    epoch: Number.isInteger(theater.epoch) ? theater.epoch : null,
    label: typeof theater.label === 'string' ? theater.label : null,
    place: typeof theater.place === 'string' ? theater.place : null,
  };
}

/**
 * The theater change between two graph theater blocks (WG §4.2.4):
 * `{from, to}` when the id changed, or when both carry an epoch and the
 * epochs differ; otherwise null. A missing epoch on either side (an older
 * server, or the first graph) is not a change by itself, so a server that
 * starts reporting epochs doesn't fake a transition.
 */
export function theaterChangeBetween(prevTheater, nextTheater) {
  const from = theaterRefOf(prevTheater);
  const to = theaterRefOf(nextTheater);
  if (!from || !to) return null;
  if (from.id !== to.id) return { from, to };
  if (from.epoch != null && to.epoch != null && from.epoch !== to.epoch)
    return { from, to };
  return null;
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
  // Wargame view (WG §5.3.3): the session it belongs to, the choice, and
  // whether the graph in hand was asked for with truth=1.
  let session = null;
  let view = DEFAULT_WARGAME_VIEW;
  let truth = false;
  let refetched = false; // one immediate re-poll when the wanted view changed
  const views = loadViews();

  function loadViews() {
    try {
      const raw = storage?.getItem?.(WARGAME_VIEW_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      const out = new Map();
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const [id, v] of Object.entries(parsed))
          if (WARGAME_VIEWS.includes(v)) out.set(id, v);
      }
      return out;
    } catch {
      return new Map();
    }
  }

  function saveViews() {
    try {
      while (views.size > VIEW_SESSIONS_MAX)
        views.delete(views.keys().next().value);
      storage?.setItem?.(
        WARGAME_VIEW_KEY,
        JSON.stringify(Object.fromEntries(views)),
      );
    } catch {
      /* private window or quota: the view still holds for this page */
    }
  }

  /** Umpire view in a running session asks the host for truth. */
  function truthWanted() {
    return session != null && view === 'umpire';
  }

  /** Follow the session in a new graph: a new session starts in its kept view, else Umpire. */
  function noteSession(next) {
    const id = wargameSessionOf(next);
    if (id === session) return;
    session = id;
    if (id) view = views.get(id) || DEFAULT_WARGAME_VIEW;
  }

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
      view: session ? view : null,
      truth,
      wargameSession: session,
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
      view: false,
      // Always present, so the orb takes the store's answer (null: none).
      theaterChanged: null,
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
    const theaterChanged = first
      ? null
      : theaterChangeBetween(graph?.theater, next?.theater);
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
      theaterChanged,
    };
  }

  function hidden() {
    return Boolean(doc && doc.visibilityState === 'hidden');
  }

  /** Schedule the next poll: `delayMs` when given (a view re-poll), else the cadence. */
  function schedule(delayMs = null) {
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
      delayMs ?? (failing ? backoffMs : intervalMs),
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
    const wantTruth = truthWanted();
    let again = false;
    const request = Promise.resolve()
      .then(() =>
        api.get(graphPath(wanted, wantTruth), {
          timeoutMs: GRAPH_TIMEOUT_MS,
        }),
      )
      .then(
        (next) => {
          if (seq !== pollSeq) return;
          failing = false;
          error = null;
          lastLiveAt = clock.now();
          const body = next && typeof next === 'object' ? next : {};
          const wasSession = session;
          truth = wantTruth;
          noteSession(body);
          const diff = applyGraph(body);
          const st = setStatus('live');
          const viewMoved = wasSession !== session;
          if (diff.graph || diff.alarms || st || viewMoved)
            emitChange({ ...diff, ...(st || {}), view: viewMoved });
          if (viewMoved) announceView();
          // A session started or ended: ask again at once in the right view
          // (once, so a host that disagrees can't loop the poll).
          again = truthWanted() !== truth && !refetched;
          refetched = again;
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
        if (seq === pollSeq) schedule(again ? 0 : null);
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

  /**
   * Full details for one entity (`GET /intel/entity/{id}`); concurrent calls
   * share one request. Umpire view in a running session adds `?truth=1`
   * (WG §3.2), so a red force opens; otherwise the path is exactly the ISR
   * one.
   */
  function entity(id) {
    const key = str(id);
    if (!key) return Promise.reject(new TypeError('entity needs an id'));
    const wantTruth = truthWanted();
    const slot = wantTruth ? `${key}\u0000truth` : key;
    if (entityInflight.has(slot)) return entityInflight.get(slot);
    const path = `/intel/entity/${encodeURIComponent(key)}${wantTruth ? '?truth=1' : ''}`;
    const p = Promise.resolve()
      .then(() => api.get(path))
      .finally(() => entityInflight.delete(slot));
    entityInflight.set(slot, p);
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

  /** Tell the bus which view the console shows (the orb, the map overlay). */
  function announceView() {
    bus?.emit?.('wargame:view', {
      view: session ? view : null,
      truth: truthWanted(),
      session_id: session,
    });
  }

  /**
   * Choose the wargame view (WG §5.3.3): `'umpire'` or `'blue'`. Kept per
   * session in storage when it works, in memory always. A poll for the old
   * view must not land; the new one is asked for at once. Leaving Umpire
   * drops the truth in hand now (`blueViewOf`), so search, the inspector
   * and the rail never show it under Blue, even while the host is down.
   * The view is announced first, so the orb hides red without a fade.
   * @returns {boolean} whether the view changed
   */
  function setView(next) {
    if (!WARGAME_VIEWS.includes(next) || next === view) return false;
    view = next;
    if (session) {
      views.delete(session);
      views.set(session, next);
      saveViews();
    }
    pollSeq += 1;
    inflight = null;
    refetched = false;
    let diff = null;
    if (truth && !truthWanted()) {
      truth = false;
      if (graph) diff = applyGraph(blueViewOf(graph));
    }
    announceView();
    emitChange({ ...(diff || {}), view: true });
    if (started) poll();
    return true;
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
    setView,
    noteStageInput,
    get lastStageInputAt() {
      return lastStageInputAt;
    },
    /** The wargame view while a session runs, else null. */
    get view() {
      return session ? view : null;
    },
    get status() {
      return status;
    },
  };
}
