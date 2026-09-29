import test from 'node:test';
import assert from 'node:assert/strict';

import { COLORS } from '../../console/orb/glyphs.js';
import { WG_INK } from '../../console/orb/wargameStyles.js';
import {
  CONTEXT_DATA_SOURCE,
  CONTEXT_ERROR_BACKOFF_MS,
  CONTEXT_POLL_MS,
  DEFAULT_VISIBILITY,
  KNOWN_KINDS,
  MAX_BILLBOARDS,
  OUTCOME_WORDS,
  WARGAME_CAPS,
  WARGAME_INK,
  WARGAME_KINDS,
  WARGAME_MAP_COPY,
  burstSpec,
  contextOverlayUrl,
  createTheaterWatcher,
  engagementHasRing,
  engagementMapLabel,
  exposureInk,
  frameSpec,
  hiddenInView,
  isScenarioFeature,
  kindVisible,
  mergeVisibility,
  readTheaterRef,
  ringSpec,
  statusInk,
  visibilitySwitch,
} from './contextPolicy.js';

test('the context overlay contract numbers are the spec values (§4.2.7)', () => {
  assert.equal(CONTEXT_DATA_SOURCE, 'uav-context-overlay');
  assert.equal(CONTEXT_POLL_MS, 3000);
  assert.equal(CONTEXT_ERROR_BACKOFF_MS, 5000);
  assert.equal(MAX_BILLBOARDS, 150);
});

test('each overlay kind answers to one switch; an unknown kind to none', () => {
  assert.equal(visibilitySwitch('site'), 'sites');
  assert.equal(visibilitySwitch('force'), 'forces');
  assert.equal(visibilitySwitch('force_envelope'), 'forces');
  assert.equal(visibilitySwitch('engagement'), 'engagements');
  assert.equal(visibilitySwitch('vector'), 'vectors');
  assert.equal(visibilitySwitch('mystery'), null);
  assert.equal(visibilitySwitch(undefined), null);
});

test('switches merge partially and an unknown kind is never hidden', () => {
  const off = mergeVisibility(DEFAULT_VISIBILITY, {
    sites: false,
    bogus: false,
    forces: 'no',
  });
  assert.deepEqual(off, { ...DEFAULT_VISIBILITY, sites: false });
  assert.equal(kindVisible('site', off), false);
  assert.equal(kindVisible('force', off), true);
  const allOff = mergeVisibility(off, {
    forces: false,
    engagements: false,
    vectors: false,
  });
  assert.equal(kindVisible('mystery', allOff), true);
  assert.deepEqual(mergeVisibility(off, null), off);
});

test('the overlay URL carries truth and the last rev, encoded', () => {
  assert.equal(
    contextOverlayUrl('http://127.0.0.1:8780/'),
    'http://127.0.0.1:8780/intel/overlay?truth=0',
  );
  assert.equal(
    contextOverlayUrl('http://h', { truth: true, rev: '3:17:0:1' }),
    'http://h/intel/overlay?truth=1&rev=3%3A17%3A0%3A1',
  );
  assert.equal(
    contextOverlayUrl('', { rev: '' }),
    '/intel/overlay?truth=0',
    'an empty base is the page origin; an empty rev is omitted',
  );
});

test('a snapshot theater block reads only a plain integer epoch', () => {
  assert.deepEqual(readTheaterRef({ id: ' dyn-a ', epoch: 3 }), {
    id: 'dyn-a',
    epoch: 3,
  });
  assert.deepEqual(readTheaterRef({ id: 'default', epoch: '3' }), {
    id: 'default',
    epoch: null,
  });
  assert.deepEqual(readTheaterRef({ id: 'default', epoch: true }), {
    id: 'default',
    epoch: null,
  });
  assert.equal(readTheaterRef({ id: null, epoch: null }), null);
  assert.equal(readTheaterRef(null), null);
  assert.equal(readTheaterRef('default'), null);
});

