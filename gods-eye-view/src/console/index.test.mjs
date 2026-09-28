import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { register } from 'node:module';

// The panels index.js mounts belong to other owners. Should one not exist in
// this checkout yet, resolve it to an empty stub: this test injects its own
// factories through `components`, so only the import has to succeed.
const OWNED_ELSEWHERE = {
  './orb/orb.js':
    'export function createOrb(){return null}\nexport function createOrbListView(){return null}',
  './chat/client.js': 'export function createChatClient(){return null}',
  './chat/view.js': 'export function createAnalyst(){return null}',
  './search.js': 'export function createSearch(){return null}',
  './inspector.js': 'export function createInspector(){return null}',
};
const hooks = `
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const STUBS = ${JSON.stringify(OWNED_ELSEWHERE)};
export async function resolve(specifier, context, next) {
  const stub = STUBS[specifier];
  if (stub && String(context.parentURL || '').endsWith('/src/console/index.js')) {
    const url = new URL(specifier, context.parentURL);
    if (!existsSync(fileURLToPath(url))) {
      return { url: 'data:text/javascript,' + encodeURIComponent(stub), shortCircuit: true };
    }
  }
  return next(specifier, context);
}`;
register(`data:text/javascript,${encodeURIComponent(hooks)}`);

const {
  COPY,
  ICON,
  analystUnavailableCopy,
  bannerModel,
  cameraFrozen,
  createPortProxy,
  honestyLine,
  invitation,
  layoutFor,
  mountIntelConsole,
  orbViewport,
  serviceLine,
  shieldFromDocumentCapture,
} = await import('./index.js');
const { createBus } = await import('./bus.js');
const { createIntelStore } = await import('./intelStore.js');

const flush = async (n = 4) => {
  for (let i = 0; i < n; i += 1) await new Promise((r) => setImmediate(r));
};

// ---- pure helpers -------------------------------------------------------------------

test('layoutFor: wide ≥ 1280, compact 720–1279, narrow < 720', () => {
  assert.equal(layoutFor(1440), 'wide');
  assert.equal(layoutFor(1280), 'wide');
  assert.equal(layoutFor(1279), 'compact');
  assert.equal(layoutFor(720), 'compact');
  assert.equal(layoutFor(719), 'narrow');
  assert.equal(layoutFor(0), 'wide');
  assert.equal(layoutFor(NaN), 'wide');
});

test('orbViewport follows the §3 sizes and never shrinks the orb at compact', () => {
  // 1440×900: stage 746 wide; margin labels need 96 px gutters.
  const wide = orbViewport({
    width: 746,
    height: 844,
    layout: 'wide',
    plateOpen: false,
  });
  assert.equal(wide.diameter, 554);
  assert.equal(wide.cx, 373);
  const plate = orbViewport({
    width: 746,
    height: 844,
    layout: 'wide',
    plateOpen: true,
  });
  assert.equal(plate.plate, 300);
  assert.ok(plate.diameter < wide.diameter);
  // Height-limited: a 16 px top margin keeps the pole caption on the stage.
  assert.equal(plate.cy, Math.round(16 + (844 - 40 - 300 - 24 - 16) / 2));
  assert.ok(plate.cy - plate.r >= 16);
  // 1024×700: compact, inline labels (24 px gutters); the plate is a sheet.
  const compact = orbViewport({
    width: 592,
    height: 644,
    layout: 'compact',
    plateOpen: true,
  });
  assert.equal(compact.diameter, 544);
  assert.equal(compact.plate, 0);
  // The 360 px minimum at compact and wide, the 880 px cap.
  assert.equal(
    orbViewport({ width: 600, height: 200, layout: 'compact' }).diameter,
    360,
  );
  assert.equal(
    orbViewport({ width: 2400, height: 1400, layout: 'wide' }).diameter,
    880,
  );
  assert.equal(
    orbViewport({ width: 300, height: 200, layout: 'narrow' }).diameter,
    120,
  );
});

test('honestyLine is built from meta, never invented', () => {
  const g = {
    scope: 'theater',
    meta: {
      counts: { track: 23 },
      duplicates_collapsed: 20,
      out_of_theater_contacts: 27,
      threat_assessed: 10,
      threat_unassessed: 2,
    },
  };
  assert.equal(
    honestyLine(g),
    '23 contacts in theater. 20 duplicates merged. 27 outside this theater. Threat assessed for 10 of 12.',
  );
  assert.equal(
    honestyLine({
      scope: 'all',
      meta: { counts: { track: 1 }, out_of_theater: 5 },
    }),
    '1 contact across all theaters.',
  );
  assert.equal(honestyLine(null), '');
});

test('invitation states what is missing, from state', () => {
  const vehicle = { id: 'veh:Drone1', type: 'vehicle', label: 'Drone1' };
  assert.deepEqual(
    invitation({ scope: 'theater', meta: { counts: {} }, nodes: [vehicle] }),
    {
      text: "No contacts yet. Drone1 hasn't scanned.",
    },
  );
  assert.deepEqual(
    invitation({
      scope: 'theater',
      meta: { counts: {}, out_of_theater_contacts: 27 },
      nodes: [],
    }),
    {
      text: 'No contacts in this theater. 27 are in other theaters.',
      action: 'all-theaters',
    },
  );
  assert.deepEqual(
    invitation({ theater: { label: 'Redmond' }, nodes: [], meta: {} }),
    {
      text: 'Nothing observed yet in Redmond.',
    },
  );
  assert.equal(invitation({ meta: { counts: { track: 3 } }, nodes: [] }), null);
  assert.equal(invitation(null), null);
});

test('bannerModel: kind label, verbatim message, BINGO tail, several, frozen view', () => {
  const bingo = {
    id: 'alarm:1',
    kind: 'bingo',
    message: 'Drone1 reached BINGO at 14:03:12Z',
  };
  assert.equal(
    bannerModel({ alarms: [bingo] }).text,
    "BINGO fuel: Drone1 reached BINGO at 14:03:12Z. The return can't be cancelled.",
  );
  const lost = {
    id: 'alarm:2',
    kind: 'lost_link',
    message: 'Drone1 link lost',
  };
  assert.equal(
    bannerModel({ alarms: [lost] }).text,
    'Lost link: Drone1 link lost',
  );
  assert.equal(
    bannerModel({ alarms: [bingo, lost, lost] }).text,
    '3 critical alarms',
  );
  const frozen = bannerModel({
    alarms: [lost],
    tracking: true,
    vehicle: { link: 'loal' },
  });
  assert.equal(
    frozen.text,
    'Lost link: Drone1 link lost. The camera view is frozen and is not live.',
  );
  assert.equal(frozen.frozen, true);
  const staleOnly = bannerModel({
    alarms: [],
    tracking: true,
    vehicle: { stale_ms: 6000 },
  });
  assert.deepEqual([staleOnly.text, staleOnly.count], [COPY.frozen, 0]);
  assert.equal(
    bannerModel({ alarms: [], tracking: false, vehicle: { link: 'loal' } })
      .text,
    '',
  );
  assert.equal(cameraFrozen({ link: 'up', stale_ms: 100 }), false);
});

test('bannerModel says the kind label once when the server message already opens with it', () => {
  // The bridge's real BINGO message (godseye_uav/bridge.py).
  const bingo = {
    id: 'alarm:1',
    kind: 'bingo',
    message: 'BINGO fuel - forcing RTB (fuel 18% at/below BINGO 20%)',
  };
  const text = bannerModel({ alarms: [bingo] }).text;
  assert.equal(
    text,
    "BINGO fuel - forcing RTB (fuel 18% at/below BINGO 20%). The return can't be cancelled.",
  );
  assert.equal(text.match(/BINGO fuel/g).length, 1);
  // Case-insensitive, and a message that doesn't open with it keeps the label.
  assert.equal(
    bannerModel({
      alarms: [{ kind: 'lost_link', message: 'lost link to Drone2' }],
    }).text,
    'lost link to Drone2',
  );
  assert.equal(
    bannerModel({
      alarms: [{ kind: 'geofence_breach', message: 'Drone1 out' }],
    }).text,
    'Geofence breach: Drone1 out',
  );
});

test('serviceLine speaks the boot and service copy deck', () => {
  assert.equal(
    serviceLine({ status: 'loading' }, { connecting: true }).text,
    COPY.connecting,
  );
  assert.equal(serviceLine({ status: 'loading' }).text, COPY.loading);
  assert.equal(
    serviceLine({ status: 'offline', error: { kind: 'offline' } }).text,
    COPY.offline,
  );
  assert.equal(
    serviceLine({
      status: 'offline',
      error: { kind: 'http', message: 'HTTP 500' },
    }).text,
    "The intel picture didn't load: HTTP 500. Retrying in 5 s.",
  );
  const at = Date.UTC(2026, 8, 27, 14, 31, 5);
  assert.equal(
    serviceLine({ status: 'stale', lastLiveAt: at }).text,
    "Lost contact with the local service at 14:31:05Z. Everything shown is from then and isn't live. Retrying every 5 s.",
  );
  assert.equal(serviceLine({ status: 'unauthorized' }).text, COPY.unauthorized);
  assert.equal(serviceLine({ status: 'live' }), null);
  assert.equal(
    serviceLine(
      { status: 'live' },
      { recoveredAt: Date.UTC(2026, 8, 27, 14, 32, 40) },
    ).text,
    'Back online. Picture updated at 14:32:40Z.',
  );
});

