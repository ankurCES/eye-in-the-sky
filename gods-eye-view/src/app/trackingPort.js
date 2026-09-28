/**
 * The tracking port: the one seam between the intelligence console
 * (src/console) and GEV's map. The console owns the landing view (the orb);
 * the Cesium map appears only while it tracks a drone in the cockpit FPV.
 *
 * `createTrackingPort` is built in the controls phase (src/app/controls.js),
 * where the viewer, the style manager's cockpit, the UAV layer and the UAV
 * mission panel live. The console is mounted BEFORE the application starts,
 * so src/main.js hands it `createDeferredTrackingPort()` — a facade that
 * queues the console's requests until the real port is attached.
 *
 * Contract (docs: CONTRACT.md §7 + §10, UX_SPEC.md §3b and §8):
 *   whenReady()            resolves once every GEV phase has run (the layer
 *                          data manager is attached); rejects if GEV failed
 *   enter(vehicle)         enable the UAV layer, track the drone and enter the
 *                          cockpit through the mission panel's own callback;
 *                          if the drone has no fix yet, arm the panel's
 *                          bounded follow (~30 s). Resolves true once
 *                          body.cockpit-mode is set, false on timeout, on a
 *                          superseding enter()/exit(), or if the panel gives up
 *   exit()                 leave the cockpit WITHOUT re-tracking, then untrack
 *   isTracking()           the cockpit is active
 *   onChange(cb)           every gev:cockpit-mode-changed, as
 *                          {active, subjectId, layerId, by:'console'|'gev'};
 *                          `by:'gev'` is GEV's own Esc / c / #map-view-switch
 *   setMapVisible(bool)    show/hide #cesiumContainer and make GEV's chrome
 *                          inert; display:none stops Cesium rendering
 *                          (CesiumWidget._canRender goes false), and the
 *                          render loop is gated too (see tools.js)
 *   openMissionPanel()     expand the UAV mission drawer
 *   setViewportInset({right, bottom})  narrow the map to leave `right` px
 *                          for the console's dock, recentring the cockpit
 *                          keyhole; `bottom` (the narrow bottom sheet) only
 *                          lifts the UAV alarm toasts above the sheet
 *   keyhole()              {x, y, r} of the keyhole circle in page (= viewport;
 *                          the page never scrolls) coordinates, or null
 *
 * Map overview (WG v2 §4.2.7):
 *   supports(name)         whether this build can do `name` (the console
 *                          disables Map when showArea is missing)
 *   showArea(target, {animate})  frame {bbox:[s,w,n,e]} or {center:[lat,lon],
 *                          radiusM} (2 km without a radius, 1 km minimum
 *                          extent, padded 15 %, pitch -60 deg). Hidden, reduced
 *                          motion or no `animate`: a synchronous setView.
 *                          Visible with `animate`: a 1.5 s flyTo. Either way
 *                          any camera flight in progress is cancelled first
 *                          (GEV's startup fly-ins), after the boot-time UAV
 *                          start if it is still running (at most 3 s). An
 *                          optional `groundM` is the area's known ground;
 *                          else the globe's, bounded to -500..9000 m.
 *                          Resolves true once the camera is there, false
 *                          otherwise
 *   enterOverview() / exitOverview()  toggle body.gev-console-overview: GEV's
 *                          chrome, the cockpit HUD and the keyhole hide; the
 *                          credits and the UAV alarm stack stay; the UAV layer
 *                          and its context overlay switch on. Never the cockpit
 *   setOverlayVisibility({sites, forces, engagements, vectors})  per-kind show
 *   onPick(cb)             cb({id}) for a context id (sit:…) or veh:{name}
 *                          clicked on the map while in overview; unsubscribe
 *   overlayStatus()        the context overlay's counts (sites drawn and not
 *                          drawn, degraded, attribution), or null
 *
 * Keyboard while the map is hidden (orb mode). GEV binds its global shortcuts
 * on `document` (bubbling: h o v f d c 1-7; capture: the cockpit's `c`). The
 * port installs two listeners that are inert unless the map is hidden:
 *   - window, capture phase: a bare `c` from a non-editable target stops
 *     propagating, so the cockpit toggle and the CCTV shortcut never see it
 *     (the console binds no `c`);
 *   - <html>, bubble phase: a bare letter or digit from a non-editable target
 *     stops propagating AFTER every element-level handler (the console's
 *     own orb keys F/A/T/L run first) and BEFORE GEV's document listeners.
 * So the console must handle its bare-letter keys at element level (or in
 * the capture phase), never on document/window in the bubble phase. Esc is
 * not touched here: the console's capture-phase guard owns Esc (UX §8).
 * In tracking mode (map visible) nothing is blocked; GEV's cockpit keys work.
 * The map overview is console-owned, so the same two guards apply there too.
 */
import * as Cesium from 'cesium';

const COCKPIT_EVENT = 'gev:cockpit-mode-changed';
export const MAP_HIDDEN_CLASS = 'gev-map-hidden';
export const MAP_INSET_CLASS = 'gev-map-inset';
export const MAP_INSET_VAR = '--gev-map-inset-right';
/** Narrow tracking: the console's dock is a bottom sheet; the alarm toasts
 *  sit above it instead of over its composer. */
export const ALARM_INSET_CLASS = 'gev-alarm-inset';
export const ALARM_INSET_VAR = '--gev-alarm-inset-bottom';
/** The console's map overview (§4.2.7): GEV chrome hidden, map kept. */
export const OVERVIEW_CLASS = 'gev-console-overview';
/** showArea: each half-extent grows by this fraction. */
export const SHOW_AREA_PAD = 0.15;
/** showArea: the framed area is never smaller than this (m, full width). */
export const SHOW_AREA_MIN_EXTENT_M = 1000;
/** showArea: a centre without a radius frames this radius (m). */
export const SHOW_AREA_DEFAULT_RADIUS_M = 2000;
/** showArea: camera pitch (deg); oblique, so terrain reads. */
export const SHOW_AREA_PITCH_DEG = -60;
/** showArea: flight time (s) when the map is visible and `animate` is set. */
export const SHOW_AREA_FLIGHT_S = 1.5;
/**
 * showArea: the ground heights it believes (m). Anything outside (the Dead
 * Sea shore to above Everest) is a coarse or unrefined terrain tile, which
 * offline can read tens of kilometres below the ellipsoid and put the camera
 * underground (a black map).
 */
