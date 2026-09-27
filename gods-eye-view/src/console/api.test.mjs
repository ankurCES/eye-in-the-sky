import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  AuthError,
  HttpError,
  OfflineError,
  SSE_RETRY_MS,
  TimeoutError,
  createApi,
  formatZulu,
  joinUrl,
  neutralizeBidi,
  showBidi,
  stripBidi,
} from './api.js';

function response(status, body, statusText = '') {
  const text =
    body === undefined
      ? ''
      : typeof body === 'string'
        ? body
        : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText,
    text: async () => text,
  };
}

function recordingFetch(reply) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return typeof reply === 'function' ? reply(url, init) : reply;
  };
  return { calls, fetchImpl };
}

/** Manual timers: nothing fires until `run()`. */
function manualTimers() {
  const pending = new Map();
  let seq = 0;
  return {
    setTimeout(fn, ms) {
      const id = ++seq;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    pending,
    runAll() {
      const list = [...pending.entries()];
      pending.clear();
      for (const [, t] of list) t.fn();
      return list.map(([, t]) => t.ms);
    },
  };
}

test('get sends the bearer token and parses JSON', async () => {
  const { calls, fetchImpl } = recordingFetch(
    response(200, { ok: true, n: 3 }),
  );
  const api = createApi({
    base: 'http://127.0.0.1:52100/',
    token: 'secret',
    fetchImpl,
  });
  const out = await api.get('/intel/graph?scope=theater');
  assert.deepEqual(out, { ok: true, n: 3 });
  assert.equal(
    calls[0].url,
    'http://127.0.0.1:52100/intel/graph?scope=theater',
  );
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer secret');
  assert.equal(calls[0].init.body, undefined);
  assert.equal(api.base, 'http://127.0.0.1:52100/');
});

test('post sends a JSON body; an empty 204 answer is null', async () => {
  const { calls, fetchImpl } = recordingFetch(response(204));
  const api = createApi({ base: '', token: 't', fetchImpl });
  const out = await api.post('/chat/sessions', { a: 1 });
  assert.equal(out, null);
  assert.equal(calls[0].url, '/chat/sessions');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.equal(calls[0].init.body, '{"a":1}');
  await api.del('/chat/sessions/abc');
  assert.equal(calls[1].init.method, 'DELETE');
});

test('401 is an AuthError', async () => {
  const { fetchImpl } = recordingFetch(
    response(401, { detail: 'unauthorized' }),
  );
  const api = createApi({ base: '', token: 'bad', fetchImpl });
  await assert.rejects(api.get('/snapshot'), (err) => {
    assert.ok(err instanceof AuthError);
    assert.equal(err.name, 'AuthError');
    assert.equal(err.status, 401);
    return true;
  });
});

test('other non-2xx answers are HttpError with the status and server detail', async () => {
  const { fetchImpl } = recordingFetch(
    response(409, { detail: 'a turn is running' }),
  );
  const api = createApi({ base: '', token: 't', fetchImpl });
  await assert.rejects(
    api.post('/chat/sessions/s/messages', { text: 'hi' }),
    (err) => {
      assert.ok(err instanceof HttpError);
      assert.equal(err.status, 409);
      assert.equal(err.message, 'a turn is running');
      assert.deepEqual(err.body, { detail: 'a turn is running' });
      return true;
    },
  );
  const plain = createApi({
    fetchImpl: async () => response(502, 'Bad gateway', 'Bad Gateway'),
  });
  await assert.rejects(
    plain.get('/x'),
    (err) => err instanceof HttpError && err.message === 'Bad gateway',
  );
  const empty = createApi({
    fetchImpl: async () => response(500, '', 'Internal'),
  });
  await assert.rejects(
    empty.get('/x'),
    (err) => err.message === 'HTTP 500 Internal',
  );
});

test('host error codes ({error} bodies) read as words and keep the code', async () => {
  const api = createApi({
    fetchImpl: async (url) =>
      url.includes('entity')
        ? response(404, { error: 'unknown_entity', id: 'trk:x' })
        : url.includes('graph')
          ? response(503, { error: 'intel_unavailable' })
          : response(422, { error: 'odd_new_code' }),
  });
  await assert.rejects(api.get('/intel/entity/trk:x'), (err) => {
    assert.ok(err instanceof HttpError);
    assert.equal(err.code, 'unknown_entity');
    assert.equal(err.message, "it isn't in the intel picture");
    return true;
  });
  await assert.rejects(
    api.get('/intel/graph'),
    (err) => err.message === "the intel service didn't start in this app",
  );
  await assert.rejects(
    api.get('/other'),
    (err) => err.code === 'odd_new_code' && err.message === 'odd new code',
  );
  const said = createApi({
    fetchImpl: async () =>
      response(503, { error: 'unavailable', message: 'The CLI is missing.' }),
  });
  await assert.rejects(
    said.get('/x'),
    (err) => err.message === 'The CLI is missing.',
  );
});

test('a network failure is an OfflineError that keeps the cause', async () => {
  const api = createApi({
    fetchImpl: async () => {
      throw new TypeError('Failed to fetch');
    },
  });
  await assert.rejects(api.get('/intel/graph'), (err) => {
    assert.ok(err instanceof OfflineError);
    assert.equal(err.cause.message, 'Failed to fetch');
    return true;
  });
  const none = createApi({ fetchImpl: null });
  const saved = globalThis.fetch;
  globalThis.fetch = undefined;
  try {
    await assert.rejects(none.get('/x'), OfflineError);
  } finally {
    globalThis.fetch = saved;
  }
});

test('no answer in time is a TimeoutError and aborts the request', async () => {
  const timers = manualTimers();
  let signal = null;
  const api = createApi({
    timers,
    timeoutMs: 1234,
    fetchImpl: (url, init) => {
      signal = init.signal;
      return new Promise(() => {});
    },
  });
  const pending = api.get('/intel/graph');
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(timers.runAll(), [1234]);
  await assert.rejects(
    pending,
    (err) => err instanceof TimeoutError && err.timeoutMs === 1234,
  );
  assert.equal(signal.aborted, true);
});

test('a per-call timeout overrides the default, and settles clear their timer', async () => {
  const timers = manualTimers();
  const api = createApi({ timers, fetchImpl: async () => response(200, {}) });
  await api.get('/x', { timeoutMs: 50 });
  assert.equal(timers.pending.size, 0);
});

test('abortVehicle uses the existing command route, never the chat', async () => {
  const { calls, fetchImpl } = recordingFetch(
    response(200, { aborted: true, cancelled: 1, task_id: 't-1' }),
  );
  const api = createApi({ base: 'http://h', token: 't', fetchImpl });
  const out = await api.abortVehicle(' Drone1 ');
  assert.deepEqual(out, { aborted: true, cancelled: 1, task_id: 't-1' });
  assert.equal(calls[0].url, 'http://h/control/command');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    tool: 'uav_abort',
    vehicle: 'Drone1',
    arguments: { vehicle: 'Drone1' },
  });
  await assert.rejects(api.abortVehicle(''), TypeError);
});

