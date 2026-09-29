import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HttpError, OfflineError } from './api.js';
import {
  STRIP_COPY,
  STRIP_H,
  activeWargame,
  createWargameStrip,
  endFailedText,
  simScaleText,
  stripModel,
} from './wargameStrip.js';

// ---- a stub document (uavDom's h() needs only createElement) ----------------------

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
      parent: null,
      append(...kids) {
        for (const k of kids) {
          if (k && typeof k === 'object') k.parent = this;
          this.children.push(k);
        }
      },
      replaceChildren(...kids) {
        this.children = [];
        this.append(...kids);
      },
      remove() {
        if (this.parent)
          this.parent.children = this.parent.children.filter((c) => c !== this);
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
      fire(t, ev = {}) {
        for (const f of this.listeners[t] || []) f({ target: this, ...ev });
      },
      focus() {
        doc.activeElement = this;
      },
    };
    return el;
  };
  doc.createElement = mk;
  return doc;
}

function withDom(fn) {
  return async () => {
    const saved = globalThis.document;
    const doc = stubDom();
    globalThis.document = doc;
    try {
      await fn(doc);
    } finally {
      globalThis.document = saved;
    }
  };
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

function all(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const kid of root.children || []) all(kid, pred, out);
  return out;
}

const byClass = (root, cls) =>
  all(root, (el) =>
    String(el.className || '')
      .split(/\s+/)
      .includes(cls),
  )[0] || null;
const isHidden = (el) => Boolean(el && 'hidden' in el.attrs);
const flush = async (n = 4) => {
  for (let i = 0; i < n; i += 1) await new Promise((r) => setImmediate(r));
};

const STARTED = Date.UTC(2026, 8, 28, 14, 2, 11);

function sessionGraph(wg = {}, theater = {}) {
  return {
    scope: 'theater',
    theater: { id: 'default', label: 'Redmond (AirSim default)', ...theater },
    nodes: [],
    meta: {
      counts: { track: 4 },
      wargame: {
        active: true,
        session_id: 'WG-1a2b3c',
        started_at_ms: STARTED,
        seed: 4417,
        engine: 'wg-notional/1',
        time_scale: 4,
        truth_view: true,
        ...wg,
      },
    },
  };
}

// ---- the model --------------------------------------------------------------------

test('the strip model exists only while a session is active', () => {
  assert.equal(activeWargame(null), null);
  assert.equal(activeWargame({ meta: {} }), null);
  assert.equal(
    activeWargame({ meta: { wargame: { active: false, last: null } } }),
    null,
  );
  assert.equal(stripModel({ meta: { wargame: { active: 'yes' } } }), null);
  assert.deepEqual(stripModel(sessionGraph()), {
    sessionId: 'WG-1a2b3c',
    theater: 'Redmond (AirSim default)',
    started: '14:02:11Z',
    scale: '×4',
    view: 'Umpire view',
  });
  const blue = stripModel(sessionGraph({ truth_view: false, time_scale: 1 }));
  assert.equal(blue.view, 'Blue view');
  assert.equal(blue.scale, null, 'no "Sim time ×1"');
  assert.equal(stripModel(sessionGraph({ truth_view: null })).view, null);
  // The operator's End hides the strip before the next poll says so.
  assert.equal(stripModel(sessionGraph(), { endedSession: 'WG-1a2b3c' }), null);
  assert.ok(stripModel(sessionGraph(), { endedSession: 'WG-other' }));
});

test('sim speed reads ×n only when it is not 1', () => {
  assert.equal(simScaleText(4), '×4');
  assert.equal(simScaleText(2.5), '×2.5');
  assert.equal(simScaleText(1), null);
  assert.equal(simScaleText(0), null);
  assert.equal(simScaleText('4'), null);
  // The theater block's scale stands in when meta.wargame has none.
  assert.equal(
    stripModel(sessionGraph({ time_scale: undefined }, { time_scale: 3 }))
      .scale,
    '×3',
  );
});

test('untrusted session and theater strings are bidi-stripped and cut short', () => {
  const m = stripModel(
    sessionGraph(
      { session_id: 'WG-‮evil‬' },
      { label: `${'<img src=x onerror=alert(1)>'}${'x'.repeat(200)}` },
    ),
  );
  assert.equal(m.sessionId, 'WG-evil');
  assert.ok(m.theater.startsWith('<img src=x onerror=alert(1)>'));
  assert.ok(m.theater.length <= 60);
  assert.doesNotMatch(JSON.stringify(m), /[‪-‮⁦-⁩]/);
});

