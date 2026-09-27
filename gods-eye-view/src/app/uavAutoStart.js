/**
 * Boot-time UAV start: switch the UAV layer on once GEV is up and put the
 * camera over the lead drone's theater.
 *
 * Ported from the ObraMaestra controls.js edit with its defects fixed:
 *   - it was wrapped in `defer(...)`, which registers a TEARDOWN callback, so
 *     at startup nothing ran at all (and the Austin fly-in it replaced was
 *     gone too); here it runs from the tools phase, when the layer data
 *     manager exists;
 *   - it called `uavLayer.enable()` directly, bypassing the data manager, so
 *     the layer rail never knew and the entity collection was never created;
 *     here it goes through `dataManager.setEnabled('uav', true)` with a
 *     programmatic origin (not persisted, so the next boot does not restore it
 *     and pop the mission drawer);
 *   - with a share link it could run twice (the settled event AND a 3.5 s
 *     safety timeout both called it); `start()` memoises one run.
 * A share link is an explicit request for a view, so it is never overridden.
 */

/**
 * The first reported drone with a position fix, from a normalized snapshot
 * (src/sources/live/uav.js `records[]`).
 * @param {object} snapshot
 * @returns {{reference: string, latitude: number, longitude: number, altitude: number}|null}
 */
export function leadVehiclePosition(snapshot) {
  const records = Array.isArray(snapshot?.records) ? snapshot.records : [];
  for (const record of records) {
    const position = record?.position;
    const latitude = position?.latitude;
    const longitude = position?.longitude;
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) continue;
    const altitude = [position.ellipsoidAltitude, position.altitude].find(
      Number.isFinite,
    );
    return {
      reference: String(record.reference ?? record.id ?? ''),
      latitude,
      longitude,
      altitude: altitude ?? 0,
    };
  }
  return null;
}

/**
 * @param {object} deps
 * @param {() => Promise<unknown>} deps.enableLayer turns the UAV layer on
 * @param {() => Promise<object>} deps.getSnapshot normalized UAV snapshot
 * @param {(lead: object) => void} deps.flyTo moves the camera
 * @param {() => boolean} [deps.shouldFly] false keeps the current view
 * @param {AbortSignal} [deps.signal] application lifetime
 * @returns {{start: () => Promise<{enabled: boolean, flew: boolean, reason: string}>}}
 */
export function createUavAutoStart({
  enableLayer,
  getSnapshot,
  flyTo,
  shouldFly = () => true,
  signal = null,
}) {
  let run = null;

  async function once() {
    const outcome = { enabled: false, flew: false, reason: '' };
    try {
      await enableLayer();
      outcome.enabled = true;
    } catch {
      outcome.reason = 'layer';
      return outcome;
    }
    if (signal?.aborted) return { ...outcome, reason: 'aborted' };
    if (!shouldFly()) return { ...outcome, reason: 'view' };
    let snapshot = null;
    try {
      snapshot = await getSnapshot();
    } catch {
      // Bridge not up: the layer keeps polling on its own; the view stays.
      return { ...outcome, reason: 'bridge' };
    }
    if (signal?.aborted) return { ...outcome, reason: 'aborted' };
    const lead = leadVehiclePosition(snapshot);
    if (!lead) return { ...outcome, reason: 'position' };
    flyTo(lead);
    return { ...outcome, flew: true };
  }

  return {
    /** Run the start once; later calls return the same promise. */
    start() {
      run ||= once();
      return run;
    },
  };
}
