/**
 * The information orb component (contract §7, UX spec §4, §9, §11.1).
 *
 *   createOrb(canvas, {onSelect, onHover, a11yHost?, onInput?, onAction?, onNotice?, env?})
 *     → { setGraph, highlight, filter, focus, select, resize, destroy, project,
 *         onFrame, setViewport, snapshot, setOptions, reveal, reframe,
 *         setView, view, … }
 *
 * Default view: the first real picture, and every reset (double-click on
 * empty space, Home), frames the populated latitudes (framingOrientation).
 * After that the orb never turns itself except as the spec allows (select
 * easing, analyst focus, idle rotation, and the theater transition's
 * reframe(), which waits for 3 s without stage input and nothing selected,
 * focused or filtered: WG spec §4.2.4).
 *
 * A theater change (intelStore `diff.theaterChanged`, or a new
 * `graph.theater` id or epoch) is one event: no per-item ripples or arrival
 * notices, one ripple on the new theater (which takes the pole), and one
 * `onNotice({kind:'theater', text, holdMs, announce:'polite', …})` caption.
 *
 * Render on demand: a dirty flag drives requestAnimationFrame, which keeps
 * running only while the camera eases, a ripple plays, a node fades, or the
 * optional 2°/s idle rotation turns (30 fps; never under reduced motion, never
 * within 30 s of input). The loop pauses when the document is hidden, when
 * the canvas is off screen, and when `setOptions({paused:true})` (tracking).
 * The canvas backing store changes only on resize(); selecting, opening the
 * inspector plate and focusing move the projection with setViewport().
 *
 * The simulated wargame (WG §5.3.3–§5.3.6): forces draw their frames by
 * side and state, engagements and vectors their burst and arrow, in the
 * session bands. `setView({umpire})` switches Blue view (every force that is
 * not provably blue and every red axis hidden, and the Umpire-only edges
 * with them) and Umpire view (everything, the default). It is a
 * presentation filter over the graph the shell passes, not a secrecy
 * boundary; switching is not an arrival, so it rings and announces nothing.
 *
 * Every platform touchpoint (rAF, timers, observers, matchMedia, storage,
 * canvas creation) is feature-checked and injectable through `env`, so the
 * component runs under node:test with a stub DOM and a recording context.
 */

import { h, replaceKids, setClass, setHidden } from '../../ui/uavDom.js';
import { createOrbListView, createOrbTwin } from './a11y.js';
import {
  EASE_THRESHOLD_DEG,
  FRAME_ELEVATION_DEG,
  ZOOM_MIN,
  centroid,
  createCamera,
  createPickGrid,
  easeInOutCubic,
  easeOut,
  meanLatLon,
  orientationFacing,
  orientationFraming,
  pullWithin,
  quatSlerp,
} from './camera.js';
import {
  COLORS,
  isOrbType,
  missionPhaseClass,
  statusColor,
  statusKey,
} from './glyphs.js';
import {
  filterForView,
  isWargameType,
  wargameGlyphStyle,
  wargameStyleKey,
} from './wargameStyles.js';
import { safeText, siteCategory, theaterChangedText } from './placeText.js';
import { computeLayout, toVector } from './layout.js';
import {
  MARGIN_LABEL_MAX,
  RING,
  TIER,
  bandCaptions,
  createRenderer,
  selectEdges,
} from './renderer.js';
import {
  formatZ,
  marginSubtitle,
  marginTitle,
  nodeLabel,
  nodeSubtitle,
  splitSegments,
} from './text.js';

export { createOrbListView };

const DEG = Math.PI / 180;

export const ORB_TIMING = Object.freeze({
  quick: 120,
  settle: 200,
  camera: 320,
  travel: 600,
  ripple: 900,
  fade: 300,
  newTag: 60_000,
  staticRing: 4_000,
  batchWindow: 5_000,
  rippleGap: 400,
  idleAfter: 30_000,
  idleFrame: 1000 / 30,
  /** reframe() waits this long after the last stage input (WG §4.2.4). */
  reframeQuiet: 3_000,
  /** The theater-change caption stays up to this long. */
  theaterCaption: 20_000,
});
export const IDLE_DEG_PER_S = 2;
export const LABEL_BUDGETS = Object.freeze({ wide: 12, compact: 6, narrow: 4 });
export const ROTATION_STORAGE_KEY = 'ic.orb.rotation';
const SLOW_FRAME_MS = 6;
const SLOW_FRAMES = 3;
const FAST_FRAME_MS = 3;
const FAST_FRAMES = 120;
const MAX_BACKING_PIXELS = 4.5e6;
const CAPTION_STRIP = 40;

/** Density factor k = clamp(1.4 − N/400, 0.65, 1.4) (spec §4.3). */
export function densityFactor(n) {
  return Math.min(1.4, Math.max(0.65, 1.4 - n / 400));
}

/** Vectors are drawn smaller than other glyphs (WG §5.3.5). */
export const VECTOR_RADIUS_FACTOR = 0.7;

/**
 * Node radius in CSS px: (4 + 8·salience)·k, vehicles at least 10, vectors
 * at 0.7×.
 */
export function nodeRadius(node, n) {
  const salience = Math.min(1, Math.max(0, Number(node?.salience) || 0));
  const r = (4 + 8 * salience) * densityFactor(n);
  if (node?.type === 'vector') return r * VECTOR_RADIUS_FACTOR;
  return node?.type === 'vehicle' ? Math.max(10, r) : r;
}

/** Device pixel ratio: min(dpr, 2), lowered to 1.5 above 4.5 MP of backing store. */
export function effectiveDpr(devicePixelRatio, cssWidth, cssHeight) {
  let dpr = Math.min(Number(devicePixelRatio) || 1, 2);
  if (dpr > 1.5 && cssWidth * cssHeight * dpr * dpr > MAX_BACKING_PIXELS)
    dpr = 1.5;
  return dpr;
}

/** Node types the default view frames when any are present (spec §4.1 intent). */
export const FRONT_WORTHY = Object.freeze(['vehicle', 'mission', 'track']);
/** The framed view keeps the most salient vehicle within this of its target. */
export const LEAD_VEHICLE_MAX_DEG = 60;

/**
 * The default (load and reset) orientation: the salience-weighted centre of
 * the front-worthy nodes (vehicles, missions, contacts; all nodes when there
 * are none), as a mean latitude and circular-mean longitude (see
 * meanLatLon), sits FRAME_ELEVATION_DEG above the centre of the view, north
 * up. The most salient vehicle is kept within LEAD_VEHICLE_MAX_DEG of that
 * centre so it is always on the labelled side. `fallback` (the tilted rest
 * orientation) for an empty picture. Pure.
 * @param {object} layout computeLayout() result
 * @param {number[]} fallback quaternion
 */
export function framingOrientation(layout, fallback) {
  const collect = (keep) => {
    const points = [];
    for (let i = 0; i < (layout?.n || 0); i += 1) {
      const node = layout.nodes[i];
      if (!keep(node)) continue;
      points.push({
        lat: layout.lat[i],
        lon: layout.lon[i],
        weight: Number(node?.salience),
      });
    }
    return points;
  };
  let points = collect((node) => FRONT_WORTHY.includes(node?.type));
  if (!points.length) points = collect(() => true);
  const mean = meanLatLon(points);
  if (!mean) return fallback;
  let target = toVector(mean.lat, mean.lon);
  // Keep the lead vehicle on the visible, labelled side: at most 60° from
  // the target, so at most 72° off the view axis (z ≥ 0.31).
  let lead = -1;
  for (let i = 0; i < (layout?.n || 0); i += 1) {
    if (layout.nodes[i]?.type !== 'vehicle') continue;
    const w = Number(layout.nodes[i].salience) || 0;
    if (lead < 0 || w > (Number(layout.nodes[lead].salience) || 0)) lead = i;
  }
  if (lead >= 0) {
    target = pullWithin(
      target,
      [
        layout.pos[lead * 3],
        layout.pos[lead * 3 + 1],
        layout.pos[lead * 3 + 2],
      ],
      LEAD_VEHICLE_MAX_DEG,
    );
  }
  return orientationFraming(target, FRAME_ELEVATION_DEG);
}

/** Width the "New" tag takes from a margin label's first line. */
const NEW_TAG_PX = 40;

/** Bands whose glyphs are nudged apart on screen: the polar cap. */
const NUDGED_BANDS = new Set(['theater', 'feed', 'poi']);
/** Bands the cap keeps clear of without moving them (own force below it). */
const CAP_NEIGHBOUR_BANDS = new Set(['vehicle', 'mission']);
/** Minimum screen gap between two cap glyphs (review: at least 4 px). */
export const CAP_GLYPH_GAP_PX = 4;
/** A cap glyph never moves farther than this from its projected point. */
export const CAP_NUDGE_MAX_PX = 28;

/**
 * Screen-space de-overlap for the polar cap. Near the pole a band ring
 * projects to an ellipse smaller than its glyphs (+80° is about 0.05 R tall),
 * so theater, feed and place glyphs are pushed apart, pairwise along the line
 * between them, until every pair is CAP_GLYPH_GAP_PX clear (vehicles and
 * missions are fixed obstacles). Only front-facing glyphs move; offsets are
 * written to `nudge` (x, y per node) and added to `proj` in place, so
 * drawing, labels, leaders and picking all use the same point.
 * @param {Float32Array} proj camera.projectAll() output (x, y, z, scale)
 * @param {number} n node count
 * @param {string[]} band layout band per node
 * @param {(i:number)=>number} radius on-screen glyph radius (0 = not drawn)
 * @param {Float32Array} nudge n × 2, overwritten
 * @returns {number} how many glyphs moved
 */
