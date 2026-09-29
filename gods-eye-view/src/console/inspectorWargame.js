/**
 * Inspector bodies for the simulated wargame (WG §5.3.4, §5.3.6, §5.3.9;
 * M14a): a scenario force, a simulated engagement and a wargame vector.
 *
 * Everything shown is a simulation. A force is a scenario unit placed by the
 * wargame, never an observation; an engagement is a notional adjudication;
 * a vector is computed by the wargame. Each body ends with its fixed line.
 * No row names a real place ("Near" is deliberately absent), and every label
 * is untrusted text: bidi-stripped, and rendered only as text.
 *
 * inspector.js owns the plate and hands its builders in as `kit` (as for
 * inspectorPlaces.js): `{field, fields, mono, caption, chip, chips, section,
 * now}`. Words come from railWargame.js, formatting from situation.js.
 */
import { h } from '../ui/uavDom.js';
import { safeText } from './orb/placeText.js';
import {
  BDA_WORD,
  CONSEQUENCE_WORD,
  ENGAGEMENT_FIXED_LINE,
  FORCE_FIXED_LINE,
  OUTCOME_HIDDEN_TEXT,
  VECTOR_FIXED_LINE,
  attackerText,
  bdaAgrees,
  bdaFacts,
  engagementPhaseWord,
  engagementTitle,
  forceStateOf,
  forceStateWord,
  outcomeWord,
  pNotional,
  sideOf,
  sideWord,
  simulatedTag,
  strikePrefill,
  targetText,
  wargameTone,
} from './railWargame.js';
import {
  bareId,
  duration,
  humanize,
  num,
  registerTag,
  segments,
  showOnMapRequest,
  simTimeSuffix,
  zulu,
} from './situation.js';

/** Altitude band words (§3.9). */
export const ALT_BAND_WORD = Object.freeze({
  surface: 'Surface',
  low: 'Low',
  medium: 'Medium',
});
/** Leg exposure words (§3.9). */
export const EXPOSURE_WORD = Object.freeze({
  low: 'Low',
  moderate: 'Moderate',
  high: 'High',
});
export const NOT_SENSED_TEXT = 'Not seen by any sensor yet';

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/** A node's facts: the live graph attrs over the entity's fields. */
export function factsOf(node, entity) {
  const f =
    entity?.fields && typeof entity.fields === 'object' ? entity.fields : {};
  return { ...f, ...(node?.attrs || {}) };
}

/** "2 km", "12.5 km" from metres; '' when unknown. */
export function kmText(metres) {
  const m = num(metres);
  return m == null ? '' : `${Number((m / 1000).toFixed(1))} km`;
}

/** A graph node's label for a chip or a title, bidi-safe; else its bare id; `fallback` without an id. */
export function labelOf(graph, id, fallback = '') {
  if (typeof id !== 'string' || !id) return fallback;
  const node = (Array.isArray(graph?.nodes) ? graph.nodes : []).find(
    (n) => n?.id === id,
  );
  return safeText(node?.label, 80) || safeText(bareId(id), 80) || fallback;
}

const REF_TYPE = Object.freeze({
  trk: 'track',
  veh: 'vehicle',
  frc: 'force',
  eng: 'engagement',
  vec: 'vector',
});

/** The node type an id's prefix names ("trk:…" -> "track"), for a chip's glyph. */
export function refType(id) {
  const s = String(id ?? '');
  const i = s.indexOf(':');
  return i > 0 && Object.hasOwn(REF_TYPE, s.slice(0, i))
    ? REF_TYPE[s.slice(0, i)]
    : undefined;
}

/** "Red axis" / "Planned corridor" (a kind the console doesn't know: "Vector"). */
export function vectorKindWord(kind) {
  if (kind === 'axis') return 'Red axis';
  if (kind === 'corridor') return 'Planned corridor';
  return 'Vector';
}

/** "Red axis from Red armour 1 to Blue depot 1" (§5.3.6 vector header). */
export function vectorTitle(node, entity, graph) {
  const a = factsOf(node, entity);
  const from = labelOf(graph, a.from, 'an unknown start');
  const to = a.to ? labelOf(graph, a.to) : 'a planned point';
  return `${vectorKindWord(a.kind)} from ${from} to ${to}`;
}

/** The header title of a wargame node (§5.3.4, §5.3.6), or null for others. */
export function wargameTitle(type, node, entity, graph) {
  if (type === 'force')
    return safeText(node?.label || entity?.label, 80) || null;
  if (type === 'engagement')
    return engagementTitle({ attrs: factsOf(node, entity) });
  if (type === 'vector') return vectorTitle(node, entity, graph);
  return null;
}

