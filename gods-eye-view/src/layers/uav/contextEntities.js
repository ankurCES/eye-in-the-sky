/**
 * @module uav/contextEntities
 * @description Turn the `/intel/overlay` body (WG v2 §3.3) into map entities.
 *
 * Two steps, so the reading can be tested without Cesium:
 * 1. `readContextFeatures(body)` validates every feature and decides what is
 *    drawn: sites (in the order the server sent them, most salient first) up
 *    to `MAX_BILLBOARDS`, and any kind this build does not know as a grey
 *    point labelled "Unrecognised map item" (§4.2.1), never silently dropped.
 * 2. `contextEntityOptions(item, {icon})` builds the Cesium entity options.
 *
 * Every label is untrusted OSM text (§3.11): it goes through `safeText`
 * (bidi controls and control characters stripped, capped) and reaches only a
 * Cesium label, which paints text on a canvas and never parses markup.
 */
import * as Cesium from 'cesium';
import { siteCategoryKey } from '../../console/orb/glyphPaths.js';
import { safeText, siteWord } from '../../console/orb/placeText.js';
import {
  CONTEXT_PREFIX,
  DEPTH_TEST_DISTANCE_M,
  ICON_PX,
  KNOWN_KINDS,
  LABEL_MAX_CHARS,
  LABEL_RANGE_M,
  MAX_BILLBOARDS,
  MAX_UNKNOWN_POINTS,
  PENCIL,
  PROTECTED_LABEL_RANGE_M,
  PROTECTED_LINE,
  SCALE_BY_DISTANCE,
  UNKNOWN_GREY,
  UNRECOGNISED_MAP_ITEM,
} from './contextPolicy.js';

const KNOWN = new Set(KNOWN_KINDS);

function finitePair(position) {
  return (
    Array.isArray(position) &&
    Number.isFinite(position[0]) &&
    Number.isFinite(position[1]) &&
    Math.abs(position[0]) <= 180 &&
    Math.abs(position[1]) <= 90
  );
}

/** Every `[lon, lat]` pair inside a GeoJSON coordinates tree. */
function collectPairs(coordinates, out = [], depth = 0) {
  if (!Array.isArray(coordinates) || depth > 4) return out;
  if (finitePair(coordinates) && !Array.isArray(coordinates[0])) {
    out.push(coordinates);
    return out;
  }
  for (const part of coordinates) collectPairs(part, out, depth + 1);
  return out;
}

/**
 * One `[lon, lat]` to draw a feature at: a Point's own position, otherwise the
 * mean of the geometry's vertices (an unknown line or polygon still shows as
 * one grey point). Null when the geometry has no usable position.
 * @param {object} geometry GeoJSON geometry.
 * @returns {number[]|null} `[lon, lat]`.
 */
export function representativePoint(geometry) {
  if (!geometry || typeof geometry !== 'object') return null;
  if (geometry.type === 'Point') {
    return finitePair(geometry.coordinates)
      ? [geometry.coordinates[0], geometry.coordinates[1]]
      : null;
  }
  const pairs = collectPairs(geometry.coordinates);
  if (!pairs.length) return null;
  const lon = pairs.reduce((sum, [x]) => sum + x, 0) / pairs.length;
  const lat = pairs.reduce((sum, [, y]) => sum + y, 0) / pairs.length;
  return [lon, lat];
}

/** Safe label text for a site (never empty). */
export function siteLabelText(properties) {
  const label = safeText(properties?.label ?? '', LABEL_MAX_CHARS);
  return label || siteWord(properties?.category);
}

/**
 * Read an overlay body into the items the map draws.
 * @param {object} body `/intel/overlay` FeatureCollection.
 * @param {{maxBillboards?: number, maxUnknown?: number}} [limits] Caps.
 * @returns {{items: object[], counts: object}} Items in draw order and counts:
 *   `site: {served, drawn, capped}`, `unknown: {served, drawn}`, `invalid`.
 */