test("a refusal reads Couldn't end the wargame: {error}. with one full stop", () => {
  assert.equal(
    endFailedText(
      new HttpError('no wargame is running', {
        status: 409,
        body: { error: 'wargame_inactive' },
      }),
    ),
    "Couldn't end the wargame: no wargame is running.",
  );
  assert.equal(
    endFailedText(new OfflineError()),
    "Couldn't end the wargame: can't reach the local service.",
  );
  assert.equal(
    endFailedText({ message: 'HTTP 500' }),
    "Couldn't end the wargame: HTTP 500.",
  );
  assert.equal(
    endFailedText(null),
    "Couldn't end the wargame: no answer from the local service.",
  );
});

test('the copy deck is sentence case, never "·", never a capitalised SIMULATED', () => {
  assert.equal(STRIP_H, 28);
  const strings = Object.values(STRIP_COPY).map((v) =>
    typeof v === 'function' ? v('X') : v,
  );
  for (const s of strings) {
    assert.doesNotMatch(s, /·/);
    assert.doesNotMatch(s, /SIMULATED/);
  }
  assert.equal(STRIP_COPY.mark, 'Simulated wargame');
  assert.equal(STRIP_COPY.honesty, 'Simulated wargame.');
  assert.equal(
    STRIP_COPY.confirm,
    'End the wargame? Scenario units and waiting engagements are cleared. The after-action review is kept. Aircraft keep their current tasks.',
  );
  assert.equal(
    STRIP_COPY.endedByYou('14:20:00Z'),
    'Wargame ended by you at 14:20:00Z.',
  );
});

// ---- the strip -------------------------------------------------------------------

function makeStrip(opts = {}) {
  const log = { ended: [], announced: [], calls: 0 };
  const strip = createWargameStrip({
    endWargame:
      opts.endWargame ??
      (async () => {
        log.calls += 1;
        return { ok: true, aar_id: 'aar-WG-1a2b3c' };
      }),
    onEnded: (info) => log.ended.push(info),
    announce: (t, p) => log.announced.push([t, p]),
    now: () => STARTED + 60_000,
  });
  return { strip, log };
}

test(
  'the strip is hidden with no session and builds nothing',
  withDom(() => {
    const { strip } = makeStrip();
    assert.ok(isHidden(strip.element));
    assert.equal(strip.element.tag, 'section');
    assert.equal(strip.element.attrs['aria-label'], 'Simulated wargame');
    strip.update(stripModel({ meta: { wargame: { active: false } } }));
    assert.ok(isHidden(strip.element));
    assert.equal(textOf(strip.element), '');
    assert.equal(strip.model, null);
  }),
);

test(
  'an active session shows the strip line, and hides it again when it ends',
  withDom(() => {
    const { strip } = makeStrip();
    strip.update(stripModel(sessionGraph()));
    assert.ok(!isHidden(strip.element));
    assert.equal(
      textOf(strip.element),
      'Simulated wargame Session WG-1a2b3c Redmond (AirSim default) Started 14:02:11Z Sim time ×4 Umpire view End wargame',
    );
    assert.equal(
      byClass(strip.element, 'ic-wgstrip__mono').children[0],
      'WG-1a2b3c',
    );
    // The full line for a pointer when the strip is cut short.
    assert.match(
      byClass(strip.element, 'ic-wgstrip__details').attrs.title,
      /^Session WG-1a2b3c {3}Redmond/,
    );
    strip.update(null);
    assert.ok(isHidden(strip.element));
  }),
);

test(
  'an XSS or bidi theater label reaches the strip only as text',
  withDom(() => {
    const { strip } = makeStrip();
    strip.update(
      stripModel(
        sessionGraph({}, { label: '<img src=x onerror=alert(1)>‮evil' }),
      ),
    );
    assert.equal(
      all(strip.element, (el) => el.tag === 'img').length,
      0,
      'no img element',
    );
    assert.equal(
      all(strip.element, (el) => 'onerror' in (el.attrs || {})).length,
      0,
    );
    assert.match(textOf(strip.element), /<img src=x onerror=alert\(1\)>evil/);
    assert.doesNotMatch(textOf(strip.element), /[‪-‮⁦-⁩]/);
  }),
);

