/**
 * Analyst chat client (contract §3, §7; v1.1 grants §10.2).
 *
 * Wraps the `/chat/*` routes and the session's SSE stream on top of the
 * shared `api` (createApi in ../api.js):
 *
 *   status()                 GET /chat/status
 *   open()                   restore the session id from sessionStorage (or
 *                            POST /chat/sessions) and follow its stream
 *   send(text, ctx?)         POST …/messages {text, context:{focused_ids}}
 *   approve(id, decision, note?, {acknowledged}?)  POST …/approvals/{id}
 *                            with `acknowledged:true` (engagement approvals,
 *                            WG spec §3.5) and the `X-Godseye-Console`
 *                            header once this console holds its key
 *   consoleAccess()          {held, refused}: whether this console may
 *                            approve engagements (the key never leaves here)
 *   interrupt()              POST …/interrupt
 *   grants() / revokeGrant(tool)  GET / DELETE …/grants (null when absent)
 *   newSession()             DELETE the old session, start a fresh one
 *   on(event, cb) -> unsub   raw SSE events by name, plus:
 *                              'event'  {name, data, seq, at, replay}
 *                              'state'  {connection, sessionId}
 *                              'session:replaced' {reason, from, to}
 *                              'interrupt' {at}   (a stop was requested)
 *   close()
 *
 * Sequence handling: every SSE event carries `id: <seq>`. Duplicate ids are
 * dropped here; gaps are detected by the reducer. Events at or below the
 * `last_seq` the server reported on connect are replays and flagged so the
 * view never re-runs their side effects (focus, track). Receipt times are
 * kept per seq in sessionStorage so a reload shows real times, never
 * invented ones.
 */

