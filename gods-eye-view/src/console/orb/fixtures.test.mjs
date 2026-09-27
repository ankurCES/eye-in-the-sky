/**
 * Shared fixtures for the orb tests: a deterministic intel-graph generator, a
 * recording 2D context, a stub canvas and DOM (the same shape as
 * src/ui/uavMissionPanel.test.mjs's stubDoc: no classList, no style, no
 * querySelector), and a manual clock/rAF. Imported by the other orb tests;
 * it registers no tests of its own (layout.test.mjs checks the generator).
 */

const GROUPS = [
  'air-defense',
  'radar-ew',
  'c2',
  'ground-forces',
  'logistics',
  'infrastructure',
  'naval',
  'air',
  'civilian',
  'unclassified',
];
const STATUSES = ['ok', 'warn', 'critical', 'unknown', 'stale'];
const CONFIDENCE = ['confirmed', 'probable', 'possible'];

/** Small deterministic PRNG (mulberry32). */
export function rng(seed = 1) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * A contract-shaped intel graph (§4) with `total` nodes: the theater, feeds,
 * POIs, vehicles, missions, then contacts, units, equipment, reports, alarms.
 */
export function makeGraph(total = 40, { seed = 7, tracks: trackCount } = {}) {
  const rand = rng(seed);
  const nodes = [];
  const edges = [];
  const add = (node) => {
    nodes.push({
      subtitle: '',
      group: node.type,
      salience: 0.5,
      status: 'ok',
      ts_ms: null,
      lat: null,
      lon: null,
      attrs: {},
      ...node,
    });
  };
  add({
    id: 'thr:default',
    type: 'theater',
    label: 'Redmond (AirSim default)',
    attrs: { active: true },
    salience: 0.95,
  });
  for (const name of ['contacts', 'mission_state', 'sim', 'real_data']) {
    add({
      id: `feed:${name}`,
      type: 'feed',
      label: name,
      status: name === 'real_data' ? 'warn' : 'ok',
      salience: 0.2,
    });
  }
  for (const name of ['North Field', 'South Field', 'East Field']) {
    add({ id: `poi:default:${name}`, type: 'poi', label: name, salience: 0.5 });
    edges.push({
      a: `poi:default:${name}`,
      b: 'thr:default',
      kind: 'in_theater',
    });
  }
  const vehicles = ['Drone1', 'Drone2'];
  for (const [k, name] of vehicles.entries()) {
    add({
      id: `veh:${name}`,
      type: 'vehicle',
      label: name,
      subtitle: 'fuel 64% / BINGO 22% · airborne',
      salience: 0.8,
      status: k ? 'warn' : 'ok',
      attrs: { fuel_pct: 64 - k * 30, bingo_fuel_pct: 22, landed: false },
    });
    edges.push({ a: `veh:${name}`, b: 'thr:default', kind: 'operating_in' });
  }
  add({
    id: 'msn:MSN-1a2b3c4d',
    type: 'mission',
    label: 'Grid search · Drone1',
    subtitle: 'executing · 42%',
    salience: 0.9,
    attrs: { phase: 'executing', vehicle: 'Drone1', progress_pct: 42 },
  });
  edges.push({ a: 'veh:Drone1', b: 'msn:MSN-1a2b3c4d', kind: 'flying' });
  const fixed = nodes.length;
  const remaining = Math.max(0, total - fixed);
  const tracks = trackCount ?? Math.max(0, Math.round(remaining * 0.7));
  const trackIds = [];
  for (let k = 0; k < tracks; k += 1) {
    const id = `trk:T-${((k * 2654435761) >>> 0).toString(16).padStart(8, '0').slice(0, 6)}${k}`;
    const group = GROUPS[Math.floor(rand() * GROUPS.length)];
    const status = STATUSES[Math.floor(rand() * STATUSES.length)];
    trackIds.push(id);
    add({
      id,
      type: 'track',
      label: `Contact ${k}`,
      subtitle: `${CONFIDENCE[k % 3]} · ${1 + (k % 4)} sightings`,
      group,
      salience: Math.round(rand() * 100) / 100,
      status,
      ts_ms: 1_700_000_000_000 + k * 1000,
      attrs: {
        confidence: CONFIDENCE[k % 3],
        threat:
          status === 'unknown'
            ? 'not assessed'
            : status === 'critical'
              ? 'critical'
              : 'high',
        duplicate_count: k % 7 === 0 ? 2 : undefined,
      },
    });
    edges.push({ a: id, b: 'thr:default', kind: 'in_theater' });
    if (k % 5 === 0)
      edges.push({ a: id, b: 'poi:default:North Field', kind: 'near' });
  }
  if (trackIds.length)
    edges.push({ a: 'veh:Drone1', b: trackIds[0], kind: 'tracking' });
  if (trackIds.length)
    edges.push({ a: 'msn:MSN-1a2b3c4d', b: trackIds[0], kind: 'target' });
  let left = total - nodes.length;
  const units = Math.min(left, Math.floor(tracks / 6));
  for (let k = 0; k < units; k += 1) {
    const id = `unit:sam:T-u${k}`;
    add({
      id,
      type: 'unit',
      label: `Element ${k}`,
      status: 'warn',
      salience: 0.6,
    });
    for (let m = k * 3; m < k * 3 + 3 && m < trackIds.length; m += 1) {
      edges.push({ a: trackIds[m], b: id, kind: 'member_of' });
    }
  }
  left = total - nodes.length;
  const equipment = Math.min(left, Math.floor(tracks / 8));
  for (let k = 0; k < equipment; k += 1) {
    const id = `ob:class_${k}`;
    add({ id, type: 'equipment', label: `Class ${k}`, salience: 0.4 });
    for (let m = k; m < trackIds.length; m += equipment || 1)
      edges.push({ a: trackIds[m], b: id, kind: 'is_a' });
  }
  left = total - nodes.length;
  const reports = Math.min(left, 2);
  for (let k = 0; k < reports; k += 1) {
    const id = `rpt:R-${k}`;
    add({
      id,
      type: 'report',
      label: `INTREP R-${k}`,
      status: k ? 'warn' : 'ok',
      salience: 0.6,
    });
    for (let m = k; m < Math.min(trackIds.length, 6); m += 2)
      edges.push({ a: id, b: trackIds[m], kind: 'reports_on' });
  }
  left = total - nodes.length;
  for (let k = 0; k < left; k += 1) {
    const id = `alarm:${k + 1}`;
    add({
      id,
      type: 'alarm',
      label: k % 3 === 0 ? 'Bingo' : 'Detection',
      subtitle: 'Drone1 reached BINGO',
      status: k % 3 === 0 ? 'critical' : 'ok',
      salience: 0.5,
      attrs: {
        seq: k + 1,
        kind: k % 3 === 0 ? 'bingo' : 'detection',
        severity: k % 3 === 0 ? 'critical' : 'info',
      },
    });
    edges.push({
      a: id,
      b:
        k % 2
          ? 'veh:Drone1'
          : trackIds[k % Math.max(1, trackIds.length)] || 'veh:Drone2',
      kind: 'about',
    });
  }
  return {
    schema: 'godseye.intel-graph/v1',
    generated_at_ms: 1_700_000_000_000,
    scope: 'theater',
    theater: { id: 'default', label: 'Redmond (AirSim default)' },
    nodes,
    edges,
    meta: {
      counts: {},
      caveats: [],
      feeds: { contacts: { ok: true, status: 'ok' } },
    },
  };
}

