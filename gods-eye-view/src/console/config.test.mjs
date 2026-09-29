import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  CONSOLE_KEY_COPY,
  CONSOLE_KEY_HEADER,
  CONSOLE_KEY_STATES,
  CONSOLE_KEY_STORAGE,
  DEFAULT_BRIDGE_BASE,
  DEFAULT_BRIDGE_TOKEN,
  STORAGE_KEYS,
  createConsoleKey,
  isConsoleKey,
  resolveBridge,
  sameOrigin,
} from './config.js';
import { HttpError, OfflineError, TimeoutError } from './api.js';

const APP = {
  origin: 'http://127.0.0.1:8780',
  protocol: 'http:',
  port: '8780',
};
const VITE = {
  origin: 'http://localhost:4173',
  protocol: 'http:',
  port: '4173',
};

function storage(values = {}) {
  return { getItem: (key) => (key in values ? values[key] : null) };
}

test('the host-injected config wins over storage and env', () => {
  const out = resolveBridge({
    globalConfig: { bridgeUrl: 'http://127.0.0.1:8780/', token: 'injected' },
    storage: storage({
      [STORAGE_KEYS.base]: 'http://x:1',
      [STORAGE_KEYS.token]: 's',
    }),
    env: { VITE_UAV_BRIDGE_URL: 'http://env:2', VITE_UAV_BRIDGE_TOKEN: 'e' },
    location: VITE,
  });
  assert.deepEqual(out, {
    base: 'http://127.0.0.1:8780',
    token: 'injected',
    source: 'injected',
    tokenSource: 'injected',
  });
});

test('an injected empty bridgeUrl means the page origin', () => {
  const out = resolveBridge({
    globalConfig: { bridgeUrl: '', token: 't' },
    storage: storage({ [STORAGE_KEYS.base]: 'http://stale:8790' }),
    env: {},
    location: APP,
  });
  assert.equal(out.base, 'http://127.0.0.1:8780');
  assert.equal(out.source, 'injected');
  // No location (node): relative, same origin.
  assert.equal(
    resolveBridge({ globalConfig: { bridgeUrl: '' }, env: {}, location: null })
      .base,
    '',
  );
});

test('storage beats env, env beats the defaults, per field', () => {
  const both = resolveBridge({
    globalConfig: null,
    storage: storage({ [STORAGE_KEYS.base]: ' http://127.0.0.1:52100 ' }),
    env: {
      VITE_UAV_BRIDGE_URL: 'http://env:2',
      VITE_UAV_BRIDGE_TOKEN: 'env-token',
    },
    location: VITE,
  });
  assert.equal(both.base, 'http://127.0.0.1:52100');
  assert.equal(both.source, 'storage');
  assert.equal(both.token, 'env-token');
  assert.equal(both.tokenSource, 'env');

  const env = resolveBridge({
    globalConfig: undefined,
    storage: null,
    env: { VITE_UAV_BRIDGE_URL: 'http://env:2/' },
    location: VITE,
  });
  assert.equal(env.base, 'http://env:2');
  assert.equal(env.source, 'env');
  assert.equal(env.token, DEFAULT_BRIDGE_TOKEN);
  assert.equal(env.tokenSource, 'default');
});

test('defaults: the dev server points at the bridge port, the app host at itself', () => {
  const dev = resolveBridge({ globalConfig: null, env: {}, location: VITE });
  assert.equal(dev.base, DEFAULT_BRIDGE_BASE);
  assert.equal(dev.source, 'default');
  const app = resolveBridge({ globalConfig: null, env: {}, location: APP });
  assert.equal(app.base, 'http://127.0.0.1:8780');
  const file = resolveBridge({
    globalConfig: null,
    env: {},
    location: { origin: 'null', protocol: 'file:', port: '' },
  });
  assert.equal(file.base, DEFAULT_BRIDGE_BASE);
  assert.equal(
    resolveBridge({ globalConfig: null, env: {}, location: undefined }).base,
    DEFAULT_BRIDGE_BASE,
  );
});

test('a storage that throws (private window) is skipped, never fatal', () => {
  const out = resolveBridge({
    globalConfig: null,
    storage: {
      getItem() {
        throw new Error('SecurityError');
      },
    },
    env: {},
    location: VITE,
  });
  assert.equal(out.base, DEFAULT_BRIDGE_BASE);
  assert.equal(out.token, DEFAULT_BRIDGE_TOKEN);
});

