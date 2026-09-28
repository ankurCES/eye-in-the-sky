/**
 * Canvas2D renderer for the information orb (UX spec §4.3–4.6, §11.1).
 *
 * Stateless per frame: orb.js hands `draw(ctx, scene)` a complete scene (the
 * projected nodes, their visual state, the edges to draw, captions, marks)
 * and the renderer paints it. Nodes are one `drawImage` each from a sprite
 * atlas keyed by (type × status × phase × confidence × duplicate × size
 * bucket × DPR); halos are pre-rendered, steady and only on criticals.
 * Edges and the graticule are batched into one path per style per
 * hemisphere. Never uses shadowBlur, ctx.filter, letterSpacing or `lighter`.
 *
 * Everything the renderer needs from the platform (canvas creation, Path2D,
 * a clock) is injectable, so node:test can drive it with a recording context.
 */

import { h } from '../../ui/uavDom.js';
import {
  COLORS,
  GLYPH_NOMINAL_RADIUS,
  GLYPH_SLASH,
  glyphFor,
  glyphStyle,
  parsePath,
  tracePath,
} from './glyphs.js';
import { SITES_DEGRADED_TEXT, siteBandCaption } from './placeText.js';
import {
  BANDS,
  BAND_ORDER,
  EDGE_POINTS,
  EDGE_STRIDE,
  GRATICULE_PARALLELS,
  SECTORS,
  toVector,
} from './layout.js';

const TAU = Math.PI * 2;

/** Ring flags on `node.ring` (who is pointing, spec §4.3). */
export const RING = Object.freeze({
  HOVER: 1,
  SELECT: 2,
  MATCH: 4,
  HIGHLIGHT_ANALYST: 8,
  HIGHLIGHT_OPERATOR: 16,
  FOCUS_ANALYST: 32,
  FOCUS_OPERATOR: 64,
});

/** Label priority tiers (spec §4.5). Lower wins. */
export const TIER = Object.freeze({
  SELECTED: 1,
  ANALYST: 2,
  HOVERED: 3,
  SEARCH: 4,
  VEHICLE: 5,
  CRITICAL: 6,
  OTHER: 7,
});

export const SIZE_BUCKETS = Object.freeze([6, 9, 13, 18]);
export const LABEL_MIN_Z = 0.2;
export const LABEL_SPACING_PX = 20;
/** Two-line margin labels (label + subtitle) need a taller slot. */
export const TWO_LINE_SPACING_PX = 36;
export const MARGIN_LABEL_MAX = 12;
const MARGIN_MIN_WIDTH = 48;
export const EDGE_CAP = 200;
export const LABEL_FONT =
  '500 12px "Atkinson Hyperlegible Next", "Atkinson Hyperlegible", "IC Sans Fallback", ' +
  '-apple-system, "SF Pro Text", "Segoe UI", system-ui, sans-serif';

/** Context calls that paint pixels (the frame budget counts these). */
export const DRAW_CALLS = Object.freeze([
  'drawImage',
  'fill',
  'stroke',
  'fillText',
  'strokeText',
  'fillRect',
  'clearRect',
]);

/** Paint calls one frame may issue for n nodes (one sprite each + fixed chrome). */
export function frameDrawBudget(n) {
  return n + 160;
}

const neighbour = (color, dash = null, width = 1) =>
  Object.freeze({ color, width, dash, reveal: 'neighbour' });
const selectedOnly = Object.freeze({
  color: COLORS.hairline,
  width: 1,
  dash: null,
  reveal: 'selected',
});

/** Edge styles by kind (spec §4.4). `about` takes its alarm's severity colour. */
export const EDGE_STYLES = Object.freeze({
  flying: Object.freeze({
    color: COLORS.magenta,
    width: 1.5,
    dash: null,
    reveal: 'always',
  }),
  tracking: Object.freeze({
    color: COLORS.magenta,
    width: 1,
    dash: [4, 3],
    reveal: 'always',
  }),
  observes: neighbour(COLORS.film35),
  target: neighbour(COLORS.film35),
  member_of: neighbour(COLORS.hairline, [1, 3]),
  is_a: neighbour(COLORS.hairline, [1, 3]),
  reports_on: neighbour(COLORS.film35, [1, 3]),
  about: neighbour(null, [3, 3]),
  near: selectedOnly,
  in_theater: selectedOnly,
  operating_in: selectedOnly,
});

export function edgeStyle(kind) {
  return Object.hasOwn(EDGE_STYLES, kind) ? EDGE_STYLES[kind] : selectedOnly;
}

/**
 * Which edges to draw this frame, most important first, capped at 200.
 * @param {object} layout computeLayout() result
 * @param {{selected?:number, hovered?:number, focus?:Iterable<number>,
 *   colorOf?:(i:number)=>string, cap?:number}} state
 * @returns {Array<{e:number, kind:string, color:string, width:number, dash:number[]|null}>}
 */
export function selectEdges(
  layout,
  { selected = -1, hovered = -1, focus = [], colorOf, cap = EDGE_CAP } = {},
) {
  const active = new Set(focus);
  if (hovered >= 0) active.add(hovered);
  if (selected >= 0) active.add(selected);
  const picked = [];
  layout.edges.forEach((edge, e) => {
    const style = edgeStyle(edge.kind);
    let rank = -1;
    if (style.reveal === 'always') rank = 0;
    else if (
      style.reveal === 'neighbour' &&
      (active.has(edge.ai) || active.has(edge.bi))
    ) {
      rank = edge.ai === selected || edge.bi === selected ? 1 : 2;
    } else if (
      style.reveal === 'selected' &&
      (edge.ai === selected || edge.bi === selected)
    ) {
      rank = 1;
    }
    if (rank < 0) return;
    let color = style.color;
    if (!color) {
      const alarmEnd = layout.band[edge.ai] === 'alarm' ? edge.ai : edge.bi;
      color = colorOf?.(alarmEnd) || COLORS.pencil;
    }
    picked.push({
      e,
      rank,
      kind: edge.kind,
      color,
      width: style.width,
      dash: style.dash,
    });
  });
  picked.sort((a, b) => a.rank - b.rank || a.e - b.e);
  return picked.slice(0, cap);
}

/**
 * Among equally ranked nodes (spec §4.5 step 7, "most salient front nodes"),
 * the picture's own entities come before alarms (they have the rail) and
 * equipment classes (reference data that repeats its contacts' names).
 */
const LABEL_GROUP = Object.freeze({ alarm: 1, equipment: 2 });
const labelGroup = (vm) =>
  vm.tier > TIER.SEARCH ? (LABEL_GROUP[vm.type] ?? 0) : 0;

/**
 * Pick labels by the spec's priority order. Only nodes with z > 0.2 qualify;
 * with 12 or fewer nodes every qualifying node is labelled. In a bigger
 * picture an equipment class is labelled only when something points at it
 * (selected, analyst, hovered or a search hit).
 * @param {{n:number, proj:Float32Array, nodes:object[], budget:number}} scene
 * @returns {number[]} node indices, highest priority first
 */
