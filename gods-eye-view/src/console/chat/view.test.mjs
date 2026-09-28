import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  COPY,
  assumedCaveats,
  caretHost,
  createAnalyst,
  detectionsFeed,
  emptyHeading,
  flyingNow,
  mapLineText,
  resolveMapArea,
  shouldAutoTrack,
  shouldShowMap,
  suggestedPrompts,
} from './view.js';
import { ARM_MS, DBLCLICK_MS } from './slip.js';
import { createChip } from './chips.js';

// ---- stub DOM -------------------------------------------------------------------------

function stubDoc() {
  const doc = {
    activeElement: null,
    hidden: false,
    listeners: {},
    createElement: (tag) => makeEl(tag, doc),
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
    removeEventListener(t, f) {
      this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f);
    },
  };
  doc.body = makeEl('body', doc);
  return doc;
}

function makeEl(tag, doc) {
  return {
    tag,
    tagName: tag.toUpperCase(),
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    _text: '',
    get textContent() {
      return this._text;
    },
    set textContent(v) {
      this.children = [];
      this._text = String(v ?? '');
    },
    value: '',
    checked: false,
    rect: { top: 100, left: 20, width: 400, height: 300 },
    append(...kids) {
      this.children.push(...kids);
    },
    replaceChildren(...kids) {
      this._text = '';
      this.children = [...kids];
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    getAttribute(k) {
      return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null;
    },
    removeAttribute(k) {
      delete this.attrs[k];
    },
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
    fire(t, e = {}) {
      const ev = {
        type: t,
        target: this,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
        ...e,
      };
      for (const f of [...(this.listeners[t] || [])]) f(ev);
      return ev;
    },
    focus() {
      doc.activeElement = this;
    },
    blur() {
      if (doc.activeElement === this) doc.activeElement = null;
    },
    remove() {},
    getBoundingClientRect() {
      return { ...this.rect };
    },
    scrollIntoView() {},
  };
}

const cls = (name) => (el) =>
  String(el?.className || '')
    .split(/\s+/)
    .includes(name);

function find(root, pred) {
  if (!root || typeof root !== 'object') return null;
  if (pred(root)) return root;
  for (const kid of root.children || []) {
    const hit = find(kid, pred);
    if (hit) return hit;
  }
  return null;
}

function findAll(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const kid of root.children || []) findAll(kid, pred, out);
  return out;
}

function textOf(node) {
  if (node == null || node === false) return '';
  if (typeof node === 'string') return node;
  return (node._text || '') + (node.children || []).map(textOf).join('');
}

const isHidden = (el) => Object.hasOwn(el.attrs, 'hidden');
const buttonNamed = (root, text) =>
  find(root, (el) => el.tag === 'button' && textOf(el).includes(text));

// ---- fakes ------------------------------------------------------------------------------

const T0 = Date.UTC(2026, 8, 27, 14, 2, 0);

function fakeClock(start = T0) {
  let t = start;
  let id = 0;
  const timers = new Map();
  const add = (fn, ms, every) => {
    id += 1;
    timers.set(id, { fn, at: t + ms, every });
    return id;
  };
  return {
    now: () => t,
    set: (v) => {
      t = v;
    },
    setTimeout: (fn, ms) => add(fn, ms, null),
    clearTimeout: (i) => timers.delete(i),
    setInterval: (fn, ms) => add(fn, ms, ms),
    clearInterval: (i) => timers.delete(i),
    advance(ms) {
      const end = t + ms;
      for (;;) {
        let next = null;
        for (const [k, v] of timers)
          if (v.at <= end && (!next || v.at < next[1].at)) next = [k, v];
        if (!next) break;
        t = next[1].at;
        if (next[1].every) next[1].at += next[1].every;
        else timers.delete(next[0]);
        next[1].fn();
      }
      t = end;
    },
  };
}

function emitter() {
  const listeners = new Map();
  return {
    on(ev, cb) {
      if (!listeners.has(ev)) listeners.set(ev, new Set());
      listeners.get(ev).add(cb);
      return () => listeners.get(ev)?.delete(cb);
    },
    emit(ev, ...args) {
      for (const cb of [...(listeners.get(ev) || [])]) cb(...args);
    },
    count: (ev) => listeners.get(ev)?.size ?? 0,
  };
}

function fakeChat(
  clock,
  { status = { available: true, model: 'claude-x' } } = {},
) {
  const em = emitter();
  const calls = [];
  let sid = null;
  const chat = {
    calls,
    sendError: null,
    on: em.on,
    emit: em.emit,
    count: em.count,
    async status() {
      calls.push(['status']);
      em.emit('status', chat.statusValue);
      return chat.statusValue;
    },
    statusValue: status,
    async open() {
      calls.push(['open']);
      sid = 's1';
      return sid;
    },
    async send(text, ctx) {
      calls.push(['send', text, ctx]);
      if (chat.sendError) throw chat.sendError;
      return { turn_id: 't-x' };
    },
    async approve(id, decision, note) {
      calls.push(['approve', id, decision, note]);
      return { ok: true };
    },
    async interrupt() {
      calls.push(['interrupt']);
      em.emit('interrupt', { at: clock.now() });
      return { ok: true };
    },
    async grants() {
      return null;
    },
    async revokeGrant() {
      return { ok: true };
    },
    async newSession() {
      calls.push(['newSession']);
      em.emit('session:replaced', { reason: 'operator', from: 's1', to: 's2' });
    },
    get sessionId() {
      return sid;
    },
    /** Deliver an SSE event as the client does. */
    event(name, data, seq, { replay = false, at } = {}) {
      em.emit('event', {
        name,
        data,
        seq,
        at: replay ? null : (at ?? clock.now()),
        replay,
      });
    },
  };
  return chat;
}

function graphFixture(extra = {}) {
  return {
    theater: { id: 'default', label: 'Redmond (AirSim default)' },
    nodes: [
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        label: 'Drone1',
        status: 'ok',
        lat: 47.6445,
        lon: -122.1402,
        attrs: {
          fuel_pct: 82,
          bingo_fuel_pct: 26.4,
          landed: true,
          bingo_latched: false,
          link: 'up',
          agl_m: 0,
          agl_is_real: false,
        },
      },
      {
        id: 'poi:default:North Field',
        type: 'poi',
        label: 'North Field',
        status: 'ok',
        attrs: {},
      },
      {
        id: 'trk:T-1',
        type: 'track',
        label: 'SA-6 battery',
        status: 'critical',
        attrs: {},
      },
      {
        id: 'trk:T-2',
        type: 'track',
        label: 'Radar vehicle',
        status: 'unknown',
        attrs: {},
      },
      ...(extra.nodes || []),
    ],
    meta: {
      caveats: [
        'Real-data layer is off: AGL is height above launch datum; LOS is geometric.',
      ],
      feeds: {
        real_data: { ok: false },
        detections: { ok: true, at_ms: T0 - 3000 },
      },
      threat_assessed: 1,
      threat_unassessed: 1,
      ...(extra.meta || {}),
    },
  };
}

function fakeStore(graph) {
  const em = emitter();
  const store = {
    graph,
    lastStageInputAt: 0,
    get() {
      const byId = new Map(store.graph.nodes.map((n) => [n.id, n]));
      return { graph: store.graph, byId, status: 'live' };
    },
    on: em.on,
    change() {
      em.emit('change', { graph: true });
    },
  };
  return store;
}