test('an injected token without a bridgeUrl still wins the token', () => {
  const out = resolveBridge({
    globalConfig: { token: 'only-token' },
    storage: storage({ [STORAGE_KEYS.base]: 'http://s:1' }),
    env: {},
    location: VITE,
  });
  assert.equal(out.base, 'http://s:1');
  assert.equal(out.token, 'only-token');
});

test('sameOrigin refuses non-http origins', () => {
  assert.equal(sameOrigin(APP), 'http://127.0.0.1:8780');
  assert.equal(sameOrigin({ origin: 'null', protocol: 'file:' }), '');
  assert.equal(sameOrigin({ origin: 'app://x', protocol: 'app:' }), '');
  assert.equal(sameOrigin(null), '');
});

test('config.js names no browser global, so portable sources may import it', () => {
  const source = readFileSync(new URL('./config.js', import.meta.url), 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  for (const name of [
    'window',
    'document',
    'localStorage',
    'sessionStorage',
    'navigator',
  ]) {
    assert.doesNotMatch(
      code,
      new RegExp(`\\b${name}\\b`),
      `${name} appears in config.js`,
    );
  }
});

// ---- engagement approval key (WG §3.5) ----------------------------------------------

const KEY = 'k3Y_abcdefghijklmnopqrstuvwxyz012';

function sessionStore(values = {}, { throwOnSet = false } = {}) {
  const data = { ...values };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem(k, v) {
      if (throwOnSet) throw new Error('QuotaExceededError');
      data[k] = String(v);
    },
    removeItem(k) {
      delete data[k];
    },
  };
}

function claimer(...answers) {
  const calls = [];
  const claim = () => {
    calls.push(1);
    const next = answers.length > 1 ? answers.shift() : answers[0];
    return typeof next === 'function' ? next() : next;
  };
  return { calls, claim };
}

test('the key holder states and copy are the spec words', () => {
  assert.equal(CONSOLE_KEY_STORAGE, 'godseye.consoleKey');
  assert.equal(CONSOLE_KEY_HEADER, 'X-Godseye-Console');
  assert.deepEqual(CONSOLE_KEY_STATES, [
    'idle',
    'claiming',
    'held',
    'refused',
    'unsupported',
    'failed',
  ]);
  assert.equal(
    CONSOLE_KEY_COPY.banner,
    'Another client claimed engagement approvals; restart to re-arm',
  );
  assert.equal(
    CONSOLE_KEY_COPY.denyOnly,
    "This console can't approve engagements: another client claimed them.",
  );
  assert.ok(isConsoleKey(KEY));
  assert.ok(!isConsoleKey('short'));
  assert.ok(!isConsoleKey(`${KEY}\r\nX-Evil: 1`), 'never a header injection');
  assert.ok(!isConsoleKey(42));
});

test('a key this tab already holds is used without a claim (a reload keeps it)', async () => {
  const storage = sessionStore({ [CONSOLE_KEY_STORAGE]: KEY });
  const { calls, claim } = claimer(Promise.resolve({ console_key: 'other' }));
  const holder = createConsoleKey({ storage, claim });
  assert.equal(holder.state, 'held');
  assert.equal(await holder.ensure(), 'held');
  assert.equal(calls.length, 0);
  assert.equal(holder.key(), KEY);
  assert.deepEqual(holder.headers(), { 'X-Godseye-Console': KEY });
  assert.equal(holder.canApprove(), true);
  assert.equal(holder.denyOnlyLine(), null);
  assert.equal(holder.bannerText(), null);
});

test('200: the first claim stores the key in sessionStorage, once', async () => {
  const storage = sessionStore();
  const { calls, claim } = claimer(Promise.resolve({ console_key: KEY }));
  const holder = createConsoleKey({ storage, claim });
  const seen = [];
  holder.onChange((info) => seen.push(info));
  assert.equal(holder.state, 'idle');
  assert.deepEqual(holder.headers(), {});
  // Concurrent callers share one POST.
  const [a, b] = await Promise.all([holder.ensure(), holder.ensure()]);
  assert.equal(a, 'held');
  assert.equal(b, 'held');
  assert.equal(calls.length, 1);
  assert.equal(holder.attempts, 1);
  assert.equal(storage.data[CONSOLE_KEY_STORAGE], KEY);
  assert.deepEqual(holder.headers(), { 'X-Godseye-Console': KEY });
  // Listeners hear the state, never the key.
  assert.deepEqual(seen, [
    { state: 'claiming', canApprove: false },
    { state: 'held', canApprove: true },
  ]);
  assert.doesNotMatch(JSON.stringify(seen), new RegExp(KEY));
  await holder.ensure();
  assert.equal(calls.length, 1, 'held: no second claim');
});