export function chooseLabels({
  n,
  proj,
  nodes,
  budget,
  labelBudget,
  minZ = LABEL_MIN_Z,
}) {
  const candidates = [];
  for (let i = 0; i < n; i += 1) {
    const vm = nodes[i];
    if (!vm || !(proj[i * 4 + 2] > minZ)) continue;
    if (vm.tier > TIER.SEARCH && vm.alpha < 0.5) continue;
    if (n > 12 && labelGroup(vm) === 2) continue;
    candidates.push(i);
  }
  candidates.sort(
    (a, b) =>
      nodes[a].tier - nodes[b].tier ||
      labelGroup(nodes[a]) - labelGroup(nodes[b]) ||
      nodes[b].salience - nodes[a].salience ||
      a - b,
  );
  const limit =
    n <= 12 ? candidates.length : Math.max(0, (budget ?? labelBudget) | 0);
  const chosen = candidates.slice(0, limit);
  // Vehicles are always labelled (spec §4.3), even past the budget.
  for (const i of candidates.slice(limit))
    if (nodes[i].type === 'vehicle') chosen.push(i);
  return chosen;
}

/** Leaders finish their sweep this far outside the limb (the bend column). */
export const LEADER_BEND_PX = 4;
/** The horizontal shoulder into the label ends this far outside the limb. */
export const LEADER_END_PX = 12;
/** Margin label text starts (right) or ends (left) this far outside the limb. */
export const LABEL_INSET_PX = 16;
/** Leaders start easing around a glyph or caption this far before it. */
export const LEADER_APPROACH_PX = 10;
/** Minimum vertical gap between two leaders, and between a leader and a glyph. */
export const LEADER_GAP_PX = 3;
const LABEL_HALF = 8;
const CAPTION_HALF = 9;
const SUBTITLE_EXTRA = 16;
/** The "not live" watermark's line, below the centre by this share of R. */
const WATERMARK_AT_R = 0.62;
/** Back glyphs this close to the limb (view z above it) block captions. */
const LIMB_BACK_Z = -0.35;
/** Room kept under a gutter's last label for the "N more match" line. */
export const MORE_LINE_EXTRA_PX = 22;

/**
 * One gutter's label positions (1-D): labels keep their nodes' order, sit
 * `gap` apart, stay inside [top, bottom], and each cluster of labels centres
 * on its nodes (least total displacement), then steps around fixed obstacles
 * (band captions that share the left gutter). Every label box ends clear of
 * every caption row; when the column is too short for that, labels are
 * packed from the top and the ones that fall off get `overflow: true`.
 * @param {Array<{y:number}>} list items sorted by desired y; `ty` and
 *   `overflow` are written
 * @param {{top:number, bottom:number, gap:(k:number)=>number,
 *   extra:(k:number)=>number, obstacles?:number[]}} options
 */
export function placeColumn(list, { top, bottom, gap, extra, obstacles = [] }) {
  const n = list.length;
  if (!n) return list;
  for (const item of list) item.overflow = false;
  const blocks = [];
  const clampPos = (block) => {
    const low = bottom - block.span - extra(block.to);
    block.pos = Math.max(top, Math.min(block.sum / block.count, low));
  };
  for (let k = 0; k < n; k += 1) {
    const block = {
      from: k,
      to: k,
      offs: [0],
      span: 0,
      sum: list[k].y,
      count: 1,
      pos: 0,
    };
    clampPos(block);
    blocks.push(block);
    while (blocks.length > 1) {
      const b = blocks[blocks.length - 1];
      const a = blocks[blocks.length - 2];
      const shift = a.span + gap(a.to);
      if (a.pos + shift <= b.pos) break;
      for (const o of b.offs) a.offs.push(o + shift);
      a.sum += b.sum - shift * b.count;
      a.count += b.count;
      a.span = shift + b.span;
      a.to = b.to;
      clampPos(a);
      blocks.pop();
    }
  }
  for (const block of blocks) {
    block.offs.forEach((o, m) => {
      list[block.from + m].ty = block.pos + o;
    });
  }
  // Step around obstacles: each is a caption row centred on its y, 18 px
  // tall. `clear` moves a label's line off every row its box
  // [t - 8, t + 8 + extra] overlaps, always in one direction, so it passes
  // each row at most once and ends clear of all of them (review: the old
  // final "keep order" pass pushed labels back into captions).
  const clear = (k, t, dir) => {
    for (let guard = 0; guard <= obstacles.length; guard += 1) {
      const oy = obstacles.find(
        (o) =>
          t - LABEL_HALF < o + CAPTION_HALF &&
          t + LABEL_HALF + extra(k) > o - CAPTION_HALF,
      );
      if (oy == null) break;
      t =
        dir > 0
          ? oy + CAPTION_HALF + LABEL_HALF + 1
          : oy - CAPTION_HALF - LABEL_HALF - extra(k) - 1;
    }
    return t;
  };
  // Down the column: each label as near its slot as the one above allows,
  // stepping over a caption whichever way is nearer its node.
  for (let k = 0; k < n; k += 1) {
    const floor = k ? list[k - 1].ty + gap(k - 1) : top;
    const t = Math.max(list[k].ty, floor);
    const up = clear(k, t, -1);
    const down = clear(k, t, 1);
    list[k].ty =
      up >= floor && Math.abs(up - list[k].y) <= Math.abs(down - list[k].y)
        ? up
        : down;
  }
  // Up the column: anything past the bottom (or the label below it) moves
  // up, stepping over captions upward. Order, gaps and clearance now hold
  // everywhere unless the first label went above the top.
  for (let k = n - 1; k >= 0; k -= 1) {
    const ceiling = k === n - 1 ? bottom - extra(k) : list[k + 1].ty - gap(k);
    if (list[k].ty > ceiling) list[k].ty = clear(k, ceiling, -1);
  }
  if (list[0].ty >= top - 1e-6) return list;
  // Not enough room: pack from the top (the tightest packing there is) and
  // mark what still falls off the bottom; the caller drops those labels.
  let floor = top;
  for (let k = 0; k < n; k += 1) {
    const t = clear(k, floor, 1);
    list[k].ty = t;
    if (t + extra(k) > bottom + 1e-6) list[k].overflow = true;
    floor = t + gap(k);
  }
  return list;
}

/**
 * Non-crossing leaders for one gutter. `list` is sorted by node y and its
 * labels (`ty`) are in the same order. A sweep runs from the centre toward
 * the gutter. Each labelled glyph, and each obstacle (a band caption), is a
 * keep-out box: at every sweep breakpoint inside a box the leaders already
 * running are put back in label order around it (earlier labels pass above a
 * glyph, later ones below; labels above a caption pass above it), at least
 * LEADER_GAP_PX apart. Between breakpoints each leader heads straight for its
 * label, and two sorted breakpoints joined by straight segments can never
 * cross. The last breakpoint is the bend column; a short horizontal shoulder
 * leads into the label.
 * @param {Array<{i:number, x:number, y:number, ty:number}>} list
 * @param {'left'|'right'} side
 * @param {{cx:number, R:number, radii?:Float32Array|null,
 *   obstacles?:Array<{s0:number, s1:number, y:number, half:number}>}} options
 *   obstacles in sweep coordinates (distance from the centre toward the gutter)
 * @returns {number[][]} flat [x0, y0, x1, y1, …] polylines, one per item
 */
