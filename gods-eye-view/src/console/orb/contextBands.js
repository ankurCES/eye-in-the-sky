/**
 * Context bands on the orb (WG spec §4.2.6; Phase B adds the session
 * profile of §5.3.5 here). Context items orient the operator; they are not
 * contacts and never carry a state colour. Phase A has one: mapped strategic
 * sites.
 *
 * Sites sit in one row at +42°, between the missions (+50°) and the contact
 * belt, whose top moves down to +35° to make room (the graticule's belt line
 * moves from +44° to +38.5°). The row is anchored to the equipment sectors:
 * 12 slots of 3° per 36° sector. A sector with more than 12 sites draws its
 * 12 most salient and counts the rest, which the caption sends to the map.
 *
 * Pure and import-free (layout.js imports this, not the other way round).
 */

export const SITE_BAND = Object.freeze({
  caption: 'Sites',
  lat: 42,
  slotDeg: 3,
  slotsPerSector: 12,
});

/** Context node types and their bands (Phase B adds forces and engagements). */
export const CONTEXT_BANDS = Object.freeze({ site: SITE_BAND });

/** Sector width in degrees; sector 0 is centred on longitude 0 (layout.js). */
const SECTOR_WIDTH_DEG = 36;

/**
 * Sector keys by site category (§3.2 `SITE_GROUP`), used only when a site
 * node arrives without a usable `group`.
 */
export const SITE_SECTORS = Object.freeze({
  airfield: 'air',
  military_base: 'ground-forces',
  port: 'naval',
  power: 'infrastructure',
  bridge: 'infrastructure',
  dam: 'infrastructure',
  fuel: 'logistics',
  rail_hub: 'logistics',
  comms: 'radar-ew',
  hq_gov: 'c2',
  border_crossing: 'civilian',
  medical: 'civilian',
  other: 'unclassified',
});

/**
 * The sector key a site sits in: its `group` when that is one of `sectorKeys`,
 * else its category's sector, else `unclassified`.
 * @param {object} node a `site` graph node
 * @param {Set<string>|string[]} sectorKeys the known sector keys
 */
export function siteSectorKey(node, sectorKeys) {
  const keys = sectorKeys instanceof Set ? sectorKeys : new Set(sectorKeys);
  if (typeof node?.group === 'string' && keys.has(node.group))
    return node.group;
  const category = node?.attrs?.category;
  if (typeof category === 'string' && Object.hasOwn(SITE_SECTORS, category))
    return SITE_SECTORS[category];
  return 'unclassified';
}

function wrapLon(lon) {
  const wrapped = ((((lon + 180) % 360) + 360) % 360) - 180;
  return Object.is(wrapped, -0) ? 0 : wrapped;
}

/** Longitude of `slot` in sector `sector` (slot 0 at the sector's west edge). */
export function sectorRowLon(
  sector,
  slot,
  { slotDeg = SITE_BAND.slotDeg } = {},
) {
  return wrapLon(
    -SECTOR_WIDTH_DEG / 2 + sector * SECTOR_WIDTH_DEG + (slot + 0.5) * slotDeg,
  );
}

/** Slots in probe order from `start`: start, +1, −1, +2, −2, … inside [0, n). */
function probeOrder(start, n) {
  const out = [];
  for (let step = 0; out.length < n && step < 2 * n; step += 1) {
    const offset = step % 2 ? (step + 1) / 2 : -step / 2;
    const slot = start + offset;
    if (slot >= 0 && slot < n) out.push(slot);
  }
  return out;
}

const compareIds = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

/**
 * Assign one-row sector slots.
 *
 * Inside each sector the items are ranked by salience (then id); the top
 * `slots` are drawn and the rest overflow. A drawn item keeps its previous
 * slot when it still has one in the same sector; new ones start at an even
 * share of the sector for their rank (so a sparse sector spreads out) and
 * probe outward to the nearest free slot. Deterministic for the same items
 * and previous assignments, in any input order.
 *
 * @param {Array<{id:string, sector:number, salience?:number}>} items
 * @param {{slots?: number, previous?: (id:string) => ({sector:number, slot:number}|null|undefined)}} [options]
 * @returns {{slots: Map<string, {sector:number, slot:number}>, overflow: string[]}}
 */
export function placeSectorRow(
  items,
  { slots = SITE_BAND.slotsPerSector, previous = () => null } = {},
) {
  const bySector = new Map();
  for (const item of items || []) {
    if (!item || typeof item.id !== 'string') continue;
    const list = bySector.get(item.sector);
    if (list) list.push(item);
    else bySector.set(item.sector, [item]);
  }
  const out = new Map();
  const overflow = [];
  const sectors = [...bySector.keys()].sort((a, b) => a - b);
  for (const sector of sectors) {
    const ranked = bySector
      .get(sector)
      .slice()
      .sort(
        (a, b) =>
          (Number(b.salience) || 0) - (Number(a.salience) || 0) ||
          compareIds(a.id, b.id),
      );
    const shown = ranked.slice(0, slots);
    for (const item of ranked.slice(slots)) overflow.push(item.id);
    const taken = new Set();
    const pending = [];
    shown.forEach((item, rank) => {
      const was = previous(item.id);
      if (
        was &&
        was.sector === sector &&
        Number.isInteger(was.slot) &&
        was.slot >= 0 &&
        was.slot < slots &&
        !taken.has(was.slot)
      ) {
        taken.add(was.slot);
        out.set(item.id, { sector, slot: was.slot });
      } else {
        pending.push([item, rank]);
      }
    });
    for (const [item, rank] of pending) {
      const start = Math.min(
        slots - 1,
        Math.floor(((rank + 0.5) * slots) / shown.length),
      );
      const slot = probeOrder(start, slots).find((s) => !taken.has(s));
      taken.add(slot);
      out.set(item.id, { sector, slot });
    }
  }
  overflow.sort(compareIds);
  return { slots: out, overflow };
}
