import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ARM_MS,
  DBLCLICK_MS,
  SLIP_NOTES,
  createSlip,
  filedText,
  grantPhrase,
  lostLinkLine,
  rightNowLine,
  segmentNodes,
  slipVariant,
} from './slip.js';

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
const byAction = (root, action) =>
  find(root, (el) => el.attrs?.['data-action'] === action);
const actions = (slip) =>
  find(slip.el, cls('ic-slip__actions')).children.map(
    (b) => b.attrs['data-action'],
  );

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

const T0 = Date.UTC(2026, 8, 27, 14, 2, 51);

function approval(extra = {}) {
  return {
    id: 'a1',
    callId: 'c1',
    tool: 'mission_grid_search',
    klass: 'command',
    title: 'Grid search',
    summary: 'Drone1 · North Field AO · 60 m AGL · 30% overlap · 8 m/s',
    args: { vehicle: 'Drone1', alt_m_agl: 60, speed_mps: 8 },
    consequences: [
      'Drone1 flies the grid search mission.',
      'Takes off first if the aircraft is on the ground.',
      "The server's fuel, BINGO and geofence gate runs first and may reject it.",
    ],
    dry_run: {
      ok: true,
      gate: {
        required_pct: 41.3,
        available_pct: 82,
        plan_fuel_pct: 14.9,
        return_fuel_pct: 6.4,
        reserve_pct: 20,
        bingo_fuel_pct: 26.4,
        envelope_violations: [],
        warnings: ['gate_start_assumed_home: priced from home'],
        est_distance_m: 9800,
      },
      eta_s: 860,
      fuel_pct_after: 67.1,
      waypoints: 26,
      at_ms: T0,
    },
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

const VEHICLE = {
  name: 'Drone1',
  fuel_pct: 82,
  bingo_fuel_pct: 26.4,
  landed: true,
  lat: 47.6445,
  lon: -122.1402,
  bingo_latched: false,
  link: 'up',
  agl_m: 60,
  agl_is_real: false,
  lost_link: null,
};

function mount(overrides = {}, deps = {}) {
  const doc = stubDoc();
  // The slip builds nodes lazily (uavDom.h reads the global at call time).
  globalThis.document = doc;
  const clock = fakeClock(T0 + 30_000);
  const decisions = [];
  const announcements = [];
  const model = {
    approval: approval(overrides.approval),
    assessment: { state: 'current', reasons: [] },
    vehicle: VEHICLE,
    before: null,
    queue: { index: 1, total: 1 },
    conflicts: [],
    caveats: [],
    detections: null,
    now: clock.now(),
    reducedMotion: false,
    ...overrides,
    ...(overrides.approval ? { approval: approval(overrides.approval) } : {}),
  };
  const slip = createSlip(model, {
    decide: async (decision, note) => {
      decisions.push({ decision, note });
    },
    announce: (text, opts) => announcements.push({ text, ...opts }),
    clock,
    raf: null,
    caf: null,
    ResizeObserver: null,
    doc,
    ...deps,
  });
  return { slip, doc, clock, decisions, announcements, model };
}

const approveBtn = (slip) =>
  byAction(slip.el, 'approve') || byAction(slip.el, 'approve-anyway');
const disabled = (el) => el.getAttribute('aria-disabled') === 'true';
const flush = () => new Promise((r) => setTimeout(r, 0));

// ---- anatomy ---------------------------------------------------------------------

test('the slip answers what happens, what it is based on and how to stop it, then decides', () => {
  const { slip } = mount();
  const text = textOf(slip.el);
  const order = [
    'Commands an aircraft',
    'Grid search',
    'Requested',
    'What happens',
    'Drone1 flies the grid search mission.',
    'Right now Drone1 is on the ground with 82.0% fuel. Link up.',
    'Based on',
    'Dry run passed at 14:02:51Z',
    'Fuel needed',
    'How to stop it',
    'Show exact request',
    'Deny',
    'Approve launch',
    'Commands are approved one at a time.',
  ];
  let at = -1;
  for (const piece of order) {
    const next = text.indexOf(piece, at + 1);
    assert.ok(next > at, `"${piece}" comes after the previous section`);
    at = next;
  }
  assert.equal(slip.el.attrs.role, 'region');
  assert.ok(slip.el.attrs['aria-labelledby'].startsWith('ic-slip-title-'));
  assert.equal(slip.el.attrs['data-class'], 'command');
});

test('consequences are rendered verbatim from the event', () => {
  const { slip } = mount({
    approval: { consequences: ['Exactly <this> text, verbatim.'] },
  });
  const list = find(slip.el, cls('ic-slip__list'));
  assert.deepEqual(list.children.map(textOf), [
    'Exactly <this> text, verbatim.',
  ]);
});

test('the summary is split on " · " into segments; the console never writes the dot', () => {
  const { slip } = mount();
  const summary = find(slip.el, cls('ic-slip__summary'));
  const segs = findAll(summary, cls('ic-seg')).map(textOf);
  assert.deepEqual(segs, [
    'Drone1',
    'North Field AO',
    '60 m AGL',
    '30% overlap',
    '8 m/s',
  ]);
  assert.ok(!textOf(slip.el).includes('·'));
  assert.deepEqual(
    segmentNodes('a · b')
      .filter((n) => n.className === 'ic-seg')
      .map(textOf),
    ['a', 'b'],
  );
});

test('dry-run rows carry registers; warnings strip their code prefix', () => {
  const { slip } = mount();
  const text = textOf(slip.el);
  assert.ok(text.includes('≈ 41.3% (plan 14.9, return 6.4, reserve 20.0)'));
  assert.ok(text.includes('Fuel at dry run82.0%Measured'));
  assert.ok(text.includes('Margin≈ 40.7 pointsEstimated'));
  assert.ok(text.includes('Fuel at the end≈ 67.1%Estimated'));
  assert.ok(text.includes('BINGO line≈ 26.4%Estimated'));
  assert.ok(text.includes('≈ 14 min 20 s, 9.8 km, 26 waypoints'));
  assert.ok(
    text.includes('No violations: 60 m AGL') === false,
    'no envelope limits: plain "No violations"',
  );
  assert.ok(text.includes('EnvelopeNo violations'));
  assert.ok(
    text.includes(
      "The gate was priced from home, not from the vehicle's live position; the ingress leg may be understated.",
    ),
  );
  assert.ok(
    text.includes("The server doesn't confirm it used these exact settings"),
  );
});

test('register tags carry their register; Measured is the same tag element as the others', () => {
  const { slip } = mount();
  const tags = findAll(slip.el, cls('ic-tag'));
  const byRegister = Object.groupBy(tags, (t) => t.attrs['data-register']);
  assert.ok(byRegister.measured?.length, 'Measured is tagged for styling');
  assert.ok(byRegister.estimated?.length);
  for (const t of tags) {
    assert.equal(textOf(t), capital(t.attrs['data-register']));
  }
});

function capital(s) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

test('envelope limits render when the gate carries them', () => {
  const a = approval();
  a.dry_run.gate.envelope = {
    ceiling_m_agl: 120,
    min_agl_m: 3,
    max_speed_mps: 20,
  };
  const { slip } = mount({ approval: { dry_run: a.dry_run } });
  assert.ok(
    textOf(slip.el).includes(
      'No violations: 60 m AGL within 3–120 m, 8 m/s within 20 m/s',
    ),
  );
});

test('without at_ms the dry run shows no time', () => {
  const a = approval();
  delete a.dry_run.at_ms;
  const { slip } = mount({ approval: { dry_run: a.dry_run } });
  const text = textOf(slip.el);
  assert.ok(text.includes('Dry run passed'));
  assert.ok(!/Dry run passed at/.test(text));
});

test('How to stop it: BINGO is uncancellable, Abort bypasses the analyst, lost-link only from data', () => {
  const { slip } = mount();
  const text = textOf(slip.el);
  assert.ok(
    text.includes(
      "If fuel reaches BINGO (≈ 26.4%), Drone1 returns home, and that return can't be cancelled.",
    ),
  );
  assert.ok(text.includes('goes straight to Drone1, not through the analyst.'));
  assert.ok(
    !text.includes('lost-link plan'),
    'no lost-link line without attrs.lost_link',
  );
  const { slip: withPlan } = mount({
    vehicle: {
      ...VEHICLE,
      lost_link: { behaviour: 'rtb', declare_after_s: 5 },
    },
  });
  assert.ok(
    textOf(withPlan.el).includes(
      "If the link drops for 5 s, Drone1 returns to base. That's its lost-link plan.",
    ),
  );
});

// ---- button order and variants -----------------------------------------------------

test('Deny comes first in DOM and tab order; Approve carries a verb', () => {
  const { slip } = mount();
  assert.deepEqual(actions(slip), ['deny', 'approve']);
  const approve = byAction(slip.el, 'approve');
  assert.equal(textOf(approve), 'Approve launch');
  assert.ok(approve.className.includes('is-primary'));
  // The grant control, if any, is never between Deny and Approve.
  const acts = find(slip.el, cls('ic-slip__actions'));
  assert.equal(acts.children.length, 2);
});

test('gate failed: only "Deny and re-plan", no way to send anyway', () => {
  const { slip, decisions } = mount({
    approval: {
      dry_run: {
        ok: false,
        gate: { required_pct: 104.2, available_pct: 86 },
        at_ms: T0,
      },
    },
    assessment: { state: 'failed', reasons: [{ code: 'gate_failed' }] },
  });
  assert.deepEqual(actions(slip), ['deny-replan']);
  const text = textOf(slip.el);
  assert.ok(
    text.includes('Fuel needed ≈ 104.2%: more than the 86.0% available'),
  );
  assert.ok(
    text.includes(
      "The server would reject this launch, so it can't be approved here.",
    ),
  );
  assert.equal(
    findAll(slip.el, (el) => el.className?.includes?.('ic-slip__approve'))
      .length,
    0,
  );
  byAction(slip.el, 'deny-replan').fire('click', { detail: 1 });
  assert.deepEqual(decisions, [{ decision: 'deny', note: SLIP_NOTES.replan }]);
});

test('gate failed: the failing numbers appear once, as critical bullets, not again as table rows', () => {
  const { slip } = mount({
    approval: {
      dry_run: {
        ok: false,
        gate: {
          required_pct: 104.2,
          available_pct: 71.5,
          envelope_violations: ['alt 150 m AGL above the 120 m ceiling'],
          envelope: { min_agl_m: 3, ceiling_m_agl: 120 },
          bingo_fuel_pct: 20,
        },
        at_ms: T0,
      },
    },
    assessment: { state: 'failed', reasons: [{ code: 'gate_failed' }] },
  });
  const failing = find(slip.el, cls('ic-slip__failing'));
  assert.ok(textOf(failing).includes('Fuel needed ≈ 104.2%'));
  assert.ok(textOf(failing).includes('alt 150 m AGL above the 120 m ceiling'));
  const table = find(slip.el, cls('ic-slip__table'));
  const heads = findAll(table, (el) => el.tag === 'th').map(textOf);
  assert.equal(heads.includes('Fuel needed'), false);
  assert.equal(heads.includes('Envelope'), false);
  assert.ok(heads.includes('BINGO line'), 'other rows stay');
  assert.equal(
    textOf(slip.el).split('alt 150 m AGL above the 120 m ceiling').length,
    2,
    'the violation is printed once',
  );
});

test('a passed gate keeps Fuel needed and Envelope in the table', () => {
  const { slip } = mount();
  const heads = findAll(
    find(slip.el, cls('ic-slip__table')),
    (el) => el.tag === 'th',
  ).map(textOf);
  assert.ok(heads.includes('Fuel needed'));
  assert.ok(heads.includes('Envelope'));
});

test('the Envelope row checks AGL for the real orbit args (alt_agl_m)', () => {
  const { slip } = mount({
    approval: {
      tool: 'uav_orbit_poi',
      title: 'Orbit point',
      args: {
        vehicle: 'Drone1',
        lat: 47.64,
        lon: -122.14,
        radius_m: 150,
        alt_agl_m: 100,
        speed_mps: 10,
      },
      dry_run: {
        ...approval().dry_run,
        gate: {
          ...approval().dry_run.gate,
          envelope: { min_agl_m: 3, ceiling_m_agl: 120, max_speed_mps: 20 },
        },
      },
    },
  });
  assert.ok(
    textOf(slip.el).includes(
      'No violations: 100 m AGL within 3–120 m, 10 m/s within 20 m/s',
    ),
  );
});

test('an orbit reads "an orbit" in the dry-run caption (live E2E copy bug)', () => {
  const orbit = {
    tool: 'uav_orbit_poi',
    title: 'Orbit point',
    args: { vehicle: 'Drone1', radius_m: 250 },
  };
  const { slip } = mount({
    approval: {
      ...orbit,
      dry_run: { ...approval().dry_run, matches_args: true },
    },
  });
  const text = textOf(slip.el);
  assert.ok(text.includes('Latest dry run of an orbit for Drone1'), text);
  assert.ok(!text.includes('a orbit'));
  const none = mount({
    approval: { ...orbit, dry_run: null },
    assessment: { state: 'none', reasons: [{ code: 'no_dry_run' }] },
  });
  assert.ok(textOf(none.slip.el).includes('No dry run of an orbit for Drone1'));
});

test('no dry run: "Ask for a dry run" is the primary, approve is secondary and armed', async () => {
  const { slip, decisions, clock } = mount({
    approval: { dry_run: null },
    assessment: { state: 'none', reasons: [{ code: 'no_dry_run' }] },
  });
  assert.deepEqual(actions(slip), ['deny', 'ask-dry-run', 'approve-anyway']);
  assert.ok(
    textOf(slip.el).includes(
      'No dry run of a grid search for Drone1 in this conversation.',
    ),
  );
  const secondary = byAction(slip.el, 'approve-anyway');
  assert.equal(textOf(secondary), 'Approve without a dry run');
  assert.ok(secondary.className.includes('is-secondary'));
  assert.equal(
    disabled(secondary),
    true,
    'approving without a dry run is armed too',
  );
  assert.equal(
    disabled(byAction(slip.el, 'ask-dry-run')),
    false,
    'asking is never delayed',
  );
  clock.advance(ARM_MS);
  assert.equal(disabled(secondary), false);
  byAction(slip.el, 'ask-dry-run').fire('click', { detail: 1 });
  await flush();
  assert.deepEqual(decisions.at(-1), {
    decision: 'deny',
    note: SLIP_NOTES.askDryRun,
  });
});

test('stale: warn strip, "Ask for a fresh dry run" primary, "Approve anyway" secondary', () => {
  const { slip } = mount({
    assessment: {
      state: 'stale',
      reasons: [
        { code: 'fuel', from: 82, to: 74.5 },
        { code: 'landed', from: true, to: false },
      ],
    },
  });
  assert.deepEqual(actions(slip), [
    'deny',
    'ask-fresh-dry-run',
    'approve-anyway',
  ]);
  assert.ok(
    textOf(slip.el).includes(
      'Conditions changed since the dry run: fuel 82.0% → 74.5%; Drone1 is now airborne. The numbers above may be wrong.',
    ),
  );
  assert.equal(slip.variant, 'stale');
});

test('the actions row says how many buttons it holds (three keep one row)', () => {
  const stale = mount({
    assessment: {
      state: 'stale',
      reasons: [{ code: 'landed', from: true, to: false }],
    },
  });
  const row = find(stale.slip.el, cls('ic-slip__actions'));
  assert.equal(row.getAttribute('data-count'), '3');
  const normal = mount();
  assert.equal(
    find(normal.slip.el, cls('ic-slip__actions')).getAttribute('data-count'),
    '2',
  );
});

test('unverified shows the caveat but does not demote Approve', () => {
  const { slip } = mount({
    assessment: { state: 'unverified', reasons: [{ code: 'no_snapshot' }] },
  });
  assert.ok(
    textOf(slip.el).includes(
      "The console can't compare this dry run with Drone1's current state.",
    ),
  );
  assert.deepEqual(actions(slip), ['deny', 'approve']);
});

test('slipVariant only applies dry-run logic to dry-runnable calls', () => {
  assert.equal(
    slipVariant({ tool: 'uav_takeoff' }, { state: 'none' }),
    'normal',
  );
  assert.equal(
    slipVariant({ tool: 'mission_grid_search' }, { state: 'none' }),
    'no_dry_run',
  );
  assert.equal(
    slipVariant({ tool: 'uav_mission' }, { state: 'stale' }),
    'stale',
  );
  assert.equal(
    slipVariant({ tool: 'uav_orbit_poi' }, { state: 'failed' }),
    'failed',
  );
  assert.equal(
    slipVariant({ tool: 'uav_orbit_poi' }, { state: 'current' }),
    'normal',
  );
});

// ---- arming ----------------------------------------------------------------------------

test('Approve is disarmed for 800 ms after arrival', () => {
  const { slip, clock, decisions } = mount();
  const approve = approveBtn(slip);
  assert.equal(disabled(approve), true);
  assert.ok(approve.getAttribute('data-arming'));
  clock.advance(ARM_MS - 1);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  assert.equal(decisions.length, 0, 'a press before arming is ignored');
  clock.advance(1);
  assert.equal(disabled(approve), false);
  assert.equal(slip.isArmed(), true);
  assert.equal(approve.getAttribute('data-arming'), null);
});

test('a pointer press counts only down+up on the armed button with detail 1', async () => {
  const { slip, clock, decisions } = mount();
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  assert.equal(decisions.length, 0, 'no pointerdown on the button');
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS - 1);
  await flush();
  assert.equal(
    decisions.length,
    0,
    'a single click waits out the double-click interval',
  );
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
});

test('a real double-click on an armed Approve never approves (the whole browser sequence)', async () => {
  const { slip, clock, decisions } = mount();
  clock.advance(ARM_MS + 1000);
  const approve = approveBtn(slip);
  assert.equal(slip.isArmed(), true);
  // What a browser really sends for a double-click: the detail-1 click
  // always comes first.
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(120);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 2 });
  approve.fire('dblclick', { detail: 2 });
  clock.advance(DBLCLICK_MS * 4);
  await flush();
  assert.deepEqual(decisions, [], 'a double-click can never approve');
  // A dblclick alone (without the detail-2 click) also cancels the first click.
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  approve.fire('dblclick', { detail: 2 });
  clock.advance(DBLCLICK_MS * 4);
  await flush();
  assert.deepEqual(decisions, []);
  // A deliberate single click later still approves.
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
});

