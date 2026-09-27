import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ALARM_INSET_CLASS,
  ALARM_INSET_VAR,
  MAP_HIDDEN_CLASS,
  MAP_INSET_CLASS,
  MAP_INSET_VAR,
  TRACKING_PORT_CSS,
  createDeferredTrackingPort,
  createTrackingPort,
  defaultKeyholeGeometry,
} from './trackingPort.js';

// ---- a small fake page ------------------------------------------------------

function eventTarget(extra = {}) {
  const listeners = [];
  return {
    listeners,
    addEventListener(type, fn, capture = false) {
      listeners.push({ type, fn, capture: Boolean(capture) });
    },
    removeEventListener(type, fn, capture = false) {
      const at = listeners.findIndex(
        (l) => l.type === type && l.fn === fn && l.capture === Boolean(capture),
      );
      if (at >= 0) listeners.splice(at, 1);
    },
    fire(type, event, { capture = null } = {}) {
      for (const l of [...listeners])
        if (l.type === type && (capture === null || l.capture === capture))
          l.fn(event);
    },
    ...extra,
  };
}

function element(tag, { classes = [], attrs = {}, editable = false } = {}) {
  const classSet = new Set(classes);
  const attributes = new Map(Object.entries(attrs));
  const props = new Map();
  return {
    ...eventTarget(),
    nodeType: 1,
    tagName: tag.toUpperCase(),
    children: [],
    classList: {
      toggle(name, on) {
        if (on) classSet.add(name);
        else classSet.delete(name);
      },
      contains: (name) => classSet.has(name),
    },
    setAttribute(key, value) {
      attributes.set(key, String(value));
    },
    removeAttribute(key) {
      attributes.delete(key);
    },
    hasAttribute: (key) => attributes.has(key),
    getAttribute: (key) => attributes.get(key) ?? null,
    style: {
      props,
      setProperty: (key, value) => props.set(key, value),
      removeProperty: (key) => props.delete(key),
    },
    append(...kids) {
      this.children.push(...kids);
    },
    removed: false,
    remove() {
      this.removed = true;
    },
    closest(selector) {
      return editable && /input|textarea|contenteditable/.test(selector)
        ? this
        : null;
    },
  };
}

function fakePage() {
  const body = element('body');
  const head = element('head');
  const documentElement = element('html');
  const map = element('div', { attrs: { id: 'cesiumContainer' } });
  const loader = element('div', { attrs: { id: 'loading-screen' } });
  const launcher = element('div', { attrs: { inert: '' } }); // GEV's own inert
  const script = element('script');
  const consoleRoot = element('div', { classes: ['ic-root'] });
  body.children.push(map, loader, launcher, script, consoleRoot);
  const doc = {
    body,
    head,
    documentElement,
    hidden: false,
    createElement: (tag) => element(tag),
    getElementById: () => null,
  };
  const win = eventTarget();
  return {
    doc,
    win,
    body,
    head,
    documentElement,
    map,
    loader,
    launcher,
    script,
    consoleRoot,
  };
}

/** Manual timers: nothing runs until the test says so. */
function fakeTimers() {
  let next = 0;
  const queue = new Map();
  return {
    queue,
    setTimer(fn, ms) {
      next += 1;
      queue.set(next, { fn, ms });
      return next;
    },
    clearTimer(id) {
      queue.delete(id);
    },
    /** Run every queued timer with delay <= ms, once. */
    run(ms = Infinity) {
      for (const [id, task] of [...queue]) {
        if (task.ms > ms) continue;
        queue.delete(id);
        task.fn();
      }
    },
  };
}

function fakeCockpit(win, body) {
  const cockpit = {
    active: false,
    subject: null,
    exits: [],
    readAircraftInfo: () =>
      cockpit.subject ? { icao24: cockpit.subject } : null,
    enterAs(subject) {
      cockpit.active = true;
      cockpit.subject = subject;
      body.classList.toggle('cockpit-mode', true);
      win.fire('gev:cockpit-mode-changed', {
        detail: {
          active: true,
          subjectId: subject.toLowerCase(),
          layerId: null,
        },
      });
      return true;
    },
    exit(options) {
      if (!cockpit.active) return false;
      cockpit.exits.push(options);
      cockpit.active = false;
      cockpit.subject = null;
      body.classList.toggle('cockpit-mode', false);
      win.fire('gev:cockpit-mode-changed', {
        detail: { active: false, subjectId: null, layerId: null },
      });
      return true;
    },
  };
  return cockpit;
}

