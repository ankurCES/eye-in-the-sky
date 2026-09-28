import test from 'node:test';
import assert from 'node:assert/strict';

import {
  activeGrants,
  approvedSince,
  availabilityOf,
  foldAll,
  grantOffered,
  grantScopeOf,
  initialState,
  latestCommandRow,
  pendingApprovals,
  rateLimit,
  reduce,
  resultState,
  sessionCost,
  statusWord,
} from './reducer.js';
import { SENSOR_TOOLS } from './format.js';

const T0 = Date.UTC(2026, 8, 27, 14, 2, 0);

/** An SSE event action with seq n, received at T0 + n seconds. */
const ev = (name, data, seq, extra = {}) => ({
  type: 'event',
  name,
  data,
  seq,
  at: T0 + seq * 1000,
  replay: false,
  ...extra,
});

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const v of Object.values(value)) deepFreeze(v);
  }
  return value;
}

const SESSION = ev(
  'session',
  { session_id: 's1', model: 'claude-x', available: true, last_seq: 0 },
  0,
);

test('a plain turn folds into operator text, thinking, rows, text and usage', () => {
  const s = foldAll([
    SESSION,
    ev(
      'turn_start',
      { turn_id: 't1', text: 'Which contacts are near North Field?' },
      1,
    ),
    ev('thinking', { turn_id: 't1', text: 'Look at the picture. ' }, 2),
    ev('thinking', { turn_id: 't1', text: 'Then search.' }, 3),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c1',
        tool: 'intel_search',
        title: 'Search intel',
        class: 'read',
        args: { query: 'North Field' },
        summary: 'North Field · contacts',
      },
      4,
    ),
    ev(
      'tool_result',
      {
        call_id: 'c1',
        ok: true,
        summary: '2 results',
        bytes: 812,
        truncated: false,
        entities: ['trk:T-1', 'trk:T-2'],
      },
      5,
    ),
    ev('text_delta', { turn_id: 't1', text: 'Two contacts ' }, 6),
    ev('text_delta', { turn_id: 't1', text: 'are unassessed.' }, 7),
    ev(
      'usage',
      { turn_id: 't1', cost_usd: 0.02, input_tokens: 3100, output_tokens: 410 },
      8,
    ),
    ev('turn_end', { turn_id: 't1', stop: 'end' }, 9),
  ]);
  assert.equal(s.session.id, 's1');
  assert.equal(s.lastSeq, 9);
  assert.deepEqual(
    s.items.map((i) => i.kind),
    ['turn'],
  );
  const turn = s.turns.t1;
  assert.equal(turn.text, 'Which contacts are near North Field?');
  assert.equal(turn.status, 'end');
  assert.equal(turn.at, T0 + 1000);
  assert.equal(turn.endAt, T0 + 9000);
  assert.deepEqual(
    turn.blocks.map((b) => b.kind),
    ['thinking', 'tool', 'text'],
  );
  assert.equal(turn.blocks[0].text, 'Look at the picture. Then search.');
  assert.equal(
    turn.blocks[0].open,
    false,
    'thinking closes when a tool call arrives',
  );
  assert.equal(turn.blocks[0].endAt, T0 + 4000);
  assert.equal(turn.blocks[2].text, 'Two contacts are unassessed.');
  assert.equal(s.rows.c1.state, 'done');
  assert.equal(s.rows.c1.klass, 'read');
  assert.deepEqual(s.rows.c1.result.entities, ['trk:T-1', 'trk:T-2']);
  assert.equal(s.rows.c1.endAt - s.rows.c1.at, 1000);
  assert.equal(s.usage.turns.t1.output_tokens, 410);
  assert.equal(sessionCost(s), 0.02);
  assert.equal(s.running, null);
  assert.equal(statusWord(s), 'Ready');
});

test('status words follow the turn: starting, thinking, answering, ready', () => {
  let s = foldAll([
    { type: 'availability', available: true, model: 'm' },
    SESSION,
  ]);
  assert.equal(statusWord(s), 'Ready');
  s = reduce(s, { type: 'send', text: 'hi', focused_ids: [], at: T0 });
  assert.equal(statusWord(s), 'Starting');
  s = reduce(s, ev('turn_start', { turn_id: 't1', text: 'hi' }, 1));
  assert.equal(statusWord(s), 'Starting', 'first turn with nothing back yet');
  s = reduce(s, ev('thinking', { turn_id: 't1', text: 'hmm' }, 2));
  assert.equal(statusWord(s), 'Thinking');
  s = reduce(s, ev('text_delta', { turn_id: 't1', text: 'Hello' }, 3));
  assert.equal(statusWord(s), 'Answering');
  s = reduce(s, ev('turn_end', { turn_id: 't1', stop: 'end' }, 4));
  assert.equal(statusWord(s), 'Ready');
  s = reduce(s, ev('turn_start', { turn_id: 't2', text: 'again' }, 5));
  assert.equal(statusWord(s), 'Thinking', 'later turns never say Starting');
});

