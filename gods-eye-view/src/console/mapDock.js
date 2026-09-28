/**
 * The map overview dock (WG spec §4.2.7): the analyst column's header while
 * the console shows the map, in place of the tracking dock.
 *
 *   mini orb + "Back to console"          (Esc)
 *   Map  {label}  {W × H} km
 *   [Track Drone1]
 *   Show ☑ Sites {n}
 *   Map data feed down. Sites may be missing, not absent.   (when degraded)
 *   {n} more sites not drawn.
 *   ▸ Key                                  (site glyphs and their words)
 *   Map data: © OpenStreetMap contributors, ODbL.   (whenever sites are drawn)
 *
 * `mapDockModel()` is pure: it reads the mode's map target, the intel graph
 * (`meta.sites`, vehicles) and, when the port offers them, the overlay's own
 * counts. `createMapDock()` builds the DOM with ../ui/uavDom.js only, so it
 * mounts under GEV's stub document in node tests.
 *
 * Every label here is untrusted text (theater labels, OSM names, vehicle
 * names): it passes through `safeText` (bidi and control characters
 * stripped) and reaches the DOM only as text nodes (§0.2, §3.11). The key's
 * glyphs are the only markup, built from constants by `glyphSvg`.
 */

import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { glyphSvg } from './orb/glyphs.js';
import { SITE_CATEGORIES } from './orb/glyphPaths.js';
import {
  MAP_ATTRIBUTION,
  SITE_CAVEAT,
  SITES_DEGRADED_TEXT,
  areaText,
  safeText,
  siteWord,
  sitesNotDrawnText,
} from './orb/placeText.js';

/** The overlay draws at most this many site billboards (§4.2.7). */
export const MAP_SITES_DRAWN_MAX = 150;
/** At most this many Track buttons (one per aircraft). */
export const MAP_TRACK_MAX = 4;

/** Dock copy (Appendix B; "Map", "Back to console", "Show"). */
export const MAP_DOCK_COPY = Object.freeze({
  region: 'Map',
  map: 'Map',
  back: 'Back to console',
  esc: 'Esc',
  show: 'Show',
  sites: (n) => `Sites ${n}`,
  key: 'Key',
  track: (v) => `Track ${v}`,
  unrecognised: 'Unrecognised map item',
});

function count(value) {
  const n = typeof value === 'string' && !value.trim() ? NaN : Number(value);
  return value != null && Number.isFinite(n)
    ? Math.max(0, Math.floor(n))
    : null;
}

function nodesOf(graph) {
  return Array.isArray(graph?.nodes) ? graph.nodes.filter(Boolean) : [];
}

/** Vehicle names for the Track buttons, as safe text, in graph order. */
export function dockVehicles(graph) {
  const out = [];
  for (const node of nodesOf(graph)) {
    if (node.type !== 'vehicle') continue;
    const raw =
      typeof node.label === 'string' && node.label
        ? node.label
        : String(node.id ?? '').replace(/^veh:/, '');
    const name = safeText(raw, 40);
    if (name && !out.includes(name)) out.push(name);
    if (out.length >= MAP_TRACK_MAX) break;
  }
  return out;
}

/**
 * The site numbers the dock shows. Sites fetched come from
 * `meta.sites.total` (or the site nodes, when more); sites drawn come from
 * the overlay when the port reports them (`{sites:{drawn, total}}`), else
 * the fetched count capped at the overlay's 150 billboards.
 * @returns {{total:number, drawn:number, notDrawn:number, degraded:boolean}}
 */
export function dockSiteCounts(graph, overlay = null) {
  const meta = graph?.meta?.sites || {};
  const inGraph = nodesOf(graph).filter((n) => n.type === 'site').length;
  const fetched = Math.max(count(meta.total) ?? 0, inGraph);
  const stats = overlay && typeof overlay === 'object' ? overlay.sites : null;
  const total = Math.max(fetched, count(stats?.total) ?? 0);
  const reported = count(stats?.drawn);
  const drawn =
    reported != null
      ? Math.min(reported, total)
      : Math.min(total, MAP_SITES_DRAWN_MAX);
  return {
    total,
    drawn,
    notDrawn: Math.max(0, total - drawn),
    degraded: meta.degraded === true,
  };
}

