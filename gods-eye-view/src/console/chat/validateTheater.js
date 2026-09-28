/**
 * Pure checks for the theater and sim-speed slips (WG spec §3.6, §4.2.2).
 *
 * `previewProblem(approval)` implements the Deny-only rule: a slip whose
 * preview is missing or lacks a required key can't be approved.
 * `assessTheater(approval, graph, opts?)` is the console's own view of the
 * switch refusals: it runs at render, on store changes and at the press, and
 * fails when any vehicle is airborne, busy, BINGO-latched or link-lost, or
 * when `graph.theater.epoch` changed since the request. The server checks the
 * same things again; the console only refuses earlier, never later.
 *
 * No DOM, no globals.
 */

import { listWords } from './format.js';
import { vehicleFacts } from './validate.js';

/** Preview key per tool, as the reducer stores it on the approval. */
export const PREVIEW_FIELD = Object.freeze({
  sim_set_theater: 'theaterPreview',
  sim_set_time_scale: 'timeScalePreview',
});

/** Required keys per preview (WG spec §3.6 Deny-only table). */
export const REQUIRED_PREVIEW_KEYS = Object.freeze({
  sim_set_theater: Object.freeze([
    'checks',
    'center',
    'bbox',
    'home',
    'airframe',
    'ground_msl_m',
  ]),
  sim_set_time_scale: Object.freeze(['checks', 'from', 'to']),
});

export const PREVIEW_MISSING =
  "The console couldn't build this preview, so it can't be approved.";

/** Whether a tool has a preview-driven slip (theater or sim speed). */
export function isPreviewTool(tool) {
  return typeof tool === 'string' && Object.hasOwn(PREVIEW_FIELD, tool);
}

/** The preview object on an approval, or null. */
export function previewOf(approval) {
  if (!approval || !isPreviewTool(approval.tool)) return null;
  const p = approval[PREVIEW_FIELD[approval.tool]];
  return p && typeof p === 'object' && !Array.isArray(p) ? p : null;
}

const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const numbers = (v, n) =>
  Array.isArray(v) && v.length === n && v.every((x) => finite(x));
const object = (v) => Boolean(v) && typeof v === 'object' && !Array.isArray(v);

/** Shape checks for the keys the slip computes with (numbers stay numbers). */
const KEY_SHAPE = Object.freeze({
  checks: Array.isArray,
  center: (v) => numbers(v, 2),
  bbox: (v) => numbers(v, 4),
  home: object,
  airframe: object,
  ground_msl_m: finite,
  from: finite,
  to: finite,
});

/**
 * Null when the preview is usable, else `{code, key?}`:
 * `missing` (no preview object) or `key` (a required key absent or malformed).
 */
export function previewProblem(approval) {
  if (!approval || !isPreviewTool(approval.tool)) return null;
  const p = previewOf(approval);
  if (!p) return { code: 'missing' };
  for (const key of REQUIRED_PREVIEW_KEYS[approval.tool]) {
    if (!Object.hasOwn(p, key) || p[key] == null) return { code: 'key', key };
    const shape = KEY_SHAPE[key];
    if (shape && !shape(p[key])) return { code: 'key', key };
  }
  return null;
}

/** Checks from a preview that failed (`ok !== true`), as `{text}`. */
export function failedChecks(preview) {
  const checks = Array.isArray(preview?.checks) ? preview.checks : [];
  return checks
    .filter((c) => !c || c.ok !== true)
    .map((c) => ({
      text:
        c && typeof c.text === 'string' && c.text.trim()
          ? c.text.trim()
          : 'an unnamed check',
    }));
}

/** Mission phases that mean a vehicle has a task running. */
const BUSY_PHASES = new Set(['planning', 'executing', 'rtb']);
/** Link states that mean the link is down or lost (format.js linkWord). */
const LINK_DOWN = new Set(['lost', 'loal', 'pending']);

/**
 * Every vehicle node in the graph as console facts plus `busy`.
 * @returns {Array<object>} `[{name, landed, fuel_pct, bingo_latched, link,
 *   eta_to_bingo_s, busy, ...}]`
 */
