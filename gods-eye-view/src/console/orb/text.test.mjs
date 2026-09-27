import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FEED_LABELS,
  cleanSubtitle,
  feedLabel,
  feedState,
  formatZ,
  nodeLabel,
  nodeSubtitle,
  optionText,
  registerOf,
  splitSegments,
  statusWord,
  threatWord,
  typeWord,
  marginSubtitle,
  marginTitle,
} from './text.js';

test('server strings split on " · " and the console never writes one', () => {
  assert.deepEqual(splitSegments('2K12 Kub · probable · 2 sightings'), [
    '2K12 Kub',
    'probable',
    '2 sightings',
  ]);
  assert.deepEqual(
    splitSegments('Iran — Isfahan'),
    ['Iran — Isfahan'],
    'server dashes are kept verbatim',
  );
  assert.deepEqual(splitSegments(''), []);
  assert.deepEqual(splitSegments(null), []);
  const text = optionText({
    id: 'trk:T-1',
    type: 'track',
    label: 'SA-6 battery',
    subtitle: 'probable · 2 sightings',
    status: 'warn',
    attrs: { confidence: 'probable', threat: 'high' },
  });
  assert.ok(!text.includes('·'));
});

test('times are Zulu', () => {
  const ms = Date.UTC(2026, 8, 27, 14, 2, 51);
  assert.equal(formatZ(ms), '14:02:51Z');
  assert.equal(formatZ(ms, { seconds: false }), '14:02Z');
  assert.equal(formatZ(null), null);
});

test('option text reads like the spec example', () => {
  const node = {
    id: 'trk:T-3fa9c1',
    type: 'track',
    label: 'SA-6 battery',
    subtitle: 'probable · 2 sightings',
    status: 'warn',
    attrs: { confidence: 'probable', threat: 'high' },
  };
  assert.equal(
    optionText(node),
    'SA-6 battery, contact, probable, threat high, estimated, 2 sightings',
  );
  assert.equal(
    optionText({
      ...node,
      status: 'unknown',
      attrs: { confidence: 'possible', threat: 'not assessed' },
      subtitle: 'possible',
    }),
    'SA-6 battery, contact, possible, threat not assessed',
  );
  assert.equal(
    optionText(
      {
        id: 'veh:Drone1',
        type: 'vehicle',
        label: 'Drone1',
        status: 'ok',
        subtitle: 'fuel 64% / BINGO 22% · airborne',
      },
      { isNew: true },
    ),
    'Drone1, vehicle, normal, fuel 64% / BINGO 22%, airborne, new',
  );
});

test('status words: not assessed is never "none", contacts are never "ok"', () => {
  assert.equal(
    threatWord({ type: 'track', status: 'unknown', attrs: {} }),
    'Not assessed',
  );
  assert.equal(
    threatWord({
      type: 'track',
      status: 'unknown',
      attrs: { threat: 'not assessed' },
    }),
    'Not assessed',
  );
  assert.equal(
    statusWord({ type: 'track', status: 'ok', attrs: { threat: 'low' } }),
    'Low',
  );
  assert.equal(
    statusWord({ type: 'track', status: 'stale', attrs: { threat: 'high' } }),
    'Stale',
  );
  assert.equal(statusWord({ type: 'feed', status: 'critical' }), 'Down');
  assert.equal(
    statusWord({ type: 'mission', status: 'ok', attrs: { phase: 'rtb' } }),
    'Returning home',
  );
  assert.equal(statusWord({ type: 'alarm', status: 'ok' }), 'Info');
  assert.equal(typeWord('track'), 'Contact');
  assert.equal(typeWord('mystery'), 'Entity');
});

