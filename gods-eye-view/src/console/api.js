/**
 * HTTP and SSE access to the app host (bridge, intel and chat routes).
 *
 * Every request carries `Authorization: Bearer <token>`; SSE carries the token
 * in the query (`?token=`, EventSource cannot set headers) and is never
 * logged. Failures are typed so each surface can say what actually happened:
 * - AuthError: 401, the token was rejected;
 * - OfflineError: the local service could not be reached;
 * - TimeoutError: no answer in time (a command may still have landed);
 * - HttpError: any other non-2xx answer, with `status` and the parsed body.
 */

export const DEFAULT_TIMEOUT_MS = 10000;
/** Reconnect delays for an SSE stream the browser gave up on. */
export const SSE_RETRY_MS = Object.freeze([1000, 2000, 5000, 10000]);
/** Named SSE events delivered by default (chat stream + bridge alarm lane). */
export const DEFAULT_SSE_EVENTS = Object.freeze([
  'message',
  'alarm',
  'session',
  'turn_start',
  'text_delta',
  'thinking',
  'tool_call',
  'approval_request',
  'approval_resolved',
  'tool_result',
  'ui',
  'usage',
  'turn_end',
  'error',
  'provider_changed',
]);

export class AuthError extends Error {
  constructor(message = "The console's access token was rejected.", options) {
    super(message, options);
    this.name = 'AuthError';
    this.status = options?.status ?? 401;
    this.body = options?.body ?? null;
  }
}

export class OfflineError extends Error {
  constructor(message = "Can't reach the local service.", options) {
    super(message, options);
    this.name = 'OfflineError';
  }
}

export class TimeoutError extends Error {
  constructor(message = 'The local service did not answer in time.', options) {
    super(message, options);
    this.name = 'TimeoutError';
    this.timeoutMs = options?.timeoutMs ?? null;
  }
}

export class HttpError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = 'HttpError';
    this.status = options?.status ?? 0;
    this.body = options?.body ?? null;
    /** The host's machine-readable `{error}` code, when it sent one. */
    this.code = errorCode(this.body);
  }
}

/**
 * The host's error codes (top-level `{error}` bodies, contract §3) in words an
 * operator can act on. Codes not listed are humanized ("rate_limited" ->
 * "rate limited"), never shown raw.
 */
export const ERROR_WORDS = Object.freeze({
  intel_unavailable: "the intel service didn't start in this app",
  chat_unavailable: "the analyst service didn't start in this app",
  unknown_entity: "it isn't in the intel picture",
  invalid_scope: "the picture scope wasn't recognized",
  unknown_session: 'the analyst session has ended',
  not_available_in_app_host: "this data source isn't available in the app",
  // Analyst settings (BYOK spec §7).
  invalid_settings: "the host didn't accept these settings",
  settings_conflict: 'these settings changed in another window',
  locked_by_environment: 'that setting comes from the environment',
  needs_check: 'the settings changed after the connection check',
  needs_ack: 'this model needs your acknowledgement first',
  cross_origin: 'analyst settings only open from the app host',
  test_busy: 'another connection check is running',
  settings_unavailable: "analyst settings aren't available in this app",
  if_match_required: 'the settings were reloaded; try again',
  read_only: 'these settings were written by a newer version of Eye in the Sky',
  key_store_failed: "the key couldn't be saved",
  unknown_provider: "that provider isn't in the list",
  not_found: "the host doesn't have that page",
  json_required: 'the host only accepts JSON here',
  too_large: 'the request was too large',
  token_in_url: 'the access token must not be in the address',
});

/** A `{error: "snake_case_code"}` body's code, or null. */
export function errorCode(body) {
  const code = body && typeof body === 'object' ? body.error : null;
  return typeof code === 'string' && /^[a-z][a-z0-9_]*$/.test(code)
    ? code
    : null;
}

// Explicit bidi embedding/override/isolate controls (LRE, RLE, PDF, LRO, RLO,
// LRI, RLI, FSI, PDI). Host payloads carry third-party strings (OSM names,
// analyst tool args): an RLO in a contact name would make "YLDNEIRF" read as
// "FRIENDLY", and one inside an interpolated field reverses the console's own
// words around it. Letters keep their own direction without these controls.
const BIDI_CONTROLS = /[\u202A-\u202E\u2066-\u2069]/g;
const HAS_BIDI = /[\u202A-\u202E\u2066-\u2069]/;

