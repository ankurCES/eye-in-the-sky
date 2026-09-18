/**
 * @module uav/state
 * @description One UAV layer's mutable scene state plus the normalized view of
 * the application services injected into it.
 *
 * Layers receive capabilities through `services` (see docs/UI-OWNERSHIP.md) and
 * never import application modules directly. Every service is optional so the
 * layer can be constructed headless — `createUavLayer({ source })` in a unit
 * test gets inert shims and behaves exactly as before.
 */

/** Resolve a pick result to a canonical string id without the registry. */
function fallbackResolvePickId(picked) {
  if (!picked) return null;
  const unwrap = (id) => {
    if (id === null || id === undefined) return undefined;
    if (typeof id === 'object')
      return typeof id.id === 'string' || typeof id.id === 'number'
        ? id.id
        : undefined;
    return id;
  };
  let id = unwrap(picked.id);
  if (id === undefined) id = unwrap(picked.primitive?.id);
  return typeof id === 'string' || typeof id === 'number' ? String(id) : null;
}

/**
 * Fill every service the layer consumes with an inert default.
 * @param {object} [services] Application-supplied service namespaces.
 * @returns {{picking: object, render: object, context: object}} Normalized services.
 */
export function normalizeServices(services = {}) {
  const picking = services.picking || {};
  const render = services.render || {};
  const context = services.context || {};
  return {
    picking: {
      registerPickOwner: picking.registerPickOwner || (() => {}),
      unregisterPickOwner: picking.unregisterPickOwner || (() => {}),
      resolvePickId: picking.resolvePickId || fallbackResolvePickId,
      isOwnedByOtherLayer: picking.isOwnedByOtherLayer || (() => false),
    },
    render: {
      holdContinuousRender: render.holdContinuousRender || (() => {}),
      releaseContinuousRender: render.releaseContinuousRender || (() => {}),
      governorRequestRender: render.governorRequestRender || (() => {}),
    },
    context: {
      selectTrackedSubjectContext:
        context.selectTrackedSubjectContext || (() => {}),
      clearTrackedSubjectContext:
        context.clearTrackedSubjectContext || (() => {}),
    },
  };
}

/**
 * Construct the per-layer state container.
 * @param {{source: object, pollMs: number, now: () => number}} options Layer options.
 * @returns {object} Mutable layer state.
 */
export function createUavState({ source, pollMs, now }) {
  return {
    source,
    pollMs,
    now,
    viewer: null,
    /** Entity snapshot data source (vehicles, trails, contacts). */
    collection: null,
    /** Mission overlay data source — created lazily, never the snapshot one. */
    overlayCollection: null,
    /** reference -> { entity, trailEntity, record, lastWriteMs } */
    entities: new Map(),
    /** reference -> Cesium.Cartesian3[] */
    trails: new Map(),
    /** reference -> consecutive missed polls */
    missingPolls: new Map(),
    /** track_id -> { entity, captionEntity, number } */
    targets: new Map(),
    /** Stable display numbers for contacts, in order of first appearance. */
    targetNumbers: new Map(),
    /** Entity ids this layer owns, for the shared pick registry. */
    ownedIds: new Set(),
    trackedReference: null,
    clickHandler: null,
    enabled: false,
    observedAtMs: null,
    simState: null,
    /** Newest `/snapshot.missions[]` rows (BRIDGE_CONTRACT §missions). */
    missions: [],
    /** Newest `/snapshot.contacts[]` rows (BRIDGE_CONTRACT §contacts). */
    contacts: [],
    /** Geoid degradation is operator-visible state, never hidden. */
    datumDegraded: false,
    lastError: null,
    retryAt: 0,
  };
}
