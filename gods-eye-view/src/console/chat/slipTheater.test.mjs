/**
 * Theater and sim-speed slips (WG spec §3.6, §4.2.2): rows from the preview
 * (a missing field hides its row), the Deny-only fallback when the preview is
 * missing or incomplete, the blocked variant, the press-time recheck, the
 * numbers-only footprint, and the §3.11 untrusted-text fixtures.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { ARM_MS, DBLCLICK_MS, createSlip, filedText } from './slip.js';
import {
  FOOTPRINT_SAFE,
  areaText,
  footprintSvg,
  geocoderSegment,
  speedLines,
  theaterPlace,
  uniqueSentences,
} from './slipTheater.js';
import {
  PREVIEW_MISSING,
  REQUIRED_PREVIEW_KEYS,
  assessTheater,
} from './validateTheater.js';

// ---- stub DOM (no real DOM in node:test; see src/ui/uavMissionPanel.test.mjs) ----

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
    fire(t, e = {}) {
      for (const f of [...(this.listeners[t] || [])]) f(e);
    },
  };
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
    // Like the DOM: setting textContent replaces the children.
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
    getBoundingClientRect() {
      return { ...this.rect };
    },
    scrollIntoView() {
      this.scrolled = true;
    },
  };
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

/** Class list membership, like `el.classList.contains`. */
const cls = (name) => (el) =>
  String(el?.className || '')
    .split(/\s+/)
    .includes(name);
const hidden = (el) => Object.hasOwn(el.attrs, 'hidden');

function fakeClock(start = 0) {
  let t = start;
  let id = 0;
  const timers = new Map();
  return {
    now: () => t,
    setTimeout(fn, ms) {
      id += 1;
      timers.set(id, { fn, at: t + ms });
      return id;
    },
    clearTimeout(i) {
      timers.delete(i);
    },
    advance(ms) {
      const end = t + ms;
      for (;;) {
        let next = null;
        for (const [k, v] of timers)
          if (v.at <= end && (!next || v.at < next[1].at)) next = [k, v];
        if (!next) break;
        timers.delete(next[0]);
        t = next[1].at;
        next[1].fn();
      }
      t = end;
    },
    pending: () => timers.size,
  };
}

const T0 = Date.UTC(2026, 8, 27, 14, 1, 0);
const XSS = '<img src=x onerror=alert(1)>';
const RLO = '\u202Eevil\u202C';
const BIDI = /[\u202A-\u202E\u2066-\u2069]/;

const PREVIEW = Object.freeze({
  label: 'Kherson',
  place: 'Kherson, Ukraine',
  query: 'Kherson',
  geocoder: 'Photon (OpenStreetMap)',
  center: [46.63542, 32.61687],
  bbox: [46.60898, 32.57838, 46.66186, 32.65536],
  half_extent_m: 2940,
  area_km2: 34.6,
  clamped_from_km: [34.4, 35.1],
  home: {
    lat: 46.638,
    lon: 32.619,
    name: 'Open ground',
    source: 'overpass-open-ground',
    distance_m: 345,
  },
  ground_msl_m: 925,
  ground_source:
    'Re:Earth terrain (ellipsoidal height), converted to sea level (EGM96) once.',
  airframe: {
    from: 'quad_suas_electric',
    to: 'quad_suas_electric',
    label: 'Quad, small electric',
    reach_m: 7350,
  },
  previous: { id: 'default', label: 'Redmond (AirSim default)' },
  now_after: [
    { row: 'Theater', now: 'Redmond (AirSim default)', after: 'Kherson' },
    { row: 'Fuel', now: 'Drone1 94.0%', after: '94.0% (kept)' },
  ],
  resets: ['Alarms in progress'],
  keeps: ['Contacts, reports and the audit trail'],
  sites: { total: 41, degraded: false },
  caveats: ['The AO is clamped to the airframe reach.'],
  checks: [
    { text: 'Drone1 on the ground', ok: true },
    { text: 'Drone1 has no task running', ok: true },
    { text: 'Fake simulator', ok: true },
    { text: 'Proposal still valid', ok: true },
  ],
});

