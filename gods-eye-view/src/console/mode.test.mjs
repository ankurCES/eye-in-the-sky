import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBus } from './bus.js';
import {
  IRIS_OPEN_MS,
  MAIN_FADE_MS,
  MODE_COPY,
  MODE_STATES,
  NOTICE_MS,
  STILL_STARTING_MS,
  createIris,
  createModeController,
} from './mode.js';

const flush = async (n = 4) => {
  for (let i = 0; i < n; i += 1) await new Promise((r) => setImmediate(r));
};

function fakeClock(start = 5_000_000) {
  let now = start;
  let seq = 0;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) {
      const id = ++seq;
      timers.set(id, { at: now + Math.max(0, Number(ms) || 0), fn });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    size: () => timers.size,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) {
          if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        }
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

function fakePort({ enter = true, keyhole = { x: 500, y: 400, r: 180 } } = {}) {
  const calls = [];
  const cbs = new Set();
  let tracking = false;
  let pending = null;
  return {
    calls,
    whenReady() {
      calls.push(['whenReady']);
      return Promise.resolve();
    },
    enter(v) {
      calls.push(['enter', v]);
      if (enter === 'never') return new Promise(() => {});
      if (enter === 'manual') {
        return new Promise((resolve) => {
          pending = (ok) => {
            tracking = ok;
            resolve(ok);
          };
        });
      }
      tracking = Boolean(enter);
      return Promise.resolve(enter);
    },
    exit() {
      calls.push(['exit']);
      tracking = false;
      for (const cb of [...cbs]) cb({ active: false });
    },
    isTracking: () => tracking,
    onChange(cb) {
      cbs.add(cb);
      return () => cbs.delete(cb);
    },
    setMapVisible: (b) => calls.push(['setMapVisible', b]),
    openMissionPanel: () => calls.push(['openMissionPanel']),
    setViewportInset: (i) => calls.push(['setViewportInset', i]),
    keyhole: () => keyhole,
    /** GEV's own Esc / `c` / map-view switch. */
    gevExit() {
      tracking = false;
      for (const cb of [...cbs]) cb({ active: false });
    },
    resolve: (ok) => pending?.(ok),
    names: () => calls.map((c) => c[0]),
  };
}

function attrEl() {
  return {
    attrs: {},
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
  };
}

function setup(opts = {}) {
  const clock = fakeClock();
  const bus = createBus();
  const port = fakePort(opts.port);
  const store = {
    lastStageInputAt: opts.lastInput ?? 0,
    vehicle: (name) => (opts.vehicles || {})[name] ?? null,
  };
  const root = attrEl();
  const orbCalls = [];
  const orb = {
    select: (id) => orbCalls.push(['select', id]),
    project: () => ({ x: 100, y: 120, front: true }),
    setOptions: (o) => orbCalls.push(['setOptions', o]),
  };
  const announced = [];
  const irisCalls = [];
  const iris = {
    open: async (from, to, o) => irisCalls.push(['open', from, to, o]),
    close: async (from, to, o) => irisCalls.push(['close', from, to, o]),
    reset: () => irisCalls.push(['reset']),
  };
  let idle = true;
  const mode = createModeController({
    trackingPort: port,
    bus,
    store,
    root,
    orb,
    isComposerIdle: () => idle,
    announce: (t, p) => announced.push([t, p]),
    viewportInset: () => ({ right: 446 }),
    stageCentre: () => ({ x: 700, y: 450 }),
    reducedMotion: () => Boolean(opts.reduced),
    clock,
    iris,
    doc: opts.doc ?? null,
  });
  const modes = [];
  bus.on('mode', (p) => modes.push(p.mode));
  let notices = [];
  mode.onNotice((list) => {
    notices = list;
  });
  return {
    clock,
    bus,
    port,
    store,
    root,
    orbCalls,
    announced,
    irisCalls,
    mode,
    modes,
    notices: () => notices,
    setIdle: (v) => {
      idle = v;
    },
  };
}

const GRID_VEHICLE = {
  Drone1: {
    missionNode: {
      label: 'Grid search · Drone1',
      attrs: { kind: 'grid_search' },
    },
  },
};