test('abortVehicle passes a refusal through unchanged for the caller to word', async () => {
  const refusal = {
    aborted: false,
    refused: true,
    reason: 'BINGO return in progress',
  };
  const api = createApi({ fetchImpl: async () => response(200, refusal) });
  assert.deepEqual(await api.abortVehicle('Drone1'), refusal);
});

test('formatZulu and joinUrl', () => {
  const ms = Date.UTC(2026, 8, 27, 14, 2, 51);
  assert.equal(formatZulu(ms), '14:02Z');
  assert.equal(formatZulu(ms, { seconds: true }), '14:02:51Z');
  assert.equal(formatZulu(null), '');
  assert.equal(formatZulu('x'), '');
  assert.equal(joinUrl('http://a/', '/b'), 'http://a/b');
  assert.equal(joinUrl('', 'b'), '/b');
  assert.equal(joinUrl('http://a', 'https://c/d'), 'https://c/d');
});

// ---- SSE ------------------------------------------------------------------------

function fakeEventSourceClass() {
  const made = [];
  class FakeES {
    static CLOSED = 2;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = {};
      this.closed = false;
      made.push(this);
    }
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    }
    close() {
      this.closed = true;
      this.readyState = 2;
    }
    open() {
      this.readyState = 1;
      this.onopen?.({});
    }
    send(name, data, id) {
      const ev = {
        data: typeof data === 'string' ? data : JSON.stringify(data),
        lastEventId: id ?? '',
      };
      if (name === 'error') {
        this.onerror?.(ev);
        return;
      }
      for (const fn of this.listeners[name] || []) fn(ev);
    }
    fail(readyState) {
      this.readyState = readyState;
      this.onerror?.({ type: 'error' });
    }
  }
  return { FakeES, made };
}