/**
 * The `map:request` for Show on map over a wargame node: a force or an
 * engagement (at its target) as a point, a vector as the box around its
 * ends padded 15 %. Null without a position.
 */
export function wargameMapRequest(id, node, entity) {
  const a = factsOf(node, entity);
  const label = safeText(node?.label || entity?.label, 80);
  const lat = num(node?.lat ?? a.lat);
  const lon = num(node?.lon ?? a.lon);
  const to = Array.isArray(a.to_point) ? a.to_point.map((v) => num(v)) : [];
  if (node?.type === 'vector' || entity?.type === 'vector') {
    const pts = [
      [lat, lon],
      [to[0], to[1]],
    ].filter(([y, x]) => y != null && x != null);
    if (pts.length === 2) {
      const ys = pts.map((p) => p[0]);
      const xs = pts.map((p) => p[1]);
      const minPad = 500 / 111_320;
      const padY = Math.max((Math.max(...ys) - Math.min(...ys)) * 0.15, minPad);
      const padX = Math.max((Math.max(...xs) - Math.min(...xs)) * 0.15, minPad);
      return showOnMapRequest(id, {
        bbox: [
          Math.min(...ys) - padY,
          Math.min(...xs) - padX,
          Math.max(...ys) + padY,
          Math.max(...xs) + padX,
        ],
        label,
      });
    }
    if (pts.length === 1)
      return showOnMapRequest(id, { lat: pts[0][0], lon: pts[0][1], label });
    return null;
  }
  return showOnMapRequest(id, { lat, lon, label });
}

/** The fixed line a wargame body ends with. */
export function fixedLineOf(type) {
  return (
    {
      force: FORCE_FIXED_LINE,
      engagement: ENGAGEMENT_FIXED_LINE,
      vector: VECTOR_FIXED_LINE,
    }[type] || null
  );
}

/**
 * Words for a wargame header after the type word (§5.3.4, §5.3.6): the side
 * word for a force; the status is the state or phase word with a Scenario
 * or Simulated tag.
 * @returns {{sideWord:string|null, status:string, tone:string, tag:object}}
 */
export function wargameHeadWords(type, node, entity) {
  const a = factsOf(node, entity);
  const shaped = { ...(node || {}), type, attrs: a };
  if (type === 'force')
    return {
      sideWord: sideWord(sideOf(shaped)),
      status: forceStateWord(forceStateOf(shaped)),
      tone: wargameTone(shaped),
      tag: registerTag('scenario'),
    };
  return {
    sideWord: null,
    status:
      type === 'engagement'
        ? engagementPhaseWord(a.phase)
        : { warn: 'Warning', critical: 'Critical' }[node?.status] || '',
    tone: wargameTone(shaped),
    tag: simulatedTag(),
  };
}

export { strikePrefill };

// ---------------------------------------------------------------------------
// Bodies
// ---------------------------------------------------------------------------

/** Caveats as a list, each with the Assumed tag; null when none. */
function caveatRows(kit, list) {
  const items = (Array.isArray(list) ? list : [])
    .map((c) => safeText(c, 240))
    .filter(Boolean)
    .slice(0, 8);
  if (!items.length) return null;
  return kit.field(
    'Caveats',
    h(
      'ul',
      { class: 'ic-kit-list' },
      ...items.map((c) => h('li', {}, c, ' ', registerTag('assumed', c))),
    ),
  );
}

function fixed(text) {
  return h('p', { class: 'ic-inspector__line ic-inspector__wg-fixed' }, text);
}

/**
 * A scenario force (§5.3.4): Side, Kind, State (+ until), Ammunition,
 * Threat range, Detection range, Strike range (blue), Movement, Sensed as
 * (Umpire view, red), Caveats; then the fixed line. No "Near" row.
 * @param {{truth?: boolean}} [view] whether the graph shows the umpire's truth
 */
