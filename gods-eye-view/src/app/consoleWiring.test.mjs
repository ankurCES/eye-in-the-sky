import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

/**
 * Source-level pins for the intelligence-console integration. The files wire
 * a live Cesium viewer, GEV's phases and the page entry, so -- like
 * controlsCockpit.test.mjs and cockpitMarkup.test.mjs -- the wiring is pinned
 * by text; the behaviour behind it is tested in trackingPort.test.mjs,
 * uavAutoStart.test.mjs and uavBridge.test.mjs.
 */
const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8');
const controls = read('./controls.js');
const tools = read('./tools.js');
const standalone = read('../standalone/application.js');
const main = read('../main.js');
const html = read('../../index.html');

test('main.js mounts the console before GEV starts, unless ?console=off', () => {
  assert.match(main, /get\('console'\) !== 'off'/);
  const mount = main.indexOf('mountIntelConsole({');
  const create = main.indexOf('createStandaloneApplication({');
  const start = main.indexOf('application.start()');
  assert.ok(mount > 0 && mount < create && create < start, 'mount first');
  assert.match(main, /trackingPort = createDeferredTrackingPort\(\)/);
  assert.match(main, /config: currentUavBridge\(\)/);
  // The console opens on the orb, so the map starts hidden.
  assert.match(main, /trackingPort\.setMapVisible\(false\)/);
  // A console that fails to mount must not take the map with it.
  assert.match(
    main,
    /catch \(error\) \{[\s\S]*?intelConsole = null;[\s\S]*?trackingPort = null;/,
  );
  assert.match(
    main,
    /intelConsole: trackingPort\s*\?\s*\{ onTrackingPort: \(port\) => trackingPort\.attach\(port\) \}\s*:\s*null/,
  );
});

test('a GEV startup failure reaches the console as well as the loader', () => {
  const failure = main.slice(main.indexOf('application.start().catch'));
  assert.match(failure, /trackingPort\?\.fail\(error\)/);
  assert.match(failure, /reportMapStatus\(\{ state: 'failed'/);
  assert.match(failure, /loaderStatus\.textContent = `Error: \$\{message\}`/);
  assert.match(main, /bus\?\.emit\?\.\('gev:status', status\)/);
});

test('the console option is threaded through the standalone application', () => {
  assert.match(
    standalone,
    /intelConsole = null,\s*firstRun = intelConsole == null,/,
  );
  assert.match(
    standalone,
    /createStandaloneControls\(\{[\s\S]*?intelConsole,\s*\}\)/,
  );
  assert.match(standalone, /createStandaloneTools\(\{[\s\S]*?firstRun,\s*\}\)/);
});

test('first-run suppression is a real option, not unreachable code', () => {
  assert.match(tools, /firstRun = true,/);
  assert.match(
    tools,
    /\.\.\.\(firstRun \? \{\} : \{ initializeWelcome: null \}\)/,
    'startApplicationChrome receives initializeWelcome: null',
  );
  const chrome = read('./startupChrome.js');
  // The reveal is guarded, never short-circuited by a bare `return;`.
  assert.match(
    chrome,
    /if \(disposed \|\| signal\.aborted \|\| firstRun\) return;\n\s*firstRun = initializeWelcome/,
  );
  assert.doesNotMatch(chrome, /gev:first-run-mission:v1/);
});

test('controls.js builds the tracking port and hands it to the console', () => {
  assert.match(controls, /const trackingPort = createTrackingPort\(\{/);
  assert.match(
    controls,
    /getCockpit: \(\) => styleManager\._cockpitCoordinator\?\.cockpitView/,
  );
  assert.match(controls, /missionPanel: uavMissionPanel/);
  assert.match(controls, /keyholeGeometry: getKeyholeGeometry/);
  assert.match(controls, /defer\(\(\) => trackingPort\.destroy\(\)\)/);
  assert.match(controls, /intelConsole\.onTrackingPort\(trackingPort\)/);
  assert.match(
    controls,
    /defer\(\(\) => intelConsole\.onTrackingPort\(null\)\)/,
  );
  // The drawer no longer pops open on every enable while the console rules.
  assert.match(
    controls,
    /if \(!intelConsole && uavLayer && typeof uavLayer\.enable === 'function'\)/,
  );
});

test('the UAV auto-start runs once, from the data phase, through the manager', () => {
  // ObraMaestra put it inside defer(): a TEARDOWN hook, so it never ran.
  assert.doesNotMatch(controls, /defer\(\(\) => \{\s*if \(!uavLayer\) return;/);
  assert.doesNotMatch(controls, /uavLayer\.enable\(\)/);
  assert.doesNotMatch(controls, /gev:initial-share-restore-settled/);
  assert.match(controls, /createUavAutoStart\(\{/);
  assert.match(
    controls,
    /setEnabled\('uav', true, \{\s*origin: 'programmatic',?\s*\}\)/,
  );
  assert.match(controls, /uavAutoStart\.start\(\)/);
  assert.match(controls, /shouldFly: \(\) => !styleManager\.hasShareState/);
  assert.match(tools, /controls\.attachData\?\.\(dataManager\);/);
});

test('one function owns the render loop, and a hidden map suspends it', () => {
  assert.match(
    tools,
    /const hidden = document\.hidden \|\| trackingPort\?\.isMapHidden\?\.\(\) === true;/,
  );
  assert.match(tools, /viewer\.useDefaultRenderLoop = !hidden;/);
  assert.match(
    tools,
    /trackingPort\?\.setRenderSync\?\.\(syncVisibilitySuspension\)/,
  );
  assert.match(tools, /trackingPort\?\.setRenderSync\?\.\(null\)/);
});

test('index.html links the console stylesheet and the Atkinson faces', () => {
  assert.match(
    html,
    /<link rel="stylesheet" href="\/src\/console\/console\.css" \/>/,
  );
  assert.ok(
    html.indexOf('/style.css') < html.indexOf('/src/console/console.css'),
    'console rules come after GEV rules',
  );
  assert.ok(
    html.includes(
      'https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Mono:wght@400;500&family=Atkinson+Hyperlegible+Next:ital,wght@0,400;0,600;0,700;1,400&display=swap',
    ),
  );
  const names = /icon_names=([a-z0-9_,]+)/.exec(html)[1].split(',');
  assert.deepEqual(names, [...names].sort(), 'icon_names stays sorted');
  assert.equal(new Set(names).size, names.length, 'no duplicates');
  // UX spec §11.5: the console's glyphs.
  for (const glyph of [
    'arrow_back',
    'block',
    'bubble_chart',
    'center_focus_weak',
    'check',
    'close',
    'cloud_off',
    'content_copy',
    'error',
    'expand_more',
    'fit_screen',
    'flight_takeoff',
    'forum',
    'gpp_maybe',
    'info',
    'keyboard',
    'link_off',
    'login',
    'manage_search',
    'more_horiz',
    'my_location',
    'open_in_full',
    'pan_tool',
    'pause',
    'play_arrow',
    'refresh',
    'right_panel_close',
    'right_panel_open',
    'route',
    'schedule',
    'search',
    'send',
    'sensors',
    'stop',
    'tune',
    'view_list',
    'warning',
    'zoom_in',
    'zoom_out',
  ])
    assert.ok(names.includes(glyph), `${glyph} is subset`);
});
