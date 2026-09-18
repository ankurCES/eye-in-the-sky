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
import { DEFAULT_POLL_MS } from './policy.js';

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

  // Compatibility handles the mission panel and the existing suite rely on.
  layer._entities = state.entities;
  Object.defineProperty(layer, '_collection', {
    get: () => state.collection,
  });
  Object.defineProperty(layer, '_overlayCollection', {
    get: () => state.overlayCollection,
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
