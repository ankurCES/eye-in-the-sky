/**
 * @module uav/contextPolicy
 * @description Tunables and pure helpers for the map's context overlay
 * (WG v2 §4.2.7, §3.3): the second Cesium data source that draws the mapped
 * sites around the active theater, plus the theater-epoch watch that resets
 * trails and refetches overlays after a runtime theater switch (§4.2.8).
 *
 * Pure: no Cesium, no DOM. `contextEntities.js` turns these numbers into
 * Cesium objects; `contextOverlay.js` owns the fetch and the data source.
 *
 * Context is never a target. Nothing here, or in the modules that use it,
 * originates a command: the overlay is read-only map furniture.
 */

/** @constant {string} Name of the context data source (never the snapshot one). */
export const CONTEXT_DATA_SOURCE = 'uav-context-overlay';

/** @constant {string} Entity id prefix for a context feature. */
export const CONTEXT_PREFIX = 'uav-context:';

/** @constant {number} Poll period (ms) while the overlay is visible. */
export const CONTEXT_POLL_MS = 3000;

/** @constant {number} Backoff (ms) after a failed overlay fetch. */
export const CONTEXT_ERROR_BACKOFF_MS = 5000;

/** @constant {number} At most this many site billboards are drawn. */
export const MAX_BILLBOARDS = 150;

/** @constant {number} At most this many unrecognised-kind points are drawn. */
export const MAX_UNKNOWN_POINTS = 60;

/** @constant {number} Billboard size on screen (CSS px). */
export const ICON_PX = 32;

/** @constant {number} Canvas backing scale for the cached icons (crisp at 2x). */
export const ICON_SCALE = 2;

/** @constant {number} The slate plate behind a glyph is drawn at 80 % alpha. */
export const ICON_PLATE_ALPHA = 0.8;

/** @constant {string} Plate colour (the console's slate). */
export const SLATE = '#1B2630';

/** @constant {string} Glyph stroke: sites are always Pencil (§4.2.6). */
export const PENCIL = '#A3B0BA';

/** @constant {string} An unrecognised map item is a grey point (§4.2.1). */
export const UNKNOWN_GREY = '#8A98A2';

/** @constant {number} Glyph stroke width, in the 24-unit glyph box. */
export const GLYPH_STROKE = 2;

/**
 * @constant {number} Billboards ignore the depth test within this range (m).
 *
 * The spec's 5 km (§4.2.7) was shorter than the map's own framing: showArea
 * puts the camera about 29 km from a 5.9 km AO and about 250 km from the
 * largest (50 km) one, and past this range the globe's depth hides every
 * CLAMP_TO_GROUND billboard (E2E A2, A14). 300 km covers every AO framing and
 * still lets the globe occlude sites from the far side at globe scale.
 */
export const DEPTH_TEST_DISTANCE_M = 300_000;

/** @constant {number[]} `NearFarScalar(2e3, 1, 5e4, .5)` for the billboards. */
export const SCALE_BY_DISTANCE = Object.freeze([2e3, 1, 5e4, 0.5]);

/** @constant {number} Labels show within this camera range (m). */
export const LABEL_RANGE_M = 15000;

/** @constant {number} Protected (medical) labels show within this range (m). */
export const PROTECTED_LABEL_RANGE_M = 30000;

/** @constant {number} Site labels are cut to this many characters. */
export const LABEL_MAX_CHARS = 80;

/** @constant {string} The fixed label of a kind the map does not know. */
export const UNRECOGNISED_MAP_ITEM = 'Unrecognised map item';

/** @constant {string} Second label line for a protected site. */
export const PROTECTED_LINE = 'Protected';

/**
 * The per-kind switches `setOverlayVisibility` takes, and the overlay kinds
 * each one governs. `forces` governs a force and its envelopes (WG v2 §5.3.12).
 */
export const VISIBILITY_KINDS = Object.freeze({
  sites: Object.freeze(['site']),
  forces: Object.freeze(['force', 'force_envelope']),
  engagements: Object.freeze(['engagement']),
  vectors: Object.freeze(['vector']),
});

/** @constant {object} Everything shows until the console says otherwise. */
export const DEFAULT_VISIBILITY = Object.freeze({
  sites: true,
  forces: true,
  engagements: true,
  vectors: true,
});

/**
 * The simulated wargame's overlay kinds (WG v2 §3.3, §5.3.12). They draw
 * with their own style only when the feature says it is a simulated scenario
 * item (`register: "scenario"`, `simulated: true`); anything else claiming one
 * of these kinds is shown as an unrecognised item, never framed (D1).
 */