test('sse puts the token in the query and delivers named events with ids', () => {
  const { FakeES, made } = fakeEventSourceClass();
  const timers = manualTimers();
  const api = createApi({
    base: 'http://h',
    token: 'tok en',
    EventSourceImpl: FakeES,
    timers,
  });
  const got = [];
  const opens = [];
  const sub = api.sse('/chat/sessions/s1/stream', {
    events: ['session', 'text_delta', 'error'],
    onEvent: (name, data, id) => got.push([name, data, id]),
    onOpen: (info) => opens.push(info),
  });
  assert.equal(made.length, 1);
  assert.equal(made[0].url, 'http://h/chat/sessions/s1/stream?token=tok%20en');
  made[0].open();
  made[0].send('session', { session_id: 's1' }, '');
  made[0].send('text_delta', { text: 'Hi' }, '7');
  made[0].send('error', { message: 'rate limited', retryable: true }, '8');
  made[0].send('usage', { cost_usd: 1 }, '9'); // not subscribed
  assert.deepEqual(got, [
    ['session', { session_id: 's1' }, null],
    ['text_delta', { text: 'Hi' }, '7'],
    ['error', { message: 'rate limited', retryable: true }, '8'],
  ]);
  assert.equal(sub.lastEventId, '8');
  assert.deepEqual(opens, [{ reconnected: false, lastEventId: null }]);
  sub.close();
  assert.equal(made[0].closed, true);
});

test('sse reopens a stream the browser gave up on, replaying from the last id', () => {
  const { FakeES, made } = fakeEventSourceClass();
  const timers = manualTimers();
  const api = createApi({
    base: '',
    token: 't',
    EventSourceImpl: FakeES,
    timers,
  });
  const errors = [];
  const opens = [];
  const got = [];
  const sub = api.sse('/chat/sessions/s1/stream', {
    events: ['text_delta'],
    lastEventId: 3,
    onEvent: (name, data, id) => got.push(id),
    onError: (info) => errors.push(info),
    onOpen: (info) => opens.push(info),
  });
  assert.equal(made[0].url, '/chat/sessions/s1/stream?token=t&last_event_id=3');
  made[0].open();
  made[0].send('text_delta', { text: 'a' }, '12');

  // The browser is retrying on its own: nothing to do but report it.
  made[0].fail(0);
  assert.equal(made.length, 1);
  assert.equal(errors.at(-1).native, true);

  // The browser gave up (CLOSED): reopen after the first backoff step.
  made[0].fail(2);
  assert.equal(errors.at(-1).delayMs, SSE_RETRY_MS[0]);
  assert.deepEqual(
    [...timers.pending.values()].map((t) => t.ms),
    [SSE_RETRY_MS[0]],
  );
  timers.runAll();
  assert.equal(made.length, 2);
  assert.equal(
    made[1].url,
    '/chat/sessions/s1/stream?token=t&last_event_id=12',
  );
  // Events from the dead source are ignored.
  made[0].send('text_delta', { text: 'stale' }, '99');
  made[1].open();
  assert.deepEqual(opens.at(-1), { reconnected: true, lastEventId: '12' });
  made[1].send('text_delta', { text: 'b' }, '13');
  assert.deepEqual(got, ['12', '13']);

  // Backoff grows while it keeps failing, and resets after an open.
  made[1].fail(2);
  timers.runAll();
  made[2].fail(2);
  assert.equal(errors.at(-1).delayMs, SSE_RETRY_MS[1]);
  sub.close();
  assert.equal(timers.pending.size, 0);
  assert.equal(made.length, 3);
});

