import { createUavLayer } from '../../layers/uav/index.js';
import * as picking from '../../data/pickRegistry.js';
import * as render from '../../renderGovernor.js';
import * as context from '../../data/contextStore.js';

/** Bridge defaults mirror src/sources/live/uav.js — loopback only. */
const DEFAULT_BRIDGE_URL = 'http://localhost:8790';
const DEFAULT_BRIDGE_TOKEN = 'dev-token';

/**
 * Construct the UAV (AirSim) layer with an application-supplied source.
 *
 * The layer receives its scene capabilities here rather than importing them:
 * `picking` so a click on a drone is recognized as this layer's pick (and so
 * siblings stop treating it as empty space), `render` so interpolated motion
 * reaches the screen while the governor is in request-render mode, and
 * `context` so a tracked drone occupies the shared subject slot the cockpit
 * and voice tools read.
 *
 * `missionOverlay` names the bridge origin for `GET /mission-overlay`; the
 * layer prefers a source that serves the overlay itself when one exists.
 *
 * @param {object} [options] Construction options.
 * @param {object} options.source Live UAV source exposing `getSnapshot`.
 * @param {(url: string) => string} [options.resolveAsset] 3D hangar resolver.
 * @param {{baseUrl: string, token: string}} [options.missionOverlay] Bridge origin.
 * @returns {object} The UAV layer module.
 */
export function createApplicationUav({
  source,
  resolveAsset = (url) =>
    `${import.meta.env?.BASE_URL || '/'}${url.replace(/^\//, '')}`,
  missionOverlay = {
    baseUrl: import.meta.env?.VITE_UAV_BRIDGE_URL || DEFAULT_BRIDGE_URL,
    token: import.meta.env?.VITE_UAV_BRIDGE_TOKEN || DEFAULT_BRIDGE_TOKEN,
  },
} = {}) {
  return createUavLayer({
    source,
    resolveAsset,
    missionOverlay,
    services: { picking, render, context },
  });
}