test('the reducer is pure: inputs are never mutated', () => {
  const s0 = deepFreeze(
    foldAll([SESSION, ev('turn_start', { turn_id: 't1', text: 'x' }, 1)]),
  );
  const actions = [
    ev('text_delta', { turn_id: 't1', text: 'a' }, 2),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c',
        tool: 'uav_takeoff',
        class: 'command',
        args: {},
      },
      3,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'a',
        call_id: 'c',
        tool: 'uav_takeoff',
        class: 'command',
        consequences: [],
      },
      4,
    ),
    {
      type: 'decision',
      id: 'a',
      decision: 'approve',
      note: null,
      at: T0 + 4500,
    },
    ev(
      'approval_resolved',
      { approval_id: 'a', call_id: 'c', decision: 'approved' },
      5,
    ),
    ev('tool_result', { call_id: 'c', ok: true, summary: 'ok' }, 6),
    ev('usage', { turn_id: 't1', cost_usd: 1 }, 7),
    ev('ui', { action: 'focus', ids: ['veh:Drone1'] }, 8),
    ev('turn_end', { turn_id: 't1', stop: 'end' }, 9),
    { type: 'new_session', reason: 'operator', at: T0 + 10_000 },
  ];
  assert.doesNotThrow(() => foldAll(actions, s0));
});

test('duplicate event ids are folded once', () => {
  const a = foldAll([
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
  ]);
  const b = reduce(a, ev('text_delta', { turn_id: 't1', text: 'once' }, 2));
  const c = reduce(b, ev('text_delta', { turn_id: 't1', text: 'once' }, 2));
  assert.equal(c, b, 'same state object: nothing changed');
  const d = reduce(c, ev('text_delta', { turn_id: 't1', text: 'old' }, 1));
  assert.equal(d, c, 'an older id is a duplicate too');
  assert.equal(d.turns.t1.blocks[0].text, 'once');
});

test('a jump in sequence ids is recorded as a gap with its time span', () => {
  let s = foldAll([
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
    ev('text_delta', { turn_id: 't1', text: 'a' }, 2),
  ]);
  s = reduce(s, ev('text_delta', { turn_id: 't1', text: 'b' }, 7));
  assert.equal(s.gaps.length, 1);
  assert.equal(s.gaps[0].afterSeq, 2);
  assert.equal(s.gaps[0].beforeSeq, 7);
  assert.equal(s.gaps[0].fromAt, T0 + 2000);
  assert.equal(s.gaps[0].toAt, T0 + 7000);
  assert.deepEqual(s.items.at(-1), { kind: 'gap', index: 0 });
  assert.equal(s.lastSeq, 7);
});

test('reconnect: nothing missed shows a transient notice; truncation shows a gap', () => {
  const base = foldAll([
    SESSION,
    { type: 'connection', state: 'open', at: T0 },
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
  ]);
  let s = reduce(base, {
    type: 'connection',
    state: 'reconnecting',
    at: T0 + 30_000,
  });
  assert.equal(s.connection.reconnecting, true);
  assert.equal(
    s.connection.lostAt,
    T0 + 1000,
    'lost from the last event received',
  );
  assert.equal(statusWord(s), 'Reconnecting');
  const ok = reduce(s, {
    type: 'event',
    name: 'session',
    data: { session_id: 's1', history_truncated: false, last_seq: 1 },
    seq: 0,
    at: T0 + 40_000,
  });
  assert.equal(ok.notice.kind, 'reconnected');
  assert.equal(ok.connection.reconnecting, false);
  assert.equal(ok.gaps.length, 0);
  assert.equal(reduce(ok, { type: 'notice_seen' }).notice, null);

  const lost = reduce(s, {
    type: 'event',
    name: 'session',
    data: { session_id: 's1', history_truncated: true, last_seq: 900 },
    seq: 0,
    at: T0 + 40_000,
  });
  assert.equal(lost.notice.kind, 'gap');
  assert.equal(lost.gaps.length, 1);
  assert.equal(lost.gaps[0].fromAt, T0 + 1000);
  assert.equal(lost.gaps[0].toAt, T0 + 40_000);
  // The replay that follows jumps ids but is already covered: one gap only.
  const after = reduce(
    lost,
    ev('text_delta', { turn_id: 't1', text: 'z' }, 600),
  );
  assert.equal(after.gaps.length, 1);
});

test('a truncated log on a fresh connect marks the start as missing', () => {
  const s = reduce(initialState(), {
    type: 'event',
    name: 'session',
    data: { session_id: 's1', history_truncated: true, last_seq: 700 },
    seq: 0,
    at: T0,
  });
  assert.equal(s.gaps.length, 1);
  assert.equal(s.gaps[0].start, true);
});

test('replayed events keep an unknown time as null, never now', () => {
  const s = foldAll([
    SESSION,
    {
      type: 'event',
      name: 'turn_start',
      data: { turn_id: 't1', text: 'x' },
      seq: 1,
      at: null,
      replay: true,
    },
    {
      type: 'event',
      name: 'text_delta',
      data: { turn_id: 't1', text: 'y' },
      seq: 2,
      at: null,
      replay: true,
    },
  ]);
  assert.equal(s.turns.t1.at, null);
  assert.equal(s.turns.t1.replay, true);
  assert.equal(s.turns.t1.replyAt, null);
});

