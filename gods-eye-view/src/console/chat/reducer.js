/**
 * Pure transcript reducer for the analyst chat.
 *
 * Folds the contract §3.1 SSE events (plus the §10 v1.1 fields) and a few
 * local actions into one immutable model:
 *
 *   { turns, rows (by call_id), approvals (by id, with states), directives,
 *     usage, grants, gaps, items (transcript order), running, … }
 *
 * Actions:
 *   {type:'event', name, data, seq, at, replay}   an SSE event
 *   {type:'connection', state:'open'|'reconnecting'|'closed', at}
 *   {type:'availability', available, reason?, hint?, model?}
 *   {type:'send', text, focused_ids, at} / 'send_ok' / 'send_failed'
 *   {type:'interrupt', at}
 *   {type:'decision', id, decision, note, at} / 'decision_ok' / 'decision_failed'
 *   {type:'grants', grants:[{tool, since_ms}] | null}   (null: route absent)
 *   {type:'revoke', tool}
 *   {type:'new_session', reason:'operator'|'lost', at}
 *   {type:'notice_seen'}
 *
 * `at` is the client's receipt time in ms, or null when unknown (a replayed
 * event from before this page load). The reducer never invents a time.
 */

import {
  SENSOR_TOOLS,
  approvalVehicle,
  callVehicle,
  classKey,
  epochMs,
} from './format.js';

/** Row states (spec §6.3). */
export const ROW_STATES = Object.freeze([
  'running',
  'awaiting',
  'granted',
  'done',
  'rejected',
  'not_run',
  'failed',
  'denied',
  'expired',
  'cancelled',
  'no_result',
]);

const SETTLED_BY_SLIP = new Set(['denied', 'expired', 'cancelled']);
const OPEN_ROW = new Set(['running', 'awaiting', 'granted']);
const RESOLVED = new Set(['approved', 'denied', 'expired', 'cancelled']);

export function initialState() {
  return {
    session: null,
    lastSeq: 0,
    connection: { state: 'idle', lostAt: null, reconnecting: false },
    lastEventAt: null,
    notice: null,
    gaps: [],
    items: [],
    turns: {},
    rows: {},
    approvals: {},
    approvalOrder: [],
    directives: [],
    usage: {
      sessionCost: 0,
      serverSessionCost: null,
      turns: {},
      rateLimit: null,
    },
    grants: {},
    grantsSupported: null,
    running: null,
    outbox: null,
    pendingContext: [],
    interruptedAt: null,
    availability: null,
    auth: null,
    lastOperatorText: null,
    turnsEnded: 0,
  };
}

// ---- helpers -----------------------------------------------------------------

const arr = (value) => (Array.isArray(value) ? value : []);
const str = (value) => (typeof value === 'string' ? value : null);
const num = (value) => (Number.isFinite(value) ? value : null);

function withTurn(state, turnId, fn) {
  const turn = state.turns[turnId];
  if (!turn) return state;
  return { ...state, turns: { ...state.turns, [turnId]: fn(turn) } };
}

/** Make sure a turn exists (an event for a turn whose start we never saw). */
function ensureTurn(state, turnId, at, replay) {
  const id = turnId || state.running;
  if (!id) return { state, id: null };
  if (state.turns[id]) return { state, id };
  const turn = {
    id,
    text: null,
    at: null,
    replyAt: at ?? null,
    endAt: null,
    status: 'running',
    error: null,
    blocks: [],
    focusedIds: [],
    replay: Boolean(replay),
  };
  return {
    id,
    state: {
      ...state,
      turns: { ...state.turns, [id]: turn },
      items: [...state.items, { kind: 'turn', id }],
      running: state.running || id,
    },
  };
}

/** Close an open thinking block when anything else arrives. */
function closeThinking(blocks, at) {
  const last = blocks[blocks.length - 1];
  if (last?.kind === 'thinking' && last.endAt == null && last.open) {
    return [...blocks.slice(0, -1), { ...last, endAt: at, open: false }];
  }
  return blocks;
}

function pushBlock(turn, block, at) {
  return {
    ...turn,
    replyAt: turn.replyAt ?? at ?? null,
    blocks: [...closeThinking(turn.blocks, at), block],
  };
}

