import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BDA_WORD,
  HIDDEN_ATTACKER_TEXT,
  OUTCOMES_NOTIONAL_TEXT,
  VIEW_SENTENCE,
  attackerText,
  bdaAgrees,
  bdaFacts,
  engagementTitle,
  forceStateWord,
  isTruthView,
  isWargameType,
  pText,
  pendingEngagements,
  recentEngagements,
  recentOutcomeText,
  sideSegments,
  sideWord,
  strikePrefill,
  wargameActive,
  wargameAssumedLines,
  wargameGlyphSvg,
  wargameOf,
  wargameSessionPrompts,
  wargameStatusWord,
  wargameStripFacts,
  wargameTone,
} from './railWargame.js';

const GREEN = /#5DD39B/i;
const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';
const BIDI_RE = /[‪-‮⁦-⁩]/;

function force(id, side, state, extra = {}) {
  return {
    id,
    type: 'force',
    label: id,
    status: 'ok',
    attrs: { side, state, provenance: 'scenario', ...extra },
  };
}

function engagement(id, phase, extra = {}) {
  return {
    id,
    type: 'engagement',
    label: id,
    status: 'ok',
    attrs: {
      kind: 'blue_strike',
      phase,
      target_label: 'Air-defence guns',
      attacker_label: 'Blue artillery 1',
      simulated: true,
      ...extra,
    },
  };
}

function session(nodes = [], wargame = {}) {
  return {
    nodes,
    edges: [],
    meta: {
      wargame: {
        active: true,
        session_id: 'WG-3fa9c1',
        truth_view: true,
        pending: [],
        counts: {
          blue: {
            units: 3,
            active: 3,
            suppressed: 0,
            damaged: 0,
            destroyed: 0,
          },
          red: { units: 2, active: 1, destroyed: 1, seen: 1 },
        },
        caveats: [],
        ...wargame,
      },
    },
  };
}

test('the wargame types and words (§3.9): unknown values read "not recognised"', () => {
  assert.ok(isWargameType('force'));
  assert.ok(isWargameType('engagement'));
  assert.ok(isWargameType('vector'));
  assert.ok(!isWargameType('site'));
  assert.ok(!isWargameType('constructor'));
  assert.equal(sideWord('red'), 'Red');
  assert.equal(sideWord('green'), 'Side not set');
  assert.equal(forceStateWord('damaged'), 'Damaged');
  assert.equal(forceStateWord('toString'), 'State not recognised');
  assert.equal(
    wargameStatusWord(engagement('eng:1', 'proposed')),
    'Waiting for you',
  );
  assert.equal(
    wargameStatusWord(engagement('eng:1', 'rolled')),
    'Phase not recognised',
  );
});

test('tones: a force is never green; pending engagements are Sand', () => {
  for (const side of ['red', 'blue'])
    for (const state of ['active', 'suppressed', 'damaged', 'destroyed'])
      for (const status of ['ok', 'warn', 'critical', 'stale', 'unknown'])
        assert.notEqual(
          wargameTone({ ...force('frc:x', side, state), status }),
          'ok',
        );
  assert.equal(wargameTone(force('frc:b', 'blue', 'active')), 'low');
  assert.equal(wargameTone(force('frc:b', 'blue', 'destroyed')), 'stale');
  assert.equal(wargameTone(force('frc:x', null, 'active')), 'unknown');
  assert.equal(wargameTone(engagement('eng:1', 'authorized')), 'sand');
  assert.equal(
    wargameTone(
      engagement('eng:1', 'adjudicated', { consequence: 'own_loss' }),
    ),
    'critical',
  );
  assert.equal(wargameTone(engagement('eng:1', 'expired')), 'stale');
  assert.equal(wargameTone({ type: 'vector', status: 'ok' }), 'neutral');
});

