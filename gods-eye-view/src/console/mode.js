/**
 * Orb ⇄ tracking state machine (UX spec §6.9 and §8).
 *
 * States, verbatim from the contract: `orb → entering_tracking → tracking →
 * exiting → orb`. The state is mirrored on `.ic-root[data-mode]` and
 * announced on the bus as `mode {mode, vehicle}`.
 *
 * Rules:
 * - The operator's own clicks act at once.
 * - The analyst (and an approved launch) never moves the view without a 3 s
 *   notice the operator can cancel, and only when the composer is empty and
 *   unfocused, the stage had no input in the last 3 s, no slip is pending and
 *   the console is not already tracking (or about to track) another vehicle.
 *   Otherwise the notice is static, with a Track button.
 * - A second vehicle's launch never switches the view.
 * - GEV's own exits (Esc, `c`, the map-view switch) return to the orb.
 *
 * Entering: select the vehicle, `whenReady()`, `setMapVisible(true)` under
 * the opaque console, `enter(vehicle)`; on success the iris opens from the
 * node to `trackingPort.keyhole()` (600 ms) and `.ic-main` fades (200 ms).
 * After 20 s without an answer the notice says the map is still starting;
 * on `false` the map is hidden again and the notice offers Try again.
 *
 * Map overview (WG spec §4.2.3, §4.2.7): `orb → entering_map → map →
 * exiting → orb`, plus `map → entering_tracking → tracking` (a 150 ms
 * crossfade: the console is already hidden) and `tracking → map`
 * (`port.exit()`, then `enterOverview()`). Entering: `showArea(target)`
 * while the map is hidden, `setMapVisible(true)`, `enterOverview()`, then the
 * iris opens from the target's orb node to cover `.ic-main`. Esc is the
 * innermost layer: tracking entered from the map returns to the map; the map
 * returns to the orb (the iris closes onto the node, then `exitOverview()`
 * and `setMapVisible(false)`). A `ui map` from the analyst follows the same
 * §6.9 gate as tracking: a 3 s notice when it passes (in the map: "Moving
 * the map…" with Stay here), otherwise a static toast with Show on map;
 * while tracking, only the toast. The operator's own Show on map acts at
 * once. Without `port.supports('showArea')` every request says the map
 * can't show areas in this build.
 */

import { kindWords } from './chat/format.js';
import { safeText } from './orb/placeText.js';
import { focusBack, rememberFocus } from './situation.js';

export const MODE_STATES = Object.freeze([
  'orb',
  'entering_tracking',
  'tracking',
  'exiting',
  'entering_map',
  'map',
]);
export const NOTICE_MS = 3000;
export const STILL_STARTING_MS = 20000;
export const RECENT_INPUT_MS = 3000;
export const IRIS_OPEN_MS = 600;
export const MAIN_FADE_MS = 200;
export const IRIS_CLOSE_MS = 400;
export const CROSSFADE_MS = 150;

/** Copy deck strings for tracking (UX spec §6.9, §8, §10). */
export const MODE_COPY = Object.freeze({
  opening: (v) => `Opening ${v}'s camera`,
  stillStarting:
    "Still starting the map. You can keep working here; tracking opens when it's ready.",
  lockFailed: (v) =>
    `Couldn't lock on to ${v}. The map is running, but ${v} isn't in view yet.`,
  tracking: (v) => `Tracking ${v}`,
  trackingAnnounce: (v) =>
    `Tracking ${v}. Map view. Press Escape to return to the console.`,
  launched: (mission, v) => `Launched: ${mission} with ${v}.`,
  launchedCountdown: (mission, v, s) =>
    `Launched: ${mission} with ${v}. Opening ${v}'s camera in ${s} s.`,
  suggests: (v, reason) =>
    reason
      ? `The analyst suggests watching ${v}: "${reason}".`
      : `The analyst suggests watching ${v}.`,
  suggestsCountdown: (v, reason, s) =>
    `${MODE_COPY.suggests(v, reason)} Opening ${v}'s camera in ${s} s.`,
  orbSuggests: 'The analyst suggests returning to the orb.',
  orbCountdown: (s) =>
    `The analyst suggests returning to the orb. Returning in ${s} s.`,
  stay: 'Stay in console',
  stayHere: 'Stay here',
  tryAgain: 'Try again',
  track: (v) => `Track ${v}`,
  back: 'Back to console',
});

/** Copy deck strings for the map overview (WG spec §4.2.3, Appendix B). */
export const MAP_COPY = Object.freeze({
  opening: 'Opening the map',
  mapOf: (label) => `Map of ${label}. Press Escape to return to the console.`,
  suggests: (label, reason) =>
    reason
      ? `The analyst suggests showing ${label} on the map: "${reason}".`
      : `The analyst suggests showing ${label} on the map.`,
  suggestsCountdown: (label, reason, s) =>
    `${MAP_COPY.suggests(label, reason)} Opening the map in ${s} s.`,
  moving: (label, s) => `Moving the map to ${label} in ${s} s.`,
  unsupported: "The map can't show areas in this build.",
  noLocation:
    'The analyst asked to show something without a location on the map.',
  didntOpen: "The map didn't open. The orb, search and the analyst still work.",
  showOnMap: 'Show on map',
  area: 'the area',
});

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/**
 * A map target from a request (`map:request`, Show on map, `M`):
 * `{ids, bbox:[s,w,n,e]|null, label, anchor}`. The bbox must be four finite
 * numbers with s < n inside ±90/±180 (east may be west of west across the
 * antimeridian), else it is null. The label is untrusted text: bidi and
 * control characters are stripped and it is cut to 80 characters. `anchor`
 * is the orb node the iris opens from and closes onto: the first `thr:` id,
 * else the first id.
 */