test('analystUnavailableCopy covers every /chat/status reason', () => {
  assert.equal(
    analystUnavailableCopy({ reason: 'sdk_missing' }).title,
    "The analyst isn't installed in this build.",
  );
  assert.equal(
    analystUnavailableCopy({ reason: 'cli_missing', hint: 'npm i -g x' }).hint,
    'npm i -g x',
  );
  assert.equal(
    analystUnavailableCopy({ reason: 'disabled' }).title,
    'The analyst is turned off for this launch.',
  );
  assert.equal(
    analystUnavailableCopy({ reason: 'auth' }).title,
    "The analyst isn't signed in.",
  );
  assert.equal(
    analystUnavailableCopy(null).title,
    "The analyst isn't available right now.",
  );
  assert.equal(
    analystUnavailableCopy({ reason: 'service_unavailable' }).title,
    "The analyst didn't start in this app.",
  );
});

test('createPortProxy waits for a port that arrives late and forwards its changes', async () => {
  let deliver;
  const proxy = createPortProxy(new Promise((r) => (deliver = r)));
  assert.equal(proxy.available, false);
  assert.equal(proxy.isTracking(), false);
  assert.equal(proxy.keyhole(), null);
  proxy.exit(); // safe before arrival
  const seen = [];
  proxy.onChange(() => seen.push('change'));
  const entering = proxy.enter('Drone1');
  let tracking = false;
  let cb = null;
  deliver({
    whenReady: async () => {},
    enter: async (v) => (tracking = v === 'Drone1'),
    exit: () => (tracking = false),
    isTracking: () => tracking,
    onChange: (fn) => ((cb = fn), () => (cb = null)),
    setMapVisible: () => {},
    keyhole: () => ({ x: 1, y: 2, r: 3 }),
  });
  assert.equal(await entering, true);
  assert.equal(proxy.available, true);
  assert.equal(proxy.isTracking(), true);
  assert.equal(proxy.supportsInset(), false);
  assert.deepEqual(proxy.keyhole(), { x: 1, y: 2, r: 3 });
  cb();
  assert.deepEqual(seen, ['change']);
  proxy.destroy();
  assert.equal(cb, null);

  const none = createPortProxy(null);
  assert.equal(await none.enter('Drone1'), false);
  await assert.rejects(none.whenReady());
  const lazy = createPortProxy(() => ({
    enter: async () => true,
    setViewportInset() {},
  }));
  assert.equal(await lazy.enter('Drone1'), true);
  assert.equal(lazy.supportsInset(), true);
});

test('shieldFromDocumentCapture hides a console key from document capture only', () => {
  const htmlListeners = [];
  const doc = {
    documentElement: {
      addEventListener: (type, fn, opts) =>
        htmlListeners.push({ type, fn, opts }),
    },
  };
  const event = {
    type: 'keydown',
    key: 'c',
    isComposing: false,
    defaultPrevented: false,
  };
  assert.equal(shieldFromDocumentCapture(event, doc), true);
  assert.equal(event.isComposing, true);
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(htmlListeners[0].opts, { capture: true, once: true });
  htmlListeners[0].fn(event);
  assert.equal(event.isComposing, false);
  assert.equal(event.defaultPrevented, false);
  assert.equal(shieldFromDocumentCapture(null, doc), false);
});

// ---- mounting under a stub DOM -------------------------------------------------------------

function stubDom() {
  const doc = { activeElement: null };
  const mk = (tag) => {
    const el = {
      tag,
      tagName: String(tag).toUpperCase(),
      children: [],
      attrs: {},
      listeners: {},
      className: '',
      textContent: '',
      value: '',
      parent: null,
      append(...kids) {
        for (const k of kids) {
          if (k && typeof k === 'object') {
            k.parent?.detach?.(k);
            k.parent = this;
          }
          this.children.push(k);
        }
      },
      detach(k) {
        this.children = this.children.filter((c) => c !== k);
      },
      replaceChildren(...kids) {
        for (const c of this.children)
          if (c && typeof c === 'object') c.parent = null;
        this.children = [];
        this.append(...kids);
      },
      remove() {
        this.parent?.detach(this);
        this.parent = null;
      },
      setAttribute(k, v) {
        this.attrs[k] = String(v);
      },
      removeAttribute(k) {
        delete this.attrs[k];
      },
      getAttribute(k) {
        return k in this.attrs ? this.attrs[k] : null;
      },
      addEventListener(t, f) {
        (this.listeners[t] ||= []).push(f);
      },
      removeEventListener(t, f) {
        this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f);
      },
      fire(t, ev = {}) {
        const event = { target: this, ...ev };
        for (const f of [...(this.listeners[t] || [])]) f(event);
        return event;
      },
      focus() {
        doc.activeElement = this;
      },
      contains(x) {
        for (let n = x; n; n = n.parent) if (n === this) return true;
        return false;
      },
    };
    return el;
  };
  doc.createElement = mk;
  doc.getElementById = () => null;
  doc.body = mk('body');
  doc.documentElement = mk('html');
  return doc;
}