test('409: refused for good, the banner and the Deny-only line', async () => {
  const storage = sessionStore();
  const { calls, claim } = claimer(() =>
    Promise.reject(
      new HttpError('another client already claimed engagement approvals', {
        status: 409,
        body: { error: 'console_already_claimed' },
      }),
    ),
  );
  const holder = createConsoleKey({ storage, claim });
  assert.equal(await holder.ensure(), 'refused');
  assert.equal(holder.refused, true);
  assert.equal(holder.canApprove(), false);
  assert.equal(holder.key(), null);
  assert.deepEqual(holder.headers(), {});
  assert.equal(holder.bannerText(), CONSOLE_KEY_COPY.banner);
  assert.equal(holder.denyOnlyLine(), CONSOLE_KEY_COPY.denyOnly);
  assert.equal(storage.data[CONSOLE_KEY_STORAGE], undefined);
  await holder.ensure();
  await holder.invalidate();
  assert.equal(calls.length, 1, 'never claims again before a reload');
  assert.equal(holder.state, 'refused');
});

test('no answer: failed, and a later ensure claims again', async () => {
  const { calls, claim } = claimer(
    () => Promise.reject(new OfflineError()),
    () => Promise.reject(new TimeoutError()),
    () => Promise.resolve({ console_key: KEY }),
  );
  const holder = createConsoleKey({ storage: sessionStore(), claim });
  assert.equal(await holder.ensure(), 'failed');
  assert.equal(holder.denyOnlyLine(), CONSOLE_KEY_COPY.missing);
  assert.equal(holder.bannerText(), null, 'no banner for a transient failure');
  assert.equal(await holder.ensure(), 'failed');
  assert.equal(await holder.ensure(), 'held');
  assert.equal(calls.length, 3);
});

test('a host without the route, or a 2xx without a key, is unsupported (no retry)', async () => {
  const missing = claimer(() =>
    Promise.reject(new HttpError('not found', { status: 404 })),
  );
  const a = createConsoleKey({ storage: sessionStore(), claim: missing.claim });
  assert.equal(await a.ensure(), 'unsupported');
  await a.ensure();
  assert.equal(missing.calls.length, 1);
  assert.equal(a.denyOnlyLine(), CONSOLE_KEY_COPY.missing);

  const empty = claimer(Promise.resolve({}));
  const b = createConsoleKey({ storage: sessionStore(), claim: empty.claim });
  assert.equal(await b.ensure(), 'unsupported');
  const bad = claimer(Promise.resolve({ console_key: 'x\ny' }));
  const c = createConsoleKey({ storage: sessionStore(), claim: bad.claim });
  assert.equal(await c.ensure(), 'unsupported');
  assert.equal(c.key(), null);

  const none = createConsoleKey({ storage: sessionStore() });
  assert.equal(await none.ensure(), 'unsupported');
});

test('a storage that throws keeps the key in memory for this page', async () => {
  const storage = sessionStore({}, { throwOnSet: true });
  const { claim } = claimer(Promise.resolve({ console_key: KEY }));
  const holder = createConsoleKey({ storage, claim });
  assert.equal(await holder.ensure(), 'held');
  assert.equal(holder.key(), KEY);
  const broken = {
    getItem() {
      throw new Error('SecurityError');
    },
  };
  const other = createConsoleKey({ storage: broken, claim });
  assert.equal(other.state, 'idle', 'an unreadable storage is an empty one');
  assert.equal(await other.ensure(), 'held');
});

test('a stored key the host no longer accepts is dropped and claimed again', async () => {
  const storage = sessionStore({ [CONSOLE_KEY_STORAGE]: KEY });
  const fresh = 'fresh_key_0123456789abcdefghijkl';
  const { calls, claim } = claimer(Promise.resolve({ console_key: fresh }));
  const holder = createConsoleKey({ storage, claim });
  assert.equal(holder.state, 'held');
  assert.equal(await holder.invalidate(), 'held');
  assert.equal(calls.length, 1);
  assert.equal(holder.key(), fresh);
  assert.equal(storage.data[CONSOLE_KEY_STORAGE], fresh);
});
