/**
 * Shared fixtures for the UAV layer unit tests. Not part of the runtime layer:
 * the suite is the only importer.
 */
import * as Cesium from 'cesium';

/** One normalized vehicle record in the shape src/sources/live/uav.js emits. */
export function uavRecord(overrides = {}) {
  const position = { ...UAV_BASE.position, ...(overrides.position || {}) };
  const velocity = { ...UAV_BASE.velocity, ...(overrides.velocity || {}) };
  const status = { ...UAV_BASE.status, ...(overrides.status || {}) };
  return {
    ...UAV_BASE,
    ...overrides,
    position,
    velocity,
    status,
  };
}

const UAV_BASE = {
  id: 'Drone1',
  reference: 'Drone1',
  label: 'Drone1',
  kind: 'uav',
  position: {
    latitude: 47.6419,
    longitude: -122.1395,
    ellipsoidAltitude: 93.2,
    agl: 28,
  },
  velocity: { speed: 12, heading: 61, verticalRate: 0 },
  attitude: { pitch: 2, roll: -1 },
  status: { fuelPct: 87, armed: true, landedState: 2 },
  observedAtMs: 1789620000000,
};

/** Stub viewer with a real CustomDataSource-backed entities collection. */
export function stubViewer() {
  const dataSources = new Cesium.DataSourceCollection();
  return {
    dataSources,
    scene: { requestRender: () => {}, pick: () => undefined },
    trackedEntity: undefined,
  };
}

/**
 * A source whose snapshot the test can rewrite between polls.
 * @param {object} snapshot Initial snapshot body.
 * @returns {object} Source with a mutable `snapshot` and a `calls` counter.
 */
export function mutableSource(snapshot) {
  const source = {
    label: 'godSeye UAV (AirSim)',
    snapshot: {
      source: 'uav',
      status: 'ok',
      simState: 'up',
      observedAtMs: 1789620000000,
      records: [],
      ...snapshot,
    },
    calls: 0,
    async getSnapshot() {
      source.calls += 1;
      return source.snapshot;
    },
  };
  return source;
}

/** ScreenSpaceEventHandler stand-in: no canvas, replayable input actions. */
export function fakeClickHandler() {
  const actions = new Map();
  return {
    destroyed: false,
    actions,
    setInputAction(action, type) {
      actions.set(type, action);
    },
    removeInputAction(type) {
      actions.delete(type);
    },
    destroy() {
      this.destroyed = true;
    },
    fire(type, event) {
      actions.get(type)?.(event);
    },
    /** Replay a clean press-and-release over one screen position. */
    click(position) {
      this.fire(Cesium.ScreenSpaceEventType.LEFT_DOWN, { position });
      this.fire(Cesium.ScreenSpaceEventType.LEFT_UP, { position });
      this.fire(Cesium.ScreenSpaceEventType.LEFT_CLICK, { position });
    },
  };
}

/** Read a Cesium property that may be constant or callback-backed. */
export function value(property, time = Cesium.JulianDate.now()) {
  if (property === undefined || property === null) return property;
  return typeof property.getValue === 'function'
    ? property.getValue(time)
    : property;
}

/** Longitude in degrees of an ECEF position. */
export function longitudeOf(cartesian) {
  return Cesium.Math.toDegrees(
    Cesium.Cartographic.fromCartesian(cartesian).longitude,
  );
}