function harness({ fix = true } = {}) {
  const page = fakePage();
  const timers = fakeTimers();
  const cockpit = fakeCockpit(page.win, page.body);
  const calls = {
    track: [],
    untrack: 0,
    armed: [],
    disarmed: 0,
    expand: 0,
    enabled: [],
  };
  let armed = false;
  const panel = {
    hasFix: fix,
    enterCockpit(reference) {
      calls.track.push(reference);
      return panel.hasFix ? cockpit.enterAs(reference) : false;
    },
    armCockpitFollow(reference) {
      calls.armed.push(reference);
      armed = true;
    },
    disarmCockpitFollow() {
      calls.disarmed += 1;
      armed = false;
    },
    isCockpitFollowArmed: () => armed,
    giveUp() {
      armed = false;
    },
    expand() {
      calls.expand += 1;
    },
  };
  const uavLayer = {
    untrack() {
      calls.untrack += 1;
    },
  };
  const viewer = {
    useDefaultRenderLoop: true,
    resized: 0,
    renders: 0,
    container: {
      getBoundingClientRect: () => ({
        left: 0,
        top: 0,
        width: 1000,
        height: 800,
      }),
    },
    resize() {
      this.resized += 1;
    },
    scene: {
      requestRender() {
        viewer.renders += 1;
      },
    },
    isDestroyed: () => false,
  };
  const dataManager = {
    enabled: false,
    isEnabled: () => dataManager.enabled,
    async setEnabled(id, on, options) {
      calls.enabled.push([id, on, options]);
      dataManager.enabled = on;
      return true;
    },
  };
  const observers = [];
  class FakeObserver {
    constructor(cb) {
      this.cb = cb;
      this.connected = false;
      observers.push(this);
    }
    observe() {
      this.connected = true;
    }
    disconnect() {
      this.connected = false;
    }
  }
  const port = createTrackingPort({
    viewer,
    getCockpit: () => cockpit,
    uavLayer,
    missionPanel: panel,
    doc: page.doc,
    win: page.win,
    setTimer: timers.setTimer,
    clearTimer: timers.clearTimer,
    MutationObserverImpl: FakeObserver,
    enterTimeoutMs: 35000,
    pollMs: 250,
  });
  return {
    ...page,
    timers,
    cockpit,
    panel,
    calls,
    uavLayer,
    viewer,
    dataManager,
    port,
    observers,
  };
}

const flush = async () => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

// ---- readiness and enter ---------------------------------------------------

test('whenReady waits for the data phase; enter() waits too and enables the UAV layer once', async () => {
  const h = harness();
  let ready = false;
  h.port.whenReady().then(() => {
    ready = true;
  });
  const entering = h.port.enter('Drone1');
  await flush();
  assert.equal(ready, false, 'not ready before attachData');
  assert.deepEqual(h.calls.track, [], 'no entry attempt before GEV is up');

  h.port.attachData(h.dataManager);
  assert.equal(await entering, true);
  assert.equal(ready, true);
  assert.deepEqual(h.calls.enabled, [
    ['uav', true, { origin: 'programmatic' }],
  ]);
  assert.deepEqual(h.calls.track, ['Drone1']);
  assert.equal(h.port.isTracking(), true);

  // Already on this drone: no second layer enable, no second entry.
  assert.equal(await h.port.enter('Drone1'), true);
  assert.equal(h.calls.enabled.length, 1);
  assert.deepEqual(h.calls.track, ['Drone1']);
});

