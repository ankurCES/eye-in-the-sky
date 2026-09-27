import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CHAT_EVENTS,
  ChatError,
  SESSION_KEY,
  bodyOf,
  chatError,
  createChatClient,
  statusOf,
} from './client.js';

class HttpError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.name = 'HttpError';
    this.status = status;
    this.body = body;
  }
}

function memoryStorage(seed = {}) {
  const data = new Map(Object.entries(seed));
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
}

/** A fake api: scripted responses per "METHOD path", recorded calls, fake SSE. */
function fakeApi(routes = {}) {
  const calls = [];
  const streams = [];
  const respond = async (method, path, body) => {
    calls.push({ method, path, body });
    const handler = routes[`${method} ${path}`] ?? routes[`${method} *`];
    if (typeof handler === 'function') return handler(body, calls);
    if (handler instanceof Error) throw handler;
    if (handler === undefined)
      throw new HttpError(404, { detail: 'Not Found' });
    return handler;
  };
  return {
    calls,
    streams,
    get: (path) => respond('GET', path),
    post: (path, body) => respond('POST', path, body),
    del: (path) => respond('DELETE', path),
    sse(path, handlers) {
      const s = {
        path,
        handlers,
        closed: false,
        close() {
          this.closed = true;
        },
      };
      streams.push(s);
      return s;
    },
  };
}

test('open() creates a session, stores it, and follows its stream', async () => {
  const storage = memoryStorage();
  const api = fakeApi({ 'POST /chat/sessions': { session_id: 'abc' } });
  const client = createChatClient({ api, storage, now: () => 1000 });
  const sid = await client.open();
  assert.equal(sid, 'abc');
  assert.equal(storage.getItem(SESSION_KEY), 'abc');
  assert.equal(api.streams.length, 1);
  const s = api.streams[0];
  assert.equal(s.path, '/chat/sessions/abc/stream');
  assert.deepEqual(s.handlers.events, [...CHAT_EVENTS]);
  assert.equal(
    s.handlers.lastEventId,
    undefined,
    'a fresh page replays the whole log',
  );
  assert.equal(client.connection, 'connecting');
});

test('a stored session is reused; replays are flagged and keep stored times', async () => {
  const storage = memoryStorage({
    [SESSION_KEY]: 'keep',
    'ic.chat.times.keep': JSON.stringify([[1, 500]]),
  });
  const api = fakeApi();
  let t = 9000;
  const client = createChatClient({ api, storage, now: () => t });
  const seen = [];
  client.on('event', (e) => seen.push(e));
  const states = [];
  client.on('state', (s) => states.push(s.connection));
  await client.open();
  assert.equal(api.calls.filter((c) => c.path === '/chat/sessions').length, 0);
  const { onEvent, onOpen } = api.streams[0].handlers;
  onOpen();
  onEvent('session', JSON.stringify({ session_id: 'keep', last_seq: 2 }), '');
  onEvent('turn_start', '{"turn_id":"t1","text":"x"}', '1');
  onEvent('text_delta', { turn_id: 't1', text: 'y' }, '2');
  t = 9500;
  onEvent('text_delta', { turn_id: 't1', text: 'z' }, '3');
  onEvent('text_delta', { turn_id: 't1', text: 'z' }, '3'); // duplicate id
  assert.deepEqual(
    seen.map((e) => [e.name, e.seq, e.replay, e.at]),
    [
      ['session', 0, false, 9000],
      ['turn_start', 1, true, 500],
      ['text_delta', 2, true, null],
      ['text_delta', 3, false, 9500],
    ],
  );
  assert.equal(seen[1].data.text, 'x', 'string data is parsed');
  assert.equal(client.lastSeq, 3);
  assert.ok(states.includes('open'));
});

test('named listeners receive the data and meta', async () => {
  const api = fakeApi({ 'POST /chat/sessions': { session_id: 's' } });
  const client = createChatClient({
    api,
    storage: memoryStorage(),
    now: () => 5,
  });
  const got = [];
  const off = client.on('ui', (data, meta) =>
    got.push([data.action, meta.seq]),
  );
  await client.open();
  api.streams[0].handlers.onEvent('ui', { action: 'focus', ids: [] }, '4');
  off();
  api.streams[0].handlers.onEvent('ui', { action: 'orb' }, '5');
  assert.deepEqual(got, [['focus', 4]]);
});