function textOf(el) {
  if (el == null || el === false) return '';
  if (typeof el !== 'object') return String(el);
  if (el.attrs && 'hidden' in el.attrs) return '';
  return [el.textContent || '', ...(el.children || []).map(textOf)]
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function find(root, pred) {
  if (!root || typeof root !== 'object') return null;
  if (pred(root)) return root;
  for (const kid of root.children || []) {
    const hit = find(kid, pred);
    if (hit) return hit;
  }
  return null;
}

const byClass = (root, cls) =>
  find(root, (el) =>
    String(el.className || '')
      .split(/\s+/)
      .includes(cls),
  );
const isHidden = (el) => Boolean(el && 'hidden' in el.attrs);

function fakeClock(start = 7_000_000) {
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

function fakeWin(width = 1440) {
  const listeners = {};
  const observers = [];
  class RO {
    constructor(cb) {
      this.cb = cb;
      this.targets = [];
      observers.push(this);
    }
    observe(t) {
      this.targets.push(t);
    }
    disconnect() {
      this.targets = [];
    }
  }
  return {
    innerWidth: width,
    innerHeight: 900,
    ResizeObserver: RO,
    observers,
    listeners,
    addEventListener(t, f, capture) {
      (listeners[t] ||= []).push({ f, capture });
    },
    removeEventListener(t, f) {
      listeners[t] = (listeners[t] || []).filter((l) => l.f !== f);
    },
    key(ev) {
      const event = {
        defaultPrevented: false,
        stopped: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {
          this.stopped = true;
        },
        ...ev,
      };
      for (const l of listeners.keydown || []) if (l.capture) l.f(event);
      return event;
    },
    resize(target, width) {
      for (const o of observers) {
        if (o.targets.includes(target))
          o.cb([{ target, contentRect: { width, height: 800 } }]);
      }
    },
  };
}

function stubComponents(log) {
  const record =
    (name) =>
    (...args) =>
      log.calls.push([name, ...args]);
  return {
    createOrb(canvas, opts) {
      log.orbOpts = opts;
      log.orbCanvas = canvas;
      return {
        setGraph: record('orb.setGraph'),
        highlight: record('orb.highlight'),
        filter: record('orb.filter'),
        focus: record('orb.focus'),
        select: record('orb.select'),
        resize: record('orb.resize'),
        destroy: record('orb.destroy'),
        project: () => ({ x: 400, y: 300, front: true }),
        onFrame: () => () => {},
        setViewport: record('orb.setViewport'),
        snapshot: () => null,
        setOptions: record('orb.setOptions'),
      };
    },
    createOrbListView: () => ({ setGraph() {}, filter() {}, destroy() {} }),
    createChatClient() {
      const listeners = {};
      log.chat = {
        status: async () =>
          log.chatStatus ?? { available: true, model: 'claude' },
        on(event, cb) {
          (listeners[event] ||= []).push(cb);
          return () => {};
        },
        emit(event, data) {
          for (const cb of listeners[event] || []) cb(data);
        },
        interrupt: record('chat.interrupt'),
        close: record('chat.close'),
      };
      return log.chat;
    },
    createAnalyst(host, ctx) {
      log.analystHost = host;
      log.ctx = ctx;
      return {
        setLayout: record('analyst.setLayout'),
        setDocked: record('analyst.setDocked'),
        focusComposer: record('analyst.focusComposer'),
        reviewApprovals: record('analyst.reviewApprovals'),
        composerIdle: () => true,
        destroy: record('analyst.destroy'),
      };
    },
    createSearch(host) {
      log.searchHost = host;
      return {
        open: record('search.open'),
        close() {},
        destroy: record('search.destroy'),
      };
    },
    createInspector(host) {
      log.inspectorHost = host;
      let cur = null;
      return (log.inspector = {
        show: (id) => {
          cur = id;
          log.calls.push(['inspector.show', id]);
        },
        hide: () => {
          cur = null;
          log.calls.push(['inspector.hide']);
        },
        current: () => cur,
        setLayout: record('inspector.setLayout'),
        destroy: record('inspector.destroy'),
      });
    },
    createSituation(host) {
      log.railHost = host;
      return {
        setLayout: record('situation.setLayout'),
        destroy: record('situation.destroy'),
      };
    },
    confirmAbort: (ctx, vehicle) => {
      log.calls.push(['confirmAbort', vehicle]);
      return Promise.resolve(false);
    },
    createSettingsSheet(host, ctx) {
      log.settingsHost = host;
      let open = false;
      log.settings = {
        open(o) {
          open = true;
          log.calls.push([
            'settings.open',
            o?.provider ?? null,
            o?.invoker ?? null,
          ]);
          ctx.bus.emit('settings:state', { open: true });
        },
        close() {
          open = false;
          log.calls.push(['settings.close']);
          ctx.bus.emit('settings:state', { open: false });
        },
        escape() {
          if (!open) return false;
          log.settings.close();
          return true;
        },
        isOpen: () => open,
        setLayout: record('settings.setLayout'),
        destroy: record('settings.destroy'),
      };
      return log.settings;
    },
  };
}

function graphWith(nodes = [], extra = {}) {
  return {
    scope: 'theater',
    theater: { id: 'default', label: 'Redmond (AirSim default)' },
    nodes,
    edges: [],
    meta: { counts: { track: 0 }, threat_assessed: 0, threat_unassessed: 0 },
    ...extra,
  };
}

async function mount({
  width = 1440,
  graph = graphWith(),
  port = null,
  chatStatus,
} = {}) {
  const doc = stubDom();
  const saved = globalThis.document;
  globalThis.document = doc;
  const log = { calls: [], chatStatus };
  const clock = fakeClock();
  const win = fakeWin(width);
  const bus = createBus();
  const api = {
    replies: [graph],
    get(path) {
      log.calls.push(['api.get', path]);
      return Promise.resolve(
        this.replies.length > 1 ? this.replies.shift() : this.replies[0],
      );
    },
    post: async () => ({}),
    sse: () => ({ close() {} }),
    abortVehicle: async () => ({ aborted: true }),
  };
  const store = createIntelStore({
    api,
    bus,
    clock,
    doc: null,
    storage: null,
    alarmStream: false,
  });
  const host = doc.createElement('div');
  const handle = mountIntelConsole({
    root: host,
    config: { api, bus, store, base: '', token: 't' },
    trackingPort: port,
    components: stubComponents(log),
    win,
    clock,
  });
  await flush();
  const restore = () => {
    handle.destroy();
    globalThis.document = saved;
  };
  return {
    doc,
    log,
    clock,
    win,
    bus,
    api,
    store,
    host,
    handle,
    el: handle.elements,
    restore,
  };
}

test('mounts .ic-root with its regions, ctx and every panel in its host', async () => {
  const t = await mount();
  try {
    const root = t.el.root;
    assert.equal(t.host.children[0], root);
    assert.equal(root.className, 'ic-root');
    assert.equal(root.attrs['data-mode'], 'orb');
    assert.equal(root.attrs['data-layout'], 'wide');
    assert.equal(root.attrs['data-analyst'], 'on');
    for (const key of [
      'api',
      'store',
      'orb',
      'chat',
      'bus',
      'mode',
      'root',
      'config',
    ]) {
      assert.ok(t.handle.ctx[key], `ctx.${key}`);
    }
    assert.equal(t.handle.ctx.root, root);
    assert.equal(t.log.ctx, t.handle.ctx, 'panels receive the shared ctx');
    assert.equal(t.log.railHost, t.el.railHost);
    assert.equal(t.log.searchHost, t.el.searchHost);
    assert.equal(t.log.inspectorHost, t.el.plate);
    assert.equal(t.log.analystHost, t.el.analystBody);
    assert.equal(t.log.orbCanvas, t.el.canvas);
    assert.equal(t.log.orbOpts.a11yHost, t.el.orbA11y);
    assert.equal(t.el.canvas.attrs['aria-hidden'], 'true');
    // Skip links first, then live regions; DOM order = focus order.
    assert.deepEqual(
      textOf(byClass(root, 'ic-skips')),
      `${COPY.skipSearch} ${COPY.skipAnalyst}`,
    );
    assert.equal(t.el.livePolite.attrs['aria-live'], 'polite');
    assert.equal(t.el.liveAssertive.attrs['aria-live'], 'assertive');
    // Theater graph polled; the orb got it; the footer says what is (not) there.
    assert.ok(
      t.log.calls.some(
        (c) => c[0] === 'api.get' && c[1] === '/intel/graph?scope=theater',
      ),
    );
    const setGraph = t.log.calls.find((c) => c[0] === 'orb.setGraph');
    assert.equal(setGraph[2].first, true);
    assert.match(
      textOf(t.el.footer),
      /Nothing observed yet in Redmond \(AirSim default\)\./,
    );
    assert.match(textOf(t.el.footer), /0 contacts in theater\./);
    assert.ok(t.log.calls.some((c) => c[0] === 'orb.setViewport'));
    // The GEV-free icon map carries only glyph names from §11.5.
    assert.ok(Object.isFrozen(ICON));
  } finally {
    t.restore();
  }
});

test('a critical alarm raises the stage banner until viewed; View inspects it', async () => {
  const alarm = {
    id: 'alarm:4',
    type: 'alarm',
    label: 'Bingo',
    subtitle: 'Drone1 reached BINGO at 14:03:12Z',
    status: 'critical',
    ts_ms: 1000,
    attrs: { seq: 4, kind: 'bingo', severity: 'critical', vehicle: 'Drone1' },
  };
  const t = await mount({ graph: graphWith([alarm]) });
  try {
    assert.equal(isHidden(t.el.stageBanner), false);
    assert.equal(
      textOf(t.el.stageBanner).replace(/^error /, ''),
      "BINGO fuel: Drone1 reached BINGO at 14:03:12Z. The return can't be cancelled. View",
    );
    const inspected = [];
    t.bus.on('inspect', (p) => inspected.push(p.id));
    const view = find(t.el.stageBanner, (el) => el.tag === 'button');
    view.fire('click');
    assert.deepEqual(inspected, ['alarm:4']);
    assert.deepEqual(t.store.unviewedCritical(), []);
    assert.equal(isHidden(t.el.stageBanner), true);
  } finally {
    t.restore();
  }
});

test('the analyst column collapses to a spine when the analyst is unavailable', async () => {
  const t = await mount({
    chatStatus: { available: false, reason: 'sdk_missing' },
  });
  try {
    const seen = [];
    assert.equal(t.el.root.attrs['data-analyst'], 'off');
    assert.equal(t.el.root.attrs['data-spine'], 'on');
    assert.equal(isHidden(t.el.spine), false);
    assert.equal(isHidden(t.el.analystBody), true);
    t.el.spine.fire('click');
    assert.equal(isHidden(t.el.spinePop), false);
    assert.match(
      textOf(t.el.spinePop),
      /The analyst isn't installed in this build\./,
    );
    assert.match(textOf(t.el.spinePop), /Check again/);
    // The analyst coming back (from the view) restores the column.
    t.bus.on('analyst:availability', (p) => seen.push(p));
    t.bus.emit('analyst:availability', { available: true });
    assert.equal(t.el.root.attrs['data-analyst'], 'on');
    assert.equal(isHidden(t.el.spine), true);
    // A service-level failure never collapses the column.
    t.log.chat.emit('status', {
      available: false,
      reason: 'unreachable',
      transient: true,
    });
    assert.equal(t.el.root.attrs['data-analyst'], 'on');
  } finally {
    t.restore();
  }
});

test('an analyst the page never reached collapses to the spine until /chat/status answers', async () => {
  const t = await mount({
    chatStatus: {
      available: false,
      reason: 'unreachable',
      transient: true,
    },
  });
  try {
    const seen = [];
    t.bus.on('analyst:availability', (p) => seen.push(p));
    assert.equal(t.el.root.attrs['data-analyst'], 'off');
    assert.equal(t.el.root.attrs['data-spine'], 'on');
    assert.equal(isHidden(t.el.analystBody), true);
    t.el.spine.fire('click');
    assert.match(
      textOf(t.el.spinePop),
      /The analyst isn't available right now\./,
    );
    // The view's own retry reaches it: the column comes back.
    t.log.chat.emit('status', { available: true, model: 'claude' });
    assert.equal(t.el.root.attrs['data-analyst'], 'on');
    assert.equal(isHidden(t.el.spine), true);
    assert.deepEqual(seen.at(-1), {
      available: true,
      reason: null,
      hint: null,
    });
  } finally {
    t.restore();
  }
});

test('a map that fails to start says so in a toast; tracking is not offered as working', async () => {
  const t = await mount();
  try {
    t.bus.emit('gev:status', { state: 'starting', phase: 'scene' });
    assert.equal(isHidden(t.el.toasts), true);
    t.bus.emit('gev:status', {
      state: 'failed',
      phase: null,
      message: 'WebGL is not available',
    });
    assert.equal(isHidden(t.el.toasts), false);
    assert.match(
      textOf(t.el.toasts),
      /The map didn't start: WebGL is not available\. Tracking isn't available; the orb, search and the analyst still work\./,
    );
    const close = find(
      t.el.toasts,
      (el) => el.tag === 'button' && el.attrs['aria-label'] === COPY.close,
    );
    close.fire('click');
    assert.equal(isHidden(t.el.toasts), true);
  } finally {
    t.restore();
  }
});

test('capture-phase keys: ⌘K and / open search; GEV never sees keys while the map is hidden', async () => {
  const t = await mount();
  try {
    const cmdK = t.win.key({ key: 'k', metaKey: true, target: t.doc.body });
    assert.equal(cmdK.defaultPrevented, true);
    assert.equal(cmdK.stopped, true);
    assert.equal(t.log.calls.filter((c) => c[0] === 'search.open').length, 1);

    const slash = t.win.key({ key: '/', target: t.el.stage });
    assert.equal(slash.defaultPrevented, true);
    assert.equal(t.log.calls.filter((c) => c[0] === 'search.open').length, 2);

    // "/" typed into a text field is text.
    const field = t.doc.createElement('input');
    t.el.searchHost.append(field);
    const opened = t.log.calls.filter((c) => c[0] === 'search.open').length;
    const typed = t.win.key({
      key: '/',
      target: field,
      isComposing: false,
      defaultPrevented: false,
    });
    assert.equal(typed.stopped, false);
    t.doc.documentElement.listeners.keydown.at(-1)(typed); // <html> capture restores
    assert.equal(typed.defaultPrevented, false);
    assert.equal(
      t.log.calls.filter((c) => c[0] === 'search.open').length,
      opened,
    );

    // A bare letter aimed outside the console is swallowed in orb mode…
    const h = t.win.key({ key: 'h', target: t.doc.body });
    assert.equal(h.stopped, true);
    assert.equal(h.defaultPrevented, false);
    // …and one inside it is shielded from GEV's document capture listeners.
    const c = t.win.key({ key: 'c', target: t.el.stage, isComposing: false });
    assert.equal(c.stopped, false);
    assert.equal(c.isComposing, true);
    const restore = t.doc.documentElement.listeners.keydown.at(-1);
    restore(c);
    assert.equal(c.isComposing, false);

    // The root's bubble listener keeps console keys from GEV's shortcuts.
    const bubble = t.el.root.fire('keydown', {
      key: 'o',
      stopPropagation() {
        this.stopped = true;
      },
    });
    assert.equal(bubble.stopped, true);

    // ⌘I goes to the composer; ⌘. stops the analyst; F6 cycles regions.
    t.win.key({ key: 'i', ctrlKey: true, target: t.doc.body });
    assert.ok(t.log.calls.some((c2) => c2[0] === 'analyst.focusComposer'));
    t.win.key({ key: '.', metaKey: true, target: t.doc.body });
    assert.ok(t.log.calls.some((c2) => c2[0] === 'chat.interrupt'));
    t.win.key({ key: 'F6', target: t.doc.body });
    assert.equal(t.doc.activeElement, t.el.railHost);
    t.win.key({ key: 'F6', target: t.el.railHost });
    assert.equal(t.doc.activeElement, t.el.stage);
    t.win.key({ key: 'F6', shiftKey: true, target: t.el.stage });
    assert.equal(t.doc.activeElement, t.el.railHost);

    // ? opens the shortcut sheet, Esc closes it first.
    t.win.key({ key: '?', target: t.doc.body });
    assert.equal(isHidden(t.el.sheet), false);
    t.win.key({ key: 'Escape', target: t.doc.body });
    assert.equal(isHidden(t.el.sheet), true);
  } finally {
    t.restore();
  }
});

test('Esc peels layers: analyst focus, then filter, then inspector, then selection', async () => {
  const t = await mount();
  try {
    t.log.orbOpts.onSelect('trk:T-1');
    assert.ok(
      t.log.calls.some((c) => c[0] === 'inspector.show' && c[1] === 'trk:T-1'),
    );
    t.bus.emit('search:filter', {
      ids: ['trk:T-1'],
      query: 'sa-6',
      source: 'search',
    });
    t.bus.emit('focus:entities', {
      ids: ['trk:T-1', 'trk:T-2'],
      by: 'analyst',
      note: 'two unassessed',
    });
    const focus = t.log.calls.filter((c) => c[0] === 'orb.focus').at(-1);
    assert.deepEqual(focus.slice(1), [
      ['trk:T-1', 'trk:T-2'],
      { by: 'analyst', camera: true },
    ]);
    assert.equal(textOf(t.el.caption), 'Analyst: two unassessed Clear');

    const esc = () => t.win.key({ key: 'Escape', target: t.doc.body });
    esc();
    assert.deepEqual(
      t.log.calls
        .filter((c) => c[0] === 'orb.focus')
        .at(-1)
        .slice(1, 2),
      [[]],
    );
    assert.equal(isHidden(t.el.caption), true);
    const filters = [];
    t.bus.on('search:filter', (p) => filters.push(p));
    esc();
    assert.deepEqual(filters, [{ ids: null, query: '', source: 'shell' }]);
    esc();
    assert.ok(t.log.calls.some((c) => c[0] === 'inspector.hide'));
    esc();
    assert.deepEqual(t.log.calls.filter((c) => c[0] === 'orb.select').at(-1), [
      'orb.select',
      null,
    ]);
  } finally {
    t.restore();
  }
});

test('analyst focus after recent stage input does not move the camera', async () => {
  const t = await mount();
  try {
    t.store.noteStageInput();
    t.bus.emit('focus:entities', { ids: ['a:1', 'b:1', 'c:1'], by: 'analyst' });
    const focus = t.log.calls.filter((c) => c[0] === 'orb.focus').at(-1);
    assert.deepEqual(focus[2], { by: 'analyst', camera: false });
    assert.equal(
      textOf(t.el.caption),
      'The analyst pointed at 3 entities. Show',
    );
  } finally {
    t.restore();
  }
});

test('the ResizeObserver drives data-layout and tells every panel', async () => {
  const t = await mount();
  try {
    const layouts = [];
    t.bus.on('layout', (p) => layouts.push(p.layout));
    t.win.resize(t.el.root, 1024);
    assert.equal(t.el.root.attrs['data-layout'], 'compact');
    t.win.resize(t.el.root, 400);
    assert.equal(t.el.root.attrs['data-layout'], 'narrow');
    assert.deepEqual(layouts, ['compact', 'narrow']);
    assert.ok(
      t.log.calls.some(
        (c) => c[0] === 'situation.setLayout' && c[1] === 'narrow',
      ),
    );
    assert.ok(
      t.log.calls.some(
        (c) => c[0] === 'analyst.setLayout' && c[1] === 'compact',
      ),
    );
    assert.ok(
      t.log.calls.some(
        (c) =>
          c[0] === 'orb.setOptions' &&
          c[1].labelBudget === 4 &&
          c[1].labelMode === 'inline',
      ),
    );
    // Narrow: tabs, one section at a time.
    assert.equal(isHidden(t.el.narrowBar), false);
    assert.equal(isHidden(t.el.stage), false);
    assert.equal(isHidden(t.el.analyst), true);
    t.el.tabs.analyst.fire('click');
    assert.equal(t.el.root.attrs['data-tab'], 'analyst');
    assert.equal(isHidden(t.el.stage), true);
    assert.equal(t.el.stage.attrs.inert, '');
    assert.equal(isHidden(t.el.analyst), false);
    // A pending slip shows on the Analyst tab badge and, elsewhere, as a banner.
    t.bus.emit('approval:pending', {
      count: 1,
      oldest: { title: 'Grid search', vehicle: 'Drone1' },
    });
    assert.equal(textOf(t.el.tabAnalystBadge), '1');
    t.el.tabs.orb.fire('click');
    assert.equal(
      textOf(t.el.narrowApproval),
      'Approval waiting: Grid search, Drone1. Review',
    );
    // An analyst focus never switches tabs: it badges the Orb tab.
    t.el.tabs.situation.fire('click');
    t.bus.emit('focus:entities', { ids: ['a:1', 'b:1'], by: 'analyst' });
    assert.equal(t.el.root.attrs['data-tab'], 'situation');
    assert.equal(textOf(t.el.tabOrbBadge), '2');
  } finally {
    t.restore();
  }
});

function trackingPort({ inset = true } = {}) {
  const calls = [];
  let tracking = false;
  const port = {
    calls,
    whenReady: async () => {},
    enter: async () => (tracking = true),
    exit: () => {
      tracking = false;
      calls.push('exit');
    },
    isTracking: () => tracking,
    onChange: () => () => {},
    setMapVisible: (b) => calls.push(`map:${b}`),
    openMissionPanel: () => calls.push('panel'),
    keyhole: () => ({ x: 500, y: 400, r: 180 }),
  };
  if (inset) port.setViewportInset = (i) => calls.push(`inset:${i.right}`);
  return port;
}

test('tracking: the dock header shows the vehicle, fuel vs BINGO and Abort; Back returns', async () => {
  const vehicle = {
    id: 'veh:Drone1',
    type: 'vehicle',
    label: 'Drone1',
    status: 'ok',
    attrs: {
      fuel_pct: 64,
      bingo_fuel_pct: 22,
      link: 'up',
      landed: false,
      mission: 'MSN-1',
    },
  };
  const mission = {
    id: 'msn:MSN-1',
    type: 'mission',
    label: 'Grid search · Drone1',
    status: 'ok',
    attrs: { kind: 'grid_search', progress_pct: 42, vehicle: 'Drone1' },
  };
  const port = trackingPort();
  const t = await mount({
    graph: graphWith([vehicle, mission], {
      edges: [{ a: 'veh:Drone1', b: 'msn:MSN-1', kind: 'flying' }],
    }),
    port,
  });
  try {
    assert.equal(isHidden(t.el.dock), true);
    t.bus.emit('track:request', { vehicle: 'Drone1', source: 'operator' });
    await flush(8);
    await t.clock.advance(400); // the iris and the fade
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.equal(isHidden(t.el.dock), false);
    assert.equal(textOf(t.el.dockLine), 'Tracking Drone1 Grid search 42%');
    assert.match(textOf(t.el.dockFuel), /Fuel 64% BINGO 22% 42 points above/);
    assert.match(textOf(t.el.dockFuel), /Link up/);
    assert.equal(
      textOf(t.el.dockAbort).replace(/^pan_tool /, ''),
      'Abort Drone1',
    );
    assert.ok(port.calls.includes('map:true'));
    assert.ok(port.calls.some((c) => c.startsWith('inset:')));
    assert.ok(
      t.log.calls.some((c) => c[0] === 'analyst.setDocked' && c[1] === true),
    );
    assert.equal(
      t.el.main.attrs.inert,
      '',
      'the orb side is inert under the map',
    );

    t.el.dockAbort.fire('click');
    assert.deepEqual(
      t.log.calls.filter((c) => c[0] === 'confirmAbort'),
      [['confirmAbort', 'Drone1']],
    );

    // ⌘\ collapses the dock to the tab; the inset follows.
    t.win.key({ key: '\\', metaKey: true, target: t.doc.body });
    assert.equal(t.el.root.attrs['data-dock'], 'collapsed');
    assert.equal(isHidden(t.el.dockTab), false);
    assert.equal(port.calls.at(-1), 'inset:0');
    t.el.dockTab.fire('click');
    assert.equal(t.el.root.attrs['data-dock'], 'open');

    t.el.dockBack.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.ok(port.calls.includes('exit'));
    assert.equal(port.calls.filter((c) => c === 'map:false').length, 1);
    assert.equal(isHidden(t.el.dock), true);
  } finally {
    t.restore();
  }
});

test('an unavailable analyst (a 56 px spine in orb mode) still insets the map by the full dock width', async () => {
  const port = trackingPort();
  const t = await mount({
    port,
    chatStatus: { available: false, reason: 'disabled' },
  });
  try {
    assert.equal(t.el.root.attrs['data-spine'], 'on');
    // What a browser measures: the spine in orb mode, the column in tracking.
    t.el.analyst.getBoundingClientRect = () => ({
      width: t.el.root.attrs['data-spine'] === 'on' ? 56 : 446,
    });
    t.handle.ctx.mode.requestTrack('Drone1', { source: 'operator' });
    await flush(8);
    await t.clock.advance(400); // the iris and the fade
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.equal(t.el.root.attrs['data-spine'], 'off');
    // 1440 wide: clamp(420px, 31vw, 520px) = 446, never the spine's 56.
    const insets = port.calls.filter((c) => c.startsWith('inset:'));
    assert.equal(insets.at(-1), 'inset:446');
    assert.ok(!insets.includes('inset:56'));
  } finally {
    t.restore();
  }
});

test('without setViewportInset the dock enters collapsed (the fallback)', async () => {
  const port = trackingPort({ inset: false });
  const t = await mount({ port });
  try {
    t.bus.emit('track:request', { vehicle: 'Drone1', source: 'operator' });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.equal(t.el.root.attrs['data-dock'], 'collapsed');
    assert.equal(isHidden(t.el.dockTab), false);
  } finally {
    t.restore();
  }
});

test('entering tracking shows "Opening Drone1\'s camera" with the elapsed time and Stay in console', async () => {
  const port = trackingPort();
  port.whenReady = () => new Promise(() => {});
  const t = await mount({ port });
  try {
    t.handle.ctx.mode.requestTrack('Drone1', { source: 'operator' });
    await flush();
    assert.equal(t.el.root.attrs['data-mode'], 'entering_tracking');
    assert.equal(isHidden(t.el.caption), false);
    assert.match(textOf(t.el.caption), /Opening Drone1's camera/);
    await t.clock.advance(2000);
    assert.match(textOf(t.el.caption), /2 s/);
    find(
      t.el.caption,
      (el) => el.tag === 'button' && textOf(el) === 'Stay in console',
    ).fire('click');
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.equal(isHidden(t.el.caption), true);
  } finally {
    t.restore();
  }
});

test('mode notices render as toasts with working actions', async () => {
  const t = await mount({ port: trackingPort() });
  try {
    t.handle.ctx.mode.requestTrack('Drone1', {
      source: 'analyst',
      reason: 'best angle',
    });
    assert.equal(isHidden(t.el.toasts), false);
    assert.match(
      textOf(t.el.toasts),
      /The analyst suggests watching Drone1: "best angle"\. Opening Drone1's camera in 3 s\./,
    );
    const stay = find(
      t.el.toasts,
      (el) => el.tag === 'button' && textOf(el) === 'Stay in console',
    );
    stay.fire('click');
    assert.equal(isHidden(t.el.toasts), true);
    await t.clock.advance(4000);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
  } finally {
    t.restore();
  }
});

test('destroy tears down panels, listeners, the store and the root', async () => {
  const t = await mount();
  const { log, win, el, host } = t;
  t.restore();
  for (const name of [
    'analyst.destroy',
    'search.destroy',
    'inspector.destroy',
    'situation.destroy',
    'orb.destroy',
    'chat.close',
  ]) {
    assert.ok(
      log.calls.some((c) => c[0] === name),
      name,
    );
  }
  assert.equal((win.listeners.keydown || []).length, 0);
  assert.equal(host.children.includes(el.root), false);
});

test('index.js imports only names its sibling modules export', () => {
  const src = readFileSync(new URL('./index.js', import.meta.url), 'utf8');
  const imports = [
    ...src.matchAll(/import\s*\{([^}]+)\}\s*from\s*'(\.[^']+)'/g),
  ];
  for (const [, names, spec] of imports) {
    const url = new URL(spec, import.meta.url);
    if (!existsSync(url)) continue; // owned elsewhere, not landed yet
    const target = readFileSync(url, 'utf8');
    for (const name of names
      .split(',')
      .map((n) => n.trim())
      .filter(Boolean)) {
      const exported = new RegExp(
        `export\\s+(?:async\\s+)?(?:function|const|let|class)\\s+${name}\\b|export\\s*\\{[^}]*\\b${name}\\b`,
      );
      assert.match(target, exported, `${spec} exports ${name}`);
    }
  }
});

test("a sheet's Review goes to the analyst's oldest slip (narrow switches tabs)", async () => {
  const t = await mount({ width: 420 });
  try {
    t.bus.emit('approval:review', {});
    assert.equal(t.el.root.attrs['data-tab'], 'analyst');
    assert.ok(t.log.calls.some(([name]) => name === 'analyst.reviewApprovals'));
  } finally {
    t.restore();
  }
});

test('tracking: the dock names the mission kind in words, never raw snake case', async () => {
  // Live E2E: an approved orbit read "Tracking Drone1 Orbit poi 5%".
  const vehicle = {
    id: 'veh:Drone1',
    type: 'vehicle',
    label: 'Drone1',
    status: 'ok',
    attrs: { fuel_pct: 90, bingo_fuel_pct: 21, link: 'up', landed: false },
  };
  const mission = {
    id: 'msn:MSN-2',
    type: 'mission',
    label: 'Orbit poi · Drone1',
    status: 'ok',
    attrs: { kind: 'orbit_poi', progress_pct: 5, vehicle: 'Drone1' },
  };
  const port = trackingPort();
  const t = await mount({
    graph: graphWith([vehicle, mission], {
      edges: [{ a: 'veh:Drone1', b: 'msn:MSN-2', kind: 'flying' }],
    }),
    port,
  });
  try {
    t.bus.emit('track:request', { vehicle: 'Drone1', source: 'operator' });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.equal(textOf(t.el.dockLine), 'Tracking Drone1 Orbit 5%');
  } finally {
    t.restore();
  }
});

test('tracking CSS: the full-root chip tether never takes input over the map or the dock', () => {
  // Live E2E: in tracking, `.ic-root[data-mode='tracking'] > *` re-enabled
  // pointer events on every root child, including the 100% × 100% tether
  // canvas (z-index 5), so the map, "Back to console" and the dock's Abort
  // could not be clicked once any chip had been hovered.
  const css = readFileSync(new URL('./console.css', import.meta.url), 'utf8');
  const rules = [
    ...css.matchAll(
      /\.ic-root\[data-mode='tracking'\]\s*>\s*([^{]+)\{([^}]*)\}/g,
    ),
  ].filter((m) => /pointer-events:\s*auto/.test(m[2]));
  assert.ok(rules.length > 0, 'the tracking input rule exists');
  for (const [, selector] of rules) {
    assert.match(
      selector,
      /:not\([^)]*\.ic-tether/,
      `tracking rule "${selector.trim()}" must exclude the tether`,
    );
  }
  const chat = readFileSync(
    new URL('./chat/chat.css', import.meta.url),
    'utf8',
  );
  assert.match(chat, /\.ic-tether\s*\{[^}]*pointer-events:\s*none/);
});

// ---- a11y and craft review fixes ------------------------------------------------------

test('F6 walks stage -> inspector -> transcript while the inspector is open', async () => {
  const t = await mount();
  try {
    t.log.orbOpts.onSelect('trk:T-1'); // opens the inspector
    assert.equal(t.el.plate.attrs.tabindex, '-1', 'the plate can take focus');
    const f6 = (target) => t.win.key({ key: 'F6', target });
    t.el.stage.focus();
    f6(t.el.stage);
    assert.equal(t.doc.activeElement, t.el.plate, 'stage -> inspector');
    f6(t.el.plate);
    assert.equal(
      t.doc.activeElement,
      t.el.analystBody,
      'inspector -> transcript, never stuck on the stage',
    );
    // From a control inside the inspector, too.
    const inside = t.doc.createElement('button');
    t.el.plate.append(inside);
    inside.focus();
    f6(inside);
    assert.equal(t.doc.activeElement, t.el.analystBody);
    t.win.key({ key: 'F6', shiftKey: true, target: t.el.analystBody });
    assert.equal(t.doc.activeElement, t.el.plate);
  } finally {
    t.restore();
  }
});

test("closing the '?' sheet puts focus back where it was opened", async () => {
  const t = await mount();
  try {
    t.el.viewOrb.focus();
    t.win.key({ key: '?', target: t.el.viewOrb });
    assert.equal(t.doc.activeElement, t.el.sheetClose);
    t.el.root.fire('keydown', { key: 'Escape', target: t.el.sheetClose });
    assert.equal(isHidden(t.el.sheet), true);
    assert.equal(t.doc.activeElement, t.el.viewOrb, 'Esc');
    t.win.key({ key: '?', target: t.el.viewOrb });
    t.el.sheetClose.fire('click');
    assert.equal(t.doc.activeElement, t.el.viewOrb, 'Close');
  } finally {
    t.restore();
  }
});

test('L on the orb listbox switches to List view and keeps focus in the view', async () => {
  const t = await mount();
  try {
    const twin = t.doc.createElement('div');
    twin.className = 'ic-orb-twin';
    t.el.orbA11y.append(twin);
    twin.focus();
    t.log.orbOpts.onAction({ action: 'list' });
    assert.equal(t.el.viewList.attrs['aria-pressed'], 'true');
    assert.equal(isHidden(t.el.orbWrap), true);
    assert.equal(t.doc.activeElement, t.el.viewList, 'never the body');
    // Back to the orb with focus inside the list: the listbox twin takes it.
    const inList = t.doc.createElement('button');
    t.el.listHost.append(inList);
    inList.focus();
    t.el.viewOrb.fire('click');
    assert.equal(t.doc.activeElement, twin);
  } finally {
    t.restore();
  }
});

test('"Opening Drone1\'s camera" ticks in place: Stay in console keeps focus and the seconds are not announced', async () => {
  const port = trackingPort();
  port.whenReady = () => new Promise(() => {});
  const t = await mount({ port });
  try {
    t.handle.ctx.mode.requestTrack('Drone1', { source: 'operator' });
    await flush();
    const stay = find(
      t.el.caption,
      (el) => el.tag === 'button' && textOf(el) === 'Stay in console',
    );
    const detail = byClass(t.el.caption, 'ic-caption__detail');
    assert.equal(detail.attrs['aria-hidden'], 'true');
    stay.focus();
    await t.clock.advance(1000);
    await t.clock.advance(1000);
    assert.match(textOf(t.el.caption), /2 s/);
    assert.equal(
      find(t.el.caption, (el) => el.tag === 'button'),
      stay,
      'the button was not rebuilt',
    );
    assert.equal(t.doc.activeElement, stay);
    assert.equal(byClass(t.el.caption, 'ic-caption__detail'), detail);
  } finally {
    t.restore();
  }
});

test('tracking: Back to console is the first stop (skip links hidden) and its name has no "Esc"', async () => {
  const t = await mount({ port: trackingPort() });
  try {
    assert.equal(isHidden(t.el.skipSearch), false);
    t.bus.emit('track:request', { vehicle: 'Drone1', source: 'operator' });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.equal(isHidden(t.el.skipSearch), true);
    assert.equal(isHidden(t.el.skipAnalyst), true);
    const kbd = find(t.el.dockBack, (el) => el.tag === 'kbd');
    assert.equal(kbd.attrs['aria-hidden'], 'true');
    t.el.dockBack.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(isHidden(t.el.skipSearch), false);
  } finally {
    t.restore();
  }
});

test('narrow tabs follow the ARIA tabs pattern: controls, tabpanels, Home/End, worded badge', async () => {
  const t = await mount();
  try {
    t.win.resize(t.el.root, 400);
    assert.equal(t.el.tabs.orb.attrs['aria-controls'], 'ic-region-stage');
    assert.equal(t.el.tabs.analyst.attrs['aria-controls'], 'ic-region-analyst');
    assert.equal(t.el.tabs.situation.attrs['aria-controls'], 'ic-region-rail');
    assert.equal(t.el.stage.attrs.role, 'tabpanel');
    assert.equal(t.el.stage.attrs['aria-labelledby'], 'ic-tab-orb');
    assert.equal(t.el.rail.attrs['aria-labelledby'], 'ic-tab-situation');
    const press = (key) =>
      t.el.tabs[t.el.root.attrs['data-tab']].fire('keydown', {
        key,
        preventDefault() {},
      });
    press('End');
    assert.equal(t.el.root.attrs['data-tab'], 'situation');
    assert.equal(t.doc.activeElement, t.el.tabs.situation);
    press('Home');
    assert.equal(t.el.root.attrs['data-tab'], 'orb');
    assert.equal(t.doc.activeElement, t.el.tabs.orb);
    const badge = t.el.tabAnalystBadge;
    assert.equal(badge.attrs['aria-hidden'], 'true');
    // Wider again: plain regions, no tabpanel roles.
    t.win.resize(t.el.root, 1440);
    assert.equal(t.el.stage.attrs.role, undefined);
  } finally {
    t.restore();
  }
});

test('narrow tracking: the dock sheet is a bottom inset, so GEV lifts its alarm toasts above it', async () => {
  const port = trackingPort();
  const insets = [];
  port.setViewportInset = (i) => insets.push(i);
  const t = await mount({ port });
  try {
    t.win.resize(t.el.root, 400);
    t.bus.emit('track:request', { vehicle: 'Drone1', source: 'operator' });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.deepEqual(insets.at(-1), { right: 0, bottom: 450 });
  } finally {
    t.restore();
  }
});

test('tracking CSS: at narrow the critical banner starts under the floating Back to console', () => {
  const css = readFileSync(new URL('./console.css', import.meta.url), 'utf8');
  assert.match(
    css,
    /\.ic-root\[data-mode='tracking'\]:where\(\[data-layout='narrow'\]\)\s*:where\(\.ic-mapbanner\) \{\s*top: 64px;/,
  );
});

test('search CSS: results never let the id print over the name, and narrow drops the id', () => {
  const css = readFileSync(new URL('./panels.css', import.meta.url), 'utf8');
  assert.match(
    css,
    /grid-template-columns: 16px minmax\(0, 1fr\) fit-content\(40%\);/,
  );
  assert.match(
    css,
    /\.ic-search__id \{\s*max-width: 100%;\s*overflow: hidden;\s*text-overflow: ellipsis;\s*white-space: nowrap;/,
  );
  assert.match(
    css,
    /:where\(\.ic-root\[data-layout='narrow'\]\) \.ic-search__id \{\s*display: none;/,
  );
  // No segment is ever cut off at a sheet edge (the BINGO sentence).
  assert.doesNotMatch(css, /\.ic-kit-part \{\s*white-space: nowrap;/);
});

// ---- analyst settings (BYOK spec §10) ------------------------------------------------------

// A button's text ends with its label (an icon's ligature word may lead).
const buttonWith = (root, text) =>
  find(root, (el) => el.tag === 'button' && textOf(el).endsWith(text));

test('⌘, opens analyst settings; the column under it is inert; Esc closes it first', async () => {
  const t = await mount();
  try {
    assert.equal(t.log.settingsHost, t.el.root);
    const ev = t.win.key({ key: ',', metaKey: true, target: t.doc.body });
    assert.equal(ev.defaultPrevented, true);
    assert.deepEqual(
      t.log.calls.filter((c) => c[0] === 'settings.open').map((c) => c[1]),
      [null],
    );
    assert.ok('inert' in t.el.analyst.attrs);
    assert.ok(!('inert' in t.el.stage.attrs));
    // Esc: the innermost layer is the sheet.
    t.el.root.fire('keydown', {
      key: 'Escape',
      target: t.el.root,
      preventDefault() {},
      stopPropagation() {},
    });
    assert.ok(t.log.calls.some((c) => c[0] === 'settings.close'));
    assert.ok(!('inert' in t.el.analyst.attrs));
    // Ctrl+, too (not a Mac), and the bus event with a provider.
    t.win.key({ key: ',', ctrlKey: true, target: t.doc.body });
    t.log.settings.close();
    t.bus.emit('settings:open', { section: 'llm', provider: 'minimax' });
    assert.equal(
      t.log.calls.filter((c) => c[0] === 'settings.open').at(-1)[1],
      'minimax',
    );
  } finally {
    t.restore();
  }
});

test('narrow: the sheet covers every region, and a tab switch leaves it', async () => {
  const t = await mount({ width: 420 });
  try {
    t.bus.emit('settings:open', {});
    assert.ok('inert' in t.el.stage.attrs);
    assert.ok('inert' in t.el.analyst.attrs);
    t.el.tabs.analyst.fire('click');
    assert.equal(t.log.settings.isOpen(), false);
  } finally {
    t.restore();
  }
});

test('review: over the spine the sheet covers the stage, which goes inert', async () => {
  const t = await mount({
    chatStatus: { available: false, reason: 'provider_key_missing' },
  });
  try {
    assert.equal(t.el.root.attrs['data-spine'], 'on');
    assert.ok(!('inert' in t.el.stage.attrs));
    t.el.spine.fire('click');
    buttonWith(t.el.spinePop, 'Open analyst settings').fire('click');
    assert.ok(t.log.settings.isOpen());
    // The sheet is wider than the spine: the stage's search and Orb/List
    // toggle sit under it, so nothing there takes a click or a Tab.
    assert.ok('inert' in t.el.stage.attrs);
    t.log.settings.close();
    assert.ok(!('inert' in t.el.stage.attrs));
  } finally {
    t.restore();
  }
});

test('the spine popover opens analyst settings for a provider reason', async () => {
  const t = await mount({
    chatStatus: {
      available: false,
      reason: 'provider_auth',
      provider: { id: 'minimax', label: 'MiniMax' },
    },
  });
  try {
    t.el.spine.fire('click');
    assert.match(textOf(t.el.spinePop), /MiniMax rejected the analyst's key\./);
    const open = buttonWith(t.el.spinePop, 'Open analyst settings');
    assert.equal(open.attrs['data-variant'], 'primary');
    assert.ok(buttonWith(t.el.spinePop, 'Check again'));
    open.fire('click');
    const call = t.log.calls.filter((c) => c[0] === 'settings.open').at(-1);
    assert.equal(call[1], 'minimax');
    assert.equal(call[2], t.el.spine);
    assert.equal(isHidden(t.el.spinePop), true);
  } finally {
    t.restore();
  }
});

test('no provider set up: Open analyst settings; a Claude sign-in failure: Use an API key instead', async () => {
  const t = await mount({
    chatStatus: { available: false, reason: 'provider_not_configured' },
  });
  try {
    t.el.spine.fire('click');
    assert.match(
      textOf(t.el.spinePop),
      /The analyst has no model provider set up\./,
    );
    assert.ok(buttonWith(t.el.spinePop, 'Open analyst settings'));
    assert.equal(buttonWith(t.el.spinePop, 'Choose a provider'), null);
    t.log.chat.emit('status', { available: false, reason: 'auth' });
    t.el.spine.fire('click');
    t.el.spine.fire('click');
    const apiKey = buttonWith(t.el.spinePop, 'Use an API key instead');
    assert.ok(apiKey);
    apiKey.fire('click');
    assert.equal(
      t.log.calls.filter((c) => c[0] === 'settings.open').at(-1)[1],
      'anthropic_api',
    );
  } finally {
    t.restore();
  }
});

test('the shortcut sheet lists analyst settings', async () => {
  const t = await mount();
  try {
    const own = (el) =>
      [
        el.textContent,
        ...el.children.filter((c) => typeof c === 'string'),
      ].join('');
    const row = find(
      t.el.sheet,
      (el) => el.tag === 'dd' && own(el) === 'Analyst settings',
    );
    assert.ok(row);
    const key = find(
      t.el.sheet,
      (el) => el.tag === 'kbd' && /,$/.test(own(el)),
    );
    assert.match(own(key), /^(⌘|Ctrl\+),$/);
  } finally {
    t.restore();
  }
});

test('analystUnavailableCopy covers the provider reasons', () => {
  const c = analystUnavailableCopy({
    reason: 'provider_key_missing',
    provider: { id: 'deepseek', label: 'DeepSeek' },
    hint: 'Open analyst settings.',
  });
  assert.equal(c.title, 'The analyst has no key for DeepSeek.');
  assert.equal(c.action, 'Open analyst settings');
  assert.equal(c.provider, 'deepseek');
  // The server's "Open analyst settings." only repeats the button.
  assert.equal(c.hint, '');
  assert.equal(
    analystUnavailableCopy({ reason: 'auth' }).secondary,
    'Use an API key instead',
  );
  assert.equal(
    analystUnavailableCopy({ reason: 'sdk_missing' }).action,
    undefined,
  );
});

// ---- map overview (WG spec §4.2.3, §4.2.4, §4.2.7) --------------------------------------

test('createPortProxy: the map methods degrade when the port has none', async () => {
  const bare = createPortProxy({ enter: async () => true });
  assert.equal(bare.supports('showArea'), false);
  assert.equal(await bare.showArea({ bbox: [0, 0, 1, 1] }), false);
  assert.equal(bare.enterOverview(), undefined);
  assert.equal(bare.exitOverview(), undefined);
  assert.equal(bare.setOverlayVisibility({ sites: true }), undefined);
  assert.equal(bare.overlayStats(), null);
  const off = bare.onPick(() => {});
  assert.equal(typeof off, 'function');
  off();
  const none = createPortProxy(null);
  assert.equal(none.supports('showArea'), false);
  assert.equal(await none.showArea({ bbox: [0, 0, 1, 1] }), false);
  none.enterOverview();
  none.destroy();
});

test('createPortProxy forwards the map methods, picks and a late port', async () => {
  const calls = [];
  let pickCb = null;
  const full = createPortProxy({
    showArea: async (t, o) => {
      calls.push(['showArea', t, o]);
      return true;
    },
    enterOverview: () => calls.push(['enterOverview']),
    exitOverview: () => calls.push(['exitOverview']),
    setOverlayVisibility: (v) => calls.push(['vis', v]),
    overlayStats: () => ({ sites: { drawn: 3, total: 4 } }),
    onPick: (cb) => {
      pickCb = cb;
      return () => {
        pickCb = null;
      };
    },
  });
  assert.equal(full.supports('showArea'), true);
  assert.equal(full.supports('onPick'), true);
  assert.equal(full.supports('launchMissiles'), false);
  assert.equal(
    await full.showArea({ bbox: [1, 2, 3, 4] }, { animate: false }),
    true,
  );
  full.enterOverview();
  full.exitOverview();
  full.setOverlayVisibility({ sites: false });
  assert.deepEqual(calls, [
    ['showArea', { bbox: [1, 2, 3, 4] }, { animate: false }],
    ['enterOverview'],
    ['exitOverview'],
    ['vis', { sites: false }],
  ]);
  assert.deepEqual(full.overlayStats(), { sites: { drawn: 3, total: 4 } });
  const picks = [];
  full.onPick((p) => picks.push(p));
  pickCb({ id: 'sit:x:node/1' });
  assert.deepEqual(picks, [{ id: 'sit:x:node/1' }]);
  full.destroy();
  assert.equal(pickCb, null, 'destroy unsubscribes from the port');

  // The port's own supports() wins (the deferred port knows its real one).
  const own = createPortProxy({ supports: (n) => n === 'showArea' });
  assert.equal(own.supports('showArea'), true);
  assert.equal(own.supports('onPick'), false);
  // showArea waits for a port that arrives late; false from it is false.
  let resolve;
  const late = createPortProxy(new Promise((r) => (resolve = r)));
  assert.equal(late.supports('showArea'), false);
  const pending = late.showArea({ bbox: [1, 2, 3, 4] });
  resolve({ showArea: async () => false });
  assert.equal(await pending, false);
  assert.equal(late.supports('showArea'), true);
});

test("createPortProxy reads GEV's overlayStatus() for the dock counts", () => {
  const status = { sites: { drawn: 150, total: 212, served: 212 } };
  const gev = createPortProxy({ overlayStatus: () => status });
  assert.deepEqual(gev.overlayStats(), status);
  const idle = createPortProxy({ overlayStatus: () => null });
  assert.equal(idle.overlayStats(), null);
});

const THEATER_BBOX = [12.9491, 77.5715, 12.9941, 77.6176];

function theaterGraph(label = 'Bengaluru centre', { sites = 41 } = {}) {
  return graphWith(
    [
      {
        id: 'thr:dyn-blr',
        type: 'theater',
        label,
        attrs: { active: true, bbox: THEATER_BBOX },
      },
      { id: 'veh:Drone1', type: 'vehicle', label: 'Drone1', attrs: {} },
    ],
    {
      theater: { id: 'dyn-blr', label, epoch: 1, bbox: THEATER_BBOX },
      meta: { counts: { track: 0 }, sites: { total: sites } },
    },
  );
}

function mapPort() {
  const port = trackingPort();
  const pickCbs = new Set();
  return Object.assign(port, {
    showArea: async (target, opts) => {
      port.calls.push(`area:${target.bbox.join(',')}:${opts?.animate}`);
      return true;
    },
    enterOverview: () => port.calls.push('overview:on'),
    exitOverview: () => port.calls.push('overview:off'),
    setOverlayVisibility: (v) => port.calls.push(`sites:${v.sites}`),
    onPick(cb) {
      pickCbs.add(cb);
      return () => pickCbs.delete(cb);
    },
    pick: (id) => {
      for (const cb of [...pickCbs]) cb({ id });
    },
  });
}

async function openMap(t) {
  t.el.viewMap.fire('click');
  await flush(8);
  await t.clock.advance(400); // the iris and the fade
}

test('Map in the toggle is disabled with the reason when the port cannot show areas', async () => {
  const t = await mount({ port: trackingPort(), graph: theaterGraph() });
  try {
    const map = t.el.viewMap;
    assert.equal(textOf(map).replace(/^map /, ''), 'Map');
    assert.equal(map.attrs['aria-disabled'], 'true');
    assert.equal(map.attrs.title, "The map can't show areas in this build.");
    map.fire('click');
    await flush();
    assert.match(
      textOf(t.el.toasts),
      /The map can't show areas in this build\./,
    );
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.match(
      textOf(t.el.livePolite),
      /The map can't show areas in this build\./,
    );
  } finally {
    t.restore();
  }
});

test('Map opens the overview on the theater: dock "Map  label  W × H km", sites, ODbL; Esc returns', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    assert.equal(t.el.viewMap.attrs['aria-disabled'], undefined);
    await openMap(t);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(t.el.viewMap.attrs['aria-pressed'], 'true');
    assert.deepEqual(port.calls.slice(0, 4), [
      `area:${THEATER_BBOX.join(',')}:false`,
      'map:true',
      `inset:${port.calls.find((c) => c.startsWith('inset:')).slice(6)}`,
      'overview:on',
    ]);
    assert.ok(port.calls.includes('sites:true'));
    const dock = t.el.mapDock.element;
    assert.equal(isHidden(dock), false);
    assert.equal(isHidden(t.el.dock), true, 'not the tracking dock');
    assert.equal(
      textOf(byClass(dock, 'ic-mapdock__line')),
      'Map Bengaluru centre 5.0 × 5.0 km',
    );
    assert.match(textOf(dock), /Track Drone1/);
    assert.match(textOf(dock), /Show Sites 41/);
    assert.match(
      textOf(dock),
      /Map data: © OpenStreetMap contributors, ODbL\./,
    );
    assert.match(
      textOf(t.el.livePolite),
      /Map of Bengaluru centre\. Press Escape to return to the console\./,
    );
    assert.ok(
      t.log.calls.some((c) => c[0] === 'analyst.setDocked' && c[1] === true),
    );
    assert.equal(t.el.main.attrs.inert, '', 'the orb side is inert');

    // The Sites switch reaches the overlay.
    const box = byClass(dock, 'ic-mapdock__check');
    box.checked = false;
    box.fire('change');
    assert.equal(port.calls.at(-1), 'sites:false');
    assert.doesNotMatch(textOf(dock), /ODbL/, 'no sites drawn, no attribution');

    // Esc (from outside the console, e.g. the map) is the innermost layer.
    const ev = t.win.key({ key: 'Escape', target: t.doc.body });
    assert.equal(ev.defaultPrevented, true);
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.ok(port.calls.includes('overview:off'));
    assert.equal(port.calls.at(-2), 'map:false');
    assert.equal(isHidden(dock), true);
  } finally {
    t.restore();
  }
});

test('M in the orb scope opens the map; never from a text field or outside the orb', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    const field = t.doc.createElement('input');
    field.type = 'text';
    t.el.searchHost.append(field);
    t.win.key({ key: 'm', target: field });
    const rail = t.doc.createElement('button');
    t.el.railHost.append(rail);
    t.win.key({ key: 'm', target: rail });
    await flush(8);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    const ev = t.win.key({ key: 'm', target: t.el.stage });
    assert.equal(ev.defaultPrevented, true);
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    // The shortcut sheet names it.
    assert.match(textOf(t.el.sheet.children[1]), /M Map view/);
  } finally {
    t.restore();
  }
});

test('a map pick opens the inspector as a sheet over the dock; Esc closes it, then the map', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    const inspects = [];
    t.bus.on('inspect', (p) => inspects.push(p.id));
    port.pick('sit:dyn-blr:node/1');
    assert.deepEqual(inspects, [], 'picks count only in the map');
    await openMap(t);
    assert.equal(t.el.plate.parent, t.el.mapSheet, 'the host moved');
    assert.equal(isHidden(t.el.mapSheet), false);
    assert.deepEqual(
      t.log.calls.filter((c) => c[0] === 'inspector.setLayout').at(-1),
      ['inspector.setLayout', 'compact'],
    );
    port.pick('sit:dyn-blr:node/1');
    assert.deepEqual(inspects, ['sit:dyn-blr:node/1']);
    t.log.inspector.show('sit:dyn-blr:node/1'); // the real one hears 'inspect'
    t.win.key({ key: 'Escape', target: t.doc.body });
    assert.ok(t.log.calls.some((c) => c[0] === 'inspector.hide'));
    assert.equal(t.el.root.attrs['data-mode'], 'map', 'the sheet went first');
    t.win.key({ key: 'Escape', target: t.doc.body });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.equal(t.el.plate.parent, t.el.stageBottom, 'back on the stage');
    assert.deepEqual(t.el.stageBottom.children, [
      t.el.caption,
      t.el.plate,
      t.el.footer,
    ]);
    assert.equal(isHidden(t.el.mapSheet), true);
    assert.deepEqual(
      t.log.calls.filter((c) => c[0] === 'inspector.setLayout').at(-1),
      ['inspector.setLayout', 'wide'],
    );
  } finally {
    t.restore();
  }
});

