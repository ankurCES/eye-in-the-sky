import test from 'node:test';
import assert from 'node:assert/strict';

import { LIST_COLUMNS, createOrbTwin, groupByBand, sortRows } from './a11y.js';
import { createOrbListView } from './orb.js';
import {
  findAll,
  findNode,
  installStubDocument,
  makeGraph,
  stubElement,
} from './fixtures.test.mjs';

let dom;
test.beforeEach(() => {
  dom = installStubDocument();
});
test.afterEach(() => {
  dom.restore();
});

const text = (el) =>
  (el.textContent || '') +
  (el.children || [])
    .map((c) => (typeof c === 'string' ? c : text(c)))
    .join('');

test('groups run pole to pole, most salient first', () => {
  const groups = groupByBand(makeGraph(60).nodes);
  const keys = groups.map((g) => g.key);
  assert.deepEqual(keys.slice(0, 5), [
    'theater',
    'feed',
    'poi',
    'vehicle',
    'mission',
  ]);
  assert.ok(keys.indexOf('track') < keys.indexOf('alarm'));
  for (const group of groups) {
    for (let k = 1; k < group.nodes.length; k += 1) {
      assert.ok(group.nodes[k - 1].salience >= group.nodes[k].salience);
    }
  }
});

test('the twin keeps option elements (and the active descendant) across polls', () => {
  const host = stubElement('div');
  const twin = createOrbTwin(host);
  const graph = makeGraph(30);
  twin.setEntities(graph.nodes);
  const box = host.children[0];
  assert.equal(box.attrs.role, 'listbox');
  const active = twin.step(1);
  const option = findNode(
    box,
    (el) => el.attrs?.id === box.attrs['aria-activedescendant'],
  );
  assert.ok(option);
  twin.setEntities(graph.nodes.slice());
  const again = findNode(
    box,
    (el) => el.attrs?.id === box.attrs['aria-activedescendant'],
  );
  assert.equal(again, option, 'same element after a re-render');
  assert.equal(twin.active, active);
  twin.setEntities(graph.nodes.filter((n) => n.id !== active));
  assert.equal(twin.active, null, 'the active entity left the picture');
  assert.equal(box.attrs['aria-activedescendant'], undefined);
  // Band jumps land on each band's first entity.
  twin.setEntities(graph.nodes);
  const firstOfBands = groupByBand(graph.nodes).map((g) => g.nodes[0].id);
  twin.setActive(null);
  assert.equal(twin.stepBand(1), firstOfBands[0]);
  assert.equal(twin.stepBand(1), firstOfBands[1]);
  assert.equal(twin.stepBand(-1), firstOfBands[0]);
  twin.destroy();
  assert.equal(host.children.length, 0);
});

