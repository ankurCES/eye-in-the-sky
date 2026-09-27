/**
 * Re-validation of an order slip at decision time (UX spec §6.5).
 *
 * Pure. `assess(approval, snapshot, vehicleNow, approvedSince)` compares the
 * vehicle state captured when the matching dry run came back (`snapshot`)
 * with the vehicle now, and looks at what was approved since, and returns
 *
 *   {state: 'current'|'stale'|'failed'|'none'|'unverified', reasons:[…], failed?}
 *
 *   current     dry run present, nothing moved, gate passed
 *   stale       conditions changed (the slip demotes Approve); `failed` is also
 *               set when the stale dry run had failed its gate
 *   failed      dry run current but the server gate failed (no approve at all)
 *   none        no dry run: either the tool can't be dry-run or none was made
 *   unverified  a dry run exists but the console can't compare it (no snapshot
 *               from this page load, or the vehicle is not in the picture)
 *
 * Fuel is judged against the burn the server already measures: an airborne
 * aircraft burns about 0.06–0.08 %/s, so a raw "fuel moved 2 points" rule
 * would demote every airborne slip within ~30 s of reading it. Staleness is
 * fuel that moved 2 points or more away from what that burn predicts
 * (spec §6.5 amendment). Without a burn rate the raw rule applies.
 */

import {
  STALING_TOOLS,
  approvalVehicle,
  isDryRunnable,
  linkWord,
  pct,
  toolTitle,
} from './format.js';

export const FUEL_POINTS = 2;
export const MOVE_METERS = 50;

const num = (v) => (Number.isFinite(v) ? v : null);
const bool = (v) => (typeof v === 'boolean' ? v : null);
const str = (v) => (typeof v === 'string' && v ? v : null);

/** Great-circle distance in metres. */
export function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6_371_008.8;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

/**
 * Normalize a vehicle graph node (contract §4) into the facts the slip uses.
 * Accepts a node `{id, lat, lon, attrs}` or an already-normalized record.
 */
export function vehicleFacts(node, at = null) {
  if (!node || typeof node !== 'object') return null;
  const attrs =
    node.attrs && typeof node.attrs === 'object' ? node.attrs : node;
  const id = str(node.id);
  const name =
    str(node.name) ||
    (id && id.startsWith('veh:') ? id.slice(4) : null) ||
    str(node.label);
  return {
    name,
    fuel_pct: num(attrs.fuel_pct),
    bingo_fuel_pct: num(attrs.bingo_fuel_pct),
    eta_to_bingo_s: num(attrs.eta_to_bingo_s),
    landed: bool(attrs.landed),
    lat: num(node.lat ?? attrs.lat),
    lon: num(node.lon ?? attrs.lon),
    bingo_latched: bool(attrs.bingo_latched),
    link: str(attrs.link),
    agl_m: num(attrs.agl_m),
    agl_is_real: bool(attrs.agl_is_real),
    stale_ms: num(attrs.stale_ms),
    mission: str(attrs.mission),
    lost_link:
      attrs.lost_link && typeof attrs.lost_link === 'object'
        ? attrs.lost_link
        : null,
    status: str(node.status),
    at: num(at ?? node.at),
    // When the reading was taken (the graph node's telemetry stamp).
    read_at: num(node.ts_ms ?? attrs.ts_ms),
  };
}

/**
 * The fuel burn in percentage points per second, from the server's own
 * projection: (fuel − BINGO line) / time to BINGO. Null when unknown, on the
 * ground, or past BINGO.
 */
export function burnRate(facts) {
  if (!facts || facts.landed === true) return null;
  const fuel = num(facts.fuel_pct);
  const bingo = num(facts.bingo_fuel_pct);
  const eta = num(facts.eta_to_bingo_s);
  if (fuel == null || bingo == null || eta == null) return null;
  if (eta <= 0 || fuel <= bingo) return null;
  return (fuel - bingo) / eta;
}

/**
 * The fuel the snapshot's burn predicts for `now`, or null when there is no
 * burn to apply. Both ends use the same clock: the telemetry stamps when both
 * readings carry one, else the capture times.
 */
export function expectedFuel(snapshot, now) {
  const f0 = num(snapshot?.fuel_pct);
  if (f0 == null || !now) return null;
  if (snapshot.landed === true || now.landed === true) return null;
  const rate = burnRate(now) ?? burnRate(snapshot);
  if (rate == null) return null;
  const stamped = num(snapshot.read_at) != null && num(now.read_at) != null;
  const t0 = stamped ? num(snapshot.read_at) : num(snapshot.at);
  const t1 = stamped ? num(now.read_at) : num(now.at);
  if (t0 == null || t1 == null || t1 <= t0) return null;
  return f0 - (rate * (t1 - t0)) / 1000;
}