export const SHOW_AREA_GROUND_MIN_M = -500;
export const SHOW_AREA_GROUND_MAX_M = 9000;
/**
 * showArea: longest it waits for the boot-time UAV start (whose camera flight
 * it must cancel, not race) before framing anyway.
 */
export const SHOW_AREA_STARTUP_WAIT_MS = 3000;
const M_PER_DEG = 111320;
const STYLE_ID = 'gev-tracking-port-style';
/** Slightly longer than the mission panel's 30 x 1 s follow. */
const ENTER_TIMEOUT_MS = 35000;
const ENTER_POLL_MS = 250;
/**
 * Quiet window after the port switches the UAV layer on. The layer manager
 * announces `visibility` for every setEnabled, even an idempotent one, and
 * GEV's Context handler (ui/contextLayerChanges.js) exits ANY active cockpit
 * on a visibility change outside the Contacts context. Entering before those
 * announcements stop made the cockpit drop 5 ms after it opened (live E2E:
 * the first Track after a page load, racing the boot-time UAV start).
 */
const LAYER_SETTLE_MS = 150;
const LAYER_SETTLE_MAX_MS = 2000;
/** Longest enter() waits for the boot-time UAV start before going ahead. */
const STARTUP_WAIT_MS = 20000;
const NOT_INERT = new Set([
  'SCRIPT',
  'STYLE',
  'LINK',
  'META',
  'TEMPLATE',
  'NOSCRIPT',
]);

/**
 * Rules the port toggles with body classes. Injected from JS (the UAV
 * surfaces' convention) so GEV's stylesheets stay untouched; the `html body.x`
 * prefix out-ranks every single-id rule they declare.
 *
 * The inset: #cesiumContainer (and the scope mask and keyhole inside it, which
 * size from the container) narrows by the dock width. The cockpit HUD's box
 * narrows with it, which recentres everything it positions by percentage and
 * moves its right-anchored panels off the dock. The two rims and the cloud
 * pass are placed with viewport units (cockpit.css .cockpit-altitude-rim,
 * foundation.css #cockpit-cloud-effects), so they get the map-width versions of
 * the same formulas: centre = (100vw - inset) / 2, radius = min(40% of the map
 * width, 52vh). The UAV alarm toasts move left of the dock. In the map
 * overview with the narrow bottom sheet, the credits (which must stay
 * visible and clickable) sit above the sheet as the alarm toasts do.
 */
export const TRACKING_PORT_CSS = `
html body.${MAP_HIDDEN_CLASS} #cesiumContainer,
html body.${MAP_HIDDEN_CLASS} .uav-alarm-stack{display:none!important}
html body.${MAP_INSET_CLASS} #cesiumContainer{width:auto;right:var(${MAP_INSET_VAR},0px)}
html body.${MAP_INSET_CLASS} #cockpit-hud{right:var(${MAP_INSET_VAR},0px)}
html body.${MAP_INSET_CLASS} .cockpit-altitude-rim{--cockpit-keyhole-radius:min(calc((100vw - var(${MAP_INSET_VAR},0px)) * 0.4),52vh)}
html body.${MAP_INSET_CLASS} .cockpit-altitude-rim:not(.cockpit-speed-rim){right:calc(50vw - var(${MAP_INSET_VAR},0px) / 2 - var(--cockpit-keyhole-radius) + 20px)}
html body.${MAP_INSET_CLASS} .cockpit-speed-rim{left:calc(50vw - var(${MAP_INSET_VAR},0px) / 2 - var(--cockpit-keyhole-radius) + 20px)}
html body.${MAP_INSET_CLASS} #cockpit-cloud-effects{clip-path:circle(min(calc((100vw - var(${MAP_INSET_VAR},0px)) * 0.4),52vh) at calc(50vw - var(${MAP_INSET_VAR},0px) / 2) 50%)}
html body.${MAP_INSET_CLASS} .uav-alarm-stack{right:calc(var(${MAP_INSET_VAR},0px) + 16px)}
html body.${ALARM_INSET_CLASS} .uav-alarm-stack{bottom:calc(var(${ALARM_INSET_VAR},0px) + 16px)}
html body.${OVERVIEW_CLASS}.${ALARM_INSET_CLASS} #cesium-credits{bottom:calc(var(${ALARM_INSET_VAR},0px) + 8px)!important}
html body.${OVERVIEW_CLASS} #title-bar,
html body.${OVERVIEW_CLASS} #style-indicator,
html body.${OVERVIEW_CLASS} #top-center-actions,
html body.${OVERVIEW_CLASS} #command-dock,
html body.${OVERVIEW_CLASS} #control-panel,
html body.${OVERVIEW_CLASS} #location-bar,
html body.${OVERVIEW_CLASS} #left-panel-stack,
html body.${OVERVIEW_CLASS} #pp-toggles,
html body.${OVERVIEW_CLASS} #clean-view-exit,
html body.${OVERVIEW_CLASS} #right-context-rail,
html body.${OVERVIEW_CLASS} #context-radio-dock,
html body.${OVERVIEW_CLASS} #intel-hud,
html body.${OVERVIEW_CLASS} #first-run-launcher,
html body.${OVERVIEW_CLASS} #key-setup-chip,
html body.${OVERVIEW_CLASS} #cockpit-hud,
html body.${OVERVIEW_CLASS} .cockpit-altitude-rim,
html body.${OVERVIEW_CLASS} #cockpit-cloud-effects,
html body.${OVERVIEW_CLASS} #safe-frame-overlay,
html body.${OVERVIEW_CLASS} #scope-mask,
html body.${OVERVIEW_CLASS} .celestial-ring-overlay{display:none!important}
`;