test('enter() lets the boot-time UAV start finish before it switches the layer on', async () => {
  // Live E2E: the first Track after a page load raced the boot start; its
  // setEnabled('uav') announced `visibility` 5 ms after the cockpit opened
  // and GEV's Context handler dropped the cockpit ("Couldn't lock on").
  const h = harness();
  let finishStart;
  const startup = new Promise((resolve) => {
    finishStart = resolve;
  });
  h.port.attachData(h.dataManager, { startup });
  const entering = h.port.enter('Drone1');
  await flush();
  assert.deepEqual(h.calls.enabled, [], 'waits for the boot start');
  assert.deepEqual(h.calls.track, []);
  h.dataManager.enabled = true; // the boot start switched the layer on
  finishStart();
  assert.equal(await entering, true);
  assert.deepEqual(h.calls.enabled, [], 'no second, idempotent enable');
  assert.deepEqual(h.calls.track, ['Drone1']);
});

test('a boot start that never settles only delays enter() up to its cap', async () => {
  const h = harness();
  h.port.attachData(h.dataManager, { startup: new Promise(() => {}) });
  const entering = h.port.enter('Drone1');
  await flush();
  assert.deepEqual(h.calls.track, []);
  h.timers.run(20000); // the startup cap
  assert.equal(await entering, true);
  assert.deepEqual(h.calls.track, ['Drone1']);
});

test('after switching the UAV layer on, enter() waits for visibility news to stop', async () => {
  const h = harness();
  const subs = new Set();
  h.dataManager.subscribe = (cb) => {
    subs.add(cb);
    return () => subs.delete(cb);
  };
  const announce = (type) => {
    for (const cb of [...subs]) cb({ type, layerId: 'uav', enabled: true });
  };
  h.port.attachData(h.dataManager);
  const entering = h.port.enter('Drone1');
  await flush();
  assert.deepEqual(
    h.calls.enabled.map((c) => c[0]),
    ['uav'],
  );
  assert.deepEqual(h.calls.track, [], 'not yet: the manager may still talk');
  assert.equal(subs.size, 1);
  const quietTimers = () =>
    [...h.timers.queue].filter(([, t]) => t.ms === 150).map(([id]) => id);
  const before = quietTimers();
  assert.equal(before.length, 1, 'one quiet window is pending');
  announce('visibility'); // a trailing (idempotent) announcement re-arms it
  const rearmed = quietTimers();
  assert.equal(rearmed.length, 1);
  assert.notEqual(rearmed[0], before[0], 'the quiet window restarted');
  announce('refresh'); // other news does not
  assert.deepEqual(quietTimers(), rearmed);
  h.timers.run(150);
  assert.equal(await entering, true);
  assert.deepEqual(h.calls.track, ['Drone1']);
  assert.equal(subs.size, 0, 'unsubscribed once settled');
});

test('enter() with no fix arms the panel follow and resolves true when it lands', async () => {
  const h = harness({ fix: false });
  h.port.attachData(h.dataManager);
  const entering = h.port.enter('Drone1');
  await flush();
  assert.deepEqual(h.calls.armed, ['Drone1'], 'the panel retry is reused');
  let settled = null;
  entering.then((value) => {
    settled = value;
  });
  h.timers.run(250); // a poll: still armed, still not in
  await flush();
  assert.equal(settled, null);
  // The panel's tick later enters (its onEnterCockpit path).
  h.cockpit.enterAs('Drone1');
  await flush();
  assert.equal(settled, true);
  assert.equal(h.timers.queue.size, 0, 'poll and timeout are cleared');
});

test('enter() resolves false on timeout and disarms the follow', async () => {
  const h = harness({ fix: false });
  h.port.attachData(h.dataManager);
  const entering = h.port.enter('Drone1');
  await flush();
  h.timers.run(35000);
  assert.equal(await entering, false);
  assert.equal(h.calls.disarmed, 1);
  assert.equal(h.port.isTracking(), false);
});

test('enter() resolves false as soon as the panel gives up', async () => {
  const h = harness({ fix: false });
  h.port.attachData(h.dataManager);
  const entering = h.port.enter('Drone1');
  await flush();
  h.panel.giveUp();
  h.timers.run(250);
  assert.equal(await entering, false);
});

