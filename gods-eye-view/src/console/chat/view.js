/**
 * The analyst column (UX spec §6): watch-log transcript with Zulu times,
 * tool activity rows, entity chips with the analyst-blue tether, inline order
 * slips, directive lines, banners, the composer, suggested prompts, the
 * unavailable panel and the usage footer.
 *
 * `createAnalyst(host, ctx, opts?)` → { destroy(), setLayout(layout),
 *   setDocked(bool), focusComposer(), ask(text, focusedIds?), insert(text,
 *   focusedIds?), stop(), reviewApprovals(), composerIdle(), newSession(),
 *   state }
 *
 * ctx = { api, store, orb, chat, bus, mode, root, config } (index.js). The
 * view never moves the operator's view by itself: `ui` directives become bus
 * events the shell owns ('focus:entities', 'inspect', 'track:request',
 * 'track:exit'), and only for LIVE events, never for replayed history.
 *
 * opts (tests): { doc, now, clock:{setTimeout, clearTimeout, setInterval,
 *   clearInterval}, raf, reducedMotion }
 */

import {
  h as domH,
  replaceKids,
  setClass,
  setHidden,
} from '../../ui/uavDom.js';
import { createChatClient } from './client.js';
import {
  activeGrants,
  approvedSince,
  availabilityOf,
  initialState,
  latestCommandRow,
  pendingApprovals,
  rateLimit,
  reduce,
  sessionCost,
  statusWord,
} from './reducer.js';
import { assess, vehicleFacts } from './validate.js';
import { createSlip, grantPhrase, segmentNodes } from './slip.js';
import { chipRefs, renderDom } from './markdown.js';
import { createChip, createTether, lookupNode, wireRoving } from './chips.js';
import {
  CLASS_META,
  ICON,
  SENSOR_TOOLS,
  approvalVehicle,
  bidiSafe,
  bytes,
  callVehicle,
  classMeta,
  duration,
  epochMs,
  escapeBidi,
  grouped,
  kindWords,
  latestSentence,
  listWords,
  missionKind,
  stripBidi,
  tokens,
  toolTitle,
  usd,
  wholeSeconds,
  zulu,
} from './format.js';

// Every string the transcript shows (graph labels, server summaries, analyst
// args and prose) goes through this factory or setText below: no bidi
// control ever reaches the DOM (format.js stripBidi).
const h = bidiSafe(domH);

export const MAX_CHARS = 8000;
export const COUNTER_FROM = 7500;
export const ARGS_LINES = 20;
export const QUIET_MS = 3000;
/**
 * shouldAutoTrack refusals that only rule out the countdown: mode.js still
 * gets the request and shows the static toast with a Track button (§6.9).
 */
export const AUTO_TRACK_DEFERRED = new Set([
  'composer',
  'stage_input',
  'slip_pending',
  'tracking_other',
]);
const STATUS_RETRY_MS = 5000;
const NOTICE_MS = 4000;

/** Copy deck strings the view writes (spec §6, §10). */
export const COPY = Object.freeze({
  placeholder: 'Ask the analyst, or give a tasking',
  howApprovals: 'How approvals work',
  approvalsBody:
    'Reading and planning run freely. Sensor tasking, flying and simulation changes wait for your approval on a slip.',
  starting: 'Starting the analyst…',
  reconnecting: 'Reconnecting to the analyst…',
  reconnected: 'Reconnected. Nothing was missed.',
  busy: 'The analyst is still answering. Wait, or press Esc to stop it.',
  sendFailed: "Couldn't send your message.",
  maxTurns:
    'The analyst hit its 40-step limit for this request. Ask it to continue, or narrow the request.',
  keepsFlying:
    "Stopping the analyst doesn't stop an aircraft that is already flying. Use Abort for that.",
  gapTail: 'This transcript may be incomplete; ask the analyst to summarize.',
  truncated:
    'Shortened to fit. Some items were left out; ask for them by name.',
  noResult:
    'No result came back. It may still have run; check the situation rail.',
  dividerOperator:
    "New session. The analyst doesn't remember the conversation above.",
  dividerLost:
    "The analyst session ended, so a new one started. The analyst doesn't remember the conversation above.",
});

/** Unavailable reasons (spec §3e, §10). */
export const UNAVAILABLE = Object.freeze({
  sdk_missing: {
    title: "The analyst isn't installed in this build.",
    body: 'Search, the orb and the situation rail still work.',
  },
  cli_missing: {
    title:
      "The analyst needs the Claude command-line tool, and it wasn't found.",
    body: null,
  },
  disabled: { title: 'The analyst is turned off for this launch.', body: null },
  auth: { title: "The analyst isn't signed in.", body: 'auth' },
  token: {
    title:
      "The console's access token was rejected. Quit and reopen the app to get a new one.",
    body: null,
  },
  service_unavailable: {
    title: "The analyst didn't start in this app.",
    body: 'Search, the orb and the situation rail still work.',
  },
});

// ---- pure helpers (exported for tests) ---------------------------------------------------

const RUNNING_PHASES = new Set(['planning', 'executing', 'rtb']);

function graphOf(store) {
  try {
    return store?.get?.()?.graph ?? null;
  } catch {
    return null;
  }
}

function nodesOf(graph, type) {
  return (Array.isArray(graph?.nodes) ? graph.nodes : []).filter(
    (n) => n && n.type === type,
  );
}

function nameOf(node) {
  if (!node) return null;
  if (typeof node.label === 'string' && node.label) return node.label;
  const id = String(node.id || '');
  return id.includes(':') ? id.slice(id.indexOf(':') + 1) : id || null;
}

/** Suggested prompts, at most 5, exact text, chosen by state (spec §6.10). */
export function suggestedPrompts(graph) {
  const vehicles = nodesOf(graph, 'vehicle');
  const pois = nodesOf(graph, 'poi');
  const tracks = nodesOf(graph, 'track');
  const running = nodesOf(graph, 'mission').filter((m) =>
    RUNNING_PHASES.has(m.attrs?.phase),
  );
  const v = nameOf(vehicles[0]);
  const base = [
    "Summarize the situation in this theater and what we don't know yet.",
  ];
  const fuel = v
    ? `How much fuel does ${v} have above BINGO, and what's the longest mission it could fly?`
    : null;
  if (fuel) base.push(fuel);
  if (running.length) {
    const m = running[0];
    const mid = m.attrs?.mission_id || String(m.id || '').replace(/^msn:/, '');
    const mv = m.attrs?.vehicle || v || 'the aircraft';
    base.push(`How is ${mid} progressing, and when does ${mv} reach BINGO?`);
  } else {
    const where = pois[0] ? nameOf(pois[0]) : 'the AO';
    base.push(
      `Plan a grid search over ${where} at 60 m AGL and show me the dry run.`,
    );
  }
  const realOn = graph?.meta?.feeds?.real_data?.ok === true;
  base.push(
    realOn
      ? 'Which readings in this picture are assumed rather than measured?'
      : 'Which readings are assumed while the real-data layer is off?',
  );
  const swaps = [];
  if (!tracks.length) {
    if (v) swaps.push(`Scan for targets from ${v}'s current position.`);
  } else {
    swaps.push("Which contacts haven't been threat-assessed yet?");
    const assessed = Number.isFinite(graph?.meta?.threat_assessed)
      ? graph.meta.threat_assessed
      : tracks.filter((t) => t.status && t.status !== 'unknown').length;
    if (assessed > 0)
      swaps.push(
        'Write an INTREP on the assessed contacts and call out the gaps.',
      );
  }
  let list = [...base, ...swaps];
  if (list.length > 5 && fuel) list = list.filter((p) => p !== fuel);
  return list.slice(0, 5);
}

/** The empty-state heading, built from state; never a greeting (spec §3d). */
export function emptyHeading(graph) {
  if (!graph) return '';
  const parts = [];
  const theater = graph.theater?.label;
  if (theater) parts.push(`${theater}.`);
  const vehicle = nodesOf(graph, 'vehicle')[0];
  if (vehicle) {
    const name = nameOf(vehicle);
    const a = vehicle.attrs || {};
    const where =
      a.landed === true
        ? 'is on the ground'
        : a.landed === false
          ? 'is airborne'
          : null;
    const fuel = Number.isFinite(a.fuel_pct)
      ? `${Number.isInteger(a.fuel_pct) ? a.fuel_pct : a.fuel_pct.toFixed(1)}% fuel`
      : null;
    if (where && fuel) parts.push(`${name} ${where} with ${fuel}.`);
    else if (where) parts.push(`${name} ${where}.`);
    else if (fuel) parts.push(`${name} has ${fuel}.`);
  }
  const tracks = nodesOf(graph, 'track').length;
  parts.push(
    tracks
      ? `${tracks} contact${tracks === 1 ? '' : 's'} in the picture.`
      : 'Nothing observed yet.',
  );
  return parts.join(' ');
}

/**
 * Whether an auto-track notice may run (spec §6.9). All must hold: composer
 * empty and unfocused, no stage input in the last 3 s, no slip pending, not
 * already tracking another vehicle. A launch only tracks after its command
 * row is `done` (busy / not run / rejected never track).
 */
export function shouldAutoTrack({
  source,
  vehicle,
  commandState = null,
  composerText = '',
  composerFocused = false,
  lastStageInputAt = null,
  now = Date.now(),
  pendingCount = 0,
  mode = 'orb',
  trackingVehicle = null,
}) {
  if (!vehicle) return { allowed: false, reason: 'no_vehicle' };
  if (source === 'launch' && commandState !== 'done')
    return { allowed: false, reason: 'not_done' };
  const tracking = mode === 'tracking' || mode === 'entering_tracking';
  if (tracking && trackingVehicle === vehicle)
    return { allowed: false, reason: 'already' };
  if (tracking) return { allowed: false, reason: 'tracking_other' };
  if (String(composerText || '').trim() || composerFocused)
    return { allowed: false, reason: 'composer' };
  if (Number.isFinite(lastStageInputAt) && now - lastStageInputAt < QUIET_MS)
    return { allowed: false, reason: 'stage_input' };
  if (pendingCount > 0) return { allowed: false, reason: 'slip_pending' };
  return { allowed: true, reason: null };
}