export function readContextFeatures(
  body,
  { maxBillboards = MAX_BILLBOARDS, maxUnknown = MAX_UNKNOWN_POINTS } = {},
) {
  const features = Array.isArray(body?.features) ? body.features : [];
  const counts = {
    site: { served: 0, drawn: 0, capped: 0 },
    unknown: { served: 0, drawn: 0 },
    invalid: 0,
  };
  const items = [];
  const seen = new Set();
  features.forEach((feature, index) => {
    const properties =
      feature?.properties && typeof feature.properties === 'object'
        ? feature.properties
        : {};
    const kind = typeof properties.kind === 'string' ? properties.kind : '';
    const known = KNOWN.has(kind);
    const bucket = known ? counts.site : counts.unknown;
    bucket.served += 1;
    const point = representativePoint(feature?.geometry);
    const rawId = properties.id ?? feature?.id;
    const id =
      typeof rawId === 'string' && rawId ? rawId : `${kind || 'item'}:${index}`;
    if (!point || seen.has(id)) {
      counts.invalid += 1;
      return;
    }
    if (known) {
      if (counts.site.drawn >= maxBillboards) {
        counts.site.capped += 1;
        return;
      }
    } else if (counts.unknown.drawn >= maxUnknown) {
      return;
    }
    seen.add(id);
    bucket.drawn += 1;
    const category = siteCategoryKey(properties.category);
    items.push({
      id,
      entityId: `${CONTEXT_PREFIX}${id}`,
      kind: kind || 'unknown',
      known,
      lon: point[0],
      lat: point[1],
      category,
      label: known ? siteLabelText(properties) : UNRECOGNISED_MAP_ITEM,
      labelled: known && properties.labelled === true,
      protected: known && properties.protected === true,
    });
  });
  return { items, counts };
}

/**
 * Cesium entity options for one item.
 * @param {object} item One of `readContextFeatures().items`.
 * @param {{icon?: object|null}} [options] The category's cached icon canvas.
 * @returns {object} Options for `EntityCollection.add`.
 */
export function contextEntityOptions(item, { icon = null } = {}) {
  const position = Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 0);
  const base = { id: item.entityId, position };
  const [near, nearValue, far, farValue] = SCALE_BY_DISTANCE;
  const scaleByDistance = new Cesium.NearFarScalar(
    near,
    nearValue,
    far,
    farValue,
  );
  if (!item.known) {
    // The fail-safe item is never hidden by terrain: it must not vanish.
    return {
      ...base,
      point: {
        pixelSize: 9,
        color: Cesium.Color.fromCssColorString(UNKNOWN_GREY),
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 1,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: labelOptions(item.label, LABEL_RANGE_M, UNKNOWN_GREY, {
        liftPx: 8,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      }),
    };
  }
  // A ground-clamped marker sits ABOVE its point (VerticalOrigin.BOTTOM), and
  // its label above it: whatever is drawn below the point on screen is nearer
  // ground at an oblique view, so beyond the depth-test range it would be cut
  // off by the terrain.
  const marker = icon
    ? {
        billboard: {
          image: icon,
          width: ICON_PX,
          height: ICON_PX,
          verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: DEPTH_TEST_DISTANCE_M,
          scaleByDistance,
        },
      }
    : {
        point: {
          pixelSize: 10,
          color: Cesium.Color.fromCssColorString(PENCIL),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: DEPTH_TEST_DISTANCE_M,
          scaleByDistance,
        },
      };
  const lift = { liftPx: icon ? ICON_PX + 3 : 9, scaleByDistance };
  let label;
  if (item.protected) {
    label = labelOptions(
      `${item.label}\n${PROTECTED_LINE}`,
      PROTECTED_LABEL_RANGE_M,
      PENCIL,
      lift,
    );
  } else if (item.labelled) {
    label = labelOptions(item.label, LABEL_RANGE_M, PENCIL, lift);
  }
  return label ? { ...base, ...marker, label } : { ...base, ...marker };
}

function labelOptions(
  text,
  rangeM,
  color,
  {
    liftPx,
    scaleByDistance = undefined,
    disableDepthTestDistance = DEPTH_TEST_DISTANCE_M,
  },
) {
  return {
    text,
    font: '12px sans-serif',
    fillColor: Cesium.Color.fromCssColorString(color),
    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
    outlineColor: Cesium.Color.BLACK,
    outlineWidth: 2,
    showBackground: false,
    horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
    verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
    pixelOffset: new Cesium.Cartesian2(0, -liftPx),
    // The gap to the icon shrinks with the icon.
    pixelOffsetScaleByDistance: scaleByDistance,
    heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    disableDepthTestDistance,
    distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, rangeM),
  };
}