async function mountView({
  graph = graphFixture(),
  status,
  modeState = 'orb',
  modeVehicle = null,
  liveMotion = false,
  raf = null,
} = {}) {
  const doc = stubDoc();
  globalThis.document = doc;
  const clock = fakeClock();
  const chat = fakeChat(clock, status ? { status } : undefined);
  const store = fakeStore(graph);
  const bus = emitter();
  const emitted = [];
  const realEmit = bus.emit;
  bus.emit = (ev, payload) => {
    emitted.push([ev, payload]);
    realEmit(ev, payload);
  };
  const highlights = [];
  const orb = {
    highlight: (ids, opts) => highlights.push([ids, opts]),
    project: () => ({ x: 10, y: 10, front: true }),
    onFrame: () => () => {},
  };
  const mode = { state: modeState, vehicle: modeVehicle };
  const announced = [];
  const host = makeEl('div', doc);
  const root = makeEl('div', doc);
  const view = createAnalyst(
    host,
    {
      chat,
      store,
      bus,
      orb,
      mode,
      root,
      announce: (text, politeness) => announced.push([text, politeness]),
    },
    liveMotion
      ? { doc, now: clock.now, clock, raf }
      : { doc, now: clock.now, clock, raf, reducedMotion: false },
  );
  await flush();
  return {
    view,
    doc,
    clock,
    chat,
    store,
    bus,
    emitted,
    highlights,
    host,
    mode,
    announced,
    orb,
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

let seq = 0;
function turn(chat, text = 'Which contacts?') {
  seq = 0;
  chat.event(
    'session',
    { session_id: 's1', model: 'claude-x', last_seq: 0 },
    0,
  );
  chat.event('turn_start', { turn_id: 't1', text }, ++seq);
}
const next = () => ++seq;

// ---- mounting and empty state ------------------------------------------------------------

test('mounting checks status, opens the session and shows the empty state', async () => {
  const { chat, host, emitted } = await mountView();
  assert.deepEqual(
    chat.calls.map((c) => c[0]),
    ['status', 'open'],
  );
  const text = textOf(host);
  assert.ok(
    text.includes(
      'Redmond (AirSim default). Drone1 is on the ground with 82% fuel. 2 contacts in the picture.',
    ),
  );
  assert.ok(text.includes('How approvals work'));
  assert.ok(text.includes(COPY.approvalsBody));
  for (const phrase of [
    'Tasks a sensor',
    'Commands an aircraft',
    'Changes the simulation',
    'Safety override',
  ])
    assert.ok(text.includes(phrase), phrase);
  assert.deepEqual(
    emitted.find(([ev]) => ev === 'analyst:availability'),
    ['analyst:availability', { available: true, reason: null, hint: null }],
  );
  assert.ok(text.includes('Standing approval: none'));
});

test('suggested prompts insert their exact text, never send', async () => {
  const { host, chat } = await mountView();
  const prompts = findAll(host, cls('ic-prompt'));
  // Contacts exist and one is assessed: both swap-ins apply, so the fuel
  // prompt gives way to stay within five.
  assert.deepEqual(prompts.map(textOf), [
    "Summarize the situation in this theater and what we don't know yet.",
    'Plan a grid search over North Field at 60 m AGL and show me the dry run.',
    'Which readings are assumed while the real-data layer is off?',
    "Which contacts haven't been threat-assessed yet?",
    'Write an INTREP on the assessed contacts and call out the gaps.',
  ]);
  prompts[1].fire('click');
  const textarea = find(host, (el) => el.tag === 'textarea');
  assert.equal(
    textarea.value,
    'Plan a grid search over North Field at 60 m AGL and show me the dry run.',
  );
  assert.equal(chat.calls.filter((c) => c[0] === 'send').length, 0);
});

// ---- transcript -------------------------------------------------------------------------------

test('a turn renders as a watch log with Zulu times and chips', async () => {
  const { chat, host, highlights, emitted } = await mountView();
  turn(chat, 'Tell me about [[trk:T-1|SA-6 battery]]');
  chat.event(
    'text_delta',
    {
      turn_id: 't1',
      text: 'Two contacts: [[trk:T-1|SA-6 battery]] and [[trk:T-9]].',
    },
    next(),
  );
  const articles = findAll(host, (el) => el.tag === 'article');
  assert.equal(articles.length, 2);
  assert.equal(articles[0].attrs['aria-label'], 'You, 14:02Z');
  assert.equal(articles[1].attrs['aria-label'], 'Analyst, 14:02Z');
  assert.equal(articles[1].attrs['aria-busy'], 'true');
  const log = find(host, (el) => el.attrs?.role === 'log');
  assert.equal(
    log.attrs['aria-live'],
    'off',
    'no token-by-token announcements',
  );
  const chips = findAll(articles[1], cls('ic-chip'));
  assert.equal(chips.length, 2);
  assert.equal(chips[0].attrs['data-id'], 'trk:T-1');
  assert.equal(
    chips[0].attrs.tabindex,
    '0',
    'roving: the first chip is the tab stop',
  );
  assert.equal(chips[1].attrs.tabindex, '-1');
  assert.equal(chips[1].attrs['data-missing'], 'true');
  assert.equal(
    chips[1].attrs.title,
    'Not in the current picture (outside this theater or aged out).',
  );
  assert.ok(find(chips[0], cls('ic-chip__glyph')).innerHTML.startsWith('<svg'));
  chips[0].fire('mouseenter');
  assert.deepEqual(highlights.at(-1), [['trk:T-1'], { by: 'analyst' }]);
  chips[0].fire('mouseleave');
  assert.deepEqual(highlights.at(-1), [[], { by: 'analyst' }]);
  chips[0].fire('click');
  assert.deepEqual(emitted.at(-1), ['inspect', { id: 'trk:T-1' }]);
  // Caret while streaming; gone and footer present after the end.
  assert.ok(find(articles[1], cls('ic-caret')));
  chat.event(
    'usage',
    { turn_id: 't1', cost_usd: 0.02, input_tokens: 3100, output_tokens: 410 },
    next(),
  );
  chat.event('turn_end', { turn_id: 't1', stop: 'end' }, next());
  const art = findAll(host, (el) => el.tag === 'article')[1];
  assert.equal(art.attrs['aria-busy'], 'false');
  assert.equal(find(art, cls('ic-caret')), null);
  const usage = find(art, cls('ic-msg__usage'));
  assert.deepEqual(usage.children.map(textOf), [
    '0.0 s',
    '3.1k in, 410 out',
    '$0.02',
  ]);
  assert.ok(textOf(host).includes('Session $0.02'));
  assert.ok(buttonNamed(art, 'Show all 2 on the orb'));
});

test('arrow keys move between chips in one message', async () => {
  const { chat, host, doc } = await mountView();
  turn(chat);
  chat.event(
    'text_delta',
    { turn_id: 't1', text: '[[trk:T-1|A]] [[trk:T-2|B]] [[veh:Drone1]]' },
    next(),
  );
  const chips = findAll(
    findAll(host, (el) => el.tag === 'article')[1],
    cls('ic-chip'),
  );
  chips[0].fire('keydown', { key: 'ArrowRight' });
  assert.equal(doc.activeElement, chips[1]);
  assert.equal(chips[1].attrs.tabindex, '0');
  assert.equal(chips[0].attrs.tabindex, '-1');
  chips[1].fire('keydown', { key: 'ArrowLeft' });
  assert.equal(doc.activeElement, chips[0]);
  chips[0].fire('keydown', { key: 'ArrowLeft' });
  assert.equal(doc.activeElement, chips[2], 'wraps');
  assert.equal(
    textOf(chips[2]).includes('Drone1'),
    true,
    'no label: the graph label',
  );
});

test('consecutive read and plan rows fold into "Looked at N sources"', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  for (const [id, tool] of [
    ['c1', 'intel_overview'],
    ['c2', 'intel_search'],
    ['c3', 'mission_dry_run'],
  ]) {
    chat.event(
      'tool_call',
      {
        turn_id: 't1',
        call_id: id,
        tool,
        title: tool,
        class: id === 'c3' ? 'plan' : 'read',
        args: {},
      },
      next(),
    );
  }
  let fold = find(host, cls('ic-fold'));
  assert.ok(textOf(fold).includes('Looking at 3 sources'));
  for (const id of ['c1', 'c2', 'c3'])
    chat.event('tool_result', { call_id: id, ok: true, summary: 'ok' }, next());
  fold = find(host, cls('ic-fold'));
  assert.ok(textOf(fold).includes('Looked at 3 sources'));
  const head = find(fold, cls('ic-fold__head'));
  assert.equal(head.attrs['aria-expanded'], 'false');
  head.fire('click');
  fold = find(host, cls('ic-fold'));
  assert.equal(findAll(fold, cls('ic-row')).length, 3);
});

test('row states: rejected, not run (busy) and failed say what happened', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  const rows = [
    ['r1', { ok: false, rejected: true, summary: 'Fuel gate · needs 104%' }],
    ['r2', { ok: false, summary: 'Vehicle busy with another task' }],
    ['r3', { ok: false, error: 'geofence doc missing' }],
  ];
  for (const [id] of rows) {
    chat.event(
      'tool_call',
      {
        turn_id: 't1',
        call_id: id,
        tool: 'uav_takeoff',
        title: 'Take off',
        class: 'command',
        args: { vehicle: 'Drone1' },
      },
      next(),
    );
    chat.event('text_delta', { turn_id: 't1', text: '.' }, next());
  }
  for (const [id, data] of rows)
    chat.event('tool_result', { call_id: id, ...data }, next());
  const text = textOf(host);
  assert.ok(text.includes('Rejected by the server gate. '));
  assert.ok(text.includes('needs 104%'));
  assert.ok(text.includes(' Nothing was run.'));
  assert.ok(
    text.includes('Not run. Vehicle busy with another task Nothing was sent.'),
  );
  assert.ok(text.includes('Failed: geofence doc missing'));
  assert.ok(!text.includes('·'), 'segments, never the dot');
});

test('analyst focus runs live, never on replay, and offers Show again', async () => {
  const { chat, host, emitted } = await mountView();
  seq = 0;
  chat.event('session', { session_id: 's1', last_seq: 2 }, 0);
  chat.event('turn_start', { turn_id: 't0', text: 'old' }, 1, { replay: true });
  chat.event('ui', { action: 'focus', ids: ['trk:T-1', 'trk:T-2'] }, 2, {
    replay: true,
  });
  assert.equal(
    emitted.filter(([e]) => e === 'focus:entities').length,
    0,
    'history never moves the view',
  );
  seq = 2;
  chat.event('turn_start', { turn_id: 't1', text: 'new' }, next());
  chat.event(
    'ui',
    { action: 'focus', ids: ['trk:T-1', 'trk:T-2'], note: 'unassessed' },
    next(),
  );
  const focus = emitted.filter(([e]) => e === 'focus:entities');
  assert.deepEqual(focus, [
    [
      'focus:entities',
      { ids: ['trk:T-1', 'trk:T-2'], by: 'analyst', note: 'unassessed' },
    ],
  ]);
  const lines = findAll(host, cls('ic-directive'));
  assert.equal(lines.length, 2);
  assert.ok(textOf(lines[1]).includes('Pointed at 2 entities on the orb.'));
  buttonNamed(lines[1], 'Show again').fire('click');
  assert.equal(emitted.filter(([e]) => e === 'focus:entities').length, 2);
  chat.event('ui', { action: 'inspect', id: 'trk:T-1' }, next());
  assert.deepEqual(emitted.at(-1), [
    'inspect',
    { id: 'trk:T-1', by: 'analyst' },
  ]);
  assert.ok(textOf(host).includes('Opened SA-6 battery in the inspector.'));
});

function launch(
  chat,
  {
    result = { ok: true, summary: 'Mission MSN-1 started' },
    approve = true,
  } = {},
) {
  turn(chat, 'Launch the grid search');
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c1',
      tool: 'mission_grid_search',
      title: 'Grid search',
      class: 'command',
      args: { vehicle: 'Drone1' },
    },
    next(),
  );
  chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'mission_grid_search',
      class: 'command',
      title: 'Grid search',
      consequences: [],
      args: { vehicle: 'Drone1' },
      expires_at_ms: T0 + 600_000,
    },
    next(),
  );
  if (!approve) return;
  chat.event(
    'approval_resolved',
    { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
    next(),
  );
  chat.event('tool_result', { call_id: 'c1', ...result }, next());
}

