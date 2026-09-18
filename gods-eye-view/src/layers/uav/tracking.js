/**
 * @module uav/tracking
 * @description Click-to-track and pick ownership for the UAV layer.
 *
 * Two things were missing and both are fixed here (PLAN §3.1 acceptance 2):
 *
 * 1. **Pick ownership.** Without a `registerPickOwner('uav', …)` predicate the
 *    sibling layers classify a click on a drone as empty space and clear the
 *    operator's existing track while tracking no drone. The layer now answers
 *    for every entity id it owns.
 * 2. **Click-to-track.** The layer had `track()` / `untrack()` but nothing that
 *    turned a click into a call, so the only route into the cockpit was a
 *    button hardcoded to one vehicle. Multi-vehicle handoff (acceptance 4)
 *    needs per-entity selection, so the handler tracks the drone that was
 *    actually clicked and announces it.
 *
 * GEV stays read-only for flight control: selecting a drone changes the camera
 * and the readout, never the aircraft.
 */
import * as Cesium from 'cesium';
import {
  bindTrackingClickGesture,
  isTrackingClickGesture,
  isTrackingSelectionGesture,
} from '../../data/trackingClickGesture.js';
import { LAYER_ID, TRAIL_PREFIX, VEHICLE_PREFIX } from './policy.js';

/**
 * Create the tracking owner for one layer.
 * @param {{state: object, services: object, parts: object, options: object}} context Layer context.
 * @returns {object} Tracking methods.
 */