export const WARGAME_KINDS = Object.freeze([
  'force',
  'force_envelope',
  'vector',
  'engagement',
]);

/** Overlay kinds this build draws with their own style. */
export const KNOWN_KINDS = Object.freeze(['site', ...WARGAME_KINDS]);

const KIND_SWITCH = new Map(
  Object.entries(VISIBILITY_KINDS).flatMap(([name, kinds]) =>
    kinds.map((kind) => [kind, name]),
  ),
);

/**
 * The visibility switch that governs an overlay kind, or null for a kind no
 * switch names (an unrecognised kind is never hidden by a switch: the
 * fail-safe is to show it, labelled as unknown).
 * @param {string} kind Feature `properties.kind`.
 * @returns {string|null} `sites` | `forces` | `engagements` | `vectors` | null.
 */
export function visibilitySwitch(kind) {
  return KIND_SWITCH.get(String(kind ?? '')) ?? null;
}

/**
 * Merge a partial `{sites, forces, engagements, vectors}` into the current
 * switches. Unknown keys and non-boolean values are ignored.
 * @param {object} current Current switches.
 * @param {object} [next] Requested switches.
 * @returns {object} The merged switches (a new object).
 */
export function mergeVisibility(current, next = {}) {
  const merged = { ...DEFAULT_VISIBILITY, ...current };
  if (!next || typeof next !== 'object') return merged;
  for (const key of Object.keys(DEFAULT_VISIBILITY)) {
    if (typeof next[key] === 'boolean') merged[key] = next[key];
  }
  return merged;
}

/**
 * Whether a feature of `kind` shows under `visibility`.
 * @param {string} kind Feature kind.
 * @param {object} visibility Current switches.
 * @returns {boolean} Shown.
 */
export function kindVisible(kind, visibility) {
  const name = visibilitySwitch(kind);
  return name === null ? true : visibility?.[name] !== false;
}

/**
 * The `/intel/overlay` URL for one request.
 * @param {string} baseUrl Bridge origin ('' is the page's own origin).
 * @param {{truth?: boolean, rev?: string|null}} [query] Query values.
 * @returns {string} Request URL.
 */
export function contextOverlayUrl(baseUrl, { truth = false, rev = null } = {}) {
  const base = String(baseUrl ?? '').replace(/\/+$/, '');
  const params = [`truth=${truth ? 1 : 0}`];
  if (typeof rev === 'string' && rev) {
    params.push(`rev=${encodeURIComponent(rev)}`);
  }
  return `${base}/intel/overlay?${params.join('&')}`;
}

// ---- theater epoch watch (§4.2.8, A10 note) ---------------------------------

/**
 * Read a `/snapshot.theater` block (`{id, epoch}`), or null when it carries
 * neither. The epoch counts only as a plain integer: the bridge publishes None
 * for a bool or string, and an older server never sends one.
 * @param {unknown} raw Snapshot `theater` value.
 * @returns {{id: string|null, epoch: number|null}|null} Reference.
 */
export function readTheaterRef(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = typeof raw.id === 'string' && raw.id.trim() ? raw.id.trim() : null;
  const epoch = Number.isInteger(raw.epoch) ? raw.epoch : null;
  if (id === null && epoch === null) return null;
  return { id, epoch };
}

/**
 * Watch the running theater and say when it CHANGED (§4.2.8).
 *
 * `/snapshot.theater.epoch` can be null for one bridge poll after a switch
 * (the geofence is being re-read), and is always null from an older server.
 * So a change is an id change, or a change between two non-null epochs, and a
 * null never erases what we knew. One switch therefore gives exactly one
 * change, whichever order the id and the epoch arrive in. The first reference
 * seen is the baseline, not a change.
 * @returns {{observe: Function, current: Function, reset: Function}} Watcher.
 */
export function createTheaterWatcher() {
  let last = null;
  return {
    /**
     * @param {unknown} raw Snapshot `theater` value.
     * @returns {{from: object, to: object}|null} The change, or null.
     */
    observe(raw) {
      const ref = readTheaterRef(raw);
      if (!ref) return null;
      const prev = last;
      if (!prev) {
        last = ref;
        return null;
      }
      const idChanged =
        ref.id !== null && prev.id !== null && ref.id !== prev.id;
      const epochChanged =
        ref.epoch !== null && prev.epoch !== null && ref.epoch !== prev.epoch;
      if (idChanged || epochChanged) {
        // An id change with the epoch still pending records the null, so the
        // epoch that follows is not read as a second switch.
        last = { id: ref.id ?? prev.id, epoch: ref.epoch };
        return { from: prev, to: { ...last } };
      }
      last = { id: ref.id ?? prev.id, epoch: ref.epoch ?? prev.epoch };
      return null;
    },
    /** @returns {{id: string|null, epoch: number|null}|null} Last known. */
    current: () => (last ? { ...last } : null),
    /** Forget the baseline (teardown). */
    reset() {
      last = null;
    },
  };
}