test('events for a turn whose start was lost create a stub turn', () => {
  const s = foldAll([
    SESSION,
    ev('text_delta', { turn_id: 'tx', text: 'orphan' }, 5),
  ]);
  assert.equal(s.turns.tx.text, null);
  assert.equal(s.running, 'tx');
  assert.equal(s.turns.tx.blocks[0].text, 'orphan');
});

function commandTurn() {
  return [
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'Launch it' }, 1),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c1',
        tool: 'mission_grid_search',
        title: 'Grid search',
        class: 'command',
        args: { vehicle: 'Drone1' },
        summary: 'Drone1 · North Field AO',
      },
      2,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'a1',
        call_id: 'c1',
        tool: 'mission_grid_search',
        class: 'command',
        title: 'Grid search',
        summary: 'Drone1 · North Field AO',
        args: { vehicle: 'Drone1' },
        consequences: ['Drone1 flies the grid search mission.'],
        dry_run: { ok: true, gate: { required_pct: 41.3 }, at_ms: T0 },
        allow_session: false,
        expires_at_ms: T0 + 600_000,
      },
      3,
    ),
  ];
}

test('approval flow: awaiting, deciding, approved, running, done', () => {
  let s = foldAll(commandTurn());
  assert.equal(s.rows.c1.state, 'awaiting');
  assert.equal(s.rows.c1.approvalId, 'a1');
  assert.equal(pendingApprovals(s).length, 1);
  assert.equal(statusWord(s), 'Waiting for your approval');
  const a = s.approvals.a1;
  assert.equal(a.klass, 'command');
  assert.equal(a.turnId, 't1');
  assert.deepEqual(a.consequences, ['Drone1 flies the grid search mission.']);
  assert.equal(a.dry_run.gate.required_pct, 41.3);
  s = reduce(s, {
    type: 'decision',
    id: 'a1',
    decision: 'approve',
    note: '',
    at: T0 + 3500,
  });
  assert.equal(s.approvals.a1.state, 'deciding');
  assert.equal(
    pendingApprovals(s).length,
    1,
    'still waiting until the server confirms',
  );
  s = reduce(s, { type: 'decision_ok', id: 'a1' });
  assert.deepEqual(s.grants, {}, 'a one-time approval grants nothing');
  s = reduce(
    s,
    ev(
      'approval_resolved',
      { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
      4,
    ),
  );
  assert.equal(s.approvals.a1.state, 'approved');
  assert.equal(s.approvals.a1.scope, 'once');
  assert.equal(s.approvals.a1.resolvedAt, T0 + 4000);
  assert.equal(s.rows.c1.state, 'running');
  assert.equal(s.rows.c1.runAt, T0 + 4000, 'running time starts at approval');
  assert.equal(pendingApprovals(s).length, 0);
  s = reduce(
    s,
    ev(
      'tool_result',
      { call_id: 'c1', ok: true, summary: 'Mission MSN-1 started' },
      5,
    ),
  );
  assert.equal(s.rows.c1.state, 'done');
  assert.equal(latestCommandRow(s, 'Drone1').callId, 'c1');
});

test('a failed decision returns the slip to pending with the error', () => {
  let s = foldAll(commandTurn());
  s = reduce(s, { type: 'decision', id: 'a1', decision: 'approve', at: T0 });
  s = reduce(s, { type: 'decision_failed', id: 'a1', error: 'offline' });
  assert.equal(s.approvals.a1.state, 'pending');
  assert.equal(s.approvals.a1.error, 'offline');
  assert.equal(s.approvals.a1.localDecision, null);
});

test('decisions on a slip that is not pending are ignored', () => {
  let s = foldAll([
    ...commandTurn(),
    ev(
      'approval_resolved',
      { approval_id: 'a1', call_id: 'c1', decision: 'expired' },
      4,
    ),
  ]);
  const before = s;
  s = reduce(s, { type: 'decision', id: 'a1', decision: 'approve', at: T0 });
  assert.equal(s, before);
});

test('an approval that arrives before its tool_call makes one row, not two', () => {
  const s = foldAll([
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
    ev(
      'approval_request',
      {
        approval_id: 'a1',
        call_id: 'c1',
        tool: 'uav_takeoff',
        class: 'command',
        title: 'Take off',
        consequences: [],
      },
      2,
    ),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c1',
        tool: 'uav_takeoff',
        class: 'command',
        title: 'Take off',
        args: { vehicle: 'Drone1' },
        summary: 'Drone1',
      },
      3,
    ),
  ]);
  const tools = s.turns.t1.blocks.filter((b) => b.kind === 'tool');
  assert.equal(tools.length, 1);
  assert.equal(s.rows.c1.state, 'awaiting');
  assert.equal(s.rows.c1.summary, 'Drone1');
});

test('denied, expired and cancelled rows keep their slip outcome over the error result', () => {
  for (const decision of ['denied', 'expired', 'cancelled']) {
    const s = foldAll([
      ...commandTurn(),
      ev(
        'approval_resolved',
        {
          approval_id: 'a1',
          call_id: 'c1',
          decision,
          note: decision === 'denied' ? 'Use 80 m instead.' : undefined,
        },
        4,
      ),
      ev(
        'tool_result',
        { call_id: 'c1', ok: false, error: 'The operator denied this call' },
        5,
      ),
    ]);
    assert.equal(s.rows.c1.state, decision);
    assert.equal(s.approvals.a1.state, decision);
    assert.equal(s.rows.c1.result.error, 'The operator denied this call');
    if (decision === 'denied')
      assert.equal(s.approvals.a1.note, 'Use 80 m instead.');
  }
});