/**
 * Everything the dock shows, as safe text and numbers (pure).
 * @param {object} p
 * @param {{label?:string|null, bbox?:number[]|null}|null} p.target the map target
 * @param {object|null} p.graph the intel graph
 * @param {object|null} [p.overlay] the port's overlay counts, if any
 * @param {boolean} [p.sitesOn] the Sites switch
 */
export function mapDockModel({
  target = null,
  graph = null,
  overlay = null,
  sitesOn = true,
} = {}) {
  const label = safeText(target?.label ?? '', 80);
  const size = target?.bbox ? areaText(target.bbox) : null;
  const sites = dockSiteCounts(graph, overlay);
  const on = sitesOn !== false;
  return {
    label,
    size,
    line: [MAP_DOCK_COPY.map, label, size].filter(Boolean),
    vehicles: dockVehicles(graph),
    sites: { ...sites, on },
    sitesRow: sites.total > 0,
    sitesLabel: MAP_DOCK_COPY.sites(on ? sites.drawn : sites.total),
    degradedText: sites.degraded ? SITES_DEGRADED_TEXT : null,
    notDrawnText:
      on && sites.drawn > 0 && sites.notDrawn > 0
        ? sitesNotDrawnText(sites.notDrawn)
        : null,
    attribution: on && sites.drawn > 0 ? MAP_ATTRIBUTION : null,
  };
}

/** The Key: one row per site glyph, then the unrecognised grey point. */
function keyRows() {
  const rows = SITE_CATEGORIES.map((category) => {
    const glyph = h('span', {
      class: 'ic-mapdock__glyph',
      'aria-hidden': 'true',
    });
    // Constants only (glyphSvg looks up type and category), never text.
    const svg = glyphSvg('site', { size: 16, category });
    if (typeof svg === 'string' && svg) glyph.innerHTML = svg;
    return h(
      'li',
      { class: 'ic-mapdock__keyrow', 'data-category': category },
      glyph,
      h('span', { class: 'ic-mapdock__keyword' }, siteWord(category)),
    );
  });
  rows.push(
    h(
      'li',
      { class: 'ic-mapdock__keyrow', 'data-category': 'unrecognised' },
      h('span', { class: 'ic-mapdock__dot', 'aria-hidden': 'true' }),
      h('span', { class: 'ic-mapdock__keyword' }, MAP_DOCK_COPY.unrecognised),
    ),
  );
  return rows;
}

/**
 * Build the dock. The shell owns what the buttons do.
 * @param {object} [hooks]
 * @param {() => void} [hooks.onBack] Back to console
 * @param {(vehicle:string) => void} [hooks.onTrack] Track {vehicle}
 * @param {(on:boolean) => void} [hooks.onSites] the Sites switch
 * @returns {{element:object, back:object, mini:object, badge:object,
 *   update(model:object):void, destroy():void}}
 */
