import flights from '../data/flights.js';
import military from '../data/militaryFlights.js';
import vessels from '../data/aisLiveVessels.js';
import { configureAlprSource } from '../data/alprCameras.js';
import { configureCctvSource } from '../data/cctv.js';
import { configureRadioSource } from '../data/radio.js';
import { configureTrafficSource } from '../data/traffic.js';
import { configureBikeshareSource } from '../data/bikeshare.js';
import { configureInstallationSource } from '../data/militaryInstallations.js';
import { configureSatelliteSource } from '../data/satellites.js';
import { configureLaunchSource } from '../data/rocketLaunches.js';
import { configureFirmsSource } from '../data/firmsHeatmap.js';
import { configureMilitaryRegistrySource } from '../data/militaryRegistry.js';
import { configureUavSource } from '../sources/live/uav.js';
configureUavSource({
  baseUrl:
    (typeof localStorage !== 'undefined' && localStorage.getItem('gev.uav.base')) ||
    import.meta.env?.VITE_UAV_BRIDGE_URL,
  token:
    (typeof localStorage !== 'undefined' && localStorage.getItem('gev.uav.token')) ||
    import.meta.env?.VITE_UAV_BRIDGE_TOKEN,
});
const configure = {
  alpr: configureAlprSource,
  cctv: configureCctvSource,
  radio: configureRadioSource,
  traffic: configureTrafficSource,
  bikeshare: configureBikeshareSource,
  installations: configureInstallationSource,
  satellites: configureSatelliteSource,
  launches: configureLaunchSource,
  firms: configureFirmsSource,
};
/** Configure sources before any registration or state restoration starts. */
export function configureApplicationSources({
  layers = {},
  live = {},
  signal,
  defer,
}) {
  for (const [name, source] of Object.entries(layers)) {
    if (!configure[name]) throw new TypeError(`Unknown layer source: ${name}`);
    defer(configure[name](source));
  }
  if (live.flights) flights.setSource(live.flights);
  if (live.military) {
    military.setSource(live.military);
    defer(configureMilitaryRegistrySource(live.military, { signal }));
  }
  if (live.vessels) vessels.setSource(live.vessels);
}
export const liveLayers = Object.freeze({ flights, military, vessels });