test('result states: ok, rejected, error, busy and not-run are distinct', () => {
  assert.equal(resultState({ ok: true }), 'done');
  assert.equal(
    resultState({ ok: false, rejected: true, summary: 'gate' }),
    'rejected',
  );
  assert.equal(resultState({ ok: false, error: 'boom' }), 'failed');
  assert.equal(resultState({ ok: false, summary: 'Vehicle busy' }), 'not_run');
  assert.equal(resultState({ ok: false }), 'not_run');
  // v1.1 outcome wins over the legacy flags.
  assert.equal(
    resultState({ ok: false, outcome: 'busy', error: 'x' }),
    'not_run',
  );
  assert.equal(resultState({ ok: false, outcome: 'not_run' }), 'not_run');
  assert.equal(resultState({ ok: true, outcome: 'rejected' }), 'rejected');
  assert.equal(resultState({ ok: false, outcome: 'error' }), 'failed');
  assert.equal(resultState({ ok: false, outcome: 'ok' }), 'done');
  const s = foldAll([
    ...commandTurn(),
    ev(
      'approval_resolved',
      { approval_id: 'a1', call_id: 'c1', decision: 'approved' },
      4,
    ),
    ev(
      'tool_result',
      {
        call_id: 'c1',
        ok: false,
        outcome: 'busy',
        busy_with: { mission_id: 'MSN-9' },
        summary: 'Vehicle busy with another task',
      },
      5,
    ),
  ]);
  assert.equal(s.rows.c1.state, 'not_run');
  assert.equal(s.rows.c1.busy, true);
  assert.deepEqual(s.rows.c1.busyWith, { mission_id: 'MSN-9' });
});

function sensorTurn(extra = {}) {
  return [
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'Scan' }, 1),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c1',
        tool: 'uav_scan_targets',
        class: 'sensor',
        title: 'Scan for targets',
        args: { vehicle: 'Drone1' },
      },
      2,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'a1',
        call_id: 'c1',
        tool: 'uav_scan_targets',
        class: 'sensor',
        title: 'Scan for targets',
        consequences: [],
        allow_session: true,
        ...extra,
      },
      3,
    ),
  ];
}

test('v1 sensor grant: approve_session covers the five sensor tools, confirmed by the server', () => {
  let s = foldAll(sensorTurn());
  assert.equal(grantOffered(s.approvals.a1), true);
  assert.deepEqual(grantScopeOf(s.approvals.a1), [...SENSOR_TOOLS]);
  s = reduce(s, {
    type: 'decision',
    id: 'a1',
    decision: 'approve_session',
    at: T0 + 3500,
  });
  s = reduce(s, { type: 'decision_ok', id: 'a1' });
  assert.deepEqual(Object.keys(s.grants).sort(), [...SENSOR_TOOLS].sort());
  assert.equal(s.grants.uav_scan_targets.confirmed, false);
  s = reduce(
    s,
    ev(
      'approval_resolved',
      {
        approval_id: 'a1',
        call_id: 'c1',
        decision: 'approved',
        scope: 'session',
      },
      4,
    ),
  );
  assert.equal(s.grants.uav_capture_image.confirmed, true);
  assert.equal(s.approvals.a1.scope, 'session');
  s = reduce(
    s,
    ev('tool_result', { call_id: 'c1', ok: true, summary: '3 detections' }, 5),
  );
  // The next sensor call runs under the standing approval: no slip.
  s = reduce(
    s,
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c2',
        tool: 'uav_capture_image',
        class: 'sensor',
        args: {},
      },
      6,
    ),
  );
  assert.equal(s.rows.c2.state, 'granted');
  assert.equal(s.rows.c2.viaGrant, true);
  s = reduce(s, ev('tool_result', { call_id: 'c2', ok: true }, 7));
  assert.equal(s.rows.c2.state, 'done');
  assert.equal(s.rows.c2.viaGrant, true);
  assert.equal(activeGrants(s).length, 5);
});

test('v1.1 grants are per tool and only when grant_scope is non-null', () => {
  let s = foldAll(sensorTurn({ grant_scope: ['uav_scan_targets'] }));
  assert.equal(grantOffered(s.approvals.a1), true);
  s = reduce(
    s,
    ev(
      'approval_resolved',
      {
        approval_id: 'a1',
        call_id: 'c1',
        decision: 'approved',
        scope: 'session',
        tool: 'uav_scan_targets',
      },
      4,
    ),
  );
  assert.deepEqual(Object.keys(s.grants), ['uav_scan_targets']);
  s = reduce(
    s,
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'c2',
        tool: 'uav_capture_image',
        class: 'sensor',
      },
      5,
    ),
  );
  assert.equal(
    s.rows.c2.state,
    'running',
    'another sensor tool is not covered',
  );

  const none = foldAll(sensorTurn({ grant_scope: null }));
  assert.equal(grantOffered(none.approvals.a1), false);
});

