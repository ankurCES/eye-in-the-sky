/**
 * Mission views: pick a running mission and fly its lead drone in FPV.
 *
 * The panel can command one vehicle at a time, but a theater can have several
 * missions in the air at once. This surface lists what is actually flying --
 * from `/snapshot.missions[]` joined to `/snapshot.vehicles[]` -- and lets the
 * operator jump the camera to any of them. Selecting a mission tracks its LEAD
 * drone and enters the cockpit, which is a real first-person view: the cockpit
 * controller holds the camera on the drone's rendered position and slews
 * heading onto its ground track every frame, so the map flies with the aircraft
 * rather than cutting to it.
 *
 * Read-only, like the rest of God's Eye View: selecting a mission changes what
 * the operator is LOOKING at, never what the aircraft is doing. The MCP server
 * remains the only command path.
 */

import { h, replaceKids, setClass, setHidden, setWidthPct } from './uavDom.js';

const STYLE = `
.uav-views{pointer-events:auto;margin-top:10px;padding-top:9px;
  border-top:1px solid #173a30;font:10px/1.45 "SF Mono",Menlo,monospace;
  color:#9fd9c8}
.uav-views .uav-views-head{display:flex;align-items:center;gap:6px;
  color:#1de9b6;letter-spacing:.12em;text-transform:uppercase;cursor:pointer;
  user-select:none}
.uav-views .uav-views-count{margin-left:auto;color:#5f8c80}
.uav-views .uav-views-caret{color:#1de9b6;font-size:12px;line-height:1}
.uav-views .uav-views-list{display:flex;flex-direction:column;gap:4px;
  margin-top:6px;max-height:210px;overflow-y:auto}
.uav-views.is-closed .uav-views-list,
.uav-views.is-closed .uav-views-empty{display:none}
.uav-views .uav-views-empty{color:#5f8c80;margin-top:6px}
.uav-views .uav-view{text-align:left;width:100%;box-sizing:border-box;
  background:#0d1512;border:1px solid #234b40;border-radius:4px;padding:5px 7px;
  color:#d7f5ec;font:10px/1.4 inherit;cursor:pointer;display:block}
.uav-views .uav-view:hover{border-color:#1de9b6}
.uav-views .uav-view.is-active{border-color:#1de9b6;background:#10231d}
.uav-views .uav-view-top{display:flex;align-items:baseline;gap:6px}
.uav-views .uav-view-kind{color:#1de9b6;text-transform:uppercase;
  letter-spacing:.06em}
.uav-views .uav-view-phase{margin-left:auto;color:#7fb5a6}
.uav-views .uav-view-lead{color:#d7f5ec}
.uav-views .uav-view-meta{color:#5f8c80;margin-top:2px}
.uav-views .uav-view-fleet{color:#7fb5a6}
.uav-views .uav-view-bar{height:2px;background:#173a30;border-radius:2px;
  margin-top:4px;overflow:hidden}
.uav-views .uav-view-bar span{display:block;height:100%;background:#1de9b6}
.uav-views .uav-view.is-incomplete .uav-view-phase{color:#ffd166}
`;

/** Phases that mean "this mission is still worth watching". */
const LIVE_PHASES = new Set(['planning', 'executing', 'rtb']);

function pct(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null;
}

/**
 * Join missions to the vehicles flying them.
 *
 * The lead drone is the mission's own `vehicle`; any other vehicle reporting
 * the same `mission` id is a wingman. Exported for tests: the joining rule is
 * the part worth pinning, not the DOM.
 */
