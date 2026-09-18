import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

/**
 * `onEnterCockpit` in controls.js is a REPORT, not a request: uavMissionPanel
 * arms a bounded retry when a mission launches and disarms it the instant this
 * says `true`. The UAV entity is created by the layer's poll loop, so the first
 * attempt at LAUNCH+0 fails -- and a version of this callback that returned an
 * unconditional `true` after scheduling a blind setTimeout retry made the panel
 * report "cockpit: following Drone1" while the view never switched.
 *
 * Source-level because controls.js wires a live Cesium viewer, a style manager
 * and a layer catalog; standing that up to assert one return value would cost
 * far more than it proves. Same idiom as cockpitMarkup.test.mjs.
 */
const SRC = readFileSync(new URL('./controls.js', import.meta.url), 'utf8');

function onEnterCockpitBody() {
  const start = SRC.indexOf('onEnterCockpit: (reference) => {');
  assert.notEqual(start, -1, 'controls.js must still wire onEnterCockpit');
  const end = SRC.indexOf('\n    },', start);
  assert.notEqual(end, -1, 'could not find the end of onEnterCockpit');
  return SRC.slice(start, end);
}

test('onEnterCockpit reports the cockpit result rather than assuming it', () => {
  const body = onEnterCockpitBody();
  assert.match(
    body,
    /return cockpit\.enter\(\) === true;/,
    'the success path must return what cockpit.enter() actually said',
  );
  assert.doesNotMatch(
    body,
    /setTimeout/,
    'no blind retry here -- uavMissionPanel owns the bounded, loud retry',
  );
  const returns = body.match(/return [^;]+;/g) ?? [];
  assert.deepEqual(
    returns.filter((r) => /return true;/.test(r)),
    [],
    'an unconditional `return true` disarms the panel retry on a failure',
  );
});