test('auto-track: an approved launch that ran asks to track when the console is quiet', async () => {
  const { chat, emitted, host } = await mountView();
  launch(chat);
  chat.event(
    'ui',
    { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
    next(),
  );
  assert.deepEqual(
    emitted.filter(([e]) => e === 'track:request'),
    [
      [
        'track:request',
        {
          vehicle: 'Drone1',
          source: 'launch',
          reason: 'mission launched',
          mission: 'Grid search',
        },
      ],
    ],
  );
  assert.ok(textOf(host).includes('Asked to watch Drone1: mission launched.'));
});

test('auto-track: a slip resolved in the same batch as the launch publishes count 0 before the track request', async () => {
  // E2E A1 step 5 (A14): the approval, the tool result and `ui track` can
  // land before the next render. mode.js gates on the bus count, so the view
  // must publish the pending count before it asks to track.
  const frames = [];
  const runFrames = () => {
    while (frames.length) frames.shift()();
  };
  const { chat, emitted } = await mountView({ raf: (cb) => frames.push(cb) });
  launch(chat, { approve: false });
  runFrames(); // the slip is on screen: count 1 was published
  assert.equal(
    emitted.filter(([e]) => e === 'approval:pending').at(-1)[1].count,
    1,
  );
  chat.event(
    'approval_resolved',
    { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
    next(),
  );
  chat.event(
    'tool_result',
    { call_id: 'c1', ok: true, summary: 'Mission MSN-1 started' },
    next(),
  );
  chat.event(
    'ui',
    { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
    next(),
  );
  const order = emitted
    .map(([e, p], i) => [e, p, i])
    .filter(([e]) => e === 'approval:pending' || e === 'track:request');
  const trackAt = order.findIndex(([e]) => e === 'track:request');
  assert.ok(trackAt > 0, 'the launch asks to track (no frame has run yet)');
  assert.deepEqual(order[trackAt - 1][0], 'approval:pending');
  assert.equal(order[trackAt - 1][1].count, 0);
  runFrames();
});

test('auto-track never fires on busy or a refused launch', async () => {
  const { chat, emitted, host } = await mountView();
  launch(chat, {
    result: { ok: false, outcome: 'busy', summary: 'Vehicle busy' },
  });
  chat.event(
    'ui',
    { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
    next(),
  );
  assert.equal(emitted.filter(([e]) => e === 'track:request').length, 0);
  const line = findAll(host, cls('ic-directive')).at(-1);
  assert.equal(
    buttonNamed(line, 'Track Drone1'),
    null,
    'nothing launched: nothing to track',
  );
});

test('auto-track with a slip pending is handed to mode.js as a static offer, and the transcript offers Track', async () => {
  const { chat, emitted, host } = await mountView();
  launch(chat);
  // A second request is waiting for approval.
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c2',
      tool: 'uav_goto_gps',
      class: 'command',
      args: { vehicle: 'Drone2' },
    },
    next(),
  );
  chat.event(
    'approval_request',
    {
      approval_id: 'a2',
      call_id: 'c2',
      tool: 'uav_goto_gps',
      class: 'command',
      title: 'Fly to point',
      consequences: [],
      args: { vehicle: 'Drone2' },
    },
    next(),
  );
  chat.event(
    'ui',
    { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
    next(),
  );
  // mode.js applies the same rule (a slip is pending) and shows the static
  // "Launched: … / Track Drone1" toast rather than a countdown (§6.9).
  assert.deepEqual(
    emitted.filter(([e]) => e === 'track:request'),
    [
      [
        'track:request',
        {
          vehicle: 'Drone1',
          source: 'launch',
          reason: 'mission launched',
          mission: 'Grid search',
        },
      ],
    ],
  );
  const line = findAll(host, cls('ic-directive')).at(-1);
  assert.ok(textOf(line).includes('Launched: Grid search with Drone1.'));
  buttonNamed(line, 'Track Drone1').fire('click');
  assert.deepEqual(emitted.at(-1), [
    'track:request',
    { vehicle: 'Drone1', source: 'operator', reason: 'mission launched' },
  ]);
});

test('auto-track: composer text, recent stage input or tracking elsewhere defer to mode.js (static toast, no countdown)', async () => {
  for (const setup of [
    (m) => {
      find(m.host, (el) => el.tag === 'textarea').value = 'drafting';
    },
    (m) => {
      m.store.lastStageInputAt = m.clock.now() - 1000;
    },
    (m) => {
      m.mode.state = 'tracking';
      m.mode.vehicle = 'Drone2';
    },
  ]) {
    const m = await mountView();
    setup(m);
    launch(m.chat);
    m.chat.event(
      'ui',
      { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
      next(),
    );
    const asked = m.emitted.filter(([e]) => e === 'track:request');
    assert.equal(asked.length, 1);
    assert.equal(asked[0][1].source, 'launch');
    // The transcript keeps its own Track offer beside mode.js's toast.
    const line = findAll(m.host, cls('ic-directive')).at(-1);
    assert.ok(buttonNamed(line, 'Track Drone1'));
  }
});

test('the analyst asking to return to the orb only matters while tracking', async () => {
  const m = await mountView({ modeState: 'tracking', modeVehicle: 'Drone1' });
  turn(m.chat);
  m.chat.event('ui', { action: 'orb' }, next());
  assert.deepEqual(
    m.emitted.filter(([e]) => e === 'track:exit'),
    [['track:exit', { source: 'analyst' }]],
  );
  assert.ok(textOf(m.host).includes('Asked to return to the orb.'));
  const o = await mountView();
  turn(o.chat);
  o.chat.event('ui', { action: 'orb' }, next());
  assert.equal(o.emitted.filter(([e]) => e === 'track:exit').length, 0);
});

// ---- approvals ------------------------------------------------------------------------------

test('an approval shows a slip, a banner, a pending count and an assertive announcement', async () => {
  const { chat, host, emitted, announced, clock } = await mountView();
  launch(chat, { approve: false });
  const slip = find(host, cls('ic-slip'));
  assert.ok(slip);
  const bar = find(host, cls('ic-approvalbar'));
  assert.equal(isHidden(bar), false);
  assert.equal(
    textOf(find(bar, cls('ic-approvalbar__text'))),
    'Approval waiting: Grid search, Drone1.',
  );
  assert.deepEqual(emitted.filter(([e]) => e === 'approval:pending').at(-1), [
    'approval:pending',
    {
      count: 1,
      oldest: {
        id: 'a1',
        title: 'Grid search',
        vehicle: 'Drone1',
        klass: 'command',
      },
    },
  ]);
  assert.deepEqual(announced.at(-1), [
    'Approval needed: Grid search. Commands an aircraft.',
    'assertive',
  ]);
  assert.ok(textOf(host).includes('Waiting for your approval'));
  // Approve after arming: the decision reaches the client.
  clock.advance(ARM_MS);
  const approve = find(
    slip,
    (el) =>
      el.attrs?.['data-action'] === 'approve-anyway' ||
      el.attrs?.['data-action'] === 'approve',
  );
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(
    chat.calls.find((c) => c[0] === 'approve'),
    ['approve', 'a1', 'approve', null],
  );
  chat.event(
    'approval_resolved',
    { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
    next(),
  );
  assert.equal(isHidden(find(host, cls('ic-approvalbar'))), true);
  assert.deepEqual(emitted.filter(([e]) => e === 'approval:pending').at(-1), [
    'approval:pending',
    { count: 0, oldest: null },
  ]);
  assert.equal(find(host, cls('ic-slip')).attrs['data-state'], 'filed');
});

test('a replayed approval is not announced again', async () => {
  const { chat, announced } = await mountView();
  chat.event('session', { session_id: 's1', last_seq: 3 }, 0);
  chat.event('turn_start', { turn_id: 't1', text: 'x' }, 1, { replay: true });
  chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'uav_takeoff',
      class: 'command',
      title: 'Take off',
      consequences: [],
    },
    2,
    { replay: true },
  );
  assert.equal(
    announced.filter(([t]) => t.startsWith('Approval needed')).length,
    0,
  );
});

test('the dry-run snapshot makes a slip stale when the vehicle changes', async () => {
  const m = await mountView();
  turn(m.chat, 'Plan and launch');
  m.chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'p1',
      tool: 'mission_dry_run',
      class: 'plan',
      args: { vehicle: 'Drone1', kind: 'grid_search' },
    },
    next(),
  );
  m.chat.event(
    'tool_result',
    { call_id: 'p1', ok: true, summary: 'gate ok' },
    next(),
  );
  m.chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c1',
      tool: 'mission_grid_search',
      class: 'command',
      args: { vehicle: 'Drone1' },
    },
    next(),
  );
  m.chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'mission_grid_search',
      class: 'command',
      title: 'Grid search',
      consequences: [],
      args: { vehicle: 'Drone1' },
      dry_run: {
        ok: true,
        gate: { required_pct: 41.3, available_pct: 82 },
        at_ms: m.clock.now(),
      },
    },
    next(),
  );
  let slip = find(m.host, cls('ic-slip'));
  assert.equal(slip.attrs['data-validity'], 'current');
  m.store.graph.nodes[0].attrs = {
    ...m.store.graph.nodes[0].attrs,
    fuel_pct: 74.5,
    landed: false,
  };
  m.store.change();
  slip = find(m.host, cls('ic-slip'));
  assert.equal(slip.attrs['data-validity'], 'stale');
  assert.ok(
    textOf(slip).includes(
      'Conditions changed since the dry run: fuel 82.0% → 74.5%; Drone1 is now airborne.',
    ),
  );
  assert.ok(textOf(slip).includes('Ask for a fresh dry run'));
});

// ---- interrupt -----------------------------------------------------------------------------

test('Esc in the composer stops the analyst; the stop says the aircraft keeps flying', async () => {
  const graph = graphFixture({
    nodes: [
      {
        id: 'msn:MSN-1',
        type: 'mission',
        label: 'Grid search',
        status: 'ok',
        attrs: { phase: 'executing', vehicle: 'Drone1', kind: 'grid_search' },
      },
    ],
  });
  const { chat, host, emitted, clock } = await mountView({ graph });
  turn(chat);
  const textarea = find(host, (el) => el.tag === 'textarea');
  const send = find(host, cls('ic-composer__send'));
  assert.equal(
    send.attrs['data-mode'],
    'stop',
    'Send becomes Stop while a turn runs',
  );
  const ev = textarea.fire('keydown', { key: 'Escape' });
  assert.equal(ev.defaultPrevented, true);
  await flush();
  assert.ok(chat.calls.some((c) => c[0] === 'interrupt'));
  clock.advance(2000);
  chat.event('turn_end', { turn_id: 't1', stop: 'interrupted' }, next());
  const text = textOf(host);
  assert.ok(text.includes('Stopped by you at 14:02:00Z.'));
  assert.ok(
    text.includes(
      "Drone1's grid search keeps flying. Use Abort to stop the aircraft.",
    ),
  );
  buttonNamed(host, 'Abort Drone1').fire('click');
  assert.deepEqual(emitted.at(-1), ['abort:request', { vehicle: 'Drone1' }]);
});

test('a stop with nothing flying still says stopping the analyst does not stop aircraft', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  await chat.interrupt();
  chat.event('turn_end', { turn_id: 't1', stop: 'interrupted' }, next());
  assert.ok(textOf(host).includes(COPY.keepsFlying));
});

// ---- composer ------------------------------------------------------------------------------

test('Enter sends with the context row; Shift+Enter does not; ↑ recalls', async () => {
  const { chat, host, bus } = await mountView();
  bus.emit('ask', {
    text: 'Tell me about [[trk:T-1|SA-6 battery]]',
    focused_ids: ['trk:T-1'],
    draft: true,
  });
  const textarea = find(host, (el) => el.tag === 'textarea');
  assert.equal(textarea.value, 'Tell me about [[trk:T-1|SA-6 battery]]');
  const ctxRow = find(host, cls('ic-composer__context'));
  assert.equal(isHidden(ctxRow), false);
  assert.ok(textOf(ctxRow).startsWith('About:'));
  assert.equal(
    chat.calls.filter((c) => c[0] === 'send').length,
    0,
    'a draft is never sent',
  );
  const shift = textarea.fire('keydown', { key: 'Enter', shiftKey: true });
  assert.equal(shift.defaultPrevented, false);
  textarea.fire('keydown', { key: 'Enter' });
  await flush();
  assert.deepEqual(
    chat.calls.find((c) => c[0] === 'send'),
    [
      'send',
      'Tell me about [[trk:T-1|SA-6 battery]]',
      { focused_ids: ['trk:T-1'] },
    ],
  );
  assert.equal(textarea.value, '');
  assert.equal(isHidden(find(host, cls('ic-composer__context'))), true);
  // The outbox shows the message until the server echoes it.
  assert.ok(textOf(host).includes('Sending…'));
  chat.event(
    'turn_start',
    { turn_id: 't9', text: 'Tell me about [[trk:T-1|SA-6 battery]]' },
    1,
  );
  assert.ok(!textOf(host).includes('Sending…'));
  assert.ok(
    textOf(findAll(host, (el) => el.tag === 'article')[0]).includes('About: '),
  );
  chat.event('turn_end', { turn_id: 't9', stop: 'end' }, 2);
  textarea.fire('keydown', { key: 'ArrowUp' });
  assert.equal(textarea.value, 'Tell me about [[trk:T-1|SA-6 battery]]');
});

test('the counter appears from 7,500 characters; over 8,000 cannot be sent', async () => {
  const { host, chat } = await mountView();
  const textarea = find(host, (el) => el.tag === 'textarea');
  textarea.value = 'x'.repeat(7812);
  textarea.fire('input');
  const counter = find(host, cls('ic-composer__counter'));
  assert.equal(isHidden(counter), false);
  assert.equal(counter.textContent, '7,812 of 8,000');
  textarea.value = 'x'.repeat(8001);
  textarea.fire('input');
  assert.equal(
    find(host, cls('ic-composer__send')).attrs['aria-disabled'],
    'true',
  );
  textarea.fire('keydown', { key: 'Enter' });
  await flush();
  assert.equal(chat.calls.filter((c) => c[0] === 'send').length, 0);
});