export function routeLeaders(
  list,
  side,
  { cx, R, radii = null, obstacles = [] },
) {
  const dir = side === 'right' ? 1 : -1;
  const bendS = R + LEADER_BEND_PX;
  const endX = cx + dir * (R + LEADER_END_PX);
  const toX = (s) => cx + dir * s;
  const boxes = list.map((item, k) => {
    const s = dir * (item.x - cx);
    const r = radii ? radii[item.i] || 0 : 0;
    return {
      s0: s - r - 2 - LEADER_APPROACH_PX,
      s1: s + r + 2,
      y: item.y,
      clear: r + LEADER_GAP_PX,
      // Label order decides: earlier labels pass above this glyph.
      above: (j) => j < k,
      owner: k,
    };
  });
  for (const o of obstacles) {
    if (!(o.s0 < bendS)) continue;
    boxes.push({
      s0: o.s0 - 2 - LEADER_APPROACH_PX,
      s1: o.s1 + 2,
      y: o.y,
      clear: o.half + LEADER_GAP_PX,
      // Leaders keep the side they are already on (a prefix: they're sorted).
      above: (_j, y) => y < o.y,
      owner: -1,
    });
  }
  const events = [];
  for (const box of boxes) {
    events.push({ s: box.s0, start: -1 });
    events.push({ s: box.s1, start: box.owner });
  }
  events.sort((a, b) => a.s - b.s || (a.start >= 0) - (b.start >= 0));
  // Push the running leaders out of one box, keeping them sorted: the ones
  // passing above it (a prefix in label order) chain upward from its top.
  const keepOut = (present, ys, box) => {
    let split = 0;
    while (split < present.length && box.above(present[split], ys[split]))
      split += 1;
    let limit = box.y - box.clear;
    for (let m = split - 1; m >= 0; m -= 1) {
      ys[m] = Math.min(ys[m], limit);
      limit = ys[m] - LEADER_GAP_PX;
    }
    limit = box.y + box.clear;
    for (let m = split; m < present.length; m += 1) {
      ys[m] = Math.max(ys[m], limit);
      limit = ys[m] + LEADER_GAP_PX;
    }
  };
  const paths = list.map(() => []);
  const cur = new Map();
  let lastS = -Infinity;
  for (const event of events) {
    const starting = event.start;
    if (starting < 0 && event.s >= bendS) continue;
    const s = Math.min(bendS - 0.5, Math.max(event.s, lastS + 0.25));
    lastS = s;
    const present = [...cur.keys()].sort((a, b) => a - b);
    const ys = present.map((j) => {
      const p = cur.get(j);
      const span = bendS - p.s;
      const f = span > 1e-6 ? Math.min(1, Math.max(0, (s - p.s) / span)) : 1;
      return p.y + (list[j].ty - p.y) * f;
    });
    for (const box of boxes) {
      if (box.owner >= 0 && box.owner === starting) continue;
      if (event.s < box.s0 - 1e-6 || event.s > box.s1 + 1e-6) continue;
      keepOut(present, ys, box);
    }
    // A starting leader is pinned to its node; its glyph box goes last.
    if (starting >= 0) keepOut(present, ys, boxes[starting]);
    present.forEach((j, m) => {
      cur.set(j, { s, y: ys[m] });
      paths[j].push(toX(s), ys[m]);
    });
    if (starting >= 0) {
      cur.set(starting, { s, y: list[starting].y });
      paths[starting].push(toX(s), list[starting].y);
    }
  }
  list.forEach((item, k) => {
    paths[k].push(toX(bendS), item.ty, endX, item.ty);
  });
  return paths.map(simplifyPolyline);
}

/**
 * Drop interior points that sit on the straight line through their
 * neighbours. The bend and the shoulder (the last two points) always stay.
 */
function simplifyPolyline(pts) {
  if (pts.length <= 6) return pts;
  const out = [pts[0], pts[1]];
  const last = pts.length - 4;
  for (let k = 2; k < last; k += 2) {
    const ax = out[out.length - 2];
    const ay = out[out.length - 1];
    const bx = pts[k];
    const by = pts[k + 1];
    const cx = pts[k + 2];
    const cy = pts[k + 3];
    const cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
    const len = Math.hypot(cx - ax, cy - ay) || 1;
    if (Math.abs(cross) / len > 0.25) out.push(bx, by);
  }
  out.push(pts[last], pts[last + 1], pts[last + 2], pts[last + 3]);
  return out;
}

/**
 * Margin label slots for the wide layout (standard non-crossing callouts):
 * a label goes to the gutter on its node's side of the centre; each gutter
 * lists its labels in their nodes' projected-y order, at least 20 px apart
 * (36 px with a subtitle line); and leaders are routed so their order matches
 * the label order, so no two leaders cross. No label box overlaps a caption
 * row in its gutter; a gutter too short for all its labels drops the lowest
 * priority ones (`indices` is in priority order). With `moreRow`, each
 * gutter keeps MORE_LINE_EXTRA_PX free under its last label for the
 * "N more match" line.
 * @returns {Array<{i:number, side:'left'|'right', x:number, y:number,
 *   width:number, leader:number[]}>}
 */
export function layoutMarginLabels(
  indices,
  proj,
  {
    cx,
    R,
    width,
    height,
    radii = null,
    twoLine = null,
    blocked = [],
    captions = [],
    moreRow = false,
  },
) {
  const priority = new Map(indices.map((i, k) => [i, k]));
  const sides = { left: [], right: [] };
  for (const i of indices) {
    const x = proj[i * 4];
    const y = proj[i * 4 + 1];
    sides[x < cx ? 'left' : 'right'].push({ i, x, y, ty: y });
  }
  const tall = (item) => Boolean(twoLine?.(item.i));
  const items = [];
  for (const side of ['left', 'right']) {
    const dir = side === 'right' ? 1 : -1;
    const anchorX = cx + dir * (R + LABEL_INSET_PX);
    const gutter = side === 'right' ? width - anchorX - 4 : anchorX - 4;
    // Caption rows in sweep coordinates; the ones reaching into the label
    // column (text, shoulder and bend) are also label-placement obstacles.
    const rows = [];
    const rowYs = side === 'left' ? [...blocked] : [];
    for (const row of captions || []) {
      const s0 = dir > 0 ? row.left - cx : cx - row.right;
      const s1 = dir > 0 ? row.right - cx : cx - row.left;
      if (!(s1 > 0)) continue;
      rows.push({ s0, s1, y: row.y, half: 9 });
      if (s1 > R + LEADER_BEND_PX && s0 < R + LABEL_INSET_PX + 120)
        rowYs.push(row.y);
    }
    // Top to bottom; on a tie the node farther from the gutter labels first.
    let list = sides[side].sort(
      (a, b) => a.y - b.y || dir * (a.x - b.x) || a.i - b.i,
    );
    rowYs.sort((a, b) => a - b);
    for (;;) {
      const last = list.length - 1;
      placeColumn(list, {
        top: 10,
        bottom: height - 10,
        gap: (k) => (tall(list[k]) ? TWO_LINE_SPACING_PX : LABEL_SPACING_PX),
        extra: (k) =>
          (tall(list[k]) ? SUBTITLE_EXTRA : 0) +
          (moreRow && k === last ? MORE_LINE_EXTRA_PX : 0),
        obstacles: rowYs,
      });
      if (!list.some((item) => item.overflow)) break;
      // Too many for this gutter: the lowest-priority label goes.
      const drop = list.reduce((a, b) =>
        priority.get(b.i) > priority.get(a.i) ? b : a,
      );
      list = list.filter((item) => item !== drop);
    }
    const leaders = routeLeaders(list, side, {
      cx,
      R,
      radii,
      obstacles: rows,
    });
    list.forEach((item, k) => {
      items.push({
        i: item.i,
        side,
        x: anchorX,
        y: item.ty,
        width: Math.max(0, gutter),
        leader: leaders[k],
      });
    });
  }
  return items;
}