test('sim, command and override classes are never offered a session grant', () => {
  for (const klass of ['sim', 'command', 'safety_override']) {
    const s = foldAll([
      SESSION,
      ev(
        'approval_request',
        {
          approval_id: 'a',
          call_id: 'c',
          tool: 'sim_set_weather',
          class: klass,
          allow_session: true,
          consequences: [],
        },
        1,
      ),
    ]);
    assert.equal(grantOffered(s.approvals.a), false, klass);
  }
});

test('server grants replace the local record; revoke removes one; null means no route', () => {
  let s = reduce(initialState(), {
    type: 'grants',
    grants: [{ tool: 'uav_scan_targets', since_ms: T0 }],
  });
  assert.equal(s.grantsSupported, true);
  assert.equal(s.grants.uav_scan_targets.since, T0);
  s = reduce(s, { type: 'revoke', tool: 'uav_scan_targets' });
  assert.deepEqual(s.grants, {});
  s = reduce(s, { type: 'grants', grants: null });
  assert.equal(s.grantsSupported, false);
});

test('interrupt: pending slips file as cancelled by you and the turn records the stop time', () => {
  let s = foldAll(commandTurn());
  s = reduce(s, { type: 'interrupt', at: T0 + 3700 });
  s = reduce(
    s,
    ev(
      'approval_resolved',
      { approval_id: 'a1', call_id: 'c1', decision: 'cancelled' },
      4,
    ),
  );
  assert.equal(s.approvals.a1.cancelCause, 'interrupt');
  assert.equal(s.rows.c1.state, 'cancelled');
  s = reduce(s, ev('turn_end', { turn_id: 't1', stop: 'interrupted' }, 5));
  const stopped = s.turns.t1.blocks.at(-1);
  assert.equal(stopped.kind, 'stopped');
  assert.equal(stopped.at, T0 + 3700, 'the press time, not the server echo');
  assert.equal(s.turns.t1.status, 'interrupted');
  assert.equal(s.interruptedAt, null);
  assert.equal(s.running, null);
});

test('interrupt when nothing runs is a no-op', () => {
  const s = foldAll([SESSION]);
  assert.equal(reduce(s, { type: 'interrupt', at: T0 }), s);
});

test('turn_end settles rows that never got a result, honestly', () => {
  const s = foldAll([
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
    ev(
      'tool_call',
      {
        turn_id: 't1',
        call_id: 'r',
        tool: 'uav_takeoff',
        class: 'command',
        args: { vehicle: 'Drone1' },
      },
      2,
    ),
    ev(
      'tool_call',
      { turn_id: 't1', call_id: 'w', tool: 'uav_land', class: 'command' },
      3,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'aw',
        call_id: 'w',
        tool: 'uav_land',
        class: 'command',
        consequences: [],
      },
      4,
    ),
    ev('turn_end', { turn_id: 't1', stop: 'error', error: 'crash' }, 5),
  ]);
  assert.equal(
    s.rows.r.state,
    'no_result',
    'it may have run: never claim either way',
  );
  assert.equal(s.rows.w.state, 'cancelled');
  assert.equal(s.approvals.aw.state, 'cancelled');
  assert.equal(s.turns.t1.error, 'crash');
});

test('errors: sign-in failures mark the analyst not signed in', () => {
  let s = foldAll([SESSION, ev('turn_start', { turn_id: 't1', text: 'x' }, 1)]);
  s = reduce(
    s,
    ev(
      'error',
      {
        message: 'The analyst could not sign in to Claude.',
        hint: 'Sign in with the claude CLI',
        retryable: false,
      },
      2,
    ),
  );
  assert.equal(s.auth, 'failed');
  assert.equal(availabilityOf(s).reason, 'auth');
  assert.equal(statusWord(s), 'Not signed in');
  assert.equal(s.turns.t1.blocks.at(-1).kind, 'error');
  s = reduce(s, { type: 'availability', available: true, clearAuth: true });
  assert.equal(s.auth, null);

  const loose = reduce(
    foldAll([SESSION]),
    ev('error', { message: 'Tool server down', retryable: true }, 1),
  );
  assert.equal(loose.items.at(-1).kind, 'error');
  assert.equal(loose.items.at(-1).retryable, true);
});

test('availability and unavailable status words', () => {
  const s = reduce(initialState(), {
    type: 'availability',
    available: false,
    reason: 'sdk_missing',
    hint: 'pip install',
  });
  assert.deepEqual(availabilityOf(s), {
    available: false,
    reason: 'sdk_missing',
    hint: 'pip install',
  });
  assert.equal(statusWord(s), 'Unavailable');
  assert.equal(statusWord(initialState()), 'Starting');
});

