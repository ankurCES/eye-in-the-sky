/**
 * UAV Mission Control panel: select a theater + mission kind, submit it to the
 * godSeye MCP control plane (via the telemetry bridge /control proxy), and
 * render the live command-center readouts — mission HUD, contact roster and
 * safety alarms. Rendered as a left-rail collapsible panel (like Scenes /
 * Data Layers) inside #left-panel-stack.
 *
 * God's Eye view stays read-only — this panel never commands the sim directly;
 * the bridge /control proxy is the only command path.
 *
 * Feeds (BRIDGE_CONTRACT.md): `/snapshot` through `createUavSource()` so the
 * panel reads the same normalized shape the globe layer does, `/theaters` for
 * the theater table (mcp/godseye_uav/theaters.py is the single source of
 * truth) and SSE `/events` for alarms. Every one of those degrades to a
 * labelled fallback rather than throwing when the bridge has not shipped it.
 *
 * The panel adopts the theater the bridge is RUNNING (`/theaters.active`, else
 * `/health`) instead of the table's default, and renders any disagreement
 * between that and the selector. Finding UI-1: a stack launched with
 * `--theater iran-isfahan` presented Redmond POIs while the aircraft flew over
 * Isfahan, leaving the operator one click from a target 10,000 km away.
 */
import {
  createUavEventStream,
  createUavSource,
  fetchUavActiveTheater,
  fetchUavTheaters,
  normalizeContact,
} from '../sources/live/uav.js';
import { h, hasClass, setClass, setHidden } from './uavDom.js';
import {
  activeTheaterNote,
  createTheaterRegistry,
  theaterList,
} from './uavTheaters.js';
import { createUavMissionHud } from './uavHud.js';
import { createUavAlarmSurface } from './uavAlarms.js';
import { createUavContactRoster } from './uavContactRoster.js';
import { createUavMissionViews } from './uavMissionViews.js';

export { THEATERS, SEED_POIS, OFFLINE_THEATERS } from './uavTheaters.js';

export const MISSIONS = Object.freeze({
  orbit_poi: { label: 'Orbit POI', desc: 'Fixed-wing orbit over point' },
  recon_route: { label: 'Recon', desc: 'Lawnmower over AO' },
  grid_search: { label: 'Grid', desc: 'SAR grid over polygon' },
  track: { label: 'Track', desc: 'Pursue contact' },
  assess: { label: 'Assess', desc: 'Cross-cue + SALUTE report' },
});

const DEFAULT_VEHICLE = 'Drone1';
const CONTROL_STATUS_EVERY = 5; // ticks between /control/status enrichment
const ADOPT_EVERY = 15; // ticks between running-theater adoption retries
const ADOPT_MAX_TRIES = 5; // then stop asking; the running line says what we know

