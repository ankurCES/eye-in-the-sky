/**
 * @module uav/rendering
 * @description Entity presentation for the UAV layer: vehicles, their trails
 * and the numbered contact markers.
 *
 * Vehicles carry BOTH a point and the MQ-9 GLB, separated by complementary
 * distance display conditions, so the operator sees a dot across the theater
 * and an airframe on approach without the layer running its own per-frame
 * model scheduler. Position and orientation come from the motion module, so
 * everything drawn here moves continuously between 5 Hz fixes.
 *
 * Scalar writes (label text, marker style) are gated to ENTITY_WRITE_MIN_MS so
 * the layer honours the 2–5 Hz entity write cap in BRIDGE_CONTRACT rule 2 even
 * if the poll interval is lowered.
 */
import * as Cesium from 'cesium';
import {
  CONFIDENCE_STYLE,
  DEFAULT_CONFIDENCE_STYLE,
  ENTITY_WRITE_MIN_MS,
  MODEL_MAX_SCALE,
  MODEL_MIN_PX,
  MODEL_SWAP_DISTANCE_M,
  MODEL_HEADING_OFFSET_DEG,
  POINT_PIXEL_SIZE,
  TARGET_CAPTION_PREFIX,
  TARGET_PREFIX,
  THREAT_COLOR,
  TRAIL_LEN,
  TRAIL_MIN_STEP_M,
  TRAIL_PREFIX,
  UAV_COLOR,
  UAV_MODEL_URL,
  VEHICLE_PREFIX,
} from './policy.js';

/** Text, trimmed, or '' — the bridge sends '' for "no value" in several slots. */
function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * Read a contact from either the normalized `contacts[]` section or a raw
 * `/tracks` row, so the layer renders whichever the bridge has shipped.
 * @param {object} row Contact row.
 * @returns {object|null} `{trackId, latitude, longitude, altitude, …}` or null.
 */
export function readContact(row) {
  if (!row) return null;
  const trackId = text(row.trackId) || text(row.track_id);
  if (!trackId) return null;
  const location = row.position || row.location || {};
  const latitude = Number(location.latitude ?? location.lat);
  const longitude = Number(location.longitude ?? location.lon);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  const altitude = Number(location.altitude ?? location.alt_m);
  const salute = row.salute || {};
  return {
    trackId,
    latitude,
    longitude,
    altitude: Number.isFinite(altitude) ? altitude : 0,
    category: text(row.category) || text(row.class),
    confidence: (text(row.confidence) || 'unknown').toLowerCase(),
    threatLevel: (
      text(row.threatLevel) ||
      text(row.threat_level) ||
      'unknown'
    ).toLowerCase(),
    unit: text(salute.unit) || text(row.unit),
    activity: text(salute.activity) || text(row.activity),
    size: text(salute.size) || text(row.size),
  };
}

/**
 * Stable display number for a contact: the digits the bridge already put in the
 * track id (`TRK-…-0003` → 3) when it has them, otherwise the lowest number no
 * other contact is wearing. Numbers never change once assigned, because the
 * operator reads them aloud.
 * @param {Map<string, number>} assigned Layer-owned number table.
 * @param {string} trackId Persistent track id.
 * @returns {number} Display number.
 */
export function contactNumber(assigned, trackId) {
  const existing = assigned.get(trackId);
  if (existing != null) return existing;
  const digits = /(\d+)\s*$/.exec(trackId);
  const parsed = digits ? Number.parseInt(digits[1], 10) : Number.NaN;
  const taken = new Set(assigned.values());
  let number = Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
  while (taken.has(number)) number += 1;
  assigned.set(trackId, number);
  return number;
}

/**
 * Create the rendering owner for one layer.
 * @param {{state: object, parts: object, resolveAsset: Function}} context Layer context.
 * @returns {object} Rendering methods.
 */
