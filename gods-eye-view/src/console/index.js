/**
 * The intelligence console shell (UX spec §3, §7.3, §8, §9).
 *
 * `mountIntelConsole({root, config, trackingPort})` builds `.ic-root` — an
 * opaque full-viewport layer at z-index 1100, above GEV's loading screen —
 * with three regions (situation rail, stage, analyst column), wires the shared
 * ctx `{api, store, orb, chat, bus, mode, root, config}` and mounts the
 * panels built by the other owners into their hosts.
 *
 * The shell owns: `data-layout` (ResizeObserver: wide ≥ 1280, compact
 * 720–1279, narrow < 720 with tabs), skip links, the global capture-phase
 * keyboard (§9) and the Esc layer stack, the analyst column's collapse when
 * the analyst is unavailable (§3e), the stage's service line, caption and
 * honesty footer, the critical banner (§7.3, persists into tracking until
 * viewed), the notices from mode.js, and the tracking dock header (mini orb
 * that doubles as Back to console, vehicle line, fuel vs BINGO, Abort).
 *
 * DOM is built with ../ui/uavDom.js helpers only, so the shell mounts under
 * GEV's stub document in node tests; every browser API (ResizeObserver,
 * matchMedia, requestAnimationFrame, fonts) is feature-checked.
 */
import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { createBus } from './bus.js';
import { createConsoleKey, resolveBridge } from './config.js';
import {
  CONSOLE_CLAIM_PATH,
  WARGAME_END_PATH,
  createApi,
  formatZulu,
} from './api.js';
import { createIntelStore } from './intelStore.js';
import { MAP_COPY, createModeController, theaterGroundFor } from './mode.js';
import { READ_COPY, createReadView, readDocOf } from './readView.js';
import {
  STRIP_COPY,
  STRIP_H,
  activeWargame,
  createWargameStrip,
  stripModel,
} from './wargameStrip.js';
import { createMapDock, mapDockModel } from './mapDock.js';
import { createOrb, createOrbListView } from './orb/orb.js';
import { createChatClient } from './chat/client.js';
import { createAnalyst, resolveMapArea } from './chat/view.js';
import { createSearch } from './search.js';
import { createInspector } from './inspector.js';
import { createSettingsSheet } from './settings/view.js';
import {
  USE_API_KEY,
  providerHint,
  providerUnavailableCopy,
} from './settings/model.js';
import {
  alarmLabel,
  confirmAbort,
  createSituation,
  formatFuel,
  fuelBar,
  fuelSentence,
  fuelState,
  focusBack,
  fuelTone,
  linkLines,
  missionKindTitle,
  rememberFocus,
} from './situation.js';

// ---------------------------------------------------------------------------
// Constants and copy
// ---------------------------------------------------------------------------

export const LAYOUT_WIDE_MIN = 1280;
export const LAYOUT_COMPACT_MIN = 720;
export const SEARCH_BAND_PX = 56;
export const CAPTION_PX = 40;
export const FOOTER_PX = 24;
export const ORB_MIN_PX = 360;
/** Room above the orb for the pole's band caption when height limits it. */
export const ORB_TOP_PX = 16;
export const ORB_MAX_PX = 880;
export const RECENT_INPUT_MS = 3000;
const BACK_ONLINE_MS = 5000;
const LOADING_AFTER_MS = 800;
const ICON_FONT_CHECK_MS = 3000;
const TELEMETRY_FROZEN_MS = 5000;
/** Retry delays for a console-key claim that got no answer (WG §3.5). */
export const CLAIM_RETRY_MS = Object.freeze([2000, 5000, 10000, 30000]);

/** Material Symbols glyph names reach the DOM only through this map (§11.2). */
export const ICON = Object.freeze({
  abort: 'pan_tool',
  analyst: 'forum',
  back: 'arrow_back',
  close: 'close',
  critical: 'error',
  dockClose: 'right_panel_close',
  dockOpen: 'right_panel_open',
  info: 'info',
  keyboard: 'keyboard',
  linkOff: 'link_off',
  list: 'view_list',
  map: 'map',
  offline: 'cloud_off',
  orb: 'bubble_chart',
  retry: 'refresh',
  settings: 'settings',
  signIn: 'login',
  track: 'my_location',
  warning: 'warning',
});

/** Shell copy, sentence case, from the UX spec copy deck (§10). */
export const COPY = Object.freeze({
  skipSearch: 'Skip to search',
  skipAnalyst: 'Skip to analyst',
  rail: 'Situation',
  stage: 'Picture',
  analyst: 'Analyst',
  orb: 'Orb',
  list: 'List',
  map: 'Map',
  showOnMap: MAP_COPY.showOnMap,
  mapUnsupported: MAP_COPY.unsupported,
  views: 'Picture view',
  tabs: 'Console sections',
  connecting: 'Connecting to Eye in the Sky…',
  loading: 'Loading the intel picture…',
  offline:
    "Can't reach Eye in the Sky's local service. Check that the app is still running. Retrying every 5 s.",
  lost: (z) =>
    `Lost contact with the local service at ${z}. Everything shown is from then and isn't live. Retrying every 5 s.`,
  backOnline: (z) => `Back online. Picture updated at ${z}.`,
  unauthorized:
    "The console's access token was rejected. Quit and reopen the app to get a new one.",
  didntLoad: (e) => `The intel picture didn't load: ${e}. Retrying in 5 s.`,
  retryNow: 'Retry now',
  nothingObserved: (t) => `Nothing observed yet in ${t}.`,
  noContactsYet: (v) => `No contacts yet. ${v} hasn't scanned.`,
  noContactsHere: (n) =>
    `No contacts in this theater. ${n} ${n === 1 ? 'is' : 'are'} in other theaters.`,
  showAllTheaters: 'Show all theaters',
  analystNote: (note) => `Analyst: ${note}`,
  analystSuggests: (label) => `The analyst suggests ${label}.`,
  inspect: 'Inspect',
  newCount: (n) => `${n} new`,
  analystPointed: (n) =>
    `The analyst pointed at ${n} ${n === 1 ? 'entity' : 'entities'}.`,
  show: 'Show',
  clear: 'Clear',
  view: 'View',
  review: 'Review',
  criticalMany: (n) => `${n} critical alarms`,
  bingoTail: "The return can't be cancelled.",
  frozen: 'The camera view is frozen and is not live.',
  back: 'Back to console',
  dock: 'Tracking',
  esc: 'Esc',
  tracking: (v) => `Tracking ${v}`,
  abort: (v) => `Abort ${v}`,
  collapseDock: 'Collapse the dock',
  expandDock: 'Show the analyst and tracking dock',
  notAvailable: 'Not available',
  checkAgain: 'Check again',
  approvalOne: 'Approval waiting',
  approvalOneNamed: (title) => `Approval waiting: ${title}.`,
  approvalMany: (n) => `${n} approvals waiting.`,
  approvalsWaiting: (n) =>
    n === 1 ? '1 approval waiting' : `${n} approvals waiting`,
  pointedOut: (n) => `${n} pointed out by the analyst`,
  shortcuts: 'Keyboard shortcuts',
  analystSettings: 'Analyst settings',
  close: 'Close',
  panelFailed: (name) =>
    `The ${name} didn't load. The rest of the console still works.`,
  mapFailed: (message) =>
    message
      ? `The map didn't start: ${sentence(message)} Tracking isn't available; the orb, search and the analyst still work.`
      : "The map didn't start. Tracking isn't available; the orb, search and the analyst still work.",
  criticalCount: (n) => `${n} critical`,
  readFailed: (e) =>
    `Couldn't open the report: ${String(e ?? '')
      .trim()
      .replace(/[.!?…]+$/, '')}.`,
});