test('usage: costs sum per turn, the server total wins, rate limits set the words', () => {
  let s = foldAll([
    SESSION,
    ev('usage', { turn_id: 't1', cost_usd: 0.1 }, 1),
    ev('usage', { turn_id: 't2', cost_usd: 0.08 }, 2),
  ]);
  assert.ok(Math.abs(sessionCost(s) - 0.18) < 1e-9);
  s = reduce(
    s,
    ev('usage', { turn_id: 't3', cost_usd: 0.01, session_cost_usd: 0.5 }, 3),
  );
  assert.equal(sessionCost(s), 0.5);
  const resets = Math.floor((T0 + 3_600_000) / 1000);
  s = reduce(
    s,
    ev(
      'usage',
      {
        turn_id: 't3',
        rate_limit: { status: 'allowed_warning', resets_at: resets },
      },
      4,
    ),
  );
  assert.equal(rateLimit(s, T0).level, 'warning');
  assert.equal(rateLimit(s, T0).resetsAt, resets * 1000);
  assert.equal(statusWord(s, T0), 'Close to the usage limit');
  s = reduce(
    s,
    ev(
      'usage',
      { turn_id: 't3', rate_limit: { status: 'rejected', resets_at: resets } },
      5,
    ),
  );
  assert.equal(statusWord(s, T0), 'Usage limit reached');
  assert.equal(
    rateLimit(s, T0 + 3_700_000).level,
    'ok',
    'past the reset time it clears',
  );
  s = reduce(s, ev('usage', { turn_id: 't3', rate_limit: null }, 6));
  assert.equal(rateLimit(s, T0).level, 'ok');
});

test('ui directives land in the running turn, or at the transcript level', () => {
  let s = foldAll([SESSION, ev('turn_start', { turn_id: 't1', text: 'x' }, 1)]);
  s = reduce(
    s,
    ev(
      'ui',
      { action: 'focus', ids: ['trk:T-1', 'trk:T-2'], note: 'unassessed' },
      2,
    ),
  );
  const d = s.turns.t1.blocks.at(-1);
  assert.equal(d.kind, 'directive');
  assert.deepEqual(d.ids, ['trk:T-1', 'trk:T-2']);
  assert.equal(d.seq, 2);
  assert.equal(s.directives.length, 1);
  s = reduce(s, ev('turn_end', { turn_id: 't1', stop: 'end' }, 3));
  s = reduce(
    s,
    ev(
      'ui',
      { action: 'track', vehicle: 'Drone1', reason: 'mission launched' },
      4,
    ),
  );
  assert.equal(
    s.turns.t1.blocks.at(-1).action,
    'track',
    'no turn running: the last turn',
  );
  const bare = reduce(foldAll([SESSION]), ev('ui', { action: 'orb' }, 1));
  assert.equal(bare.items.at(-1).kind, 'directive');
});

test('send: the outbox shows until the echo, and the echo carries the context', () => {
  let s = foldAll([SESSION]);
  s = reduce(s, {
    type: 'send',
    text: '  Tell me about [[trk:T-1|SA-6]]  ',
    focused_ids: ['trk:T-1'],
    at: T0,
  });
  assert.equal(s.outbox.status, 'sending');
  assert.equal(s.outbox.text, 'Tell me about [[trk:T-1|SA-6]]');
  s = reduce(s, { type: 'send_ok', turn_id: 't1' });
  assert.equal(s.outbox.status, 'sent');
  s = reduce(
    s,
    ev(
      'turn_start',
      { turn_id: 't1', text: 'Tell me about [[trk:T-1|SA-6]]' },
      1,
    ),
  );
  assert.equal(s.outbox, null);
  assert.deepEqual(s.turns.t1.focusedIds, ['trk:T-1']);
  assert.equal(s.lastOperatorText, 'Tell me about [[trk:T-1|SA-6]]');

  let f = reduce(foldAll([SESSION]), {
    type: 'send',
    text: 'x',
    focused_ids: [],
    at: T0,
  });
  f = reduce(f, { type: 'send_failed', error: 'offline', code: 'offline' });
  assert.equal(f.outbox.status, 'failed');
  assert.equal(f.pendingContext.length, 0);
  assert.equal(reduce(f, { type: 'clear_outbox' }).outbox, null);
});

test('new session: pending slips cancel, grants clear, the transcript keeps a divider', () => {
  let s = foldAll([
    ...sensorTurn(),
    ev(
      'approval_resolved',
      {
        approval_id: 'a1',
        call_id: 'c1',
        decision: 'approved',
        scope: 'session',
      },
      4,
    ),
  ]);
  s = foldAll(
    [
      ev(
        'tool_call',
        { turn_id: 't1', call_id: 'c9', tool: 'uav_takeoff', class: 'command' },
        5,
      ),
      ev(
        'approval_request',
        {
          approval_id: 'a9',
          call_id: 'c9',
          tool: 'uav_takeoff',
          class: 'command',
          consequences: [],
        },
        6,
      ),
    ],
    s,
  );
  assert.ok(Object.keys(s.grants).length);
  s = reduce(s, { type: 'new_session', reason: 'operator', at: T0 + 60_000 });
  assert.deepEqual(s.grants, {});
  assert.equal(s.lastSeq, 0);
  assert.equal(s.running, null);
  assert.equal(s.approvals.a9.state, 'cancelled');
  assert.equal(s.approvals.a9.cancelCause, 'session');
  assert.equal(s.items.at(-1).kind, 'divider');
  assert.equal(s.turns.t1.status, 'interrupted');
  // Events of the new session start again at seq 1.
  s = reduce(s, ev('turn_start', { turn_id: 'n1', text: 'fresh' }, 1));
  assert.equal(s.turns.n1.text, 'fresh');
});