function compare(snapshot, now, reasons) {
  const f0 = num(snapshot.fuel_pct);
  const f1 = num(now.fuel_pct);
  if (f0 != null && f1 != null) {
    const expected = expectedFuel(snapshot, now);
    if (Math.abs(f1 - (expected ?? f0)) >= FUEL_POINTS) {
      reasons.push(
        expected == null
          ? { code: 'fuel', from: f0, to: f1 }
          : { code: 'fuel', from: f0, to: f1, expected },
      );
    }
  }
  const l0 = bool(snapshot.landed);
  const l1 = bool(now.landed);
  if (l0 != null && l1 != null && l0 !== l1) {
    reasons.push({ code: 'landed', from: l0, to: l1 });
  }
  const { lat: a0, lon: o0 } = snapshot;
  const { lat: a1, lon: o1 } = now;
  if ([a0, o0, a1, o1].every(Number.isFinite)) {
    const meters = haversineM(a0, o0, a1, o1);
    if (meters > MOVE_METERS) reasons.push({ code: 'moved', meters });
  }
  const b0 = bool(snapshot.bingo_latched);
  const b1 = bool(now.bingo_latched);
  if (b0 != null && b1 != null && b0 !== b1) {
    reasons.push({ code: 'bingo', from: b0, to: b1 });
  }
  const k0 = str(snapshot.link);
  const k1 = str(now.link);
  if (k0 && k1 && k0 !== k1) reasons.push({ code: 'link', from: k0, to: k1 });
}

/**
 * @param {object} approval reducer approval record (tool, klass, args, dry_run…)
 * @param {object|null} snapshot vehicle facts captured with the dry run
 * @param {object|null} vehicleNow vehicle facts now
 * @param {Array} approvedSince approvals approved after the dry run:
 *   [{id?, tool, klass, vehicle, title?, at?}]
 */
export function assess(approval, snapshot, vehicleNow, approvedSince = []) {
  if (!approval) return { state: 'none', reasons: [{ code: 'no_approval' }] };
  if (!isDryRunnable(approval)) {
    return { state: 'none', reasons: [{ code: 'not_dry_runnable' }] };
  }
  const dry = approval.dry_run;
  if (!dry || typeof dry !== 'object') {
    return { state: 'none', reasons: [{ code: 'no_dry_run' }] };
  }
  const reasons = [];
  if (dry.matches_args === false) reasons.push({ code: 'args_differ' });
  const vehicle = approvalVehicle(approval);
  const since = num(snapshot?.at) ?? num(dry.at_ms);
  for (const other of Array.isArray(approvedSince) ? approvedSince : []) {
    if (!other || (other.id && other.id === approval.id)) continue;
    if (since != null && Number.isFinite(other.at) && other.at <= since)
      continue;
    if (STALING_TOOLS.includes(other.tool)) {
      reasons.push({ code: 'sim_change', tool: other.tool });
    } else if (
      (other.klass ?? other.class) === 'command' &&
      vehicle &&
      other.vehicle === vehicle
    ) {
      reasons.push({
        code: 'other_command',
        tool: other.tool,
        title: other.title ?? null,
      });
    }
  }
  if (snapshot && vehicleNow) compare(snapshot, vehicleNow, reasons);
  const failed = dry.ok === false;
  if (reasons.length) return { state: 'stale', reasons, failed };
  if (failed) return { state: 'failed', reasons: [{ code: 'gate_failed' }] };
  if (!snapshot)
    return { state: 'unverified', reasons: [{ code: 'no_snapshot' }] };
  if (!vehicleNow)
    return { state: 'unverified', reasons: [{ code: 'no_vehicle_now' }] };
  return { state: 'current', reasons: [] };
}

/** Whether Approve is demoted (primary becomes "Ask for a fresh dry run"). */
export function demotesApprove(assessment) {
  return assessment?.state === 'stale';
}

/** Whether approval is impossible from the slip (gate failed, dry run current). */
export function blocksApprove(assessment) {
  return assessment?.state === 'failed';
}

/**
 * Human summary of stale reasons for the warn strip:
 * "fuel 82.0% → 74.5%; Drone1 is now airborne".
 */
export function staleSummary(reasons, vehicle = null) {
  const who = vehicle || 'The aircraft';
  const parts = [];
  for (const r of Array.isArray(reasons) ? reasons : []) {
    switch (r.code) {
      case 'fuel':
        parts.push(
          Number.isFinite(r.expected)
            ? `fuel ${pct(r.from)} → ${pct(r.to)} (its burn predicts ≈ ${pct(r.expected)})`
            : `fuel ${pct(r.from)} → ${pct(r.to)}`,
        );
        break;
      case 'landed':
        parts.push(r.to ? `${who} has landed` : `${who} is now airborne`);
        break;
      case 'moved':
        parts.push(`${who} moved ${Math.round(r.meters)} m`);
        break;
      case 'bingo':
        parts.push(
          r.to ? `${who} reached BINGO` : `${who}'s BINGO latch cleared`,
        );
        break;
      case 'link': {
        const from = linkWord(r.from) || `Link ${r.from}`;
        const to = (linkWord(r.to) || `Link ${r.to}`).replace(/^Link /, '');
        parts.push(`${from.toLowerCase()} → ${to}`);
        break;
      }
      case 'sim_change':
        parts.push(`${toolTitle(r.tool)} was approved`);
        break;
      case 'other_command':
        parts.push(
          `another command for ${who} was approved (${r.title || toolTitle(r.tool)})`,
        );
        break;
      case 'args_differ':
        parts.push('the dry run used different settings');
        break;
      default:
        break;
    }
  }
  return parts.join('; ');
}