test('Deny inside the double-click interval wins over a pending pointer approval', async () => {
  const { slip, clock, decisions } = mount();
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  byAction(slip.el, 'deny').fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS * 2);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'deny', note: null }]);
});

test('a slip destroyed inside the double-click interval sends nothing', async () => {
  const { slip, clock, decisions } = mount();
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  slip.destroy();
  clock.advance(DBLCLICK_MS * 2);
  await flush();
  assert.deepEqual(decisions, []);
});

test('a pointer that went down before arming does not count after it arms', () => {
  const { slip, clock, decisions } = mount();
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  clock.advance(ARM_MS);
  approve.fire('click', { detail: 1 });
  assert.equal(decisions.length, 0);
});

test('Enter or Space counts once; key auto-repeat never approves', async () => {
  const { slip, clock, decisions } = mount();
  const approve = approveBtn(slip);
  const early = approve.fire('keydown', { key: 'Enter', repeat: false });
  assert.equal(early.defaultPrevented, true);
  assert.equal(decisions.length, 0, 'unarmed');
  clock.advance(ARM_MS);
  const rep = approve.fire('keydown', { key: 'Enter', repeat: true });
  assert.equal(rep.defaultPrevented, true, 'repeats are swallowed');
  assert.equal(decisions.length, 0);
  approve.fire('keydown', { key: ' ', repeat: false });
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
});