/** `text` with the explicit bidi controls removed. */
export function stripBidi(text) {
  if (typeof text !== 'string' || !HAS_BIDI.test(text)) return text;
  return text.replace(BIDI_CONTROLS, '');
}

/** `text` with each explicit bidi control shown as a visible, inert
 *  `<U+202E>` token, for the exact request an operator approves. */
export function showBidi(text) {
  if (typeof text !== 'string' || !HAS_BIDI.test(text)) return text;
  return text.replace(
    BIDI_CONTROLS,
    (c) => `<U+${c.charCodeAt(0).toString(16).toUpperCase()}>`,
  );
}

/**
 * Neutralize bidi controls in every string of a parsed host payload, in
 * place (the payload is freshly parsed and owned by the caller). Tool `args`
 * keep a visible token instead, so "Show exact request" stays exact.
 */
export function neutralizeBidi(value, visible = false, depth = 0) {
  if (typeof value === 'string')
    return visible ? showBidi(value) : stripBidi(value);
  if (!value || typeof value !== 'object' || depth > 64) return value;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const v = value[i];
      if (v && (typeof v === 'object' || typeof v === 'string'))
        value[i] = neutralizeBidi(v, visible, depth + 1);
    }
    return value;
  }
  for (const key of Object.keys(value)) {
    const v = value[key];
    const clean = neutralizeBidi(v, visible || key === 'args', depth + 1);
    if (HAS_BIDI.test(key)) {
      delete value[key];
      value[stripBidi(key)] = clean;
    } else if (clean !== v) value[key] = clean;
  }
  return value;
}

/** Zulu clock time: "14:02Z", or "14:02:51Z" with seconds. */
export function formatZulu(ms, { seconds = false } = {}) {
  if (ms == null || ms === '' || typeof ms === 'boolean') return '';
  const n = Number(ms);
  if (!Number.isFinite(n)) return '';
  const iso = new Date(n).toISOString();
  return `${iso.slice(11, seconds ? 19 : 16)}Z`;
}