/** Analyst availability copy (§10), keyed by `/chat/status.reason`. */
export function analystUnavailableCopy(status) {
  const reason = status?.reason ?? null;
  const hint =
    typeof status?.hint === 'string' && status.hint ? status.hint : '';
  // Provider settings (BYOK spec §10): the popover opens analyst settings.
  const provider = providerUnavailableCopy(reason, status?.provider);
  if (provider) return { ...provider, hint: providerHint(hint) };
  switch (reason) {
    case 'sdk_missing':
      return {
        title: "The analyst isn't installed in this build.",
        body: 'Search, the orb and the situation rail still work.',
        hint,
      };
    case 'cli_missing':
      return {
        title:
          "The analyst needs the Claude command-line tool, and it wasn't found.",
        body: '',
        hint,
      };
    case 'disabled':
      return {
        title: 'The analyst is turned off for this launch.',
        body: '',
        hint,
      };
    case 'service_unavailable':
      return {
        title: "The analyst didn't start in this app.",
        body: 'Search, the orb and the situation rail still work.',
        hint,
      };
    case 'auth':
    case 'not_signed_in':
    case 'auth_error':
      return {
        title: "The analyst isn't signed in.",
        body: 'Sign in with the claude CLI (`claude`, then /login) or set ANTHROPIC_API_KEY.',
        hint,
        secondary: USE_API_KEY,
        secondaryProvider: 'anthropic_api',
      };
    default:
      return {
        title: "The analyst isn't available right now.",
        body: 'Search, the orb and the situation rail still work.',
        hint,
      };
  }
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

/** wide ≥ 1280, compact 720–1279, narrow < 720. */
export function layoutFor(width) {
  const w = Number(width);
  if (!Number.isFinite(w) || w <= 0) return 'wide';
  if (w >= LAYOUT_WIDE_MIN) return 'wide';
  if (w >= LAYOUT_COMPACT_MIN) return 'compact';
  return 'narrow';
}

/**
 * The orb's projection centre and radius inside the stage canvas (§3):
 * diameter = min(stageW − 2·gutter, canvasH − caption − plate − footer −
 * a 16 px top margin that keeps the pole's band caption off the stage edge),
 * at least 360 px at wide and compact, at most 880 px. The plate only
 * shrinks the orb at wide; at compact and narrow the inspector covers the
 * analyst column or the screen, never the orb.
 * @param {{width:number, height:number, layout:string, plateOpen:boolean}} p
 *   canvas size in CSS px (the stage below the search band)
 */
export function orbViewport({
  width,
  height,
  layout = 'wide',
  plateOpen = false,
}) {
  const w = Math.max(0, Number(width) || 0);
  const hgt = Math.max(0, Number(height) || 0);
  const gutter = layout === 'wide' ? 96 : 24;
  const stageH = hgt + SEARCH_BAND_PX;
  const plate =
    plateOpen && layout === 'wide' ? Math.min(300, 0.36 * stageH) : 0;
  const avail = Math.max(0, hgt - CAPTION_PX - plate - FOOTER_PX - ORB_TOP_PX);
  let d = Math.min(w - 2 * gutter, avail);
  if (layout !== 'narrow') d = Math.max(d, Math.min(ORB_MIN_PX, w));
  d = Math.max(0, Math.min(d, ORB_MAX_PX));
  const r = d / 2;
  return {
    cx: Math.round(w / 2),
    cy: Math.round(ORB_TOP_PX + Math.max(avail / 2, r)),
    r: Math.round(r),
    diameter: Math.round(d),
    plate: Math.round(plate),
  };
}

function num(value) {
  const n = typeof value === 'string' && !value.trim() ? NaN : Number(value);
  return value != null && Number.isFinite(n) ? n : null;
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function nodesOf(graph, type) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  return nodes.filter((n) => n?.type === type);
}

/**
 * The stage footer's honesty line, built from `meta` (§4.6), e.g.
 * "23 contacts in theater. 20 duplicates merged. 27 outside this theater.
 * Threat assessed for 10 of 12." During a simulated wargame session it
 * starts "Simulated wargame." (WG §5.3.2); otherwise it is unchanged.
 */
export function honestyLine(graph) {
  const meta = graph?.meta;
  if (!meta || typeof meta !== 'object') return '';
  const all = graph.scope === 'all';
  const counts = meta.counts || {};
  const contacts = num(counts.track) ?? nodesOf(graph, 'track').length;
  const parts = [
    `${plural(contacts, 'contact', 'contacts')} ${all ? 'across all theaters' : 'in theater'}.`,
  ];
  if (activeWargame(graph)) parts.unshift(STRIP_COPY.honesty);
  const dup = num(meta.duplicates_collapsed);
  if (dup) parts.push(`${plural(dup, 'duplicate', 'duplicates')} merged.`);
  const out = num(meta.out_of_theater_contacts ?? meta.out_of_theater);
  if (!all && out) parts.push(`${out} outside this theater.`);
  const assessed = num(meta.threat_assessed) ?? 0;
  const unassessed = num(meta.threat_unassessed) ?? 0;
  if (assessed + unassessed > 0) {
    parts.push(`Threat assessed for ${assessed} of ${assessed + unassessed}.`);
  }
  return parts.join(' ');
}

/**
 * The line under an empty or near-empty orb (§3d). Built from state, never a
 * greeting. Returns null when the picture has contacts.
 * @returns {{text:string, action?:'all-theaters'}|null}
 */
export function invitation(graph) {
  if (!graph) return null;
  const contacts =
    num(graph.meta?.counts?.track) ?? nodesOf(graph, 'track').length;
  if (contacts > 0) return null;
  const out = num(
    graph.meta?.out_of_theater_contacts ?? graph.meta?.out_of_theater,
  );
  if (graph.scope !== 'all' && out) {
    return { text: COPY.noContactsHere(out), action: 'all-theaters' };
  }
  const vehicles = nodesOf(graph, 'vehicle');
  if (vehicles.length) return { text: COPY.noContactsYet(vehicles[0].label) };
  const label = graph.theater?.label;
  return label ? { text: COPY.nothingObserved(label) } : null;
}

function sentence(text) {
  const t = String(text ?? '').trim();
  return !t || /[.!?…]$/.test(t) ? t : `${t}.`;
}

/** Whether the tracked vehicle's camera view is not live (§7.3). */
export function cameraFrozen(vehicle) {
  if (!vehicle) return false;
  const link = String(vehicle.link ?? '').toLowerCase();
  const stale = num(vehicle.stale_ms) ?? 0;
  return link === 'loal' || link === 'lost' || stale > TELEMETRY_FROZEN_MS;
}

/**
 * The critical banner (§7.3): "{Kind label}: {message verbatim}", plus the
 * BINGO tail; several alarms read "3 critical alarms". In tracking, a frozen
 * camera view is stated even with no alarm.
 * @returns {{text:string, count:number, first:object|null, frozen:boolean}}
 */
export function bannerModel({
  alarms = [],
  tracking = false,
  vehicle = null,
} = {}) {
  const list = Array.isArray(alarms) ? alarms.filter(Boolean) : [];
  let text = '';
  if (list.length === 1) {
    const a = list[0];
    const message = String(a.message ?? '').trim();
    const label = alarmLabel(a.kind);
    // The server's message often already opens with the kind ("BINGO fuel -
    // forcing RTB"): say the label once.
    if (!message) text = label;
    else if (message.toLowerCase().startsWith(label.toLowerCase()))
      text = message;
    else text = `${label}: ${message}`;
    if (a.kind === 'bingo') text = `${sentence(text)} ${COPY.bingoTail}`;
  } else if (list.length > 1) {
    text = COPY.criticalMany(list.length);
  }
  const frozen = Boolean(tracking && cameraFrozen(vehicle));
  if (frozen) text = text ? `${sentence(text)} ${COPY.frozen}` : COPY.frozen;
  return { text, count: list.length, first: list[0] || null, frozen };
}

/**
 * The stage's service line (§10 boot and service copy), or null when live.
 * @returns {{text:string, tone:'info'|'ok'|'warn'|'critical', retry:boolean}|null}
 */
export function serviceLine(
  state,
  { recoveredAt = null, connecting = false } = {},
) {
  switch (state?.status) {
    case 'unauthorized':
      return { text: COPY.unauthorized, tone: 'critical', retry: false };
    case 'offline':
      if (state.error?.kind === 'http') {
        return {
          text: COPY.didntLoad(state.error.message),
          tone: 'warn',
          retry: true,
        };
      }
      return { text: COPY.offline, tone: 'critical', retry: true };
    case 'stale':
      return {
        text: COPY.lost(
          formatZulu(state.lastLiveAt, { seconds: true }) || 'an unknown time',
        ),
        tone: 'warn',
        retry: true,
      };
    case 'loading':
      return {
        text: connecting ? COPY.connecting : COPY.loading,
        tone: 'info',
        retry: false,
      };
    case 'live':
      return recoveredAt != null
        ? {
            text: COPY.backOnline(formatZulu(recoveredAt, { seconds: true })),
            tone: 'ok',
            retry: false,
          }
        : null;
    default:
      return null;
  }
}

/** Port methods the map overview needs (WG spec §4.2.7). */
const MAP_PORT_METHODS = Object.freeze([
  'showArea',
  'enterOverview',
  'exitOverview',
  'setOverlayVisibility',
  'onPick',
]);

/**
 * Wrap GEV's tracking port, which may arrive late: an object, a Promise of
 * one, or a function returning either. Calls made before it arrives are
 * safe: `enter` and `showArea` wait for it, the rest are no-ops. A port
 * without the map methods (an older GEV) degrades: `supports()` says no,
 * `showArea` resolves false, the overview and overlay calls do nothing and
 * `onPick` never fires.
 */
export function createPortProxy(source) {
  let real = null;
  let offReal = null;
  let offPick = null;
  // The wargame view asked for before the real port arrived (WG §5.3.3).
  let overlayTruth = null;
  const changeCbs = new Set();
  const pickCbs = new Set();
  function fanOut(cbs, args) {
    for (const cb of [...cbs]) {
      try {
        cb(...args);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
  }
  function adopt(p) {
    if (!p || typeof p !== 'object' || real) return p ?? null;
    real = p;
    if (typeof p.onChange === 'function') {
      const off = p.onChange((...args) => fanOut(changeCbs, args));
      offReal = typeof off === 'function' ? off : null;
    }
    if (typeof p.onPick === 'function') {
      const off = p.onPick((...args) => fanOut(pickCbs, args));
      offPick = typeof off === 'function' ? off : null;
    }
    if (overlayTruth !== null && typeof p.setOverlayTruth === 'function') {
      try {
        p.setOverlayTruth(overlayTruth);
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
    return p;
  }
  /** Call an optional method of the real port; a throw is reported, not raised. */
  function optional(name, ...args) {
    const fn = real?.[name];
    if (typeof fn !== 'function') return undefined;
    try {
      return fn.apply(real, args);
    } catch (err) {
      globalThis.console?.error?.(err);
      return undefined;
    }
  }
  let ready;
  if (typeof source === 'function') {
    ready = Promise.resolve()
      .then(() => source())
      .then(adopt, () => null);
  } else if (source && typeof source.then === 'function') {
    ready = Promise.resolve(source).then(adopt, () => null);
  } else {
    adopt(source);
    ready = Promise.resolve(real);
  }
  return {
    get available() {
      return Boolean(real);
    },
    async whenReady() {
      const p = await ready;
      if (!p) throw new Error('The map is not available in this window.');
      await p.whenReady?.();
    },
    async enter(vehicle) {
      const p = await ready;
      return p ? Boolean(await p.enter?.(vehicle)) : false;
    },
    exit: () => real?.exit?.(),
    isTracking: () => Boolean(real?.isTracking?.()),
    onChange(cb) {
      if (typeof cb !== 'function') return () => {};
      changeCbs.add(cb);
      return () => changeCbs.delete(cb);
    },
    setMapVisible: (visible) => real?.setMapVisible?.(Boolean(visible)),
    openMissionPanel: () => real?.openMissionPanel?.(),
    setViewportInset: (inset) => real?.setViewportInset?.(inset),
    supportsInset: () => typeof real?.setViewportInset === 'function',
    keyhole: () => real?.keyhole?.() ?? null,
    /** Whether the port offers `name` (its own answer when it has one). */
    supports(name) {
      if (!real) return false;
      if (typeof real.supports === 'function') {
        try {
          return Boolean(real.supports(name));
        } catch {
          return false;
        }
      }
      return (
        MAP_PORT_METHODS.includes(name) && typeof real[name] === 'function'
      );
    },
    /** Resolves false when there is no port, no showArea, or it failed. */
    async showArea(target, opts = {}) {
      const p = await ready;
      if (!p || typeof p.showArea !== 'function') return false;
      try {
        return (await p.showArea(target, opts)) !== false;
      } catch (err) {
        globalThis.console?.error?.(err);
        return false;
      }
    },
    enterOverview: () => optional('enterOverview'),
    exitOverview: () => optional('exitOverview'),
    setOverlayVisibility: (v) => optional('setOverlayVisibility', v),
    /**
     * The wargame view for the map's context overlay: true is Umpire view
     * (red truth), anything else Blue view. Remembered until the port
     * arrives; a port without it stays in Blue view, the safe side.
     */
    setOverlayTruth(on) {
      overlayTruth = on === true;
      return optional('setOverlayTruth', overlayTruth) ?? null;
    },
    /**
     * The overlay's own counts, when the port reports them. GEV's port names
     * it `overlayStatus()` (A18); `overlayStats()` is the older spelling.
     */
    overlayStats: () =>
      optional('overlayStatus') ?? optional('overlayStats') ?? null,
    onPick(cb) {
      if (typeof cb !== 'function') return () => {};
      pickCbs.add(cb);
      return () => pickCbs.delete(cb);
    },
    destroy() {
      offReal?.();
      offPick?.();
      changeCbs.clear();
      pickCbs.clear();
    },
  };
}

// ---------------------------------------------------------------------------
// Small DOM kit (stub-DOM safe)
// ---------------------------------------------------------------------------

function setText(el, value) {
  if (el) el.textContent = String(value ?? '');
}

function setInert(el, on) {
  if (!el) return;
  if (on) el.setAttribute('inert', '');
  else el.removeAttribute?.('inert');
}

function iconEl(name) {
  return h(
    'span',
    { class: 'ic-icon material-symbols-outlined', 'aria-hidden': 'true' },
    name,
  );
}

function btn(
  label,
  { icon, variant = 'quiet', cls = '', title, aria, onClick } = {},
) {
  const el = h(
    'button',
    {
      type: 'button',
      class: `ic-btn ${cls}`.trim(),
      'data-variant': variant,
      title,
      'aria-label': aria,
    },
    icon ? iconEl(icon) : null,
    label ? h('span', { class: 'ic-btn__label' }, label) : null,
  );
  if (onClick) el.addEventListener('click', onClick);
  return el;
}

/** `a.contains(b)` that also walks a stub tree. */
function contains(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  if (typeof a.contains === 'function') {
    try {
      return a.contains(b);
    } catch {
      /* fall through to the walk */
    }
  }
  for (const kid of a.children || []) if (contains(kid, b)) return true;
  return false;
}

/**
 * Whether focus is somewhere the operator can't use: nowhere, the page
 * itself, a node gone from the page, or inside a hidden or inert subtree
 * (a real one, or a stub tree's `parent` chain).
 */
function focusStranded(active, doc) {
  if (!active || active === doc?.body || active === doc?.documentElement)
    return true;
  if (active.isConnected === false) return true;
  for (let n = active; n && n !== doc?.body;) {
    if (n.getAttribute?.('hidden') != null || n.getAttribute?.('inert') != null)
      return true;
    n = n.parentElement ?? n.parent ?? null;
  }
  try {
    if (typeof active.getClientRects === 'function')
      return active.getClientRects().length === 0;
  } catch {
    /* no layout to ask */
  }
  return false;
}

/** The first element under `root` with class `cls`, stub tree or not. */
function firstByClass(root, cls) {
  if (!root || typeof root !== 'object') return null;
  if (typeof root.querySelector === 'function') {
    try {
      return root.querySelector(`.${cls}`);
    } catch {
      return null;
    }
  }
  for (const kid of root.children || []) {
    if (!kid || typeof kid !== 'object') continue;
    if (
      String(kid.className || '')
        .split(/\s+/)
        .includes(cls)
    )
      return kid;
    const hit = firstByClass(kid, cls);
    if (hit) return hit;
  }
  return null;
}

const NON_TEXT_INPUTS = new Set([
  'button',
  'checkbox',
  'radio',
  'submit',
  'reset',
  'range',
  'color',
  'file',
  'image',
]);

function isTextField(el) {
  const tag = String(el?.tagName || el?.tag || '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = String(
      el.type || el.getAttribute?.('type') || 'text',
    ).toLowerCase();
    return !NON_TEXT_INPUTS.has(type);
  }
  return Boolean(el?.isContentEditable);
}

function isMacLike() {
  const p = String(
    globalThis.navigator?.userAgentData?.platform ||
      globalThis.navigator?.platform ||
      '',
  );
  return /mac|iphone|ipad/i.test(p);
}

function safeStorage(name) {
  try {
    return globalThis[name] || null;
  } catch {
    return null;
  }
}

function nullOrb() {
  const noop = () => {};
  return {
    setGraph: noop,
    highlight: noop,
    filter: noop,
    focus: noop,
    select: noop,
    resize: noop,
    destroy: noop,
    project: () => null,
    onFrame: () => noop,
    setViewport: noop,
    snapshot: () => null,
    setOptions: noop,
  };
}

/**
 * Shield GEV's document-level capture listeners (cockpit Esc/`c`, voice
 * push-to-talk) from a key the console owns. They run before any console
 * element sees the key and cannot be skipped selectively, but each already
 * ignores composing and default-prevented events. The console presents the
 * event that way to that one phase only: the capture listener on <html>, which
 * runs next, restores the real values before the key reaches its target.
 */
export function shieldFromDocumentCapture(event, doc = globalThis.document) {
  const html = doc?.documentElement;
  if (!event || !html?.addEventListener) return false;
  const keys = ['isComposing', 'defaultPrevented'];
  // A real KeyboardEvent reads these from its prototype; keep any own value.
  const own = keys.map(
    (k) => Object.getOwnPropertyDescriptor(event, k) || null,
  );
  try {
    for (const k of keys) {
      Object.defineProperty(event, k, {
        value: true,
        configurable: true,
        writable: true,
      });
    }
  } catch {
    return false;
  }
  const restore = (ev) => {
    if (ev !== event) return;
    keys.forEach((k, i) => {
      delete event[k];
      if (own[i]) Object.defineProperty(event, k, own[i]);
    });
  };
  html.addEventListener(event.type || 'keydown', restore, {
    capture: true,
    once: true,
  });
  return true;
}

// ---------------------------------------------------------------------------
// Mount
// ---------------------------------------------------------------------------

const DEFAULT_COMPONENTS = Object.freeze({
  createOrb,
  createOrbListView,
  createChatClient,
  createAnalyst,
  createSearch,
  createInspector,
  createSituation,
  createSettingsSheet,
  confirmAbort,
});

/**
 * Mount the console.
 * @param {object} options
 * @param {object} [options.root] parent element (defaults to document.body)
 * @param {object} [options.config] `{base?, token?, api?, globalConfig?, ...}`
 * @param {object|Promise|Function} [options.trackingPort] GEV's port (§7)
 * @param {object} [options.components] factory overrides (tests)
 * @param {object} [options.win] window-like (listeners, observers, media)
 * @param {object} [options.clock] {now, setTimeout, clearTimeout}
 * @returns {{setMode(mode:string, opts?:object):void, destroy():void, ctx:object, elements:object}}
 */
export function mountIntelConsole({
  root = null,
  config = {},
  trackingPort = null,
  components = {},
  win = globalThis.window ?? globalThis,
  clock = null,
} = {}) {
  const doc = globalThis.document;
  const C = { ...DEFAULT_COMPONENTS, ...(components || {}) };
  const time = clock || {
    now: () => Date.now(),
    setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
    clearTimeout: (id) => globalThis.clearTimeout(id),
  };
  const mac = isMacLike();
  const offs = [];
  const timers = new Set();
  let destroyed = false;

  function later(fn, ms) {
    const id = time.setTimeout(() => {
      timers.delete(id);
      if (!destroyed) fn();
    }, ms);
    timers.add(id);
    return id;
  }

  function cancel(id) {
    if (id == null) return;
    time.clearTimeout(id);
    timers.delete(id);
  }

  // ---- shell DOM -------------------------------------------------------------

  const dockHooks = {};
  const stripHooks = {};
  const el = buildShell(mac, dockHooks, stripHooks);
  (root || doc?.body)?.append?.(el.root);
  const strip = el.wargameStrip;

  // ---- ctx -------------------------------------------------------------------

  const bridge = {
    ...resolveBridge({
      globalConfig: config?.globalConfig ?? globalThis.__GODSEYE__,
      storage: safeStorage('localStorage'),
    }),
  };
  if (typeof config?.base === 'string') bridge.base = config.base;
  if (typeof config?.token === 'string' && config.token)
    bridge.token = config.token;
  const api =
    config?.api || createApi({ base: bridge.base, token: bridge.token });
  const bus = config?.bus || createBus();
  const store =
    config?.store ||
    createIntelStore({ api, bus, clock: config?.storeClock || undefined });
  const port = createPortProxy(trackingPort);
  // The engagement approval key (WG §3.5): claimed once per launch, kept in
  // sessionStorage, sent only as the approval header. Panels get the holder
  // (ctx.consoleKey), never the key as a value.
  const consoleKey =
    config?.consoleKey ||
    createConsoleKey({
      storage: safeStorage('sessionStorage'),
      claim: () =>
        typeof api.claimConsole === 'function'
          ? api.claimConsole()
          : api.post(CONSOLE_CLAIM_PATH, {}),
    });

  let chat = null;
  try {
    chat = C.createChatClient({
      api,
      storage: safeStorage('sessionStorage'),
      // The client reads the key through the holder (never storage), so a
      // window whose storage fails still approves, and a refused claim says
      // "another client claimed them" (WG §3.5).
      consoleKey: () =>
        typeof consoleKey?.key === 'function' ? consoleKey.key() : null,
      consoleRefused: () => consoleKey?.refused === true,
      // A key the host no longer accepts (a tab kept across a restart):
      // drop it and claim again.
      onConsoleRejected: () => {
        Promise.resolve()
          .then(() => consoleKey?.invalidate?.())
          .catch((err) => globalThis.console?.error?.(err));
      },
    });
  } catch (err) {
    globalThis.console?.error?.(err);
    chat = null;
  }

  let orb = null;
  try {
    orb = C.createOrb(el.canvas, {
      onSelect: (id) => onOrbSelect(id),
      onHover: () => {},
      a11yHost: el.orbA11y,
      onInput: () => store.noteStageInput?.(),
      onAction: (action) => onOrbAction(action),
      onNotice: (notice) => onOrbNotice(notice),
    });
  } catch (err) {
    globalThis.console?.error?.(err);
  }
  if (!orb) orb = nullOrb();

  let layout = 'wide';
  let tab = 'orb';
  let analystOn = true;
  let analystStatus = null;
  let dockCollapsed = false;
  let pending = { count: 0, oldest: null };
  let focusState = null;
  let filterIds = null;
  let selectedId = null;
  let plateOpen = false;
  let view = 'orb';
  let listView = null;
  // The wargame view the store last announced ('umpire' | 'blue' | null).
  let wargameView = null;
  let turnRunning = false;
  let sheetOpen = false;
  let sheetOpener = null;
  let spineOpen = false;
  let settingsOpen = false;
  let recoveredAt = null;
  let recoveredTimer = null;
  let connecting = true;
  let orbTabBadge = 0;
  let lastAnnounce = { polite: '', assertive: '' };
  // Whether /chat/status ever answered. Until it does, an unreachable or
  // token-refused analyst collapses to the spine (§3e); after that a service
  // blip is the service line's to report and the transcript stays readable.
  let analystReached = false;
  let modeNotices = [];
  let mapNotice = null;
  // The orb's theater-change caption (WG spec §4.2.4), held for its holdMs;
  // a toast while the console is not on the orb.
  let theaterNotice = null;
  let theaterTimer = null;
  let plateInSheet = false;
  // Simulated wargame (WG §5.3.2): whether the strip shows, and the session
  // the operator just ended (the strip goes before the next poll says so).
  let wargameOn = false;
  let endedSession = null;
  // The read view (WG §5.3.11): {returnView, inspecting, opener} while open.
  let reading = null;
  let claimTimer = null;
  let claimTries = 0;

  function announce(text, politeness = 'polite') {
    const region =
      politeness === 'assertive' ? el.liveAssertive : el.livePolite;
    const key = politeness === 'assertive' ? 'assertive' : 'polite';
    let message = String(text ?? '').trim();
    if (!message) return;
    // Re-announcing the same words needs a change a screen reader notices.
    if (message === lastAnnounce[key]) message = `${message} `;
    lastAnnounce[key] = message;
    setText(region, message);
  }

  function isComposerIdle() {
    if (typeof analyst?.composerIdle === 'function') {
      try {
        return analyst.composerIdle() !== false;
      } catch {
        /* fall back to the DOM */
      }
    }
    const active = doc?.activeElement;
    if (active && contains(el.analystBody, active) && isTextField(active))
      return false;
    const field = el.analystBody.querySelector?.('textarea');
    return !(field && String(field.value || '').trim());
  }

  function reducedMotion() {
    try {
      return Boolean(
        win?.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches,
      );
    } catch {
      return false;
    }
  }

  function dockWidth() {
    // In orb mode an unavailable analyst is a 56 px spine, but the dock is
    // never a spine: measure only the full column.
    const spine = el.root.getAttribute?.('data-spine') === 'on';
    const w = spine ? 0 : el.analyst.getBoundingClientRect?.().width;
    if (w > 0) return Math.round(w);
    const vw = Number(win?.innerWidth) || 1440;
    if (layout === 'wide')
      return Math.round(Math.min(520, Math.max(420, 0.31 * vw)));
    if (layout === 'compact')
      return Math.round(Math.min(420, Math.max(360, 0.36 * vw)));
    return 0;
  }

  function viewportInset() {
    // During a simulated wargame the strip sits over the map's top edge.
    const top = wargameOn ? { top: STRIP_H } : {};
    if (layout === 'narrow') {
      // The dock is a bottom sheet of at most 50vh: GEV's alarm toasts sit
      // above it, never over the composer or the standing approval.
      const sheet = dockCollapsed
        ? 0
        : Math.round((Number(win?.innerHeight) || 0) * 0.5);
      return { right: 0, bottom: sheet, ...top };
    }
    if (dockCollapsed || !port.supportsInset()) return { right: 0, ...top };
    return { right: dockWidth(), ...top };
  }

  function stageCentre() {
    const r = el.stage.getBoundingClientRect?.();
    if (r && r.width > 0)
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    return {
      x: (Number(win?.innerWidth) || 0) / 2,
      y: (Number(win?.innerHeight) || 0) / 2,
    };
  }

  const mode = createModeController({
    trackingPort: port,
    bus,
    store,
    root: el.root,
    orb,
    main: el.main,
    isComposerIdle,
    reducedMotion,
    announce,
    viewportInset,
    stageCentre,
    clock: time,
    doc,
  });
  dockHooks.onBack = () => mode.backToConsole();
  dockHooks.onTrack = (v) => mode.requestTrack(v, { source: 'operator' });
  dockHooks.onSites = (on) => {
    mode.setOverlays({ sites: on });
    renderMapDock();
  };
  // The wargame's switches (WG v2 §5.3.12): forces, engagements, vectors.
  dockHooks.onWargame = (kinds) => {
    mode.setOverlays(kinds);
    renderMapDock();
  };

  const ctx = {
    api,
    store,
    orb,
    chat,
    bus,
    mode,
    root: el.root,
    config: {
      ...(config || {}),
      base: bridge.base,
      token: bridge.token,
      source: bridge.source,
    },
    announce,
    trackingPort: port,
    /** The engagement approval key holder (config.js createConsoleKey). */
    consoleKey,
    /** Open a report's full Markdown in the read view (WG §5.3.11). */
    openRead: (target) => requestRead(target),
    get layout() {
      return layout;
    },
  };

  // ---- panels ----------------------------------------------------------------

  function mountPanel(name, host, factory) {
    try {
      return factory() || null;
    } catch (err) {
      globalThis.console?.error?.(err);
      replaceKids(host, [
        h(
          'p',
          { class: 'ic-panelerror', role: 'status' },
          COPY.panelFailed(name),
        ),
      ]);
      return null;
    }
  }

  const situation = mountPanel('situation rail', el.railHost, () =>
    C.createSituation(el.railHost, ctx),
  );
  const search = mountPanel('search bar', el.searchHost, () =>
    C.createSearch(el.searchHost, ctx),
  );
  const inspector = mountPanel('inspector', el.plate, () =>
    C.createInspector(el.plate, ctx),
  );
  const analyst = mountPanel('analyst', el.analystBody, () =>
    C.createAnalyst(el.analystBody, ctx),
  );
  // Analyst settings (BYOK spec §10): a sheet over the analyst column, a
  // direct child of the root so it also takes input in tracking.
  let settingsSheet = null;
  try {
    settingsSheet = C.createSettingsSheet?.(el.root, ctx) || null;
  } catch (err) {
    globalThis.console?.error?.(err);
    settingsSheet = null;
  }

  // The read view (WG §5.3.11): a report's full Markdown in the stage.
  let readView = null;
  try {
    readView = createReadView(el.readHost, { onBack: () => closeRead() });
  } catch (err) {
    globalThis.console?.error?.(err);
    readView = null;
  }

  /** Open analyst settings, optionally at one provider (⌘, / Ctrl+,, the
   *  analyst menu, the unavailable panel, the spine popover). */
  function openSettings({ provider = null, invoker = null } = {}) {
    if (!settingsSheet) return;
    if (sheetOpen) closeSheet();
    const from =
      invoker || (spineOpen ? el.spine : null) || doc?.activeElement || null;
    if (spineOpen) closeSpine({ restore: false });
    settingsSheet.setLayout?.(layout);
    settingsSheet.open({ provider, invoker: from });
  }

  // ---- orb wiring --------------------------------------------------------------

  function onOrbSelect(id) {
    selectedId = id || null;
    if (selectedId) {
      if (focusState?.by === 'analyst') clearFocus({ silent: true });
      inspector?.show?.(selectedId);
      setPlate(true);
    }
  }

  function setPlate(open) {
    if (plateOpen === Boolean(open)) return;
    plateOpen = Boolean(open);
    applyViewport();
  }

  function stageCanvasSize() {
    const r = el.orbWrap.getBoundingClientRect?.();
    if (r && r.width > 0) return { width: r.width, height: r.height };
    const vw = Number(win?.innerWidth) || 1440;
    const vh = Number(win?.innerHeight) || 900;
    const rail =
      layout === 'wide'
        ? Math.min(320, Math.max(248, 0.17 * vw))
        : layout === 'compact'
          ? 64
          : 0;
    const col = layout === 'narrow' ? 0 : dockWidth();
    return {
      width: Math.max(0, vw - rail - col),
      height: Math.max(0, vh - SEARCH_BAND_PX),
    };
  }

  function applyViewport() {
    const size = stageCanvasSize();
    const vp = orbViewport({ ...size, layout, plateOpen });
    // The caption sits above the plate; the honesty footer stays below it.
    el.stage.style?.setProperty?.('--ic-plate-h', `${vp.plate}px`);
    syncPlateBottom();
    try {
      orb.setViewport?.({ cx: vp.cx, cy: vp.cy, r: vp.r });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
  }

  function clearFocus({ silent = false } = {}) {
    if (!focusState) return;
    focusState = null;
    if (!silent) orb.focus?.([], { by: 'analyst', camera: false });
    renderCaption();
  }

  function clearFilter() {
    if (!filterIds) return false;
    filterIds = null;
    orb.filter?.(null);
    listView?.filter?.(null);
    bus.emit('search:filter', { ids: null, query: '', source: 'shell' });
    return true;
  }

  // ---- rendering: caption, footer, service line, notices ----------------------

  let progressNotice = null;
  /** The progress caption on screen: its shape, and the ticking seconds. */
  let captionSig = null;
  let captionDetail = null;
  let arrivalNotice = null;
  let arrivalTimer = null;
  let suggestion = null;

  function nodeById(id) {
    const st = store.get?.() || {};
    const nodes = Array.isArray(st.graph?.nodes) ? st.graph.nodes : [];
    return st.byId?.get?.(id) || nodes.find((n) => n?.id === id) || null;
  }

  function onOrbAction(action) {
    const id = action?.id;
    switch (action?.action) {
      case 'ask': {
        const node = id ? nodeById(id) : null;
        if (!node) return;
        bus.emit('ask', {
          text: `Tell me about [[${node.id}|${node.label || node.id}]]`,
          focused_ids: [node.id],
          draft: true,
        });
        return;
      }
      case 'track': {
        const node = id ? nodeById(id) : null;
        const vehicle =
          node?.type === 'vehicle'
            ? node.label || String(node.id).replace(/^veh:/, '')
            : node?.type === 'mission'
              ? node.attrs?.vehicle
              : null;
        if (vehicle) mode.requestTrack(vehicle, { source: 'operator' });
        return;
      }
      case 'list':
        setView('list');
        // The listbox twin is hidden with the orb: focus follows to the table.
        (
          firstByClass(listView?.element, 'ic-orb-list__sort') || el.viewList
        )?.focus?.();
        return;
      case 'escape':
        if (action.cleared === 'focus') {
          focusState = null;
          renderCaption();
        } else if (!action.cleared) escapeLayer();
        return;
      default:
    }
  }

  // ---- map overview (WG spec §4.2.3, §4.2.7) ----------------------------------

  /** The `[s,w,n,e]` area for graph ids, resolved as `ui map` does. */
  function areaFor(ids) {
    const list = Array.isArray(ids) ? ids.filter(Boolean) : [];
    const area = list.length
      ? resolveMapArea(list, (id) => nodeById(id), store.get?.().graph)
      : { bbox: null, label: null };
    return { ids: list, bbox: area.bbox, label: area.label };
  }

  /** The operator's Show on map: at once, no notice. */
  function showOnMap(ids) {
    return mode.requestMap(areaFor(ids), { source: 'operator' });
  }

  /** The Map toggle and `M`: the active theater, else the aircraft. */
  function openTheaterMap() {
    const g = store.get?.().graph;
    const tid = g?.theater?.id;
    const active = nodesOf(g, 'theater').find((n) => n.attrs?.active === true);
    const ids =
      tid != null && tid !== ''
        ? [`thr:${String(tid).replace(/^thr:/, '')}`]
        : active
          ? [active.id]
          : [];
    let target = areaFor(ids);
    if (!target.bbox) {
      const fleet = areaFor(nodesOf(g, 'vehicle').map((n) => n.id));
      if (fleet.bbox) target = fleet;
    }
    return mode.requestMap(target, { source: 'operator' });
  }

  function mapSupported() {
    try {
      return Boolean(port.supports('showArea'));
    } catch {
      return false;
    }
  }

  function clearTheaterNotice() {
    cancel(theaterTimer);
    theaterTimer = null;
    theaterNotice = null;
    renderCaption();
    renderNotices();
  }

  /** Show on map for the theater caption, when the port and the area allow. */
  function theaterMapAction() {
    const id = theaterNotice?.id;
    if (!id || !mapSupported() || !areaFor([id]).bbox) return null;
    return {
      id: 'map',
      label: COPY.showOnMap,
      run: () => {
        clearTheaterNotice();
        showOnMap([id]);
      },
    };
  }

  /**
   * The orb's theater change (WG spec §4.2.4): its caption, with Show on map,
   * for holdMs (20 s), announced politely; the hidden map is pre-positioned
   * on the new theater (§4.2.2 step 8).
   */
  function onTheaterNotice(notice) {
    const raw = notice.ids?.[0] || notice.to?.id || null;
    const id = raw ? `thr:${String(raw).replace(/^thr:/, '')}` : null;
    theaterNotice = { text: String(notice.text ?? ''), id };
    cancel(theaterTimer);
    const hold = Number(notice.holdMs);
    theaterTimer = later(
      () => {
        theaterTimer = null;
        theaterNotice = null;
        renderCaption();
        renderNotices();
      },
      Number.isFinite(hold) && hold > 0 ? hold : 20000,
    );
    renderCaption();
    renderNotices();
    if (theaterNotice.text)
      announce(
        theaterNotice.text,
        notice.announce === 'assertive' ? 'assertive' : 'polite',
      );
    if (id && mode.state === 'orb' && mapSupported()) {
      const area = areaFor([id]);
      if (area.bbox) {
        const target = { bbox: area.bbox };
        const ground = theaterGroundFor(area.bbox, store.get?.().graph);
        if (ground != null) target.groundM = ground;
        port.showArea(target, { animate: false }).catch(() => {});
      }
    }
  }

  function onOrbNotice(notice) {
    if (notice?.kind === 'theater') {
      onTheaterNotice(notice);
      return;
    }
    if (!notice || !Array.isArray(notice.ids) || !notice.ids.length) return;
    arrivalNotice = notice;
    cancel(arrivalTimer);
    arrivalTimer = later(() => {
      arrivalNotice = null;
      renderCaption();
    }, 60000);
    renderCaption();
    if (notice.kind === 'batch') announce(notice.text, 'polite');
  }

  function renderCaption() {
    const kids = [];
    let sig = null;
    let detail = null;
    if (progressNotice) {
      const p = progressNotice;
      sig = [
        'progress',
        p.id,
        p.text,
        Boolean(p.detail),
        ...(p.actions || []).map((a) => a.label),
      ].join('\u0001');
      if (sig === captionSig && (!p.detail || captionDetail)) {
        // Only the seconds moved: update them in place, so Stay in console
        // keeps focus and the status is not re-announced every tick.
        if (captionDetail) replaceKids(captionDetail, [p.detail]);
        return;
      }
      kids.push(h('span', { class: 'ic-caption__text' }, p.text));
      if (p.detail) {
        detail = h(
          'span',
          { class: 'ic-caption__detail', 'aria-hidden': 'true' },
          p.detail,
        );
        kids.push(detail);
      }
      for (const a of p.actions || []) {
        kids.push(btn(a.label, { variant: 'link', onClick: () => a.run() }));
      }
      el.caption.setAttribute('data-kind', 'progress');
    } else if (focusState?.by === 'analyst') {
      const n = focusState.ids.length;
      if (focusState.camera) {
        kids.push(
          h(
            'span',
            { class: 'ic-caption__text', 'data-ink': 'analyst' },
            focusState.note
              ? COPY.analystNote(focusState.note)
              : COPY.analystPointed(n),
          ),
          btn(COPY.clear, { variant: 'link', onClick: () => clearFocus() }),
        );
      } else {
        kids.push(
          h(
            'span',
            { class: 'ic-caption__text', 'data-ink': 'analyst' },
            COPY.analystPointed(n),
          ),
          btn(COPY.show, {
            variant: 'link',
            onClick: () => {
              if (!focusState) return;
              focusState = { ...focusState, camera: true };
              orb.focus?.(focusState.ids, { by: 'analyst', camera: true });
              renderCaption();
            },
          }),
        );
      }
      el.caption.setAttribute('data-kind', 'analyst');
    } else if (suggestion) {
      kids.push(
        h(
          'span',
          { class: 'ic-caption__text', 'data-ink': 'analyst' },
          COPY.analystSuggests(suggestion.label),
        ),
        btn(COPY.inspect, {
          variant: 'link',
          onClick: () => {
            const id = suggestion?.id;
            suggestion = null;
            if (id) bus.emit('inspect', { id });
            renderCaption();
          },
        }),
      );
      el.caption.setAttribute('data-kind', 'analyst');
    } else if (theaterNotice && mode.state === 'orb') {
      kids.push(h('span', { class: 'ic-caption__text' }, theaterNotice.text));
      const act = theaterMapAction();
      if (act) kids.push(btn(act.label, { variant: 'link', onClick: act.run }));
      el.caption.setAttribute('data-kind', 'theater');
    } else if (arrivalNotice) {
      const n = arrivalNotice;
      kids.push(
        h(
          'span',
          { class: 'ic-caption__text' },
          n.text || COPY.newCount(n.count),
        ),
        btn(COPY.show, {
          variant: 'link',
          onClick: () => {
            if (n.kind === 'new_behind' && typeof orb.reveal === 'function') {
              orb.reveal(n.ids);
            } else {
              orb.focus?.(n.ids, { by: 'operator', camera: true });
            }
            arrivalNotice = null;
            renderCaption();
          },
        }),
      );
      el.caption.setAttribute('data-kind', 'arrivals');
    } else {
      el.caption.removeAttribute?.('data-kind');
    }
    captionSig = sig;
    captionDetail = detail;
    replaceKids(el.caption, kids);
    setHidden(el.caption, kids.length === 0);
  }

  function renderFooter() {
    const g = store.get?.().graph;
    const kids = [];
    const inv = invitation(g);
    if (inv) {
      const line = h('p', { class: 'ic-footer__invite' }, inv.text);
      if (
        inv.action === 'all-theaters' &&
        typeof store.setScope === 'function'
      ) {
        line.append(
          btn(COPY.showAllTheaters, {
            variant: 'link',
            onClick: () => store.setScope('all'),
          }),
        );
      }
      kids.push(line);
    }
    let honesty = honestyLine(g);
    // The operator just ended the session: the prefix goes with the strip.
    if (!wargameOn && honesty.startsWith(`${STRIP_COPY.honesty} `))
      honesty = honesty.slice(STRIP_COPY.honesty.length + 1);
    if (honesty) kids.push(h('p', { class: 'ic-footer__honesty' }, honesty));
    replaceKids(el.footer, kids);
    syncPlateBottom();
  }

  /** Keep the wide plate above the footer, however many lines it wraps to. */
  function syncPlateBottom() {
    const fh = el.footer.getBoundingClientRect?.().height;
    if (!(fh > 0)) return;
    // The stage bottom's 12 px padding, the footer, then an 8 px gap.
    el.stage.style?.setProperty?.(
      '--ic-plate-bottom',
      `${Math.ceil(12 + fh + 8)}px`,
    );
  }

  function renderService() {
    const st = store.get?.() || {};
    const line = serviceLine(st, { recoveredAt, connecting });
    if (!line) {
      replaceKids(el.status, []);
      setHidden(el.status, true);
      return;
    }
    const kids = [
      iconEl(
        line.tone === 'critical'
          ? st.status === 'unauthorized'
            ? ICON.signIn
            : ICON.offline
          : line.tone === 'ok'
            ? ICON.info
            : ICON.warning,
      ),
      h('span', { class: 'ic-status__text' }, line.text),
    ];
    if (line.retry && typeof store.refresh === 'function') {
      kids.push(
        btn(COPY.retryNow, {
          icon: ICON.retry,
          variant: 'link',
          onClick: () => store.refresh(),
        }),
      );
    }
    el.status.setAttribute('data-tone', line.tone);
    replaceKids(el.status, kids);
    setHidden(el.status, false);
  }

  function renderNotices(list = modeNotices) {
    modeNotices = Array.isArray(list) ? list : [];
    progressNotice = modeNotices.find((n) => n.kind === 'progress') || null;
    renderCaption();
    const toastList = modeNotices.filter((n) => n.kind !== 'progress');
    if (mapNotice) toastList.push(mapNotice);
    // Off the orb the caption is hidden: the theater change is a toast.
    if (theaterNotice && mode.state !== 'orb') {
      const act = theaterMapAction();
      toastList.push({
        id: 'theater',
        kind: 'static',
        source: 'orb',
        text: theaterNotice.text,
        actions: act ? [act] : [],
        theater: true,
      });
    }
    const kids = toastList.map((n) => {
      const toast = h(
        'div',
        {
          class: 'ic-toast',
          'data-kind': n.kind,
          'data-source': n.source || null,
          role: n.kind === 'error' ? 'alert' : 'status',
        },
        h('p', { class: 'ic-toast__text' }, n.text),
      );
      const actions = h('div', { class: 'ic-toast__actions' });
      for (const a of n.actions || []) {
        actions.append(
          btn(a.label, {
            variant:
              a.id === 'track' ||
              a.id === 'retry' ||
              a.id === 'back' ||
              a.id === 'map'
                ? 'primary'
                : 'quiet',
            onClick: () => a.run(),
          }),
        );
      }
      if (n.kind === 'static' || n.kind === 'error') {
        actions.append(
          btn('', {
            icon: ICON.close,
            variant: 'icon',
            aria: COPY.close,
            title: COPY.close,
            onClick: () => {
              if (n === mapNotice) {
                mapNotice = null;
                renderNotices();
              } else if (n.theater) clearTheaterNotice();
              else mode.dismissNotice(n.id);
            },
          }),
        );
      }
      toast.append(actions);
      return toast;
    });
    replaceKids(el.toasts, kids);
    setHidden(el.toasts, kids.length === 0);
  }

  // ---- rendering: banners ----------------------------------------------------------

  function trackedVehicle() {
    return mode.vehicle ? store.vehicle?.(mode.vehicle) || null : null;
  }

  function viewBanner() {
    const list = store.unviewedCritical?.() || [];
    const first = list[0];
    if (!first) return;
    store.markAlarmViewed?.(first.id);
    const graphId = /^alarm:\d+$/.test(String(first.id)) ? first.id : null;
    if (graphId && mode.state === 'orb') {
      if (layout === 'narrow') setTab('orb');
      bus.emit('inspect', { id: graphId });
    }
    renderBanners();
  }

  function bannerNode(model, where) {
    const kids = [
      iconEl(model.count ? ICON.critical : ICON.linkOff),
      h('p', { class: 'ic-banner__text' }, model.text),
    ];
    if (model.count) {
      kids.push(
        btn(COPY.view, { variant: 'on-critical', onClick: () => viewBanner() }),
      );
    }
    return h(
      'div',
      { class: 'ic-banner', role: 'alert', 'data-where': where },
      ...kids,
    );
  }

  function keyBanner(text, where) {
    return h(
      'div',
      {
        class: 'ic-banner',
        role: 'status',
        'data-tone': 'warn',
        'data-where': where,
        'data-kind': 'console-key',
      },
      iconEl(ICON.warning),
      h('p', { class: 'ic-banner__text' }, text),
    );
  }

  function renderBanners() {
    const tracking = mode.state === 'tracking';
    const model = bannerModel({
      alarms: store.unviewedCritical?.() || [],
      tracking,
      vehicle: tracking ? trackedVehicle() : null,
    });
    const show = Boolean(model.text);
    const overMap = tracking || mode.state === 'map';
    const target = overMap ? 'map' : layout === 'narrow' ? 'narrow' : 'stage';
    // A refused console-key claim (WG §3.5): a warn banner until reload.
    const keyText = consoleKey?.bannerText?.() || null;
    for (const [where, host] of [
      ['stage', el.stageBanner],
      ['narrow', el.narrowBanner],
      ['map', el.mapBanner],
    ]) {
      const kids = [];
      if (where === target && show) kids.push(bannerNode(model, where));
      if (where === target && keyText) kids.push(keyBanner(keyText, where));
      replaceKids(host, kids);
      setHidden(host, kids.length === 0);
    }
    // The dock's live alarm badge.
    const n = model.count;
    setText(el.dockBadge, n ? String(n) : '');
    setHidden(el.dockBadge, !n);
    setText(el.mapDock.badge, n ? String(n) : '');
    setHidden(el.mapDock.badge, !n);
  }

  // ---- rendering: dock, narrow bar, tabs -----------------------------------------

  function renderDock() {
    const v = mode.vehicle;
    const tracking = mode.state === 'tracking' || mode.state === 'exiting';
    setHidden(el.dock, !(tracking && v));
    if (!(tracking && v)) return;
    const veh = store.vehicle?.(v) || {};
    const mission = veh.missionNode;
    const line = [COPY.tracking(v)];
    if (mission) {
      const kind = mission.attrs?.kind
        ? missionKindTitle(mission.attrs.kind)
        : String(mission.label || '').split(' · ')[0];
      if (kind) line.push(kind[0].toUpperCase() + kind.slice(1));
      const pct = num(mission.attrs?.progress_pct);
      if (pct != null) line.push(`${Math.round(pct)}%`);
    }
    replaceKids(
      el.dockLine,
      line.map((part, i) =>
        h(
          'span',
          { class: i === 0 ? 'ic-dock__vehicle' : 'ic-dock__seg' },
          part,
        ),
      ),
    );
    const fs = fuelState(veh);
    const readout = [];
    if (fs.fuel != null) readout.push(`Fuel ${formatFuel(fs.fuel)}`);
    if (fs.bingo != null) readout.push(`BINGO ${formatFuel(fs.bingo)}`);
    readout.push(fuelSentence(fs, { short: true }));
    replaceKids(el.dockFuel, [
      h(
        'p',
        { class: 'ic-dock__readout' },
        ...readout.map((t) => h('span', { class: 'ic-dock__seg' }, t)),
      ),
      fuelBar(fs, { status: fuelTone(fs) }),
      ...linkLines(veh).map((l) =>
        h('p', { class: 'ic-dock__link', 'data-status': l.status }, l.text),
      ),
    ]);
    setText(el.dockAbortLabel, COPY.abort(v));
    el.dockAbort.setAttribute('aria-label', COPY.abort(v));
    // Collapsed tab: approval badge and a vertical fuel bar.
    replaceKids(el.dockTabFuel, [
      fuelBar(fs, { status: fuelTone(fs), vertical: true }),
    ]);
  }

  function refreshMini(host = el.dockMini) {
    let snap = null;
    try {
      snap = orb.snapshot?.(64) || null;
    } catch {
      snap = null;
    }
    replaceKids(host, [
      snap && typeof snap === 'object' ? snap : iconEl(ICON.orb),
    ]);
  }

  /** The map overview dock (mapDock.js), shown in the map and leaving it. */
  function renderMapDock() {
    const m = mode.state;
    const target = mode.target;
    const on = Boolean(target) && (m === 'map' || m === 'exiting');
    setHidden(el.mapDock.element, !on);
    if (!on) return;
    let overlay = null;
    try {
      overlay = port.overlayStats?.() ?? null;
    } catch {
      overlay = null;
    }
    el.mapDock.update(
      mapDockModel({
        target,
        graph: store.get?.().graph,
        overlay,
        sitesOn: mode.overlays?.sites !== false,
        wargameOn: mode.overlays || {},
      }),
    );
  }

  /** Orb | List | Map: Map says why when the port can't show areas. */
  function renderMapToggle() {
    el.viewMap.setAttribute(
      'aria-pressed',
      mode.state === 'map' ? 'true' : 'false',
    );
    if (mapSupported()) {
      el.viewMap.removeAttribute?.('aria-disabled');
      el.viewMap.removeAttribute?.('title');
    } else {
      el.viewMap.setAttribute('aria-disabled', 'true');
      el.viewMap.setAttribute('title', COPY.mapUnsupported);
    }
  }

  /** The inspector's layout: a sheet over the dock while in the map. */
  function inspectorLayout() {
    return plateInSheet && layout !== 'narrow' ? 'compact' : layout;
  }

  /**
   * In the map a pick opens the inspector as a sheet over the dock (WG spec
   * §4.2.7): its host moves into the analyst column, since `.ic-main` is
   * hidden and inert; it moves back to the stage when the map closes.
   */
  function placePlate(inMap) {
    if (inMap === plateInSheet) return;
    plateInSheet = inMap;
    if (inMap) {
      // The map opens on the dock, not on an inspector left open on the orb.
      inspector?.hide?.();
      el.mapSheet.append(el.plate);
    } else replaceKids(el.stageBottom, [el.caption, el.plate, el.footer]);
    setHidden(el.mapSheet, !inMap);
    inspector?.setLayout?.(inspectorLayout());
  }

  function renderNarrowBar() {
    const st = store.get?.() || {};
    const g = st.graph;
    setText(
      el.narrowTheater,
      g?.theater?.label || (g ? 'No active theater' : 'Theater not known yet'),
    );
    const vehicles = nodesOf(g, 'vehicle');
    const lead = mode.vehicle ? `veh:${mode.vehicle}` : vehicles[0]?.id;
    const node = lead
      ? st.byId?.get?.(lead) || vehicles.find((x) => x.id === lead)
      : null;
    const kids = [];
    if (node) {
      const fs = fuelState(node.attrs || {});
      kids.push(h('span', { class: 'ic-narrowbar__vehicle' }, node.label));
      if (fs.fuel != null)
        kids.push(
          h('span', { class: 'ic-narrowbar__seg' }, formatFuel(fs.fuel)),
        );
      if (fs.bingo != null) {
        kids.push(
          h(
            'span',
            { class: 'ic-narrowbar__seg' },
            `BINGO ${formatFuel(fs.bingo)}`,
          ),
        );
      }
      kids.push(
        h(
          'span',
          { class: 'ic-narrowbar__bar' },
          fuelBar(fs, { status: fuelTone(fs) }),
        ),
      );
    }
    const crit = (store.unviewedCritical?.() || []).length;
    if (crit) {
      kids.push(
        h(
          'span',
          {
            class: 'ic-narrowbar__crit',
            'aria-label': COPY.criticalCount(crit),
          },
          iconEl(ICON.warning),
          String(crit),
        ),
      );
    }
    replaceKids(el.narrowFleet, kids);
  }

  function renderBadges() {
    const n = pending.count;
    setText(el.tabAnalystBadge, n ? String(n) : '');
    setHidden(el.tabAnalystBadge, !n);
    setText(el.dockTabBadge, n ? String(n) : '');
    setHidden(el.dockTabBadge, !n);
    setText(el.tabOrbBadge, orbTabBadge ? String(orbTabBadge) : '');
    setHidden(el.tabOrbBadge, !orbTabBadge);
    setText(el.tabAnalystNote, n ? `, ${COPY.approvalsWaiting(n)}` : '');
    setText(
      el.tabOrbNote,
      orbTabBadge ? `, ${COPY.pointedOut(orbTabBadge)}` : '',
    );
    const showApproval =
      n > 0 && layout === 'narrow' && tab !== 'analyst' && !overMap();
    if (showApproval) {
      const oldest = pending.oldest;
      const title = oldest?.title
        ? [oldest.title, oldest.vehicle].filter(Boolean).join(', ')
        : '';
      replaceKids(el.narrowApproval, [
        h(
          'p',
          { class: 'ic-narrowbar__approvaltext' },
          n > 1
            ? COPY.approvalMany(n)
            : title
              ? COPY.approvalOneNamed(title)
              : COPY.approvalOne,
        ),
        btn(COPY.review, {
          variant: 'primary',
          // The Analyst tab, scrolled to the oldest slip (§6.5 Review).
          onClick: () => bus.emit('approval:review', {}),
        }),
      ]);
    } else replaceKids(el.narrowApproval, []);
    setHidden(el.narrowApproval, !showApproval);
  }

  /**
   * The narrow bar (fuel vs BINGO, critical count, tabs, banners) is always
   * visible (§3c): sheets opened at narrow start under it, never over it.
   */
  function syncNarrowBarHeight() {
    const hgt = el.narrowBar.getBoundingClientRect?.().height;
    // Sheets at narrow start under the wargame strip too (WG §5.3.2).
    const stripH = wargameOn
      ? Number(strip.element.getBoundingClientRect?.().height) || STRIP_H
      : 0;
    el.root.style?.setProperty?.(
      '--ic-narrowbar-h',
      `${Math.max(0, Math.round((Number(hgt) || 0) + stripH))}px`,
    );
  }

  function setTab(next) {
    const value = ['orb', 'analyst', 'situation'].includes(next) ? next : 'orb';
    // A tab chosen on the narrow bar leaves analyst settings.
    if (value !== tab && settingsSheet?.isOpen?.())
      settingsSheet.close({ restore: false });
    tab = value;
    if (tab === 'orb') orbTabBadge = 0;
    el.root.setAttribute('data-tab', tab);
    for (const [key, b] of Object.entries(el.tabs)) {
      b.setAttribute('aria-selected', key === tab ? 'true' : 'false');
      b.setAttribute('tabindex', key === tab ? '0' : '-1');
    }
    applyRegions();
    renderBadges();
  }

  // ---- regions -----------------------------------------------------------------------

  /** Tracking and the map overview: the map shows and the column is a dock. */
  function overMap() {
    return mode.state === 'tracking' || mode.state === 'map';
  }

  function applyRegions() {
    // The map overview lays the regions out as tracking does (WG §4.2.7).
    const tracking = overMap();
    const narrow = layout === 'narrow';
    el.root.setAttribute('data-layout', layout);
    el.root.setAttribute('data-analyst', analystOn ? 'on' : 'off');
    el.root.setAttribute(
      'data-dock',
      tracking && dockCollapsed ? 'collapsed' : 'open',
    );

    // Narrow: one section at a time; the others are hidden and inert.
    const railOn = !narrow || tab === 'situation';
    const stageOn = !narrow || tab === 'orb';
    // Analyst settings cover the analyst column (wide, compact) or every
    // region under the narrow bar (narrow): what is under the sheet is inert.
    // Over the 56 px spine the sheet also covers the stage's right edge
    // (search, the Orb/List toggle), so the stage goes inert too.
    const covered = settingsOpen && narrow;
    const overSpine = settingsOpen && !analystOn && !narrow && !tracking;
    setHidden(el.rail, !railOn);
    setInert(el.rail, !railOn || covered);
    setHidden(el.stage, !stageOn);
    setInert(el.stage, !stageOn || covered || overSpine);
    const analystVisible = tracking
      ? !dockCollapsed
      : !narrow || tab === 'analyst';
    setHidden(el.analyst, !analystVisible);
    setInert(el.analyst, !analystVisible || settingsOpen);

    // Analyst unavailable (§3e): a 56 px spine, never at narrow or in tracking.
    const spine = !analystOn && !narrow && !tracking;
    el.root.setAttribute('data-spine', spine ? 'on' : 'off');
    setHidden(el.spine, !spine);
    setHidden(el.analystBody, spine);
    if (!spine && spineOpen) closeSpine({ restore: false });

    setHidden(el.narrowBar, !(narrow && !tracking));
    // In tracking, Back to console comes first (§8): search and the analyst
    // skips point into the inert stage or duplicate the dock header.
    setHidden(el.skipSearch, tracking);
    setHidden(el.skipAnalyst, tracking);
    // Narrow: the tabs own their panels (ARIA tabs); wider, plain regions.
    for (const [key, panel] of [
      ['orb', el.stage],
      ['analyst', el.analyst],
      ['situation', el.rail],
    ]) {
      if (narrow && !tracking) {
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', `ic-tab-${key}`);
      } else {
        panel.removeAttribute?.('role');
        panel.removeAttribute?.('aria-labelledby');
      }
    }
    setHidden(el.dockTab, !(tracking && dockCollapsed));
    setHidden(el.backFloat, !(tracking && narrow));
    el.dockCollapse.setAttribute(
      'aria-expanded',
      dockCollapsed ? 'false' : 'true',
    );
    renderBanners();
  }

  function setLayout(next) {
    const value = ['wide', 'compact', 'narrow'].includes(next) ? next : 'wide';
    if (value === layout) return;
    layout = value;
    applyRegions();
    try {
      orb.setOptions?.({
        labelBudget: layout === 'wide' ? 12 : layout === 'compact' ? 6 : 4,
        labelMode: layout === 'wide' ? 'margin' : 'inline',
      });
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    situation?.setLayout?.(layout);
    analyst?.setLayout?.(layout);
    inspector?.setLayout?.(inspectorLayout());
    settingsSheet?.setLayout?.(layout);
    bus.emit('layout', { layout });
    // The inspector also hears 'layout': in the map it stays a sheet.
    if (plateInSheet) inspector?.setLayout?.(inspectorLayout());
    applyViewport();
    renderBadges();
    if (overMap()) port.setViewportInset(viewportInset());
  }

  function setDockCollapsed(collapsed) {
    dockCollapsed = Boolean(collapsed);
    applyRegions();
    if (mode.state === 'tracking') port.setViewportInset(viewportInset());
    analyst?.setDocked?.(mode.state === 'tracking' && !dockCollapsed);
    if (!dockCollapsed) el.dockBack.focus?.();
    else el.dockTab.focus?.();
  }

  // ---- analyst availability ----------------------------------------------------------

  function applyAvailability(st, { emit = true } = {}) {
    if (!st || typeof st !== 'object') return;
    const serviceFailure =
      st.transient || st.reason === 'token' || st.reason === 'unreachable';
    if (serviceFailure) {
      // Once the analyst has answered, a service failure is the service
      // line's to report: keep the transcript and its slips on screen.
      if (analystReached) return;
      st = {
        available: false,
        reason: st.reason === 'token' ? 'token' : 'unreachable',
        hint: null,
      };
    } else if (st.available !== false) analystReached = true;
    const available = st.available !== false;
    analystStatus = st;
    const changed = available !== analystOn;
    analystOn = available;
    if (changed) {
      applyRegions();
      applyViewport();
    }
    renderSpine();
    if (emit) {
      bus.emit('analyst:availability', {
        available,
        reason: st.reason ?? null,
        hint: st.hint ?? null,
        ...(st.provider ? { provider: st.provider } : {}),
      });
    }
  }

  async function checkAnalyst() {
    if (!chat?.status) return;
    try {
      applyAvailability(await chat.status());
    } catch (err) {
      globalThis.console?.error?.(err);
    }
  }

  function renderSpine() {
    const copy = analystUnavailableCopy(analystStatus);
    el.spine.setAttribute('title', COPY.notAvailable);
    const kids = [h('h2', { class: 'ic-popover__title' }, copy.title)];
    if (copy.body) kids.push(h('p', { class: 'ic-popover__body' }, copy.body));
    if (copy.hint && copy.hint.trim() !== copy.title)
      kids.push(h('p', { class: 'ic-popover__hint' }, copy.hint));
    const actions = [];
    // A provider reason opens analyst settings (primary); Check again stays.
    if (copy.action && settingsSheet) {
      actions.push(
        btn(copy.action, {
          icon: ICON.settings,
          variant: 'primary',
          onClick: () => openSettings({ provider: copy.provider || null }),
        }),
      );
    }
    actions.push(
      btn(COPY.checkAgain, {
        icon: ICON.retry,
        variant: copy.action && settingsSheet ? 'quiet' : 'primary',
        onClick: () => checkAnalyst(),
      }),
    );
    if (copy.secondary && settingsSheet) {
      actions.push(
        btn(copy.secondary, {
          variant: 'quiet',
          onClick: () =>
            openSettings({ provider: copy.secondaryProvider || null }),
        }),
      );
    }
    kids.push(h('div', { class: 'ic-popover__actions' }, ...actions));
    replaceKids(el.spinePop, kids);
  }

  function openSpine() {
    spineOpen = true;
    renderSpine();
    setHidden(el.spinePop, false);
    el.spine.setAttribute('aria-expanded', 'true');
  }

  function closeSpine({ restore = true } = {}) {
    if (!spineOpen) return;
    spineOpen = false;
    setHidden(el.spinePop, true);
    el.spine.setAttribute('aria-expanded', 'false');
    if (restore) el.spine.focus?.();
  }

  // ---- shortcut sheet ------------------------------------------------------------------

  function openSheet() {
    if (!sheetOpen) sheetOpener = rememberFocus(doc?.activeElement);
    sheetOpen = true;
    setHidden(el.sheet, false);
    el.sheetClose.focus?.();
  }

  function closeSheet() {
    if (!sheetOpen) return;
    sheetOpen = false;
    setHidden(el.sheet, true);
    const opener = sheetOpener;
    sheetOpener = null;
    // Back to where '?' was pressed, unless focus already moved on.
    const active = doc?.activeElement;
    if (!active || active === doc?.body || contains(el.sheet, active))
      focusBack(opener, { doc });
  }

  // ---- wargame view (WG §5.3.3) -------------------------------------------------------

  /**
   * Blue view or Umpire view, as the intel store last announced it
   * (`wargame:view {view, truth}`): the orb and the List view hide or show
   * red, and the map overlay asks for truth only in Umpire view. Outside a
   * session (`view` null) the orb keeps its default and the map stays in
   * Blue view.
   */
  function applyWargameView(p) {
    const v = p?.view === 'umpire' || p?.view === 'blue' ? p.view : null;
    wargameView = v;
    if (v) {
      const umpire = v === 'umpire';
      try {
        orb.setView?.({ umpire });
      } catch (err) {
        globalThis.console?.error?.(err);
      }
      try {
        listView?.setView?.({ umpire });
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
    try {
      port.setOverlayTruth?.(v === 'umpire' && p?.truth === true);
    } catch (err) {
      globalThis.console?.error?.(err);
    }
  }

  // ---- view toggle (Orb | List) --------------------------------------------------------

  function setView(next) {
    // Orb or List from the toggle leaves the read view (§5.3.11).
    if (reading) {
      reading = null;
      readView?.close?.();
      setHidden(el.readHost, true);
      el.stage.removeAttribute?.('data-view');
    }
    const wasInList =
      view === 'list' &&
      next !== 'list' &&
      contains(el.listHost, doc?.activeElement);
    view = next === 'list' ? 'list' : 'orb';
    el.viewOrb.setAttribute('aria-pressed', view === 'orb' ? 'true' : 'false');
    el.viewList.setAttribute(
      'aria-pressed',
      view === 'list' ? 'true' : 'false',
    );
    if (
      view === 'list' &&
      !listView &&
      typeof C.createOrbListView === 'function'
    ) {
      try {
        listView = C.createOrbListView(el.listHost, {
          onSelect: (id) => {
            if (!id) return;
            orb.select?.(id);
            bus.emit('inspect', { id });
          },
        });
        if (wargameView)
          listView?.setView?.({ umpire: wargameView === 'umpire' });
        listView?.setGraph?.(store.get?.().graph);
        if (filterIds) listView?.filter?.((n) => filterIds.has(n?.id ?? n));
      } catch (err) {
        globalThis.console?.error?.(err);
        listView = null;
      }
    }
    setHidden(el.orbWrap, view !== 'orb');
    setHidden(el.listHost, view !== 'list');
    // Leaving the list with focus in it: the orb's listbox twin takes it.
    if (wasInList)
      (firstByClass(el.orbA11y, 'ic-orb-twin') || el.viewOrb)?.focus?.();
  }

  // ---- read view (WG §5.3.11) -------------------------------------------------------------

  /** A report to read: `{id, markdown?, title?, node?, entity?}`. Without
   *  Markdown in hand the entity is fetched for its `fields.markdown`. */
  function requestRead(target) {
    if (destroyed || !target || typeof target !== 'object') return;
    if (readDocOf(target)) {
      mode.requestRead(target);
      return;
    }
    const id = typeof target.id === 'string' ? target.id : '';
    // An entity in hand without Markdown has nothing more to fetch.
    const held = target.entity && typeof target.entity === 'object';
    if (!id || held || typeof store.entity !== 'function') {
      announce(READ_COPY.empty, 'polite');
      return;
    }
    Promise.resolve()
      .then(() => store.entity(id))
      .then(
        (entity) => {
          if (destroyed) return;
          if (!mode.requestRead({ id, entity, node: nodeById(id) }))
            announce(READ_COPY.empty, 'polite');
        },
        (err) => {
          if (!destroyed)
            announce(COPY.readFailed(err?.message || err), 'polite');
        },
      );
  }

  function openRead(docToRead) {
    if (!readView || destroyed) return;
    const first = !reading;
    const prev = reading;
    const inspecting = first ? inspector?.current?.() || null : null;
    if (!readView.open(docToRead)) return;
    reading = first
      ? {
          returnView: view,
          inspecting,
          opener: rememberFocus(doc?.activeElement),
        }
      : prev;
    if (layout === 'narrow' && tab !== 'orb') setTab('orb');
    if (inspecting) inspector?.hide?.();
    setPlate(false);
    setHidden(el.orbWrap, true);
    setHidden(el.listHost, true);
    setHidden(el.readHost, false);
    el.stage.setAttribute('data-view', 'read');
    el.viewOrb.setAttribute('aria-pressed', 'false');
    el.viewList.setAttribute('aria-pressed', 'false');
    readView.focus();
  }

  /** Back to the view the read view covered, and the inspector it hid. */
  function closeRead() {
    if (!reading) return false;
    const was = reading;
    setView(was.returnView);
    if (was.inspecting) {
      inspector?.show?.(was.inspecting);
      setPlate(true);
    }
    if (
      !focusBack(was.opener, { doc }) &&
      focusStranded(doc?.activeElement, doc)
    )
      (view === 'list' ? el.viewList : el.viewOrb)?.focus?.();
    return true;
  }

  // ---- Esc layer stack -------------------------------------------------------------------

  /** Close the innermost layer (§9). Returns whether something closed. */
  function escapeLayer() {
    // The End wargame popover is the topmost layer (WG §5.3.2).
    if (strip.escape()) return true;
    if (settingsSheet?.isOpen?.()) {
      settingsSheet.escape();
      return true;
    }
    if (sheetOpen) {
      closeSheet();
      return true;
    }
    if (spineOpen) {
      closeSpine();
      return true;
    }
    if (situation?.isDrawerOpen?.()) {
      situation.closeDrawer?.();
      return true;
    }
    if (mode.cancelNotice()) return true;
    if (mode.state === 'entering_tracking' || mode.state === 'entering_map') {
      mode.cancel();
      return true;
    }
    if (mode.state === 'map') {
      // A pick's inspector sheet first, then the map itself.
      if (plateInSheet && inspector?.current?.()) {
        inspector.hide?.();
        el.mapDock.back.focus?.();
        return true;
      }
      mode.exit();
      return true;
    }
    if (mode.state === 'tracking') {
      mode.exit();
      return true;
    }
    if (reading) {
      // A plate opened over the read view (a search pick) closes first.
      if (inspector?.current?.() || plateOpen) {
        inspector?.hide?.();
        setPlate(false);
        return true;
      }
      return closeRead();
    }
    if (focusState) {
      clearFocus();
      return true;
    }
    if (clearFilter()) return true;
    if (inspector?.current?.() || plateOpen) {
      inspector?.hide?.();
      setPlate(false);
      return true;
    }
    if (selectedId) {
      selectedId = null;
      orb.select?.(null);
      return true;
    }
    return false;
  }

  // ---- regions (F6) ------------------------------------------------------------------------

  function regionList() {
    if (mode.state === 'map') {
      const list = [{ key: 'dock', el: el.mapDock.back }];
      if (plateInSheet && inspector?.current?.())
        list.push({ key: 'inspector', el: inspector?.element || el.plate });
      list.push(
        { key: 'transcript', el: el.analystBody },
        { key: 'composer', el: el.analystBody, composer: true },
      );
      return list;
    }
    if (mode.state === 'tracking') {
      return [
        { key: 'dock', el: el.dockBack },
        { key: 'transcript', el: el.analystBody },
        { key: 'composer', el: el.analystBody, composer: true },
      ];
    }
    const list = [
      { key: 'rail', el: el.railHost },
      { key: 'stage', el: el.stage },
    ];
    if (inspector?.current?.() || plateOpen)
      list.push({ key: 'inspector', el: inspector?.element || el.plate });
    if (analystOn) {
      list.push({ key: 'transcript', el: el.analystBody });
      list.push({ key: 'composer', el: el.analystBody, composer: true });
    } else list.push({ key: 'analyst', el: el.spine });
    return list;
  }

  function cycleRegion(dir) {
    if (layout === 'narrow' && !overMap()) {
      const order = ['orb', 'analyst', 'situation'];
      setTab(order[(order.indexOf(tab) + (dir < 0 ? 2 : 1)) % 3]);
      el.tabs[tab].focus?.();
      return;
    }
    const list = regionList();
    const active = doc?.activeElement;
    // The innermost region holding focus: the inspector sits inside the
    // stage, so "the first that contains it" would always say stage.
    let at = -1;
    list.forEach((r, i) => {
      if (!contains(r.el, active)) return;
      if (at < 0 || (r.el !== list[at].el && contains(list[at].el, r.el)))
        at = i;
    });
    if (at >= 0 && list[at].key === 'transcript' && isTextField(active)) {
      at = list.findIndex((r) => r.composer);
    }
    const next = list[(at + (dir < 0 ? -1 : 1) + list.length) % list.length];
    if (next.key === 'rail' && layout === 'compact') {
      situation?.openDrawer?.();
      return;
    }
    if (next.composer) {
      if (analyst?.focusComposer) analyst.focusComposer();
      else el.analystBody.focus?.();
      return;
    }
    next.el.focus?.();
  }

  // ---- keyboard ----------------------------------------------------------------------------

  /** Stop the analyst's turn: the view's own stop when it has one. */
  function stopAnalyst() {
    if (typeof analyst?.stop === 'function') analyst.stop();
    else chat?.interrupt?.();
  }

  function consume(event) {
    event.preventDefault?.();
    event.stopPropagation?.();
  }

  function openSearch() {
    if (layout === 'narrow') setTab('orb');
    search?.open?.();
  }

  function focusComposer() {
    if (layout === 'narrow' && !overMap()) setTab('analyst');
    if (mode.state === 'tracking' && dockCollapsed) setDockCollapsed(false);
    analyst?.focusComposer?.();
  }

  function onCaptureKey(event) {
    if (destroyed || !event) return;
    const key = event.key;
    const mod = Boolean(event.metaKey || event.ctrlKey);
    const target = event.target;
    const inside = contains(el.root, target);
    const text = isTextField(target);
    const m = mode.state;
    const lower = typeof key === 'string' ? key.toLowerCase() : '';

    if (
      mod &&
      !event.altKey &&
      !event.shiftKey &&
      lower === 'k' &&
      m === 'orb'
    ) {
      consume(event);
      openSearch();
      return;
    }
    if (!mod && !event.altKey && key === '/' && !text && m === 'orb') {
      consume(event);
      openSearch();
      return;
    }
    if (mod && !event.altKey && !event.shiftKey && lower === 'i') {
      consume(event);
      focusComposer();
      return;
    }
    if (mod && !event.altKey && key === '.') {
      consume(event);
      stopAnalyst();
      return;
    }
    if (mod && !event.altKey && !event.shiftKey && key === ',') {
      // ⌘, / Ctrl+, : analyst settings, from anywhere (BYOK spec §10).
      if (settingsSheet) {
        consume(event);
        openSettings({ invoker: doc?.activeElement || null });
      }
      return;
    }
    if (key === 'F6' && !mod && !event.altKey) {
      consume(event);
      cycleRegion(event.shiftKey ? -1 : 1);
      return;
    }
    if (mod && !event.altKey && key === '\\' && m === 'tracking') {
      consume(event);
      setDockCollapsed(!dockCollapsed);
      return;
    }
    if (!mod && !event.altKey && key === '?' && !text && m === 'orb') {
      consume(event);
      if (sheetOpen) closeSheet();
      else openSheet();
      return;
    }
    if (
      event.altKey &&
      !mod &&
      event.code === 'KeyA' &&
      !text &&
      typeof analyst?.reviewApprovals === 'function'
    ) {
      consume(event);
      if (layout === 'narrow' && m !== 'tracking') setTab('analyst');
      analyst.reviewApprovals();
      return;
    }

    if (
      !mod &&
      !event.altKey &&
      lower === 'm' &&
      !text &&
      m === 'orb' &&
      inOrbScope(target)
    ) {
      // M in the orb scope opens the map of the theater (WG spec §4.2.7).
      consume(event);
      openTheaterMap();
      return;
    }

    if (m !== 'tracking') {
      // The map is hidden (or in overview, where GEV's own shortcuts would
      // fight the console): nothing outside the console may react to a key.
      if (!inside) {
        event.stopPropagation?.();
        if (key === 'Escape' && !mod) {
          if (escapeLayer()) event.preventDefault?.();
        }
        return;
      }
      shieldFromDocumentCapture(event, doc);
      return;
    }
    // Tracking: GEV's cockpit keys keep working, but an Esc pressed inside the
    // console belongs to the console (never GEV's own cockpit exit first).
    if (key === 'Escape' && inside) shieldFromDocumentCapture(event, doc);
  }

  /** The orb scope: the stage (not the inspector or search), or nothing focused. */
  function inOrbScope(target) {
    if (!target || target === doc?.body || target === doc?.documentElement)
      return true;
    if (target === el.root) return true;
    return (
      contains(el.stage, target) &&
      !contains(el.plate, target) &&
      !contains(el.searchHost, target)
    );
  }

  function onRootKey(event) {
    if (destroyed || !event) return;
    const m = mode.state;
    const isEsc = event.key === 'Escape';
    // GEV's bubbling shortcuts (h, o, v, f, d, c, 1–7) never see console keys
    // while the map is hidden; in tracking only Esc is kept back.
    if (m !== 'tracking' || isEsc) event.stopPropagation?.();
    if (!isEsc || event.defaultPrevented) return;
    if (m === 'tracking') {
      const target = event.target;
      if (
        turnRunning &&
        contains(el.analystBody, target) &&
        isTextField(target)
      ) {
        // Esc in the composer while a turn runs stops the analyst, never tracking.
        stopAnalyst();
        event.preventDefault?.();
        return;
      }
      if (sheetOpen) closeSheet();
      else if (!mode.cancelNotice()) mode.exit();
      event.preventDefault?.();
      return;
    }
    // A focused field's own Esc (search, composer, note) already ran.
    if (isTextField(event.target)) return;
    if (escapeLayer()) event.preventDefault?.();
  }

  function onStageInput() {
    store.noteStageInput?.();
  }

  // ---- simulated wargame (WG §5.3.2) and the console key (§3.5) -----------------------------

  /** The strip, the root's data-wargame and the map's top inset, from meta. */
  function renderWargame() {
    const g = store.get?.().graph;
    // The host caught up with an End (or a new session began): forget it.
    if (endedSession && activeWargame(g)?.session_id !== endedSession)
      endedSession = null;
    const model = stripModel(g, { endedSession });
    strip.update(model);
    const on = model != null;
    if (on === wargameOn) return;
    wargameOn = on;
    if (on) el.root.setAttribute('data-wargame', 'on');
    else el.root.removeAttribute?.('data-wargame');
    if (overMap()) port.setViewportInset(viewportInset());
    syncNarrowBarHeight();
    // A console that couldn't claim at boot tries again when a session starts.
    if (on && consoleKey.state === 'failed') claimKey();
  }

  /** Claim the engagement approval key once; retry only an unanswered claim. */
  function claimKey() {
    if (destroyed) return;
    cancel(claimTimer);
    claimTimer = null;
    Promise.resolve()
      .then(() => consoleKey.ensure())
      .then((state) => {
        if (destroyed) return;
        if (state === 'failed') {
          const delay =
            CLAIM_RETRY_MS[Math.min(claimTries, CLAIM_RETRY_MS.length - 1)];
          claimTries += 1;
          claimTimer = later(() => claimKey(), delay);
        } else {
          claimTries = 0;
        }
      })
      .catch((err) => globalThis.console?.error?.(err));
  }

  /** The operator's End wargame succeeded: the strip goes now, the divider
   *  (chat/view.js) hears `wargame:ended`, and the picture refreshes. */
  function onWargameEnded(info) {
    if (destroyed) return;
    endedSession = info?.session_id || null;
    const z = formatZulu(info?.ended_at_ms, { seconds: true });
    const hadFocus = contains(strip.element, doc?.activeElement);
    renderWargame();
    renderFooter();
    if (z) announce(STRIP_COPY.endedByYou(z), 'polite');
    bus.emit('wargame:ended', {
      by: 'operator',
      session_id: info?.session_id ?? null,
      aar_id: info?.aar_id ?? null,
      ended_at_ms: info?.ended_at_ms ?? null,
    });
    try {
      store.refresh?.();
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    if (hadFocus || focusStranded(doc?.activeElement, doc))
      (mode.state === 'orb' ? el.stage : el.analystBody)?.focus?.();
  }

  stripHooks.end = () =>
    typeof api.endWargame === 'function'
      ? api.endWargame()
      : api.post(WARGAME_END_PATH, {});
  stripHooks.onEnded = (info) => onWargameEnded(info);
  stripHooks.announce = (text, politeness) => announce(text, politeness);
  stripHooks.now = () => time.now();

  // ---- event wiring ------------------------------------------------------------------------

  el.skipSearch.addEventListener('click', () => openSearch());
  el.skipAnalyst.addEventListener('click', () => focusComposer());
  el.viewOrb.addEventListener('click', () => setView('orb'));
  el.viewList.addEventListener('click', () => setView('list'));
  el.viewMap.addEventListener('click', () => openTheaterMap());
  el.spine.addEventListener('click', () =>
    spineOpen ? closeSpine() : openSpine(),
  );
  el.sheetClose.addEventListener('click', () => closeSheet());
  el.dockBack.addEventListener('click', () => mode.backToConsole());
  el.backFloat.addEventListener('click', () => mode.backToConsole());
  el.dockCollapse.addEventListener('click', () => setDockCollapsed(true));
  el.dockTab.addEventListener('click', () => setDockCollapsed(false));
  el.dockAbort.addEventListener('click', () => {
    const v = mode.vehicle;
    if (v) C.confirmAbort?.(ctx, v);
  });
  for (const [key, b] of Object.entries(el.tabs)) {
    b.addEventListener('click', () => setTab(key));
    b.addEventListener('keydown', (event) => {
      const order = ['orb', 'analyst', 'situation'];
      const at = order.indexOf(key);
      const next =
        event.key === 'ArrowRight'
          ? order[(at + 1) % 3]
          : event.key === 'ArrowLeft'
            ? order[(at + 2) % 3]
            : event.key === 'Home'
              ? order[0]
              : event.key === 'End'
                ? order[2]
                : null;
      if (!next) return;
      event.preventDefault?.();
      setTab(next);
      el.tabs[next].focus?.();
    });
  }
  for (const type of ['pointerdown', 'wheel', 'keydown']) {
    el.stage.addEventListener(
      type,
      onStageInput,
      type === 'wheel' ? { passive: true } : undefined,
    );
  }
  el.root.addEventListener('keydown', onRootKey);
  win?.addEventListener?.('keydown', onCaptureKey, true);
  offs.push(() => win?.removeEventListener?.('keydown', onCaptureKey, true));

  offs.push(
    bus.on('focus:entities', (p) => {
      const ids = Array.isArray(p?.ids) ? p.ids.filter(Boolean) : [];
      const by = p?.by === 'analyst' ? 'analyst' : 'operator';
      if (!ids.length) {
        focusState = null;
        orb.focus?.([], { by, camera: false });
        renderCaption();
        return;
      }
      const last = Number(store.lastStageInputAt) || 0;
      const quiet = !last || time.now() - last >= RECENT_INPUT_MS;
      const camera = by === 'operator' || quiet;
      orb.focus?.(ids, { by, camera });
      focusState =
        by === 'analyst'
          ? { ids, by, note: String(p?.note || ''), camera }
          : null;
      if (by === 'analyst' && layout === 'narrow' && tab !== 'orb') {
        orbTabBadge = ids.length;
        renderBadges();
      }
      renderCaption();
    }),
    bus.on('search:filter', (p) => {
      const ids = Array.isArray(p?.ids) ? p.ids : null;
      filterIds = ids ? new Set(ids) : null;
      if (p?.source !== 'shell')
        listView?.filter?.(filterIds ? (n) => filterIds.has(n?.id ?? n) : null);
      if (filterIds && focusState) clearFocus({ silent: true });
    }),
    bus.on('inspect', (p) => {
      if (!p?.id) {
        setPlate(false);
        return;
      }
      if (p.by === 'analyst' && layout !== 'wide') {
        // The analyst never covers the chat: select, and suggest (§4.6).
        const node = nodeById(p.id);
        orb.select?.(p.id);
        suggestion = { id: p.id, label: node?.label || p.id };
        renderCaption();
        return;
      }
      suggestion = null;
      selectedId = p.id;
      if (layout === 'narrow' && tab !== 'orb' && !overMap()) setTab('orb');
      setPlate(true);
    }),
    bus.on('inspector:state', (p) => {
      setPlate(Boolean(p?.open));
    }),
    bus.on('ask', () => {
      if (layout === 'narrow' && mode.state !== 'tracking') setTab('analyst');
      if (mode.state === 'tracking' && dockCollapsed) setDockCollapsed(false);
    }),
    bus.on('alarm:viewed', (p) => {
      if (p?.id) store.markAlarmViewed?.(p.id);
      renderBanners();
      renderNarrowBar();
    }),
    bus.on('approval:pending', (p) => {
      const n = Number(p?.count);
      pending = {
        count: Number.isFinite(n) && n > 0 ? n : 0,
        oldest: p?.oldest || null,
      };
      renderBadges();
    }),
    bus.on('analyst:availability', (p) => {
      if (!p) return;
      applyAvailability(
        {
          available: p.available !== false,
          reason: p.reason ?? null,
          hint: p.hint ?? null,
          provider: p.provider ?? analystStatus?.provider ?? null,
        },
        { emit: false },
      );
    }),
    bus.on('approval:review', () => {
      // Review from a sheet that covered the analyst's approval bar.
      if (layout === 'narrow' && mode.state !== 'tracking') setTab('analyst');
      analyst?.reviewApprovals?.();
    }),
    bus.on('abort:request', (p) => {
      if (p?.vehicle) C.confirmAbort?.(ctx, p.vehicle);
    }),
    // Read in full (the report inspector, WG §5.3.11). The inspector's
    // after-action review says `read:open {id, title, markdown}`.
    bus.on('read:request', (p) => requestRead(p)),
    bus.on('read:open', (p) => requestRead(p)),
    // The rail's "Show all" engagements: the List view (its search:filter
    // came first).
    bus.on('view:request', (p) => {
      if (p?.view === 'list' || p?.view === 'orb') setView(p.view);
    }),
    // The wargame view (intelStore, WG §5.3.3): the orb, the List view and
    // the map's context overlay follow it.
    bus.on('wargame:view', (p) => applyWargameView(p)),
    bus.on('settings:open', (p) => {
      openSettings({
        provider: typeof p?.provider === 'string' ? p.provider : null,
        invoker: p?.invoker || null,
      });
    }),
    bus.on('settings:state', (p) => {
      const next = Boolean(p?.open);
      if (next === settingsOpen) return;
      settingsOpen = next;
      applyRegions();
    }),
    bus.on('settings:changed', () => {
      // A new provider or key: ask the analyst again (the spine may lift).
      checkAnalyst();
    }),
    bus.on('gev:status', (p) => {
      // GEV (the map under the console) reports its start-up. Only a failure
      // is the operator's business: tracking cannot work without the map.
      renderMapToggle();
      if (p?.state !== 'failed') {
        if (mapNotice && p?.state === 'ready') {
          mapNotice = null;
          renderNotices();
        }
        return;
      }
      const text = COPY.mapFailed(String(p.message ?? '').trim());
      if (mapNotice?.text === text) return;
      mapNotice = {
        id: 'gev',
        kind: 'error',
        source: 'gev',
        text,
        actions: [],
      };
      renderNotices();
      announce(text, 'polite');
    }),
  );

  if (typeof store.on === 'function') {
    offs.push(
      store.on('change', (diff) => {
        const st = store.get?.() || {};
        if (diff?.graph || diff?.first) {
          try {
            orb.setGraph?.(st.graph, diff);
          } catch (err) {
            globalThis.console?.error?.(err);
          }
          listView?.setGraph?.(st.graph);
          renderWargame();
          renderFooter();
        }
        if (diff?.status || diff?.first) {
          try {
            orb.setOptions?.({
              pictureStatus: st.status,
              lastLiveAt: st.lastLiveAt ?? null,
            });
          } catch (err) {
            globalThis.console?.error?.(err);
          }
        }
        if (diff?.status) {
          connecting = false;
          const from = diff.statusFrom;
          if (
            diff.statusTo === 'live' &&
            (from === 'stale' || from === 'offline' || from === 'unauthorized')
          ) {
            recoveredAt = st.lastLiveAt;
            cancel(recoveredTimer);
            recoveredTimer = later(() => {
              recoveredAt = null;
              renderService();
            }, BACK_ONLINE_MS);
            announce(
              COPY.backOnline(formatZulu(st.lastLiveAt, { seconds: true })),
              'polite',
            );
            // The analyst lives in the same service: ask it again at once.
            if (!analystReached) checkAnalyst();
          } else if (diff.statusTo === 'stale' || diff.statusTo === 'offline') {
            recoveredAt = null;
            const line = serviceLine(st);
            if (line) announce(line.text, 'polite');
          } else if (diff.statusTo === 'unauthorized') {
            announce(COPY.unauthorized, 'assertive');
          }
        }
        for (const alarm of diff?.newAlarms || []) {
          const text =
            bannerModel({ alarms: [alarm] }).text || alarmLabel(alarm.kind);
          if (alarm.critical) announce(text, 'assertive');
          else if (alarm.severity === 'warning') announce(text, 'polite');
        }
        renderService();
        renderBanners();
        renderNarrowBar();
        if (mode.state === 'tracking' || mode.state === 'exiting') renderDock();
        renderMapDock();
        renderMapToggle();
      }),
    );
  }

  offs.push(
    mode.onRead((d) => openRead(d)),
    consoleKey.onChange?.(({ state }) => {
      renderBanners();
      // Slips re-check Deny-only (chat/view.js); the key itself never travels.
      bus.emit('console:key', { state, canApprove: state === 'held' });
      if (state === 'refused')
        announce(consoleKey.bannerText?.() || '', 'polite');
    }),
    mode.onNotice((list) => renderNotices(list)),
    mode.onChange(({ mode: m, prev }) => {
      if (m === 'tracking') {
        refreshMini();
        if (!port.supportsInset()) dockCollapsed = true;
        analyst?.setDocked?.(!dockCollapsed);
        renderDock();
      }
      if (m === 'map' && prev !== 'map') {
        // The map dock is never collapsed; the analyst sits under it.
        dockCollapsed = false;
        refreshMini(el.mapDock.mini);
        analyst?.setDocked?.(true);
        renderDock();
      }
      if (m === 'orb') {
        if (prev === 'exiting' || prev === 'tracking')
          analyst?.setDocked?.(false);
        dockCollapsed = false;
        renderDock();
      }
      placePlate(m === 'map');
      renderMapDock();
      renderMapToggle();
      applyRegions();
      renderBadges();
      renderCaption();
      renderNotices();
      // The column may have changed width with the mode (the spine is never
      // a dock): hand GEV the width the dock really has.
      if (m === 'tracking' || m === 'map')
        port.setViewportInset(viewportInset());
      // Entering the map hides or inerts whatever held focus (the Map toggle,
      // `M` on the orb, a rail button, the tracking dock): put it in the map
      // dock, as tracking does its dock (UX §947), never on <body>.
      if (
        m === 'map' &&
        prev !== 'map' &&
        focusStranded(doc?.activeElement, doc)
      )
        el.mapDock.back.focus?.();
    }),
    // A click on the map in overview opens the inspector sheet (§4.2.7).
    port.onPick((p) => {
      const id = typeof p?.id === 'string' ? p.id : '';
      if (!id || mode.state !== 'map') return;
      bus.emit('inspect', { id });
    }),
  );

  if (chat?.on) {
    const offs2 = [
      chat.on('turn_start', () => {
        turnRunning = true;
      }),
      chat.on('turn_end', () => {
        turnRunning = false;
      }),
      chat.on('status', (st) => applyAvailability(st)),
    ];
    for (const off of offs2) if (typeof off === 'function') offs.push(off);
  }

  // ---- layout observer ---------------------------------------------------------------------

  function measureLayout() {
    const w =
      el.root.getBoundingClientRect?.().width || Number(win?.innerWidth) || 0;
    setLayout(layoutFor(w));
  }

  let ro = null;
  const RO = win?.ResizeObserver || globalThis.ResizeObserver;
  if (typeof RO === 'function') {
    try {
      ro = new RO((entries) => {
        for (const entry of entries) {
          if (entry.target === el.root) {
            const w = entry.contentRect?.width;
            setLayout(layoutFor(w > 0 ? w : Number(win?.innerWidth) || 0));
          } else if (entry.target === el.narrowBar) {
            syncNarrowBarHeight();
          } else if (entry.target === el.orbWrap) {
            try {
              orb.resize?.();
            } catch (err) {
              globalThis.console?.error?.(err);
            }
            applyViewport();
          }
        }
      });
      ro.observe(el.root);
      ro.observe(el.orbWrap);
      ro.observe(el.narrowBar);
    } catch {
      ro = null;
    }
  }
  if (!ro && typeof win?.addEventListener === 'function') {
    const onResize = () => {
      measureLayout();
      orb.resize?.();
      applyViewport();
    };
    win.addEventListener('resize', onResize);
    offs.push(() => win.removeEventListener?.('resize', onResize));
  }

  const motion = (() => {
    try {
      return win?.matchMedia?.('(prefers-reduced-motion: reduce)') || null;
    } catch {
      return null;
    }
  })();
  const onMotion = () =>
    orb.setOptions?.({ reducedMotion: Boolean(motion?.matches) });
  if (motion) {
    onMotion();
    motion.addEventListener?.('change', onMotion);
    offs.push(() => motion.removeEventListener?.('change', onMotion));
  }

  // Offline, the icon font never arrives: hide ligature words rather than show them.
  if (doc?.fonts?.check) {
    later(() => {
      try {
        if (!doc.fonts.check('20px "Material Symbols Outlined"')) {
          el.root.setAttribute('data-icons', 'off');
        }
      } catch {
        /* no font API: keep icons on */
      }
    }, ICON_FONT_CHECK_MS);
  }

  // ---- start -------------------------------------------------------------------------------

  measureLayout();
  setTab('orb');
  setView('orb');
  renderMapToggle();
  applyRegions();
  applyViewport();
  renderService();
  renderFooter();
  renderCaption();
  renderSpine();
  later(() => {
    connecting = false;
    renderService();
  }, LOADING_AFTER_MS);
  try {
    store.start?.();
  } catch (err) {
    globalThis.console?.error?.(err);
  }
  checkAnalyst();
  renderWargame();
  // At boot (WG §3.5): a key kept by this tab, else one claim POST.
  claimKey();

  function setMode(next, opts = {}) {
    if (next === 'orb') mode.backToConsole();
    else if (next === 'map') openTheaterMap();
    else if (
      (next === 'tracking' || next === 'entering_tracking') &&
      opts?.vehicle
    ) {
      mode.requestTrack(opts.vehicle, {
        source: opts.source || 'operator',
        reason: opts.reason,
      });
    }
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    for (const id of [...timers]) time.clearTimeout(id);
    timers.clear();
    for (const off of offs.splice(0)) {
      try {
        off?.();
      } catch {
        /* keep tearing down */
      }
    }
    ro?.disconnect?.();
    for (const panel of [
      analyst,
      inspector,
      search,
      situation,
      listView,
      settingsSheet,
      readView,
      strip,
    ]) {
      try {
        panel?.destroy?.();
      } catch (err) {
        globalThis.console?.error?.(err);
      }
    }
    mode.destroy();
    try {
      orb.destroy?.();
    } catch (err) {
      globalThis.console?.error?.(err);
    }
    store.stop?.();
    chat?.close?.();
    if (mode.state !== 'orb') {
      port.exit();
      port.exitOverview();
      port.setMapVisible(false);
      port.setViewportInset({ right: 0 });
    }
    el.mapDock.destroy();
    port.destroy();
    el.root.remove?.();
  }

  return { setMode, destroy, ctx, elements: el };
}

// ---------------------------------------------------------------------------
// Shell markup
// ---------------------------------------------------------------------------

function buildShell(mac, dockHooks = {}, stripHooks = {}) {
  // The simulated wargame's session strip (WG §5.3.2): hidden, and building
  // nothing, until a session is active. The shell fills in what End does.
  const wargameStrip = createWargameStrip({
    endWargame: () => stripHooks.end?.(),
    onEnded: (info) => stripHooks.onEnded?.(info),
    announce: (text, politeness) => stripHooks.announce?.(text, politeness),
    now: () => stripHooks.now?.() ?? Date.now(),
  });
  const livePolite = h('div', {
    class: 'ic-live',
    role: 'status',
    'aria-live': 'polite',
    'aria-atomic': 'true',
  });
  const liveAssertive = h('div', {
    class: 'ic-live',
    role: 'alert',
    'aria-live': 'assertive',
    'aria-atomic': 'true',
  });
  const skipSearch = h(
    'button',
    { type: 'button', class: 'ic-skip' },
    COPY.skipSearch,
  );
  const skipAnalyst = h(
    'button',
    { type: 'button', class: 'ic-skip' },
    COPY.skipAnalyst,
  );
  const skips = h('div', { class: 'ic-skips' }, skipSearch, skipAnalyst);

  // Narrow bar: theater, lead fuel vs BINGO, critical count, tabs, sticky banners.
  const narrowTheater = h('p', { class: 'ic-narrowbar__theater' });
  const narrowFleet = h('div', { class: 'ic-narrowbar__fleet' });
  // The badge is a bare count for the eye; the tab's name gets the words.
  const tabBadge = () =>
    h('span', { class: 'ic-tab__badge', 'aria-hidden': 'true', hidden: true });
  const tabNote = () => h('span', { class: 'ic-vh' });
  const tabOrbBadge = tabBadge();
  const tabAnalystBadge = tabBadge();
  const tabOrbNote = tabNote();
  const tabAnalystNote = tabNote();
  const TAB_PANEL = {
    orb: 'ic-region-stage',
    analyst: 'ic-region-analyst',
    situation: 'ic-region-rail',
  };
  const tab = (key, label, badge, note) =>
    h(
      'button',
      {
        type: 'button',
        class: 'ic-tab',
        role: 'tab',
        id: `ic-tab-${key}`,
        'aria-selected': key === 'orb' ? 'true' : 'false',
        'aria-controls': TAB_PANEL[key],
        'data-tab': key,
      },
      h('span', { class: 'ic-tab__label' }, label),
      note,
      badge,
    );
  const tabs = {
    orb: tab('orb', COPY.orb, tabOrbBadge, tabOrbNote),
    analyst: tab('analyst', COPY.analyst, tabAnalystBadge, tabAnalystNote),
    situation: tab('situation', COPY.rail, null, null),
  };
  const tabList = h(
    'div',
    { class: 'ic-tabs', role: 'tablist', 'aria-label': COPY.tabs },
    tabs.orb,
    tabs.analyst,
    tabs.situation,
  );
  const narrowBanner = h('div', { class: 'ic-bannerhost', hidden: true });
  const narrowApproval = h('div', {
    class: 'ic-narrowbar__approval',
    hidden: true,
  });
  const narrowBar = h(
    'header',
    { class: 'ic-narrowbar', hidden: true },
    h('div', { class: 'ic-narrowbar__row' }, narrowTheater),
    narrowFleet,
    tabList,
    narrowBanner,
    narrowApproval,
  );

  // Rail.
  const railHost = h('div', { class: 'ic-railhost', tabindex: '-1' });
  const rail = h(
    'div',
    { class: 'ic-col ic-col--rail', id: 'ic-region-rail' },
    railHost,
  );

  // Stage.
  const stageBanner = h('div', { class: 'ic-bannerhost', hidden: true });
  const searchHost = h('div', { class: 'ic-searchhost' });
  const viewOrb = h(
    'button',
    { type: 'button', class: 'ic-viewtoggle__btn', 'aria-pressed': 'true' },
    iconEl(ICON.orb),
    h('span', { class: 'ic-viewtoggle__label' }, COPY.orb),
  );
  const viewList = h(
    'button',
    { type: 'button', class: 'ic-viewtoggle__btn', 'aria-pressed': 'false' },
    iconEl(ICON.list),
    h('span', { class: 'ic-viewtoggle__label' }, COPY.list),
  );
  // Map is a mode, not a view: it opens the map overview (WG spec §4.2.7).
  const viewMap = h(
    'button',
    {
      type: 'button',
      class: 'ic-viewtoggle__btn',
      'aria-pressed': 'false',
      'aria-keyshortcuts': 'M',
      'data-view': 'map',
    },
    iconEl(ICON.map),
    h('span', { class: 'ic-viewtoggle__label' }, COPY.map),
  );
  const viewToggle = h(
    'div',
    { class: 'ic-viewtoggle', role: 'group', 'aria-label': COPY.views },
    viewOrb,
    viewList,
    viewMap,
  );
  const searchBand = h(
    'div',
    { class: 'ic-searchband' },
    searchHost,
    viewToggle,
  );
  const status = h('div', { class: 'ic-status', role: 'status', hidden: true });
  const canvas = h('canvas', {
    class: 'ic-canvas',
    'aria-hidden': 'true',
    tabindex: '-1',
  });
  const orbA11y = h('div', { class: 'ic-orba11y' });
  const orbWrap = h('div', { class: 'ic-orbwrap' }, canvas, orbA11y);
  const listHost = h('div', { class: 'ic-listhost', hidden: true });
  // The read view (WG §5.3.11) takes the orb's place in the stage.
  const readHost = h('div', { class: 'ic-readhost', hidden: true });
  const caption = h('div', {
    class: 'ic-caption',
    role: 'status',
    hidden: true,
  });
  const plate = h('div', { class: 'ic-plate', tabindex: '-1' });
  const footer = h('div', { class: 'ic-footer' });
  const stageBottom = h(
    'div',
    { class: 'ic-stage__bottom' },
    caption,
    plate,
    footer,
  );
  const stage = h(
    'main',
    {
      class: 'ic-stage',
      id: 'ic-region-stage',
      'aria-label': COPY.stage,
      tabindex: '-1',
    },
    stageBanner,
    searchBand,
    status,
    orbWrap,
    listHost,
    readHost,
    stageBottom,
  );
  const main = h('div', { class: 'ic-main' }, rail, stage);

  // Analyst column; in tracking it is the dock (same element, same width).
  const dockMini = h('span', { class: 'ic-dock__mini', 'aria-hidden': 'true' });
  const dockBadge = h('span', { class: 'ic-dock__badge', hidden: true });
  const dockBack = h(
    'button',
    { type: 'button', class: 'ic-dock__back', 'aria-keyshortcuts': 'Escape' },
    h('span', { class: 'ic-dock__miniwrap' }, dockMini, dockBadge),
    h('span', { class: 'ic-dock__backlabel' }, COPY.back),
    h('kbd', { class: 'ic-dock__kbd', 'aria-hidden': 'true' }, COPY.esc),
  );
  const dockLine = h('p', { class: 'ic-dock__line' });
  const dockFuel = h('div', { class: 'ic-dock__fuel' });
  const dockAbortLabel = h('span', { class: 'ic-btn__label' });
  const dockAbort = h(
    'button',
    { type: 'button', class: 'ic-btn', 'data-variant': 'danger' },
    iconEl(ICON.abort),
    dockAbortLabel,
  );
  const dockCollapse = h(
    'button',
    {
      type: 'button',
      class: 'ic-btn',
      'data-variant': 'icon',
      'aria-label': COPY.collapseDock,
      title: `${COPY.collapseDock} (${mac ? '⌘\\' : 'Ctrl+\\'})`,
      'aria-expanded': 'true',
      'aria-keyshortcuts': mac ? 'Meta+\\' : 'Control+\\',
    },
    iconEl(ICON.dockClose),
  );
  const dock = h(
    'section',
    { class: 'ic-dock', 'aria-label': COPY.dock, hidden: true },
    dockBack,
    dockLine,
    dockFuel,
    h('div', { class: 'ic-dock__actions' }, dockAbort, dockCollapse),
  );
  // The map overview's dock (mapDock.js) and the sheet a map pick opens the
  // inspector in; the shell fills in what the dock's buttons do.
  const mapDock = createMapDock({
    onBack: () => dockHooks.onBack?.(),
    onTrack: (v) => dockHooks.onTrack?.(v),
    onSites: (on) => dockHooks.onSites?.(on),
    onWargame: (kinds) => dockHooks.onWargame?.(kinds),
  });
  const mapSheet = h('div', { class: 'ic-mapsheet', hidden: true });
  const analystBody = h('div', { class: 'ic-analyst__body', tabindex: '-1' });
  const spine = h(
    'button',
    {
      type: 'button',
      class: 'ic-spine',
      'aria-label': `${COPY.analyst}: ${COPY.notAvailable}`,
      'aria-expanded': 'false',
      'aria-haspopup': 'dialog',
      hidden: true,
    },
    iconEl(ICON.analyst),
  );
  const spinePop = h('div', {
    class: 'ic-popover ic-spinepop',
    role: 'dialog',
    'aria-label': COPY.analyst,
    hidden: true,
  });
  const analyst = h(
    'aside',
    {
      class: 'ic-analyst',
      id: 'ic-region-analyst',
      'aria-label': COPY.analyst,
    },
    dock,
    mapDock.element,
    analystBody,
    spine,
    spinePop,
    mapSheet,
  );

  // Tracking-only chrome over the map region.
  const mapBanner = h('div', {
    class: 'ic-bannerhost ic-mapbanner',
    hidden: true,
  });
  const dockTabBadge = h('span', { class: 'ic-docktab__badge', hidden: true });
  const dockTabFuel = h('span', { class: 'ic-docktab__fuel' });
  const dockTab = h(
    'button',
    {
      type: 'button',
      class: 'ic-docktab',
      'aria-label': COPY.expandDock,
      title: `${COPY.expandDock} (${mac ? '⌘\\' : 'Ctrl+\\'})`,
      hidden: true,
    },
    iconEl(ICON.analyst),
    dockTabBadge,
    dockTabFuel,
  );
  const backFloat = h(
    'button',
    { type: 'button', class: 'ic-backfloat', hidden: true },
    iconEl(ICON.back),
    h('span', { class: 'ic-btn__label' }, COPY.back),
  );
  const toasts = h('div', { class: 'ic-toasts', hidden: true });

  // Shortcut sheet (?).
  const sheetClose = h(
    'button',
    {
      type: 'button',
      class: 'ic-btn',
      'data-variant': 'icon',
      'aria-label': COPY.close,
    },
    iconEl(ICON.close),
  );
  const cmd = mac ? '⌘' : 'Ctrl+';
  const rows = [
    [`${cmd}K`, 'Search the picture'],
    ['/', 'Search, outside text fields'],
    [`${cmd}I`, 'Go to the composer'],
    [`${cmd}.`, 'Stop the analyst'],
    [`${cmd},`, COPY.analystSettings],
    ['F6, Shift+F6', 'Move between regions'],
    [mac ? '⌥A' : 'Alt+A', 'Oldest approval waiting'],
    ['Esc', 'Close the innermost layer'],
    [`${cmd}\\`, 'Collapse or expand the dock while tracking'],
    ['↑ ↓ ← →', 'Step through entities, rotate the orb'],
    ['+ −', 'Zoom the orb'],
    ['Enter', 'Inspect the entity'],
    ['L', 'List view'],
    ['M', 'Map view'],
    ['Space', 'Pause or resume rotation'],
  ];
  const sheet = h(
    'div',
    {
      class: 'ic-sheet',
      role: 'dialog',
      'aria-modal': 'false',
      'aria-labelledby': 'ic-sheet-title',
      hidden: true,
    },
    h(
      'div',
      { class: 'ic-sheet__head' },
      iconEl(ICON.keyboard),
      h(
        'h2',
        { class: 'ic-sheet__title', id: 'ic-sheet-title' },
        COPY.shortcuts,
      ),
      sheetClose,
    ),
    h(
      'dl',
      { class: 'ic-sheet__list' },
      ...rows.flatMap(([k, d]) => [
        h('dt', { class: 'ic-sheet__key' }, h('kbd', { class: 'ic-kbd' }, k)),
        h('dd', { class: 'ic-sheet__desc' }, d),
      ]),
    ),
  );

  const root = h(
    'div',
    {
      class: 'ic-root',
      'data-mode': 'orb',
      'data-layout': 'wide',
      'data-analyst': 'on',
      'data-tab': 'orb',
      'data-dock': 'open',
    },
    skips,
    livePolite,
    liveAssertive,
    wargameStrip.element,
    narrowBar,
    main,
    analyst,
    mapBanner,
    toasts,
    dockTab,
    backFloat,
    sheet,
  );
  return {
    root,
    skipSearch,
    skipAnalyst,
    livePolite,
    liveAssertive,
    narrowBar,
    narrowTheater,
    narrowFleet,
    narrowBanner,
    narrowApproval,
    tabs,
    tabOrbBadge,
    tabAnalystBadge,
    tabOrbNote,
    tabAnalystNote,
    main,
    rail,
    railHost,
    stage,
    stageBanner,
    searchHost,
    viewOrb,
    viewList,
    viewMap,
    status,
    canvas,
    orbA11y,
    orbWrap,
    listHost,
    readHost,
    wargameStrip,
    caption,
    plate,
    footer,
    stageBottom,
    analyst,
    analystBody,
    dock,
    mapDock,
    mapSheet,
    dockBack,
    dockMini,
    dockBadge,
    dockLine,
    dockFuel,
    dockAbort,
    dockAbortLabel,
    dockCollapse,
    spine,
    spinePop,
    mapBanner,
    dockTab,
    dockTabBadge,
    dockTabFuel,
    backFloat,
    toasts,
    sheet,
    sheetClose,
  };
}
