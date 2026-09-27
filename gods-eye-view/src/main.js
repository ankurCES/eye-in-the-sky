import { createStandaloneApplication } from './standalone/application.js';
import { describeError } from './standalone/errors.js';
import { mountIntelConsole } from './console/index.js';
import { createDeferredTrackingPort } from './app/trackingPort.js';
import { currentUavBridge } from './app/uavBridge.js';

// The intelligence console is the landing view: it mounts BEFORE the map
// starts, renders at once above GEV's loading screen, and drives the map only
// through the tracking port, which exists once GEV's controls do.
// `?console=off` restores the plain map application (first-run launcher and
// all).
const consoleRequested =
  new URLSearchParams(window.location.search).get('console') !== 'off';

let intelConsole = null;
let trackingPort = null;
if (consoleRequested) {
  trackingPort = createDeferredTrackingPort();
  try {
    intelConsole = mountIntelConsole({
      root: document.body,
      config: currentUavBridge(),
      trackingPort,
    });
    // The console opens on the orb; the map stays hidden (and suspended) until
    // it tracks a drone. Remembered by the deferred port until GEV attaches.
    trackingPort.setMapVisible(false);
  } catch (error) {
    // A console that cannot mount must not take the map down with it.
    console.error('Intelligence console failed to mount:', error);
    intelConsole = null;
    trackingPort = null;
  }
}

/** Tell the console how the map is doing (NEEDS: the console renders it). */
function reportMapStatus(status) {
  try {
    intelConsole?.ctx?.bus?.emit?.('gev:status', status);
  } catch {
    /* the loader line below still carries it */
  }
}

const application = createStandaloneApplication({
  googleApiKey: import.meta.env.GOOGLE_MAPS_API_KEY,
  cesiumToken: import.meta.env.CESIUM_ION_TOKEN,
  allowQaRegistration: import.meta.env.DEV,
  intelConsole: trackingPort
    ? { onTrackingPort: (port) => trackingPort.attach(port) }
    : null,
});

if (intelConsole)
  application.subscribe(({ status, phase }) =>
    reportMapStatus({ state: status, phase }),
  );

application.start().catch((error) => {
  console.error("God's Eye View initialization failed:", error);
  const message = describeError(error);
  // The loader sits under the console, so it is told too: tracking cannot
  // work, and whenReady()/enter() now say so instead of waiting forever.
  trackingPort?.fail(error);
  reportMapStatus({ state: 'failed', phase: null, message });
  const loaderStatus = document.querySelector('#loading-screen .loader-status');
  loaderStatus.textContent = `Error: ${message}`;
  loaderStatus.style.color = '#ff4444';
});

export { application, intelConsole };