/** Row state from a tool_result (contract §3.1 + §10.3). */
export function resultState(data) {
  const outcome = str(data?.outcome);
  if (outcome === 'ok') return 'done';
  if (outcome === 'rejected') return 'rejected';
  if (outcome === 'error') return 'failed';
  if (outcome === 'busy' || outcome === 'not_run') return 'not_run';
  if (data?.rejected === true) return 'rejected';
  if (data?.error) return 'failed';
  if (data?.ok === true) return 'done';
  return 'not_run';
}

/** The tools a session grant on this approval covers. */
export function grantScopeOf(approval, resolvedTool = null) {
  if (!approval) return resolvedTool ? [resolvedTool] : [];
  if (Array.isArray(approval.grantScope)) return approval.grantScope.slice();
  if (approval.grantScope === null) return resolvedTool ? [resolvedTool] : [];
  // v1 server: approve_session grants the whole sensor class.
  if (approval.klass === 'sensor') return SENSOR_TOOLS.slice();
  return resolvedTool ? [resolvedTool] : [];
}

/** Whether the console may offer a session grant on this approval (§6.6, §10.2). */
export function grantOffered(approval) {
  if (!approval || approval.klass !== 'sensor') return false;
  if (Array.isArray(approval.grantScope)) return approval.grantScope.length > 0;
  if (approval.grantScope === null) return false;
  return approval.allowSession === true;
}

function addGrants(grants, tools, at, confirmed) {
  const next = { ...grants };
  for (const tool of tools) {
    if (!tool) continue;
    const prev = next[tool];
    next[tool] = {
      tool,
      since: prev?.since ?? at ?? null,
      confirmed: Boolean(confirmed || prev?.confirmed),
    };
  }
  return next;
}

function approvalFrom(data, at, seq, replay, turnId) {
  return {
    id: String(data.approval_id),
    callId: str(data.call_id),
    tool: str(data.tool) || '',
    klass: classKey(data.class),
    title: str(data.title) || str(data.tool) || '',
    summary: str(data.summary) || '',
    args: data.args && typeof data.args === 'object' ? data.args : {},
    consequences: arr(data.consequences).map(String),
    dry_run:
      data.dry_run && typeof data.dry_run === 'object' ? data.dry_run : null,
    dry_runnable:
      typeof data.dry_runnable === 'boolean' ? data.dry_runnable : undefined,
    allowSession: data.allow_session === true,
    grantScope: Array.isArray(data.grant_scope)
      ? data.grant_scope.map(String)
      : data.grant_scope === null
        ? null
        : undefined,
    vehicle: str(data.vehicle),
    expiresAt: num(data.expires_at_ms),
    at: at ?? null,
    seq,
    replay: Boolean(replay),
    turnId: turnId || null,
    state: 'pending',
    localDecision: null,
    note: null,
    decidedAt: null,
    resolvedAt: null,
    scope: null,
    error: null,
    cancelCause: null,
  };
}

function rowFrom(fields) {
  return {
    callId: fields.callId,
    turnId: fields.turnId ?? null,
    tool: fields.tool ?? '',
    title: fields.title ?? fields.tool ?? '',
    klass: classKey(fields.klass),
    args: fields.args ?? {},
    summary: fields.summary ?? '',
    state: fields.state ?? 'running',
    at: fields.at ?? null,
    endAt: null,
    result: null,
    approvalId: fields.approvalId ?? null,
    // When an approved call started running (durations exclude the wait).
    runAt: null,
    viaGrant: Boolean(fields.viaGrant),
    busy: false,
    busyWith: null,
  };
}

function setRow(state, callId, row) {
  return { ...state, rows: { ...state.rows, [callId]: row } };
}

function attachToolBlock(state, callId, turnId, at, replay) {
  const ensured = ensureTurn(state, turnId, at, replay);
  if (!ensured.id) {
    // No turn to hang it on: keep it visible at the transcript level.
    return {
      ...ensured.state,
      items: [...ensured.state.items, { kind: 'tool', callId }],
    };
  }
  return withTurn(ensured.state, ensured.id, (turn) =>
    pushBlock(turn, { kind: 'tool', callId }, at),
  );
}

// ---- events ------------------------------------------------------------------