test('a 409 keeps the draft and says the analyst is still answering', async () => {
  const { host, chat } = await mountView();
  chat.sendError = Object.assign(new Error('busy'), { code: 'busy' });
  const textarea = find(host, (el) => el.tag === 'textarea');
  textarea.value = 'Hello';
  textarea.fire('keydown', { key: 'Enter' });
  await flush();
  assert.equal(textarea.value, 'Hello');
  assert.equal(find(host, cls('ic-composer__notice')).textContent, COPY.busy);
});

test('a failed send shows Retry', async () => {
  const { host, chat } = await mountView();
  chat.sendError = Object.assign(new Error('Failed to fetch'), {
    code: 'offline',
  });
  const textarea = find(host, (el) => el.tag === 'textarea');
  textarea.value = 'Hello';
  textarea.fire('keydown', { key: 'Enter' });
  await flush();
  assert.ok(textOf(host).includes(COPY.sendFailed));
  chat.sendError = null;
  buttonNamed(host, 'Retry').fire('click');
  await flush();
  assert.equal(chat.calls.filter((c) => c[0] === 'send').length, 2);
});

test('the search ask row sends with the top matches as context', async () => {
  const { chat, bus } = await mountView();
  bus.emit('ask', {
    text: 'which contacts are unassessed?',
    focused_ids: ['trk:T-1', 'trk:T-2'],
  });
  await flush();
  assert.deepEqual(
    chat.calls.find((c) => c[0] === 'send'),
    [
      'send',
      'which contacts are unassessed?',
      { focused_ids: ['trk:T-1', 'trk:T-2'] },
    ],
  );
});

test('usage limit reached disables sending but keeps the transcript and draft', async () => {
  const { chat, host, clock } = await mountView();
  turn(chat);
  const resets = Math.floor((clock.now() + 23 * 60_000) / 1000);
  chat.event(
    'usage',
    { turn_id: 't1', rate_limit: { status: 'rejected', resets_at: resets } },
    next(),
  );
  chat.event('turn_end', { turn_id: 't1', stop: 'end' }, next());
  const notice = find(host, cls('ic-composer__notice'));
  assert.equal(
    notice.textContent,
    'Usage limit reached. The analyst can answer again at 14:25Z, in 23 min. Search, the orb and the rail keep working. Your draft is kept.',
  );
  const textarea = find(host, (el) => el.tag === 'textarea');
  textarea.value = 'draft';
  textarea.fire('input');
  assert.equal(
    find(host, cls('ic-composer__send')).attrs['aria-disabled'],
    'true',
  );
  assert.equal(
    isHidden(find(host, cls('ic-composer'))),
    false,
    'the column does not collapse',
  );
});

// ---- availability -----------------------------------------------------------------------------

test('unavailable: the reason replaces the composer and the shell is told', async () => {
  const { host, emitted, chat } = await mountView({
    status: {
      available: false,
      reason: 'cli_missing',
      hint: 'Install the Claude CLI',
    },
  });
  assert.equal(chat.calls.filter((c) => c[0] === 'open').length, 0);
  const panel = find(host, cls('ic-unavailable'));
  assert.equal(isHidden(panel), false);
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    "The analyst needs the Claude command-line tool, and it wasn't found.",
  );
  assert.equal(
    find(panel, cls('ic-unavailable__hint')).textContent,
    'Install the Claude CLI',
  );
  assert.equal(isHidden(find(host, cls('ic-composer'))), true);
  assert.deepEqual(
    emitted.find(([e]) => e === 'analyst:availability'),
    [
      'analyst:availability',
      {
        available: false,
        reason: 'cli_missing',
        hint: 'Install the Claude CLI',
      },
    ],
  );
  assert.ok(textOf(host).includes('Unavailable'));
  // Check again after installing.
  chat.statusValue = { available: true, model: 'claude-x' };
  buttonNamed(panel, 'Check again').fire('click');
  await flush();
  await flush();
  assert.ok(chat.calls.some((c) => c[0] === 'open'));
  assert.equal(isHidden(find(host, cls('ic-unavailable'))), true);
});

test('a sign-in failure shows the sign-in copy', async () => {
  const { chat, host, emitted } = await mountView();
  turn(chat);
  chat.event(
    'error',
    {
      message: 'The analyst could not sign in to Claude.',
      hint: 'Sign in with the claude CLI',
      retryable: false,
    },
    next(),
  );
  const panel = find(host, cls('ic-unavailable'));
  assert.equal(isHidden(panel), false);
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    "The analyst isn't signed in.",
  );
  assert.equal(
    textOf(find(panel, cls('ic-unavailable__body'))),
    'Sign in with the claude CLI (claude, then /login) or set ANTHROPIC_API_KEY.',
  );
  assert.ok(textOf(host).includes('Not signed in'));
  assert.deepEqual(
    emitted.filter(([e]) => e === 'analyst:availability').at(-1)[1].reason,
    'auth',
  );
});

test('turn errors and the 40-step limit have their own lines', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  chat.event('turn_end', { turn_id: 't1', stop: 'max_turns' }, next());
  assert.ok(textOf(host).includes(COPY.maxTurns));
  chat.event('turn_start', { turn_id: 't2', text: 'again' }, next());
  chat.event(
    'turn_end',
    { turn_id: 't2', stop: 'error', error: 'overloaded' },
    next(),
  );
  assert.ok(
    textOf(host).includes('The analyst stopped with an error: overloaded.'),
  );
  buttonNamed(host, 'Try again').fire('click');
  await flush();
  assert.deepEqual(chat.calls.filter((c) => c[0] === 'send').at(-1), [
    'send',
    'again',
    { focused_ids: [] },
  ]);
});

test('reconnect gaps are written into the transcript', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  chat.emit('state', { connection: 'reconnecting' });
  assert.ok(textOf(host).includes('Reconnecting to the analyst…'));
  chat.event(
    'session',
    { session_id: 's1', history_truncated: true, last_seq: 400 },
    0,
  );
  const text = textOf(host);
  assert.equal(
    /updates from (\d\d:\d\dZ) to \1 were lost/.test(text),
    false,
    'never a zero-length range at minute precision',
  );
  assert.ok(
    /Reconnected, but updates (from \d\d:\d\d:\d\dZ to \d\d:\d\d:\d\dZ|around \d\d:\d\d:\d\dZ) were lost\./.test(
      text,
    ),
    text,
  );
  assert.ok(textOf(host).includes(COPY.gapTail));
});

test('new session keeps the old transcript under a divider', async () => {
  const { chat, host, view } = await mountView();
  turn(chat);
  chat.event('turn_end', { turn_id: 't1', stop: 'end' }, next());
  await view.newSession();
  assert.ok(textOf(host).includes(COPY.dividerOperator));
});

test('composerIdle and destroy', async () => {
  const { view, host, doc, chat } = await mountView();
  assert.equal(view.composerIdle(), true);
  const textarea = find(host, (el) => el.tag === 'textarea');
  textarea.focus();
  assert.equal(view.composerIdle(), false);
  doc.activeElement = null;
  textarea.value = 'x';
  assert.equal(view.composerIdle(), false);
  view.destroy();
  assert.equal(chat.count('event'), 0, 'unsubscribed');
  view.destroy();
});

// ---- pure helpers ------------------------------------------------------------------------------

test('shouldAutoTrack: every condition must hold', () => {
  const base = {
    source: 'launch',
    vehicle: 'Drone1',
    commandState: 'done',
    now: T0,
    mode: 'orb',
  };
  assert.deepEqual(shouldAutoTrack(base), { allowed: true, reason: null });
  assert.equal(
    shouldAutoTrack({ ...base, commandState: 'not_run' }).reason,
    'not_done',
  );
  assert.equal(
    shouldAutoTrack({ ...base, commandState: null }).reason,
    'not_done',
  );
  assert.equal(
    shouldAutoTrack({ ...base, composerText: ' x ' }).reason,
    'composer',
  );
  assert.equal(
    shouldAutoTrack({ ...base, composerFocused: true }).reason,
    'composer',
  );
  assert.equal(
    shouldAutoTrack({ ...base, lastStageInputAt: T0 - 2999 }).reason,
    'stage_input',
  );
  assert.equal(
    shouldAutoTrack({ ...base, lastStageInputAt: T0 - 3000 }).allowed,
    true,
  );
  assert.equal(
    shouldAutoTrack({ ...base, pendingCount: 1 }).reason,
    'slip_pending',
  );
  assert.equal(
    shouldAutoTrack({ ...base, mode: 'tracking', trackingVehicle: 'Drone2' })
      .reason,
    'tracking_other',
  );
  assert.equal(
    shouldAutoTrack({ ...base, mode: 'tracking', trackingVehicle: 'Drone1' })
      .reason,
    'already',
  );
  assert.equal(
    shouldAutoTrack({ ...base, vehicle: null }).reason,
    'no_vehicle',
  );
  assert.equal(
    shouldAutoTrack({ ...base, source: 'analyst', commandState: null }).allowed,
    true,
    'an analyst ui_track needs no launch',
  );
});

test('suggestedPrompts swap by state', () => {
  const quiet = suggestedPrompts(
    graphFixture({ meta: { threat_assessed: 0 } }),
  );
  assert.ok(
    quiet.includes(
      "How much fuel does Drone1 have above BINGO, and what's the longest mission it could fly?",
    ),
  );
  const running = graphFixture({
    nodes: [
      {
        id: 'msn:MSN-1a2b3c4d',
        type: 'mission',
        attrs: {
          phase: 'executing',
          vehicle: 'Drone1',
          mission_id: 'MSN-1a2b3c4d',
        },
      },
    ],
    meta: { feeds: { real_data: { ok: true } } },
  });
  const p = suggestedPrompts(running);
  assert.ok(
    p.includes(
      'How is MSN-1a2b3c4d progressing, and when does Drone1 reach BINGO?',
    ),
  );
  assert.ok(!p.some((x) => x.startsWith('Plan a grid search')));
  assert.ok(
    p.includes(
      'Which readings in this picture are assumed rather than measured?',
    ),
  );
  assert.ok(p.length <= 5);
  const empty = {
    theater: { label: 'T' },
    nodes: [{ id: 'veh:Drone1', type: 'vehicle', label: 'Drone1', attrs: {} }],
    meta: {},
  };
  const q = suggestedPrompts(empty);
  assert.ok(
    q.includes(
      'Plan a grid search over the AO at 60 m AGL and show me the dry run.',
    ),
  );
  assert.ok(q.includes("Scan for targets from Drone1's current position."));
  const none = suggestedPrompts(null);
  assert.deepEqual(
    none[0],
    "Summarize the situation in this theater and what we don't know yet.",
  );
});