/**
 * The keyhole circle GEV draws, for a map of `width` x `height`. Mirrors
 * getKeyholeGeometry in src/celestialRing.js (controls.js passes that one in;
 * this default only keeps the port constructible without Cesium, in tests).
 */
export function defaultKeyholeGeometry(width, height) {
  const w = Number(width);
  const h = Number(height);
  if (!(w > 0) || !(h > 0)) return { centerX: 0, centerY: 0, radius: 0 };
  return { centerX: w / 2, centerY: h / 2, radius: h * 0.5 * 1.05 };
}

/**
 * A ground height showArea can use: a finite number of metres within
 * SHOW_AREA_GROUND_MIN_M..SHOW_AREA_GROUND_MAX_M, else null.
 * @param {unknown} value
 * @returns {number|null}
 */
export function plausibleGround(value) {
  return typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= SHOW_AREA_GROUND_MIN_M &&
    value <= SHOW_AREA_GROUND_MAX_M
    ? value
    : null;
}

function finiteLat(value) {
  return Number.isFinite(value) && Math.abs(value) <= 90;
}

function finiteLon(value) {
  return Number.isFinite(value) && Math.abs(value) <= 180;
}

/**
 * The area showArea frames, in degrees: `{bbox:[s,w,n,e]}` or
 * `{center:[lat,lon], radiusM}` (2 km when the radius is missing), each
 * half-extent padded by SHOW_AREA_PAD and never under half of
 * SHOW_AREA_MIN_EXTENT_M. A bbox whose west is east of its east crosses the
 * antimeridian. Null for anything unusable.
 * @param {object} target showArea target
 * @returns {{south: number, west: number, north: number, east: number,
 *   centerLat: number, centerLon: number, halfWidthM: number,
 *   halfHeightM: number}|null}
 */
export function areaBounds(target) {
  let centerLat;
  let centerLon;
  let halfHeightM;
  let halfWidthM;
  if (Array.isArray(target?.bbox)) {
    const [s, w, n, e] = target.bbox.map(Number);
    if (!finiteLat(s) || !finiteLat(n) || !finiteLon(w) || !finiteLon(e))
      return null;
    if (s > n) return null;
    const widthDeg = e >= w ? e - w : e + 360 - w;
    centerLat = (s + n) / 2;
    centerLon = w + widthDeg / 2;
    if (centerLon > 180) centerLon -= 360;
    halfHeightM = ((n - s) / 2) * M_PER_DEG;
    halfWidthM =
      (widthDeg / 2) *
      M_PER_DEG *
      Math.max(0.01, Math.cos((centerLat * Math.PI) / 180));
  } else if (Array.isArray(target?.center)) {
    const [lat, lon] = target.center.map(Number);
    if (!finiteLat(lat) || !finiteLon(lon)) return null;
    const radius = Number(target.radiusM);
    const r =
      Number.isFinite(radius) && radius > 0
        ? radius
        : SHOW_AREA_DEFAULT_RADIUS_M;
    centerLat = lat;
    centerLon = lon;
    halfHeightM = r;
    halfWidthM = r;
  } else {
    return null;
  }
  const floor = SHOW_AREA_MIN_EXTENT_M / 2;
  halfHeightM = Math.max(floor, halfHeightM * (1 + SHOW_AREA_PAD));
  halfWidthM = Math.max(floor, halfWidthM * (1 + SHOW_AREA_PAD));
  const dLat = halfHeightM / M_PER_DEG;
  const dLon =
    halfWidthM /
    (M_PER_DEG * Math.max(0.01, Math.cos((centerLat * Math.PI) / 180)));
  const wrap = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180;
  return {
    south: Math.max(-89.9, centerLat - dLat),
    north: Math.min(89.9, centerLat + dLat),
    west: dLon >= 180 ? -180 : wrap(centerLon - dLon),
    east: dLon >= 180 ? 180 : wrap(centerLon + dLon),
    centerLat,
    centerLon,
    halfWidthM,
    halfHeightM,
  };
}

/**
 * The padded area as a Cesium Rectangle: `Rectangle.fromDegrees(w, s, e, n)`.
 * @param {object} target showArea target
 * @returns {Cesium.Rectangle|null}
 */
export function areaRectangle(target) {
  const b = areaBounds(target);
  return b
    ? Cesium.Rectangle.fromDegrees(b.west, b.south, b.east, b.north)
    : null;
}

/**
 * Camera destination and orientation that frame an area at SHOW_AREA_PITCH_DEG
 * from due south: the area's bounding circle fits the vertical field of view,
 * and the camera looks at the area's centre.
 * @param {object} bounds areaBounds() result
 * @param {{fovy?: number, groundM?: number}} [view] vertical field of view
 *   (rad) and the ground height at the centre (m)
 * @returns {{destination: Cesium.Cartesian3, orientation: object, rangeM: number}}
 */
