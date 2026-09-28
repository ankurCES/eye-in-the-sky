/**
 * Entity chips (UX spec §6.4) and the chip-to-node tether (§4.6, §11.1).
 *
 * A chip is a raised pill: a 10 px glyph in the node's status colour, then the
 * label (the id in mono when there is no label). Chips in one message form a
 * roving tab set (Tab enters the first, ←/→ move). Hover or focus highlights
 * the node in analyst blue and draws the tether; click inspects. An id that
 * is not in the current graph is dashed with an explanatory tooltip.
 */

import { h as domH, setClass } from '../../ui/uavDom.js';
import { glyphSvg } from '../orb/glyphs.js';
import { bidiSafe, nodeTypeOf } from './format.js';
import { feedLabel } from '../orb/text.js';

// Labels are third-party text (OSM names): no bidi control reaches the DOM.
const h = bidiSafe(domH);

export const MISSING_TIP =
  'Not in the current picture (outside this theater or aged out).';

export const NODE_TYPES = new Set([
  'vehicle',
  'mission',
  'track',
  'unit',
  'equipment',
  'report',
  'theater',
  'poi',
  'alarm',
  'feed',
  'site',
]);
const STATUSES = new Set(['ok', 'warn', 'critical', 'stale', 'unknown']);

/** Find a graph node by id in an intelStore snapshot (Map or object index). */
export function lookupNode(store, id) {
  if (!id) return null;
  let snap = null;
  try {
    snap = store?.get?.() ?? null;
  } catch {
    snap = null;
  }
  const byId = snap?.byId;
  if (byId) {
    if (typeof byId.get === 'function') {
      const hit = byId.get(id);
      if (hit) return hit;
    } else if (Object.hasOwn(byId, id)) return byId[id];
  }
  const nodes = snap?.graph?.nodes;
  if (Array.isArray(nodes)) return nodes.find((n) => n?.id === id) ?? null;
  return null;
}

/**
 * A chip's status word. An unrecognised node type ignores its status and is
 * never green (WG spec §4.2.1): it shows as 'unknown', like the orb's lilac
 * "Unrecognised" glyph.
 */
export function chipStatus(type, status) {
  if (!NODE_TYPES.has(type)) return 'unknown';
  return STATUSES.has(status) ? status : 'unknown';
}

/** The glyph markup for a chip: constant SVG from orb/glyphs.js only. */
function chipGlyph(type, status, category = null) {
  // orb/glyphs.js draws any type it doesn't know as the unrecognised glyph.
  const safeType = NODE_TYPES.has(type) ? type : 'unknown';
  const safeStatus = chipStatus(type, status);
  try {
    // `category` picks a site's glyph (A15); other types ignore it.
    const svg = glyphSvg(safeType, {
      status: safeStatus,
      size: 10,
      category: typeof category === 'string' ? category : undefined,
    });
    return typeof svg === 'string' ? svg : '';
  } catch {
    return '';
  }
}

/**
 * Build one chip button.
 * @param {{id:string, label?:string|null}} ref entity reference
 * @param {{store?, onEnter?, onLeave?, onActivate?}} hooks
 */
export function createChip(ref, hooks = {}) {
  const node = lookupNode(hooks.store, ref.id);
  const type = node?.type || nodeTypeOf(ref.id);
  const status = chipStatus(type, node?.status);
  // A feed without a markup label gets the orb's plain name ("Contacts
  // feed"), never the server's internal one.
  const labelText =
    ref.label ||
    (node?.type === 'feed' ? feedLabel(node) : node?.label) ||
    null;
  const glyph = h('span', { class: 'ic-chip__glyph', 'aria-hidden': 'true' });
  const svg = chipGlyph(type, status, node?.attrs?.category);
  if (svg) glyph.innerHTML = svg;
  const text = h(
    'span',
    { class: labelText ? 'ic-chip__label' : 'ic-chip__label ic-mono' },
    labelText || ref.id,
  );
  const chip = h(
    'button',
    {
      type: 'button',
      class: 'ic-chip',
      'data-id': ref.id,
      'data-status': status,
      'data-type': NODE_TYPES.has(type) ? type : 'unrecognised',
      'data-missing': node ? null : 'true',
      title: node ? null : MISSING_TIP,
      tabindex: '-1',
    },
    glyph,
    text,
    node ? null : h('span', { class: 'ic-vh' }, ` (${MISSING_TIP})`),
  );
  const enter = () => hooks.onEnter?.(ref.id, chip);
  const leave = () => hooks.onLeave?.(ref.id, chip);
  chip.addEventListener('mouseenter', enter);
  chip.addEventListener('focus', enter);
  chip.addEventListener('mouseleave', leave);
  chip.addEventListener('blur', leave);
  chip.addEventListener('click', () => hooks.onActivate?.(ref.id, chip));
  return chip;
}