test('a newer enter() or an exit() supersedes a pending one', async () => {
  const h = harness({ fix: false });
  h.port.attachData(h.dataManager);
  const first = h.port.enter('Drone1');
  await flush();
  const second = h.port.enter('Drone2');
  assert.equal(await first, false);
  await flush();
  h.port.exit();
  assert.equal(await second, false);
  assert.ok(h.calls.disarmed >= 1, 'exit disarms the panel follow');
});

// ---- exit and change notifications -----------------------------------------

test('exit() leaves the cockpit without re-tracking, then untracks', async () => {
  const h = harness();
  const changes = [];
  h.port.onChange((change) => changes.push(change));
  h.port.attachData(h.dataManager);
  await h.port.enter('Drone1');
  h.port.exit();
  assert.deepEqual(h.cockpit.exits, [{ restoreTracking: false }]);
  assert.equal(h.calls.untrack, 1);
  assert.deepEqual(
    changes.map(({ active, by }) => [active, by]),
    [
      [true, 'console'],
      [false, 'console'],
    ],
  );
});

test("GEV's own exit (Esc / c / map switch) is reported and drops the track", async () => {
  const h = harness();
  const changes = [];
  h.port.onChange((change) => changes.push(change));
  h.port.attachData(h.dataManager);
  await h.port.enter('Drone1');
  h.cockpit.exit({ restoreTracking: true }); // what GEV's controls call
  assert.deepEqual(changes.at(-1), {
    active: false,
    subjectId: null,
    layerId: null,
    by: 'gev',
  });
  assert.equal(h.calls.untrack, 0, 'deferred until exit() has returned');
  h.timers.run(0);
  assert.equal(h.calls.untrack, 1, 'the restored track is dropped');
});

test('switching drones exits the first quietly, then enters the second', async () => {
  const h = harness();
  const changes = [];
  h.port.attachData(h.dataManager);
  await h.port.enter('Drone1');
  h.port.onChange((change) => changes.push(change));
  assert.equal(await h.port.enter('Drone2'), true);
  assert.deepEqual(h.cockpit.exits, [{ restoreTracking: false }]);
  assert.deepEqual(
    changes.map(({ active, subjectId }) => [active, subjectId]),
    [[true, 'drone2']],
    'no spurious "left tracking" in between',
  );
});

test('a cockpit GEV enters behind a hidden map is undone, not reported', async () => {
  const h = harness();
  const changes = [];
  h.port.onChange((change) => changes.push(change));
  h.port.attachData(h.dataManager);
  h.port.setMapVisible(false);
  h.cockpit.enterAs('abc123'); // e.g. a restored share link
  assert.equal(h.cockpit.active, true);
  h.timers.run(0);
  assert.equal(h.cockpit.active, false);
  assert.deepEqual(h.cockpit.exits, [{ restoreTracking: true }]);
  assert.deepEqual(changes, []);
});

// ---- map visibility ---------------------------------------------------------