test('emptyHeading is built from state and never greets', () => {
  assert.equal(
    emptyHeading({
      theater: { label: 'Redmond (AirSim default)' },
      nodes: [
        {
          id: 'veh:Drone1',
          type: 'vehicle',
          label: 'Drone1',
          attrs: { landed: true, fuel_pct: 100 },
        },
      ],
    }),
    'Redmond (AirSim default). Drone1 is on the ground with 100% fuel. Nothing observed yet.',
  );
  assert.equal(emptyHeading(null), '');
});

test('graph helpers: flying now, assumed caveats, detections feed', () => {
  const g = graphFixture({
    nodes: [
      {
        id: 'msn:M',
        type: 'mission',
        attrs: { phase: 'rtb', vehicle: 'Drone1', kind: 'track' },
      },
      {
        id: 'veh:Drone2',
        type: 'vehicle',
        label: 'Drone2',
        attrs: { landed: false, mission: 'M2' },
      },
    ],
  });
  assert.deepEqual(flyingNow(g), [
    { vehicle: 'Drone1', kind: 'track' },
    { vehicle: 'Drone2', kind: null },
  ]);
  assert.deepEqual(assumedCaveats(g), [
    'Real-data layer is off: AGL is height above launch datum; LOS is geometric.',
  ]);
  assert.deepEqual(
    assumedCaveats(
      graphFixture({ meta: { feeds: { real_data: { ok: true } } } }),
    ),
    [],
  );
  assert.deepEqual(detectionsFeed(g), { ok: true, at_ms: T0 - 3000 });
  assert.equal(detectionsFeed({ meta: {} }), null);
});

// ---- JS-PANELS-POLISH ------------------------------------------------------------------

test('never reached: a service that never answered is "not available", never "Reconnecting"', async () => {
  const { host, chat, clock } = await mountView({
    status: { available: false, reason: 'unreachable', transient: true },
  });
  await flush();
  assert.ok(!textOf(host).includes(COPY.reconnecting));
  const panel = find(host, cls('ic-unavailable'));
  assert.equal(isHidden(panel), false);
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    "The analyst isn't available right now.",
  );
  assert.ok(textOf(host).includes('Unavailable'));
  assert.equal(
    findAll(host, cls('ic-prompt')).length,
    0,
    'no prompts to insert into a composer that is not there',
  );
  // It keeps checking quietly; when the service answers, the composer returns.
  chat.statusValue = { available: true, model: 'claude-x' };
  clock.advance(5000);
  await flush();
  await flush();
  assert.ok(chat.calls.some((c) => c[0] === 'open'));
  assert.equal(isHidden(find(host, cls('ic-unavailable'))), true);
  // Once reached, a later blip is a reconnect, and the transcript stays.
  chat.emit('status', {
    available: false,
    reason: 'unreachable',
    transient: true,
  });
  await flush();
  assert.ok(textOf(host).includes(COPY.reconnecting));
  assert.equal(isHidden(find(host, cls('ic-unavailable'))), true);
});

test('standing approvals: one Revoke per granted tool, read from the server after a session grant', async () => {
  const { host, chat } = await mountView();
  let grants = [];
  let reads = 0;
  const revoked = [];
  chat.grants = async () => {
    reads += 1;
    return grants;
  };
  chat.revokeGrant = async (tool) => {
    revoked.push(tool);
    grants = grants.filter((g) => g.tool !== tool);
    return { ok: true };
  };
  turn(chat, 'Scan for targets from Drone1.');
  await flush();
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c1',
      tool: 'uav_scan_targets',
      title: 'Scan for targets',
      class: 'sensor',
      args: { vehicle: 'Drone1' },
      summary: 'Drone1 · camera 0',
    },
    next(),
  );
  chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'uav_scan_targets',
      class: 'sensor',
      title: 'Scan for targets',
      summary: 'Drone1 · camera 0',
      args: { vehicle: 'Drone1' },
      consequences: ['Reads the sensor.'],
      allow_session: true,
      grant_scope: ['uav_scan_targets'],
      vehicle: 'Drone1',
      expires_at_ms: T0 + 600_000,
    },
    next(),
  );
  const before = reads;
  grants = [{ tool: 'uav_scan_targets', since_ms: T0 }];
  chat.event(
    'approval_resolved',
    {
      approval_id: 'a1',
      call_id: 'c1',
      decision: 'approved',
      tool: 'uav_scan_targets',
      scope: 'session',
    },
    next(),
  );
  await flush();
  await flush();
  assert.ok(reads > before, 'the grant list is read again');
  const standing = find(host, cls('ic-composer__standing'));
  assert.ok(
    textOf(standing).includes(
      'Standing approval: Scan for targets, until you start a new session.',
    ),
    textOf(standing),
  );
  const revoke = find(
    standing,
    (el) => el.tag === 'button' && el.attrs['data-tool'] === 'uav_scan_targets',
  );
  assert.ok(revoke, 'a Revoke for the granted tool');
  assert.equal(textOf(revoke), 'Revoke');
  assert.equal(
    revoke.attrs['aria-label'],
    'Revoke the standing approval for Scan for targets',
  );
  revoke.fire('click');
  await flush();
  await flush();
  assert.deepEqual(revoked, ['uav_scan_targets']);
  assert.ok(
    textOf(find(host, cls('ic-composer__standing'))).includes(
      'Standing approval: none',
    ),
  );
});

test('a v1 server without the grants route offers no Revoke, only New session', async () => {
  const { host, chat } = await mountView();
  turn(chat);
  await flush();
  const standing = find(host, cls('ic-composer__standing'));
  assert.equal(
    find(standing, (el) => el.tag === 'button' && el.attrs['data-tool']),
    null,
  );
});

test("a feed chip without a label uses the orb's plain name, never the server label", async () => {
  const { chat, host } = await mountView({
    graph: graphFixture({
      nodes: [
        {
          id: 'feed:contacts',
          type: 'feed',
          label: 'Contacts',
          subtitle: 'mcp:uav_list_tracks',
          status: 'ok',
          attrs: { ok: true },
        },
      ],
    }),
  });
  turn(chat, 'Is detection up?');
  chat.event(
    'text_delta',
    { turn_id: 't1', text: 'The [[feed:contacts]] is up.' },
    next(),
  );
  await flush();
  const chip = find(
    host,
    (el) => el.tag === 'button' && textOf(el).includes('Contacts feed'),
  );
  assert.ok(chip, 'chip named as the orb names it');
});

// ---- the transcript after an operator action -----------------------------------------------

function scroller(host, { top, height = 2000, client = 500 }) {
  const log = find(host, cls('ic-log'));
  log.scrollHeight = height;
  log.clientHeight = client;
  log.scrollTop = top;
  return log;
}

test('deciding a slip reveals the newest line even when Review had scrolled the log up', async () => {
  // Live E2E: Review scrolled to a tall slip, Approve collapsed it into a
  // one-line record, and the log was left on an older turn while the
  // analyst's result streamed out of sight below.
  const { chat, host, clock } = await mountView();
  launch(chat, { approve: false });
  const log = scroller(host, { top: 100 });
  // A streamed line while the operator reads further up never yanks the view.
  chat.event('text_delta', { turn_id: 't1', text: 'Working on it.' }, next());
  await flush();
  assert.equal(log.scrollTop, 100);
  clock.advance(ARM_MS);
  const slip = find(host, cls('ic-slip'));
  const approve = find(
    slip,
    (el) =>
      el.attrs?.['data-action'] === 'approve-anyway' ||
      el.attrs?.['data-action'] === 'approve',
  );
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.equal(log.scrollTop, 2000, 'the decision shows the newest line');
  // Following resumes from the bottom; later lines keep it there.
  log.scrollHeight = 2400;
  chat.event(
    'approval_resolved',
    { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
    next(),
  );
  await flush();
  assert.equal(log.scrollTop, 2400);
});

test('sending a message reveals it even when the log was scrolled up', async () => {
  const { host, view } = await mountView();
  const log = scroller(host, { top: 50 });
  await view.ask('What changed?');
  await flush();
  assert.equal(log.scrollTop, 2000);
});

test('the log keeps following when a slip grows in place during the same render', async () => {
  // Slips update their own DOM while the transcript is rebuilt; measuring
  // "at the bottom" after that read their growth as the operator scrolling
  // up, and the log stopped following the analyst (live E2E).
  const { chat, host } = await mountView();
  launch(chat, { approve: false });
  const log = find(host, cls('ic-log'));
  let height = 2000;
  Object.defineProperty(log, 'scrollHeight', { get: () => height });
  log.clientHeight = 500;
  log.scrollTop = 1500; // at the bottom
  const slip = find(host, cls('ic-slip'));
  const set = slip.setAttribute.bind(slip);
  slip.setAttribute = (k, v) => {
    if (k === 'data-state') height += 300; // an in-place re-render grows it
    set(k, v);
  };
  chat.event('text_delta', { turn_id: 't1', text: 'More.' }, next());
  await flush();
  assert.equal(log.scrollTop, height, 'still following the newest line');
});

test('a decided slip keeps revealing until it is filed, then normal following resumes', async () => {
  const { chat, host, clock } = await mountView();
  launch(chat, { approve: false });
  const log = scroller(host, { top: 100 });
  clock.advance(ARM_MS);
  const slip = find(host, cls('ic-slip'));
  const approve = find(
    slip,
    (el) =>
      el.attrs?.['data-action'] === 'approve-anyway' ||
      el.attrs?.['data-action'] === 'approve',
  );
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.equal(log.scrollTop, 2000);
  // Something scrolls the log up before the server files the decision (a
  // layout shift, a stray wheel): the filing render still shows the newest.
  log.scrollTop = 300;
  chat.event(
    'approval_resolved',
    { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
    next(),
  );
  await flush();
  assert.equal(log.scrollTop, 2000);
  // Filed: from now on the operator's own scroll position is respected.
  log.scrollTop = 300;
  chat.event('text_delta', { turn_id: 't1', text: 'Later.' }, next());
  await flush();
  assert.equal(log.scrollTop, 300);
});

// ---- FIXER-CHAT (review2) -------------------------------------------------------------

const BIDI = /[\u202A-\u202E\u2066-\u2069]/;

function sensorRequest(chat, extra = {}) {
  turn(chat, 'Scan for targets from Drone1.');
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c1',
      tool: 'uav_scan_targets',
      title: 'Scan for targets',
      class: 'sensor',
      args: { vehicle: 'Drone1' },
      summary: 'Drone1 · camera 0',
    },
    next(),
  );
  chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'uav_scan_targets',
      class: 'sensor',
      title: 'Scan for targets',
      summary: 'Drone1 · camera 0',
      args: { vehicle: 'Drone1' },
      consequences: ['Reads the sensor.'],
      vehicle: 'Drone1',
      expires_at_ms: T0 + 600_000,
      ...extra,
    },
    next(),
  );
}

