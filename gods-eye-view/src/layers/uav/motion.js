/**
 * @module uav/motion
 * @description Client-side interpolation for 5 Hz UAV telemetry.
 *
 * The bridge publishes a fix every 200 ms. Writing each fix as a
 * `ConstantPositionProperty` makes the drone teleport five times a second —
 * that is the T6 defect this module exists to remove. Instead every fix is
 * appended to a `Cesium.SampledPositionProperty` with linear interpolation,
 * and the entity reads a `CallbackPositionProperty` that evaluates that
 * sampled property at `now - renderDelayMs`. Rendering one poll interval
 * behind guarantees the frame always lands BETWEEN two known fixes, so the
 * path is continuous instead of extrapolating ahead and snapping back.
 *
 * The layer deliberately does not touch `viewer.clock`: the timeline is shared
 * with the satellite/scenario layers, and a poll-driven layer has no business
 * driving simulation time. Interpolation runs off wall-clock milliseconds, so
 * it behaves identically whether the clock is animating, paused or scrubbed.
 *
 * Samples are bounded: each vehicle retains at most MAX_POSITION_SAMPLES fixes
 * (~6 s at 5 Hz) and the expired head is removed from the sampled property, so
 * a long mission cannot grow the interpolation table without bound.
 */
import * as Cesium from 'cesium';
import {
  MAX_POSITION_SAMPLES,
  MAX_RENDER_DELAY_MS,
  MODEL_HEADING_OFFSET_DEG,
} from './policy.js';

/**
 * Create the motion owner for one layer.
 * @param {{state: object}} context Layer composition context.
 * @returns {object} Motion methods used by rendering, tracking and lifecycle.
 */