export function mapTargetOf(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const ids = Array.isArray(src.ids)
    ? src.ids.filter((id) => typeof id === 'string' && id).slice(0, 50)
    : [];
  const b =
    Array.isArray(src.bbox) && src.bbox.length === 4 && src.bbox.every(finite)
      ? src.bbox.slice()
      : null;
  const bbox =
    b &&
    b[0] < b[2] &&
    b[0] >= -90 &&
    b[2] <= 90 &&
    Math.abs(b[1]) <= 180 &&
    Math.abs(b[3]) <= 180
      ? b
      : null;
  const label = safeText(src.label ?? '', 80) || null;
  const anchor = ids.find((id) => id.startsWith('thr:')) || ids[0] || null;
  return { ids, bbox, label, anchor };
}

/**
 * The known ground (m above sea level) under a map area: the active
 * theater's `ground_msl_m`, else its home's `alt_msl_m`, when the area's
 * centre is inside the theater's bbox; else null. The port frames the camera
 * above it instead of trusting a coarse terrain tile (WG review: a black,
 * underground map offline).
 * @param {number[]|null} bbox the area `[s, w, n, e]`
 * @param {object|null} graph the intel graph (`graph.theater`, §3.2)
 * @returns {number|null}
 */
export function theaterGroundFor(bbox, graph) {
  const t = graph?.theater;
  const box = t?.bbox;
  const ok = (b) => Array.isArray(b) && b.length === 4 && b.every(finite);
  if (!ok(bbox) || !ok(box)) return null;
  const lat = (bbox[0] + bbox[2]) / 2;
  const lon = (bbox[1] + bbox[3]) / 2;
  if (lat < box[0] || lat > box[2] || lon < box[1] || lon > box[3]) return null;
  return [t.ground_msl_m, t.home?.alt_msl_m].find(finite) ?? null;
}

const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const lerp = (a, b, t) => a + (b - a) * t;

function defaultClock() {
  return {
    now: () => Date.now(),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (id) => globalThis.clearTimeout(id),
  };
}

function humanize(value) {
  const s = String(value ?? '')
    .replace(/_/g, ' ')
    .trim();
  return s ? s[0].toUpperCase() + s.slice(1) : '';
}

/** The mission title for launch copy, from the graph, never invented. */
function missionTitle(store, vehicle, explicit) {
  if (explicit) return String(explicit);
  const v = store?.vehicle?.(vehicle);
  const node = v?.missionNode;
  if (node?.attrs?.kind) return humanize(kindWords(node.attrs.kind));
  if (node?.label) return String(node.label).split(' · ')[0];
  return 'a mission';
}

/**
 * The iris on `.ic-main` (CSS `mask-image` driven by --ic-iris-x/y/r) and the
 * fade. Every DOM touch is feature-checked, so under a stub document or with
 * no `main` the sequence completes at once.
 */
export function createIris({
  main = null,
  clock = defaultClock(),
  raf = null,
} = {}) {
  const frame =
    raf ||
    (typeof globalThis.requestAnimationFrame === 'function'
      ? (fn) => globalThis.requestAnimationFrame(fn)
      : null);

  function box() {
    const r = main?.getBoundingClientRect?.();
    return r ? { left: r.left || 0, top: r.top || 0 } : { left: 0, top: 0 };
  }

  function setVars(x, y, r) {
    const style = main?.style;
    if (!style?.setProperty) return;
    const b = box();
    style.setProperty('--ic-iris-x', `${Math.round(x - b.left)}px`);
    style.setProperty('--ic-iris-y', `${Math.round(y - b.top)}px`);
    style.setProperty('--ic-iris-r', `${Math.max(0, Math.round(r))}px`);
  }

  function wait(ms) {
    return new Promise((resolve) => {
      if (!(ms > 0)) resolve();
      else clock.setTimeout(resolve, ms);
    });
  }

  function animate(from, to, ms) {
    if (!frame || !main?.style?.setProperty || !(ms > 0)) {
      setVars(to.x, to.y, to.r);
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const start = clock.now();
      const step = () => {
        const t = Math.min(1, (clock.now() - start) / ms);
        const e = easeInOut(t);
        setVars(
          lerp(from.x, to.x, e),
          lerp(from.y, to.y, e),
          lerp(from.r, to.r, e),
        );
        if (t < 1) frame(step);
        else resolve();
      };
      step();
    });
  }

  function fade(out, ms) {
    if (!main) return Promise.resolve();
    if (out) main.setAttribute('data-fade', 'out');
    else main.removeAttribute?.('data-fade');
    return wait(ms);
  }

  return {
    /** Node → keyhole, then fade `.ic-main` away. */
    async open(from, to, { reduced = false } = {}) {
      if (!main) return;
      if (reduced) {
        await fade(true, CROSSFADE_MS);
      } else {
        main.setAttribute('data-iris', '');
        setVars(from.x, from.y, from.r);
        await animate(from, to, IRIS_OPEN_MS);
        await fade(true, MAIN_FADE_MS);
      }
      main.setAttribute('inert', '');
    },
    /** Fade `.ic-main` back with the iris at the keyhole, then close onto the node. */
    async close(from, to, { reduced = false } = {}) {
      if (!main) return;
      main.removeAttribute?.('inert');
      if (reduced) {
        main.removeAttribute?.('data-iris');
        await fade(false, CROSSFADE_MS);
        return;
      }
      main.setAttribute('data-iris', '');
      setVars(from.x, from.y, from.r);
      await fade(false, MAIN_FADE_MS);
      await animate(from, { ...to, r: 0 }, IRIS_CLOSE_MS);
      main.removeAttribute?.('data-iris');
    },
    /** Put `.ic-main` back to its orb-mode look at once. */
    reset() {
      if (!main) return;
      main.removeAttribute?.('data-iris');
      main.removeAttribute?.('data-fade');
      main.removeAttribute?.('inert');
    },
  };
}