/**
 * Where band captions go: each band's leftmost visible point gives its y; the
 * text hugs the left limb, outside it in the margin layout and inside it in
 * the inline layout (where polar bands, whose chord is too short, are skipped).
 * A caption never sits on a glyph (`avoid`: [x, y, w, h] boxes of the drawn
 * front glyphs): it moves up or down by its own height, and is left out when
 * both are taken (review: "Alarms 17" on the BINGO triangle, "Contacts 16"
 * under a disc). Two captions closer than 16 px keep only the upper one.
 */
export function placeCaptions(
  captions,
  camera,
  { cx, cy, R, mode, measure, avoid = [] },
) {
  const probe = [0, 0, 0];
  const out = [0, 0, 0, 0];
  const found = [];
  for (const caption of captions || []) {
    let best = null;
    for (let k = 0; k < 36; k += 1) {
      toVector(caption.lat, -180 + k * 10, probe);
      camera.projectXYZ(probe[0], probe[1], probe[2], out, 0);
      if (out[2] < -0.05) continue;
      if (!best || out[0] < best[0]) best = [out[0], out[1]];
    }
    if (best) found.push({ caption, y: best[1] });
  }
  // A warning caption (a feed down: contacts or map data "may be missing,
  // not absent") claims its row first; the rest go top to bottom.
  found.sort(
    (a, b) => (a.caption.ink ? 0 : 1) - (b.caption.ink ? 0 : 1) || a.y - b.y,
  );
  const rowAt = (caption, y, w) => {
    const dy = y - cy;
    const chord = Math.sqrt(Math.max(0, R * R - dy * dy));
    const limbX = cx - chord;
    // Outside the limb in the margin layout, unless the gutter is too narrow.
    if (mode === 'margin' && limbX - 8 - w >= 4) {
      return {
        ...caption,
        y,
        x: limbX - 8,
        align: 'right',
        left: limbX - 8 - w,
        right: limbX - 8,
      };
    }
    if (Math.abs(dy) < R * 0.8) {
      return {
        ...caption,
        y,
        x: limbX + 6,
        align: 'left',
        left: limbX + 6,
        right: limbX + 6 + w,
      };
    }
    return null;
  };
  const onGlyph = (row) =>
    avoid.some(
      (b) =>
        b[0] < row.right + 2 &&
        row.left - 2 < b[0] + b[2] &&
        b[1] < row.y + 9 &&
        row.y - 9 < b[1] + b[3],
    );
  const kept = [];
  const crowded = (y) => kept.some((row) => Math.abs(row.y - y) < 16);
  for (const { caption, y } of found) {
    const w = measure(caption.text);
    const home = rowAt(caption, y, w);
    if (!home || crowded(y)) continue;
    let pick = onGlyph(home) ? null : home;
    for (const dy of pick ? [] : [-18, 18]) {
      const row = rowAt(caption, y + dy, w);
      if (row && !crowded(row.y) && !onGlyph(row)) {
        pick = row;
        break;
      }
    }
    if (pick) kept.push(pick);
  }
  kept.sort((a, b) => a.y - b.y);
  return kept;
}

function defaultCanvas(width, height) {
  try {
    if (globalThis.document?.createElement) {
      const canvas = h('canvas');
      if (typeof canvas?.getContext !== 'function') return null;
      canvas.width = width;
      canvas.height = height;
      return canvas;
    }
    if (typeof globalThis.OffscreenCanvas === 'function') {
      return new globalThis.OffscreenCanvas(width, height);
    }
  } catch {
    // Fall through: nodes are then painted directly, without sprites.
  }
  return null;
}

export function bucketFor(r) {
  for (const b of SIZE_BUCKETS) if (r <= b) return b;
  return SIZE_BUCKETS[SIZE_BUCKETS.length - 1];
}

/** Static graticule geometry: band-boundary parallels and sector meridians. */
function buildGraticule() {
  const parallel = (lat) => {
    const pts = new Float32Array(73 * 3);
    for (let k = 0; k <= 72; k += 1) toVector(lat, -180 + k * 5, pts, k * 3);
    return pts;
  };
  const meridians = SECTORS.map((sector) => {
    const pts = new Float32Array(13 * 3);
    const top = GRATICULE_PARALLELS.beltTop;
    const bottom = GRATICULE_PARALLELS.beltBottom;
    for (let k = 0; k <= 12; k += 1) {
      toVector(top + ((bottom - top) * k) / 12, sector.lonStart, pts, k * 3);
    }
    return pts;
  });
  return {
    belt: [
      parallel(GRATICULE_PARALLELS.beltTop),
      parallel(GRATICULE_PARALLELS.beltBottom),
      ...meridians,
    ],
    cap: [parallel(GRATICULE_PARALLELS.alarmCap)],
    empty: [parallel(GRATICULE_PARALLELS.emptyRow)],
  };
}

/**
 * @param {{createCanvas?:Function, Path2D?:Function|null, now?:()=>number}} [env]
 */