test('approvedSince lists approvals after a reference time, never without one', () => {
  let s = foldAll([
    SESSION,
    ev(
      'approval_request',
      {
        approval_id: 'w',
        call_id: 'cw',
        tool: 'sim_set_weather',
        class: 'sim',
        consequences: [],
      },
      1,
    ),
    ev(
      'approval_resolved',
      { approval_id: 'w', call_id: 'cw', decision: 'approved' },
      2,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'g',
        call_id: 'cg',
        tool: 'uav_goto_gps',
        class: 'command',
        args: { vehicle: 'Drone1' },
        consequences: [],
      },
      5,
    ),
    ev(
      'approval_resolved',
      { approval_id: 'g', call_id: 'cg', decision: 'approved' },
      6,
    ),
    ev(
      'approval_request',
      {
        approval_id: 'd',
        call_id: 'cd',
        tool: 'uav_land',
        class: 'command',
        consequences: [],
      },
      7,
    ),
    ev(
      'approval_resolved',
      { approval_id: 'd', call_id: 'cd', decision: 'denied' },
      8,
    ),
  ]);
  assert.deepEqual(approvedSince(s, null), []);
  assert.deepEqual(
    approvedSince(s, T0).map((a) => a.id),
    ['w', 'g'],
  );
  assert.deepEqual(
    approvedSince(s, T0 + 3000).map((a) => [a.id, a.vehicle, a.klass]),
    [['g', 'Drone1', 'command']],
  );
  assert.deepEqual(
    approvedSince(s, T0, 'g').map((a) => a.id),
    ['w'],
  );
});

test('unknown actions and events leave the state alone', () => {
  const s = foldAll([SESSION]);
  assert.equal(reduce(s, { type: 'nope' }), s);
  assert.equal(reduce(s, null), s);
  const t = reduce(s, ev('mystery', {}, 1));
  assert.equal(t.lastSeq, 1, 'its id still counts');
  assert.equal(t.items.length, 0);
});

// ---- BYOK providers (BYOK spec §5, §8) ------------------------------------------------------

test('error.code drives availability: the words are never matched', async () => {
  const { currentProvider } = await import('./reducer.js');
  const start = foldAll([
    SESSION,
    ev('turn_start', { turn_id: 't1', text: 'x' }, 1),
  ]);
  // A provider's 401: its label, never "not signed in".
  let s = reduce(
    start,
    ev(
      'error',
      {
        code: 'auth',
        provider: { id: 'minimax', label: 'MiniMax' },
        message: 'MiniMax rejected the key.',
        hint: 'Open analyst settings to replace the key.',
      },
      2,
    ),
  );
  assert.equal(s.auth, 'provider');
  assert.deepEqual(availabilityOf(s), {
    available: false,
    reason: 'provider_auth',
    hint: null,
    provider: { id: 'minimax', label: 'MiniMax' },
  });
  assert.equal(statusWord(s), 'Unavailable');
  assert.equal(s.turns.t1.blocks.at(-1).code, 'auth');
  // The Claude login's auth code keeps today's sign-in state.
  s = reduce(
    start,
    ev(
      'error',
      {
        code: 'auth',
        provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
        message: 'x',
      },
      2,
    ),
  );
  assert.equal(availabilityOf(s).reason, 'auth');
  // Sign-in words with another code change nothing.
  s = reduce(
    start,
    ev(
      'error',
      {
        code: 'network',
        message: "Couldn't reach the login server to authenticate.",
      },
      2,
    ),
  );
  assert.equal(s.auth, null);
  // A turn refused for settings: the status names the exact reason.
  s = reduce(
    start,
    ev(
      'error',
      {
        code: 'config',
        provider: { id: 'deepseek', label: 'DeepSeek' },
        message: 'x',
      },
      2,
    ),
  );
  assert.equal(availabilityOf(s).reason, 'provider_not_configured');
  s = reduce(s, {
    type: 'availability',
    available: false,
    reason: 'provider_key_missing',
    hint: 'Open analyst settings.',
  });
  assert.equal(availabilityOf(s).reason, 'provider_key_missing');
  assert.equal(availabilityOf(s).provider.label, 'DeepSeek');
  s = reduce(s, {
    type: 'availability',
    available: true,
    clearAuth: true,
    provider: { id: 'deepseek', label: 'DeepSeek' },
  });
  assert.equal(availabilityOf(s).available, true);
  assert.equal(currentProvider(s)?.label, 'DeepSeek');
});

test('provider_changed: a divider (kept or cleared) and the header follows the new provider', async () => {
  const { currentProvider } = await import('./reducer.js');
  let s = foldAll([
    ev(
      'session',
      {
        session_id: 's1',
        model: 'claude-opus-5-5',
        provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
      },
      1,
    ),
    ev('turn_start', { turn_id: 't1', text: 'x' }, 2),
    ev('turn_end', { turn_id: 't1', stop: 'end' }, 3),
  ]);
  assert.equal(currentProvider(s).id, 'anthropic_login');
  s = reduce(
    s,
    ev(
      'provider_changed',
      {
        from: {
          id: 'anthropic_login',
          label: 'Claude login (this Mac)',
          model: 'claude-opus-5-5',
        },
        to: { id: 'minimax', label: 'MiniMax', model: 'MiniMax-M3[1m]' },
        memory: 'cleared',
        at_ms: T0,
      },
      4,
    ),
  );
  const div = s.items.at(-1);
  assert.equal(div.kind, 'divider');
  assert.equal(div.reason, 'provider');
  assert.equal(div.memory, 'cleared');
  assert.equal(div.to.label, 'MiniMax');
  assert.equal(s.session.model, 'MiniMax-M3[1m]');
  assert.equal(currentProvider(s).label, 'MiniMax');
  s = reduce(
    s,
    ev(
      'provider_changed',
      {
        to: { id: 'minimax', label: 'MiniMax', model: 'MiniMax-M2.7' },
        memory: 'kept',
      },
      5,
    ),
  );
  assert.equal(s.items.at(-1).memory, 'kept');
  assert.equal(s.session.model, 'MiniMax-M2.7');
});