test('send posts text and focused ids; errors are typed', async () => {
  const api = fakeApi({
    'POST /chat/sessions': { session_id: 's1' },
    'POST /chat/sessions/s1/messages': (body) => ({
      turn_id: 'turn-1',
      echo: body,
    }),
  });
  const client = createChatClient({ api, storage: memoryStorage() });
  const res = await client.send('  Which contacts?  ', {
    focused_ids: ['trk:T-1', 7, 'veh:Drone1'],
  });
  assert.equal(res.turn_id, 'turn-1');
  assert.deepEqual(res.echo, {
    text: 'Which contacts?',
    context: { focused_ids: ['trk:T-1', 'veh:Drone1'] },
  });
  await assert.rejects(
    client.send('   '),
    (e) => e instanceof ChatError && e.code === 'invalid',
  );
  await assert.rejects(
    client.send('x'.repeat(8001)),
    (e) => e.code === 'invalid',
  );
});

test('send maps 409 to busy and 503 to unavailable with the reason', async () => {
  const api = fakeApi({
    'POST /chat/sessions': { session_id: 's1' },
    'POST /chat/sessions/s1/messages': new HttpError(409, {
      error: 'busy',
      turn_id: 't0',
    }),
  });
  const client = createChatClient({ api, storage: memoryStorage() });
  await assert.rejects(
    client.send('hi'),
    (e) => e.code === 'busy' && e.turnId === 't0',
  );
  const api2 = fakeApi({
    'POST /chat/sessions': { session_id: 's2' },
    'POST /chat/sessions/s2/messages': new HttpError(503, {
      error: 'unavailable',
      reason: 'cli_missing',
      hint: 'install',
    }),
  });
  const client2 = createChatClient({ api: api2, storage: memoryStorage() });
  await assert.rejects(
    client2.send('hi'),
    (e) =>
      e.code === 'unavailable' &&
      e.reason === 'cli_missing' &&
      e.hint === 'install',
  );
});

test('an unknown session on send starts a new one and retries once', async () => {
  const storage = memoryStorage({ [SESSION_KEY]: 'gone' });
  const api = fakeApi({
    'POST /chat/sessions/gone/messages': new HttpError(404, {
      error: 'unknown_session',
    }),
    'POST /chat/sessions': { session_id: 'fresh' },
    'POST /chat/sessions/fresh/messages': { turn_id: 'ok' },
  });
  const client = createChatClient({ api, storage });
  const replaced = [];
  client.on('session:replaced', (e) => replaced.push(e));
  const res = await client.send('hello');
  assert.equal(res.turn_id, 'ok');
  assert.deepEqual(replaced, [{ reason: 'lost', from: 'gone', to: 'fresh' }]);
  assert.equal(storage.getItem(SESSION_KEY), 'fresh');
});

test('approve posts the decision and a trimmed note; bad decisions throw', async () => {
  const api = fakeApi({
    'POST /chat/sessions': { session_id: 's' },
    'POST *': (body) => ({ ok: true, body }),
  });
  const client = createChatClient({ api, storage: memoryStorage() });
  await client.open();
  const res = await client.approve('ap/1', 'deny', '  Use 80 m instead.  ');
  assert.deepEqual(res.body, { decision: 'deny', note: 'Use 80 m instead.' });
  assert.equal(api.calls.at(-1).path, '/chat/sessions/s/approvals/ap%2F1');
  const plain = await client.approve('a2', 'approve_session');
  assert.deepEqual(plain.body, { decision: 'approve_session' });
  await assert.rejects(
    client.approve('a3', 'yes'),
    (e) => e.code === 'invalid',
  );
});