test('states are the contract names verbatim', () => {
  assert.deepEqual(MODE_STATES, [
    'orb',
    'entering_tracking',
    'tracking',
    'exiting',
  ]);
});

test("an operator's Track acts at once: ready, map, inset, enter, iris, tracking", async () => {
  const t = setup();
  assert.equal(t.root.attrs['data-mode'], 'orb');
  assert.equal(
    t.mode.requestTrack('Drone1', { source: 'operator' }),
    'entering',
  );
  assert.equal(t.mode.state, 'entering_tracking');
  assert.equal(t.mode.vehicle, 'Drone1');
  assert.equal(t.notices()[0].text, MODE_COPY.opening('Drone1'));
  await flush();
  assert.deepEqual(t.port.calls, [
    ['whenReady'],
    ['setMapVisible', true],
    ['setViewportInset', { right: 446 }],
    ['enter', 'Drone1'],
  ]);
  assert.deepEqual(t.irisCalls[0], [
    'open',
    { x: 100, y: 120, r: 8 },
    { x: 500, y: 400, r: 180 },
    { reduced: false },
  ]);
  assert.equal(t.mode.state, 'tracking');
  assert.equal(t.root.attrs['data-mode'], 'tracking');
  assert.deepEqual(t.modes, ['entering_tracking', 'tracking']);
  assert.deepEqual(t.orbCalls, [
    ['select', 'veh:Drone1'],
    ['setOptions', { paused: true }],
  ]);
  assert.deepEqual(t.announced.at(-1), [
    MODE_COPY.trackingAnnounce('Drone1'),
    'polite',
  ]);
  assert.deepEqual(t.notices(), [], 'the progress caption is gone');
});

test('Back to console runs the exit: iris closes, then exit, map hidden, inset cleared', async () => {
  const t = setup();
  t.mode.requestTrack('Drone1');
  await flush();
  t.port.calls.length = 0;
  await t.mode.exit();
  await flush();
  assert.deepEqual(t.modes.slice(-2), ['exiting', 'orb']);
  assert.equal(t.irisCalls.at(-2)[0], 'close');
  assert.deepEqual(t.irisCalls.at(-2).slice(1, 3), [
    { x: 500, y: 400, r: 180 },
    { x: 100, y: 120, r: 8 },
  ]);
  assert.deepEqual(t.port.calls, [
    ['exit'],
    ['setMapVisible', false],
    ['setViewportInset', { right: 0 }],
  ]);
  assert.equal(t.mode.state, 'orb');
  assert.equal(t.mode.vehicle, null);
  assert.deepEqual(t.orbCalls.slice(-2), [
    ['setOptions', { paused: false }],
    ['select', 'veh:Drone1'],
  ]);
});

test("GEV's own exit (Esc, c, map switch) returns the console to the orb and untracks", async () => {
  const t = setup();
  t.mode.requestTrack('Drone1');
  await flush();
  t.port.calls.length = 0;
  t.port.gevExit();
  await flush();
  assert.equal(t.mode.state, 'orb');
  assert.deepEqual(t.port.names(), [
    'exit',
    'setMapVisible',
    'setViewportInset',
  ]);
});

test('an analyst request counts down 3 s before moving the view', async () => {
  const t = setup();
  const outcome = t.mode.requestTrack('Drone1', {
    source: 'analyst',
    reason: 'good view of the AO',
  });
  assert.equal(outcome, 'countdown');
  assert.equal(t.mode.state, 'orb');
  const n = t.notices()[0];
  assert.equal(n.kind, 'countdown');
  assert.equal(
    n.text,
    'The analyst suggests watching Drone1: "good view of the AO". Opening Drone1\'s camera in 3 s.',
  );
  assert.deepEqual(
    n.actions.map((a) => a.label),
    ['Stay in console'],
  );
  await t.clock.advance(1000);
  assert.match(t.notices()[0].text, /in 2 s\.$/);
  assert.equal(t.port.calls.length, 0);
  await t.clock.advance(NOTICE_MS - 1000);
  assert.equal(t.mode.state, 'tracking');
  assert.deepEqual(
    t.port.calls.find((c) => c[0] === 'enter'),
    ['enter', 'Drone1'],
  );
});