test('Track from the map dock: tracking, then Esc goes back to the map, Back to console to the orb', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    await openMap(t);
    const track = find(
      t.el.mapDock.element,
      (el) => el.tag === 'button' && /Track Drone1/.test(textOf(el)),
    );
    track.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    assert.ok(port.calls.includes('overview:off'));
    assert.equal(isHidden(t.el.mapDock.element), true);
    assert.equal(isHidden(t.el.dock), false);
    // Esc inside the console, in tracking: the innermost layer is the map.
    t.el.root.fire('keydown', {
      key: 'Escape',
      stopPropagation() {},
      preventDefault() {},
    });
    await flush(8);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(isHidden(t.el.mapDock.element), false);
    assert.equal(port.calls.at(-1), `area:${THEATER_BBOX.join(',')}:true`);
    t.el.mapDock.back.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
  } finally {
    t.restore();
  }
});

test("the orb's theater notice: caption with Show on map for 20 s, announced, the hidden map pre-positioned", async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    t.log.orbOpts.onNotice({
      kind: 'theater',
      count: 12,
      ids: ['thr:dyn-blr'],
      text: 'Theater changed to Bengaluru centre. 12 items arrived.',
      label: 'Bengaluru centre',
      holdMs: 20000,
      announce: 'polite',
    });
    await flush();
    assert.equal(
      textOf(t.el.caption),
      'Theater changed to Bengaluru centre. 12 items arrived. Show on map',
    );
    assert.equal(t.el.caption.attrs['data-kind'], 'theater');
    assert.match(textOf(t.el.livePolite), /Theater changed to Bengaluru/);
    assert.equal(port.calls.at(-1), `area:${THEATER_BBOX.join(',')}:false`);
    await t.clock.advance(19000);
    assert.match(textOf(t.el.caption), /Theater changed/);
    await t.clock.advance(1500);
    assert.equal(isHidden(t.el.caption), true, 'gone after holdMs');

    // Show on map opens the map on the theater at once.
    t.log.orbOpts.onNotice({
      kind: 'theater',
      ids: [],
      to: { id: 'dyn-blr' },
      text: 'Theater changed to Bengaluru centre. 0 items arrived.',
      holdMs: 20000,
    });
    const show = find(
      t.el.caption,
      (el) => el.tag === 'button' && textOf(el) === 'Show on map',
    );
    show.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(isHidden(t.el.caption), true);
  } finally {
    t.restore();
  }
});

