import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CLASS_META,
  ICON,
  SENSOR_TOOLS,
  STALING_TOOLS,
  ago,
  approvalVehicle,
  approveVerb,
  bidiSafe,
  bytes,
  classApprovable,
  classKey,
  coord,
  countdown,
  duration,
  epochMs,
  escapeBidi,
  isDryRunnable,
  kindWords,
  latestSentence,
  linkWord,
  listWords,
  missionKind,
  nodeTypeOf,
  policyLine,
  segments,
  stripBidi,
  tokens,
  toolTitle,
  truncate,
  usd,
  warningText,
  wholeSeconds,
  withArticle,
  zulu,
} from './format.js';

test('Zulu times: minutes for messages, seconds for records, empty when unknown', () => {
  const t = Date.UTC(2026, 8, 27, 14, 2, 51);
  assert.equal(zulu(t), '14:02Z');
  assert.equal(zulu(t, { seconds: true }), '14:02:51Z');
  assert.equal(zulu(null), '');
  assert.equal(zulu(NaN), '');
});

test('durations and counters', () => {
  assert.equal(duration(400), '0.4 s');
  assert.equal(duration(6200), '6.2 s');
  assert.equal(duration(12_000), '12 s');
  assert.equal(duration(125_000), '2 min 5 s');
  assert.equal(duration(120_000), '2 min');
  assert.equal(duration(3_900_000), '1 h 5 min');
  assert.equal(duration(-1), '');
  assert.equal(wholeSeconds(12_900), '12 s');
  assert.equal(countdown(588_000), '9:48');
  assert.equal(countdown(-5), '0:00');
  assert.equal(ago(0, 12_000), '12 s ago');
  assert.equal(ago(0, 60_000), '1 min ago');
  assert.equal(
    ago(0, 289_000),
    '4 min ago',
    'whole minutes, as the rail counts',
  );
  assert.equal(ago(0, 7_200_000), '2 h ago');
  assert.equal(epochMs(1_789_000_000), 1_789_000_000_000, 'unix seconds');
  assert.equal(epochMs(1_789_000_000_000), 1_789_000_000_000, 'already ms');
  assert.equal(epochMs('x'), null);
});

test('numbers: tokens, dollars, bytes, coordinates', () => {
  assert.equal(tokens(410), '410');
  assert.equal(tokens(3100), '3.1k');
  assert.equal(tokens(3000), '3k');
  assert.equal(tokens(1_200_000), '1.2M');
  assert.equal(usd(0.018), '$0.02');
  assert.equal(bytes(3277), '3.2 KB');
  assert.equal(bytes(10), '10 B');
  assert.equal(coord(-122.1402), '−122.14020');
  assert.equal(coord(47.6445), '47.64450');
});

test('server strings split on " · " and the console never adds one', () => {
  assert.deepEqual(segments('Drone1 · AO polygon · 60 m AGL · dry run'), [
    'Drone1',
    'AO polygon',
    '60 m AGL',
    'dry run',
  ]);
  assert.deepEqual(segments(''), []);
  assert.deepEqual(segments(null), []);
  assert.deepEqual(
    segments('a·b'),
    ['a·b'],
    'only the spaced separator splits',
  );
});

test('unknown classes fail safe to a non-approvable "unknown" (WG D7 #8)', () => {
  assert.equal(classKey('sensor'), 'sensor');
  // Replaces the old fail-closed-to-command pin: a new server class (e.g.
  // `engagement`) must never become an approvable command slip.
  for (const klass of [
    'mystery',
    'engagement',
    'toString',
    '__proto__',
    'hasOwnProperty',
    '',
    null,
    undefined,
    42,
    { toString: () => 'command' },
  ]) {
    assert.equal(classKey(klass), 'unknown', String(klass));
    assert.equal(classApprovable(klass), false, String(klass));
  }
  assert.deepEqual(
    { ...CLASS_META.unknown },
    {
      phrase: 'Unrecognised action',
      icon: ICON.warning,
      slip: true,
      approvable: false,
    },
  );
  for (const k of [
    'read',
    'plan',
    'sensor',
    'command',
    'sim',
    'safety_override',
  ])
    assert.equal(classApprovable(k), true, k);
  assert.equal(CLASS_META.command.phrase, 'Commands an aircraft');
  assert.equal(CLASS_META.safety_override.icon, ICON.override);
  assert.equal(CLASS_META.read.slip, false);
  assert.equal(approveVerb('x', 'unknown'), '');
  assert.equal(policyLine('unknown'), '');
});

