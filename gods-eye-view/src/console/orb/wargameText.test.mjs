import test from 'node:test';
import assert from 'node:assert/strict';

import * as wargameText from './wargameText.js';
import {
  FORCE_FIXED_LINE,
  NOT_SCENARIO_WORD,
  OUTCOME_HIDDEN,
  RED_NOT_IDENTIFIED,
  SCENARIO_TOOLTIP,
  engagementLabel,
  engagementSubtitle,
  forceStateWord,
  forceSubtitle,
  vectorLabel,
  vectorSubtitle,
  wargameBandCaption,
  wargameDisplayStatus,
  wargameNodeLabel,
  wargameOptionText,
  wargameStatusWord,
} from './wargameText.js';
import {
  marginTitle,
  nodeLabel,
  nodeSubtitle,
  optionText,
  registerOf,
  splitSegments,
  statusWord,
  typeLabel,
} from './text.js';

const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';
const BIDI_CHARS = /[‪-‮⁦-⁩]/;

const force = (side, state, extra = {}) => ({
  id: `frc:${side}-sam-1`,
  type: 'force',
  label: side === 'blue' ? 'Blue artillery 1' : 'Red SAM 1',
  status: 'ok',
  attrs: {
    side,
    state,
    provenance: 'scenario',
    kind_label: 'Short-range air defence',
    ...extra,
  },
});

test('force words: designator label, "{side}  {kind}  {state}  Scenario", state as status', () => {
  const red = force('red', 'damaged');
  assert.equal(nodeLabel(red), 'Red SAM 1', 'the designator');
  assert.deepEqual(splitSegments(nodeSubtitle(red)), [
    'Red',
    'Short-range air defence',
    'Damaged',
    'Scenario',
  ]);
  assert.equal(statusWord(red), 'Damaged');
  assert.equal(registerOf(red), 'Scenario');
  assert.equal(typeLabel('force', { wargame: true }), 'Force');
  for (const [state, word] of [
    ['active', 'Active'],
    ['suppressed', 'Suppressed'],
    ['destroyed', 'Destroyed'],
    ['routed', 'State not recognised'],
  ])
    assert.equal(forceStateWord(force('blue', state)), word);
  assert.equal(forceStateWord(force('green', 'active')), 'Side not set');
  assert.equal(
    splitSegments(forceSubtitle(force(undefined, 'active')))[0],
    'Side not set',
  );
  const bare = {
    ...force('red', 'active'),
    attrs: { side: 'red', state: 'active' },
  };
  assert.equal(statusWord(bare), NOT_SCENARIO_WORD);
  assert.ok(!forceSubtitle(bare).includes('Scenario'), 'no Scenario register');
  assert.equal(
    optionText(red),
    'Red SAM 1, force, red, Short-range air defence, damaged, scenario, simulated',
  );
  assert.equal(
    FORCE_FIXED_LINE,
    'Simulated scenario unit. Placed by the wargame, not observed.',
  );
  assert.equal(SCENARIO_TOOLTIP, 'Set by the wargame, not seen by a sensor.');
});

test('engagement words: "Simulated {strike|shot|ground fire} on {target}", phase and outcome', () => {
  const eng = (attrs) => ({
    id: 'eng:1',
    type: 'engagement',
    label: 'eng 1',
    attrs,
  });
  assert.equal(
    engagementLabel(
      eng({ kind: 'blue_strike', target_label: 'Towed anti-aircraft gun' }),
    ),
    'Simulated strike on Towed anti-aircraft gun',
  );
  assert.equal(
    engagementLabel(eng({ kind: 'red_shot', target_label: 'Drone1' })),
    'Simulated shot on Drone1',
  );
  assert.equal(
    engagementLabel(
      eng({ kind: 'red_ground', target_label: 'Blue artillery 1' }),
    ),
    'Simulated ground fire on Blue artillery 1',
  );
  assert.equal(
    engagementLabel(eng({ kind: 'odd', target_label: 'x' })),
    'eng 1',
  );
  assert.deepEqual(
    splitSegments(engagementSubtitle(eng({ phase: 'proposed' }))),
    ['Waiting for you', 'Simulated'],
  );
  assert.deepEqual(
    splitSegments(
      engagementSubtitle(eng({ phase: 'adjudicated', outcome: 'destroyed' })),
    ),
    ['Adjudicated', 'Destroyed (simulated)', 'Simulated'],
  );
  assert.deepEqual(
    splitSegments(
      engagementSubtitle(
        eng({
          phase: 'adjudicated',
          outcome: 'destroyed',
          outcome_hidden: true,
        }),
      ),
    ),
    ['Adjudicated', OUTCOME_HIDDEN, 'Simulated'],
    'fog: a hidden outcome is never read',
  );
  assert.equal(
    wargameStatusWord(eng({ phase: 'launched' })),
    'Phase not recognised',
  );
  assert.equal(
    optionText(
      eng({
        kind: 'red_shot',
        target_label: 'Drone1',
        phase: 'adjudicated',
        outcome: 'missed',
        consequence: 'none',
      }),
    ),
    'Simulated shot on Drone1, engagement, adjudicated, missed (simulated), no effect on own side, scenario, simulated',
  );
  assert.equal(RED_NOT_IDENTIFIED, 'Red air defence (not identified)');
});