// ---- the simulated wargame on the map (WG v2 §5.3.12, B16) -------------------

/**
 * Inks for the wargame layer (the console's palette; a test holds them equal
 * to `console/orb/wargameStyles.js` WG_INK). Sides read by frame shape and by
 * designator; the side hues are secondary and every frame also carries a Film
 * stroke, so no frame relies on its hue. Nothing here is own-systems green.
 */
export const WARGAME_INK = Object.freeze({
  film: '#E6ECEF',
  pencil: PENCIL,
  warn: '#F2B544',
  critical: '#FF7B7B',
  stale: '#8A98A2',
  lilac: '#BBA7E0',
  sand: '#CDBC8C',
  friendly: '#80E0FF',
  hostile: '#FF8080',
});

/** Side hues fill a frame at 70 % (§5.3.12). */
export const FRAME_FILL_ALPHA = 0.7;

/** A destroyed blue unit is drawn at 50 % (§5.3.4). */
export const DESTROYED_ALPHA = 0.5;

/** An engagement's outcome ring: a screen-space billboard, this many px. */
export const RING_PX = 44;

/** Threat envelope fill alpha and stroke width (px). */
export const ENVELOPE_FILL_ALPHA = 0.06;
export const ENVELOPE_STROKE_PX = 2;

/** Detection rings: the selected force, plus forces whose threat reaches us. */
export const MAX_DETECTION_RINGS = 8;

/** Red axes: an arrow in the hostile hue, this wide (px). */
export const AXIS_WIDTH_PX = 3;

/** Corridor legs are filled at this alpha; the centreline is dashed Film. */
export const CORRIDOR_ALPHA = 0.12;
export const CORRIDOR_LINE_PX = 1.5;
export const CORRIDOR_DASH_PX = 12;
/** Corridor width when the feature gives none (m), and its bounds. */
export const CORRIDOR_DEFAULT_M = 100;
export const CORRIDOR_MIN_M = 10;
export const CORRIDOR_MAX_M = 2000;

/** Wargame labels show within this range (m): every AO framing (≤ 250 km). */
export const WARGAME_LABEL_RANGE_M = DEPTH_TEST_DISTANCE_M;

/** Designators are cut to this many characters. */
export const DESIGNATOR_MAX_CHARS = 40;

/**
 * Client caps (the server caps first, §3.3; these hold if it does not):
 * forces 60, threat envelopes 80, axes 20, corridors 6, engagements 24.
 */
export const WARGAME_CAPS = Object.freeze({
  force: 60,
  force_envelope: 80,
  axis: 20,
  corridor: 6,
  engagement: 24,
});

const closed = (list) => {
  const set = new Set(list);
  return (value) =>
    typeof value === 'string' && set.has(value) ? value : 'unknown';
};

/** Closed vocabularies (§3.9); anything else reads `unknown`. */
export const wgSideKey = closed(['red', 'blue']);
export const wgStateKey = closed([
  'active',
  'suppressed',
  'damaged',
  'destroyed',
]);
export const wgStatusKey = closed(['ok', 'warn', 'critical', 'stale']);
export const wgPhaseKey = closed([
  'proposed',
  'authorized',
  'adjudicated',
  'denied',
  'expired',
]);
export const wgOutcomeKey = closed([
  'missed',
  'suppressed',
  'damaged',
  'destroyed',
]);
export const wgConsequenceKey = closed([
  'own_loss',
  'own_damage',
  'red_effect',
  'none',
]);
export const wgKindKey = closed(['blue_strike', 'red_shot', 'red_ground']);
export const wgVectorKey = closed(['axis', 'corridor']);
export const wgExposureKey = closed(['low', 'moderate', 'high']);
export const wgRingKey = closed(['threat', 'detection']);

/**
 * Whether an overlay feature is a simulated scenario item: the only kind the
 * wargame layer draws with its own style (a frame, a burst, an envelope).
 * @param {object} properties Feature properties.
 * @returns {boolean} Scenario and simulated.
 */
