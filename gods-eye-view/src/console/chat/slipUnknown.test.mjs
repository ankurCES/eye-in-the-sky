/**
 * Unknown approval class fail-safe (WG spec §4.2.1, D7 #8): a class the
 * console doesn't know gives a Deny-only slip with no Approve element in the
 * DOM, whatever the server says about it.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ARM_MS,
  DBLCLICK_MS,
  createSlip,
  denyOnlyOf,
  filedText,
} from './slip.js';
import { classWord, unknownDenyNote, unknownLine } from './slipUnknown.js';
import { initialState, reduce } from './reducer.js';

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

const T0 = Date.UTC(2026, 8, 27, 14, 2, 51);
const XSS = '<img src=x onerror=alert(1)>';
const BIDI = /[‪-‮⁦-⁩]/;

/** An approval as the reducer builds it from an `approval_request`. */
function fold(data) {
  let s = initialState();
  s = reduce(s, {
    type: 'event',
    name: 'approval_request',
    data: {
      approval_id: 'a1',
      call_id: 'c1',
      tool: 'wg_execute_engagement',
      title: 'Execute a simulated engagement',
      summary: 'Blue artillery 1 · trk:T-9',
      args: { pending_id: 'p1' },
      consequences: ['Rolls a notional outcome.'],
      expires_at_ms: T0 + 600_000,
      ...data,
    },
    seq: 1,
    at: T0,
  });
  return s.approvals.a1;
}