function theaterApproval(extra = {}) {
  return {
    id: 'a1',
    callId: 'c1',
    tool: 'sim_set_theater',
    klass: 'sim',
    rawClass: 'sim',
    title: 'Set the theater',
    summary: 'Kherson',
    args: { proposal_id: 'p1', label: 'Kherson' },
    consequences: [
      'Moves the simulation to Kherson: a 5.9 × 5.9 km area around 46.63542, 32.61687.',
    ],
    theaterPreview: structuredClone(PREVIEW),
    timeScalePreview: null,
    acknowledgeRequired: false,
    dry_run: null,
    allowSession: false,
    grantScope: undefined,
    vehicle: null,
    expiresAt: T0 + 600_000,
    at: T0,
    state: 'pending',
    note: null,
    error: null,
    ...extra,
  };
}

function speedApproval(extra = {}) {
  return theaterApproval({
    tool: 'sim_set_time_scale',
    title: 'Set sim speed',
    summary: '',
    args: { scale: 4 },
    consequences: [
      'Runs the fake simulator 4× faster (physics, fuel, sun). Link-loss timers stay in wall-clock seconds.',
    ],
    theaterPreview: null,
    timeScalePreview: {
      from: 1,
      to: 4,
      checks: [{ text: 'Fake simulator', ok: true }],
      caveats: ['Safety checks run every 0.5 sim-seconds.'],
    },
    ...extra,
  });
}

const DRONE1 = Object.freeze({
  name: 'Drone1',
  fuel_pct: 94,
  landed: true,
  link: 'up',
  bingo_latched: false,
  eta_to_bingo_s: 660,
  busy: false,
});

function mount(approval, { assessment, fleet = [DRONE1], revalidate } = {}) {
  const doc = stubDoc();
  globalThis.document = doc;
  const clock = fakeClock(T0 + 30_000);
  const decisions = [];
  const announcements = [];
  const slip = createSlip(
    {
      approval,
      assessment: assessment ?? { state: 'none', reasons: [] },
      vehicle: null,
      before: null,
      queue: { index: 1, total: 1 },
      conflicts: [],
      caveats: [],
      detections: null,
      fleet,
      now: clock.now(),
      reducedMotion: false,
    },
    {
      decide: async (decision, note) => {
        decisions.push({ decision, note });
      },
      revalidate,
      announce: (text, opts) => announcements.push({ text, ...opts }),
      clock,
      raf: null,
      caf: null,
      ResizeObserver: null,
      doc,
      getRect: (el) => el.getBoundingClientRect(),
    },
  );
  return { slip, clock, decisions, announcements, doc };
}

const approveEls = (root) =>
  findAll(
    root,
    (el) =>
      cls('ic-slip__approve')(el) ||
      /^approve/.test(el.attrs?.['data-action'] || ''),
  );

/** The table row whose header cell reads `name`, as text. */
function rowText(root, name) {
  const th = find(root, (el) => el.tag === 'th' && textOf(el) === name);
  if (!th) return null;
  const tr = find(root, (el) => el.tag === 'tr' && el.children.includes(th));
  return tr ? textOf(tr) : null;
}

async function press(slip, clock) {
  const approve = find(
    slip.el,
    (el) => el.attrs?.['data-action'] === 'approve',
  );
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await Promise.resolve();
}

test('coordinates are placed, not geocoded; a table theater is a preset', () => {
  assert.equal(
    geocoderSegment('Photon (OpenStreetMap)'),
    'geocoded by Photon (OpenStreetMap)',
  );
  assert.equal(
    geocoderSegment('Coordinates'),
    'placed from coordinates, not geocoded',
  );
  assert.equal(geocoderSegment('Theater table'), 'preset theater');
  assert.equal(geocoderSegment(null), null);
  const a = theaterApproval();
  a.theaterPreview = { ...a.theaterPreview, geocoder: 'Coordinates' };
  const { slip } = mount(a);
  const summary = textOf(find(slip.el, cls('ic-slip__summary')));
  assert.ok(summary.includes('placed from coordinates, not geocoded'));
  assert.ok(!summary.includes('geocoded by'));
});

