import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CAMERA_DISTANCE,
  FRAME_ELEVATION_DEG,
  ZOOM_MAX,
  ZOOM_MIN,
  centroid,
  createCamera,
  createPickGrid,
  cubicBezier,
  easeIn,
  easeInOutCubic,
  easeOut,
  meanLatLon,
  orientationFacing,
  orientationFraming,
  pullWithin,
  quatFromAxisAngle,
  quatFromTo,
  quatIdentity,
  quatMul,
  quatRotate,
  quatSlerp,
  vecToLatLon,
} from './camera.js';
import { toVector } from './layout.js';

const close = (a, b, eps = 1e-6, msg) =>
  assert.ok(Math.abs(a - b) <= eps, msg ?? `${a} ≈ ${b}`);
const vclose = (a, b, eps = 1e-6) => a.forEach((v, i) => close(v, b[i], eps));

function cameraAt({ cx = 400, cy = 300, r = 200 } = {}) {
  const camera = createCamera();
  camera.setViewport({ cx, cy, r });
  return camera;
}

test('quaternion basics: identity, composition, shortest arc, slerp', () => {
  const v = [0.3, -0.4, 0.866];
  vclose(quatRotate(quatIdentity(), v), v);
  const qx = quatFromAxisAngle(1, 0, 0, Math.PI / 2);
  vclose(quatRotate(qx, [0, 1, 0]), [0, 0, 1]);
  const qy = quatFromAxisAngle(0, 1, 0, Math.PI / 2);
  // (qx·qy) applies qy first.
  vclose(
    quatRotate(quatMul(qx, qy), [0, 0, 1]),
    quatRotate(qx, quatRotate(qy, [0, 0, 1])),
  );
  for (const [u, w] of [
    [
      [1, 0, 0],
      [0, 1, 0],
    ],
    [
      [0, 0, 1],
      [0, 0, -1],
    ],
    [
      [0.6, 0.8, 0],
      [0.6, 0.8, 0],
    ],
  ]) {
    vclose(quatRotate(quatFromTo(u, w), u), w, 1e-6);
  }
  const a = quatIdentity();
  const b = quatFromAxisAngle(0, 0, 1, 1);
  vclose(quatSlerp(a, b, 0), a);
  vclose(quatSlerp(a, b, 1), b);
  vclose(quatSlerp(a, b, 0.5), quatFromAxisAngle(0, 0, 1, 0.5), 1e-9);
});

test('at rest the polar axis tilts 18° toward the viewer', () => {
  const camera = cameraAt();
  const pole = camera.toView([0, 1, 0]);
  close(Math.asin(pole[2]) * (180 / Math.PI), 18, 1e-9);
  assert.ok(
    camera.project([0, 1, 0]).front,
    'the north pole is on the front hemisphere',
  );
  assert.ok(camera.project([0, 1, 0]).y < 300, 'and above the centre');
});

test('the facing point projects to the centre and the limb to the viewport radius', () => {
  const camera = cameraAt();
  const facing = orientationFacing(toVector(0, 0));
  camera.setOrientation(facing);
  const centre = camera.project(toVector(0, 0));
  close(centre.x, 400, 1e-6);
  close(centre.y, 300, 1e-6);
  close(centre.z, 1, 1e-6);
  assert.equal(centre.front, true);
  // The silhouette of a unit sphere seen from D lies at view z = 1/D.
  const limbLon = Math.acos(1 / CAMERA_DISTANCE) * (180 / Math.PI);
  const limb = camera.project(toVector(0, limbLon));
  close(
    limb.x - 400,
    200,
    1e-6,
    `limb at the viewport radius (${limb.x - 400})`,
  );
  close(limb.z, 1 / CAMERA_DISTANCE, 1e-9);
  const back = camera.project(toVector(0, 180));
  assert.equal(back.front, false);
  close(back.x, 400, 1e-6);
  // Zoom scales the projected radius.
  camera.setZoom(2);
  close(camera.project(toVector(0, limbLon)).x - 400, 400, 1e-6);
});