export function forceBody(kit, node, entity, graph, { truth = false } = {}) {
  const a = factsOf(node, entity);
  const shaped = { ...(node || {}), type: 'force', attrs: a };
  const side = sideOf(shaped);
  const state = forceStateOf(shaped);
  const rows = [
    kit.field(
      'Side',
      h(
        'span',
        { class: 'ic-inspector__wg-side', 'data-side': side || 'unknown' },
        sideWord(side),
      ),
    ),
  ];
  const kind = safeText(a.kind_label, 80);
  if (kind) rows.push(kit.field('Kind', kind));
  const until = num(a.state_until_ms);
  rows.push(
    kit.field(
      'State',
      h(
        'span',
        {
          class: 'ic-inspector__status',
          'data-tone': wargameTone(shaped),
          'data-state': state || 'unknown',
        },
        forceStateWord(state),
      ),
      until != null ? ` until ${zulu(until)}` : null,
    ),
  );
  const ammo = num(a.ammo);
  if (ammo != null) rows.push(kit.field('Ammunition', `${ammo} left`));
  const threat = num(a.threat_range_m);
  if (threat) {
    const ceiling = num(a.threat_ceiling_m);
    rows.push(
      kit.field(
        'Threat range',
        ceiling != null
          ? `${kmText(threat)}, up to ${Math.round(ceiling)} m above the unit`
          : kmText(threat),
        registerTag('scenario'),
      ),
    );
  }
  const detect = num(a.detection_range_m);
  if (detect)
    rows.push(
      kit.field('Detection range', kmText(detect), registerTag('scenario')),
    );
  if (side === 'blue') {
    const strike = num(a.strike_range_m);
    if (strike)
      rows.push(
        kit.field('Strike range', kmText(strike), registerTag('scenario')),
      );
    else if (a.wg_class === 'blue_strike_air')
      rows.push(
        kit.field('Strike range', 'The whole AO', registerTag('scenario')),
      );
  }
  const objective =
    typeof a.objective === 'string' && a.objective ? a.objective : null;
  rows.push(
    kit.field(
      'Movement',
      objective
        ? [
            'Moves toward ',
            kit.chip(objective, labelOf(graph, objective), 'force'),
          ]
        : 'Static',
    ),
  );
  if (side === 'red' && truth) {
    const ids = (Array.isArray(a.correlated) ? a.correlated : []).filter(
      (id) => typeof id === 'string' && id,
    );
    rows.push(
      kit.field(
        'Sensed as',
        ids.length
          ? kit.chips(
              ids.map((id) => {
                const ref = id.startsWith('trk:') ? id : `trk:${id}`;
                return kit.chip(ref, null, 'track');
              }),
            )
          : NOT_SENSED_TEXT,
      ),
    );
  }
  const cav = caveatRows(kit, a.caveats);
  if (cav) rows.push(cav);
  return [kit.fields(...rows), fixed(FORCE_FIXED_LINE)];
}

/** §5.3.9 battle damage: the word, "{n} looks, last {Z}", and an umpire mismatch. */
function bdaValue(a, { truth }) {
  const b = bdaFacts(a.bda);
  const kids = [
    h(
      'span',
      {
        class:
          b.state === 'none' ? 'ic-kit-notassessed' : 'ic-inspector__status',
        'data-tone': b.tone,
        'data-bda': b.state || 'unknown',
      },
      b.state === 'none' ? BDA_WORD.none : b.word,
    ),
    ` ${b.looks} ${b.looks === 1 ? 'look' : 'looks'}${
      b.lastMs != null ? `, last ${zulu(b.lastMs)}` : ''
    }`,
  ];
  if (truth && a.outcome_hidden !== true && !bdaAgrees(a.outcome, b.state))
    kids.push(
      h(
        'span',
        { class: 'ic-inspector__line ic-inspector__wg-mismatch' },
        `Umpire outcome: ${outcomeWord(a.outcome).toLowerCase()}. Sensors haven't confirmed it.`,
      ),
    );
  return kids;
}

/** "Approved by you at 14:02Z" / "Denied by you", or null. */
function approvalText(a) {
  if (a.phase === 'denied') return 'Denied by you';
  if (!a.approval_id && a.phase !== 'authorized') return null;
  const at = num(a.authorized_at_ms ?? a.approved_at_ms);
  return at != null ? `Approved by you at ${zulu(at)}` : 'Approved by you';
}

/**
 * A simulated engagement (§5.3.6): Attacker, Target, Outcome, Consequence,
 * Chance of effect, Inputs, Battle damage (blue strikes), Vector, Approval,
 * Replay and times in Z; then "Simulated. Nothing real was fired."
 * @param {{truth?: boolean}} [view] whether the graph shows the umpire's truth
 */