export function createTracking({ state, services, parts, options }) {
  const { resolvePickId, isOwnedByOtherLayer } = services.picking;
  const { selectTrackedSubjectContext, clearTrackedSubjectContext } =
    services.context;

  function emit(type, detail) {
    if (
      typeof window === 'undefined' ||
      !window.dispatchEvent ||
      typeof CustomEvent === 'undefined'
    )
      return;
    window.dispatchEvent(new CustomEvent(type, { detail }));
  }

  function cockpitModeActive() {
    return (
      typeof document !== 'undefined' &&
      document.body?.classList?.contains('cockpit-mode') === true
    );
  }

  /**
   * True when a picked id belongs to this layer. Registered with the shared
   * pick registry so siblings leave UAV picks alone.
   * @param {string} pickedId Canonical pick id.
   * @returns {boolean} Ownership.
   */
  function ownsPick(pickedId) {
    return state.ownedIds.has(String(pickedId));
  }

  /**
   * Vehicle reference behind a picked id, if any.
   * @param {string|null} pickedId Canonical pick id.
   * @returns {string|null} Vehicle reference.
   */
  function referenceForPickId(pickedId) {
    if (!pickedId) return null;
    const id = String(pickedId);
    for (const prefix of [VEHICLE_PREFIX, TRAIL_PREFIX]) {
      if (!id.startsWith(prefix)) continue;
      const reference = id.slice(prefix.length);
      if (state.entities.has(reference)) return reference;
    }
    return null;
  }

  function subjectMetadata(reference) {
    const record = state.entities.get(reference)?.record;
    if (!record) return null;
    return {
      layerId: LAYER_ID,
      id: reference,
      label: record.label || reference,
      kind: 'uav',
      latitude: record.position?.latitude ?? null,
      longitude: record.position?.longitude ?? null,
      altitudeM:
        record.position?.ellipsoidAltitude ?? record.position?.altitude ?? null,
    };
  }

  /**
   * Track one drone. Recording the intent first means a track() that arrives
   * before the entity exists (poll race) is applied on its first render.
   * @param {string} reference Vehicle reference.
   * @param {{origin: string}} [options] Selection provenance.
   * @returns {void}
   */
  function track(reference, { origin = 'programmatic' } = {}) {
    state.trackedReference = reference;
    const slot = state.entities.get(reference);
    if (!slot || !state.viewer) return;
    slot.entity.gevSelectionOrigin = origin;
    state.viewer.trackedEntity = slot.entity;
    const metadata = subjectMetadata(reference);
    if (metadata) selectTrackedSubjectContext(metadata);
    const position = parts.motion.positionAt(reference);
    emit('gev:awareness-subject-selected', {
      layerId: LAYER_ID,
      id: reference,
      label: metadata?.label || reference,
      position: position ? Cesium.Cartesian3.clone(position) : null,
      origin,
    });
    // Dedicated lane so the shell can open the cockpit on the drone the
    // operator actually clicked instead of a hardcoded vehicle name.
    emit('gev:uav-vehicle-selected', {
      layerId: LAYER_ID,
      reference,
      label: metadata?.label || reference,
      origin,
    });
  }

  /** Release the tracked drone and the shared context slot. */
  function untrack() {
    state.trackedReference = null;
    if (state.viewer) state.viewer.trackedEntity = undefined;
    clearTrackedSubjectContext(LAYER_ID);
  }

  /**
   * Mission row the bridge published for a vehicle, when it has shipped
   * `missions[]` (BRIDGE_CONTRACT §missions).
   * @param {string} reference Vehicle reference.
   * @returns {object|null} Mission row.
   */
  function missionFor(reference) {
    return (
      state.missions.find(
        (mission) => (mission.vehicle ?? mission.vehicle_id) === reference,
      ) || null
    );
  }

  /**
   * Tracked drone descriptor in the flight-layer shape the cockpit reads,
   * extended with the mission/fuel figures PLAN §3.1 acceptance 2 puts in the
   * HUD (fuel %, ETA-to-BINGO, phase and progress).
   * @returns {object|null} Tracked descriptor, or null when nothing is tracked.
   */
  function getTrackedInfo() {
    const reference = state.trackedReference;
    if (!reference) return null;
    const record = state.entities.get(reference)?.record;
    if (!record) return null;
    const position = record.position || {};
    const velocity = record.velocity || {};
    const status = record.status || {};
    const mission = missionFor(reference);
    const live =
      parts.motion.positionAt(reference) ??
      Cesium.Cartesian3.fromDegrees(
        position.longitude,
        position.latitude,
        position.ellipsoidAltitude ?? position.altitude ?? 0,
      );
    return {
      layerId: LAYER_ID,
      icao24: reference,
      callsign: record.label || reference,
      position: Cesium.Cartesian3.clone(live),
      latitude: position.latitude,
      longitude: position.longitude,
      altitudeM: position.ellipsoidAltitude ?? position.altitude ?? 0,
      aglM: Number.isFinite(position.agl) ? position.agl : null,
      onGround: status.landedState === 0,
      velocityMps: Number.isFinite(velocity.speed) ? velocity.speed : null,
      track: Number.isFinite(velocity.heading) ? velocity.heading : null,
      verticalRateMps: Number.isFinite(velocity.verticalRate)
        ? velocity.verticalRate
        : null,
      pitchDeg: record.attitude?.pitch ?? null,
      rollDeg: record.attitude?.roll ?? null,
      fuelPct: Number.isFinite(status.fuelPct) ? status.fuelPct : null,
      bingoFuelPct: Number.isFinite(status.bingoFuelPct)
        ? status.bingoFuelPct
        : null,
      etaToBingoS: Number.isFinite(status.etaToBingoS)
        ? status.etaToBingoS
        : null,
      missionId: status.mission || mission?.missionId || '',
      missionPhase: mission?.phase ?? null,
      missionProgressPct: Number.isFinite(mission?.progressPct)
        ? mission.progressPct
        : null,
      trackId: status.trackId || '',
      datumDegraded: status.datumDegraded === true || state.datumDegraded,
      stale: false,
    };
  }

  /**
   * Install the click-to-track handler.
   * @param {Cesium.Viewer} viewer Owning viewer.
   * @returns {void}
   */
  function installClickHandler(viewer) {
    if (state.clickHandler || !viewer) return;
    state.clickHandler = options.createClickHandler
      ? options.createClickHandler(viewer)
      : new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    bindTrackingClickGesture(state.clickHandler, (click, gesture) => {
      // A camera drag that happens to finish over a drone is a drag.
      if (!isTrackingSelectionGesture(gesture)) return;
      // Cockpit mode owns the camera and its own subject; globe clicks are
      // inert until the operator leaves it.
      if (cockpitModeActive()) return;
      const picked = viewer.scene.pick(click.position);
      if (picked) {
        const pickedId = resolvePickId(picked);
        const reference = referenceForPickId(pickedId);
        if (reference) {
          if (reference !== state.trackedReference)
            track(reference, { origin: 'user' });
          return;
        }
        // Our OWN non-vehicle entities — a numbered contact, its SALUTE
        // caption, a mission-overlay waypoint — are not empty space either.
        // Reading a contact marker must never drop the drone the operator is
        // flying with.
        if (pickedId && ownsPick(pickedId)) return;
        // Someone else's contact is not empty space — let that layer have it.
        if (pickedId && isOwnedByOtherLayer(LAYER_ID, pickedId)) return;
      }
      // Empty space releases tracking, but only on a clean short click.
      if (!isTrackingClickGesture(gesture)) return;
      if (state.trackedReference) untrack();
    });
  }

  /** Remove the click handler (disable/destroy). */
  function removeClickHandler() {
    if (!state.clickHandler) return;
    state.clickHandler.destroy?.();
    state.clickHandler = null;
  }

  return {
    ownsPick,
    referenceForPickId,
    track,
    untrack,
    getTrackedInfo,
    installClickHandler,
    removeClickHandler,
  };
}