/** Deterministic shuffle (for order-independence tests). */
export function shuffled(list, seed = 3) {
  const rand = rng(seed);
  const out = list.slice();
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rand() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

const DRAW = new Set([
  'drawImage',
  'fill',
  'stroke',
  'fillText',
  'strokeText',
  'fillRect',
  'clearRect',
]);

/**
 * A recording CanvasRenderingContext2D. Method calls land in `calls`
 * ([name, ...args]); property writes land in `sets` ([name, value]).
 */
export function fakeCtx({ width = 800, height = 600 } = {}) {
  const calls = [];
  const sets = [];
  const target = {
    canvas: { width, height },
    calls,
    sets,
    measureText: (text) => ({ width: String(text).length * 6.5 }),
    createRadialGradient: () => ({ addColorStop() {} }),
    createLinearGradient: () => ({ addColorStop() {} }),
    getLineDash: () => [],
    count(name) {
      return calls.filter((c) => c[0] === name).length;
    },
    draws() {
      return calls.filter((c) => DRAW.has(c[0])).length;
    },
    reset() {
      calls.length = 0;
      sets.length = 0;
    },
  };
  const state = {};
  return new Proxy(target, {
    get(t, key) {
      if (key in t) {
        const value = t[key];
        if (
          typeof value === 'function' &&
          !['count', 'draws', 'reset'].includes(key)
        ) {
          return (...args) => {
            calls.push([key, ...args]);
            return value(...args);
          };
        }
        return value;
      }
      if (key in state) return state[key];
      if (typeof key === 'symbol') return undefined;
      return (...args) => {
        calls.push([key, ...args]);
        return undefined;
      };
    },
    set(t, key, value) {
      sets.push([key, value]);
      state[key] = value;
      return true;
    },
  });
}

/** A Path2D stand-in that records its source string. */
export class FakePath2D {
  constructor(d) {
    this.d = d;
  }
}

/** Stub element in the uavMissionPanel.test.mjs style (no classList/style). */
export function stubElement(tag) {
  return {
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    textContent: '',
    parentNode: null,
    append(...kids) {
      for (const kid of kids) {
        if (kid && typeof kid === 'object') kid.parentNode = this;
        this.children.push(kid);
      }
    },
    replaceChildren(...kids) {
      this.children = [];
      this.append(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    getAttribute(k) {
      return this.attrs[k];
    },
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter(
        (f) => f !== fn,
      );
    },
    fire(type, event = {}) {
      for (const fn of this.listeners[type] || []) fn(event);
    },
    remove() {
      if (this.parentNode) {
        this.parentNode.children = this.parentNode.children.filter(
          (c) => c !== this,
        );
        this.parentNode = null;
      }
    },
  };
}

/** Install a stub `document` on globalThis; returns a restore function. */
export function installStubDocument() {
  const previous = globalThis.document;
  const doc = {
    createElement: (tag) => stubElement(tag),
    getElementById: () => null,
    body: stubElement('body'),
    hidden: false,
    listeners: {},
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter(
        (f) => f !== fn,
      );
    },
  };
  globalThis.document = doc;
  return {
    doc,
    restore() {
      if (previous === undefined) delete globalThis.document;
      else globalThis.document = previous;
    },
  };
}

/** A stub canvas whose 2D context records calls. */
export function fakeCanvas(width = 1000, height = 700) {
  const el = stubElement('canvas');
  const ctx = fakeCtx({ width, height });
  el.clientWidth = width;
  el.clientHeight = height;
  el.width = 0;
  el.height = 0;
  el.ctx = ctx;
  el.getContext = () => ctx;
  el.getBoundingClientRect = () => ({ left: 10, top: 20, width, height });
  const parent = stubElement('div');
  parent.append(el);
  return el;
}

/** Find the first node in a stub tree matching a predicate. */
export function findNode(root, predicate) {
  if (!root || typeof root !== 'object') return null;
  if (predicate(root)) return root;
  for (const child of root.children || []) {
    const hit = findNode(child, predicate);
    if (hit) return hit;
  }
  return null;
}

export function findAll(root, predicate, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (predicate(root)) out.push(root);
  for (const child of root.children || []) findAll(child, predicate, out);
  return out;
}

/**
 * A manual clock + rAF + timers environment for createOrb({env}).
 * `flush()` runs queued animation frames; `advance(ms)` moves the clock and
 * fires due timers.
 */
export function fakeEnv(extra = {}) {
  let now = 1000;
  let seq = 0;
  const frames = new Map();
  const timers = new Map();
  const sprites = [];
  const env = {
    now: () => now,
    requestAnimationFrame: (cb) => {
      seq += 1;
      frames.set(seq, cb);
      return seq;
    },
    cancelAnimationFrame: (id) => frames.delete(id),
    setTimeout: (fn, ms) => {
      seq += 1;
      timers.set(seq, { fn, at: now + (ms || 0) });
      return seq;
    },
    clearTimeout: (id) => timers.delete(id),
    devicePixelRatio: 2,
    matchMedia: null,
    ResizeObserver: undefined,
    IntersectionObserver: undefined,
    document: null,
    storage: null,
    Path2D: FakePath2D,
    createCanvas: (w, h) => {
      const ctx = fakeCtx({ width: w, height: h });
      const canvas = { width: w, height: h, getContext: () => ctx, ctx };
      sprites.push(canvas);
      return canvas;
    },
    ...extra,
  };
  return {
    env,
    sprites,
    get pendingFrames() {
      return frames.size;
    },
    get now() {
      return now;
    },
    flush(max = 50) {
      let ran = 0;
      while (frames.size && ran < max) {
        const batch = [...frames.entries()];
        frames.clear();
        for (const [, cb] of batch) cb(now);
        ran += 1;
      }
      return ran;
    },
    advance(ms, { frames: runFrames = true } = {}) {
      const target = now + ms;
      for (;;) {
        const due = [...timers.entries()]
          .filter(([, t]) => t.at <= target)
          .sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, t] = due[0];
        now = Math.max(now, t.at);
        timers.delete(id);
        t.fn();
      }
      now = target;
      if (runFrames) this.flush();
    },
    step(ms) {
      now += ms;
    },
  };
}