export function isScenarioFeature(properties) {
  return properties?.register === 'scenario' && properties?.simulated === true;
}

/**
 * Whether Blue view hides a wargame feature (§5.3.3, §5.3.12): any force,
 * envelope or vector that is not provably blue, and every axis. Engagements
 * stay (the server already masks a hidden attacker and a hidden outcome).
 * Umpire view shows a red item only when the feature itself says it came
 * from a truth request, so a body fetched for Blue view never shows red.
 * A presentation filter, not a secrecy boundary: the server leaves red truth
 * out of a Blue-view body.
 * @param {string} kind Feature kind.
 * @param {object} properties Feature properties.
 * @param {{truth?: boolean}} [view] Whether the console asked for Umpire view.
 * @returns {boolean} Hidden.
 */
export function hiddenInView(kind, properties, { truth = false } = {}) {
  if (kind === 'engagement') return false;
  const blue =
    wgSideKey(properties?.side) === 'blue' &&
    !(kind === 'vector' && properties?.kind_detail === 'axis');
  if (blue) return false;
  return !(truth === true && properties?.truth === true);
}

/** A status as ink, never green: `ok` is Film (§5.3.4). */
export function statusInk(status, okInk = WARGAME_INK.film) {
  switch (wgStatusKey(status)) {
    case 'ok':
      return okInk;
    case 'warn':
      return WARGAME_INK.warn;
    case 'critical':
      return WARGAME_INK.critical;
    case 'stale':
      return WARGAME_INK.stale;
    default:
      return WARGAME_INK.lilac;
  }
}

/**
 * How a force frame is painted (§5.3.4, §5.3.12), from closed-vocabulary
 * values only (so it doubles as the icon cache key). Blue is a rectangle and
 * red a diamond, filled with the side hue at 70 % under a Film outline; a
 * side the map does not know is a lilac dashed quatrefoil with no hue. The
 * state shows by pattern: suppressed dashes the outline, damaged breaks the
 * bar, destroyed is stale with a slash (blue also at 50 %). The bar carries
 * the status ink; a red unit whose status is critical gets a steady halo.
 * @param {object} properties Force feature properties.
 * @returns {object} `{key, side, state, status, fill, stroke, dash, bar,
 *   barBroken, slash, halo, alpha}`.
 */
export function frameSpec(properties) {
  const side = wgSideKey(properties?.side);
  const state = wgStateKey(properties?.state);
  const status = wgStatusKey(properties?.status);
  const spec = {
    key: `${side}:${state}:${status}`,
    side,
    state,
    status,
    fill: null,
    stroke: WARGAME_INK.film,
    dash: null,
    bar: WARGAME_INK.film,
    barBroken: false,
    slash: false,
    halo: false,
    alpha: 1,
  };
  if (side === 'unknown') {
    return {
      ...spec,
      stroke: WARGAME_INK.lilac,
      bar: WARGAME_INK.lilac,
      dash: 'unknown',
    };
  }
  spec.fill = side === 'blue' ? WARGAME_INK.friendly : WARGAME_INK.hostile;
  if (state === 'unknown') {
    return { ...spec, stroke: WARGAME_INK.lilac, bar: WARGAME_INK.lilac };
  }
  if (state === 'destroyed') {
    return {
      ...spec,
      stroke: WARGAME_INK.stale,
      bar: WARGAME_INK.stale,
      slash: true,
      alpha: side === 'blue' ? DESTROYED_ALPHA : 1,
    };
  }
  if (side === 'blue') {
    spec.bar = state === 'active' ? WARGAME_INK.film : WARGAME_INK.warn;
  } else {
    spec.bar = statusInk(status);
    spec.halo = status === 'critical';
  }
  if (state === 'suppressed') spec.dash = 'frame';
  if (state === 'damaged') spec.barBroken = true;
  return spec;
}

/**
 * How an engagement's Sand burst is painted (§5.3.6, §5.3.12): dashed while
 * it waits (proposed, authorized); solid with a light Sand fill once
 * adjudicated; Pencil dashed at 50 % when denied or expired; lilac dashed for
 * a phase the map does not know.
 * @param {object} properties Engagement feature properties.
 * @returns {{key: string, stroke: string, fill: string|null, dash: boolean,
 *   alpha: number}} Burst paint.
 */