test('a launch notice names the mission from the graph', async () => {
  const t = setup({ vehicles: GRID_VEHICLE });
  t.mode.requestTrack('Drone1', {
    source: 'launch',
    reason: 'mission launched',
  });
  assert.equal(
    t.notices()[0].text,
    "Launched: Grid search with Drone1. Opening Drone1's camera in 3 s.",
  );
  assert.equal(t.notices()[0].source, 'launch');
});

test('a launch notice names an orbit in words, not "Orbit poi"', () => {
  const t = setup({
    vehicles: {
      Drone1: {
        missionNode: {
          label: 'Orbit poi · Drone1',
          attrs: { kind: 'orbit_poi' },
        },
      },
    },
  });
  t.mode.requestTrack('Drone1', { source: 'launch' });
  assert.equal(
    t.notices()[0].text,
    "Launched: Orbit with Drone1. Opening Drone1's camera in 3 s.",
  );
});

test('Stay in console (or Esc) cancels the countdown; nothing moves', async () => {
  const t = setup();
  t.mode.requestTrack('Drone1', { source: 'launch' });
  t.notices()[0].actions[0].run();
  await t.clock.advance(NOTICE_MS + 10);
  assert.equal(t.mode.state, 'orb');
  assert.equal(t.port.calls.length, 0);

  t.mode.requestTrack('Drone1', { source: 'analyst' });
  assert.equal(t.mode.cancelNotice(), true);
  assert.equal(t.mode.cancelNotice(), false);
  await t.clock.advance(NOTICE_MS + 10);
  assert.equal(t.port.calls.length, 0);
});

test('a busy composer, recent stage input or a pending slip makes the notice static', async () => {
  const busy = setup({ vehicles: GRID_VEHICLE });
  busy.setIdle(false);
  assert.equal(
    busy.mode.requestTrack('Drone1', { source: 'launch' }),
    'static',
  );
  const n = busy.notices()[0];
  assert.equal(n.text, 'Launched: Grid search with Drone1.');
  assert.deepEqual(
    n.actions.map((a) => a.label),
    ['Track Drone1'],
  );
  await busy.clock.advance(NOTICE_MS + 10);
  assert.equal(busy.mode.state, 'orb');
  n.actions[0].run(); // the operator's own click acts at once
  await flush();
  assert.equal(busy.mode.state, 'tracking');

  const stage = setup();
  stage.store.lastStageInputAt = stage.clock.now() - 1000;
  assert.equal(
    stage.mode.requestTrack('Drone1', { source: 'analyst', reason: 'x' }),
    'static',
  );
  assert.equal(
    stage.notices()[0].text,
    'The analyst suggests watching Drone1: "x".',
  );

  const slip = setup();
  slip.bus.emit('approval:pending', { count: 1 });
  assert.equal(slip.mode.pendingApprovals, 1);
  assert.equal(
    slip.mode.requestTrack('Drone1', { source: 'analyst' }),
    'static',
  );
  slip.bus.emit('approval:pending', { count: 0 });
  assert.equal(
    slip.mode.requestTrack('Drone2', { source: 'analyst' }),
    'countdown',
  );
});

test('input during the countdown turns it static at expiry instead of moving', async () => {
  const t = setup();
  t.mode.requestTrack('Drone1', { source: 'analyst' });
  await t.clock.advance(1500);
  t.setIdle(false);
  await t.clock.advance(2000);
  assert.equal(t.mode.state, 'orb');
  assert.equal(t.port.calls.length, 0);
  assert.equal(t.notices()[0].kind, 'static');
  assert.equal(t.notices()[0].actions[0].label, 'Track Drone1');
});