function onSession(state, data, at) {
  const id = str(data.session_id);
  let next = {
    ...state,
    session: {
      id,
      model: str(data.model) || state.session?.model || null,
      available: data.available !== false,
    },
  };
  const truncated = data.history_truncated === true;
  const wasReconnecting = state.connection.reconnecting;
  if (truncated && (state.lastSeq > 0 || state.items.length)) {
    next = {
      ...next,
      gaps: [
        ...next.gaps,
        {
          afterSeq: state.lastSeq,
          fromAt: state.connection.lostAt ?? state.lastEventAt,
          toAt: at ?? null,
        },
      ],
      items: [...next.items, { kind: 'gap', index: next.gaps.length }],
    };
  } else if (truncated) {
    next = {
      ...next,
      gaps: [
        ...next.gaps,
        { afterSeq: 0, fromAt: null, toAt: at ?? null, start: true },
      ],
      items: [...next.items, { kind: 'gap', index: next.gaps.length }],
    };
  }
  if (wasReconnecting) {
    next = {
      ...next,
      notice: truncated
        ? { kind: 'gap', fromAt: state.connection.lostAt, toAt: at ?? null }
        : { kind: 'reconnected', at: at ?? null },
      connection: { ...next.connection, reconnecting: false, lostAt: null },
    };
  }
  return next;
}

function onTurnStart(state, data, at, replay) {
  const id = str(data.turn_id);
  if (!id) return state;
  const text = str(data.text) ?? '';
  if (state.turns[id]) {
    // A stub turn created by an earlier event: fill in the operator message.
    return withTurn(state, id, (turn) => ({
      ...turn,
      text: turn.text ?? text,
      at: turn.at ?? at ?? null,
    }));
  }
  const key = text.trim();
  const ctxIndex = state.pendingContext.findIndex((p) => p.text === key);
  const focusedIds =
    ctxIndex >= 0 ? state.pendingContext[ctxIndex].focusedIds : [];
  const pendingContext =
    ctxIndex >= 0
      ? state.pendingContext.filter((_, i) => i !== ctxIndex)
      : state.pendingContext;
  const outbox =
    state.outbox && state.outbox.text === key ? null : state.outbox;
  const turn = {
    id,
    text,
    at: at ?? null,
    replyAt: null,
    endAt: null,
    status: 'running',
    error: null,
    blocks: [],
    focusedIds,
    replay: Boolean(replay),
  };
  let turns = state.turns;
  let running = state.running;
  if (running && turns[running] && turns[running].status === 'running') {
    // A new turn can only start after the previous one ended; close it.
    turns = {
      ...turns,
      [running]: { ...turns[running], status: 'end', endAt: at ?? null },
    };
  }
  running = id;
  return {
    ...state,
    turns: { ...turns, [id]: turn },
    items: [...state.items, { kind: 'turn', id }],
    running,
    outbox,
    pendingContext,
    lastOperatorText: text,
  };
}

function onStreamText(state, data, at, replay, kind) {
  const text = typeof data.text === 'string' ? data.text : '';
  if (!text) return state;
  const ensured = ensureTurn(state, str(data.turn_id), at, replay);
  if (!ensured.id) return state;
  return withTurn(ensured.state, ensured.id, (turn) => {
    const blocks = turn.blocks;
    const last = blocks[blocks.length - 1];
    if (kind === 'text') {
      if (last?.kind === 'text') {
        return {
          ...turn,
          blocks: [...blocks.slice(0, -1), { ...last, text: last.text + text }],
        };
      }
      return pushBlock(turn, { kind: 'text', text }, at);
    }
    if (last?.kind === 'thinking' && last.open) {
      return {
        ...turn,
        blocks: [
          ...blocks.slice(0, -1),
          { ...last, text: last.text + text, lastAt: at ?? last.lastAt },
        ],
      };
    }
    return pushBlock(
      turn,
      {
        kind: 'thinking',
        text,
        at: at ?? null,
        lastAt: at ?? null,
        endAt: null,
        open: true,
      },
      at,
    );
  });
}

