import test from 'node:test';
import assert from 'node:assert/strict';

import { createApi } from '../api.js';
import {
  SETTINGS_PATH,
  TEST_TIMEOUT_MS,
  createSettingsClient,
} from './client.js';

function recording(reply = { ok: true }) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    const body = typeof reply === 'function' ? reply(url, init) : reply;
    return {
      status: 200,
      ok: true,
      text: async () => JSON.stringify(body),
    };
  };
  const api = createApi({
    base: 'http://127.0.0.1:54300',
    token: 'tkn',
    fetchImpl,
  });
  return { calls, api };
}

test('needs an api with get, post, put and del', () => {
  assert.throws(
    () => createSettingsClient({ get() {}, post() {} }),
    /api\.put/,
  );
  assert.throws(() => createSettingsClient(null), /api\.get/);
});

test('load is a bearer GET of /settings/llm', async () => {
  const { calls, api } = recording({ rev: 1, providers: [] });
  const out = await createSettingsClient(api).load();
  assert.deepEqual(out, { rev: 1, providers: [] });
  assert.equal(calls[0].url, `http://127.0.0.1:54300${SETTINGS_PATH}`);
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer tkn');
  assert.equal(calls[0].init.cache, 'no-store');
});

test('save is a JSON PUT with If-Match; the key rides only in the body', async () => {
  const { calls, api } = recording();
  const body = {
    provider: 'minimax',
    model: 'MiniMax-M3[1m]',
    key: { action: 'set', value: 'test-key-123' },
    activate: false,
  };
  await createSettingsClient(api).save(body, 7);
  const { url, init } = calls[0];
  assert.equal(init.method, 'PUT');
  assert.equal(url, 'http://127.0.0.1:54300/settings/llm');
  assert.doesNotMatch(url, /test-key-123/);
  assert.equal(init.headers['If-Match'], '7');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(init.body), body);
});

test('test posts to /settings/llm/test with a depth and its own timeout', async () => {
  const seen = [];
  const api = {
    get() {},
    put() {},
    del() {},
    post(path, body, opts) {
      seen.push({ path, body, opts });
      return Promise.resolve({ ok: true });
    },
  };
  const client = createSettingsClient(api);
  await client.test({ provider: 'openrouter', key: 'test-key-123' });
  await client.test({ provider: 'bedrock', depth: 'full' });
  assert.equal(seen[0].path, '/settings/llm/test');
  assert.equal(seen[0].body.depth, 'quick');
  assert.equal(seen[0].body.key, 'test-key-123');
  assert.equal(seen[0].opts.timeoutMs, TEST_TIMEOUT_MS.quick);
  assert.equal(seen[1].body.depth, 'full');
  assert.equal(seen[1].opts.timeoutMs, TEST_TIMEOUT_MS.full);
  assert.ok(TEST_TIMEOUT_MS.quick > 20000 && TEST_TIMEOUT_MS.full > 45000);
});

test('removeKey is a JSON-typed DELETE with If-Match and an encoded id', async () => {
  const { calls, api } = recording();
  await createSettingsClient(api).removeKey('open router', 9);
  const { url, init } = calls[0];
  assert.equal(init.method, 'DELETE');
  assert.equal(
    url,
    'http://127.0.0.1:54300/settings/llm/providers/open%20router/key',
  );
  assert.equal(init.headers['If-Match'], '9');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.equal(init.body, undefined);
});
