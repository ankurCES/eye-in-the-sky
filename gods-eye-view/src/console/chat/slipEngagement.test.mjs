import test from 'node:test';
import assert from 'node:assert/strict';

import { ARM_MS, DBLCLICK_MS, createSlip, filedText } from './slip.js';
import {
  ENGAGEMENT_SLIP_COPY,
  ENGAGE_ARM_MS,
  SCENARIO_TIP,
  engagementOutcome,
  engagementTitle,
  exposureText,
  seenText,
  straightText,
} from './slipEngagement.js';
import { assessEngagement } from './validateEngagement.js';
import { PREVIEW_MISSING } from './validateTheater.js';

// ---- stub DOM (as slip.test.mjs) ---------------------------------------------------

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
    get textContent() {
      return this._text;
    },
    set textContent(v) {
      this.children = [];
      this._text = String(v ?? '');
    },
    set innerHTML(v) {
      this.html = String(v);
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
const disabled = (el) => el.getAttribute('aria-disabled') === 'true';
const flush = () => new Promise((r) => setTimeout(r, 0));
const BIDI = /[‪-‮⁦-⁩]/;
const XSS = '<img src=x onerror=alert(1)>';

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
  };
}

const T0 = Date.UTC(2026, 8, 28, 14, 2, 51);

/** The §5.2.7 `engagement` preview. */
function preview(extra = {}) {
  return {
    id: 'WG-3fa9c1-E7',
    kind: 'blue_strike',
    verb_kind: 'engagement',
    attacker: { id: 'frc:blue-artillery-1', label: 'Blue artillery 1' },
    target: {
      track_id: 'TRK-9',
      graph_id: 'trk:TRK-9',
      label: 'Air-defence guns',
      perceived_class: 'aaa_towed',
      confidence: 'probable',
      sightings: 2,
      last_seen_ms: T0 - 60_000,
      lat: 47.65,
      lon: -122.13,
      scenario: true,
      protected: false,
    },
    vector: null,
    p_notional: { effect: 0.62, destroyed: 0.2, damaged: 0.25 },
    inputs: [
      'Range 3.2 km of 20.0 km',
      'Perceived as air-defence guns, probable',
    ],
    range_m: 3200,
    seed: 4417,
    engine: 'wg-notional/1',
    caveats: ['Probabilities are notional play-balance numbers.'],
    checks: [
      { text: 'Target is a simulated scenario unit', ok: true },
      { text: 'In range', ok: true },
      { text: 'Wargame session active', ok: true },
    ],
    ...extra,
  };
}

function approval(extra = {}) {
  return {
    id: 'a1',
    callId: 'c1',
    tool: 'wg_execute_engagement',
    klass: 'engagement',
    rawClass: 'engagement',
    acknowledgeRequired: true,
    title: 'Execute a simulated engagement',
    summary: 'Blue artillery 1 · TRK-9',
    args: {
      pending_id: 'WG-3fa9c1-E7',
      shooter_id: 'frc:blue-artillery-1',
      target_track_id: 'TRK-9',
    },
    consequences: [
      'Rolls one simulated outcome for this engagement against a scenario unit.',
      'Nothing real is fired.',
      'The outcome stands for the rest of this wargame; only ending the wargame clears it.',
    ],
    engagementPreview: preview(),
    allowSession: false,
    expiresAt: T0 + 600_000,
    at: T0,
    state: 'pending',
    note: null,
    error: null,
    ...extra,
  };
}

const NONE = { state: 'none', ok: true, reasons: [], stale: [], line: null };

function mount(overrides = {}, deps = {}) {
  const doc = stubDoc();
  globalThis.document = doc;
  const clock = fakeClock(T0 + 30_000);
  const decisions = [];
  const announcements = [];
  const model = {
    approval: approval(overrides.approval),
    assessment: NONE,
    console: { held: true, refused: false },
    vehicle: null,
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
    decide: async (decision, note, opts) => {
      decisions.push({ decision, note, acknowledged: opts?.acknowledged });
    },
    announce: (text, opts) => announcements.push({ text, ...opts }),
    clock,
    raf: null,
    caf: null,
    ResizeObserver: null,
    doc,
    getRect: (el) => el.getBoundingClientRect(),
    ...deps,
  });
  return { slip, doc, clock, decisions, announcements, model };
}

