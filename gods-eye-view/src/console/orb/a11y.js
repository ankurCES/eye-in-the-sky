/**
 * The orb's accessible twin and its List view (UX spec §9).
 *
 * The canvas is aria-hidden. The Tab stop is a `role="listbox"` labelled
 * "Entities on the orb", with one `role="group"` per band and
 * `aria-activedescendant` tracking the keyboard's entity. List view is a
 * sortable table (type, name, status, register, salience, last seen in Z):
 * the full visual alternative to the orb.
 *
 * The simulated wargame (WG §5.3.3–§5.3.5): forces, engagements and
 * vectors are grouped in their session bands, read "Force", "Engagement" and
 * "Vector" with their state or phase words, and sit in the Scenario register
 * (a Sand tag, "Set by the wargame, not seen by a sensor."). List view has
 * the orb's Blue view too: `setView({umpire: false})` drops every force that
 * is not provably blue and every red axis.
 *
 * Built only with the uavDom helpers so the stub DOM used by node:test works.
 */

import { h, replaceKids, setHidden } from '../../ui/uavDom.js';
import { isOrbType } from './glyphs.js';
import { BANDS, BAND_ORDER, bandOfNode } from './layout.js';
import { filterForView, isWargameType } from './wargameStyles.js';
import { SCENARIO_TOOLTIP, wargameDisplayStatus } from './wargameText.js';
import {
  formatZ,
  nodeLabel,
  nodeSubtitle,
  optionText,
  registerOf,
  splitSegments,
  statusWord,
  typeLabel,
} from './text.js';

let instances = 0;

const compareIds = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const bySalience = (a, b) =>
  (b.salience || 0) - (a.salience || 0) || compareIds(a.id, b.id);

/**
 * The status key List view colours a row's status word by: an unknown type's
 * status is never read (WG §4.2.1), and a site's is context, not a state.
 */
export function displayStatus(node) {
  if (!isOrbType(node?.type)) return 'unknown';
  if (node.type === 'site') return 'mapped';
  if (isWargameType(node.type)) return wargameDisplayStatus(node);
  return String(node.status || 'unknown');
}

/**
 * Group nodes by band (pole to pole), most salient first inside each band.
 * @returns {Array<{key:string, caption:string, nodes:object[]}>}
 */
export function groupByBand(nodes) {
  const groups = new Map(BAND_ORDER.map((key) => [key, []]));
  for (const node of nodes || []) {
    if (node && typeof node.id === 'string')
      groups.get(bandOfNode(node)).push(node);
  }
  const out = [];
  for (const [key, list] of groups) {
    if (!list.length) continue;
    out.push({
      key,
      caption: BANDS[key].caption,
      nodes: list.sort(bySalience),
    });
  }
  return out;
}

/**
 * The listbox twin.
 * @param {HTMLElement|null} host where the listbox is appended
 * @param {{onKeyDown?:Function, onFocusChange?:(focused:boolean)=>void,
 *   onOptionClick?:(id:string)=>void}} [handlers]
 */