test('the theater slip shows every preview row, the checks and the undo line', async () => {
  const { slip, clock, decisions } = mount(theaterApproval());
  const text = textOf(slip.el);
  assert.equal(slip.el.attrs['data-class'], 'sim');
  assert.ok(text.includes('Changes the simulation'));
  assert.ok(text.includes('Set the theater'));
  const summary = find(slip.el, cls('ic-slip__summary'));
  assert.ok(textOf(summary).includes('Kherson, Ukraine'));
  assert.ok(textOf(summary).includes('geocoded by Photon (OpenStreetMap)'));
  assert.ok(textOf(summary).includes('Requested'));
  assert.ok(
    text.includes(
      'Right now Drone1 is on the ground with 94.0% fuel. Link up.',
    ),
  );
  assert.ok(
    text.includes(
      'Dry runs made before this change will be marked out of date.',
    ),
  );
  assert.equal(rowText(slip.el, 'Place'), 'PlaceKherson, Ukraine');
  assert.equal(rowText(slip.el, 'Centre'), 'Centre46.63542, 32.61687');
  const area = rowText(slip.el, 'Area');
  assert.ok(area.includes('5.9 × 5.9 km (34.6 km²)'), area);
  assert.ok(area.includes('Requested'));
  assert.ok(
    area.includes("Clamped from 34.4 × 35.1 km to fit Drone1's reach."),
    area,
  );
  assert.ok(area.includes('Estimated'));
  const home = rowText(slip.el, 'Home');
  assert.ok(home.includes('Open ground'));
  assert.ok(home.includes('46.63800, 32.61900'));
  assert.ok(home.includes('0.3 km from the centre'));
  assert.ok(home.includes('Mapped'));
  const ground = rowText(slip.el, 'Ground');
  assert.ok(ground.includes('≈ 925 m above sea level'));
  assert.ok(ground.includes('converted to sea level (EGM96) once'));
  assert.ok(
    rowText(slip.el, 'Airframe').includes(
      'Quad, small electric, reach ≈ 7.4 km',
    ),
  );
  assert.ok(rowText(slip.el, 'Theater').includes('Redmond (AirSim default)'));
  assert.ok(text.includes('Alarms in progress'));
  assert.ok(text.includes('Contacts, reports and the audit trail'));
  assert.ok(text.includes('The AO is clamped to the airframe reach.'));
  const checks = find(slip.el, cls('ic-slip__checks'));
  assert.equal(checks.children.length, 4);
  assert.ok(checks.children.every((li) => li.attrs['data-ok'] === 'true'));
  assert.ok(textOf(checks).includes('✓'));
  assert.ok(
    text.includes(
      'Set the theater back to Redmond (AirSim default) with another change, which also needs your approval.',
    ),
  );
  assert.equal(
    textOf(find(slip.el, cls('ic-slip__policy'))),
    'Theater changes are approved one at a time.',
  );
  assert.equal(hidden(find(slip.el, cls('ic-slip__denyonly'))), true);
  // Approve after the 800 ms arm, with the theater verb.
  const approve = find(
    slip.el,
    (el) => el.attrs?.['data-action'] === 'approve',
  );
  assert.equal(textOf(approve), 'Approve theater change');
  clock.advance(ARM_MS);
  await press(slip, clock);
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
});