const approveEl = (slip) =>
  byAction(slip.el, 'approve') || byAction(slip.el, 'approve-anyway');
const ackBox = (slip) =>
  find(find(slip.el, cls('ic-slip__ack')), (el) => el.tag === 'input');

function tick(slip, on = true) {
  const box = ackBox(slip);
  box.checked = on;
  box.fire('change');
}

async function press(slip, clock) {
  const b = approveEl(slip);
  b.fire('pointerdown');
  b.fire('click', { detail: 1 });
  clock.advance(DBLCLICK_MS);
  await flush();
}

// ---- anatomy -------------------------------------------------------------------------

test('the engagement slip: hatch, phrase, title, fixed line, checks, undo, box, verb', () => {
  const { slip } = mount();
  assert.equal(slip.el.attrs['data-class'], 'engagement');
  const hatch = find(slip.el, cls('ic-slip__hatch'));
  assert.ok(hatch, 'a Sand hatch on top of the band');
  assert.equal(hatch.attrs['aria-hidden'], 'true');
  const text = textOf(slip.el);
  const order = [
    'Simulates an engagement',
    'Simulated engagement on Air-defence guns',
    'Blue artillery 1',
    'Air-defence guns',
    'TRK-9',
    'Requested',
    'Simulated. Nothing real is fired. Air-defence guns is a scenario unit, not a real place.',
    'What happens',
    'Rolls one simulated outcome for this engagement against a scenario unit.',
    'Checks',
    'Chance of effect',
    '≈ 0.62, notional',
    'Estimated',
    'Inputs',
    'Range 3.2 km of 20.0 km',
    'Rules',
    'Target is a simulated scenario unit',
    'Scenario',
    'Target seen',
    'Seen as Air-defence guns, probable, 2 sightings, 14:01:51Z',
    'Measured',
    'Replay',
    'Seed 4417',
    'Engine wg-notional/1',
    'Probabilities are notional play-balance numbers.',
    'Assumed',
    "What can't be undone",
    'The outcome stands for the rest of this wargame. Only ending the wargame clears it.',
    "I understand this rolls a simulated outcome against Air-defence guns that can't be undone.",
    'Show exact request',
    'Deny',
    'Approve simulated engagement',
    "Engagements are approved one at a time. They can't be allowed for the session.",
  ];
  let at = -1;
  for (const piece of order) {
    const next = text.indexOf(piece, at + 1);
    assert.ok(next > at, `"${piece}" comes after the previous piece`);
    at = next;
  }
  // The track id is mono; the Scenario tag carries its tooltip.
  assert.ok(
    find(slip.el, (el) => cls('ic-mono')(el) && textOf(el) === 'TRK-9'),
  );
  const scenario = find(
    slip.el,
    (el) => el.attrs?.['data-register'] === 'scenario',
  );
  assert.equal(scenario.attrs.title, SCENARIO_TIP);
  assert.equal(textOf(scenario), 'Scenario');
  // Never session-grantable, never a dot, never shouting.
  assert.equal(hidden(find(slip.el, cls('ic-slip__grant'))), true);
  assert.ok(!text.includes('·'));
  assert.ok(!/SIMULATED/.test(text));
  assert.deepEqual(actions(slip), ['deny', 'approve']);
  assert.ok(!text.includes('corridor'), 'no vector, no corridor segment');
  assert.ok(!text.includes('Exposure'));
});