test('Phase A tools: titles, verbs, policy line, staling and the sit prefix', () => {
  assert.equal(toolTitle('geo_lookup'), 'Look up a place');
  assert.equal(toolTitle('geo_sites'), 'List mapped sites');
  assert.equal(toolTitle('theater_propose'), 'Propose a theater');
  assert.equal(toolTitle('sim_set_theater'), 'Set the theater');
  assert.equal(toolTitle('sim_set_time_scale'), 'Set sim speed');
  assert.equal(toolTitle('ui_show_map'), 'Show on the map');
  assert.equal(approveVerb('sim_set_theater', 'sim'), 'Approve theater change');
  assert.equal(approveVerb('sim_set_time_scale', 'sim'), 'Approve change');
  assert.equal(
    policyLine('sim', 'sim_set_theater'),
    'Theater changes are approved one at a time.',
  );
  assert.equal(
    policyLine('sim', 'sim_set_time_scale'),
    'Simulation changes are approved one at a time in this version.',
  );
  assert.ok(STALING_TOOLS.includes('sim_set_theater'));
  assert.equal(nodeTypeOf('sit:dyn-x:way/1'), 'site');
});

test('approve verbs per tool and class', () => {
  const cases = {
    mission_grid_search: 'Approve launch',
    uav_mission: 'Approve launch',
    uav_takeoff: 'Approve takeoff',
    uav_land: 'Approve landing',
    uav_return_to_home: 'Approve return',
    uav_goto_gps: 'Approve flight',
    uav_fly_route: 'Approve flight',
    uav_orbit_poi: 'Approve flight',
    uav_hover: 'Approve flight',
    mission_cancel: 'Approve cancel',
    uav_abort: 'Approve abort',
    uav_handoff_target: 'Approve handoff',
    unknown_tool: 'Approve command',
  };
  for (const [tool, verb] of Object.entries(cases))
    assert.equal(approveVerb(tool, 'command'), verb, tool);
  assert.equal(approveVerb('uav_scan_targets', 'sensor'), 'Approve scan');
  assert.equal(
    approveVerb('uav_set_gimbal', 'sensor'),
    'Approve sensor tasking',
  );
  assert.equal(approveVerb('sim_set_weather', 'sim'), 'Approve change');
  assert.equal(
    approveVerb('sim_set_fuel', 'safety_override'),
    'Approve override',
  );
});

test('policy lines per class', () => {
  assert.equal(policyLine('command'), 'Commands are approved one at a time.');
  assert.equal(
    policyLine('sim'),
    'Simulation changes are approved one at a time in this version.',
  );
  assert.equal(
    policyLine('safety_override'),
    'Overrides are approved one at a time.',
  );
  assert.equal(policyLine('sensor'), '');
});

test('mission kinds, vehicles and dry-runnability mirror the server policy', () => {
  assert.equal(missionKind('mission_grid_search', {}), 'grid_search');
  assert.equal(missionKind('mission_dry_run', {}), 'grid_search');
  assert.equal(
    missionKind('mission_dry_run', { kind: 'Recon_Route' }),
    'recon_route',
  );
  assert.equal(missionKind('uav_mission', { kind: 'track' }), 'track_target');
  assert.equal(missionKind('uav_mission', {}), null);
  assert.equal(missionKind('uav_takeoff', {}), null);
  assert.equal(kindWords('grid_search'), 'grid search');
  assert.equal(kindWords('track'), 'contact track');
  assert.equal(kindWords('new_kind'), 'new kind');
  assert.equal(
    approvalVehicle({
      tool: 'mission_handoff_track',
      args: { vehicle: 'A', to_vehicle: 'B' },
    }),
    'B',
  );
  assert.equal(
    approvalVehicle({
      tool: 'uav_takeoff',
      args: { vehicle: 'A' },
      vehicle: 'C',
    }),
    'C',
  );
  assert.equal(approvalVehicle({ tool: 'uav_takeoff', args: {} }), null);
  assert.equal(isDryRunnable({ tool: 'mission_grid_search' }), true);
  assert.equal(
    isDryRunnable({ tool: 'mission_cancel' }),
    false,
    'mission_* is not enough',
  );
  assert.equal(isDryRunnable({ tool: 'uav_orbit_poi' }), true);
  assert.equal(
    isDryRunnable({ tool: 'uav_orbit_poi', args: { dry_run: true } }),
    false,
  );
  assert.equal(
    isDryRunnable({ tool: 'uav_takeoff', dry_runnable: true }),
    true,
  );
  assert.equal(SENSOR_TOOLS.length, 5);
  assert.equal(toolTitle('uav_set_fov'), 'Set field of view');
  assert.equal(toolTitle('other'), 'other');
  assert.equal(nodeTypeOf('trk:T-1'), 'track');
  assert.equal(nodeTypeOf('ob:sa6'), 'equipment');
  assert.equal(nodeTypeOf('zzz:1'), 'unknown');
});