test('registers are fixed per meaning and "Assumed" only when the server says so', () => {
  assert.equal(
    registerOf({ type: 'track', status: 'warn', attrs: { threat: 'high' } }),
    'Estimated',
  );
  assert.equal(
    registerOf({ type: 'track', status: 'unknown', attrs: {} }),
    'Not assessed',
  );
  assert.equal(
    registerOf({ type: 'vehicle', attrs: { fuel_pct: 60, agl_is_real: true } }),
    'Measured',
  );
  assert.equal(
    registerOf({
      type: 'vehicle',
      attrs: { fuel_pct: 60, agl_is_real: false },
    }),
    'Assumed',
  );
  assert.equal(registerOf({ type: 'vehicle', attrs: {} }), 'No reading');
  assert.equal(registerOf({ type: 'feed', status: 'critical' }), 'No reading');
});

const feed = (name, status, subtitle, extra = {}) => ({
  id: `feed:${name}`,
  type: 'feed',
  label: name.replace(/_/g, ' '),
  status,
  subtitle,
  ...extra,
});

test('feeds get plain names, never the server name', () => {
  assert.equal(feedLabel(feed('contacts', 'ok', '')), 'Contacts feed');
  assert.equal(feedLabel(feed('mission_state', 'ok', '')), 'Mission state');
  assert.equal(feedLabel(feed('detections', 'ok', '')), 'Detections');
  assert.equal(feedLabel(feed('real_data', 'warn', '')), 'Real data');
  assert.equal(feedLabel(feed('sim', 'ok', '')), 'Sim');
  assert.equal(feedLabel(feed('loop_c', 'critical', '')), 'Picture poll');
  assert.equal(feedLabel(feed('weather_feed', 'ok', '')), 'Weather feed');
  assert.equal(nodeLabel(feed('contacts', 'ok', '')), 'Contacts feed');
  assert.equal(
    nodeLabel({ id: 'veh:Drone1', type: 'vehicle', label: 'Drone1' }),
    'Drone1',
  );
  assert.equal(nodeLabel({ id: 'trk:T-1', type: 'track' }), 'trk:T-1');
  for (const label of Object.values(FEED_LABELS))
    assert.match(label, /^[A-Z][a-z]/, 'sentence case');
});

test('feed states are short words; raw sources and errors never show', () => {
  const up = feed('contacts', 'ok', 'mcp:uav_list_tracks');
  assert.equal(feedState(up), 'Up');
  assert.equal(nodeSubtitle(up), 'Up');
  const mission = feed(
    'mission_state',
    'ok',
    'mcp:uav_task_status+mission_status @ http://127.0.0.1:52300/mcp',
  );
  assert.equal(nodeSubtitle(mission), 'Up');
  const sim = feed(
    'sim',
    'critical',
    'down: RPCError: datalink lost: no response from 127.0.0.1:41451 after 3 retries',
    { ts_ms: Date.UTC(2026, 8, 27, 14, 0, 12) },
  );
  assert.equal(feedState(sim), 'Down', 'no known start: just "Down"');
  assert.equal(
    feedState(sim, { downSince: Date.UTC(2026, 8, 27, 14, 0, 12) }),
    'Down since 14:00Z',
  );
  assert.equal(
    feedState(
      feed(
        'real_data',
        'warn',
        'off: AGL is height above the launch datum, LOS is geometric',
      ),
    ),
    'Off',
  );
  assert.equal(
    feedState(
      feed(
        'real_data',
        'warn',
        'enabled, not hydrated: every feed still reads as synthetic',
      ),
    ),
    'Not loaded',
  );
  assert.equal(
    feedState(feed('real_data', 'warn', 'hydrated; degraded: terrain')),
    'Degraded',
  );
  assert.equal(feedState(feed('real_data', 'ok', 'hydrated')), 'Up');
  assert.equal(
    feedState(feed('theater', 'warn', 'active theater unknown')),
    'Unknown',
  );
  assert.equal(feedState(feed('sim', 'warn', 'up (slow)')), 'Degraded');
  assert.equal(feedState(feed('sim', 'stale', '')), 'Stale');
  assert.equal(feedState(feed('sim', 'unknown', '')), 'No reading');
  for (const node of [up, mission, sim]) {
    const text = optionText(node);
    assert.ok(!/mcp:|RPCError|http/.test(text), text);
  }
  assert.equal(optionText(up), 'Contacts feed, feed, up');
  assert.equal(
    optionText(sim, { downSince: Date.UTC(2026, 8, 27, 14, 0, 12) }),
    'Sim, feed, down since 14:00Z',
  );
});