test('there is no approval shortcut: ⌘Enter anywhere never approves', async () => {
  const { slip, clock, decisions, doc } = mount();
  clock.advance(ARM_MS);
  slip.el.fire('keydown', { key: 'Enter', metaKey: true });
  doc.fire('keydown', { key: 'Enter', metaKey: true });
  approveBtn(slip).fire('keydown', { key: 'a', metaKey: true });
  await flush();
  assert.equal(decisions.length, 0);
  assert.equal(
    (slip.el.listeners.keydown || []).length,
    0,
    'the slip root binds no keys',
  );
});

test('⌘Enter in the note field denies and sends the note', async () => {
  const { slip, decisions } = mount();
  const note = find(slip.el, cls('ic-slip__note'));
  note.value = 'Use 80 m instead.';
  note.fire('input');
  assert.equal(textOf(byAction(slip.el, 'deny')), 'Deny and send note');
  assert.ok(textOf(slip.el).includes('⌘Enter denies and sends this note.'));
  const ev = note.fire('keydown', { key: 'Enter', metaKey: true });
  assert.equal(ev.defaultPrevented, true);
  await flush();
  assert.deepEqual(decisions, [
    { decision: 'deny', note: 'Use 80 m instead.' },
  ]);
  note.fire('keydown', { key: 'Enter', ctrlKey: true, repeat: true });
  await flush();
  assert.equal(decisions.length, 1, 'no second decision from a repeat');
});