test('sse without an EventSource reports it and returns a closable handle', () => {
  const saved = globalThis.EventSource;
  globalThis.EventSource = undefined;
  try {
    const api = createApi({});
    const errors = [];
    const sub = api.sse('/events', { onError: (e) => errors.push(e) });
    assert.equal(errors[0].unsupported, true);
    sub.close();
  } finally {
    globalThis.EventSource = saved;
  }
});

test('sse reconnect() reopens at once with the last id', () => {
  const { FakeES, made } = fakeEventSourceClass();
  const api = createApi({
    base: '',
    token: '',
    EventSourceImpl: FakeES,
    timers: manualTimers(),
  });
  const sub = api.sse('/events', { events: ['alarm'], onEvent: () => {} });
  assert.equal(made[0].url, '/events');
  made[0].open();
  made[0].send('alarm', { kind: 'bingo' }, '5');
  sub.reconnect();
  assert.equal(made[0].closed, true);
  assert.equal(made[1].url, '/events?last_event_id=5');
  sub.close();
});

// ---- bidi controls in host strings ----------------------------------------------

const RLO = '\u202E';
const PDF = '\u202C';

test('stripBidi/showBidi remove or expose explicit bidi controls only', () => {
  assert.equal(
    stripBidi(`BMP ${RLO}YLDNEIRF${PDF} convoy`),
    'BMP YLDNEIRF convoy',
  );
  assert.equal(
    stripBidi('\u2066a\u2067b\u2068c\u2069\u202A\u202B\u202D'),
    'abc',
  );
  assert.equal(
    stripBidi('שלום abc'),
    'שלום abc',
    'letters keep their direction',
  );
  assert.equal(stripBidi(7), 7);
  assert.equal(showBidi(`Drone1${RLO}`), 'Drone1<U+202E>');
});

test('graph and REST bodies arrive with bidi controls removed (labels, subtitles, attrs, keys)', async () => {
  const graph = {
    nodes: [
      {
        id: 'trk:T1',
        label: `BMP ${RLO}YLDNEIRF${PDF} convoy`,
        subtitle: `BMP ${RLO}YLDNEIRF${PDF} convoy · possible`,
        attrs: { detected_as: `x${RLO}y`, [`k${RLO}`]: 1, list: [`a${RLO}`] },
      },
    ],
  };
  const { fetchImpl } = recordingFetch(response(200, graph));
  const api = createApi({ base: 'http://h', fetchImpl });
  const out = await api.get('/intel/graph');
  const node = out.nodes[0];
  assert.equal(node.label, 'BMP YLDNEIRF convoy');
  assert.equal(node.subtitle, 'BMP YLDNEIRF convoy · possible');
  assert.equal(node.attrs.detected_as, 'xy');
  assert.deepEqual(node.attrs.list, ['a']);
  assert.equal(node.attrs.k, 1);
  assert.equal(
    JSON.stringify(out).includes(RLO),
    false,
    'no control survives anywhere',
  );
});

test('SSE events arrive with bidi controls removed; tool args keep a visible token', () => {
  const { FakeES, made } = fakeEventSourceClass();
  const api = createApi({
    base: 'http://h',
    EventSourceImpl: FakeES,
    timers: manualTimers(),
  });
  const got = [];
  api.sse('/chat/sessions/s1/stream', {
    events: ['approval_request', 'ui'],
    onEvent: (name, data) => got.push([name, data]),
  });
  made[0].open();
  made[0].send(
    'approval_request',
    {
      title: `Orbit ${RLO}ynneD`,
      summary: `Drone1 ${RLO}evil`,
      consequences: [`Drone1 flies ${RLO}x`],
      args: { vehicle: `Drone1${RLO}`, params: { note: `n${RLO}` } },
    },
    '1',
  );
  made[0].send('ui', { kind: 'ui.track', reason: `watch ${RLO}evil` }, '2');
  const [[, approval], [, ui]] = got;
  assert.equal(approval.title, 'Orbit ynneD');
  assert.equal(approval.summary, 'Drone1 evil');
  assert.deepEqual(approval.consequences, ['Drone1 flies x']);
  assert.deepEqual(approval.args, {
    vehicle: 'Drone1<U+202E>',
    params: { note: 'n<U+202E>' },
  });
  assert.equal(ui.reason, 'watch evil');
  assert.equal(neutralizeBidi(null), null);
});
