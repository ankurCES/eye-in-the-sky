/**
 * Search and ask bar (UX spec §5): ranked matches over the intel graph, live
 * orb filtering, type filters with counts, and a final "Ask the analyst" row.
 *
 * Ranking (pure, exported as rankNodes): exact id > label prefix > label word
 * start or every token at a word start > partial id > subtitle > attributes
 * (including "Near {place}" through `near` edges). Ties: critical first, then
 * salience, then recency.
 *
 * Wiring: live matches go to `ctx.orb.filter(pred)` and the bus as
 * `search:filter {ids, query, source:"search"}`; Enter selects and emits
 * `inspect {id}`; asking emits `ask {text, focused_ids}` with the top ten
 * matches. Global ⌘K / `/` focus is index.js's job: it calls `open()`.
 */
import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { SITE_CATEGORIES, siteCategoryKey } from './orb/glyphPaths.js';
import { siteCountLabel, siteWord, sitePlural } from './orb/placeText.js';
import { cleanSubtitle, typeLabel } from './orb/text.js';
import { siteKeyTag } from './inspectorPlaces.js';
import {
  CONSEQUENCE_WORD,
  ENGAGEMENT_PHASE_WORD,
  FORCE_STATE_WORD,
  OUTCOME_WORD,
  SIDE_WORD,
  isWargameType,
  wargameActive,
} from './railWargame.js';
import {
  ICON,
  TYPE_WORD,
  bareId,
  displayLabel,
  feedSummary,
  glyph,
  icon,
  isPanelType,
  nodeGlyph,
  num,
  registerTag,
  segments,
  statusWord,
  toneOf,
  zulu,
} from './situation.js';

// ---------------------------------------------------------------------------
// Pure ranking
// ---------------------------------------------------------------------------

/** Type filter chips (§5). "All" also counts equipment classes and feeds. */
export const TYPE_FILTERS = Object.freeze([
  Object.freeze({ key: 'all', label: 'All', types: null }),
  Object.freeze({
    key: 'contacts',
    label: 'Contacts',
    types: ['track', 'unit'],
  }),
  Object.freeze({ key: 'vehicles', label: 'Vehicles', types: ['vehicle'] }),
  Object.freeze({ key: 'missions', label: 'Missions', types: ['mission'] }),
  // WG §5.3 (M14a): the simulated wargame's forces and engagements. Their
  // chips show only in a session, or when they match.
  Object.freeze({ key: 'forces', label: 'Forces', types: ['force'] }),
  Object.freeze({
    key: 'engagements',
    label: 'Engagements',
    types: ['engagement'],
  }),
  // WG §4.2.6: mapped sites are places.
  Object.freeze({
    key: 'places',
    label: 'Places',
    types: ['theater', 'poi', 'site'],
  }),
  Object.freeze({ key: 'reports', label: 'Reports', types: ['report'] }),
  Object.freeze({ key: 'alarms', label: 'Alarms', types: ['alarm'] }),
]);

/** Rank tiers, highest first. */
export const TIER = Object.freeze({
  exactId: 6,
  labelPrefix: 5,
  labelWord: 4,
  partialId: 3,
  subtitle: 2,
  attribute: 1,
});

const QUESTION_WORDS = new Set([
  'who',
  'what',
  'which',
  'where',
  'when',
  'why',
  'how',
  'can',
  'should',
  'is',
  'are',
  'plan',
  'fly',
  'scan',
  'show',
  'compare',
  'write',
  'summarize',
  // WG §4.2.6: taskings that move the theater or plan over a place.
  'set',
  'go',
  'recce',
  'move',
  'change',
]);

/**
 * Attribute key -> match reason, by node type (WG §4.2.6): `'*'` holds the
 * reasons every type shares, and a type's own table wins over it (a site's
 * `category` reads "Category", a unit's "Equipment"). Keys not listed read
 * "Details".
 */
export const ATTR_REASON = Object.freeze({
  '*': Object.freeze({
    ob_class: 'Equipment',
    category: 'Equipment',
    vehicle: 'Vehicle',
    mission: 'Mission',
    mission_id: 'ID match',
    kind: 'Kind',
    phase: 'Phase',
    severity: 'Severity',
    threat: 'Threat',
    confidence: 'Confidence',
    theater: 'Theater',
    place: 'Place',
    format: 'Format',
    link: 'Link',
  }),
  site: Object.freeze({
    category: 'Category',
    subtype: 'Category',
    tags: 'Tags',
  }),
  // WG §5.3: a force matches on its side, kind and state; an engagement on
  // its outcome, phase and the designators involved.
  force: Object.freeze({
    side: 'Side',
    state: 'State',
    kind_label: 'Kind',
    wg_class: 'Kind',
  }),
  engagement: Object.freeze({
    outcome: 'Outcome',
    phase: 'Phase',
    consequence: 'Outcome',
    kind: 'Kind',
    attacker_label: 'Attacker',
    target_label: 'Target',
  }),
  vector: Object.freeze({ side: 'Side', kind: 'Kind', alt_band: 'Kind' }),
});

