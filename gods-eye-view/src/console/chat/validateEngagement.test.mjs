import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ENGAGEMENT_COPY,
  MASKED_ATTACKER,
  MOVED_STALE_M,
  REQUIRED_ENGAGEMENT_KEYS,
  assessEngagement,
  consoleProblem,
  distanceM,
  engagementDenyOnly,
  engagementPreviewProblem,
  staleLine,
  targetGraphId,
  targetLabel,
} from './validateEngagement.js';

const T0 = Date.UTC(2026, 8, 28, 14, 2, 51);

/** The §5.2.7 `engagement` preview. */
function preview(extra = {}) {
  return {
    id: 'WG-3fa9c1-E7',
    kind: 'blue_strike',
    verb_kind: 'engagement',
    attacker: {
      id: 'frc:blue-artillery-1',
      label: 'Blue artillery 1',
      wg_class: 'blue_artillery',
    },
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
    p_notional: {
      effect: 0.62,
      destroyed: 0.2,
      damaged: 0.25,
      suppressed: 0.17,
      package_survive: null,
    },
    inputs: [
      'Range 3.2 km of 20.0 km',
      'Perceived as air-defence guns, probable',
    ],
    range_m: 3200,
    seed: 4417,
    engine: 'wg-notional/1',
    caveats: ['Probabilities are notional play-balance numbers.'],
    expires_at_ms: T0 + 600_000,
    checks: [
      { text: 'Target is a simulated scenario unit', ok: true },
      {
        text: 'More than 500 m from any mapped place or theater point',
        ok: true,
      },
      { text: 'Not within 1 km of a protected place', ok: true },
      { text: 'Shooter active with ammunition', ok: true },
      { text: 'In range', ok: true },
      { text: 'Engagement still waiting for approval', ok: true },
      { text: 'Wargame session active', ok: true },
    ],
    ...extra,
  };
}

const approval = (p = preview(), extra = {}) => ({
  id: 'a1',
  tool: 'wg_execute_engagement',
  klass: 'engagement',
  at: T0,
  engagementPreview: p,
  ...extra,
});

function graph({ wargame = {}, nodes = [] } = {}) {
  return {
    nodes: [
      {
        id: 'trk:TRK-9',
        type: 'track',
        label: 'Air-defence guns',
        lat: 47.65,
        lon: -122.13,
        attrs: { scenario: true },
      },
      ...nodes,
    ],
    meta: {
      wargame: {
        active: true,
        session_id: 'WG-3fa9c1',
        truth_view: false,
        ...wargame,
      },
    },
  };
}

test('a complete preview, a held key and a live session: nothing to refuse', () => {
  const a = approval();
  assert.equal(engagementPreviewProblem(a), null);
  const verdict = assessEngagement(a, graph(), {
    requestSession: 'WG-3fa9c1',
  });
  assert.deepEqual(
    { state: verdict.state, ok: verdict.ok, line: verdict.line },
    { state: 'none', ok: true, line: null },
  );
  assert.equal(engagementDenyOnly(a, verdict, { held: true }), null);
});

test('Deny-only: a missing preview or any missing or malformed required key', () => {
  assert.deepEqual(REQUIRED_ENGAGEMENT_KEYS, [
    'checks',
    'target',
    'attacker',
    'p_notional',
  ]);
  assert.deepEqual(engagementPreviewProblem({ klass: 'engagement' }), {
    code: 'missing',
  });
  assert.deepEqual(
    engagementPreviewProblem(approval([1, 2])),
    { code: 'missing' },
    'an array is not a preview',
  );
  for (const key of REQUIRED_ENGAGEMENT_KEYS) {
    const p = preview();
    delete p[key];
    assert.deepEqual(engagementPreviewProblem(approval(p)), {
      code: 'key',
      key,
    });
  }
  const bad = {
    checks: 'all good',
    target: { track_id: 'TRK-9' }, // no label to name it by
    attacker: 'Blue artillery 1',
    p_notional: { effect: '0.62' },
  };
  for (const [key, value] of Object.entries(bad)) {
    assert.deepEqual(
      engagementPreviewProblem(approval(preview({ [key]: value }))),
      { code: 'key', key },
      key,
    );
  }
  const only = engagementDenyOnly(approval(null), null, { held: true });
  assert.equal(only.kind, 'preview');
});

test('Deny-only: no console key, or another client claimed it (§3.5)', () => {
  assert.equal(consoleProblem({ held: true }), null);
  assert.deepEqual(consoleProblem({ held: false, refused: true }), {
    code: 'claimed',
    line: "This console can't approve engagements: another client claimed them.",
  });
  for (const access of [null, undefined, {}, { held: 'yes' }, { held: false }])
    assert.equal(consoleProblem(access)?.code, 'no_key', String(access));
  const a = approval();
  const ok = assessEngagement(a, graph());
  assert.equal(engagementDenyOnly(a, ok, null).kind, 'console');
  assert.equal(
    engagementDenyOnly(a, ok, { refused: true }).line,
    ENGAGEMENT_COPY.claimed,
  );
  // The preview problem is named first; then the key.
  assert.equal(engagementDenyOnly(approval(null), ok, null).kind, 'preview');
});