test(
  'End wargame opens a one-step popover; Keep playing and Esc close it',
  withDom(async (doc) => {
    const { strip, log } = makeStrip();
    strip.update(stripModel(sessionGraph()));
    const end = byClass(strip.element, 'ic-wgstrip__end');
    const pop = byClass(strip.element, 'ic-wgstrip__pop');
    assert.ok(isHidden(pop));
    assert.equal(end.attrs['aria-haspopup'], 'dialog');
    end.fire('click');
    assert.ok(!isHidden(pop));
    assert.equal(end.attrs['aria-expanded'], 'true');
    assert.equal(pop.attrs.role, 'dialog');
    assert.match(textOf(pop), /^End the wargame\? Scenario units/);
    assert.match(textOf(pop), /Keep playing End wargame$/);
    // The safe choice holds focus.
    assert.equal(doc.activeElement, byClass(pop, 'ic-wgstrip__keep'));
    byClass(pop, 'ic-wgstrip__keep').fire('click');
    assert.ok(isHidden(pop));
    assert.equal(doc.activeElement, end, 'focus returns to End wargame');
    assert.equal(log.calls, 0, 'Keep playing never ends');
    end.fire('click');
    assert.equal(strip.escape(), true);
    assert.ok(isHidden(pop));
    assert.equal(strip.escape(), false, 'nothing left to close');
    // Esc inside the popover closes it without reaching the shell.
    end.fire('click');
    let stopped = false;
    pop.fire('keydown', {
      key: 'Escape',
      preventDefault() {},
      stopPropagation() {
        stopped = true;
      },
    });
    assert.ok(isHidden(pop));
    assert.ok(stopped);
  }),
);

test(
  'End calls the API once, then reports the AAR to the shell',
  withDom(async () => {
    const { strip, log } = makeStrip();
    strip.update(stripModel(sessionGraph()));
    byClass(strip.element, 'ic-wgstrip__end').fire('click');
    const confirm = byClass(strip.element, 'ic-wgstrip__confirm');
    confirm.fire('click');
    confirm.fire('click'); // a double-click never ends twice
    assert.equal(strip.ending, true);
    assert.equal(confirm.attrs['aria-disabled'], 'true');
    await flush();
    assert.equal(log.calls, 1);
    assert.deepEqual(log.ended, [
      {
        session_id: 'WG-1a2b3c',
        aar_id: 'aar-WG-1a2b3c',
        ended_at_ms: STARTED + 60_000,
      },
    ]);
    assert.equal(strip.isOpen(), false);
    assert.equal(strip.ending, false);
    assert.deepEqual(log.announced[0], [STRIP_COPY.ending, 'polite']);
  }),
);

test(
  "a refused End says Couldn't end the wargame: {error}. with Retry",
  withDom(async (doc) => {
    let answer = 'refuse';
    let calls = 0;
    const { strip, log } = makeStrip({
      endWargame: async () => {
        calls += 1;
        if (answer === 'refuse') {
          throw new HttpError('no wargame is running', {
            status: 409,
            body: { error: 'wargame_inactive' },
          });
        }
        return { ok: true, aar_id: 'aar-WG-1a2b3c' };
      },
    });
    strip.update(stripModel(sessionGraph()));
    byClass(strip.element, 'ic-wgstrip__end').fire('click');
    const pop = byClass(strip.element, 'ic-wgstrip__pop');
    const confirm = byClass(pop, 'ic-wgstrip__confirm');
    confirm.fire('click');
    await flush();
    assert.equal(calls, 1);
    assert.ok(!isHidden(pop), 'the popover stays with the refusal');
    assert.equal(
      textOf(byClass(pop, 'ic-wgstrip__status')),
      "Couldn't end the wargame: no wargame is running.",
    );
    assert.equal(textOf(confirm), 'Retry');
    assert.equal(doc.activeElement, confirm);
    assert.deepEqual(log.announced.at(-1), [
      "Couldn't end the wargame: no wargame is running.",
      'assertive',
    ]);
    assert.deepEqual(log.ended, []);
    answer = 'ok';
    confirm.fire('click');
    await flush();
    assert.equal(calls, 2);
    assert.equal(log.ended.length, 1);
    assert.ok(isHidden(pop));
  }),
);

test(
  'an {ok:false} answer is a refusal too, worded from its code',
  withDom(async () => {
    const { strip, log } = makeStrip({
      endWargame: async () => ({ ok: false, error: 'wargame_inactive' }),
    });
    strip.update(stripModel(sessionGraph()));
    byClass(strip.element, 'ic-wgstrip__end').fire('click');
    byClass(strip.element, 'ic-wgstrip__confirm').fire('click');
    await flush();
    assert.deepEqual(log.ended, []);
    assert.equal(
      textOf(byClass(strip.element, 'ic-wgstrip__status')),
      "Couldn't end the wargame: no wargame is running.",
    );
  }),
);

test(
  'a session that ends elsewhere takes the strip and its popover away',
  withDom(() => {
    const { strip } = makeStrip();
    strip.update(stripModel(sessionGraph()));
    byClass(strip.element, 'ic-wgstrip__end').fire('click');
    assert.equal(strip.isOpen(), true);
    strip.update(null);
    assert.equal(strip.isOpen(), false);
    assert.ok(isHidden(strip.element));
    assert.ok(isHidden(byClass(strip.element, 'ic-wgstrip__pop')));
  }),
);
