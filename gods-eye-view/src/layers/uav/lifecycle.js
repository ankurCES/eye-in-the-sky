/**
 * @module uav/lifecycle
 * @description Layer lifetime: scene ownership, pick registration and the
 * render-governor hold.
 *
 * Two of these are new and both are load-bearing. The pick owner stops sibling
 * layers from treating a click on a drone as empty space, and the continuous
 * render hold is what lets interpolated motion actually reach the screen — with
 * the scene in `requestRenderMode` and no hold, an untracked drone repaints
 * only on the layer's own poll-tick render request, which is exactly the 5 Hz
 * stutter the sampled properties exist to remove.
 */
import * as Cesium from 'cesium';
import { ENTITY_DATA_SOURCE, LAYER_ID } from './policy.js';

/**
 * Create the lifecycle owner for one layer.
 * @param {{state: object, services: object, parts: object, layer: object}} context Layer context.
 * @returns {{methods: object}} Lifecycle methods mixed into the layer.
 */
export function createLifecycle({ state, services, parts, layer, options }) {
  const { registerPickOwner, unregisterPickOwner } = services.picking;
  const { holdContinuousRender, releaseContinuousRender } = services.render;
  const { tracking, rendering, overlay, motion } = parts;

  function claimScene(viewer) {
    registerPickOwner(LAYER_ID, tracking.ownsPick);
    holdContinuousRender(LAYER_ID);
    // Headless construction (unit tests, server-side composition) has no canvas
    // to bind to; the layer is fully functional without the handler.
    if (options.createClickHandler || viewer?.scene?.canvas)
      tracking.installClickHandler(viewer);
  }

  const methods = {
    /**
     * Configure the source before initialization; an active layer keeps its own.
     * @param {object} next Live source exposing getSnapshot.
     * @returns {void}
     */
    setSource(next) {
      if (state.viewer)
        throw new Error('Configure the source before layer initialization');
      if (typeof next?.getSnapshot !== 'function')
        throw new TypeError('A snapshot source is required');
      state.source = next;
      layer.source = next.label || layer.source;
    },

    /**
     * Create the entity data source and take scene ownership.
     * @param {Cesium.Viewer} viewer Owning viewer.
     * @returns {void}
     */
    init(viewer) {
      if (state.viewer) throw new Error('UAV layer is already initialized');
      if (typeof state.source?.getSnapshot !== 'function')
        throw new TypeError('A snapshot source is required');
      state.viewer = viewer;
      state.collection = new Cesium.CustomDataSource(ENTITY_DATA_SOURCE);
      viewer.dataSources.add(state.collection);
      claimScene(viewer);
    },

    /**
     * Show the layer and re-claim scene ownership.
     * @param {Cesium.Viewer} [viewer] Owning viewer.
     * @returns {Promise<boolean>} Always true.
     */
    async enable(viewer) {
      if (viewer && !state.viewer) methods.init(viewer);
      state.enabled = true;
      if (state.collection) state.collection.show = true;
      if (state.overlayCollection) state.overlayCollection.show = true;
      claimScene(viewer || state.viewer);
      return true;
    },

    /**
     * Hide the layer, release the render hold and stop intercepting clicks.
     * @returns {Promise<boolean>} Always true.
     */
    async disable() {
      state.enabled = false;
      if (state.collection) state.collection.show = false;
      if (state.overlayCollection) state.overlayCollection.show = false;
      unregisterPickOwner(LAYER_ID);
      releaseContinuousRender(LAYER_ID);
      tracking.removeClickHandler();
      return true;
    },

    /**
     * Tear the layer down: both data sources, every handler and all state.
     * @param {Cesium.Viewer} [viewer] Owning viewer.
     * @returns {void}
     */
    destroy(viewer) {
      const owner = viewer || state.viewer;
      unregisterPickOwner(LAYER_ID);
      releaseContinuousRender(LAYER_ID);
      tracking.removeClickHandler();
      overlay.destroy(owner);
      if (owner && state.collection)
        owner.dataSources.remove(state.collection, true);
      rendering.clear();
      motion.clear();
      state.trackedReference = null;
      state.missions = [];
      state.contacts = [];
      state.datumDegraded = false;
      state.observedAtMs = null;
      state.simState = null;
      state.lastError = null;
      state.retryAt = 0;
      state.enabled = false;
      state.collection = null;
      state.viewer = null;
    },
  };

  return { methods };
}