function onToolCall(state, data, at, replay) {
  const callId = str(data.call_id);
  if (!callId) return state;
  const existing = state.rows[callId];
  const fields = {
    callId,
    turnId: str(data.turn_id) || existing?.turnId || state.running,
    tool: str(data.tool) || existing?.tool || '',
    title: str(data.title) || existing?.title || str(data.tool) || '',
    klass: data.class ?? existing?.klass,
    args:
      data.args && typeof data.args === 'object' ? data.args : existing?.args,
    summary: str(data.summary) ?? existing?.summary ?? '',
    at: existing?.at ?? at ?? null,
  };
  if (existing) {
    return setRow(state, callId, {
      ...existing,
      ...fields,
      klass: classKey(fields.klass),
    });
  }
  const klass = classKey(fields.klass);
  const viaGrant = klass === 'sensor' && Boolean(state.grants[fields.tool]);
  const row = rowFrom({
    ...fields,
    klass,
    state: viaGrant ? 'granted' : 'running',
    viaGrant,
  });
  const next = setRow(state, callId, row);
  return attachToolBlock(next, callId, fields.turnId, at, replay);
}

function onApprovalRequest(state, data, at, seq, replay) {
  if (data.approval_id == null) return state;
  const id = String(data.approval_id);
  if (state.approvals[id]) return state;
  const callId = str(data.call_id);
  const existingRow = callId ? state.rows[callId] : null;
  const turnId = existingRow?.turnId || state.running;
  const approval = approvalFrom(data, at, seq, replay, turnId);
  let next = {
    ...state,
    approvals: { ...state.approvals, [id]: approval },
    approvalOrder: [...state.approvalOrder, id],
  };
  if (!callId) return next;
  if (existingRow) {
    return setRow(next, callId, {
      ...existingRow,
      state: 'awaiting',
      approvalId: id,
      viaGrant: false,
    });
  }
  next = setRow(
    next,
    callId,
    rowFrom({
      callId,
      turnId,
      tool: approval.tool,
      title: approval.title,
      klass: approval.klass,
      args: approval.args,
      summary: approval.summary,
      state: 'awaiting',
      at,
      approvalId: id,
    }),
  );
  return attachToolBlock(next, callId, turnId, at, replay);
}

function onApprovalResolved(state, data, at) {
  if (data.approval_id == null) return state;
  const id = String(data.approval_id);
  const decision = RESOLVED.has(data.decision) ? data.decision : 'cancelled';
  const prev = state.approvals[id];
  const scope =
    str(data.scope) ||
    (decision === 'approved' && prev?.localDecision === 'approve_session'
      ? 'session'
      : decision === 'approved'
        ? 'once'
        : null);
  const approval = {
    ...(prev ||
      approvalFrom(
        { approval_id: id, call_id: data.call_id, tool: data.tool },
        at,
        0,
        false,
        state.running,
      )),
    state: decision,
    resolvedAt: at ?? prev?.decidedAt ?? null,
    scope,
    note: str(data.note) ?? prev?.note ?? null,
    cancelCause:
      decision === 'cancelled'
        ? prev?.cancelCause ||
          (state.interruptedAt != null ? 'interrupt' : null)
        : null,
  };
  let next = {
    ...state,
    approvals: { ...state.approvals, [id]: approval },
    approvalOrder: prev ? state.approvalOrder : [...state.approvalOrder, id],
  };
  if (decision === 'approved' && scope === 'session') {
    next = {
      ...next,
      grants: addGrants(
        next.grants,
        grantScopeOf(approval, str(data.tool)),
        at,
        true,
      ),
    };
  }
  const callId = str(data.call_id) || approval.callId;
  const row = callId ? next.rows[callId] : null;
  if (row && !row.result) {
    const rowState = decision === 'approved' ? 'running' : decision;
    next = setRow(next, callId, {
      ...row,
      state: rowState,
      runAt: decision === 'approved' ? (at ?? null) : row.runAt,
    });
  }
  return next;
}

