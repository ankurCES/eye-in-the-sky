import * as Cesium from 'cesium';
import { catalogControlServices } from './catalog.js';
import { StyleManager } from '../ui/composition.js';
import { flyToAustin } from '../camera.js';
import { initCockpitCloudEffects } from '../cockpitCloudEffects.js';
import { createUavMissionPanel } from '../ui/uavMissionPanel.js';

/** Construct the existing controls and camera presentation. */
export function createApplicationControls({
  scene: { viewer, mapStackController, operations },
  loaderStatus,
  Controls = StyleManager,
  services,
  catalog,
  placeSearch,
  defer,
}) {
  // Initialize the style manager (post-processing, HUD, locations, share links)
  const styleManager = new Controls(viewer, {
    services: {
      ...services,
      ...operations.surface.controlServices,
      searchAndFlyTo: operations.searchAndFlyTo,
      fetchRegionalBrief: (...args) =>
        operations.requests.regional.getBrief(...args),
      ...catalogControlServices(catalog),
    },
    requestServices: operations.requests,
    mapStackController,
    placeSearch,
  });
  defer(() => styleManager.orbitController.stop());
  defer(() => styleManager.hud.destroy());
  defer(() => styleManager.dispose());
  // The previous multi-canvas weather compositor remains disabled. Cockpit
  // clouds use a separate, capped low-resolution GPU pass that never attaches
  // Cesium fog or post-process stages and is fully stopped in map mode.
  const weatherEffects = null;
  const cockpitCloudEffects = initCockpitCloudEffects(viewer, {
    weatherService: operations.requests.weather,
  });
  defer(() => cockpitCloudEffects?.destroy());

  // UAV Mission Control panel: theater + mission selection, MCP-gated launch.
  // Read-only God's Eye; the bridge /control proxy is the only command path.
  const uavBridgeUrl = () =>
    (typeof localStorage !== 'undefined' &&
      localStorage.getItem('gev.uav.base')) ||
    import.meta.env?.VITE_UAV_BRIDGE_URL ||
    'http://localhost:8790';
  const uavBridgeToken = () =>
    (typeof localStorage !== 'undefined' &&
      localStorage.getItem('gev.uav.token')) ||
    import.meta.env?.VITE_UAV_BRIDGE_TOKEN ||
    'dev-token';
  const uavLayer = catalog?.get?.('uav');
  const uavMissionPanel = createUavMissionPanel({
    bridgeUrl: uavBridgeUrl,
    token: uavBridgeToken,
    viewer,
    Cesium,

    onTheater: ([lat, lon, alt]) => {
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(
          lon,
          lat,
          (alt || 0) + 25000,
        ),
        duration: 2.5,
      });
    },
    // MUST report whether the cockpit ACTUALLY entered. uavMissionPanel arms a
    // bounded retry when a mission launches and disarms it the instant this
    // returns true, so an optimistic "return true" here means the view never
    // switches: the UAV entity is created by the layer's poll loop, so the
    // first attempt right after LAUNCH normally fails, and that first failure
    // is precisely the case the retry exists for. track() still records the
    // intent, so a later attempt finds the entity already selected.
    onEnterCockpit: (reference) => {
      const cockpit = styleManager._cockpitCoordinator?.cockpitView;
      if (!uavLayer?.track || !cockpit?.enter) return false;
      uavLayer.track(reference);
      return cockpit.enter() === true;
    },
  }).mount();
  defer(() => uavMissionPanel.destroy());

  // Open the mission panel when the operator switches the UAV layer on from the
  // layer rail. Without this the green "UAV (AirSim)" toggle enables the layer
  // and nothing visible happens: the mission controls live in a separate panel
  // that mounts collapsed, so there is no way to fly anything until the
  // operator finds it and expands it by hand.
  //
  // Hooked onto the layer's own enable()/disable() rather than polling a data
  // manager: `styleManager._dataManager` does not exist (verified at runtime --
  // the optional chaining elsewhere in this file hides that), so any
  // isEnabled('uav') probe silently answers null forever.
  if (uavLayer && typeof uavLayer.enable === 'function') {
    const layerEnable = uavLayer.enable.bind(uavLayer);
    uavLayer.enable = async (...args) => {
      const result = await layerEnable(...args);
      // Never let a panel problem break enabling the layer itself.
      try {
        uavMissionPanel.expand?.();
      } catch {
        /* the layer is what matters here */
      }
      return result;
    };
    defer(() => {
      uavLayer.enable = layerEnable;
    });
  }

  // If no share link state, do default fly-to Austin
  if (!styleManager.hasShareState) {
    loaderStatus.textContent = 'Flying to Austin, TX...';
    defer(flyToAustin(viewer));
  } else {
    loaderStatus.textContent = 'Restoring shared view...';
  }

  return { styleManager, weatherEffects, cockpitCloudEffects };
}