function mount(approval) {
  const doc = stubDoc();
  globalThis.document = doc;
  const clock = fakeClock(T0 + 30_000);
  const decisions = [];
  const announcements = [];
  const slip = createSlip(
    {
      approval,
      assessment: { state: 'none', reasons: [] },
      vehicle: null,
      before: null,
      queue: { index: 1, total: 1 },
      conflicts: [],
      caveats: [],
      detections: null,
      now: clock.now(),
      reducedMotion: false,
    },
    {
      decide: async (decision, note) => {
        decisions.push({ decision, note });
      },
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

test('the reducer keeps the raw class word and maps it to "unknown"', () => {
  const a = fold({ class: 'engagement', acknowledge_required: true });
  assert.equal(a.klass, 'unknown');
  assert.equal(a.rawClass, 'engagement');
  assert.equal(a.acknowledgeRequired, true);
  assert.equal(fold({}).klass, 'unknown', 'a missing class is unknown too');
  assert.equal(fold({ class: 'sim' }).klass, 'sim');
});

test('an unknown class gives a Deny-only slip: no Approve element in the DOM, ever', async () => {
  for (const data of [
    { class: 'engagement' },
    { class: 'zzz' },
    { class: '__proto__' },
    {},
    // Even a dry-runnable launch with a passing dry run and a session grant.
    {
      class: 'mystery',
      tool: 'mission_grid_search',
      args: { vehicle: 'Drone1' },
      allow_session: true,
      grant_scope: ['mission_grid_search'],
      dry_run: { ok: true, gate: { required_pct: 10, available_pct: 90 } },
    },
  ]) {
    const { slip, clock, decisions } = mount(fold(data));
    assert.equal(slip.el.attrs['data-class'], 'unknown');
    assert.equal(slip.el.attrs['data-variant'], 'deny_only');
    assert.deepEqual(approveEls(slip.el), [], JSON.stringify(data));
    clock.advance(ARM_MS * 3);
    slip.update({ now: clock.now() });
    assert.deepEqual(approveEls(slip.el), [], 'still none after arming');
    assert.equal(slip.isArmed(), true, 'arming is harmless: nothing to press');
    // No grant checkbox and no acknowledgement for an unknown class.
    assert.equal(hidden(find(slip.el, cls('ic-slip__grant'))), true);
    assert.equal(hidden(find(slip.el, cls('ic-slip__ack'))), true);
    const buttons = find(slip.el, cls('ic-slip__actions')).children;
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].attrs['data-action'], 'deny');
    buttons[0].fire('click', { detail: 1 });
    await Promise.resolve();
    assert.deepEqual(decisions.at(-1), {
      decision: 'deny',
      note: unknownDenyNote(data.class ?? null),
    });
  }
});

test('the unknown slip shows the class phrase, the fixed line and the server text as text', () => {
  const { slip } = mount(fold({ class: 'engagement' }));
  const text = textOf(slip.el);
  assert.ok(text.includes('Unrecognised action'));
  assert.ok(text.includes('Execute a simulated engagement'));
  assert.ok(text.includes('wg_execute_engagement'), 'the tool, in mono');
  assert.ok(
    find(
      slip.el,
      (el) => cls('ic-mono')(el) && textOf(el) === 'wg_execute_engagement',
    ),
  );
  assert.ok(text.includes('Blue artillery 1'));
  assert.ok(text.includes('Rolls a notional outcome.'));
  const line = find(slip.el, cls('ic-slip__denyonly'));
  assert.equal(hidden(line), false);
  assert.equal(
    textOf(line),
    'This console doesn\'t recognise the approval class "engagement", so it can\'t show what this does or approve it. Update the app, or deny it.',
  );
  assert.equal(unknownLine('engagement'), textOf(line));
  assert.equal(
    unknownDenyNote('engagement'),
    'The console can\'t approve the "engagement" class.',
  );
  // No undo or stop section is invented for an action nobody recognised.
  assert.ok(!text.includes('How to undo it'));
  assert.ok(!text.includes('How to stop it'));
  // No policy line either.
  assert.equal(hidden(find(slip.el, cls('ic-slip__policy'))), true);
});

test('keyboard and note paths on an unknown slip only ever deny', async () => {
  const { slip, clock, decisions } = mount(fold({ class: 'engagement' }));
  clock.advance(ARM_MS);
  const deny = find(slip.el, (el) => el.attrs?.['data-action'] === 'deny');
  deny.fire('keydown', { key: 'Enter', repeat: false });
  await Promise.resolve();
  assert.equal(decisions.at(-1).decision, 'deny');
  // ⌘Enter in the note field denies with the typed note and the class note.
  const { slip: s2, decisions: d2 } = mount(fold({ class: 'engagement' }));
  const note = find(s2.el, cls('ic-slip__note'));
  note.value = 'why?';
  note.fire('input');
  note.fire('keydown', { key: 'Enter', metaKey: true });
  await Promise.resolve();
  assert.deepEqual(d2.at(-1), {
    decision: 'deny',
    note: 'The console can\'t approve the "engagement" class. why?',
  });
  clock.advance(DBLCLICK_MS);
  assert.equal(
    decisions.filter((d) => d.decision !== 'deny').length,
    0,
    'nothing approves',
  );
});

test('denyOnlyOf is null for known classes and "unknown" only for unknown ones', () => {
  for (const klass of ['sensor', 'command', 'sim', 'safety_override']) {
    assert.equal(denyOnlyOf({ klass, tool: 'uav_land' }, null), null, klass);
  }
  assert.equal(
    denyOnlyOf({ klass: 'unknown', rawClass: 'x' }, null).kind,
    'unknown',
  );
});

test('XSS and bidi fixtures in an unknown slip render as text (§3.11)', () => {
  const { slip } = mount(
    fold({
      class: `${XSS}‮evil‬`,
      title: `${XSS} ‮evil‬`,
      summary: `${XSS} · ‮evil‬`,
      consequences: [XSS, '‮evil‬'],
    }),
  );
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
  assert.ok(text.includes('evil'));
  // The class word is capped and bidi-free.
  assert.equal(classWord('‮' + 'x'.repeat(80)).length, 40);
  assert.equal(classWord(null), 'none');
});

test('a filed unknown slip reads like any other record', () => {
  const a = {
    ...fold({ class: 'engagement' }),
    state: 'denied',
    resolvedAt: T0 + 60_000,
    note: 'The console can\'t approve the "engagement" class.',
  };
  assert.equal(
    filedText(a),
    'Denied by you at 14:03:51Z. Your note: “The console can\'t approve the "engagement" class.”',
  );
});
