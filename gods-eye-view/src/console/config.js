/**
 * Bridge address and token: ONE resolver for every UAV reader in the app.
 *
 * Precedence, per field (contract §7):
 * 1. `globalThis.__GODSEYE__` — injected by the app host into index.html
 *    (`{bridgeUrl:"", token:"…"}`; an empty bridgeUrl means same origin).
 * 2. Storage `gev.uav.base` / `gev.uav.token` (a developer override).
 * 3. `import.meta.env.VITE_UAV_BRIDGE_URL` / `VITE_UAV_BRIDGE_TOKEN`.
 * 4. Defaults: the page's own origin when it is served by the app host,
 *    otherwise `http://localhost:8790`; token `dev-token`.
 *
 * This module is platform-free on purpose: it names no browser global, so a
 * portable source (src/sources/live/*) may import it. Browser callers pass
 * `storage` (for example `localStorage`) explicitly; without it step 2 is
 * skipped.
 */

export const DEFAULT_BRIDGE_BASE = 'http://localhost:8790';
export const DEFAULT_BRIDGE_TOKEN = 'dev-token';
export const STORAGE_KEYS = Object.freeze({
  base: 'gev.uav.base',
  token: 'gev.uav.token',
});

/** Ports the Vite dev and preview servers use; the bridge is never there. */
const DEV_SERVER_PORTS = new Set(['4173', '5173', '5199']);

function viteEnv() {
  return import.meta.env || {};
}

function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function stripSlash(base) {
  return base.replace(/\/+$/, '');
}

function readStorage(storage, key) {
  if (!storage || typeof storage.getItem !== 'function') return '';
  try {
    return text(storage.getItem(key));
  } catch {
    return '';
  }
}

/** The page's own origin when it can host the bridge routes, else ''. */
export function sameOrigin(location) {
  const origin = text(location?.origin);
  const protocol = text(location?.protocol);
  if (!origin || origin === 'null') return '';
  if (protocol && protocol !== 'http:' && protocol !== 'https:') return '';
  return stripSlash(origin);
}

function defaultBase(location) {
  const origin = sameOrigin(location);
  const port = text(location?.port);
  if (!origin || DEV_SERVER_PORTS.has(port)) return DEFAULT_BRIDGE_BASE;
  return origin;
}

/**
 * Resolve the bridge address and token.
 * @param {object} [options]
 * @param {object} [options.globalConfig] defaults to `globalThis.__GODSEYE__`
 * @param {{getItem(key:string):string|null}|null} [options.storage] e.g. localStorage
 * @param {object} [options.env] defaults to `import.meta.env`
 * @param {{origin?:string, protocol?:string, port?:string}} [options.location]
 * @returns {{base:string, token:string, source:'injected'|'storage'|'env'|'default', tokenSource:'injected'|'storage'|'env'|'default'}}
 */
export function resolveBridge({
  globalConfig = globalThis.__GODSEYE__,
  storage = null,
  env = viteEnv(),
  location = globalThis.location,
} = {}) {
  const injected =
    globalConfig && typeof globalConfig === 'object' ? globalConfig : null;

  let base = '';
  let source = 'default';
  if (injected && typeof injected.bridgeUrl === 'string') {
    // "" is meaningful: same origin as the page the host served.
    base = text(injected.bridgeUrl) || sameOrigin(location);
    source = 'injected';
  } else if (readStorage(storage, STORAGE_KEYS.base)) {
    base = readStorage(storage, STORAGE_KEYS.base);
    source = 'storage';
  } else if (text(env?.VITE_UAV_BRIDGE_URL)) {
    base = text(env.VITE_UAV_BRIDGE_URL);
    source = 'env';
  } else {
    base = defaultBase(location);
  }

  let token = '';
  let tokenSource = 'default';
  if (injected && text(injected.token)) {
    token = text(injected.token);
    tokenSource = 'injected';
  } else if (readStorage(storage, STORAGE_KEYS.token)) {
    token = readStorage(storage, STORAGE_KEYS.token);
    tokenSource = 'storage';
  } else if (text(env?.VITE_UAV_BRIDGE_TOKEN)) {
    token = text(env.VITE_UAV_BRIDGE_TOKEN);
    tokenSource = 'env';
  } else {
    token = DEFAULT_BRIDGE_TOKEN;
  }

  return { base: stripSlash(base), token, source, tokenSource };
}