test('a layout shift over 4 px re-arms; smaller movement does not', () => {
  const { slip, clock } = mount();
  slip.checkLayout();
  clock.advance(ARM_MS);
  assert.equal(slip.isArmed(), true);
  slip.el.rect = { ...slip.el.rect, top: 104 };
  slip.checkLayout();
  assert.equal(slip.isArmed(), true, '4 px is within the slop');
  slip.el.rect = { ...slip.el.rect, top: 120 };
  slip.checkLayout();
  assert.equal(slip.isArmed(), false, 'streaming text above pushed it down');
  assert.equal(disabled(approveBtn(slip)), true);
  clock.advance(ARM_MS);
  assert.equal(slip.isArmed(), true);
  slip.el.rect = { ...slip.el.rect, height: 330 };
  slip.checkLayout();
  assert.equal(slip.isArmed(), false, 'a resize re-arms too');
});

test('a ResizeObserver drives the layout check when present', () => {
  let callback = null;
  class RO {
    constructor(cb) {
      callback = cb;
    }
    observe() {}
    disconnect() {}
  }
  const { slip, clock } = mount({}, { ResizeObserver: RO });
  assert.ok(callback);
  callback();
  clock.advance(ARM_MS);
  slip.el.rect = { ...slip.el.rect, width: 300 };
  callback();
  assert.equal(slip.isArmed(), false);
});