test('vector words: kind, length, "Simulated"', () => {
  const vec = {
    id: 'vec:cor-1',
    type: 'vector',
    attrs: { kind: 'corridor', length_m: 4210 },
  };
  assert.equal(vectorLabel(vec), 'Planned corridor');
  assert.deepEqual(
    splitSegments(vectorSubtitle(vec)),
    ['4.2 km', 'Simulated'],
    'the kind is the label already',
  );
  const named = {
    type: 'vector',
    label: 'Red SAM 1 axis',
    attrs: { kind: 'axis', length_m: 2100 },
  };
  assert.equal(wargameNodeLabel(named), 'Red SAM 1 axis');
  assert.deepEqual(splitSegments(vectorSubtitle(named)), [
    'Red axis',
    '2.1 km',
    'Simulated',
  ]);
  assert.equal(
    wargameOptionText(vec),
    'Planned corridor, vector, planned corridor, 4.2 km, scenario, simulated',
  );
  assert.equal(
    wargameOptionText(named),
    'Red SAM 1 axis, vector, red axis, 2.1 km, scenario, simulated',
  );
});

test('margin titles keep what an engagement engaged (the gutter is narrow)', () => {
  const label = 'Simulated strike on Towed anti-aircraft gun';
  assert.equal(marginTitle(label), label, 'whole when it fits');
  assert.equal(
    marginTitle(label, (t) => t.length <= 20),
    'Strike on Towed anti-aircraft gun',
  );
  assert.equal(
    marginTitle('Simulated ground fire on Blue artillery 1', () => false),
    'Ground fire on Blue artillery 1',
  );
  assert.equal(
    marginTitle('Simulated engagement', () => false),
    'Simulated engagement',
  );
});

test('band captions: "Simulated red forces 3", none when empty, side-not-set and vectors counted', () => {
  assert.equal(wargameBandCaption('force_red', { count: 0 }), null);
  assert.equal(
    wargameBandCaption('force_red', { count: 3 }),
    'Simulated red forces 3',
  );
  assert.equal(
    wargameBandCaption('force_red', { count: 3, sideNotSet: 1 }),
    'Simulated red forces 2 (1 side not set)',
  );
  assert.equal(
    wargameBandCaption('force_red', { count: 2, sideNotSet: 2 }),
    'Simulated forces, side not set 2',
  );
  assert.equal(
    wargameBandCaption('force_blue', { count: 2, recent: 1 }),
    'Simulated blue forces 2 (+1)',
  );
  assert.equal(
    wargameBandCaption('engagement', { count: 6, vectors: 2 }),
    'Simulated engagements 4, vectors 2',
  );
  assert.equal(
    wargameBandCaption('engagement', { count: 2, vectors: 2 }),
    'Simulated vectors 2',
  );
  assert.equal(wargameBandCaption('track', { count: 2 }), null);
});

test('List view status keys are never green for what is not fine', () => {
  const eng = (phase, consequence) => ({
    type: 'engagement',
    attrs: { phase, consequence },
  });
  assert.equal(wargameDisplayStatus(eng('proposed')), 'sand');
  assert.equal(
    wargameDisplayStatus(eng('adjudicated', 'own_loss')),
    'critical',
  );
  assert.equal(wargameDisplayStatus(eng('adjudicated', 'own_damage')), 'warn');
  assert.equal(wargameDisplayStatus(eng('expired')), 'stale');
  assert.equal(wargameDisplayStatus(eng('odd')), 'unknown');
  assert.equal(wargameDisplayStatus(force('blue', 'damaged')), 'warn');
  assert.equal(wargameDisplayStatus(force('blue', 'destroyed')), 'stale');
  assert.equal(
    wargameDisplayStatus({ ...force('red', 'active'), status: 'critical' }),
    'critical',
  );
  assert.equal(wargameDisplayStatus(force('green', 'active')), 'unknown');
  assert.equal(wargameDisplayStatus({ type: 'force', attrs: {} }), 'unknown');
});

test('designators and server labels render as text: markup literal, no bidi control (§3.11)', () => {
  const hostile = {
    ...force('red', 'active', { kind_label: `${BIDI}${XSS}` }),
    label: `${XSS}${BIDI}`,
  };
  const eng = {
    type: 'engagement',
    attrs: {
      kind: 'blue_strike',
      target_label: `${BIDI}${XSS}`,
      phase: 'proposed',
    },
  };
  const vec = {
    type: 'vector',
    label: `${XSS}${BIDI}`,
    attrs: { kind: 'axis' },
  };
  for (const node of [hostile, eng, vec]) {
    for (const text of [nodeLabel(node), optionText(node)]) {
      assert.ok(text.includes('<img src=x onerror=alert(1)>'), text);
      assert.ok(!BIDI_CHARS.test(text), `${text}: bidi-safe`);
    }
    assert.ok(!BIDI_CHARS.test(nodeSubtitle(node)));
  }
  assert.ok(nodeSubtitle(hostile).includes(XSS), 'the kind label, as text');
});

test('the copy is sentence case, never "·" or a stamp, and names no real system or weaponeering', () => {
  const strings = Object.values(wargameText)
    .flatMap((v) =>
      typeof v === 'string'
        ? [v]
        : v && typeof v === 'object'
          ? Object.values(v)
          : [],
    )
    .filter((v) => typeof v === 'string');
  assert.ok(strings.length > 40);
  const REAL = [
    /S-300/,
    /\bSA-\d/,
    /\bTor\b/,
    /Pantsir/,
    /ZSU/,
    /\bZU-/,
    /warhead/i,
    /munition/i,
    /fuze|fuzing/i,
    /blast radius/i,
    /aimpoint/i,
    /\bCEP\b/,
    /\d+\s?(kg|mm)\b/,
  ];
  for (const text of strings) {
    assert.ok(!text.includes('·'), `${text}: no middle dot`);
    assert.ok(!text.includes('SIMULATED'), `${text}: no capitalised stamp`);
    assert.ok(!/^[a-z]/.test(text) || text === text.toLowerCase(), text);
    for (const re of REAL) assert.ok(!re.test(text), `${text} matches ${re}`);
  }
});