/** The wargame keys a query may match, with the console's words for their values. */
const WARGAME_MATCH = Object.freeze({
  force: Object.freeze({
    side: SIDE_WORD,
    state: FORCE_STATE_WORD,
    kind_label: null,
    wg_class: null,
  }),
  engagement: Object.freeze({
    outcome: OUTCOME_WORD,
    phase: ENGAGEMENT_PHASE_WORD,
    consequence: CONSEQUENCE_WORD,
    kind: null,
    attacker_label: null,
    target_label: null,
  }),
  vector: Object.freeze({ side: SIDE_WORD, kind: null, alt_band: null }),
});

/** The match reason for an attribute key of a node type. */
export function attrReason(type, key) {
  const own =
    typeof type === 'string' && Object.hasOwn(ATTR_REASON, type)
      ? ATTR_REASON[type]
      : null;
  if (own && Object.hasOwn(own, key)) return own[key];
  return Object.hasOwn(ATTR_REASON['*'], key)
    ? ATTR_REASON['*'][key]
    : 'Details';
}

/** Site attrs that are plumbing, never a reason to match (ids, counts, times). */
const SITE_QUIET_ATTRS = new Set([
  'tags_total',
  'fetched_at_ms',
  'source',
  'register',
]);

const CONFIDENCE_WORDS = new Set([
  'confirmed',
  'probable',
  'possible',
  'unrated',
]);

/** Filter chips that belong to the simulated wargame. */
const WARGAME_FILTERS = new Set(['forces', 'engagements']);

const ASK_LIMIT = 10;
const MAX_ROWS = 8;