function onToolResult(state, data, at, replay) {
  const callId = str(data.call_id);
  if (!callId) return state;
  let next = state;
  if (!next.rows[callId]) {
    next = setRow(
      next,
      callId,
      rowFrom({ callId, turnId: next.running, at, state: 'running' }),
    );
    next = attachToolBlock(next, callId, next.running, at, replay);
  }
  const row = next.rows[callId];
  const result = {
    ok: data.ok === true,
    rejected: data.rejected === true,
    error: str(data.error),
    summary: str(data.summary) ?? '',
    bytes: num(data.bytes),
    truncated: data.truncated === true,
    entities: arr(data.entities).map(String),
    outcome: str(data.outcome),
    busyWith:
      data.busy_with && typeof data.busy_with === 'object'
        ? data.busy_with
        : null,
  };
  const rowState = SETTLED_BY_SLIP.has(row.state)
    ? row.state
    : resultState(data);
  return setRow(next, callId, {
    ...row,
    state: rowState,
    endAt: at ?? null,
    result,
    busy: result.outcome === 'busy' || Boolean(result.busyWith),
    busyWith: result.busyWith,
  });
}

function onUi(state, data, at, seq, replay) {
  const action = str(data.action);
  if (!action) return state;
  const directive = {
    kind: 'directive',
    action,
    ids: arr(data.ids).map(String),
    note: str(data.note),
    vehicle: str(data.vehicle),
    reason: str(data.reason),
    id: str(data.id),
    at: at ?? null,
    seq,
    replay: Boolean(replay),
  };
  let next = { ...state, directives: [...state.directives, directive] };
  const turnId = next.running || lastTurnId(next);
  if (turnId && next.turns[turnId]) {
    next = withTurn(next, turnId, (turn) => pushBlock(turn, directive, at));
  } else {
    next = { ...next, items: [...next.items, directive] };
  }
  return next;
}

function lastTurnId(state) {
  for (let i = state.items.length - 1; i >= 0; i -= 1) {
    if (state.items[i].kind === 'turn') return state.items[i].id;
  }
  return null;
}

function onUsage(state, data, at) {
  const turnId = str(data.turn_id);
  const usage = { ...state.usage };
  if (turnId) {
    const prev = usage.turns[turnId] || {};
    const merged = { ...prev };
    for (const key of ['cost_usd', 'input_tokens', 'output_tokens']) {
      if (Number.isFinite(data[key])) merged[key] = data[key];
    }
    usage.turns = { ...usage.turns, [turnId]: merged };
  }
  if (Number.isFinite(data.cost_usd)) usage.sessionCost += data.cost_usd;
  if (Number.isFinite(data.session_cost_usd))
    usage.serverSessionCost = data.session_cost_usd;
  if (Object.hasOwn(data, 'rate_limit')) {
    usage.rateLimit =
      data.rate_limit && typeof data.rate_limit === 'object'
        ? { ...data.rate_limit, at: at ?? null }
        : null;
  }
  return { ...state, usage };
}

function onTurnEnd(state, data, at) {
  const id = str(data.turn_id) || state.running;
  if (!id || !state.turns[id]) {
    return state.running === id ? { ...state, running: null } : state;
  }
  const stop = str(data.stop) || 'end';
  const interruptedAt = state.interruptedAt;
  let next = withTurn(state, id, (turn) => {
    let blocks = closeThinking(turn.blocks, at);
    if (stop === 'interrupted') {
      blocks = [
        ...blocks,
        { kind: 'stopped', at: interruptedAt ?? at ?? null },
      ];
    }
    return {
      ...turn,
      status: stop,
      endAt: at ?? null,
      error: str(data.error),
      blocks,
    };
  });
  // Rows that never got a result: honest about not knowing.
  const rows = { ...next.rows };
  let rowsChanged = false;
  for (const block of next.turns[id].blocks) {
    if (block.kind !== 'tool') continue;
    const row = rows[block.callId];
    if (!row || !OPEN_ROW.has(row.state)) continue;
    rows[block.callId] = {
      ...row,
      state: row.state === 'awaiting' ? 'cancelled' : 'no_result',
      endAt: at ?? null,
    };
    rowsChanged = true;
  }
  if (rowsChanged) next = { ...next, rows };
  // Defensive: a turn cannot end with a live approval.
  const approvals = { ...next.approvals };
  let approvalsChanged = false;
  for (const aid of next.approvalOrder) {
    const a = approvals[aid];
    if (a.turnId === id && (a.state === 'pending' || a.state === 'deciding')) {
      approvals[aid] = {
        ...a,
        state: 'cancelled',
        resolvedAt: at ?? null,
        cancelCause: stop === 'interrupted' ? 'interrupt' : 'turn_end',
      };
      approvalsChanged = true;
    }
  }
  if (approvalsChanged) next = { ...next, approvals };
  return {
    ...next,
    running: next.running === id ? null : next.running,
    interruptedAt: null,
    turnsEnded: next.turnsEnded + 1,
  };
}