export function areaCameraView(
  bounds,
  { fovy = Math.PI / 3, groundM = 0 } = {},
) {
  const pitch = Cesium.Math.toRadians(SHOW_AREA_PITCH_DEG);
  const half = Number.isFinite(fovy) && fovy > 0.01 ? fovy / 2 : Math.PI / 6;
  const radius = Math.hypot(bounds.halfWidthM, bounds.halfHeightM);
  const rangeM = radius / Math.tan(half);
  const center = Cesium.Cartesian3.fromDegrees(
    bounds.centerLon,
    bounds.centerLat,
    Number.isFinite(groundM) ? groundM : 0,
  );
  const enu = Cesium.Transforms.eastNorthUpToFixedFrame(center);
  const offset = new Cesium.Cartesian3(
    0,
    -rangeM * Math.cos(pitch),
    -rangeM * Math.sin(pitch),
  );
  const destination = Cesium.Matrix4.multiplyByPoint(
    enu,
    offset,
    new Cesium.Cartesian3(),
  );
  return {
    destination,
    orientation: { heading: 0, pitch, roll: 0 },
    rangeM,
  };
}

function toggleClass(el, name, on) {
  if (!el) return;
  if (el.classList?.toggle) el.classList.toggle(name, Boolean(on));
}

function hasClass(el, name) {
  return el?.classList?.contains?.(name) === true;
}

function isEditable(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  return Boolean(
    target.closest?.('input, textarea, select, [contenteditable]'),
  );
}

function isBareKey(event) {
  return Boolean(
    event &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.isComposing &&
    typeof event.key === 'string',
  );
}

/**
 * Create the real port. Every collaborator is injected so the behaviour is
 * testable with fakes; controls.js passes the live ones.
 * @param {object} deps
 * @param {object} [deps.viewer] Cesium viewer (container, resize, scene)
 * @param {() => object} deps.getCockpit the CockpitViewController
 * @param {object} [deps.uavLayer] catalog.get('uav') (track/untrack)
 * @param {object} deps.missionPanel createUavMissionPanel handle
 * @param {(w: number, h: number) => object} [deps.keyholeGeometry]
 * @param {Document} [deps.doc]
 * @param {Window} [deps.win]
 * @param {Function} [deps.setTimer] setTimeout seam
 * @param {Function} [deps.clearTimer] clearTimeout seam
 * @param {Function} [deps.MutationObserverImpl]
 * @param {number} [deps.enterTimeoutMs]
 * @param {number} [deps.pollMs]
 * @param {number} [deps.settleMs] quiet window after enabling the UAV layer
 * @param {number} [deps.settleMaxMs] cap on that wait
 * @param {number} [deps.startupWaitMs] cap on waiting for the boot UAV start
 * @returns {object} the port (contract methods plus GEV-internal ones)
 */