test('a strike names itself; a vector adds the corridor and exposure rows', () => {
  const vector = {
    id: 'vec:cor-2',
    exposure_s: 42,
    p_survive: 0.91,
    delta_exposure_s: 118,
    delta_length_m: 1400,
  };
  const { slip } = mount({
    approval: { engagementPreview: preview({ verb_kind: 'strike', vector }) },
  });
  const text = textOf(slip.el);
  assert.ok(text.includes('Simulated strike on Air-defence guns'));
  assert.ok(text.includes('corridor'));
  assert.ok(text.includes('≈ 42 s exposed; survival ≈ 0.91'));
  assert.ok(text.includes('Against a straight route'));
  assert.ok(text.includes('≈ 118 s less exposure, 1.4 km longer'));
  assert.equal(textOf(approveEl(slip)), 'Approve simulated strike');
  assert.equal(
    engagementTitle(
      approval({ engagementPreview: preview({ verb_kind: 'strike' }) }),
    ),
    'Simulated strike on Air-defence guns',
  );
  assert.equal(
    engagementTitle(approval({ engagementPreview: null })),
    'Execute a simulated engagement',
    'the server title when there is no preview',
  );
  assert.equal(exposureText(null), '');
  assert.equal(
    straightText({ delta_exposure_s: -20, delta_length_m: -300 }),
    '≈ 20 s more exposure, 300 m shorter',
  );
  assert.equal(seenText({ label: 'x', sightings: 1 }), 'Seen as x, 1 sighting');
});

// ---- arming ------------------------------------------------------------------------

test('arming: nothing before the box; 1600 ms after it; uncheck disarms', async () => {
  assert.equal(ENGAGE_ARM_MS, 1600);
  const { slip, clock, decisions } = mount();
  clock.advance(60_000);
  assert.equal(disabled(approveEl(slip)), true, 'no amount of waiting arms it');
  assert.equal(slip.isArmed(), false);
  await press(slip, clock);
  assert.deepEqual(decisions, [], 'a press before the box does nothing');
  tick(slip);
  clock.advance(ARM_MS);
  assert.equal(
    disabled(approveEl(slip)),
    true,
    'not at the 800 ms of other slips',
  );
  clock.advance(ENGAGE_ARM_MS - ARM_MS - 1);
  assert.equal(disabled(approveEl(slip)), true, 'not at 1599 ms');
  clock.advance(1);
  assert.equal(disabled(approveEl(slip)), false, 'armed at 1600 ms');
  tick(slip, false);
  assert.equal(disabled(approveEl(slip)), true, 'unchecking disarms');
  await press(slip, clock);
  assert.deepEqual(decisions, []);
  tick(slip);
  clock.advance(ENGAGE_ARM_MS - 1);
  assert.equal(disabled(approveEl(slip)), true, 're-checking arms from zero');
  clock.advance(1);
  await press(slip, clock);
  assert.deepEqual(decisions, [
    { decision: 'approve', note: null, acknowledged: true },
  ]);
});

test('every re-arm trigger restarts the 1600 ms (layout, visibility, validation, review)', () => {
  const { slip, clock, doc } = mount();
  tick(slip);
  clock.advance(ENGAGE_ARM_MS);
  assert.equal(slip.isArmed(), true);
  // Layout shift.
  slip.checkLayout();
  slip.el.rect = { ...slip.el.rect, top: 180 };
  slip.checkLayout();
  assert.equal(slip.isArmed(), false);
  clock.advance(ENGAGE_ARM_MS);
  assert.equal(slip.isArmed(), true);
  // Returning to the tab.
  doc.fire('visibilitychange');
  assert.equal(slip.isArmed(), false);
  clock.advance(ENGAGE_ARM_MS);
  // A validation change.
  slip.update({
    assessment: { ...NONE, state: 'stale', stale: [{ text: 'x' }] },
  });
  assert.equal(slip.isArmed(), false);
  clock.advance(ENGAGE_ARM_MS);
  assert.equal(slip.isArmed(), true);
  slip.review();
  assert.equal(slip.isArmed(), false);
});

test('reduced motion shows "Ready in 2 s" while arming', () => {
  const { slip, clock } = mount({ reducedMotion: true });
  assert.equal(textOf(approveEl(slip)), 'Approve simulated engagement');
  tick(slip);
  assert.equal(textOf(approveEl(slip)), ENGAGEMENT_SLIP_COPY.readyIn);
  assert.equal(ENGAGEMENT_SLIP_COPY.readyIn, 'Ready in 2 s');
  clock.advance(ENGAGE_ARM_MS);
  assert.equal(textOf(approveEl(slip)), 'Approve simulated engagement');
});