const SIGN_IN = /sign in|signed in|log ?in|authenticat/i;

function onError(state, data, at) {
  const block = {
    kind: 'error',
    message: str(data.message) || 'The analyst reported an error.',
    hint: str(data.hint),
    retryable: data.retryable === true,
    at: at ?? null,
  };
  let next = state;
  if (SIGN_IN.test(`${block.message} ${block.hint || ''}`)) {
    next = { ...next, auth: 'failed' };
  }
  if (next.running && next.turns[next.running]) {
    return withTurn(next, next.running, (turn) => pushBlock(turn, block, at));
  }
  return { ...next, items: [...next.items, block] };
}

function onEvent(state, action) {
  const { name } = action;
  const data =
    action.data && typeof action.data === 'object' ? action.data : {};
  const seq = Number.isFinite(action.seq) ? action.seq : 0;
  const at = Number.isFinite(action.at) ? action.at : null;
  const replay = Boolean(action.replay);
  let next = state;
  if (seq > 0) {
    if (seq <= state.lastSeq) return state; // duplicate id: already folded
    if (state.lastSeq > 0 && seq > state.lastSeq + 1 && name !== 'session') {
      const covered = state.gaps.some((g) => g.afterSeq === state.lastSeq);
      if (!covered) {
        next = {
          ...next,
          gaps: [
            ...next.gaps,
            {
              afterSeq: state.lastSeq,
              beforeSeq: seq,
              fromAt: state.connection.lostAt ?? state.lastEventAt,
              toAt: at,
            },
          ],
          items: [...next.items, { kind: 'gap', index: next.gaps.length }],
        };
      }
    }
    next = { ...next, lastSeq: seq };
    if (next.connection.reconnecting && name !== 'session') {
      // Back without a `session` event first: judge by the sequence alone.
      const lost = next.gaps.length > state.gaps.length;
      next = {
        ...next,
        notice: lost
          ? { kind: 'gap', fromAt: state.connection.lostAt, toAt: at }
          : { kind: 'reconnected', at },
        connection: { ...next.connection, reconnecting: false, lostAt: null },
      };
    }
  }
  if (at != null) next = { ...next, lastEventAt: at };
  switch (name) {
    case 'session':
      return onSession(next, data, at);
    case 'turn_start':
      return onTurnStart(next, data, at, replay);
    case 'text_delta':
      return onStreamText(next, data, at, replay, 'text');
    case 'thinking':
      return onStreamText(next, data, at, replay, 'thinking');
    case 'tool_call':
      return onToolCall(next, data, at, replay);
    case 'approval_request':
      return onApprovalRequest(next, data, at, seq, replay);
    case 'approval_resolved':
      return onApprovalResolved(next, data, at);
    case 'tool_result':
      return onToolResult(next, data, at, replay);
    case 'ui':
      return onUi(next, data, at, seq, replay);
    case 'usage':
      return onUsage(next, data, at);
    case 'turn_end':
      return onTurnEnd(next, data, at);
    case 'error':
      return onError(next, data, at);
    default:
      return next;
  }
}

// ---- local actions -------------------------------------------------------------

function onDecision(state, action) {
  const a = state.approvals[action.id];
  if (!a || a.state !== 'pending') return state;
  return {
    ...state,
    approvals: {
      ...state.approvals,
      [a.id]: {
        ...a,
        state: 'deciding',
        localDecision: action.decision,
        note: str(action.note) || null,
        decidedAt: num(action.at),
        error: null,
      },
    },
  };
}

function onDecisionOk(state, action) {
  const a = state.approvals[action.id];
  if (!a) return state;
  if (a.localDecision === 'approve_session' && grantOffered(a)) {
    return {
      ...state,
      grants: addGrants(state.grants, grantScopeOf(a), a.decidedAt, false),
    };
  }
  return state;
}

