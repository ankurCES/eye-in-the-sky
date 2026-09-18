/**
 * @module uav/policy
 * @description Tunables for the godSeye UAV layer. Everything the operator
 * experience in PLAN §3.1 is measured against lives here so the rendering,
 * motion and overlay modules stay declarative.
 *
 * The layer is READ-ONLY for flight control (PLAN §1 hard rule): the MCP
 * server is the only command path. Nothing in this layer may originate a
 * flight command.
 */
import * as Cesium from 'cesium';

/** @constant {string} Pick-registry / render-governor owner id. */
export const LAYER_ID = 'uav';

/** @constant {string} Entity id prefix for a vehicle. */
export const VEHICLE_PREFIX = 'uav:';

/** @constant {string} Entity id prefix for a vehicle's trail polyline. */
export const TRAIL_PREFIX = 'uav-trail:';

/** @constant {string} Entity id prefix for a numbered contact marker. */
export const TARGET_PREFIX = 'uav-target:';

/** @constant {string} Entity id prefix for a contact's SALUTE caption. */
export const TARGET_CAPTION_PREFIX = 'uav-target-caption:';

/** @constant {string} Entity id prefix for a mission-overlay feature. */
export const OVERLAY_PREFIX = 'uav-overlay:';

/** @constant {string} Name of the entity snapshot data source. */
export const ENTITY_DATA_SOURCE = 'uav';

/**
 * @constant {string} Name of the mission-overlay data source. Mission geometry
 * is rendered here and NEVER through the entity snapshot (BRIDGE_CONTRACT T6).
 */
export const OVERLAY_DATA_SOURCE = 'uav-mission-overlay';

/** @constant {number} Bridge poll interval (ms) — 5 Hz per the contract. */
export const DEFAULT_POLL_MS = 200;

/** @constant {number} Retained trail vertices per vehicle. */
export const TRAIL_LEN = 120;

/** @constant {number} Minimum metres between retained trail vertices. */
export const TRAIL_MIN_STEP_M = 0.5;

// --- Motion (PLAN §3.1 acceptance 1, REVIEW_TECHNICAL T6) -------------------
// 5 Hz telemetry is rendered through a SampledPositionProperty evaluated one
// poll interval behind the newest fix, so every frame lands BETWEEN two known
// samples instead of snapping to the newest one. That is what turns a 5 Hz
// feed into continuous motion; a ConstantPositionProperty per poll cannot.

/**
 * @constant {number} Display latency ceiling (ms). The PLAN §3.1 definition of
 * done requires motion ≤250 ms behind the sim, so the render delay is one poll
 * interval clamped to this.
 */
export const MAX_RENDER_DELAY_MS = 250;

/** @constant {number} Retained position samples per vehicle (~6 s at 5 Hz). */
export const MAX_POSITION_SAMPLES = 32;

/**
 * @constant {number} Entity property write floor (ms). BRIDGE_CONTRACT rule 2
 * caps entity property writes at 2–5 Hz; scalar/label writes are gated by this
 * even if the poll rate is raised, while position samples continue to carry
 * the full feed (they are interpolated, not per-frame writes).
 */
export const ENTITY_WRITE_MIN_MS = 200;

/** @constant {number} Consecutive missed polls before a vehicle is evicted. */
export const MISSING_POLL_LIMIT = 3;

/** @constant {number} Backoff after a bridge error (ms). */
export const ERROR_BACKOFF_MS = 5000;

// --- Presentation ----------------------------------------------------------

/** @constant {Cesium.Color} UAV identity hue. */
export const UAV_COLOR = Cesium.Color.CYAN;

/** @constant {number} Point size for a distant vehicle. */
export const POINT_PIXEL_SIZE = 12;

/** @constant {string} MQ-9 asset in the shared 3D hangar (see aircraftClass). */
export const UAV_MODEL_URL = '/models/mq9.glb';

/**
 * @constant {number} Camera distance (m) at which the point hands over to the
 * GLB. Closer than this the operator sees an airframe; further out a dot.
 */
export const MODEL_SWAP_DISTANCE_M = 60000;

/** @constant {number} Model floor so a distant airframe stays readable. */
export const MODEL_MIN_PX = 32;

/**
 * @constant {number} Upper bound on the inflation `minimumPixelSize` may apply,
 * so a distant airframe stays visible without ballooning past 12× real size.
 */
export const MODEL_MAX_SCALE = 12;

/**
 * @constant {number} Heading offset (deg) for the shared nose -X GLB export
 * convention — identical to the military layer's MODEL_HEADING_OFFSET_DEG.
 */
export const MODEL_HEADING_OFFSET_DEG = 180;

// --- Contacts (PLAN §3.1 item 3 — numbered track markers, M11) -------------

/** @constant {Object<string, string>} Marker hue by contact threat level. */
export const THREAT_COLOR = {
  critical: '#ff3b30',
  high: '#ff6b3d',
  medium: '#ffb800',
  low: '#7ad46c',
  unknown: '#9ca6b0',
};

/**
 * @constant {Object<string, {pixelSize: number, outlineWidth: number,
 *   alpha: number}>} Marker weight by reporting confidence. A confirmed
 * contact draws big and solid; a possible one draws small and faint.
 */
export const CONFIDENCE_STYLE = {
  confirmed: { pixelSize: 16, outlineWidth: 3, alpha: 1 },
  probable: { pixelSize: 13, outlineWidth: 2, alpha: 0.85 },
  possible: { pixelSize: 10, outlineWidth: 1, alpha: 0.6 },
};

/** @constant {{pixelSize: number, outlineWidth: number, alpha: number}} */
export const DEFAULT_CONFIDENCE_STYLE = CONFIDENCE_STYLE.possible;

// --- Mission overlay styling (BRIDGE_CONTRACT /mission-overlay) -------------

/**
 * @constant {Object<string, string>} Stroke hue per `properties.kind`. Every
 * kind the contract lists has an explicit entry; an unknown kind falls back to
 * `default` rather than silently disappearing.
 */
export const OVERLAY_COLOR = {
  route: '#4dd0e1',
  flown: '#7ad46c',
  waypoint: '#4dd0e1',
  grid: '#8ea6c8',
  coverage: '#7ad46c',
  geofence: '#ffd166',
  threat_ring: '#ff6b3d',
  target: '#ff3b30',
  default: '#9ca6b0',
};

/** @constant {string} Engagement rings read hotter than acquisition rings. */
export const THREAT_RING_COLOR = {
  engagement: '#ff3b30',
  acquisition: '#ffb800',
};

/** @constant {number} Translucent fill alpha for coverage/threat polygons. */
export const OVERLAY_FILL_ALPHA = 0.18;

/** @constant {number} Planned-route dash length (px). */
export const ROUTE_DASH_LENGTH = 16;