test('Review, a validation change and returning to the tab re-arm the slip', () => {
  const { slip, clock, doc } = mount();
  clock.advance(ARM_MS);
  slip.review();
  assert.equal(slip.isArmed(), false);
  assert.equal(slip.el.scrolled, true);
  assert.equal(doc.activeElement?.tag, 'h3', 'Review focuses the slip heading');
  clock.advance(ARM_MS);
  slip.update({ assessment: { state: 'unverified', reasons: [] } });
  assert.equal(slip.isArmed(), false, 'validation state changed');
  clock.advance(ARM_MS);
  slip.update({ assessment: { state: 'unverified', reasons: [] } });
  assert.equal(slip.isArmed(), true, 'same state: no re-arm');
  doc.fire('visibilitychange');
  assert.equal(slip.isArmed(), false);
});

test('press-time re-validation swallows a press on a slip that just went stale', async () => {
  let fresh = { state: 'current', reasons: [] };
  const { slip, clock, decisions, announcements } = mount(
    {},
    { revalidate: () => fresh },
  );
  clock.advance(ARM_MS);
  fresh = { state: 'stale', reasons: [{ code: 'fuel', from: 82, to: 70 }] };
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.equal(decisions.length, 0);
  assert.deepEqual(announcements.at(-1), {
    text: 'Conditions changed. Review the slip again.',
    assertive: true,
  });
  assert.deepEqual(actions(slip), [
    'deny',
    'ask-fresh-dry-run',
    'approve-anyway',
  ]);
  assert.equal(slip.isArmed(), false, 're-armed from zero');
  clock.advance(ARM_MS);
  const anyway = byAction(slip.el, 'approve-anyway');
  anyway.fire('pointerdown');
  anyway.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
});

test('reduced motion: the approve label reads "Ready in 1 s" while arming', () => {
  const { slip, clock } = mount({ reducedMotion: true });
  assert.equal(textOf(approveBtn(slip)), 'Ready in 1 s');
  clock.advance(ARM_MS);
  assert.equal(textOf(approveBtn(slip)), 'Approve launch');
});

test('reduced motion is live: a toggle after arrival relabels the arming Approve', () => {
  // Review (a11y): the view read prefers-reduced-motion once at mount, so a
  // live system toggle left the arming underline and no "Ready in 1 s".
  const { slip, clock, model } = mount();
  assert.equal(textOf(approveBtn(slip)), 'Approve launch');
  slip.update({ ...model, reducedMotion: true });
  assert.equal(textOf(approveBtn(slip)), 'Ready in 1 s');
  slip.update({ ...model, reducedMotion: false });
  assert.equal(textOf(approveBtn(slip)), 'Approve launch');
  slip.update({ ...model, reducedMotion: true });
  clock.advance(ARM_MS);
  assert.equal(textOf(approveBtn(slip)), 'Approve launch', 'armed');
});

test('a second press while the decision is in flight is ignored', async () => {
  let release;
  const gate = new Promise((r) => {
    release = r;
  });
  const calls = [];
  const { slip, clock } = mount(
    {},
    {
      decide: (d) => {
        calls.push(d);
        return gate;
      },
    },
  );
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  assert.deepEqual(calls, ['approve']);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  approve.fire('keydown', { key: 'Enter', repeat: false });
  byAction(slip.el, 'deny').fire('click', { detail: 1 });
  assert.deepEqual(calls, ['approve']);
  release();
  await flush();
});