test('the first theater seen is a baseline, not a change', () => {
  const watch = createTheaterWatcher();
  assert.equal(watch.observe({ id: 'default', epoch: 0 }), null);
  assert.equal(watch.observe({ id: 'default', epoch: 0 }), null);
  assert.deepEqual(watch.current(), { id: 'default', epoch: 0 });
});

test('a switch is one change whichever order the id and epoch arrive in', () => {
  // id and epoch together
  let watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.deepEqual(watch.observe({ id: 'dyn-k', epoch: 1 }), {
    from: { id: 'default', epoch: 0 },
    to: { id: 'dyn-k', epoch: 1 },
  });
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // unknown for one poll, then the new theater
  watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.equal(watch.observe({ id: null, epoch: null }), null);
  assert.ok(watch.observe({ id: 'dyn-k', epoch: 1 }));
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // the id first with a null epoch, the epoch a poll later: still one change
  watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: 0 });
  assert.ok(watch.observe({ id: 'dyn-k', epoch: null }));
  assert.equal(watch.observe({ id: 'dyn-k', epoch: 1 }), null);

  // the same id, a transient null, then a new epoch (airframe-only switch)
  watch = createTheaterWatcher();
  watch.observe({ id: 'dyn-k', epoch: 1 });
  assert.equal(watch.observe({ id: 'dyn-k', epoch: null }), null);
  assert.deepEqual(watch.observe({ id: 'dyn-k', epoch: 2 }), {
    from: { id: 'dyn-k', epoch: 1 },
    to: { id: 'dyn-k', epoch: 2 },
  });
});

test('an older server (never an epoch) still reports an id change', () => {
  const watch = createTheaterWatcher();
  watch.observe({ id: 'default', epoch: null });
  assert.equal(watch.observe({ id: 'default', epoch: null }), null);
  assert.ok(watch.observe({ id: 'iran-isfahan', epoch: null }));
  watch.reset();
  assert.equal(watch.current(), null);
});

// ---- the simulated wargame on the map (WG v2 §5.3.12, B16) --------------------

/** The real-system and weaponeering tokens of tests/support/wg_tokens.py. */
const REAL_SYSTEM_TOKENS = Object.freeze([
  /S-300/i,
  /\bSA-\d/i,
  /\bTor\b/i,
  /Pantsir/i,
  /ZSU/i,
  /\bZU-/i,
  /warhead/i,
  /\bmunition/i,
  /fuze/i,
  /fuzing/i,
  /blast radius/i,
  /aimpoint/i,
  /\bCEP\b/i,
  /\d+\s?(kg|mm)\b/i,
]);

const PHASES = ['proposed', 'authorized', 'adjudicated', 'denied', 'expired'];
const OUTCOMES = ['missed', 'suppressed', 'damaged', 'destroyed', null, 'gone'];

test('the map draws the four wargame kinds, each under its dock switch', () => {
  assert.deepEqual(WARGAME_KINDS, [
    'force',
    'force_envelope',
    'vector',
    'engagement',
  ]);
  assert.deepEqual(KNOWN_KINDS, ['site', ...WARGAME_KINDS]);
  assert.deepEqual(WARGAME_CAPS, {
    force: 60,
    force_envelope: 80,
    axis: 20,
    corridor: 6,
    engagement: 24,
  });
  assert.equal(
    isScenarioFeature({ register: 'scenario', simulated: true }),
    true,
  );
  assert.equal(isScenarioFeature({ register: 'scenario' }), false);
  assert.equal(
    isScenarioFeature({ register: 'mapped', simulated: true }),
    false,
  );
});

