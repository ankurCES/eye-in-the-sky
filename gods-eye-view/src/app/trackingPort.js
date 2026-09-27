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
 */

const COCKPIT_EVENT = 'gev:cockpit-mode-changed';
export const MAP_HIDDEN_CLASS = 'gev-map-hidden';
export const MAP_INSET_CLASS = 'gev-map-inset';
export const MAP_INSET_VAR = '--gev-map-inset-right';
/** Narrow tracking: the console's dock is a bottom sheet; the alarm toasts
 *  sit above it instead of over its composer. */
export const ALARM_INSET_CLASS = 'gev-alarm-inset';
export const ALARM_INSET_VAR = '--gev-alarm-inset-bottom';
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
 * width, 52vh). The UAV alarm toasts move left of the dock.
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

  // ---- keyboard guard (see the header) -----------------------------------
  const onWindowKeyDown = (event) => {
    if (!mapHidden || !isBareKey(event) || isEditable(event.target)) return;
    if (event.key.toLowerCase() === 'c') event.stopPropagation();
  };
  const onRootKeyDown = (event) => {
    if (!mapHidden || !isBareKey(event) || isEditable(event.target)) return;
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
    body()?.style?.removeProperty?.(MAP_INSET_VAR);
    body()?.style?.removeProperty?.(ALARM_INSET_VAR);
    styleEl?.remove?.();
    styleEl = null;
    listeners.clear();
    renderSync = null;
  }

  return {
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
    destroy,
  };
}

/**
 * The port src/main.js hands the console before GEV has started: every call
 * is safe immediately. Visibility and inset requests made before the real
 * port exists are remembered and applied when it attaches (the console's
 * first act is to hide the map), enter() waits for it, and onChange
 * subscriptions carry over.
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

  function forward(change) {
    for (const cb of [...listeners]) {
      try {
        cb(change);
      } catch {
        /* one listener must not starve the rest */
      }
    }
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

    /** GEV side: attach the real port (null detaches on teardown). */
    attach(real) {
      unsubscribe?.();
      unsubscribe = null;
      port = real ?? null;
      if (!port) return;
      unsubscribe = port.onChange(forward);
      if (mapVisible !== null) port.setMapVisible(mapVisible);
      if (inset !== null) port.setViewportInset(inset);
      if (!failure) resolveArrival(port);
    },
    /**
     * GEV side: startup failed. Later whenReady() calls reject with this
     * error, enter() resolves false, and a port that was already attached is
     * told as well (the application's cleanup destroys it anyway).
     */
    fail(error) {
      failure = error instanceof Error ? error : new Error(String(error));
      port?.fail?.(failure);
      rejectArrival(failure);
    },
    isAttached: () => port !== null,
  };
}