test('approve without a session is refused, not sent', async () => {
  const api = fakeApi();
  const client = createChatClient({ api, storage: memoryStorage() });
  await assert.rejects(
    client.approve('a', 'approve'),
    (e) => e.code === 'unknown_session',
  );
  assert.equal(api.calls.length, 0);
});

test('interrupt announces itself, then posts', async () => {
  const api = fakeApi({
    'POST /chat/sessions': { session_id: 's' },
    'POST /chat/sessions/s/interrupt': { ok: true },
  });
  const client = createChatClient({
    api,
    storage: memoryStorage(),
    now: () => 777,
  });
  const seen = [];
  client.on('interrupt', (e) => seen.push(e));
  assert.deepEqual(
    await client.interrupt(),
    { ok: false },
    'no session: nothing to stop',
  );
  await client.open();
  await client.interrupt();
  assert.deepEqual(seen, [{ at: 777 }]);
  assert.equal(api.calls.at(-1).path, '/chat/sessions/s/interrupt');
});

test('grants: a list when the route exists, null when it does not', async () => {
  const api = fakeApi({
    'POST /chat/sessions': { session_id: 's' },
    'GET /chat/sessions/s/grants': {
      grants: [{ tool: 'uav_scan_targets', since_ms: 5 }],
    },
    'DELETE /chat/sessions/s/grants/uav_scan_targets': { ok: true },
  });
  const client = createChatClient({ api, storage: memoryStorage() });
  assert.equal(await client.grants(), null, 'no session yet');
  await client.open();
  assert.deepEqual(await client.grants(), [
    { tool: 'uav_scan_targets', since_ms: 5 },
  ]);
  assert.deepEqual(await client.revokeGrant('uav_scan_targets'), { ok: true });
  const v1 = fakeApi({ 'POST /chat/sessions': { session_id: 'v' } });
  const old = createChatClient({ api: v1, storage: memoryStorage() });
  await old.open();
  assert.equal(await old.grants(), null, 'v1 server: 404 Not Found');
});

test('status: service failures are transient, a rejected token is not', async () => {
  const ok = fakeApi({ 'GET /chat/status': { available: true, model: 'm' } });
  assert.deepEqual(
    await createChatClient({ api: ok, storage: null }).status(),
    {
      available: true,
      model: 'm',
    },
  );
  const down = fakeApi({
    'GET /chat/status': Object.assign(new Error('fetch failed'), {
      name: 'OfflineError',
    }),
  });
  const st = await createChatClient({ api: down, storage: null }).status();
  assert.equal(st.available, false);
  assert.equal(st.transient, true);
  const auth = fakeApi({
    'GET /chat/status': new HttpError(401, { detail: 'unauthorized' }),
  });
  const at = await createChatClient({ api: auth, storage: null }).status();
  assert.equal(at.reason, 'token');
  assert.equal(at.transient, false);
  // The host answered 503 with its JSON: the analyst isn't running in this
  // app. That is a reason, not a blip to wait out.
  const off = fakeApi({
    'GET /chat/status': new HttpError(503, { error: 'chat_unavailable' }),
  });
  const ot = await createChatClient({ api: off, storage: null }).status();
  assert.equal(ot.available, false);
  assert.equal(ot.reason, 'service_unavailable');
  assert.equal(ot.transient, false);
  const why = fakeApi({
    'GET /chat/status': new HttpError(503, {
      error: 'unavailable',
      reason: 'cli_missing',
      hint: 'install the CLI',
    }),
  });
  const wt = await createChatClient({ api: why, storage: null }).status();
  assert.equal(wt.reason, 'cli_missing');
  assert.equal(wt.hint, 'install the CLI');
  assert.equal(wt.transient, false);
});

test('a stream that fails before any session event: a gone session is replaced', async () => {
  const storage = memoryStorage({ [SESSION_KEY]: 'old' });
  const api = fakeApi({
    'GET /chat/status': { available: true },
    'GET /chat/sessions/old/grants': new HttpError(404, {
      error: 'unknown_session',
    }),
    'POST /chat/sessions': { session_id: 'new' },
  });
  const client = createChatClient({ api, storage });
  const replaced = [];
  client.on('session:replaced', (e) => replaced.push(e));
  await client.open();
  const first = api.streams[0];
  first.handlers.onError();
  assert.equal(client.connection, 'reconnecting');
  first.handlers.onError();
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(replaced, [{ reason: 'lost', from: 'old', to: 'new' }]);
  assert.equal(first.closed, true);
  assert.equal(api.streams.length, 2);
  assert.equal(api.streams[1].path, '/chat/sessions/new/stream');
});