test('text helpers', () => {
  assert.equal(listWords(['a']), 'a');
  assert.equal(listWords(['a', 'b', 'c']), 'a, b and c');
  assert.equal(truncate('abcdef', 4), 'abc…');
  assert.equal(
    latestSentence('First thought. Second one is here.'),
    'Second one is here.',
  );
  assert.equal(latestSentence(''), '');
  assert.equal(
    warningText('gate_start_assumed_home: priced from home'),
    'Priced from home',
  );
  assert.equal(warningText('wind unknown'), 'Wind unknown');
  assert.equal(linkWord('up'), 'Link up');
  assert.equal(linkWord('loal'), 'Link lost');
  assert.equal(linkWord(null), '');
});

test('withArticle picks "an" before a vowel sound (live: "a orbit")', () => {
  assert.equal(withArticle('orbit'), 'an orbit');
  assert.equal(withArticle('grid search'), 'a grid search');
  assert.equal(withArticle('route recon'), 'a route recon');
  assert.equal(withArticle('Orbit'), 'an Orbit');
  assert.equal(withArticle(''), '');
  assert.equal(withArticle(null), '');
});

test('bidi controls never reach the screen (review: an OSM name "BMP \u202eYLDNEIRF\u202c convoy" read "BMP FRIENDLY convoy")', () => {
  const spoof = 'BMP \u202eYLDNEIRF\u202c convoy';
  assert.equal(stripBidi(spoof), 'BMP YLDNEIRF convoy');
  // Every embedding, override and isolate control goes.
  const all = '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069';
  assert.equal(stripBidi(`a${all}b`), 'ab');
  // Letters, RTL script and marks that are not controls stay.
  assert.equal(stripBidi('שלום Drone1 \u200f'), 'שלום Drone1 \u200f');
  assert.equal(stripBidi(null), null);
  assert.equal(stripBidi(42), 42);
  // The exact request shows the control as its JSON escape.
  assert.equal(
    escapeBidi(JSON.stringify({ vehicle: 'Drone1\u202e' })),
    '{"vehicle":"Drone1\\u202e"}',
  );
  assert.deepEqual(JSON.parse(escapeBidi(JSON.stringify({ v: 'x\u2066' }))), {
    v: 'x\u2066',
  });
  // The wrapped element factory cleans string children and text attributes,
  // and leaves ids, data attributes and nodes alone.
  const calls = [];
  const h = bidiSafe((tag, attrs, ...kids) => {
    calls.push({ tag, attrs, kids });
    return { tag };
  });
  const node = { tag: 'span' };
  h(
    'p',
    {
      title: 'Orbit \u202eynneD',
      'aria-label': '\u2067x',
      'data-id': 'trk:\u202e1',
    },
    'Approval waiting: \u202eynneD',
    node,
    null,
  );
  assert.deepEqual(calls[0].attrs, {
    title: 'Orbit ynneD',
    'aria-label': 'x',
    'data-id': 'trk:\u202e1',
  });
  assert.deepEqual(calls[0].kids, ['Approval waiting: ynneD', node, null]);
  h('br');
  assert.deepEqual(calls[1].attrs, {});
});