test('without showArea the theater caption has no Show on map', async () => {
  const t = await mount({ port: trackingPort(), graph: theaterGraph() });
  try {
    t.log.orbOpts.onNotice({
      kind: 'theater',
      ids: ['thr:dyn-blr'],
      text: 'Theater changed to Bengaluru centre. 1 item arrived.',
      holdMs: 20000,
    });
    assert.equal(
      textOf(t.el.caption),
      'Theater changed to Bengaluru centre. 1 item arrived.',
    );
  } finally {
    t.restore();
  }
});

test('opening the map by keyboard, or returning to it from tracking, puts focus in the map dock', async () => {
  // Review: Enter on the Map toggle (or M on the orb, or Esc from tracking)
  // left focus on <body>; the toggle and the tracking dock were gone.
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    t.el.viewMap.focus();
    await openMap(t);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(t.doc.activeElement, t.el.mapDock.back, 'the Map toggle');

    // Track Drone1 from the dock, then Esc: back in the map, in its dock.
    const track = find(
      t.el.mapDock.element,
      (el) => el.tag === 'button' && /Track Drone1/.test(textOf(el)),
    );
    track.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'tracking');
    t.el.dockBack.focus();
    t.el.root.fire('keydown', {
      key: 'Escape',
      stopPropagation() {},
      preventDefault() {},
    });
    await flush(8);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(t.doc.activeElement, t.el.mapDock.back, 'from tracking');

    // Back to the orb, then M from the orb: the same.
    t.el.mapDock.back.fire('click');
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    t.el.stage.focus();
    t.win.key({ key: 'm', target: t.el.stage });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(t.doc.activeElement, t.el.mapDock.back, 'M on the orb');
  } finally {
    t.restore();
  }
});