test("a second vehicle's launch never switches the view", async () => {
  const t = setup();
  t.mode.requestTrack('Drone1');
  await flush();
  assert.equal(t.mode.requestTrack('Drone2', { source: 'launch' }), 'static');
  await t.clock.advance(NOTICE_MS + 10);
  assert.equal(t.mode.state, 'tracking');
  assert.equal(t.mode.vehicle, 'Drone1');
  assert.equal(t.port.calls.filter((c) => c[0] === 'enter').length, 1);
  // The same vehicle again is a no-op.
  assert.equal(t.mode.requestTrack('Drone1', { source: 'launch' }), 'ignored');

  const pending = setup();
  pending.mode.requestTrack('Drone1', { source: 'launch' });
  assert.equal(
    pending.mode.requestTrack('Drone2', { source: 'launch' }),
    'static',
  );
  await pending.clock.advance(NOTICE_MS + 10);
  assert.equal(pending.mode.vehicle, 'Drone1');
});

test('after 20 s without an answer the caption says the map is still starting', async () => {
  const t = setup({ port: { enter: 'never' } });
  t.mode.requestTrack('Drone1');
  await flush();
  await t.clock.advance(STILL_STARTING_MS);
  const n = t.notices()[0];
  assert.equal(n.kind, 'progress');
  assert.equal(n.text, MODE_COPY.stillStarting);
  assert.equal(n.detail, '20 s');
  assert.equal(t.mode.state, 'entering_tracking');
  n.actions[0].run(); // Stay in console
  assert.equal(t.mode.state, 'orb');
  assert.deepEqual(t.port.calls.slice(-2), [
    ['setMapVisible', false],
    ['setViewportInset', { right: 0 }],
  ]);
  assert.ok(
    !t.port.names().includes('exit'),
    'nothing to undo: GEV never entered',
  );
});

test('enter() false hides the map again and offers Try again', async () => {
  const t = setup({ port: { enter: false } });
  t.mode.requestTrack('Drone1');
  await flush();
  assert.equal(t.mode.state, 'orb');
  assert.deepEqual(t.port.calls.slice(-2), [
    ['setMapVisible', false],
    ['setViewportInset', { right: 0 }],
  ]);
  const n = t.notices()[0];
  assert.equal(n.kind, 'error');
  assert.equal(
    n.text,
    "Couldn't lock on to Drone1. The map is running, but Drone1 isn't in view yet.",
  );
  assert.deepEqual(
    n.actions.map((a) => a.label),
    ['Try again', 'Stay in console'],
  );
  assert.equal(t.announced.at(-1)[1], 'assertive');
  n.actions[0].run();
  await flush();
  assert.equal(t.port.calls.filter((c) => c[0] === 'enter').length, 2);
});

test('cancelling while GEV works undoes an entry that lands late', async () => {
  const t = setup({ port: { enter: 'manual' } });
  t.mode.requestTrack('Drone1');
  await flush();
  t.mode.cancel();
  assert.equal(t.mode.state, 'orb');
  t.port.resolve(true);
  await flush();
  assert.equal(t.mode.state, 'orb');
  assert.equal(t.port.names().at(-1), 'exit');
  assert.equal(t.irisCalls.filter((c) => c[0] === 'open').length, 0);
});

test("the analyst's return to the orb is a 3 s notice; a bare track:exit is immediate", async () => {
  const t = setup();
  t.mode.requestTrack('Drone1');
  await flush();
  t.bus.emit('track:exit', { source: 'analyst' });
  const n = t.notices()[0];
  assert.equal(
    n.text,
    'The analyst suggests returning to the orb. Returning in 3 s.',
  );
  assert.deepEqual(
    n.actions.map((a) => a.label),
    ['Stay here'],
  );
  await t.clock.advance(NOTICE_MS);
  assert.equal(t.mode.state, 'orb');

  const direct = setup();
  direct.mode.requestTrack('Drone1');
  await flush();
  direct.bus.emit('track:exit', {});
  await flush();
  assert.equal(direct.mode.state, 'orb');

  const busy = setup();
  busy.mode.requestTrack('Drone1');
  await flush();
  busy.setIdle(false);
  assert.equal(busy.mode.requestOrb({ source: 'analyst' }), 'static');
  assert.equal(busy.notices()[0].text, MODE_COPY.orbSuggests);
  assert.equal(busy.notices()[0].actions[0].label, 'Back to console');
});