test('List view: a sortable table with type, name, status, register, salience, last seen', () => {
  const host = stubElement('div');
  const picked = [];
  const view = createOrbListView(host, { onSelect: (id) => picked.push(id) });
  const graph = makeGraph(40);
  assert.equal(view.setGraph(graph), graph.nodes.length);
  const headers = findAll(host, (el) => el.tag === 'th');
  assert.deepEqual(
    headers.map((th) => text(th)),
    LIST_COLUMNS.map((c) => c.label),
  );
  assert.equal(headers[0].attrs['aria-sort'], 'ascending');
  const rows = findAll(host, (el) => el.className === 'ic-orb-list__row');
  assert.equal(
    rows[0].attrs['data-id'],
    'thr:default',
    'band order by default',
  );
  // Sort by salience, then reverse.
  view.sort('salience');
  let values = findAll(
    host,
    (el) => el.className === 'ic-orb-list__td ic-orb-list__num',
  )
    .filter((_, k) => k % 2 === 0)
    .map((el) => Number(text(el)));
  for (let k = 1; k < values.length; k += 1)
    assert.ok(values[k - 1] <= values[k]);
  view.sort('salience');
  assert.equal(view.sortState.dir, 'descending');
  values = findAll(
    host,
    (el) => el.className === 'ic-orb-list__td ic-orb-list__num',
  )
    .filter((_, k) => k % 2 === 0)
    .map((el) => Number(text(el)));
  for (let k = 1; k < values.length; k += 1)
    assert.ok(values[k - 1] >= values[k]);
  assert.equal(
    findAll(host, (el) => el.tag === 'th')[4].attrs['aria-sort'],
    'descending',
  );
  // Times are Zulu, and missing ones are never invented.
  const seen = findAll(
    host,
    (el) => el.className === 'ic-orb-list__td ic-orb-list__num',
  )
    .filter((_, k) => k % 2 === 1)
    .map(text);
  assert.ok(seen.some((t) => /^\d\d:\d\d:\d\dZ$/.test(t)));
  assert.ok(seen.includes('Not recorded'));
  // Activating a name selects.
  const open = findNode(host, (el) => el.className === 'ic-orb-list__open');
  open.fire('click');
  assert.equal(picked.length, 1);
  // Filtering, and the empty states.
  assert.equal(
    view.filter((node) => node.type === 'alarm'),
    graph.nodes.filter((n) => n.type === 'alarm').length,
  );
  assert.equal(
    view.filter(() => false),
    0,
  );
  const empty = findNode(host, (el) => el.className === 'ic-orb-list__empty');
  assert.equal(empty.textContent, 'Nothing matches this filter.');
  assert.equal(empty.attrs.hidden, undefined, 'the message shows');
  view.filter(null);
  assert.equal(empty.attrs.hidden, '', 'and hides again when rows return');
  view.setGraph(null);
  assert.equal(empty.textContent, 'Nothing in the picture yet.');
  view.destroy();
  assert.equal(host.children.length, 0);
});

test('status and register columns never read "none" or green for contacts', () => {
  const rows = sortRows(makeGraph(80).nodes, 'status');
  assert.ok(rows.length === 80);
  const host = stubElement('div');
  const view = createOrbListView(host);
  view.setGraph({
    nodes: [
      { id: 'trk:a', type: 'track', label: 'A', status: 'unknown', attrs: {} },
    ],
  });
  const status = findNode(host, (el) =>
    /ic-orb-list__status/.test(el.className),
  );
  assert.equal(text(status), 'Not assessed');
  assert.equal(status.attrs['data-status'], 'unknown');
  const register = findNode(host, (el) =>
    /ic-orb-list__register/.test(el.className),
  );
  assert.equal(text(register), 'Not assessed');
});

test('List view and twin name feeds plainly and keep raw errors out', () => {
  const host = stubElement('div');
  const view = createOrbListView(host);
  const nodes = [
    {
      id: 'feed:contacts',
      type: 'feed',
      label: 'Contacts',
      subtitle: 'mcp:uav_list_tracks',
      status: 'ok',
      salience: 0.2,
    },
    {
      id: 'feed:sim',
      type: 'feed',
      label: 'Sim',
      subtitle: 'down: RPCError: datalink lost',
      status: 'critical',
      salience: 0.7,
    },
    {
      id: 'trk:T-1',
      type: 'track',
      label: 'SA-6 battery',
      subtitle: 'sam_medium_range_1 · probable · 6 sightings',
      status: 'warn',
      salience: 0.8,
      attrs: { confidence: 'probable', threat: 'high' },
    },
  ];
  view.setGraph({ nodes });
  const all = text(host);
  assert.ok(all.includes('Contacts feed'));
  assert.ok(all.includes('Up') && all.includes('Down'));
  assert.ok(!/mcp:|RPCError|sam_medium_range_1/.test(all), all);
  view.sort('label');
  const names = findAll(host, (el) => el.className === 'ic-orb-list__open').map(
    text,
  );
  assert.deepEqual(names, ['Contacts feed', 'SA-6 battery', 'Sim']);
  const twinHost = stubElement('div');
  const twin = createOrbTwin(twinHost);
  twin.setEntities(
    nodes,
    () => false,
    (id) =>
      id === 'feed:sim' ? { downSince: Date.UTC(2026, 8, 27, 9, 5) } : null,
  );
  const options = findAll(twinHost, (el) => el.attrs?.role === 'option').map(
    (el) => el.textContent,
  );
  assert.ok(options.includes('Sim, feed, down since 09:05Z'));
  assert.ok(options.includes('Contacts feed, feed, up'));
  twin.destroy();
});