function onDecisionFailed(state, action) {
  const a = state.approvals[action.id];
  if (!a || a.state !== 'deciding') return state;
  return {
    ...state,
    approvals: {
      ...state.approvals,
      [a.id]: {
        ...a,
        state: 'pending',
        localDecision: null,
        error: str(action.error) || 'The decision did not reach the analyst.',
      },
    },
  };
}

function onNewSession(state, action) {
  const at = num(action.at);
  const approvals = { ...state.approvals };
  for (const id of state.approvalOrder) {
    const a = approvals[id];
    if (a.state === 'pending' || a.state === 'deciding') {
      approvals[id] = {
        ...a,
        state: 'cancelled',
        resolvedAt: at,
        cancelCause: 'session',
      };
    }
  }
  const rows = { ...state.rows };
  for (const [callId, row] of Object.entries(rows)) {
    if (OPEN_ROW.has(row.state)) {
      rows[callId] = {
        ...row,
        state: row.state === 'awaiting' ? 'cancelled' : 'no_result',
      };
    }
  }
  const turns = { ...state.turns };
  if (state.running && turns[state.running]) {
    turns[state.running] = {
      ...turns[state.running],
      status: 'interrupted',
      endAt: at,
      blocks: closeThinking(turns[state.running].blocks, at),
    };
  }
  return {
    ...state,
    session: null,
    lastSeq: 0,
    approvals,
    rows,
    turns,
    running: null,
    grants: {},
    grantsSupported: state.grantsSupported,
    outbox: null,
    pendingContext: [],
    interruptedAt: null,
    notice: null,
    connection: {
      state: state.connection.state,
      lostAt: null,
      reconnecting: false,
    },
    items: state.items.length
      ? [
          ...state.items,
          { kind: 'divider', reason: action.reason || 'operator', at },
        ]
      : state.items,
  };
}

export function reduce(state, action) {
  const s = state || initialState();
  if (!action || typeof action !== 'object') return s;
  switch (action.type) {
    case 'event':
      return onEvent(s, action);
    case 'connection': {
      const conn = { ...s.connection, state: action.state };
      if (action.state === 'reconnecting' && !s.connection.reconnecting) {
        conn.reconnecting = s.connection.state === 'open' || s.lastSeq > 0;
        conn.lostAt = s.lastEventAt ?? num(action.at);
      }
      return { ...s, connection: conn };
    }
    case 'availability':
      return {
        ...s,
        availability: {
          available: action.available === true,
          reason: str(action.reason),
          hint: str(action.hint),
          model: str(action.model),
        },
        auth: action.available === true && action.clearAuth ? null : s.auth,
      };
    case 'send': {
      const text = String(action.text || '').trim();
      const focusedIds = arr(action.focused_ids).map(String);
      return {
        ...s,
        outbox: {
          text,
          focusedIds,
          status: 'sending',
          at: num(action.at),
          error: null,
        },
        pendingContext: [...s.pendingContext, { text, focusedIds }],
      };
    }
    case 'send_ok':
      if (!s.outbox) return s;
      // turn_start may already have cleared the outbox; otherwise wait for it.
      return {
        ...s,
        outbox: { ...s.outbox, status: 'sent', turnId: str(action.turn_id) },
      };
    case 'send_failed': {
      if (!s.outbox) return s;
      const text = s.outbox.text;
      return {
        ...s,
        outbox: {
          ...s.outbox,
          status: 'failed',
          error: str(action.error),
          code: str(action.code),
        },
        pendingContext: s.pendingContext.filter((p) => p.text !== text),
      };
    }
    case 'clear_outbox':
      return { ...s, outbox: null };
    case 'interrupt':
      return s.running ? { ...s, interruptedAt: num(action.at) } : s;
    case 'decision':
      return onDecision(s, action);
    case 'decision_ok':
      return onDecisionOk(s, action);
    case 'decision_failed':
      return onDecisionFailed(s, action);
    case 'grants': {
      if (action.grants == null) return { ...s, grantsSupported: false };
      const grants = {};
      for (const g of arr(action.grants)) {
        const tool = str(g?.tool);
        if (tool)
          grants[tool] = { tool, since: num(g.since_ms), confirmed: true };
      }
      return { ...s, grants, grantsSupported: true };
    }
    case 'revoke': {
      if (!s.grants[action.tool]) return s;
      const grants = { ...s.grants };
      delete grants[action.tool];
      return { ...s, grants };
    }
    case 'new_session':
      return onNewSession(s, action);
    case 'notice_seen':
      return { ...s, notice: null };
    default:
      return s;
  }
}

