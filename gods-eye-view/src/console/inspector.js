/**
 * Entity inspector (UX spec §7.1): a plate under the orb at wide, a sheet over
 * the analyst column at compact, a full-screen sheet at narrow. Placement is
 * CSS on `.ic-inspector[data-layout]`; this module sets the attribute from the
 * bus `layout` event.
 *
 * Details come from `ctx.store.entity(id)` (GET /intel/entity/{id}). While
 * they load, the body renders from the graph node so the plate is never
 * empty. Every reading states how it knows (§2.6): measured values are plain,
 * estimates carry `≈`, substitutions carry an "Assumed" tag with the caveat
 * verbatim, a missing threat level is "Not assessed" (never "none" or "Low"),
 * and a missing value is a "No reading" box, never a zero or a blank.
 *
 * Actions go to the bus: `ask {text, focused_ids, draft:true}` (inserts, never
 * sends), `focus:entities {ids, by:"operator"}`, `track:request {vehicle,
 * source:"operator"}`; Abort goes through confirmAbort to the existing
 * `/control/command`, never through the analyst.
 */
import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { renderDom } from './chat/markdown.js';
import { lostLinkLine } from './chat/slip.js';
import { isKnownType } from './orb/glyphs.js';
import { safeText } from './orb/placeText.js';
import { cleanSubtitle } from './orb/text.js';
import {
  SITE_PROTECTED_TEXT,
  isPlaceType,
  placeMapRequest,
  recceText,
  siteBody,
  siteHeader,
  siteOffGraphLine,
  theaterBody,
  unknownBody,
  unknownTitle,
} from './inspectorPlaces.js';
import {
  ICON,
  NOT_IN_PICTURE,
  PHASE_WORD,
  SEVERITY_WORD,
  TYPE_WORD,
  ago,
  alarmId,
  bareMessage,
  alarmLabel,
  bareId,
  button,
  canAbort,
  confirmAbort,
  displayLabel,
  duration,
  feedState,
  focusedKey,
  formatFuel,
  formatPoints,
  fuelBar,
  fuelSentence,
  fuelState,
  fuelTone,
  glyph,
  humanize,
  icon,
  isAssessed,
  linkLines,
  missionKindTitle,
  noReading,
  nodeOf,
  notAssessed,
  num,
  progressBar,
  registerTag,
  restoreFocus,
  segments,
  simTimeSuffix,
  statusWord,
  timeScaleOf,
  toneOf,
  vehicleStateWord,
  zulu,
} from './situation.js';

/** Edge kind -> related-group label (§7.1), in display order. */
export const RELATED_GROUP = Object.freeze({
  flying: 'Flying',
  tracking: 'Tracked by',
  member_of: 'Member of',
  is_a: 'Is a',
  near: 'Near',
  reports_on: 'Reported in',
  about: 'Alarms about this',
  observes: 'Observed by mission',
  target: 'Observed by mission',
  operating_in: 'Operating in',
  in_theater: 'In theater',
});

const THREAT_WORD = Object.freeze({
  critical: 'Critical',
  high: 'High',
  moderate: 'Moderate',
  low: 'Low',
  none: 'None',
});

const CONFIDENCE_WORD = Object.freeze({
  confirmed: 'Confirmed',
  probable: 'Probable',
  possible: 'Possible',
});

const MAX_FOCUS_IDS = 20;
const MAX_VALUE_CHARS = 240;

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests)
// ---------------------------------------------------------------------------

/** Provenance key -> the "How we know" row label; others are humanized. */
export const PROVENANCE_LABEL = Object.freeze({
  source: 'Source',
  telemetry: 'Telemetry',
  telemetry_error: 'Telemetry error',
  stale_ms: 'Telemetry age',
  fuel_source: 'Fuel',
  agl_is_measured: 'AGL measured',
  agl_note: 'AGL',
  los_is_measured: 'Line of sight measured',
  datum_source: 'Altitude datum',
  datum_degraded: 'Datum degraded',
  link_source: 'Link',
  location_source: 'Location',
  observer_position_used: 'Observer position used',
  duplicates_basis: 'Merged',
  row: 'Record',
});

export function provenanceLabel(key) {
  return PROVENANCE_LABEL[key] || humanize(key);
}

/** A SALUTE value as text: a string, or an object's `text`. Null when missing. */
export function saluteText(value) {
  if (value == null) return null;
  if (typeof value === 'string') return value.trim() || null;
  if (typeof value === 'number') return String(value);
  if (typeof value === 'object') {
    const t = value.text ?? value.assessment ?? value.label ?? value.name;
    return typeof t === 'string' && t.trim() ? t.trim() : null;
  }
  return null;
}

/** "47.64450, -122.14020" (5 dp), or null. */
export function coords(lat, lon) {
  const a = num(lat);
  const b = num(lon);
  if (a == null || b == null) return null;
  return `${a.toFixed(5)}, ${b.toFixed(5)}`;
}

/** Threat word for display, or null when not assessed. */
export function threatWord(threat) {
  if (!isAssessed(threat)) return null;
  const t = String(threat).trim().toLowerCase();
  return THREAT_WORD[t] || humanize(t);
}

/** "Probable, 2 sightings". */
export function confidenceLine(confidence, sightings) {
  const c = String(confidence ?? '').toLowerCase();
  const word = CONFIDENCE_WORD[c] || (c ? humanize(c) : 'Unrated');
  const n = num(sightings);
  return n == null
    ? word
    : `${word}, ${n} ${n === 1 ? 'sighting' : 'sightings'}`;
}

/** A compact, bounded text form of any field value. */
export function formatValue(value) {
  if (value == null) return '';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number')
    return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'string') return value;
  let text;
  if (Array.isArray(value)) {
    text = value
      .map((v) => (v && typeof v === 'object' ? JSON.stringify(v) : String(v)))
      .join(', ');
  } else {
    text = Object.entries(value)
      .filter(([, v]) => v != null)
      .map(
        ([k, v]) =>
          `${humanize(k)}: ${v && typeof v === 'object' ? JSON.stringify(v) : v}`,
      )
      .join('; ');
  }
  return text.length > MAX_VALUE_CHARS
    ? `${text.slice(0, MAX_VALUE_CHARS - 1)}…`
    : text;
}

/**
 * Edge kind -> group label when the edge points AT the inspected entity
 * (`related[].dir === "in"`): a mission is "Flown by" its vehicle, a unit
 * lists its "Members". Kinds not listed read the same both ways.
 */
export const RELATED_GROUP_IN = Object.freeze({
  flying: 'Flown by',
  member_of: 'Members',
  is_a: 'Contacts of this class',
  near: 'Contacts nearby',
  about: 'Alarms about this',
  observes: 'Observed by mission',
  target: 'Observed by mission',
  operating_in: 'Vehicles operating here',
  in_theater: 'In this theater',
});

/** Edge kind -> group label when the edge points AWAY from the entity. */
export const RELATED_GROUP_OUT = Object.freeze({
  tracking: 'Tracking',
  reports_on: 'Reports on',
  about: 'About',
  observes: 'Observes',
  target: 'Target',
});

/** The group label for one related row, reading its direction when known. */
export function relatedLabel(kind, dir) {
  if (dir === 'in' && RELATED_GROUP_IN[kind]) return RELATED_GROUP_IN[kind];
  if (dir === 'out' && RELATED_GROUP_OUT[kind]) return RELATED_GROUP_OUT[kind];
  return RELATED_GROUP[kind] || humanize(kind) || 'Related';
}

