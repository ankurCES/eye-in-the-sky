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
 * each one governs. Phase A draws only `site`; the Phase B kinds are listed so
 * a switch the dock already sends keeps working when B16 draws them.
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

/** Overlay kinds this build draws with their own style. */
export const KNOWN_KINDS = Object.freeze(['site']);

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