const CSS = `
/* A HORIZONTAL DRAWER, not a tall column.
   Stacked vertically in the rail this panel grew to ~1900px -- taller than any
   normal viewport, so everything from the contact roster down was simply
   unreachable. It now opens SIDEWAYS: collapsed it is just the header at rail
   width; open it slides out to a fixed drawer width and lays its content in
   columns, capped to the viewport with its own scroll. */
#uav-mission-panel{position:relative;width:100%;z-index:auto;
  background:rgba(10,14,18,.92);border:1px solid #1de9b6;border-radius:8px;
  color:#d7f5ec;font:12px/1.5 "SF Mono",Menlo,monospace;
  box-shadow:0 6px 24px rgba(0,0,0,.5);backdrop-filter:blur(6px);
  transition:width .18s ease}
#uav-mission-panel:not(.collapsed){width:680px;
  max-width:calc(100vw - 96px);
  /* The rail measures how much vertical room this panel may take and publishes
     it as --left-panel-allocated-height (the sibling panels consume it through
     flex-basis). Honour the same budget instead of guessing a vh figure, or the
     drawer runs off the bottom whenever another panel is open above it. */
  display:flex;flex-direction:column;
  max-height:var(--left-panel-allocated-height,72vh)}
#uav-mission-panel:not(.collapsed) .uav-inner{display:flex;flex-direction:column;
  min-height:0;flex:1 1 auto}
/* The body is the drawer: two columns, and it owns the scrolling so the panel
   itself never runs past the bottom of the screen. */
#uav-mission-panel .uav-body{display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:0 16px;align-content:start;
  /* min-height:0 is what actually lets a flex child scroll instead of growing
     past its parent. */
  flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;
  padding-right:4px}
/* Full-width rows inside the grid: anything that reads as a band rather than a
   field. Declared by class so a new section defaults to a column, not a band. */
#uav-mission-panel .uav-body > .uav-span{grid-column:1 / -1}
#uav-mission-panel .uav-col{min-width:0}
/* The readout column starts level with the first control, not with the
   'MCP-gated' subtitle above it. */
#uav-mission-panel .uav-col-readouts{padding-top:18px}
#uav-mission-panel .uav-col-readouts > :first-child{margin-top:0}
/* One column when the drawer cannot be wide (narrow window / phone). */
@media (max-width:760px){
  #uav-mission-panel:not(.collapsed){width:calc(100vw - 32px)}
  #uav-mission-panel .uav-body{grid-template-columns:1fr}
}
#uav-mission-panel .uav-inner{padding:10px 12px 12px}
#uav-mission-panel .panel-header{display:flex;align-items:center;gap:8px;
  cursor:pointer;user-select:none}
#uav-mission-panel .panel-title{font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:#1de9b6;font-weight:700}
#uav-mission-panel .panel-divider{flex:1;height:1px;background:#173a30}
#uav-mission-panel .panel-collapse-btn{background:transparent;border:0;
  color:#1de9b6;font-size:14px;cursor:pointer;line-height:1;padding:0 2px}
#uav-mission-panel .sub{font-size:10px;color:#5f8c80;margin:2px 0 8px}
#uav-mission-panel .origin{font-size:9px;color:#5f8c80;margin:2px 0 0;
  letter-spacing:.04em}
#uav-mission-panel .origin.offline{color:#ffd166}
#uav-mission-panel .running{font-size:9px;color:#5f8c80;margin:2px 0 0;
  letter-spacing:.04em}
#uav-mission-panel .running.mismatch{color:#04110c;background:#ff7a7a;
  font-weight:700;padding:2px 4px;border-radius:3px}
#uav-mission-panel label{display:block;font-size:10px;color:#7fb5a6;
  margin:8px 0 3px;text-transform:uppercase;letter-spacing:.08em}
#uav-mission-panel select,#uav-mission-panel input{width:100%;box-sizing:border-box;
  background:#0d1512;border:1px solid #234b40;color:#d7f5ec;border-radius:4px;
  padding:6px;font:11px inherit}
#uav-mission-panel .row{display:flex;gap:6px;margin-top:10px}
/* Scoped to the action row on purpose. As a bare '#uav-mission-panel button'
   this rule carried an id, so no class selector a child surface can write will
   ever outrank it: it repainted every contact-roster card in the Launch
   button's fill and uppercased the SALUTE lines. */
#uav-mission-panel .row button{flex:1;background:#1de9b6;color:#04110c;border:0;
  border-radius:4px;padding:8px;font:700 11px inherit;cursor:pointer;
  text-transform:uppercase;letter-spacing:.06em}
#uav-mission-panel .row button.ghost{background:transparent;
  border:1px solid #234b40;color:#7fb5a6}
#uav-mission-panel .row button:disabled{opacity:.4;cursor:default}
#uav-mission-panel .row button[aria-pressed="true"]{background:#1de9b6;
  color:#04110c;border-color:#1de9b6}
#uav-mission-panel .sec{margin-top:12px;padding-top:10px;border-top:1px solid #173a30}
#uav-mission-panel .sec-title{font-size:10px;letter-spacing:.12em;color:#1de9b6;
  text-transform:uppercase;margin-bottom:6px}
#uav-mission-panel .opt{display:flex;align-items:center;gap:8px;font-size:11px;
  color:#9fd9c8;margin:4px 0;text-transform:none;letter-spacing:0}
#uav-mission-panel .opt input{width:auto;margin:0}
#uav-mission-panel .status{margin-top:10px;font-size:10px;min-height:14px;
  color:#8fe9cf;white-space:pre-wrap}
#uav-mission-panel .tele{margin-top:8px;padding-top:8px;border-top:1px solid #173a30;
  font-size:10px;color:#9fd9c8;white-space:pre-wrap}
#uav-mission-panel .err{color:#ff7a7a}
#uav-mission-panel .coord{font-size:10px;color:#8fe9cf;margin-top:4px;min-height:12px}
/* The rail (#left-panel-stack) is pointer-events:none so the globe stays
   draggable behind it, and style.css re-enables clicks only for an explicit
   ALLOWLIST of panel ids (#data-panel, #cctv-panel, #global-context-panel,
   #scene-panel). This panel is not on that list, so without this rule every
   click — the theater select, LAUNCH, even the expand caret — fell straight
   through to the Cesium canvas and the panel was completely inert.
   Set here rather than in style.css so the panel stays self-contained: an
   inherited pointer-events value always loses to one declared on the element. */
#uav-mission-panel{pointer-events:auto}
#uav-mission-panel.collapsed .uav-body{display:none}
#uav-mission-panel .is-hidden{display:none}
`;

/** Unwrap an MCP tool result: FastMCP wraps flat dicts in content/structured. */
export function unwrapMcp(payload) {
  const result = payload?.result ?? payload;
  if (!result || typeof result !== 'object') return result ?? null;
  if (result.structuredContent && typeof result.structuredContent === 'object')
    return result.structuredContent;
  const text = Array.isArray(result.content) ? result.content[0]?.text : null;
  if (typeof text === 'string') {
    try {
      return JSON.parse(text);
    } catch {
      return result;
    }
  }
  return result;
}

/**
 * Read a SALUTE field the bridge may serve either as a plain string or as the
 * structured object `targets.salute_report()` actually returns. Every one of
 * those objects carries the operator-facing wording under `text`; `keys` names
 * the spellings to try, in order. Reading the object itself stringified every
 * SALUTE line to `[object Object]`.
 * @param {*} value raw field
 * @param {...string} keys keys to try inside an object value, best first
 * @returns {string} display text, or '' when the field says nothing
 */
export function saluteText(value, ...keys) {
  if (typeof value === 'string') return value.trim();
  if (Number.isFinite(value)) return String(value);
  if (!value || typeof value !== 'object') return '';
  for (const key of keys.length ? keys : ['text']) {
    const candidate = value[key];
    if (typeof candidate === 'string' && candidate.trim())
      return candidate.trim();
    if (Number.isFinite(candidate)) return String(candidate);
  }
  return '';
}

/**
 * Adapt a bridge `/tracks` row to the `contacts[]` shape so the roster works
 * before the bridge ships `/snapshot.contacts[]`.
 *
 * `/tracks` proxies `uav_list_tracks`, i.e. `targets.salute_report()`: its
 * size/activity/unit/time/equipment/confidence are OBJECTS, `category` and
 * `confidence_level` are the flat mirrors, and the timestamp is `time.last_seen`
 * in SECONDS. Reading them as if they were flat strings put `[object Object]`
 * in every SALUTE line and left the roster's confidence chip unreadable.
 *
 * @param {object} row raw `/tracks` entry
 * @returns {object} a `contacts[]`-shaped row
 */