test('subtitles drop plumbing segments and cut long ones', () => {
  assert.equal(
    cleanSubtitle('aaa_self_propelled_9 · probable · 9 sightings'),
    'probable · 9 sightings',
    'bare machine identifiers go',
  );
  assert.equal(cleanSubtitle('mcp:uav_list_tracks'), '');
  assert.equal(cleanSubtitle('ValueError: bad frame · stale'), 'stale');
  assert.equal(
    cleanSubtitle('fuel 64% / BINGO 22% · airborne'),
    'fuel 64% / BINGO 22% · airborne',
  );
  assert.equal(
    cleanSubtitle('Redmond, Washington, USA · active'),
    'Redmond, Washington, USA · active',
  );
  const long = cleanSubtitle('x'.repeat(80));
  assert.equal(long.length, 48);
  assert.ok(long.endsWith('…'));
  assert.equal(cleanSubtitle(null), '');
  assert.equal(
    nodeSubtitle({
      type: 'track',
      subtitle: 'sam_medium_range_1 · probable · 6 sightings',
    }),
    'probable · 6 sightings',
  );
});

test('margin titles keep the part that tells contacts apart (review: "medium-ran…" twice)', () => {
  const fits = (max) => (text) => text.length * 6.5 <= max;
  const sa6 = 'medium-range SAM battery (SA-6/2K12 class)';
  assert.equal(marginTitle(sa6), sa6, 'room enough: the whole name');
  assert.equal(marginTitle(sa6, fits(110)), 'SA-6/2K12 class');
  assert.equal(marginTitle(sa6, fits(76)), 'SA-6/2K12');
  assert.equal(
    marginTitle('long-range SAM system (S-300/SA-10 class)', fits(76)),
    'S-300/SA-10',
    'still the designation when even that is cut by CSS',
  );
  assert.equal(marginTitle(`3 x ${sa6}`, fits(90)), '3 x SA-6/2K12');
  assert.equal(marginTitle('command post / C2 node', fits(76)), 'C2 node');
  assert.equal(
    marginTitle('air-surveillance / acquisition radar', fits(76)),
    'air-surveillance / acquisition radar',
    'a tail that does not fit either leaves the name to the CSS ellipsis',
  );
  assert.equal(
    marginTitle('SAM element (3 contacts)', fits(76)),
    'SAM element (3 contacts)',
  );
  assert.equal(marginTitle(null), '');
});

test('margin subtitles drop the confidence word the ring already shows', () => {
  const track = (confidence, subtitle) => ({
    type: 'track',
    subtitle,
    attrs: { confidence },
  });
  assert.equal(
    marginSubtitle(track('probable', 'probable · 6 sightings')),
    '6 sightings',
  );
  assert.equal(
    marginSubtitle(track(null, 'SA-6_TEL_1 · unrated · 1 sighting')),
    'SA-6_TEL_1 · 1 sighting',
  );
  assert.equal(
    marginSubtitle({
      type: 'unit',
      subtitle: 'probable · 3 co-located contacts',
    }),
    'probable · 3 co-located contacts',
    'only contacts carry the ring',
  );
});

test('a place reads its type word once, never "place, place"', () => {
  const name = optionText({
    id: 'poi:default:East Field',
    type: 'poi',
    label: 'East Field',
    subtitle: 'Redmond (AirSim default)',
  });
  assert.equal(name, 'East Field, place, Redmond (AirSim default)');
  const ob = optionText({
    id: 'ob:mbt',
    type: 'equipment',
    label: 'main battle tank',
  });
  assert.doesNotMatch(ob, /equipment, equipment/);
});