test('wargame inks are the console palette, and nothing is own-systems green', () => {
  for (const key of [
    'film',
    'pencil',
    'warn',
    'critical',
    'stale',
    'lilac',
    'sand',
  ])
    assert.equal(WARGAME_INK[key], WG_INK[key], key);
  assert.equal(WARGAME_INK.friendly, '#80E0FF');
  assert.equal(WARGAME_INK.hostile, '#FF8080');
  const inks = new Set(Object.values(WARGAME_INK));
  for (const side of ['red', 'blue', 'x'])
    for (const state of ['active', 'suppressed', 'damaged', 'destroyed', 'x'])
      for (const status of ['ok', 'warn', 'critical', 'stale', 'x']) {
        const spec = frameSpec({ side, state, status });
        for (const ink of [spec.fill, spec.stroke, spec.bar].filter(Boolean))
          inks.add(ink);
      }
  for (const phase of [...PHASES, 'x']) inks.add(burstSpec({ phase }).stroke);
  for (const c of ['own_loss', 'own_damage', 'red_effect', 'none', 'x'])
    inks.add(ringSpec(c).stroke);
  for (const e of ['low', 'moderate', 'high', 'x']) inks.add(exposureInk(e));
  for (const s of ['ok', 'warn', 'critical', 'stale', 'x'])
    inks.add(statusInk(s));
  assert.equal(inks.has(COLORS.ok), false, 'never green');
});

test('Blue view hides every red or unsided force, envelope and axis; Umpire shows them only from a truth body', () => {
  const red = { side: 'red', truth: true };
  const blueView = { truth: false };
  const umpire = { truth: true };
  assert.equal(hiddenInView('force', red, blueView), true);
  assert.equal(hiddenInView('force', red, umpire), false);
  assert.equal(
    hiddenInView('force', { side: 'red', truth: false }, umpire),
    true,
    'a body fetched for Blue view never shows red, whatever the console asks',
  );
  assert.equal(hiddenInView('force', { side: 'blue' }, blueView), false);
  assert.equal(hiddenInView('force', { side: '<img>' }, blueView), true);
  assert.equal(hiddenInView('force_envelope', red, blueView), true);
  assert.equal(
    hiddenInView('vector', { side: 'red', kind_detail: 'axis' }, blueView),
    true,
  );
  assert.equal(
    hiddenInView('vector', { side: 'blue', kind_detail: 'axis' }, blueView),
    true,
    'every axis is red truth',
  );
  assert.equal(
    hiddenInView('vector', { side: 'blue', kind_detail: 'corridor' }, blueView),
    false,
  );
  // Engagements stay: the server masks a hidden attacker and outcome.
  assert.equal(hiddenInView('engagement', { side: 'red' }, blueView), false);
  assert.equal(hiddenInView('force', red), true, 'Blue view by default');
});

test('frames: shape by side, pattern by state, halo only for a red unit whose threat reaches us', () => {
  const blue = frameSpec({ side: 'blue', state: 'active', status: 'ok' });
  assert.equal(blue.side, 'blue');
  assert.equal(blue.fill, WARGAME_INK.friendly);
  assert.equal(blue.stroke, WARGAME_INK.film, 'a Film stroke on every frame');
  assert.equal(blue.bar, WARGAME_INK.film);
  const red = frameSpec({ side: 'red', state: 'active', status: 'critical' });
  assert.equal(red.fill, WARGAME_INK.hostile);
  assert.equal(red.stroke, WARGAME_INK.film);
  assert.equal(red.bar, WARGAME_INK.critical);
  assert.equal(red.halo, true);
  assert.equal(
    frameSpec({ side: 'red', state: 'active', status: 'warn' }).halo,
    false,
  );
  assert.equal(frameSpec({ side: 'blue', state: 'suppressed' }).dash, 'frame');
  assert.equal(
    frameSpec({ side: 'blue', state: 'suppressed' }).bar,
    WARGAME_INK.warn,
  );
  assert.equal(
    frameSpec({ side: 'red', state: 'damaged', status: 'ok' }).barBroken,
    true,
  );
  const gone = frameSpec({ side: 'blue', state: 'destroyed', status: 'ok' });
  assert.equal(gone.slash, true);
  assert.equal(gone.stroke, WARGAME_INK.stale);
  assert.equal(gone.alpha, 0.5);
  assert.equal(frameSpec({ side: 'red', state: 'destroyed' }).alpha, 1);
  const unsided = frameSpec({ side: 'green', state: 'active' });
  assert.equal(unsided.side, 'unknown');
  assert.equal(unsided.fill, null, 'no hue for a side the map does not know');
  assert.equal(unsided.stroke, WARGAME_INK.lilac);
  assert.equal(unsided.dash, 'unknown');
  assert.equal(
    frameSpec({ side: 'red', state: 'melted' }).stroke,
    WARGAME_INK.lilac,
  );
  // Keys come from the closed vocabularies only.
  const odd = frameSpec({ side: '<img src=x>', state: '‮x', status: 'x' });
  assert.equal(odd.key, 'unknown:unknown:unknown');
});