export function trackRowAsContact(row) {
  const time = row?.time;
  const lastSeenS =
    (typeof time === 'object' ? (time?.last_seen ?? time?.epoch) : null) ??
    row?.last_seen ??
    null;
  return {
    track_id: row?.track_id ?? row?.id,
    category:
      saluteText(row?.category) ||
      saluteText(row?.unit, 'category') ||
      saluteText(row?.classification) ||
      saluteText(row?.mesh) ||
      '',
    confidence:
      saluteText(row?.confidence_level) ||
      saluteText(row?.confidence, 'level') ||
      '',
    location: row?.location ?? {
      lat: row?.lat,
      lon: row?.lon,
      alt_m: row?.alt_m,
    },
    last_seen_ms:
      row?.last_seen_ms ??
      row?.timestamp_ms ??
      (Number.isFinite(lastSeenS) ? lastSeenS * 1000 : null),
    threat_level:
      saluteText(row?.threat_level) || saluteText(row?.threat, 'level'),
    salute: row?.salute ?? {
      size: saluteText(row?.size, 'text', 'element'),
      activity: saluteText(row?.activity, 'text', 'code'),
      location: saluteText(row?.location_text, 'text'),
      unit: saluteText(row?.unit, 'text', 'assessment'),
      time: saluteText(row?.time, 'iso'),
      equipment: saluteText(row?.equipment, 'text', 'platform'),
    },
  };
}

/**
 * Create the mission-control panel.
 * @param {object} opts
 * @param {() => string} opts.bridgeUrl  base URL of the telemetry bridge
 * @param {() => string} opts.token      Bearer token for the bridge
 * @param {(home:[lat,lon,alt]) => void} [opts.onTheater] fly the camera to a theater
 * @param {(reference:string) => boolean} [opts.onEnterCockpit] track + enter cockpit
 * @param {(contact:object) => boolean} [opts.onFocusContact] focus a contact
 * @param {object} [opts.viewer] Cesium viewer (for globe interactions)
 * @param {object} [opts.Cesium] Cesium namespace (for markers/overlays)
 * @param {object} [opts.source] live UAV source (defaults to createUavSource())
 * @param {(url:string) => object} [opts.eventSourceFactory] SSE constructor
 * @param {(opts:object) => Promise<object|null>} [opts.theaterLoader] served
 *   theater-table fetcher (defaults to the bridge's `/theaters`)
 * @param {(opts:object) => Promise<object|null>} [opts.activeTheaterLoader]
 *   running-theater probe (defaults to the bridge's `/health`)
 * @param {number} [opts.tickMs] readout poll interval
 */
