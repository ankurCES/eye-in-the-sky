import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  currentUavBridge,
  pageStorage,
  uavBridgeToken,
  uavBridgeUrl,
} from './uavBridge.js';

function memoryStorage(entries = {}) {
  const map = new Map(Object.entries(entries));
  return { getItem: (key) => (map.has(key) ? map.get(key) : null), map };
}

/** Run `fn` with page globals swapped in, restoring them afterwards. */
function withPage({ injected, storage, location }, fn) {
  const saved = {
    injected: Object.getOwnPropertyDescriptor(globalThis, '__GODSEYE__'),
    storage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    location: Object.getOwnPropertyDescriptor(globalThis, 'location'),
  };
  const define = (name, value) =>
    Object.defineProperty(globalThis, name, {
      value,
      configurable: true,
      writable: true,
    });
  try {
    if (injected === undefined) delete globalThis.__GODSEYE__;
    else define('__GODSEYE__', injected);
    define('localStorage', storage ?? null);
    define('location', location ?? undefined);
    return fn();
  } finally {
    for (const [key, name] of [
      ['injected', '__GODSEYE__'],
      ['storage', 'localStorage'],
      ['location', 'location'],
    ]) {
      if (saved[key]) Object.defineProperty(globalThis, name, saved[key]);
      else delete globalThis[name];
    }
  }
}

test('the host-injected config beats a stale localStorage override', () => {
  withPage(
    {
      injected: { bridgeUrl: '', token: 'host-token' },
      storage: memoryStorage({
        'gev.uav.base': 'http://old-dev:8790',
        'gev.uav.token': 'old-token',
      }),
      location: {
        origin: 'http://127.0.0.1:8780',
        protocol: 'http:',
        port: '8780',
      },
    },
    () => {
      const bridge = currentUavBridge();
      assert.equal(bridge.base, 'http://127.0.0.1:8780', "'' is same origin");
      assert.equal(bridge.token, 'host-token');
      assert.equal(uavBridgeUrl(), 'http://127.0.0.1:8780');
      assert.equal(uavBridgeToken(), 'host-token');
    },
  );
});

test('GEV hands the page storage to the resolver, so an operator override counts', () => {
  const storage = memoryStorage({
    'gev.uav.base': 'http://10.0.0.5:8790/',
    'gev.uav.token': 'operator-token',
  });
  withPage(
    { storage, location: { origin: 'http://localhost:4173', port: '4173' } },
    () => {
      assert.equal(pageStorage(), storage);
      const bridge = currentUavBridge();
      assert.equal(bridge.base, 'http://10.0.0.5:8790');
      assert.equal(bridge.token, 'operator-token');
      // Read per call: a token written after load is picked up at once.
      storage.map.set('gev.uav.token', 'rotated');
      assert.equal(uavBridgeToken(), 'rotated');
    },
  );
});

test('with nothing configured, a dev server falls back to the loopback bridge', () => {
  withPage(
    {
      location: {
        origin: 'http://localhost:4173',
        protocol: 'http:',
        port: '4173',
      },
    },
    () => {
      assert.equal(uavBridgeUrl(), 'http://localhost:8790');
      assert.equal(uavBridgeToken(), 'dev-token');
    },
  );
});

test('storage that throws on access is treated as unset', () => {
  const hostile = {
    getItem() {
      throw new Error('SecurityError');
    },
  };
  withPage(
    {
      storage: hostile,
      location: { origin: 'http://localhost:4173', port: '4173' },
    },
    () => {
      assert.equal(uavBridgeToken(), 'dev-token');
    },
  );
});

// The four readers this replaced must not come back: every GEV UAV reader
// goes through ./uavBridge.js, never straight to localStorage or the env.
test('no GEV module reads the bridge origin or token on its own any more', () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8');
  for (const rel of ['./controls.js', './sources.js', './layers/uav.js']) {
    const source = read(rel);
    assert.doesNotMatch(source, /gev\.uav\.(base|token)/, rel);
    assert.doesNotMatch(source, /VITE_UAV_BRIDGE_(URL|TOKEN)/, rel);
    assert.doesNotMatch(source, /localhost:8790|'dev-token'/, rel);
    assert.match(source, /from '\.{1,2}\/(?:\.\.\/app\/)?uavBridge\.js'/, rel);
  }
  // The globe layer's source was the fourth reader nobody configured: the
  // standalone catalog built it bare, so it used the build env and
  // localhost:8790 whatever the host injected.
  assert.match(
    read('../standalone/layerSources.js'),
    /uav: createUavSource\(\{ baseUrl: uavBridgeUrl, token: uavBridgeToken \}\)/,
  );
  // The portable source takes the resolved getters by injection; its own env
  // lookup is only the fallback for callers that never configure it.
  const live = read('../sources/live/uav.js');
  assert.match(
    live,
    /resolve\(override\) \|\|\s*resolve\(runtime\.baseUrl\) \|\|\s*import\.meta\.env\?\.VITE_UAV_BRIDGE_URL/,
  );
  assert.match(
    live,
    /resolve\(override\) \|\|\s*resolve\(runtime\.token\) \|\|\s*import\.meta\.env\?\.VITE_UAV_BRIDGE_TOKEN/,
  );
});