export function engagementBody(
  kit,
  node,
  entity,
  graph,
  { truth = false } = {},
) {
  const a = factsOf(node, entity);
  const rows = [];
  const attacker =
    typeof a.attacker === 'string' && a.attacker ? a.attacker : null;
  rows.push(
    kit.field(
      'Attacker',
      attacker
        ? kit.chip(attacker, safeText(a.attacker_label, 80) || null, 'force')
        : attackerText({ attrs: a }),
      registerTag('scenario'),
    ),
  );
  const target = typeof a.target === 'string' && a.target ? a.target : null;
  rows.push(
    kit.field(
      'Target',
      target
        ? kit.chip(target, targetText({ attrs: a }), refType(target))
        : targetText({ attrs: a }),
      target?.startsWith('trk:') ? registerTag('measured') : null,
    ),
  );
  let outcome;
  if (a.outcome_hidden === true) outcome = OUTCOME_HIDDEN_TEXT;
  else if (a.outcome != null) outcome = `${outcomeWord(a.outcome)} (simulated)`;
  else outcome = 'No outcome yet';
  rows.push(kit.field('Outcome', outcome, registerTag('scenario')));
  if (a.consequence != null)
    rows.push(
      kit.field(
        'Consequence',
        h(
          'span',
          {
            class: 'ic-inspector__status',
            'data-tone': wargameTone({ type: 'engagement', attrs: a }),
          },
          Object.hasOwn(CONSEQUENCE_WORD, a.consequence)
            ? CONSEQUENCE_WORD[a.consequence]
            : humanize(a.consequence),
        ),
      ),
    );
  const p = pNotional(a.p_notional);
  if (p != null)
    rows.push(
      kit.field(
        'Chance of effect',
        `≈ ${p.toFixed(2)}, notional`,
        registerTag('estimated'),
      ),
    );
  const inputs = (Array.isArray(a.inputs) ? a.inputs : [])
    .map((x) => safeText(x, 160))
    .filter(Boolean);
  if (inputs.length)
    rows.push(
      kit.field(
        'Inputs',
        h('ul', { class: 'ic-kit-list' }, ...inputs.map((x) => h('li', {}, x))),
        registerTag('estimated'),
      ),
    );
  if (a.kind === 'blue_strike' || a.bda)
    rows.push(kit.field('Battle damage', ...bdaValue(a, { truth })));
  if (typeof a.vector === 'string' && a.vector)
    rows.push(
      kit.field(
        'Vector',
        kit.chip(a.vector, labelOf(graph, a.vector), 'vector'),
      ),
    );
  const approval = approvalText(a);
  if (approval) rows.push(kit.field('Approval', approval));
  const replay = [
    num(a.seed) != null ? `Seed ${a.seed}` : '',
    a.engine ? `Engine ${safeText(a.engine, 40)}` : '',
    num(a.draw) != null ? `Draw ${a.draw}` : '',
  ].filter(Boolean);
  if (replay.length)
    rows.push(
      kit.field(
        'Replay',
        h('span', { class: 'ic-kit-mono' }, segments(replay.join(' · '))),
      ),
    );
  const proposed = num(a.proposed_at_ms);
  if (proposed != null)
    rows.push(kit.field('Proposed', zulu(proposed, { seconds: true })));
  const done = num(a.adjudicated_at_ms);
  if (done != null)
    rows.push(kit.field('Adjudicated', zulu(done, { seconds: true })));
  return [kit.fields(...rows), fixed(ENGAGEMENT_FIXED_LINE)];
}

/** The legs of a vector as a small table: exposure word, seconds exposed, length. */
function legsTable(legs) {
  const list = (Array.isArray(legs) ? legs : []).filter(
    (l) => l && typeof l === 'object',
  );
  if (!list.length) return null;
  const cell = (tag, text) => h(tag, { class: 'ic-inspector__legcell' }, text);
  return h(
    'table',
    { class: 'ic-inspector__legs' },
    h(
      'thead',
      {},
      h(
        'tr',
        {},
        cell('th', 'Leg'),
        cell('th', 'Exposure'),
        cell('th', 'Exposed'),
        cell('th', 'Length'),
      ),
    ),
    h(
      'tbody',
      {},
      ...list
        .slice(0, 40)
        .map((l, i) =>
          h(
            'tr',
            { 'data-exposure': l.exposure || 'unknown' },
            cell('td', String(i + 1)),
            cell(
              'td',
              Object.hasOwn(EXPOSURE_WORD, l.exposure ?? '')
                ? EXPOSURE_WORD[l.exposure]
                : 'Exposure not recognised',
            ),
            cell(
              'td',
              num(l.exposure_s) != null
                ? `≈ ${Math.round(l.exposure_s)} s`
                : '',
            ),
            cell('td', kmText(l.length_m)),
          ),
        ),
    ),
  );
}