// ---- class-specific controls ---------------------------------------------------------

function sensorModel(extra = {}) {
  return {
    approval: {
      tool: 'uav_scan_targets',
      klass: 'sensor',
      title: 'Scan for targets',
      summary: 'Drone1 · camera 0',
      args: { vehicle: 'Drone1' },
      consequences: ["Reads Drone1's sensor and updates the contact store."],
      dry_run: null,
      allowSession: true,
      ...extra,
    },
    assessment: { state: 'none', reasons: [{ code: 'not_dry_runnable' }] },
  };
}

test('sensor v1: the class grant checkbox is apart from the buttons and re-arms', async () => {
  const { slip, clock, decisions } = mount(sensorModel());
  const grant = find(slip.el, cls('ic-slip__grant'));
  assert.equal(hidden(grant), false);
  const text = textOf(grant);
  assert.ok(
    text.includes(
      "Don't ask again for sensor tasking until I start a new session",
    ),
  );
  assert.ok(
    text.includes(
      'Covers Collect detections, Scan for targets, Capture image, Point camera and Set field of view. Flying and simulation changes still ask every time.',
    ),
  );
  assert.equal(textOf(approveBtn(slip)), 'Approve scan');
  assert.ok(textOf(slip.el).includes('Nothing to stop.'));
  clock.advance(ARM_MS);
  const box = find(grant, (el) => el.tag === 'input');
  box.checked = true;
  box.fire('change');
  assert.equal(slip.isArmed(), false, 'checking re-arms');
  assert.equal(textOf(approveBtn(slip)), 'Approve and allow for session');
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve_session', note: null }]);
});

test('sensor v1.1: per-tool grant label; null scope offers nothing', () => {
  const { slip } = mount(sensorModel({ grantScope: ['uav_scan_targets'] }));
  const grant = find(slip.el, cls('ic-slip__grant'));
  assert.ok(textOf(grant).includes('Allow Scan for targets for this session'));
  const { slip: none } = mount(sensorModel({ grantScope: null }));
  assert.equal(hidden(find(none.el, cls('ic-slip__grant'))), true);
});

test('sensor: position is Assumed when AGL is above the launch datum; feed down is not a clear reading', () => {
  const { slip } = mount({
    ...sensorModel(),
    detections: { ok: false, at_ms: Date.UTC(2026, 8, 27, 14, 0, 12) },
  });
  const text = textOf(slip.el);
  assert.ok(text.includes('Drone1 at 47.64450, −122.14020'));
  assert.ok(
    text.includes('60 m above ground (Assumed: height above launch datum).'),
  );
  assert.ok(
    text.includes(
      "Detections feed is down since 14:00:12Z. A scan now may return nothing, and that isn't a clear reading.",
    ),
  );
});

test('sim: never offers a session grant; undo copy and policy line', () => {
  const { slip } = mount({
    approval: {
      tool: 'sim_set_weather',
      klass: 'sim',
      title: 'Set sim weather',
      summary: 'fog 0.4 · wind 6 m/s from 270°',
      args: {},
      consequences: [
        'Changes the sim weather and wind; detection range and fuel burn change with it.',
      ],
      dry_run: null,
      allowSession: true,
    },
    assessment: { state: 'none', reasons: [] },
  });
  assert.equal(hidden(find(slip.el, cls('ic-slip__grant'))), true);
  const text = textOf(slip.el);
  assert.ok(
    text.includes(
      'Dry runs made before this change will be marked out of date.',
    ),
  );
  assert.ok(text.includes('How to undo it'));
  assert.ok(
    text.includes(
      'Set the weather back with another simulation change, which also needs your approval.',
    ),
  );
  assert.ok(
    text.includes(
      'Simulation changes are approved one at a time in this version.',
    ),
  );
  assert.equal(textOf(approveBtn(slip)), 'Approve change');
});

function overrideModel() {
  return {
    approval: {
      tool: 'sim_set_fuel',
      klass: 'safety_override',
      title: 'Refuel',
      summary: 'Drone1 · 100%',
      args: { vehicle: 'Drone1', fuel_pct: 100 },
      consequences: [
        "Sets Drone1's fuel to 100% and clears the BINGO latch.",
        'This overrides a safety latch. A return already flying is not cancelled.',
      ],
      dry_run: null,
    },
    assessment: { state: 'none', reasons: [] },
    vehicle: { ...VEHICLE, fuel_pct: 17.2, bingo_latched: true },
    before: { ...VEHICLE, fuel_pct: 17.2, bingo_latched: true },
  };
}