test('orientationFacing brings any point to the centre with north kept up', () => {
  const camera = cameraAt();
  for (const [lat, lon] of [
    [60, 40],
    [-74, -150],
    [6, 179],
    [89, 10],
  ]) {
    const p = toVector(lat, lon);
    camera.setOrientation(orientationFacing(p));
    const view = camera.toView(p);
    vclose(view, [0, 0, 1], 1e-9);
    close(camera.angleFromFront(p), 0, 1e-6);
    // No roll: the north pole stays on the screen's vertical through the centre.
    const pole = camera.project([0, 1, 0]);
    if (Math.abs(lat) < 89) close(pole.x, 400, 1e-6);
  }
  const { lat, lon } = vecToLatLon(toVector(33, -120));
  close(lat, 33, 1e-9);
  close(lon, -120, 1e-9);
});

test('projectAll matches project, packed as (x, y, z, scale)', () => {
  const camera = cameraAt();
  camera.spin(0.7);
  camera.tilt(-0.2);
  const pos = new Float32Array([
    ...toVector(10, 20),
    ...toVector(-40, 100),
    ...toVector(70, -60),
  ]);
  const out = camera.projectAll(pos, 3);
  for (let i = 0; i < 3; i += 1) {
    const p = camera.project([pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]]);
    close(out[i * 4], p.x, 1e-3);
    close(out[i * 4 + 1], p.y, 1e-3);
    close(out[i * 4 + 2], p.z, 1e-5);
    assert.ok(out[i * 4 + 3] > 0.8 && out[i * 4 + 3] < 1.2);
  }
});

test('pick round-trips a projected node, respects its radius and skips the ineligible', () => {
  const camera = cameraAt();
  const pos = new Float32Array([
    ...toVector(0, 0),
    ...toVector(10, 12),
    ...toVector(0, 180),
  ]);
  const proj = camera.projectAll(pos, 3);
  const radii = [6, 12, 6];
  const grid = createPickGrid();
  grid.build(proj, 3, (i) => (proj[i * 4 + 2] >= 0 ? radii[i] : 0));
  assert.equal(grid.pick(proj[0], proj[1]), 0);
  assert.equal(
    grid.pick(proj[4] + 15, proj[5]),
    1,
    'within r + 4 of a 12 px node',
  );
  assert.equal(grid.pick(proj[4] + 17, proj[5]), -1, 'beyond r + 4');
  assert.equal(
    grid.pick(proj[0] + 11, proj[1]),
    -1,
    'beyond max(8, r + 4) for a small node',
  );
  assert.equal(
    grid.pick(proj[0] + 12, proj[1], { touch: true }),
    0,
    '14 px reach on touch',
  );
  assert.equal(
    grid.pick(proj[8], proj[9]),
    -1,
    'back-hemisphere nodes are not pickable',
  );
});

test('the trackball rotates the point under the cursor toward the drag', () => {
  const camera = cameraAt();
  const p = toVector(0, 0);
  const before = camera.project(p);
  camera.trackball(before.x, before.y, before.x + 40, before.y);
  const after = camera.project(p);
  assert.ok(after.x > before.x + 20, `moved right: ${before.x} → ${after.x}`);
  close(after.y, before.y, 8);
  const ball = camera.toBall(400, 300);
  vclose(ball, [0, 0, 1]);
  const far = camera.toBall(4000, 300);
  close(Math.hypot(...far), 1, 1e-9);
});

test('zoom clamps to 0.8–2.4 and spin keeps the tilt', () => {
  const camera = cameraAt();
  assert.equal(camera.setZoom(10), ZOOM_MAX);
  assert.equal(camera.setZoom(0.1), ZOOM_MIN);
  assert.equal(camera.zoomBy(1.5), ZOOM_MIN * 1.5);
  camera.reset();
  const tiltBefore = camera.toView([0, 1, 0])[2];
  camera.spin(1.3);
  close(camera.toView([0, 1, 0])[2], tiltBefore, 1e-9);
  const v0 = camera.version;
  camera.setViewport({ cx: 400, cy: 300, r: 200 });
  assert.equal(camera.version, v0, 'an unchanged viewport is not a change');
  assert.equal(camera.setViewport({ cx: 1, cy: 2, r: -3 }), false);
});