/** Join a base ("" = same origin) and an absolute path. */
export function joinUrl(base, path) {
  const b = String(base || '').replace(/\/+$/, '');
  const p = String(path || '');
  if (/^https?:\/\//i.test(p)) return p;
  return `${b}${p.startsWith('/') ? '' : '/'}${p}`;
}

function withQuery(url, params) {
  const parts = Object.entries(params)
    .filter(([, v]) => v != null && v !== '')
    .map(
      ([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`,
    );
  if (!parts.length) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${parts.join('&')}`;
}

function errorMessage(body, status, statusText) {
  if (body && typeof body === 'object') {
    // Prefer a sentence the host wrote; a bare code gets the console's words.
    if (typeof body.message === 'string' && body.message.trim())
      return body.message.trim();
    const code = errorCode(body);
    if (code) return ERROR_WORDS[code] || code.replace(/_/g, ' ');
    const detail = body.detail ?? body.error ?? body.message;
    if (typeof detail === 'string' && detail) return detail;
    if (detail != null) {
      try {
        return JSON.stringify(detail);
      } catch {
        /* fall through */
      }
    }
  }
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 300);
  return `HTTP ${status}${statusText ? ` ${statusText}` : ''}`;
}

async function readBody(res) {
  if (typeof res?.text === 'function') {
    const raw = await res.text();
    if (!raw) return null;
    try {
      return neutralizeBidi(JSON.parse(raw));
    } catch {
      return stripBidi(raw);
    }
  }
  if (typeof res?.json === 'function') return neutralizeBidi(await res.json());
  return null;
}

function parseData(data) {
  if (typeof data !== 'string') return data ?? null;
  if (!data) return null;
  try {
    return neutralizeBidi(JSON.parse(data));
  } catch {
    return stripBidi(data);
  }
}

/**
 * Create the API client.
 * @param {object} options
 * @param {string} options.base origin of the app host ("" = same origin)
 * @param {string} options.token bearer token
 * @param {Function} [options.fetchImpl] defaults to globalThis.fetch
 * @param {Function} [options.EventSourceImpl] defaults to globalThis.EventSource
 * @param {number} [options.timeoutMs]
 * @param {{setTimeout:Function, clearTimeout:Function}} [options.timers]
 */
export function createApi({
  base = '',
  token = '',
  fetchImpl,
  EventSourceImpl,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  timers,
} = {}) {
  const setT =
    timers?.setTimeout || ((fn, ms) => globalThis.setTimeout(fn, ms));
  const clearT = timers?.clearTimeout || ((id) => globalThis.clearTimeout(id));
  const url = (path) => joinUrl(base, path);

  async function request(method, path, options = {}) {
    const fetchFn = fetchImpl || globalThis.fetch;
    if (typeof fetchFn !== 'function') {
      throw new OfflineError('This window has no network access.');
    }
    const limit = Number.isFinite(options.timeoutMs)
      ? options.timeoutMs
      : timeoutMs;
    const headers = { Accept: 'application/json', ...(options.headers || {}) };
    if (token) headers.Authorization = `Bearer ${token}`;
    const init = { method, headers, cache: 'no-store' };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    const controller =
      typeof globalThis.AbortController === 'function'
        ? new globalThis.AbortController()
        : null;
    if (controller) init.signal = controller.signal;
    const outer = options.signal;
    const onOuterAbort = () => controller?.abort(outer.reason);
    if (outer) {
      if (outer.aborted) onOuterAbort();
      else outer.addEventListener?.('abort', onOuterAbort, { once: true });
    }

    let timer = null;
    let timedOut = false;
    const timeout = new Promise((_, reject) => {
      if (!(limit > 0)) return;
      timer = setT(() => {
        timedOut = true;
        controller?.abort();
        reject(new TimeoutError(undefined, { timeoutMs: limit }));
      }, limit);
    });

    try {
      let res;
      try {
        res = await Promise.race([fetchFn(url(path), init), timeout]);
      } catch (error) {
        if (timedOut || error instanceof TimeoutError) {
          throw new TimeoutError(undefined, { timeoutMs: limit, cause: error });
        }
        if (outer?.aborted) throw error;
        throw new OfflineError(undefined, { cause: error });
      }
      const body = await Promise.race([
        readBody(res).catch(() => null),
        timeout,
      ]);
      const status = Number(res?.status) || 0;
      if (status === 401) throw new AuthError(undefined, { status, body });
      if (!(res?.ok ?? (status >= 200 && status < 300))) {
        throw new HttpError(errorMessage(body, status, res?.statusText), {
          status,
          body,
        });
      }
      return body;
    } finally {
      if (timer != null) clearT(timer);
      outer?.removeEventListener?.('abort', onOuterAbort);
    }
  }

  const get = (path, opts) => request('GET', path, opts);
  const post = (path, body, opts) =>
    request('POST', path, { ...(opts || {}), body: body ?? {} });
  const del = (path, opts) => request('DELETE', path, opts);
  /** PUT a JSON body; `opts.headers` carries e.g. `If-Match`. */
  const put = (path, body, opts) =>
    request('PUT', path, { ...(opts || {}), body: body ?? {} });

  /**
   * Subscribe to an SSE stream with reconnect and Last-Event-ID replay.
   *
   * The browser's own EventSource retry already sends Last-Event-ID. When it
   * gives up (readyState CLOSED: an HTTP error, a dropped host), this wrapper
   * reopens the stream after a backoff and passes the last id it saw as
   * `last_event_id` in the query, which the host honours like the header.
   * @param {string} path e.g. `/events` or `/chat/sessions/{sid}/stream`
   * @param {object} handlers
   * @param {(name:string, data:any, id:string|null) => void} handlers.onEvent
   * @param {(info:{reconnected:boolean, lastEventId:string|null}) => void} [handlers.onOpen]
   * @param {(info:{reconnecting:boolean, attempt:number, delayMs:number|null, native:boolean}) => void} [handlers.onError]
   * @param {string|number} [handlers.lastEventId]
   * @param {string[]} [handlers.events] named events to deliver
   * @returns {{close():void, reconnect():void, readonly lastEventId:string|null}}
   */
  function sse(path, handlers = {}) {
    const { onEvent, onOpen, onError } = handlers;
    const names = [...new Set(handlers.events || DEFAULT_SSE_EVENTS)];
    const ES = EventSourceImpl || globalThis.EventSource;
    let lastEventId =
      handlers.lastEventId != null && handlers.lastEventId !== ''
        ? String(handlers.lastEventId)
        : null;
    let es = null;
    let closed = false;
    let timer = null;
    let attempt = 0;
    let opens = 0;

    if (typeof ES !== 'function') {
      onError?.({
        reconnecting: false,
        attempt: 0,
        delayMs: null,
        native: false,
        unsupported: true,
      });
      return {
        close() {},
        reconnect() {},
        get lastEventId() {
          return lastEventId;
        },
      };
    }

    function deliver(name, ev) {
      if (closed) return;
      const id = ev?.lastEventId ? String(ev.lastEventId) : null;
      if (id) lastEventId = id;
      try {
        onEvent?.(name, parseData(ev?.data), id);
      } catch (error) {
        globalThis.console?.error?.(error);
      }
    }

    function connectionError(source) {
      if (closed || source !== es) return;
      const state = es?.readyState;
      if (state === 2 || state === ES.CLOSED) {
        // The browser gave up. Reopen ourselves, replaying from the last id.
        try {
          es.close();
        } catch {
          /* already closed */
        }
        es = null;
        const delayMs =
          SSE_RETRY_MS[Math.min(attempt, SSE_RETRY_MS.length - 1)];
        attempt += 1;
        onError?.({ reconnecting: true, attempt, delayMs, native: false });
        timer = setT(() => {
          timer = null;
          connect();
        }, delayMs);
      } else {
        onError?.({ reconnecting: true, attempt, delayMs: null, native: true });
      }
    }

    function connect() {
      if (closed) return;
      const target = withQuery(url(path), {
        token: token || null,
        last_event_id: lastEventId,
      });
      let source;
      try {
        source = new ES(target);
      } catch {
        es = null;
        const delayMs =
          SSE_RETRY_MS[Math.min(attempt, SSE_RETRY_MS.length - 1)];
        attempt += 1;
        onError?.({ reconnecting: true, attempt, delayMs, native: false });
        timer = setT(() => {
          timer = null;
          connect();
        }, delayMs);
        return;
      }
      es = source;
      const opened = () => {
        if (closed || source !== es) return;
        attempt = 0;
        opens += 1;
        onOpen?.({ reconnected: opens > 1, lastEventId });
      };
      // `error` is both the connection-error event and a named server event
      // (`event: error`). Only a MessageEvent carries string data.
      const errored = (ev) => {
        if (typeof ev?.data === 'string') deliver('error', ev);
        else connectionError(source);
      };
      source.onopen = opened;
      source.onerror = errored;
      for (const name of names) {
        if (name === 'error') continue;
        source.addEventListener?.(name, (ev) => {
          if (source === es) deliver(name, ev);
        });
      }
    }

    connect();

    return {
      close() {
        closed = true;
        if (timer != null) clearT(timer);
        timer = null;
        try {
          es?.close();
        } catch {
          /* already closed */
        }
        es = null;
      },
      reconnect() {
        if (closed) return;
        if (timer != null) clearT(timer);
        timer = null;
        try {
          es?.close();
        } catch {
          /* already closed */
        }
        es = null;
        connect();
      },
      get lastEventId() {
        return lastEventId;
      },
    };
  }

  /**
   * Operator-direct Abort (contract §10.5): the EXISTING bridge command route,
   * never the analyst. Resolves with the bridge's unwrapped tool answer
   * (`{aborted:true,…}`, `{aborted:false, refused:true, reason}`, or
   * `{error, isError:true}`); transport failures throw the typed errors above.
   */
  function abortVehicle(vehicle) {
    const name = String(vehicle ?? '').trim();
    if (!name)
      return Promise.reject(new TypeError('abortVehicle needs a vehicle name'));
    return post('/control/command', {
      tool: 'uav_abort',
      vehicle: name,
      arguments: { vehicle: name },
    });
  }

  return { base, token, url, get, post, put, del, sse, abortVehicle };
}