test('a live status after a settings change moves the header to the new provider', async () => {
  const { currentProvider } = await import('./reducer.js');
  let s = foldAll([
    ev(
      'session',
      {
        session_id: 's1',
        model: 'claude-opus-5',
        provider: { id: 'anthropic_login', label: 'Claude login (this Mac)' },
      },
      1,
    ),
  ]);
  s = reduce(s, {
    type: 'availability',
    available: true,
    model: 'stub-model-1',
    provider: { id: 'custom', label: 'Custom Anthropic-compatible endpoint' },
  });
  assert.equal(s.session.model, 'stub-model-1');
  assert.equal(currentProvider(s).id, 'custom');
  assert.equal(s.session.id, 's1');
  // An unavailable status leaves what the session last used on screen.
  s = reduce(s, {
    type: 'availability',
    available: false,
    reason: 'provider_key_missing',
    model: 'MiniMax-M3[1m]',
    provider: { id: 'minimax', label: 'MiniMax' },
  });
  assert.equal(s.session.model, 'stub-model-1');
  assert.equal(currentProvider(s).id, 'custom');
});

test('usage: an unreliable cost basis is never summed into dollars', async () => {
  const { costBasis } = await import('./reducer.js');
  let s = foldAll([SESSION, ev('turn_start', { turn_id: 't1', text: 'x' }, 1)]);
  s = reduce(
    s,
    ev(
      'usage',
      {
        turn_id: 't1',
        input_tokens: 10,
        output_tokens: 5,
        cost_basis: 'unreliable',
        cost_usd: 0.5,
        session_cost_usd: 0.5,
      },
      2,
    ),
  );
  assert.equal(costBasis(s), 'unreliable');
  assert.equal(s.usage.sessionCost, 0);
  assert.equal(s.usage.serverSessionCost, null);
  assert.equal(s.usage.turns.t1.input_tokens, 10);
  assert.equal(s.usage.turns.t1.costBasis, 'unreliable');
});

test('review: a switch mid-turn leaves the running turn on its own provider', async () => {
  const { currentProvider } = await import('./reducer.js');
  const custom = {
    id: 'custom',
    label: 'Custom Anthropic-compatible endpoint',
  };
  const minimax = { id: 'minimax', label: 'MiniMax' };
  let s = foldAll([
    ev(
      'session',
      { session_id: 's1', model: 'stub-model-1', provider: custom },
      1,
    ),
    ev('turn_start', { turn_id: 't1', text: 'x' }, 2),
  ]);
  assert.equal(s.turns.t1.provider.id, 'custom');
  // Analyst settings switch to MiniMax while t1 runs.
  s = reduce(s, {
    type: 'availability',
    available: true,
    model: 'MiniMax-M3[1m]',
    provider: minimax,
  });
  assert.equal(
    currentProvider(s).id,
    'custom',
    'the header waits for the turn',
  );
  assert.equal(s.session.model, 'stub-model-1');
  assert.equal(s.availability.provider.id, 'minimax');
  s = reduce(
    s,
    ev(
      'usage',
      { turn_id: 't1', input_tokens: 3, cost_basis: 'unreliable' },
      3,
    ),
  );
  assert.equal(s.usage.turns.t1.provider.id, 'custom');
  s = reduce(s, ev('turn_end', { turn_id: 't1', stop: 'end' }, 4));
  assert.equal(currentProvider(s).id, 'minimax');
  assert.equal(s.session.model, 'MiniMax-M3[1m]');
  // A provider_changed event supersedes a deferred status.
  s = reduce(s, ev('turn_start', { turn_id: 't2', text: 'y' }, 5));
  assert.equal(s.turns.t2.provider.id, 'minimax');
  s = reduce(s, {
    type: 'availability',
    available: true,
    model: 'stub-model-1',
    provider: custom,
  });
  s = reduce(
    s,
    ev('provider_changed', { to: { ...minimax, model: 'MiniMax-M2.7' } }, 6),
  );
  s = reduce(s, ev('turn_end', { turn_id: 't2', stop: 'end' }, 7));
  assert.equal(currentProvider(s).id, 'minimax');
  assert.equal(s.session.model, 'MiniMax-M2.7');
  // A usage event that names its provider wins.
  s = reduce(s, ev('turn_start', { turn_id: 't3', text: 'z' }, 8));
  s = reduce(
    s,
    ev('usage', { turn_id: 't3', provider: custom, input_tokens: 1 }, 9),
  );
  assert.equal(s.usage.turns.t3.provider.id, 'custom');
});