export function fleetOf(graph) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const busyBy = new Set();
  for (const n of nodes) {
    if (!n || n.type !== 'mission') continue;
    const vehicle = n.attrs?.vehicle;
    if (typeof vehicle === 'string' && BUSY_PHASES.has(n.attrs?.phase))
      busyBy.add(vehicle);
  }
  const out = [];
  for (const n of nodes) {
    if (!n || n.type !== 'vehicle') continue;
    const facts = vehicleFacts(n);
    if (!facts) continue;
    out.push({
      ...facts,
      busy: Boolean(facts.mission) || busyBy.has(facts.name),
    });
  }
  return out;
}

/**
 * The console's own switch refusals.
 * @param {object} approval reducer approval
 * @param {object|null} graph intel graph
 * @param {{requestEpoch?: number|null}} [opts] the theater epoch when the
 *   request arrived (the view records it; unknown for a replayed request)
 * @returns {{ok: boolean, reasons: Array<{code, vehicle?, text}>,
 *   airborne: string[], line: string|null}}
 */
export function assessTheater(approval, graph, opts = {}) {
  const reasons = [];
  const airborne = [];
  if (approval?.tool !== 'sim_set_theater') {
    return { ok: true, reasons, airborne, line: null };
  }
  for (const v of fleetOf(graph)) {
    const name = v.name || 'A drone';
    if (v.landed === false) {
      reasons.push({
        code: 'airborne',
        vehicle: name,
        text: `${name} is airborne`,
      });
      airborne.push(name);
    }
    if (v.busy)
      reasons.push({
        code: 'busy',
        vehicle: name,
        text: `${name} has a task running`,
      });
    if (v.bingo_latched === true)
      reasons.push({
        code: 'bingo',
        vehicle: name,
        text: `${name} has BINGO latched`,
      });
    if (LINK_DOWN.has(v.link))
      reasons.push({
        code: 'link',
        vehicle: name,
        text: `${name} has lost its link`,
      });
  }
  const theater = graph?.theater;
  if (theater?.state === 'switching')
    reasons.push({ code: 'switching', text: 'a theater switch is running' });
  const epoch = theater?.epoch;
  const asked = opts?.requestEpoch;
  if (finite(epoch) && finite(asked) && epoch !== asked)
    reasons.push({
      code: 'epoch',
      text: 'the theater changed after this request',
    });
  const ok = reasons.length === 0;
  return {
    ok,
    reasons,
    airborne,
    line: ok ? null : lineFor(reasons, airborne),
  };
}

function lineFor(texts, airborne) {
  const words = listWords(
    texts.map((r) => (typeof r === 'string' ? r : r.text)),
  );
  const land = airborne.length
    ? ` Land ${listWords(airborne)} first, then ask again.`
    : '';
  return `This can't be approved now: ${words}.${land}`;
}

/**
 * The Deny-only line for a blocked theater or speed slip, or null when it
 * can be approved: "This can't be approved now: {reason}. Land {vehicle}
 * first, then ask again." (the Land clause only for airborne vehicles).
 */
export function blockedLine(approval, assessment) {
  if (!approval || !isPreviewTool(approval.tool)) return null;
  const texts = [];
  let airborne = [];
  const t = approval.tool === 'sim_set_theater' ? assessment?.theater : null;
  if (t && t.ok === false) {
    for (const r of Array.isArray(t.reasons) ? t.reasons : [])
      if (r && typeof r.text === 'string') texts.push(r.text);
    airborne = Array.isArray(t.airborne) ? t.airborne.map(String) : [];
    if (!texts.length) texts.push('the console found a problem');
  }
  const failed = failedChecks(previewOf(approval));
  if (failed.length === 1) texts.push(`a check failed: ${failed[0].text}`);
  else if (failed.length > 1)
    texts.push(`checks failed: ${failed.map((c) => c.text).join('; ')}`);
  return texts.length ? lineFor(texts, airborne) : null;
}
