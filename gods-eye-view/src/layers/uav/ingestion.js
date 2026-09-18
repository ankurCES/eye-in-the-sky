/**
 * @module uav/ingestion
 * @description The poll tick: acquire one bridge snapshot, render it, and
 * refresh the mission overlay when the mission state moved.
 *
 * The layer is armed by its `refreshInterval`, so this runs at the bridge's
 * 5 Hz. Everything expensive is gated: vehicles interpolate between fixes
 * rather than being rewritten, scalar writes are capped, and the mission
 * overlay refreshes on a mission-state change instead of on every tick.
 */
import { ERROR_BACKOFF_MS, MISSING_POLL_LIMIT } from './policy.js';

/**
 * Create the ingestion owner for one layer.
 * @param {{state: object, services: object, parts: object}} context Layer context.
 * @returns {{methods: object}} Ingestion methods mixed into the layer.
 */
export function createIngestion({ state, services, parts }) {
  const { rendering, overlay } = parts;

  /** Contacts come from `contacts[]`; the `/tracks` rows are the fallback. */
  function contactRows(snapshot) {
    const contacts = Array.isArray(snapshot?.contacts) ? snapshot.contacts : [];
    if (contacts.length) return contacts;
    return Array.isArray(snapshot?.targets) ? snapshot.targets : [];
  }

  function evictMissing(present) {
    for (const reference of [...state.entities.keys()]) {
      if (present.has(reference)) {
        state.missingPolls.delete(reference);
        continue;
      }
      const missed = (state.missingPolls.get(reference) ?? 0) + 1;
      state.missingPolls.set(reference, missed);
      if (missed >= MISSING_POLL_LIMIT) rendering.removeVehicle(reference);
    }
  }

  const methods = {
    /**
     * Poll the bridge and render one snapshot.
     * @param {Cesium.Viewer} [viewer] Viewer supplied by the lifecycle manager.
     * @param {{signal: AbortSignal|null}} [options] Cancellation.
     * @returns {Promise<{status: string, ids: Set<string>}>} Tick outcome.
     */
    async update(viewer, { signal = null } = {}) {
      if (!state.viewer) return { status: 'not-initialized', ids: new Set() };
      const usedViewer = viewer || state.viewer;
      const nowMs = state.now();
      if (state.retryAt && nowMs < state.retryAt) {
        return {
          status: 'error',
          ids: new Set(),
          backoff: true,
          message: state.lastError?.message,
        };
      }
      try {
        const snapshot = await state.source.getSnapshot({}, { signal });
        state.observedAtMs = snapshot.observedAtMs ?? state.now();
        state.simState = snapshot.simState ?? null;
        state.missions = Array.isArray(snapshot.missions)
          ? snapshot.missions
          : [];
        state.contacts = Array.isArray(snapshot.contacts)
          ? snapshot.contacts
          : [];
        state.datumDegraded = snapshot.datumDegraded === true;

        const ids = new Set();
        for (const record of snapshot.records || []) {
          // A record that cannot be placed is skipped, not counted present:
          // it ages out through the missed-poll path like any other silence.
          const reference = rendering.upsertVehicle(record, state.observedAtMs);
          if (reference) ids.add(reference);
        }
        evictMissing(ids);

        // Numbered contact markers (PLAN §3.1 item 3, M11).
        const seen = new Set();
        for (const row of contactRows(snapshot)) {
          const trackId = rendering.upsertContact(row);
          if (trackId) seen.add(trackId);
        }
        rendering.pruneContacts(seen);

        // Mission geometry lands in its own data source, and only when the
        // mission state actually moved (BRIDGE_CONTRACT T6).
        await overlay.refresh(snapshot, { signal });

        usedViewer.scene?.requestRender?.();
        services.render.governorRequestRender('uav-poll');
        state.lastError = null;
        state.retryAt = 0;
        return {
          status: 'ok',
          ids,
          observedAtMs: state.observedAtMs,
          simState: state.simState,
        };
      } catch (error) {
        if (signal?.aborted || error?.name === 'AbortError') throw error;
        state.lastError = error;
        state.retryAt = state.now() + (error?.retryAfterMs ?? ERROR_BACKOFF_MS);
        return { status: 'error', ids: new Set(), message: error?.message };
      }
    },
  };

  return { methods };
}