/** Caveats to show as Assumed on slips while the real-data layer is off. */
export function assumedCaveats(graph) {
  const caveats = Array.isArray(graph?.meta?.caveats)
    ? graph.meta.caveats.map(String)
    : [];
  const rd = graph?.meta?.feeds?.real_data;
  if (rd && rd.ok === true) return [];
  return caveats.filter((c) => /real[- ]data/i.test(c));
}

/**
 * The detections feed, if the graph reports one. The server's key is
 * `contacts` (the orb's detectionsDown reads the same keys, in the same
 * order); `detections` and any other /detect/ key are fallbacks. A feed is up
 * only when it says ok and is not critical.
 */
export function detectionsFeed(graph) {
  const feeds = graph?.meta?.feeds;
  if (!feeds || typeof feeds !== 'object') return null;
  const isFeed = (feed) => feed && typeof feed === 'object';
  const feed =
    [feeds.contacts, feeds.detections].find(isFeed) ??
    Object.entries(feeds).find(([k, f]) => /detect/i.test(k) && isFeed(f))?.[1];
  if (!feed) return null;
  return {
    ok: feed.ok === true && feed.status !== 'critical',
    at_ms: Number.isFinite(feed.at_ms) ? feed.at_ms : null,
  };
}

/** Aircraft the graph says are flying a mission right now. */
export function flyingNow(graph) {
  const out = [];
  const seen = new Set();
  for (const m of nodesOf(graph, 'mission')) {
    const phase = m.attrs?.phase;
    const vehicle = m.attrs?.vehicle;
    if (!vehicle || !RUNNING_PHASES.has(phase) || seen.has(vehicle)) continue;
    seen.add(vehicle);
    out.push({ vehicle, kind: m.attrs?.kind ?? null });
  }
  for (const v of nodesOf(graph, 'vehicle')) {
    const name = nameOf(v);
    if (!name || seen.has(name)) continue;
    if (v.attrs?.landed === false && v.attrs?.mission) {
      seen.add(name);
      out.push({ vehicle: name, kind: null });
    }
  }
  return out;
}

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

/** Write text only when it changed: rewriting a live region (the approval
 *  bar is role=status) with identical words makes screen readers say it
 *  again on every render. */
function setText(el, text) {
  const next = stripBidi(text == null ? '' : String(text));
  if (el.textContent !== next) el.textContent = next;
}

/** The reduced-motion MediaQueryList, kept so a live system toggle counts. */
function motionQuery() {
  try {
    return globalThis.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null;
  } catch {
    return null;
  }
}

/** Block containers the streaming caret descends into (never tables). */
const CARET_INTO = new Set([
  'P',
  'UL',
  'OL',
  'LI',
  'BLOCKQUOTE',
  'H1',
  'H2',
  'H3',
  'H4',
  'PRE',
  'CODE',
]);

/**
 * Where the streaming caret goes: inside the last text-bearing block, so it
 * follows the last word instead of dropping onto its own line under a
 * paragraph or list. Falls back to the message body itself.
 */
export function caretHost(body) {
  let host = body;
  for (;;) {
    const kids = host?.children;
    const last =
      host?.lastChild !== undefined
        ? host.lastChild
        : kids && kids.length
          ? kids[kids.length - 1]
          : null;
    if (
      !last ||
      typeof last !== 'object' ||
      !CARET_INTO.has(String(last.tagName || '').toUpperCase())
    )
      return host;
    host = last;
  }
}

function jsonLines(value) {
  let text;
  try {
    text = JSON.stringify(value ?? {}, null, 2);
  } catch {
    text = String(value);
  }
  // A bidi control in the args shows as its JSON escape.
  return escapeBidi(text).split('\n');
}

// ---- the component --------------------------------------------------------------------------

let analystCounter = 0;