export function createRendering({ state, parts, resolveAsset }) {
  const { motion } = parts;

  /** Track every entity id we own so the pick registry can answer for it. */
  function own(id) {
    state.ownedIds.add(id);
    return id;
  }

  function disown(id) {
    state.ownedIds.delete(id);
  }

  /** Whether this entity may take a scalar write on this tick. */
  function writeAllowed(slot, nowMs) {
    return (
      slot.lastWriteMs == null ||
      nowMs - slot.lastWriteMs >= ENTITY_WRITE_MIN_MS
    );
  }

  function vehicleLabel(record) {
    const status = record.status || {};
    const velocity = record.velocity || {};
    const agl =
      record.position?.agl != null
        ? ` ${Math.round(record.position.agl)}m`
        : '';
    const speed =
      velocity.speed != null ? ` ${velocity.speed.toFixed(0)}m/s` : '';
    const fuel =
      status.fuelPct != null ? ` ${Math.round(status.fuelPct)}%` : '';
    return `${record.label}${agl}${speed}${fuel}`;
  }

  /**
   * Create or refresh one vehicle.
   *
   * A record the bridge cannot place costs only itself: `Cartesian3.fromDegrees`
   * throws on a non-numeric coordinate, and one such row used to abort the whole
   * tick — every other drone would stop updating and the layer would take an
   * error backoff over a single malformed vehicle.
   * @param {object} record Normalized UAV record.
   * @param {number} observedAtMs Snapshot observation time (Unix ms).
   * @returns {string|null} The reference rendered, or null when unusable.
   */
  function upsertVehicle(record, observedAtMs) {
    const reference = record?.reference;
    const position = record?.position || {};
    if (
      !reference ||
      !Number.isFinite(position.latitude) ||
      !Number.isFinite(position.longitude)
    )
      return null;
    const cart = Cesium.Cartesian3.fromDegrees(
      position.longitude,
      position.latitude,
      position.ellipsoidAltitude ?? position.altitude ?? 0,
    );
    const atMs = Number.isFinite(record.observedAtMs)
      ? record.observedAtMs
      : observedAtMs;
    motion.addSample(reference, cart, atMs);
    motion.setAttitude(reference, {
      heading: record.velocity?.heading,
      pitch: record.attitude?.pitch,
      roll: record.attitude?.roll,
    });

    let slot = state.entities.get(reference);
    if (!slot) {
      const entity = state.collection.entities.add({
        id: own(`${VEHICLE_PREFIX}${reference}`),
        position: motion.positionProperty(reference),
        orientation: motion.orientationProperty(reference),
        point: {
          pixelSize: POINT_PIXEL_SIZE,
          color: UAV_COLOR,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          // Hands over to the GLB on approach; the two conditions meet exactly
          // at MODEL_SWAP_DISTANCE_M so the vehicle is never drawn twice and
          // never drawn not at all.
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(
            MODEL_SWAP_DISTANCE_M,
            Number.MAX_VALUE,
          ),
        },
        model: {
          uri: resolveAsset(UAV_MODEL_URL),
          minimumPixelSize: MODEL_MIN_PX,
          maximumScale: MODEL_MAX_SCALE,
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(
            0,
            MODEL_SWAP_DISTANCE_M,
          ),
        },
        label: {
          text: record.label,
          font: '11px monospace',
          fillColor: UAV_COLOR,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          pixelOffset: new Cesium.Cartesian2(0, -20),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      const trailEntity = state.collection.entities.add({
        id: own(`${TRAIL_PREFIX}${reference}`),
        polyline: {
          positions: new Cesium.CallbackProperty(
            () => state.trails.get(reference) || [],
            false,
          ),
          width: 2,
          material: UAV_COLOR.withAlpha(0.45),
        },
      });
      slot = { entity, trailEntity, record, lastWriteMs: null };
      state.entities.set(reference, slot);
      state.trails.set(reference, []);
      // A track() call may have arrived before this entity existed (poll
      // race): apply the pending tracked reference as soon as it appears.
      if (state.trackedReference === reference && state.viewer)
        state.viewer.trackedEntity = entity;
    }

    slot.record = record;
    // Normalized tracked identity so the cockpit's readAircraftInfo() can
    // resolve this drone via the standard gevTrackedId convention.
    slot.entity.gevTrackedId = `${VEHICLE_PREFIX}${reference}`;
    slot.entity._uavRecord = record;

    const trail = state.trails.get(reference);
    const last = trail[trail.length - 1];
    if (!last || Cesium.Cartesian3.distance(last, cart) > TRAIL_MIN_STEP_M) {
      trail.push(cart);
      if (trail.length > TRAIL_LEN) trail.shift();
    }

    const nowMs = state.now();
    if (!writeAllowed(slot, nowMs)) return reference;
    slot.lastWriteMs = nowMs;
    slot.entity.label.text = vehicleLabel(record);
    return reference;
  }

  /**
   * Remove one vehicle and everything drawn for it.
   * @param {string} reference Vehicle reference.
   * @returns {void}
   */
  function removeVehicle(reference) {
    const slot = state.entities.get(reference);
    if (!slot) return;
    state.collection?.entities.remove(slot.entity);
    state.collection?.entities.remove(slot.trailEntity);
    disown(`${VEHICLE_PREFIX}${reference}`);
    disown(`${TRAIL_PREFIX}${reference}`);
    state.entities.delete(reference);
    state.trails.delete(reference);
    state.missingPolls.delete(reference);
    motion.forget(reference);
    if (state.trackedReference === reference) {
      state.trackedReference = null;
      if (state.viewer) state.viewer.trackedEntity = undefined;
    }
  }

  function contactStyle(contact) {
    const confidence =
      CONFIDENCE_STYLE[contact.confidence] || DEFAULT_CONFIDENCE_STYLE;
    const hue = THREAT_COLOR[contact.threatLevel] || THREAT_COLOR.unknown;
    return {
      ...confidence,
      color: Cesium.Color.fromCssColorString(hue).withAlpha(confidence.alpha),
    };
  }

  function contactCaption(contact) {
    return [
      contact.trackId,
      contact.category,
      contact.confidence !== 'unknown' ? contact.confidence : '',
      contact.unit,
      contact.activity,
    ]
      .filter(Boolean)
      .join(' · ');
  }

  /**
   * Create or refresh one numbered contact marker (PLAN §3.1 item 3).
   * The number rides the marker; SALUTE detail rides the caption beneath it.
   * @param {object} row Contact row, normalized or raw.
   * @returns {string|null} The track id rendered, or null when unusable.
   */
  function upsertContact(row) {
    const contact = readContact(row);
    if (!contact) return null;
    const number = contactNumber(state.targetNumbers, contact.trackId);
    const cart = Cesium.Cartesian3.fromDegrees(
      contact.longitude,
      contact.latitude,
      contact.altitude,
    );
    const style = contactStyle(contact);
    let slot = state.targets.get(contact.trackId);
    if (!slot) {
      const entity = state.collection.entities.add({
        id: own(`${TARGET_PREFIX}${contact.trackId}`),
        position: new Cesium.ConstantPositionProperty(cart),
        point: {
          pixelSize: style.pixelSize,
          color: style.color,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: style.outlineWidth,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: String(number),
          font: 'bold 11px monospace',
          fillColor: Cesium.Color.BLACK,
          style: Cesium.LabelStyle.FILL,
          horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      const captionEntity = state.collection.entities.add({
        id: own(`${TARGET_CAPTION_PREFIX}${contact.trackId}`),
        position: new Cesium.ConstantPositionProperty(cart),
        label: {
          text: contactCaption(contact),
          font: '10px monospace',
          fillColor: style.color,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          pixelOffset: new Cesium.Cartesian2(0, 16),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      slot = { entity, captionEntity, number, lastWriteMs: null };
      state.targets.set(contact.trackId, slot);
    }

    slot.entity.position = new Cesium.ConstantPositionProperty(cart);
    slot.captionEntity.position = new Cesium.ConstantPositionProperty(cart);
    const nowMs = state.now();
    if (!writeAllowed(slot, nowMs)) return contact.trackId;
    slot.lastWriteMs = nowMs;
    slot.entity.point.pixelSize = style.pixelSize;
    slot.entity.point.color = style.color;
    slot.entity.point.outlineWidth = style.outlineWidth;
    slot.entity.label.text = String(slot.number);
    slot.captionEntity.label.text = contactCaption(contact);
    slot.captionEntity.label.fillColor = style.color;
    return contact.trackId;
  }

  /**
   * Remove contacts the bridge no longer reports.
   * @param {Set<string>} keep Track ids present in this snapshot.
   * @returns {void}
   */
  function pruneContacts(keep) {
    for (const [trackId, slot] of state.targets) {
      if (keep.has(trackId)) continue;
      state.collection?.entities.remove(slot.entity);
      state.collection?.entities.remove(slot.captionEntity);
      disown(`${TARGET_PREFIX}${trackId}`);
      disown(`${TARGET_CAPTION_PREFIX}${trackId}`);
      state.targets.delete(trackId);
    }
  }

  /** Release every rendered entity without touching the data source itself. */
  function clear() {
    state.entities.clear();
    state.trails.clear();
    state.targets.clear();
    state.missingPolls.clear();
    state.ownedIds.clear();
    motion.clear();
  }

  return {
    upsertVehicle,
    removeVehicle,
    upsertContact,
    pruneContacts,
    vehicleLabel,
    modelHeadingOffsetDeg: MODEL_HEADING_OFFSET_DEG,
    clear,
  };
}