export function separateCap(proj, n, band, radius, nudge, rounds = 24) {
  nudge.fill(0);
  const movable = [];
  const fixed = [];
  for (let i = 0; i < n; i += 1) {
    if (!(proj[i * 4 + 2] >= 0)) continue;
    if (NUDGED_BANDS.has(band[i])) movable.push(i);
    else if (CAP_NEIGHBOUR_BANDS.has(band[i])) fixed.push(i);
  }
  if (!movable.length) return 0;
  const r = new Map();
  for (const i of [...movable, ...fixed]) r.set(i, Math.max(0, radius(i)));
  const x = new Map(movable.map((i) => [i, proj[i * 4]]));
  const y = new Map(movable.map((i) => [i, proj[i * 4 + 1]]));
  const at = (i) =>
    x.has(i) ? [x.get(i), y.get(i)] : [proj[i * 4], proj[i * 4 + 1]];
  // Keep a glyph within CAP_NUDGE_MAX_PX of its projected point while the
  // pushes run (not after them), so a pinned glyph's neighbours take up the
  // rest of the gap instead of the clamp undoing it at the end.
  const moveTo = (i, px, py) => {
    let ox = px - proj[i * 4];
    let oy = py - proj[i * 4 + 1];
    const len = Math.hypot(ox, oy);
    if (len > CAP_NUDGE_MAX_PX) {
      ox *= CAP_NUDGE_MAX_PX / len;
      oy *= CAP_NUDGE_MAX_PX / len;
    }
    x.set(i, proj[i * 4] + ox);
    y.set(i, proj[i * 4 + 1] + oy);
  };
  for (let round = 0; round < rounds; round += 1) {
    let pushed = false;
    for (let a = 0; a < movable.length; a += 1) {
      const i = movable[a];
      if (!r.get(i)) continue;
      for (let b = a + 1; b < movable.length + fixed.length; b += 1) {
        const j = b < movable.length ? movable[b] : fixed[b - movable.length];
        if (!r.get(j)) continue;
        const [xi, yi] = at(i);
        const [xj, yj] = at(j);
        const want = r.get(i) + r.get(j) + CAP_GLYPH_GAP_PX;
        let dx = xi - xj;
        let dy = yi - yj;
        let d = Math.hypot(dx, dy);
        if (d >= want - 1e-3) continue;
        if (d < 1e-3) {
          // Coincident: a fixed, deterministic direction per pair.
          const angle = (i * 2.399963 + j) % (2 * Math.PI);
          dx = Math.cos(angle);
          dy = Math.sin(angle);
          d = 1;
        }
        const deficit = want - Math.hypot(xi - xj, yi - yj);
        const share = x.has(j) ? deficit / 2 : deficit;
        moveTo(i, xi + (dx / d) * share, yi + (dy / d) * share);
        if (x.has(j)) {
          // j takes whatever i could not (i may be pinned at its limit).
          const rest = deficit - Math.hypot(x.get(i) - xi, y.get(i) - yi);
          moveTo(j, xj - (dx / d) * rest, yj - (dy / d) * rest);
        }
        pushed = true;
      }
    }
    if (!pushed) break;
  }
  let moved = 0;
  for (const i of movable) {
    const ox = x.get(i) - proj[i * 4];
    const oy = y.get(i) - proj[i * 4 + 1];
    if (Math.hypot(ox, oy) < 0.01) continue;
    nudge[i * 2] = ox;
    nudge[i * 2 + 1] = oy;
    proj[i * 4] += ox;
    proj[i * 4 + 1] += oy;
    moved += 1;
  }
  return moved;
}

/**
 * Store statuses in which the drawn picture is no longer live (spec §4.6):
 * `stale` is what a failed poll gives once a picture has loaded, so it must
 * restyle the orb exactly like `offline` (review: the orb looked live while
 * the rail said "not live").
 */
const NOT_LIVE = new Set(['stale', 'offline', 'unauthorized']);

/** Whether an orb `pictureStatus` means the picture is not live. */
export function pictureNotLive(status) {
  return NOT_LIVE.has(status);
}

/** A theater reference ({id, epoch, label} or a bare id) as an object. */
function theaterRef(value) {
  if (typeof value === 'string' && value) return { id: value };
  if (value && typeof value === 'object' && typeof value.id === 'string')
    return value;
  return null;
}

/**
 * The theater change a graph update carries (WG §4.2.4): intelStore's
 * `diff.theaterChanged = {from, to}` when the store says (null when it says
 * nothing changed); otherwise derived from `graph.theater` id and epoch.
 * @returns {{from: object|null, to: object}|null}
 */
export function theaterChangeOf(diff, prevGraph, nextGraph) {
  if (diff && typeof diff === 'object' && 'theaterChanged' in diff) {
    const given = diff.theaterChanged;
    const to = theaterRef(given?.to) ?? theaterRef(nextGraph?.theater);
    return given && to ? { from: theaterRef(given.from), to } : null;
  }
  const a = theaterRef(prevGraph?.theater);
  const b = theaterRef(nextGraph?.theater);
  if (!a || !b) return null;
  const epoch = (t) => (Number.isFinite(t.epoch) ? t.epoch : null);
  if (a.id === b.id && epoch(a) === epoch(b)) return null;
  return { from: a, to: b };
}

/**
 * The orb node for the theater a change moved to: `thr:{to.id}` when that is
 * in the picture, else the active theater node, else null.
 * @param {{to: {id: string}}|null} change
 * @param {object[]} nodes graph nodes
 */
export function theaterNodeId(change, nodes) {
  const list = Array.isArray(nodes) ? nodes : [];
  const raw = change?.to?.id;
  if (typeof raw === 'string' && raw) {
    const id = raw.startsWith('thr:') ? raw : `thr:${raw}`;
    if (list.some((node) => node?.id === id)) return id;
  }
  const active = list.find(
    (node) => node?.type === 'theater' && node?.attrs?.active === true,
  );
  return active ? active.id : null;
}

/**
 * The site band's caption numbers (WG §4.2.6): drawn on the orb, omitted
 * (fetched but not drawn: capped out of the graph or sector overflow), and
 * whether the map data feed is degraded.
 * @param {object} graph the intel graph (reads `meta.sites`)
 * @param {object} layout computeLayout() result
 */
export function siteBandState(graph, layout) {
  const meta = graph?.meta?.sites;
  const inGraph = layout?.counts?.site || 0;
  const drawn = Math.max(0, inGraph - (layout?.overflow?.site || 0));
  const total = Number.isFinite(meta?.total)
    ? Math.max(meta.total, inGraph)
    : inGraph;
  return {
    count: drawn,
    omitted: Math.max(0, total - drawn),
    degraded: meta?.degraded === true,
  };
}

/** Whether the graph says the detections (contacts) feed is down. */
export function detectionsDown(graph) {
  const feeds = graph?.meta?.feeds;
  for (const name of ['contacts', 'detections']) {
    const feed = feeds?.[name];
    if (
      feed &&
      (feed.status === 'critical' ||
        (feed.ok === false && feed.status !== 'warn'))
    ) {
      return true;
    }
  }
  return (graph?.nodes || []).some(
    (node) =>
      node?.type === 'feed' &&
      (node.id === 'feed:contacts' || /detection/i.test(String(node.id))) &&
      node.status === 'critical',
  );
}

function confidenceKey(value) {
  const word = String(value || '').toLowerCase();
  if (word.startsWith('confirm')) return 'confirmed';
  if (word.startsWith('probab')) return 'probable';
  if (word.startsWith('possib')) return 'possible';
  return '';
}

function truncate(text, max) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function resolveEnv(overrides = {}) {
  const g = globalThis;
  const now = overrides.now ?? (() => g.performance?.now?.() ?? Date.now());
  const setT = overrides.setTimeout ?? g.setTimeout?.bind(g);
  const clearT = overrides.clearTimeout ?? g.clearTimeout?.bind(g);
  const raf =
    overrides.requestAnimationFrame ??
    (typeof g.requestAnimationFrame === 'function'
      ? g.requestAnimationFrame.bind(g)
      : (cb) => setT?.(() => cb(now()), 16));
  const caf =
    overrides.cancelAnimationFrame ??
    (typeof g.cancelAnimationFrame === 'function'
      ? g.cancelAnimationFrame.bind(g)
      : (id) => clearT?.(id));
  let storage = overrides.storage;
  if (storage === undefined) {
    try {
      storage = g.localStorage ?? null;
    } catch {
      storage = null;
    }
  }
  return {
    now,
    wallNow: overrides.wallNow ?? (() => Date.now()),
    raf,
    caf,
    setTimeout: (fn, ms) => {
      const id = setT?.(fn, ms);
      id?.unref?.();
      return id;
    },
    clearTimeout: (id) => clearT?.(id),
    devicePixelRatio: () =>
      overrides.devicePixelRatio ?? g.devicePixelRatio ?? 1,
    matchMedia:
      overrides.matchMedia ??
      (typeof g.matchMedia === 'function' ? g.matchMedia.bind(g) : null),
    ResizeObserver:
      'ResizeObserver' in overrides
        ? overrides.ResizeObserver
        : g.ResizeObserver,
    IntersectionObserver:
      'IntersectionObserver' in overrides
        ? overrides.IntersectionObserver
        : g.IntersectionObserver,
    document: 'document' in overrides ? overrides.document : g.document,
    storage,
    createCanvas: overrides.createCanvas,
    Path2D: 'Path2D' in overrides ? overrides.Path2D : g.Path2D,
  };
}

/**
 * @param {HTMLCanvasElement} canvas the stage canvas (sized by CSS)
 * @param {{onSelect?:(id:string|null)=>void, onHover?:(id:string|null)=>void,
 *   a11yHost?:HTMLElement, onInput?:()=>void,
 *   onAction?:(action:{action:string, id?:string, cleared?:string|null})=>void,
 *   onNotice?:(notice:{kind:string, count:number, ids:string[]})=>void,
 *   env?:object}} [options]
 */
