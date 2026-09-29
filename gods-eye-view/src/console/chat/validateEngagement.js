/**
 * Pure checks for the engagement slip (WG spec §3.5, §3.6, §5.3.7; M14a).
 *
 * An engagement slip can be approved only when all of these hold:
 * - its preview is present and complete (`engagementPreviewProblem`);
 * - this console holds the engagement approval key (`consoleProblem`);
 * - `assessEngagement` doesn't find it blocked.
 * Any failure makes the slip Deny-only. The server re-runs every gate at
 * authorize and execute, so the console only ever refuses earlier.
 *
 * `assessEngagement(approval, graph, opts)` runs at render, on store changes
 * and at the press.
 * - **Blocked** on a failing preview check, a target that isn't a scenario
 *   unit or is protected, an inactive (or replaced) wargame session, or, in
 *   Umpire view, a correlated force that is already destroyed.
 * - **Stale** when the target track moved more than 250 m since the request,
 *   or when sim speed or weather was approved since the request.
 *
 * No DOM, no globals.
 */

import { listWords, stripBidi, truncate } from './format.js';

/** Where the view keeps the `engagement` preview on the approval. */
export const ENGAGEMENT_PREVIEW_FIELD = 'engagementPreview';

/** Required preview keys (WG spec §3.6 Deny-only table). */
export const REQUIRED_ENGAGEMENT_KEYS = Object.freeze([
  'checks',
  'target',
  'attacker',
  'p_notional',
]);

/** A target track that moved further than this since the request is stale. */
export const MOVED_STALE_M = 250;

/** Approved sim changes after which an engagement slip is stale. */
export const STALING_ENGAGEMENT_TOOLS = Object.freeze({
  sim_set_time_scale: 'sim speed changed',
  sim_set_weather: 'the weather changed',
});

/** The masked red attacker in Blue view (WG spec §3.2 fog). */
export const MASKED_ATTACKER = 'Red air defence (not identified)';

export const ENGAGEMENT_COPY = Object.freeze({
  blocked: (reason) => `This can't be approved here: ${reason}.`,
  ended: 'The wargame has ended.',
  destroyed: (target) => `${target} is already destroyed in this wargame.`,
  claimed:
    "This console can't approve engagements: another client claimed them.",
  noKey:
    "This console can't approve engagements: it holds no engagement approval key.",
});

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const isObj = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);

/** Whether an approval is an engagement (the class, never the tool name). */
export function isEngagement(approval) {
  return approval?.klass === 'engagement';
}

/** The engagement preview on an approval, or null. */
export function engagementPreviewOf(approval) {
  const p = approval?.[ENGAGEMENT_PREVIEW_FIELD];
  return isObj(p) ? p : null;
}

/** Shape checks for the keys the slip computes with. */
const KEY_SHAPE = Object.freeze({
  checks: Array.isArray,
  target: (v) => isObj(v) && str(v.label) != null,
  attacker: isObj,
  p_notional: (v) => isObj(v) && isNum(v.effect),
});

/**
 * Null when the preview is usable, else `{code:'missing'}` or
 * `{code:'key', key}` (a required key absent or malformed).
 */
export function engagementPreviewProblem(approval) {
  const p = engagementPreviewOf(approval);
  if (!p) return { code: 'missing' };
  for (const key of REQUIRED_ENGAGEMENT_KEYS) {
    if (!Object.hasOwn(p, key) || p[key] == null) return { code: 'key', key };
    if (!KEY_SHAPE[key](p[key])) return { code: 'key', key };
  }
  return null;
}

/**
 * Null when this console may approve engagements, else `{code, line}`.
 * `access` is `{held, refused}` from the chat client; anything else (no
 * access information at all) counts as no key: the console fails safe.
 */
export function consoleProblem(access) {
  if (isObj(access) && access.held === true) return null;
  if (isObj(access) && access.refused === true)
    return { code: 'claimed', line: ENGAGEMENT_COPY.claimed };
  return { code: 'no_key', line: ENGAGEMENT_COPY.noKey };
}

/** The target's generic label, bidi-free and short, or a fallback. */
export function targetLabel(preview) {
  const label = str(preview?.target?.label);
  return label ? truncate(stripBidi(label), 80) : 'the target';
}

/** Great-circle distance in metres between two `[lat, lon]` points. */
export function distanceM(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b)) return null;
  if (![a[0], a[1], b[0], b[1]].every(isNum)) return null;
  const rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad;
  const dLon = (b[1] - a[1]) * rad;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(s)));
}

function nodesOf(graph) {
  return Array.isArray(graph?.nodes) ? graph.nodes.filter(isObj) : [];
}

function pointOf(node) {
  const lat = node?.lat ?? node?.attrs?.lat;
  const lon = node?.lon ?? node?.attrs?.lon;
  return isNum(lat) && isNum(lon) ? [lat, lon] : null;
}

/** The graph id of the preview's target track. */
export function targetGraphId(preview) {
  const t = preview?.target;
  return str(t?.graph_id) || (str(t?.track_id) ? `trk:${t.track_id}` : null);
}