test('a double-click, key repeat or ⌘Enter never approves; ⌘Enter in the note denies', async () => {
  const { slip, clock, decisions } = mount();
  tick(slip);
  clock.advance(ENGAGE_ARM_MS);
  const b = approveEl(slip);
  // A real double-click: detail 1, detail 2, dblclick.
  b.fire('pointerdown');
  b.fire('click', { detail: 1 });
  b.fire('pointerdown');
  b.fire('click', { detail: 2 });
  b.fire('dblclick', { detail: 2 });
  clock.advance(DBLCLICK_MS * 2);
  await flush();
  // Key auto-repeat.
  b.fire('keydown', { key: 'Enter', repeat: true });
  b.fire('keydown', { key: ' ', repeat: true });
  await flush();
  // ⌘Enter on the button is not a shortcut.
  b.fire('keydown', { key: 'Enter', metaKey: true, repeat: true });
  await flush();
  assert.deepEqual(decisions, []);
  const note = find(slip.el, cls('ic-slip__note'));
  note.value = 'Not this one.';
  note.fire('keydown', { key: 'Enter', metaKey: true });
  await flush();
  assert.deepEqual(decisions, [
    { decision: 'deny', note: 'Not this one.', acknowledged: false },
  ]);
});

test('Enter on the armed, focused Approve approves once, with acknowledged', async () => {
  const { slip, clock, decisions } = mount();
  tick(slip);
  clock.advance(ENGAGE_ARM_MS);
  approveEl(slip).fire('keydown', { key: 'Enter' });
  await flush();
  assert.deepEqual(decisions, [
    { decision: 'approve', note: null, acknowledged: true },
  ]);
});

// ---- Deny-only, blocked and stale ----------------------------------------------------

const approveEls = (root) =>
  findAll(
    root,
    (el) =>
      cls('ic-slip__approve')(el) ||
      /^approve/.test(el.attrs?.['data-action'] || ''),
  );

function assertDenyOnly(slip, line) {
  assert.equal(slip.el.attrs['data-variant'], 'deny_only');
  assert.deepEqual(approveEls(slip.el), [], 'no Approve element in the DOM');
  assert.deepEqual(actions(slip), ['deny']);
  const lineEl = find(slip.el, cls('ic-slip__denyonly'));
  assert.equal(hidden(lineEl), false);
  assert.equal(textOf(lineEl), line);
}

test('Deny-only when the preview is missing or lacks a required key (§3.6)', async () => {
  for (const engagementPreview of [
    null,
    preview({ checks: undefined }),
    preview({ target: null }),
    preview({ attacker: undefined }),
    preview({ p_notional: {} }),
  ]) {
    const { slip, clock, decisions } = mount({
      approval: { engagementPreview },
    });
    assertDenyOnly(slip, PREVIEW_MISSING);
    assert.equal(
      PREVIEW_MISSING,
      "The console couldn't build this preview, so it can't be approved.",
    );
    // Even a ticked box never brings an Approve back.
    tick(slip);
    clock.advance(ENGAGE_ARM_MS * 2);
    assert.deepEqual(approveEls(slip.el), []);
    byAction(slip.el, 'deny').fire('click', { detail: 1 });
    await flush();
    assert.equal(decisions[0].decision, 'deny');
    assert.equal(decisions[0].acknowledged, false);
  }
});

test('Deny-only without the console key, or when another client claimed it (§3.5)', () => {
  let { slip } = mount({ console: null });
  assertDenyOnly(
    slip,
    "This console can't approve engagements: it holds no engagement approval key.",
  );
  ({ slip } = mount({ console: { held: false, refused: true } }));
  assertDenyOnly(
    slip,
    "This console can't approve engagements: another client claimed them.",
  );
  // The key arriving later (the claim finished) brings Approve back.
  slip.update({ console: { held: true, refused: false } });
  assert.deepEqual(actions(slip), ['deny', 'approve']);
});

