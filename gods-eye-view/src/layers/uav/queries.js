/**
 * @module uav/queries
 * @description Read-only views of the layer for the panel, the HUD and the
 * lifecycle manager's failure classification.
 */

/**
 * Create the query owner for one layer.
 * @param {{state: object, parts: object}} context Layer context.
 * @returns {{methods: object}} Query methods mixed into the layer.
 */
export function createQueries({ state, parts }) {
  const methods = {
    /**
     * Layer statistics. `lastError` is the field the lifecycle manager reads to
     * classify a refresh, so it stays a string-or-null exactly as before.
     * @returns {object} Counters and feed status.
     */
    getStats() {
      return {
        count: state.entities.size,
        vehicles: state.entities.size,
        contacts: state.targets.size,
        missions: state.missions.length,
        lastUpdate: state.observedAtMs,
        observedAtMs: state.observedAtMs,
        simState: state.simState,
        datumDegraded: state.datumDegraded,
        missionOverlay: parts.overlay.getStatus(),
        lastError: state.lastError?.message ?? null,
      };
    },

    /**
     * Mission rows for a vehicle, or all of them (BRIDGE_CONTRACT §missions).
     * @param {string} [reference] Vehicle reference.
     * @returns {object[]} Mission rows.
     */
    getMissions(reference) {
      if (!reference) return [...state.missions];
      return state.missions.filter(
        (mission) => (mission.vehicle ?? mission.vehicle_id) === reference,
      );
    },

    /**
     * Contact roster as last served (BRIDGE_CONTRACT §contacts).
     * @returns {object[]} Contact rows.
     */
    getContacts() {
      return [...state.contacts];
    },

    /**
     * Identity passthrough kept for the analyst export path.
     * @param {object} record Normalized UAV record.
     * @returns {object} The same record.
     */
    mapAnalystRecord(record) {
      return record;
    },
  };

  return { methods };
}