/** Checks from the preview that didn't pass, as their text. */
function failedCheckTexts(preview) {
  const checks = Array.isArray(preview?.checks) ? preview.checks : [];
  return checks
    .filter((c) => !isObj(c) || c.ok !== true)
    .map((c) => str(c?.text) || 'an unnamed check');
}

/** Blocking reasons, in the order the slip names them. */
function blockingReasons(preview, graph, requestSession) {
  const reasons = [];
  for (const text of failedCheckTexts(preview)) {
    reasons.push({ code: 'check', text: `the check “${text}” failed` });
  }
  const target = preview.target || {};
  if (target.scenario !== true) {
    reasons.push({
      code: 'not_scenario',
      text: 'the target is not a simulated scenario unit',
    });
  }
  if (target.protected === true) {
    reasons.push({ code: 'protected', text: 'the target is marked protected' });
  }
  const wg = graph?.meta?.wargame;
  if (isObj(wg)) {
    const replaced =
      str(requestSession) &&
      str(wg.session_id) &&
      wg.session_id !== requestSession;
    if (wg.active !== true || replaced) {
      reasons.push({ code: 'inactive', line: ENGAGEMENT_COPY.ended });
    } else if (wg.truth_view === true) {
      const id = targetGraphId(preview);
      const gone = nodesOf(graph).some(
        (n) =>
          n.type === 'force' &&
          n.attrs?.state === 'destroyed' &&
          Array.isArray(n.attrs?.correlated) &&
          n.attrs.correlated.includes(id),
      );
      if (id && gone) {
        reasons.push({
          code: 'destroyed',
          line: ENGAGEMENT_COPY.destroyed(targetLabel(preview)),
        });
      }
    }
  }
  return reasons;
}

/** Stale reasons: the target moved, or sim speed or weather changed. */
function staleReasons(preview, graph, approvedSince) {
  const reasons = [];
  const id = targetGraphId(preview);
  const node = id ? nodesOf(graph).find((n) => n.id === id) : null;
  const was = [preview.target?.lat, preview.target?.lon];
  const moved = node ? distanceM(was, pointOf(node)) : null;
  if (moved != null && moved > MOVED_STALE_M) {
    reasons.push({
      code: 'moved',
      text: `the target moved about ${Math.round(moved)} m`,
      metres: moved,
    });
  }
  const seen = new Set();
  for (const a of Array.isArray(approvedSince) ? approvedSince : []) {
    const tool = a?.tool;
    if (!Object.hasOwn(STALING_ENGAGEMENT_TOOLS, tool) || seen.has(tool))
      continue;
    seen.add(tool);
    reasons.push({ code: tool, text: STALING_ENGAGEMENT_TOOLS[tool] });
  }
  return reasons;
}

/** The "Conditions changed" line for a stale engagement slip. */
export function staleLine(reasons) {
  const words = (Array.isArray(reasons) ? reasons : [])
    .map((r) => r?.text)
    .filter(Boolean);
  if (!words.length) return '';
  return `Conditions changed since this was proposed: ${listWords(words)}. The numbers above may be out of date.`;
}

/**
 * The console's own view of an engagement request.
 * @param {object} approval the approval, with `engagementPreview`
 * @param {object|null} graph the intel graph
 * @param {{approvedSince?: Array<{tool:string}>, requestSession?: string|null}} [opts]
 *   `approvedSince`: approvals approved after the request arrived
 *   (reducer.approvedSince); `requestSession`: the wargame session id when
 *   the request arrived (a different active session means this one ended).
 * @returns {{state:'none'|'blocked'|'stale', ok:boolean,
 *   reasons:Array<{code:string, text?:string, line?:string}>,
 *   stale:Array<{code:string, text:string}>, line:string|null}}
 */
export function assessEngagement(
  approval,
  graph,
  { approvedSince = [], requestSession = null } = {},
) {
  const preview = engagementPreviewOf(approval);
  if (!preview || engagementPreviewProblem(approval)) {
    return {
      state: 'blocked',
      ok: false,
      reasons: [{ code: 'preview' }],
      stale: [],
      line: null,
    };
  }
  const blocking = blockingReasons(preview, graph, requestSession);
  if (blocking.length) {
    const first = blocking[0];
    return {
      state: 'blocked',
      ok: false,
      reasons: blocking,
      stale: [],
      line: first.line || ENGAGEMENT_COPY.blocked(first.text),
    };
  }
  const stale = staleReasons(preview, graph, approvedSince);
  return {
    state: stale.length ? 'stale' : 'none',
    ok: true,
    reasons: stale,
    stale,
    line: stale.length ? staleLine(stale) : null,
  };
}

/**
 * Why an engagement slip can only be denied, or null (the order is the
 * spec's: preview, console key, then the console's own assessment).
 * @returns {{kind:'preview'|'console'|'blocked', line:string|null}|null}
 */
export function engagementDenyOnly(approval, assessment, access) {
  if (engagementPreviewProblem(approval))
    return { kind: 'preview', line: null };
  const key = consoleProblem(access);
  if (key) return { kind: 'console', line: key.line };
  if (assessment?.state === 'blocked')
    return { kind: 'blocked', line: assessment.line || null };
  return null;
}