test('opening the map leaves focus alone where it is still usable (the composer)', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    t.el.analystBody.focus();
    t.bus.emit('map:request', {
      ids: ['thr:dyn-blr'],
      bbox: THEATER_BBOX,
      label: 'Bengaluru centre',
      source: 'operator',
    });
    await flush(8);
    await t.clock.advance(400);
    assert.equal(t.el.root.attrs['data-mode'], 'map');
    assert.equal(t.doc.activeElement, t.el.analystBody);
  } finally {
    t.restore();
  }
});

test("an analyst's ui map: the 3 s notice as a toast, Stay in console cancels", async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    t.bus.emit('map:request', {
      ids: ['thr:dyn-blr'],
      bbox: THEATER_BBOX,
      label: 'Bengaluru centre',
      reason: 'Watch the recce',
      source: 'analyst',
      countdown: true,
    });
    assert.match(
      textOf(t.el.toasts),
      /The analyst suggests showing Bengaluru centre on the map: "Watch the recce"\. Opening the map in 3 s\./,
    );
    const stay = find(
      t.el.toasts,
      (el) => el.tag === 'button' && textOf(el) === 'Stay in console',
    );
    stay.fire('click');
    await t.clock.advance(4000);
    assert.equal(t.el.root.attrs['data-mode'], 'orb');
    assert.ok(!port.calls.includes('map:true'));
  } finally {
    t.restore();
  }
});