export function createOrb(canvas, options = {}) {
  const { onSelect, onHover, onInput, onAction, onNotice } = options;
  const env = resolveEnv(options.env);
  const camera = createCamera();
  const rendererEnv = { now: env.now, Path2D: env.Path2D };
  if (env.createCanvas) rendererEnv.createCanvas = env.createCanvas;
  const renderer = createRenderer(rendererEnv);
  const grid = createPickGrid();
  const ctx = canvas?.getContext?.('2d') ?? null;

  const state = {
    destroyed: false,
    graph: { nodes: [], edges: [] },
    /** The graph as the shell passed it; `graph` is it filtered for the view. */
    rawGraph: { nodes: [], edges: [] },
    view: { umpire: true },
    layout: computeLayout(null),
    vms: [],
    proj: new Float32Array(0),
    nudge: new Float32Array(0),
    order: new Uint32Array(0),
    selected: null,
    hovered: null,
    active: null,
    highlight: null,
    focus: null,
    filter: null,
    newAt: new Map(),
    behind: new Map(),
    ripples: [],
    ghosts: [],
    arrivals: [],
    lastRippleAt: -Infinity,
    sizeFrom: null,
    cameraTween: null,
    viewportTween: null,
    viewportExplicit: null,
    cssW: 0,
    cssH: 0,
    dpr: 1,
    raf: 0,
    dirty: true,
    lastFrameAt: 0,
    lastInputAt: env.now(),
    lastInputNotice: -Infinity,
    pointerInside: false,
    docHidden: Boolean(env.document?.hidden),
    offscreen: false,
    twinFocused: false,
    ladder: 0,
    slowFrames: 0,
    fastFrames: 0,
    lastFrameMs: 0,
    frames: 0,
    rotation: readRotation(),
    detectionsDown: false,
    idleTimer: null,
    houseTimer: null,
    labelItems: [],
    framed: false,
    userOriented: false,
    feedDown: new Map(),
  };
  const opts = {
    labelBudget: LABEL_BUDGETS.wide,
    labelMode: 'margin',
    idle: true,
    reducedMotion: false,
    paused: false,
    pictureStatus: 'live',
    lastLiveAt: null,
  };
  const frameListeners = new Set();
  const cleanups = [];

  function readRotation() {
    try {
      return env.storage?.getItem?.(ROTATION_STORAGE_KEY) !== 'off';
    } catch {
      return true;
    }
  }

  // ---- DOM: canvas attributes, the listbox twin, margin labels -------------------

  canvas?.setAttribute?.('aria-hidden', 'true');
  canvas?.setAttribute?.('tabindex', '-1');
  if (canvas) setClass(canvas, 'ic-orb-canvas', true);

  const twinHost = options.a11yHost ?? canvas?.parentNode ?? null;
  const twin = createOrbTwin(twinHost, {
    onKeyDown: (event) => onKey(event),
    onFocusChange: (focused) => {
      state.twinFocused = focused;
      if (focused && !state.active)
        setActive(state.selected ?? twin.order[0] ?? null, false);
      invalidate();
    },
    onOptionClick: (id) => userSelect(id),
  });

  const labelParent = canvas?.parentNode ?? null;
  const labelLayer = labelParent
    ? h('div', { class: 'ic-orb-labels', 'aria-hidden': 'true' })
    : null;
  const labelPool = [];
  let moreLine = null;
  if (labelLayer) {
    for (let k = 0; k < MARGIN_LABEL_MAX; k += 1) {
      const title = h('span', { class: 'ic-orb-label__title' });
      const sub = h('span', { class: 'ic-orb-label__sub' });
      const tag = h('span', { class: 'ic-orb-label__new' }, 'New');
      const el = h('div', { class: 'ic-orb-label' }, title, tag, sub);
      setHidden(el, true);
      setHidden(tag, true);
      labelPool.push({
        el,
        title,
        sub,
        tag,
        text: null,
        subText: null,
        side: null,
        ink: null,
        isNew: null,
      });
    }
    moreLine = h('div', { class: 'ic-orb-more' });
    setHidden(moreLine, true);
    replaceKids(labelLayer, [...labelPool.map((item) => item.el), moreLine]);
    labelParent.append?.(labelLayer);
  }

  // ---- listeners -------------------------------------------------------------------

  const listen = (target, type, fn, listenerOptions) => {
    if (!target?.addEventListener) return;
    target.addEventListener(type, fn, listenerOptions);
    cleanups.push(() =>
      target.removeEventListener?.(type, fn, listenerOptions),
    );
  };

  const pointers = new Map();
  let drag = null;
  let pinch = null;

  const pointFrom = (event) => {
    if (Number.isFinite(event?.offsetX) && Number.isFinite(event?.offsetY)) {
      return [event.offsetX, event.offsetY];
    }
    const rect = canvas?.getBoundingClientRect?.() ?? { left: 0, top: 0 };
    return [
      (event?.clientX ?? 0) - rect.left,
      (event?.clientY ?? 0) - rect.top,
    ];
  };

  listen(canvas, 'pointerdown', (event) => {
    noteInput(true);
    const [x, y] = pointFrom(event);
    pointers.set(event.pointerId ?? 0, [x, y]);
    if (pointers.size === 1) {
      drag = {
        x0: x,
        y0: y,
        x,
        y,
        moved: false,
        touch: event.pointerType === 'touch',
      };
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = {
        d0: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1,
        z0: camera.zoom,
      };
      drag = null;
    }
    try {
      canvas.setPointerCapture?.(event.pointerId);
    } catch {
      // Synthetic or already-released pointers cannot be captured.
    }
  });
  listen(canvas, 'pointermove', (event) => {
    const [x, y] = pointFrom(event);
    state.pointerInside = true;
    if (pointers.has(event.pointerId ?? 0))
      pointers.set(event.pointerId ?? 0, [x, y]);
    if (pinch && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      camera.setZoom(
        pinch.z0 * ((Math.hypot(a[0] - b[0], a[1] - b[1]) || 1) / pinch.d0),
      );
      noteInput(true);
      invalidate();
      return;
    }
    if (drag) {
      if (!drag.moved && Math.hypot(x - drag.x0, y - drag.y0) > 3)
        drag.moved = true;
      if (drag.moved) {
        state.cameraTween = null;
        state.userOriented = true;
        camera.trackball(drag.x, drag.y, x, y);
        drag.x = x;
        drag.y = y;
        noteInput(true);
        invalidate();
      }
      return;
    }
    noteInput(false);
    hoverAt(x, y, event.pointerType === 'touch');
  });
  const endPointer = (event, cancelled) => {
    const [x, y] = pointFrom(event);
    pointers.delete(event.pointerId ?? 0);
    if (pointers.size < 2) pinch = null;
    if (!cancelled && drag && !drag.moved && pointers.size === 0)
      clickAt(x, y, drag.touch);
    if (pointers.size === 0) drag = null;
  };
  listen(canvas, 'pointerup', (event) => endPointer(event, false));
  listen(canvas, 'pointercancel', (event) => endPointer(event, true));
  listen(canvas, 'pointerenter', () => {
    state.pointerInside = true;
  });
  listen(canvas, 'pointerleave', () => {
    state.pointerInside = false;
    if (!drag) setHovered(null);
    scheduleIdle();
  });
  listen(
    canvas,
    'wheel',
    (event) => {
      event.preventDefault?.();
      noteInput(true);
      camera.zoomBy(Math.exp(-(Number(event.deltaY) || 0) * 0.0015));
      invalidate();
    },
    { passive: false },
  );
  listen(canvas, 'dblclick', (event) => {
    const [x, y] = pointFrom(event);
    const i = grid.pick(x, y);
    noteInput(true);
    if (i >= 0) focusNeighbourhood(state.layout.ids[i]);
    else resetView();
  });
  listen(canvas, 'keydown', (event) => onKey(event));

  const doc = env.document;
  listen(doc, 'visibilitychange', () => {
    state.docHidden = Boolean(doc.hidden);
    if (!state.docHidden) invalidate();
  });
  const fonts = doc?.fonts;
  if (fonts) {
    const flush = () => {
      renderer.flushText();
      invalidate();
    };
    fonts.ready?.then?.(flush, () => {});
    listen(fonts, 'loadingdone', flush);
    const t = env.setTimeout(flush, 800);
    cleanups.push(() => env.clearTimeout(t));
  }
  const motionQuery = env.matchMedia?.('(prefers-reduced-motion: reduce)');
  if (motionQuery) {
    opts.reducedMotion = Boolean(motionQuery.matches);
    listen(motionQuery, 'change', (event) => {
      opts.reducedMotion = Boolean(event?.matches);
      invalidate();
    });
  }
  if (typeof env.ResizeObserver === 'function' && canvas) {
    const observer = new env.ResizeObserver(() => orb.resize());
    observer.observe(canvas);
    cleanups.push(() => observer.disconnect());
  }
  if (typeof env.IntersectionObserver === 'function' && canvas) {
    const observer = new env.IntersectionObserver((entries) => {
      const entry = entries?.[entries.length - 1];
      state.offscreen = entry ? !entry.isIntersecting : false;
      if (!state.offscreen) invalidate();
    });
    observer.observe(canvas);
    cleanups.push(() => observer.disconnect());
  }

  // ---- state → view model ----------------------------------------------------------

  const t0 = () => env.now();
  const indexOf = (id) =>
    id == null ? -1 : (state.layout.index.get(id) ?? -1);
  const posOf = (i) => [
    state.layout.pos[i * 3],
    state.layout.pos[i * 3 + 1],
    state.layout.pos[i * 3 + 2],
  ];
  const isNew = (id, now = t0()) => {
    const at = state.newAt.get(id);
    return at != null && now - at < ORB_TIMING.newTag;
  };

  function buildVms() {
    const { layout } = state;
    const n = layout.n;
    const offline = pictureNotLive(opts.pictureStatus);
    const now = t0();
    const previous = new Map(state.vms.map((vm) => [vm.id, vm]));
    state.vms = layout.nodes.map((node, index) => {
      const type = String(node.type || '');
      const known = isOrbType(type);
      const wargame = isWargameType(type);
      // Fail-safe (WG §4.2.1): an unknown type's status is ignored, so it
      // never rings, halos or reads green; a site's status is ignored too.
      const status = offline
        ? 'stale'
        : !known
          ? 'unknown'
          : type === 'site'
            ? 'ok'
            : statusKey(node.status);
      // Wargame glyphs (WG §5.3.4, §5.3.6) read side, state, phase,
      // consequence and kind through closed vocabularies only.
      const attrs = wargame ? node.attrs : undefined;
      const wgStyle = wargame ? wargameGlyphStyle(type, attrs, status) : null;
      const phase = node.attrs?.phase;
      const conf =
        type === 'track' ? confidenceKey(node.attrs?.confidence) : '';
      const dup =
        type === 'track' &&
        (Number(node.attrs?.duplicate_count) > 0 ||
          (node.attrs?.duplicates?.length ?? 0) > 0);
      const category = type === 'site' ? siteCategory(node) : undefined;
      const spec = { type, status, phase, conf, dup, category, attrs };
      const glyphKey = !known
        ? '?'
        : wargame
          ? wargameStyleKey(type, attrs, status)
          : category
            ? `site:${category}`
            : type;
      const style = wgStyle
        ? wgStyle.color
        : type === 'mission'
          ? offline
            ? COLORS.stale
            : missionPhaseClass(phase) === 'fill'
              ? COLORS.magenta
              : COLORS.pencil
          : statusColor(type, status);
      const r = nodeRadius(node, n);
      const prev = previous.get(node.id);
      return {
        id: node.id,
        node,
        type,
        status,
        spec,
        spriteKey: `${glyphKey}|${status}|${type === 'mission' ? missionPhaseClass(phase) : ''}|${conf}|${dup ? 1 : 0}`,
        color: style,
        critical: status === 'critical',
        // A wargame style says whether its critical wears the halo.
        halo: wgStyle ? wgStyle.halo : undefined,
        label: truncate(nodeLabel(node), 32),
        fullLabel: nodeLabel(node),
        subtitle: marginSubtitle(
          node,
          nodeSubtitle(node, { downSince: state.feedDown.get(node.id) }),
        ),
        salience: Number(node.salience) || 0,
        fuel: Number.isFinite(node.attrs?.fuel_pct)
          ? node.attrs.fuel_pct
          : null,
        bingo: Number.isFinite(node.attrs?.bingo_fuel_pct)
          ? node.attrs.bingo_fuel_pct
          : null,
        baseR: r,
        fromR: prev ? prev.r : r,
        r,
        isNew: isNew(node.id, now),
        hidden: Boolean(layout.hidden?.[index]),
        alpha: 1,
        sizeFactor: 1,
        ring: 0,
        tier: TIER.OTHER,
        ink: 'film',
      };
    });
  }

  function applyState() {
    const { focus, highlight, filter } = state;
    const hovered = state.hovered ?? state.active;
    for (const vm of state.vms) {
      let alpha = 1;
      let sizeFactor = 1;
      let ring = 0;
      let tier = TIER.OTHER;
      let ink = 'film';
      if (vm.type === 'vehicle') tier = TIER.VEHICLE;
      if (vm.critical) tier = Math.min(tier, TIER.CRITICAL);
      if (filter) {
        if (filter.matches.has(vm.id)) {
          ring |= RING.MATCH;
          if (filter.top.has(vm.id)) tier = Math.min(tier, TIER.SEARCH);
        } else {
          alpha = Math.min(alpha, 0.15);
          sizeFactor = 0.8;
        }
      }
      if (highlight) {
        if (highlight.ids.has(vm.id)) {
          const analyst = highlight.by === 'analyst';
          ring |= analyst ? RING.HIGHLIGHT_ANALYST : RING.HIGHLIGHT_OPERATOR;
          tier = Math.min(tier, analyst ? TIER.ANALYST : TIER.SEARCH);
          if (analyst) ink = 'analyst';
        } else {
          alpha = Math.min(alpha, 0.7);
        }
      }
      if (focus) {
        if (focus.ids.has(vm.id)) {
          const analyst = focus.by === 'analyst';
          ring |= analyst ? RING.FOCUS_ANALYST : RING.FOCUS_OPERATOR;
          tier = Math.min(tier, analyst ? TIER.ANALYST : TIER.SEARCH);
          if (analyst) ink = 'analyst';
        } else {
          alpha = Math.min(alpha, 0.35);
        }
      }
      if (vm.id === hovered) {
        ring |= RING.HOVER;
        tier = Math.min(tier, TIER.HOVERED);
      }
      if (vm.id === state.selected) {
        ring |= RING.SELECT;
        tier = TIER.SELECTED;
      }
      // Criticals are never dimmed below 40 % (spec §4.3).
      if (vm.critical) alpha = Math.max(alpha, 0.4);
      // Site-band overflow is counted, not drawn, until something points at
      // it (a search pick, a chip, the analyst): then it shows in place.
      if (vm.hidden && !ring) alpha = 0;
      vm.alpha = alpha;
      vm.sizeFactor = sizeFactor;
      vm.ring = ring;
      vm.tier = tier;
      vm.ink = ink;
    }
  }

  function refreshFilter() {
    const { filter } = state;
    if (!filter) return;
    const matches = new Set();
    for (const node of state.layout.nodes) {
      try {
        if (filter.pred(node)) matches.add(node.id);
      } catch {
        // A throwing predicate is a non-match, never a crash.
      }
    }
    filter.matches = matches;
    const ranked = (filter.ranked || []).filter((id) => matches.has(id));
    filter.top = new Set((ranked.length ? ranked : [...matches]).slice(0, 8));
  }

  // ---- motion ------------------------------------------------------------------------

  function startCameraTween(targetQ, targetZoom, duration, ease) {
    state.userOriented = true;
    if (opts.reducedMotion || duration <= 0) {
      camera.setOrientation(targetQ);
      if (targetZoom != null) camera.setZoom(targetZoom);
      state.cameraTween = null;
    } else {
      state.cameraTween = {
        q0: camera.q,
        q1: targetQ,
        z0: camera.zoom,
        z1: targetZoom ?? camera.zoom,
        start: t0(),
        duration,
        ease,
      };
    }
    invalidate();
  }

  function easeToFront(i, duration = ORB_TIMING.camera, ease = easeOut) {
    if (i < 0) return false;
    const p = posOf(i);
    if (camera.angleFromFront(p) <= EASE_THRESHOLD_DEG) return false;
    startCameraTween(orientationFacing(p), null, duration, ease);
    return true;
  }

  function addRipple(id, color, now = t0()) {
    if (opts.reducedMotion) {
      state.ripples.push({
        id,
        color,
        start: now,
        duration: ORB_TIMING.staticRing,
        static: true,
      });
    } else {
      const start = Math.max(now, state.lastRippleAt + ORB_TIMING.rippleGap);
      state.lastRippleAt = start;
      state.ripples.push({
        id,
        color,
        start,
        duration: ORB_TIMING.ripple,
        static: false,
      });
    }
  }

  function animating(now) {
    return Boolean(
      state.cameraTween ||
      state.viewportTween ||
      state.sizeFrom ||
      state.ghosts.length ||
      state.ripples.some(
        (ripple) => !ripple.static && now < ripple.start + ripple.duration,
      ),
    );
  }

  function isPaused() {
    return opts.paused || state.docHidden || state.offscreen;
  }

  function idleActive(now) {
    return (
      opts.idle !== false &&
      state.rotation &&
      !opts.reducedMotion &&
      state.selected == null &&
      !state.focus &&
      !state.filter &&
      !state.pointerInside &&
      !drag &&
      now - state.lastInputAt >= ORB_TIMING.idleAfter &&
      !isPaused()
    );
  }

  function noteInput(strong) {
    const now = t0();
    state.lastInputAt = now;
    if (strong || now - state.lastInputNotice > 250) {
      state.lastInputNotice = now;
      onInput?.();
    }
    scheduleIdle();
  }

  /** One pending timer that fires 30 s after the latest input (re-arming itself). */
  function scheduleIdle() {
    if (state.destroyed || state.idleTimer != null) return;
    const wait = Math.max(0, state.lastInputAt + ORB_TIMING.idleAfter - t0());
    state.idleTimer = env.setTimeout(() => {
      state.idleTimer = null;
      if (t0() - state.lastInputAt < ORB_TIMING.idleAfter) scheduleIdle();
      else invalidate();
    }, wait + 20);
  }

  /** One timer for time-based state: "New" tags, behind arcs, static rings. */
  function scheduleHousekeeping() {
    if (state.destroyed) return;
    const now = t0();
    let next = Infinity;
    for (const at of state.newAt.values())
      next = Math.min(next, at + ORB_TIMING.newTag);
    for (const at of state.behind.values())
      next = Math.min(next, at + ORB_TIMING.newTag);
    for (const ripple of state.ripples) {
      if (ripple.static) next = Math.min(next, ripple.start + ripple.duration);
    }
    if (state.houseTimer != null) env.clearTimeout(state.houseTimer);
    state.houseTimer = null;
    if (!Number.isFinite(next)) return;
    state.houseTimer = env.setTimeout(
      () => {
        state.houseTimer = null;
        const at = t0();
        for (const [id, when] of state.newAt)
          if (at - when >= ORB_TIMING.newTag) state.newAt.delete(id);
        for (const [id, when] of state.behind)
          if (at - when >= ORB_TIMING.newTag) state.behind.delete(id);
        state.ripples = state.ripples.filter((r) => at < r.start + r.duration);
        for (const vm of state.vms) vm.isNew = isNew(vm.id, at);
        refreshTwin();
        invalidate();
        scheduleHousekeeping();
      },
      Math.max(16, next - now + 5),
    );
  }

  // ---- the frame -----------------------------------------------------------------------

  function invalidate() {
    if (state.destroyed) return;
    state.dirty = true;
    if (!isPaused()) schedule();
  }

  function schedule() {
    if (!state.raf && !state.destroyed) state.raf = env.raf(frame) || -1;
  }

  function frame() {
    state.raf = 0;
    if (state.destroyed || isPaused()) return;
    const now = t0();
    const idle = idleActive(now);
    if (
      !state.dirty &&
      !animating(now) &&
      idle &&
      now - state.lastFrameAt < ORB_TIMING.idleFrame - 1
    ) {
      schedule();
      return;
    }
    if (idle && state.lastFrameAt) {
      camera.spin(
        IDLE_DEG_PER_S * DEG * Math.min(0.1, (now - state.lastFrameAt) / 1000),
      );
    }
    renderFrame(now);
    if (animating(t0()) || idleActive(t0())) schedule();
  }

  function advance(now) {
    const tween = state.cameraTween;
    if (tween) {
      const p = Math.min(1, (now - tween.start) / tween.duration);
      const e = tween.ease(p);
      camera.setOrientation(quatSlerp(tween.q0, tween.q1, e));
      camera.setZoom(tween.z0 + (tween.z1 - tween.z0) * e);
      if (p >= 1) state.cameraTween = null;
    }
    const vt = state.viewportTween;
    if (vt) {
      const p = Math.min(1, (now - vt.start) / vt.duration);
      const e = easeOut(p);
      camera.setViewport({
        cx: vt.from.cx + (vt.to.cx - vt.from.cx) * e,
        cy: vt.from.cy + (vt.to.cy - vt.from.cy) * e,
        r: vt.from.r + (vt.to.r - vt.from.r) * e,
      });
      if (p >= 1) state.viewportTween = null;
    }
    const sizes = state.sizeFrom;
    const sp = sizes ? Math.min(1, (now - sizes.start) / ORB_TIMING.settle) : 1;
    for (const vm of state.vms) {
      const target = vm.baseR * vm.sizeFactor;
      vm.r =
        sizes && sp < 1 ? vm.fromR + (target - vm.fromR) * easeOut(sp) : target;
    }
    if (sizes && sp >= 1) state.sizeFrom = null;
    state.ghosts = state.ghosts.filter(
      (ghost) => now < ghost.start + ORB_TIMING.fade,
    );
  }

  function sortOrder(n) {
    const { proj } = state;
    let order = state.order;
    if (order.length !== n) {
      order = new Uint32Array(n);
      for (let i = 0; i < n; i += 1) order[i] = i;
      state.order = order;
    }
    // Insertion sort: nearly sorted between frames.
    for (let k = 1; k < n; k += 1) {
      const i = order[k];
      const z = proj[i * 4 + 2];
      let j = k - 1;
      while (j >= 0 && proj[order[j] * 4 + 2] > z) {
        order[j + 1] = order[j];
        j -= 1;
      }
      order[j + 1] = i;
    }
  }

  function buildScene(now) {
    const { layout, vms, proj } = state;
    const n = layout.n;
    const R = camera.radius();
    const vp = camera.viewport;
    const hoveredId = state.hovered ?? state.active;
    const selectedIndex = indexOf(state.selected);
    const focusIdx = state.focus
      ? [...state.focus.ids].map(indexOf).filter((i) => i >= 0)
      : [];
    const edges = selectEdges(layout, {
      selected: selectedIndex,
      hovered: indexOf(hoveredId),
      focus: focusIdx,
      colorOf: (i) => vms[i]?.color,
      umpire: state.view.umpire,
    });
    // Not live: the canvas desaturates, so the magenta mission lines go grey too.
    if (pictureNotLive(opts.pictureStatus))
      for (const edge of edges)
        if (edge.color === COLORS.magenta) edge.color = COLORS.stale;
    const tmp = [0, 0, 0, 0];
    const ripples = [];
    state.ripples = state.ripples.filter(
      (ripple) => now < ripple.start + ripple.duration,
    );
    for (const ripple of state.ripples) {
      const i = indexOf(ripple.id);
      if (i < 0 || now < ripple.start) continue;
      const p = (now - ripple.start) / ripple.duration;
      const r0 = vms[i].r * proj[i * 4 + 3];
      ripples.push({
        x: proj[i * 4],
        y: proj[i * 4 + 1],
        z: proj[i * 4 + 2],
        color: ripple.color,
        radius: ripple.static ? r0 * 2 : r0 + 2 * r0 * easeOut(p),
        alpha: ripple.static ? 0.6 : 0.6 * (1 - p),
      });
    }
    const ghosts = state.ghosts.map((ghost) => {
      camera.projectXYZ(ghost.pos[0], ghost.pos[1], ghost.pos[2], tmp, 0);
      return {
        x: tmp[0],
        y: tmp[1],
        z: tmp[2],
        r: ghost.r * tmp[3],
        alpha: ghost.alpha * (1 - (now - ghost.start) / ORB_TIMING.fade),
        spec: ghost.spec,
        spriteKey: ghost.spriteKey,
      };
    });
    const limbMarks = [];
    for (const id of state.behind.keys()) {
      const i = indexOf(id);
      if (i < 0) continue;
      if (proj[i * 4 + 2] >= 0) {
        state.behind.delete(id);
        continue;
      }
      limbMarks.push({
        kind: 'arc',
        angle: camera.limbAngle(posOf(i)),
        color: vms[i].color,
        id,
      });
    }
    if (state.focus?.by === 'analyst') {
      for (const i of focusIdx) {
        if (proj[i * 4 + 2] < 0) {
          limbMarks.push({
            kind: 'tick',
            angle: camera.limbAngle(posOf(i)),
            color: COLORS.analyst,
          });
        }
      }
    }
    const recent = {};
    for (const [id, at] of state.newAt) {
      const i = indexOf(id);
      if (i >= 0 && now - at < ORB_TIMING.newTag)
        recent[layout.band[i]] = (recent[layout.band[i]] || 0) + 1;
    }
    const allCaptions = bandCaptions(layout.counts, {
      recent,
      detectionsDown: state.detectionsDown,
      sites: siteBandState(state.graph, layout),
      wargame: layout.wargame,
      profile: layout.profile,
    });
    const captions =
      R >= 160
        ? allCaptions
        : allCaptions.filter(
            (c) =>
              c.key === 'track' &&
              (state.detectionsDown || !layout.counts.track),
          );
    let labelBudget = opts.labelBudget;
    if (state.ladder >= 2) labelBudget = Math.min(labelBudget, 6);
    let watermark = null;
    if (pictureNotLive(opts.pictureStatus)) {
      const at = formatZ(opts.lastLiveAt);
      watermark = at
        ? `Last picture ${at}, not live.`
        : 'The picture is not live.';
    }
    return {
      width: state.cssW,
      height: state.cssH,
      dpr: state.dpr,
      cx: vp.cx,
      cy: vp.cy,
      R,
      camera,
      n,
      proj,
      order: state.order,
      nodes: vms,
      edges,
      edgePts: layout.edgePts,
      beltDashed: state.detectionsDown,
      beltEmpty: !layout.counts.track,
      captions,
      sectorNames: R >= 160 && layout.counts.track > 0,
      labelMode: opts.labelMode,
      labelBudget,
      // A filter may leave matches unlabelled: keep room for "N more match".
      moreRow: Boolean(state.filter),
      hovered: indexOf(hoveredId),
      ripples,
      ghosts,
      limbMarks,
      twinFocused: state.twinFocused,
      watermark,
      pointsBack: state.ladder >= 1 || n >= 300,
      dropBackHalos: state.ladder >= 1,
      // The session profile puts blue forces at −47°, so equipment (−46°)
      // collapses to ticks (WG §5.3.5).
      equipmentTicks: n > 150 || layout.profile === 'session',
    };
  }

  function renderFrame(now = t0()) {
    if (!ctx || state.cssW <= 0 || state.cssH <= 0) {
      state.dirty = false;
      return null;
    }
    const started = t0();
    advance(now);
    const n = state.layout.n;
    if (state.proj.length !== n * 4) state.proj = new Float32Array(n * 4);
    if (state.nudge.length !== n * 2) state.nudge = new Float32Array(n * 2);
    camera.projectAll(state.layout.pos, n, state.proj);
    separateCap(
      state.proj,
      n,
      state.layout.band,
      (i) => {
        const vm = state.vms[i];
        if (!vm || vm.alpha < 0.1) return 0;
        const r = vm.r * state.proj[i * 4 + 3];
        return vm.type === 'vehicle' ? r * 1.45 + 2 : r;
      },
      state.nudge,
    );
    sortOrder(n);
    const scene = buildScene(now);
    const stats = renderer.draw(ctx, scene);
    const { proj, vms } = state;
    grid.build(proj, n, (i) =>
      proj[i * 4 + 2] >= 0 && vms[i] && vms[i].alpha >= 0.1
        ? vms[i].r * proj[i * 4 + 3]
        : 0,
    );
    updateMarginLabels(stats.labels, scene);
    const elapsed = t0() - started;
    state.lastFrameMs = elapsed;
    trackBudget(elapsed);
    state.lastFrameAt = now;
    state.dirty = false;
    state.frames += 1;
    for (const cb of [...frameListeners]) {
      try {
        cb();
      } catch {
        // A listener must never stop the orb from drawing.
      }
    }
    return stats;
  }

  function trackBudget(ms) {
    if (ms > SLOW_FRAME_MS) {
      state.fastFrames = 0;
      state.slowFrames += 1;
      if (state.slowFrames >= SLOW_FRAMES && state.ladder < 2) {
        state.ladder += 1;
        state.slowFrames = 0;
      }
    } else {
      state.slowFrames = 0;
      if (ms < FAST_FRAME_MS && state.ladder > 0) {
        state.fastFrames += 1;
        if (state.fastFrames >= FAST_FRAMES) {
          state.ladder -= 1;
          state.fastFrames = 0;
        }
      }
    }
  }

  function updateMarginLabels(labels, scene) {
    if (!labelLayer) return;
    const items = labels?.mode === 'margin' ? labels.items : [];
    state.labelItems = items;
    let lowest = null;
    labelPool.forEach((slot, k) => {
      const item = items[k];
      if (!item) {
        setHidden(slot.el, true);
        return;
      }
      const vm = state.vms[item.i];
      setHidden(slot.el, false);
      // The gutter is narrow: keep the words that tell entities apart
      // (review: "medium-ran…" twice). The inline hover label shows it all.
      const room = item.width - (vm.isNew ? NEW_TAG_PX : 0);
      const title = marginTitle(
        vm.fullLabel,
        (text) => renderer.measureLabel(ctx, text) <= room,
      );
      if (slot.text !== title) {
        slot.title.textContent = title;
        slot.text = title;
      }
      const sub = vm.subtitle;
      if (slot.subText !== sub) {
        replaceKids(
          slot.sub,
          splitSegments(sub).map((segment) =>
            h('span', { class: 'ic-orb-seg' }, segment),
          ),
        );
        slot.subText = sub;
      }
      if (slot.isNew !== vm.isNew) {
        setHidden(slot.tag, !vm.isNew);
        slot.isNew = vm.isNew;
      }
      if (slot.side !== item.side) {
        slot.el.setAttribute('data-side', item.side);
        slot.side = item.side;
      }
      if (slot.ink !== vm.ink) {
        slot.el.setAttribute('data-ink', vm.ink);
        slot.ink = vm.ink;
      }
      const left = item.side === 'right' ? item.x : item.x - item.width;
      if (slot.el.style) {
        slot.el.style.width = `${Math.round(item.width)}px`;
        slot.el.style.transform = `translate(${Math.round(left)}px, ${Math.round(item.y - 8)}px)`;
      }
      if (!lowest || item.y > lowest.y)
        lowest = { y: item.y, x: left, width: item.width, sub: Boolean(sub) };
    });
    const filter = state.filter;
    let more = 0;
    if (filter && items.length) {
      const labelled = new Set(items.map((item) => state.vms[item.i].id));
      more = [...filter.matches].filter((id) => !labelled.has(id)).length;
    }
    if (moreLine) {
      if (more > 0 && lowest) {
        moreLine.textContent = `${more} more match`;
        setHidden(moreLine, false);
        if (moreLine.style) {
          moreLine.style.width = `${Math.round(lowest.width)}px`;
          // Under the label's subtitle line when it has one (review: the
          // line overprinted "probable 6…"); the layout kept this row free.
          const below = lowest.y + 14 + (lowest.sub ? 16 : 0);
          moreLine.style.transform = `translate(${Math.round(lowest.x)}px, ${Math.round(below)}px)`;
        }
      } else {
        setHidden(moreLine, true);
      }
    }
    if (labelLayer.style && canvas) {
      labelLayer.style.left = `${canvas.offsetLeft || 0}px`;
      labelLayer.style.top = `${canvas.offsetTop || 0}px`;
      labelLayer.style.width = `${scene.width}px`;
      labelLayer.style.height = `${scene.height}px`;
    }
  }

  // ---- interaction helpers -------------------------------------------------------------

  function setHovered(id) {
    if (state.hovered === id) return;
    state.hovered = id;
    applyState();
    if (canvas?.style) canvas.style.cursor = id ? 'pointer' : 'grab';
    onHover?.(id);
    invalidate();
  }

  function hoverAt(x, y, touch) {
    const i = grid.pick(x, y, { touch });
    setHovered(i >= 0 ? state.layout.ids[i] : null);
  }

  function clickAt(x, y, touch) {
    const R = camera.radius();
    const vp = camera.viewport;
    const d = Math.hypot(x - vp.cx, y - vp.cy);
    if (Math.abs(d - (R + 4)) <= 10) {
      const angle = Math.atan2(y - vp.cy, x - vp.cx);
      for (const id of state.behind.keys()) {
        const i = indexOf(id);
        if (i < 0) continue;
        const a = camera.limbAngle(posOf(i));
        const diff = Math.abs(
          Math.atan2(Math.sin(angle - a), Math.cos(angle - a)),
        );
        if (diff * (R + 4) <= 10) {
          state.behind.delete(id);
          orb.reveal([id]);
          return;
        }
      }
    }
    const i = grid.pick(x, y, { touch });
    userSelect(i >= 0 ? state.layout.ids[i] : null);
  }

  function userSelect(id) {
    orb.select(id);
    if (id) setActive(id, false);
    onSelect?.(id ?? null);
  }

  function setActive(id, ease = true) {
    const next = twin.setActive(id);
    state.active = next;
    applyState();
    if (next && ease) easeToFront(indexOf(next));
    invalidate();
    return next;
  }

  function neighbourhood(id) {
    const ids = new Set([id]);
    for (const edge of state.layout.edges) {
      if (edge.a === id) ids.add(edge.b);
      if (edge.b === id) ids.add(edge.a);
    }
    return [...ids];
  }

  function focusNeighbourhood(id) {
    if (!id) return;
    orb.focus(neighbourhood(id), { by: 'operator', camera: true });
  }

  /** The default view for the current picture (see framingOrientation). */
  function homeOrientation() {
    return framingOrientation(state.layout, camera.rest());
  }

  /**
   * Whether the orb may turn itself (WG §4.2.4): no stage input in the last
   * 3 s, no drag, and nothing selected, focused or filtered.
   */
  function mayReframe(now = t0()) {
    return (
      now - state.lastInputAt >= ORB_TIMING.reframeQuiet &&
      !drag &&
      state.selected == null &&
      !state.focus &&
      !state.filter
    );
  }

  /**
   * The theater transition (WG §4.2.4): one ripple on the new theater (a
   * static ring under reduced motion), a re-frame when the operator is not
   * using the orb, and the caption "Theater changed to {label}. {n} items
   * arrived." for the shell to show (up to 20 s, with Show on map when the
   * port can) and announce politely.
   */
  function theaterTransition(change, added, now) {
    const id = theaterNodeId(change, state.graph.nodes);
    const i = indexOf(id);
    if (i >= 0) addRipple(id, state.vms[i].color, now);
    const reframed = orb.reframe();
    const label =
      change.to?.label ||
      (state.graph?.theater?.id === change.to?.id
        ? state.graph.theater.label
        : null) ||
      (i >= 0 ? state.vms[i].fullLabel : null) ||
      change.to?.id;
    const count = added.filter((a) => a !== id).length;
    onNotice?.({
      kind: 'theater',
      count,
      ids: id ? [id] : [],
      text: theaterChangedText(label, count),
      label: safeText(label, 80),
      from: change.from ?? null,
      to: change.to,
      holdMs: ORB_TIMING.theaterCaption,
      announce: 'polite',
      reframed,
    });
  }

  /** Double-click on empty space and Home: ease back to the default view. */
  function resetView() {
    startCameraTween(homeOrientation(), 1, ORB_TIMING.camera, easeOut);
  }

  /**
   * Remember when each feed was first seen going down, so its state can read
   * "Down since 14:00Z". A feed already down in the first picture has no
   * known start and reads "Down".
   */
  function trackFeeds(prevLayout) {
    const before = new Map(
      prevLayout.nodes.map((node) => [node.id, node.status]),
    );
    const seen = new Set();
    for (const node of state.layout.nodes) {
      if (node.type !== 'feed') continue;
      seen.add(node.id);
      if (node.status !== 'critical') {
        state.feedDown.delete(node.id);
        continue;
      }
      if (state.feedDown.has(node.id)) continue;
      const was = before.get(node.id);
      state.feedDown.set(
        node.id,
        was && was !== 'critical'
          ? Number.isFinite(node.ts_ms)
            ? node.ts_ms
            : env.wallNow()
          : null,
      );
    }
    for (const id of [...state.feedDown.keys()])
      if (!seen.has(id)) state.feedDown.delete(id);
  }

  function toggleRotation() {
    state.rotation = !state.rotation;
    try {
      env.storage?.setItem?.(
        ROTATION_STORAGE_KEY,
        state.rotation ? 'on' : 'off',
      );
    } catch {
      // Storage may be blocked; the toggle still applies for this page.
    }
    invalidate();
  }

  function onKey(event) {
    if (state.destroyed || !event) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    noteInput(true);
    const { key, shiftKey } = event;
    const active = state.active;
    let handled = true;
    switch (key) {
      case 'ArrowDown':
        if (shiftKey) rotateBy(() => camera.tilt(-10 * DEG));
        else setActive(twin.step(1));
        break;
      case 'ArrowUp':
        if (shiftKey) rotateBy(() => camera.tilt(10 * DEG));
        else setActive(twin.step(-1));
        break;
      case ']':
        setActive(twin.step(1));
        break;
      case '[':
        setActive(twin.step(-1));
        break;
      case 'PageDown':
        setActive(twin.stepBand(1));
        break;
      case 'PageUp':
        setActive(twin.stepBand(-1));
        break;
      case 'ArrowLeft':
        rotateBy(() => camera.spin(-(shiftKey ? 45 : 10) * DEG));
        break;
      case 'ArrowRight':
        rotateBy(() => camera.spin((shiftKey ? 45 : 10) * DEG));
        break;
      case '+':
      case '=':
        camera.zoomBy(1.15);
        invalidate();
        break;
      case '-':
      case '_':
        camera.zoomBy(1 / 1.15);
        invalidate();
        break;
      case 'Home':
        resetView();
        break;
      case 'Enter':
        if (active) userSelect(active);
        break;
      case 'f':
      case 'F':
        if (active) focusNeighbourhood(active);
        break;
      case 'a':
      case 'A':
        if (active) onAction?.({ action: 'ask', id: active });
        break;
      case 't':
      case 'T':
        if (active) onAction?.({ action: 'track', id: active });
        break;
      case 'l':
      case 'L':
        onAction?.({ action: 'list' });
        break;
      case ' ':
      case 'Spacebar':
        toggleRotation();
        break;
      case 'Escape': {
        let cleared = null;
        if (state.focus) {
          orb.focus(null);
          cleared = 'focus';
        } else if (state.highlight) {
          orb.highlight(null);
          cleared = 'highlight';
        } else if (state.selected) {
          orb.select(null);
          onSelect?.(null);
          cleared = 'selection';
        } else if (state.active) {
          setActive(null);
          cleared = 'active';
        }
        onAction?.({ action: 'escape', cleared });
        break;
      }
      default:
        handled = false;
    }
    if (handled) {
      event.preventDefault?.();
      event.stopPropagation?.();
    }
  }

  function rotateBy(apply) {
    state.cameraTween = null;
    state.userOriented = true;
    apply();
    invalidate();
  }

  function refreshTwin() {
    twin.setEntities(
      state.layout.nodes,
      (id) => isNew(id),
      (id) => ({ downSince: state.feedDown.get(id) ?? null }),
    );
    twin.setSelected(state.selected);
    if (state.active && indexOf(state.active) < 0) state.active = null;
  }

  function autoViewport() {
    const gutter = opts.labelMode === 'margin' ? 96 : 24;
    const w = state.cssW;
    const hgt = state.cssH;
    const available = Math.max(0, hgt - CAPTION_STRIP);
    const r = Math.max(40, Math.min(w / 2 - gutter, available / 2 - 12));
    return { cx: w / 2, cy: Math.max(r + 8, available / 2), r };
  }

  function applyViewport(target, animate) {
    if (!target) return;
    if (animate && !opts.reducedMotion && state.frames > 0) {
      state.viewportTween = {
        from: camera.viewport,
        to: target,
        start: t0(),
        duration: ORB_TIMING.settle,
      };
    } else {
      state.viewportTween = null;
      camera.setViewport(target);
    }
    invalidate();
  }

  // ---- public API --------------------------------------------------------------------

  const orb = {
    /**
     * Replace the picture. `diff` is intelStore's {added, removed, updated};
     * without it the orb diffs against the previous graph itself.
     */
    setGraph(graph, diff) {
      if (state.destroyed) return;
      const now = t0();
      const prevLayout = state.layout;
      // Arrivals, fades and status rings animate only against a real previous picture.
      const animateChanges = prevLayout.n > 0;
      const prevStatus = new Map(state.vms.map((vm) => [vm.id, vm.status]));
      const prevVms = new Map(state.vms.map((vm) => [vm.id, vm]));
      const prevGraph = state.graph;
      state.rawGraph =
        graph && typeof graph === 'object' ? graph : { nodes: [], edges: [] };
      state.graph = filterForView(state.rawGraph, state.view);
      state.layout = computeLayout(state.graph, prevLayout);
      state.detectionsDown = detectionsDown(state.graph);
      const { layout } = state;
      trackFeeds(prevLayout);
      // The first real picture opens on its populated latitudes (once; never
      // again on its own, and not after the operator has turned the orb).
      if (!state.framed && layout.n > 0) {
        state.framed = true;
        if (!state.userOriented) {
          state.cameraTween = null;
          camera.setOrientation(homeOrientation());
        }
      }
      const known = (id) => layout.index.has(id);
      const prevIds = new Set(prevLayout.ids);
      const added = (
        Array.isArray(diff?.added)
          ? diff.added
          : layout.ids.filter((id) => !prevIds.has(id))
      ).filter(known);
      const removed = Array.isArray(diff?.removed)
        ? diff.removed.filter((id) => prevIds.has(id) && !known(id))
        : prevLayout.ids.filter((id) => !known(id));

      buildVms();
      const sizeChanged = state.vms.some(
        (vm) =>
          prevVms.has(vm.id) &&
          Math.abs(prevVms.get(vm.id).r - vm.baseR) > 0.25,
      );
      if (sizeChanged && !opts.reducedMotion && animateChanges)
        state.sizeFrom = { start: now };
      else for (const vm of state.vms) vm.fromR = vm.baseR;

      if (animateChanges) {
        // Removed nodes fade out over 300 ms.
        if (!opts.reducedMotion) {
          for (const id of removed) {
            const i = prevLayout.index.get(id);
            const vm = prevVms.get(id);
            if (i == null || !vm) continue;
            state.ghosts.push({
              pos: [
                prevLayout.pos[i * 3],
                prevLayout.pos[i * 3 + 1],
                prevLayout.pos[i * 3 + 2],
              ],
              r: vm.r,
              alpha: vm.alpha,
              spec: vm.spec,
              spriteKey: vm.spriteKey,
              start: now,
            });
          }
        }
        const change = theaterChangeOf(diff, prevGraph, state.graph);
        if (change) {
          // A theater change (WG §4.2.4) is one event: no per-item ripples
          // or arrival notices, just the transition and its caption.
          for (const id of added) state.newAt.set(id, now);
          for (const vm of state.vms) vm.isNew = isNew(vm.id, now);
          theaterTransition(change, added, now);
        } else {
          // Status changes: one ring in the new colour.
          for (const vm of state.vms) {
            const before = prevStatus.get(vm.id);
            if (
              before &&
              before !== vm.status &&
              !pictureNotLive(opts.pictureStatus)
            )
              addRipple(vm.id, vm.color, now);
          }
          // Arrivals.
          if (added.length) {
            for (const id of added) state.newAt.set(id, now);
            state.arrivals = state.arrivals.filter(
              (at) => now - at < ORB_TIMING.batchWindow,
            );
            for (let k = 0; k < added.length; k += 1) state.arrivals.push(now);
            const batched = state.arrivals.length > 3;
            if (batched) {
              const perSector = new Map();
              for (const id of added) {
                const i = layout.index.get(id);
                const key =
                  layout.band[i] === 'track'
                    ? `s${layout.sector[i]}`
                    : layout.band[i];
                if (!perSector.has(key)) perSector.set(key, id);
              }
              for (const id of perSector.values())
                addRipple(id, state.vms[layout.index.get(id)].color, now);
              const contacts = added.filter(
                (id) => layout.band[layout.index.get(id)] === 'track',
              ).length;
              onNotice?.({
                kind: 'batch',
                count: state.arrivals.length,
                ids: added.slice(),
                text:
                  contacts === added.length
                    ? `${state.arrivals.length} new contacts`
                    : `${state.arrivals.length} new entities`,
              });
            } else {
              const behind = [];
              for (const id of added) {
                const i = layout.index.get(id);
                addRipple(id, state.vms[i].color, now);
                if (camera.project(posOf(i)).z < 0) {
                  state.behind.set(id, now);
                  behind.push(id);
                }
              }
              if (behind.length) {
                onNotice?.({
                  kind: 'new_behind',
                  count: behind.length,
                  ids: behind,
                  text: `${behind.length} new behind`,
                });
              }
            }
            for (const vm of state.vms) vm.isNew = isNew(vm.id, now);
          }
        }
      }
      for (const id of removed) {
        state.newAt.delete(id);
        state.behind.delete(id);
      }
      if (state.focus) {
        state.focus.ids = new Set([...state.focus.ids].filter(known));
        if (!state.focus.ids.size) state.focus = null;
      }
      refreshFilter();
      applyState();
      refreshTwin();
      scheduleHousekeeping();
      invalidate();
    },
    /**
     * Blue view (`umpire: false`) or Umpire view (`umpire: true`, the
     * default) (WG §5.3.3). Blue view hides every force that is not provably
     * blue and every red axis, and the `axis` and `correlates` edges; Umpire
     * view shows the graph as given. Nodes keep their slots across a switch,
     * and a switch is not an arrival: no ripple, no notice. A presentation
     * filter, not a secrecy boundary (the shell asks the server for
     * `truth=1` only in Umpire view). Anything but a boolean is ignored.
     * @param {{umpire?: boolean}} [view]
     * @returns {{umpire: boolean}} the view now in force
     */
    setView({ umpire } = {}) {
      if (
        !state.destroyed &&
        typeof umpire === 'boolean' &&
        umpire !== state.view.umpire
      ) {
        state.view = { umpire };
        orb.setGraph(state.rawGraph, {
          added: [],
          removed: [],
          theaterChanged: null,
        });
      }
      return { ...state.view };
    },
    /** The view in force: `{umpire}`. */
    get view() {
      return { ...state.view };
    },
    /** Chip or search-result hover: ring + label, others at 70 %. Never rotates. */
    highlight(ids, { by = 'analyst' } = {}) {
      const list = Array.isArray(ids)
        ? ids.filter((id) => typeof id === 'string')
        : [];
      state.highlight = list.length
        ? { ids: new Set(list), by: by === 'operator' ? 'operator' : 'analyst' }
        : null;
      applyState();
      invalidate();
    },
    /**
     * Keep only what matches: non-matches at 15 % and 0.8× size, criticals
     * never below 40 %. `ranked` (search order) chooses which matches get labels.
     */
    filter(pred, { ranked } = {}) {
      if (typeof pred !== 'function') {
        state.filter = null;
      } else {
        state.filter = {
          pred,
          ranked: Array.isArray(ranked) ? ranked : [],
          matches: new Set(),
          top: new Set(),
        };
        state.focus = null;
        refreshFilter();
      }
      applyState();
      invalidate();
      return state.filter ? state.filter.matches.size : null;
    },
    /**
     * Analyst or operator focus: rings + labels, others at 35 %. With
     * `camera:true` the centroid rotates to the front over 600 ms, zooming out
     * when the spread is over 90°; otherwise limb ticks point at back nodes.
     */
    focus(ids, { by = 'analyst', camera: moveCamera = false } = {}) {
      const list = Array.isArray(ids)
        ? ids.filter((id) => typeof id === 'string')
        : [];
      if (!list.length) {
        state.focus = null;
        applyState();
        invalidate();
        return false;
      }
      state.focus = {
        ids: new Set(list),
        by: by === 'operator' ? 'operator' : 'analyst',
      };
      let moved = false;
      if (moveCamera) moved = orb.reveal(list, { duration: ORB_TIMING.travel });
      applyState();
      invalidate();
      return moved;
    },
    /**
     * Ease to the default view (the populated latitudes, north up, the
     * theater on the pole) over --ic-t-travel; instant under reduced motion.
     * Only when the operator is not using the orb (no stage input in the
     * last 3 s, nothing selected, focused or filtered) unless `force`.
     * @returns {boolean} whether the camera moves
     */
    reframe({ force = false } = {}) {
      if (state.destroyed || state.layout.n === 0) return false;
      if (!force && !mayReframe()) return false;
      startCameraTween(homeOrientation(), 1, ORB_TIMING.travel, easeInOutCubic);
      return true;
    },
    /** Rotate the given entities' centroid to the front without changing focus. */
    reveal(ids, { duration = ORB_TIMING.travel } = {}) {
      const vectors = (ids || [])
        .map(indexOf)
        .filter((i) => i >= 0)
        .map(posOf);
      const c = centroid(vectors);
      if (!c) return false;
      startCameraTween(
        orientationFacing(c.vector),
        c.spread > 90 ? ZOOM_MIN : Math.max(1, camera.zoom),
        duration,
        easeInOutCubic,
      );
      return true;
    },
    /**
     * Programmatic selection (does not call onSelect). Eases the node to the
     * front over 320 ms only when it is more than 50° off centre.
     */
    select(id) {
      const next = typeof id === 'string' && id ? id : null;
      state.selected = next;
      if (next) {
        state.focus = null;
        easeToFront(indexOf(next));
      }
      applyState();
      twin.setSelected(next);
      invalidate();
    },
    /** Re-measure the canvas and resize its backing store (ResizeObserver only). */
    resize() {
      if (state.destroyed || !canvas) return;
      const rect = canvas.getBoundingClientRect?.();
      const w = Math.round(canvas.clientWidth || rect?.width || 0);
      const hgt = Math.round(canvas.clientHeight || rect?.height || 0);
      if (w <= 0 || hgt <= 0) return;
      const dpr = effectiveDpr(env.devicePixelRatio(), w, hgt);
      const bw = Math.round(w * dpr);
      const bh = Math.round(hgt * dpr);
      state.cssW = w;
      state.cssH = hgt;
      state.dpr = dpr;
      if (canvas.width !== bw) canvas.width = bw;
      if (canvas.height !== bh) canvas.height = bh;
      applyViewport(state.viewportExplicit ?? autoViewport(), false);
      invalidate();
    },
    /** Move the projection centre/radius (inspector plate). Null = automatic. */
    setViewport(viewport) {
      if (
        viewport &&
        [viewport.cx, viewport.cy, viewport.r].every(Number.isFinite) &&
        viewport.r > 0
      ) {
        state.viewportExplicit = {
          cx: viewport.cx,
          cy: viewport.cy,
          r: viewport.r,
        };
      } else {
        state.viewportExplicit = null;
      }
      if (state.cssW > 0)
        applyViewport(state.viewportExplicit ?? autoViewport(), true);
    },
    /** Where a node is on screen, in viewport (client) coordinates. */
    project(id) {
      const i = indexOf(id);
      if (i < 0) return null;
      const p = camera.project(posOf(i));
      const rect = canvas?.getBoundingClientRect?.() ?? { left: 0, top: 0 };
      // The glyph's drawn point: a crowded polar cap nudges glyphs apart.
      const dx = p.front ? state.nudge[i * 2] || 0 : 0;
      const dy = p.front ? state.nudge[i * 2 + 1] || 0 : 0;
      return {
        x: (rect.left || 0) + p.x + dx,
        y: (rect.top || 0) + p.y + dy,
        front: p.front,
      };
    },
    onFrame(cb) {
      if (typeof cb !== 'function') return () => {};
      frameListeners.add(cb);
      return () => frameListeners.delete(cb);
    },
    /** A px×px copy of the orb (the tracking dock's mini orb), or null. */
    snapshot(px = 64) {
      if (!ctx || state.cssW <= 0) return null;
      if (state.dirty) renderFrame();
      const size = Math.max(8, Math.round(Number(px) || 64));
      const dpr = state.dpr;
      const out = env.createCanvas
        ? env.createCanvas(Math.round(size * dpr), Math.round(size * dpr))
        : (() => {
            try {
              const c = h('canvas');
              if (typeof c.getContext !== 'function') return null;
              c.width = Math.round(size * dpr);
              c.height = Math.round(size * dpr);
              return c;
            } catch {
              return null;
            }
          })();
      const g = out?.getContext?.('2d');
      if (!g) return null;
      const R = camera.radius() + 4;
      const vp = camera.viewport;
      g.drawImage(
        canvas,
        (vp.cx - R) * dpr,
        (vp.cy - R) * dpr,
        2 * R * dpr,
        2 * R * dpr,
        0,
        0,
        out.width,
        out.height,
      );
      if (out.style) {
        out.style.width = `${size}px`;
        out.style.height = `${size}px`;
      }
      out.setAttribute?.('aria-hidden', 'true');
      return out;
    },
    /**
     * {labelBudget, labelMode:'margin'|'inline', idle, reducedMotion, paused,
     *  pictureStatus:'live'|'stale'|'offline'|…, lastLiveAt}
     */
    setOptions(next = {}) {
      if (state.destroyed || !next) return;
      let restyle = false;
      let relayout = false;
      if (Number.isFinite(next.labelBudget))
        opts.labelBudget = Math.max(0, Math.round(next.labelBudget));
      if (next.labelMode === 'margin' || next.labelMode === 'inline') {
        relayout = opts.labelMode !== next.labelMode;
        opts.labelMode = next.labelMode;
      }
      if (typeof next.idle === 'boolean') opts.idle = next.idle;
      if (typeof next.reducedMotion === 'boolean')
        opts.reducedMotion = next.reducedMotion;
      if (typeof next.paused === 'boolean') opts.paused = next.paused;
      if (
        typeof next.pictureStatus === 'string' &&
        next.pictureStatus !== opts.pictureStatus
      ) {
        restyle =
          pictureNotLive(opts.pictureStatus) !==
          pictureNotLive(next.pictureStatus);
        opts.pictureStatus = next.pictureStatus;
      }
      if ('lastLiveAt' in next)
        opts.lastLiveAt = Number.isFinite(next.lastLiveAt)
          ? next.lastLiveAt
          : null;
      if (restyle) {
        buildVms();
        for (const vm of state.vms) vm.fromR = vm.baseR;
        applyState();
      }
      if (relayout && state.cssW > 0 && !state.viewportExplicit)
        applyViewport(autoViewport(), false);
      if (!opts.paused) scheduleIdle();
      invalidate();
    },
    destroy() {
      if (state.destroyed) return;
      state.destroyed = true;
      if (state.raf && state.raf !== -1) env.caf(state.raf);
      state.raf = 0;
      if (state.idleTimer != null) env.clearTimeout(state.idleTimer);
      if (state.houseTimer != null) env.clearTimeout(state.houseTimer);
      for (const fn of cleanups.splice(0)) fn();
      frameListeners.clear();
      twin.destroy();
      labelLayer?.remove?.();
      renderer.clearSprites();
    },
    // ---- additive, for the shell and tests ----
    /** Draw synchronously now (tests, snapshots). */
    renderNow() {
      return renderFrame();
    },
    get lastInputAt() {
      return state.lastInputAt;
    },
    get selected() {
      return state.selected;
    },
    get twin() {
      return twin.element;
    },
    get labelLayer() {
      return labelLayer;
    },
    /** Diagnostics: frame budget state and counts. */
    stats() {
      return {
        n: state.layout.n,
        ladder: state.ladder,
        lastFrameMs: state.lastFrameMs,
        frames: state.frames,
        rafPending: Boolean(state.raf),
        dpr: state.dpr,
        zoom: camera.zoom,
        viewport: camera.viewport,
        idle: idleActive(t0()),
        rotation: state.rotation,
      };
    },
    /** Diagnostics: one node's visual state ({alpha, r, ring, tier, ink, status, isNew}). */
    visual(id) {
      const vm = state.vms[indexOf(id)];
      if (!vm) return null;
      const { alpha, r, ring, tier, ink, status, isNew: fresh } = vm;
      return { alpha, r, ring, tier, ink, status, isNew: fresh };
    },
    /** The current layout (read-only use). */
    get layout() {
      return state.layout;
    },
    get camera() {
      return camera;
    },
  };

  orb.resize();
  buildVms();
  applyState();
  refreshTwin();
  scheduleIdle();
  return orb;
}