test('a missing preview field hides its row', () => {
  const minimal = {
    checks: [{ text: 'Fake simulator', ok: true }],
    center: [12.9716, 77.5946],
    bbox: [12.94912, 77.57156, 12.99408, 77.61764],
    home: {},
    airframe: {},
    ground_msl_m: 920,
  };
  const { slip } = mount(theaterApproval({ theaterPreview: minimal }), {
    fleet: [],
  });
  const text = textOf(slip.el);
  assert.equal(rowText(slip.el, 'Place'), null);
  assert.equal(rowText(slip.el, 'Home'), null);
  assert.equal(rowText(slip.el, 'Airframe'), null);
  assert.equal(rowText(slip.el, 'Sites'), null);
  assert.equal(rowText(slip.el, 'Centre'), 'Centre12.97160, 77.59460');
  assert.ok(rowText(slip.el, 'Area').includes('5.0 × 5.0 km (25.0 km²)'));
  assert.ok(!rowText(slip.el, 'Area').includes('Clamped'));
  assert.ok(rowText(slip.el, 'Ground').includes('≈ 920 m above sea level'));
  for (const gone of ['Now', 'Resets', 'Stays', 'geocoded by', 'Right now']) {
    assert.ok(!text.includes(gone), gone);
  }
  assert.ok(
    text.includes(
      'Set the theater back with another change, which also needs your approval.',
    ),
  );
  // The summary falls back to the args label.
  assert.ok(textOf(find(slip.el, cls('ic-slip__summary'))).includes('Kherson'));
  // Still approvable: every required key is there.
  assert.equal(approveEls(slip.el).length, 1);
});

test('Deny-only when the preview is missing or lacks a required key (§3.6)', async () => {
  const cases = [
    ['no preview', theaterApproval({ theaterPreview: null })],
    ['empty preview', theaterApproval({ theaterPreview: {} })],
    ...REQUIRED_PREVIEW_KEYS.sim_set_theater.map((key) => {
      const p = structuredClone(PREVIEW);
      delete p[key];
      return [`no ${key}`, theaterApproval({ theaterPreview: p })];
    }),
    ['speed: no preview', speedApproval({ timeScalePreview: null })],
    ...REQUIRED_PREVIEW_KEYS.sim_set_time_scale.map((key) => {
      const p = { from: 1, to: 4, checks: [] };
      delete p[key];
      return [`speed: no ${key}`, speedApproval({ timeScalePreview: p })];
    }),
  ];
  for (const [name, approval] of cases) {
    const { slip, clock, decisions } = mount(approval);
    assert.deepEqual(approveEls(slip.el), [], name);
    assert.equal(slip.el.attrs['data-variant'], 'deny_only', name);
    const line = find(slip.el, cls('ic-slip__denyonly'));
    assert.equal(hidden(line), false, name);
    assert.equal(textOf(line), PREVIEW_MISSING, name);
    clock.advance(ARM_MS);
    const deny = find(slip.el, (el) => el.attrs?.['data-action'] === 'deny');
    deny.fire('click', { detail: 1 });
    await Promise.resolve();
    assert.deepEqual(decisions, [{ decision: 'deny', note: null }], name);
  }
});

test('blocked: a failing check or the console assessment makes the slip Deny-only', () => {
  const failing = structuredClone(PREVIEW);
  failing.checks[0].ok = false;
  const a = mount(theaterApproval({ theaterPreview: failing }));
  assert.deepEqual(approveEls(a.slip.el), []);
  assert.equal(
    textOf(find(a.slip.el, cls('ic-slip__denyonly'))),
    "This can't be approved now: a check failed: Drone1 on the ground.",
  );
  const li = find(a.slip.el, cls('ic-slip__checks')).children[0];
  assert.equal(li.attrs['data-ok'], 'false');
  assert.ok(textOf(li).includes('✕'));
  // The graph says Drone1 is airborne: the Land clause.
  const approval = theaterApproval();
  const theater = assessTheater(approval, {
    theater: { id: 'default', epoch: 0 },
    nodes: [
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        attrs: { landed: false, link: 'up' },
      },
    ],
  });
  const b = mount(approval, {
    assessment: { state: 'blocked', reasons: theater.reasons, theater },
  });
  assert.deepEqual(approveEls(b.slip.el), []);
  assert.equal(
    textOf(find(b.slip.el, cls('ic-slip__denyonly'))),
    "This can't be approved now: Drone1 is airborne. Land Drone1 first, then ask again.",
  );
  // It comes back when the conditions clear (on a store change).
  b.slip.update({ assessment: { state: 'none', reasons: [] } });
  assert.equal(approveEls(b.slip.el).length, 1);
  assert.equal(hidden(find(b.slip.el, cls('ic-slip__denyonly'))), true);
});

