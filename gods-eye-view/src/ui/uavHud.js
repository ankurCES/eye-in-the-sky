/**
 * UAV mission HUD (PLAN §3.1 acceptance 2 and 3).
 *
 * Renders the live mission state for the tracked drone: phase, active tool,
 * task progress %, flown coverage %, and fuel % against the BINGO line with
 * ETA-to-BINGO — fed from `/snapshot.missions[]` and the vehicle record, not
 * from a shape the bridge never emits.
 *
 * Read-only: the HUD reports, it never commands. The MCP server is the only
 * command path.
 */
import {
  formatDuration,
  formatPct,
  h,
  label,
  replaceKids,
  setClass,
  setHidden,
} from './uavDom.js';

/** Warn this many points of fuel above the BINGO line. */
const FUEL_WARN_MARGIN_PCT = 10;

const HUD_CSS = `
.uav-hud{margin-top:10px;padding-top:10px;border-top:1px solid #173a30;
  font:10px/1.5 "SF Mono",Menlo,monospace;color:#9fd9c8}
.uav-hud .uav-hud-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.uav-hud .uav-hud-phase{padding:1px 6px;border-radius:3px;font-weight:700;
  letter-spacing:.1em;background:#173a30;color:#1de9b6}
.uav-hud[data-severity="warning"] .uav-hud-phase{background:#3a3117;color:#ffd166}
.uav-hud[data-severity="critical"] .uav-hud-phase{background:#3a1717;color:#ff7a7a}
.uav-hud .uav-hud-kind{color:#5f8c80;letter-spacing:.08em}
.uav-hud .uav-hud-tool{margin-left:auto;color:#7fb5a6}
.uav-hud .uav-hud-note{margin-top:4px;color:#ffd166}
.uav-hud .uav-hud-note.critical{color:#ff7a7a}
.uav-hud .uav-meter{margin-top:6px}
.uav-hud .uav-meter-label{display:flex;justify-content:space-between;
  color:#7fb5a6;letter-spacing:.08em}
.uav-hud .uav-meter-track{position:relative;height:6px;margin-top:2px;
  border-radius:3px;background:#0d1512;border:1px solid #234b40;overflow:hidden}
.uav-hud .uav-meter-fill{position:absolute;left:0;top:0;bottom:0;
  background:#1de9b6}
.uav-hud .uav-meter-fill.warning{background:#ffd166}
.uav-hud .uav-meter-fill.critical{background:#ff7a7a}
.uav-hud .uav-meter-mark{position:absolute;top:-2px;bottom:-2px;width:2px;
  background:#ff7a7a}
.uav-hud .uav-hud-grid{display:grid;grid-template-columns:repeat(2,1fr);
  gap:2px 10px;margin-top:8px}
.uav-hud .uav-hud-cell{display:flex;justify-content:space-between;gap:8px}
.uav-hud .uav-hud-cell span:first-child{color:#5f8c80;letter-spacing:.06em}
.uav-hud .uav-hud-cell.alert span:last-child{color:#ff7a7a;font-weight:700}
.uav-hud .is-hidden{display:none}
`;

function pick(...values) {
  for (const value of values) if (Number.isFinite(value)) return value;
  return null;
}

/**
 * Derive the HUD display model. Pure: no DOM, so the fuel/BINGO and phase
 * logic is testable without a document.
 * @param {object} [state]
 * @param {object|null} [state.mission] normalized `/snapshot.missions[]` row
 * @param {object|null} [state.vehicle] normalized vehicle record
 * @param {boolean} [state.missionsServed] whether the bridge served missions[]
 * @param {string} [state.simState] `/snapshot.sim_state`
 * @returns {object} display model
 */