/**
 * Create the mode controller.
 * @param {object} options
 * @param {object} options.trackingPort GEV's port (see contract §7)
 * @param {object} options.bus console bus
 * @param {object} options.store intelStore (vehicle lookups, lastStageInputAt)
 * @param {object} [options.root] `.ic-root`; receives `data-mode`
 * @param {object} [options.orb] orb handle: select, project, setOptions
 * @param {object} [options.main] `.ic-main`, the iris target
 * @param {() => boolean} [options.isComposerIdle] composer empty and unfocused
 * @param {() => boolean} [options.reducedMotion]
 * @param {(text:string, politeness?:'polite'|'assertive') => void} [options.announce]
 * @param {() => {right:number}} [options.viewportInset] inset for the dock
 * @param {() => {x:number,y:number}} [options.stageCentre] iris fallback origin
 * @param {object} [options.clock] {now, setTimeout, clearTimeout}
 * @param {object} [options.iris] createIris() override (tests)
 * @param {object} [options.doc] document, for restoring focus
 * @param {() => ({left?:number, top?:number, width:number, height:number}|null)} [options.mainRect]
 *   `.ic-main`'s box, which the map's iris opens to cover (tests)
 */
export function createModeController({
  trackingPort = null,
  bus = null,
  store = null,
  root = null,
  orb = null,
  main = null,
  isComposerIdle = () => true,
  reducedMotion = () => false,
  announce = () => {},
  viewportInset = () => ({ right: 0 }),
  stageCentre = () => ({ x: 0, y: 0 }),
  clock = defaultClock(),
  iris = null,
  doc = globalThis.document ?? null,
  mainRect = () => main?.getBoundingClientRect?.() ?? null,
} = {}) {
  const port = trackingPort || {};
  const fx = iris || createIris({ main, clock });
  let state = 'orb';
  let vehicle = null;
  let attempt = 0;
  let pendingApprovals = 0;
  let notices = [];
  let noticeSeq = 0;
  let opener = null;
  let destroyed = false;
  let switching = false;
  // Map overview: the area on the map, the area tracking returns to when it
  // was entered from the map, which surface an `exiting` state leaves, and
  // the per-kind overlay switches the dock owns.
  let mapTarget = null;
  let returnTarget = null;
  let exitingFrom = null;
  let overlays = { sites: true };
  const changeListeners = new Set();
  const noticeListeners = new Set();
  const timers = new Set();
  const offs = [];

  // ---- small helpers -------------------------------------------------------

  function later(fn, ms) {
    const id = clock.setTimeout(() => {
      timers.delete(id);
      if (!destroyed) fn();
    }, ms);
    timers.add(id);
    return id;
  }

  function cancelTimer(id) {
    if (id == null) return;
    clock.clearTimeout(id);
    timers.delete(id);
  }

  function emitNotices() {
    const list = notices.map((n) => ({ ...n }));
    for (const cb of [...noticeListeners]) {
      try {
        cb(list);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
  }

  function emitChange(prev) {
    const payload = { mode: state, prev, vehicle, target: mapTarget };
    for (const cb of [...changeListeners]) {
      try {
        cb(payload);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
  }

  function setState(next, nextVehicle = vehicle) {
    if (next === state && nextVehicle === vehicle) return;
    const prev = state;
    state = next;
    vehicle = nextVehicle;
    if (state !== 'exiting') exitingFrom = null;
    root?.setAttribute?.('data-mode', state);
    emitChange(prev);
    bus?.emit?.('mode', { mode: state, vehicle });
  }

  function vehicleId(v) {
    return v ? `veh:${v}` : null;
  }

  /** Where an orb node is on screen (the iris origin), else the stage centre. */
  function projectId(id) {
    const p = id ? orb?.project?.(id) : null;
    if (
      p &&
      p.front !== false &&
      Number.isFinite(p.x) &&
      Number.isFinite(p.y)
    ) {
      return { x: p.x, y: p.y, r: 8 };
    }
    const c = stageCentre() || { x: 0, y: 0 };
    return { x: c.x || 0, y: c.y || 0, r: 8 };
  }

  function project(v) {
    return projectId(vehicleId(v));
  }

  /** The iris that uncovers all of `.ic-main`: its centre, r = hypot(w,h)/2. */
  function fullIris() {
    let r = null;
    try {
      r = mainRect();
    } catch {
      r = null;
    }
    if (r && r.width > 0 && r.height > 0) {
      return {
        x: (r.left || 0) + r.width / 2,
        y: (r.top || 0) + r.height / 2,
        r: Math.hypot(r.width, r.height) / 2,
      };
    }
    const c = stageCentre() || { x: 0, y: 0 };
    const w = Number(globalThis.innerWidth) || 0;
    const hh = Number(globalThis.innerHeight) || 0;
    return { x: c.x || 0, y: c.y || 0, r: Math.hypot(w, hh) / 2 };
  }

  function pause(ms) {
    return new Promise((resolve) => {
      if (!(ms > 0)) resolve();
      else later(resolve, ms);
    });
  }

  function keyhole() {
    const k = port.keyhole?.();
    if (k && Number.isFinite(k.x) && Number.isFinite(k.y)) {
      return { x: k.x, y: k.y, r: Number.isFinite(k.r) ? k.r : 160 };
    }
    const c = stageCentre() || { x: 0, y: 0 };
    return { x: c.x || 0, y: c.y || 0, r: 160 };
  }

  function stageQuiet() {
    const last = Number(store?.lastStageInputAt) || 0;
    return !last || clock.now() - last >= RECENT_INPUT_MS;
  }

  function mayMoveView() {
    let idle = true;
    try {
      idle = isComposerIdle() !== false;
    } catch {
      idle = true;
    }
    return idle && stageQuiet() && pendingApprovals === 0;
  }

  function busyWithOther(v) {
    if (
      (state === 'tracking' || state === 'entering_tracking') &&
      vehicle &&
      vehicle !== v
    ) {
      return true;
    }
    return notices.some(
      (n) => n.kind === 'countdown' && n.vehicle && n.vehicle !== v,
    );
  }

  // ---- notices -------------------------------------------------------------

  function addNotice(notice) {
    const n = { id: ++noticeSeq, ...notice };
    // One notice per purpose and vehicle: a newer one replaces it.
    notices = notices.filter(
      (x) => !(x.purpose === n.purpose && x.vehicle === n.vehicle),
    );
    notices.push(n);
    if (notices.length > 4) {
      const [drop] = notices.splice(0, 1);
      cancelTimer(drop.timer);
    }
    emitNotices();
    return n;
  }

  function dropNotice(id, { silent = false } = {}) {
    const hit = notices.find((n) => n.id === id);
    if (!hit) return;
    cancelTimer(hit.timer);
    notices = notices.filter((n) => n.id !== id);
    if (!silent) emitNotices();
  }

  function dropWhere(pred) {
    const gone = notices.filter(pred);
    if (!gone.length) return;
    for (const n of gone) cancelTimer(n.timer);
    notices = notices.filter((n) => !pred(n));
    emitNotices();
  }

  function patchNotice(id, patch) {
    const hit = notices.find((n) => n.id === id);
    if (!hit) return;
    Object.assign(hit, patch);
    emitNotices();
  }

  function countdown({
    purpose,
    v,
    source,
    textAt,
    staticText,
    staticAction,
    run,
    cancelLabel = purpose === 'orb' ? MODE_COPY.stayHere : MODE_COPY.stay,
  }) {
    const started = clock.now();
    const n = addNotice({
      kind: 'countdown',
      purpose,
      vehicle: v,
      source,
      text: textAt(Math.ceil(NOTICE_MS / 1000)),
      secondsLeft: Math.ceil(NOTICE_MS / 1000),
      actions: [],
    });
    n.actions = [
      { id: 'cancel', label: cancelLabel, run: () => dropNotice(n.id) },
    ];
    const blocked = () =>
      (purpose === 'track' && busyWithOther(v)) ||
      (purpose === 'map' &&
        (state === 'tracking' || state === 'entering_tracking'));
    const tick = () => {
      if (!notices.some((x) => x.id === n.id)) return;
      const left = Math.max(0, NOTICE_MS - (clock.now() - started));
      if (left <= 0) {
        dropNotice(n.id, { silent: true });
        if (mayMoveView() && !blocked()) {
          emitNotices();
          run();
        } else {
          // Something changed during the countdown: say it, do not move.
          addNotice({
            kind: 'static',
            purpose,
            vehicle: v,
            source,
            text: staticText,
            actions: [staticAction],
          });
        }
        return;
      }
      const s = Math.ceil(left / 1000);
      patchNotice(n.id, { text: textAt(s), secondsLeft: s });
      n.timer = later(tick, Math.min(1000, left));
    };
    n.timer = later(tick, Math.min(1000, NOTICE_MS));
    emitNotices();
    return n;
  }

  // ---- entering --------------------------------------------------------------

  async function startEnter(
    v,
    { source = 'operator', openMissionPanel = false } = {},
  ) {
    if (destroyed || !v) return false;
    const my = ++attempt;
    dropWhere(
      (n) =>
        n.purpose === 'enter' ||
        n.purpose === 'orb' ||
        (n.purpose === 'track' && n.vehicle === v),
    );
    if (source === 'operator' && doc?.activeElement && state === 'orb') {
      // By key too: the rail and the inspector rebuild their buttons on
      // every poll, so the element itself is gone by the time we return.
      opener = rememberFocus(doc.activeElement);
    }
    if (state === 'tracking' || state === 'exiting') {
      // Switching aircraft: leave the current cockpit quietly first.
      switching = true;
      try {
        port.exit?.();
      } finally {
        switching = false;
      }
    }
    // From the map overview the console is already hidden: leave the
    // overview (its CSS hides the cockpit HUD), crossfade the docks, and
    // come back to the same area on Esc (WG spec §4.2.7).
    const crossfade = state === 'map';
    if (state === 'map') {
      returnTarget = mapTarget;
      port.exitOverview?.();
    } else if (
      state === 'entering_map' ||
      (state === 'exiting' && exitingFrom === 'map')
    ) {
      port.exitOverview?.();
      fx.reset();
      returnTarget = null;
    } else if (state === 'orb') returnTarget = null;
    mapTarget = null;
    setState('entering_tracking', v);
    orb?.select?.(vehicleId(v));
    const started = clock.now();
    const progress = addNotice({
      kind: 'progress',
      purpose: 'enter',
      vehicle: v,
      source,
      text: MODE_COPY.opening(v),
      detail: '0 s',
      actions: [],
    });
    progress.actions = [
      { id: 'cancel', label: MODE_COPY.stay, run: () => cancelEnter() },
    ];
    emitNotices();
    const tick = () => {
      if (attempt !== my || state !== 'entering_tracking') return;
      const elapsed = Math.floor((clock.now() - started) / 1000);
      const patch = { detail: `${elapsed} s` };
      if (clock.now() - started >= STILL_STARTING_MS)
        patch.text = MODE_COPY.stillStarting;
      patchNotice(progress.id, patch);
      progress.timer = later(tick, 1000);
    };
    progress.timer = later(tick, 1000);

    let ok = false;
    try {
      await port.whenReady?.();
      if (attempt !== my) return false;
      port.setMapVisible?.(true);
      port.setViewportInset?.(viewportInset());
      ok = Boolean(await port.enter?.(v));
    } catch (err) {
      globalThis.console?.error?.(err);
      ok = false;
    }
    if (attempt !== my || destroyed) {
      // Cancelled while GEV was working: undo whatever landed.
      if (ok) port.exit?.();
      return false;
    }
    dropNotice(progress.id);
    if (!ok) {
      failEnter(v, source);
      return false;
    }
    const from = project(v);
    const to = keyhole();
    try {
      if (crossfade) await pause(CROSSFADE_MS);
      else await fx.open(from, to, { reduced: Boolean(reducedMotion()) });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    if (attempt !== my || destroyed) return false;
    // GEV may have left its cockpit while the iris ran (its own Esc, or a
    // cockpit that could not hold the aircraft); onChange is ignored until
    // the state is `tracking`, so ask once more before claiming it.
    if (typeof port.isTracking === 'function' && !port.isTracking()) {
      // GEV's own exit re-tracks the entity; exit() also untracks. Idempotent.
      port.exit?.();
      if (!returnTarget) fx.reset();
      failEnter(v, source);
      return false;
    }
    orb?.setOptions?.({ paused: true });
    setState('tracking', v);
    announce(MODE_COPY.trackingAnnounce(v), 'polite');
    if (openMissionPanel) port.openMissionPanel?.();
    return true;
  }

  /** Back to the map overview this tracking was entered from (map stays up). */
  function restoreMap(target) {
    returnTarget = null;
    mapTarget = target;
    port.setMapVisible?.(true);
    port.setViewportInset?.(viewportInset());
    port.enterOverview?.();
    port.setOverlayVisibility?.({ ...overlays });
    setState('map', null);
  }

  /**
   * Entering did not hold: hide the map again (or go back to the map
   * overview it was entered from) and offer Try again.
   */
  function failEnter(v, source) {
    if (returnTarget) restoreMap(returnTarget);
    else {
      port.setMapVisible?.(false);
      port.setViewportInset?.({ right: 0 });
      setState('orb', null);
    }
    const fail = addNotice({
      kind: 'error',
      purpose: 'enter',
      vehicle: v,
      source,
      text: MODE_COPY.lockFailed(v),
      actions: [],
    });
    fail.actions = [
      {
        id: 'retry',
        label: MODE_COPY.tryAgain,
        run: () => {
          dropNotice(fail.id);
          startEnter(v, { source: 'operator' });
        },
      },
      { id: 'cancel', label: MODE_COPY.stay, run: () => dropNotice(fail.id) },
    ];
    emitNotices();
    announce(MODE_COPY.lockFailed(v), 'assertive');
  }

  function cancelEnter() {
    if (state !== 'entering_tracking') return;
    attempt += 1;
    dropWhere((n) => n.purpose === 'enter');
    if (returnTarget) {
      // Entered from the map: Stay in console keeps the map overview.
      if (port.isTracking?.()) {
        switching = true;
        try {
          port.exit?.();
        } finally {
          switching = false;
        }
      }
      restoreMap(returnTarget);
      return;
    }
    port.setMapVisible?.(false);
    port.setViewportInset?.({ right: 0 });
    if (port.isTracking?.()) port.exit?.();
    fx.reset();
    setState('orb', null);
    // "Stay in console" is gone with its caption: when focus went with it,
    // put it back on the control that started tracking.
    const target = opener;
    opener = null;
    const active = doc?.activeElement;
    if (!active || active === doc?.body || active.isConnected === false)
      focusBack(target, { doc });
  }

  // ---- exiting ---------------------------------------------------------------

  async function doExit() {
    if (state === 'entering_tracking') {
      cancelEnter();
      return;
    }
    if (state !== 'tracking') return;
    const v = vehicle;
    const my = ++attempt;
    returnTarget = null;
    dropWhere((n) => n.purpose === 'orb');
    exitingFrom = 'tracking';
    setState('exiting', v);
    const from = keyhole();
    const to = project(v);
    try {
      await fx.close(from, to, { reduced: Boolean(reducedMotion()) });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    // Another request took over during the animation: it owns the map now.
    if (attempt !== my || destroyed) return;
    // GEV's own exit re-tracks the entity; exit() also untracks. Idempotent.
    port.exit?.();
    port.setMapVisible?.(false);
    port.setViewportInset?.({ right: 0 });
    fx.reset();
    orb?.setOptions?.({ paused: false });
    setState('orb', null);
    // Back on the orb with the vehicle selected and its inspector open (§8).
    orb?.select?.(vehicleId(v));
    bus?.emit?.('inspect', { id: vehicleId(v) });
    const target = opener;
    opener = null;
    // The control that started tracking, or its re-rendered twin; else the
    // inspector's Track, which the line above just reopened for this vehicle.
    focusBack(target, { doc, fallbackKeys: target ? ['act:track'] : [] });
  }

  // ---- map overview (WG spec §4.2.7) -------------------------------------------

  function supportsArea() {
    try {
      if (typeof port.supports === 'function')
        return Boolean(port.supports('showArea'));
    } catch {
      return false;
    }
    return typeof port.showArea === 'function';
  }

  function labelOf(target) {
    return target?.label || MAP_COPY.area;
  }

  /** `showArea` resolves false only when the port says so; a throw is false. */
  async function showArea(target, animate) {
    try {
      const area = { bbox: target.bbox };
      const ground = theaterGroundFor(target.bbox, store?.get?.()?.graph);
      if (ground != null) area.groundM = ground;
      const ok = await port.showArea?.(area, { animate: Boolean(animate) });
      return ok !== false;
    } catch (err) {
      globalThis.console?.error?.(err);
      return false;
    }
  }

  function leaveOverview() {
    port.exitOverview?.();
    port.setMapVisible?.(false);
    port.setViewportInset?.({ right: 0 });
  }

  /** orb → entering_map → map: pre-position, show, overview, then the iris. */
  async function enterMap(target, { source = 'operator' } = {}) {
    const my = ++attempt;
    dropWhere(
      (n) =>
        n.purpose === 'map' || n.purpose === 'orb' || n.purpose === 'enter',
    );
    if (source === 'operator' && doc?.activeElement && state === 'orb') {
      opener = rememberFocus(doc.activeElement);
    }
    if (state === 'exiting' && exitingFrom === 'tracking') {
      // The cockpit was still closing: leave it now, quietly.
      switching = true;
      try {
        port.exit?.();
      } finally {
        switching = false;
      }
    }
    returnTarget = null;
    mapTarget = target;
    setState('entering_map', null);
    const started = clock.now();
    const progress = addNotice({
      kind: 'progress',
      purpose: 'enter',
      vehicle: null,
      source,
      text: MAP_COPY.opening,
      detail: '0 s',
      actions: [],
    });
    progress.actions = [
      { id: 'cancel', label: MODE_COPY.stay, run: () => cancelMap() },
    ];
    emitNotices();
    const tick = () => {
      if (attempt !== my || state !== 'entering_map') return;
      const elapsed = Math.floor((clock.now() - started) / 1000);
      patchNotice(progress.id, { detail: `${elapsed} s` });
      progress.timer = later(tick, 1000);
    };
    progress.timer = later(tick, 1000);

    let ok = false;
    try {
      await port.whenReady?.();
      // Pre-position while the map is still hidden: no visible fly-in.
      if (attempt === my) ok = await showArea(target, false);
      if (ok && attempt === my && !destroyed) {
        port.setMapVisible?.(true);
        port.setViewportInset?.(viewportInset());
        port.enterOverview?.();
        port.setOverlayVisibility?.({ ...overlays });
      }
    } catch (err) {
      globalThis.console?.error?.(err);
      ok = false;
    }
    // Cancelled or superseded: the map was never shown for this attempt.
    if (attempt !== my || destroyed) return false;
    dropNotice(progress.id);
    if (!ok) {
      failMap(target, source);
      return false;
    }
    try {
      await fx.open(projectId(target.anchor), fullIris(), {
        reduced: Boolean(reducedMotion()),
      });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    if (attempt !== my || destroyed) {
      // Cancelled while the iris ran: the console must not stay hidden.
      if (state === 'orb') fx.reset();
      return false;
    }
    orb?.setOptions?.({ paused: true });
    setState('map', null);
    announce(MAP_COPY.mapOf(labelOf(mapTarget)), 'polite');
    // Asked for another area while the iris ran: go there now.
    if (mapTarget !== target) moveMap(mapTarget);
    return true;
  }

  function failMap(target, source) {
    leaveOverview();
    fx.reset();
    mapTarget = null;
    setState('orb', null);
    const fail = addNotice({
      kind: 'error',
      purpose: 'map',
      vehicle: null,
      source,
      text: MAP_COPY.didntOpen,
      actions: [],
    });
    fail.actions = [
      {
        id: 'retry',
        label: MODE_COPY.tryAgain,
        run: () => {
          dropNotice(fail.id);
          openMap(target, { source: 'operator' });
        },
      },
      { id: 'cancel', label: MODE_COPY.stay, run: () => dropNotice(fail.id) },
    ];
    emitNotices();
    announce(MAP_COPY.didntOpen, 'assertive');
  }

  function cancelMap() {
    if (state !== 'entering_map') return;
    attempt += 1;
    dropWhere((n) => n.purpose === 'enter');
    leaveOverview();
    fx.reset();
    mapTarget = null;
    setState('orb', null);
    const target = opener;
    opener = null;
    const active = doc?.activeElement;
    if (!active || active === doc?.body || active.isConnected === false)
      focusBack(target, { doc });
  }

  /** In the map: fly to another area (setView under reduced motion). */
  async function moveMap(target) {
    mapTarget = target;
    emitChange(state);
    const ok = await showArea(target, !reducedMotion());
    if (ok && state === 'map' && mapTarget === target)
      announce(MAP_COPY.mapOf(labelOf(target)), 'polite');
  }

  /** tracking → map: `port.exit()`, then `enterOverview()` (WG §4.2.7). */
  async function trackingToMap(target) {
    const t = target || returnTarget;
    if (!t?.bbox) {
      doExit();
      return;
    }
    attempt += 1;
    dropWhere((n) => n.purpose === 'orb' || n.purpose === 'map');
    switching = true;
    try {
      port.exit?.();
    } finally {
      switching = false;
    }
    returnTarget = null;
    mapTarget = t;
    port.setMapVisible?.(true);
    port.setViewportInset?.(viewportInset());
    port.enterOverview?.();
    port.setOverlayVisibility?.({ ...overlays });
    setState('map', null);
    announce(MAP_COPY.mapOf(labelOf(t)), 'polite');
    await showArea(t, !reducedMotion());
  }

  /** map → exiting → orb: the iris closes onto the node, then the map hides. */
  async function exitMap() {
    if (state === 'entering_map') {
      cancelMap();
      return;
    }
    if (state !== 'map') return;
    const target = mapTarget;
    const my = ++attempt;
    dropWhere((n) => n.purpose === 'map');
    exitingFrom = 'map';
    setState('exiting', null);
    try {
      await fx.close(fullIris(), projectId(target?.anchor), {
        reduced: Boolean(reducedMotion()),
      });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    if (attempt !== my || destroyed) return;
    leaveOverview();
    fx.reset();
    orb?.setOptions?.({ paused: false });
    mapTarget = null;
    setState('orb', null);
    const back = opener;
    opener = null;
    focusBack(back, { doc });
  }

  /** Show `target` on the map now, from whatever the console is doing. */
  function openMap(target, { source = 'operator' } = {}) {
    if (destroyed || !target?.bbox) return;
    if (state === 'map') {
      moveMap(target);
      return;
    }
    if (state === 'entering_map') {
      // enterMap moves on to it when the iris has opened.
      mapTarget = target;
      return;
    }
    if (state === 'entering_tracking') {
      cancelEnter();
      openMap(target, { source });
      return;
    }
    if (state === 'tracking') {
      trackingToMap(target);
      return;
    }
    enterMap(target, { source });
  }

  // ---- public ----------------------------------------------------------------

  /**
   * Ask to track a vehicle.
   * @param {string} v vehicle name, e.g. "Drone1"
   * @param {{source?:'operator'|'analyst'|'launch', reason?:string, mission?:string, openMissionPanel?:boolean}} [opts]
   * @returns {'entering'|'countdown'|'static'|'ignored'}
   */
  function requestTrack(
    v,
    {
      source = 'operator',
      reason = '',
      mission = '',
      openMissionPanel = false,
    } = {},
  ) {
    const name = String(v ?? '').trim();
    if (destroyed || !name) return 'ignored';
    if (
      (state === 'tracking' || state === 'entering_tracking') &&
      vehicle === name
    ) {
      if (openMissionPanel && state === 'tracking') port.openMissionPanel?.();
      return 'ignored';
    }
    if (source === 'operator') {
      startEnter(name, { source, openMissionPanel: Boolean(openMissionPanel) });
      return 'entering';
    }
    const launch = source === 'launch' || reason === 'mission launched';
    const title = launch ? missionTitle(store, name, mission) : '';
    const staticText = launch
      ? MODE_COPY.launched(title, name)
      : MODE_COPY.suggests(name, reason);
    const trackAction = {
      id: 'track',
      label: MODE_COPY.track(name),
      run: () => {
        dropWhere((n) => n.purpose === 'track' && n.vehicle === name);
        startEnter(name, { source: 'operator' });
      },
    };
    if (busyWithOther(name) || !mayMoveView()) {
      addNotice({
        kind: 'static',
        purpose: 'track',
        vehicle: name,
        source,
        text: staticText,
        actions: [trackAction],
      });
      return 'static';
    }
    countdown({
      purpose: 'track',
      v: name,
      source,
      textAt: (s) =>
        launch
          ? MODE_COPY.launchedCountdown(title, name, s)
          : MODE_COPY.suggestsCountdown(name, reason, s),
      staticText,
      staticAction: trackAction,
      run: () => startEnter(name, { source }),
    });
    announce(
      launch
        ? MODE_COPY.launchedCountdown(title, name, Math.ceil(NOTICE_MS / 1000))
        : MODE_COPY.suggestsCountdown(
            name,
            reason,
            Math.ceil(NOTICE_MS / 1000),
          ),
      'polite',
    );
    return 'countdown';
  }

  /** The analyst asks to return to the orb: a cancellable notice, never a jump. */
  function requestOrb({ source = 'analyst' } = {}) {
    if (destroyed) return 'ignored';
    if (source === 'operator') {
      doExit();
      return 'entering';
    }
    if (state !== 'tracking') return 'ignored';
    const back = { id: 'back', label: MODE_COPY.back, run: () => doExit() };
    if (!mayMoveView()) {
      addNotice({
        kind: 'static',
        purpose: 'orb',
        vehicle,
        source,
        text: MODE_COPY.orbSuggests,
        actions: [back],
      });
      return 'static';
    }
    countdown({
      purpose: 'orb',
      v: vehicle,
      source,
      textAt: (s) => MODE_COPY.orbCountdown(s),
      staticText: MODE_COPY.orbSuggests,
      staticAction: back,
      run: () => doExit(),
    });
    announce(MODE_COPY.orbCountdown(Math.ceil(NOTICE_MS / 1000)), 'polite');
    return 'countdown';
  }

  /**
   * Ask to show an area on the map (WG spec §4.2.3, §4.2.7).
   * @param {{ids?:string[], bbox?:number[], label?:string}} raw the area
   * @param {{source?:'operator'|'analyst', reason?:string, countdown?:boolean}} [opts]
   *   `countdown:false` means the view's §6.9 gate already failed
   * @returns {'entering'|'countdown'|'static'|'unsupported'|'ignored'}
   */
  function requestMap(
    raw,
    { source = 'operator', reason = '', countdown: gate = true } = {},
  ) {
    if (destroyed) return 'ignored';
    const target = mapTargetOf(raw);
    const operator = source === 'operator';
    const who = operator ? 'operator' : 'analyst';
    if (!supportsArea()) {
      addNotice({
        kind: 'static',
        purpose: 'map',
        vehicle: null,
        source: who,
        text: MAP_COPY.unsupported,
        actions: [],
      });
      announce(MAP_COPY.unsupported, 'polite');
      return 'unsupported';
    }
    if (!target.bbox) {
      if (!operator) {
        addNotice({
          kind: 'static',
          purpose: 'map',
          vehicle: null,
          source: who,
          text: MAP_COPY.noLocation,
          actions: [],
        });
      }
      return 'ignored';
    }
    if (operator) {
      openMap(target, { source: 'operator' });
      return 'entering';
    }
    const label = labelOf(target);
    const why = safeText(reason ?? '', 200);
    const staticText = MAP_COPY.suggests(label, why);
    const show = {
      id: 'map',
      label: MAP_COPY.showOnMap,
      run: () => {
        dropWhere((n) => n.purpose === 'map');
        openMap(target, { source: 'operator' });
      },
    };
    const tracking = state === 'tracking' || state === 'entering_tracking';
    if (tracking || gate === false || !mayMoveView()) {
      addNotice({
        kind: 'static',
        purpose: 'map',
        vehicle: null,
        source: who,
        text: staticText,
        actions: [show],
      });
      return 'static';
    }
    const inMap = state === 'map';
    const textAt = (s) =>
      inMap
        ? MAP_COPY.moving(label, s)
        : MAP_COPY.suggestsCountdown(label, why, s);
    countdown({
      purpose: 'map',
      v: null,
      source: who,
      textAt,
      staticText,
      staticAction: show,
      cancelLabel: inMap ? MODE_COPY.stayHere : MODE_COPY.stay,
      run: () => openMap(target, { source: who }),
    });
    announce(textAt(Math.ceil(NOTICE_MS / 1000)), 'polite');
    return 'countdown';
  }

  /**
   * Esc: close the innermost layer. Tracking entered from the map goes back
   * to the map; the map goes back to the orb.
   */
  function exitInnermost() {
    if (state === 'entering_tracking') {
      cancelEnter();
      return;
    }
    if (state === 'tracking' && returnTarget) {
      trackingToMap(returnTarget);
      return;
    }
    if (state === 'entering_map' || state === 'map') {
      exitMap();
      return;
    }
    doExit();
  }

  /** Back to console: always the orb, from tracking or the map. */
  function backToConsole() {
    if (state === 'entering_tracking') {
      cancelEnter();
      if (state === 'map') exitMap();
      return;
    }
    if (state === 'entering_map' || state === 'map') {
      exitMap();
      return;
    }
    returnTarget = null;
    doExit();
  }

  /** The dock's overlay switches (Phase A: `sites`). */
  function setOverlays(next) {
    if (!next || typeof next !== 'object') return;
    if (typeof next.sites === 'boolean')
      overlays = { ...overlays, sites: next.sites };
    if (state === 'map') port.setOverlayVisibility?.({ ...overlays });
  }

  /** Cancel the newest countdown notice (Esc). Returns whether one was open. */
  function cancelNotice() {
    const open = [...notices].reverse().find((n) => n.kind === 'countdown');
    if (!open) return false;
    dropNotice(open.id);
    return true;
  }

  function dismissNotice(id) {
    dropNotice(id);
  }

  function onGevChange() {
    if (destroyed || switching) return;
    const tracking = Boolean(port.isTracking?.());
    if (state === 'tracking' && !tracking) {
      // GEV left its cockpit: back to the map when tracking came from it.
      if (returnTarget) trackingToMap(returnTarget);
      else doExit();
    }
  }

  // ---- wiring ----------------------------------------------------------------

  if (typeof port.onChange === 'function') {
    const off = port.onChange(() => onGevChange());
    if (typeof off === 'function') offs.push(off);
  }
  if (bus?.on) {
    offs.push(
      bus.on('track:request', (p) => {
        if (p?.vehicle) requestTrack(p.vehicle, p);
      }),
      bus.on('track:exit', (p) => {
        if (p?.source === 'analyst') requestOrb({ source: 'analyst' });
        else if (state === 'tracking' || state === 'entering_tracking')
          exitInnermost();
      }),
      // `ui map` and Show on map (chat/view.js, WG spec §4.2.3).
      bus.on('map:request', (p) => {
        if (!p || typeof p !== 'object') return;
        requestMap(
          { ids: p.ids, bbox: p.bbox, label: p.label },
          {
            source: p.source === 'operator' ? 'operator' : 'analyst',
            reason: typeof p.reason === 'string' ? p.reason : '',
            countdown: p.countdown !== false,
          },
        );
      }),
      bus.on('approval:pending', (p) => {
        const n = Number(p?.count);
        pendingApprovals = Number.isFinite(n) && n > 0 ? n : 0;
      }),
    );
  }
  root?.setAttribute?.('data-mode', state);

  return {
    get state() {
      return state;
    },
    get vehicle() {
      return vehicle;
    },
    get notices() {
      return notices.map((n) => ({ ...n }));
    },
    get pendingApprovals() {
      return pendingApprovals;
    },
    /** The area on the map (entering, in, or leaving the map), else null. */
    get target() {
      return mapTarget ? { ...mapTarget } : null;
    },
    /** Whether tracking will return to the map on Esc. */
    get fromMap() {
      return returnTarget != null;
    },
    get overlays() {
      return { ...overlays };
    },
    requestTrack,
    requestOrb,
    requestMap,
    exit: () => exitInnermost(),
    backToConsole,
    setOverlays,
    supportsMap: () => supportsArea(),
    cancel: () => {
      if (state === 'entering_map') cancelMap();
      else cancelEnter();
    },
    cancelNotice,
    dismissNotice,
    onChange(cb) {
      if (typeof cb !== 'function') return () => {};
      const entry = (p) => cb(p);
      changeListeners.add(entry);
      return () => changeListeners.delete(entry);
    },
    onNotice(cb) {
      if (typeof cb !== 'function') return () => {};
      const entry = (list) => cb(list);
      noticeListeners.add(entry);
      return () => noticeListeners.delete(entry);
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      attempt += 1;
      for (const id of [...timers]) clock.clearTimeout(id);
      timers.clear();
      for (const off of offs.splice(0)) off?.();
      changeListeners.clear();
      noticeListeners.clear();
      notices = [];
    },
  };
}
