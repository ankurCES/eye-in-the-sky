/**
 * @module uav
 * @description UAV layer: renders godSeye AirSim drones, their contacts and the
 * live mission geometry on the Cesium globe.
 *
 * Composition mirrors `src/layers/military/`: state and policy are data, every
 * capability is a focused module, and the application injects scene services
 * rather than the layer importing application modules.
 *
 * - `motion` turns the 5 Hz poll into continuous motion (sampled properties).
 * - `rendering` owns the vehicle entities, trails and numbered contacts.
 * - `missionOverlay` owns a SECOND data source for `/mission-overlay`.
 * - `contextOverlay` owns a THIRD, "uav-context-overlay", for the mapped sites
 *   of `/intel/overlay` (WG v2 §4.2.7). It polls only while the console's map
 *   overview has it active.
 * - The theater watch reads `/snapshot.theater` and, when the running theater
 *   changes, clears every trail and all interpolation and refetches both
 *   overlays (§4.2.8).
 * - `tracking` owns click-to-track and the shared pick registry.
 * - `ingestion` owns the poll tick; `lifecycle` owns scene ownership.
 *
 * Lifecycle matches the GEV layer contract: configure source, init(viewer),
 * update(viewer, {signal}) on a poll tick, destroy.
 *
 * READ-ONLY (God's Eye): the MCP server is the only command path. Nothing in
 * this layer originates a flight command.
 */
import { createUavState, normalizeServices } from './state.js';
import { createMotion } from './motion.js';
import { createRendering } from './rendering.js';
import { createMissionOverlay } from './missionOverlay.js';
import { createTracking } from './tracking.js';
import { createIngestion } from './ingestion.js';
import { createLifecycle } from './lifecycle.js';
import { createQueries } from './queries.js';
import { createContextOverlay } from './contextOverlay.js';
import { createTheaterWatcher } from './contextPolicy.js';
import { DEFAULT_POLL_MS } from './policy.js';

const OBSERVED = Symbol('uav.observedSource');

/**
 * A view of `source` whose `getSnapshot` also hands each snapshot to
 * `onSnapshot` BEFORE the tick renders it (so a theater reset lands before
 * the new theater's first fix). Everything else reads through to `source`.
 * @param {object} source Live source.
 * @param {(snapshot: object) => void} onSnapshot Observer.
 * @returns {object} Observed source.
 */
function observeSource(source, onSnapshot) {
  if (!source || typeof source.getSnapshot !== 'function' || source[OBSERVED])
    return source;
  const observed = Object.create(source);
  observed[OBSERVED] = true;
  observed.getSnapshot = async (...args) => {
    const snapshot = await source.getSnapshot(...args);
    try {
      onSnapshot(snapshot);
    } catch {
      /* the vehicles still render */
    }
    return snapshot;
  };
  return observed;
}

/**
 * Compose one UAV layer.
 * @param {object} [options] Construction options.
 * @param {object} options.source Live source exposing `getSnapshot`.
 * @param {object} [options.services] Application scene services
 *   (`picking`, `render`, `context`). Every one is optional: omitting them
 *   yields inert shims so the layer can be built headless.
 * @param {number} [options.pollMs] Bridge poll interval in milliseconds.
 * @param {(url: string) => string} [options.resolveAsset] Maps a repository
 *   asset path to a served URL (the 3D hangar convention).
 * @param {{baseUrl: string, token: string, fetchImpl: Function}} [options.missionOverlay]
 *   Bridge origin for `GET /mission-overlay`. Ignored when the source itself
 *   serves `getMissionOverlay`.
 * @param {() => number} [options.now] Clock seam for tests.
 * @param {(viewer: object) => object} [options.createClickHandler] Input
 *   handler seam for tests.
 * @returns {object} The layer module.
 */