test('override: Approve stays disabled until the untimed acknowledgement, then arms', async () => {
  const { slip, clock, decisions } = mount(overrideModel());
  const ack = find(slip.el, cls('ic-slip__ack'));
  assert.equal(hidden(ack), false);
  assert.ok(
    textOf(ack).includes("I understand this clears Drone1's BINGO latch."),
  );
  clock.advance(60_000);
  assert.equal(
    disabled(approveBtn(slip)),
    true,
    'no amount of waiting arms it',
  );
  assert.equal(slip.isArmed(), false);
  const box = find(ack, (el) => el.tag === 'input');
  box.checked = true;
  box.fire('change');
  assert.equal(
    disabled(approveBtn(slip)),
    true,
    'checking starts the 800 ms arming',
  );
  clock.advance(ARM_MS);
  assert.equal(disabled(approveBtn(slip)), false);
  box.checked = false;
  box.fire('change');
  assert.equal(disabled(approveBtn(slip)), true);
  box.checked = true;
  box.fire('change');
  clock.advance(ARM_MS);
  const approve = approveBtn(slip);
  approve.fire('pointerdown');
  approve.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
  assert.deepEqual(decisions, [{ decision: 'approve', note: null }]);
  const text = textOf(slip.el);
  assert.ok(text.includes('Safety override'));
  assert.ok(text.includes('Latched, returning home'));
  assert.ok(
    text.includes('Latch cleared; the return already flying continues'),
  );
  assert.ok(
    text.includes(
      "The console can't undo an override. Drone1's state before it (17.2% fuel, BINGO latched) stays in this transcript.",
    ),
  );
  assert.ok(text.includes('Overrides are approved one at a time.'));
  assert.equal(textOf(approve), 'Approve override');
});

// ---- queue, conflicts, expiry, filing ------------------------------------------------------

test('queue position and same-vehicle conflict lines', () => {
  const { slip } = mount({
    queue: { index: 1, total: 2 },
    conflicts: [{ title: 'Fly to point' }],
  });
  const text = textOf(slip.el);
  assert.ok(text.includes('1 of 2'));
  assert.ok(
    text.includes(
      'Another request for Drone1 is waiting: Fly to point. Only one command runs at a time; the later one will be refused as busy.',
    ),
  );
});

test('expiry counts down, turns urgent under a minute and announces once', () => {
  const { slip, announcements } = mount();
  const expiry = find(slip.el, cls('ic-slip__expiry'));
  assert.equal(expiry.attrs['aria-hidden'], 'true');
  slip.tick(T0 + 12_000);
  assert.equal(expiry.textContent, 'Expires in 9:48');
  assert.equal(slip.el.attrs['data-urgent'], 'false');
  slip.tick(T0 + 540_000);
  assert.equal(expiry.textContent, 'Expires in 1:00');
  assert.equal(slip.el.attrs['data-urgent'], 'true');
  slip.tick(T0 + 545_000);
  const warns = announcements.filter(
    (a) => a.text === '1 minute left to decide: Grid search.',
  );
  assert.equal(warns.length, 1);
  assert.equal(warns[0].assertive, true);
});

test('a decided slip files into a one-line record with Details', () => {
  const { slip } = mount();
  slip.update({
    approval: approval({
      state: 'approved',
      resolvedAt: Date.UTC(2026, 8, 27, 14, 3, 22),
      scope: 'once',
    }),
  });
  assert.equal(slip.el.attrs['data-state'], 'filed');
  const record = find(slip.el, cls('ic-slip__record'));
  assert.equal(
    textOf(record).replace(/^check/, ''),
    'Approved by you at 14:03:22Z. Grid search, Drone1.',
  );
  const pending = find(slip.el, cls('ic-slip__pending'));
  assert.equal(hidden(pending), true, 'no buttons on a filed slip');
  const details = find(
    slip.el,
    (el) => el.tag === 'button' && textOf(el) === 'Details',
  );
  assert.equal(details.attrs['aria-expanded'], 'false');
  details.fire('click');
  const again = find(
    slip.el,
    (el) => el.tag === 'button' && textOf(el) === 'Details',
  );
  assert.equal(again.attrs['aria-expanded'], 'true');
  assert.equal(hidden(find(slip.el, cls('ic-slip__info'))), false);
});

test('filed record copy for every outcome', () => {
  const at = Date.UTC(2026, 8, 27, 14, 3, 40);
  const base = approval();
  assert.equal(
    filedText({
      ...base,
      state: 'denied',
      resolvedAt: at,
      note: 'Use 80 m instead.',
    }),
    'Denied by you at 14:03:40Z. Your note: “Use 80 m instead.”',
  );
  assert.equal(
    filedText({ ...base, state: 'denied', resolvedAt: at }),
    'Denied by you at 14:03:40Z.',
  );
  assert.equal(
    filedText({
      ...base,
      state: 'expired',
      at: T0,
      expiresAt: T0 + 600_000,
      resolvedAt: T0 + 600_000,
    }),
    'Expired at 14:12:51Z after 10 minutes without a decision. Nothing was sent.',
  );
  assert.equal(
    filedText({
      ...base,
      state: 'cancelled',
      resolvedAt: at,
      cancelCause: 'interrupt',
    }),
    'Cancelled at 14:03:40Z when you stopped the analyst. Nothing was sent.',
  );
  assert.equal(
    filedText({
      ...base,
      state: 'cancelled',
      resolvedAt: at,
      cancelCause: 'session',
    }),
    'Cancelled at 14:03:40Z when the session ended. Nothing was sent.',
  );
  assert.equal(
    filedText({
      ...base,
      tool: 'uav_scan_targets',
      klass: 'sensor',
      state: 'approved',
      scope: 'session',
      resolvedAt: Date.UTC(2026, 8, 27, 14, 5, 2),
    }),
    'Approved by you at 14:05:02Z, with sensor tasking allowed until you start a new session.',
  );
  assert.equal(
    filedText({
      ...base,
      state: 'approved',
      resolvedAt: null,
      decidedAt: null,
      replay: true,
    }),
    'Approved by you. Grid search, Drone1.',
    'a replayed record with no known time claims none',
  );
  assert.equal(
    grantPhrase({
      klass: 'sensor',
      grantScope: ['uav_scan_targets', 'uav_capture_image'],
    }),
    'Scan for targets and Capture image',
  );
});