export function createOrbTwin(
  host,
  { onKeyDown, onFocusChange, onOptionClick } = {},
) {
  instances += 1;
  const prefix = `ic-orb-${instances}`;
  const listbox = h('div', {
    class: 'ic-orb-twin',
    role: 'listbox',
    tabindex: '0',
    'aria-label': 'Entities on the orb',
  });
  const domIds = new Map();
  const options = new Map();
  let seq = 0;
  let order = [];
  let bandStarts = [];
  let active = null;
  let selected = null;

  const domId = (id) => {
    let value = domIds.get(id);
    if (!value) {
      seq += 1;
      value = `${prefix}-o${seq}`;
      domIds.set(id, value);
    }
    return value;
  };

  const listeners = [
    ['keydown', (event) => onKeyDown?.(event)],
    ['focus', () => onFocusChange?.(true)],
    ['blur', () => onFocusChange?.(false)],
  ];
  for (const [type, fn] of listeners) listbox.addEventListener?.(type, fn);
  host?.append?.(listbox);

  function optionFor(node, isNew, extra) {
    let option = options.get(node.id);
    if (!option) {
      option = h('div', {
        class: 'ic-orb-twin__option',
        role: 'option',
        id: domId(node.id),
        'aria-selected': 'false',
      });
      option.addEventListener?.('click', () => onOptionClick?.(node.id));
      options.set(node.id, option);
    }
    const text = optionText(node, { ...extra, isNew });
    if (option._icText !== text) {
      option.textContent = text;
      option._icText = text;
    }
    return option;
  }

  const twin = {
    element: listbox,
    /**
     * Rebuild the groups. Option elements are reused by id so the active
     * descendant survives a poll.
     * @param {object[]} nodes graph nodes
     * @param {(id:string)=>boolean} [isNew]
     * @param {(id:string)=>({downSince?:number|null})} [describe] per-node
     *   wording state the orb tracks (when a feed was first seen down)
     */
    setEntities(nodes, isNew = () => false, describe = () => null) {
      const groups = groupByBand(nodes);
      const seen = new Set();
      order = [];
      bandStarts = [];
      const groupEls = groups.map((group) => {
        bandStarts.push(order.length);
        const kids = group.nodes.map((node) => {
          seen.add(node.id);
          order.push(node.id);
          return optionFor(node, isNew(node.id), describe(node.id) || {});
        });
        return h(
          'div',
          {
            class: 'ic-orb-twin__group',
            role: 'group',
            'aria-label': `${group.caption}, ${group.nodes.length}`,
          },
          ...kids,
        );
      });
      for (const id of [...options.keys()]) {
        if (!seen.has(id)) options.delete(id);
      }
      replaceKids(listbox, groupEls);
      if (active && !seen.has(active)) twin.setActive(null);
      if (selected && !seen.has(selected)) selected = null;
      return order.length;
    },
    get order() {
      return order.slice();
    },
    get active() {
      return active;
    },
    setActive(id) {
      active = id && options.has(id) ? id : null;
      if (active) listbox.setAttribute('aria-activedescendant', domId(active));
      else listbox.removeAttribute?.('aria-activedescendant');
      return active;
    },
    setSelected(id) {
      if (selected && options.has(selected))
        options.get(selected).setAttribute('aria-selected', 'false');
      selected = id && options.has(id) ? id : null;
      if (selected) options.get(selected).setAttribute('aria-selected', 'true');
    },
    /** Next/previous entity in listbox order; returns the new active id. */
    step(delta) {
      if (!order.length) return null;
      const at = active ? order.indexOf(active) : -1;
      const next =
        at < 0
          ? delta > 0
            ? 0
            : order.length - 1
          : Math.max(0, Math.min(order.length - 1, at + delta));
      return twin.setActive(order[next]);
    },
    /** First entity of the next/previous band. */
    stepBand(delta) {
      if (!order.length) return null;
      const at = active ? order.indexOf(active) : -1;
      let band = 0;
      for (let b = 0; b < bandStarts.length; b += 1)
        if (bandStarts[b] <= at) band = b;
      if (at < 0) band = delta > 0 ? -1 : bandStarts.length;
      const target = Math.max(0, Math.min(bandStarts.length - 1, band + delta));
      return twin.setActive(order[bandStarts[target]]);
    },
    focus() {
      listbox.focus?.();
    },
    destroy() {
      for (const [type, fn] of listeners)
        listbox.removeEventListener?.(type, fn);
      listbox.remove?.();
      options.clear();
    },
  };
  return twin;
}

/** Column definitions for List view. */
export const LIST_COLUMNS = Object.freeze([
  Object.freeze({ key: 'type', label: 'Type' }),
  Object.freeze({ key: 'label', label: 'Name' }),
  Object.freeze({ key: 'status', label: 'Status' }),
  Object.freeze({ key: 'register', label: 'Register' }),
  Object.freeze({ key: 'salience', label: 'Salience', numeric: true }),
  Object.freeze({ key: 'seen', label: 'Last seen', numeric: true }),
]);

