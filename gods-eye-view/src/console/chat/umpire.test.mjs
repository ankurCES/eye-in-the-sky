import test from 'node:test';
import assert from 'node:assert/strict';

import {
  UMPIRE_COPY,
  UMPIRE_FOLD_MS,
  bdaRowText,
  createUmpireLog,
  groupUmpireRows,
  isUmpireView,
  lossBanners,
  rowText,
  umpireEvents,
  umpireGroupNode,
  umpireKey,
  umpireRowNode,
  umpireRowText,
} from './umpire.js';

// ---- stub DOM ------------------------------------------------------------------------

function stubDoc() {
  return { createElement: (tag) => makeEl(tag) };
}

function makeEl(tag) {
  return {
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    append(...kids) {
      this.children.push(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
    getAttribute(k) {
      return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null;
    },
    addEventListener(t, f) {
      (this.listeners[t] ||= []).push(f);
    },
    fire(t, e = {}) {
      for (const f of this.listeners[t] || []) f({ type: t, ...e });
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

function textOf(node) {
  if (node == null) return '';
  if (typeof node === 'string') return node;
  return (node.children || []).map(textOf).join('');
}

const cls = (name) => (el) =>
  String(el?.className || '')
    .split(/\s+/)
    .includes(name);
const BIDI = /[‪-‮⁦-⁩]/;
const XSS = '<img src=x onerror=alert(1)>';
const T0 = Date.UTC(2026, 8, 28, 14, 2, 51);

// ---- fixtures --------------------------------------------------------------------------

function redShot(extra = {}) {
  return {
    id: 'eng:WG-1-E1',
    type: 'engagement',
    label: 'Simulated shot on Drone1',
    status: 'ok',
    attrs: {
      kind: 'red_shot',
      phase: 'adjudicated',
      attacker: 'frc:red-sam-2',
      attacker_label: 'Red SAM 2',
      target: 'veh:Drone1',
      target_label: 'Drone1',
      outcome: 'missed',
      outcome_hidden: false,
      consequence: 'none',
      p_notional: 0.18,
      adjudicated_at_ms: T0,
      simulated: true,
      ...extra,
    },
  };
}

function blueStrike(extra = {}) {
  return {
    id: 'eng:WG-1-E2',
    type: 'engagement',
    label: 'Simulated strike on Air-defence guns',
    attrs: {
      kind: 'blue_strike',
      phase: 'adjudicated',
      attacker: 'frc:blue-artillery-1',
      attacker_label: 'Blue artillery 1',
      target: 'trk:TRK-9',
      target_label: 'Air-defence guns',
      outcome: null,
      outcome_hidden: true,
      consequence: 'red_effect',
      p_notional: { effect: 0.62 },
      bda: { state: 'none', looks: 0, last_look_ms: null },
      adjudicated_at_ms: T0 + 20_000,
      simulated: true,
      ...extra,
    },
  };
}

function graph(nodes, { truth = false, session = 'WG-1' } = {}) {
  return {
    nodes,
    meta: { wargame: { active: true, session_id: session, truth_view: truth } },
  };
}

// ---- text ----------------------------------------------------------------------------

test('row text: designators and generic labels; Blue view masks the red attacker and the chance', () => {
  const shot = redShot();
  assert.equal(
    umpireRowText(shot, { umpire: true }),
    'Red SAM 2 engaged Drone1. Missed (≈ 0.18).',
  );
  assert.equal(
    umpireRowText(shot, { umpire: false }),
    'Red air defence (not identified) engaged Drone1. Missed.',
  );
  // The graph already masks in Blue view; the row masks whatever it says.
  const masked = redShot({ attacker: null, attacker_label: null });
  assert.equal(
    umpireRowText(masked, { umpire: true }),
    'Red air defence (not identified) engaged Drone1. Missed (≈ 0.18).',
  );
  assert.equal(
    umpireRowText(blueStrike(), { umpire: false }),
    'Blue artillery 1 engaged Air-defence guns. Outcome hidden in blue view.',
  );
  assert.equal(
    umpireRowText(blueStrike({ outcome: 'damaged', outcome_hidden: false }), {
      umpire: true,
    }),
    'Blue artillery 1 engaged Air-defence guns. Damaged (≈ 0.62).',
  );
  assert.equal(
    umpireRowText(redShot({ outcome: 'zapped' }), { umpire: true }),
    'Red SAM 2 engaged Drone1. Outcome not recognised (≈ 0.18).',
  );
  for (const [state, looks, words] of [
    [
      'no_change',
      1,
      'Battle damage on Air-defence guns: no change seen (1 look).',
    ],
    ['damaged', 2, 'Battle damage on Air-defence guns: damage seen (2 looks).'],
    [
      'destroyed_probable',
      1,
      'Battle damage on Air-defence guns: probably destroyed (1 look).',
    ],
    [
      'destroyed_confirmed',
      2,
      'Battle damage on Air-defence guns: destroyed, confirmed by 2 looks.',
    ],
  ])
    assert.equal(bdaRowText(blueStrike({ bda: { state, looks } })), words);
  assert.equal(bdaRowText(blueStrike()), null, 'none is not a row');
  for (const text of Object.values(UMPIRE_COPY).filter(
    (v) => typeof v === 'string',
  )) {
    assert.ok(!text.includes('·'));
    assert.ok(!/SIMULATED/.test(text));
  }
});

test('the dedupe key is eng:{id}:{phase}:{bda.state}', () => {
  assert.equal(umpireKey(redShot()), 'eng:WG-1-E1:adjudicated:none');
  assert.equal(
    umpireKey(blueStrike({ bda: { state: 'damaged', looks: 1 } })),
    'eng:WG-1-E2:adjudicated:damaged',
  );
  assert.equal(isUmpireView(graph([], { truth: true })), true);
  assert.equal(isUmpireView(graph([])), false);
  assert.equal(isUmpireView(null), false);
});

// ---- the log ---------------------------------------------------------------------------

test('the log: a baseline first, then append-only rows deduplicated by key', () => {
  const log = createUmpireLog();
  // The first graph is history: nothing is replayed into the transcript.
  assert.deepEqual(log.observe(graph([redShot()]), { now: T0 }), []);
  assert.equal(log.primed, true);
  // Nothing new: nothing appended.
  assert.deepEqual(log.observe(graph([redShot()]), { now: T0 + 1 }), []);
  const strike = blueStrike();
  let added = log.observe(graph([redShot(), strike]), {
    now: T0 + 30_000,
    anchor: 4,
    pendingId: null,
  });
  assert.equal(added.length, 1);
  assert.equal(added[0].key, 'eng:WG-1-E2:adjudicated:none');
  assert.equal(added[0].anchor, 4);
  assert.equal(added[0].at, T0 + 20_000);
  // The view flipping (same keys) or the outcome un-hiding doesn't repeat it.
  added = log.observe(
    graph(
      [redShot(), blueStrike({ outcome: 'damaged', outcome_hidden: false })],
      {
        truth: true,
      },
    ),
  );
  assert.deepEqual(added, []);
  // Battle damage seen: a new row, keyed by the new bda state.
  added = log.observe(
    graph([
      redShot(),
      blueStrike({
        bda: { state: 'damaged', looks: 1, last_look_ms: T0 + 90_000 },
      }),
    ]),
    { now: T0 + 95_000, anchor: 6, pendingId: 'a9' },
  );
  assert.deepEqual(
    added.map((r) => [r.key, r.kind, r.at, r.pendingId]),
    [['eng:WG-1-E2:adjudicated:damaged', 'bda', T0 + 90_000, 'a9']],
  );
  assert.equal(log.rows.length, 2, 'rows are only ever appended');
  // An engagement that arrives already assessed gets both rows, in order.
  added = log.observe(
    graph(
      [
        redShot(),
        blueStrike({ bda: { state: 'damaged', looks: 1 } }),
        blueStrike({
          bda: { state: 'no_change', looks: 1, last_look_ms: T0 + 120_000 },
        }),
      ].map((n, i) => (i === 2 ? { ...n, id: 'eng:WG-1-E3' } : n)),
    ),
    { now: T0 + 121_000 },
  );
  assert.deepEqual(
    added.map((r) => r.kind),
    ['adjudicated', 'bda'],
  );
  assert.deepEqual(log.observe(null), []);
});

test('own losses: a red shot row tells it; a lost drone with no shot gets its own row', () => {
  const lost = {
    id: 'veh:Drone1',
    type: 'vehicle',
    label: 'Drone1',
    attrs: { wargame_state: 'lost', wargame_lost_at_ms: T0 + 5000 },
  };
  const shot = redShot({ outcome: 'destroyed', consequence: 'own_loss' });
  let events = umpireEvents(graph([shot, lost], { truth: true }));
  assert.deepEqual(
    events.map((e) => [e.kind, e.tone]),
    [['adjudicated', 'critical']],
  );
  events = umpireEvents(graph([lost], { truth: true }));
  assert.deepEqual(
    events.map((e) => [e.kind, e.tone, e.text, e.masked]),
    [
      [
        'loss',
        'critical',
        'Drone1 was destroyed by Red air defence (not identified). Simulated loss.',
        'Drone1 was destroyed by Red air defence (not identified). Simulated loss.',
      ],
    ],
  );
  const damage = redShot({ outcome: 'damaged', consequence: 'own_damage' });
  assert.equal(umpireEvents(graph([damage]))[0].tone, 'warn');
});

// ---- folding ---------------------------------------------------------------------------

test('folding: rows since a pending request fold; others fold within 10 s', () => {
  assert.equal(UMPIRE_FOLD_MS, 10_000);
  const row = (key, at, pendingId = null) => ({
    key,
    at,
    pendingId,
    tone: null,
  });
  const rows = [
    row('a', T0),
    row('b', T0 + 9_000),
    row('c', T0 + 18_000), // within 10 s of b: same group
    row('d', T0 + 40_000),
    row('e', T0 + 41_000, 'ap1'),
    row('f', T0 + 90_000, 'ap1'),
  ];
  let groups = groupUmpireRows(rows, { isPending: (id) => id === 'ap1' });
  assert.deepEqual(
    groups.map((g) => [g.kind, g.rows.map((r) => r.key).join('')]),
    [
      ['fold', 'abc'],
      ['row', 'd'],
      ['since', 'ef'],
    ],
  );
  // The request was decided: the rows fall back to the 10 s rule.
  groups = groupUmpireRows(rows, { isPending: () => false });
  assert.deepEqual(
    groups.map((g) => [g.kind, g.rows.map((r) => r.key).join('')]),
    [
      ['fold', 'abc'],
      ['fold', 'de'],
      ['row', 'f'],
    ],
  );
  assert.deepEqual(groupUmpireRows(null), []);
});

// ---- DOM -----------------------------------------------------------------------------

test('a row: Sand marker, "Umpire  {Z}  Simulated", the text in the current view', () => {
  globalThis.document = stubDoc();
  const [event] = umpireEvents(graph([redShot()], { truth: true }));
  const el = umpireRowNode(event, { umpire: true });
  assert.ok(find(el, cls('ic-umpire__mark')));
  const meta = find(el, cls('ic-umpire__meta'));
  assert.equal(textOf(meta), 'Umpire, 14:02:51Z, Simulated');
  assert.equal(
    textOf(find(el, cls('ic-umpire__text'))),
    'Red SAM 2 engaged Drone1. Missed (≈ 0.18).',
  );
  // Switching to Blue view re-masks what was seen in Umpire view.
  assert.equal(
    textOf(
      find(umpireRowNode(event, { umpire: false }), cls('ic-umpire__text')),
    ),
    'Red air defence (not identified) engaged Drone1. Missed.',
  );
  assert.equal(rowText(event, { umpire: false }), event.masked);
});

test('a folded group shows its count and Show; a loss inside marks it critical', () => {
  globalThis.document = stubDoc();
  const rows = umpireEvents(
    graph(
      [
        redShot(),
        redShot({ outcome: 'destroyed', consequence: 'own_loss' }),
      ].map((n, i) => ({ ...n, id: `eng:E${i}` })),
    ),
  );
  const toggled = [];
  const el = umpireGroupNode(
    { kind: 'since', key: 'since:x', rows },
    { onToggle: (key) => toggled.push(key) },
  );
  assert.equal(el.attrs['data-tone'], 'critical');
  assert.ok(textOf(el).includes('2 umpire events since this request.'));
  const button = find(el, (n) => n.tag === 'button');
  assert.equal(textOf(button), 'Show');
  assert.equal(button.attrs['aria-expanded'], 'false');
  button.fire('click');
  assert.deepEqual(toggled, ['since:x']);
  assert.equal(find(el, cls('ic-umpire__rows')), null, 'folded: no rows');
  const open = umpireGroupNode(
    { kind: 'fold', key: 'fold:y', rows },
    { open: true },
  );
  assert.ok(
    textOf(open).startsWith(
      'Umpire, 14:02:51Z, Simulated2 umpire events from 14:02:51Z.',
    ),
  );
  assert.equal(find(open, cls('ic-umpire__rows')).children.length, 2);
});

test('XSS and bidi in labels stay text in rows and banners', () => {
  globalThis.document = stubDoc();
  const evil = `${XSS}‮evil‬`;
  const [event] = umpireEvents(
    graph([redShot({ attacker_label: evil, target_label: evil })], {
      truth: true,
    }),
  );
  const el = umpireRowNode(event, { umpire: true });
  assert.equal(
    find(el, (n) => n.tag === 'img'),
    null,
  );
  assert.ok(textOf(el).includes(XSS));
  assert.ok(!BIDI.test(textOf(el)));
  const [banner] = lossBanners(
    graph(
      [
        {
          id: 'veh:Drone1',
          type: 'vehicle',
          label: evil,
          attrs: { wargame_state: 'lost' },
        },
      ],
      { truth: true },
    ),
  );
  assert.ok(!BIDI.test(banner.text));
  assert.ok(banner.text.includes(XSS));
});

// ---- the loss banner -------------------------------------------------------------------

test('loss banner: "Simulated loss: {vehicle} destroyed by {attacker} at {Z}."', () => {
  const lost = {
    id: 'veh:Drone1',
    type: 'vehicle',
    label: 'Drone1',
    attrs: {
      wargame_state: 'lost',
      wargame_lost_by: 'frc:red-sam-2',
      wargame_lost_at_ms: T0 + 5000,
    },
  };
  const sam = {
    id: 'frc:red-sam-2',
    type: 'force',
    label: 'Red SAM 2',
    attrs: {},
  };
  assert.deepEqual(
    lossBanners(graph([lost, sam])).map((b) => b.text),
    [
      'Simulated loss: Drone1 destroyed by Red air defence (not identified) at 14:02:56Z.',
    ],
  );
  assert.deepEqual(
    lossBanners(graph([lost, sam], { truth: true })).map((b) => b.text),
    ['Simulated loss: Drone1 destroyed by Red SAM 2 at 14:02:56Z.'],
  );
  const shot = redShot({
    outcome: 'destroyed',
    consequence: 'own_loss',
    attacker_label: 'Red SAM 3',
  });
  assert.deepEqual(
    lossBanners(graph([lost, shot], { truth: true })).map((b) => b.text),
    ['Simulated loss: Drone1 destroyed by Red SAM 3 at 14:02:56Z.'],
  );
  assert.deepEqual(lossBanners(graph([{ ...lost, attrs: {} }])), []);
  assert.deepEqual(lossBanners(null), []);
});