test('blocked: a failing check, a non-scenario or protected target', () => {
  const failing = preview({
    checks: [
      { text: 'In range', ok: false },
      { text: 'Wargame session active', ok: true },
    ],
  });
  let v = assessEngagement(approval(failing), graph());
  assert.equal(v.state, 'blocked');
  assert.equal(
    v.line,
    "This can't be approved here: the check “In range” failed.",
  );
  assert.equal(
    engagementDenyOnly(approval(failing), v, { held: true }).kind,
    'blocked',
  );

  const odd = preview({ checks: [null, { ok: 'true', text: '' }] });
  v = assessEngagement(approval(odd), graph());
  assert.equal(v.reasons.filter((r) => r.code === 'check').length, 2);

  for (const scenario of [false, undefined, 'true']) {
    const p = preview();
    p.target = { ...p.target, scenario };
    v = assessEngagement(approval(p), graph());
    assert.equal(v.state, 'blocked', String(scenario));
    assert.equal(
      v.line,
      "This can't be approved here: the target is not a simulated scenario unit.",
    );
  }
  const p = preview();
  p.target = { ...p.target, protected: true };
  v = assessEngagement(approval(p), graph());
  assert.equal(
    v.line,
    "This can't be approved here: the target is marked protected.",
  );
});

test('blocked: the wargame ended, or a new session replaced it', () => {
  let v = assessEngagement(approval(), graph({ wargame: { active: false } }));
  assert.equal(v.state, 'blocked');
  assert.equal(v.line, 'The wargame has ended.');
  v = assessEngagement(
    approval(),
    graph({ wargame: { session_id: 'WG-new' } }),
    {
      requestSession: 'WG-3fa9c1',
    },
  );
  assert.equal(v.line, 'The wargame has ended.');
  // No wargame block at all (an older server): the console can't tell, and
  // the server still refuses an inactive session.
  const bare = graph();
  delete bare.meta.wargame;
  assert.equal(assessEngagement(approval(), bare).state, 'none');
  assert.equal(assessEngagement(approval(), null).state, 'none');
});

test('blocked in Umpire view only: the correlated force is already destroyed', () => {
  const destroyed = {
    id: 'frc:red-aaa-1',
    type: 'force',
    label: 'Red AAA 1',
    attrs: { side: 'red', state: 'destroyed', correlated: ['trk:TRK-9'] },
  };
  let v = assessEngagement(
    approval(),
    graph({ wargame: { truth_view: true }, nodes: [destroyed] }),
  );
  assert.equal(v.state, 'blocked');
  assert.equal(
    v.line,
    'Air-defence guns is already destroyed in this wargame.',
  );
  // Blue view doesn't know (and the node wouldn't be there).
  v = assessEngagement(
    approval(),
    graph({ wargame: { truth_view: false }, nodes: [destroyed] }),
  );
  assert.equal(v.state, 'none');
  const alive = {
    ...destroyed,
    attrs: { ...destroyed.attrs, state: 'damaged' },
  };
  v = assessEngagement(
    approval(),
    graph({ wargame: { truth_view: true }, nodes: [alive] }),
  );
  assert.equal(v.state, 'none');
});

test('stale: the track moved more than 250 m, or sim speed or weather changed', () => {
  assert.equal(MOVED_STALE_M, 250);
  const g = graph();
  const track = g.nodes[0];
  // ~200 m north: still current.
  track.lat = 47.65 + 200 / 111_195;
  assert.equal(assessEngagement(approval(), g).state, 'none');
  // ~300 m north: stale.
  track.lat = 47.65 + 300 / 111_195;
  let v = assessEngagement(approval(), g);
  assert.equal(v.state, 'stale');
  assert.equal(v.ok, true, 'stale is still approvable, boxed and re-armed');
  assert.equal(v.stale[0].code, 'moved');
  assert.match(v.stale[0].text, /^the target moved about 30\d m$/);
  track.lat = 47.65;
  v = assessEngagement(approval(), g, {
    approvedSince: [
      { tool: 'sim_set_time_scale' },
      { tool: 'uav_goto_gps' },
      { tool: 'sim_set_weather' },
      { tool: 'sim_set_time_scale' },
    ],
  });
  assert.equal(v.state, 'stale');
  assert.deepEqual(
    v.stale.map((r) => r.code),
    ['sim_set_time_scale', 'sim_set_weather'],
  );
  assert.equal(
    v.line,
    'Conditions changed since this was proposed: sim speed changed and the weather changed. The numbers above may be out of date.',
  );
  assert.equal(staleLine([]), '');
  // Blocked wins over stale.
  const ended = graph({ wargame: { active: false } });
  v = assessEngagement(approval(), ended, {
    approvedSince: [{ tool: 'sim_set_weather' }],
  });
  assert.equal(v.state, 'blocked');
});

test('helpers: target label, graph id, distance and the masked attacker', () => {
  assert.equal(MASKED_ATTACKER, 'Red air defence (not identified)');
  assert.equal(targetLabel(preview()), 'Air-defence guns');
  assert.equal(targetLabel(null), 'the target');
  assert.equal(targetLabel(preview({ target: { label: 'guns‮⁦' } })), 'guns');
  assert.equal(targetGraphId(preview()), 'trk:TRK-9');
  assert.equal(
    targetGraphId(preview({ target: { track_id: 'TRK-2', label: 'x' } })),
    'trk:TRK-2',
  );
  assert.equal(distanceM([0, 0], [0, 0]), 0);
  assert.ok(Math.abs(distanceM([0, 0], [1, 0]) - 111_195) < 5);
  assert.equal(distanceM([0, 'x'], [1, 0]), null);
});