/** Fold a list of actions (tests, replays). */
export function foldAll(actions, state = initialState()) {
  return actions.reduce(reduce, state);
}

// ---- selectors -----------------------------------------------------------------

/** Waiting approvals, oldest first. */
export function pendingApprovals(state) {
  const out = [];
  for (const id of state.approvalOrder) {
    const a = state.approvals[id];
    if (a && (a.state === 'pending' || a.state === 'deciding')) out.push(a);
  }
  return out;
}

export function sessionCost(state) {
  return state.usage.serverSessionCost ?? state.usage.sessionCost;
}

/** Granted tools, in grant order. */
export function activeGrants(state) {
  return Object.values(state.grants).sort(
    (a, b) => (a.since ?? 0) - (b.since ?? 0),
  );
}

/** `{level:'ok'|'warning'|'reached', resetsAt}` from the last rate-limit report. */
export function rateLimit(state, now = null) {
  const rl = state.usage.rateLimit;
  if (!rl) return { level: 'ok', resetsAt: null };
  const resetsAt = epochMs(rl.resets_at);
  if (resetsAt != null && Number.isFinite(now) && now >= resetsAt) {
    return { level: 'ok', resetsAt: null };
  }
  if (rl.status === 'rejected') return { level: 'reached', resetsAt };
  if (rl.status === 'allowed_warning') return { level: 'warning', resetsAt };
  return { level: 'ok', resetsAt };
}

/** Whether the analyst can be used at all (spec §3e). */
export function availabilityOf(state) {
  if (state.auth === 'failed') return { available: false, reason: 'auth' };
  const av = state.availability;
  if (!av) return { available: null, reason: null };
  return { available: av.available, reason: av.reason, hint: av.hint };
}

/** Header status word (spec §6.1). */
export function statusWord(state, now = null) {
  const av = availabilityOf(state);
  if (av.reason === 'auth') return 'Not signed in';
  if (av.available === false) return 'Unavailable';
  if (
    state.connection.reconnecting ||
    state.connection.state === 'reconnecting'
  )
    return 'Reconnecting';
  if (pendingApprovals(state).length) return 'Waiting for your approval';
  if (rateLimit(state, now).level === 'reached') return 'Usage limit reached';
  const running = state.running && state.turns[state.running];
  if (running || state.outbox?.status === 'sending') {
    if (!running) return state.turnsEnded === 0 ? 'Starting' : 'Thinking';
    const last = running.blocks[running.blocks.length - 1];
    if (!last) return state.turnsEnded === 0 ? 'Starting' : 'Thinking';
    if (last.kind === 'text') return 'Answering';
    return 'Thinking';
  }
  if (rateLimit(state, now).level === 'warning')
    return 'Close to the usage limit';
  if (av.available == null && !state.session) return 'Starting';
  return 'Ready';
}

/** Rows of a vehicle's latest command in the transcript (auto-track guard). */
export function latestCommandRow(state, vehicle) {
  let best = null;
  for (const row of Object.values(state.rows)) {
    if (row.klass !== 'command') continue;
    if (callVehicle(row.tool, row.args) !== vehicle) continue;
    if (!best || (row.at ?? 0) >= (best.at ?? 0)) best = row;
  }
  return best;
}

/** Approvals approved after `sinceMs` (validate.assess input). */
export function approvedSince(state, sinceMs, excludeId = null) {
  // Without a reference time the order of events is unknown: claim nothing.
  if (!Number.isFinite(sinceMs)) return [];
  const out = [];
  for (const id of state.approvalOrder) {
    const a = state.approvals[id];
    if (!a || a.id === excludeId || a.state !== 'approved') continue;
    const at = a.resolvedAt ?? a.decidedAt;
    if (!Number.isFinite(at) || at <= sinceMs) continue;
    out.push({
      id: a.id,
      tool: a.tool,
      klass: a.klass,
      title: a.title,
      vehicle: approvalVehicle(a),
      at: at ?? null,
    });
  }
  return out;
}