test('frames (Appendix A, §5.3.4): shape by side, never green, constants only', () => {
  const blue = wargameGlyphSvg('force', { side: 'blue', state: 'active' });
  assert.match(blue, /M2\.5 6\.5H21\.5V17\.5H2\.5Z/);
  assert.match(blue, /#E6ECEF/i, 'Film stroke');
  const red = wargameGlyphSvg('force', {
    side: 'red',
    state: 'active',
    status: 'critical',
  });
  assert.match(red, /M12 2L22 12L12 22L2 12Z/);
  assert.match(red, /<circle/, 'a steady halo when critical');
  const odd = wargameGlyphSvg('force', { side: 'purple', state: 'active' });
  assert.match(odd, /A4 4 0 0 1 16 8/, 'the quatrefoil');
  assert.match(odd, /#BBA7E0/i);
  assert.match(odd, /stroke-dasharray/);
  const suppressed = wargameGlyphSvg('force', {
    side: 'blue',
    state: 'suppressed',
  });
  assert.match(suppressed, /stroke-dasharray/);
  assert.match(suppressed, /#F2B544/i);
  const damaged = wargameGlyphSvg('force', { side: 'blue', state: 'damaged' });
  assert.match(damaged, /M7 12H10\.5M13\.5 12H17/, 'the bar broken in two');
  const dead = wargameGlyphSvg('force', { side: 'red', state: 'destroyed' });
  assert.match(dead, /M3 21\.5L21 2\.5/, 'the slash');
  assert.match(dead, /#8A98A2/i);
  assert.match(dead, /opacity="0\.5"/);
  for (const svg of [blue, red, odd, suppressed, damaged, dead])
    assert.doesNotMatch(svg, GREEN);
  const hostile = wargameGlyphSvg('force', {
    side: '"><script>',
    state: XSS,
    status: XSS,
    size: '9999',
  });
  assert.doesNotMatch(hostile, /script|onerror|<img/);
  assert.match(hostile, /width="64"/, 'size clamped');
  assert.equal(wargameGlyphSvg('site', {}), '', 'frames only on wargame types');
});

test('engagement and vector glyphs follow their phase and status', () => {
  const pending = wargameGlyphSvg('engagement', { phase: 'proposed' });
  assert.match(pending, /#CDBC8C/i);
  assert.match(pending, /stroke-dasharray/);
  const loss = wargameGlyphSvg('engagement', {
    phase: 'adjudicated',
    consequence: 'own_loss',
  });
  assert.match(loss, /fill="#FF7B7B"/i);
  assert.match(
    wargameGlyphSvg('engagement', { phase: 'denied' }),
    /opacity="0\.5"/,
  );
  assert.match(wargameGlyphSvg('engagement', { phase: 'x' }), /#BBA7E0/i);
  assert.match(wargameGlyphSvg('vector', { status: 'ok' }), /#A3B0BA/i);
  assert.doesNotMatch(wargameGlyphSvg('vector', { status: 'ok' }), GREEN);
});

test('wargameOf normalises meta.wargame; ISR graphs have none', () => {
  assert.equal(wargameOf({ meta: {} }), null);
  assert.equal(wargameActive({ meta: {} }), false);
  const ended = wargameOf({
    meta: {
      wargame: {
        active: false,
        last: { session_id: 'WG-1', aar_id: 'aar-WG-1', ended_at_ms: 5 },
      },
    },
  });
  assert.deepEqual(ended, {
    active: false,
    last: { session_id: 'WG-1', aar_id: 'aar-WG-1', ended_at_ms: 5 },
  });
  const w = wargameOf(
    session([], { pending: ['eng:1', 7, null], caveats: [`${BIDI}c`, ''] }),
  );
  assert.equal(w.active, true);
  assert.deepEqual(w.pending, ['eng:1']);
  assert.deepEqual(w.caveats, ['evilc']);
  assert.equal(isTruthView(session()), true);
  assert.equal(isTruthView(session([], { truth_view: false })), false);
  assert.equal(isTruthView(session([], { truth_view: false }), true), true);
});

test('side lines: zero segments omitted; Blue view counts red as seen', () => {
  assert.deepEqual(
    sideSegments('blue', { units: 3, active: 3, suppressed: 0, destroyed: 0 }),
    ['Blue', '3 units', '3 active'],
  );
  assert.deepEqual(sideSegments('red', { units: 1, active: 0, destroyed: 1 }), [
    'Red',
    '1 unit',
    '1 destroyed',
  ]);
  assert.deepEqual(sideSegments('red', { seen: 2 }), ['Red', '2 seen']);
  assert.deepEqual(sideSegments('blue', null), ['Blue', '0 units']);
});

test('pending comes from meta.wargame.pending; recent is the newest three adjudicated', () => {
  const nodes = [
    engagement('eng:1', 'proposed'),
    engagement('eng:2', 'adjudicated', { adjudicated_at_ms: 10 }),
    engagement('eng:3', 'adjudicated', { adjudicated_at_ms: 30 }),
    engagement('eng:4', 'adjudicated', { adjudicated_at_ms: 20 }),
    engagement('eng:5', 'adjudicated', { adjudicated_at_ms: 5 }),
    engagement('eng:6', 'authorized'),
  ];
  const g = session(nodes, { pending: ['eng:1'] });
  assert.deepEqual(
    pendingEngagements(g).map((n) => n.id),
    ['eng:1'],
  );
  assert.deepEqual(
    pendingEngagements(session(nodes)).map((n) => n.id),
    ['eng:1', 'eng:6'],
    'no pending list: every proposed or authorized',
  );
  assert.deepEqual(
    recentEngagements(g).map((n) => n.id),
    ['eng:3', 'eng:4', 'eng:2'],
  );
  assert.equal(
    engagementTitle(nodes[0]),
    'Simulated strike on Air-defence guns',
  );
  assert.equal(
    recentOutcomeText(engagement('e', 'adjudicated', { outcome_hidden: true })),
    'Outcome hidden',
  );
  assert.equal(
    recentOutcomeText(engagement('e', 'adjudicated', { outcome: 'missed' })),
    'Missed',
  );
  assert.equal(
    attackerText({ attrs: { kind: 'red_shot', attacker: null } }),
    HIDDEN_ATTACKER_TEXT,
  );
  assert.equal(pText({ effect: 0.617 }), '≈ 0.62');
  assert.equal(pText(null), '');
});

test('battle damage words (§5.3.9) and the umpire mismatch rule', () => {
  assert.equal(bdaFacts({ state: 'none', looks: 0 }).word, BDA_WORD.none);
  assert.equal(bdaFacts({ state: 'damaged', looks: 1 }).tone, 'warn');
  assert.equal(
    bdaFacts({ state: 'destroyed_confirmed', looks: 2 }).word,
    'Destroyed, confirmed by 2 looks',
  );
  assert.equal(
    bdaFacts({ state: 'burnt' }).word,
    'Battle damage not recognised',
  );
  assert.ok(bdaAgrees('destroyed', 'destroyed_probable'));
  assert.ok(bdaAgrees('missed', 'no_change'));
  assert.ok(!bdaAgrees('destroyed', 'no_change'));
  assert.ok(!bdaAgrees('damaged', 'none'));
});

test('the strike prefill: a red force with correlated contacts, in a session, generic label only', () => {
  const track = {
    id: 'trk:TRK-9',
    type: 'track',
    label: 'Air-defence guns',
    attrs: { scenario: true },
  };
  const red = force('frc:red-aaa-1', 'red', 'active', {
    correlated: ['trk:TRK-9'],
    kind_label: 'Air-defence guns',
  });
  const g = session([track, red]);
  assert.equal(
    strikePrefill(red, g),
    'Plan a simulated strike on contact [[trk:TRK-9|Air-defence guns]] with the least exposure and show me the dry run.',
  );
  assert.equal(
    strikePrefill(red, { nodes: [track, red], meta: {} }),
    null,
    'no session',
  );
  assert.equal(
    strikePrefill(
      force('frc:b', 'blue', 'active', { correlated: ['trk:TRK-9'] }),
      g,
    ),
    null,
    'never on a blue force',
  );
  assert.equal(
    strikePrefill(force('frc:r', 'red', 'active', { correlated: [] }), g),
    null,
    'only with correlated[]',
  );
  // A contact that isn't a scenario track (or is missing) is cited as "Contact".
  const real = { ...track, label: 'SA-6 battery', attrs: {} };
  assert.match(strikePrefill(red, session([real, red])), /\|Contact\]\]/);
  const hostile = { ...track, label: `${BIDI}${XSS}|]]` };
  const text = strikePrefill(red, session([hostile, red]));
  assert.doesNotMatch(text, BIDI_RE);
  assert.doesNotMatch(text, /\|\]\]\]\]/);
});

test('session prompts replace the ISR ones only in a session; the strike cites a generic label', () => {
  assert.deepEqual(wargameSessionPrompts({ nodes: [], meta: {} }), []);
  const plain = wargameSessionPrompts(session());
  assert.equal(plain.length, 4);
  assert.equal(plain[0], 'Generate a medium air-defence scenario here.');
  assert.equal(
    plain.at(-1),
    'End the wargame and show the after-action review.',
  );
  const withTrack = wargameSessionPrompts(
    session([
      {
        id: 'trk:TRK-9',
        type: 'track',
        label: 'Air-defence contact',
        attrs: { scenario: true },
      },
    ]),
  );
  assert.equal(withTrack.length, 5);
  assert.equal(
    withTrack[2],
    'Plan a simulated strike on [[trk:TRK-9|Air-defence contact]] and show me the dry run.',
  );
});

test('assumed lines and the compact strip facts appear only in a session', () => {
  assert.deepEqual(wargameAssumedLines({ meta: {} }, 'umpire'), []);
  assert.deepEqual(wargameAssumedLines(session(), 'blue'), [
    VIEW_SENTENCE.blue,
    OUTCOMES_NOTIONAL_TEXT,
  ]);
  assert.deepEqual(wargameAssumedLines(session(), null), [
    VIEW_SENTENCE.umpire,
    OUTCOMES_NOTIONAL_TEXT,
  ]);
  assert.equal(wargameStripFacts({ meta: {} }), null);
  const facts = wargameStripFacts(
    session([engagement('eng:1', 'proposed')], { pending: ['eng:1'] }),
  );
  assert.equal(facts.pending, 1);
  assert.match(facts.summary, /^Simulated wargame: Blue 3 units 3 active/);
  assert.match(facts.summary, /1 engagement waiting for you$/);
});
