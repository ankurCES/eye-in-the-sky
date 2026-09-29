import test from 'node:test';
import assert from 'node:assert/strict';

import {
  factsOf,
  fixedLineOf,
  kmText,
  labelOf,
  refType,
  straightText,
  vectorKindWord,
  vectorTitle,
  wargameHeadWords,
  wargameMapRequest,
  wargameTitle,
} from './inspectorWargame.js';

const XSS = '<img src=x onerror=alert(1)>';
const BIDI = '‮evil‬';

function stubDom() {
  const mk = (tag) => ({
    tag,
    children: [],
    attrs: {},
    className: '',
    append(...kids) {
      this.children.push(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = String(v);
    },
  });
  globalThis.document = { createElement: mk };
}

const GRAPH = {
  nodes: [
    { id: 'frc:red-armour-1', type: 'force', label: 'Red armour 1' },
    { id: 'frc:blue-depot-1', type: 'force', label: `${BIDI}Blue depot 1` },
  ],
};

test('pure words: km, kinds, fixed lines and id prefixes', () => {
  assert.equal(kmText(2000), '2 km');
  assert.equal(kmText(12_460), '12.5 km');
  assert.equal(kmText(null), '');
  assert.equal(vectorKindWord('axis'), 'Red axis');
  assert.equal(vectorKindWord('corridor'), 'Planned corridor');
  assert.equal(vectorKindWord('spiral'), 'Vector');
  assert.equal(
    fixedLineOf('force'),
    'Simulated scenario unit. Placed by the wargame, not observed.',
  );
  assert.equal(fixedLineOf('engagement'), 'Simulated. Nothing real was fired.');
  assert.equal(fixedLineOf('vector'), 'Simulated. Computed by the wargame.');
  assert.equal(fixedLineOf('site'), null);
  assert.equal(refType('trk:T-1'), 'track');
  assert.equal(refType('frc:x'), 'force');
  assert.equal(refType('constructor:x'), undefined);
  assert.equal(refType('bare'), undefined);
});

test('facts: the live graph attrs win over the fetched entity fields', () => {
  assert.deepEqual(
    factsOf(
      { attrs: { state: 'damaged' } },
      { fields: { state: 'active', ammo: 3 } },
    ),
    { state: 'damaged', ammo: 3 },
  );
  assert.deepEqual(factsOf(null, null), {});
});

test('titles: a vector names its ends by designator, bidi-safe; an engagement its target', () => {
  const axis = {
    type: 'vector',
    attrs: { kind: 'axis', from: 'frc:red-armour-1', to: 'frc:blue-depot-1' },
  };
  assert.equal(
    vectorTitle(axis, null, GRAPH),
    'Red axis from Red armour 1 to evilBlue depot 1',
  );
  assert.equal(
    vectorTitle(
      { attrs: { kind: 'corridor', from: 'veh:Drone1' } },
      null,
      GRAPH,
    ),
    'Planned corridor from Drone1 to a planned point',
  );
  assert.equal(labelOf(GRAPH, 'frc:missing-1'), 'missing-1');
  assert.equal(
    wargameTitle(
      'engagement',
      { attrs: { kind: 'red_ground', target_label: 'Blue depot 1' } },
      null,
      GRAPH,
    ),
    'Simulated ground fire on Blue depot 1',
  );
  assert.equal(
    wargameTitle('force', { label: `${BIDI}${XSS}` }, null, GRAPH),
    `evil${XSS}`,
  );
  assert.equal(wargameTitle('site', {}, null, GRAPH), null);
});

test('Show on map: a point for forces and engagements, a padded box for a vector', () => {
  const point = wargameMapRequest('frc:x', {
    type: 'force',
    lat: 46.64,
    lon: 32.62,
    label: 'Red SAM 1',
  });
  assert.deepEqual(point.ids, ['frc:x']);
  assert.equal(point.source, 'operator');
  assert.equal(point.countdown, false);
  assert.ok(point.bbox[0] < 46.64 && point.bbox[2] > 46.64);
  const box = wargameMapRequest('vec:cor-1', {
    type: 'vector',
    lat: 46.6,
    lon: 32.6,
    attrs: { to_point: [46.7, 32.8] },
  });
  assert.ok(box.bbox[0] < 46.6 && box.bbox[2] > 46.7);
  assert.ok(box.bbox[1] < 32.6 && box.bbox[3] > 32.8);
  assert.equal(wargameMapRequest('frc:y', { type: 'force' }), null);
  assert.equal(wargameMapRequest('vec:z', { type: 'vector', attrs: {} }), null);
});

test('against the straight route: less or more exposure, longer or shorter', () => {
  assert.equal(straightText(118, 1400), '≈ 118 s less exposure, 1400 m longer');
  assert.equal(straightText(-5, -200), '≈ 5 s more exposure, 200 m shorter');
  assert.equal(straightText(null, undefined), '');
});

test('header words: a force states its side and state with the Scenario tag', () => {
  stubDom();
  const head = wargameHeadWords(
    'force',
    { status: 'critical', attrs: { side: 'red', state: 'active' } },
    null,
  );
  assert.equal(head.sideWord, 'Red');
  assert.equal(head.status, 'Active');
  assert.equal(head.tone, 'critical');
  assert.equal(head.tag.attrs['data-register'], 'scenario');
  const odd = wargameHeadWords('force', { attrs: { side: 'x', state: 'y' } });
  assert.equal(odd.sideWord, 'Side not set');
  assert.equal(odd.status, 'State not recognised');
  assert.equal(odd.tone, 'unknown');
  const eng = wargameHeadWords('engagement', {
    attrs: { phase: 'proposed' },
  });
  assert.equal(eng.status, 'Waiting for you');
  assert.equal(eng.tone, 'sand');
  assert.equal(eng.tag.attrs['data-register'], 'simulated');
});
