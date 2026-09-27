import * as Cesium from 'cesium';
import { catalogControlServices } from './catalog.js';
import { StyleManager } from '../ui/composition.js';
import { flyToAustin } from '../camera.js';
import { initCockpitCloudEffects } from '../cockpitCloudEffects.js';
import { createUavMissionPanel } from '../ui/uavMissionPanel.js';
import { createUavSource } from '../sources/live/uav.js';
import { getKeyholeGeometry } from '../celestialRing.js';
import { uavBridgeToken, uavBridgeUrl } from './uavBridge.js';
import { createTrackingPort } from './trackingPort.js';
import { createUavAutoStart } from './uavAutoStart.js';

/**
 * Construct the existing controls and camera presentation.
 *
 * `intelConsole` is set when the intelligence console (src/console) owns the
 * landing view: `{onTrackingPort(port|null)}` receives the tracking port the
 * console drives, and the UAV mission drawer no longer pops open whenever the
 * UAV layer is switched on (the console enables it for its own reasons). The
 * first-run launcher is suppressed by the same option in the tools phase.
 */
export function createApplicationControls({
  scene: { viewer, mapStackController, operations },
  loaderStatus,
  Controls = StyleManager,
  services,
  catalog,
  placeSearch,
  intelConsole = null,
  signal = null,
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
  // Origin and token come from the one resolver (./uavBridge.js): the in-app
  // host's injected config, then localStorage, then the build env.
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
  //
  // Not when the intelligence console owns the view: it enables the layer at
  // boot and on every Track, and the drawer is opened only on request there
  // (trackingPort.openMissionPanel).
  if (!intelConsole && uavLayer && typeof uavLayer.enable === 'function') {
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

  // The console's seam onto the map: cockpit tracking, map visibility, the
  // dock inset and the keyhole. Always built (it is inert until asked), handed
  // to the console only when one is mounted.
  const trackingPort = createTrackingPort({
    viewer,
    getCockpit: () => styleManager._cockpitCoordinator?.cockpitView,
    uavLayer,
    missionPanel: uavMissionPanel,
    keyholeGeometry: getKeyholeGeometry,
  });
  defer(() => trackingPort.destroy());
  if (typeof intelConsole?.onTrackingPort === 'function') {
    intelConsole.onTrackingPort(trackingPort);
    defer(() => intelConsole.onTrackingPort(null));
  }

  // If no share link state, do default fly-to Austin
  if (!styleManager.hasShareState) {
    loaderStatus.textContent = 'Flying to Austin, TX...';
    defer(flyToAustin(viewer));
  } else {
    loaderStatus.textContent = 'Restoring shared view...';
  }

  // Once GEV is up, switch the UAV layer on and put the camera over the lead
  // drone's theater (the Austin fly-in above is what shows until then, and
  // what stays when the bridge is down). Runs once; see ./uavAutoStart.js.
  let attachedData = null;
  let uavStartSource = null;
  const uavAutoStart = createUavAutoStart({
    enableLayer: () =>
      attachedData?.setEnabled('uav', true, {
        origin: 'programmatic',
      }),
    getSnapshot: () => {
      uavStartSource ||= createUavSource({
        baseUrl: uavBridgeUrl,
        token: uavBridgeToken,
      });
      return uavStartSource.getSnapshot({}, {});
    },
    // A share link is an explicit request for a view; never override it.
    shouldFly: () => !styleManager.hasShareState && !viewer.isDestroyed?.(),
    flyTo: ({ latitude, longitude, altitude }) => {
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(
          longitude,
          latitude,
          altitude + 25000,
        ),
        duration: 3.0,
      });
    },
    signal,
  });

  /**
   * Called by the tools phase once the layer data manager exists: the port
   * becomes ready (whenReady resolves) and the UAV start runs, after any
   * share-link restoration has settled.
   */
  function attachData(dataManager) {
    if (attachedData || !dataManager) return;
    attachedData = dataManager;
    const startup = Promise.resolve(styleManager.initialRestorePromise)
      .catch(() => {
        /* restoration reports its own outcome */
      })
      .then(() => (signal?.aborted ? null : uavAutoStart.start()))
      .catch(() => {
        /* the start is best effort; the layer rail still works */
      });
    // The port's enter() lets this start finish first: its setEnabled('uav')
    // announces `visibility`, which would drop a cockpit that just opened.
    trackingPort.attachData(dataManager, { startup });
  }

  return {
    styleManager,
    weatherEffects,
    cockpitCloudEffects,
    uavMissionPanel,
    trackingPort,
    attachData,
  };
}