export function createMapDock({ onBack, onTrack, onSites } = {}) {
  let destroyed = false;
  let model = null;

  const mini = h('span', { class: 'ic-dock__mini', 'aria-hidden': 'true' });
  const badge = h('span', { class: 'ic-dock__badge', hidden: true });
  const back = h(
    'button',
    {
      type: 'button',
      class: 'ic-dock__back ic-mapdock__back',
      'aria-keyshortcuts': 'Escape',
      'data-key': 'map:back',
    },
    h('span', { class: 'ic-dock__miniwrap' }, mini, badge),
    h('span', { class: 'ic-dock__backlabel' }, MAP_DOCK_COPY.back),
    h(
      'kbd',
      { class: 'ic-dock__kbd', 'aria-hidden': 'true' },
      MAP_DOCK_COPY.esc,
    ),
  );
  back.addEventListener('click', () => {
    if (!destroyed) onBack?.();
  });

  const line = h('p', { class: 'ic-mapdock__line' });
  const actions = h('div', { class: 'ic-mapdock__actions' });

  const sitesBox = h('input', {
    type: 'checkbox',
    class: 'ic-mapdock__check',
    'data-key': 'map:sites',
  });
  sitesBox.checked = true;
  const sitesText = h('span', { class: 'ic-mapdock__sites' });
  const sitesRow = h(
    'label',
    { class: 'ic-mapdock__toggle', hidden: true },
    h('span', { class: 'ic-mapdock__show' }, MAP_DOCK_COPY.show),
    sitesBox,
    sitesText,
  );
  sitesBox.addEventListener('change', () => {
    if (!destroyed) onSites?.(sitesBox.checked !== false);
  });

  const degraded = h('p', {
    class: 'ic-mapdock__warn',
    role: 'status',
    hidden: true,
  });
  const notDrawn = h('p', { class: 'ic-mapdock__note', hidden: true });
  const key = h(
    'details',
    { class: 'ic-mapdock__key' },
    h('summary', { class: 'ic-mapdock__keysum' }, MAP_DOCK_COPY.key),
    h('ul', { class: 'ic-mapdock__keylist' }, ...keyRows()),
    h('p', { class: 'ic-mapdock__caveat' }, SITE_CAVEAT),
  );
  const attribution = h('p', {
    class: 'ic-mapdock__attribution',
    hidden: true,
  });

  const element = h(
    'section',
    {
      class: 'ic-dock ic-mapdock',
      'aria-label': MAP_DOCK_COPY.region,
      hidden: true,
    },
    back,
    line,
    actions,
    sitesRow,
    degraded,
    notDrawn,
    key,
    attribution,
  );

  function trackButton(name) {
    const b = h(
      'button',
      {
        type: 'button',
        class: 'ic-btn',
        'data-variant': 'quiet',
        'data-key': `map:track:${name}`,
      },
      h(
        'span',
        { class: 'ic-icon material-symbols-outlined', 'aria-hidden': 'true' },
        'my_location',
      ),
      h('span', { class: 'ic-btn__label' }, MAP_DOCK_COPY.track(name)),
    );
    b.addEventListener('click', () => {
      if (!destroyed) onTrack?.(name);
    });
    return b;
  }

  function update(next) {
    if (destroyed || !next) return;
    const prev = model;
    model = next;
    // The line: "Map  {label}  {W × H} km", each part a text node. The
    // two-space separators read in textContent; flex drops them on screen.
    const lineSig = next.line.join('\u0001');
    if (!prev || prev.line.join('\u0001') !== lineSig) {
      const kids = [];
      next.line.forEach((part, i) => {
        if (i > 0) kids.push('  ');
        const cls =
          i === 0
            ? 'ic-mapdock__word'
            : part === next.size
              ? 'ic-mapdock__size'
              : 'ic-mapdock__label';
        kids.push(h('span', { class: cls }, part));
      });
      replaceKids(line, kids);
    }
    // Rebuild the Track buttons only when the fleet changes (keeps focus).
    if (!prev || prev.vehicles.join('\u0001') !== next.vehicles.join('\u0001'))
      replaceKids(actions, next.vehicles.map(trackButton));
    setHidden(actions, next.vehicles.length === 0);
    sitesText.textContent = next.sitesLabel;
    if (sitesBox.checked !== next.sites.on) sitesBox.checked = next.sites.on;
    setHidden(sitesRow, !next.sitesRow);
    degraded.textContent = next.degradedText || '';
    setHidden(degraded, !next.degradedText);
    notDrawn.textContent = next.notDrawnText || '';
    setHidden(notDrawn, !next.notDrawnText);
    attribution.textContent = next.attribution || '';
    setHidden(attribution, !next.attribution);
  }

  return {
    element,
    back,
    mini,
    badge,
    get model() {
      return model;
    },
    update,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      element.remove?.();
    },
  };
}