export function createTrackingPort({
  viewer = null,
  getCockpit = () => null,
  uavLayer = null,
  missionPanel = null,
  keyholeGeometry = defaultKeyholeGeometry,
  doc = globalThis.document ?? null,
  win = globalThis.window ?? null,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  MutationObserverImpl = globalThis.MutationObserver,
  enterTimeoutMs = ENTER_TIMEOUT_MS,
  pollMs = ENTER_POLL_MS,
  settleMs = LAYER_SETTLE_MS,
  settleMaxMs = LAYER_SETTLE_MAX_MS,
  startupWaitMs = STARTUP_WAIT_MS,
} = {}) {
  let destroyed = false;
  let dataManager = null;
  let startup = null; // the boot-time UAV start (controls.js), if any
  let startupSettled = true; // no boot start, or it has finished
  let readyState = 'pending';
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve;
    rejectReady = reject;
  });
  // A console that never asks must not see an unhandled rejection.
  ready.catch(() => {});

  const listeners = new Set();
  let mapHidden = false;
  let insetRight = 0;
  let renderSync = null;
  let enterToken = 0;
  let pending = null; // {token, reference, resolve, poll, timeout}
  let owned = false; // the active cockpit session was started by enter()
  let portAction = null; // 'enter' | 'exit' | 'retarget' while the port drives
  let reverting = false;
  const inerted = new Set();
  let observer = null;
  let styleEl = null;

  const body = () => doc?.body ?? null;

  // ---- readiness ---------------------------------------------------------
  /**
   * @param {object} manager the layer data manager
   * @param {{startup?: Promise<unknown>}} [options] the boot-time UAV start;
   *   enter() lets it finish first so its own setEnabled cannot land (and
   *   announce `visibility`) just after the cockpit opens
   */
  function attachData(manager, { startup: bootStart = null } = {}) {
    if (destroyed || readyState !== 'pending') return;
    dataManager = manager ?? null;
    startup =
      bootStart && typeof bootStart.then === 'function'
        ? Promise.resolve(bootStart).catch(() => {})
        : null;
    if (startup) {
      startupSettled = false;
      startup.then(() => {
        startupSettled = true;
      });
    }
    readyState = 'ready';
    resolveReady();
  }

  function fail(error) {
    if (readyState !== 'pending') return;
    readyState = 'failed';
    rejectReady(error instanceof Error ? error : new Error(String(error)));
  }

  // ---- cockpit state -----------------------------------------------------
  function cockpitActive() {
    const cockpit = getCockpit?.();
    if (cockpit && typeof cockpit.active === 'boolean') return cockpit.active;
    return hasClass(body(), 'cockpit-mode');
  }

  function notify(change) {
    for (const cb of [...listeners]) {
      try {
        cb(change);
      } catch {
        /* a console listener must never break GEV's own dispatch */
      }
    }
  }

  function settle(result) {
    if (!pending) return;
    const { resolve, poll, timeout } = pending;
    pending = null;
    if (poll != null) clearTimer(poll);
    if (timeout != null) clearTimer(timeout);
    if (result) owned = true;
    resolve(result);
  }

  function onCockpitEvent(event) {
    const detail = event?.detail ?? {};
    const active = detail.active === true;
    const change = {
      active,
      subjectId: detail.subjectId ?? null,
      layerId: detail.layerId ?? null,
      by: portAction ? 'console' : 'gev',
    };
    if (reverting) {
      // The exit that undoes an entry nobody asked for: not news either.
      if (!active) reverting = false;
      return;
    }
    if (portAction === 'retarget') {
      // enter(other) leaving the current subject on the way in: the console
      // asked for a switch, not for the orb, so this exit is not reported.
      owned = false;
      return;
    }
    if (active) {
      if (pending || portAction === 'enter') {
        owned = true;
        settle(true);
      } else if (mapHidden && !owned) {
        // GEV entered its cockpit behind a hidden map (a restored share link,
        // a voice command). Nobody can see it, and the console is in orb
        // mode, so undo it rather than report a tracking session that the
        // console never started. Deferred: we are inside enter()'s dispatch.
        reverting = true;
        setTimer(() => {
          try {
            getCockpit?.()?.exit?.({ restoreTracking: true });
          } catch {
            /* leave GEV as it is */
          } finally {
            // exit() dispatches synchronously, so its event has been seen.
            reverting = false;
          }
        }, 0);
        return;
      }
    } else if (owned) {
      owned = false;
      if (!portAction) {
        // GEV's own Esc / c / #map-view-switch. exit() restores tracking by
        // default, which would keep the map following the drone; the console
        // is going back to the orb, so drop the track once exit() returns.
        setTimer(() => {
          if (!cockpitActive()) untrack();
        }, 0);
      }
    }
    notify(change);
  }

  function untrack() {
    try {
      uavLayer?.untrack?.();
    } catch {
      /* the layer may already be gone */
    }
  }

  async function ensureUavLayer() {
    if (!dataManager?.setEnabled) return false;
    if (dataManager.isEnabled?.('uav') === true) return false;
    await dataManager.setEnabled('uav', true, { origin: 'programmatic' });
    return true;
  }

  /** Resolve after `ms`, through the timer seam. */
  function delay(ms) {
    return new Promise((resolve) => setTimer(resolve, ms));
  }

  /**
   * Resolve once the layer manager has announced no `visibility*` change for
   * `settleMs` (at most `settleMaxMs`). Feature-checked: a manager without
   * subscribe() settles at once.
   */
  function layersSettled() {
    if (typeof dataManager?.subscribe !== 'function') return Promise.resolve();
    return new Promise((resolve) => {
      let quiet = null;
      let cap = null;
      let off = null;
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        if (quiet != null) clearTimer(quiet);
        if (cap != null) clearTimer(cap);
        try {
          off?.();
        } catch {
          /* the manager is going away */
        }
        resolve();
      };
      const arm = () => {
        if (done) return;
        if (quiet != null) clearTimer(quiet);
        quiet = setTimer(finish, settleMs);
      };
      try {
        off = dataManager.subscribe((change) => {
          if (String(change?.type || '').startsWith('visibility')) arm();
        });
      } catch {
        off = null;
      }
      arm();
      cap = setTimer(finish, settleMaxMs);
    });
  }

  // ---- enter / exit --------------------------------------------------------
  async function enter(vehicle) {
    const reference = typeof vehicle === 'string' ? vehicle.trim() : '';
    if (!reference || destroyed) return false;
    const token = ++enterToken;
    settle(false); // a newer request supersedes an older one
    try {
      await ready;
    } catch {
      return false;
    }
    if (token !== enterToken || destroyed) return false;
    if (startup) {
      // Let the boot-time UAV start finish (bounded) so its setEnabled cannot
      // announce `visibility` just after the cockpit opens.
      await Promise.race([startup, delay(startupWaitMs)]);
      if (token !== enterToken || destroyed) return false;
    }
    let switchedOn = false;
    try {
      switchedOn = await ensureUavLayer();
    } catch {
      return false;
    }
    if (token !== enterToken || destroyed) return false;
    if (switchedOn) {
      await layersSettled();
      if (token !== enterToken || destroyed) return false;
    }

    const cockpit = getCockpit?.();
    if (cockpitActive()) {
      const subject = String(
        cockpit?.readAircraftInfo?.()?.icao24 || '',
      ).toLowerCase();
      if (subject === reference.toLowerCase()) {
        owned = true;
        return true;
      }
      // Another subject is in the cockpit: leave it quietly, then enter.
      portAction = 'retarget';
      try {
        cockpit?.exit?.({ restoreTracking: false });
      } finally {
        portAction = null;
      }
    }

    portAction = 'enter';
    let entered = false;
    try {
      entered = missionPanel?.enterCockpit?.(reference) === true;
    } finally {
      portAction = null;
    }
    if (entered || cockpitActive()) {
      owned = true;
      return true;
    }
    // No fix yet (the layer's poll creates the entity): let the panel's
    // bounded, position-gated follow keep trying, and report when it lands.
    missionPanel?.armCockpitFollow?.(reference);
    return new Promise((resolve) => {
      pending = { token, reference, resolve, poll: null, timeout: null };
      const check = () => {
        if (!pending || pending.token !== token) return;
        if (cockpitActive()) {
          settle(true);
          return;
        }
        if (missionPanel?.isCockpitFollowArmed?.() === false) {
          // The panel gave up (it says so in its own status line).
          settle(false);
          return;
        }
        pending.poll = setTimer(check, pollMs);
      };
      pending.timeout = setTimer(() => {
        if (!pending || pending.token !== token) return;
        missionPanel?.disarmCockpitFollow?.();
        settle(false);
      }, enterTimeoutMs);
      pending.poll = setTimer(check, pollMs);
    });
  }

  function exit() {
    enterToken += 1;
    settle(false);
    missionPanel?.disarmCockpitFollow?.();
    const cockpit = getCockpit?.();
    portAction = 'exit';
    try {
      if (cockpitActive()) cockpit?.exit?.({ restoreTracking: false });
    } catch {
      /* the cockpit is already down */
    } finally {
      portAction = null;
    }
    owned = false;
    untrack();
  }

  // ---- map visibility ----------------------------------------------------
  function ensureStyle() {
    if (styleEl || !doc?.createElement) return;
    const existing = doc.getElementById?.(STYLE_ID);
    if (existing) {
      styleEl = existing;
      return;
    }
    styleEl = doc.createElement('style');
    styleEl.id = STYLE_ID;
    styleEl.textContent = TRACKING_PORT_CSS;
    (doc.head || body())?.append?.(styleEl);
  }

  function maybeInert(el) {
    if (!el || el.nodeType !== 1 || NOT_INERT.has(el.tagName)) return;
    // The console's own root, and anything that opts out.
    if (hasClass(el, 'ic-root') || el.hasAttribute?.('data-gev-keep-active'))
      return;
    // GEV made it inert itself (the first-run launcher does); not ours.
    if (el.hasAttribute?.('inert')) return;
    el.setAttribute('inert', '');
    inerted.add(el);
  }

  function makeChromeInert() {
    const host = body();
    // A console mounted ON body (not inside it) leaves no GEV-only subtree to
    // mark; the opaque layer still covers the map.
    if (!host || hasClass(host, 'ic-root')) return;
    for (const el of Array.from(host.children || [])) maybeInert(el);
    if (typeof MutationObserverImpl === 'function' && !observer) {
      // GEV keeps appending to body (toasts, the credit lightbox, alarms).
      observer = new MutationObserverImpl((records) => {
        if (!mapHidden) return;
        for (const record of records)
          for (const node of Array.from(record.addedNodes || []))
            maybeInert(node);
      });
      observer.observe(host, { childList: true });
    }
  }

  function releaseInert() {
    observer?.disconnect?.();
    observer = null;
    for (const el of inerted) el.removeAttribute?.('inert');
    inerted.clear();
  }

  function syncRendering() {
    if (renderSync) {
      renderSync();
    } else if (viewer && !viewer.isDestroyed?.()) {
      viewer.useDefaultRenderLoop = !mapHidden && !doc?.hidden;
    }
    if (!mapHidden && viewer && !viewer.isDestroyed?.()) {
      viewer.resize?.();
      viewer.scene?.requestRender?.();
    }
  }

  function setMapVisible(visible) {
    if (destroyed) return;
    const hide = visible === false;
    ensureStyle();
    const changed = hide !== mapHidden;
    mapHidden = hide;
    toggleClass(body(), MAP_HIDDEN_CLASS, hide);
    if (hide) makeChromeInert();
    else releaseInert();
    if (changed) syncRendering();
  }

  function setViewportInset({ right = 0, bottom = 0 } = {}) {
    if (destroyed) return;
    const value = Number(right);
    insetRight = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
    const low = Number(bottom);
    const insetBottom = Number.isFinite(low) ? Math.max(0, Math.round(low)) : 0;
    ensureStyle();
    const host = body();
    host?.style?.setProperty?.(MAP_INSET_VAR, `${insetRight}px`);
    toggleClass(host, MAP_INSET_CLASS, insetRight > 0);
    host?.style?.setProperty?.(ALARM_INSET_VAR, `${insetBottom}px`);
    toggleClass(host, ALARM_INSET_CLASS, insetBottom > 0);
    if (viewer && !viewer.isDestroyed?.()) {
      viewer.resize?.();
      viewer.scene?.requestRender?.();
    }
  }

  function keyhole() {
    const container =
      viewer?.container ?? doc?.getElementById?.('cesiumContainer') ?? null;
    const rect = container?.getBoundingClientRect?.();
    if (!rect || !(rect.width > 0) || !(rect.height > 0)) return null;
    const geometry = keyholeGeometry(rect.width, rect.height);
    if (!geometry || !(geometry.radius > 0)) return null;
    return {
      x: rect.left + geometry.centerX,
      y: rect.top + geometry.centerY,
      r: geometry.radius,
    };
  }

  function openMissionPanel() {
    if (!missionPanel?.expand) return false;
    missionPanel.expand();
    return true;
  }

  // ---- map overview (WG v2 §4.2.7) -----------------------------------------
  let overview = false;
  const pickListeners = new Set();
  let pickOff = null;

  function reducedMotion() {
    try {
      return (
        win?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true
      );
    } catch {
      return false;
    }
  }

  /**
   * The ground under the area (m): the caller's known ground (the theater's
   * `ground_msl_m`; the geoid difference is negligible at framing range),
   * else the globe's height there, and 0 when neither is plausible.
   */
  function groundHeightAt(bounds, hint) {
    const known = plausibleGround(hint);
    if (known != null) return known;
    try {
      const height = viewer?.scene?.globe?.getHeight?.(
        Cesium.Cartographic.fromDegrees(bounds.centerLon, bounds.centerLat),
      );
      return plausibleGround(height) ?? 0;
    } catch {
      return 0;
    }
  }

  /** Stop any camera flight: GEV's startup fly-ins freeze while the map is
   *  hidden and would resume, and override the framing, on the first frame. */
  function cancelFlight(camera) {
    try {
      camera.cancelFlight?.();
    } catch {
      /* no flight to stop */
    }
  }

  /**
   * Frame an area (see the header). The setView of a hidden map happens
   * before this returns, so the camera is in place when the map is shown;
   * only while the boot-time UAV start is still running does it first wait
   * for that start (bounded), so the start's flight is cancelled, not raced.
   * @param {object} target {bbox:[s,w,n,e]} or {center:[lat,lon], radiusM},
   *   plus an optional `groundM` (the area's known ground, m)
   * @param {{animate?: boolean}} [options]
   * @returns {Promise<boolean>}
   */
  function showArea(target, options = {}) {
    if (startup && !startupSettled && !destroyed) {
      return Promise.race([startup, delay(SHOW_AREA_STARTUP_WAIT_MS)]).then(
        () => frameArea(target, options),
      );
    }
    return frameArea(target, options);
  }

  function frameArea(target, { animate = false } = {}) {
    const camera = viewer?.camera;
    if (destroyed || !camera || viewer?.isDestroyed?.()) {
      return Promise.resolve(false);
    }
    // The cockpit owns the camera while it is active; never fight it.
    if (cockpitActive()) return Promise.resolve(false);
    const bounds = areaBounds(target);
    if (!bounds) return Promise.resolve(false);
    const view = areaCameraView(bounds, {
      fovy: camera.frustum?.fovy ?? camera.frustum?.fov,
      groundM: groundHeightAt(bounds, target?.groundM),
    });
    // A tracked entity would pull the camera straight back.
    if (viewer.trackedEntity) {
      untrack();
      viewer.trackedEntity = undefined;
    }
    // setView does not stop a flight (flyTo does): a startup fly-in queued
    // while the map was hidden would take the camera away on the first frame.
    cancelFlight(camera);
    const fly =
      animate === true &&
      !mapHidden &&
      !reducedMotion() &&
      typeof camera.flyTo === 'function';
    if (!fly) {
      try {
        camera.setView({
          destination: view.destination,
          orientation: view.orientation,
        });
      } catch {
        return Promise.resolve(false);
      }
      viewer.scene?.requestRender?.();
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      try {
        camera.flyTo({
          destination: view.destination,
          orientation: view.orientation,
          duration: SHOW_AREA_FLIGHT_S,
          complete: () => resolve(true),
          cancel: () => resolve(false),
        });
      } catch {
        resolve(false);
      }
    });
  }

  function enterOverview() {
    if (destroyed) return false;
    ensureStyle();
    overview = true;
    toggleClass(body(), OVERVIEW_CLASS, true);
    uavLayer?.setContextActive?.(true);
    // The UAV layer on once the data phase has run; never the cockpit.
    ready
      .then(() => (overview && !destroyed ? ensureUavLayer() : false))
      .catch(() => {});
    viewer?.scene?.requestRender?.();
    return true;
  }

  function exitOverview() {
    if (destroyed) return false;
    overview = false;
    toggleClass(body(), OVERVIEW_CLASS, false);
    uavLayer?.setContextActive?.(false);
    return true;
  }

  function setOverlayVisibility(kinds = {}) {
    if (destroyed || !kinds || typeof kinds !== 'object') return null;
    return uavLayer?.setContextVisibility?.(kinds) ?? null;
  }

  function forwardPick(pick) {
    if (!overview || destroyed) return;
    const id = typeof pick?.id === 'string' ? pick.id : '';
    if (!id) return;
    for (const cb of [...pickListeners]) {
      try {
        cb({ id });
      } catch {
        /* a console listener must never break the map's input */
      }
    }
  }

  function onPick(cb) {
    if (typeof cb !== 'function' || destroyed) return () => {};
    pickListeners.add(cb);
    if (!pickOff && typeof uavLayer?.onContextPick === 'function') {
      const off = uavLayer.onContextPick(forwardPick);
      pickOff = typeof off === 'function' ? off : () => {};
    }
    return () => pickListeners.delete(cb);
  }

  const SUPPORT = {
    showArea: () =>
      typeof viewer?.camera?.setView === 'function' && !viewer?.isDestroyed?.(),
    enterOverview: () => true,
    exitOverview: () => true,
    setOverlayVisibility: () =>
      typeof uavLayer?.setContextVisibility === 'function',
    onPick: () => typeof uavLayer?.onContextPick === 'function',
    overlayStatus: () => typeof uavLayer?.getContextStatus === 'function',
  };

  // ---- keyboard guard (see the header) -----------------------------------
  const guarded = () => mapHidden || overview;
  const onWindowKeyDown = (event) => {
    if (!guarded() || !isBareKey(event) || isEditable(event.target)) return;
    if (event.key.toLowerCase() === 'c') event.stopPropagation();
  };
  const onRootKeyDown = (event) => {
    if (!guarded() || !isBareKey(event) || isEditable(event.target)) return;
    if (/^[a-z0-9]$/i.test(event.key)) event.stopPropagation();
  };
  const rootEl = doc?.documentElement ?? null;
  win?.addEventListener?.(COCKPIT_EVENT, onCockpitEvent);
  win?.addEventListener?.('keydown', onWindowKeyDown, true);
  rootEl?.addEventListener?.('keydown', onRootKeyDown);

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    fail(new Error('The map was shut down'));
    enterToken += 1;
    settle(false);
    win?.removeEventListener?.(COCKPIT_EVENT, onCockpitEvent);
    win?.removeEventListener?.('keydown', onWindowKeyDown, true);
    rootEl?.removeEventListener?.('keydown', onRootKeyDown);
    releaseInert();
    toggleClass(body(), MAP_HIDDEN_CLASS, false);
    toggleClass(body(), MAP_INSET_CLASS, false);
    toggleClass(body(), ALARM_INSET_CLASS, false);
    if (overview) uavLayer?.setContextActive?.(false);
    overview = false;
    toggleClass(body(), OVERVIEW_CLASS, false);
    pickOff?.();
    pickOff = null;
    pickListeners.clear();
    body()?.style?.removeProperty?.(MAP_INSET_VAR);
    body()?.style?.removeProperty?.(ALARM_INSET_VAR);
    styleEl?.remove?.();
    styleEl = null;
    listeners.clear();
    renderSync = null;
  }

  const api = {
    // Contract (src/console consumes these).
    whenReady: () => ready,
    enter,
    exit,
    isTracking: () => cockpitActive(),
    onChange(cb) {
      if (typeof cb !== 'function') return () => {};
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    setMapVisible,
    openMissionPanel,
    setViewportInset,
    keyhole,
    // Map overview (§4.2.7).
    supports(name) {
      if (destroyed) return false;
      const check = SUPPORT[name];
      if (check) return check() === true;
      return typeof api[name] === 'function';
    },
    showArea,
    enterOverview,
    exitOverview,
    setOverlayVisibility,
    onPick,
    overlayStatus: () => uavLayer?.getContextStatus?.() ?? null,
    // GEV-internal.
    attachData,
    fail,
    /** True while the console has the map hidden (orb mode). */
    isMapHidden: () => mapHidden,
    /** tools.js registers its render-loop sync so one function owns it. */
    setRenderSync(fn) {
      renderSync = typeof fn === 'function' ? fn : null;
    },
    viewportInset: () => insetRight,
    /** True while the console's map overview is on. */
    isOverview: () => overview,
    destroy,
  };
  return api;
}

