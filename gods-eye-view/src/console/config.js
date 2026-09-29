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

// ---------------------------------------------------------------------------
// Engagement approval key (WG §3.5, §5.3.2). Platform-free like the rest of
// this module: the caller passes `storage` (sessionStorage in the console)
// and `claim` (api.claimConsole). The key is never logged, never put in a
// URL, and only leaves this holder through `headers()` or `key()`.
// ---------------------------------------------------------------------------

/** Where the console keeps the key for this tab; a reload keeps it. */
export const CONSOLE_KEY_STORAGE = 'godseye.consoleKey';
/** The approval POST header that carries the key. */
export const CONSOLE_KEY_HEADER = 'X-Godseye-Console';
/**
 * Holder states:
 * - `idle`: nothing claimed yet;
 * - `claiming`: the claim POST is in flight;
 * - `held`: this console holds the key and may approve engagements;
 * - `refused`: another client claimed first (409), final until reload;
 * - `unsupported`: this host has no claim route (a Phase A host, the dev
 *   bridge), final until reload;
 * - `failed`: the claim didn't get an answer; `ensure()` may try again.
 */
export const CONSOLE_KEY_STATES = Object.freeze([
  'idle',
  'claiming',
  'held',
  'refused',
  'unsupported',
  'failed',
]);
/** Copy deck (Appendix B). `missing` covers every state but `refused`. */
export const CONSOLE_KEY_COPY = Object.freeze({
  banner: 'Another client claimed engagement approvals; restart to re-arm',
  denyOnly:
    "This console can't approve engagements: another client claimed them.",
  missing:
    "This console can't approve engagements: it doesn't hold the approval key.",
});

const KEY_SHAPE = /^[A-Za-z0-9_-]{16,256}$/;

/** Whether `value` looks like a `secrets.token_urlsafe` key (header-safe). */
export function isConsoleKey(value) {
  return typeof value === 'string' && KEY_SHAPE.test(value);
}

function claimOutcome(error) {
  const status = Number(error?.status) || 0;
  const code = error?.code ?? error?.body?.error ?? null;
  if (status === 409 || code === 'console_already_claimed') return 'refused';
  if (status === 404 || status === 405 || status === 501) return 'unsupported';
  return 'failed';
}

/**
 * The console's engagement approval key.
 *
 * At creation the holder reads `storage[CONSOLE_KEY_STORAGE]`; a valid key
 * there is `held` at once (a reload keeps the key). Otherwise `ensure()`
 * POSTs the claim once: 200 stores the key, 409 is `refused` (the console
 * shows the banner and every engagement slip is Deny-only), a missing
 * route is `unsupported`, and anything else is `failed`, which a later
 * `ensure()` retries. A 2xx without a usable key is `unsupported`: asking
 * again would get the same answer. A storage that throws keeps the key in
 * memory for this page only.
 * @param {object} [options]
 * @param {{getItem(k:string):string|null, setItem(k:string,v:string):void, removeItem?(k:string):void}|null} [options.storage]
 * @param {(() => Promise<{console_key?:string}>)|null} [options.claim]
 */
export function createConsoleKey({ storage = null, claim = null } = {}) {
  let key = null;
  let state = 'idle';
  let inflight = null;
  let attempts = 0;
  const listeners = new Set();

  const stored = readStorage(storage, CONSOLE_KEY_STORAGE);
  if (isConsoleKey(stored)) {
    key = stored;
    state = 'held';
  }

  function set(next) {
    if (next === state) return;
    state = next;
    const info = { state, canApprove: state === 'held' };
    for (const cb of [...listeners]) {
      try {
        cb(info);
      } catch (error) {
        globalThis.console?.error?.(error);
      }
    }
  }

  function remember(value) {
    try {
      storage?.setItem?.(CONSOLE_KEY_STORAGE, value);
    } catch {
      /* private window: the key lives in memory for this page */
    }
  }

  function forget() {
    try {
      storage?.removeItem?.(CONSOLE_KEY_STORAGE);
    } catch {
      /* nothing stored */
    }
  }

  /**
   * Claim the key unless it is held or the answer is final. Concurrent
   * calls share one POST. Resolves with the state; never rejects.
   * @returns {Promise<string>}
   */
  function ensure() {
    if (state === 'held' || state === 'refused' || state === 'unsupported')
      return Promise.resolve(state);
    if (inflight) return inflight;
    if (typeof claim !== 'function') {
      set('unsupported');
      return Promise.resolve(state);
    }
    attempts += 1;
    set('claiming');
    inflight = Promise.resolve()
      .then(() => claim())
      .then(
        (body) => {
          const value =
            body && typeof body === 'object' ? body.console_key : '';
          if (isConsoleKey(value)) {
            key = value;
            remember(value);
            set('held');
          } else {
            set('unsupported');
          }
        },
        (error) => set(claimOutcome(error)),
      )
      .then(() => {
        inflight = null;
        return state;
      });
    return inflight;
  }

  /**
   * Drop a key the host no longer accepts (an approval answered 422
   * `console_required`, for example after the app restarted under a tab
   * that kept its sessionStorage), then claim again. A refused holder stays
   * refused: the banner persists until reload.
   * @returns {Promise<string>}
   */
  function invalidate() {
    if (state === 'refused') return Promise.resolve(state);
    key = null;
    forget();
    if (state === 'held' || state === 'unsupported') set('idle');
    return ensure();
  }

  return {
    /** One of CONSOLE_KEY_STATES. */
    get state() {
      return state;
    },
    /** Whether the claim was refused (409): banner and Deny-only. */
    get refused() {
      return state === 'refused';
    },
    /** How many claim POSTs this holder has sent. */
    get attempts() {
      return attempts;
    },
    /** The key, or null. For the approval header only. */
    key: () => (state === 'held' ? key : null),
    /** Whether this console may approve an engagement. */
    canApprove: () => state === 'held' && key != null,
    /** `{[CONSOLE_KEY_HEADER]: key}` while held, else `{}`. */
    headers: () =>
      state === 'held' && key ? { [CONSOLE_KEY_HEADER]: key } : {},
    /** The Deny-only line for an engagement slip, or null while held. */
    denyOnlyLine: () =>
      state === 'held'
        ? null
        : state === 'refused'
          ? CONSOLE_KEY_COPY.denyOnly
          : CONSOLE_KEY_COPY.missing,
    /** The persistent warn banner's text, or null. */
    bannerText: () => (state === 'refused' ? CONSOLE_KEY_COPY.banner : null),
    ensure,
    invalidate,
    /**
     * Subscribe to state changes: `cb({state, canApprove})` (never the key).
     * @returns {() => void} unsubscribe
     */
    onChange(cb) {
      if (typeof cb !== 'function') return () => {};
      const entry = (info) => cb(info);
      listeners.add(entry);
      return () => listeners.delete(entry);
    },
  };
}