export function createMotion({ state }) {
  /** reference -> motion slot */
  const slots = new Map();
  const renderDelayMs = Math.min(
    MAX_RENDER_DELAY_MS,
    Math.max(0, state.pollMs || 0),
  );
  const renderScratch = new Cesium.JulianDate();
  const hprScratch = new Cesium.HeadingPitchRoll();
  let renderScratchMs = null;

  /**
   * Julian time the fleet renders at: one poll interval behind wall clock.
   * Cached per millisecond so a 60 fps frame with several drones converts once.
   * @returns {Cesium.JulianDate} Shared scratch instant — do not retain.
   */
  function renderTime() {
    const atMs = state.now() - renderDelayMs;
    if (atMs !== renderScratchMs) {
      Cesium.JulianDate.fromDate(new Date(atMs), renderScratch);
      renderScratchMs = atMs;
    }
    return renderScratch;
  }

  function createSlot() {
    const sampled = new Cesium.SampledPositionProperty();
    // Linear over a dense 5 Hz feed: a higher-degree fit overshoots on the
    // hover-to-translate transitions a quadrotor makes constantly.
    sampled.setInterpolationOptions({
      interpolationDegree: 1,
      interpolationAlgorithm: Cesium.LinearApproximation,
    });
    // A single fix (first poll) or a stalled feed must still render the last
    // known position rather than disappearing.
    sampled.forwardExtrapolationType = Cesium.ExtrapolationType.HOLD;
    sampled.backwardExtrapolationType = Cesium.ExtrapolationType.HOLD;
    return {
      sampled,
      times: [],
      lastSampleMs: null,
      lastPosition: null,
      heading: null,
      pitch: null,
      roll: null,
    };
  }

  /** Drop the oldest fixes so the interpolation table stays bounded. */
  function trim(slot) {
    while (slot.times.length > MAX_POSITION_SAMPLES) {
      const expired = slot.times.shift();
      slot.sampled.removeSample(expired);
    }
  }

  /**
   * Append one telemetry fix.
   * @param {string} reference Vehicle reference.
   * @param {Cesium.Cartesian3} position ECEF position for this fix.
   * @param {number} atMs Observation time (Unix ms).
   * @returns {object} The vehicle's motion slot.
   */
  function addSample(reference, position, atMs) {
    let slot = slots.get(reference);
    if (!slot) {
      slot = createSlot();
      slots.set(reference, slot);
    }
    // A repeated or out-of-order observedAtMs would corrupt the interpolation
    // table; the newest position still wins for the extrapolated hold.
    slot.lastPosition = Cesium.Cartesian3.clone(position, slot.lastPosition);
    if (slot.lastSampleMs != null && atMs <= slot.lastSampleMs) return slot;
    const time = Cesium.JulianDate.fromDate(new Date(atMs));
    slot.sampled.addSample(time, position);
    slot.times.push(time);
    slot.lastSampleMs = atMs;
    trim(slot);
    return slot;
  }

  /**
   * Record the attitude carried by the newest fix (drives the GLB's pose).
   * @param {string} reference Vehicle reference.
   * @param {{heading: number|null, pitch: number|null, roll: number|null}} attitude Degrees.
   * @returns {void}
   */
  function setAttitude(reference, { heading, pitch, roll }) {
    const slot = slots.get(reference);
    if (!slot) return;
    if (Number.isFinite(heading)) slot.heading = heading;
    if (Number.isFinite(pitch)) slot.pitch = pitch;
    if (Number.isFinite(roll)) slot.roll = roll;
  }

  /**
   * Interpolated position at the current render instant.
   * @param {string} reference Vehicle reference.
   * @param {Cesium.Cartesian3} [result] Optional result instance.
   * @returns {Cesium.Cartesian3|undefined} Position, or undefined when unknown.
   */
  function positionAt(reference, result) {
    const slot = slots.get(reference);
    if (!slot) return undefined;
    const value = slot.sampled.getValue(renderTime(), result);
    return value ?? slot.lastPosition ?? undefined;
  }

  /**
   * Position property for a vehicle entity. A `CallbackPositionProperty` (not a
   * plain `CallbackProperty`) so `viewer.trackedEntity` and the cockpit camera
   * can resolve it in the fixed reference frame.
   * @param {string} reference Vehicle reference.
   * @returns {Cesium.CallbackPositionProperty} Interpolated position property.
   */
  function positionProperty(reference) {
    return new Cesium.CallbackPositionProperty(
      (time, result) => positionAt(reference, result),
      false,
    );
  }

  /**
   * Orientation property for a vehicle entity, from the newest attitude.
   * The shared aircraft GLBs are exported nose -X, so the heading carries the
   * same MODEL_HEADING_OFFSET_DEG correction the military layer applies.
   * @param {string} reference Vehicle reference.
   * @returns {Cesium.CallbackProperty} Orientation property.
   */
  function orientationProperty(reference) {
    return new Cesium.CallbackProperty((time, result) => {
      const slot = slots.get(reference);
      const position = positionAt(reference);
      if (!slot || !position || slot.heading == null) return undefined;
      hprScratch.heading = Cesium.Math.toRadians(
        slot.heading + MODEL_HEADING_OFFSET_DEG,
      );
      hprScratch.pitch = Cesium.Math.toRadians(slot.pitch ?? 0);
      hprScratch.roll = Cesium.Math.toRadians(slot.roll ?? 0);
      return Cesium.Transforms.headingPitchRollQuaternion(
        position,
        hprScratch,
        Cesium.Ellipsoid.WGS84,
        undefined,
        result,
      );
    }, false);
  }

  /**
   * The raw sampled property, for tests and for anything that needs to read the
   * interpolation table rather than the rendered instant.
   * @param {string} reference Vehicle reference.
   * @returns {Cesium.SampledPositionProperty|null} Sampled property.
   */
  function sampledProperty(reference) {
    return slots.get(reference)?.sampled ?? null;
  }

  /**
   * Retained sample count for a vehicle (bounded by MAX_POSITION_SAMPLES).
   * @param {string} reference Vehicle reference.
   * @returns {number} Retained samples.
   */
  function sampleCount(reference) {
    return slots.get(reference)?.times.length ?? 0;
  }

  /**
   * Drop a vehicle's interpolation state.
   * @param {string} reference Vehicle reference.
   * @returns {void}
   */
  function forget(reference) {
    slots.delete(reference);
  }

  /** Release every vehicle's interpolation state. */
  function clear() {
    slots.clear();
    renderScratchMs = null;
  }

  return {
    renderDelayMs,
    renderTime,
    addSample,
    setAttitude,
    positionAt,
    positionProperty,
    orientationProperty,
    sampledProperty,
    sampleCount,
    forget,
    clear,
  };
}