/** Overview methods the deferred port queues until the real port exists. */
const DEFERRED_OVERVIEW_METHODS = new Set([
  'showArea',
  'enterOverview',
  'exitOverview',
  'setOverlayVisibility',
  'onPick',
]);

/**
 * The port src/main.js hands the console before GEV has started: every call
 * is safe immediately. Visibility and inset requests made before the real
 * port exists are remembered and applied when it attaches (the console's
 * first act is to hide the map), enter() waits for it, and onChange and
 * onPick subscriptions carry over. Map-overview calls (showArea,
 * enterOverview, exitOverview, setOverlayVisibility) are queued and replayed
 * in order right after the visibility and inset; a failed start answers them
 * false.
 * @returns {object} contract methods plus attach(port|null) / fail(error)
 */
export function createDeferredTrackingPort() {
  let port = null;
  let failure = null;
  let resolveArrival;
  let rejectArrival;
  const arrival = new Promise((resolve, reject) => {
    resolveArrival = resolve;
    rejectArrival = reject;
  });
  arrival.catch(() => {});
  const listeners = new Set();
  let unsubscribe = null;
  let mapVisible = null;
  let inset = null;
  // Map-overview calls made before the real port exists, replayed in order.
  const queue = [];
  const pickListeners = new Set();
  let pickOff = null;

  function forward(change) {
    for (const cb of [...listeners]) {
      try {
        cb(change);
      } catch {
        /* one listener must not starve the rest */
      }
    }
  }

  function forwardPick(pick) {
    for (const cb of [...pickListeners]) {
      try {
        cb(pick);
      } catch {
        /* one listener must not starve the rest */
      }
    }
  }

  /** Call the real port now, or queue the call until it attaches. */
  function call(method, args, fallback) {
    if (port) {
      try {
        return port[method]?.(...args) ?? fallback;
      } catch {
        return fallback;
      }
    }
    if (failure) return fallback;
    return new Promise((resolve) => queue.push({ method, args, resolve }));
  }

  function replay(real) {
    for (const item of queue.splice(0)) {
      let out;
      try {
        out = real[item.method]?.(...item.args);
      } catch {
        out = false;
      }
      item.resolve(out ?? false);
    }
  }

  function drop() {
    for (const item of queue.splice(0)) item.resolve(false);
  }

  return {
    whenReady: () =>
      failure
        ? Promise.reject(failure)
        : arrival.then((real) => real.whenReady()),
    async enter(vehicle) {
      let real;
      try {
        real = await arrival;
      } catch {
        return false;
      }
      return real === port ? real.enter(vehicle) : false;
    },
    exit() {
      port?.exit();
    },
    isTracking: () => port?.isTracking() === true,
    onChange(cb) {
      if (typeof cb !== 'function') return () => {};
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    setMapVisible(visible) {
      mapVisible = visible !== false;
      port?.setMapVisible(mapVisible);
    },
    openMissionPanel: () => port?.openMissionPanel() === true,
    setViewportInset(value = {}) {
      inset = value;
      port?.setViewportInset(value);
    },
    keyhole: () => port?.keyhole() ?? null,
    /**
     * Before the real port attaches this build's methods count as supported
     * (their calls are queued); after a failed start nothing is.
     */
    supports(name) {
      if (failure) return false;
      if (port) {
        return typeof port.supports === 'function'
          ? port.supports(name) === true
          : typeof port[name] === 'function';
      }
      return DEFERRED_OVERVIEW_METHODS.has(name);
    },
    showArea: (target, options = {}) =>
      Promise.resolve(call('showArea', [target, options], false)).then(
        (ok) => ok === true,
      ),
    enterOverview() {
      if (port || failure) return call('enterOverview', [], false) === true;
      call('enterOverview', [], false);
      return true;
    },
    exitOverview() {
      if (port || failure) return call('exitOverview', [], false) === true;
      call('exitOverview', [], false);
      return true;
    },
    setOverlayVisibility(kinds = {}) {
      const out = call('setOverlayVisibility', [kinds], null);
      return port ? out : null;
    },
    onPick(cb) {
      if (typeof cb !== 'function') return () => {};
      pickListeners.add(cb);
      return () => pickListeners.delete(cb);
    },
    overlayStatus: () => port?.overlayStatus?.() ?? null,

    /** GEV side: attach the real port (null detaches on teardown). */
    attach(real) {
      unsubscribe?.();
      unsubscribe = null;
      pickOff?.();
      pickOff = null;
      port = real ?? null;
      if (!port) return;
      unsubscribe = port.onChange(forward);
      const off = port.onPick?.(forwardPick);
      pickOff = typeof off === 'function' ? off : null;
      if (mapVisible !== null) port.setMapVisible(mapVisible);
      if (inset !== null) port.setViewportInset(inset);
      replay(port);
      if (!failure) resolveArrival(port);
    },
    /**
     * GEV side: startup failed. Later whenReady() calls reject with this
     * error, enter() resolves false, and a port that was already attached is
     * told as well (the application's cleanup destroys it anyway).
     */
    fail(error) {
      failure = error instanceof Error ? error : new Error(String(error));
      drop();
      port?.fail?.(failure);
      rejectArrival(failure);
    },
    isAttached: () => port !== null,
  };
}
