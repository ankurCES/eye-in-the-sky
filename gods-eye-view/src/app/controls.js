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
    (typeof localStorage !== 'undefined' && localStorage.getItem('gev.uav.base')) ||
    import.meta.env?.VITE_UAV_BRIDGE_URL ||
    'http://localhost:8790';
  const uavBridgeToken = () =>
    (typeof localStorage !== 'undefined' && localStorage.getItem('gev.uav.token')) ||
    import.meta.env?.VITE_UAV_BRIDGE_TOKEN ||
    'dev-token';
  const uavLayer = catalog?.get?.('uav');
  const dataManager = styleManager._dataManager;
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
    onEnterCockpit: (reference) => {
      if (!uavLayer?.track) return false;
      const cockpit = styleManager._cockpitCoordinator?.cockpitView;
      if (!cockpit?.enter) return false;
      const trackAndEnter = () => {
        uavLayer.track(reference);
        return cockpit.enter() === true;
      };
      // Enable the UAV AirSim layer so it is live, then track + enter cockpit.
      const enabled = dataManager?.isEnabled?.('uav');
      if (enabled === false && dataManager?.setEnabled) {
        // Kick off enable; the poll loop starts populating entities.
        Promise.resolve(dataManager.setEnabled('uav', true)).catch(() => {});
      }
      if (trackAndEnter()) return true;
      // Entity may not exist yet (poll race / just enabled): track() already
      // recorded the intent, so retry once after the next poll tick.
      setTimeout(() => {
        uavLayer.track(reference);
        cockpit.enter?.();
      }, 1500);
      return true;
    },
  }).mount();
  defer(() => uavMissionPanel.destroy());

  // If no share link state, do default fly-to Austin
  if (!styleManager.hasShareState) {
    loaderStatus.textContent = 'Flying to Austin, TX...';
    defer(flyToAustin(viewer));
  } else {
    loaderStatus.textContent = 'Restoring shared view...';
  }

  return { styleManager, weatherEffects, cockpitCloudEffects };
}