test('engagement words: outcome labels, hidden outcomes and phases, constants only', () => {
  const label = (phase, outcome, kind = 'blue_strike') =>
    engagementMapLabel({ phase, outcome, kind_detail: kind });
  assert.equal(label('adjudicated', 'destroyed'), 'Destroyed (simulated)');
  assert.equal(
    label('adjudicated', 'missed', 'red_shot'),
    'Missed (simulated)',
  );
  assert.equal(label('adjudicated', null), WARGAME_MAP_COPY.outcomeHidden);
  assert.equal(label('adjudicated', 'gone'), WARGAME_MAP_COPY.outcomeUnknown);
  assert.equal(label('proposed', null), 'Simulated strike, waiting for you');
  assert.equal(
    label('denied', null, 'red_ground'),
    'Simulated ground fire, denied',
  );
  assert.equal(label('boom', null), WARGAME_MAP_COPY.phaseUnknown);
  assert.equal(
    engagementHasRing({ phase: 'adjudicated', outcome: 'damaged' }),
    true,
  );
  assert.equal(
    engagementHasRing({ phase: 'adjudicated', outcome: null }),
    false,
  );
  assert.equal(
    engagementHasRing({ phase: 'authorized', outcome: null }),
    false,
  );
  assert.equal(OUTCOME_WORDS.destroyed, 'Destroyed');
  const words = [
    ...Object.values(WARGAME_MAP_COPY),
    ...PHASES.flatMap((phase) =>
      OUTCOMES.flatMap((outcome) =>
        ['blue_strike', 'red_shot', 'red_ground', 'x'].map((kind) =>
          label(phase, outcome, kind),
        ),
      ),
    ),
  ];
  for (const text of words) {
    assert.doesNotMatch(text, /·/);
    assert.doesNotMatch(text, /SIMULATED/);
    for (const token of REAL_SYSTEM_TOKENS) assert.doesNotMatch(text, token);
  }
});

test('ring, burst and exposure paint follow the closed vocabularies', () => {
  assert.equal(ringSpec('own_loss').stroke, WARGAME_INK.critical);
  assert.equal(ringSpec('own_damage').stroke, WARGAME_INK.warn);
  assert.equal(ringSpec('red_effect').stroke, WARGAME_INK.pencil);
  assert.equal(ringSpec('<b>').key, 'unknown');
  assert.equal(ringSpec('<b>').stroke, WARGAME_INK.lilac);
  assert.equal(burstSpec({ phase: 'proposed' }).dash, true);
  assert.equal(burstSpec({ phase: 'proposed' }).stroke, WARGAME_INK.sand);
  assert.equal(burstSpec({ phase: 'adjudicated' }).fill, WARGAME_INK.sand);
  assert.equal(burstSpec({ phase: 'expired' }).alpha, 0.5);
  assert.equal(burstSpec({ phase: 'x' }).stroke, WARGAME_INK.lilac);
  assert.equal(exposureInk('low'), WARGAME_INK.pencil);
  assert.equal(exposureInk('moderate'), WARGAME_INK.warn);
  assert.equal(exposureInk('high'), WARGAME_INK.critical);
  assert.equal(exposureInk('extreme'), WARGAME_INK.lilac);
});