/** "≈ 118 s less exposure, 1400 m longer" against the straight route. */
export function straightText(deltaExposureS, deltaLengthM) {
  const ds = num(deltaExposureS);
  const dm = num(deltaLengthM);
  if (ds == null && dm == null) return '';
  const parts = [];
  if (ds != null)
    parts.push(
      ds >= 0
        ? `≈ ${Math.round(ds)} s less exposure`
        : `≈ ${Math.round(-ds)} s more exposure`,
    );
  if (dm != null)
    parts.push(
      dm >= 0 ? `${Math.round(dm)} m longer` : `${Math.round(-dm)} m shorter`,
    );
  return parts.join(', ');
}

/**
 * A wargame vector (§5.3.6): Kind, Side, From and To, Bearing, Length,
 * Altitude band, Width, Speed, ETA, Exposure, Against a straight route,
 * Legs, Threat basis and Caveats; then "Simulated. Computed by the wargame."
 * @param {{scale?: number|null}} [opts] the sim speed when it isn't ×1
 */
export function vectorBody(kit, node, entity, graph, { scale = null } = {}) {
  const a = factsOf(node, entity);
  const rows = [
    kit.field('Kind', vectorKindWord(a.kind)),
    kit.field(
      'Side',
      sideWord(a.side === 'red' || a.side === 'blue' ? a.side : null),
    ),
  ];
  if (typeof a.from === 'string' && a.from)
    rows.push(
      kit.field(
        'From',
        kit.chip(a.from, labelOf(graph, a.from), refType(a.from)),
      ),
    );
  if (typeof a.to === 'string' && a.to)
    rows.push(
      kit.field('To', kit.chip(a.to, labelOf(graph, a.to), refType(a.to))),
    );
  else if (Array.isArray(a.to_point) && a.to_point.length === 2)
    rows.push(
      kit.field(
        'To',
        kit.mono(a.to_point.map((v) => (num(v) ?? 0).toFixed(5)).join(', ')),
      ),
    );
  const bearing = num(a.bearing_deg);
  if (bearing != null)
    rows.push(kit.field('Bearing', `${Math.round(bearing)}°`));
  if (num(a.length_m) != null)
    rows.push(kit.field('Length', kmText(a.length_m)));
  if (Object.hasOwn(ALT_BAND_WORD, a.alt_band ?? ''))
    rows.push(kit.field('Altitude band', ALT_BAND_WORD[a.alt_band]));
  if (num(a.corridor_m) != null)
    rows.push(kit.field('Width', `${Math.round(a.corridor_m)} m`));
  if (num(a.speed_mps) != null)
    rows.push(kit.field('Speed', `${Number(num(a.speed_mps).toFixed(1))} m/s`));
  const eta = num(a.eta_s);
  if (eta != null)
    rows.push(
      kit.field(
        'ETA',
        `≈ ${duration(eta)}${simTimeSuffix(eta, scale)}`,
        registerTag('estimated'),
      ),
    );
  const exp = num(a.exposure_s);
  const ps = num(a.p_survive);
  if (exp != null || ps != null)
    rows.push(
      kit.field(
        'Exposure',
        [
          exp != null ? `≈ ${Math.round(exp)} s exposed` : '',
          ps != null ? `survival ≈ ${ps.toFixed(2)}` : '',
        ]
          .filter(Boolean)
          .join('; '),
        registerTag('estimated'),
      ),
    );
  const straight = straightText(a.delta_exposure_s, a.delta_length_m);
  if (straight)
    rows.push(
      kit.field('Against a straight route', straight, registerTag('estimated')),
    );
  const legs = legsTable(a.legs);
  if (legs) rows.push(kit.field('Legs', legs));
  if (a.threat_basis === 'sensed')
    rows.push(
      kit.field(
        'Threat basis',
        'Planned against sensed contacts',
        registerTag('scenario'),
      ),
    );
  else if (a.threat_basis === 'truth')
    rows.push(
      kit.field(
        'Threat basis',
        'Planned against scenario truth',
        registerTag(
          'assumed',
          'The plan used the scenario, not what sensors saw.',
        ),
      ),
    );
  const cav = caveatRows(kit, a.caveats);
  if (cav) rows.push(cav);
  return [kit.fields(...rows), fixed(VECTOR_FIXED_LINE)];
}