export function burstSpec(properties) {
  const phase = wgPhaseKey(properties?.phase);
  const base = { key: phase, stroke: WARGAME_INK.sand, fill: null, alpha: 1 };
  if (phase === 'proposed' || phase === 'authorized')
    return { ...base, dash: true };
  if (phase === 'adjudicated')
    return { ...base, fill: WARGAME_INK.sand, dash: false };
  if (phase === 'denied' || phase === 'expired')
    return { ...base, stroke: WARGAME_INK.pencil, dash: true, alpha: 0.5 };
  return { ...base, stroke: WARGAME_INK.lilac, dash: true };
}

/**
 * The outcome ring's ink, by consequence (§5.3.6): own loss critical, own
 * damage warn, anything else Pencil; lilac for a consequence not recognised.
 * @param {unknown} consequence Feature `consequence`.
 * @returns {{key: string, stroke: string}} Ring paint.
 */
export function ringSpec(consequence) {
  const key = wgConsequenceKey(consequence);
  const stroke =
    key === 'own_loss'
      ? WARGAME_INK.critical
      : key === 'own_damage'
        ? WARGAME_INK.warn
        : key === 'unknown'
          ? WARGAME_INK.lilac
          : WARGAME_INK.pencil;
  return { key, stroke };
}

/** Exposure buckets as corridor ink (§5.3.12): low, moderate, high. */
export function exposureInk(exposure) {
  switch (wgExposureKey(exposure)) {
    case 'low':
      return WARGAME_INK.pencil;
    case 'moderate':
      return WARGAME_INK.warn;
    case 'high':
      return WARGAME_INK.critical;
    default:
      return WARGAME_INK.lilac;
  }
}

// ---- wargame words (sentence case, never "·"; built from constants only) -----

/** Outcome words for the map's outcome labels (§5.3.1). */
export const OUTCOME_WORDS = Object.freeze({
  missed: 'Missed',
  suppressed: 'Suppressed',
  damaged: 'Damaged',
  destroyed: 'Destroyed',
});

const KIND_WORDS = Object.freeze({
  blue_strike: 'strike',
  red_shot: 'shot',
  red_ground: 'ground fire',
});

const PHASE_WORDS = Object.freeze({
  proposed: 'waiting for you',
  authorized: 'authorized',
  denied: 'denied',
  expired: 'expired',
});

/** The fixed words on the map's wargame layer (Appendix B style). */
export const WARGAME_MAP_COPY = Object.freeze({
  simulated: '(simulated)',
  outcomeHidden: 'Outcome hidden (simulated)',
  outcomeUnknown: 'Outcome not recognised (simulated)',
  phaseUnknown: 'Simulated engagement, phase not recognised',
  corridor: 'Planned corridor (simulated)',
  axis: 'Red axis (simulated)',
  scenarioUnit: 'Scenario unit',
  sideNotSet: 'Side not set',
  ringsNote: 'Rings mark outcomes. They are not effect areas.',
  layerNote: 'Everything on this layer from the wargame is simulated.',
});

/**
 * An engagement's map label: "{Outcome} (simulated)" once adjudicated and
 * shown, "Outcome hidden (simulated)" when Blue view hides it, else the
 * kind and phase ("Simulated strike, waiting for you").
 * @param {object} properties Engagement feature properties.
 * @returns {string} Label text (constants only; no feature text).
 */
export function engagementMapLabel(properties) {
  const phase = wgPhaseKey(properties?.phase);
  if (phase === 'adjudicated') {
    if (properties?.outcome === null || properties?.outcome === undefined)
      return WARGAME_MAP_COPY.outcomeHidden;
    const outcome = wgOutcomeKey(properties.outcome);
    return outcome === 'unknown'
      ? WARGAME_MAP_COPY.outcomeUnknown
      : `${OUTCOME_WORDS[outcome]} ${WARGAME_MAP_COPY.simulated}`;
  }
  if (phase === 'unknown') return WARGAME_MAP_COPY.phaseUnknown;
  const kind = wgKindKey(properties?.kind_detail);
  const word = kind === 'unknown' ? 'engagement' : KIND_WORDS[kind];
  return `Simulated ${word}, ${PHASE_WORDS[phase]}`;
}

/**
 * Whether an engagement shows an outcome ring: adjudicated, with an outcome
 * the map may show (a hidden outcome has no ring).
 * @param {object} properties Engagement feature properties.
 * @returns {boolean} Ring drawn.
 */
export function engagementHasRing(properties) {
  return (
    wgPhaseKey(properties?.phase) === 'adjudicated' &&
    properties?.outcome !== null &&
    properties?.outcome !== undefined
  );
}
