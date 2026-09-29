/**
 * The order slip (UX spec §6.5–§6.8): the one light surface in the console,
 * shown inline in the transcript for every approval request.
 *
 * Every slip answers, in order: What happens / Based on / How to stop or undo
 * it, and only then offers a decision. Deny is first in DOM, visual and tab
 * order. Approve is ink-filled with a verb and is ARMED only after 800 ms
 * without a layout shift, a checkbox change, a validation change or a return
 * to the tab. A press counts only for a pointer down+up on the same armed
 * button with `detail === 1` that is not followed within DBLCLICK_MS by a
 * second click (a double-click can never approve, so a pointer approval waits
 * out the double-click interval), or Enter/Space on the focused armed button
 * without key auto-repeat. There is no approval shortcut; ⌘Enter in the note
 * field DENIES with the note. The slip re-validates at press time and
 * swallows a press on a slip that just went stale.
 *
 * `createSlip(model, deps)` → { el, update(model), tick(now), rearm(reason),
 *   review(), focusHeading(), isArmed(), destroy(), variant }
 *
 * model: { approval, assessment, vehicle, before, queue:{index,total},
 *          conflicts:[{title}], caveats:[str], detections:{ok, at_ms}|null,
 *          fleet:[vehicle facts + busy] (theater and speed slips),
 *          now, reducedMotion }
 *
 * Deny-only (WG spec §3.6, §4.2.1, §4.2.2): an unknown approval class, a
 * theater or speed slip whose preview is missing or incomplete, or one that
 * is blocked (a failing check, or `assessment.theater.ok === false`) shows
 * only [Deny], with the reason on a line above it. No Approve element exists.
 * `denyOnlyOf(approval, assessment, access)` is the single decision point.
 *
 * Engagement (WG spec §5.3.7, M14a): a Sand hatch over the band, the body
 * from slipEngagement.js, the acknowledgement box always, Approve armed
 * 1600 ms after the box is ticked (unticking disarms), and `decide` gets
 * `{acknowledged}`. Deny-only also when the preview is incomplete, when
 * this console holds no engagement key (`model.console` or
 * `deps.consoleAccess()`: `{held, refused}`), or when `assessEngagement`
 * finds it blocked. The model adds `console` and `outcome` (the filed
 * slip's live outcome line).
 * deps:  { decide(decision, note, {acknowledged}) → Promise,
 *          revalidate() → assessment, consoleAccess() → {held, refused},
 *          announce(text, {assertive}), clock:{setTimeout, clearTimeout, now},
 *          raf, caf, ResizeObserver, getRect(el), doc }
 */

import { h as domH, replaceKids, setHidden } from '../../ui/uavDom.js';
import {
  ICON,
  STALING_TOOLS,
  TOOL_TITLES,
  SENSOR_TOOLS,
  ago,
  approvalVehicle,
  approveVerb,
  bidiSafe,
  capitalize,
  classApprovable,
  classMeta,
  coord,
  countdown,
  escapeBidi,
  isDryRunnable,
  kindLabel,
  withArticle,
  linkWord,
  listWords,
  missionKind,
  policyLine,
  segments,
  spanText,
  stripBidi,
  toolTitle,
  warningText,
  zulu,
} from './format.js';
import { grantOffered, grantScopeOf } from './reducer.js';
import { staleSummary } from './validate.js';
import {
  unknownDenyNote,
  unknownInfoNodes,
  unknownLine,
} from './slipUnknown.js';
import {
  PREVIEW_MISSING,
  blockedLine,
  isPreviewTool,
  previewProblem,
} from './validateTheater.js';
import {
  theaterInfoNodes,
  theaterPlace,
  timeScaleInfoNodes,
} from './slipTheater.js';
import {
  engagementDenyOnly,
  engagementPreviewOf,
  isEngagement,
} from './validateEngagement.js';
import {
  ENGAGEMENT_SLIP_COPY,
  ENGAGE_ARM_MS,
  engagementAck,
  engagementInfoNodes,
  engagementTitle,
  verbKindOf,
} from './slipEngagement.js';

// Titles, summaries, consequences and args carry analyst- and map-sourced
// text: no bidi control reaches the slip (see format.js stripBidi).
const h = bidiSafe(domH);

export const ARM_MS = 800;
/** The platform double-click interval: a detail-1 pointer approval waits this
 *  long and is cancelled by a second press, a detail≥2 click or a dblclick. */
export const DBLCLICK_MS = 500;
export const LAYOUT_SLOP_PX = 4;
export const WARN_MS = 60_000;

/** Notes sent with the deny-type primaries (spec §6.7). */
export const SLIP_NOTES = Object.freeze({
  askDryRun: 'Dry-run this first and show me the gate.',
  replan:
    'The dry run failed the gate. Re-plan, dry-run again and show me the gate.',
  freshDryRun:
    'Conditions changed since the dry run. Dry-run this again and show me the gate.',
  freshPlan: ENGAGEMENT_SLIP_COPY.freshPlanNote,
});

const KNOWN_GATE_KEYS = new Set([
  'ok',
  'required_pct',
  'available_pct',
  'plan_fuel_pct',
  'return_fuel_pct',
  'reserve_pct',
  'bingo_latched',
  'bingo_fuel_pct',
  'envelope_violations',
  'warnings',
  'est_time_s',
  'est_distance_m',
  'envelope',
]);

const ASSUMED_HOME =
  "The gate was priced from home, not from the vehicle's live position; the ingress leg may be understated.";

const fixed1 = (n) => Number(n).toFixed(1);
const isNum = (n) => Number.isFinite(n);

function icon(name, extra = '') {
  return h(
    'span',
    {
      class: `material-symbols-outlined ic-icon${extra ? ` ${extra}` : ''}`,
      'aria-hidden': 'true',
    },
    name,
  );
}

function tag(register, text = register) {
  return h(
    'span',
    { class: 'ic-tag', 'data-register': register.toLowerCase() },
    text,
  );
}

/** Write text only when it changed, without bidi controls. */
function setText(el, text) {
  const next = stripBidi(text == null ? '' : String(text));
  if (el.textContent !== next) el.textContent = next;
}

/** Server strings split into spaced segments with hidden commas (spec §11.2). */
export function segmentNodes(text, cls = 'ic-seg') {
  const parts = segments(text);
  const out = [];
  parts.forEach((part, i) => {
    if (i) out.push(h('span', { class: 'ic-vh' }, ', '));
    out.push(h('span', { class: cls }, part));
  });
  return out;
}