test('while the service is down the session is kept', async () => {
  const storage = memoryStorage({ [SESSION_KEY]: 'keep' });
  const api = fakeApi({
    'GET /chat/status': Object.assign(new Error('down'), {
      name: 'OfflineError',
    }),
  });
  const client = createChatClient({ api, storage });
  await client.open();
  api.streams[0].handlers.onError();
  api.streams[0].handlers.onError();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(client.sessionId, 'keep');
  assert.equal(api.streams.length, 1);
});

test('newSession deletes the old one, starts fresh and says so', async () => {
  const storage = memoryStorage({ [SESSION_KEY]: 'a' });
  const api = fakeApi({
    'DELETE /chat/sessions/a': { ok: true },
    'POST /chat/sessions': { session_id: 'b' },
  });
  const client = createChatClient({ api, storage });
  const replaced = [];
  client.on('session:replaced', (e) => replaced.push(e));
  await client.open();
  await client.newSession();
  assert.deepEqual(replaced, [{ reason: 'operator', from: 'a', to: 'b' }]);
  assert.ok(
    api.calls.some(
      (c) => c.method === 'DELETE' && c.path === '/chat/sessions/a',
    ),
  );
  assert.equal(api.streams.at(-1).path, '/chat/sessions/b/stream');
});

test('close stops the stream and ignores late events', async () => {
  const api = fakeApi({ 'POST /chat/sessions': { session_id: 's' } });
  const client = createChatClient({ api, storage: memoryStorage() });
  const seen = [];
  client.on('event', (e) => seen.push(e));
  await client.open();
  client.close();
  assert.equal(api.streams[0].closed, true);
  api.streams[0].handlers.onEvent('text_delta', { text: 'late' }, '9');
  assert.equal(seen.length, 0);
  assert.equal(client.connection, 'closed');
});

test('a throwing listener never stops the others', async () => {
  const api = fakeApi({ 'POST /chat/sessions': { session_id: 's' } });
  const client = createChatClient({ api, storage: memoryStorage() });
  const prev = globalThis.console.error;
  globalThis.console.error = () => {};
  try {
    const seen = [];
    client.on('event', () => {
      throw new Error('boom');
    });
    client.on('event', (e) => seen.push(e.name));
    await client.open();
    api.streams[0].handlers.onEvent('usage', {}, '1');
    assert.deepEqual(seen, ['usage']);
  } finally {
    globalThis.console.error = prev;
  }
});

test('error helpers read status and body in any shape', () => {
  assert.equal(statusOf({ status: 404 }), 404);
  assert.equal(statusOf({ response: { status: 500 } }), 500);
  assert.equal(statusOf({}), null);
  assert.deepEqual(bodyOf({ body: '{"error":"x"}' }), { error: 'x' });
  assert.deepEqual(bodyOf({ data: { a: 1 } }), { a: 1 });
  assert.equal(bodyOf({ body: 'not json' }), null);
  assert.equal(chatError({ name: 'AuthError', message: 'no' }).code, 'auth');
  assert.equal(chatError({ name: 'TimeoutError' }).code, 'timeout');
  assert.equal(
    chatError(new HttpError(422, { message: 'bad' })).message,
    'bad',
  );
  assert.equal(
    chatError(new HttpError(404, { error: 'unknown_session' })).code,
    'unknown_session',
  );
  assert.equal(chatError(new HttpError(500, {})).code, 'http');
  assert.equal(chatError(new TypeError('Failed to fetch')).code, 'offline');
  const e = new ChatError('busy', 'x');
  assert.equal(chatError(e), e);
  assert.throws(() => createChatClient({}), /needs an api/);
});
