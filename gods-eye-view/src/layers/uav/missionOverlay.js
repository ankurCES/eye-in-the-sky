/**
 * @module uav/missionOverlay
 * @description Mission geometry from `GET /mission-overlay`, rendered into its
 * OWN `Cesium.CustomDataSource`.
 *
 * BRIDGE_CONTRACT T6 is explicit: mission overlays are never carried through
 * the entity snapshot. The vehicle data source turns over at the 5 Hz poll
 * rate; route lines, grid lanes, coverage polygons, the geofence and the threat
 * rings change only when the mission state changes, and rebuilding them five
 * times a second would be both wrong and expensive. They therefore live in a
 * second data source that is refreshed on a mission-state signature change,
 * not on a tick.
 *
 * Every feature carries `properties.kind`; the contract's eight kinds each get
 * an explicit style and an unknown kind still draws (in the fallback hue)
 * rather than vanishing silently.
 */
import * as Cesium from 'cesium';
import {
  ERROR_BACKOFF_MS,
  OVERLAY_COLOR,
  OVERLAY_DATA_SOURCE,
  OVERLAY_FILL_ALPHA,
  OVERLAY_PREFIX,
  ROUTE_DASH_LENGTH,
  THREAT_RING_COLOR,
} from './policy.js';

/** Ground-conforming kinds: they describe ground, not a flight path. */
const GROUND_KINDS = new Set(['grid', 'coverage', 'geofence', 'threat_ring']);

/**
 * Mission-state signature. The overlay is refetched when this changes, which is
 * what "refresh on mission-state change" means for a REST feed: phase, active
 * tool, waypoint index and whole-percent progress all move the geometry.
 * @param {object} snapshot Source snapshot.
 * @returns {string} Signature, stable while the mission is unchanged.
 */
export function missionSignature(snapshot) {
  const missions = Array.isArray(snapshot?.missions) ? snapshot.missions : [];
  if (missions.length) {
    return missions
      .map((mission) =>
        [
          mission.missionId ?? mission.mission_id ?? '',
          mission.phase ?? '',
          mission.activeTool ?? mission.active_tool ?? '',
          mission.waypoint?.index ?? '',
          mission.waypoint?.of ?? '',
          Math.round(Number(mission.progressPct ?? mission.progress_pct ?? -1)),
        ].join(':'),
      )
      .join('|');
  }
  // Before the bridge ships missions[], the vehicles' own mission/track ids are
  // the only mission state there is — a mission swap still refreshes geometry.
  const records = Array.isArray(snapshot?.records) ? snapshot.records : [];
  return records
    .map(
      (record) =>
        `${record.reference}:${record.status?.mission ?? ''}:${record.status?.trackId ?? ''}`,
    )
    .join('|');
}

function color(kind, properties) {
  if (kind === 'threat_ring') {
    const ring = String(properties?.ring ?? '').toLowerCase();
    return Cesium.Color.fromCssColorString(
      THREAT_RING_COLOR[ring] || OVERLAY_COLOR.threat_ring,
    );
  }
  return Cesium.Color.fromCssColorString(
    OVERLAY_COLOR[kind] || OVERLAY_COLOR.default,
  );
}

function hasHeights(coordinates) {
  return coordinates.every((position) => Number.isFinite(position?.[2]));
}

function linePositions(coordinates) {
  if (hasHeights(coordinates)) {
    return Cesium.Cartesian3.fromDegreesArrayHeights(
      coordinates.flatMap(([lon, lat, height]) => [lon, lat, height]),
    );
  }
  return Cesium.Cartesian3.fromDegreesArray(
    coordinates.flatMap(([lon, lat]) => [lon, lat]),
  );
}

function ringPositions(ring) {
  return Cesium.Cartesian3.fromDegreesArray(
    ring.flatMap(([lon, lat]) => [lon, lat]),
  );
}

function polygonHierarchy(rings) {
  const [outer, ...holes] = rings;
  return new Cesium.PolygonHierarchy(
    ringPositions(outer),
    holes.map((hole) => new Cesium.PolygonHierarchy(ringPositions(hole))),
  );
}

/** Valid `[lon, lat]`-style position arrays only. */
function usableRing(ring) {
  return (
    Array.isArray(ring) &&
    ring.length >= 3 &&
    ring.every(
      (position) =>
        Array.isArray(position) &&
        Number.isFinite(position[0]) &&
        Number.isFinite(position[1]),
    )
  );
}

function usableLine(coordinates) {
  return (
    Array.isArray(coordinates) &&
    coordinates.length >= 2 &&
    coordinates.every(
      (position) =>
        Array.isArray(position) &&
        Number.isFinite(position[0]) &&
        Number.isFinite(position[1]),
    )
  );
}

/**
 * Create the mission-overlay owner for one layer.
 * @param {{state: object, options: object}} context Layer context.
 * @returns {object} Mission overlay methods.
 */