test('the approval bar (role=status) is written once, not on every render (review: re-announced every second)', async () => {
  const { chat, host, clock, store } = await mountView();
  launch(chat, { approve: false });
  const text = find(host, cls('ic-approvalbar__text'));
  assert.equal(textOf(text), 'Approval waiting: Grid search, Drone1.');
  const desc = Object.getOwnPropertyDescriptor(text, 'textContent');
  let writes = 0;
  Object.defineProperty(text, 'textContent', {
    configurable: true,
    get: desc.get,
    set(v) {
      writes += 1;
      desc.set.call(this, v);
    },
  });
  // The expiry ticker renders every second; graph changes render too.
  clock.advance(5000);
  store.change();
  store.change();
  assert.equal(writes, 0, 'the same words are never rewritten');
  chat.event(
    'approval_request',
    {
      approval_id: 'a2',
      call_id: 'c2',
      tool: 'uav_takeoff',
      class: 'command',
      title: 'Take off',
      consequences: [],
      args: { vehicle: 'Drone1' },
    },
    next(),
  );
  clock.advance(3000);
  assert.equal(writes, 1, 'new words are written once');
  assert.equal(
    textOf(text),
    '2 approvals waiting. Oldest: Grid search, Drone1.',
  );
});

test("the sensor slip reads the server's contacts feed (review: the lookup only matched /detect/ keys)", async () => {
  assert.deepEqual(
    detectionsFeed({
      meta: { feeds: { contacts: { ok: true, status: 'ok', at_ms: T0 } } },
    }),
    { ok: true, at_ms: T0 },
  );
  assert.deepEqual(
    detectionsFeed({
      meta: {
        feeds: {
          detections: { ok: true, at_ms: 1 },
          contacts: { ok: false, status: 'critical', at_ms: 2 },
        },
      },
    }),
    { ok: false, at_ms: 2 },
    'contacts first, like the orb',
  );
  assert.deepEqual(
    detectionsFeed({
      meta: { feeds: { contacts: { ok: true, status: 'critical' } } },
    }),
    { ok: false, at_ms: null },
    'a critical feed is never "up"',
  );
  assert.deepEqual(
    detectionsFeed({ meta: { feeds: { cv_detector: { ok: true } } } }),
    { ok: true, at_ms: null },
  );
  const down = await mountView({
    graph: graphFixture({
      meta: {
        feeds: {
          real_data: { ok: false },
          contacts: { ok: false, status: 'critical', at_ms: T0 - 60_000 },
        },
      },
    }),
  });
  sensorRequest(down.chat);
  assert.ok(
    textOf(find(down.host, cls('ic-slip'))).includes(
      "Detections feed is down since 14:01:00Z. A scan now may return nothing, and that isn't a clear reading.",
    ),
  );
  const up = await mountView({
    graph: graphFixture({
      meta: {
        feeds: {
          real_data: { ok: false },
          contacts: { ok: true, status: 'ok', at_ms: T0 - 3000 },
        },
      },
    }),
  });
  sensorRequest(up.chat);
  const text = textOf(find(up.host, cls('ic-slip')));
  assert.ok(text.includes('Detections feed up, last update'), text);
  assert.ok(!text.includes('A scan now may return nothing'));
});

test('the streaming caret follows the last word, inside the last paragraph or list item', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  chat.event(
    'text_delta',
    { turn_id: 't1', text: 'Plan:\n\n- first leg\n- 150 m apart and' },
    next(),
  );
  const body = find(host, cls('ic-msg__text'));
  const caret = find(body, cls('ic-caret'));
  assert.ok(caret);
  const holder = find(
    body,
    (el) => Array.isArray(el.children) && el.children.includes(caret),
  );
  assert.equal(holder.tag, 'li', 'not a sibling after the <ul>');
  assert.equal(textOf(holder), '150 m apart and');
  chat.event('text_delta', { turn_id: 't1', text: ' done.\n\nNext' }, next());
  const again = find(host, cls('ic-msg__text'));
  const holder2 = find(
    again,
    (el) =>
      Array.isArray(el.children) && el.children.some((k) => cls('ic-caret')(k)),
  );
  assert.equal(holder2.tag, 'p');
  assert.equal(textOf(holder2), 'Next');
  // An empty body keeps the caret in the body itself.
  const empty = { tag: 'div', children: [] };
  assert.equal(caretHost(empty), empty);
  const table = { tag: 'div', tagName: 'DIV', children: [] };
  const root = { tag: 'div', children: [table] };
  assert.equal(caretHost(root), root, 'never into a table wrapper');
});

test('the analyst menu is a disclosure, not an ARIA menu without menu keys', async () => {
  const { host, doc } = await mountView();
  const btn = find(host, (el) => el.attrs?.['aria-label'] === 'Analyst menu');
  assert.equal(btn.getAttribute('aria-haspopup'), null);
  const menu = find(host, cls('ic-menu'));
  assert.equal(menu.getAttribute('role'), null);
  assert.equal(btn.getAttribute('aria-controls'), menu.attrs.id);
  assert.equal(
    findAll(menu, (el) => el.attrs?.role === 'menuitem').length,
    0,
    'plain buttons',
  );
  btn.focus();
  btn.fire('click');
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(isHidden(menu), false, 'shown at once, before the next frame');
  assert.equal(doc.activeElement, btn, 'focus stays on the button');
  const root = find(host, cls('ic-chat'));
  root.fire('keydown', { key: 'Escape' });
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(isHidden(menu), true);
  assert.equal(doc.activeElement, btn);
});

test('reduced motion is read live: a system toggle after load steadies the caret and relabels a slip', async () => {
  const listeners = new Set();
  const mq = {
    matches: false,
    addEventListener: (t, f) => listeners.add(f),
    removeEventListener: (t, f) => listeners.delete(f),
  };
  const prev = globalThis.matchMedia;
  globalThis.matchMedia = () => mq;
  try {
    const { chat, host, view } = await mountView({ liveMotion: true });
    turn(chat);
    chat.event('text_delta', { turn_id: 't1', text: 'Working' }, next());
    assert.equal(find(host, cls('ic-caret')).getAttribute('data-steady'), null);
    launch(chat, { approve: false });
    const approve = () =>
      find(host, (el) => /^approve/.test(el.attrs?.['data-action'] ?? ''));
    assert.ok(!textOf(approve()).includes('Ready in 1 s'));
    mq.matches = true;
    for (const f of listeners) f({ matches: true });
    assert.ok(textOf(approve()).includes('Ready in 1 s'));
    view.destroy();
    assert.equal(listeners.size, 0, 'destroy removes the listener');
  } finally {
    globalThis.matchMedia = prev;
  }
});

test('reduced motion toggles the caret without a reload', async () => {
  const listeners = new Set();
  const mq = {
    matches: false,
    addEventListener: (t, f) => listeners.add(f),
    removeEventListener: (t, f) => listeners.delete(f),
  };
  const prev = globalThis.matchMedia;
  globalThis.matchMedia = () => mq;
  try {
    const { chat, host } = await mountView({ liveMotion: true });
    turn(chat);
    chat.event('text_delta', { turn_id: 't1', text: 'Working' }, next());
    mq.matches = true;
    for (const f of listeners) f({ matches: true });
    assert.equal(
      find(host, cls('ic-caret')).getAttribute('data-steady'),
      'true',
    );
  } finally {
    globalThis.matchMedia = prev;
  }
});

test('bidi controls from contact names and analyst args never reach the transcript (review: RLO spoofing)', async () => {
  const RLO = '\u202E';
  const graph = graphFixture({
    nodes: [
      {
        id: 'trk:T-6',
        type: 'track',
        label: `BMP ${RLO}YLDNEIRF\u202C convoy`,
        status: 'unknown',
        attrs: {},
      },
    ],
  });
  const { chat, host, emitted, announced } = await mountView({ graph });
  turn(chat, 'Who is [[trk:T-6]]?');
  chat.event(
    'text_delta',
    { turn_id: 't1', text: `That is [[trk:T-6]], ${RLO}evil.` },
    next(),
  );
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'c1',
      tool: 'uav_orbit_poi',
      title: `Orbit ${RLO}ynneD`,
      class: 'command',
      args: { vehicle: `Drone1${RLO}` },
      summary: `Drone1 · ${RLO}YLDNEIRF`,
    },
    next(),
  );
  chat.event(
    'approval_request',
    {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'uav_orbit_poi',
      class: 'command',
      title: `Orbit ${RLO}ynneD`,
      summary: `Drone1 · ${RLO}YLDNEIRF`,
      consequences: [`Drone1 orbits ${RLO}evil.`],
      args: { vehicle: 'Drone1' },
      vehicle: 'Drone1',
    },
    next(),
  );
  chat.event(
    'ui',
    { action: 'track', vehicle: 'Drone1', reason: `watch ${RLO}evil` },
    next(),
  );
  const text = textOf(host);
  assert.doesNotMatch(text, BIDI);
  assert.ok(text.includes('BMP YLDNEIRF convoy'), 'the chip label');
  assert.ok(text.includes('Approval waiting: Orbit ynneD, Drone1.'));
  assert.ok(text.includes('watching Drone1: “watch evil”.'), text);
  for (const [, payload] of emitted) {
    assert.doesNotMatch(JSON.stringify(payload ?? {}), BIDI);
  }
  for (const [t] of announced) assert.doesNotMatch(t, BIDI);
  // Tool-row args show the control as its JSON escape.
  chat.event(
    'tool_call',
    {
      turn_id: 't1',
      call_id: 'r1',
      tool: 'uav_get_telemetry',
      title: 'Read telemetry',
      class: 'read',
      args: { vehicle: `Drone1${RLO}` },
    },
    next(),
  );
  const row = findAll(host, cls('ic-row')).at(-1);
  find(row, cls('ic-row__head')).fire('click');
  const args = find(row, cls('ic-row__args'));
  assert.ok(textOf(args).includes('"vehicle": "Drone1\\u202e"'), textOf(args));
  assert.doesNotMatch(textOf(host), BIDI);
});

test('while reasoning streams, the thinking line is one truncating span plus the time', async () => {
  const { chat, host } = await mountView();
  turn(chat);
  chat.event(
    'thinking',
    {
      turn_id: 't1',
      text: 'I should list contacts, then check assessments for every one of them before I answer.',
    },
    next(),
  );
  const line = find(host, cls('ic-thinking'));
  assert.equal(line.attrs['data-open'], 'true');
  const span = find(line, cls('ic-thinking__text'));
  assert.ok(textOf(span).startsWith('Thinking: I should list contacts'));
  assert.equal(span.attrs.title, textOf(span).slice('Thinking: '.length));
});