export function createRenderer(env = {}) {
  const createCanvas = env.createCanvas ?? defaultCanvas;
  const Path2DImpl = 'Path2D' in env ? env.Path2D : globalThis.Path2D;
  const now = env.now ?? (() => globalThis.performance?.now?.() ?? Date.now());
  const sprites = new Map();
  const glyphPaths = new Map();
  const widths = new Map();
  const graticule = buildGraticule();
  const tmp = new Float32Array(EDGE_POINTS * 4);
  let spriteDpr = 0;
  let body = { key: '', gradient: null, ctx: null };

  const glyphPath = (d) => {
    let path = glyphPaths.get(d);
    if (path === undefined) {
      path =
        typeof Path2DImpl === 'function' ? new Path2DImpl(d) : parsePath(d);
      glyphPaths.set(d, path);
    }
    return path;
  };
  const paintPath = (g, d, op) => {
    const path = glyphPath(d);
    if (Array.isArray(path)) {
      g.beginPath();
      tracePath(g, path);
      g[op]();
    } else {
      g[op](path);
    }
  };

  /** Paint one node glyph directly (sprite construction, or no-sprite fallback). */
  function paintNode(g, spec, x, y, r) {
    const style = glyphStyle(spec.type, spec.status, { phase: spec.phase });
    const d = glyphFor(spec.type, { category: spec.category }).path;
    const k = r / GLYPH_NOMINAL_RADIUS;
    const drawGlyph = (ox, oy, alpha) => {
      g.save();
      g.translate(ox - 12 * k, oy - 12 * k);
      g.scale(k, k);
      g.lineWidth = Math.max(2, 1.1 / k);
      g.lineJoin = 'round';
      if (style.fill) {
        g.globalAlpha = alpha * style.fillAlpha;
        g.fillStyle = style.fill;
        paintPath(g, d, 'fill');
        g.globalAlpha = alpha;
      }
      if (style.stroke) {
        g.strokeStyle = style.stroke;
        g.setLineDash(style.dash || []);
        paintPath(g, d, 'stroke');
        g.setLineDash([]);
      }
      if (style.slash) {
        g.strokeStyle = COLORS.critical;
        g.lineCap = 'round';
        paintPath(g, GLYPH_SLASH, 'stroke');
      }
      g.restore();
    };
    if (spec.dup) drawGlyph(x + 2.5, y - 2.5, 0.5);
    drawGlyph(x, y, 1);
    if (style.outerRing) {
      g.beginPath();
      g.arc(x, y, r * 1.3, 0, TAU);
      g.lineWidth = 1.2;
      g.strokeStyle = style.outerRing;
      g.stroke();
    }
    if (spec.type === 'track' && spec.conf) {
      const ring = r + 2.5;
      g.beginPath();
      if (spec.conf === 'probable')
        g.arc(x, y, ring, -Math.PI / 2 + 0.4, (3 * Math.PI) / 2 - 0.4);
      else g.arc(x, y, ring, 0, TAU);
      g.lineWidth = 1.2;
      g.strokeStyle = style.stroke || style.fill || style.color;
      g.setLineDash(spec.conf === 'possible' ? [1.2, 2.2] : []);
      g.stroke();
      g.setLineDash([]);
    }
  }

  function sprite(spec, key, bucket, dpr) {
    const cacheKey = `${key}|${bucket}`;
    let entry = sprites.get(cacheKey);
    if (entry !== undefined) return entry;
    const half = Math.ceil(bucket * 1.6 + 4);
    const size = Math.ceil(half * 2 * dpr);
    const canvas = createCanvas(size, size);
    const g = canvas?.getContext?.('2d');
    entry = null;
    if (g) {
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      paintNode(g, spec, half, half, bucket);
      entry = { canvas, half };
    }
    sprites.set(cacheKey, entry);
    return entry;
  }

  function haloSprite(bucket, dpr) {
    const cacheKey = `halo|${bucket}`;
    let entry = sprites.get(cacheKey);
    if (entry !== undefined) return entry;
    const half = Math.ceil(bucket * 2.2 + 1);
    const size = Math.ceil(half * 2 * dpr);
    const canvas = createCanvas(size, size);
    const g = canvas?.getContext?.('2d');
    entry = null;
    if (g) {
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      const grad = g.createRadialGradient(
        half,
        half,
        0,
        half,
        half,
        bucket * 2.2,
      );
      grad.addColorStop(0, 'rgba(255,123,123,0.25)');
      grad.addColorStop(0.45, 'rgba(255,123,123,0.25)');
      grad.addColorStop(1, 'rgba(255,123,123,0)');
      g.fillStyle = grad;
      g.beginPath();
      g.arc(half, half, bucket * 2.2, 0, TAU);
      g.fill();
      entry = { canvas, half };
    }
    sprites.set(cacheKey, entry);
    return entry;
  }

  const measure = (ctx, text) => {
    let w = widths.get(text);
    if (w === undefined) {
      w = Number(ctx.measureText?.(text)?.width) || text.length * 6.5;
      widths.set(text, w);
    }
    return w;
  };

  /** Project a packed polyline and append its front/back runs to a bucket. */
  function runsOf(camera, pts, count, bucket) {
    for (let k = 0; k < count; k += 1) {
      camera.projectXYZ(pts[k * 3], pts[k * 3 + 1], pts[k * 3 + 2], tmp, k * 4);
    }
    let run = null;
    let side = null;
    for (let k = 0; k < count - 1; k += 1) {
      const front = (tmp[k * 4 + 2] + tmp[k * 4 + 6]) / 2 >= 0;
      const target = front ? bucket.front : bucket.back;
      if (target !== side || !run) {
        run = [tmp[k * 4], tmp[k * 4 + 1]];
        target.push(run);
        side = target;
      }
      run.push(tmp[k * 4 + 4], tmp[k * 4 + 5]);
    }
  }

  function strokeRuns(ctx, runs, color, width, dash, alpha) {
    if (!runs.length) return;
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    for (const run of runs) {
      ctx.moveTo(run[0], run[1]);
      for (let k = 2; k < run.length; k += 2) ctx.lineTo(run[k], run[k + 1]);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawBody(ctx, cx, cy, R) {
    const key = `${cx}|${cy}|${R}`;
    if (body.key !== key || body.ctx !== ctx) {
      const gradient = ctx.createRadialGradient(
        cx - R * 0.35,
        cy - R * 0.4,
        0,
        cx,
        cy,
        R,
      );
      gradient.addColorStop(0, COLORS.bodyLight);
      gradient.addColorStop(1, COLORS.slate);
      body = { key, gradient, ctx };
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = body.gradient;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, TAU);
    ctx.fill();
    ctx.strokeStyle = COLORS.limb;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function drawFuelRing(ctx, vm, x, y, r, alpha) {
    const rr = r * 1.45 + 1;
    ctx.globalAlpha = alpha;
    if (!Number.isFinite(vm.fuel)) {
      ctx.beginPath();
      ctx.arc(x, y, rr, 0, TAU);
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = COLORS.pencil;
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.setLineDash([]);
      return;
    }
    const fraction = Math.max(0, Math.min(1, vm.fuel / 100));
    ctx.beginPath();
    ctx.arc(x, y, rr, -Math.PI / 2, -Math.PI / 2 + TAU * fraction);
    ctx.strokeStyle = vm.color;
    ctx.lineWidth = 2;
    ctx.stroke();
    if (Number.isFinite(vm.bingo)) {
      const a = -Math.PI / 2 + TAU * Math.max(0, Math.min(1, vm.bingo / 100));
      ctx.beginPath();
      ctx.moveTo(x + Math.cos(a) * (rr - 3), y + Math.sin(a) * (rr - 3));
      ctx.lineTo(x + Math.cos(a) * (rr + 3), y + Math.sin(a) * (rr + 3));
      ctx.strokeStyle = COLORS.film;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  }

  function drawText(ctx, text, x, y, align, ink) {
    ctx.textAlign = align;
    ctx.strokeText(text, x, y);
    ctx.fillStyle = ink;
    ctx.fillText(text, x, y);
  }

  function prepText(ctx) {
    ctx.globalAlpha = 1;
    ctx.font = LABEL_FONT;
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 3;
    ctx.strokeStyle = COLORS.slate;
  }

  /**
   * Paint one frame.
   * @param {CanvasRenderingContext2D} ctx
   * @param {object} scene see orb.js `buildScene()`
   * @returns {{ms:number, labels:object, drawn:number}}
   */
  function draw(ctx, scene) {
    const started = now();
    const { width, height, dpr, cx, cy, R, camera, n, proj, order, nodes } =
      scene;
    if (dpr !== spriteDpr) {
      sprites.clear();
      spriteDpr = dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, width, height);
    drawBody(ctx, cx, cy, R);

    // Graticule and edges, bucketed by style and hemisphere.
    const grat = { front: [], back: [] };
    const gratBelt = { front: [], back: [] };
    const gratEmpty = { front: [], back: [] };
    for (const pts of graticule.cap) runsOf(camera, pts, pts.length / 3, grat);
    for (const pts of graticule.belt) {
      runsOf(camera, pts, pts.length / 3, scene.beltDashed ? gratBelt : grat);
    }
    if (scene.beltEmpty) {
      for (const pts of graticule.empty)
        runsOf(camera, pts, pts.length / 3, gratEmpty);
    }
    const edgeBuckets = new Map();
    for (const edge of scene.edges || []) {
      const key = `${edge.color}|${edge.width}|${edge.dash ? edge.dash.join(',') : ''}`;
      let bucket = edgeBuckets.get(key);
      if (!bucket) {
        bucket = {
          front: [],
          back: [],
          color: edge.color,
          width: edge.width,
          dash: edge.dash,
        };
        edgeBuckets.set(key, bucket);
      }
      runsOf(
        camera,
        scene.edgePts.subarray(
          edge.e * EDGE_STRIDE,
          (edge.e + 1) * EDGE_STRIDE,
        ),
        EDGE_POINTS,
        bucket,
      );
    }
    const drawLines = (side, alpha) => {
      strokeRuns(ctx, grat[side], COLORS.graticule, 1, null, alpha);
      strokeRuns(ctx, gratBelt[side], COLORS.graticule, 1, [4, 4], alpha);
      strokeRuns(ctx, gratEmpty[side], COLORS.pencil, 1.5, [1, 5], alpha * 0.8);
      for (const bucket of edgeBuckets.values()) {
        strokeRuns(
          ctx,
          bucket[side],
          bucket.color,
          bucket.width,
          bucket.dash,
          alpha,
        );
      }
    };
    drawLines('back', 0.35);

    // Nodes, back to front.
    const pointsBack = scene.pointsBack;
    const backHalos = !scene.dropBackHalos;
    const points = new Map();
    const ticks = [];
    const vehicles = [];
    let drawn = 0;
    let frontStart = n;
    for (let k = 0; k < n; k += 1) {
      const i = order[k];
      const z = proj[i * 4 + 2];
      if (z >= 0 && frontStart === n) {
        frontStart = k;
        // Everything behind is painted; lay the front lines over it.
        flushPoints();
        drawLines('front', 1);
      }
      const vm = nodes[i];
      if (!vm) continue;
      const back = z < 0;
      const alpha = vm.alpha * (back ? 0.3 : 1);
      if (alpha <= 0.01) continue;
      const x = proj[i * 4];
      const y = proj[i * 4 + 1];
      const r = vm.r * proj[i * 4 + 3] * (back ? 0.8 : 1);
      if (back && pointsBack) {
        let list = points.get(vm.color);
        if (!list) points.set(vm.color, (list = []));
        list.push(x, y);
        continue;
      }
      if (scene.equipmentTicks && vm.type === 'equipment') {
        ticks.push(x, y, alpha);
        continue;
      }
      if (vm.critical && (!back || backHalos)) {
        const bucket = bucketFor(r);
        const halo = haloSprite(bucket, dpr);
        if (halo) {
          const s = r / bucket;
          ctx.globalAlpha = alpha;
          ctx.drawImage(
            halo.canvas,
            x - halo.half * s,
            y - halo.half * s,
            halo.half * 2 * s,
            halo.half * 2 * s,
          );
        }
      }
      const bucket = bucketFor(r);
      const entry = sprite(vm.spec, vm.spriteKey, bucket, dpr);
      ctx.globalAlpha = alpha;
      if (entry) {
        const s = r / bucket;
        ctx.drawImage(
          entry.canvas,
          x - entry.half * s,
          y - entry.half * s,
          entry.half * 2 * s,
          entry.half * 2 * s,
        );
      } else {
        paintNode(ctx, vm.spec, x, y, r);
      }
      drawn += 1;
      if (vm.type === 'vehicle') vehicles.push(i, r, alpha);
    }
    if (frontStart === n) {
      flushPoints();
      drawLines('front', 1);
    }
    function flushPoints() {
      for (const [color, list] of points) {
        ctx.globalAlpha = 0.3;
        ctx.fillStyle = color;
        ctx.beginPath();
        for (let k = 0; k < list.length; k += 2)
          ctx.rect(list[k] - 0.75, list[k + 1] - 0.75, 1.5, 1.5);
        ctx.fill();
      }
      points.clear();
    }
    if (ticks.length) {
      ctx.strokeStyle = COLORS.pencil;
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      for (let k = 0; k < ticks.length; k += 3) {
        ctx.moveTo(ticks[k] - 3, ticks[k + 1]);
        ctx.lineTo(ticks[k] + 3, ticks[k + 1]);
      }
      ctx.stroke();
    }
    for (let k = 0; k < vehicles.length; k += 3) {
      const i = vehicles[k];
      drawFuelRing(
        ctx,
        nodes[i],
        proj[i * 4],
        proj[i * 4 + 1],
        vehicles[k + 1],
        vehicles[k + 2],
      );
    }

    // Removed nodes fading out.
    for (const ghost of scene.ghosts || []) {
      if (!(ghost.z >= 0) || ghost.alpha <= 0.01) continue;
      const bucket = bucketFor(ghost.r);
      const entry = sprite(ghost.spec, ghost.spriteKey, bucket, dpr);
      ctx.globalAlpha = ghost.alpha;
      if (entry) {
        const s = ghost.r / bucket;
        ctx.drawImage(
          entry.canvas,
          ghost.x - entry.half * s,
          ghost.y - entry.half * s,
          entry.half * 2 * s,
          entry.half * 2 * s,
        );
      }
    }

    // Who is pointing: rings, batched by ink and width.
    const rings = {
      select: [],
      analyst: [],
      operator: [],
      hover: [],
      match: [],
    };
    for (let i = 0; i < n; i += 1) {
      const vm = nodes[i];
      if (!vm?.ring || proj[i * 4 + 2] < 0) continue;
      const x = proj[i * 4];
      const y = proj[i * 4 + 1];
      const r = vm.r * proj[i * 4 + 3];
      const f = vm.ring;
      const selected = f & RING.SELECT;
      if (selected) rings.select.push(x, y, r + 4, x, y, r + 7);
      if (f & (RING.HIGHLIGHT_ANALYST | RING.FOCUS_ANALYST))
        rings.analyst.push(x, y, selected ? r + 10 : r + 4);
      else if (!selected && f & (RING.HIGHLIGHT_OPERATOR | RING.FOCUS_OPERATOR))
        rings.operator.push(x, y, r + 4);
      else if (!selected && f & RING.HOVER) rings.hover.push(x, y, r + 4);
      else if (!selected && f & RING.MATCH) rings.match.push(x, y, r + 3);
    }
    const strokeCircles = (list, color, lineWidth) => {
      if (!list.length) return;
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      ctx.beginPath();
      for (let k = 0; k < list.length; k += 3) {
        ctx.moveTo(list[k] + list[k + 2], list[k + 1]);
        ctx.arc(list[k], list[k + 1], list[k + 2], 0, TAU);
      }
      ctx.stroke();
    };
    strokeCircles(rings.select, COLORS.film, 2);
    strokeCircles(rings.analyst, COLORS.analyst, 1.5);
    strokeCircles(rings.operator, COLORS.film, 1.5);
    strokeCircles(rings.hover, COLORS.film, 1);
    strokeCircles(rings.match, COLORS.film, 1);

    // Arrivals and status changes: one ring each.
    for (const ripple of scene.ripples || []) {
      if (!(ripple.z >= 0)) continue;
      ctx.globalAlpha = ripple.alpha;
      ctx.strokeStyle = ripple.color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(ripple.x, ripple.y, ripple.radius, 0, TAU);
      ctx.stroke();
    }

    // Limb marks: new-behind arcs and analyst-focus ticks.
    for (const mark of scene.limbMarks || []) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = mark.color;
      ctx.beginPath();
      if (mark.kind === 'tick') {
        ctx.lineWidth = 1.5;
        ctx.moveTo(
          cx + Math.cos(mark.angle) * (R + 2),
          cy + Math.sin(mark.angle) * (R + 2),
        );
        ctx.lineTo(
          cx + Math.cos(mark.angle) * (R + 9),
          cy + Math.sin(mark.angle) * (R + 9),
        );
      } else {
        const delta = 4 / (R + 4);
        ctx.lineWidth = 2;
        ctx.arc(cx, cy, R + 4, mark.angle - delta, mark.angle + delta);
      }
      ctx.stroke();
    }

    // Labels (and the band captions they must not collide with).
    const labels = { mode: scene.labelMode, items: [], inline: [] };
    const chosen = chooseLabels({ n, proj, nodes, budget: scene.labelBudget });
    prepText(ctx);
    const extent = (i) => {
      const r = nodes[i].r * proj[i * 4 + 3];
      return nodes[i].type === 'vehicle' ? r * 1.45 + 2 : r;
    };
    const glyphBox = (i) => {
      const g = extent(i);
      return [proj[i * 4] - g, proj[i * 4 + 1] - g, 2 * g, 2 * g];
    };
    const frontGlyphs = [];
    // Captions also keep off back glyphs near the limb: drawn at 30 % and
    // 0.8×, they still read as a disc under the text (review: "Contacts 16"
    // under a limb contact at compact width).
    const limbGlyphs = [];
    for (let i = 0; i < n; i += 1) {
      const z = proj[i * 4 + 2];
      if (!nodes[i] || nodes[i].alpha < 0.3) continue;
      if (z > 0) frontGlyphs.push(glyphBox(i));
      else if (z > LIMB_BACK_Z && !scene.pointsBack) {
        const g = extent(i) * 0.8;
        limbGlyphs.push([proj[i * 4] - g, proj[i * 4 + 1] - g, 2 * g, 2 * g]);
      }
    }
    // The "not live" watermark is text too: captions, labels and sector
    // names keep off it (review: "Units 4" ran into it at phone width).
    const watermarkY = cy + R * WATERMARK_AT_R;
    const watermarkBox = scene.watermark
      ? (() => {
          const w = measure(ctx, scene.watermark);
          return [cx - w / 2 - 2, watermarkY - 9, w + 4, 18];
        })()
      : null;
    const captionRows = placeCaptions(scene.captions, camera, {
      cx,
      cy,
      R,
      mode: scene.labelMode,
      measure: (text) => measure(ctx, text),
      avoid: [
        ...frontGlyphs,
        ...limbGlyphs,
        ...(watermarkBox ? [watermarkBox] : []),
      ],
    });
    // Inline labels keep off each other and off the band captions. They
    // prefer a slot clear of every front glyph (a vehicle's fuel ring
    // included); failing that, a label that outranks ordinary nodes
    // (selected, analyst, hovered, search hit, vehicle, critical) takes one
    // clear of the glyphs that outrank it, so it is never lost to a lesser
    // node's glyph. An ordinary label never prints over a glyph: in a crowded
    // polar cap it stays hidden (review: "Sim" and "East Field" drawn over
    // feed bars at phone width); the twin, List view and hover still name it.
    const chosenGlyphs = chosen.map(glyphBox);
    const rankOf = new Map(chosen.map((i, k) => [i, k]));
    const placed = captionRows.map((row) => [
      row.left - 2,
      row.y - 9,
      row.right - row.left + 4,
      18,
    ]);
    if (watermarkBox) placed.push(watermarkBox);
    const labelled = new Set(chosen);
    const others = [];
    for (let i = 0; i < n; i += 1) {
      if (labelled.has(i) || !(proj[i * 4 + 2] > 0)) continue;
      if (!nodes[i] || nodes[i].alpha < 0.5) continue;
      others.push(glyphBox(i));
    }
    const hits = (list, box) =>
      list.some(
        (b) =>
          b[0] < box[0] + box[2] &&
          box[0] < b[0] + b[2] &&
          b[1] < box[1] + box[3] &&
          box[1] < b[1] + b[3],
      );
    const inlineLabel = (i) => {
      const vm = nodes[i];
      const text = vm.isNew ? `${vm.label}  New` : vm.label;
      const w = measure(ctx, text);
      const x = proj[i * 4];
      const y = proj[i * 4 + 1];
      const g = extent(i);
      // Right, then left; a crowded cap may also take the slot above or
      // below, centred. Otherwise the label stays hidden.
      const tries = [
        [x + g + 6, y, 'left', x + g + 6],
        [x - g - 6, y, 'right', x - g - 6 - w],
        [x, y - g - 11, 'center', x - w / 2],
        [x, y + g + 11, 'center', x - w / 2],
      ];
      const outranking = chosenGlyphs.slice(0, (rankOf.get(i) ?? -1) + 1);
      const fits = ([, ty, , left], strict) => {
        const box = [left - 2, ty - 9, w + 4, 18];
        if (left < 2 || left + w > width - 2 || ty < 9 || ty > height - 9)
          return null;
        if (hits(placed, box)) return null;
        if (strict && (hits(chosenGlyphs, box) || hits(others, box)))
          return null;
        if (!strict && hits(outranking, box)) return null;
        return box;
      };
      let pick = null;
      for (const strict of vm.tier < TIER.OTHER ? [true, false] : [true]) {
        for (const slot of tries) {
          const box = fits(slot, strict);
          if (box) {
            pick = [slot, box];
            break;
          }
        }
        if (pick) break;
      }
      if (pick) {
        const [[tx, ty, align], box] = pick;
        placed.push(box);
        drawText(
          ctx,
          text,
          tx,
          ty,
          align,
          vm.ink === 'analyst' ? COLORS.analyst : COLORS.film,
        );
        labels.inline.push(i);
        return true;
      }
      return false;
    };
    // Sector names on the belt: never on a caption, a label or a labelled
    // glyph. In the margin layout they are placed first and the leaders
    // route around them; inline labels come first and push them out.
    const sectorRows = [];
    const placeSectorNames = () => {
      if (!scene.sectorNames) return;
      const probe = [0, 0, 0];
      for (const sector of SECTORS) {
        toVector(BANDS.track.lat, sector.lonCenter, probe);
        if (camera.toView(probe)[2] < 0.5) continue;
        toVector(GRATICULE_PARALLELS.sectorNames, sector.lonCenter, probe);
        camera.projectXYZ(probe[0], probe[1], probe[2], tmp, 0);
        const w = measure(ctx, sector.label);
        const box = [tmp[0] - w / 2 - 2, tmp[1] - 9, w + 4, 18];
        // Never on any drawn glyph either (review: "Command and control"
        // printed over a contact disc at compact width).
        if (
          hits(placed, box) ||
          hits(chosenGlyphs, box) ||
          hits(frontGlyphs, box)
        )
          continue;
        placed.push(box);
        sectorRows.push({
          text: sector.label,
          x: tmp[0],
          y: tmp[1],
          left: tmp[0] - w / 2,
          right: tmp[0] + w / 2,
        });
      }
    };
    if (scene.labelMode === 'margin') {
      placeSectorNames();
      const hovered = scene.hovered ?? -1;
      const margin = chosen
        .filter((i) => i !== hovered)
        .slice(0, MARGIN_LABEL_MAX);
      const radii = new Float32Array(n);
      for (const i of margin) radii[i] = extent(i);
      const laidOut = layoutMarginLabels(margin, proj, {
        cx,
        cy,
        R,
        width,
        height,
        radii,
        captions: [...captionRows, ...sectorRows],
        twoLine: (i) => Boolean(nodes[i].subtitle),
        moreRow: Boolean(scene.moreRow),
      });
      // A gutter too narrow for text (zoomed in) falls back to inline labels.
      labels.items = laidOut.filter((item) => item.width >= MARGIN_MIN_WIDTH);
      const squeezed = laidOut.filter((item) => item.width < MARGIN_MIN_WIDTH);
      const leaders = { film: [], analyst: [] };
      for (const item of labels.items) {
        const list =
          nodes[item.i].ink === 'analyst' ? leaders.analyst : leaders.film;
        list.push(item.leader);
      }
      for (const [ink, list] of Object.entries(leaders)) {
        if (!list.length) continue;
        ctx.globalAlpha = 0.6;
        ctx.strokeStyle = ink === 'analyst' ? COLORS.analyst : COLORS.film;
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (const pts of list) {
          ctx.moveTo(pts[0], pts[1]);
          for (let k = 2; k < pts.length; k += 2)
            ctx.lineTo(pts[k], pts[k + 1]);
        }
        ctx.stroke();
      }
      prepText(ctx);
      if (hovered >= 0 && chosen.includes(hovered)) inlineLabel(hovered);
      for (const item of squeezed) inlineLabel(item.i);
    } else {
      for (const i of chosen) inlineLabel(i);
      placeSectorNames();
    }

    // Band captions hugging the left limb, sector names on the belt.
    for (const row of captionRows) {
      drawText(
        ctx,
        row.text,
        row.x,
        row.y,
        row.align,
        row.ink || COLORS.pencil,
      );
    }
    for (const row of sectorRows)
      drawText(ctx, row.text, row.x, row.y, 'center', COLORS.pencil);

    if (scene.twinFocused) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = COLORS.film;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, R + 6, 0, TAU);
      ctx.stroke();
    }
    if (scene.watermark) {
      prepText(ctx);
      drawText(ctx, scene.watermark, cx, watermarkY, 'center', COLORS.pencil);
    }
    ctx.globalAlpha = 1;
    return { ms: now() - started, labels, drawn, chosen };
  }

  return {
    draw,
    paintNode,
    /** Width of `text` in the label font (cached; the margin labels use it). */
    measureLabel(ctx, text) {
      if (!ctx) return String(text).length * 6.5;
      ctx.font = LABEL_FONT;
      return measure(ctx, String(text));
    },
    /** Forget cached text widths (after web fonts load). */
    flushText() {
      widths.clear();
    },
    clearSprites() {
      sprites.clear();
    },
    get spriteCount() {
      return [...sprites.values()].filter(Boolean).length;
    },
  };
}

/**
 * Band captions in pole-to-pole order, e.g. "Contacts 23 (+1)". The site band
 * (WG §4.2.6) reads "Sites 41 (12 more on the map)", draws nothing when
 * empty, and says so in warn ink when the map data feed is degraded.
 * @param {object} counts layout.counts
 * @param {{recent?: object, detectionsDown?: boolean,
 *   sites?: {count?: number, omitted?: number, degraded?: boolean}}} [options]
 *   `sites.count` is the number drawn on the orb (default counts.site)
 */
export function bandCaptions(
  counts,
  { recent = {}, detectionsDown = false, sites = null } = {},
) {
  const out = [];
  for (const key of BAND_ORDER) {
    const band = BANDS[key];
    const count = counts?.[key] || 0;
    if (key === 'other' && !count) continue;
    if (key === 'site') {
      const row = siteCaption(count, sites);
      if (row) out.push({ key, ...row, lat: band.lat });
      continue;
    }
    let text = `${band.caption} ${count}`;
    let ink = null;
    if (key === 'track' && detectionsDown) {
      text = 'Detection feed down. Contacts may be missing, not absent.';
      ink = COLORS.warn;
    } else if (key === 'track' && !count) {
      text = 'Contacts: none reported yet';
    } else if (recent[key] > 0) {
      text += ` (+${recent[key]})`;
    }
    out.push({ key, text, ink, lat: key === 'theater' ? 86 : band.lat });
  }
  return out;
}

/** The site band caption row ({text, ink}) or null for an empty band. */
function siteCaption(count, sites) {
  if (sites?.degraded) return { text: SITES_DEGRADED_TEXT, ink: COLORS.warn };
  const drawn = Number.isFinite(sites?.count) ? sites.count : count;
  const text = siteBandCaption({ count: drawn, omitted: sites?.omitted || 0 });
  return text ? { text, ink: null } : null;
}