test('track:request from the bus, including Open mission panel', async () => {
  const t = setup();
  t.bus.emit('track:request', {
    vehicle: 'Drone1',
    source: 'operator',
    openMissionPanel: true,
  });
  await flush();
  assert.equal(t.mode.state, 'tracking');
  assert.equal(t.port.names().at(-1), 'openMissionPanel');
  t.bus.emit('track:request', {
    vehicle: 'Drone1',
    source: 'operator',
    openMissionPanel: true,
  });
  assert.equal(
    t.port.names().filter((n) => n === 'openMissionPanel').length,
    2,
  );
});

test('the operator switching aircraft leaves the old cockpit without bouncing to the orb', async () => {
  const t = setup();
  t.mode.requestTrack('Drone1');
  await flush();
  t.mode.requestTrack('Drone2');
  await flush();
  assert.equal(t.mode.state, 'tracking');
  assert.equal(t.mode.vehicle, 'Drone2');
  assert.deepEqual(t.modes, [
    'entering_tracking',
    'tracking',
    'entering_tracking',
    'tracking',
  ]);
});

test('reduced motion swaps the iris for a crossfade (the flag reaches the effect)', async () => {
  const t = setup({ reduced: true });
  t.mode.requestTrack('Drone1');
  await flush();
  assert.deepEqual(t.irisCalls[0][3], { reduced: true });
});

test('destroy stops every timer and listener', async () => {
  const t = setup();
  t.mode.requestTrack('Drone1', { source: 'analyst' });
  t.mode.destroy();
  assert.equal(t.clock.size(), 0);
  t.bus.emit('track:request', { vehicle: 'Drone2', source: 'operator' });
  await flush();
  assert.equal(t.port.calls.length, 0);
});

// ---- the iris effect itself ---------------------------------------------------------