test('easings: endpoints, monotonic, and the spec curves', () => {
  for (const ease of [easeOut, easeIn, easeInOutCubic]) {
    assert.equal(ease(0), 0);
    assert.equal(ease(1), 1);
    let last = 0;
    for (let t = 0.05; t < 1; t += 0.05) {
      const v = ease(t);
      assert.ok(v >= last - 1e-9, 'monotonic');
      last = v;
    }
  }
  assert.ok(easeOut(0.2) > 0.5, 'out is front-loaded');
  assert.ok(easeIn(0.2) < 0.1, 'in is back-loaded');
  close(easeInOutCubic(0.5), 0.5, 1e-12);
  const linear = cubicBezier(0, 0, 1, 1);
  close(linear(0.37), 0.37, 1e-4);
});

test('centroid and spread of a set of directions', () => {
  const c = centroid([toVector(0, -30), toVector(0, 30)]);
  vclose(c.vector, toVector(0, 0), 1e-9);
  close(c.spread, 30, 1e-6);
  assert.equal(centroid([]), null);
  assert.equal(
    centroid([toVector(0, 0), toVector(0, 180)]),
    null,
    'opposite points have no centroid',
  );
});

test('orientationFraming shows a point 12° above the centre, north up', () => {
  assert.equal(FRAME_ELEVATION_DEG, 12);
  const camera = cameraAt();
  for (const [lat, lon] of [
    [60, -75],
    [17, 33],
    [-40, 150],
    [0, 0],
    [85, -170],
  ]) {
    const p = toVector(lat, lon);
    camera.setOrientation(orientationFraming(p));
    const v = camera.toView(p);
    close(Math.asin(v[1]) / (Math.PI / 180), 12, 1e-6);
    close(v[0], 0, 1e-6, 'centred horizontally');
    const north = camera.toView([0, 1, 0]);
    close(north[0], 0, 1e-6, 'no roll: north stays straight up');
    assert.ok(north[1] > 0);
    const p2 = camera.project(p);
    assert.ok(p2.y < camera.viewport.cy, 'above the centre on screen');
  }
  // Elevation 0 is orientationFacing.
  const p = toVector(20, 40);
  vclose(orientationFraming(p, 0), orientationFacing(p));
});

test('meanLatLon: weighted latitude, circular longitude, poles do not steer', () => {
  close(
    meanLatLon([
      { lat: 10, lon: 170 },
      { lat: 30, lon: -170 },
    ]).lat,
    20,
  );
  close(
    Math.abs(
      meanLatLon([
        { lat: 0, lon: 170 },
        { lat: 0, lon: -170 },
      ]).lon,
    ),
    180,
    1e-6,
    'wraps across the antimeridian',
  );
  const weighted = meanLatLon([
    { lat: 0, lon: 0, weight: 3 },
    { lat: 40, lon: 90, weight: 1 },
  ]);
  close(weighted.lat, 10);
  assert.ok(weighted.lon > 0 && weighted.lon < 45, 'pulled toward the heavier');
  // Contacts spread round the belt do not drag the centre to the pole.
  const ring = [0, 60, 120, 180, 240, 300].map((lon) => ({ lat: 10, lon }));
  close(meanLatLon(ring).lat, 10);
  // A point on the pole weighs in latitude but not longitude.
  const withPole = meanLatLon([
    { lat: 90, lon: -120, weight: 1 },
    { lat: 0, lon: 45, weight: 1 },
  ]);
  close(withPole.lat, 45);
  close(withPole.lon, 45, 1e-6);
  // Missing and zero weights count as a small floor, not as nothing.
  close(meanLatLon([{ lat: 20, lon: 0, weight: 0 }]).lat, 20);
  assert.equal(meanLatLon([]), null);
  assert.equal(meanLatLon([{ lat: NaN, lon: 0 }]), null);
});

test('pullWithin moves a centre just far enough toward a point', () => {
  const c = toVector(0, 0);
  const p = toVector(0, 90);
  const moved = pullWithin(c, p, 60);
  const angle = (a, b) =>
    Math.acos(Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])) /
    (Math.PI / 180);
  close(angle(moved, p), 60, 1e-6);
  close(angle(moved, c), 30, 1e-6);
  vclose(pullWithin(c, toVector(0, 40), 60), c); // already close: unchanged
  close(Math.hypot(...moved), 1);
});
