import test from 'node:test';
import assert from 'node:assert/strict';

import { createUavMissionHud, missionHudModel } from './uavHud.js';

/** Minimal document stub: createElement plus the accessors uavDom uses. */
function stubDocument() {
  const create = (tag) => ({
    tag,
    children: [],
    attrs: {},
    listeners: {},
    className: '',
    textContent: '',
    append(...kids) {
      this.children.push(...kids);
    },
    replaceChildren(...kids) {
      this.children = [...kids];
    },
    setAttribute(key, value) {
      this.attrs[key] = value;
    },
    removeAttribute(key) {
      delete this.attrs[key];
    },
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    },
    remove() {},
  });
  return { createElement: create, body: create('body') };
}

const MISSION = {
  id: 'msn-0007',
  vehicle: 'Drone1',
  kind: 'grid_search',
  phase: 'executing',
  activeTool: 'uav_fly_route',
  progressPct: 43.5,
  waypoint: { index: 6, of: 14 },
  etaS: 480,
  fuelPct: 62.1,
  bingoFuelPct: 24.8,
  coveragePct: 41.0,
  safety: { geofence: 'ok', proximityM: 310, bingoLatched: false },
  incompleteReason: null,
};

const VEHICLE = {
  reference: 'Drone1',
  position: {
    latitude: 33.72,
    longitude: 51.73,
    ellipsoidAltitude: 1620,
    mslAltitude: 1580,
    agl: 60,
  },
  status: {
    fuelPct: 62.1,
    bingoFuelPct: 24.8,
    etaToBingoS: 930,
    datumDegraded: false,
  },
};

test('the HUD model carries phase, tool, progress, coverage and BINGO', () => {
  const model = missionHudModel({ mission: MISSION, vehicle: VEHICLE });
  assert.equal(model.hasMission, true);
  assert.equal(model.phase, 'executing');
  assert.equal(model.activeTool, 'uav_fly_route');
  assert.equal(model.progressPct, 43.5);
  assert.equal(model.coveragePct, 41.0);
  assert.equal(model.waypointIndex, 6);
  assert.equal(model.waypointOf, 14);
  assert.equal(model.fuelPct, 62.1);
  assert.equal(model.bingoFuelPct, 24.8);
  assert.equal(model.etaToBingoS, 930);
  assert.equal(model.altMslM, 1580);
  assert.equal(model.aglM, 60);
  assert.equal(model.fuelState, 'ok');
  assert.equal(model.severity, 'ok');
});

test('fuel within ten points of the BINGO line warns, at or below it latches', () => {
  const near = missionHudModel({
    mission: { ...MISSION, fuelPct: 30.0, bingoFuelPct: 24.8 },
    vehicle: VEHICLE,
  });
  assert.equal(near.fuelState, 'warning');
  assert.equal(near.severity, 'warning');

  const reached = missionHudModel({
    mission: { ...MISSION, fuelPct: 24.0, bingoFuelPct: 24.8 },
    vehicle: VEHICLE,
  });
  assert.equal(reached.fuelState, 'bingo');
  assert.equal(reached.severity, 'critical');

  const latched = missionHudModel({
    mission: {
      ...MISSION,
      safety: { ...MISSION.safety, bingoLatched: true },
    },
    vehicle: VEHICLE,
  });
  assert.equal(latched.fuelState, 'bingo');
  assert.ok(latched.notes.some((note) => /BINGO latched/.test(note.text)));
});

test('an unknown fuel or BINGO figure is not a silent zero', () => {
  const model = missionHudModel({
    mission: { ...MISSION, fuelPct: null, bingoFuelPct: null },
    vehicle: { ...VEHICLE, status: {} },
  });
  assert.equal(model.fuelPct, null);
  assert.equal(model.bingoFuelPct, null);
  assert.equal(model.fuelMarginPct, null);
  assert.equal(model.fuelState, 'ok');
});

test('a geofence breach and a degraded datum raise the HUD severity', () => {
  const breach = missionHudModel({
    mission: {
      ...MISSION,
      safety: { geofence: 'breach', proximityM: -20, bingoLatched: false },
    },
    vehicle: VEHICLE,
  });
  assert.equal(breach.severity, 'critical');

  const degraded = missionHudModel({
    mission: MISSION,
    vehicle: {
      ...VEHICLE,
      status: { ...VEHICLE.status, datumDegraded: true },
    },
  });
  assert.equal(degraded.severity, 'warning');
  assert.ok(degraded.notes.some((note) => /DATUM DEGRADED/.test(note.text)));
});

test('a bridge that serves no missions[] says so instead of faking idle', () => {
  const model = missionHudModel({
    mission: null,
    vehicle: VEHICLE,
    missionsServed: false,
  });
  assert.equal(model.hasMission, false);
  assert.ok(
    model.notes.some((note) => /mission feed not served/.test(note.text)),
  );
});

test('an incomplete mission keeps its reason in front of the operator', () => {
  const model = missionHudModel({
    mission: {
      ...MISSION,
      phase: 'rtb',
      incompleteReason: 'incomplete - fuel',
    },
    vehicle: VEHICLE,
  });
  assert.equal(model.incompleteReason, 'incomplete - fuel');
  assert.equal(model.notes[0].text, 'incomplete - fuel');
});

test('the HUD renders phase, progress, coverage and the BINGO marker', () => {
  globalThis.document = stubDocument();
  const hud = createUavMissionHud();
  const model = hud.update({ mission: MISSION, vehicle: VEHICLE });
  assert.equal(model.phase, 'executing');
  assert.equal(hud.element.attrs['data-severity'], 'ok');
  const text = JSON.stringify(hud.element, (key, value) =>
    key === 'listeners' ? undefined : value,
  );
  assert.match(text, /EXECUTING/);
  assert.match(text, /uav_fly_route/);
  assert.match(text, /43\.5%/); // progress
  assert.match(text, /41\.0%/); // coverage flown
  assert.match(text, /bingo 24\.8%/);
  assert.match(text, /15:30/); // ETA to BINGO, 930 s
  assert.match(text, /6 \/ 14/);
  hud.reset();
  hud.destroy();
});

test('the HUD paints critical styling once BINGO is reached', () => {
  globalThis.document = stubDocument();
  const hud = createUavMissionHud();
  hud.update({
    mission: { ...MISSION, fuelPct: 20, bingoFuelPct: 24.8 },
    vehicle: VEHICLE,
  });
  assert.equal(hud.element.attrs['data-severity'], 'critical');
  hud.destroy();
});
