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
 *
 * Phase B (WG v2 §5.3.12) adds the simulated wargame: force frames with
 * their designators, threat and detection envelopes, red axes as arrows,
 * planned corridors coloured leg by leg by exposure, and engagements as a
 * Sand burst plus a SCREEN-SPACE outcome ring billboard (never a ground
 * ellipse: a ring marks an outcome, not an effect area). Red is drawn only
 * in Umpire view (`hiddenInView`). A designator is untrusted text like any
 * label; every other wargame word is built from constants.
 */
import * as Cesium from 'cesium';
import { siteCategoryKey } from '../../console/orb/glyphPaths.js';
import { safeText, siteWord } from '../../console/orb/placeText.js';
import {
  AXIS_WIDTH_PX,
  CONTEXT_PREFIX,
  CORRIDOR_ALPHA,
  CORRIDOR_DASH_PX,
  CORRIDOR_DEFAULT_M,
  CORRIDOR_LINE_PX,
  CORRIDOR_MAX_M,
  CORRIDOR_MIN_M,
  DEPTH_TEST_DISTANCE_M,
  DESIGNATOR_MAX_CHARS,
  ENVELOPE_FILL_ALPHA,
  ENVELOPE_STROKE_PX,
  ICON_PX,
  LABEL_MAX_CHARS,
  LABEL_RANGE_M,
  MAX_BILLBOARDS,
  MAX_DETECTION_RINGS,
  MAX_UNKNOWN_POINTS,
  PENCIL,
  PROTECTED_LABEL_RANGE_M,
  PROTECTED_LINE,
  RING_PX,
  SCALE_BY_DISTANCE,
  UNKNOWN_GREY,
  UNRECOGNISED_MAP_ITEM,
  WARGAME_CAPS,
  WARGAME_INK,
  WARGAME_KINDS,
  WARGAME_LABEL_RANGE_M,
  WARGAME_MAP_COPY,
  burstSpec,
  engagementHasRing,
  engagementMapLabel,
  exposureInk,
  frameSpec,
  hiddenInView,
  isScenarioFeature,
  ringSpec,
  statusInk,
  wgExposureKey,
  wgRingKey,
  wgStatusKey,
  wgVectorKey,
} from './contextPolicy.js';

const WARGAME = new Set(WARGAME_KINDS);

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

/** Empty per-kind counts for the wargame kinds. */
export function emptyWargameCounts() {
  return Object.fromEntries(
    WARGAME_KINDS.map((kind) => [
      kind,
      { served: 0, drawn: 0, capped: 0, hidden: 0 },
    ]),
  );
}

/**
 * Read an overlay body into the items the map draws.
 * @param {object} body `/intel/overlay` FeatureCollection.
 * @param {{maxBillboards?: number, maxUnknown?: number, truth?: boolean,
 *   selected?: string|null}} [options] Caps; `truth` is the console's
 *   Umpire view (red shows only then); `selected` is the force (`frc:…`)
 *   whose detection ring shows.
 * @returns {{items: object[], counts: object}} Items in draw order and counts:
 *   `site: {served, drawn, capped}`, `unknown: {served, drawn}`, `invalid`,
 *   and per wargame kind `{served, drawn, capped, hidden}`.
 */