test('chat.css pins: narrow slip stacking, registers, checkbox ink, one-line thinking, target sizes', () => {
  // Whitespace-normalized, so a selector prettier wraps still matches.
  const css = readFileSync(
    new URL('./chat.css', import.meta.url),
    'utf8',
  ).replace(/\s+/g, ' ');
  const rule = (selector) => {
    const i = css.indexOf(`${selector} {`);
    assert.ok(i >= 0, `missing rule ${selector}`);
    return css.slice(i, css.indexOf('}', i));
  };
  // §3c: the narrow tab's column (~420 px) is wider than the container query.
  assert.match(
    rule(":where(.ic-root[data-layout='narrow']) .ic-slip__actions"),
    /flex-direction:\s*column/,
  );
  assert.match(
    rule(":where(.ic-root[data-layout='narrow']) .ic-slip__btn[type='button']"),
    /width:\s*100%/,
  );
  assert.match(
    rule(
      ":where(.ic-root[data-layout='narrow']) .ic-slip__approve[type='button']",
    ),
    /margin-left:\s*0/,
  );
  // §2.6: a register is a quiet box like the inspector's; never underlined.
  assert.doesNotMatch(
    css,
    /\.ic-tag\[data-register='(estimated|requested)'\][^{]*\{[^}]*underline/,
  );
  assert.match(rule(':where(.ic-root) .ic-tag'), /border:\s*1px solid/);
  assert.match(
    rule(":where(.ic-root) .ic-tag[data-register='measured']"),
    /border-color:\s*transparent/,
  );
  // Checkboxes in ink, not the analyst-blue system accent.
  assert.match(
    rule(':where(.ic-root) .ic-slip__check'),
    /accent-color:\s*var\(--ic-ink/,
  );
  // One truncated thinking line.
  assert.match(
    rule(":where(.ic-root) .ic-thinking[data-open='true']"),
    /flex-wrap:\s*nowrap/,
  );
  assert.match(
    rule(':where(.ic-root) .ic-thinking__text'),
    /text-overflow:\s*ellipsis/,
  );
  // Three slip buttons keep one row.
  assert.match(
    rule(
      ":where(.ic-root .ic-slip__actions[data-count='3']) .ic-slip__btn[type='button']",
    ),
    /padding:\s*0 var\(--ic-s-4/,
  );
  // §9 targets: 32 px (44 px narrow) through --ic-target.
  for (const sel of [
    ":where(.ic-root) .ic-slip__link[type='button']",
    ":where(.ic-root) .ic-thinking__toggle[type='button']",
  ]) {
    assert.match(rule(sel), /min-height:\s*var\(--ic-target/);
  }
  assert.match(
    rule(":where(.ic-root) .ic-chip[type='button']::after"),
    /inset:\s*calc\(\(22px - var\(--ic-target, 32px\)\) \/ 2\) -1px/,
  );
  assert.match(
    rule(":where(.ic-root) .ic-composer__send[type='button']"),
    /width:\s*max\(40px, var\(--ic-target/,
  );
});

// ---- BYOK providers (BYOK spec §10) --------------------------------------------------------

const MINIMAX = {
  id: 'minimax',
  label: 'MiniMax',
  kind: 'anthropic_compatible',
};

test('the header reads "{model} via {label}" and follows provider_changed', async () => {
  const { chat, host } = await mountView({
    status: {
      available: true,
      model: 'claude-opus-5-5',
      provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
    },
  });
  await flush();
  const model = find(host, cls('ic-chat__model'));
  assert.equal(
    model.textContent,
    'claude-opus-5-5 via Claude login (this Mac)',
  );
  seq = 0;
  chat.event(
    'session',
    {
      session_id: 's1',
      model: 'claude-opus-5-5',
      provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
    },
    0,
  );
  chat.event('turn_start', { turn_id: 't1', text: 'x' }, next());
  chat.event('turn_end', { turn_id: 't1', stop: 'end' }, next());
  chat.event(
    'provider_changed',
    {
      from: { id: 'anthropic_login' },
      to: { ...MINIMAX, model: 'MiniMax-M3[1m]' },
      memory: 'cleared',
    },
    next(),
  );
  await flush();
  assert.equal(model.textContent, 'MiniMax-M3[1m] via MiniMax');
  assert.doesNotMatch(model.textContent, /·/);
  const div = findAll(host, cls('ic-divider')).at(-1);
  assert.equal(
    textOf(div),
    "The analyst now uses MiniMax (MiniMax-M3[1m]). It doesn't remember the conversation above.",
  );
  chat.event(
    'provider_changed',
    { to: { ...MINIMAX, model: 'MiniMax-M2.7' }, memory: 'kept' },
    next(),
  );
  await flush();
  assert.equal(
    textOf(findAll(host, cls('ic-divider')).at(-1)),
    'The analyst now uses MiniMax (MiniMax-M2.7).',
  );
});

test('the analyst menu opens analyst settings, after New session', async () => {
  const { host, emitted } = await mountView();
  const menuBtn = find(
    host,
    (el) => el.attrs?.['aria-label'] === 'Analyst menu',
  );
  menuBtn.fire('click');
  const menu = find(host, cls('ic-menu'));
  const items = menu.children.map((c) => textOf(c));
  assert.deepEqual(items, [
    'New session',
    'Analyst settings…',
    'Stop the analyst',
  ]);
  buttonNamed(menu, 'Analyst settings…').fire('click');
  const [name, payload] = emitted.find(([e]) => e === 'settings:open');
  assert.equal(name, 'settings:open');
  assert.equal(payload.section, 'llm');
  assert.equal(payload.invoker, menuBtn);
});

test('no provider set up: the panel offers Open analyst settings', async () => {
  const { host, emitted } = await mountView({
    status: {
      available: false,
      reason: 'provider_not_configured',
      hint: 'Open analyst settings.',
    },
  });
  await flush();
  const panel = find(host, cls('ic-unavailable'));
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    'The analyst has no model provider set up.',
  );
  assert.equal(
    textOf(find(panel, cls('ic-unavailable__body'))),
    'Add a key for Claude or another provider.',
  );
  const choose = buttonNamed(panel, 'Open analyst settings');
  assert.ok(choose && !isHidden(choose));
  assert.equal(choose.attrs['data-variant'], 'primary');
  assert.equal(
    buttonNamed(panel, 'Check again').attrs['data-variant'],
    'quiet',
  );
  choose.fire('click');
  const open = emitted.filter(([e]) => e === 'settings:open').at(-1)[1];
  assert.equal(open.section, 'llm');
  assert.equal(open.provider, undefined);
});

test('a missing key names the provider; the Claude sign-in offers an API key instead', async () => {
  const { host, emitted, chat } = await mountView({
    status: {
      available: false,
      reason: 'provider_key_missing',
      provider: MINIMAX,
    },
  });
  await flush();
  let panel = find(host, cls('ic-unavailable'));
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    'The analyst has no key for MiniMax.',
  );
  buttonNamed(panel, 'Open analyst settings').fire('click');
  assert.equal(
    emitted.filter(([e]) => e === 'settings:open').at(-1)[1].provider,
    'minimax',
  );
  assert.equal(
    emitted.filter(([e]) => e === 'analyst:availability').at(-1)[1].provider
      .label,
    'MiniMax',
  );
  assert.ok(isHidden(buttonNamed(panel, 'Use an API key instead')));
  // The Claude login failing sign-in: keep its copy, add the way out.
  chat.statusValue = { available: false, reason: 'auth' };
  await chat.status();
  await flush();
  panel = find(host, cls('ic-unavailable'));
  assert.equal(
    find(panel, cls('ic-unavailable__title')).textContent,
    "The analyst isn't signed in.",
  );
  const apiKey = buttonNamed(panel, 'Use an API key instead');
  assert.ok(!isHidden(apiKey));
  apiKey.fire('click');
  assert.equal(
    emitted.filter(([e]) => e === 'settings:open').at(-1)[1].provider,
    'anthropic_api',
  );
});

test('an unreliable cost basis hides dollars and says why', async () => {
  const { chat, host } = await mountView({
    status: {
      available: true,
      model: 'MiniMax-M3[1m]',
      provider: MINIMAX,
      cost_basis: 'unreliable',
    },
  });
  turn(chat);
  chat.event('text_delta', { turn_id: 't1', text: 'Done.' }, next());
  chat.event(
    'usage',
    {
      turn_id: 't1',
      input_tokens: 1200,
      output_tokens: 80,
      cost_basis: 'unreliable',
    },
    next(),
  );
  chat.event('turn_end', { turn_id: 't1', stop: 'end' }, next());
  await flush();
  const usage = textOf(find(host, cls('ic-msg__usage')));
  assert.match(usage, /in, 80 out/);
  assert.match(usage, /Cost isn't shown: MiniMax bills you directly\./);
  assert.doesNotMatch(usage, /\$/);
  assert.equal(find(host, cls('ic-chat__cost')).textContent, '');
  // Back on the Claude login: the header follows the live status, and the
  // turn that ran on MiniMax still says MiniMax billed it.
  chat.statusValue = {
    available: true,
    model: 'claude-opus-5',
    provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
    cost_basis: 'anthropic_list',
  };
  await chat.status();
  await flush();
  assert.equal(
    find(host, cls('ic-chat__model')).textContent,
    'claude-opus-5 via Claude login (this Mac)',
  );
  assert.match(
    textOf(find(host, cls('ic-msg__usage'))),
    /Cost isn't shown: MiniMax bills you directly\./,
  );
});

// ---- WG Phase A: theater and map directives, theater slips, chips ----------------------

const XSS = '<img src=x onerror=alert(1)>';
const THEATER_BBOX = [46.60898, 32.57838, 46.66186, 32.65536];

function theaterGraph(extra = {}) {
  const g = graphFixture();
  g.theater = {
    id: 'dyn-kherson',
    label: 'Kherson',
    epoch: 1,
    bbox: THEATER_BBOX,
    ...(extra.theater || {}),
  };
  g.nodes.push({
    id: 'thr:dyn-kherson',
    type: 'theater',
    label: extra.label ?? 'Kherson',
    status: 'ok',
    attrs: { bbox: THEATER_BBOX },
  });
  g.nodes.push({
    id: 'sit:dyn-kherson:way/1',
    type: 'site',
    label: 'Kherson airfield',
    status: 'ok',
    lat: 46.67,
    lon: 32.5,
    attrs: { category: 'airfield' },
  });
  return g;
}

test('ui theater writes one line with a chip and Show on map, and never moves the view', async () => {
  const { chat, host, emitted } = await mountView({ graph: theaterGraph() });
  turn(chat, 'Set the theater to Kherson');
  chat.event('ui', { action: 'theater', id: 'thr:dyn-kherson' }, next());
  chat.event('ui', { action: 'theater', id: 'thr:dyn-kherson' }, next());
  assert.deepEqual(
    emitted.filter(([e]) => e === 'map:request'),
    [],
    'the directive alone never opens the map',
  );
  const lines = findAll(host, cls('ic-directive'));
  assert.equal(lines.length, 2);
  assert.equal(textOf(lines[0]), textOf(lines[1]), 'idempotent');
  assert.ok(textOf(lines[0]).startsWith('Theater set to Kherson.'));
  const chip = find(lines[0], cls('ic-chip'));
  assert.equal(chip.attrs['data-id'], 'thr:dyn-kherson');
  assert.equal(chip.attrs.tabindex, '0');
  buttonNamed(lines[0], 'Show on map').fire('click');
  assert.deepEqual(
    emitted.filter(([e]) => e === 'map:request'),
    [
      [
        'map:request',
        {
          ids: ['thr:dyn-kherson'],
          bbox: THEATER_BBOX,
          label: 'Kherson',
          source: 'operator',
          countdown: false,
        },
      ],
    ],
  );
});

test('ui map: the gate decides between the 3 s notice and a static offer', async () => {
  const { chat, host, emitted, view } = await mountView({
    graph: theaterGraph(),
  });
  turn(chat, 'Show me');
  chat.event(
    'ui',
    { action: 'map', ids: ['thr:dyn-kherson'], reason: 'Watch the recce' },
    next(),
  );
  const first = emitted.filter(([e]) => e === 'map:request').at(-1)[1];
  assert.deepEqual(first, {
    ids: ['thr:dyn-kherson'],
    bbox: THEATER_BBOX,
    label: 'Kherson',
    reason: 'Watch the recce',
    source: 'analyst',
    countdown: true,
    gate: null,
  });
  let lines = findAll(host, cls('ic-directive'));
  assert.ok(
    textOf(lines.at(-1)).startsWith(
      'Asked to show Kherson on the map: Watch the recce.',
    ),
  );
  assert.ok(buttonNamed(lines.at(-1), 'Show on map'));
  // Composer text: no countdown, a static offer, and the "suggests" line.
  view.insert('half-typed');
  chat.event(
    'ui',
    { action: 'map', ids: ['sit:dyn-kherson:way/1'], reason: 'Airfield' },
    next(),
  );
  const second = emitted.filter(([e]) => e === 'map:request').at(-1)[1];
  assert.equal(second.countdown, false);
  assert.equal(second.gate, 'composer');
  assert.equal(second.label, 'Kherson airfield');
  lines = findAll(host, cls('ic-directive'));
  assert.ok(
    textOf(lines.at(-1)).startsWith(
      'The analyst suggests showing Kherson airfield on the map: “Airfield”.',
    ),
  );
  // Ids without a position: no request, the fixed line, no button.
  const before = emitted.filter(([e]) => e === 'map:request').length;
  chat.event('ui', { action: 'map', ids: ['trk:T-1'] }, next());
  assert.equal(emitted.filter(([e]) => e === 'map:request').length, before);
  lines = findAll(host, cls('ic-directive'));
  assert.equal(
    textOf(lines.at(-1)),
    'The analyst asked to show something without a location on the map.',
  );
  assert.equal(buttonNamed(lines.at(-1), 'Show on map'), null);
});

test('ui map never runs from replayed history', async () => {
  const { chat, emitted } = await mountView({ graph: theaterGraph() });
  seq = 0;
  chat.event('session', { session_id: 's1', last_seq: 2 }, 0);
  chat.event('turn_start', { turn_id: 't0', text: 'old' }, 1, { replay: true });
  chat.event('ui', { action: 'map', ids: ['thr:dyn-kherson'] }, 2, {
    replay: true,
  });
  assert.deepEqual(
    emitted.filter(([e]) => e === 'map:request'),
    [],
  );
});

function theaterRequest(chat, extra = {}) {
  chat.event(
    'approval_request',
    {
      approval_id: 'th1',
      call_id: 'cth1',
      tool: 'sim_set_theater',
      class: 'sim',
      title: 'Set the theater',
      summary: 'Kherson',
      consequences: ['Moves the simulation to Kherson.'],
      args: { label: 'Kherson' },
      expires_at_ms: T0 + 600_000,
      theater_preview: {
        place: 'Kherson, Ukraine',
        center: [46.63542, 32.61687],
        bbox: THEATER_BBOX,
        home: { lat: 46.638, lon: 32.619 },
        airframe: { label: 'Quad, small electric', reach_m: 7350 },
        ground_msl_m: 925,
        checks: [{ text: 'Drone1 on the ground', ok: true }],
      },
      ...extra,
    },
    next(),
  );
}

const approveIn = (root) =>
  find(root, (el) => /^approve/.test(el.attrs?.['data-action'] || ''));

test('a theater slip goes Deny-only when a drone takes off or the epoch moves on', async () => {
  const graph = theaterGraph();
  const { chat, host, store, announced } = await mountView({ graph });
  turn(chat, 'Move to Kherson');
  theaterRequest(chat);
  let slip = find(host, cls('ic-slip'));
  assert.ok(approveIn(slip), 'approvable while everything is landed');
  assert.ok(textOf(slip).includes('Right now Drone1 is on the ground'));
  assert.deepEqual(announced.at(-1), [
    'Approval needed: Set the theater. Changes the simulation.',
    'assertive',
  ]);
  // Drone1 takes off: the store change re-renders the slip as blocked.
  graph.nodes[0].attrs.landed = false;
  store.change();
  await flush();
  slip = find(host, cls('ic-slip'));
  assert.equal(approveIn(slip), null);
  assert.equal(
    textOf(find(slip, cls('ic-slip__denyonly'))),
    "This can't be approved now: Drone1 is airborne. Land Drone1 first, then ask again.",
  );
  // Landed again, but the theater epoch changed since the request.
  graph.nodes[0].attrs.landed = true;
  graph.theater = { ...graph.theater, epoch: 2 };
  store.change();
  await flush();
  slip = find(host, cls('ic-slip'));
  assert.equal(approveIn(slip), null);
  assert.equal(
    textOf(find(slip, cls('ic-slip__denyonly'))),
    "This can't be approved now: the theater changed after this request.",
  );
});

test('an injected class "zzz" and a theater slip without a preview are Deny-only in the view', async () => {
  const { chat, host, view } = await mountView({ graph: theaterGraph() });
  turn(chat, 'x');
  chat.event(
    'approval_request',
    {
      approval_id: 'z1',
      call_id: 'cz1',
      tool: 'zzz_tool',
      class: 'zzz',
      title: 'Something new',
      consequences: [],
      args: {},
      expires_at_ms: T0 + 600_000,
    },
    next(),
  );
  theaterRequest(chat, {
    approval_id: 'th2',
    call_id: 'cth2',
    theater_preview: undefined,
  });
  const slips = findAll(host, cls('ic-slip'));
  assert.equal(slips.length, 2);
  for (const slip of slips) assert.equal(approveIn(slip), null);
  assert.equal(slips[0].attrs['data-class'], 'unknown');
  assert.ok(textOf(slips[0]).includes('Unrecognised action'));
  assert.equal(
    textOf(find(slips[1], cls('ic-slip__denyonly'))),
    "The console couldn't build this preview, so it can't be approved.",
  );
  // The call's row carries the unknown class too (a stale band, never command).
  assert.equal(view.state.rows.cz1.klass, 'unknown');
});

test('directive lines render untrusted labels and reasons as text (§3.11)', async () => {
  const { chat, host } = await mountView({
    graph: theaterGraph({ label: `${XSS}‮evil‬` }),
  });
  turn(chat, 'x');
  chat.event('ui', { action: 'theater', id: 'thr:dyn-kherson' }, next());
  chat.event(
    'ui',
    { action: 'map', ids: ['thr:dyn-kherson'], reason: `${XSS}‮` },
    next(),
  );
  const lines = findAll(host, cls('ic-directive'));
  assert.equal(lines.length, 2);
  for (const line of lines) {
    assert.equal(
      find(line, (el) => el.tag === 'img'),
      null,
    );
    assert.equal(
      find(line, (el) => el.attrs && Object.hasOwn(el.attrs, 'onerror')),
      null,
    );
    assert.ok(textOf(line).includes('<img src=x onerror=alert(1)>'));
    assert.ok(!BIDI.test(textOf(line)));
  }
});

test('resolveMapArea: theater bbox, vector, padded points, 1 km minimum, missing', () => {
  const graph = theaterGraph();
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const lookup = (id) => byId.get(id) ?? null;
  assert.deepEqual(resolveMapArea(['thr:dyn-kherson'], lookup, graph), {
    bbox: THEATER_BBOX,
    label: 'Kherson',
    missing: [],
  });
  // The active theater's block when its node has no bbox.
  const bare = { ...graph, nodes: [] };
  assert.deepEqual(
    resolveMapArea(['thr:dyn-kherson'], () => null, bare).bbox,
    THEATER_BBOX,
  );
  // One point: at least 1 km each way, about the point.
  const one = resolveMapArea(['sit:dyn-kherson:way/1'], lookup, graph);
  const [s, w, n, e] = one.bbox;
  const hM = (n - s) * 111195;
  const wM = (e - w) * 111195 * Math.cos((46.67 * Math.PI) / 180);
  assert.ok(Math.abs(hM - 1000) < 1 && Math.abs(wM - 1000) < 1, `${hM} ${wM}`);
  assert.ok(Math.abs((s + n) / 2 - 46.67) < 1e-9);
  // Two far points: 15 % padding on each side of the span.
  const pts = new Map([
    ['veh:A', { id: 'veh:A', lat: 46.0, lon: 32.0 }],
    ['veh:B', { id: 'veh:B', lat: 46.2, lon: 32.4 }],
  ]);
  const two = resolveMapArea(['veh:A', 'veh:B'], (id) => pts.get(id), null);
  assert.deepEqual(
    two.bbox.map((v) => Number(v.toFixed(6))),
    [45.97, 31.94, 46.23, 32.46],
  );
  assert.equal(two.label, '2 items');
  // A vector: centre ± half-length × 1.15.
  const vec = {
    id: 'vec:cor-1',
    lat: 46.5,
    lon: 32.5,
    attrs: { length_m: 4000 },
  };
  const v = resolveMapArea(['vec:cor-1'], () => vec, null).bbox;
  assert.ok(Math.abs((v[2] - v[0]) * 111195 - 4600) < 1);
  // Nothing with a position.
  assert.deepEqual(resolveMapArea(['trk:T-1'], lookup, graph), {
    bbox: null,
    label: 'SA-6 battery',
    missing: ['trk:T-1'],
  });
  assert.equal(
    mapLineText({ reason: null }, { bbox: null }, null),
    'The analyst asked to show something without a location on the map.',
  );
});

test('shouldShowMap: the UX §6.9 gate', () => {
  const base = {
    composerText: '',
    composerFocused: false,
    lastStageInputAt: T0 - 10_000,
    now: T0,
    pendingCount: 0,
    mode: 'orb',
  };
  assert.deepEqual(shouldShowMap(base), { allowed: true, reason: null });
  assert.equal(shouldShowMap({ ...base, mode: 'map' }).allowed, true);
  assert.equal(shouldShowMap({ ...base, mode: 'tracking' }).reason, 'tracking');
  assert.equal(
    shouldShowMap({ ...base, composerText: 'x' }).reason,
    'composer',
  );
  assert.equal(
    shouldShowMap({ ...base, composerFocused: true }).reason,
    'composer',
  );
  assert.equal(
    shouldShowMap({ ...base, lastStageInputAt: T0 - 2999 }).reason,
    'stage_input',
  );
  assert.equal(
    shouldShowMap({ ...base, pendingCount: 1 }).reason,
    'slip_pending',
  );
});

test('chips: sites are known; an unrecognised type is never status-coloured', () => {
  globalThis.document = stubDoc();
  const store = fakeStore(theaterGraph());
  const site = createChip({ id: 'sit:dyn-kherson:way/1' }, { store });
  assert.equal(site.attrs['data-type'], 'site');
  assert.equal(textOf(site).includes('Kherson airfield'), true);
  store.graph.nodes.push({
    id: 'frc:red-sam-1',
    type: 'force',
    label: `${XSS}‮`,
    status: 'ok',
    attrs: {},
  });
  const odd = createChip({ id: 'frc:red-sam-1' }, { store });
  assert.equal(odd.attrs['data-status'], 'unknown', 'never ok/green');
  assert.equal(odd.attrs['data-type'], 'unrecognised');
  assert.equal(
    find(odd, (el) => el.tag === 'img'),
    null,
  );
  assert.ok(textOf(odd).includes('<img src=x onerror=alert(1)>'));
  assert.ok(!BIDI.test(textOf(odd)));
});