export function createAnalyst(host, ctx = {}, opts = {}) {
  const doc = opts.doc ?? globalThis.document;
  const now = opts.now ?? (() => Date.now());
  const clock = opts.clock ?? globalThis;
  const raf =
    opts.raf !== undefined
      ? opts.raf
      : typeof globalThis.requestAnimationFrame === 'function'
        ? globalThis.requestAnimationFrame.bind(globalThis)
        : null;
  // Reduced motion is read live (macOS toggles it without a reload), like the
  // orb and the iris; opts.reducedMotion pins it for tests.
  const motionMQ = opts.reducedMotion === undefined ? motionQuery() : null;
  const reduced = () => opts.reducedMotion ?? Boolean(motionMQ?.matches);
  const bus = ctx.bus ?? null;
  const store = ctx.store ?? null;
  const orb = ctx.orb ?? null;
  const ownClient = !ctx.chat;
  const chat =
    ctx.chat ?? (ctx.api ? createChatClient({ api: ctx.api }) : null);
  const uid = (analystCounter += 1);
  const titleId = `ic-analyst-title-${uid}`;

  let state = initialState();
  let layout = ['wide', 'compact', 'narrow'].includes(ctx.layout)
    ? ctx.layout
    : 'wide';
  let docked = false;
  // The operator just acted (sent a message, decided a slip): the next render
  // shows the newest line even if they had scrolled up (e.g. by Review). A
  // decided slip keeps revealing until it has been filed, because its
  // collapse (a render later, on approval_resolved) is what would otherwise
  // strand the operator on older turns.
  let revealLatest = false;
  const revealUntilFiled = new Set();
  let destroyed = false;
  let renderQueued = false;
  let serviceDown = false;
  // Whether /chat/status ever answered. Until it has, a failure to reach it
  // is "not available" (with Check again), never "Reconnecting": there is
  // nothing to reconnect to yet.
  let reached = false;
  let menuOpen = false;
  let contextIds = [];
  let composerNotice = null;
  let lastPendingCount = -1;
  let lastAvailKey = null;
  let tickTimer = null;
  let retryTimer = null;
  let noticeTimer = null;
  let currentMode = 'orb';
  let history = [];
  let booted = false;
  const unsubs = [];
  const cache = new Map();
  const slips = new Map();
  const snapshots = new Map();
  const befores = new Map();
  const executed = new Set();
  const trackDecisions = new Map();
  const stopFacts = new Map();
  const expanded = new Set();
  const announced = new Set();

  const tether = createTether({ root: ctx.root, host, orb });

  // ---- skeleton ----
  const modelEl = h('span', { class: 'ic-chat__model' });
  const statusText = h('span', { class: 'ic-chat__statusword' });
  const statusEl = h(
    'span',
    { class: 'ic-chat__status' },
    h('span', { class: 'ic-chat__dot', 'aria-hidden': 'true' }),
    statusText,
  );
  const costEl = h('span', { class: 'ic-chat__cost' });
  // A disclosure, not an ARIA menu: a button with aria-expanded that shows
  // two ordinary buttons next in tab order. (It had role=menu without the
  // menu keyboard pattern, so screen readers announced a menu that did not
  // behave like one.) Esc closes it and returns focus to the button.
  const menuId = `ic-analyst-menu-${uid}`;
  const menuBtn = h(
    'button',
    {
      type: 'button',
      class: 'ic-btn',
      'data-variant': 'icon',
      'aria-expanded': 'false',
      'aria-controls': menuId,
      'aria-label': 'Analyst menu',
    },
    icon(ICON.more),
  );
  const newSessionItem = h(
    'button',
    { type: 'button', class: 'ic-menu__item' },
    'New session',
  );
  const stopItem = h(
    'button',
    { type: 'button', class: 'ic-menu__item' },
    'Stop the analyst',
  );
  const menuEl = h(
    'div',
    { class: 'ic-menu', id: menuId, hidden: true },
    newSessionItem,
    stopItem,
  );
  const header = h(
    'header',
    { class: 'ic-chat__head' },
    h('h2', { class: 'ic-chat__title', id: titleId }, 'Analyst'),
    modelEl,
    statusEl,
    h('span', { class: 'ic-chat__spacer' }),
    costEl,
    menuBtn,
    menuEl,
  );
  const bannerEl = h('div', { class: 'ic-chat__banners' });
  const logEl = h('div', {
    class: 'ic-log',
    role: 'log',
    'aria-label': 'Analyst transcript',
    'aria-live': 'off',
    tabindex: '-1',
  });
  const approvalText = h('span', { class: 'ic-approvalbar__text' });
  const reviewBtn = h(
    'button',
    { type: 'button', class: 'ic-approvalbar__review' },
    'Review',
  );
  const approvalBar = h(
    'div',
    { class: 'ic-approvalbar', role: 'status', hidden: true },
    approvalText,
    reviewBtn,
  );

  const contextRow = h('div', { class: 'ic-composer__context', hidden: true });
  const textarea = h('textarea', {
    class: 'ic-composer__input',
    rows: '1',
    placeholder: COPY.placeholder,
    'aria-label': 'Message the analyst',
  });
  const sendIcon = icon(ICON.send);
  const sendLabel = h('span', { class: 'ic-vh' }, 'Send');
  const sendBtn = h(
    'button',
    { type: 'button', class: 'ic-composer__send', 'data-mode': 'send' },
    sendIcon,
    sendLabel,
  );
  sendBtn.setAttribute('aria-label', 'Send');
  const counterEl = h('p', { class: 'ic-composer__counter', hidden: true });
  const noticeEl = h('p', {
    class: 'ic-composer__notice',
    role: 'status',
    hidden: true,
  });
  const standingText = h('span', {});
  const standingNew = h(
    'button',
    { type: 'button', class: 'ic-btn', 'data-variant': 'link', hidden: true },
    'New session',
  );
  // One Revoke per granted tool (v1.1 DELETE …/grants/{tool}); shown only
  // when the server has the grants route.
  const standingRevokes = h('span', { class: 'ic-composer__revokes' });
  const standingEl = h(
    'p',
    { class: 'ic-composer__standing' },
    standingText,
    standingRevokes,
    standingNew,
  );
  const composerEl = h(
    'div',
    { class: 'ic-composer' },
    contextRow,
    h('div', { class: 'ic-composer__row' }, textarea, sendBtn),
    counterEl,
    noticeEl,
    standingEl,
  );
  const livePolite = h('div', { class: 'ic-vh', 'aria-live': 'polite' });
  const liveAssertive = h('div', {
    class: 'ic-vh',
    'aria-live': 'assertive',
  });
  // Analyst unavailable (spec §3e): at wide/compact the shell collapses this
  // column to its spine; where the column stays visible (narrow, the tracking
  // dock) the reason replaces the composer and the transcript stays readable.
  const unavailTitle = h('p', { class: 'ic-unavailable__title' });
  const unavailBody = h('p', { class: 'ic-unavailable__body' });
  const unavailHint = h('p', { class: 'ic-unavailable__hint ic-mono' });
  const checkAgainBtn = h(
    'button',
    { type: 'button', class: 'ic-btn', 'data-variant': 'primary' },
    icon(ICON.refresh),
    'Check again',
  );
  const unavailableEl = h(
    'div',
    { class: 'ic-unavailable', role: 'status', hidden: true },
    icon(ICON.forum, 'ic-unavailable__glyph'),
    unavailTitle,
    unavailBody,
    unavailHint,
    checkAgainBtn,
  );

  const column = h(
    'div',
    { class: 'ic-chat__column' },
    header,
    bannerEl,
    logEl,
    approvalBar,
    unavailableEl,
    composerEl,
  );

  const rootEl = h(
    'section',
    {
      class: 'ic-chat',
      'aria-labelledby': titleId,
      'data-available': 'unknown',
    },
    column,
    livePolite,
    liveAssertive,
  );
  host?.append?.(rootEl);

  // ---- state plumbing ----
  function dispatch(action) {
    const next = reduce(state, action);
    if (next === state) return;
    state = next;
    schedule();
  }

  function schedule() {
    if (destroyed) return;
    if (!raf) {
      render();
      return;
    }
    if (renderQueued) return;
    renderQueued = true;
    raf(() => {
      renderQueued = false;
      if (!destroyed) render();
    });
  }

  function announce(raw, { assertive = false } = {}) {
    const text = stripBidi(String(raw ?? ''));
    if (typeof ctx.announce === 'function') {
      ctx.announce(text, assertive ? 'assertive' : 'polite');
      return;
    }
    const el = assertive ? liveAssertive : livePolite;
    setText(el, '');
    setText(el, text);
  }

  function modeInfo() {
    const st = ctx.mode?.state;
    if (typeof st === 'string') {
      return { mode: st, vehicle: ctx.mode?.vehicle ?? null };
    }
    return {
      mode: st?.mode ?? currentMode,
      vehicle: st?.vehicle ?? ctx.mode?.vehicle ?? null,
    };
  }

  function vehicleNow(name) {
    if (!name) return null;
    let node = null;
    try {
      const v = store?.vehicle?.(name);
      if (v && typeof v === 'object' && (v.attrs || v.id)) node = v;
    } catch {
      node = null;
    }
    node ??= lookupNode(store, `veh:${name}`);
    // No time on "now" facts: a changing stamp would rebuild every slip on
    // every frame. Snapshots add their own `at`.
    return node ? vehicleFacts(node) : null;
  }

  // ---- chips ----
  function chipHooks() {
    return {
      store,
      onEnter(id, el) {
        try {
          orb?.highlight?.([id], { by: 'analyst' });
        } catch {
          // The orb is optional (narrow layout, tests).
        }
        if (layout === 'wide' && !docked) tether.show(el, id, 'analyst');
      },
      onLeave() {
        try {
          orb?.highlight?.([], { by: 'analyst' });
        } catch {
          // ignore
        }
        tether.hide();
      },
      onActivate(id) {
        tether.hide();
        bus?.emit?.('inspect', { id });
      },
    };
  }

  function markdownNodes(text, streaming = false) {
    const chips = [];
    const hooks = chipHooks();
    const nodes = renderDom(text, {
      streaming,
      chip: (ref) => {
        const chip = createChip(ref, hooks);
        chips.push(chip);
        return chip;
      },
    });
    return { nodes, chips };
  }

  function chipList(ids) {
    const hooks = chipHooks();
    return ids.map((id) => createChip({ id, label: null }, hooks));
  }

  // ---- element cache ----
  function cached(key, sig, build, update) {
    const hit = cache.get(key);
    if (hit && hit.sig === sig) return hit.el;
    if (hit && update) {
      update(hit.el);
      hit.sig = sig;
      return hit.el;
    }
    const el = build();
    cache.set(key, { el, sig });
    return el;
  }

  function setKids(el, kids) {
    const prev = el.__icKids;
    if (
      prev &&
      prev.length === kids.length &&
      prev.every((kid, i) => kid === kids[i])
    )
      return;
    el.__icKids = kids;
    replaceKids(el, kids);
  }

  // ---- transcript pieces ----
  function metaLine(who, at, extra = null) {
    const z = zulu(at);
    return h(
      'p',
      { class: 'ic-msg__meta' },
      who === 'Analyst'
        ? h('span', { class: 'ic-msg__dot', 'aria-hidden': 'true' })
        : null,
      h('span', { class: 'ic-msg__who' }, who),
      z ? h('time', { class: 'ic-msg__time' }, z) : null,
      extra,
    );
  }

  function operatorArticle(turn) {
    const sig = `${turn.text}|${turn.at}|${turn.focusedIds.join(',')}`;
    return cached(`op:${turn.id}`, sig, () => {
      const { nodes, chips } = markdownNodes(turn.text ?? '');
      const context = turn.focusedIds.length ? chipList(turn.focusedIds) : [];
      const contextLine = context.length
        ? h(
            'p',
            { class: 'ic-msg__context' },
            context.length === 1
              ? 'About: '
              : `With ${context.length} entities in view `,
            ...context,
          )
        : null;
      wireRoving([...chips, ...context]);
      const z = zulu(turn.at);
      return h(
        'article',
        {
          class: 'ic-msg',
          'data-role': 'you',
          'aria-label': z ? `You, ${z}` : 'You',
        },
        metaLine('You', turn.at),
        h('div', { class: 'ic-msg__body ic-md' }, ...nodes),
        contextLine,
      );
    });
  }

  function textBlock(turn, index, block, streaming) {
    const key = `tx:${turn.id}:${index}`;
    const sig = `${block.text.length}|${streaming ? 1 : 0}|${streaming && reduced() ? 1 : 0}`;
    const fill = (el) => {
      const { nodes, chips } = markdownNodes(block.text, streaming);
      el.__icChips = chips;
      replaceKids(el, nodes);
      if (streaming) {
        caretHost(el).append(
          h('span', {
            class: 'ic-caret',
            'aria-hidden': 'true',
            'data-steady': reduced() ? 'true' : null,
          }),
        );
      }
    };
    return cached(
      key,
      sig,
      () => {
        const el = h('div', { class: 'ic-md ic-msg__text' });
        fill(el);
        return el;
      },
      fill,
    );
  }

  function thinkingBlock(turn, index, block, t) {
    const key = `th:${turn.id}:${index}`;
    const open = block.open && turn.status === 'running';
    const isExpanded = expanded.has(key);
    const elapsed =
      Number.isFinite(block.at) && open ? wholeSeconds(t - block.at) : '';
    const sig = `${open}|${block.text.length}|${elapsed}|${isExpanded}`;
    return cached(key, sig, () => {
      if (open) {
        // One truncated line (chat.css): the sentence is its own flex item
        // so it can ellipsize; the full sentence is its tooltip.
        const sentence = latestSentence(block.text);
        return h(
          'p',
          { class: 'ic-thinking', 'data-open': 'true' },
          h(
            'span',
            { class: 'ic-thinking__text', title: sentence || null },
            `Thinking: ${sentence}`,
          ),
          elapsed ? h('span', { class: 'ic-thinking__time' }, elapsed) : null,
        );
      }
      const span =
        Number.isFinite(block.at) && Number.isFinite(block.endAt)
          ? `, ${wholeSeconds(block.endAt - block.at)}`
          : '';
      const toggle = h(
        'button',
        {
          type: 'button',
          class: 'ic-thinking__toggle',
          'aria-expanded': isExpanded ? 'true' : 'false',
        },
        `Reasoning summary${span}`,
        icon(ICON.expand, 'ic-row__chevron'),
      );
      toggle.addEventListener('click', () => {
        if (expanded.has(key)) expanded.delete(key);
        else expanded.add(key);
        schedule();
      });
      const body = isExpanded
        ? h(
            'div',
            { class: 'ic-thinking__body ic-md' },
            ...markdownNodes(block.text).nodes,
          )
        : null;
      return h(
        'div',
        { class: 'ic-thinking', 'data-open': 'false' },
        toggle,
        body,
      );
    });
  }

  function rowStateWord(row, t) {
    switch (row.state) {
      case 'running':
      case 'granted':
        return Number.isFinite(row.runAt ?? row.at)
          ? `Running, ${wholeSeconds(t - (row.runAt ?? row.at))}`
          : 'Running';
      case 'awaiting':
        return 'Waiting for your approval';
      case 'done':
        return Number.isFinite(row.runAt ?? row.at) &&
          Number.isFinite(row.endAt)
          ? `Done, ${duration(row.endAt - (row.runAt ?? row.at))}`
          : 'Done';
      case 'rejected':
        return 'Rejected';
      case 'not_run':
        return 'Not run';
      case 'failed':
        return 'Failed';
      case 'denied':
        return 'Denied';
      case 'expired':
        return 'Expired';
      case 'cancelled':
        return 'Cancelled';
      case 'no_result':
        return 'No result';
      default:
        return '';
    }
  }

  function grantedLine(row) {
    const grant = state.grants[row.tool];
    if (!grant) return 'Allowed by your standing approval for sensor tasking.';
    const all = SENSOR_TOOLS.every((tool) => state.grants[tool]);
    return all
      ? 'Allowed by your standing approval for sensor tasking.'
      : `Allowed by your standing approval for ${toolTitle(row.tool)}.`;
  }

  function outcomeNodes(row) {
    const r = row.result;
    const out = [];
    if (row.viaGrant)
      out.push(h('p', { class: 'ic-row__line' }, grantedLine(row)));
    switch (row.state) {
      case 'done':
        if (r?.summary)
          out.push(
            h(
              'p',
              { class: 'ic-row__line' },
              h(
                'span',
                { class: 'ic-row__result' },
                ...segmentNodes(r.summary),
              ),
            ),
          );
        break;
      case 'rejected':
        out.push(
          h(
            'p',
            { class: 'ic-row__line', 'data-tone': 'warn' },
            'Rejected by the server gate. ',
            r?.summary
              ? h(
                  'span',
                  { class: 'ic-row__result' },
                  ...segmentNodes(r.summary),
                )
              : null,
            ' Nothing was run.',
          ),
        );
        break;
      case 'not_run':
        out.push(
          h(
            'p',
            { class: 'ic-row__line', 'data-tone': 'warn' },
            'Not run. ',
            r?.summary || '',
            ' Nothing was sent.',
          ),
        );
        break;
      case 'failed':
        out.push(
          h(
            'p',
            { class: 'ic-row__line', 'data-tone': 'critical' },
            `Failed: ${r?.error || r?.summary || 'no detail from the server'}`,
          ),
        );
        break;
      case 'no_result':
        out.push(
          h('p', { class: 'ic-row__line', 'data-tone': 'warn' }, COPY.noResult),
        );
        break;
      default:
        break;
    }
    return out;
  }

  function rowBody(row) {
    const key = `row:${row.callId}`;
    const lines = jsonLines(row.args);
    const all = expanded.has(`${key}:args`);
    const shown = all ? lines : lines.slice(0, ARGS_LINES);
    const nodes = [
      h('p', { class: 'ic-row__tool ic-mono' }, row.tool),
      h('pre', { class: 'ic-row__args' }, h('code', {}, shown.join('\n'))),
    ];
    if (!all && lines.length > ARGS_LINES) {
      const more = h(
        'button',
        { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
        'Show all',
      );
      more.addEventListener('click', () => {
        expanded.add(`${key}:args`);
        schedule();
      });
      nodes.push(more);
    }
    const r = row.result;
    if (r) {
      nodes.push(
        h(
          'p',
          { class: 'ic-row__result' },
          ...segmentNodes(r.summary),
          Number.isFinite(r.bytes)
            ? h('span', { class: 'ic-row__size' }, bytes(r.bytes))
            : null,
        ),
      );
      if (r.entities.length) {
        const chips = chipList(r.entities);
        wireRoving(chips);
        nodes.push(h('p', { class: 'ic-row__chips' }, ...chips));
      }
      if (r.truncated)
        nodes.push(h('p', { class: 'ic-row__line' }, COPY.truncated));
    }
    return nodes;
  }

  function toolRow(row, t) {
    const key = `row:${row.callId}`;
    const isOpen = expanded.has(key);
    const ticking = row.state === 'running' || row.state === 'granted';
    const sig = JSON.stringify([
      row.state,
      row.title,
      row.summary,
      row.result,
      row.endAt,
      row.viaGrant,
      isOpen,
      expanded.has(`${key}:args`),
      ticking ? Math.floor((t - (row.runAt ?? row.at ?? t)) / 1000) : 0,
      Boolean(state.grants[row.tool]),
    ]);
    const fill = (el) => {
      const parts = el.__icParts;
      const meta = classMeta(row.klass);
      el.setAttribute('data-class', row.klass);
      el.setAttribute('data-state', row.state);
      parts.head.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      replaceKids(parts.head, [
        icon(meta.icon, 'ic-row__glyph'),
        h('span', { class: 'ic-row__title' }, row.title || toolTitle(row.tool)),
        h('span', { class: 'ic-row__summary' }, ...segmentNodes(row.summary)),
        h('span', { class: 'ic-row__state' }, rowStateWord(row, t)),
        icon(ICON.expand, 'ic-row__chevron'),
      ]);
      replaceKids(parts.outcome, outcomeNodes(row));
      replaceKids(parts.body, isOpen ? rowBody(row) : []);
      setHidden(parts.body, !isOpen);
    };
    return cached(
      key,
      sig,
      () => {
        const head = h('button', {
          type: 'button',
          class: 'ic-row__head',
          'aria-expanded': 'false',
        });
        head.addEventListener('click', () => {
          if (expanded.has(key)) expanded.delete(key);
          else expanded.add(key);
          schedule();
        });
        const outcome = h('div', { class: 'ic-row__outcome' });
        const body = h('div', { class: 'ic-row__body', hidden: true });
        const el = h('div', { class: 'ic-row' }, head, outcome, body);
        el.__icParts = { head, outcome, body };
        fill(el);
        return el;
      },
      fill,
    );
  }

  function foldRow(rows, t) {
    const key = `fold:${rows[0].callId}`;
    const isOpen = expanded.has(key);
    const running = rows.some((r) => r.state === 'running');
    const first = rows[0].at;
    const last = rows.reduce(
      (m, r) => (Number.isFinite(r.endAt) ? Math.max(m, r.endAt) : m),
      -Infinity,
    );
    const span =
      !running && Number.isFinite(first) && Number.isFinite(last)
        ? duration(last - first)
        : '';
    const label = running
      ? `Looking at ${rows.length} sources`
      : `Looked at ${rows.length} sources`;
    const inner = isOpen ? rows.map((r) => toolRow(r, t)) : [];
    const sig = `${label}|${span}|${isOpen}|${inner.length}`;
    const el = cached(key, sig, () => {
      const head = h(
        'button',
        {
          type: 'button',
          class: 'ic-fold__head',
          'aria-expanded': isOpen ? 'true' : 'false',
        },
        icon(ICON.read, 'ic-row__glyph'),
        h('span', { class: 'ic-row__title' }, label),
        span ? h('span', { class: 'ic-row__state' }, span) : null,
        icon(ICON.expand, 'ic-row__chevron'),
      );
      head.addEventListener('click', () => {
        if (expanded.has(key)) expanded.delete(key);
        else expanded.add(key);
        schedule();
      });
      const list = h('div', { class: 'ic-fold__rows', hidden: !isOpen });
      const wrap = h(
        'div',
        { class: 'ic-fold', 'data-class': 'read' },
        head,
        list,
      );
      wrap.__icList = list;
      return wrap;
    });
    if (isOpen) setKids(el.__icList, inner);
    return el;
  }

  function slipModel(a, t) {
    const vehicle = approvalVehicle(a);
    const pending = pendingApprovals(state);
    const index = pending.findIndex((p) => p.id === a.id);
    const conflicts =
      vehicle && a.klass === 'command'
        ? pending
            .filter(
              (p) =>
                p.id !== a.id &&
                p.klass === 'command' &&
                approvalVehicle(p) === vehicle,
            )
            .map((p) => ({ title: p.title }))
        : [];
    const graph = graphOf(store);
    return {
      approval: a,
      assessment: assessApproval(a),
      vehicle: vehicleNow(vehicle),
      before: befores.get(a.id) ?? null,
      queue: index >= 0 ? { index: index + 1, total: pending.length } : null,
      conflicts: index >= 0 ? conflicts : [],
      caveats: assumedCaveats(graph),
      detections: a.klass === 'sensor' ? detectionsFeed(graph) : null,
      now: t,
      reducedMotion: reduced(),
    };
  }

  function assessApproval(a) {
    const vehicle = approvalVehicle(a);
    const kind = missionKind(a.tool, a.args);
    const snap = snapshots.get(`${kind}|${vehicle}`) ?? null;
    const ref = snap?.at ?? a.dry_run?.at_ms ?? null;
    const facts = vehicleNow(vehicle);
    // The time matters here only: fuel is judged against the measured burn
    // since the snapshot (validate.js expectedFuel).
    return assess(
      a,
      snap,
      facts ? { ...facts, at: now() } : null,
      approvedSince(state, ref, a.id),
    );
  }

  function slipFor(a, t) {
    const model = slipModel(a, t);
    let slip = slips.get(a.id);
    if (!slip) {
      slip = createSlip(model, {
        decide: (decision, note) => decide(a.id, decision, note),
        revalidate: () => {
          const current = state.approvals[a.id];
          return current ? assessApproval(current) : null;
        },
        announce,
        clock,
        doc,
      });
      slips.set(a.id, slip);
    } else slip.update(model);
    return slip.el;
  }

  function directiveLine(d, key) {
    const decision = trackDecisions.get(d.seq);
    const sig = JSON.stringify([
      d.action,
      d.ids,
      d.id,
      d.vehicle,
      decision,
      d.note,
    ]);
    return cached(key, sig, () => {
      const kids = [];
      if (d.action === 'focus') {
        const n = d.ids.length;
        const single = n === 1 ? lookupNode(store, d.ids[0]) : null;
        const what = n === 1 ? single?.label || d.ids[0] : `${n} entities`;
        kids.push(`Pointed at ${what} on the orb.`);
        if (n) {
          const again = h(
            'button',
            { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
            'Show again',
          );
          again.addEventListener('click', () =>
            bus?.emit?.('focus:entities', {
              ids: d.ids,
              by: 'analyst',
              note: stripBidi(d.note) ?? undefined,
            }),
          );
          kids.push(' ', again);
        }
      } else if (d.action === 'inspect') {
        const node = lookupNode(store, d.id);
        kids.push(
          `Opened ${node?.label || d.id || 'an entity'} in the inspector.`,
        );
      } else if (d.action === 'track') {
        const v = d.vehicle || 'the aircraft';
        const reason = d.reason ? `: ${d.reason}` : '';
        if (
          !decision ||
          decision.allowed ||
          ['already', 'not_done', 'no_vehicle'].includes(decision.reason)
        ) {
          kids.push(`Asked to watch ${v}${reason}.`);
        } else if (decision.source === 'launch') {
          kids.push(
            decision.title
              ? `Launched: ${decision.title} with ${v}.`
              : `Asked to watch ${v}${reason}.`,
          );
        } else {
          kids.push(
            `The analyst suggests watching ${v}${d.reason ? `: “${d.reason}”` : ''}.`,
          );
        }
        const offerTrack =
          d.vehicle &&
          (!decision ||
            (!decision.allowed &&
              !['already', 'not_done'].includes(decision.reason)));
        if (offerTrack) {
          const track = h(
            'button',
            { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
            icon(ICON.track),
            `Track ${d.vehicle}`,
          );
          track.addEventListener('click', () =>
            bus?.emit?.('track:request', {
              vehicle: d.vehicle,
              source: 'operator',
              reason: stripBidi(d.reason) ?? undefined,
            }),
          );
          kids.push(' ', track);
        }
      } else if (d.action === 'orb') {
        kids.push('Asked to return to the orb.');
      } else {
        kids.push(`The analyst sent an unknown view request (${d.action}).`);
      }
      return h(
        'p',
        { class: 'ic-directive', 'data-action': d.action },
        ...kids,
      );
    });
  }

  function errorLine(block, key, canRetry) {
    const sig = `${block.message}|${block.hint}|${canRetry}`;
    return cached(key, sig, () => {
      const kids = [h('span', {}, block.message)];
      if (block.hint)
        kids.push(h('span', { class: 'ic-line__hint ic-mono' }, block.hint));
      if (canRetry) {
        const retry = h(
          'button',
          { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
          icon(ICON.refresh),
          'Try again',
        );
        retry.addEventListener('click', () => retryLast());
        kids.push(retry);
      }
      return h(
        'p',
        { class: 'ic-line', 'data-tone': 'critical', role: 'alert' },
        ...kids,
      );
    });
  }

  function stoppedLine(turn, block, key) {
    if (!stopFacts.has(turn.id)) {
      stopFacts.set(
        turn.id,
        block.at != null && !turn.replay ? flyingNow(graphOf(store)) : null,
      );
    }
    const flying = stopFacts.get(turn.id);
    const sig = JSON.stringify([block.at, flying]);
    return cached(key, sig, () => {
      const z = zulu(block.at, { seconds: true });
      const kids = [
        h('span', {}, z ? `Stopped by you at ${z}.` : 'Stopped by you.'),
      ];
      if (flying && flying.length) {
        for (const f of flying) {
          const text = f.kind
            ? `${f.vehicle}'s ${kindWords(f.kind)} keeps flying. Use Abort to stop the aircraft.`
            : `${f.vehicle} keeps flying. Use Abort to stop the aircraft.`;
          const abort = h(
            'button',
            { type: 'button', class: 'ic-btn', 'data-variant': 'danger' },
            icon(ICON.abort),
            `Abort ${f.vehicle}`,
          );
          abort.addEventListener('click', () =>
            bus?.emit?.('abort:request', { vehicle: f.vehicle }),
          );
          kids.push(h('span', { class: 'ic-line__more' }, text), abort);
        }
      } else {
        kids.push(h('span', { class: 'ic-line__more' }, COPY.keepsFlying));
      }
      return h('p', { class: 'ic-line', 'data-tone': 'info' }, ...kids);
    });
  }

  function isFoldable(row) {
    return Boolean(
      row && (row.klass === 'read' || row.klass === 'plan') && !row.approvalId,
    );
  }

  function blockEls(turn, t) {
    const out = [];
    const { blocks } = turn;
    const lastTurn = lastTurnId();
    let i = 0;
    while (i < blocks.length) {
      const b = blocks[i];
      if (b.kind === 'tool') {
        const row = state.rows[b.callId];
        if (isFoldable(row)) {
          const group = [];
          while (
            i < blocks.length &&
            blocks[i].kind === 'tool' &&
            isFoldable(state.rows[blocks[i].callId])
          ) {
            group.push(state.rows[blocks[i].callId]);
            i += 1;
          }
          out.push(
            group.length >= 2 ? foldRow(group, t) : toolRow(group[0], t),
          );
          continue;
        }
        if (row?.approvalId && state.approvals[row.approvalId]) {
          const a = state.approvals[row.approvalId];
          out.push(slipFor(a, t));
          if (a.state !== 'pending' && a.state !== 'deciding')
            out.push(toolRow(row, t));
        } else if (row) out.push(toolRow(row, t));
      } else if (b.kind === 'text') {
        const streaming = turn.status === 'running' && i === blocks.length - 1;
        out.push(textBlock(turn, i, b, streaming));
      } else if (b.kind === 'thinking') {
        out.push(thinkingBlock(turn, i, b, t));
      } else if (b.kind === 'directive') {
        out.push(directiveLine(b, `dir:${turn.id}:${i}`));
      } else if (b.kind === 'error') {
        out.push(
          errorLine(
            b,
            `err:${turn.id}:${i}`,
            b.retryable && turn.id === lastTurn && !state.running,
          ),
        );
      } else if (b.kind === 'stopped') {
        out.push(stoppedLine(turn, b, `stop:${turn.id}:${i}`));
      }
      i += 1;
    }
    if (turn.status === 'max_turns') {
      out.push(
        cached(`max:${turn.id}`, '1', () =>
          h('p', { class: 'ic-line', 'data-tone': 'warn' }, COPY.maxTurns),
        ),
      );
    }
    if (turn.status === 'error' && !blocks.some((b) => b.kind === 'error')) {
      out.push(
        errorLine(
          {
            message: `The analyst stopped with an error: ${turn.error || 'no detail'}.`,
            hint: null,
          },
          `terr:${turn.id}`,
          turn.id === lastTurn && !state.running,
        ),
      );
    }
    if (turn.status === 'running' && !blocks.length) {
      out.push(
        cached(`wait:${turn.id}:${state.turnsEnded === 0}`, '1', () =>
          h(
            'p',
            { class: 'ic-thinking', 'data-open': 'true' },
            state.turnsEnded === 0 ? COPY.starting : 'Thinking',
          ),
        ),
      );
    }
    return out;
  }

  function usageFooter(turn) {
    const u = state.usage.turns[turn.id] || {};
    const parts = [];
    if (Number.isFinite(turn.at) && Number.isFinite(turn.endAt))
      parts.push(duration(turn.endAt - turn.at));
    if (Number.isFinite(u.input_tokens) || Number.isFinite(u.output_tokens)) {
      parts.push(
        `${tokens(u.input_tokens ?? 0)} in, ${tokens(u.output_tokens ?? 0)} out`,
      );
    }
    if (Number.isFinite(u.cost_usd)) parts.push(usd(u.cost_usd));
    return parts;
  }

  function analystArticle(turn, t) {
    const key = `an:${turn.id}`;
    let entry = cache.get(key);
    if (!entry) {
      const meta = h('div', { class: 'ic-msg__metaslot' });
      const blocksHost = h('div', { class: 'ic-msg__blocks' });
      const footer = h('div', { class: 'ic-msg__footer' });
      const el = h(
        'article',
        { class: 'ic-msg', 'data-role': 'analyst' },
        meta,
        blocksHost,
        footer,
      );
      entry = {
        el,
        sig: null,
        meta,
        blocksHost,
        footer,
        metaKey: null,
        footKey: null,
      };
      cache.set(key, entry);
    }
    const { el } = entry;
    const running = turn.status === 'running';
    el.setAttribute('aria-busy', running ? 'true' : 'false');
    const z = zulu(turn.replyAt ?? turn.at);
    el.setAttribute('aria-label', z ? `Analyst, ${z}` : 'Analyst');
    const metaKey = `${turn.replyAt ?? turn.at}`;
    if (entry.metaKey !== metaKey) {
      entry.metaKey = metaKey;
      replaceKids(entry.meta, [metaLine('Analyst', turn.replyAt ?? turn.at)]);
    }
    const kids = blockEls(turn, t);
    setKids(entry.blocksHost, kids);
    const chips = [];
    for (const kid of kids) if (kid.__icChips) chips.push(...kid.__icChips);
    wireRoving(chips);
    const refs = running
      ? []
      : chipRefs(
          turn.blocks
            .filter((b) => b.kind === 'text')
            .map((b) => b.text)
            .join('\n'),
        );
    const foot = running ? [] : usageFooter(turn);
    const footKey = JSON.stringify([foot, refs.map((r) => r.id)]);
    if (entry.footKey !== footKey) {
      entry.footKey = footKey;
      const nodes = [];
      if (refs.length >= 2) {
        const show = h(
          'button',
          { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
          `Show all ${refs.length} on the orb`,
        );
        show.addEventListener('click', () =>
          bus?.emit?.('focus:entities', {
            ids: refs.map((r) => r.id),
            by: 'operator',
          }),
        );
        nodes.push(h('p', { class: 'ic-msg__showall' }, show));
      }
      if (foot.length) {
        nodes.push(
          h(
            'p',
            { class: 'ic-msg__usage' },
            ...foot.map((part) => h('span', { class: 'ic-seg' }, part)),
          ),
        );
      }
      replaceKids(entry.footer, nodes);
    }
    return el;
  }

  function lastTurnId() {
    for (let i = state.items.length - 1; i >= 0; i -= 1) {
      if (state.items[i].kind === 'turn') return state.items[i].id;
    }
    return null;
  }

  function gapLine(index) {
    const gap = state.gaps[index] || {};
    let from = zulu(gap.fromAt);
    let to = zulu(gap.toAt);
    if (from && from === to) {
      // Inside one minute: "from 12:29Z to 12:29Z" reads as no gap at all.
      from = zulu(gap.fromAt, { seconds: true });
      to = zulu(gap.toAt, { seconds: true });
    }
    let text;
    if (gap.start)
      text = `The start of this session is no longer available. ${COPY.gapTail}`;
    else if (from && from === to)
      text = `Reconnected, but updates around ${from} were lost. ${COPY.gapTail}`;
    else if (from && to)
      text = `Reconnected, but updates from ${from} to ${to} were lost. ${COPY.gapTail}`;
    else text = `Reconnected, but some updates were lost. ${COPY.gapTail}`;
    return cached(`gap:${index}`, text, () =>
      h('p', { class: 'ic-line', 'data-tone': 'warn', role: 'note' }, text),
    );
  }

  function dividerLine(item, index) {
    const text =
      item.reason === 'lost' ? COPY.dividerLost : COPY.dividerOperator;
    return cached(`div:${index}`, text, () =>
      h('p', { class: 'ic-divider', role: 'separator' }, text),
    );
  }

  function outboxArticle() {
    const o = state.outbox;
    const sig = JSON.stringify([o.text, o.status, o.error, o.focusedIds]);
    return cached('outbox', sig, () => {
      const kids = [
        metaLine(
          'You',
          null,
          o.status === 'failed'
            ? null
            : h('span', { class: 'ic-msg__time' }, 'Sending…'),
        ),
        h(
          'div',
          { class: 'ic-msg__body ic-md' },
          ...markdownNodes(o.text).nodes,
        ),
      ];
      if (o.status === 'failed') {
        const retry = h(
          'button',
          { type: 'button', class: 'ic-btn', 'data-variant': 'link' },
          icon(ICON.refresh),
          'Retry',
        );
        retry.addEventListener('click', () => {
          const { text, focusedIds } = state.outbox || {};
          dispatch({ type: 'clear_outbox' });
          if (text) sendText(text, focusedIds || []);
        });
        kids.push(
          h(
            'p',
            { class: 'ic-line', 'data-tone': 'critical', role: 'alert' },
            COPY.sendFailed,
            o.error ? h('span', { class: 'ic-line__hint' }, o.error) : null,
            ' ',
            retry,
          ),
        );
      }
      return h(
        'article',
        {
          class: 'ic-msg',
          'data-role': 'you',
          'data-pending': 'true',
          'aria-label': 'You, sending',
        },
        ...kids,
      );
    });
  }

  function emptyState() {
    const graph = graphOf(store);
    const heading = emptyHeading(graph);
    // Prompts insert into the composer; with the analyst unavailable there
    // is no composer to insert into, so none are offered.
    const prompts =
      availabilityOf(state).available === false ? [] : suggestedPrompts(graph);
    const sig = JSON.stringify([heading, prompts]);
    return cached('empty', sig, () => {
      const swatches = ['sensor', 'command', 'sim', 'safety_override'].map(
        (k) =>
          h(
            'li',
            { class: 'ic-swatch', 'data-class': k },
            h('span', { class: 'ic-swatch__band', 'aria-hidden': 'true' }),
            icon(CLASS_META[k].icon),
            CLASS_META[k].phrase,
          ),
      );
      const promptButtons = prompts.map((text) => {
        const b = h('button', { type: 'button', class: 'ic-prompt' }, text);
        b.addEventListener('click', () => insertText(text));
        return b;
      });
      return h(
        'div',
        { class: 'ic-empty' },
        heading ? h('p', { class: 'ic-empty__heading' }, heading) : null,
        h('h3', { class: 'ic-empty__title' }, COPY.howApprovals),
        h('p', { class: 'ic-empty__body' }, COPY.approvalsBody),
        h('ul', { class: 'ic-swatches' }, ...swatches),
        promptButtons.length
          ? h(
              'div',
              {
                class: 'ic-prompts',
                role: 'group',
                'aria-label': 'Suggested prompts',
              },
              ...promptButtons,
            )
          : null,
      );
    });
  }

  function renderTranscript(t) {
    // Measure BEFORE any child updates: a slip collapsing or streamed text
    // growing in place (both happen while `els` is built) would otherwise read
    // as the operator having scrolled up, and the log would stop following.
    const scrolled =
      Number.isFinite(logEl.scrollHeight) &&
      Number.isFinite(logEl.scrollTop) &&
      Number.isFinite(logEl.clientHeight);
    const atBottom = scrolled
      ? logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 32
      : false;
    const els = [];
    state.items.forEach((item, index) => {
      if (item.kind === 'turn') {
        const turn = state.turns[item.id];
        if (!turn) return;
        if (turn.text != null) els.push(operatorArticle(turn));
        if (
          turn.blocks.length ||
          turn.status === 'running' ||
          turn.status !== 'end'
        )
          els.push(analystArticle(turn, t));
      } else if (item.kind === 'gap') els.push(gapLine(item.index));
      else if (item.kind === 'error')
        els.push(errorLine(item, `ierr:${index}`, false));
      else if (item.kind === 'directive')
        els.push(directiveLine(item, `idir:${index}`));
      else if (item.kind === 'tool') {
        const row = state.rows[item.callId];
        if (row?.approvalId && state.approvals[row.approvalId])
          els.push(slipFor(state.approvals[row.approvalId], t));
        if (row) els.push(toolRow(row, t));
      } else if (item.kind === 'divider') els.push(dividerLine(item, index));
    });
    if (state.outbox && state.outbox.status !== 'done')
      els.push(outboxArticle());
    const empty = !els.length;
    if (empty) els.push(emptyState());
    setKids(logEl, els);
    // The empty state reads from its heading down; a transcript follows its
    // newest line while the operator is at the bottom.
    const reveal = revealLatest || revealUntilFiled.size > 0;
    if (empty) {
      if (!logEl.__icEmpty) logEl.scrollTop = 0;
    } else if (atBottom || reveal) logEl.scrollTop = logEl.scrollHeight;
    if (!empty) {
      revealLatest = false;
      for (const id of [...revealUntilFiled]) {
        const st = state.approvals[id]?.state;
        if (st !== 'pending' && st !== 'deciding') revealUntilFiled.delete(id);
      }
    }
    logEl.__icEmpty = empty;
    // Drop slips for approvals that are no longer in the transcript.
    for (const [id, slip] of slips) {
      if (!state.approvals[id]) {
        slip.destroy();
        slips.delete(id);
      }
    }
  }

  // ---- header, banners, approval bar, composer ----
  function renderHeader(t) {
    setText(modelEl, state.session?.model || state.availability?.model || '');
    const word = serviceDown ? 'Reconnecting' : statusWord(state, t);
    setText(statusText, word);
    rootEl.setAttribute(
      'data-status',
      word.toLowerCase().replace(/[^a-z]+/g, '-'),
    );
    const hasCost =
      state.usage.serverSessionCost != null || state.usage.sessionCost > 0;
    setText(costEl, hasCost ? `Session ${usd(sessionCost(state))}` : '');
    stopItem.setAttribute('aria-disabled', state.running ? 'false' : 'true');
    menuBtn.setAttribute('aria-expanded', menuOpen ? 'true' : 'false');
    setHidden(menuEl, !menuOpen);
  }

  function renderBanners(t) {
    const kids = [];
    const rl = rateLimit(state, t);
    if (serviceDown || state.connection.reconnecting) {
      kids.push(
        h(
          'p',
          { class: 'ic-chat__banner', 'data-tone': 'warn', role: 'status' },
          icon(ICON.offline),
          COPY.reconnecting,
        ),
      );
    } else if (state.notice?.kind === 'reconnected') {
      kids.push(
        h(
          'p',
          { class: 'ic-chat__banner', 'data-tone': 'ok', role: 'status' },
          COPY.reconnected,
        ),
      );
    }
    if (rl.level === 'warning') {
      const z = zulu(rl.resetsAt);
      kids.push(
        h(
          'p',
          { class: 'ic-chat__banner', 'data-tone': 'warn', role: 'status' },
          `Close to the usage limit.${z ? ` It resets at ${z}.` : ''}`,
        ),
      );
    }
    const sig = JSON.stringify([
      serviceDown,
      state.connection.reconnecting,
      state.notice,
      rl,
    ]);
    if (bannerEl.__icSig !== sig) {
      bannerEl.__icSig = sig;
      replaceKids(bannerEl, kids);
    }
  }

  function renderApprovalBar() {
    const pending = pendingApprovals(state);
    setHidden(approvalBar, pending.length === 0);
    if (!pending.length) return;
    const oldest = pending[0];
    const v = approvalVehicle(oldest);
    const what = `${oldest.title}${v ? `, ${v}` : ''}`;
    setText(
      approvalText,
      pending.length === 1
        ? `Approval waiting: ${what}.`
        : `${pending.length} approvals waiting. Oldest: ${what}.`,
    );
  }

  function standingLine() {
    const grants = activeGrants(state);
    if (!grants.length) return 'Standing approval: none';
    const tools = grants.map((g) => g.tool);
    const all = SENSOR_TOOLS.every((tool) => tools.includes(tool));
    const words = all
      ? 'sensor tasking'
      : listWords(tools.map((tool) => toolTitle(tool)));
    return `Standing approval: ${words}, until you start a new session.`;
  }

  function composerBlockReason(t) {
    const av = availabilityOf(state);
    if (av.available === false) return 'unavailable';
    if (rateLimit(state, t).level === 'reached') return 'rate_limited';
    return null;
  }

  function renderComposer(t) {
    const running = Boolean(state.running);
    const text = String(textarea.value ?? '');
    const len = text.length;
    const mode = running ? 'stop' : 'send';
    if (sendBtn.getAttribute('data-mode') !== mode) {
      sendBtn.setAttribute('data-mode', mode);
      replaceKids(sendBtn, [icon(running ? ICON.stop : ICON.send), sendLabel]);
      setText(sendLabel, running ? 'Stop the analyst' : 'Send');
    }
    sendBtn.setAttribute('aria-label', running ? 'Stop the analyst' : 'Send');
    const blocked = composerBlockReason(t);
    const cannotSend =
      !running &&
      (blocked != null ||
        !text.trim() ||
        len > MAX_CHARS ||
        state.outbox?.status === 'sending');
    sendBtn.setAttribute('aria-disabled', cannotSend ? 'true' : 'false');
    setHidden(counterEl, len < COUNTER_FROM);
    if (len >= COUNTER_FROM) {
      setText(counterEl, `${grouped(len)} of ${grouped(MAX_CHARS)}`);
      counterEl.setAttribute('data-over', len > MAX_CHARS ? 'true' : 'false');
    }
    let notice = composerNotice;
    const rl = rateLimit(state, t);
    if (rl.level === 'reached') {
      const z = zulu(rl.resetsAt);
      const mins = Number.isFinite(rl.resetsAt)
        ? Math.max(1, Math.round((rl.resetsAt - t) / 60_000))
        : null;
      notice = {
        tone: 'warn',
        text: `Usage limit reached.${z ? ` The analyst can answer again at ${z}${mins ? `, in ${mins} min` : ''}.` : ''} Search, the orb and the rail keep working. Your draft is kept.`,
      };
    }
    setHidden(noticeEl, !notice);
    if (notice) {
      setText(noticeEl, notice.text);
      noticeEl.setAttribute('data-tone', notice.tone || 'info');
    }
    // Context row: "About: (SA-6 battery ×)".
    setHidden(contextRow, contextIds.length === 0);
    const ctxSig = contextIds.join('|');
    if (contextRow.__icSig !== ctxSig) {
      contextRow.__icSig = ctxSig;
      const hooks = chipHooks();
      const pills = contextIds.map((id) => {
        const chip = createChip({ id, label: null }, hooks);
        const remove = h(
          'button',
          {
            type: 'button',
            class: 'ic-chip__remove',
            'aria-label': `Remove ${id} from the context`,
          },
          icon(ICON.close, 'ic-chip__x'),
        );
        remove.addEventListener('click', () => {
          contextIds = contextIds.filter((x) => x !== id);
          schedule();
        });
        return h('span', { class: 'ic-chip-pill' }, chip, remove);
      });
      replaceKids(contextRow, [
        h('span', { class: 'ic-composer__about' }, 'About:'),
        ...pills,
      ]);
    }
    renderStanding();
  }

  function renderStanding() {
    setText(standingText, standingLine());
    const grants = activeGrants(state);
    setHidden(standingNew, grants.length === 0);
    const canRevoke =
      state.grantsSupported === true && typeof chat?.revokeGrant === 'function';
    const tools = canRevoke ? grants.map((g) => g.tool) : [];
    const sig = tools.join('|');
    if (standingRevokes.__icSig === sig) return;
    standingRevokes.__icSig = sig;
    replaceKids(
      standingRevokes,
      tools.map((tool) => {
        const title = toolTitle(tool);
        const b = h(
          'button',
          {
            type: 'button',
            class: 'ic-btn',
            'data-variant': 'link',
            'data-tool': tool,
            'aria-label': `Revoke the standing approval for ${title}`,
          },
          tools.length === 1 ? 'Revoke' : `Revoke ${title}`,
        );
        b.addEventListener('click', () => revokeGrant(tool));
        return b;
      }),
    );
  }

  async function revokeGrant(tool) {
    if (!chat?.revokeGrant) return;
    const title = toolTitle(tool);
    try {
      await chat.revokeGrant(tool);
      dispatch({ type: 'revoke', tool });
      composerNotice = null;
      announce(
        `Standing approval for ${title} revoked. It will ask again next time.`,
      );
      refreshGrants();
    } catch {
      composerNotice = {
        tone: 'warn',
        text: `Couldn't revoke the standing approval for ${title}. Start a new session to end it.`,
      };
    }
    schedule();
  }

  function renderAvailability() {
    const av = availabilityOf(state);
    const off = av.available === false;
    rootEl.setAttribute(
      'data-available',
      off ? 'false' : av.available ? 'true' : 'unknown',
    );
    setHidden(unavailableEl, !off);
    setHidden(composerEl, off);
    const key = `${off}|${av.reason}|${av.hint}`;
    if (key !== lastAvailKey) {
      lastAvailKey = key;
      if (av.available != null) {
        bus?.emit?.('analyst:availability', {
          available: !off,
          reason: off ? (av.reason ?? null) : null,
          hint: off ? (av.hint ?? null) : null,
        });
      }
    }
    if (!off) return;
    const copy = UNAVAILABLE[av.reason] || {
      title: "The analyst isn't available right now.",
      body: 'Search, the orb and the situation rail still work.',
    };
    setText(unavailTitle, copy.title);
    if (copy.body === 'auth') {
      replaceKids(unavailBody, [
        'Sign in with the claude CLI (',
        h('code', {}, 'claude'),
        ', then /login) or set ANTHROPIC_API_KEY.',
      ]);
      setHidden(unavailBody, false);
    } else {
      replaceKids(unavailBody, copy.body ? [copy.body] : []);
      setHidden(unavailBody, !copy.body);
    }
    // The server's hint can repeat the title word for word ("disabled").
    const hint =
      av.reason === 'auth' || String(av.hint ?? '').trim() === copy.title
        ? null
        : av.hint;
    setText(unavailHint, hint || '');
    setHidden(unavailHint, !hint);
    setHidden(checkAgainBtn, av.reason === 'token');
  }

  function emitPending() {
    const pending = pendingApprovals(state);
    const oldest = pending[0] || null;
    const key = `${pending.length}|${oldest?.id ?? ''}`;
    if (key !== lastPendingCount) {
      lastPendingCount = key;
      bus?.emit?.('approval:pending', {
        count: pending.length,
        oldest: oldest
          ? {
              id: oldest.id,
              title: stripBidi(oldest.title),
              vehicle: approvalVehicle(oldest),
              klass: oldest.klass,
            }
          : null,
      });
    }
  }

  function needsTicker() {
    if (pendingApprovals(state).length) return true;
    if (state.running) return true;
    return false;
  }

  function ensureTicker() {
    const want = needsTicker() && !destroyed;
    if (want && tickTimer == null && clock.setInterval) {
      tickTimer = clock.setInterval(() => {
        const t = now();
        for (const slip of slips.values()) slip.tick(t);
        schedule();
      }, 1000);
    } else if (!want && tickTimer != null) {
      clock.clearInterval?.(tickTimer);
      tickTimer = null;
    }
  }

  function render() {
    if (destroyed) return;
    const t = now();
    renderHeader(t);
    renderBanners(t);
    renderTranscript(t);
    renderApprovalBar();
    renderComposer(t);
    renderAvailability();
    emitPending();
    ensureTicker();
    tether.redraw();
  }

  // ---- actions ----
  async function decide(id, decision, note) {
    revealUntilFiled.add(id);
    dispatch({ type: 'decision', id, decision, note, at: now() });
    try {
      await chat.approve(id, decision, note);
      dispatch({ type: 'decision_ok', id });
    } catch (error) {
      revealUntilFiled.delete(id);
      dispatch({
        type: 'decision_failed',
        id,
        error: error?.message || 'The decision did not reach the analyst',
      });
      throw error;
    }
  }

  async function sendText(text, focusedIds = []) {
    if (!chat) return false;
    const body = String(text ?? '').trim();
    if (!body) return false;
    if (state.running) {
      composerNotice = { tone: 'warn', text: COPY.busy };
      schedule();
      return false;
    }
    composerNotice = null;
    revealLatest = true;
    dispatch({ type: 'send', text: body, focused_ids: focusedIds, at: now() });
    history = [...history.filter((x) => x !== body), body].slice(-50);
    try {
      if (!chat.sessionId) await chat.open();
      const res = await chat.send(body, { focused_ids: focusedIds });
      dispatch({ type: 'send_ok', turn_id: res?.turn_id });
      return true;
    } catch (error) {
      if (error?.code === 'busy') {
        dispatch({ type: 'clear_outbox' });
        composerNotice = { tone: 'warn', text: COPY.busy };
        if (!String(textarea.value ?? '').trim()) {
          textarea.value = body;
          contextIds = focusedIds;
        }
      } else if (error?.code === 'unavailable') {
        dispatch({
          type: 'send_failed',
          error: error.message,
          code: error.code,
        });
        applyStatus({
          available: false,
          reason: error.reason,
          hint: error.hint,
        });
      } else {
        dispatch({
          type: 'send_failed',
          error: error?.message || null,
          code: error?.code || null,
        });
      }
      schedule();
      return false;
    }
  }

  function retryLast() {
    const text = state.lastOperatorText;
    if (text) sendText(text, []);
  }

  function submit() {
    if (state.running) return;
    const text = String(textarea.value ?? '');
    if (!text.trim() || text.length > MAX_CHARS) return;
    if (composerBlockReason(now())) return;
    const ids = contextIds.slice();
    textarea.value = '';
    contextIds = [];
    autosize();
    sendText(text, ids);
  }

  function interrupt() {
    if (!state.running || !chat) return;
    // The client emits 'interrupt' (recorded below) for every stop, whoever asks.
    chat.interrupt().catch(() => {
      composerNotice = {
        tone: 'warn',
        text: "Couldn't reach the analyst to stop it. Try again.",
      };
      schedule();
    });
  }

  function insertText(text, ids = null) {
    const current = String(textarea.value ?? '');
    textarea.value = current.trim()
      ? `${current.replace(/\s+$/, '')} ${text}`
      : text;
    if (Array.isArray(ids)) {
      contextIds = [...new Set([...contextIds, ...ids.map(String)])].slice(
        0,
        20,
      );
    }
    autosize();
    textarea.focus?.();
    schedule();
  }

  function autosize() {
    const lines = String(textarea.value ?? '').split('\n').length;
    textarea.setAttribute('rows', String(Math.max(1, Math.min(8, lines))));
  }

  function reviewOldest() {
    const oldest = pendingApprovals(state)[0];
    if (!oldest) return false;
    const slip = slips.get(oldest.id);
    if (!slip) return false;
    slip.review();
    return true;
  }

  async function refreshGrants() {
    if (!chat?.grants) return;
    try {
      const grants = await chat.grants();
      dispatch({ type: 'grants', grants });
    } catch {
      // Keep the locally tracked grants.
    }
  }

  async function newSession() {
    menuOpen = false;
    if (!chat) return;
    try {
      await chat.newSession();
      refreshGrants();
    } catch (error) {
      composerNotice = {
        tone: 'warn',
        text: `Couldn't start a new session: ${error?.message || 'no detail'}.`,
      };
      schedule();
    }
  }

  function applyStatus(st) {
    if (!st) return;
    if (st.transient) {
      scheduleRetry();
      if (!reached) {
        // Never reached: say the analyst isn't available (the shell's spine
        // says the same at wide and compact) and keep checking quietly.
        serviceDown = false;
        dispatch({
          type: 'availability',
          available: false,
          reason: 'unreachable',
          hint: null,
          model: null,
        });
      } else serviceDown = true;
      schedule();
      return;
    }
    reached = true;
    serviceDown = false;
    dispatch({
      type: 'availability',
      available: st.available === true,
      reason: st.reason ?? null,
      hint: st.hint ?? null,
      model: st.model ?? null,
      // A fresh check that says "available" lets the operator try again
      // after signing in; the next turn re-detects a sign-in failure.
      clearAuth: st.available === true,
    });
    schedule();
  }

  function scheduleRetry() {
    if (retryTimer != null || destroyed || !clock.setTimeout) return;
    retryTimer = clock.setTimeout(() => {
      retryTimer = null;
      boot();
    }, STATUS_RETRY_MS);
  }

  async function boot() {
    if (!chat || destroyed) {
      if (!chat) {
        dispatch({
          type: 'availability',
          available: false,
          reason: 'sdk_missing',
        });
      }
      return;
    }
    const st = await chat.status();
    if (destroyed) return;
    if (st?.available && !booted) {
      try {
        await chat.open();
        booted = true;
        refreshGrants();
      } catch {
        serviceDown = true;
        scheduleRetry();
        schedule();
      }
    } else if (st?.transient) scheduleRetry();
  }

  async function checkAgain() {
    if (!chat) return;
    const st = await chat.status();
    if (st?.available) {
      dispatch({
        type: 'availability',
        available: true,
        reason: null,
        hint: null,
        model: st.model ?? null,
        clearAuth: true,
      });
      if (!booted) boot();
    }
    schedule();
  }

  // ---- live side effects ----
  function runDirective(d) {
    if (!d || executed.has(d.seq)) return;
    executed.add(d.seq);
    if (d.action === 'focus') {
      if (d.ids.length)
        bus?.emit?.('focus:entities', {
          ids: d.ids,
          by: 'analyst',
          note: stripBidi(d.note) ?? undefined,
        });
    } else if (d.action === 'inspect') {
      if (d.id) bus?.emit?.('inspect', { id: d.id, by: 'analyst' });
    } else if (d.action === 'track') {
      const source = d.reason === 'mission launched' ? 'launch' : 'analyst';
      const row = d.vehicle ? latestCommandRow(state, d.vehicle) : null;
      const m = modeInfo();
      const verdict = shouldAutoTrack({
        source,
        vehicle: d.vehicle,
        commandState: row?.state ?? null,
        composerText: textarea.value,
        composerFocused: doc?.activeElement === textarea,
        lastStageInputAt: Number(store?.lastStageInputAt) || null,
        now: now(),
        pendingCount: pendingApprovals(state).length,
        mode: m.mode,
        trackingVehicle: m.vehicle,
      });
      trackDecisions.set(d.seq, {
        ...verdict,
        title: row?.title ?? null,
        source,
      });
      // A view-change condition that fails (composer busy, recent stage
      // input, a pending slip, tracking another aircraft) still goes to
      // mode.js, which applies the same §6.9 rules and shows the static
      // "Launched: … / Track Drone1" toast instead of the countdown. A launch
      // that didn't run (busy, rejected, failed) or the aircraft already in
      // view never asks.
      if (verdict.allowed || AUTO_TRACK_DEFERRED.has(verdict.reason)) {
        bus?.emit?.('track:request', {
          vehicle: d.vehicle,
          source,
          reason: stripBidi(d.reason) ?? undefined,
          mission: stripBidi(row?.title) ?? undefined,
        });
      }
    } else if (d.action === 'orb') {
      const m = modeInfo();
      if (m.mode === 'tracking' || m.mode === 'entering_tracking') {
        bus?.emit?.('track:exit', { source: 'analyst' });
      }
    }
    // The directive line depends on what was decided here.
    schedule();
  }

  function onChatEvent(ev) {
    if (!ev) return;
    const { name, data, seq, at, replay } = ev;
    const before = state;
    dispatch({ type: 'event', name, data, seq, at, replay });
    if (state === before) return; // duplicate or ignored
    if (name === 'session' && data?.session_id && state.lastSeq === 0)
      refreshGrants();
    if (replay) return;
    // A session grant was confirmed: read the server's list, so each granted
    // tool gets its Revoke.
    if (name === 'approval_resolved' && data?.scope === 'session')
      refreshGrants();
    if (name === 'approval_request') {
      const a = state.approvals[String(data.approval_id)];
      if (a && !announced.has(a.id)) {
        announced.add(a.id);
        befores.set(a.id, vehicleNow(approvalVehicle(a)));
        announce(`Approval needed: ${a.title}. ${classMeta(a.klass).phrase}.`, {
          assertive: true,
        });
      }
    } else if (name === 'tool_result') {
      const row = state.rows[data.call_id];
      if (row && row.klass === 'plan' && row.state === 'done') {
        const kind = missionKind(row.tool, row.args);
        const vehicle = callVehicle(row.tool, row.args);
        if (vehicle) {
          const facts = vehicleNow(vehicle);
          if (facts)
            snapshots.set(`${kind}|${vehicle}`, { ...facts, at: at ?? now() });
        }
      }
    } else if (name === 'ui') {
      const d = state.directives[state.directives.length - 1];
      if (d && d.seq === seq) runDirective(d);
    } else if (name === 'turn_end') {
      const turn = state.turns[data.turn_id];
      const text = turn?.blocks
        .filter((b) => b.kind === 'text')
        .map((b) => b.text)
        .join(' ');
      const first = latestSentence(
        String(text || '').split(/(?<=[.!?])\s/)[0] || '',
        140,
      );
      if (data.stop === 'end')
        announce(first ? `Analyst replied. ${first}` : 'Analyst replied.');
    } else if (name === 'usage' && state.notice?.kind === 'reconnected') {
      // no-op: keeps the notice timer logic in one place
    }
    if (
      state.notice?.kind === 'reconnected' &&
      noticeTimer == null &&
      clock.setTimeout
    ) {
      noticeTimer = clock.setTimeout(() => {
        noticeTimer = null;
        dispatch({ type: 'notice_seen' });
      }, NOTICE_MS);
    }
  }

  // ---- wiring ----
  textarea.addEventListener('input', () => {
    autosize();
    if (composerNotice?.text === COPY.busy && !state.running)
      composerNotice = null;
    schedule();
  });
  textarea.addEventListener('keydown', (event) => {
    const key = event?.key;
    if (key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault?.();
      submit();
      return;
    }
    if (key === 'ArrowUp' && !String(textarea.value ?? '') && history.length) {
      event.preventDefault?.();
      textarea.value = history[history.length - 1];
      autosize();
      schedule();
      return;
    }
    if (key === 'Escape' && state.running) {
      // Esc stops the analyst; it never leaves tracking from the composer.
      event.preventDefault?.();
      event.stopPropagation?.();
      interrupt();
    }
  });
  sendBtn.addEventListener('click', () => {
    if (state.running) interrupt();
    else submit();
  });
  reviewBtn.addEventListener('click', () => reviewOldest());
  menuBtn.addEventListener('click', () => {
    menuOpen = !menuOpen;
    // Show it now, not on the next frame: Tab from the button must reach
    // "New session" straight away.
    menuBtn.setAttribute('aria-expanded', menuOpen ? 'true' : 'false');
    setHidden(menuEl, !menuOpen);
    schedule();
  });
  newSessionItem.addEventListener('click', () => newSession());
  stopItem.addEventListener('click', () => {
    menuOpen = false;
    interrupt();
    schedule();
  });
  standingNew.addEventListener('click', () => newSession());
  checkAgainBtn.addEventListener('click', () => checkAgain());
  rootEl.addEventListener('keydown', (event) => {
    if ((event?.metaKey || event?.ctrlKey) && event?.key === '.') {
      event.preventDefault?.();
      interrupt();
    } else if (event?.key === 'Escape' && menuOpen) {
      event.preventDefault?.();
      event.stopPropagation?.();
      menuOpen = false;
      menuBtn.focus?.();
      schedule();
    }
  });
  logEl.addEventListener('scroll', () => tether.redraw());
  if (motionMQ) {
    // A live reduced-motion toggle re-renders: the slip's "Ready in 1 s"
    // label and the steady caret follow it without a reload.
    const onMotion = () => schedule();
    try {
      if (typeof motionMQ.addEventListener === 'function') {
        motionMQ.addEventListener('change', onMotion);
        unsubs.push(() => motionMQ.removeEventListener?.('change', onMotion));
      } else if (typeof motionMQ.addListener === 'function') {
        motionMQ.addListener(onMotion);
        unsubs.push(() => motionMQ.removeListener?.(onMotion));
      }
    } catch {
      // No live updates; the value is still read on every render.
    }
  }

  if (chat) {
    unsubs.push(chat.on('event', onChatEvent));
    unsubs.push(
      chat.on('state', ({ connection }) =>
        dispatch({ type: 'connection', state: connection, at: now() }),
      ),
    );
    unsubs.push(
      chat.on('session:replaced', ({ reason }) => {
        dispatch({ type: 'new_session', reason, at: now() });
        for (const slip of slips.values()) slip.update({});
      }),
    );
    unsubs.push(chat.on('status', (st) => applyStatus(st)));
    unsubs.push(
      chat.on('interrupt', ({ at } = {}) =>
        dispatch({ type: 'interrupt', at: Number.isFinite(at) ? at : now() }),
      ),
    );
  }
  if (store?.on) {
    const off = store.on('change', () => schedule());
    if (typeof off === 'function') unsubs.push(off);
  }
  if (bus?.on) {
    unsubs.push(
      bus.on('ask', (payload) => {
        const text = String(payload?.text ?? '').trim();
        const ids = Array.isArray(payload?.focused_ids)
          ? payload.focused_ids
          : [];
        if (!text && !ids.length) return;
        // The inspector's "Ask about this" is a draft: insert, never send.
        if (payload?.draft === true || payload?.send === false || !text)
          insertText(text, ids);
        else self.ask(text, ids);
      }),
    );
    unsubs.push(
      bus.on('mode', (payload) => {
        if (payload?.mode) currentMode = payload.mode;
      }),
    );
  }

  render();
  boot();

  const self = {
    get state() {
      return state;
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      for (const off of unsubs.splice(0)) {
        try {
          off?.();
        } catch {
          // ignore
        }
      }
      for (const slip of slips.values()) slip.destroy();
      slips.clear();
      if (tickTimer != null) clock.clearInterval?.(tickTimer);
      if (retryTimer != null) clock.clearTimeout?.(retryTimer);
      if (noticeTimer != null) clock.clearTimeout?.(noticeTimer);
      tether.destroy();
      if (ownClient) chat?.close?.();
      rootEl.remove?.();
    },
    setLayout(next) {
      layout = next || 'wide';
      rootEl.setAttribute('data-layout', layout);
      if (layout !== 'wide') tether.hide();
    },
    setDocked(on) {
      docked = Boolean(on);
      rootEl.setAttribute('data-docked', docked ? 'true' : 'false');
      if (docked) tether.hide();
    },
    focusComposer() {
      textarea.focus?.();
    },
    async ask(text, focusedIds = []) {
      const ids = Array.isArray(focusedIds) ? focusedIds.slice(0, 10) : [];
      if (state.running) {
        insertText(String(text ?? ''), ids);
        composerNotice = { tone: 'warn', text: COPY.busy };
        schedule();
        return false;
      }
      const ok = await sendText(text, ids);
      logEl.focus?.();
      return ok;
    },
    insert(text, focusedIds = []) {
      insertText(String(text ?? ''), focusedIds);
    },
    stop: interrupt,
    reviewOldest,
    reviewApprovals: reviewOldest,
    composerIdle() {
      return (
        !String(textarea.value ?? '').trim() && doc?.activeElement !== textarea
      );
    },
    newSession,
    _render: render,
  };
  return self;
}