export function missionViewRows(snapshot = {}) {
  const missions = Array.isArray(snapshot.missions) ? snapshot.missions : [];
  const records = Array.isArray(snapshot.records) ? snapshot.records : [];
  return missions.map((m) => {
    // The live source normalizes to camelCase (sources/live/uav.js
    // normalizeMission); the bridge's own /snapshot is snake_case. Read the
    // normalized contract first and fall back, so this works against either
    // without silently reporting "no telemetry yet" on a healthy mission.
    const missionId = String(m?.missionId ?? m?.id ?? m?.mission_id ?? '');
    const lead = String(m?.vehicle || '');
    const onMission = records
      .filter((r) => {
        const ref = String(r?.reference || '');
        if (ref === lead) return true;
        const mid = String(r?.status?.mission || '');
        return mid && missionId && mid === missionId;
      })
      .map((r) => String(r.reference));
    // Lead first ALWAYS, whatever order the records arrived in -- the UI names
    // it as the drone the camera will follow, so it cannot be whichever wingman
    // the snapshot happened to list first.
    const fleet = lead
      ? [lead, ...onMission.filter((v) => v !== lead)]
      : onMission;
    return {
      missionId,
      kind: String(m?.kind || 'mission'),
      phase: String(m?.phase || 'unknown'),
      lead,
      fleet,
      progressPct: pct(m?.progressPct ?? m?.progress_pct),
      coveragePct: pct(m?.coveragePct ?? m?.coverage_pct),
      incompleteReason: m?.incompleteReason ?? m?.incomplete_reason ?? null,
      live: LIVE_PHASES.has(String(m?.phase || '')),
    };
  });
}

/**
 * @param {object} [opts]
 * @param {(row: object) => void} [opts.onSelect] operator picked a mission
 * @param {Document} [opts.doc]
 */
export function createUavMissionViews({ onSelect } = {}) {
  const style = h('style');
  style.textContent = STYLE;

  const caret = h('span', { class: 'uav-views-caret' }, '−');
  const count = h('span', { class: 'uav-views-count' }, '0');
  const head = h(
    'div',
    { class: 'uav-views-head' },
    h('span', {}, 'Mission views'),
    count,
    caret,
  );
  const list = h('div', { class: 'uav-views-list' });
  const empty = h('div', { class: 'uav-views-empty' }, 'no missions running');
  const element = h('div', { class: 'uav-views' }, head, empty, list);

  let open = true;
  let activeMissionId = null;
  let rows = [];

  function setOpen(next) {
    open = next;
    setClass(element, 'is-closed', !open);
    caret.textContent = open ? '−' : '+';
  }
  head.addEventListener('click', () => setOpen(!open));

  function buildRow(row) {
    const btn = h('button', { type: 'button', class: 'uav-view' });
    if (row.missionId && row.missionId === activeMissionId)
      setClass(btn, 'is-active', true);
    if (row.incompleteReason) setClass(btn, 'is-incomplete', true);

    btn.append(
      h(
        'div',
        { class: 'uav-view-top' },
        h('span', { class: 'uav-view-kind' }, row.kind),
        h('span', { class: 'uav-view-lead' }, row.lead || '—'),
        h(
          'span',
          { class: 'uav-view-phase' },
          row.incompleteReason || row.phase,
        ),
      ),
    );

    const bits = [];
    if (row.progressPct != null) bits.push(`${row.progressPct.toFixed(0)}%`);
    if (row.coveragePct != null)
      bits.push(`cov ${row.coveragePct.toFixed(0)}%`);
    // Name the fleet only when there IS one: "1 drone" on every row is noise,
    // and the lead is already named above.
    if (row.fleet.length > 1) {
      const others = row.fleet.filter((v) => v !== row.lead);
      bits.push(`+${others.length} (${others.join(', ')})`);
    }
    const meta = h(
      'div',
      {
        class:
          row.fleet.length > 1
            ? 'uav-view-meta uav-view-fleet'
            : 'uav-view-meta',
      },
      bits.join(' · ') || 'no telemetry yet',
    );
    btn.append(meta);

    if (row.progressPct != null) {
      const fill = h('span');
      setWidthPct(fill, row.progressPct);
      btn.append(h('div', { class: 'uav-view-bar' }, fill));
    }

    btn.addEventListener('click', () => {
      activeMissionId = row.missionId || null;
      render();
      onSelect?.(row);
    });
    return btn;
  }

  function render() {
    const live = rows.filter((r) => r.live);
    const shown = live.length ? live : rows;
    count.textContent = String(live.length);
    setHidden(empty, shown.length > 0);
    replaceKids(list, shown.map(buildRow));
  }

  return {
    element,
    style,
    /** Feed a normalized source snapshot (records + missions). */
    update(snapshot) {
      rows = missionViewRows(snapshot);
      render();
      return rows;
    },
    /** Mark which mission the camera is currently on. */
    setActive(missionId) {
      activeMissionId = missionId || null;
      render();
    },
    isOpen: () => open,
    setOpen,
    destroy() {
      element.remove();
      style.remove();
    },
    _rows: () => rows,
    _list: list,
  };
}