test('a decision error is shown and the slip stays usable', () => {
  const { slip, clock } = mount();
  slip.update({ approval: approval({ state: 'pending', error: 'offline' }) });
  const err = find(slip.el, cls('ic-slip__error'));
  assert.equal(hidden(err), false);
  assert.equal(
    err.textContent,
    "Couldn't send your decision: offline. Try again.",
  );
  clock.advance(ARM_MS);
  assert.equal(disabled(approveBtn(slip)), false);
});

test('destroy stops the timers', () => {
  const { slip, clock } = mount();
  slip.destroy();
  assert.equal(clock.pending(), 0);
});

// ---- console-computed lines -------------------------------------------------------------

test('"Right now" lines only say what the graph says', () => {
  assert.equal(
    rightNowLine('Drone1', VEHICLE),
    'Right now Drone1 is on the ground with 82.0% fuel. Link up.',
  );
  assert.equal(
    rightNowLine('Drone1', {
      landed: false,
      fuel_pct: null,
      link: 'loal',
      bingo_latched: true,
      stale_ms: 42_000,
    }),
    "Right now Drone1 is airborne. Link lost. BINGO is latched: Drone1 is returning home, and that can't be cancelled. Telemetry stale, last 42 s ago.",
  );
  assert.equal(
    rightNowLine('Drone1', null),
    'Right now the console has no reading for Drone1.',
  );
  assert.equal(rightNowLine(null, VEHICLE), '');
});

test('lost-link lines come only from data', () => {
  assert.equal(lostLinkLine('Drone1', null), '');
  assert.equal(lostLinkLine('Drone1', {}), '');
  assert.equal(
    lostLinkLine('Drone1', {
      behaviour: 'climb_for_los',
      climb_to_m: 120,
      declare_after_s: 5,
      escalate_to_rtb_after_s: 30,
    }),
    "If the link drops for 5 s, Drone1 climbs to 120 m to regain the link, then returns to base after 30 s. That's its lost-link plan.",
  );
  assert.equal(
    lostLinkLine('Drone1', { behaviour: 'loiter_far' }),
    'If the link drops, Drone1 follows its "loiter far" plan. That\'s its lost-link plan.',
  );
});

test('Review scrolls only the transcript, never the console around it', () => {
  const { slip, clock } = mount();
  clock.advance(ARM_MS);
  const log = {
    scrollTop: 100,
    getBoundingClientRect: () => ({ top: 50, height: 400 }),
  };
  slip.el.closest = (sel) => (sel === '.ic-log' ? log : null);
  slip.el.rect = { top: 900, left: 20, width: 400, height: 200 };
  let focusOpts = null;
  const heading = find(slip.el, (el) => el.tag === 'h3');
  heading.focus = (opts) => {
    focusOpts = opts;
  };
  slip.review();
  // Centre the slip in the log: 100 + (900 - 50) - (400 - 200) / 2 = 850.
  assert.equal(log.scrollTop, 850);
  assert.notEqual(slip.el.scrolled, true, 'no scrollIntoView');
  assert.deepEqual(focusOpts, { preventScroll: true });
  assert.equal(slip.isArmed(), false, 'Review re-arms');
});

// ---- untrusted text: bidi controls (review: security) ------------------------------------

test('bidi controls in the title, summary, consequences and args never reach the slip', () => {
  const RLO = '\u202e';
  const { slip } = mount({
    approval: {
      title: `Orbit <img src=x> ${RLO}ynneD`,
      summary: `Drone1 · ${RLO}YLDNEIRF\u202c · 60 m AGL`,
      consequences: [`Drone1 flies to ${RLO}evil.`, 'x'.repeat(50_000)],
      args: { vehicle: `Drone1${RLO}`, alt_m_agl: 60 },
    },
  });
  find(slip.el, (el) => textOf(el) === 'Show exact request').fire('click');
  const text = textOf(slip.el);
  assert.doesNotMatch(text, /[\u202a-\u202e\u2066-\u2069]/);
  assert.ok(text.includes('Orbit <img src=x> ynneD'), 'markup stays text');
  assert.ok(text.includes('YLDNEIRF'));
  assert.ok(text.includes('Drone1 flies to evil.'));
  // The exact request shows the control as its JSON escape.
  const args = find(slip.el, cls('ic-slip__args'));
  assert.ok(textOf(args).includes('"vehicle": "Drone1\\u202e"'));
  // Text attributes are clean too.
  const titled = findAll(slip.el, (el) => el.attrs && 'title' in el.attrs);
  for (const el of titled)
    assert.doesNotMatch(el.attrs.title, /[\u202a-\u202e\u2066-\u2069]/);
});
