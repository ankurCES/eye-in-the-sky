import test from 'node:test';
import assert from 'node:assert/strict';
import * as Cesium from 'cesium';

import {
  ALARM_INSET_CLASS,
  ALARM_INSET_VAR,
  MAP_HIDDEN_CLASS,
  MAP_INSET_CLASS,
  MAP_INSET_TOP_CLASS,
  MAP_INSET_TOP_VAR,
  MAP_INSET_VAR,
  OVERVIEW_CLASS,
  SHOW_AREA_DEFAULT_RADIUS_M,
  SHOW_AREA_FLIGHT_S,
  SHOW_AREA_GROUND_MAX_M,
  SHOW_AREA_GROUND_MIN_M,
  SHOW_AREA_MIN_EXTENT_M,
  SHOW_AREA_PAD,
  SHOW_AREA_PITCH_DEG,
  SHOW_AREA_STARTUP_WAIT_MS,
  TRACKING_PORT_CSS,
  areaBounds,
  areaRectangle,
  createDeferredTrackingPort,
  createTrackingPort,
  defaultKeyholeGeometry,
  plausibleGround,
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

// ---- map overview (WG v2 §4.2.7) --------------------------------------------

/** The standard harness plus a camera and a UAV layer with a context overlay. */
function overviewHarness(options) {
  const h = harness(options);
  const camera = {
    views: [],
    flights: [],
    frustum: { fovy: 1.0 },
    setView(view) {
      camera.views.push(view);
    },
    flyTo(flight) {
      camera.flights.push(flight);
    },
  };
  h.viewer.camera = camera;
  const context = {
    active: [],
    visibility: [],
    pickCb: null,
    unsubscribed: 0,
  };
  Object.assign(h.uavLayer, {
    setContextActive(on) {
      context.active.push(on);
      return on;
    },
    setContextVisibility(kinds) {
      context.visibility.push(kinds);
      return { sites: true, forces: true, ...kinds };
    },
    onContextPick(cb) {
      context.pickCb = cb;
      return () => {
        context.unsubscribed += 1;
        context.pickCb = null;
      };
    },
    getContextStatus: () => ({ status: 'ok', sites: { drawn: 3 } }),
  });
  return { ...h, camera, context };
}

const KHERSON = { bbox: [46.6, 32.5, 46.7, 32.7] };

function cartographicOf(cartesian) {
  const c = Cesium.Cartographic.fromCartesian(cartesian);
  return {
    lat: Cesium.Math.toDegrees(c.latitude),
    lon: Cesium.Math.toDegrees(c.longitude),
    height: c.height,
  };
}

test('areaRectangle is Rectangle.fromDegrees(w, s, e, n), padded 15 %', () => {
  const rect = areaRectangle({ bbox: [10, 20, 11, 22] });
  assert.ok(rect instanceof Cesium.Rectangle);
  const deg = (r) => Cesium.Math.toDegrees(r);
  // Centre (10.5, 21); half-extents 0.5 and 1 degree, each grown 15 %.
  assert.ok(Math.abs(deg(rect.south) - (10.5 - 0.575)) < 1e-9);
  assert.ok(Math.abs(deg(rect.north) - (10.5 + 0.575)) < 1e-9);
  assert.ok(Math.abs(deg(rect.west) - (21 - 1.15)) < 1e-6);
  assert.ok(Math.abs(deg(rect.east) - (21 + 1.15)) < 1e-6);
  assert.ok(rect.west < rect.east, 'west and east are not swapped');
  assert.ok(rect.south < rect.north, 'south and north are not swapped');
  assert.equal(SHOW_AREA_PAD, 0.15);
});

test('a centre without a radius frames 2 km; nothing frames under 1 km', () => {
  const around = areaBounds({ center: [47.64, -122.14] });
  assert.equal(SHOW_AREA_DEFAULT_RADIUS_M, 2000);
  assert.ok(Math.abs(around.halfHeightM - 2000 * 1.15) < 1e-6);
  assert.ok(Math.abs(around.halfWidthM - 2000 * 1.15) < 1e-6);
  const sized = areaBounds({ center: [47.64, -122.14], radiusM: 5000 });
  assert.ok(Math.abs(sized.halfHeightM - 5750) < 1e-6);
  const tiny = areaBounds({ bbox: [47.64, -122.14, 47.6401, -122.1399] });
  assert.equal(SHOW_AREA_MIN_EXTENT_M, 1000);
  assert.equal(tiny.halfHeightM, 500);
  assert.equal(tiny.halfWidthM, 500);
  // Across the antimeridian (Fiji): west > east is a crossing, not an error.
  const fiji = areaBounds({ bbox: [-17, 179.5, -16, -179.5] });
  assert.ok(Math.abs(Math.abs(fiji.centerLon) - 180) < 1e-9);
  for (const bad of [
    null,
    {},
    { bbox: [1, 2, 3] },
    { bbox: [50, 0, 40, 1] },
    { bbox: [0, 0, 100, 1] },
    { center: ['x', 1] },
  ])
    assert.equal(areaBounds(bad), null);
});

test('hidden, showArea is a synchronous setView at -60 deg, looking at the centre', async () => {
  const h = overviewHarness();
  h.port.setMapVisible(false);
  const shown = h.port.showArea(KHERSON, { animate: true });
  assert.equal(h.camera.views.length, 1, 'set before showArea returned');
  assert.equal(h.camera.flights.length, 0, 'never a flight while hidden');
  assert.equal(await shown, true);
  const [view] = h.camera.views;
  assert.equal(view.orientation.heading, 0);
  assert.ok(
    Math.abs(
      view.orientation.pitch - Cesium.Math.toRadians(SHOW_AREA_PITCH_DEG),
    ) < 1e-12,
  );
  assert.equal(SHOW_AREA_PITCH_DEG, -60);
  const at = cartographicOf(view.destination);
  // Due south of the centre, as far back as it is high over tan(60).
  assert.ok(Math.abs(at.lon - 32.6) < 1e-6);
  assert.ok(at.lat < 46.65);
  const backM = (46.65 - at.lat) * 111320;
  assert.ok(Math.abs(at.height / backM - Math.tan(Math.PI / 3)) < 0.05);
  // The whole padded area fits the vertical field of view.
  const bounds = areaBounds(KHERSON);
  const range = Math.hypot(backM, at.height);
  assert.ok(
    range >=
      (0.99 * Math.hypot(bounds.halfWidthM, bounds.halfHeightM)) /
        Math.tan(0.5),
  );
});

test('visible with animate, showArea flies for 1.5 s and resolves on arrival', async () => {
  const h = overviewHarness();
  const shown = h.port.showArea(KHERSON, { animate: true });
  assert.equal(h.camera.views.length, 0);
  assert.equal(h.camera.flights.length, 1);
  const [flight] = h.camera.flights;
  assert.equal(flight.duration, SHOW_AREA_FLIGHT_S);
  assert.equal(SHOW_AREA_FLIGHT_S, 1.5);
  flight.complete();
  assert.equal(await shown, true);

  const cancelled = h.port.showArea(KHERSON, { animate: true });
  h.camera.flights[1].cancel();
  assert.equal(await cancelled, false);

  // Without `animate`, a visible map is set, not flown.
  assert.equal(await h.port.showArea(KHERSON), true);
  assert.equal(h.camera.views.length, 1);
  assert.equal(h.camera.flights.length, 2);
});

test('reduced motion turns every showArea into a setView', async () => {
  const h = overviewHarness();
  h.win.matchMedia = (query) => ({
    matches: query === '(prefers-reduced-motion: reduce)',
  });
  assert.equal(await h.port.showArea(KHERSON, { animate: true }), true);
  assert.equal(h.camera.flights.length, 0);
  assert.equal(h.camera.views.length, 1);
});

test('showArea never fights the cockpit, and releases a tracked entity', async () => {
  const h = overviewHarness();
  h.cockpit.enterAs('Drone1');
  assert.equal(await h.port.showArea(KHERSON), false);
  assert.equal(h.camera.views.length, 0);
  h.cockpit.exit({ restoreTracking: false });

  h.viewer.trackedEntity = { id: 'uav:Drone1' };
  const untracks = h.calls.untrack;
  assert.equal(await h.port.showArea({ center: [46.64, 32.6] }), true);
  assert.equal(h.viewer.trackedEntity, undefined);
  assert.equal(h.calls.untrack, untracks + 1);

  assert.equal(await h.port.showArea({ bbox: 'nowhere' }), false);
  assert.equal(h.camera.views.length, 1, 'a bad target moves nothing');
});

/** Record the order of the camera calls showArea makes. */
function flightLog(h) {
  const log = [];
  const { setView, flyTo } = h.camera;
  h.camera.setView = (view) => {
    log.push('setView');
    setView(view);
  };
  h.camera.flyTo = (flight) => {
    log.push('flyTo');
    flyTo(flight);
  };
  h.camera.cancelFlight = () => log.push('cancel');
  return log;
}

test('showArea cancels a flight queued behind the hidden map before framing, hidden or visible', async () => {
  // Review: GEV's startup fly-ins (camera.js flyToAustin, uavAutoStart) froze
  // while the map was hidden and resumed on its first frame, overriding the
  // setView: the first map entry ended over Austin or the old theater.
  const h = overviewHarness();
  const log = flightLog(h);
  h.port.setMapVisible(false);
  h.camera.flyTo({ destination: 'Austin, 600 m' });
  assert.equal(await h.port.showArea(KHERSON), true);
  assert.deepEqual(log, ['flyTo', 'cancel', 'setView']);
  h.port.setMapVisible(true);
  log.length = 0;
  const shown = h.port.showArea(KHERSON, { animate: true });
  assert.deepEqual(log, ['cancel', 'flyTo']);
  h.camera.flights.at(-1).complete();
  assert.equal(await shown, true);
});

test('while the boot-time UAV start runs, showArea waits for it, then cancels its flight', async () => {
  const h = overviewHarness();
  const log = flightLog(h);
  let finishStart;
  const startup = new Promise((resolve) => {
    finishStart = resolve;
  });
  h.port.attachData(h.dataManager, { startup });
  h.port.setMapVisible(false);
  const shown = h.port.showArea(KHERSON);
  await flush();
  assert.deepEqual(log, [], 'nothing framed while the start may still fly');
  h.camera.flyTo({ destination: 'lead drone home + 25 km' }); // uavAutoStart
  finishStart();
  assert.equal(await shown, true);
  assert.deepEqual(log, ['flyTo', 'cancel', 'setView']);
  // Once the start is over, a hidden showArea is synchronous again.
  log.length = 0;
  h.port.showArea(KHERSON);
  assert.deepEqual(log, ['cancel', 'setView']);
});

test('a boot start that never settles delays showArea only up to its cap', async () => {
  const h = overviewHarness();
  h.port.attachData(h.dataManager, { startup: new Promise(() => {}) });
  const shown = h.port.showArea(KHERSON);
  await flush();
  assert.equal(h.camera.views.length, 0);
  assert.equal(SHOW_AREA_STARTUP_WAIT_MS, 3000);
  h.timers.run(SHOW_AREA_STARTUP_WAIT_MS);
  assert.equal(await shown, true);
  assert.equal(h.camera.views.length, 1);
});

test('showArea never puts the camera underground on an unrefined terrain tile', async () => {
  // Review: offline, globe.getHeight at a far AO read -46114 m and the map
  // went black with the camera 38 km under the ellipsoid.
  const h = overviewHarness();
  h.port.setMapVisible(false);
  let tile = -46114;
  h.viewer.scene.globe = { getHeight: () => tile };
  const bounds = areaBounds(KHERSON);
  const rangeM =
    Math.hypot(bounds.halfWidthM, bounds.halfHeightM) / Math.tan(0.5);
  const up = rangeM * Math.sin(Math.PI / 3);
  const heightAt = () => cartographicOf(h.camera.views.at(-1).destination);

  assert.equal(await h.port.showArea(KHERSON), true);
  assert.ok(heightAt().height > up * 0.5, 'above the ground');
  assert.ok(Math.abs(heightAt().height - up) < 50, 'an implausible tile is 0');
  // The caller's known ground (the theater's ground_msl_m) wins.
  assert.equal(await h.port.showArea({ ...KHERSON, groundM: 925 }), true);
  assert.ok(Math.abs(heightAt().height - (925 + up)) < 50);
  assert.equal(await h.port.showArea({ ...KHERSON, groundM: -9e4 }), true);
  assert.ok(Math.abs(heightAt().height - up) < 50, 'a bad hint is ignored');
  // A plausible globe height is still used without a hint.
  tile = 300;
  assert.equal(await h.port.showArea(KHERSON), true);
  assert.ok(Math.abs(heightAt().height - (300 + up)) < 50);

  assert.equal(SHOW_AREA_GROUND_MIN_M, -500);
  assert.equal(SHOW_AREA_GROUND_MAX_M, 9000);
  for (const bad of [-46114, 9001, -501, NaN, Infinity, null, '12', undefined])
    assert.equal(plausibleGround(bad), null, String(bad));
  for (const ok of [-430, 0, 925, 8849]) assert.equal(plausibleGround(ok), ok);
});

test('in the overview over a narrow bottom sheet the credits sit above it, never hidden', () => {
  // Review: at 420 px the sheet covered the Cesium credits and "Data
  // attribution"; the overview must keep them visible and clickable.
  const rules = TRACKING_PORT_CSS.split('\n').filter((line) =>
    line.includes('#cesium-credits'),
  );
  assert.equal(rules.length, 1);
  const [rule] = rules;
  assert.ok(
    rule.includes(
      `body.${OVERVIEW_CLASS}.${ALARM_INSET_CLASS} #cesium-credits{`,
    ),
  );
  assert.ok(
    rule.endsWith(`{bottom:calc(var(${ALARM_INSET_VAR},0px) + 8px)!important}`),
  );
  assert.doesNotMatch(rule, /display|visibility|opacity/);
});

test('the overview hides GEV chrome, keyhole and cockpit HUD, never credits or alarms', () => {
  // The credits' lift above the narrow sheet (the one overview rule that
  // names them) is checked above; every other overview rule hides.
  const rules = TRACKING_PORT_CSS.split('\n').filter(
    (line) =>
      line.includes(OVERVIEW_CLASS) && !line.includes(ALARM_INSET_CLASS),
  );
  const selectors = rules.join('\n');
  for (const hidden of [
    '#title-bar',
    '#command-dock',
    '#left-panel-stack',
    '#right-context-rail',
    '#intel-hud',
    '#first-run-launcher',
    '#cockpit-hud',
    '#safe-frame-overlay',
    '#scope-mask',
    '.celestial-ring-overlay',
    '#cockpit-cloud-effects',
  ])
    assert.ok(selectors.includes(`${OVERVIEW_CLASS} ${hidden}`), hidden);
  assert.match(rules.at(-1), /\{display:none!important\}/);
  assert.doesNotMatch(
    selectors,
    /cesium-credits|uav-alarm-stack|cesiumContainer/,
  );
});

test('enterOverview switches the UAV and context layers on, never the cockpit', async () => {
  const h = overviewHarness();
  assert.equal(h.port.enterOverview(), true);
  assert.equal(h.body.classList.contains(OVERVIEW_CLASS), true);
  assert.equal(h.port.isOverview(), true);
  assert.deepEqual(h.context.active, [true]);
  assert.ok(h.head.children.some((el) => el.textContent === TRACKING_PORT_CSS));
  // The UAV layer waits for the data phase, then comes on once.
  assert.deepEqual(h.calls.enabled, []);
  h.port.attachData(h.dataManager);
  await flush();
  assert.deepEqual(
    h.calls.enabled.map(([id, on]) => [id, on]),
    [['uav', true]],
  );
  assert.equal(h.cockpit.active, false, 'the overview is not the cockpit');
  assert.deepEqual(h.calls.track, []);

  assert.equal(h.port.exitOverview(), true);
  assert.equal(h.body.classList.contains(OVERVIEW_CLASS), false);
  assert.deepEqual(h.context.active, [true, false]);
});

test('setOverlayVisibility forwards per-kind switches to the context layer', () => {
  const h = overviewHarness();
  assert.deepEqual(h.port.setOverlayVisibility({ sites: false }), {
    sites: false,
    forces: true,
  });
  assert.deepEqual(h.context.visibility, [{ sites: false }]);
  assert.equal(h.port.setOverlayVisibility(null), null);
});

test('onPick reports map picks only while in overview', () => {
  const h = overviewHarness();
  const picks = [];
  const off = h.port.onPick((pick) => picks.push(pick));
  assert.equal(typeof h.context.pickCb, 'function', 'subscribed once');
  h.context.pickCb({ id: 'sit:dyn-k:way/1' });
  assert.deepEqual(picks, [], 'not in overview yet');
  h.port.enterOverview();
  h.context.pickCb({ id: 'sit:dyn-k:way/1' });
  h.context.pickCb({ id: 'veh:Drone1' });
  h.context.pickCb({ id: '' });
  h.context.pickCb(null);
  assert.deepEqual(picks, [{ id: 'sit:dyn-k:way/1' }, { id: 'veh:Drone1' }]);
  off();
  h.context.pickCb({ id: 'veh:Drone1' });
  assert.equal(picks.length, 2);
});

test('supports() names what this build can do', () => {
  const h = overviewHarness();
  for (const name of [
    'showArea',
    'enterOverview',
    'exitOverview',
    'setOverlayVisibility',
    'onPick',
    'overlayStatus',
    'setViewportInset',
  ])
    assert.equal(h.port.supports(name), true, name);
  assert.equal(h.port.supports('teleport'), false);
  assert.deepEqual(h.port.overlayStatus(), {
    status: 'ok',
    sites: { drawn: 3 },
  });

  // A port with no camera and a plain UAV layer can't show areas.
  const bare = harness();
  assert.equal(bare.port.supports('showArea'), false);
  assert.equal(bare.port.supports('onPick'), false);
  assert.equal(bare.port.overlayStatus(), null);
  bare.port.destroy();
  h.port.destroy();
  assert.equal(h.port.supports('showArea'), false, 'nothing after destroy');
});

test('in the overview, bare GEV shortcut keys are held back as when hidden', () => {
  const h = overviewHarness();
  const key = (k) => ({
    key: k,
    stopped: false,
    stopPropagation() {
      this.stopped = true;
    },
  });
  const before = key('c');
  h.win.fire('keydown', before, { capture: true });
  assert.equal(before.stopped, false, 'tracking view: GEV keys work');
  h.port.enterOverview();
  const c = key('c');
  h.win.fire('keydown', c, { capture: true });
  assert.equal(c.stopped, true);
  const h1 = key('h');
  h.documentElement.fire('keydown', h1);
  assert.equal(h1.stopped, true);
  h.port.exitOverview();
  const after = key('h');
  h.documentElement.fire('keydown', after);
  assert.equal(after.stopped, false);
});

test('destroy leaves the overview and drops the pick subscription', () => {
  const h = overviewHarness();
  h.port.onPick(() => {});
  h.port.enterOverview();
  h.port.destroy();
  assert.equal(h.body.classList.contains(OVERVIEW_CLASS), false);
  assert.deepEqual(h.context.active, [true, false]);
  assert.equal(h.context.unsubscribed, 1);
  assert.equal(h.port.enterOverview(), false);
});

// ---- the deferred port's overview queue ---------------------------------------

/** A real-port stand-in that records every call in order. */
function recordingPort() {
  const calls = [];
  let pickCb = null;
  const port = {
    calls,
    emitPick: (pick) => pickCb?.(pick),
    whenReady: async () => {},
    enter: async () => true,
    exit() {},
    isTracking: () => false,
    onChange: () => () => {},
    setMapVisible: (v) => calls.push(['setMapVisible', v]),
    setViewportInset: (v) => calls.push(['setViewportInset', v]),
    openMissionPanel: () => true,
    keyhole: () => null,
    supports: (name) => name !== 'onPick',
    showArea(target, options) {
      calls.push(['showArea', target, options]);
      return Promise.resolve(true);
    },
    enterOverview: () => (calls.push(['enterOverview']), true),
    exitOverview: () => (calls.push(['exitOverview']), true),
    setOverlayVisibility: (kinds) => (
      calls.push(['setOverlayVisibility', kinds]),
      kinds
    ),
    onPick(cb) {
      pickCb = cb;
      return () => {
        pickCb = null;
      };
    },
    overlayStatus: () => ({ status: 'ok' }),
  };
  return port;
}

test('the deferred port queues overview calls and replays them in order', async () => {
  const deferred = createDeferredTrackingPort();
  assert.equal(deferred.supports('showArea'), true, 'queued, so supported');
  assert.equal(deferred.supports('teleport'), false);
  assert.equal(deferred.overlayStatus(), null);
  deferred.setMapVisible(false);
  const shown = deferred.showArea(KHERSON, { animate: false });
  assert.equal(deferred.enterOverview(), true);
  deferred.setOverlayVisibility({ sites: false });
  deferred.exitOverview();
  deferred.enterOverview();
  const picks = [];
  deferred.onPick((pick) => picks.push(pick));

  const real = recordingPort();
  deferred.attach(real);
  assert.deepEqual(real.calls, [
    ['setMapVisible', false],
    ['showArea', KHERSON, { animate: false }],
    ['enterOverview'],
    ['setOverlayVisibility', { sites: false }],
    ['exitOverview'],
    ['enterOverview'],
  ]);
  assert.equal(await shown, true);

  // After attach, calls go straight through; picks are forwarded.
  assert.equal(await deferred.showArea(KHERSON), true);
  assert.equal(real.calls.at(-1)[0], 'showArea');
  real.emitPick({ id: 'sit:dyn-k:way/1' });
  assert.deepEqual(picks, [{ id: 'sit:dyn-k:way/1' }]);
  assert.equal(deferred.supports('onPick'), false, 'the real port decides');
  assert.deepEqual(deferred.overlayStatus(), { status: 'ok' });

  deferred.attach(null);
  real.emitPick({ id: 'veh:Drone1' });
  assert.equal(picks.length, 1, 'detached ports are not heard');
});

test('a failed start answers queued overview calls false', async () => {
  const deferred = createDeferredTrackingPort();
  const shown = deferred.showArea(KHERSON);
  deferred.enterOverview();
  deferred.fail(new Error('Cesium could not start'));
  assert.equal(await shown, false);
  assert.equal(deferred.supports('showArea'), false);
  assert.equal(await deferred.showArea(KHERSON), false);
  assert.equal(deferred.enterOverview(), false);
});

// ---- the simulated wargame: the strip's top inset and the view (B16) -----------

test('a top inset (the wargame strip) lowers the map and recentres the keyhole rules', () => {
  const h = harness();
  h.port.setViewportInset({ right: 446, top: 27.6 });
  assert.equal(h.body.style.props.get(MAP_INSET_TOP_VAR), '28px');
  assert.equal(h.body.classList.contains(MAP_INSET_TOP_CLASS), true);
  assert.equal(h.body.classList.contains(MAP_INSET_CLASS), true);
  assert.equal(h.port.viewportInsetTop(), 28);
  assert.equal(h.port.viewportInset(), 446, 'the right inset is kept');
  const top = `var(${MAP_INSET_TOP_VAR},0px)`;
  const right = `var(${MAP_INSET_VAR},0px)`;
  const rules = TRACKING_PORT_CSS.split('\n').filter((rule) =>
    rule.includes(`body.${MAP_INSET_TOP_CLASS} `),
  );
  assert.ok(
    rules.includes(
      `html body.${MAP_INSET_TOP_CLASS} #cesiumContainer{height:auto;top:${top};bottom:0}`,
    ),
  );
  assert.ok(
    rules.includes(`html body.${MAP_INSET_TOP_CLASS} #cockpit-hud{top:${top}}`),
  );
  const rim = rules.find((rule) => rule.includes('.cockpit-altitude-rim{'));
  assert.ok(
    rim.includes(`calc((100vh - ${top}) * 0.52)`),
    'radius from the map height',
  );
  assert.ok(
    rim.includes(`calc((100vw - ${right}) * 0.4)`),
    'and the map width',
  );
  assert.ok(
    rim.includes(
      `top:calc(50vh - ${top} / 2 - var(--cockpit-keyhole-radius) + 20px)`,
    ),
  );
  const cloud = rules.find((rule) => rule.includes('#cockpit-cloud-effects{'));
  assert.ok(
    cloud.includes(`at calc(50vw - ${right} / 2) calc(50vh + ${top} / 2)`),
  );
  // The top rules come after the right-inset ones, so they win together.
  const css = TRACKING_PORT_CSS;
  assert.ok(
    css.indexOf(`body.${MAP_INSET_TOP_CLASS} .cockpit-altitude-rim{`) >
      css.indexOf(`body.${MAP_INSET_CLASS} .cockpit-altitude-rim{`),
  );
  // No top means 0: the strip is gone after the session.
  h.port.setViewportInset({ right: 446 });
  assert.equal(h.body.style.props.get(MAP_INSET_TOP_VAR), '0px');
  assert.equal(h.body.classList.contains(MAP_INSET_TOP_CLASS), false);
  h.port.setViewportInset({ top: 'x' });
  assert.equal(h.port.viewportInsetTop(), 0);
  h.port.setViewportInset({ top: 28 });
  h.port.destroy();
  assert.equal(h.body.classList.contains(MAP_INSET_TOP_CLASS), false);
  assert.equal(h.body.style.props.has(MAP_INSET_TOP_VAR), false);
});

test('setOverlayTruth sets the wargame view on the context layer (Blue view unless true)', () => {
  const h = overviewHarness();
  let truth = false;
  h.uavLayer.getContextStatus = () => ({ status: 'ok', truth });
  const setVisibility = h.uavLayer.setContextVisibility;
  h.uavLayer.setContextVisibility = (kinds) => {
    if (typeof kinds.truth === 'boolean') truth = kinds.truth;
    return setVisibility(kinds);
  };
  assert.equal(h.port.supports('setOverlayTruth'), true);
  assert.equal(h.port.setOverlayTruth(true), true);
  assert.deepEqual(h.context.visibility.at(-1), { truth: true });
  assert.equal(
    h.port.setOverlayTruth('yes'),
    false,
    'only true is Umpire view',
  );
  assert.deepEqual(h.context.visibility.at(-1), { truth: false });
  // A layer with its own setter is used directly.
  const direct = [];
  h.uavLayer.setContextTruth = (on) => (direct.push(on), on);
  assert.equal(h.port.setOverlayTruth(true), true);
  assert.deepEqual(direct, [true]);
  h.port.destroy();
  assert.equal(h.port.setOverlayTruth(true), null);
  const bare = harness();
  assert.equal(bare.port.supports('setOverlayTruth'), false);
  assert.equal(bare.port.setOverlayTruth(true), null);
  bare.port.destroy();
});

test('the deferred port remembers the view and applies it before the queued overview', () => {
  const deferred = createDeferredTrackingPort();
  assert.equal(deferred.supports('setOverlayTruth'), true);
  assert.equal(deferred.setOverlayTruth(false), null);
  deferred.enterOverview();
  assert.equal(deferred.setOverlayTruth(true), null, 'the latest wins');
  const real = recordingPort();
  real.setOverlayTruth = (on) => (real.calls.push(['setOverlayTruth', on]), on);
  deferred.attach(real);
  assert.deepEqual(real.calls, [['setOverlayTruth', true], ['enterOverview']]);
  assert.equal(deferred.setOverlayTruth(false), false);
  assert.deepEqual(real.calls.at(-1), ['setOverlayTruth', false]);
  // An older real port without the method: nothing breaks (Blue view).
  const older = createDeferredTrackingPort();
  older.setOverlayTruth(true);
  older.attach(recordingPort());
  assert.equal(older.setOverlayTruth(true), null);
});