export function createUavMissionPanel({
  bridgeUrl,
  token,
  onTheater,
  onEnterCockpit,
  onFocusContact,
  viewer = null,
  Cesium = null,
  source = null,
  eventSourceFactory = null,
  theaterLoader = null,
  activeTheaterLoader = null,
  tickMs = 1000,
} = {}) {
  const style = h('style');
  style.textContent = CSS;

  // One bridge, one origin. The panel's `/control/*` calls and the telemetry
  // it renders must resolve the same way, or the readouts describe a different
  // aircraft from the one the Launch button commands.
  const live = source || createUavSource({ baseUrl: bridgeUrl, token: token });
  const theaters = createTheaterRegistry({
    load: theaterLoader
      ? theaterLoader
      : (options = {}) =>
          fetchUavTheaters({ ...options, baseUrl: bridgeUrl, token: token }),
    // Which theater the bridge is RUNNING. Same origin as the table and the
    // /control calls, so all three describe one aircraft.
    loadActive: activeTheaterLoader
      ? activeTheaterLoader
      : (options = {}) =>
          fetchUavActiveTheater({
            ...options,
            baseUrl: bridgeUrl,
            token: token,
          }),
  });

  // ---- Mission controls -------------------------------------------------
  const theaterSel = h('select', { id: 'uav-theater' });
  const originEl = h('div', { class: 'origin' }, theaters.originLabel());
  const runningEl = h('div', { class: 'running' });
  const vehicleSel = h(
    'select',
    { id: 'uav-vehicle' },
    h('option', { value: DEFAULT_VEHICLE }, DEFAULT_VEHICLE),
  );
  const missionSel = h(
    'select',
    { id: 'uav-mission' },
    ...Object.entries(MISSIONS).map(([k, m]) =>
      h('option', { value: k }, `${m.label} — ${m.desc}`),
    ),
  );
  const poiInput = h('input', {
    id: 'uav-poi',
    placeholder: 'POI lat,lon (orbit/track/assess)',
  });
  const poiSel = h(
    'select',
    { id: 'uav-poi-seed' },
    h('option', { value: '' }, 'seed POI…'),
  );
  poiSel.addEventListener('change', () => {
    if (poiSel.value) poiInput.value = poiSel.value;
  });
  const launchBtn = h('button', {}, 'Launch');
  const abortBtn = h('button', { class: 'ghost' }, 'Abort');
  const cockpitBtn = h('button', { class: 'ghost' }, 'Cockpit');
  const statusEl = h('div', { class: 'status' });
  const teleEl = h('div', { class: 'tele' }, 'telemetry: —');

  // ---- Live command-center surfaces -------------------------------------
  const hud = createUavMissionHud();
  const alarms = createUavAlarmSurface();
  const roster = createUavContactRoster({
    onSelect: (contact) => focusContact(contact),
  });

  // Mission views: every mission actually in the air, and a way to put the
  // camera on any of them. Selecting one tracks that mission's LEAD drone and
  // enters the cockpit FPV -- looking, never commanding (the MCP server stays
  // the only command path).
  const missionViews = createUavMissionViews({
    onSelect: (row) => {
      if (!row?.lead) return;
      // Point the panel's own vehicle selector at the lead so every later
      // readout and command in this panel refers to the drone being watched.
      if ([...vehicleSel.options].some((o) => o.value === row.lead))
        vehicleSel.value = row.lead;
      const ok = onEnterCockpit?.(row.lead);
      setStatus(
        ok === false
          ? `mission view: ${row.lead} is not trackable yet`
          : `mission view: ${row.kind} — FPV on ${row.lead}`,
      );
    },
  });

  // ---- Tactical options -------------------------------------------------
  // Target coordinate mapping
  const mapTargetChk = h('input', {
    type: 'checkbox',
    id: 'uav-opt-maptarget',
  });
  const coordEl = h('div', { class: 'coord' }, 'click map to set target');
  // Path-ahead terrain simulation
  const pathAheadChk = h('input', {
    type: 'checkbox',
    id: 'uav-opt-pathahead',
  });
  // Real-time on-ground tracking + data capture
  const groundTrackChk = h('input', {
    type: 'checkbox',
    id: 'uav-opt-groundtrack',
  });

  const tacticalSec = h(
    'div',
    { class: 'sec' },
    h('div', { class: 'sec-title' }, 'Tactical Overlays'),
    h('label', { class: 'opt' }, mapTargetChk, 'Target coordinate mapping'),
    coordEl,
    h('label', { class: 'opt' }, pathAheadChk, 'Path-ahead terrain sim'),
    h('label', { class: 'opt' }, groundTrackChk, 'Ground track + data capture'),
  );

  // ---- Collapsible panel chrome (matches Scenes / Data Layers) ----------
  const collapseBtn = h(
    'button',
    {
      class: 'panel-collapse-btn',
      'data-collapse-target': 'uav-mission-panel',
      title: 'Collapse panel',
    },
    '+',
  );
  const header = h(
    'div',
    { class: 'panel-header' },
    h('span', { class: 'panel-title' }, 'UAV MISSION CONTROL'),
    h('span', { class: 'panel-divider' }),
    collapseBtn,
  );
  // Two columns inside the drawer: what the operator SETS on the left, what the
  // aircraft REPORTS on the right. Grouped into column elements rather than
  // dropped straight into the grid, so a label never lands in one column with
  // its field in the other.
  const bodyControls = h(
    'div',
    { class: 'uav-col uav-col-controls' },
    h('div', { class: 'sub' }, 'MCP-gated · ISR only'),
    h('label', {}, 'Theater / Map'),
    theaterSel,
    originEl,
    runningEl,
    h('label', {}, 'Vehicle'),
    vehicleSel,
    h('label', {}, 'Mission'),
    missionSel,
    h('label', {}, 'Target POI'),
    poiInput,
    poiSel,
    h('div', { class: 'row' }, launchBtn, abortBtn),
    h('div', { class: 'row' }, cockpitBtn),
    tacticalSec,
    statusEl,
  );
  const bodyReadouts = h(
    'div',
    { class: 'uav-col uav-col-readouts' },
    alarms.banner,
    hud.element,
    missionViews.element,
    roster.element,
    teleEl,
  );
  const body = h('div', { class: 'uav-body' }, bodyControls, bodyReadouts);
  const panel = h(
    'div',
    {
      id: 'uav-mission-panel',
      class: 'panel-collapsible collapsed',
      'data-panel-id': 'uav-mission-panel',
    },
    h('div', { class: 'panel-glow' }),
    h('div', { class: 'uav-inner' }, header, body),
  );

  // Self-contained expand/collapse. The panel mounts after PanelChrome's init
  // scan, so it binds its own disclosure rather than relying on the shell.
  function setCollapsed(collapsed) {
    setClass(panel, 'collapsed', collapsed);
    collapseBtn.textContent = collapsed ? '+' : '−';
    collapseBtn.setAttribute('aria-expanded', String(!collapsed));
    collapseBtn.title = collapsed ? 'Expand panel' : 'Collapse panel';
  }
  // True once the operator has collapsed the panel THEMSELVES. Enabling the
  // UAV layer auto-expands this panel (see syncLayerEnabled), but that must
  // never fight someone who deliberately closed it.
  let operatorCollapsed = false;
  function toggleCollapsed() {
    const next = !hasClass(panel, 'collapsed');
    operatorCollapsed = next;
    setCollapsed(next);
  }
  collapseBtn.addEventListener('click', toggleCollapsed);
  header.addEventListener('click', (e) => {
    if (e.target === collapseBtn) return;
    toggleCollapsed();
  });
  setCollapsed(true);

  function setStatus(msg, isErr = false) {
    statusEl.textContent = msg;
    statusEl.className = isErr ? 'status err' : 'status';
  }

  function theater() {
    return theaters.get(theaterSel.value) || theaters.list()[0] || null;
  }

  function vehicle() {
    return vehicleSel.value || DEFAULT_VEHICLE;
  }

  // ---- Theater table: served rows, bundled fallback ----------------------

  // Set once the operator picks a theater by hand. From then on the panel
  // stops adopting: it still SAYS the bridge is elsewhere, it just does not
  // move the selection out from under them.
  let operatorChose = false;

  /**
   * Render the running-theater line. Called on every table refresh and on
   * every selection change, so a mismatch cannot survive unseen: the operator
   * either reads "running: <theater>" or reads the mismatch in red.
   */
  function renderRunningTheater() {
    // Before the first refresh the answer is not "nothing is published", it is
    // "we have not asked". Saying the former for that window would be the same
    // class of confident wrong answer this whole surface exists to stop.
    const note = theaters.asked()
      ? activeTheaterNote(theaters.active(), theaterSel.value)
      : {
          state: 'asking',
          text: 'running theater: asking the bridge…',
          warn: false,
        };
    runningEl.textContent = note.text;
    setClass(runningEl, 'mismatch', note.warn);
    return note;
  }

  function refreshTheaterOptions() {
    const rows = theaterList(theaters.table());
    const previous = theaterSel.value;
    theaterSel.replaceChildren(
      ...rows.map((t) => h('option', { value: t.id }, t.label)),
    );
    // The theater the bridge is FLYING wins over the table's own default.
    // Defaulting is what put Redmond POIs in front of an operator whose
    // aircraft was over Isfahan, one click from a target 10,000 km out.
    const running = theaters.active();
    const adopt = !operatorChose && running?.inTable ? running.id : null;
    const keep =
      adopt ??
      (rows.some((t) => t.id === previous)
        ? previous
        : theaters.table().defaultId);
    theaterSel.value = keep;
    originEl.textContent = theaters.originLabel();
    setClass(originEl, 'offline', theaters.isOffline());
    renderRunningTheater();
    refreshPoiSeeds();
    // The theater this pass moved AWAY from, or '' when it moved nothing. Only
    // this function moves the selection without the operator acting (adoption,
    // or a served table that no longer carries their row), so it is the one
    // place that can tell the caller a target is now stale.
    return previous && keep !== previous ? previous : '';
  }

  function refreshPoiSeeds() {
    const seeds = theater()?.pois || [];
    poiSel.replaceChildren(
      h('option', { value: '' }, 'seed POI…'),
      ...seeds.map((p) =>
        h(
          'option',
          { value: `${p.lat},${p.lon}` },
          `${p.name} (${p.lat},${p.lon})`,
        ),
      ),
    );
  }
  // An explicit operator selection: stop adopting, and re-render the running
  // line so a deliberate divergence from the bridge still reads as a mismatch.
  theaterSel.addEventListener('change', () => {
    operatorChose = true;
    renderRunningTheater();
    refreshPoiSeeds();
  });
  refreshTheaterOptions();

  function parsePoi() {
    const t = theater();
    const txt = (poiInput.value || '').trim();
    if (!txt) {
      const [lat, lon] = t?.home || [0, 0];
      return { lat, lon };
    }
    const [lat, lon] = txt.split(',').map(Number);
    if (!Number.isFinite(lat) || !Number.isFinite(lon))
      throw new Error('POI must be "lat,lon"');
    return { lat, lon };
  }

  async function post(path, body) {
    const res = await fetch(`${bridgeUrl()}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token()}`,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function get(path) {
    const res = await fetch(`${bridgeUrl()}${path}`, {
      headers: { Authorization: `Bearer ${token()}` },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function missionParams() {
    const kind = missionSel.value;
    const t = theater();
    if (kind === 'recon_route') {
      const c = t?.ao || [];
      return { waypoints: c.map(([lat, lon]) => ({ lat, lon, alt_m: 120 })) };
    }
    if (kind === 'grid_search') {
      // Prefer the served demo box: it is centred on the AO and is the polygon
      // the safety envelope's geofence check is known to accept.
      const polygon = t?.demo?.polygon?.length ? t.demo.polygon : t?.ao || [];
      return { polygon, alt_m: t?.demo?.altMAgl ?? 120 };
    }
    const { lat, lon } = parsePoi();
    return { lat, lon, radius_m: t?.orbitRadiusM ?? 150, alt_m: 120 };
  }

  async function launch() {
    launchBtn.disabled = true;
    try {
      const kind = missionSel.value;
      setStatus(`submitting ${MISSIONS[kind].label}…`);
      const out = await post('/control/mission', {
        vehicle: vehicle(),
        kind,
        params: missionParams(),
      });
      const r = unwrapMcp(out);
      if (r?.rejected) {
        setStatus(`rejected: ${r.error || JSON.stringify(r.gate || r)}`, true);
      } else {
        setStatus(
          `${MISSIONS[kind].label} queued · task ${r?.task_id?.slice(0, 8) || '?'}`,
        );
      }
    } catch (e) {
      setStatus(`launch failed: ${e.message}`, true);
    } finally {
      launchBtn.disabled = false;
    }
  }

  async function abort() {
    try {
      await post('/control/command', { tool: 'uav_abort', vehicle: vehicle() });
      setStatus('aborted — hover');
    } catch (e) {
      setStatus(`abort failed: ${e.message}`, true);
    }
  }

  // ---- Tactical overlay state ------------------------------------------
  let mapTargetHandler = null;
  let targetMarker = null;
  let pathAheadEntity = null;
  let groundTrackEntity = null;
  let overlayDs = null;

  function ensureOverlayDs() {
    if (!viewer || !Cesium) return null;
    if (!overlayDs) {
      overlayDs = new Cesium.CustomDataSource('uav-tactical');
      viewer.dataSources.add(overlayDs);
    }
    return overlayDs;
  }

  function setTargetMarker(lat, lon) {
    const ds = ensureOverlayDs();
    if (!ds) return;
    if (!targetMarker) {
      targetMarker = ds.entities.add({
        id: 'uav-target-marker',
        point: {
          pixelSize: 12,
          color: Cesium.Color.YELLOW,
          outlineColor: Cesium.Color.RED,
          outlineWidth: 2,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: 'TARGET',
          font: '10px monospace',
          fillColor: Cesium.Color.YELLOW,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          pixelOffset: new Cesium.Cartesian2(0, -18),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    }
    targetMarker.position = new Cesium.ConstantPositionProperty(
      Cesium.Cartesian3.fromDegrees(lon, lat, 0),
    );
    viewer.scene.requestRender?.();
  }

  /**
   * Forget the target coordinate, the seed that filled it and its globe pin.
   *
   * The POI box holds a coordinate entered under whichever theater was
   * selected at the time. Adoption moves that selection without the operator
   * acting, and a target carried across is finding UI-1 by another route: in a
   * real browser the panel read "iran-isfahan" and the running line agreed,
   * while the Launch button submitted the Redmond coordinate still sitting in
   * the box — 10,000 km from the aircraft — and the status said "queued".
   * Cleared, the empty box means "the theater's home", which is inside its AO.
   *
   * @returns {string} the coordinate that was dropped, or '' if there was none
   */
  function clearTarget() {
    const had = (poiInput.value || '').trim();
    poiInput.value = '';
    if (poiSel.value) poiSel.value = '';
    if (targetMarker) {
      overlayDs?.entities.remove(targetMarker);
      targetMarker = null;
      viewer?.scene?.requestRender?.();
    }
    coordEl.textContent = 'click map to set target';
    return had;
  }

  /** Focus a roster contact: move the operator's view, never the drone. */
  function focusContact(contact) {
    if (!contact) return false;
    if (onFocusContact?.(contact) === true) return true;
    const pos = contact.position;
    if (!pos) {
      setStatus(`contact ${contact.trackId}: no position reported`, true);
      return false;
    }
    setStatus(`contact ${contact.trackId} · ${contact.threatLevel}`);
    if (!viewer || !Cesium) return false;
    setTargetMarker(pos.latitude, pos.longitude);
    viewer.camera?.flyTo?.({
      destination: Cesium.Cartesian3.fromDegrees(
        pos.longitude,
        pos.latitude,
        (pos.altitude || 0) + 4000,
      ),
      duration: 1.5,
    });
    return true;
  }

  function enableMapTarget(on) {
    if (!viewer || !Cesium) return;
    if (on && !mapTargetHandler) {
      mapTargetHandler = new Cesium.ScreenSpaceEventHandler(
        viewer.scene.canvas,
      );
      mapTargetHandler.setInputAction((movement) => {
        const scene = viewer.scene;
        const ray = viewer.camera.getPickRay(movement.position);
        const cart =
          scene.globe?.pick(ray, scene) ||
          viewer.camera.pickEllipsoid(
            movement.position,
            Cesium.Ellipsoid.WGS84,
          );
        if (!cart) return;
        const c = Cesium.Cartographic.fromCartesian(cart);
        const lat = Cesium.Math.toDegrees(c.latitude);
        const lon = Cesium.Math.toDegrees(c.longitude);
        poiInput.value = `${lat.toFixed(5)},${lon.toFixed(5)}`;
        coordEl.textContent = `target ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
        setTargetMarker(lat, lon);
      }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
      coordEl.textContent = 'click map to set target';
    } else if (!on && mapTargetHandler) {
      mapTargetHandler.destroy();
      mapTargetHandler = null;
      coordEl.textContent = 'click map to set target';
    }
  }

  mapTargetChk.addEventListener('change', () =>
    enableMapTarget(mapTargetChk.checked),
  );
  pathAheadChk.addEventListener('change', () => {
    if (!pathAheadChk.checked && pathAheadEntity) {
      overlayDs?.entities.remove(pathAheadEntity);
      pathAheadEntity = null;
    }
  });
  groundTrackChk.addEventListener('change', () => {
    if (!groundTrackChk.checked && groundTrackEntity) {
      overlayDs?.entities.remove(groundTrackEntity);
      groundTrackEntity = null;
    }
  });

  /**
   * Draw the tactical overlays from the SAME normalized record the globe
   * layer renders. The previous implementation read `snapshot.records` off the
   * raw bridge body, which only ever emits `vehicles[]` with flat snake_case
   * fields — so both toggles drew nothing.
   */
  function drawOverlays(rec) {
    if (!viewer || !Cesium || !rec) return;
    if (!pathAheadChk.checked && !groundTrackChk.checked) return;
    const pos = rec.position || {};
    const vel = rec.velocity || {};
    const ds = ensureOverlayDs();
    if (!ds) return;

    if (pathAheadChk.checked) {
      // Project the path ahead: current position + N seconds along heading.
      const speed = Number.isFinite(vel.speed) ? vel.speed : 0;
      const heading = Number.isFinite(vel.heading) ? vel.heading : 0;
      const alt = pos.ellipsoidAltitude ?? pos.altitude ?? 0;
      const pts = [
        Cesium.Cartesian3.fromDegrees(pos.longitude, pos.latitude, alt),
      ];
      const ahead = 5;
      for (let i = 1; i <= ahead; i++) {
        const d = speed * i * 4; // 4s steps
        const rad = Cesium.Math.toRadians(heading);
        const dLat = (d * Math.cos(rad)) / 111320;
        const dLon =
          (d * Math.sin(rad)) /
          (111320 * Math.cos(Cesium.Math.toRadians(pos.latitude)));
        pts.push(
          Cesium.Cartesian3.fromDegrees(
            pos.longitude + dLon,
            pos.latitude + dLat,
            alt,
          ),
        );
      }
      if (!pathAheadEntity) {
        pathAheadEntity = ds.entities.add({
          id: 'uav-path-ahead',
          polyline: {
            width: 3,
            material: new Cesium.PolylineGlowMaterialProperty({
              glowPower: 0.2,
              color: Cesium.Color.LIME.withAlpha(0.8),
            }),
          },
        });
      }
      pathAheadEntity.polyline.positions = new Cesium.ConstantProperty(pts);
    }

    if (groundTrackChk.checked) {
      // Ground track: stamp the sub-aircraft point (terrain) as the drone flies.
      if (!groundTrackEntity) {
        groundTrackEntity = ds.entities.add({
          id: 'uav-ground-track',
          polyline: {
            width: 2,
            material: Cesium.Color.ORANGE.withAlpha(0.7),
            clampToGround: true,
          },
        });
        groundTrackEntity._pts = [];
      }
      const arr = groundTrackEntity._pts;
      const last = arr[arr.length - 1];
      const here = Cesium.Cartesian3.fromDegrees(
        pos.longitude,
        pos.latitude,
        0,
      );
      if (!last || Cesium.Cartesian3.distance(last, here) > 2) {
        arr.push(here);
        if (arr.length > 400) arr.shift();
        groundTrackEntity.polyline.positions = new Cesium.ConstantProperty(
          arr.slice(),
        );
      }
    }
    viewer.scene.requestRender?.();
  }

  // ---- Readout tick -----------------------------------------------------
  let timer = null;
  let ticks = 0;
  let controlStatus = null;
  let adoptTries = 0;

  /**
   * Learn the served table AND the running theater, then adopt it.
   *
   * Retried a bounded number of times because the panel mounts with the page
   * and the bridge is routinely started afterwards: a single load-time attempt
   * leaves the operator on the table default for the whole sortie. A bridge
   * that simply does not publish an active theater is not an error — the
   * running line says so and the retries stop.
   *
   * Two deliberate limits. Adoption moves the SELECTION only, never the
   * camera: where the operator is looking is theirs. And once a running
   * theater has been learned the panel stops asking, so a bridge restarted
   * onto a different theater is not picked up until the page reloads.
   */
  async function adoptRunningTheater() {
    adoptTries += 1;
    const before = theaters.active()?.id ?? null;
    await theaters.refresh();
    // A selection the operator did not make invalidates a target they set
    // under the theater it replaces (see clearTarget).
    const movedFrom = refreshTheaterOptions();
    const dropped = movedFrom ? clearTarget() : '';
    const running = theaters.active();
    const moved = Boolean(running?.id) && running.id !== before;
    if (!moved && !dropped) return running;
    // Never a silent drop: whatever else this pass says, it says what it threw
    // away and why, so the empty POI box is never a surprise at Launch.
    const note = dropped
      ? ` · target ${dropped} dropped — it was set for ${movedFrom}`
      : '';
    const head = !moved
      ? `theater: ${theaterSel.value}`
      : running.inTable
        ? `theater: ${theaters.get(running.id)?.label || running.id} — adopted from the running bridge`
        : `running theater "${running.id}" is not in this table`;
    setStatus(`${head}${note}`, moved && !running.inTable);
    return running;
  }

  /**
   * Whether this tick should re-ask which theater the bridge is flying.
   * An explicit "unknown, because the MCP server is not up yet" is a reason to
   * ask AGAIN, not a reason to stop: only a named theater settles it.
   */
  function adoptionDue() {
    if (operatorChose || theaters.active()?.known) return false;
    if (adoptTries >= ADOPT_MAX_TRIES) return false;
    return ticks % ADOPT_EVERY === 0;
  }

  function refreshVehicleOptions(records) {
    const names = records.map((r) => r.reference).filter(Boolean);
    if (!names.length) return;
    const previous = vehicleSel.value;
    vehicleSel.replaceChildren(
      ...names.map((name) => h('option', { value: name }, name)),
    );
    vehicleSel.value = names.includes(previous) ? previous : names[0];
  }

  function renderTelemetry(rec, mission) {
    if (!rec) {
      teleEl.textContent = 'telemetry: —';
      return;
    }
    const pos = rec.position || {};
    const vel = rec.velocity || {};
    const st = rec.status || {};
    const num = (v, digits = 0) =>
      Number.isFinite(v) ? v.toFixed(digits) : '—';
    teleEl.textContent =
      `lat ${num(pos.latitude, 5)}  lon ${num(pos.longitude, 5)}\n` +
      `hae ${num(pos.ellipsoidAltitude)}m  msl ${num(pos.mslAltitude)}m  ` +
      `agl ${num(pos.agl)}m  spd ${num(vel.speed, 1)}m/s\n` +
      `fuel ${num(st.fuelPct, 1)}%  bingo ${num(st.bingoFuelPct, 1)}%` +
      (mission ? `  · ${mission.phase}` : '') +
      (st.datumDegraded ? '\nDATUM DEGRADED' : '');
  }

  /**
   * Enrich fuel/BINGO from `/control/status` when the snapshot omits them.
   *
   * The fetch is rate-limited, the enrichment is NOT: the last reading is held
   * and applied on every tick. Applying it only on the fetch tick made the
   * BINGO %, the fuel bar's BINGO tick and the fuel warning/critical state
   * blink on for one tick in `CONTROL_STATUS_EVERY` and vanish in between —
   * a drone sitting just above its BINGO line looked safe four seconds out of
   * five. `records` are rebuilt each poll, so nothing carries over by itself.
   */
  async function enrichFromControl(rec) {
    if (rec?.status?.bingoFuelPct != null) return;
    if (ticks % CONTROL_STATUS_EVERY === 1 || controlStatus == null) {
      try {
        const fresh = unwrapMcp(await get(`/control/status/${vehicle()}`));
        // Keep the last good reading on a transient failure; a dropped poll is
        // not evidence that the BINGO line went away.
        if (fresh && typeof fresh === 'object') controlStatus = fresh;
      } catch {
        /* keep the previous reading; the readout ages, it does not blank */
      }
    }
    if (!controlStatus || !rec) return;
    if (
      rec.status.bingoFuelPct == null &&
      Number.isFinite(controlStatus.bingo_fuel_pct)
    )
      rec.status.bingoFuelPct = controlStatus.bingo_fuel_pct;
    if (
      rec.status.etaToBingoS == null &&
      Number.isFinite(controlStatus.eta_to_bingo_s)
    )
      rec.status.etaToBingoS = controlStatus.eta_to_bingo_s;
    if (rec.status.fuelPct == null && Number.isFinite(controlStatus.fuel_pct))
      rec.status.fuelPct = controlStatus.fuel_pct;
  }

  async function tick() {
    ticks += 1;
    if (adoptionDue()) await adoptRunningTheater();
    let snap = null;
    try {
      snap = await live.getSnapshot({}, {});
    } catch {
      teleEl.textContent = 'telemetry: offline';
      hud.reset();
      return null;
    }
    const records = snap.records || [];
    refreshVehicleOptions(records);
    const rec =
      records.find((r) => r.reference === vehicle()) || records[0] || null;
    await enrichFromControl(rec);
    const missions = snap.missions || [];
    const mission =
      missions.find((m) => m.vehicle === rec?.reference) || missions[0] || null;
    renderTelemetry(rec, mission);
    hud.update({
      mission,
      vehicle: rec,
      missionsServed: snap.sections?.missions !== false,
      simState: snap.simState,
    });
    // Fall back to /tracks only while the bridge serves NO contacts[] at all.
    // Keying on `.length` meant a served, legitimately empty roster resurrected
    // the /tracks rows — the operator would see contacts the feed had dropped.
    const served = snap.sections?.contacts ?? Boolean(snap.contacts?.length);
    const contacts = served
      ? snap.contacts || []
      : normalizeTargetsAsContacts(snap.targets);
    roster.update(contacts);
    // Mission views list everything in the air, not just the selected vehicle,
    // so it takes the whole snapshot rather than the single chosen record.
    missionViews.update(snap);
    // normalizeMission publishes `missionId`; `mission_id` is the raw bridge
    // spelling and would silently clear the highlight on every tick.
    const activeId = mission?.missionId ?? mission?.id ?? mission?.mission_id;
    if (activeId) missionViews.setActive(activeId);
    drawOverlays(rec);
    return snap;
  }

  /**
   * Reshape `/tracks` rows and run them through the source's OWN contact
   * normalizer, so the fallback roster grades confidence, folds threat-level
   * casing and reads timestamps exactly as `contacts[]` will. Hand-rolling a
   * second mapper here let `threat_level: "High"` through unchanged, which
   * scored 0 on the roster's threat ranking and matched none of its colour
   * rules — the worst contact sorted last and rendered as routine.
   * @param {Array<object>} targets raw `/tracks` rows
   * @returns {Array<object>} normalized contacts
   */
  function normalizeTargetsAsContacts(targets) {
    return (Array.isArray(targets) ? targets : [])
      .map((row) => normalizeContact(trackRowAsContact(row)))
      .filter(Boolean);
  }

  // ---- Alarm stream (SSE /events) ---------------------------------------
  const resolvedFactory =
    eventSourceFactory ||
    (typeof globalThis.EventSource === 'function'
      ? (url) => new globalThis.EventSource(url)
      : null);
  const events = createUavEventStream({
    eventSourceFactory: resolvedFactory,
    // Getters, not values: a reconnect must pick up an origin the operator
    // changed after the panel mounted.
    baseUrl: bridgeUrl,
    token: token,
    onAlarm: (alarm) => alarms.push(alarm),
    onStatus: (status, detail) => alarms.setStreamStatus(status, detail),
  });

  launchBtn.addEventListener('click', launch);
  abortBtn.addEventListener('click', abort);
  cockpitBtn.addEventListener('click', () => {
    const reference = vehicle();
    const ok = onEnterCockpit?.(reference);
    setStatus(
      ok === false
        ? 'cockpit: no drone tracked'
        : `cockpit: tracking ${reference}`,
    );
  });
  theaterSel.addEventListener('change', () => {
    const t = theater();
    if (!t) return;
    setStatus(
      `theater: ${t.label}${theaters.isOffline() ? ' (offline table)' : ''}`,
    );
    onTheater?.(t.home);
  });

  return {
    mount(parent = null) {
      const host =
        parent || document.getElementById('left-panel-stack') || document.body;
      host.append(style, panel);
      // Toasts float over the globe, not inside the rail.
      const overlayHost = document.body || host;
      if (alarms.style) overlayHost.append(alarms.style);
      overlayHost.append(alarms.element);
      if (hud.style) host.append(hud.style);
      if (roster.style) host.append(roster.style);
      if (missionViews.style) host.append(missionViews.style);
      setHidden(alarms.element, true);
      // Served theater table first; the bundled copy stays labelled offline.
      // The same pass adopts whichever theater the bridge is flying.
      Promise.resolve(adoptRunningTheater()).catch(() => {});
      events.start();
      timer = setInterval(tick, tickMs);
      tick();
      return this;
    },
    destroy() {
      clearInterval(timer);
      timer = null;
      events.stop();
      alarms.destroy();
      roster.destroy();
      missionViews.destroy();
      hud.destroy();
      enableMapTarget(false);
      if (overlayDs && viewer) viewer.dataSources.remove(overlayDs, true);
      panel.remove();
      style.remove();
    },
    /** Force one readout tick — used by tests and by manual refresh. */
    tick,
    /** Force one theater-adoption pass — used by tests and by manual refresh. */
    adoptRunningTheater,
    /** Open the panel. Clears the operator's manual-collapse latch. */
    expand() {
      operatorCollapsed = false;
      setCollapsed(false);
    },
    /** Close the panel, as if the operator had. */
    collapse() {
      operatorCollapsed = true;
      setCollapsed(true);
    },
    /** True when the body is showing. */
    isExpanded: () => !hasClass(panel, 'collapsed'),
    _panel: panel,
    _running: runningEl,
    _hud: hud,
    _alarms: alarms,
    _roster: roster,
    _missionViews: missionViews,
    _theaters: theaters,
    _events: events,
  };
}