test('List view keeps keyboard focus on a row across graph polls', () => {
  // Review: every poll rebuilt the rows and dropped focus to <body>.
  const make = dom.doc.createElement;
  dom.doc.activeElement = dom.doc.body;
  dom.doc.createElement = (tag) => {
    const el = make(tag);
    el.focus = () => {
      dom.doc.activeElement = el;
    };
    return el;
  };
  const host = stubElement('div');
  const view = createOrbListView(host);
  const graph = makeGraph(30);
  view.setGraph(graph);
  const openOf = (id) =>
    findNode(
      findNode(host, (el) => el.attrs?.['data-id'] === id),
      (el) => el.className === 'ic-orb-list__open',
    );
  const id = 'veh:Drone1';
  const button = openOf(id);
  button.focus();
  // Same nodes (a fresh object, as a poll delivers): nothing is rebuilt.
  view.setGraph(structuredClone(graph));
  assert.equal(openOf(id), button, 'unchanged rows keep their elements');
  assert.equal(dom.doc.activeElement, button);
  // A changed picture rebuilds the rows; focus follows the entity.
  const changed = structuredClone(graph);
  changed.nodes.find((n) => n.id === id).salience = 0.11;
  view.setGraph(changed);
  const rebuilt = openOf(id);
  assert.notEqual(rebuilt, button, 'rows were rebuilt');
  assert.equal(dom.doc.activeElement, rebuilt, 'focus is on the new row');
  // The entity leaves the picture: focus lands on the first sort button.
  const gone = structuredClone(changed);
  gone.nodes = gone.nodes.filter((n) => n.id !== id);
  view.setGraph(gone);
  assert.equal(openOf(id), null);
  assert.equal(dom.doc.activeElement.className, 'ic-orb-list__sort');
  // Focus elsewhere on the page is never moved by a poll.
  const outside = make('input');
  dom.doc.activeElement = outside;
  view.setGraph(changed);
  assert.equal(dom.doc.activeElement, outside);
});

test('List view: an unknown type reads "Unrecognised (strike_package)" in lilac; sites sit in their own band (WG §4.2.1, §4.2.6)', () => {
  const host = stubElement('div');
  const view = createOrbListView(host);
  const graph = makeGraph(20);
  graph.nodes.push(
    {
      id: 'stk:1',
      type: 'strike_package',
      label: '<img src=x onerror=alert(1)>',
      status: 'ok',
      salience: 0.6,
      attrs: {},
    },
    {
      id: 'sit:dyn-x:way/1',
      type: 'site',
      label: 'Kherson International',
      group: 'air',
      status: 'critical',
      salience: 0.4,
      attrs: { category: 'airfield' },
    },
  );
  view.setGraph(graph);
  const row = (id) =>
    findNode(host, (el) => el.attrs?.['data-id'] === id && el.tag === 'tr');
  const force = row('stk:1');
  assert.equal(text(force.children[0]), 'Unrecognised (strike_package)');
  assert.match(force.children[0].className, /ic-orb-list__type--unrecognised/);
  assert.equal(force.children[2].attrs['data-status'], 'unknown');
  assert.equal(text(force.children[2]), 'Not assessed');
  assert.ok(text(force.children[1]).includes('<img src=x onerror=alert(1)>'));
  assert.equal(
    findNode(host, (el) => el.tag === 'img'),
    null,
  );
  const site = row('sit:dyn-x:way/1');
  assert.equal(text(site.children[0]), 'Site');
  assert.equal(site.children[2].attrs['data-status'], 'mapped');
  assert.equal(text(site.children[2]), 'Mapped, not verified');
  assert.equal(text(site.children[3]), 'Mapped');
  // Band order: sites between missions and contacts, the unknown in Other.
  const groups = groupByBand(graph.nodes).map((g) => g.key);
  assert.ok(groups.indexOf('site') > groups.indexOf('mission'));
  assert.ok(groups.indexOf('site') < groups.indexOf('track'));
  assert.equal(
    groupByBand(graph.nodes).find((g) => g.key === 'other').nodes[0].id,
    'stk:1',
  );
});