export const CHAT_EVENTS = Object.freeze([
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

export const SESSION_KEY = 'ic.chat.session';
/** Where the shell keeps the engagement approval key (WG spec §3.5). */
export const CONSOLE_KEY_STORAGE = 'godseye.consoleKey';
/** The header an approval POST carries once the console holds its key. */
export const CONSOLE_HEADER = 'X-Godseye-Console';
const TIMES_PREFIX = 'ic.chat.times.';
const TIMES_MAX = 500;
const TIMES_SAVE_MS = 2000;
const MAX_TEXT = 8000;

/** A typed error for chat calls. `code` drives the copy in the view. */
export class ChatError extends Error {
  constructor(code, message, extra = {}) {
    super(message || code);
    this.name = 'ChatError';
    this.code = code;
    Object.assign(this, extra);
  }
}

function defaultStorage() {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function read(storage, key) {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(storage, key, value) {
  try {
    if (value == null) storage?.removeItem(key);
    else storage?.setItem(key, value);
  } catch {
    // Private mode or quota: the session simply won't survive a reload.
  }
}

/** HTTP status of an api error, whatever shape the api gives it. */
export function statusOf(error) {
  const s =
    error?.status ?? error?.statusCode ?? error?.response?.status ?? null;
  return Number.isFinite(s) ? s : null;
}

/** Parsed body of an api error, if any. */
export function bodyOf(error) {
  const b = error?.body ?? error?.data ?? error?.payload ?? null;
  if (typeof b === 'string') {
    try {
      return JSON.parse(b);
    } catch {
      return null;
    }
  }
  return b && typeof b === 'object' ? b : null;
}

/** Normalize any failure into a ChatError. */
export function chatError(error) {
  if (error instanceof ChatError) return error;
  const name = error?.name || error?.constructor?.name || '';
  const status = statusOf(error);
  const body = bodyOf(error);
  const message = error?.message || String(error);
  if (name === 'AuthError' || status === 401)
    return new ChatError('auth', message, { status });
  if (name === 'OfflineError') return new ChatError('offline', message);
  if (name === 'TimeoutError') return new ChatError('timeout', message);
  if (status === 409)
    return new ChatError('busy', message, {
      status,
      turnId: body?.turn_id ?? null,
    });
  if (status === 503)
    return new ChatError('unavailable', message, {
      status,
      reason: body?.reason ?? null,
      hint: body?.hint ?? null,
    });
  if (status === 404 && body?.error === 'unknown_session')
    return new ChatError('unknown_session', message, { status });
  if (status === 404)
    return new ChatError('not_found', message, { status, body });
  if (status === 422 && body?.error === 'console_required')
    return new ChatError(
      'console_required',
      "the server didn't accept this console's engagement key",
      { status },
    );
  if (status === 422 && body?.error === 'acknowledgement_required')
    return new ChatError(
      'acknowledgement_required',
      'the acknowledgement box must be ticked first',
      { status },
    );
  if (status === 422)
    return new ChatError('invalid', body?.message || message, { status });
  if (status != null) return new ChatError('http', message, { status });
  return new ChatError('offline', message);
}

function parseData(data) {
  if (typeof data !== 'string') return data ?? {};
  try {
    return JSON.parse(data);
  } catch {
    return {};
  }
}

/**
 * @param {object} opts
 * @param {object} opts.api the shared api (createApi)
 * @param {object} [opts.storage] sessionStorage-like
 * @param {() => number} [opts.now]
 * @param {() => (string|null)} [opts.consoleKey] the engagement approval key
 *   (B15a config.js); default: `storage[CONSOLE_KEY_STORAGE]`
 * @param {() => boolean} [opts.consoleRefused] true once the claim got 409
 *   (another client holds the key); default false
 * @param {() => void} [opts.onConsoleRejected] called when an approval POST
 *   answers 422 `console_required` (the host no longer accepts the key held)
 */
export function createChatClient({
  api,
  storage = defaultStorage(),
  now = () => Date.now(),
  consoleKey = null,
  consoleRefused = null,
  onConsoleRejected = null,
} = {}) {
  if (!api) throw new Error('createChatClient needs an api');
  const listeners = new Map();
  let sid = read(storage, SESSION_KEY);
  let stream = null;
  let lastSeq = 0;
  let liveFrom = 0;
  let connection = 'idle';
  let gotSession = false;
  let failures = 0;
  let probing = false;
  let closed = false;
  let times = new Map();
  let timesSavedAt = 0;
  let lastStatus = null;

  function emit(event, ...args) {
    for (const cb of [...(listeners.get(event) || [])]) {
      try {
        cb(...args);
      } catch (error) {
        globalThis.console?.error?.('chat listener failed', error);
      }
    }
  }

  function on(event, cb) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(cb);
    return () => listeners.get(event)?.delete(cb);
  }

  function setConnection(state) {
    if (state === connection) return;
    connection = state;
    emit('state', { connection, sessionId: sid });
  }

  function loadTimes() {
    times = new Map();
    if (!sid) return;
    const raw = read(storage, TIMES_PREFIX + sid);
    if (!raw) return;
    try {
      for (const [seq, at] of JSON.parse(raw)) {
        if (Number.isFinite(seq) && Number.isFinite(at)) times.set(seq, at);
      }
    } catch {
      times = new Map();
    }
  }

  function saveTimes(force = false) {
    if (!sid) return;
    const t = now();
    if (!force && t - timesSavedAt < TIMES_SAVE_MS) return;
    timesSavedAt = t;
    const entries = [...times.entries()].slice(-TIMES_MAX);
    times = new Map(entries);
    write(storage, TIMES_PREFIX + sid, JSON.stringify(entries));
  }

  /** The engagement approval key, or null. Never logged, never emitted. */
  function heldKey() {
    let key = null;
    try {
      key =
        typeof consoleKey === 'function'
          ? consoleKey()
          : read(storage, CONSOLE_KEY_STORAGE);
    } catch {
      key = null;
    }
    return typeof key === 'string' && key.trim() ? key.trim() : null;
  }

  /** Whether this console may approve engagements (WG spec §3.5). */
  function consoleAccess() {
    const held = heldKey() != null;
    let refused = false;
    if (!held && typeof consoleRefused === 'function') {
      try {
        refused = consoleRefused() === true;
      } catch {
        refused = false;
      }
    }
    return { held, refused };
  }

  function path(suffix = '') {
    return `/chat/sessions/${encodeURIComponent(sid)}${suffix}`;
  }

  async function status() {
    try {
      lastStatus = await api.get('/chat/status');
    } catch (error) {
      const err = chatError(error);
      const body = bodyOf(error);
      if (err.code === 'unavailable' && body && (body.error || body.reason)) {
        // The host answered, with a reason: the analyst isn't running in
        // this app (its module failed to start). Not a blip to wait out.
        lastStatus = {
          available: false,
          reason:
            typeof body.reason === 'string' && body.reason
              ? body.reason
              : 'service_unavailable',
          hint: typeof body.hint === 'string' ? body.hint : null,
          error: err.message,
          transient: false,
        };
      } else {
        lastStatus = {
          available: false,
          reason: err.code === 'auth' ? 'token' : 'unreachable',
          error: err.message,
          transient: err.code !== 'auth',
        };
      }
    }
    emit('status', lastStatus);
    return lastStatus;
  }

  async function createSession() {
    const res = await api.post('/chat/sessions', {});
    const id = res?.session_id;
    if (typeof id !== 'string' || !id) {
      throw new ChatError('http', 'The analyst did not start a session.');
    }
    return id;
  }

  function adopt(id) {
    sid = id;
    write(storage, SESSION_KEY, id);
    lastSeq = 0;
    liveFrom = 0;
    loadTimes();
  }

  async function ensureSession() {
    if (sid) return sid;
    adopt(await createSession());
    return sid;
  }

  function onEvent(name, rawData, rawId) {
    if (closed) return;
    const data = parseData(rawData);
    const seq = Number.parseInt(rawId, 10) || 0;
    if (name === 'session') {
      gotSession = true;
      failures = 0;
      liveFrom = Number.isFinite(data?.last_seq) ? data.last_seq : lastSeq;
      setConnection('open');
      const at = now();
      emit('event', { name, data, seq: 0, at, replay: false });
      emit(name, data, { seq: 0, at, replay: false });
      return;
    }
    if (seq > 0 && seq <= lastSeq) return; // duplicate id
    const replay = seq > 0 && seq <= liveFrom;
    let at = null;
    if (replay) at = times.get(seq) ?? null;
    else {
      at = now();
      if (seq > 0) times.set(seq, at);
    }
    if (seq > 0) lastSeq = seq;
    const meta = { seq, at, replay };
    emit('event', { name, data, ...meta });
    emit(name, data, meta);
    saveTimes(name === 'turn_end' || name === 'approval_resolved');
  }

  async function probeSession() {
    if (probing || !sid) return;
    probing = true;
    try {
      const st = await status();
      if (st?.transient || st?.reason === 'token') return; // service down: keep waiting
      let gone = false;
      try {
        await api.get(path('/grants'));
      } catch (error) {
        const err = chatError(error);
        gone = err.code === 'unknown_session' || err.code === 'not_found';
      }
      if (gone && !closed) await replaceSession('lost');
    } finally {
      probing = false;
    }
  }

  function connect() {
    stream?.close?.();
    gotSession = false;
    failures = 0;
    setConnection(lastSeq > 0 ? 'reconnecting' : 'connecting');
    stream = api.sse(path('/stream'), {
      events: CHAT_EVENTS,
      lastEventId: lastSeq > 0 ? String(lastSeq) : undefined,
      onOpen: () => {
        if (!closed) setConnection('open');
      },
      onEvent,
      onError: () => {
        if (closed) return;
        setConnection('reconnecting');
        if (!gotSession) {
          failures += 1;
          if (failures >= 2) probeSession();
        } else {
          // The wrapper reconnects with Last-Event-ID; the next `session`
          // event decides whether anything was lost.
          gotSession = false;
        }
      },
    });
  }

  async function open() {
    closed = false;
    const hadSession = Boolean(sid);
    if (hadSession) loadTimes();
    await ensureSession();
    connect();
    return sid;
  }

  async function replaceSession(reason) {
    const from = sid;
    stream?.close?.();
    stream = null;
    write(storage, TIMES_PREFIX + (from || ''), null);
    write(storage, SESSION_KEY, null);
    sid = null;
    const id = await createSession();
    adopt(id);
    emit('session:replaced', { reason, from, to: id });
    if (!closed) connect();
    return id;
  }

  async function newSession() {
    const from = sid;
    if (from) {
      try {
        await api.del(path(''));
      } catch {
        // Already gone, or offline: a fresh session is started either way.
      }
    }
    return replaceSession('operator');
  }

  async function send(text, ctx = {}) {
    const body = { text: String(text ?? '').trim() };
    if (!body.text) throw new ChatError('invalid', 'Nothing to send.');
    if (body.text.length > MAX_TEXT)
      throw new ChatError(
        'invalid',
        `Messages are limited to ${MAX_TEXT} characters.`,
      );
    const ids = Array.isArray(ctx?.focused_ids)
      ? ctx.focused_ids.filter((id) => typeof id === 'string').slice(0, 20)
      : [];
    if (ids.length) body.context = { focused_ids: ids };
    await ensureSession();
    try {
      return await api.post(path('/messages'), body);
    } catch (error) {
      const err = chatError(error);
      if (err.code !== 'unknown_session') throw err;
      await replaceSession('lost');
      try {
        return await api.post(path('/messages'), body);
      } catch (retryError) {
        throw chatError(retryError);
      }
    }
  }

  async function approve(approvalId, decision, note, opts = {}) {
    if (!['approve', 'deny', 'approve_session'].includes(decision)) {
      throw new ChatError('invalid', `Unknown decision ${decision}`);
    }
    if (!sid) throw new ChatError('unknown_session', 'No analyst session.');
    const body = { decision };
    const text = typeof note === 'string' ? note.trim() : '';
    if (text) body.note = text.slice(0, 2000);
    // Only an approval carries the acknowledgement; a deny never needs it.
    if (opts?.acknowledged === true && decision !== 'deny')
      body.acknowledged = true;
    // The key goes on every approval POST once the console holds it.
    const key = heldKey();
    try {
      return await api.post(
        path(`/approvals/${encodeURIComponent(approvalId)}`),
        body,
        ...(key ? [{ headers: { [CONSOLE_HEADER]: key } }] : []),
      );
    } catch (error) {
      const err = chatError(error);
      if (
        err.code === 'console_required' &&
        key &&
        typeof onConsoleRejected === 'function'
      ) {
        try {
          onConsoleRejected();
        } catch (hookError) {
          globalThis.console?.error?.(hookError);
        }
      }
      throw err;
    }
  }

  async function interrupt() {
    if (!sid) return { ok: false };
    // Tell listeners first: whoever pressed Stop (composer, menu, ⌘.), the
    // transcript records "Stopped by you at …" with the press time.
    emit('interrupt', { at: now() });
    try {
      return await api.post(path('/interrupt'), {});
    } catch (error) {
      throw chatError(error);
    }
  }

  /** Current grants, or null when the server has no grants route (v1). */
  async function grants() {
    if (!sid) return null;
    try {
      const res = await api.get(path('/grants'));
      return Array.isArray(res?.grants) ? res.grants : [];
    } catch (error) {
      const err = chatError(error);
      if (err.code === 'not_found') return null;
      throw err;
    }
  }

  async function revokeGrant(tool) {
    if (!sid) throw new ChatError('unknown_session', 'No analyst session.');
    try {
      return await api.del(path(`/grants/${encodeURIComponent(tool)}`));
    } catch (error) {
      throw chatError(error);
    }
  }

  function close() {
    closed = true;
    saveTimes(true);
    stream?.close?.();
    stream = null;
    setConnection('closed');
  }

  return {
    status,
    open,
    send,
    approve,
    consoleAccess,
    interrupt,
    grants,
    revokeGrant,
    newSession,
    on,
    close,
    get sessionId() {
      return sid;
    },
    get connection() {
      return connection;
    },
    get lastStatus() {
      return lastStatus;
    },
    get lastSeq() {
      return lastSeq;
    },
  };
}