test('blocked after the request: the check it contradicts turns ✕ and the list says when it was measured', () => {
  // Review: Drone1 took off while the slip waited; the slip went Deny-only
  // ("Drone1 is airborne") but still listed "✓ Passed: Drone1 on the ground".
  const approval = theaterApproval();
  const theater = assessTheater(approval, {
    theater: { id: 'default', epoch: 0 },
    nodes: [
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        attrs: { landed: false, link: 'up' },
      },
    ],
  });
  const { slip } = mount(approval, {
    assessment: { state: 'blocked', reasons: theater.reasons, theater },
  });
  const list = find(slip.el, cls('ic-slip__checks'));
  const ground = list.children.find((li) =>
    textOf(li).endsWith('Drone1 on the ground'),
  );
  assert.equal(ground.attrs['data-ok'], 'false');
  assert.equal(ground.attrs['data-changed'], 'true');
  assert.match(textOf(ground), /^✕Failed now: Drone1 on the ground$/);
  assert.doesNotMatch(textOf(list), /Passed: Drone1 on the ground/);
  const idle = list.children.find((li) =>
    textOf(li).endsWith('Drone1 has no task running'),
  );
  assert.equal(idle.attrs['data-ok'], 'true', 'an unaffected check stays');
  assert.match(
    textOf(slip.el),
    /Measured when the analyst asked\. Marked ✕ where that has changed since\./,
  );
  // Conditions clear: the checks read as the server measured them again.
  slip.update({ assessment: { state: 'none', reasons: [] } });
  const again = find(slip.el, cls('ic-slip__checks'));
  assert.equal(again.children[0].attrs['data-ok'], 'true');
  assert.doesNotMatch(textOf(slip.el), /Marked ✕ where/);
});

test('the home register is the one the theater inspector gives it; the AO centre is Assumed', () => {
  // Review: an AO-centre home (coordinates, geodata off) read "Mapped" here
  // and "Requested" in the inspector; nothing was mapped.
  const cases = [
    ['overpass-open-ground', /Mapped$/],
    ['preset', /Measured$/],
    ['operator', /Requested$/],
    [
      'ao-centre',
      /^AO centre \(no mapped open ground\) 12\.97160, 77\.59460, 0\.0 km from the centre Assumed$/,
    ],
  ];
  for (const [source, want] of cases) {
    const p = structuredClone(PREVIEW);
    p.home = {
      lat: 12.9716,
      lon: 77.5946,
      name: source === 'ao-centre' ? null : 'Field',
      source,
      distance_m: 0,
    };
    const { slip } = mount(theaterApproval({ theaterPreview: p }));
    const home = rowText(slip.el, 'Home').replace(/^Home/, '');
    assert.match(home, want, source);
  }
  const p = structuredClone(PREVIEW);
  p.home = { lat: 1, lon: 2, source: 'somewhere-new' };
  const { slip } = mount(theaterApproval({ theaterPreview: p }));
  assert.doesNotMatch(
    rowText(slip.el, 'Home'),
    /Mapped|Measured|Requested|Assumed/,
    'an unknown source claims no register',
  );
});

test('the speed slip says a sentence once when the policy and the preview both send it', () => {
  // Review: "Link-loss timers stay in wall-clock seconds." appeared twice.
  // The server strings as sent (analyst_policy.py, theater_tools.py).
  const { slip } = mount(
    speedApproval({
      consequences: [
        'Runs the fake simulator 4× faster (physics, fuel, sun). Link-loss timers stay in wall-clock seconds.',
        'Safety checks and camera captures stay on a real-time clock, so they happen less often per simulated second.',
      ],
      timeScalePreview: {
        from: 1,
        to: 4,
        checks: [{ text: 'Fake simulator', ok: true }],
        caveats: [
          'Link-loss timers stay in wall-clock seconds.',
          'Detections and scans run in real time.',
          "The analyst's clock is wall time.",
          'Detections and  scans run in real time. ',
        ],
      },
    }),
  );
  const items = find(slip.el, cls('ic-slip__list')).children.map(textOf);
  assert.deepEqual(items, [
    'Runs the fake simulator 4× faster (physics, fuel, sun). Link-loss timers stay in wall-clock seconds.',
    'Safety checks and camera captures stay on a real-time clock, so they happen less often per simulated second.',
    'Detections and scans run in real time.',
    "The analyst's clock is wall time.",
  ]);
  assert.equal(
    (textOf(slip.el).match(/Link-loss timers stay/g) || []).length,
    1,
  );
  // Only whole sentences: one that merely shares words stays.
  assert.deepEqual(
    uniqueSentences(['Keep it. Now.', 'Keep', 'It. Now.', 'Now.', 7, '']),
    ['Keep it. Now.', 'Keep', 'It. Now.'],
  );
});