export function missionHudModel({
  mission = null,
  vehicle = null,
  missionsServed = true,
  simState = '',
} = {}) {
  const status = vehicle?.status || {};
  const position = vehicle?.position || {};
  const safety = mission?.safety || {};
  const fuelPct = pick(mission?.fuelPct, status.fuelPct);
  const bingoFuelPct = pick(mission?.bingoFuelPct, status.bingoFuelPct);
  const margin =
    fuelPct != null && bingoFuelPct != null ? fuelPct - bingoFuelPct : null;
  const bingoLatched = safety.bingoLatched === true;
  const fuelState =
    bingoLatched || (margin != null && margin <= 0)
      ? 'bingo'
      : margin != null && margin <= FUEL_WARN_MARGIN_PCT
        ? 'warning'
        : 'ok';
  const geofence = mission?.safety?.geofence || 'unknown';
  const datumDegraded = status.datumDegraded === true;
  const notes = [];
  if (!missionsServed)
    notes.push({
      text: 'mission feed not served by this bridge (/snapshot.missions[])',
      severity: 'warning',
    });
  if (mission?.incompleteReason)
    notes.push({ text: mission.incompleteReason, severity: 'critical' });
  if (bingoLatched)
    notes.push({ text: 'BINGO latched — RTB forced', severity: 'critical' });
  if (datumDegraded)
    notes.push({
      text: 'DATUM DEGRADED — geoid source degraded',
      severity: 'warning',
    });
  const severity =
    fuelState === 'bingo' || geofence === 'breach'
      ? 'critical'
      : fuelState === 'warning' ||
          geofence === 'proximity' ||
          geofence === 'margin' ||
          datumDegraded
        ? 'warning'
        : 'ok';
  return {
    hasMission: Boolean(mission),
    missionsServed,
    simState: simState || 'unknown',
    missionId: mission?.id || '',
    kind: mission?.kind || '',
    phase: mission?.phase || (vehicle ? 'idle' : 'no mission'),
    activeTool: mission?.activeTool || '',
    progressPct: mission?.progressPct ?? null,
    coveragePct: mission?.coveragePct ?? null,
    waypointIndex: mission?.waypoint?.index ?? null,
    waypointOf: mission?.waypoint?.of ?? null,
    etaS: mission?.etaS ?? null,
    fuelPct,
    bingoFuelPct,
    fuelMarginPct: margin,
    fuelState,
    etaToBingoS: status.etaToBingoS ?? null,
    geofence,
    proximityM: mission?.safety?.proximityM ?? null,
    bingoLatched,
    incompleteReason: mission?.incompleteReason || null,
    altHaeM: position.ellipsoidAltitude ?? null,
    altMslM: position.mslAltitude ?? position.barometricAltitude ?? null,
    aglM: position.agl ?? null,
    datumDegraded,
    notes,
    severity,
  };
}

function meter(title) {
  const value = h('span', {}, '—');
  const fill = h('i', { class: 'uav-meter-fill', style: 'width:0%' });
  const mark = h('i', { class: 'uav-meter-mark', style: 'left:0%' });
  const track = h('div', { class: 'uav-meter-track' }, fill);
  const element = h(
    'div',
    { class: 'uav-meter' },
    h('div', { class: 'uav-meter-label' }, h('span', {}, title), value),
    track,
  );
  return { element, value, fill, mark, track };
}

function cell(title) {
  const value = h('span', {}, '—');
  return {
    element: h('div', { class: 'uav-hud-cell' }, h('span', {}, title), value),
    value,
  };
}

/**
 * Create the mission HUD surface.
 * @param {object} [options]
 * @param {boolean} [options.withStyle] emit the scoped stylesheet element
 * @returns {object} `{element, style, update, destroy}`
 */