export function createMissionOverlay({ state, options }) {
  const config = options.missionOverlay || {};
  const fetchImpl =
    config.fetchImpl || ((...args) => globalThis.fetch(...args));
  let signature = null;
  let status = 'idle';
  let lastError = null;
  let featureCount = 0;
  let retryAtMs = 0;

  /**
   * Entity ids this overlay currently owns. They go into the layer's shared
   * `ownedIds` so the pick registry answers for a waypoint or target marker:
   * without that, a click on mission geometry reads as empty space and every
   * layer that tracks something (this one included) drops its track.
   */
  const ownedOverlayIds = new Set();

  /** Claim one overlay entity id for the shared pick registry. */
  function own(id) {
    ownedOverlayIds.add(id);
    state.ownedIds.add(id);
    return id;
  }

  /** Release every overlay id before the collection is rebuilt or torn down. */
  function disownAll() {
    for (const id of ownedOverlayIds) state.ownedIds.delete(id);
    ownedOverlayIds.clear();
  }

  /** Lazily create the overlay data source — never the snapshot one. */
  function ensureCollection() {
    if (state.overlayCollection || !state.viewer)
      return state.overlayCollection;
    state.overlayCollection = new Cesium.CustomDataSource(OVERLAY_DATA_SOURCE);
    state.viewer.dataSources.add(state.overlayCollection);
    return state.overlayCollection;
  }

  /**
   * Fetch the FeatureCollection. A source that already serves mission overlays
   * wins; otherwise the configured bridge origin is polled directly. With
   * neither, the overlay is simply unsupported and the layer says so.
   * @param {{signal: AbortSignal|null}} options Cancellation.
   * @returns {Promise<object|null>} FeatureCollection, or null when unsupported.
   */
  async function fetchOverlay({ signal = null } = {}) {
    if (typeof state.source?.getMissionOverlay === 'function')
      return state.source.getMissionOverlay({}, { signal });
    if (!config.baseUrl) return null;
    const response = await fetchImpl(`${config.baseUrl}/mission-overlay`, {
      signal,
      headers: config.token
        ? { Authorization: `Bearer ${config.token}` }
        : undefined,
    });
    if (!response?.ok)
      throw new Error(`UAV mission overlay HTTP ${response?.status ?? 0}`);
    return response.json();
  }

  function addPoint(collection, id, coordinates, kind, properties) {
    const stroke = color(kind, properties);
    const label =
      kind === 'waypoint'
        ? `W${properties?.index ?? ''}`.trim()
        : String(properties?.track_id ?? properties?.label ?? '');
    const reached = properties?.reached === true;
    collection.entities.add({
      id: own(id),
      position: Cesium.Cartesian3.fromDegrees(
        coordinates[0],
        coordinates[1],
        Number.isFinite(coordinates[2]) ? coordinates[2] : 0,
      ),
      point: {
        pixelSize: kind === 'target' ? 12 : 8,
        color: reached ? stroke.withAlpha(0.45) : stroke,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: label
        ? {
            text: label,
            font: '10px monospace',
            fillColor: stroke,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            pixelOffset: new Cesium.Cartesian2(0, -14),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          }
        : undefined,
    });
  }

  function addLine(collection, id, coordinates, kind, properties) {
    const stroke = color(kind, properties);
    // A planned route is dashed and a flown track is solid, so the operator can
    // read plan against reality without a legend.
    const material =
      kind === 'route'
        ? new Cesium.PolylineDashMaterialProperty({
            color: stroke,
            dashLength: ROUTE_DASH_LENGTH,
          })
        : new Cesium.ColorMaterialProperty(stroke);
    const clampToGround = !hasHeights(coordinates);
    collection.entities.add({
      id: own(id),
      polyline: {
        positions: linePositions(coordinates),
        width: kind === 'flown' ? 3 : 2,
        material,
        clampToGround,
      },
    });
  }

  function addPolygon(collection, id, rings, kind, properties) {
    const stroke = color(kind, properties);
    const filled = kind === 'coverage' || kind === 'threat_ring';
    collection.entities.add({
      id: own(id),
      polygon: {
        hierarchy: polygonHierarchy(rings),
        material: filled
          ? stroke.withAlpha(OVERLAY_FILL_ALPHA)
          : Cesium.Color.TRANSPARENT,
        fill: filled,
        outline: true,
        outlineColor: stroke,
        outlineWidth: 2,
        classificationType: GROUND_KINDS.has(kind)
          ? Cesium.ClassificationType.TERRAIN
          : undefined,
      },
    });
  }

  /**
   * Replace the overlay with one FeatureCollection.
   * @param {object} featureCollection GeoJSON FeatureCollection.
   * @returns {number} Features rendered.
   */
  function apply(featureCollection) {
    const collection = ensureCollection();
    if (!collection) return 0;
    collection.entities.removeAll();
    disownAll();
    const features = Array.isArray(featureCollection?.features)
      ? featureCollection.features
      : [];
    let rendered = 0;
    features.forEach((feature, index) => {
      const geometry = feature?.geometry;
      if (!geometry) return;
      const properties = feature.properties || {};
      const kind = String(properties.kind ?? 'default');
      const id = `${OVERLAY_PREFIX}${feature.id ?? `${kind}:${index}`}`;
      const coordinates = geometry.coordinates;
      try {
        switch (geometry.type) {
          case 'Point':
            if (
              Number.isFinite(coordinates?.[0]) &&
              Number.isFinite(coordinates?.[1])
            ) {
              addPoint(collection, id, coordinates, kind, properties);
              rendered += 1;
            }
            break;
          case 'MultiPoint':
            (coordinates || []).forEach((position, part) => {
              if (
                Number.isFinite(position?.[0]) &&
                Number.isFinite(position?.[1])
              ) {
                addPoint(
                  collection,
                  `${id}:${part}`,
                  position,
                  kind,
                  properties,
                );
                rendered += 1;
              }
            });
            break;
          case 'LineString':
            if (usableLine(coordinates)) {
              addLine(collection, id, coordinates, kind, properties);
              rendered += 1;
            }
            break;
          case 'MultiLineString':
            (coordinates || []).forEach((line, part) => {
              if (usableLine(line)) {
                addLine(collection, `${id}:${part}`, line, kind, properties);
                rendered += 1;
              }
            });
            break;
          case 'Polygon':
            if (usableRing(coordinates?.[0])) {
              addPolygon(collection, id, coordinates, kind, properties);
              rendered += 1;
            }
            break;
          case 'MultiPolygon':
            (coordinates || []).forEach((rings, part) => {
              if (usableRing(rings?.[0])) {
                addPolygon(
                  collection,
                  `${id}:${part}`,
                  rings,
                  kind,
                  properties,
                );
                rendered += 1;
              }
            });
            break;
          default:
            break;
        }
      } catch {
        // One malformed feature must never cost the operator the rest of the
        // mission picture.
      }
    });
    featureCount = rendered;
    return rendered;
  }

  /**
   * Refresh when the mission state moved (or on the first snapshot). Never
   * throws: a missing or broken overlay endpoint degrades to status, because
   * the vehicles must keep flying either way.
   * @param {object} snapshot Source snapshot.
   * @param {{signal: AbortSignal|null, force: boolean}} [options] Cancellation and override.
   * @returns {Promise<string>} Resulting status.
   */
  async function refresh(snapshot, { signal = null, force = false } = {}) {
    const next = missionSignature(snapshot);
    const nowMs = state.now();
    if (!force) {
      // A failing endpoint is retried on a backoff clock, never on the poll
      // tick. `/mission-overlay` is not served yet, and a failed attempt does
      // not advance the signature — without this gate the layer would put one
      // failed request per poll (5 Hz) on the wire and in the operator's
      // console for as long as the bridge lacks the route.
      if (status === 'error') {
        if (nowMs < retryAtMs) return status;
      } else if (signature !== null && next === signature) {
        return status;
      }
    }
    // Inline delivery wins when the source already carries the geometry.
    if (snapshot?.missionOverlay) {
      signature = next;
      retryAtMs = 0;
      apply(snapshot.missionOverlay);
      status = 'ok';
      lastError = null;
      return status;
    }
    try {
      const payload = await fetchOverlay({ signal });
      if (payload === null) {
        status = 'unsupported';
        signature = next;
        retryAtMs = 0;
        return status;
      }
      apply(payload);
      signature = next;
      retryAtMs = 0;
      status = 'ok';
      lastError = null;
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      status = 'error';
      lastError = error?.message ?? 'mission overlay unavailable';
      // Keep the signature unset so the next attempt after the backoff is a
      // real one, and hold the whole feed off until then.
      retryAtMs = state.now() + ERROR_BACKOFF_MS;
    }
    return status;
  }

  /** Overlay feed status for the layer's getStats(). */
  function getStatus() {
    return {
      status,
      features: featureCount,
      signature,
      lastError,
      /** When a failed feed will be tried again (0 when nothing is pending). */
      retryAtMs,
    };
  }

  /**
   * Remove the overlay data source from the viewer.
   * @param {Cesium.Viewer} viewer Owning viewer.
   * @returns {void}
   */
  function destroy(viewer) {
    if (state.overlayCollection && viewer)
      viewer.dataSources.remove(state.overlayCollection, true);
    disownAll();
    state.overlayCollection = null;
    signature = null;
    status = 'idle';
    featureCount = 0;
    retryAtMs = 0;
    lastError = null;
  }

  return { ensureCollection, apply, refresh, getStatus, destroy };
}