test('press-time recheck: a slip that just became blocked swallows the press', async () => {
  let fresh = { state: 'none', reasons: [] };
  const { slip, clock, decisions, announcements } = mount(theaterApproval(), {
    revalidate: () => fresh,
  });
  clock.advance(ARM_MS);
  assert.equal(slip.isArmed(), true);
  const theater = {
    ok: false,
    reasons: [
      { code: 'epoch', text: 'the theater changed after this request' },
    ],
    airborne: [],
  };
  fresh = { state: 'blocked', reasons: theater.reasons, theater };
  await press(slip, clock);
  assert.deepEqual(decisions, [], 'swallowed');
  assert.deepEqual(announcements.at(-1), {
    text: 'Conditions changed. Review the slip again.',
    assertive: true,
  });
  assert.deepEqual(approveEls(slip.el), [], 'now Deny-only');
  assert.equal(
    textOf(find(slip.el, cls('ic-slip__denyonly'))),
    "This can't be approved now: the theater changed after this request.",
  );
});

test('the footprint is numbers only, with a warn caption when reach is short', () => {
  const { svg, warn } = footprintSvg(PREVIEW);
  assert.equal(warn, false);
  assert.match(svg, FOOTPRINT_SAFE);
  // Every attribute value is a number list or a fixed class name.
  for (const [, name, value] of svg.matchAll(/ ([a-z-]+)="([^"]*)"/g)) {
    if (
      ['xmlns', 'viewBox', 'aria-hidden', 'focusable', 'class'].includes(name)
    )
      continue;
    assert.match(value, /^[-0-9. ]+$/, name);
  }
  // Text fields never reach the markup.
  const hostile = {
    ...structuredClone(PREVIEW),
    place: XSS,
    label: XSS,
    home: { ...PREVIEW.home, name: XSS },
  };
  assert.equal(footprintSvg(hostile).svg, svg);
  assert.equal(footprintSvg({ ...PREVIEW, center: ['1', 2] }).svg, '');
  // Reach below the half-diagonal (≈ 4.2 km here): warn.
  const short = { ...structuredClone(PREVIEW), airframe: { reach_m: 3000 } };
  assert.equal(footprintSvg(short).warn, true);
  const { slip } = mount(theaterApproval({ theaterPreview: short }));
  const pic = find(slip.el, cls('ic-slip__footprint'));
  assert.equal(pic.attrs['data-warn'], 'true');
  assert.equal(pic.attrs['aria-hidden'], 'true');
  assert.match(pic.innerHTML, FOOTPRINT_SAFE);
  assert.equal(
    textOf(find(slip.el, cls('ic-slip__warnline'))),
    "Drone1 can't reach the far corners and return.",
  );
  assert.ok(
    textOf(slip.el).includes(
      "Not a map. Shows the area against Drone1's reach.",
    ),
  );
  assert.equal(areaText({ bbox: [1, 2, 3] }), '');
});