test('untrusted text: an XSS or bidi theater label reaches the dock only as text', async () => {
  const label = '‮evil‬ <img src=x onerror=alert(1)>';
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph(label) });
  try {
    await openMap(t);
    const line = textOf(byClass(t.el.mapDock.element, 'ic-mapdock__line'));
    assert.ok(line.includes('evil <img src=x onerror=alert(1)>'));
    assert.doesNotMatch(line, /[‪-‮⁦-⁩]/);
    assert.equal(
      find(t.el.root, (el) => el.tag === 'img'),
      null,
      'no img element is created',
    );
    assert.equal(
      find(t.el.root, (el) => 'onerror' in (el.attrs || {})),
      null,
    );
    assert.doesNotMatch(textOf(t.el.livePolite), /[‪-‮]/);
  } finally {
    t.restore();
  }
});

test('map CSS: the overview passes input to the map but never through the tether', () => {
  const css = readFileSync(new URL('./console.css', import.meta.url), 'utf8');
  assert.match(css, /\.ic-root\[data-mode='map'\] \{[^}]*pointer-events: none/);
  assert.match(
    css,
    /\.ic-root\[data-mode='map'\] > :where\(:not\(\.ic-main, \.ic-tether\)\) \{\s*pointer-events: auto;/,
  );
  assert.match(css, /\.ic-mapsheet \{[^}]*--ic-inspector-top: 0px/);
});

test('off the orb the theater change is a toast; its Show on map moves the map', async () => {
  const port = mapPort();
  const t = await mount({ port, graph: theaterGraph() });
  try {
    await openMap(t);
    t.log.orbOpts.onNotice({
      kind: 'theater',
      ids: ['thr:dyn-blr'],
      text: 'Theater changed to Bengaluru centre. 3 items arrived.',
      holdMs: 20000,
    });
    assert.ok(
      !port.calls.slice(-1)[0].endsWith(':false'),
      'no hidden pre-positioning while the map shows',
    );
    assert.match(
      textOf(t.el.toasts),
      /Theater changed to Bengaluru centre\. 3 items arrived\./,
    );
    const show = find(
      t.el.toasts,
      (el) => el.tag === 'button' && textOf(el) === 'Show on map',
    );
    show.fire('click');
    await flush(8);
    assert.equal(port.calls.at(-1), `area:${THEATER_BBOX.join(',')}:true`);
    assert.doesNotMatch(textOf(t.el.toasts), /Theater changed/);
  } finally {
    t.restore();
  }
});