function distanceText(m) {
  if (!isNum(m)) return '';
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

/** Which button set the slip shows. */
export function slipVariant(approval, assessment) {
  if (!approval) return 'normal';
  // A stale engagement (target moved, sim speed or weather changed) offers
  // Ask for a fresh plan first; Approve anyway stays boxed and re-armed.
  if (isEngagement(approval))
    return assessment?.state === 'stale' ? 'stale' : 'normal';
  if (isDryRunnable(approval)) {
    const state = assessment?.state;
    if (state === 'none') return 'no_dry_run';
    if (state === 'failed') return 'failed';
    if (state === 'stale') return 'stale';
  }
  return 'normal';
}

/** Words for the grant a session approval covers. */
export function grantPhrase(approval) {
  const scope = grantScopeOf(approval);
  const all = SENSOR_TOOLS.every((t) => scope.includes(t));
  if (all && !Array.isArray(approval?.grantScope)) return 'sensor tasking';
  return listWords(scope.map((t) => toolTitle(t)));
}

/** The slip's title: an engagement names its target (WG spec §5.3.7). */
export function slipTitle(approval) {
  if (isEngagement(approval)) return engagementTitle(approval);
  return approval?.title ?? '';
}

/** The one-line audit record of a decided slip (spec §6.8). */
export function filedText(approval) {
  const a = approval || {};
  const vehicle = approvalVehicle(a);
  const when = a.resolvedAt ?? a.decidedAt;
  const z = zulu(when, { seconds: true });
  const at = z ? ` at ${z}` : '';
  switch (a.state) {
    case 'approved':
      if (a.scope === 'session') {
        return `Approved by you${at}, with ${grantPhrase(a)} allowed until you start a new session.`;
      }
      return `Approved by you${at}. ${slipTitle(a)}${filedObject(a, vehicle)}.`;
    case 'denied':
      return `Denied by you${at}.${a.note ? ` Your note: “${a.note}”` : ''}`;
    case 'expired': {
      const minutes =
        isNum(a.expiresAt) && isNum(a.at) && !a.replay
          ? Math.round((a.expiresAt - a.at) / 60_000)
          : null;
      const span =
        minutes && minutes > 0
          ? ` after ${minutes} minute${minutes === 1 ? '' : 's'} without a decision`
          : ' without a decision';
      const z2 = zulu(a.resolvedAt ?? a.expiresAt, { seconds: true });
      return `Expired${z2 ? ` at ${z2}` : ''}${span}. Nothing was sent.`;
    }
    case 'cancelled':
      if (a.cancelCause === 'interrupt')
        return `Cancelled${at} when you stopped the analyst. Nothing was sent.`;
      if (a.cancelCause === 'session')
        return `Cancelled${at} when the session ended. Nothing was sent.`;
      return `Cancelled${at}. Nothing was sent.`;
    default:
      return '';
  }
}

/** What a filed approval acted on: the vehicle, the theater or the speed. */
function filedObject(a, vehicle) {
  if (isEngagement(a)) return '';
  if (a.tool === 'sim_set_theater') {
    const place = theaterPlace(a);
    return place ? `, ${place}` : '';
  }
  if (a.tool === 'sim_set_time_scale') {
    const to = a.args?.scale ?? a.timeScalePreview?.to;
    return isNum(to) ? `, ×${to}` : '';
  }
  return vehicle ? `, ${vehicle}` : '';
}

/**
 * Why a slip can only be denied, or null (WG spec §3.5, §3.6, §4.2.1,
 * §4.2.2, §5.3.7): an unknown class, a missing or incomplete preview, a
 * blocked theater, speed or engagement request, or an engagement this
 * console holds no approval key for (`access` is the chat client's
 * `{held, refused}`; missing access counts as no key). A Deny-only slip has
 * no Approve element in the DOM.
 * @returns {{kind:'unknown'|'preview'|'blocked'|'console', line:string, note:string|null}|null}
 */
export function denyOnlyOf(approval, assessment, access = null) {
  const a = approval || {};
  if (!classApprovable(a.klass)) {
    return {
      kind: 'unknown',
      line: unknownLine(a.rawClass),
      note: unknownDenyNote(a.rawClass),
    };
  }
  if (isEngagement(a)) {
    const only = engagementDenyOnly(a, assessment, access);
    if (!only) return null;
    return {
      kind: only.kind,
      line:
        only.kind === 'preview'
          ? PREVIEW_MISSING
          : only.line || "This can't be approved here.",
      note: null,
    };
  }
  if (a.klass === 'sim' && isPreviewTool(a.tool)) {
    if (previewProblem(a))
      return { kind: 'preview', line: PREVIEW_MISSING, note: null };
    const line = blockedLine(a, assessment);
    if (line) return { kind: 'blocked', line, note: null };
  }
  return null;
}

const FILED_ICON = {
  approved: ICON.check,
  denied: ICON.block,
  expired: ICON.schedule,
  cancelled: ICON.stop,
};

// ---- console-computed lines ------------------------------------------------------------

/** "Right now Drone1 is on the ground with 82.0% fuel. Link up." */
export function rightNowLine(vehicleName, v) {
  if (!vehicleName) return '';
  if (!v) return `Right now the console has no reading for ${vehicleName}.`;
  const state =
    v.landed === true
      ? 'is on the ground'
      : v.landed === false
        ? 'is airborne'
        : null;
  const fuel = isNum(v.fuel_pct) ? `${fixed1(v.fuel_pct)}% fuel` : null;
  let text;
  if (state && fuel) text = `Right now ${vehicleName} ${state} with ${fuel}.`;
  else if (state) text = `Right now ${vehicleName} ${state}.`;
  else if (fuel) text = `Right now ${vehicleName} has ${fuel}.`;
  else text = `Right now the console has no reading for ${vehicleName}.`;
  const link = linkWord(v.link);
  if (link) text += ` ${link}.`;
  if (v.bingo_latched === true) {
    text += ` BINGO is latched: ${vehicleName} is returning home, and that can't be cancelled.`;
  }
  if (isNum(v.stale_ms) && v.stale_ms > 5000) {
    text += ` Telemetry stale, last ${ago(0, v.stale_ms)}.`;
  }
  return text;
}

const LOST_LINK_WORDS = {
  rtb: 'returns to base',
  return_to_base: 'returns to base',
  continue: 'continues its mission',
  orbit: 'orbits where it is',
  loiter: 'orbits where it is',
  hover: 'holds where it is',
  land: 'lands',
  climb_for_los: 'climbs to regain the link',
};

/** Lost-link line, only from data (spec §6.7, §10.4). */
export function lostLinkLine(vehicleName, lostLink) {
  if (!vehicleName || !lostLink || typeof lostLink !== 'object') return '';
  const behaviour = String(lostLink.behaviour ?? lostLink.behavior ?? '');
  if (!behaviour) return '';
  let does =
    LOST_LINK_WORDS[behaviour] ||
    `follows its "${behaviour.replace(/_/g, ' ')}" plan`;
  if (behaviour === 'climb_for_los' && isNum(lostLink.climb_to_m)) {
    does = `climbs to ${Math.round(lostLink.climb_to_m)} m to regain the link`;
  }
  const after = isNum(lostLink.declare_after_s)
    ? `for ${Math.round(lostLink.declare_after_s)} s`
    : '';
  let text = `If the link drops${after ? ` ${after}` : ''}, ${vehicleName} ${does}`;
  if (
    isNum(lostLink.escalate_to_rtb_after_s) &&
    !/^(rtb|return_to_base)$/.test(behaviour)
  ) {
    text += `, then returns to base after ${Math.round(lostLink.escalate_to_rtb_after_s)} s`;
  }
  return `${text}. That's its lost-link plan.`;
}

function ackCopy(tool, vehicle) {
  const who = vehicle || 'the aircraft';
  if (tool === 'sim_set_fuel')
    return `I understand this clears ${who}'s BINGO latch.`;
  if (tool === 'sim_set_link_state')
    return `I understand this changes ${who}'s link and can start its lost-link return.`;
  if (tool === 'sim_reset')
    return 'I understand this resets the simulation and drops in-flight state.';
  return 'I understand this overrides a safety control.';
}

function undoLine(tool) {
  if (tool === 'sim_set_weather')
    return 'Set the weather back with another simulation change, which also needs your approval.';
  if (tool === 'sim_set_time')
    return 'Set the time back with another simulation change, which also needs your approval.';
  // The wargame's sim tools (M14a): there is no despawn; only ending the
  // wargame removes scenario units.
  if (tool === 'wg_session_start')
    return 'End the wargame with another change, which also needs your approval.';
  if (tool === 'wg_session_end')
    return "Start a new simulated wargame with another change, which also needs your approval. This session's scenario units don't come back.";
  if (tool === 'wg_generate_scenario' || tool === 'wg_spawn_force')
    return 'Only ending the wargame removes scenario units. That also needs your approval.';
  return 'Reverse it with another simulation change, which also needs your approval.';
}

// ---- the dry-run block -----------------------------------------------------------------

function dryRunRows(dry, approval) {
  const gate = dry.gate && typeof dry.gate === 'object' ? dry.gate : {};
  const rows = [];
  const failing = [];
  // Rows whose content is already in the critical `failing` list: a failed
  // gate shows them once, as failures, not again as plain table rows.
  const failingKeys = new Set();
  const req = gate.required_pct;
  const avail = gate.available_pct;
  if (isNum(req)) {
    const parts = [];
    if (isNum(gate.plan_fuel_pct))
      parts.push(`plan ${fixed1(gate.plan_fuel_pct)}`);
    if (isNum(gate.return_fuel_pct))
      parts.push(`return ${fixed1(gate.return_fuel_pct)}`);
    if (isNum(gate.reserve_pct))
      parts.push(`reserve ${fixed1(gate.reserve_pct)}`);
    const value = `≈ ${fixed1(req)}%${parts.length ? ` (${parts.join(', ')})` : ''}`;
    rows.push(['Fuel needed', value, 'Estimated']);
    if (isNum(avail) && req > avail) {
      failingKeys.add('Fuel needed');
      failing.push(
        `Fuel needed ≈ ${fixed1(req)}%: more than the ${fixed1(avail)}% available`,
      );
    }
  }
  if (isNum(avail))
    rows.push(['Fuel at dry run', `${fixed1(avail)}%`, 'Measured']);
  if (isNum(req) && isNum(avail)) {
    rows.push(['Margin', `≈ ${fixed1(avail - req)} points`, 'Estimated']);
  }
  if (isNum(dry.fuel_pct_after)) {
    rows.push([
      'Fuel at the end',
      `≈ ${fixed1(dry.fuel_pct_after)}%`,
      'Estimated',
    ]);
  }
  if (isNum(gate.bingo_fuel_pct)) {
    rows.push(['BINGO line', `≈ ${fixed1(gate.bingo_fuel_pct)}%`, 'Estimated']);
  }
  const eta = isNum(dry.eta_s) ? dry.eta_s : gate.est_time_s;
  const td = [];
  if (isNum(eta)) td.push(`≈ ${spanText(eta)}`);
  if (isNum(gate.est_distance_m)) td.push(distanceText(gate.est_distance_m));
  if (isNum(dry.waypoints)) td.push(`${dry.waypoints} waypoints`);
  if (td.length) rows.push(['Time and distance', td.join(', '), 'Estimated']);
  if (typeof gate.bingo_latched === 'boolean') {
    rows.push([
      'BINGO latch',
      gate.bingo_latched ? 'Latched, returning home' : 'Not latched',
      'Measured',
    ]);
    if (gate.bingo_latched) {
      failingKeys.add('BINGO latch');
      failing.push('BINGO is latched: a return home is flying');
    }
  }
  const violations = Array.isArray(gate.envelope_violations)
    ? gate.envelope_violations.slice(0, 8).map(String)
    : null;
  if (violations) {
    if (violations.length) {
      rows.push(['Envelope', violations.join('; '), 'Measured']);
      failingKeys.add('Envelope');
      failing.push(...violations);
    } else {
      rows.push(['Envelope', envelopeOk(gate.envelope, approval), 'Measured']);
    }
  }
  for (const [key, value] of Object.entries(gate)) {
    if (KNOWN_GATE_KEYS.has(key)) continue;
    if (value == null || typeof value === 'object') continue;
    rows.push([key, String(value), '']);
  }
  const warnings = Array.isArray(gate.warnings)
    ? gate.warnings
        .slice(0, 8)
        .map((w) =>
          /^gate_start_assumed_home\b/.test(String(w))
            ? ASSUMED_HOME
            : warningText(w),
        )
    : [];
  return { rows, failing, failingKeys, warnings };
}

function envelopeOk(envelope, approval) {
  if (!envelope || typeof envelope !== 'object') return 'No violations';
  const args = approval?.args || {};
  const parts = [];
  const alt =
    args.alt_agl_m ??
    args.params?.alt_agl_m ??
    args.alt_m_agl ??
    args.altitude_m_agl ??
    args.alt_m ??
    args.altitude_m;
  if (
    isNum(alt) &&
    isNum(envelope.min_agl_m) &&
    isNum(envelope.ceiling_m_agl)
  ) {
    parts.push(
      `${Math.round(alt)} m AGL within ${Math.round(envelope.min_agl_m)}–${Math.round(envelope.ceiling_m_agl)} m`,
    );
  }
  const speed = args.speed_mps ?? args.params?.speed_mps ?? args.speed_ms;
  if (isNum(speed) && isNum(envelope.max_speed_mps)) {
    parts.push(
      `${Math.round(speed)} m/s within ${Math.round(envelope.max_speed_mps)} m/s`,
    );
  }
  return parts.length ? `No violations: ${parts.join(', ')}` : 'No violations';
}

function table(rows, head = null) {
  return h(
    'table',
    { class: 'ic-slip__table' },
    head
      ? h(
          'thead',
          {},
          h('tr', {}, ...head.map((cell) => h('th', { scope: 'col' }, cell))),
        )
      : null,
    h(
      'tbody',
      {},
      ...rows.map((cells) =>
        h(
          'tr',
          {},
          h('th', { scope: 'row' }, cells[0]),
          ...cells
            .slice(1)
            .map((cell) =>
              h(
                'td',
                {},
                ...(Array.isArray(cell) ? cell : [cell]).map((part) =>
                  typeof part === 'string' &&
                  /^(Estimated|Measured|Assumed|Requested|Mapped)$/.test(part)
                    ? tag(part)
                    : part,
                ),
              ),
            ),
        ),
      ),
    ),
  );
}

// ---- the slip ----------------------------------------------------------------------------

let slipCounter = 0;

export function createSlip(initialModel, deps = {}) {
  const clock = deps.clock || globalThis;
  const setT = (fn, ms) => (clock.setTimeout ? clock.setTimeout(fn, ms) : null);
  const clearT = (id) => {
    if (id != null) clock.clearTimeout?.(id);
  };
  const raf =
    deps.raf === undefined ? globalThis.requestAnimationFrame : deps.raf;
  const caf =
    deps.caf === undefined ? globalThis.cancelAnimationFrame : deps.caf;
  const RO =
    deps.ResizeObserver === undefined
      ? globalThis.ResizeObserver
      : deps.ResizeObserver;
  const doc = deps.doc === undefined ? globalThis.document : deps.doc;
  const announce = deps.announce || (() => {});

  let model = initialModel;
  const approval0 = model.approval;
  const uid = `${String(approval0.id).replace(/[^A-Za-z0-9_-]/g, '')}-${(slipCounter += 1)}`;
  const titleId = `ic-slip-title-${uid}`;
  const noteId = `ic-slip-note-${uid}`;

  let armed = false;
  let armTimer = null;
  let pointerApproveTimer = null;
  let armCycle = 0;
  let deciding = false;
  let variant = null;
  let lastAssessState = model.assessment?.state ?? null;
  let lastRect = null;
  let frame = null;
  let observer = null;
  let visibilityHandler = null;
  let minuteWarned = false;
  let infoKey = null;
  let filed = false;
  let detailsOpen = false;
  let destroyed = false;

  // ---- persistent skeleton ----
  const band = h('div', { class: 'ic-slip__band', 'aria-hidden': 'true' });
  const glyph = icon(classMeta(approval0.klass).icon, 'ic-slip__glyph');
  const phrase = h(
    'span',
    { class: 'ic-slip__phrase' },
    classMeta(approval0.klass).phrase,
  );
  const queueEl = h('span', { class: 'ic-slip__queue' });
  const expiryEl = h('span', {
    class: 'ic-slip__expiry',
    'aria-hidden': 'true',
  });
  const classLine = h(
    'div',
    { class: 'ic-slip__classline' },
    glyph,
    phrase,
    queueEl,
    expiryEl,
  );
  const titleEl = h(
    'h3',
    { class: 'ic-slip__title', id: titleId, tabindex: '-1' },
    slipTitle(approval0),
  );
  // Engagement (WG spec §5.3.1): a 6 px Sand 45° hatch on top of the band,
  // meaning "acknowledge, and irreversible".
  const hatch = isEngagement(approval0)
    ? h('div', { class: 'ic-slip__hatch', 'aria-hidden': 'true' })
    : null;
  const infoEl = h('div', { class: 'ic-slip__info' });

  const requestBtn = h(
    'button',
    { type: 'button', class: 'ic-slip__link', 'aria-expanded': 'false' },
    'Show exact request',
  );
  const noteBtn = h(
    'button',
    {
      type: 'button',
      class: 'ic-slip__link',
      'aria-expanded': 'false',
      'aria-controls': noteId,
    },
    'Add a note for the analyst',
  );
  const requestPanel = h('div', { class: 'ic-slip__request', hidden: true });
  const noteField = h('textarea', {
    class: 'ic-slip__note',
    rows: '2',
    maxlength: '2000',
    'aria-label': 'Note for the analyst',
    'aria-describedby': `${noteId}-hint`,
  });
  const notePanel = h(
    'div',
    { class: 'ic-slip__notebox', id: noteId, hidden: true },
    noteField,
    h(
      'p',
      { class: 'ic-slip__hint', id: `${noteId}-hint` },
      '⌘Enter denies and sends this note.',
    ),
  );
  const extras = h('div', { class: 'ic-slip__extras' }, requestBtn, noteBtn);

  const grantBox = h('input', { type: 'checkbox', class: 'ic-slip__check' });
  const grantLabelText = h('span', {});
  const grantCaption = h('p', { class: 'ic-slip__caption' });
  const grantEl = h(
    'div',
    { class: 'ic-slip__grant' },
    h('label', { class: 'ic-slip__checkline' }, grantBox, grantLabelText),
    grantCaption,
  );
  const ackBox = h('input', { type: 'checkbox', class: 'ic-slip__check' });
  const ackTextEl = h('span', {});
  const ackEl = h(
    'label',
    { class: 'ic-slip__ack ic-slip__checkline' },
    ackBox,
    ackTextEl,
  );

  const errorEl = h('p', {
    class: 'ic-slip__error',
    role: 'alert',
    hidden: true,
  });
  // The reason a slip is Deny-only (unknown class, missing preview, blocked).
  const denyLineEl = h('p', {
    class: 'ic-slip__blocked ic-slip__denyonly',
    role: 'note',
    hidden: true,
  });
  const actionsEl = h('div', { class: 'ic-slip__actions' });
  const policyEl = h('p', { class: 'ic-slip__policy' });
  // An engagement's box comes straight after "What can't be undone", before
  // the request and note links (WG spec §5.3.7).
  const ackFirst = isEngagement(approval0);
  const pendingEl = h(
    'div',
    { class: 'ic-slip__pending' },
    ackFirst ? ackEl : null,
    extras,
    requestPanel,
    notePanel,
    grantEl,
    ackFirst ? null : ackEl,
    errorEl,
    denyLineEl,
    actionsEl,
    policyEl,
  );

  const filedEl = h('div', { class: 'ic-slip__filed', hidden: true });
  const el = h(
    'section',
    {
      class: 'ic-slip',
      role: 'region',
      'aria-labelledby': titleId,
      'data-class': approval0.klass,
      'data-state': 'pending',
    },
    hatch,
    band,
    classLine,
    titleEl,
    infoEl,
    pendingEl,
    filedEl,
  );

  /** @type {Array<{el:object, kind:'approve'|'deny', label:string}>} */
  let buttons = [];

  // ---- arming ----
  // An engagement always needs the box, whatever the event says (§5.3.7).
  const ackOffered = () =>
    classApprovable(model.approval.klass) &&
    (model.approval.klass === 'safety_override' ||
      isEngagement(model.approval) ||
      model.approval.acknowledgeRequired === true);
  const needsAck = () => ackOffered() && ackBox.checked !== true;
  const consoleAccess = () =>
    typeof deps.consoleAccess === 'function'
      ? deps.consoleAccess()
      : (model.console ?? null);
  const denyOnly = () =>
    denyOnlyOf(model.approval, model.assessment, consoleAccess());
  // Engagements arm 1600 ms after the box is ticked; everything else 800 ms.
  const armMs = () => (isEngagement(model.approval) ? ENGAGE_ARM_MS : ARM_MS);
  const isPending = () =>
    !filed &&
    (model.approval.state === 'pending' || model.approval.state === 'deciding');

  function approveLabel(base) {
    if (!armed && !needsAck() && model.reducedMotion)
      return isEngagement(model.approval)
        ? ENGAGEMENT_SLIP_COPY.readyIn
        : 'Ready in 1 s';
    return base;
  }

  let appliedReduced = null;

  function applyArm() {
    appliedReduced = Boolean(model.reducedMotion);
    const busy = deciding || model.approval.state === 'deciding';
    for (const b of buttons) {
      if (b.kind === 'approve') {
        const disabled = !armed || needsAck() || busy || !isPending();
        b.el.setAttribute('aria-disabled', disabled ? 'true' : 'false');
        if (!armed && !needsAck() && isPending()) {
          b.el.setAttribute('data-arming', armCycle % 2 ? 'b' : 'a');
        } else b.el.removeAttribute('data-arming');
        setText(b.labelEl, approveLabel(b.label));
      } else {
        b.el.setAttribute(
          'aria-disabled',
          busy || !isPending() ? 'true' : 'false',
        );
      }
    }
    el.setAttribute('data-armed', armed ? 'true' : 'false');
  }

  function startArming() {
    armed = false;
    clearT(armTimer);
    armTimer = null;
    armCycle += 1;
    if (isPending() && !needsAck()) {
      armTimer = setT(() => {
        armTimer = null;
        if (destroyed || !isPending()) return;
        armed = true;
        applyArm();
      }, armMs());
    }
    applyArm();
  }

  function rearm() {
    if (!isPending()) return;
    startArming();
  }

  function checkLayout(rect) {
    const r = rect || deps.getRect?.(el) || el.getBoundingClientRect?.();
    if (!r) return;
    if (
      lastRect &&
      (Math.abs(r.top - lastRect.top) > LAYOUT_SLOP_PX ||
        Math.abs(r.left - lastRect.left) > LAYOUT_SLOP_PX ||
        Math.abs(r.width - lastRect.width) > LAYOUT_SLOP_PX ||
        Math.abs(r.height - lastRect.height) > LAYOUT_SLOP_PX)
    ) {
      rearm('layout');
    }
    lastRect = { top: r.top, left: r.left, width: r.width, height: r.height };
  }

  function loop() {
    frame = null;
    if (destroyed || !isPending()) return;
    if (!doc?.hidden) checkLayout();
    frame = raf(loop);
  }

  function startWatching() {
    if (typeof RO === 'function' && !observer) {
      try {
        observer = new RO(() => checkLayout());
        observer.observe(el);
      } catch {
        observer = null;
      }
    }
    if (typeof raf === 'function' && frame == null) frame = raf(loop);
    if (doc?.addEventListener && !visibilityHandler) {
      visibilityHandler = () => {
        if (!doc.hidden) rearm('visible');
      };
      doc.addEventListener('visibilitychange', visibilityHandler);
    }
  }

  function stopWatching() {
    clearT(armTimer);
    armTimer = null;
    if (frame != null && typeof caf === 'function') caf(frame);
    frame = null;
    observer?.disconnect?.();
    observer = null;
    if (visibilityHandler)
      doc?.removeEventListener?.('visibilitychange', visibilityHandler);
    visibilityHandler = null;
  }

  // ---- decisions ----
  function noteText() {
    return String(noteField.value ?? '').trim();
  }

  async function decide(decision, preset = null) {
    cancelPointerApprove();
    if (deciding || !isPending() || model.approval.state === 'deciding') return;
    const typed = noteText();
    const note = preset ? (typed ? `${preset} ${typed}` : preset) : typed;
    // The server requires `acknowledged:true` on an engagement approval
    // (WG spec §3.5); it is sent only when the box is actually ticked.
    const acknowledged =
      decision !== 'deny' && ackOffered() && ackBox.checked === true;
    deciding = true;
    applyArm();
    try {
      await deps.decide?.(decision, note || null, { acknowledged });
    } catch {
      // The view folds the failure into the model (approval.error).
    } finally {
      deciding = false;
      if (!destroyed) {
        if (isPending()) startArming();
        else applyArm();
      }
    }
  }

  function attemptApprove() {
    if (!isPending() || deciding || !armed || needsAck()) return;
    // A Deny-only slip never approves, even if a stale button were pressed.
    if (denyOnly()) return;
    if (typeof deps.revalidate === 'function') {
      const fresh = deps.revalidate();
      if (
        fresh &&
        (fresh.state !== model.assessment?.state ||
          denyOnlyOf(model.approval, fresh, consoleAccess()) != null)
      ) {
        model = { ...model, assessment: fresh };
        render(true);
        startArming();
        announce('Conditions changed. Review the slip again.', {
          assertive: true,
        });
        return;
      }
    }
    const offered = grantOffered(model.approval) && grantBox.checked === true;
    decide(offered ? 'approve_session' : 'approve');
  }

  function cancelPointerApprove() {
    clearT(pointerApproveTimer);
    pointerApproveTimer = null;
  }

  function wireApprove(button) {
    let downOn = false;
    let downArmed = false;
    button.addEventListener('pointerdown', () => {
      // A second press inside the double-click interval cancels the first.
      cancelPointerApprove();
      downOn = true;
      downArmed = armed && !needsAck();
    });
    button.addEventListener('keydown', (event) => {
      const key = event?.key;
      if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar') return;
      event.preventDefault?.();
      if (event.repeat) return; // key auto-repeat never approves
      attemptApprove();
    });
    button.addEventListener('keyup', (event) => {
      const key = event?.key;
      if (key === 'Enter' || key === ' ' || key === 'Spacebar')
        event.preventDefault?.();
    });
    button.addEventListener('click', (event) => {
      event?.preventDefault?.();
      const detail = Number(event?.detail ?? 0);
      const viaPointer = downOn;
      const wasArmed = downArmed;
      downOn = false;
      downArmed = false;
      if (detail > 1) {
        cancelPointerApprove(); // a double-click can never approve
        return;
      }
      if (detail === 1) {
        if (!viaPointer || !wasArmed) return; // down and up on the same armed button
        // Wait out the double-click interval: the first click of a real
        // double-click (click detail 1, then detail 2, then dblclick) must not
        // approve either. attemptApprove re-checks arming when it fires.
        cancelPointerApprove();
        pointerApproveTimer = setT(() => {
          pointerApproveTimer = null;
          if (!destroyed) attemptApprove();
        }, DBLCLICK_MS);
        return;
      }
      // detail 0 is neither a pointer nor our (suppressed) key activation:
      // assistive technology. Still gated by arming.
      attemptApprove();
    });
    button.addEventListener('dblclick', (event) => {
      event?.preventDefault?.();
      cancelPointerApprove();
    });
  }

  function wireDeny(button, preset) {
    const act = () => decide('deny', preset);
    button.addEventListener('keydown', (event) => {
      const key = event?.key;
      if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar') return;
      event.preventDefault?.();
      if (event.repeat) return;
      act();
    });
    button.addEventListener('click', (event) => {
      event?.preventDefault?.();
      if (Number(event?.detail ?? 0) > 1) return;
      act();
    });
  }

  function makeButton(
    kind,
    label,
    { primary = false, action, preset = null } = {},
  ) {
    const labelEl = h('span', { class: 'ic-slip__btnlabel' }, label);
    const cls =
      kind === 'approve'
        ? `ic-slip__btn ic-slip__approve${primary ? ' is-primary' : ' is-secondary'}`
        : `ic-slip__btn ic-slip__deny${primary ? ' is-primary' : ''}`;
    const button = h(
      'button',
      {
        type: 'button',
        class: cls,
        'data-action': action,
        'aria-disabled': 'false',
      },
      labelEl,
    );
    if (kind === 'approve') wireApprove(button);
    else wireDeny(button, preset);
    return { el: button, kind, label, labelEl };
  }

  function denyLabel() {
    return noteText() ? 'Deny and send note' : 'Deny';
  }

  function buildButtons() {
    const a = model.approval;
    const v = slipVariant(a, model.assessment);
    const offered = grantOffered(a) && grantBox.checked === true;
    const verb = offered
      ? 'Approve and allow for session'
      : approveVerb(a.tool, a.klass, {
          verbKind: verbKindOf(engagementPreviewOf(a)),
        });
    const list = [];
    const only = denyOnly();
    if (only) {
      // Deny-only: no Approve element exists in the DOM (WG spec §4.2.1).
      list.push(
        makeButton('deny', denyLabel(), {
          primary: true,
          action: 'deny',
          preset: only.note,
        }),
      );
      return { v: 'deny_only', list };
    }
    if (v === 'failed') {
      list.push(
        makeButton('deny', 'Deny and re-plan', {
          primary: true,
          action: 'deny-replan',
          preset: SLIP_NOTES.replan,
        }),
      );
    } else if (v === 'no_dry_run') {
      list.push(makeButton('deny', denyLabel(), { action: 'deny' }));
      list.push(
        makeButton('deny', 'Ask for a dry run', {
          primary: true,
          action: 'ask-dry-run',
          preset: SLIP_NOTES.askDryRun,
        }),
      );
      list.push(
        makeButton('approve', 'Approve without a dry run', {
          action: 'approve-anyway',
        }),
      );
    } else if (v === 'stale' && isEngagement(a)) {
      // WG spec §5.3.7: [Deny] [Ask for a fresh plan], and Approve anyway
      // as an outline button that is still boxed and re-armed.
      list.push(makeButton('deny', denyLabel(), { action: 'deny' }));
      list.push(
        makeButton('deny', ENGAGEMENT_SLIP_COPY.freshPlan, {
          primary: true,
          action: 'ask-fresh-plan',
          preset: SLIP_NOTES.freshPlan,
        }),
      );
      list.push(
        makeButton('approve', 'Approve anyway', { action: 'approve-anyway' }),
      );
    } else if (v === 'stale') {
      list.push(makeButton('deny', denyLabel(), { action: 'deny' }));
      list.push(
        makeButton('deny', 'Ask for a fresh dry run', {
          primary: true,
          action: 'ask-fresh-dry-run',
          preset: SLIP_NOTES.freshDryRun,
        }),
      );
      list.push(
        makeButton('approve', 'Approve anyway', { action: 'approve-anyway' }),
      );
    } else {
      list.push(makeButton('deny', denyLabel(), { action: 'deny' }));
      list.push(
        makeButton('approve', verb, { primary: true, action: 'approve' }),
      );
    }
    return { v, list };
  }

  function renderButtons(force = false) {
    const a = model.approval;
    const v = slipVariant(a, model.assessment);
    const offered = grantOffered(a) && grantBox.checked === true;
    const only = denyOnly();
    const key = `${v}|${offered}|${noteText() ? 1 : 0}|${only ? only.kind : ''}`;
    if (!force && key === variant) return;
    const focusedAction = buttons.find((b) => doc?.activeElement === b.el);
    variant = key;
    const built = buildButtons();
    buttons = built.list;
    replaceKids(
      actionsEl,
      buttons.map((b) => b.el),
    );
    // Three buttons get tighter padding so they keep one row (chat.css).
    actionsEl.setAttribute('data-count', String(buttons.length));
    el.setAttribute('data-variant', built.v);
    if (focusedAction) {
      // Keep keyboard users in the slip; never land them on an approve button.
      buttons.find((b) => b.kind === 'deny')?.el.focus?.();
    }
    applyArm();
  }

  // ---- information ----
  function whatHappens(a) {
    const nodes = [h('h4', { class: 'ic-slip__head' }, 'What happens')];
    if (a.consequences.length) {
      nodes.push(
        h(
          'ul',
          { class: 'ic-slip__list' },
          ...a.consequences.map((c) => h('li', {}, c)),
        ),
      );
    } else {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__muted' },
          'The server sent no description of the effects.',
        ),
      );
    }
    return nodes;
  }

  function caveatNodes() {
    return (model.caveats || []).map((c) =>
      h(
        'p',
        { class: 'ic-slip__caveat', title: c },
        h('span', { class: 'ic-assumed' }, c),
        ' ',
        tag('Assumed'),
      ),
    );
  }

  function basedOnCommand(a, vehicle) {
    const nodes = [];
    const dry = a.dry_run;
    const dryRunnable = isDryRunnable(a);
    const kind = kindLabel(missionKind(a.tool, a.args));
    const assess = model.assessment || { state: 'none', reasons: [] };
    const who = vehicle || 'the aircraft';
    if (!dryRunnable) {
      nodes.push(h('h4', { class: 'ic-slip__head' }, 'Based on'));
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__console' },
          rightNowLine(vehicle, model.vehicle) ||
            'No reading for the aircraft.',
        ),
      );
      nodes.push(...caveatNodes());
      return nodes;
    }
    if (!dry) {
      nodes.push(h('h4', { class: 'ic-slip__head' }, 'Based on'));
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__console' },
          `No dry run of ${withArticle(kind)} for ${who} in this conversation. The server gates it at launch either way.`,
        ),
      );
      nodes.push(...caveatNodes());
      return nodes;
    }
    const passed = dry.ok !== false;
    const z = zulu(dry.at_ms, { seconds: true });
    const when = z
      ? ` at ${z}${isNum(model.now) ? `, ${ago(dry.at_ms, model.now)}` : ''}`
      : '';
    nodes.push(
      h(
        'h4',
        { class: 'ic-slip__head' },
        'Based on',
        h(
          'span',
          { class: 'ic-slip__dryhead', 'data-ok': passed ? 'true' : 'false' },
          passed ? `Dry run passed${when}` : `Dry run failed the gate${when}`,
        ),
      ),
    );
    nodes.push(
      h(
        'p',
        { class: 'ic-slip__console' },
        `Latest dry run of ${withArticle(kind)} for ${who} in this conversation. ` +
          (dry.matches_args === true
            ? 'The server confirms it used these exact settings, and it gates the launch again.'
            : "The server doesn't confirm it used these exact settings, and it gates the launch again."),
      ),
    );
    if (assess.state === 'unverified') {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__console' },
          `The console can't compare this dry run with ${vehicle ? `${vehicle}'s` : "the aircraft's"} current state.`,
        ),
      );
    }
    const { rows, failing, failingKeys, warnings } = dryRunRows(dry, a);
    const showFailing = !passed && failing.length > 0;
    if (showFailing) {
      nodes.push(
        h(
          'ul',
          { class: 'ic-slip__failing' },
          ...failing.map((f) => h('li', {}, f)),
        ),
      );
    }
    const tableRows = showFailing
      ? rows.filter(([k]) => !failingKeys.has(k))
      : rows;
    if (tableRows.length)
      nodes.push(table(tableRows.map(([k, v, r]) => [k, v, r ? [r] : ''])));
    if (warnings.length) {
      nodes.push(
        h(
          'ul',
          { class: 'ic-slip__warnings' },
          ...warnings.map((w) => h('li', {}, w)),
        ),
      );
    }
    nodes.push(...caveatNodes());
    if (assess.state === 'stale') {
      const summary = staleSummary(assess.reasons, vehicle);
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__stale', role: 'note' },
          `Conditions changed since the dry run: ${summary}. The numbers above may be wrong.`,
        ),
      );
    } else if (assess.state === 'failed' || !passed) {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__blocked' },
          "The server would reject this launch, so it can't be approved here.",
        ),
      );
    }
    return nodes;
  }

  function basedOnSensor(vehicle) {
    const v = model.vehicle;
    const nodes = [h('h4', { class: 'ic-slip__head' }, 'Based on')];
    const para = [];
    if (!vehicle) para.push('No aircraft named in the request.');
    else if (!v || !isNum(v.lat) || !isNum(v.lon)) {
      para.push(
        h(
          'span',
          { class: 'ic-noreading' },
          `No reading for ${vehicle}'s position.`,
        ),
      );
    } else {
      para.push(
        `${vehicle} at `,
        h('span', { class: 'ic-mono' }, `${coord(v.lat)}, ${coord(v.lon)}`),
      );
      if (isNum(v.agl_m)) {
        const agl = `${Math.round(v.agl_m)} m above ground`;
        if (v.agl_is_real === false) {
          para.push(
            ', ',
            h(
              'span',
              {
                class: 'ic-assumed',
                title: 'Height above the launch datum, not terrain clearance.',
              },
              agl,
            ),
            ' (Assumed: height above launch datum).',
          );
        } else para.push(`, ${agl}.`);
      } else para.push('.');
    }
    const det = model.detections;
    if (det) {
      if (det.ok) {
        const age =
          isNum(det.at_ms) && isNum(model.now)
            ? `, last update ${ago(det.at_ms, model.now)}`
            : '';
        para.push(` Detections feed up${age}.`);
      }
    }
    nodes.push(h('p', { class: 'ic-slip__console' }, ...para));
    if (det && !det.ok) {
      const since = zulu(det.at_ms, { seconds: true });
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__noreading' },
          `Detections feed is down${since ? ` since ${since}` : ''}. A scan now may return nothing, and that isn't a clear reading.`,
        ),
      );
    }
    nodes.push(...caveatNodes());
    return nodes;
  }

  function overrideTable(a, vehicle) {
    if (a.tool !== 'sim_set_fuel') return [];
    const v = model.before || model.vehicle;
    const requested = isNum(a.args?.fuel_pct) ? a.args.fuel_pct : 100;
    const latched = v?.bingo_latched;
    const rows = [
      [
        'Fuel',
        isNum(v?.fuel_pct)
          ? [`${fixed1(v.fuel_pct)}% `, 'Measured']
          : [h('span', { class: 'ic-noreading' }, 'No reading')],
        [`${Math.round(requested)}% `, 'Requested'],
      ],
      [
        'BINGO',
        latched === true
          ? 'Latched, returning home'
          : latched === false
            ? 'Not latched'
            : 'No reading',
        latched === true
          ? 'Latch cleared; the return already flying continues'
          : latched === false
            ? 'Not latched'
            : 'Latch cleared',
      ],
    ];
    return [table(rows, ['', 'Now', 'After'])];
  }

  function stopSection(a, vehicle) {
    const k = a.klass;
    if (k === 'sensor') {
      return [
        h('h4', { class: 'ic-slip__head' }, 'Nothing to stop.'),
        h(
          'p',
          { class: 'ic-slip__console' },
          "Sensor tasking doesn't fly the aircraft. What the scan records stays in the intel picture.",
        ),
      ];
    }
    if (k === 'sim') {
      return [
        h('h4', { class: 'ic-slip__head' }, 'How to undo it'),
        h('p', { class: 'ic-slip__console' }, undoLine(a.tool)),
      ];
    }
    if (k === 'safety_override') {
      const b = model.before;
      const who = vehicle || 'The aircraft';
      let line = "The console can't undo an override.";
      if (vehicle && b) {
        const facts = [];
        if (isNum(b.fuel_pct)) facts.push(`${fixed1(b.fuel_pct)}% fuel`);
        if (b.bingo_latched === true) facts.push('BINGO latched');
        else if (b.bingo_latched === false) facts.push('BINGO not latched');
        line += ` ${who}'s state before it${facts.length ? ` (${facts.join(', ')})` : ''} stays in this transcript.`;
      }
      return [
        h('h4', { class: 'ic-slip__head' }, 'How to undo it'),
        h('p', { class: 'ic-slip__console' }, line),
      ];
    }
    const items = [];
    const bingo = isNum(a.dry_run?.gate?.bingo_fuel_pct)
      ? a.dry_run.gate.bingo_fuel_pct
      : model.vehicle?.bingo_fuel_pct;
    const who = vehicle || 'the aircraft';
    items.push(
      `If fuel reaches BINGO${isNum(bingo) ? ` (≈ ${fixed1(bingo)}%)` : ''}, ${who} returns home, and that return can't be cancelled.`,
    );
    if (a.tool !== 'uav_abort') {
      items.push(
        vehicle
          ? `Abort stops ${vehicle} and holds it where it is. It's in the situation rail, the inspector and the tracking dock, and it goes straight to ${vehicle}, not through the analyst.`
          : "Abort stops an aircraft and holds it where it is. It's in the situation rail, the inspector and the tracking dock, and it doesn't go through the analyst.",
      );
    }
    const lost = lostLinkLine(vehicle, model.vehicle?.lost_link);
    if (lost) items.push(capitalize(lost));
    return [
      h('h4', { class: 'ic-slip__head' }, 'How to stop it'),
      h(
        'ul',
        { class: 'ic-slip__list ic-slip__console' },
        ...items.map((t) => h('li', {}, t)),
      ),
    ];
  }

  function infoNodes() {
    const a = model.approval;
    // Unknown class: only what the server sent, as text (WG spec §4.2.1).
    if (!classApprovable(a.klass))
      return unknownInfoNodes(a, { h, segmentNodes });
    if (isEngagement(a))
      return engagementInfoNodes({
        approval: a,
        assessment: model.assessment,
        tag,
      });
    if (a.klass === 'sim' && isPreviewTool(a.tool)) {
      const ctx = {
        approval: a,
        fleet: Array.isArray(model.fleet) ? model.fleet : [],
        assessment: model.assessment,
        tag,
        table,
        now: model.now,
        rightNow: rightNowLine,
      };
      return a.tool === 'sim_set_theater'
        ? theaterInfoNodes(ctx)
        : timeScaleInfoNodes(ctx);
    }
    const vehicle = approvalVehicle(a);
    const nodes = [];
    const summary = segmentNodes(a.summary);
    nodes.push(
      h(
        'p',
        { class: 'ic-slip__summary' },
        ...summary,
        summary.length ? h('span', { class: 'ic-vh' }, ', ') : null,
        tag('Requested'),
      ),
    );
    for (const c of model.conflicts || []) {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__conflict', role: 'note' },
          `Another request for ${vehicle} is waiting: ${c.title}. Only one command runs at a time; the later one will be refused as busy.`,
        ),
      );
    }
    nodes.push(...whatHappens(a));
    if (a.klass === 'command' && isDryRunnable(a) && vehicle) {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__console' },
          rightNowLine(vehicle, model.vehicle),
        ),
      );
    }
    if (a.klass === 'sim' && STALING_TOOLS.includes(a.tool)) {
      nodes.push(
        h(
          'p',
          { class: 'ic-slip__console' },
          'Dry runs made before this change will be marked out of date.',
        ),
      );
    }
    if (a.klass === 'command') nodes.push(...basedOnCommand(a, vehicle));
    else if (a.klass === 'sensor') nodes.push(...basedOnSensor(vehicle));
    else if (a.klass === 'safety_override')
      nodes.push(...overrideTable(a, vehicle));
    nodes.push(...stopSection(a, vehicle));
    return nodes;
  }

  function requestNodes() {
    const a = model.approval;
    let json = '';
    try {
      json = JSON.stringify(a.args ?? {}, null, 2);
    } catch {
      json = String(a.args);
    }
    // The exact request: a bidi control shows as its JSON escape, not as a
    // reordering of the text around it.
    json = escapeBidi(json);
    return [
      h('p', { class: 'ic-mono ic-slip__tool' }, a.tool),
      h('pre', { class: 'ic-slip__args' }, h('code', {}, json)),
    ];
  }

  function renderControls() {
    const a = model.approval;
    const vehicle = approvalVehicle(a);
    // Session grant: sensor only, never between Deny and Approve (spec §6.6).
    const offered = grantOffered(a);
    setHidden(grantEl, !offered);
    if (offered) {
      if (Array.isArray(a.grantScope)) {
        const titles = listWords(a.grantScope.map((t) => toolTitle(t)));
        setText(grantLabelText, `Allow ${titles} for this session`);
        setText(
          grantCaption,
          'Until you start a new session. Flying and simulation changes still ask every time.',
        );
      } else {
        setText(
          grantLabelText,
          "Don't ask again for sensor tasking until I start a new session",
        );
        setText(
          grantCaption,
          `Covers ${listWords(SENSOR_TOOLS.map((t) => TOOL_TITLES[t]))}. Flying and simulation changes still ask every time.`,
        );
      }
    }
    // A Deny-only slip has nothing to acknowledge: no box without Approve.
    const ack = ackOffered() && !denyOnly();
    setHidden(ackEl, !ack);
    if (ack)
      setText(
        ackTextEl,
        isEngagement(a) ? engagementAck(a) : ackCopy(a.tool, vehicle),
      );
    const policy = policyLine(a.klass, a.tool);
    setText(policyEl, policy);
    setHidden(policyEl, !policy);
    const only = denyOnly();
    setText(denyLineEl, only ? only.line : '');
    setHidden(denyLineEl, !only);
    const err = a.error
      ? `Couldn't send your decision: ${a.error}. Try again.`
      : '';
    setText(errorEl, err);
    setHidden(errorEl, !err);
  }

  function renderFiled() {
    const a = model.approval;
    const line = h(
      'p',
      { class: 'ic-slip__record' },
      icon(FILED_ICON[a.state] || ICON.info, 'ic-slip__recordglyph'),
      h('span', {}, filedText(a)),
    );
    // An approved engagement's outcome, live from the graph (§5.3.7).
    const outcome =
      isEngagement(a) && a.state === 'approved' ? model.outcome : null;
    const outcomeEl = outcome?.text
      ? h(
          'p',
          {
            class: 'ic-slip__outcome',
            'data-hidden': outcome.hidden ? 'true' : 'false',
            'data-tone': outcome.tone || null,
          },
          outcome.text,
        )
      : null;
    const details = h(
      'button',
      {
        type: 'button',
        class: 'ic-slip__link',
        'aria-expanded': detailsOpen ? 'true' : 'false',
      },
      'Details',
    );
    details.addEventListener('click', () => {
      detailsOpen = !detailsOpen;
      renderFiled();
    });
    replaceKids(
      filedEl,
      outcomeEl ? [line, outcomeEl, details] : [line, details],
    );
    setHidden(infoEl, !detailsOpen);
    setHidden(classLine, !detailsOpen);
    setHidden(titleEl, !detailsOpen);
    setHidden(band, false);
  }

  function renderInfo(force = false) {
    let key = '';
    try {
      key = JSON.stringify([
        model.approval.title,
        model.approval.summary,
        model.approval.consequences,
        model.approval.dry_run,
        model.approval.theaterPreview,
        model.approval.timeScalePreview,
        model.approval.engagementPreview,
        model.approval.rawClass,
        model.assessment,
        model.fleet,
        model.vehicle,
        model.before,
        model.conflicts,
        model.caveats,
        model.detections,
        Math.floor((model.now ?? 0) / 30_000),
      ]);
    } catch {
      key = String(Math.random());
    }
    if (!force && key === infoKey) return;
    infoKey = key;
    replaceKids(infoEl, infoNodes());
    if (requestBtn.getAttribute('aria-expanded') === 'true') {
      replaceKids(requestPanel, requestNodes());
    }
  }

  function render(force = false) {
    const a = model.approval;
    const pending = a.state === 'pending' || a.state === 'deciding';
    el.setAttribute('data-class', a.klass);
    el.setAttribute('data-validity', model.assessment?.state || 'none');
    setText(titleEl, slipTitle(a));
    const q = model.queue;
    setText(queueEl, q && q.total > 1 ? `${q.index} of ${q.total}` : '');
    if (pending) {
      el.setAttribute(
        'data-state',
        a.state === 'deciding' ? 'deciding' : 'pending',
      );
      setHidden(pendingEl, false);
      setHidden(filedEl, true);
      setHidden(infoEl, false);
      setHidden(classLine, false);
      setHidden(titleEl, false);
      renderInfo(force);
      renderControls();
      renderButtons(force);
      // Reduced motion is live (a system toggle): relabel "Ready in 1 s".
      if (appliedReduced !== Boolean(model.reducedMotion)) applyArm();
      tick(model.now);
    } else {
      if (!filed) {
        filed = true;
        stopWatching();
        armed = false;
      }
      el.setAttribute('data-state', 'filed');
      el.setAttribute('data-outcome', a.state);
      setHidden(pendingEl, true);
      setHidden(filedEl, false);
      renderInfo(force);
      renderFiled();
    }
  }

  function tick(now) {
    if (!isNum(now)) return;
    model = { ...model, now };
    const a = model.approval;
    if (!isPending() || !isNum(a.expiresAt)) {
      setText(expiryEl, '');
      return;
    }
    const left = a.expiresAt - now;
    setText(expiryEl, left > 0 ? `Expires in ${countdown(left)}` : 'Expiring');
    el.setAttribute('data-urgent', left <= WARN_MS ? 'true' : 'false');
    if (left <= WARN_MS && left > 0 && !minuteWarned) {
      minuteWarned = true;
      announce(`1 minute left to decide: ${a.title}.`, { assertive: true });
    }
  }

  // ---- wiring of persistent controls ----
  requestBtn.addEventListener('click', () => {
    const open = requestBtn.getAttribute('aria-expanded') !== 'true';
    requestBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) replaceKids(requestPanel, requestNodes());
    setHidden(requestPanel, !open);
  });
  noteBtn.addEventListener('click', () => {
    const open = noteBtn.getAttribute('aria-expanded') !== 'true';
    noteBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    setHidden(notePanel, !open);
    if (open) noteField.focus?.();
  });
  noteField.addEventListener('keydown', (event) => {
    if ((event?.metaKey || event?.ctrlKey) && event?.key === 'Enter') {
      // ⌘Enter in the note field DENIES with the note. It never approves.
      event.preventDefault?.();
      event.stopPropagation?.();
      if (!event.repeat) decide('deny', denyOnly()?.note ?? null);
    }
  });
  noteField.addEventListener('input', () => renderButtons());
  grantBox.addEventListener('change', () => {
    renderButtons();
    rearm('checkbox');
  });
  ackBox.addEventListener('change', () => {
    rearm('checkbox');
  });

  render(true);
  if (isPending()) {
    startWatching();
    startArming();
  }

  return {
    el,
    get variant() {
      return slipVariant(model.approval, model.assessment);
    },
    update(next) {
      if (destroyed) return;
      const prevState = lastAssessState;
      const wasPending = isPending();
      model = { ...model, ...next };
      lastAssessState = model.assessment?.state ?? null;
      render();
      if (!wasPending) return;
      if (isPending()) {
        if (prevState !== lastAssessState) rearm('validation');
        else if (
          model.approval.state === 'pending' &&
          !armed &&
          armTimer == null &&
          !deciding
        ) {
          startArming();
        }
        startWatching();
      }
    },
    tick,
    rearm,
    review() {
      // Scroll only the transcript: scrollIntoView would also scroll every
      // ancestor, the console layer included.
      const log = el.closest?.('.ic-log');
      const r = el.getBoundingClientRect?.();
      const lr = log?.getBoundingClientRect?.();
      if (log && r && lr && Number.isFinite(log.scrollTop)) {
        const offset = r.top - lr.top - Math.max(0, (lr.height - r.height) / 2);
        log.scrollTop = Math.max(0, log.scrollTop + offset);
      } else el.scrollIntoView?.({ block: 'center', behavior: 'auto' });
      rearm('review');
      titleEl.focus?.({ preventScroll: true });
    },
    focusHeading() {
      titleEl.focus?.();
    },
    isArmed: () => armed && !needsAck(),
    checkLayout,
    destroy() {
      destroyed = true;
      cancelPointerApprove();
      stopWatching();
    },
  };
}