test('the sim speed slip: summary, caveats, BINGO line, checks and undo', async () => {
  const { slip, clock, decisions } = mount(speedApproval());
  const text = textOf(slip.el);
  assert.ok(text.includes('Set sim speed'));
  const summary = textOf(find(slip.el, cls('ic-slip__summary')));
  assert.ok(summary.includes('×4') && summary.includes('Requested'));
  const list = find(slip.el, cls('ic-slip__list'));
  assert.deepEqual(
    list.children.map((li) => textOf(li)),
    [
      'Runs the fake simulator 4× faster (physics, fuel, sun). Link-loss timers stay in wall-clock seconds.',
      'Safety checks run every 0.5 sim-seconds.',
    ],
  );
  assert.ok(
    text.includes(
      'Right now ×1. Drone1 has ≈ 11 min to BINGO in sim time; at ×4 that is ≈ 2 min 45 s of real time.',
    ),
  );
  assert.ok(
    text.includes(
      'Set the speed back to ×1 with another simulation change, which also needs your approval.',
    ),
  );
  assert.equal(
    textOf(find(slip.el, cls('ic-slip__policy'))),
    'Simulation changes are approved one at a time in this version.',
  );
  const approve = find(
    slip.el,
    (el) => el.attrs?.['data-action'] === 'approve',
  );
  assert.equal(textOf(approve), 'Approve change');
  clock.advance(ARM_MS);
  await press(slip, clock);
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
  // No BINGO time in the graph: no console line.
  assert.deepEqual(speedLines([{ name: 'Drone1' }], 1, 4), []);
  // Blocked when a check fails.
  const blocked = mount(
    speedApproval({
      timeScalePreview: {
        from: 1,
        to: 4,
        checks: [{ text: 'Fake simulator', ok: false }],
      },
    }),
  );
  assert.deepEqual(approveEls(blocked.slip.el), []);
  assert.equal(
    textOf(find(blocked.slip.el, cls('ic-slip__denyonly'))),
    "This can't be approved now: a check failed: Fake simulator.",
  );
});

test('filed theater and speed slips name the place and the speed', () => {
  const at = Date.UTC(2026, 8, 27, 14, 1, 40);
  assert.equal(
    filedText(theaterApproval({ state: 'approved', resolvedAt: at })),
    'Approved by you at 14:01:40Z. Set the theater, Kherson, Ukraine.',
  );
  assert.equal(
    filedText(speedApproval({ state: 'approved', resolvedAt: at })),
    'Approved by you at 14:01:40Z. Set sim speed, ×4.',
  );
  assert.equal(
    theaterPlace(theaterApproval({ theaterPreview: null })),
    'Kherson',
    'falls back to the args label',
  );
});

test('XSS and bidi fixtures in the place, label and home name render as text (§3.11)', () => {
  const p = structuredClone(PREVIEW);
  p.place = `${XSS} ${RLO}`;
  p.label = `${RLO}${XSS}`;
  p.home.name = `${XSS}${RLO}`;
  p.previous = { id: 'x', label: `${XSS}${RLO}` };
  p.now_after = [{ row: 'Theater', now: RLO, after: XSS }];
  p.checks = [{ text: `${XSS}${RLO}`, ok: true }];
  const approval = theaterApproval({
    theaterPreview: p,
    args: { label: `${RLO}${XSS}` },
  });
  const { slip } = mount(approval);
  assert.equal(
    find(slip.el, (el) => el.tag === 'img'),
    null,
  );
  assert.equal(
    find(slip.el, (el) => el.attrs && Object.hasOwn(el.attrs, 'onerror')),
    null,
  );
  const text = textOf(slip.el);
  assert.ok(text.includes('<img src=x onerror=alert(1)>'));
  assert.ok(!BIDI.test(text), 'no bidi control reaches the slip');
  // Only the footprint uses markup, and it holds numbers only.
  const withHtml = findAll(slip.el, (el) => el.innerHTML != null);
  assert.deepEqual(
    withHtml.map((el) => el.className),
    ['ic-slip__footprint'],
  );
  assert.match(withHtml[0].innerHTML, FOOTPRINT_SAFE);
  // A place that happens to read like a register word stays plain text.
  const q = structuredClone(PREVIEW);
  q.place = 'Measured';
  const m = mount(theaterApproval({ theaterPreview: q }));
  const placeCell = find(m.slip.el, (el) => el.tag === 'td');
  assert.equal(find(placeCell, cls('ic-tag')), null);
  // The filed record is bidi-free as well.
  assert.ok(
    !BIDI.test(
      textOf(mount({ ...approval, state: 'approved', resolvedAt: T0 }).slip.el),
    ),
  );
});