/** Lowercase, collapse whitespace, trim. */
export function normalize(text) {
  return String(text ?? '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

/** Whether a query reads as a question or a tasking (§5). */
export function isQuestion(query) {
  const q = normalize(query);
  if (!q) return false;
  if (q.endsWith('?')) return true;
  const first = q.split(' ')[0].replace(/[^a-z]/g, '');
  return QUESTION_WORDS.has(first);
}

/** Which filter chip a node type belongs to (null = only "All"). */
export function filterKeyOf(type) {
  for (const f of TYPE_FILTERS) {
    if (f.types?.includes(type)) return f.key;
  }
  return null;
}

const isWordChar = (ch) => /[a-z0-9]/.test(ch);

/** Index of `q` in `hay` at a word start, or -1. */
function wordStartIndex(hay, q) {
  let i = hay.indexOf(q);
  while (i >= 0) {
    if (i === 0 || !isWordChar(hay[i - 1])) return i;
    i = hay.indexOf(q, i + 1);
  }
  return -1;
}

/** Highlight ranges only when lowercasing kept the label's length. */
function safeRanges(label, lower, ranges) {
  return String(label ?? '').length === lower.length ? ranges : [];
}

function attrEntries(node) {
  const attrs = node?.attrs || {};
  const site = node?.type === 'site';
  const out = [];
  // Wargame nodes match only their words (never seeds, draws or ids).
  if (isWargameType(node?.type)) {
    for (const [key, words] of Object.entries(WARGAME_MATCH[node.type])) {
      const value = attrs[key];
      if (typeof value !== 'string' || !value) continue;
      out.push([key, value]);
      if (words && Object.hasOwn(words, value)) out.push([key, words[value]]);
    }
    return out;
  }
  for (const [key, value] of Object.entries(attrs)) {
    if (site && SITE_QUIET_ATTRS.has(key)) continue;
    if (typeof value === 'string' || typeof value === 'number') {
      out.push([key, String(value)]);
    }
  }
  if (site) {
    // A site matches its category words ("military" finds "Military
    // site") and its mapped tags ("OIFM" finds icao=OIFM).
    out.push(
      ['category', siteWord(attrs.category)],
      ['category', sitePlural(attrs.category)],
    );
    const tags = attrs.tags && typeof attrs.tags === 'object' ? attrs.tags : {};
    for (const [k, v] of Object.entries(tags)) {
      if (typeof v === 'string' || typeof v === 'number')
        out.push(['tags', `${k}=${v}`]);
    }
  }
  return out;
}

/**
 * Match one node against a normalized query.
 * @returns {{tier:number, reason:string, ranges:Array<[number,number]>}|null}
 */
export function matchNode(node, query, extra = {}) {
  const q = normalize(query);
  if (!q || !node?.id) return null;
  const tokens = q.split(' ').filter(Boolean);
  const id = normalize(node.id);
  const bare = normalize(bareId(node.id));
  const dups = Array.isArray(node.attrs?.duplicates)
    ? node.attrs.duplicates.map((d) => normalize(d))
    : [];

  if (
    id === q ||
    bare === q ||
    dups.includes(q) ||
    dups.some((d) => normalize(bareId(d)) === q)
  ) {
    return { tier: TIER.exactId, reason: 'ID match', ranges: [] };
  }

  const rawLabel = displayLabel(node);
  const label = rawLabel.toLowerCase();
  if (label.startsWith(q)) {
    return {
      tier: TIER.labelPrefix,
      reason: 'Label',
      ranges: safeRanges(rawLabel, label, [[0, q.length]]),
    };
  }
  const at = wordStartIndex(label, q);
  if (at >= 0) {
    return {
      tier: TIER.labelWord,
      reason: 'Label',
      ranges: safeRanges(rawLabel, label, [[at, at + q.length]]),
    };
  }
  if (tokens.length > 1) {
    const hits = tokens.map((t) => [wordStartIndex(label, t), t.length]);
    if (hits.every(([i]) => i >= 0)) {
      return {
        tier: TIER.labelWord,
        reason: 'Label',
        ranges: safeRanges(
          rawLabel,
          label,
          hits.map(([i, n]) => [i, i + n]),
        ),
      };
    }
  }
  if (q.length < 2) return null;

  // A site id repeats its theater's id (`sit:{theater}:{osm}`): only the
  // OSM part is a partial-id match, or every site would match the place.
  const partial =
    node.type === 'site'
      ? normalize(node.id.split(':').slice(2).join(':'))
      : id;
  if (partial.includes(q) || dups.some((d) => d.includes(q))) {
    return { tier: TIER.partialId, reason: 'Partial ID', ranges: [] };
  }

  const subtitle = String(node.subtitle ?? '');
  if (subtitle.toLowerCase().includes(q)) {
    const segs = subtitle.split(' · ');
    const first = normalize(segs[0]);
    const equipment =
      node.type === 'track' &&
      segs.length > 1 &&
      !CONFIDENCE_WORDS.has(first) &&
      first.includes(q);
    return {
      tier: TIER.subtitle,
      reason: equipment ? 'Equipment' : 'Subtitle',
      ranges: [],
    };
  }

  for (const [key, value] of attrEntries(node)) {
    if (normalize(value).includes(q)) {
      return {
        tier: TIER.attribute,
        reason: attrReason(node.type, key),
        ranges: [],
      };
    }
  }

  const near = extra.nearReason?.get(node.id);
  if (near) return { tier: TIER.attribute, reason: near, ranges: [] };
  return null;
}

/** Contacts near places whose label matches the query: id -> "Near {place}". */
function nearReasons(graph, q) {
  const out = new Map();
  if (q.length < 2) return out;
  const places = new Map();
  for (const n of graph?.nodes || []) {
    if (n?.type !== 'poi') continue;
    const label = String(n.label ?? '').toLowerCase();
    if (label.startsWith(q) || wordStartIndex(label, q) >= 0)
      places.set(n.id, n.label);
  }
  if (!places.size) return out;
  for (const e of graph?.edges || []) {
    if (e?.kind !== 'near') continue;
    const place = places.get(e.b) ?? places.get(e.a);
    const other = places.has(e.b) ? e.a : places.has(e.a) ? e.b : null;
    if (place && other && !out.has(other)) out.set(other, `Near ${place}`);
  }
  return out;
}

/** Compare two matches: tier, critical first, salience, recency, label. */
function compareMatches(a, b) {
  if (b.tier !== a.tier) return b.tier - a.tier;
  const ca = a.node.status === 'critical' ? 1 : 0;
  const cb = b.node.status === 'critical' ? 1 : 0;
  if (cb !== ca) return cb - ca;
  const sa = num(a.node.salience) ?? 0;
  const sb = num(b.node.salience) ?? 0;
  if (sb !== sa) return sb - sa;
  const ta = num(a.node.ts_ms) ?? -Infinity;
  const tb = num(b.node.ts_ms) ?? -Infinity;
  if (tb !== ta) return tb > ta ? 1 : -1;
  return String(a.node.label ?? a.id).localeCompare(
    String(b.node.label ?? b.id),
  );
}

/**
 * Rank every graph node against a query. Pure.
 * @param {object} graph intel graph ({nodes, edges})
 * @param {string} query raw query
 * @returns {Array<{id:string, node:object, tier:number, reason:string, ranges:Array}>}
 */
export function rankNodes(graph, query) {
  const q = normalize(query);
  if (!q || !Array.isArray(graph?.nodes)) return [];
  const extra = { nearReason: nearReasons(graph, q) };
  const out = [];
  for (const node of graph.nodes) {
    const m = matchNode(node, q, extra);
    if (m) out.push({ id: node.id, node, ...m });
  }
  return out.sort(compareMatches);
}

/** Counts per filter chip for a ranked list. */
export function filterCounts(matches) {
  const counts = Object.fromEntries(TYPE_FILTERS.map((f) => [f.key, 0]));
  for (const m of matches) {
    counts.all += 1;
    const key = filterKeyOf(m.node?.type);
    if (key) counts[key] += 1;
  }
  return counts;
}

/**
 * Matches kept by a filter chip, and within Places by a site category chip
 * (`siteCategory`, WG §4.2.6): then only that category's sites stay.
 */
export function applyTypeFilter(matches, key, siteCategory = null) {
  const f = TYPE_FILTERS.find((x) => x.key === key);
  if (!f?.types) return matches;
  const kept = matches.filter((m) => f.types.includes(m.node?.type));
  if (key !== 'places' || !siteCategory) return kept;
  return kept.filter(
    (m) =>
      m.node?.type === 'site' &&
      siteCategoryKey(m.node.attrs?.category) === siteCategory,
  );
}

/**
 * Site matches per category, in the copy deck's order: `[[category, n]]`
 * for the Places category chips ("Airfields 3"). Unknown categories count
 * as `other`.
 */
export function siteCategoryCounts(matches) {
  const counts = new Map();
  for (const m of matches || []) {
    if (m?.node?.type !== 'site') continue;
    const key = siteCategoryKey(m.node.attrs?.category);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return SITE_CATEGORIES.filter((c) => counts.has(c)).map((c) => [
    c,
    counts.get(c),
  ]);
}

/** The words a search row states for a node type: "Contact", "Unrecognised (force)". */
export function typeWordOf(type) {
  return isPanelType(type) ? TYPE_WORD[type] || 'Entity' : typeLabel(type);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const ANALYST_REASON = Object.freeze({
  sdk_missing: "The analyst isn't installed in this build.",
  cli_missing:
    "The analyst needs the Claude command-line tool, and it wasn't found.",
  disabled: 'The analyst is turned off for this launch.',
  service_unavailable: "The analyst didn't start in this app.",
  auth: "The analyst isn't signed in.",
  error: "The analyst isn't signed in.",
});

/**
 * Where a match sits relative to the active theater (graph v1.1
 * `attrs.out_of_theater` / `outside_ao`), or '' when inside it.
 */
export function outsideWords(node) {
  const a = node?.attrs || {};
  if (a.out_of_theater === true) return 'Outside this theater';
  if (a.outside_ao === true) return 'Just outside the AO';
  return '';
}

/** Why the ask row is disabled, in copy-deck words. */
export function analystReasonText(reason) {
  return ANALYST_REASON[reason] || "The analyst isn't available right now.";
}

function isMacLike() {
  const p =
    globalThis.navigator?.platform || globalThis.navigator?.userAgent || '';
  return /Mac|iPhone|iPad/.test(String(p));
}

let searchSeq = 0;

/**
 * Mount the search and ask bar.
 * @param {object} host the stage search band
 * @param {object} ctx console ctx ({store, orb, bus})
 * @param {{debounceMs?:number, now?:()=>number}} [opts]
 * @returns {{open():void, close():void, destroy():void, element:object, input:object, query():string, results():Array}}
 */
export function createSearch(host, ctx, opts = {}) {
  const bus = ctx?.bus;
  const store = ctx?.store;
  const now = opts.now || (() => Date.now());
  const debounceMs = opts.debounceMs ?? 80;
  const n = ++searchSeq;
  const listId = `ic-search-list-${n}`;
  const optionId = (i) => `ic-search-opt-${n}-${i}`;
  const mac = isMacLike();
  const askHint = mac ? '⌘Enter' : 'Ctrl+Enter';

  let query = '';
  let filterKey = 'all';
  /** A site category chip under Places (WG §4.2.6), or null. */
  let siteCat = null;
  let matches = [];
  let options = [];
  let active = -1;
  let isOpen = false;
  let persisted = null;
  let analyst = { available: true, reason: null };
  let lastFilterSig = null;
  let timer = null;
  let destroyed = false;
  /** True while render() swaps the popover's children (see focusout). */
  let rendering = false;
  /** The last count read out, so a poll with the same answer stays quiet. */
  let lastCountMsg = null;
  const offs = [];

  const input = h('input', {
    class: 'ic-search__input',
    type: 'text',
    role: 'combobox',
    autocomplete: 'off',
    spellcheck: 'false',
    'aria-label': 'Search the picture, or ask a question',
    'aria-expanded': 'false',
    'aria-controls': listId,
    'aria-autocomplete': 'list',
    placeholder: 'Search the picture, or ask a question',
  });
  const kbd = h(
    'kbd',
    { class: 'ic-search__kbd', 'aria-hidden': 'true' },
    mac ? '⌘K' : 'Ctrl+K',
  );
  const bar = h(
    'div',
    { class: 'ic-search__bar' },
    icon(ICON.search),
    input,
    kbd,
  );
  const pill = h('div', { class: 'ic-search__pill', hidden: true });
  const chips = h('div', {
    class: 'ic-search__chips',
    role: 'group',
    'aria-label': 'Filter by type',
  });
  const siteChips = h('div', {
    class: 'ic-search__chips ic-search__chips--sites',
    role: 'group',
    'aria-label': 'Filter sites by category',
    hidden: true,
  });
  const caption = h('p', { class: 'ic-search__caption' });
  const list = h('ul', {
    class: 'ic-search__list',
    role: 'listbox',
    id: listId,
    'aria-label': 'Matches',
  });
  const foot = h('div', { class: 'ic-search__foot' });
  const live = h('p', {
    class: 'ic-kit-vh',
    role: 'status',
    'aria-live': 'polite',
  });
  const pop = h(
    'div',
    { class: 'ic-search__pop', hidden: true },
    chips,
    siteChips,
    caption,
    list,
    foot,
  );
  const root = h(
    'div',
    { class: 'ic-search', role: 'search' },
    bar,
    pill,
    pop,
    live,
  );

  const state = () => store?.get?.() || {};
  const emit = (event, payload) => bus?.emit?.(event, payload);

  // ---- orb + bus -------------------------------------------------------------

  const sigOf = (ids, q) =>
    ids ? `${q}\u0000${[...ids].join('\u0001')}` : null;

  function applyFilter(ids, q) {
    const sig = sigOf(ids, q);
    if (sig === lastFilterSig) return;
    lastFilterSig = sig;
    const set = ids ? new Set(ids) : null;
    // `ranked` (search order) tells the orb which matches get its labels.
    ctx?.orb?.filter?.(
      set
        ? (node) => set.has(typeof node === 'string' ? node : node?.id)
        : null,
      set ? { ranked: [...ids] } : undefined,
    );
    emit('search:filter', {
      ids: ids ? [...ids] : null,
      query: q,
      source: 'search',
    });
  }

  function restoreBaseFilter() {
    if (persisted) applyFilter(persisted.ids, persisted.query);
    else applyFilter(null, '');
  }

  function highlight(id) {
    ctx?.orb?.highlight?.(id ? [id] : [], { by: 'operator' });
  }

  // ---- model -----------------------------------------------------------------

  function recompute() {
    const st = state();
    matches = query ? rankNodes(st.graph, query) : [];
  }

  function filtered() {
    return applyTypeFilter(matches, filterKey, siteCat);
  }

  function buildOptions() {
    const shown = filtered().slice(0, MAX_ROWS);
    const opts2 = shown.map((m) => ({ kind: 'result', match: m }));
    if (query) {
      const askFirst = isQuestion(query) || filtered().length === 0;
      const ask = { kind: 'ask' };
      if (askFirst) opts2.unshift(ask);
      else opts2.push(ask);
    }
    return opts2;
  }

  function preselect() {
    if (!options.length) return -1;
    return 0;
  }

  // ---- rendering ---------------------------------------------------------------

  function renderPill() {
    // A poll rebuilds the pill; its focused clear must not drop focus to
    // <body> (keyboard users land here after "Show all").
    const el = globalThis.document?.activeElement;
    const hadFocus =
      Boolean(el) && typeof pill.contains === 'function' && pill.contains(el);
    if (!persisted) {
      setHidden(pill, true);
      replaceKids(pill, []);
      return;
    }
    const clear = h(
      'button',
      {
        type: 'button',
        class: 'ic-search__pill-clear',
        'aria-label': 'Clear filter',
        'data-key': 'search:pill-clear',
      },
      icon(ICON.close),
    );
    clear.addEventListener('click', () => clearPersisted());
    replaceKids(pill, [
      h(
        'span',
        { class: 'ic-search__pill-text' },
        `Filtered: ${persisted.query}, ${persisted.ids.length} shown`,
      ),
      clear,
    ]);
    setHidden(pill, false);
    if (hadFocus) clear.focus?.();
  }

  function renderChips(withCounts) {
    const counts = withCounts ? filterCounts(matches) : null;
    // Forces and Engagements show before a query only in a wargame (WG §5.3).
    const session = wargameActive(state().graph);
    const kids = [];
    for (const f of TYPE_FILTERS) {
      if (counts && f.key !== 'all' && !counts[f.key]) continue;
      if (!counts && WARGAME_FILTERS.has(f.key) && !session) continue;
      const b = h(
        'button',
        {
          type: 'button',
          class: 'ic-search__chip',
          'aria-pressed': filterKey === f.key ? 'true' : 'false',
          'data-filter': f.key,
        },
        counts ? `${f.label} ${counts[f.key]}` : f.label,
      );
      // Mouse: keep focus in the input (WebKit never focuses a clicked
      // button, so the input's focusout would close the list first).
      b.addEventListener('mousedown', (event) => event?.preventDefault?.());
      // render() puts focus back on the rebuilt chip with this data-filter.
      b.addEventListener('click', () => {
        filterKey = f.key;
        siteCat = null;
        update({ keepActive: false });
      });
      kids.push(b);
    }
    replaceKids(chips, kids);
  }

  /** Category chips under Places when sites match ("Airfields 3"). */
  function renderSiteChips(show) {
    const counts =
      show && filterKey === 'places' ? siteCategoryCounts(matches) : [];
    const kids = counts.map(([category, n]) => {
      const b = h(
        'button',
        {
          type: 'button',
          class: 'ic-search__chip ic-search__chip--site',
          'aria-pressed': siteCat === category ? 'true' : 'false',
          'data-filter': `site:${category}`,
        },
        siteCountLabel(category, n),
      );
      b.addEventListener('mousedown', (event) => event?.preventDefault?.());
      b.addEventListener('click', () => {
        siteCat = siteCat === category ? null : category;
        update({ keepActive: false });
      });
      return b;
    });
    replaceKids(siteChips, kids);
    setHidden(siteChips, !kids.length);
  }

  function labelWithHits(label, ranges) {
    const text = String(label ?? '');
    if (!ranges?.length) return [text];
    const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
    const out = [];
    let at = 0;
    for (const [start, end] of sorted) {
      if (start < at) continue;
      if (start > at) out.push(text.slice(at, start));
      out.push(
        h('strong', { class: 'ic-search__hit' }, text.slice(start, end)),
      );
      at = end;
    }
    if (at < text.length) out.push(text.slice(at));
    return out;
  }

  function resultRow(m, i) {
    const node = m.node;
    const feed = node.type === 'feed';
    // A feed's subtitle names server plumbing; its state is the useful part
    // (and already says Up/Down, so no second status word).
    const word = feed ? '' : statusWord(node);
    const where = outsideWords(node);
    const known = isPanelType(node.type);
    const site = node.type === 'site';
    const wargame = isWargameType(node.type);
    // WG §4.2.6: "Site  Airfield  Mapped, not verified  ICAO OIFM".
    const keyTag = site ? siteKeyTag(node) : '';
    const meta = h(
      'span',
      { class: 'ic-search__meta' },
      h(
        'span',
        {
          class: known
            ? 'ic-search__type'
            : 'ic-search__type ic-search__type--unrecognised',
        },
        typeWordOf(node.type),
      ),
      site
        ? h(
            'span',
            { class: 'ic-search__category' },
            siteWord(node.attrs?.category),
          )
        : feed
          ? h(
              'span',
              {
                class: 'ic-search__status',
                'data-status': node.status,
                'data-tone': toneOf(node.type, node.status, node),
              },
              feedSummary(node, now()),
            )
          : cleanSubtitle(node.subtitle)
            ? segments(cleanSubtitle(node.subtitle))
            : null,
      where
        ? h(
            'span',
            { class: 'ic-search__where', 'data-status': 'stale' },
            where,
          )
        : null,
      word
        ? h(
            'span',
            {
              class:
                word === 'Not assessed'
                  ? 'ic-kit-notassessed'
                  : 'ic-search__status',
              'data-status': node.status,
              'data-tone': toneOf(node.type, node.status, node),
            },
            word,
          )
        : null,
      // A force is set by the wargame, not seen by a sensor (WG §5.3.1).
      node.type === 'force' ? registerTag('scenario') : null,
      keyTag ? h('span', { class: 'ic-search__tag' }, keyTag) : null,
    );
    const li = h(
      'li',
      {
        class: 'ic-search__row',
        role: 'option',
        id: optionId(i),
        'aria-selected': i === active ? 'true' : 'false',
        'data-status': node.status || 'unknown',
        'data-tone': toneOf(node.type, node.status, node),
        'data-type': known ? node.type : 'unknown',
        'data-id': node.id,
        'data-kind': 'result',
      },
      wargame
        ? nodeGlyph(node, 16)
        : glyph(node.type, node.status, 16, node.attrs?.phase, {
            category: node.attrs?.category,
          }),
      h(
        'span',
        { class: 'ic-search__main' },
        h(
          'span',
          { class: 'ic-search__label' },
          ...labelWithHits(displayLabel(node) || node.id, m.ranges),
        ),
        meta,
      ),
      h(
        'span',
        { class: 'ic-search__aside' },
        h('span', { class: 'ic-search__reason' }, m.reason),
        h('span', { class: 'ic-search__id ic-kit-mono' }, node.id),
      ),
    );
    // Keep focus in the input so its focusout doesn't close the list before the click lands.
    li.addEventListener('mousedown', (event) => event?.preventDefault?.());
    li.addEventListener('click', () => openResult(m));
    li.addEventListener('mouseenter', () => {
      setActive(i, { announce: false });
      highlight(node.id);
    });
    li.addEventListener('mouseleave', () => highlight(null));
    return li;
  }

  function askRow(i) {
    const disabled = !analyst.available;
    const li = h(
      'li',
      {
        class: 'ic-search__row ic-search__ask',
        role: 'option',
        id: optionId(i),
        'aria-selected': i === active ? 'true' : 'false',
        'aria-disabled': disabled ? 'true' : undefined,
        'data-kind': 'ask',
      },
      icon(ICON.ask),
      h(
        'span',
        { class: 'ic-search__main' },
        h('span', { class: 'ic-search__label' }, `Ask the analyst: "${query}"`),
        disabled
          ? h(
              'span',
              { class: 'ic-search__meta ic-search__reason-off' },
              analystReasonText(analyst.reason),
            )
          : null,
      ),
      h(
        'span',
        { class: 'ic-search__aside' },
        h('kbd', { class: 'ic-search__kbd' }, askHint),
      ),
    );
    li.addEventListener('mousedown', (event) => event?.preventDefault?.());
    li.addEventListener('click', () => ask());
    return li;
  }

  function renderCaption(st) {
    const kids = [];
    let tone = 'hint';
    if (!st.graph) {
      kids.push("Search needs the intel picture, which hasn't loaded yet.");
      tone = 'warn';
    } else if (!query) {
      kids.push('Try a callsign, a track id, or a question.');
    } else {
      if (st.status === 'stale' || st.status === 'offline') {
        const at = num(st.lastLiveAt) ?? num(st.graph.generated_at_ms);
        kids.push(
          at != null
            ? `From the picture at ${zulu(at, { seconds: true })}, not live.`
            : 'From the last picture, not live.',
        );
        tone = 'stale';
      }
      if (!filtered().length) {
        if (kids.length) kids.push(' ');
        kids.push(`Nothing on the picture matches "${query}".`);
        tone = tone === 'stale' ? 'stale' : 'empty';
      }
    }
    replaceKids(caption, kids);
    caption.setAttribute('data-tone', tone);
    setHidden(caption, !kids.length);
  }

  function renderFoot(st) {
    const kids = [];
    const all = filtered();
    if (query && all.length) {
      const b = h(
        'button',
        {
          type: 'button',
          class: 'ic-kit-btn ic-kit-btn--link',
          'data-key': 'search:show-all',
        },
        `Show all ${all.length} on the orb`,
      );
      b.addEventListener('mousedown', (event) => event?.preventDefault?.());
      b.addEventListener('click', () => persist());
      kids.push(b);
    }
    if (
      query &&
      st.graph &&
      st.graph.scope !== 'all' &&
      typeof store?.setScope === 'function' &&
      !all.length
    ) {
      const b = h(
        'button',
        {
          type: 'button',
          class: 'ic-kit-btn ic-kit-btn--link',
          'data-key': 'search:all-theaters',
        },
        'Search all theaters',
      );
      b.addEventListener('mousedown', (event) => event?.preventDefault?.());
      b.addEventListener('click', () => {
        store.setScope('all');
        input.focus?.();
      });
      kids.push(b);
    }
    replaceKids(foot, kids);
    setHidden(foot, !kids.length);
  }

  /** Which popover control has focus (a chip or a footer link), by key. */
  function focusedControl() {
    const el = globalThis.document?.activeElement;
    if (!el || el === input || typeof pop.contains !== 'function') return null;
    if (!pop.contains(el)) return null;
    return {
      filter: el.getAttribute?.('data-filter') ?? null,
      key: el.getAttribute?.('data-key') ?? null,
    };
  }

  function findControl(root, { filter, key }) {
    const hit = (el) =>
      (filter != null && el.getAttribute?.('data-filter') === filter) ||
      (key != null && el.getAttribute?.('data-key') === key);
    const walk = (el) => {
      if (!el || typeof el !== 'object') return null;
      if (hit(el)) return el;
      for (const kid of el.children || []) {
        const found = walk(kid);
        if (found) return found;
      }
      return null;
    };
    return walk(root);
  }

  /** After a rebuild, focus goes to the same control, else the input. */
  function restoreControl(kept) {
    if (!kept) return;
    if (isOpen) {
      (findControl(pop, kept) || input).focus?.();
      return;
    }
    // Closed under the focus (Show all kept the filter): the pill's clear
    // is the nearest control. Focusing the input would reopen the list.
    if (persisted) findControl(pill, { key: 'search:pill-clear' })?.focus?.();
  }

  function render() {
    const kept = focusedControl();
    // Swapping children drops the focused chip, and Chrome fires focusout
    // (relatedTarget null) while it is still connected: not a real exit.
    rendering = true;
    try {
      const st = state();
      renderPill();
      input.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      setHidden(pop, !isOpen);
      if (!isOpen) {
        input.removeAttribute?.('aria-activedescendant');
        return;
      }
      renderChips(Boolean(query) && Boolean(st.graph));
      renderSiteChips(Boolean(query) && Boolean(st.graph));
      renderCaption(st);
      replaceKids(
        list,
        options.map((o, i) =>
          o.kind === 'ask' ? askRow(i) : resultRow(o.match, i),
        ),
      );
      setHidden(list, !options.length);
      renderFoot(st);
      if (active >= 0 && options[active])
        input.setAttribute('aria-activedescendant', optionId(active));
      else input.removeAttribute?.('aria-activedescendant');
    } finally {
      rendering = false;
      restoreControl(kept);
    }
  }

  function announce(text) {
    replaceKids(live, [text]);
  }

  /** Move the active option in place: no re-render, so hover never replaces the row under the pointer. */
  function setActive(i, { announce: say = true } = {}) {
    if (!options.length) {
      active = -1;
      return;
    }
    active = ((i % options.length) + options.length) % options.length;
    let index = 0;
    for (const li of list.children || []) {
      li.setAttribute?.('aria-selected', index === active ? 'true' : 'false');
      index += 1;
    }
    input.setAttribute('aria-activedescendant', optionId(active));
    const o = options[active];
    if (say && o?.kind === 'result') highlight(o.match.id);
  }

  // ---- flow ---------------------------------------------------------------------

  function update({ keepActive = false } = {}) {
    const previous = keepActive ? options[active] : null;
    recompute();
    // A category chip whose sites no longer match lets go.
    if (
      siteCat &&
      !siteCategoryCounts(matches).some(([category]) => category === siteCat)
    )
      siteCat = null;
    options = buildOptions();
    if (previous) {
      const idx = options.findIndex((o) =>
        o.kind === 'ask'
          ? previous.kind === 'ask'
          : o.match?.id === previous.match?.id,
      );
      active = idx >= 0 ? idx : preselect();
    } else active = preselect();
    if (query) {
      applyFilter(
        filtered().map((m) => m.id),
        query,
      );
      const count = filtered().length;
      const msg = count
        ? `${count} ${count === 1 ? 'match' : 'matches'}`
        : 'No matches';
      // A poll that finds the same answer says nothing new.
      if (msg !== lastCountMsg) {
        announce(msg);
        lastCountMsg = msg;
      }
    } else {
      restoreBaseFilter();
    }
    render();
  }

  function schedule() {
    if (timer != null) globalThis.clearTimeout?.(timer);
    timer = null;
    if (debounceMs > 0 && typeof globalThis.setTimeout === 'function') {
      timer = globalThis.setTimeout(() => {
        timer = null;
        update();
      }, debounceMs);
    } else update();
  }

  function setQuery(text) {
    const next = String(text ?? '')
      .replace(/\s+/g, ' ')
      .trim();
    if (next !== query) lastCountMsg = null;
    query = next;
    if (!query) {
      filterKey = 'all';
      siteCat = null;
    }
  }

  function clearQuery() {
    input.value = '';
    setQuery('');
    highlight(null);
    update();
  }

  function open() {
    if (destroyed) return;
    if (!isOpen) {
      isOpen = true;
      update({ keepActive: true });
    }
    input.focus?.();
  }

  function close() {
    if (!isOpen && !query) return;
    isOpen = false;
    lastCountMsg = null;
    if (timer != null) globalThis.clearTimeout?.(timer);
    timer = null;
    input.value = '';
    setQuery('');
    options = [];
    active = -1;
    highlight(null);
    restoreBaseFilter();
    render();
  }

  function openResult(m) {
    if (!m?.id) return;
    close();
    ctx?.orb?.select?.(m.id);
    emit('inspect', { id: m.id });
  }

  function ask() {
    const text = query;
    if (!text) return;
    if (!analyst.available) {
      announce(analystReasonText(analyst.reason));
      return;
    }
    const focused = matches.slice(0, ASK_LIMIT).map((m) => m.id);
    const payload = { text };
    if (focused.length) payload.focused_ids = focused;
    close();
    input.blur?.();
    emit('ask', payload);
  }

  function persist() {
    const ids = filtered().map((m) => m.id);
    if (!query || !ids.length) return;
    persisted = { query, ids, external: false };
    close();
    announce(`Filter kept: ${ids.length} shown`);
  }

  function clearPersisted() {
    persisted = null;
    restoreBaseFilter();
    render();
    input.focus?.();
  }

  // ---- events -------------------------------------------------------------------

  input.addEventListener('focus', () => {
    if (!isOpen) open();
  });
  input.addEventListener('input', () => {
    if (!isOpen) {
      isOpen = true;
    }
    setQuery(input.value);
    schedule();
  });
  input.addEventListener('keydown', (event) => {
    const key = event?.key;
    const modAsk =
      key === 'Enter' && (event.metaKey || event.ctrlKey || event.shiftKey);
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      event.preventDefault?.();
      if (!isOpen) open();
      if (timer != null) {
        globalThis.clearTimeout?.(timer);
        timer = null;
        update();
      }
      setActive(active + (key === 'ArrowDown' ? 1 : -1));
      return;
    }
    if (modAsk) {
      event.preventDefault?.();
      if (timer != null) {
        globalThis.clearTimeout?.(timer);
        timer = null;
      }
      setQuery(input.value);
      recompute();
      ask();
      return;
    }
    if (key === 'Enter') {
      event.preventDefault?.();
      if (timer != null) {
        globalThis.clearTimeout?.(timer);
        timer = null;
        setQuery(input.value);
        update();
      }
      const o = options[active] || options[0];
      if (!o) return;
      if (o.kind === 'ask') ask();
      else openResult(o.match);
      return;
    }
    if (key === 'Escape') {
      event.preventDefault?.();
      event.stopPropagation?.();
      if (query || input.value) clearQuery();
      else {
        close();
        input.blur?.();
      }
    }
  });
  root.addEventListener('focusout', (event) => {
    if (rendering) return;
    const next = event?.relatedTarget;
    if (next && typeof root.contains === 'function' && root.contains(next))
      return;
    if (next === undefined) return;
    if (isOpen) close();
  });

  if (store?.on) {
    const off = store.on('change', () => {
      if (isOpen && query) update({ keepActive: true });
      else if (persisted && !persisted.external) {
        const ids = rankNodes(state().graph, persisted.query).map((m) => m.id);
        persisted = { ...persisted, ids };
        applyFilter(ids, persisted.query);
        renderPill();
      } else if (isOpen) render();
    });
    if (typeof off === 'function') offs.push(off);
  }
  if (bus?.on) {
    const offAvail = bus.on('analyst:availability', (p) => {
      analyst = {
        available: p?.available !== false,
        reason: p?.reason ?? null,
      };
      if (isOpen) render();
    });
    if (typeof offAvail === 'function') offs.push(offAvail);
    // Narrow (§3c) has room for the short placeholder only.
    const offLayout = bus.on('layout', (p) => {
      input.setAttribute(
        'placeholder',
        p?.layout === 'narrow'
          ? 'Search or ask'
          : 'Search the picture, or ask a question',
      );
    });
    if (typeof offLayout === 'function') offs.push(offLayout);
    const offFilter = bus.on('search:filter', (p) => {
      if (!p || p.source === 'search') return;
      persisted = Array.isArray(p.ids)
        ? { query: String(p.query ?? ''), ids: [...p.ids], external: true }
        : null;
      // The orb already shows this filter (its sender applied it): remember
      // it, so clearing the pill really clears the orb.
      lastFilterSig = persisted ? sigOf(persisted.ids, persisted.query) : null;
      renderPill();
    });
    if (typeof offFilter === 'function') offs.push(offFilter);
  }

  host?.append?.(root);
  render();

  return {
    element: root,
    input,
    open,
    close,
    query: () => query,
    results: () => filtered(),
    destroy() {
      if (destroyed) return;
      destroyed = true;
      if (timer != null) globalThis.clearTimeout?.(timer);
      for (const off of offs.splice(0)) off();
      root.remove?.();
    },
  };
}