const RELATED_ORDER = [
  ...new Set([
    ...Object.values(RELATED_GROUP),
    ...Object.values(RELATED_GROUP_IN),
    ...Object.values(RELATED_GROUP_OUT),
  ]),
];

/** Group `related` rows by edge kind and direction, in a stable order. */
export function groupRelated(related) {
  const groups = new Map();
  for (const r of Array.isArray(related) ? related : []) {
    if (!r?.id) continue;
    const label = relatedLabel(r.kind, r.dir);
    if (!groups.has(label)) groups.set(label, []);
    const list = groups.get(label);
    if (!list.some((x) => x.id === r.id)) list.push(r);
  }
  return [...groups.entries()].sort(
    (a, b) =>
      (RELATED_ORDER.indexOf(a[0]) + 1 || 99) -
      (RELATED_ORDER.indexOf(b[0]) + 1 || 99),
  );
}

/**
 * Pattern of life at a place, as one honest line from the entity's
 * `pattern_of_life {baseline, deviation}`: never an invented activity level.
 * Null when there is nothing to say.
 */
export function patternOfLifeLine(pol) {
  if (!pol || typeof pol !== 'object') return null;
  const base =
    pol.baseline && typeof pol.baseline === 'object' ? pol.baseline : null;
  const dev =
    pol.deviation && typeof pol.deviation === 'object' ? pol.deviation : null;
  if (!base && !dev) return null;
  const obs = num(base?.total_obs);
  const visits = num(base?.visits);
  const samples = num(dev?.baseline_samples);
  const parts = [];
  if (obs === 0 || (obs == null && samples === 0)) {
    parts.push('No observations here yet.');
  } else if (obs != null) {
    parts.push(
      `${obs} ${obs === 1 ? 'observation' : 'observations'}` +
        (visits != null
          ? `, ${visits} ${visits === 1 ? 'visit' : 'visits'}.`
          : '.'),
    );
  }
  if (dev?.mature === true) {
    const d = num(dev.deviation);
    if (d != null)
      parts.push(`Deviation from the baseline now ≈ ${d.toFixed(2)}.`);
  } else if (dev) {
    parts.push(
      samples != null
        ? `The baseline isn't mature yet (${samples} of 24 observations), so no deviation is scored.`
        : "The baseline isn't mature yet, so no deviation is scored.",
    );
  }
  return parts.length ? parts.join(' ') : null;
}

const PREFIX_TYPE = Object.freeze({
  veh: 'vehicle',
  msn: 'mission',
  trk: 'track',
  unit: 'unit',
  ob: 'equipment',
  rpt: 'report',
  thr: 'theater',
  poi: 'poi',
  alarm: 'alarm',
  feed: 'feed',
  sit: 'site',
});

/**
 * Node type from an id prefix ("veh:Drone1" -> "vehicle", "sit:…" ->
 * "site"). A bare id is a contact; a prefix the console doesn't know is
 * `'unknown'` (WG §4.2.1), never another type.
 */
export function typeFromId(id) {
  const s = String(id ?? '');
  const i = s.indexOf(':');
  if (i <= 0) return 'track';
  return PREFIX_TYPE[s.slice(0, i)] || 'unknown';
}

/** The vehicle a Track/Abort/Open mission action acts on, or null. */
export function actionVehicle(type, id, attrs = {}, fields = {}) {
  if (type === 'vehicle') return bareId(id) || null;
  if (type === 'mission') return attrs.vehicle || fields.vehicle || null;
  return null;
}