test('blocked: a failing check makes the slip Deny-only with its reason', () => {
  const failing = preview({
    checks: [
      { text: 'Target is a simulated scenario unit', ok: true },
      { text: 'In range', ok: false },
    ],
  });
  const a = approval({ engagementPreview: failing });
  const { slip } = mount({
    approval: { engagementPreview: failing },
    assessment: assessEngagement(a, null),
  });
  assertDenyOnly(
    slip,
    "This can't be approved here: the check “In range” failed.",
  );
  const rules = find(slip.el, cls('ic-slip__checks'));
  assert.deepEqual(
    rules.children.map((li) => li.attrs['data-ok']),
    ['true', 'false'],
  );
  assert.ok(textOf(rules).includes('Failed: In range'));
});

test('a press on a slip that just became blocked, or lost its key, is swallowed', async () => {
  let fresh = NONE;
  let access = { held: true, refused: false };
  const { slip, clock, decisions, announcements } = mount(
    {},
    { revalidate: () => fresh, consoleAccess: () => access },
  );
  tick(slip);
  clock.advance(ENGAGE_ARM_MS);
  fresh = { state: 'blocked', ok: false, line: 'The wargame has ended.' };
  await press(slip, clock);
  assert.deepEqual(decisions, []);
  assert.deepEqual(announcements.at(-1), {
    text: 'Conditions changed. Review the slip again.',
    assertive: true,
  });
  assertDenyOnly(slip, 'The wargame has ended.');

  const second = mount({}, { consoleAccess: () => access });
  tick(second.slip);
  second.clock.advance(ENGAGE_ARM_MS);
  access = { held: false, refused: true };
  await press(second.slip, second.clock);
  assert.deepEqual(second.decisions, []);
});

test('stale: [Deny] [Ask for a fresh plan], and Approve anyway still boxed and re-armed', async () => {
  const stale = {
    state: 'stale',
    ok: true,
    reasons: [{ code: 'moved', text: 'the target moved about 310 m' }],
    stale: [{ code: 'moved', text: 'the target moved about 310 m' }],
    line: 'Conditions changed since this was proposed: the target moved about 310 m. The numbers above may be out of date.',
  };
  const { slip, clock, decisions } = mount({ assessment: stale });
  assert.deepEqual(actions(slip), ['deny', 'ask-fresh-plan', 'approve-anyway']);
  assert.equal(slip.el.attrs['data-variant'], 'stale');
  const strip = find(slip.el, cls('ic-slip__stale'));
  assert.equal(strip.attrs.role, 'note');
  assert.equal(textOf(strip), stale.line);
  assert.equal(
    textOf(byAction(slip.el, 'ask-fresh-plan')),
    'Ask for a fresh plan',
  );
  const anyway = approveEl(slip);
  assert.equal(textOf(anyway), 'Approve anyway');
  assert.ok(!anyway.className.includes('is-primary'), 'an outline button');
  clock.advance(10_000);
  assert.equal(disabled(anyway), true, 'still boxed');
  tick(slip);
  clock.advance(ENGAGE_ARM_MS - 1);
  assert.equal(disabled(approveEl(slip)), true);
  clock.advance(1);
  assert.equal(disabled(approveEl(slip)), false);
  byAction(slip.el, 'ask-fresh-plan').fire('click', { detail: 1 });
  await flush();
  assert.deepEqual(decisions, [
    {
      decision: 'deny',
      note: ENGAGEMENT_SLIP_COPY.freshPlanNote,
      acknowledged: false,
    },
  ]);
});

// ---- untrusted text --------------------------------------------------------------------

test('XSS and bidi fixtures in target and attacker labels render as text', () => {
  const p = preview({
    attacker: { id: 'frc:blue-artillery-1', label: `${XSS}‮evil‬` },
  });
  p.target = { ...p.target, label: `${XSS}‮evil‬` };
  const { slip } = mount({ approval: { engagementPreview: p } });
  assert.equal(
    find(slip.el, (el) => el.tag === 'img'),
    null,
  );
  assert.equal(
    find(slip.el, (el) => Object.hasOwn(el.attrs || {}, 'onerror')),
    null,
  );
  const text = textOf(slip.el);
  assert.ok(text.includes('<img src=x onerror=alert(1)>'));
  assert.ok(!BIDI.test(text), 'no bidi control reaches the slip');
  assert.ok(text.includes(`Simulated engagement on ${XSS}evil`));
  assert.ok(
    text.includes(
      `I understand this rolls a simulated outcome against ${XSS}evil that can't be undone.`,
    ),
  );
});