/** Make the chips of one message a roving tab set. */
export function wireRoving(chips) {
  chips.forEach((chip, i) => {
    chip.setAttribute('tabindex', i === 0 ? '0' : '-1');
    if (chip.__icRoving) {
      chip.__icRoving.list = chips;
      return;
    }
    const state = { list: chips };
    chip.__icRoving = state;
    chip.addEventListener('keydown', (event) => {
      const list = state.list;
      const at = list.indexOf(chip);
      let next = null;
      if (event?.key === 'ArrowRight') next = list[(at + 1) % list.length];
      else if (event?.key === 'ArrowLeft')
        next = list[(at - 1 + list.length) % list.length];
      else if (event?.key === 'Home') next = list[0];
      else if (event?.key === 'End') next = list[list.length - 1];
      if (!next) return;
      event.preventDefault?.();
      for (const c of list) c.setAttribute('tabindex', c === next ? '0' : '-1');
      next.focus?.();
    });
  });
}

/**
 * The tether overlay: a full-root canvas with pointer-events:none, drawn only
 * while a chip is hovered or focused, redrawn on orb frames and on scroll.
 * Wide layout only (the caller decides). Feature-checked for node tests.
 */
export function createTether({ root, host, orb, win = globalThis } = {}) {
  let canvas = null;
  let ctx2d = null;
  let active = null;
  let unsubFrame = null;

  function ensureCanvas() {
    if (canvas) return ctx2d;
    if (!root?.append) return null;
    const el = h('canvas', { class: 'ic-tether', 'aria-hidden': 'true' });
    if (typeof el.getContext !== 'function') return null;
    const context = el.getContext('2d');
    if (!context) return null;
    root.append(el);
    canvas = el;
    ctx2d = context;
    return ctx2d;
  }

  function ink(kind) {
    const name = kind === 'operator' ? '--ic-film' : '--ic-analyst';
    let color = '';
    try {
      color =
        win.getComputedStyle?.(root)?.getPropertyValue?.(name)?.trim() ?? '';
    } catch {
      color = '';
    }
    return color || (kind === 'operator' ? '#E6ECEF' : '#8CC8FF');
  }

  function clear() {
    if (!ctx2d || !canvas) return;
    ctx2d.setTransform?.(1, 0, 0, 1, 0, 0);
    ctx2d.clearRect(0, 0, canvas.width, canvas.height);
  }

  function draw() {
    if (!ctx2d || !canvas) return;
    const rootRect = root.getBoundingClientRect?.();
    if (!rootRect) return;
    const dpr = Math.min(win.devicePixelRatio || 1, 2);
    const w = Math.round(rootRect.width * dpr);
    const hgt = Math.round(rootRect.height * dpr);
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== hgt) canvas.height = hgt;
    clear();
    if (!active) return;
    const point = orb?.project?.(active.id);
    const chipRect = active.el.getBoundingClientRect?.();
    const hostRect = host?.getBoundingClientRect?.();
    if (!point || !chipRect || !hostRect) return;
    if (chipRect.bottom < hostRect.top || chipRect.top > hostRect.bottom)
      return;
    const y0 = chipRect.top + chipRect.height / 2 - rootRect.top;
    const x0 = chipRect.left - rootRect.left;
    const x1 = hostRect.left - rootRect.left;
    const x2 = point.x - rootRect.left;
    const y2 = point.y - rootRect.top;
    ctx2d.setTransform?.(dpr, 0, 0, dpr, 0, 0);
    ctx2d.globalAlpha = 0.6;
    ctx2d.strokeStyle = ink(active.kind);
    ctx2d.lineWidth = 1;
    ctx2d.setLineDash?.(point.front === false ? [4, 4] : []);
    ctx2d.beginPath();
    ctx2d.moveTo(x0, y0);
    ctx2d.lineTo(x1, y0);
    ctx2d.lineTo(x2, y2);
    ctx2d.stroke();
    ctx2d.globalAlpha = 1;
  }

  return {
    show(el, id, kind = 'analyst') {
      active = { el, id, kind };
      if (!ensureCanvas()) return;
      if (!unsubFrame && typeof orb?.onFrame === 'function') {
        try {
          unsubFrame = orb.onFrame(draw);
        } catch {
          unsubFrame = null;
        }
      }
      setClass(canvas, 'is-active', true);
      draw();
    },
    hide() {
      active = null;
      clear();
      if (canvas) setClass(canvas, 'is-active', false);
      if (typeof unsubFrame === 'function') unsubFrame();
      unsubFrame = null;
    },
    redraw() {
      if (active) draw();
    },
    get active() {
      return active;
    },
    destroy() {
      this.hide();
      canvas?.remove?.();
      canvas = null;
      ctx2d = null;
    },
  };
}