/** `[[type:id|label]]` markup for "Ask about this", with markup-breaking characters removed. */
export function entityMarkup(id, label) {
  // Bidi controls never reach the composer (§3.11).
  const clean = safeText(label, 160)
    .replace(/[[\]|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return clean ? `[[${id}|${clean}]]` : `[[${id}]]`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

let inspectorSeq = 0;

/**
 * Mount the inspector.
 * @param {object} host element the inspector renders into
 * @param {object} ctx console ctx ({store, bus, api, orb, root})
 * @param {{now?:()=>number, layout?:string}} [opts]
 * @returns {{show(id:string):void, hide():void, destroy():void, setLayout(layout:string):void, current():string|null, element:object}}
 */
export function createInspector(host, ctx, opts = {}) {
  const now = opts.now || (() => Date.now());
  const bus = ctx?.bus;
  const store = ctx?.store;
  const n = ++inspectorSeq;
  const titleId = `ic-inspector-title-${n}`;
  let layout = opts.layout || ctx?.layout || 'wide';
  let destroyed = false;
  const offs = [];

  /** {id, entity, status:'loading'|'ready'|'error'|'gone', error, rawOpen} */
  let cur = null;
  /** Waiting approvals, from the analyst (`approval:pending`). */
  let pending = { count: 0, oldest: null };
  /** One level of history: {id, label}. */
  let previous = null;
  let loadSeq = 0;

  const head = h('header', { class: 'ic-inspector__head' });
  const notice = h('div', { class: 'ic-inspector__notice', hidden: true });
  const status = h('p', { class: 'ic-inspector__loading', role: 'status' });
  const body = h('div', { class: 'ic-inspector__body' });
  const actions = h('div', {
    class: 'ic-inspector__actions',
    role: 'group',
    'aria-label': 'Actions',
  });
  const live = h('p', {
    class: 'ic-kit-vh',
    role: 'status',
    'aria-live': 'polite',
  });
  const root = h(
    'section',
    {
      class: 'ic-inspector',
      role: 'region',
      'aria-labelledby': titleId,
      // F6 lands on the region itself (§8 regions).
      tabindex: '-1',
      'data-layout': layout,
      hidden: true,
    },
    head,
    notice,
    status,
    body,
    actions,
    live,
  );

  const state = () => store?.get?.() || {};
  const emit = (event, payload) => bus?.emit?.(event, payload);
  const nodeFor = (id) => nodeOf(state(), id);
  /** The sim speed when it isn't ×1: durations then say "in sim time". */
  const simScale = () => timeScaleOf(state().graph?.theater);

  // ---- small builders -------------------------------------------------------

  function field(labelText, ...value) {
    const kids = value
      .flat()
      .filter((v) => v != null && v !== false && v !== '');
    return h(
      'div',
      { class: 'ic-kit-dl__row' },
      h('dt', { class: 'ic-kit-dl__term' }, labelText),
      h(
        'dd',
        { class: 'ic-kit-dl__def' },
        ...(kids.length ? kids : [noReading()]),
      ),
    );
  }

  /** A stacked label-over-value cell (the SALUTE grid). */
  function cell(labelText, ...value) {
    const kids = value
      .flat()
      .filter((v) => v != null && v !== false && v !== '');
    return h(
      'div',
      { class: 'ic-kit-cell' },
      h('dt', { class: 'ic-kit-cell__term' }, labelText),
      h(
        'dd',
        { class: 'ic-kit-cell__def' },
        ...(kids.length ? kids : [noReading()]),
      ),
    );
  }

  function fields(...rows) {
    return h('dl', { class: 'ic-kit-dl' }, ...rows.filter(Boolean));
  }

  function caption(text) {
    return h('p', { class: 'ic-kit-note' }, text);
  }

  function mono(text) {
    return h('span', { class: 'ic-kit-mono' }, text);
  }

  function assumed(text, why) {
    return [
      h('span', { class: 'ic-kit-assumed', title: why || undefined }, text),
      registerTag('assumed', why || undefined),
    ];
  }

  function chip(id, labelText, type) {
    const node = nodeFor(id);
    const known = Boolean(node);
    const shown = labelText || node?.label || null;
    const el = h(
      'button',
      {
        type: 'button',
        class: 'ic-kit-chip',
        'data-known': known ? 'true' : 'false',
        'data-key': `chip:${id}`,
        title: known ? undefined : NOT_IN_PICTURE,
        'aria-label': `Inspect ${shown || id}`,
      },
      glyph(node?.type || type, node?.status || 'unknown', 10, undefined, {
        category: node?.attrs?.category,
      }),
      h(
        'span',
        {
          class: shown
            ? 'ic-kit-chip__label'
            : 'ic-kit-chip__label ic-kit-mono',
        },
        shown || id,
      ),
    );
    el.addEventListener('click', () => show(id));
    const on = () => ctx?.orb?.highlight?.([id], { by: 'operator' });
    const off = () => ctx?.orb?.highlight?.([], { by: 'operator' });
    el.addEventListener('mouseenter', on);
    el.addEventListener('focus', on);
    el.addEventListener('mouseleave', off);
    el.addEventListener('blur', off);
    return el;
  }

  function chips(list) {
    const kids = list.filter(Boolean);
    return kids.length ? h('div', { class: 'ic-kit-chips' }, ...kids) : null;
  }

  function section(titleText, ...kids) {
    const inner = kids.flat().filter(Boolean);
    if (!inner.length) return null;
    return h(
      'section',
      { class: 'ic-inspector__section' },
      h('h3', { class: 'ic-inspector__subtitle' }, titleText),
      ...inner,
    );
  }

  // ---- per-type bodies ---------------------------------------------------------

  function trackBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const sal = f.salute && typeof f.salute === 'object' ? f.salute : {};
    const loc =
      sal.location && typeof sal.location === 'object' ? sal.location : {};
    const lat = f.lat ?? loc.lat ?? node?.lat;
    const lon = f.lon ?? loc.lon ?? node?.lon;
    const where = coords(lat, lon);
    const losAssumed =
      loc.los_is_measured === false || f.los_is_measured === false;
    const lastSeen = num(f.last_seen_ms) ?? num(node?.ts_ms);
    const missing = 'Not in the track record';
    const equipment =
      saluteText(sal.equipment) || f.equipment_name || f.platform || null;
    const unitText = saluteText(sal.unit);

    const salute = h(
      'dl',
      { class: 'ic-inspector__salute' },
      cell('Size', saluteText(sal.size) ?? noReading(missing)),
      cell('Activity', saluteText(sal.activity) ?? noReading(missing)),
      cell(
        'Location',
        where
          ? losAssumed
            ? assumed(where, 'Line of sight is geometric, not measured.')
            : mono(where)
          : noReading('No position fix'),
      ),
      cell(
        'Unit',
        unitText ??
          (f.unit || a.unit ? chip(f.unit || a.unit) : noReading(missing)),
      ),
      cell(
        'Time',
        lastSeen != null
          ? `Last seen ${zulu(lastSeen, { seconds: true })}, ${ago(lastSeen, now())} ago`
          : noReading('No sighting time recorded'),
      ),
      cell('Equipment', equipment ?? noReading(missing)),
    );

    const threat = f.threat ?? a.threat;
    const word = threatWord(threat);
    const threatRow = word
      ? field(
          'Threat',
          h(
            'span',
            {
              class: 'ic-kit-threat',
              'data-tone': toneOf('track', node?.status),
            },
            `${word} ≈`,
          ),
          registerTag('estimated'),
          caption('Model output. Sensor-posture advice only.'),
        )
      : field(
          'Threat',
          notAssessed(),
          caption('This is not the same as no threat.'),
        );

    const rows = [
      field(
        'Confidence',
        confidenceLine(
          f.confidence ?? a.confidence,
          f.sightings ?? a.sightings,
        ),
      ),
      threatRow,
    ];
    if (f.custody_lapsed || a.stale) {
      rows.push(
        field('Custody', 'Lapsed: the position is the last fix, not current.'),
      );
    }
    const inTheater = theaterNote(a, f);
    if (inTheater) rows.push(field('Theater', inTheater));
    const dups = Array.isArray(f.duplicates) ? f.duplicates : [];
    const dupIds = dups.length
      ? dups.map((d) => d?.track_id).filter(Boolean)
      : (Array.isArray(a.duplicates) ? a.duplicates : []).map((d) => bareId(d));
    // The graph lists at most a few duplicate ids; `duplicate_count` is the
    // total (graph v1.1). Count from it, never from the list's length.
    const dupCount = Math.max(
      num(a.duplicate_count) ?? 0,
      dups.length,
      dupIds.length,
    );
    const kids = [salute, fields(...rows)];
    if (dupCount > 0) {
      const more = dupCount - dupIds.length;
      kids.push(
        section(
          'Merged',
          h(
            'p',
            { class: 'ic-inspector__line' },
            `${dupCount + 1} sightings from separate runs merged into this contact`,
          ),
          dupIds.length
            ? h(
                'div',
                { class: 'ic-kit-chips' },
                ...dupIds.map((d) =>
                  h('span', { class: 'ic-kit-idtag ic-kit-mono' }, d),
                ),
              )
            : null,
          more > 0 ? caption(`${more} more not listed.`) : null,
        ),
      );
    }
    return kids;
  }

  /**
   * Where a contact or place sits relative to the active theater (graph
   * v1.1 `out_of_theater`, `outside_ao`, `unlocated`), or null when inside.
   */
  function theaterNote(a = {}, f = {}) {
    const theater = f.theater ?? a.theater;
    if (a.unlocated || f.unlocated) {
      return h(
        'span',
        { class: 'ic-kit-status', 'data-status': 'warn' },
        'No position fix, so its theater is not known',
      );
    }
    if (a.out_of_theater || f.out_of_theater) {
      return h(
        'span',
        { class: 'ic-kit-status', 'data-status': 'stale' },
        theater
          ? `Outside the active theater (${theater})`
          : 'Outside the active theater',
      );
    }
    if (a.outside_ao || f.outside_ao) {
      const margin = num(state().graph?.meta?.ao_margin_m);
      return h(
        'span',
        { class: 'ic-kit-status', 'data-status': 'warn' },
        margin != null
          ? `Just outside the AO, within the ${formatKm(margin)} margin`
          : 'Just outside the AO',
      );
    }
    return null;
  }

  function formatKm(metres) {
    const km = metres / 1000;
    return `${Number.isInteger(km) ? km : km.toFixed(1)} km`;
  }

  function vehicleBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const fuel = f.fuel && typeof f.fuel === 'object' ? f.fuel : {};
    const agl = f.agl && typeof f.agl === 'object' ? f.agl : {};
    const prov = entity?.provenance || {};
    // Live readings come from the graph node (polled every 2 s); the entity
    // (fetched once on open) fills in only what the node lacks.
    const merged = {
      fuel_pct: a.fuel_pct ?? fuel.fuel_pct,
      bingo_fuel_pct: a.bingo_fuel_pct ?? fuel.bingo_fuel_pct,
      bingo_latched: a.bingo_latched ?? fuel.bingo_latched,
      landed: a.landed ?? f.landed,
    };
    const fs = fuelState(merged);
    const stateAttrs = {
      ...a,
      bingo_latched: merged.bingo_latched,
      link: a.link ?? f.link,
    };
    const stateWord =
      fs.state === 'latched' && !fs.landed
        ? "Returning home on BINGO; can't be cancelled"
        : vehicleStateWord({ ...stateAttrs, landed: merged.landed });

    let fuelRow;
    if (fs.state === 'unknown')
      fuelRow = field('Fuel', noReading('No fuel telemetry yet.'));
    else {
      const readout = [formatFuel(fs.fuel, 1)];
      if (fs.bingo != null) readout.push(`BINGO ${formatFuel(fs.bingo, 1)}`);
      const sentence =
        fs.state === 'above' || fs.state === 'near'
          ? `${formatPoints(fs.margin, 1)} above`
          : fuelSentence(fs);
      readout.push(sentence);
      fuelRow = field(
        'Fuel',
        fuelBar(fs, { status: fuelTone(fs) }),
        h(
          'span',
          { class: 'ic-kit-fuelline', 'data-state': fs.state },
          segmentsOf(readout),
        ),
      );
    }
    const rows = [
      field(
        'State',
        h(
          'span',
          { class: 'ic-kit-status', 'data-status': node?.status },
          stateWord,
        ),
      ),
      fuelRow,
    ];
    const eta = num(a.eta_to_bingo_s ?? fuel.eta_to_bingo_s);
    if (eta != null && fs.state !== 'latched' && fs.state !== 'below') {
      rows.push(
        field(
          'To BINGO',
          `≈ ${duration(eta)} to BINGO${simTimeSuffix(eta, simScale())}`,
          registerTag('estimated'),
        ),
      );
    }
    const aglM = num(a.agl_m ?? agl.alt_agl_m);
    const aglReal = a.agl_is_real ?? agl.alt_agl_is_real;
    if (aglM != null) {
      const text = `${Math.round(aglM)} m`;
      rows.push(
        field(
          'AGL',
          aglReal === false
            ? assumed(
                text,
                prov.agl_note ||
                  'Height above launch datum, not terrain clearance.',
              )
            : text,
        ),
      );
    } else rows.push(field('AGL', noReading('No altitude reading')));
    rows.push(
      field(
        'Link',
        linkLines(stateAttrs).map((l) =>
          h('span', { class: 'ic-kit-line', 'data-status': l.status }, l.text),
        ),
      ),
    );
    const ts = num(node?.ts_ms);
    if (ts != null)
      rows.push(
        field(
          'Telemetry',
          `Last update ${zulu(ts, { seconds: true })}, ${ago(ts, now())} ago`,
        ),
      );
    // The node (polled) says whether a mission is running NOW; the entity
    // was fetched on open and may still name one that has since ended.
    const mission = node ? a.mission : f.mission;
    if (mission)
      rows.push(field('Mission', chip(`msn:${mission}`, null, 'mission')));
    if (prov.datum_degraded || a.datum_degraded) {
      rows.push(
        field(
          'Datum',
          h(
            'span',
            { class: 'ic-kit-status', 'data-status': 'warn' },
            'Altitude datum degraded',
          ),
        ),
      );
    }
    // The lost-link plan only from data (graph v1.1 `attrs.lost_link`, or
    // the entity's live link plan); never assumed.
    const linkField = f.link && typeof f.link === 'object' ? f.link : {};
    const ll =
      a.lost_link && typeof a.lost_link === 'object'
        ? a.lost_link
        : linkField.plan && typeof linkField.plan === 'object'
          ? { ...linkField.plan, source: 'vehicle' }
          : null;
    const llText = ll
      ? lostLinkLine(node?.label || bareId(node?.id || cur?.id), ll).replace(
          /\s*That's its lost-link plan\.$/,
          '',
        )
      : '';
    if (llText) {
      const fromDefault = ll.source && ll.source !== 'vehicle';
      rows.push(
        field(
          'Lost-link plan',
          llText,
          fromDefault
            ? caption("The server's default plan, not read from this aircraft.")
            : null,
        ),
      );
    }
    return [fields(...rows)];
  }

  function segmentsOf(parts) {
    return segments(parts.filter(Boolean).join(' · '));
  }

  function missionBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const phase = String(f.phase ?? a.phase ?? '').toLowerCase();
    const kind = missionKindTitle(f.kind ?? a.kind) || 'Mission';
    const vehicle = f.vehicle ?? a.vehicle;
    const pct = num(f.progress_pct ?? a.progress_pct);
    const rows = [
      field('Kind', kind),
      field(
        'Vehicle',
        vehicle
          ? chip(`veh:${vehicle}`, vehicle, 'vehicle')
          : noReading('No vehicle recorded'),
      ),
      field(
        'Phase',
        h(
          'span',
          { 'data-phase': phase },
          PHASE_WORD[phase] || humanize(phase) || 'Phase unknown',
        ),
      ),
      field(
        'Progress',
        pct != null
          ? [
              progressBar(pct, kind),
              h('span', { class: 'ic-kit-pct' }, `${Math.round(pct)}%`),
            ]
          : noReading('Progress not reported'),
      ),
    ];
    const wp =
      f.waypoint && typeof f.waypoint === 'object' ? f.waypoint : a.waypoint;
    const wpIndex = num(wp?.index ?? f.waypoint_index ?? a.waypoint_index);
    const wpOf = num(
      wp?.of ?? f.waypoint_count ?? a.waypoint_count ?? f.waypoints?.count,
    );
    if (wpIndex != null && wpOf != null)
      rows.push(field('Waypoint', `Waypoint ${wpIndex} of ${wpOf}`));
    else if (wpOf != null) rows.push(field('Waypoints', `${wpOf} waypoints`));
    const eta = num(f.eta_s ?? a.eta_s);
    if (eta != null)
      rows.push(
        field(
          'ETA',
          `≈ ${duration(eta)}${simTimeSuffix(eta, simScale())}`,
          registerTag('estimated'),
        ),
      );
    const reason = f.incomplete_reason ?? a.incomplete_reason;
    if (reason)
      rows.push(
        field(
          'Incomplete',
          h(
            'span',
            { class: 'ic-kit-status', 'data-status': 'warn' },
            String(reason),
          ),
        ),
      );
    const started = num(f.started_ms) ?? num(node?.ts_ms);
    rows.push(
      field(
        'Started',
        started != null
          ? zulu(started, { seconds: true })
          : 'Start time not recorded',
      ),
    );
    return [fields(...rows)];
  }

  function listOfText(items) {
    return h(
      'ul',
      { class: 'ic-kit-list' },
      ...items.map((item) =>
        h('li', {}, typeof item === 'string' ? item : formatValue(item)),
      ),
    );
  }

  /** Report prose through the chat's safe Markdown renderer (text nodes only). */
  function markdown(text) {
    try {
      const nodes = renderDom(String(text), {
        chip: (ref) => chip(ref.id, ref.label),
      });
      if (Array.isArray(nodes) && nodes.length) {
        return h('div', { class: 'ic-md ic-inspector__prose' }, ...nodes);
      }
    } catch {
      // fall through to plain paragraphs
    }
    return paragraphs(text);
  }

  function paragraphs(text) {
    return String(text)
      .split(/\n{2,}/)
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => h('p', { class: 'ic-inspector__prose' }, p));
  }

  function reportBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const header = f.header && typeof f.header === 'object' ? f.header : {};
    const format = header.format || a.format || 'Report';
    const rid = header.report_id || a.report_id || bareId(node?.id || cur?.id);
    const rows = [field('Format', format, ' ', mono(rid))];
    if (f.confidence_summary != null)
      rows.push(field('Confidence', formatValue(f.confidence_summary)));
    let gaps;
    if (Array.isArray(f.gaps)) {
      gaps = f.gaps.length
        ? listOfText(f.gaps)
        : h('p', { class: 'ic-inspector__line' }, 'No gaps recorded.');
    } else {
      gaps = h(
        'p',
        { class: 'ic-inspector__line' },
        "This report doesn't list gaps.",
      );
    }
    const kids = [fields(...rows), section('Gaps', gaps)];
    const summary = f.mission_summary ?? header.detail ?? a.detail;
    if (summary) {
      kids.push(
        section(
          'Summary',
          typeof summary === 'string'
            ? markdown(summary)
            : h('p', { class: 'ic-inspector__prose' }, formatValue(summary)),
        ),
      );
    }
    const contacts = (Array.isArray(f.contacts) ? f.contacts : [])
      .map((c) => c?.track_id)
      .filter(Boolean);
    const assessed = (Array.isArray(f.assessments) ? f.assessments : []).filter(
      (x) => x?.track_id,
    );
    if (contacts.length)
      kids.push(
        section(
          'Contacts',
          chips(contacts.map((t) => chip(`trk:${t}`, null, 'track'))),
        ),
      );
    if (assessed.length) {
      kids.push(
        section(
          'Assessments',
          h(
            'ul',
            { class: 'ic-kit-list' },
            ...assessed.map((x) =>
              h(
                'li',
                {},
                chip(`trk:${x.track_id}`, null, 'track'),
                ' ',
                threatWord(x.threat_level)
                  ? `${threatWord(x.threat_level)} ≈`
                  : notAssessed(),
              ),
            ),
          ),
        ),
      );
    }
    return kids;
  }

  function equipmentBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const lib = f.library && typeof f.library === 'object' ? f.library : {};
    const acq = num(lib.acquisition_range_m ?? a.acquisition_range_m);
    const emitter =
      lib.emitter ??
      (Array.isArray(lib.signature_cues)
        ? lib.signature_cues.join(', ')
        : null);
    const rows = [
      field('Name', lib.name || node?.label || bareId(node?.id || cur?.id)),
      field('Role', lib.role || noReading('Not in the reference library')),
      field(
        'Acquisition range',
        acq != null
          ? `${(acq / 1000).toFixed(1)} km`
          : noReading('Not in the reference library'),
      ),
    ];
    if (emitter) rows.push(field('Emitter', String(emitter)));
    const contacts = Array.isArray(f.contacts) ? f.contacts : [];
    return [
      caption('Reference data, not an observation.'),
      fields(...rows),
      section(
        'Contacts of this class',
        chips(contacts.map((id) => chip(id, null, 'track'))),
      ),
    ];
  }

  function unitBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const word = threatWord(f.threat ?? a.threat);
    const members = Array.isArray(f.members) ? f.members : [];
    const rows = [
      field(
        'Category',
        humanize(f.category ?? a.category) || noReading('No category'),
      ),
      field(
        'Top threat',
        word ? [`${word} ≈`, registerTag('estimated')] : notAssessed(),
      ),
    ];
    const basis = f.basis ?? a.basis;
    const kids = [fields(...rows)];
    if (members.length) {
      kids.push(
        section(
          'Members',
          chips(members.map((m) => chip(m?.id, m?.label, 'track'))),
        ),
      );
    }
    if (basis) kids.push(caption(String(basis)));
    return kids;
  }

  function placeBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const where = coords(f.lat ?? node?.lat, f.lon ?? node?.lon);
    const rows = [
      field(
        'Label',
        node?.label || f.label || f.name || bareId(node?.id || cur?.id),
      ),
    ];
    const place = f.place ?? a.place;
    if (place) rows.push(field('Place', String(place)));
    rows.push(
      field('Coordinates', where ? mono(where) : noReading('No coordinates')),
    );
    if (node?.type === 'theater')
      rows.push(field('Active', (f.active ?? a.active) ? 'Yes' : 'No'));
    const outside = theaterNote(a, {});
    if (outside && node?.type === 'poi') rows.push(field('Theater', outside));
    const kids = [fields(...rows)];
    const near = Array.isArray(f.near_contacts) ? f.near_contacts : [];
    if (node?.type === 'poi') {
      kids.push(
        section(
          'Nearby contacts',
          near.length
            ? chips(near.map((id) => chip(id, null, 'track')))
            : h(
                'p',
                { class: 'ic-inspector__line' },
                'No contacts within 250 m.',
              ),
        ),
      );
      const polLine = patternOfLifeLine(f.pattern_of_life);
      kids.push(
        section(
          'Pattern of life',
          polLine
            ? h('p', { class: 'ic-inspector__line' }, polLine)
            : noReading('No pattern-of-life baseline for this place.'),
        ),
      );
    }
    return kids;
  }

  function alarmBody(node, entity) {
    const a = node?.attrs || {};
    const f = entity?.fields || {};
    const kind = f.kind ?? a.kind;
    const sev = f.severity ?? a.severity;
    const at = num(f.atMs ?? f.at_ms) ?? num(node?.ts_ms);
    const rows = [
      field('Kind', alarmLabel(kind)),
      field(
        'Severity',
        h(
          'span',
          { 'data-severity': sev },
          SEVERITY_WORD[sev] || humanize(sev) || 'Not reported',
        ),
      ),
      field(
        'Message',
        String(f.message ?? node?.subtitle ?? '') || noReading('No message'),
      ),
      field(
        'Time',
        at != null
          ? zulu(at, { seconds: true })
          : noReading('No time recorded'),
      ),
    ];
    const about = [];
    const vehicle = f.vehicle ?? a.vehicle;
    const track = f.track_id ?? a.track_id;
    const mission = f.mission_id ?? a.mission_id;
    if (vehicle) about.push(chip(`veh:${vehicle}`, vehicle, 'vehicle'));
    if (track) about.push(chip(`trk:${track}`, null, 'track'));
    if (mission) about.push(chip(`msn:${mission}`, null, 'mission'));
    if (about.length) rows.push(field('About', chips(about)));
    const kids = [fields(...rows)];
    if (kind === 'bingo')
      kids.push(
        h(
          'p',
          { class: 'ic-inspector__line', 'data-status': 'critical' },
          "The BINGO return can't be cancelled.",
        ),
      );
    return kids;
  }

  function feedBody(node, entity) {
    const f = entity?.fields || {};
    const id = node?.id || entity?.id || cur?.id;
    const fs = feedState(id, {
      node: node || {
        id,
        status: entity?.status,
        subtitle: entity?.subtitle,
        attrs: f,
      },
      feed: f,
      now: now(),
    });
    if (/^Down/.test(fs.word)) {
      const reason =
        (fs.at != null
          ? `Down since ${zulu(fs.at, { seconds: true })}.`
          : 'Down.') + ' Missing data here is not a clear picture.';
      const err = String(f.error ?? node?.attrs?.error ?? '').trim();
      return [noReading(reason), err ? caption(err) : null];
    }
    return [
      h(
        'p',
        { class: 'ic-inspector__line', 'data-status': fs.status },
        fs.word === 'Up' && fs.at != null
          ? `Up, last update ${ago(fs.at, now())} ago`
          : fs.text,
      ),
    ];
  }

  /** The builders inspectorPlaces.js draws with (theater, site, unknown). */
  const kit = {
    field,
    fields,
    mono,
    caption,
    chip,
    chips,
    section,
    now,
    /** A disclosure inside the body ("Show all 9 tags"), per entity. */
    isOpen: (key) => Boolean(cur?.open?.has(key)),
    toggle(key) {
      if (!cur) return;
      cur.open ||= new Set();
      if (cur.open.has(key)) cur.open.delete(key);
      else cur.open.add(key);
      renderBody();
    },
  };

  const BODY = {
    track: trackBody,
    vehicle: vehicleBody,
    mission: missionBody,
    report: reportBody,
    equipment: equipmentBody,
    unit: unitBody,
    theater: (node, entity) => theaterBody(kit, node, entity, state().graph),
    poi: placeBody,
    alarm: alarmBody,
    feed: feedBody,
    site: (node, entity) => siteBody(kit, node, entity),
  };

  function provenanceSection(entity) {
    const prov =
      entity?.provenance && typeof entity.provenance === 'object'
        ? entity.provenance
        : {};
    const caveats = (Array.isArray(entity?.caveats) ? entity.caveats : [])
      .map((c) => String(c ?? '').trim())
      .filter(Boolean)
      .slice(0, 6);
    const rows = [];
    for (const [key, value] of Object.entries(prov)) {
      if (value == null || value === '') continue;
      const labelText = provenanceLabel(key);
      // `*_at_ms` / `*_since_ms` are epoch timestamps (e.g. link_lost_since_ms);
      // formatting them as durations printed "497391 h". Ages stay durations.
      if (/_(at|since)_ms$/.test(key) && typeof value === 'number') {
        rows.push(field(labelText, zulu(value, { seconds: true })));
        continue;
      }
      if (/_ms$/.test(key) && typeof value === 'number') {
        rows.push(field(labelText, duration(value / 1000) || '0 s'));
        continue;
      }
      if (key === 'agl_is_measured' || key === 'los_is_measured') {
        rows.push(
          field(
            labelText,
            value === false
              ? ['No', registerTag('assumed', prov.agl_note || undefined)]
              : [formatValue(value), registerTag('measured')],
          ),
        );
        continue;
      }
      if (
        Array.isArray(value) &&
        value.every((v) => v && typeof v === 'object')
      ) {
        rows.push(field(labelText, listOfText(value.slice(0, 8))));
        continue;
      }
      rows.push(field(labelText, formatValue(value)));
    }
    // The entity's caveats (what is assumed while real data or a feed is
    // off), verbatim with the Assumed tag.
    const caveatList = caveats.length
      ? h(
          'ul',
          { class: 'ic-kit-list ic-inspector__caveats' },
          ...caveats.map((c) => h('li', {}, c, ' ', registerTag('assumed', c))),
        )
      : null;
    if (!rows.length && !caveatList) return null;
    return section(
      'How we know',
      rows.length ? fields(...rows) : null,
      caveatList,
    );
  }

  function relatedSection(entity) {
    const groups = groupRelated(entity?.related);
    if (!groups.length) return null;
    const kids = groups.map(([labelText, list]) =>
      h(
        'div',
        { class: 'ic-inspector__related' },
        h('h4', { class: 'ic-inspector__related-label' }, labelText),
        chips(list.map((r) => chip(r.id, r.label, r.type))),
      ),
    );
    if (num(entity?.related_omitted)) {
      kids.push(
        caption(`${entity.related_omitted} more related entities not listed.`),
      );
    }
    return section('Related', ...kids);
  }

  function rawSection(entity) {
    if (!entity || entity.raw == null) return null;
    const open = Boolean(cur?.rawOpen);
    const toggle = h(
      'button',
      {
        type: 'button',
        class: 'ic-kit-btn ic-kit-btn--link',
        'aria-expanded': open ? 'true' : 'false',
        'data-key': 'raw',
      },
      open ? 'Hide raw record' : 'Show raw record',
    );
    toggle.addEventListener('click', () => {
      if (!cur) return;
      cur.rawOpen = !cur.rawOpen;
      renderBody();
    });
    const pre = open
      ? h(
          'pre',
          { class: 'ic-inspector__rawtext ic-kit-mono' },
          JSON.stringify(entity.raw, null, 2),
        )
      : null;
    return h('div', { class: 'ic-inspector__raw' }, toggle, pre);
  }

  // ---- header, notice, body, actions ------------------------------------------------

  function renderHead() {
    if (!cur) return;
    const node = nodeFor(cur.id);
    const entity = cur.entity;
    const type = node?.type || entity?.type || typeFromId(cur.id);
    const stat = node?.status || entity?.status || 'unknown';
    const known = isKnownType(type);
    const site = type === 'site' ? siteHeader(node, entity) : null;
    const labelText =
      (node ? displayLabel(node) : '') ||
      (type === 'feed' ? displayLabel({ type, id: cur.id }) : '') ||
      entity?.label ||
      cur.id;
    const feedNode =
      type === 'feed'
        ? node || {
            id: cur.id,
            type,
            status: stat,
            subtitle: entity?.subtitle,
            attrs: entity?.fields || {},
          }
        : null;
    const word = feedNode
      ? feedState(feedNode.id, { node: feedNode, now: now() }).word
      : statusWord(node || { type, status: stat, attrs: entity?.fields || {} });
    const kids = [];
    // The compact sheet covers the analyst's own approval bar: a waiting
    // decision stays pinned at the sheet's top, with Review (§3a).
    // (At narrow the shell's own bar already pins it above every tab.)
    if (layout === 'compact' && pending.count > 0) kids.push(approvalBanner());
    if (layout === 'compact') {
      kids.push(
        button('Back to analyst', {
          icon: ICON.back,
          cls: 'ic-kit-btn--quiet ic-inspector__back',
          key: 'back',
          onClick: () => hide(),
        }),
      );
    } else if (previous) {
      kids.push(
        button(`Back to ${previous.label}`, {
          icon: ICON.back,
          cls: 'ic-kit-btn--quiet ic-inspector__back',
          key: 'back',
          onClick: () => goBack(),
        }),
      );
    } else if (layout === 'narrow') {
      kids.push(
        button('Back', {
          icon: ICON.back,
          cls: 'ic-kit-btn--quiet ic-inspector__back',
          key: 'back',
          onClick: () => hide(),
        }),
      );
    }
    const copy = h(
      'button',
      {
        type: 'button',
        class: 'ic-kit-btn ic-kit-btn--icon',
        'aria-label': 'Copy id',
        title: 'Copy id',
        'data-key': 'copy',
      },
      icon(ICON.copy),
    );
    copy.addEventListener('click', () => copyId(cur?.id));
    kids.push(
      h(
        'div',
        { class: 'ic-inspector__titlebar' },
        glyph(type, stat, 20, undefined, { category: site?.category }),
        h(
          'h2',
          { class: 'ic-inspector__title', id: titleId, tabindex: '-1' },
          // Labels may be OSM names or geocoder text: bidi-safe (§3.11).
          safeText(labelText, 160) || cur.id,
        ),
        h(
          'span',
          {
            class: known
              ? 'ic-inspector__type'
              : 'ic-inspector__type ic-inspector__type--unrecognised',
          },
          known ? TYPE_WORD[type] || humanize(type) : unknownTitle(type),
        ),
        site
          ? h(
              'span',
              {
                class: 'ic-inspector__category',
                'data-category': site.category,
              },
              site.categoryWord,
            )
          : null,
        word
          ? h(
              'span',
              {
                class:
                  word === 'Not assessed'
                    ? 'ic-kit-notassessed'
                    : 'ic-inspector__status',
                'data-status': stat,
                'data-tone': toneOf(type, stat),
              },
              word,
            )
          : null,
        site?.protected
          ? h(
              'span',
              { class: 'ic-inspector__protected', 'data-status': 'protected' },
              SITE_PROTECTED_TEXT,
            )
          : null,
        h('code', { class: 'ic-inspector__id ic-kit-mono' }, cur.id),
        copy,
      ),
    );
    // A site's subtitle repeats its category and status words, shown above.
    const sub = feedNode || site ? '' : cleanSubtitle(node?.subtitle);
    if (sub) kids.push(segments(sub, 'ic-inspector__subtitle-line'));
    replaceKids(head, kids);
  }

  function approvalBanner() {
    const o = pending.oldest;
    const what = o?.title
      ? [o.title, o.vehicle].filter(Boolean).join(', ')
      : '';
    const words =
      pending.count > 1
        ? `${pending.count} approvals waiting.${what ? ` Oldest: ${what}.` : ''}`
        : what
          ? `Approval waiting: ${what}.`
          : 'Approval waiting.';
    return h(
      'div',
      { class: 'ic-inspector__approval', role: 'status' },
      h('p', { class: 'ic-inspector__approvaltext' }, words),
      button('Review', {
        cls: 'ic-inspector__review',
        key: 'approval:review',
        onClick: () => {
          hide();
          emit('approval:review', {});
        },
      }),
    );
  }

  function renderNotice() {
    if (!cur) return;
    const kids = [];
    const node = nodeFor(cur.id);
    const hasGraph = Boolean(state().graph);
    if (cur.status === 'error') {
      const retry = button('Retry', {
        icon: ICON.retry,
        key: 'retry',
        onClick: () => load(cur.id),
      });
      kids.push(
        h(
          'p',
          {
            class: 'ic-inspector__line',
            role: 'alert',
            'data-status': 'critical',
          },
          `Couldn't load details for ${cur.id}: ${cur.error}.`,
        ),
        retry,
      );
    }
    if (cur.status === 'gone' || (hasGraph && !node && cur.seenInGraph)) {
      const last = num(cur.lastSeen);
      kids.push(
        h(
          'p',
          { class: 'ic-inspector__line', 'data-status': 'stale' },
          last != null
            ? `This entity left the picture. Last seen ${zulu(last, { seconds: true })}.`
            : 'This entity left the picture.',
        ),
      );
    }
    if (hasGraph && !node && !cur.seenInGraph && cur.status === 'ready') {
      // A site the map draws but the graph's cap left out is in this
      // theater: say so, not "outside this theater or aged out".
      const offGraph = siteOffGraphLine(cur.entity, state().graph);
      kids.push(
        h('p', { class: 'ic-inspector__line' }, offGraph || NOT_IN_PICTURE),
      );
    }
    const requested = cur.entity?.requested_id;
    if (requested && requested !== cur.id) {
      kids.push(
        h(
          'p',
          { class: 'ic-inspector__line' },
          `Merged into ${bareId(cur.id)}.`,
        ),
      );
    }
    if (cur.entity?._truncated) {
      kids.push(
        h(
          'p',
          { class: 'ic-inspector__line' },
          'Details are larger than shown. Ask the analyst for the full record.',
        ),
      );
    }
    replaceKids(notice, kids);
    setHidden(notice, !kids.length);
  }

  function renderStatus() {
    const loading = cur?.status === 'loading';
    replaceKids(status, loading ? ['Loading details…'] : []);
    setHidden(status, !loading);
  }

  function renderBody() {
    if (!cur) return;
    const key = focusedKey(root);
    const node = nodeFor(cur.id);
    const entity = cur.entity;
    const type = node?.type || entity?.type;
    // Own keys only: a type named "constructor" is unknown, not Object.
    const build =
      typeof type === 'string' && Object.hasOwn(BODY, type) ? BODY[type] : null;
    const kids = [];
    if (build && (node || entity)) kids.push(...build(node, entity).flat());
    else if ((node || entity) && !isKnownType(type))
      kids.push(...unknownBody(kit, node, entity)); // WG §4.2.1
    else if (!node && !entity && cur.status !== 'loading')
      kids.push(noReading('No details for this entity.'));
    if (entity) {
      // A site states its source and caveat in its own rows (WG §4.2.6).
      if (type !== 'site') kids.push(provenanceSection(entity));
      kids.push(relatedSection(entity));
      kids.push(rawSection(entity));
    }
    replaceKids(body, kids.filter(Boolean));
    renderActions(node, entity);
    restoreFocus(root, key);
  }

  function renderActions(node, entity) {
    if (!cur) return;
    const id = cur.id;
    const type = node?.type || entity?.type;
    const attrs = node?.attrs || {};
    const f = entity?.fields || {};
    const labelText = node?.label || entity?.label || bareId(id);
    const vehicle = actionVehicle(type, id, attrs, f);
    const kids = [
      button('Ask about this', {
        icon: ICON.ask,
        key: 'act:ask',
        onClick: () =>
          emit('ask', {
            text: `Tell me about ${entityMarkup(id, labelText)}`,
            focused_ids: [id],
            draft: true,
          }),
      }),
      button('Focus', {
        icon: ICON.focus,
        key: 'act:focus',
        onClick: () => focusEntity(id, entity),
      }),
    ];
    // Places (WG §4.2.5, §4.2.6): Show on map and a drafted recce. A site is
    // context only, so it never gets any other action.
    if (isPlaceType(type)) {
      const req = placeMapRequest(id, node, entity, state().graph);
      if (req)
        kids.push(
          button('Show on map', {
            icon: ICON.map,
            key: 'act:map',
            onClick: () => emit('map:request', req),
          }),
        );
      kids.push(
        button('Plan recce over this', {
          icon: ICON.recce,
          key: 'act:recce',
          onClick: () =>
            emit('ask', {
              text: recceText(id, labelText),
              focused_ids: [id],
              draft: true,
            }),
        }),
      );
    }
    if (vehicle && (type === 'vehicle' || type === 'mission')) {
      kids.push(
        button('Track', {
          icon: ICON.track,
          key: 'act:track',
          label: `Track ${vehicle}`,
          onClick: () => emit('track:request', { vehicle, source: 'operator' }),
        }),
      );
    }
    // A vehicle in the picture: its live node decides (the entity may name a
    // mission that has ended since it was fetched).
    const liveMission = node ? attrs.mission : f.mission;
    const hasMission = type === 'mission' || Boolean(liveMission);
    if (vehicle && hasMission) {
      kids.push(
        button('Open mission panel', {
          icon: ICON.open,
          key: 'act:mission',
          onClick: () =>
            emit('track:request', {
              vehicle,
              source: 'operator',
              reason: 'Open mission panel',
              openMissionPanel: true,
            }),
        }),
      );
    }
    if (type === 'vehicle' && canAbort({ ...attrs, mission: liveMission })) {
      kids.push(
        button('Abort', {
          icon: ICON.abort,
          cls: 'ic-kit-btn--danger',
          key: 'act:abort',
          label: `Abort ${vehicle}`,
          onClick: (event) =>
            confirmAbort(ctx, vehicle, {
              now,
              anchor: event?.currentTarget,
            }),
        }),
      );
    }
    kids.push(
      button('Close', {
        icon: ICON.close,
        cls: 'ic-kit-btn--quiet',
        key: 'act:close',
        onClick: () => hide(),
      }),
    );
    replaceKids(actions, kids);
  }

  function renderAll() {
    renderHead();
    renderNotice();
    renderStatus();
    renderBody();
  }

  // ---- behaviour ------------------------------------------------------------------

  function focusEntity(id, entity) {
    const ids = [id];
    for (const r of Array.isArray(entity?.related) ? entity.related : []) {
      if (ids.length >= MAX_FOCUS_IDS) break;
      if (r?.id && !ids.includes(r.id) && nodeFor(r.id)) ids.push(r.id);
    }
    emit('focus:entities', { ids, by: 'operator' });
  }

  function copyId(id) {
    if (!id) return;
    const clip = globalThis.navigator?.clipboard;
    const done = (ok) =>
      replaceKids(live, [ok ? `Copied ${id}` : "Couldn't copy the id"]);
    if (typeof clip?.writeText === 'function') {
      Promise.resolve()
        .then(() => clip.writeText(id))
        .then(
          () => done(true),
          () => done(false),
        );
    } else done(false);
  }

  function markViewedIfAlarm(id, node) {
    if (!String(id).startsWith('alarm:') && node?.type !== 'alarm') return;
    // The store emits `alarm:viewed` itself; emit only when there is no store.
    if (typeof store?.markAlarmViewed === 'function') store.markAlarmViewed(id);
    else emit('alarm:viewed', { id });
  }

  async function load(id) {
    const mine = ++loadSeq;
    if (!cur || cur.id !== id) return;
    cur.status = 'loading';
    cur.error = null;
    renderNotice();
    renderStatus();
    let entity = null;
    let error = null;
    try {
      if (typeof store?.entity === 'function') entity = await store.entity(id);
      else if (typeof ctx?.api?.get === 'function') {
        entity = await ctx.api.get(`/intel/entity/${encodeURIComponent(id)}`);
      } else throw new Error('no intel source');
    } catch (err) {
      error = err;
    }
    if (destroyed || mine !== loadSeq || !cur || cur.id !== id) return;
    const notFound =
      error &&
      (error.status === 404 ||
        /404|not found/i.test(String(error.message || '')));
    if (error && !notFound) {
      cur.status = 'error';
      cur.error = bareMessage(error.message || error, 'unknown error');
    } else if (!entity || notFound) {
      cur.status = 'gone';
    } else {
      cur.status = 'ready';
      cur.entity = entity;
      if (entity.id && entity.id !== id && !nodeFor(id)) {
        cur.id = entity.id;
      }
    }
    renderAll();
    const node = nodeFor(cur.id);
    replaceKids(live, [
      cur.status === 'ready'
        ? `${(node && displayLabel(node)) || cur.entity?.label || cur.id} details loaded`
        : cur.status === 'gone'
          ? 'This entity left the picture.'
          : `Couldn't load details for ${cur.id}.`,
    ]);
  }

  function show(id, { keepHistory = true } = {}) {
    if (destroyed || !id) return;
    const key = String(id);
    if (cur && cur.id === key) {
      setHidden(root, false);
      return;
    }
    if (cur && keepHistory) {
      const pnode = nodeFor(cur.id);
      previous = {
        id: cur.id,
        label: pnode?.label || cur.entity?.label || bareId(cur.id),
      };
    } else if (!keepHistory) previous = null;
    const node = nodeFor(key);
    cur = {
      id: key,
      entity: null,
      status: 'loading',
      error: null,
      rawOpen: false,
      lastSeen: num(node?.ts_ms),
      seenInGraph: Boolean(node),
      nodePrint: node ? JSON.stringify(node) : null,
    };
    setHidden(root, false);
    renderAll();
    markViewedIfAlarm(key, node);
    emit('inspector:state', { open: true, id: key });
    load(key);
  }

  function goBack() {
    if (!previous) return;
    const target = previous.id;
    previous = null;
    show(target, { keepHistory: false });
    head.focus?.();
  }

  function hide() {
    if (!cur) {
      setHidden(root, true);
      return;
    }
    const was = cur.id;
    cur = null;
    previous = null;
    loadSeq += 1;
    setHidden(root, true);
    replaceKids(head, []);
    replaceKids(body, []);
    replaceKids(actions, []);
    replaceKids(notice, []);
    setHidden(notice, true);
    emit('inspector:state', { open: false, id: was });
  }

  function setLayout(next) {
    const value = ['wide', 'compact', 'narrow'].includes(next) ? next : 'wide';
    if (value === layout) return;
    layout = value;
    root.setAttribute('data-layout', layout);
    if (cur) renderHead();
  }

  root.addEventListener('keydown', (event) => {
    if (event?.key !== 'Escape' || !cur) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    hide();
  });

  if (store?.on) {
    const off = store.on('change', () => {
      if (!cur) return;
      const node = nodeFor(cur.id);
      if (node) {
        cur.lastSeen = num(node.ts_ms) ?? cur.lastSeen;
        cur.seenInGraph = true;
      }
      renderHead();
      renderNotice();
      // Readings in the body (fuel, link, phase, progress) follow the graph
      // live; the body is rebuilt only when this node actually changed.
      const print = node ? JSON.stringify(node) : null;
      if (print !== cur.nodePrint) {
        cur.nodePrint = print;
        if (cur.status !== 'loading') renderBody();
      }
    });
    if (typeof off === 'function') offs.push(off);
  }
  if (bus?.on) {
    for (const [event, fn] of [
      [
        'inspect',
        (p) => {
          if (!p?.id) return;
          // The analyst never covers the chat (§4.6): at compact and narrow
          // the shell selects the node and offers "Inspect" instead.
          if (p.by === 'analyst' && layout !== 'wide') return;
          show(p.id);
        },
      ],
      ['layout', (p) => setLayout(p?.layout)],
      [
        'approval:pending',
        (p) => {
          const n = Number(p?.count);
          pending = {
            count: Number.isFinite(n) && n > 0 ? n : 0,
            oldest: p?.oldest || null,
          };
          if (cur) renderHead();
        },
      ],
    ]) {
      const off = bus.on(event, fn);
      if (typeof off === 'function') offs.push(off);
    }
  }

  host?.append?.(root);

  return {
    element: root,
    show: (id) => show(id),
    hide,
    setLayout,
    current: () => cur?.id ?? null,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      loadSeq += 1;
      for (const off of offs.splice(0)) off();
      root.remove?.();
    },
  };
}