function fakeMain() {
  const props = {};
  return {
    attrs: {},
    props,
    style: {
      setProperty: (k, v) => {
        props[k] = v;
      },
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    getBoundingClientRect: () => ({ left: 248, top: 0 }),
  };
}

test('createIris opens from the node to the keyhole, then fades .ic-main', async () => {
  const clock = fakeClock();
  const main = fakeMain();
  const iris = createIris({
    main,
    clock,
    raf: (fn) => clock.setTimeout(fn, 16),
  });
  const done = iris.open({ x: 348, y: 120, r: 8 }, { x: 748, y: 400, r: 180 });
  assert.equal(main.attrs['data-iris'], '');
  assert.deepEqual(main.props, {
    '--ic-iris-x': '100px',
    '--ic-iris-y': '120px',
    '--ic-iris-r': '8px',
  });
  await clock.advance(IRIS_OPEN_MS + 32);
  assert.deepEqual(main.props, {
    '--ic-iris-x': '500px',
    '--ic-iris-y': '400px',
    '--ic-iris-r': '180px',
  });
  assert.equal(main.attrs['data-fade'], 'out');
  await clock.advance(MAIN_FADE_MS);
  await done;
  assert.equal(main.attrs.inert, '');

  const closed = iris.close(
    { x: 748, y: 400, r: 180 },
    { x: 348, y: 120, r: 8 },
  );
  await clock.advance(MAIN_FADE_MS + IRIS_OPEN_MS);
  await closed;
  assert.equal(main.attrs['data-fade'], undefined);
  assert.equal(main.attrs.inert, undefined);
  assert.equal(main.attrs['data-iris'], undefined);
  assert.equal(main.props['--ic-iris-r'], '0px');
});

test('createIris under reduced motion only crossfades; with no main it is a no-op', async () => {
  const clock = fakeClock();
  const main = fakeMain();
  const iris = createIris({ main, clock, raf: null });
  const done = iris.open(
    { x: 0, y: 0, r: 8 },
    { x: 1, y: 1, r: 9 },
    { reduced: true },
  );
  assert.equal(main.attrs['data-iris'], undefined);
  assert.equal(main.attrs['data-fade'], 'out');
  await clock.advance(200);
  await done;
  assert.equal(main.attrs.inert, '');
  iris.reset();
  assert.deepEqual(main.attrs, {});
  await createIris({ main: null, clock }).open(
    { x: 0, y: 0, r: 0 },
    { x: 0, y: 0, r: 0 },
  );
});

test('GEV leaving its cockpit during the iris returns to the orb with Try again, never a stale tracking state', async () => {
  const t = setup();
  // The iris runs while GEV drops the cockpit (its own Esc, or a cockpit
  // that cannot hold the aircraft); onChange is ignored until `tracking`.
  let during = null;
  const iris = {
    open: async () => {
      t.port.gevExit();
      during = mode.state;
    },
    close: async () => {},
    reset: () => {},
  };
  const mode = createModeController({
    trackingPort: t.port,
    bus: createBus(),
    store: t.store,
    root: attrEl(),
    orb: { select() {}, project: () => null, setOptions() {} },
    clock: t.clock,
    iris,
    doc: null,
  });
  let notices = [];
  mode.onNotice((list) => {
    notices = list;
  });
  mode.requestTrack('Drone1', { source: 'operator' });
  await flush(8);
  assert.equal(during, 'entering_tracking');
  assert.equal(mode.state, 'orb');
  assert.deepEqual(t.port.calls.at(-2), ['setMapVisible', false]);
  const fail = notices.find((n) => n.kind === 'error');
  assert.equal(fail?.text, MODE_COPY.lockFailed('Drone1'));
  assert.deepEqual(
    fail.actions.map((a) => a.label),
    [MODE_COPY.tryAgain, MODE_COPY.stay],
  );
  mode.destroy();
});

// ---- focus returns to the control that started tracking (a11y review) -------------

function miniDoc() {
  const doc = { activeElement: null };
  const mk = (tag, key) => {
    const el = {
      tag,
      children: [],
      attrs: key ? { 'data-key': key } : {},
      parent: null,
      append(...kids) {
        for (const k of kids) {
          k.parent = this;
          this.children.push(k);
        }
      },
      getAttribute(k) {
        return k in this.attrs ? this.attrs[k] : null;
      },
      focus() {
        doc.activeElement = this;
      },
    };
    return el;
  };
  doc.mk = mk;
  doc.body = mk('body');
  return doc;
}

test('leaving tracking puts focus back on the Track control, re-rendered or not', async () => {
  const doc = miniDoc();
  const rail = doc.mk('div');
  doc.body.append(rail);
  const first = doc.mk('button', 'track:Drone1');
  rail.append(first);
  first.focus();
  const t = setup({ doc });
  t.mode.requestTrack('Drone1', { source: 'operator' });
  await flush();
  assert.equal(t.mode.state, 'tracking');
  // Polls rebuilt the rail while the map was up: a new node, same key.
  rail.children = [];
  first.isConnected = false;
  const again = doc.mk('button', 'track:Drone1');
  rail.append(again);
  doc.activeElement = doc.body; // the dock's Back went with the dock
  await t.mode.exit();
  await flush();
  assert.equal(t.mode.state, 'orb');
  assert.equal(doc.activeElement, again);
});

test('with the opener gone for good, focus lands on the reopened inspector Track', async () => {
  const doc = miniDoc();
  const opener = doc.mk('button', 'track:Drone1');
  doc.body.append(opener);
  opener.focus();
  const t = setup({ doc });
  t.mode.requestTrack('Drone1', { source: 'operator' });
  await flush();
  doc.body.children = [];
  opener.isConnected = false;
  // exit() emits `inspect`; the inspector renders its actions for Drone1.
  t.bus.on('inspect', () => doc.body.append(doc.mk('button', 'act:track')));
  await t.mode.exit();
  await flush();
  assert.equal(doc.activeElement?.attrs['data-key'], 'act:track');
});

test('Stay in console returns focus to the Track control when the caption took it along', async () => {
  const doc = miniDoc();
  const opener = doc.mk('button', 'act:track');
  doc.body.append(opener);
  opener.focus();
  const t = setup({ doc, port: { enter: 'never' } });
  t.mode.requestTrack('Drone1', { source: 'operator' });
  await flush();
  assert.equal(t.mode.state, 'entering_tracking');
  doc.activeElement = doc.body; // "Stay in console" was removed with the caption
  t.notices()[0].actions[0].run();
  assert.equal(t.mode.state, 'orb');
  assert.equal(doc.activeElement, opener);
});
