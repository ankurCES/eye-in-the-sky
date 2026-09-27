import { createUavLayer } from '../../layers/uav/index.js';
import * as picking from '../../data/pickRegistry.js';
import * as render from '../../renderGovernor.js';
import * as context from '../../data/contextStore.js';
import { uavBridgeToken, uavBridgeUrl } from '../uavBridge.js';

/**
 * The bridge origin for `GET /mission-overlay`, resolved on every read by the
 * one UAV bridge resolver. It used to read only the build env and ignored the
 * localStorage override (and the in-app host's injected config) that the
 * panel and the source honoured, so the overlay alone asked the wrong origin.
 * Getters rather than values: the overlay reads `baseUrl`/`token` per fetch.
 */
function liveMissionOverlayConfig() {
  return {
    get baseUrl() {
      return uavBridgeUrl();
    },
    get token() {
      return uavBridgeToken();
    },
  };
}

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
  missionOverlay = liveMissionOverlayConfig(),
} = {}) {
  return createUavLayer({
    source,
    resolveAsset,
    missionOverlay,
    services: { picking, render, context },
  });
}