const BAND_RANK = new Map(BAND_ORDER.map((key, i) => [key, i]));

/** Sort rows for List view. Default: band order, then salience. */
export function sortRows(nodes, key = 'type', dir = 'ascending') {
  const sign = dir === 'descending' ? -1 : 1;
  const value = (node) => {
    switch (key) {
      case 'label':
        return nodeLabel(node).toLowerCase();
      case 'status':
        return statusWord(node);
      case 'register':
        return registerOf(node);
      case 'salience':
        return Number(node.salience) || 0;
      case 'seen':
        return Number.isFinite(node.ts_ms) ? node.ts_ms : -Infinity;
      default:
        return BAND_RANK.get(bandOfNode(node)) ?? 99;
    }
  };
  return [...nodes].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    const primary = va < vb ? -1 : va > vb ? 1 : 0;
    return primary * sign || bySalience(a, b);
  });
}

/**
 * List view: a sortable table of every entity, filterable like the orb.
 * @param {HTMLElement|null} host
 * @param {{onSelect?:(id:string)=>void}} [handlers]
 */
export function createOrbListView(host, { onSelect } = {}) {
  let nodes = [];
  let graphIn = null;
  let viewMode = { umpire: true };
  let predicate = null;
  let sort = { key: 'type', dir: 'ascending' };
  /** The rows on screen: [{id, key, tr, open}], in table order. */
  let rendered = [];
  const headerCells = new Map();
  const sortButtons = [];
  const headRow = h(
    'tr',
    { class: 'ic-orb-list__head' },
    ...LIST_COLUMNS.map((column) => {
      const button = h(
        'button',
        { class: 'ic-orb-list__sort', type: 'button' },
        column.label,
      );
      button.addEventListener?.('click', () => view.sort(column.key));
      sortButtons.push(button);
      const cell = h(
        'th',
        {
          class: column.numeric
            ? 'ic-orb-list__th ic-orb-list__th--num'
            : 'ic-orb-list__th',
          scope: 'col',
          'aria-sort': 'none',
        },
        button,
      );
      headerCells.set(column.key, cell);
      return cell;
    }),
  );
  const body = h('tbody', { class: 'ic-orb-list__body' });
  const table = h(
    'table',
    { class: 'ic-orb-list__table' },
    h('caption', { class: 'ic-orb-vh' }, 'Entities in the picture'),
    h('thead', {}, headRow),
    body,
  );
  const empty = h('p', { class: 'ic-orb-list__empty' });
  setHidden(empty, true);
  const element = h('div', { class: 'ic-orb-list' }, table, empty);
  host?.append?.(element);

  const cell = (text, className = 'ic-orb-list__td') =>
    h('td', { class: className }, text);

  /** Everything a row shows, so an unchanged poll leaves the table alone. */
  const rowKey = (node) =>
    JSON.stringify([
      node.id,
      node.type,
      nodeLabel(node),
      nodeSubtitle(node),
      node.status,
      statusWord(node),
      registerOf(node),
      node.salience,
      node.ts_ms,
    ]);

  function row(node) {
    const open = h(
      'button',
      { class: 'ic-orb-list__open', type: 'button' },
      nodeLabel(node),
    );
    open.addEventListener?.('click', () => onSelect?.(node.id));
    const name = h(
      'td',
      { class: 'ic-orb-list__td' },
      open,
      h('span', { class: 'ic-orb-list__id' }, node.id),
      ...splitSegments(nodeSubtitle(node)).map((segment) =>
        h('span', { class: 'ic-orb-list__seg' }, segment),
      ),
    );
    const status = h(
      'td',
      {
        class: 'ic-orb-list__td ic-orb-list__status',
        'data-status': displayStatus(node),
      },
      statusWord(node),
    );
    const salience = Number(node.salience);
    const tr = h(
      'tr',
      { class: 'ic-orb-list__row', 'data-id': node.id },
      cell(
        typeLabel(node.type, { wargame: true }),
        isOrbType(node.type)
          ? 'ic-orb-list__td'
          : 'ic-orb-list__td ic-orb-list__type--unrecognised',
      ),
      name,
      status,
      isWargameType(node.type)
        ? h(
            'td',
            {
              class: 'ic-orb-list__td ic-orb-list__register',
              'data-register': 'scenario',
              title: SCENARIO_TOOLTIP,
            },
            registerOf(node),
          )
        : cell(registerOf(node), 'ic-orb-list__td ic-orb-list__register'),
      cell(
        Number.isFinite(salience) ? salience.toFixed(2) : 'No reading',
        'ic-orb-list__td ic-orb-list__num',
      ),
      cell(
        formatZ(node.ts_ms) ?? 'Not recorded',
        'ic-orb-list__td ic-orb-list__num',
      ),
    );
    return { tr, open };
  }

  function render() {
    const visible = nodes.filter((node) => {
      if (!predicate) return true;
      try {
        return Boolean(predicate(node));
      } catch {
        return false;
      }
    });
    const sorted = sortRows(visible, sort.key, sort.dir);
    const keys = sorted.map(rowKey);
    const unchanged =
      keys.length === rendered.length &&
      keys.every((key, i) => key === rendered[i].key);
    // Every graph poll calls setGraph: rebuilding the rows would drop the
    // keyboard focus on a row's name button to <body> (review: List view
    // focus lost every 4.5 s). Same rows -> no DOM change at all; changed
    // rows -> rebuild, then put focus back on that entity's row (or on the
    // first column header when the entity has left the picture).
    if (!unchanged) {
      const active = globalThis.document?.activeElement;
      const held = active ? rendered.find((r) => r.open === active) : null;
      rendered = sorted.map((node, i) => ({
        id: node.id,
        key: keys[i],
        ...row(node),
      }));
      replaceKids(
        body,
        rendered.map((r) => r.tr),
      );
      if (held) {
        const next = rendered.find((r) => r.id === held.id);
        (next ? next.open : sortButtons[0])?.focus?.();
      }
    }
    const rows = rendered;
    for (const [key, th] of headerCells) {
      th.setAttribute('aria-sort', key === sort.key ? sort.dir : 'none');
    }
    const message = !nodes.length
      ? 'Nothing in the picture yet.'
      : !rows.length
        ? 'Nothing matches this filter.'
        : '';
    empty.textContent = message;
    setHidden(empty, !message);
    return rows.length;
  }

  const view = {
    element,
    setGraph(graph) {
      graphIn = graph;
      const shown = filterForView(graph, viewMode);
      nodes = Array.isArray(shown?.nodes)
        ? shown.nodes.filter((n) => n && typeof n.id === 'string')
        : [];
      return render();
    },
    /**
     * Blue view (`umpire: false`) or Umpire view (`umpire: true`, the
     * default), as on the orb (WG §5.3.3). Anything but a boolean is ignored.
     * @returns {{umpire: boolean}} the view now in force
     */
    setView({ umpire } = {}) {
      if (typeof umpire === 'boolean' && umpire !== viewMode.umpire) {
        viewMode = { umpire };
        view.setGraph(graphIn);
      }
      return { ...viewMode };
    },
    get view() {
      return { ...viewMode };
    },
    filter(pred) {
      predicate = typeof pred === 'function' ? pred : null;
      return render();
    },
    /** Sort by a column; the same column again reverses the order. */
    sort(key, dir) {
      if (!LIST_COLUMNS.some((column) => column.key === key)) return render();
      const next =
        dir ||
        (sort.key === key
          ? sort.dir === 'ascending'
            ? 'descending'
            : 'ascending'
          : 'ascending');
      sort = { key, dir: next };
      return render();
    },
    get sortState() {
      return { ...sort };
    },
    destroy() {
      element.remove?.();
      nodes = [];
      rendered = [];
    },
  };
  render();
  return view;
}
