/**
 * GEV's reader for the UAV bridge origin and bearer token.
 *
 * Before this module there were four readers (the mission panel's getters in
 * controls.js, the source configuration in sources.js, the live source's own
 * env lookup and the mission overlay in layers/uav.js), and they disagreed:
 * the overlay ignored localStorage, the source let a build-time env value
 * beat an operator's localStorage override, and none of them knew about the
 * in-app host, which injects `window.__GODSEYE__ = {bridgeUrl:'', token}`
 * into index.html (bridgeUrl '' means "same origin").
 *
 * The order itself lives in ONE place, the console's platform-free resolver
 * (src/console/config.js `resolveBridge`, contract §7), per field:
 *   1. globalThis.__GODSEYE__ {bridgeUrl, token} injected by the host
 *   2. localStorage gev.uav.base / gev.uav.token (operator override)
 *   3. import.meta.env VITE_UAV_BRIDGE_URL / VITE_UAV_BRIDGE_TOKEN
 *   4. defaults (http://localhost:8790 off the app host, dev-token)
 * This module only supplies the page's storage, which that resolver leaves to
 * browser callers, and resolves on every call, so a token written to
 * localStorage (or a host restart that injects a new one) reaches the next
 * request without a reload. The console and GEV therefore always talk to the
 * same bridge with the same token.
 */
import { resolveBridge } from '../console/config.js';

/** The page's localStorage, or null where reading it throws or is absent. */
export function pageStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * Resolve against the live page.
 * @param {object} [overrides] injectable inputs, for tests
 * @returns {{base: string, token: string, source: string, tokenSource: string}}
 */
export function currentUavBridge(overrides = {}) {
  return resolveBridge({ storage: pageStorage(), ...overrides });
}

/** Late-bound getter for the bridge origin (read on every request). */
export const uavBridgeUrl = () => currentUavBridge().base;

/** Late-bound getter for the bearer token (read on every request). */
export const uavBridgeToken = () => currentUavBridge().token;