export function readContextFeatures(
  body,
  {
    maxBillboards = MAX_BILLBOARDS,
    maxUnknown = MAX_UNKNOWN_POINTS,
    truth = false,
    selected = null,
  } = {},
) {
  const features = Array.isArray(body?.features) ? body.features : [];
  const counts = {
    site: { served: 0, drawn: 0, capped: 0 },
    unknown: { served: 0, drawn: 0 },
    invalid: 0,
    ...emptyWargameCounts(),
  };
  const items = [];
  const seen = new Set();
  const wargame = { counts, items, seen, truth: truth === true, used: {} };
  const detections = [];
  features.forEach((feature, index) => {
    const properties =
      feature?.properties && typeof feature.properties === 'object'
        ? feature.properties
        : {};
    const kind = typeof properties.kind === 'string' ? properties.kind : '';
    if (WARGAME.has(kind) && isScenarioFeature(properties)) {
      readWargameFeature(feature, properties, kind, index, wargame, detections);
      return;
    }
    const known = kind === 'site';
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
  // Detection rings (§5.3.12): the selected force's first, then forces whose
  // threat reaches us (status critical), in the order served; at most 8.
  const isSelected = (item) =>
    typeof selected === 'string' && item.pickId === selected;
  const chosen = [
    ...detections.filter(isSelected),
    ...detections.filter(
      (item) => !isSelected(item) && item.status === 'critical',
    ),
  ];
  const envelopeCounts = counts.force_envelope;
  for (const item of detections) {
    const rank = chosen.indexOf(item);
    if (rank < 0) continue; // served, not asked for: no ring
    if (seen.has(item.id)) {
      counts.invalid += 1;
    } else if (rank >= MAX_DETECTION_RINGS) {
      envelopeCounts.capped += 1;
    } else {
      seen.add(item.id);
      envelopeCounts.drawn += 1;
      items.push(item);
    }
  }
  return { items, counts };
}

// ---- the simulated wargame (§5.3.12) ------------------------------------------

/** The `[lon, lat]` pairs of a GeoJSON LineString or a Polygon's outer ring. */
function linePairs(geometry, type) {
  if (geometry?.type !== type || !Array.isArray(geometry.coordinates))
    return null;
  const raw =
    type === 'Polygon' ? geometry.coordinates[0] : geometry.coordinates;
  if (!Array.isArray(raw)) return null;
  const pairs = raw.filter(finitePair).map(([lon, lat]) => [lon, lat]);
  if (pairs.length !== raw.length) return null;
  return pairs;
}

/** A polygon's outer ring, open (no repeated last vertex), or null. */
function openRing(pairs) {
  if (!pairs) return null;
  const ring = [...pairs];
  const [first, last] = [ring[0], ring.at(-1)];
  if (ring.length > 1 && first[0] === last[0] && first[1] === last[1])
    ring.pop();
  return ring.length >= 3 ? ring : null;
}

/** The middle vertex of a path (a label anchor on the path itself). */
function middleOf(pairs) {
  return pairs[Math.floor((pairs.length - 1) / 2)];
}

/**
 * Consecutive corridor legs that share an exposure bucket, as runs of path
 * vertices. Leg i is the segment from vertex i to vertex i + 1 (as the
 * planner summarises it). A segment with no leg (a re-look ring appended to
 * the path) gets no fill: no exposure is claimed for it.
 */
export function corridorRuns(pairs, legs) {
  const list = Array.isArray(legs) ? legs : [];
  const runs = [];
  for (let i = 0; i < pairs.length - 1 && i < list.length; i += 1) {
    const exposure = wgExposureKey(list[i]?.exposure);
    const last = runs.at(-1);
    if (last && last.exposure === exposure && last.to === i) {
      last.to = i + 1;
    } else {
      runs.push({ exposure, from: i, to: i + 1 });
    }
  }
  return runs.map((run) => ({
    exposure: run.exposure,
    ink: exposureInk(run.exposure),
    positions: pairs.slice(run.from, run.to + 1),
  }));
}

/** Corridor width (m): the feature's, bounded, else the default. */
function corridorWidth(value) {
  const width = Number(value);
  if (!Number.isFinite(width) || width <= 0) return CORRIDOR_DEFAULT_M;
  return Math.min(CORRIDOR_MAX_M, Math.max(CORRIDOR_MIN_M, width));
}

/** Which client cap a wargame feature counts against. */
function capKey(kind, properties) {
  if (kind !== 'vector') return kind;
  return wgVectorKey(properties.kind_detail) === 'axis' ? 'axis' : 'corridor';
}

/**
 * Read one simulated scenario feature (§3.3) into a drawn item. Red in Blue
 * view is counted `hidden` and never becomes an item; detection envelopes
 * are held back for the selection pass.
 */
function readWargameFeature(feature, properties, kind, index, ctx, detections) {
  const bucket = ctx.counts[kind];
  bucket.served += 1;
  if (hiddenInView(kind, properties, { truth: ctx.truth })) {
    bucket.hidden += 1;
    return;
  }
  const rawId = properties.id ?? feature?.id;
  const id = typeof rawId === 'string' && rawId ? rawId : `${kind}:${index}`;
  const item = wargameItem(feature?.geometry, properties, kind, id);
  if (!item || ctx.seen.has(id)) {
    ctx.counts.invalid += 1;
    return;
  }
  if (item.ring === 'detection') {
    detections.push(item);
    return;
  }
  const cap = capKey(kind, properties);
  const used = ctx.used[cap] ?? 0;
  if (used >= WARGAME_CAPS[cap]) {
    bucket.capped += 1;
    return;
  }
  ctx.used[cap] = used + 1;
  ctx.seen.add(id);
  bucket.drawn += 1;
  ctx.items.push(item);
}

/** The drawn item for one wargame feature, or null when its geometry is unusable. */
function wargameItem(geometry, p, kind, id) {
  const base = {
    id,
    entityId: `${CONTEXT_PREFIX}${id}`,
    pickId: id,
    kind,
    known: true,
    wargame: true,
  };
  if (kind === 'force' || kind === 'engagement') {
    if (geometry?.type !== 'Point' || !finitePair(geometry.coordinates))
      return null;
    const [lon, lat] = geometry.coordinates;
    if (kind === 'force') {
      const frame = frameSpec(p);
      const label =
        safeText(p.label ?? '', DESIGNATOR_MAX_CHARS) ||
        (frame.side === 'unknown'
          ? WARGAME_MAP_COPY.sideNotSet
          : WARGAME_MAP_COPY.scenarioUnit);
      return { ...base, lon, lat, label, frame };
    }
    return {
      ...base,
      lon,
      lat,
      label: engagementMapLabel(p),
      burst: burstSpec(p),
      ring: engagementHasRing(p) ? ringSpec(p.consequence) : null,
    };
  }
  if (kind === 'force_envelope') {
    const ring = openRing(linePairs(geometry, 'Polygon'));
    if (!ring) return null;
    const force =
      typeof p.force === 'string' && p.force.startsWith('frc:') ? p.force : id;
    const which = wgRingKey(p.ring);
    return {
      ...base,
      pickId: force,
      force,
      ring: which,
      status: wgStatusKey(p.status),
      ink:
        which === 'unknown' ? WARGAME_INK.lilac : statusInk(p.status, PENCIL),
      positions: ring,
    };
  }
  const path = linePairs(geometry, 'LineString');
  if (!path || path.length < 2) return null;
  const detail = wgVectorKey(p.kind_detail);
  return {
    ...base,
    detail,
    positions: path,
    anchor: middleOf(path),
    width: corridorWidth(p.corridor_m),
    runs: detail === 'corridor' ? corridorRuns(path, p.legs) : [],
    label:
      detail === 'axis'
        ? WARGAME_MAP_COPY.axis
        : detail === 'corridor'
          ? WARGAME_MAP_COPY.corridor
          : null,
  };
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

// ---- wargame entities (§5.3.12) ------------------------------------------------

const cssColor = (css) => Cesium.Color.fromCssColorString(css);

function degreesArray(pairs) {
  return Cesium.Cartesian3.fromDegreesArray(pairs.flat());
}

function scaleByDistanceScalar() {
  const [near, nearValue, far, farValue] = SCALE_BY_DISTANCE;
  return new Cesium.NearFarScalar(near, nearValue, far, farValue);
}

/** A screen-space marker: the canvas as a billboard, else a plain point. */
function screenMarker(image, px, fallback, scaleByDistance) {
  const common = {
    heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    disableDepthTestDistance: DEPTH_TEST_DISTANCE_M,
    scaleByDistance,
  };
  if (image) {
    return {
      billboard: {
        image,
        width: px,
        height: px,
        verticalOrigin: Cesium.VerticalOrigin.CENTER,
        ...common,
      },
    };
  }
  return { point: { ...fallback, ...common } };
}

/**
 * A wargame label: Film text, shown at every AO framing. `below` hangs it
 * under the marker instead of over it: an engagement sits on its target, so
 * its words must not land on the target force's designator.
 */
function wargameLabel(text, liftPx, scaleByDistance, rangeM, below = false) {
  const label = labelOptions(
    text,
    rangeM ?? WARGAME_LABEL_RANGE_M,
    WARGAME_INK.film,
    { liftPx, scaleByDistance },
  );
  if (!below) return label;
  return {
    ...label,
    verticalOrigin: Cesium.VerticalOrigin.TOP,
    pixelOffset: new Cesium.Cartesian2(0, liftPx),
  };
}

function forceEntities(item, icons) {
  const scale = scaleByDistanceScalar();
  const icon = icons?.frameIcon?.(item.frame) ?? null;
  const fallback = {
    pixelSize: 12,
    color: cssColor(item.frame.fill ?? WARGAME_INK.lilac),
    outlineColor: cssColor(item.frame.stroke),
    outlineWidth: 2,
  };
  return [
    {
      id: item.entityId,
      position: Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 0),
      ...screenMarker(icon, ICON_PX, fallback, scale),
      label: wargameLabel(item.label, ICON_PX / 2 + 3, scale),
    },
  ];
}

function envelopeEntities(item) {
  const closedRing = degreesArray([...item.positions, item.positions[0]]);
  if (item.ring === 'detection') {
    return [
      {
        id: item.entityId,
        polyline: {
          positions: closedRing,
          width: ENVELOPE_STROKE_PX,
          clampToGround: true,
          material: new Cesium.PolylineDashMaterialProperty({
            color: cssColor(PENCIL),
            dashLength: CORRIDOR_DASH_PX,
          }),
        },
      },
    ];
  }
  const ink = cssColor(item.ink);
  const outline = {
    positions: closedRing,
    width: ENVELOPE_STROKE_PX,
    clampToGround: true,
    material:
      item.ring === 'unknown'
        ? new Cesium.PolylineDashMaterialProperty({
            color: ink,
            dashLength: CORRIDOR_DASH_PX,
          })
        : new Cesium.ColorMaterialProperty(ink),
  };
  if (item.ring === 'unknown') {
    return [{ id: item.entityId, polyline: outline }];
  }
  return [
    {
      id: item.entityId,
      // No height: Cesium drapes it on the terrain (a ground primitive).
      polygon: {
        hierarchy: new Cesium.PolygonHierarchy(degreesArray(item.positions)),
        material: new Cesium.ColorMaterialProperty(
          ink.withAlpha(ENVELOPE_FILL_ALPHA),
        ),
        outline: false,
      },
      polyline: outline,
    },
  ];
}

function vectorEntities(item) {
  const positions = degreesArray(item.positions);
  const [lon, lat] = item.anchor;
  const anchor = Cesium.Cartesian3.fromDegrees(lon, lat, 0);
  if (item.detail === 'axis') {
    return [
      {
        id: item.entityId,
        position: anchor,
        polyline: {
          positions,
          width: AXIS_WIDTH_PX,
          clampToGround: true,
          material: new Cesium.PolylineArrowMaterialProperty(
            cssColor(WARGAME_INK.hostile),
          ),
        },
        label: wargameLabel(item.label, 8, undefined, LABEL_RANGE_M),
      },
    ];
  }
  const dashed = (css) =>
    new Cesium.PolylineDashMaterialProperty({
      color: cssColor(css),
      dashLength: CORRIDOR_DASH_PX,
    });
  if (item.detail !== 'corridor') {
    // A vector kind the map does not know: lilac dashes, no claim of exposure.
    return [
      {
        id: item.entityId,
        polyline: {
          positions,
          width: CORRIDOR_LINE_PX,
          clampToGround: true,
          material: dashed(WARGAME_INK.lilac),
        },
      },
    ];
  }
  const legs = item.runs.map((run, n) => ({
    id: `${item.entityId}#leg${n}`,
    // One corridor per run of legs in one exposure bucket; no height, so it
    // is draped on the terrain.
    corridor: {
      positions: degreesArray(run.positions),
      width: item.width,
      material: new Cesium.ColorMaterialProperty(
        cssColor(run.ink).withAlpha(CORRIDOR_ALPHA),
      ),
      cornerType: Cesium.CornerType.ROUNDED,
    },
  }));
  return [
    ...legs,
    {
      id: item.entityId,
      position: anchor,
      polyline: {
        positions,
        width: CORRIDOR_LINE_PX,
        clampToGround: true,
        material: dashed(WARGAME_INK.film),
      },
      label: wargameLabel(item.label, 8, undefined),
    },
  ];
}

function engagementEntities(item, icons) {
  const scale = scaleByDistanceScalar();
  const position = Cesium.Cartesian3.fromDegrees(item.lon, item.lat, 0);
  const burst = icons?.burstIcon?.(item.burst) ?? null;
  const lift = (item.ring ? RING_PX : ICON_PX) / 2 + 3;
  const out = [
    {
      id: item.entityId,
      position,
      ...screenMarker(
        burst,
        ICON_PX,
        {
          pixelSize: 10,
          color: cssColor(item.burst.stroke),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 1,
        },
        scale,
      ),
      label: wargameLabel(item.label, lift, scale, undefined, true),
    },
  ];
  if (item.ring) {
    // The outcome ring lives in SCREEN space (a billboard, or a point's
    // outline without a canvas): it marks an outcome, it is not an effect
    // area, so it is never a ground ellipse and never sized in metres.
    const ring = icons?.ringIcon?.(item.ring) ?? null;
    out.push({
      id: `${item.entityId}#ring`,
      position,
      ...screenMarker(
        ring,
        RING_PX,
        {
          pixelSize: RING_PX - 6,
          color: Cesium.Color.TRANSPARENT,
          outlineColor: cssColor(item.ring.stroke),
          outlineWidth: 3,
        },
        scale,
      ),
    });
  }
  return out;
}

/**
 * Cesium entity options for one wargame item (one or more entities: a
 * corridor has one per exposure run plus its centreline, an engagement its
 * burst plus its outcome ring).
 * @param {object} item One of `readContextFeatures().items` (`wargame`).
 * @param {{icons?: object|null}} [options] The icon cache (`frameIcon`,
 *   `burstIcon`, `ringIcon`); without one, markers are plain points.
 * @returns {object[]} Options for `EntityCollection.add`, each with its id.
 */
export function wargameEntityOptions(item, { icons = null } = {}) {
  switch (item?.kind) {
    case 'force':
      return forceEntities(item, icons);
    case 'force_envelope':
      return envelopeEntities(item);
    case 'vector':
      return vectorEntities(item);
    case 'engagement':
      return engagementEntities(item, icons);
    default:
      return [];
  }
}

/**
 * Every entity an item draws: a site or an unrecognised item is one entity
 * (`contextEntityOptions`), a wargame item one or more.
 * @param {object} item One of `readContextFeatures().items`.
 * @param {{icons?: object|null}} [options] The icon cache.
 * @returns {object[]} Entity options.
 */
export function contextEntityList(item, { icons = null } = {}) {
  if (item?.wargame) return wargameEntityOptions(item, { icons });
  const icon = item?.known ? (icons?.iconFor?.(item.category) ?? null) : null;
  return [contextEntityOptions(item, { icon })];
}
