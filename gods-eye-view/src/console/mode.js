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
 */

import { kindWords } from './chat/format.js';
import { focusBack, rememberFocus } from './situation.js';

export const MODE_STATES = Object.freeze([
  'orb',
  'entering_tracking',
  'tracking',
  'exiting',
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

  function setState(next, nextVehicle = vehicle) {
    if (next === state && nextVehicle === vehicle) return;
    const prev = state;
    state = next;
    vehicle = nextVehicle;
    root?.setAttribute?.('data-mode', state);
    const payload = { mode: state, prev, vehicle };
    for (const cb of [...changeListeners]) {
      try {
        cb(payload);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
    bus?.emit?.('mode', { mode: state, vehicle });
  }

  function vehicleId(v) {
    return v ? `veh:${v}` : null;
  }

  function project(v) {
    const p = v ? orb?.project?.(vehicleId(v)) : null;
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
    const cancelLabel = purpose === 'orb' ? MODE_COPY.stayHere : MODE_COPY.stay;
    n.actions = [
      { id: 'cancel', label: cancelLabel, run: () => dropNotice(n.id) },
    ];
    const tick = () => {
      if (!notices.some((x) => x.id === n.id)) return;
      const left = Math.max(0, NOTICE_MS - (clock.now() - started));
      if (left <= 0) {
        dropNotice(n.id, { silent: true });
        if (mayMoveView() && !(purpose === 'track' && busyWithOther(v))) {
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
      await fx.open(from, to, { reduced: Boolean(reducedMotion()) });
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
      fx.reset();
      failEnter(v, source);
      return false;
    }
    orb?.setOptions?.({ paused: true });
    setState('tracking', v);
    announce(MODE_COPY.trackingAnnounce(v), 'polite');
    if (openMissionPanel) port.openMissionPanel?.();
    return true;
  }

  /** Entering did not hold: hide the map again and offer Try again. */
  function failEnter(v, source) {
    port.setMapVisible?.(false);
    port.setViewportInset?.({ right: 0 });
    setState('orb', null);
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
    dropWhere((n) => n.purpose === 'orb');
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
    if (state === 'tracking' && !tracking) doExit();
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
        else doExit();
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
    requestTrack,
    requestOrb,
    exit: () => doExit(),
    cancel: () => cancelEnter(),
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