test('hiding the map hides the container, suspends rendering and inerts GEV chrome', () => {
  const h = harness();
  h.port.setMapVisible(false);
  assert.equal(h.body.classList.contains(MAP_HIDDEN_CLASS), true);
  assert.equal(h.port.isMapHidden(), true);
  assert.equal(h.viewer.useDefaultRenderLoop, false);
  assert.equal(h.map.hasAttribute('inert'), true);
  assert.equal(h.loader.hasAttribute('inert'), true);
  assert.equal(h.consoleRoot.hasAttribute('inert'), false, 'never the console');
  assert.equal(h.script.hasAttribute('inert'), false);
  const style = h.head.children.find(
    (el) => el.id === 'gev-tracking-port-style',
  );
  assert.ok(style, 'the port injects its rules once');
  assert.match(style.textContent, /body\.gev-map-hidden #cesiumContainer/);
  assert.match(
    style.textContent,
    /\.uav-alarm-stack\{display:none!important\}/,
  );

  // GEV keeps appending to body while the orb is up.
  const toast = element('div');
  h.observers[0].cb([{ addedNodes: [toast] }]);
  assert.equal(toast.hasAttribute('inert'), true);

  h.port.setMapVisible(true);
  assert.equal(h.body.classList.contains(MAP_HIDDEN_CLASS), false);
  assert.equal(h.viewer.useDefaultRenderLoop, true);
  assert.equal(h.map.hasAttribute('inert'), false);
  assert.equal(toast.hasAttribute('inert'), false);
  assert.equal(h.launcher.hasAttribute('inert'), true, "GEV's own inert stays");
  assert.equal(h.observers[0].connected, false);
  assert.ok(
    h.viewer.resized >= 1 && h.viewer.renders >= 1,
    'first frame requested',
  );
  assert.equal(h.head.children.length, 1, 'the stylesheet is not duplicated');
});

test("a registered render sync owns the loop (tools.js's visibility handler)", () => {
  const h = harness();
  const seen = [];
  h.port.setRenderSync(() => seen.push(h.port.isMapHidden()));
  h.viewer.useDefaultRenderLoop = 'untouched';
  h.port.setMapVisible(false);
  h.port.setMapVisible(false); // no change, no second sync
  h.port.setMapVisible(true);
  assert.deepEqual(seen, [true, false]);
  assert.equal(h.viewer.useDefaultRenderLoop, 'untouched');
});

// ---- keyboard guard -----------------------------------------------------------

function key(k, { target = null, ...mods } = {}) {
  return {
    key: k,
    target,
    stopped: false,
    stopPropagation() {
      this.stopped = true;
    },
    ...mods,
  };
}

test('while the map is hidden, bare GEV shortcut keys stop before GEV sees them', () => {
  const h = harness();
  const plain = element('button');
  const field = element('textarea', { editable: true });
  const press = (event, where) =>
    where === 'window'
      ? h.win.fire('keydown', event, { capture: true })
      : h.documentElement.fire('keydown', event, { capture: false });

  // Map visible: nothing is touched.
  let event = key('c', { target: plain });
  press(event, 'window');
  assert.equal(event.stopped, false);

  h.port.setMapVisible(false);
  event = key('c', { target: plain });
  press(event, 'window');
  assert.equal(
    event.stopped,
    true,
    'the cockpit c toggle is blocked at capture',
  );
  event = key('h', { target: plain });
  press(event, 'window');
  assert.equal(event.stopped, false, 'other letters reach the console first');
  press(event, 'html');
  assert.equal(event.stopped, true, '...then stop before document listeners');
  for (const k of ['1', 'F', 'v']) {
    event = key(k, { target: plain });
    press(event, 'html');
    assert.equal(event.stopped, true, `${k} blocked`);
  }
  for (const [k, mods] of [
    ['k', { metaKey: true }],
    ['Escape', {}],
    ['/', {}],
    ['ArrowUp', {}],
  ]) {
    event = key(k, { target: plain, ...mods });
    press(event, 'window');
    press(event, 'html');
    assert.equal(event.stopped, false, `${k} passes`);
  }
  event = key('c', { target: field });
  press(event, 'window');
  press(event, 'html');
  assert.equal(event.stopped, false, 'typing is never touched');
});

// ---- inset, keyhole, panel -------------------------------------------------

test('the viewport inset narrows the map and recentres the keyhole rules', () => {
  const h = harness();
  h.port.setViewportInset({ right: 446.4 });
  assert.equal(h.body.style.props.get(MAP_INSET_VAR), '446px');
  assert.equal(h.body.classList.contains(MAP_INSET_CLASS), true);
  assert.equal(h.port.viewportInset(), 446);
  assert.ok(h.viewer.resized >= 1);
  assert.match(
    TRACKING_PORT_CSS,
    /body\.gev-map-inset #cesiumContainer\{width:auto;right:var\(--gev-map-inset-right/,
  );
  assert.match(
    TRACKING_PORT_CSS,
    /body\.gev-map-inset #cockpit-hud\{right:var/,
  );
  assert.match(
    TRACKING_PORT_CSS,
    /--cockpit-keyhole-radius:min\(calc\(\(100vw - var\(--gev-map-inset-right,0px\)\) \* 0\.4\),52vh\)/,
  );
  assert.match(TRACKING_PORT_CSS, /#cockpit-cloud-effects\{clip-path:circle\(/);
  h.port.setViewportInset({ right: 0 });
  assert.equal(h.body.classList.contains(MAP_INSET_CLASS), false);
  h.port.setViewportInset({ right: 'nonsense' });
  assert.equal(h.port.viewportInset(), 0);
});

test('a bottom inset (the narrow dock sheet) lifts the alarm toasts above it', () => {
  const h = harness();
  h.port.setViewportInset({ right: 0, bottom: 430.2 });
  assert.equal(h.body.style.props.get(ALARM_INSET_VAR), '430px');
  assert.equal(h.body.classList.contains(ALARM_INSET_CLASS), true);
  assert.equal(h.body.classList.contains(MAP_INSET_CLASS), false);
  assert.match(
    TRACKING_PORT_CSS,
    /body\.gev-alarm-inset \.uav-alarm-stack\{bottom:calc\(var\(--gev-alarm-inset-bottom,0px\) \+ 16px\)\}/,
  );
  h.port.setViewportInset({ right: 0 });
  assert.equal(h.body.classList.contains(ALARM_INSET_CLASS), false);
  h.port.setViewportInset({ bottom: 200 });
  h.port.destroy();
  assert.equal(h.body.classList.contains(ALARM_INSET_CLASS), false);
});

test('keyhole() is the circle GEV draws, in page coordinates of the map', () => {
  const h = harness();
  assert.deepEqual(h.port.keyhole(), { x: 500, y: 400, r: 420 });
  h.viewer.container.getBoundingClientRect = () => ({
    left: 0,
    top: 0,
    width: 0,
    height: 0,
  });
  assert.equal(h.port.keyhole(), null, 'a hidden map has no keyhole');
  assert.deepEqual(defaultKeyholeGeometry(0, 10), {
    centerX: 0,
    centerY: 0,
    radius: 0,
  });
});

test('openMissionPanel() expands the UAV drawer', () => {
  const h = harness();
  assert.equal(h.port.openMissionPanel(), true);
  assert.equal(h.calls.expand, 1);
});

test('destroy() removes every listener and class and fails a pending wait', async () => {
  const h = harness();
  h.port.setMapVisible(false);
  h.port.setViewportInset({ right: 300 });
  const waiting = h.port.whenReady();
  h.port.destroy();
  await assert.rejects(waiting, /shut down/);
  assert.equal(h.win.listeners.length, 0);
  assert.equal(h.documentElement.listeners.length, 0);
  assert.equal(h.body.classList.contains(MAP_HIDDEN_CLASS), false);
  assert.equal(h.body.classList.contains(MAP_INSET_CLASS), false);
  assert.equal(h.map.hasAttribute('inert'), false);
  assert.equal(await h.port.enter('Drone1'), false);
});

// ---- the deferred port main.js hands the console -----------------------------

test('the deferred port replays early requests and forwards changes on attach', async () => {
  const deferred = createDeferredTrackingPort();
  const changes = [];
  deferred.onChange((change) => changes.push(change));
  deferred.setMapVisible(false);
  deferred.setViewportInset({ right: 420 });
  assert.equal(deferred.isTracking(), false);
  assert.equal(deferred.keyhole(), null);
  assert.equal(deferred.openMissionPanel(), false);
  const entering = deferred.enter('Drone1');

  const h = harness();
  deferred.attach(h.port);
  assert.equal(h.port.isMapHidden(), true, 'the orb-mode hide was replayed');
  assert.equal(h.port.viewportInset(), 420);
  h.port.attachData(h.dataManager);
  await deferred.whenReady();
  assert.equal(await entering, true);
  assert.equal(deferred.isTracking(), true);
  assert.equal(changes.at(-1).active, true);
  deferred.exit();
  assert.equal(deferred.isTracking(), false);

  deferred.attach(null); // teardown
  assert.equal(deferred.isAttached(), false);
  assert.equal(deferred.isTracking(), false);
});

test('a failed start rejects whenReady and makes enter() resolve false', async () => {
  const deferred = createDeferredTrackingPort();
  const entering = deferred.enter('Drone1');
  deferred.fail(new Error('Cesium could not start'));
  assert.equal(await entering, false);
  await assert.rejects(deferred.whenReady(), /Cesium could not start/);
});