// ---- filed -----------------------------------------------------------------------------

test('filed: "Approved by you at {Z}. Simulated {kind} on {target}." then the live outcome', () => {
  const at = T0 + 90_000;
  const decided = approval({ state: 'approved', resolvedAt: at });
  assert.equal(
    filedText(decided),
    'Approved by you at 14:04:21Z. Simulated engagement on Air-defence guns.',
  );
  const { slip } = mount();
  slip.update({
    approval: decided,
    outcome: { text: 'Outcome hidden in blue view.', hidden: true, tone: null },
  });
  let line = find(slip.el, cls('ic-slip__outcome'));
  assert.equal(textOf(line), 'Outcome hidden in blue view.');
  assert.equal(line.attrs['data-hidden'], 'true');
  slip.update({
    outcome: {
      text: 'Outcome at 14:04:40Z: damaged (simulated).',
      hidden: false,
      tone: null,
    },
  });
  line = find(slip.el, cls('ic-slip__outcome'));
  assert.equal(textOf(line), 'Outcome at 14:04:40Z: damaged (simulated).');
  // A denied engagement has no outcome line.
  const denied = mount();
  denied.slip.update({
    approval: approval({ state: 'denied', resolvedAt: at }),
    outcome: { text: 'Outcome at 14:04:40Z: damaged (simulated).' },
  });
  assert.equal(find(denied.slip.el, cls('ic-slip__outcome')), null);
});

test('engagementOutcome reads the eng: node: hidden, shown, unknown, or not yet', () => {
  const a = approval();
  const g = (attrs) => ({
    nodes: [{ id: 'eng:WG-3fa9c1-E7', type: 'engagement', attrs }],
  });
  assert.equal(engagementOutcome(a, g({ phase: 'authorized' })), null);
  assert.equal(engagementOutcome(a, null), null);
  assert.deepEqual(
    engagementOutcome(a, g({ phase: 'adjudicated', outcome_hidden: true })),
    { text: 'Outcome hidden in blue view.', hidden: true, tone: null },
  );
  assert.deepEqual(
    engagementOutcome(
      a,
      g({
        phase: 'adjudicated',
        outcome: 'destroyed',
        outcome_hidden: false,
        adjudicated_at_ms: T0 + 100_000,
      }),
    ),
    {
      text: 'Outcome at 14:04:31Z: destroyed (simulated).',
      hidden: false,
      tone: null,
    },
  );
  assert.deepEqual(
    engagementOutcome(a, g({ phase: 'adjudicated', outcome: 'vaporised' })),
    {
      text: 'Outcome: outcome not recognised (simulated).',
      hidden: false,
      tone: 'unknown',
    },
  );
});

test('the wargame sim slips say how to undo them honestly', () => {
  const sim = (tool) =>
    mount({
      approval: {
        tool,
        klass: 'sim',
        rawClass: 'sim',
        acknowledgeRequired: false,
        engagementPreview: undefined,
        title: tool,
        args: {},
      },
    }).slip;
  let text = textOf(sim('wg_session_start').el);
  assert.ok(text.includes('End the wargame with another change'));
  assert.ok(text.includes('Approve start'));
  text = textOf(sim('wg_spawn_force').el);
  assert.ok(text.includes('Only ending the wargame removes scenario units.'));
  assert.ok(!text.includes('Reverse it with another simulation change'));
  text = textOf(sim('wg_session_end').el);
  assert.ok(text.includes('Approve end'));
  assert.ok(text.includes("This session's scenario units don't come back."));
});

test('a Deny-only engagement slip shows no acknowledgement box', () => {
  const { slip } = mount({ console: null });
  assert.equal(hidden(find(slip.el, cls('ic-slip__ack'))), true);
  slip.update({ console: { held: true, refused: false } });
  assert.equal(hidden(find(slip.el, cls('ic-slip__ack'))), false);
});
