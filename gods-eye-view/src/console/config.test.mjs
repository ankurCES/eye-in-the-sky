import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  DEFAULT_BRIDGE_BASE,
  DEFAULT_BRIDGE_TOKEN,
  STORAGE_KEYS,
  resolveBridge,
  sameOrigin,
} from './config.js';

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