export function createUavMissionHud({ withStyle = true } = {}) {
  const style = withStyle ? h('style') : null;
  if (style) style.textContent = HUD_CSS;

  const phase = h('span', { class: 'uav-hud-phase' }, 'NO MISSION');
  const kind = h('span', { class: 'uav-hud-kind' }, '');
  const tool = h('span', { class: 'uav-hud-tool' }, '');
  const head = h('div', { class: 'uav-hud-head' }, phase, kind, tool);
  const notes = h('div', { class: 'uav-hud-note' });

  const progress = meter('PROGRESS');
  const coverage = meter('COVERAGE (FLOWN)');
  const fuel = meter('FUEL vs BINGO');
  fuel.track.append(fuel.mark);

  const cells = {
    bingo: cell('ETA TO BINGO'),
    eta: cell('MISSION ETA'),
    waypoint: cell('WAYPOINT'),
    geofence: cell('GEOFENCE'),
    alt: cell('ALT MSL'),
    agl: cell('AGL'),
  };
  const grid = h(
    'div',
    { class: 'uav-hud-grid' },
    ...Object.values(cells).map((entry) => entry.element),
  );

  const element = h(
    'div',
    { class: 'uav-hud', 'data-severity': 'ok' },
    head,
    notes,
    progress.element,
    coverage.element,
    fuel.element,
    grid,
  );

  function bar(entry, value, state = 'ok') {
    const width = Number.isFinite(value)
      ? Math.min(100, Math.max(0, value))
      : 0;
    entry.fill.setAttribute('style', `width:${width.toFixed(1)}%`);
    setClass(entry.fill, 'warning', state === 'warning');
    setClass(entry.fill, 'critical', state === 'bingo' || state === 'critical');
    entry.value.textContent = formatPct(value, 1);
  }

  return {
    element,
    style,
    /**
     * Repaint from a snapshot slice.
     * @param {object} state see `missionHudModel`
     * @returns {object} the model that was rendered
     */
    update(state = {}) {
      const model = missionHudModel(state);
      element.setAttribute('data-severity', model.severity);
      phase.textContent = label(model.phase, 'NO MISSION');
      kind.textContent = [label(model.kind, ''), model.missionId]
        .filter(Boolean)
        .join(' · ');
      tool.textContent = model.activeTool || '';

      const worst = model.notes.find((note) => note.severity === 'critical');
      const shown = worst || model.notes[0] || null;
      notes.textContent = shown ? shown.text : '';
      setClass(notes, 'critical', shown?.severity === 'critical');
      setHidden(notes, !shown);

      bar(progress, model.progressPct);
      bar(coverage, model.coveragePct);
      bar(fuel, model.fuelPct, model.fuelState);
      const markAt = Number.isFinite(model.bingoFuelPct)
        ? Math.min(100, Math.max(0, model.bingoFuelPct))
        : null;
      fuel.mark.setAttribute(
        'style',
        markAt == null ? 'display:none' : `left:${markAt.toFixed(1)}%`,
      );
      fuel.value.textContent =
        markAt == null
          ? formatPct(model.fuelPct, 1)
          : `${formatPct(model.fuelPct, 1)} / bingo ${formatPct(markAt, 1)}`;

      cells.bingo.value.textContent = formatDuration(model.etaToBingoS);
      setClass(cells.bingo.element, 'alert', model.fuelState !== 'ok');
      cells.eta.value.textContent = formatDuration(model.etaS);
      cells.waypoint.value.textContent =
        model.waypointIndex == null || model.waypointOf == null
          ? '—'
          : `${model.waypointIndex} / ${model.waypointOf}`;
      cells.geofence.value.textContent = Number.isFinite(model.proximityM)
        ? `${label(model.geofence)} ${Math.round(model.proximityM)}m`
        : label(model.geofence);
      setClass(
        cells.geofence.element,
        'alert',
        model.geofence === 'breach' || model.geofence === 'proximity',
      );
      cells.alt.value.textContent = Number.isFinite(model.altMslM)
        ? `${Math.round(model.altMslM)}m`
        : '—';
      setClass(cells.alt.element, 'alert', model.datumDegraded);
      cells.agl.value.textContent = Number.isFinite(model.aglM)
        ? `${Math.round(model.aglM)}m`
        : '—';
      return model;
    },
    /** Clear every readout back to unknown. */
    reset() {
      return this.update({ mission: null, vehicle: null });
    },
    destroy() {
      replaceKids(element, []);
      element.remove?.();
      style?.remove?.();
    },
  };
}