export function createUavLayer(options = {}) {
  const {
    source,
    services,
    pollMs = DEFAULT_POLL_MS,
    resolveAsset = (url) => url,
    now = () => Date.now(),
  } = options;
  if (typeof source?.getSnapshot !== 'function')
    throw new TypeError('A snapshot source is required');

  const resolved = normalizeServices(services);
  const state = createUavState({ source, pollMs, now });
  const parts = {};
  const layer = {};
  const context = {
    state,
    services: resolved,
    parts,
    layer,
    options,
    resolveAsset,
  };
  parts.motion = createMotion(context);
  parts.rendering = createRendering(context);
  parts.overlay = createMissionOverlay(context);
  parts.tracking = createTracking(context);
  parts.ingestion = createIngestion(context);
  parts.lifecycle = createLifecycle(context);
  parts.queries = createQueries(context);
  parts.context = createContextOverlay(context);

  // ---- theater watch (§4.2.8) ------------------------------------------------
  const theaterWatcher = createTheaterWatcher();
  const theaterListeners = new Set();
  function onSnapshot(snapshot) {
    const change = theaterWatcher.observe(snapshot?.theater);
    if (!change) return;
    parts.rendering.resetTrails();
    Promise.resolve(parts.overlay.refresh(snapshot, { force: true })).catch(
      () => {},
    );
    parts.context.resetForTheater();
    for (const cb of [...theaterListeners]) {
      try {
        cb(change);
      } catch {
        /* one listener must not starve the rest */
      }
    }
  }
  state.source = observeSource(state.source, onSnapshot);

  Object.assign(
    layer,
    {
      id: 'uav',
      name: 'UAV (AirSim)',
      icon: '🛸',
      source: source.label || 'godSeye UAV',
      // LayerLifecycle arms the update loop from this property (no manual polling).
      refreshInterval: pollMs,

      /**
       * Track one drone by reference.
       * @param {string} reference Vehicle reference.
       * @returns {void}
       */
      track(reference) {
        parts.tracking.track(reference);
      },

      /** Release the tracked drone. */
      untrack() {
        parts.tracking.untrack();
      },

      /**
       * Tracked drone descriptor in the flight-layer shape the cockpit reads.
       * @returns {object|null} Descriptor, or null when nothing is tracked.
       */
      getTrackedInfo() {
        return parts.tracking.getTrackedInfo();
      },
    },
    parts.queries.methods,
    parts.lifecycle.methods,
    parts.ingestion.methods,
  );

  // ---- context overlay and theater watch (WG v2 §4.2.7, §4.2.8) -------------
  const base = {
    update: layer.update,
    enable: layer.enable,
    disable: layer.disable,
    destroy: layer.destroy,
    setSource: layer.setSource,
    getStats: layer.getStats,
  };
  Object.assign(layer, {
    /** One poll tick, then the context overlay's own (unawaited) poll. */
    async update(viewer, options) {
      const result = await base.update(viewer, options);
      parts.context.tick();
      return result;
    },
    async enable(viewer) {
      const result = await base.enable(viewer);
      parts.context.setLayerShown(true);
      return result;
    },
    async disable(viewer) {
      parts.context.setLayerShown(false);
      return base.disable(viewer);
    },
    destroy(viewer) {
      parts.context.destroy(viewer || state.viewer);
      theaterWatcher.reset();
      return base.destroy(viewer);
    },
    setSource(next) {
      base.setSource(next);
      state.source = observeSource(state.source, onSnapshot);
    },
    getStats() {
      return {
        ...base.getStats(),
        contextOverlay: parts.context.getStatus(),
        theater: theaterWatcher.current(),
      };
    },
    /**
     * Show and poll the context overlay (the console's map overview), or
     * hide it and stop polling.
     * @param {boolean} on Active.
     * @returns {boolean} The new state.
     */
    setContextActive: (on) => parts.context.setActive(on),
    /**
     * Per-kind show for the context overlay.
     * @param {{sites?: boolean, forces?: boolean, engagements?: boolean,
     *   vectors?: boolean}} kinds Switches.
     * @returns {object} The switches in force.
     */
    setContextVisibility: (kinds) => parts.context.setVisibility(kinds),
    /**
     * Picks on the map while the context overlay is active: `cb({id})` with a
     * context id (`sit:…`) or `veh:{name}`.
     * @param {Function} cb Listener.
     * @returns {Function} Unsubscribe.
     */
    onContextPick: (cb) => parts.context.onPick(cb),
    /** Fetch the context overlay now (`{force}` drops the held rev). */
    refreshContext: (options) => parts.context.refresh(options),
    /** Context overlay status (sites drawn, not drawn, degraded, …). */
    getContextStatus: () => parts.context.getStatus(),
    /**
     * The running theater changed: `cb({from, to})`, each `{id, epoch}`.
     * @param {Function} cb Listener.
     * @returns {Function} Unsubscribe.
     */
    onTheaterChange(cb) {
      if (typeof cb !== 'function') return () => {};
      theaterListeners.add(cb);
      return () => theaterListeners.delete(cb);
    },
    /** The last running theater seen on `/snapshot`, or null. */
    getTheater: () => theaterWatcher.current(),
  });

  // Compatibility handles the mission panel and the existing suite rely on.
  layer._entities = state.entities;
  Object.defineProperty(layer, '_collection', {
    get: () => state.collection,
  });
  Object.defineProperty(layer, '_overlayCollection', {
    get: () => state.overlayCollection,
  });
  Object.defineProperty(layer, '_contextCollection', {
    get: () => parts.context.collection(),
  });
  Object.defineProperty(layer, 'testing', {
    value: { state, motion: parts.motion, parts },
  });

  return layer;
}

export {
  DEFAULT_POLL_MS,
  LAYER_ID,
  MODEL_SWAP_DISTANCE_M,
  MAX_POSITION_SAMPLES,
  MAX_RENDER_DELAY_MS,
  UAV_MODEL_URL,
} from './policy.js';
export { missionSignature } from './missionOverlay.js';
export { contactNumber, readContact } from './rendering.js';
export { JUMP_GUARD_M } from './motion.js';
export {
  CONTEXT_DATA_SOURCE,
  CONTEXT_POLL_MS,
  MAX_BILLBOARDS,
} from './contextPolicy.js';
