/**
 * Pure camera math for the information orb (UX spec §4.1, §4.6, §11.1).
 *
 * The orb is a unit sphere seen from 3.2 R with mild perspective. Its polar
 * axis tilts 18° toward the viewer at rest (the empty picture); a populated
 * picture opens on its own latitudes instead (orientationFraming, and
 * framingOrientation in orb.js). Orientation is one quaternion
 * (orb-local → view space); the view looks down −z from +z, screen y points
 * down. Nothing here touches the DOM, so every function runs under node:test.
 *
 * Depth convention (spec §4.1, §4.5): projections report the view-space `z`
 * of the unit vector, 1 facing the camera and −1 directly behind. A node is on
 * the front hemisphere when z ≥ 0; labels need z > 0.2. (With perspective the
 * true silhouette sits at z = 1/3.2; the spec's hemisphere split is kept so
 * the north pole, tilted 18° toward the viewer, reads as front.)
 */

const DEG = Math.PI / 180;

export const CAMERA_DISTANCE = 3.2;
export const TILT_DEG = 18;
export const ZOOM_MIN = 0.8;
export const ZOOM_MAX = 2.4;
/**
 * Default and reset views put the populated latitudes' centroid this far
 * above the centre of the view (degrees of view elevation).
 */
export const FRAME_ELEVATION_DEG = 12;
/** Nodes more than this far off centre ease to the front on select. */
export const EASE_THRESHOLD_DEG = 50;
/** Screen-space pick grid cell. */
export const PICK_CELL_PX = 32;

// ---- quaternions [w, x, y, z] ------------------------------------------------

export function quatIdentity() {
  return [1, 0, 0, 0];
}

export function quatFromAxisAngle(ax, ay, az, rad) {
  const len = Math.hypot(ax, ay, az) || 1;
  const s = Math.sin(rad / 2) / len;
  return [Math.cos(rad / 2), ax * s, ay * s, az * s];
}

/** Hamilton product a·b (apply b first, then a). */
export function quatMul(a, b) {
  return [
    a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
    a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
    a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
    a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0],
  ];
}

export function quatNormalize(q) {
  const len = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
  return [q[0] / len, q[1] / len, q[2] / len, q[3] / len];
}

export function quatSlerp(a, b, t) {
  let bw = b[0];
  let bx = b[1];
  let by = b[2];
  let bz = b[3];
  let dot = a[0] * bw + a[1] * bx + a[2] * by + a[3] * bz;
  if (dot < 0) {
    dot = -dot;
    bw = -bw;
    bx = -bx;
    by = -by;
    bz = -bz;
  }
  if (dot > 0.9995) {
    return quatNormalize([
      a[0] + (bw - a[0]) * t,
      a[1] + (bx - a[1]) * t,
      a[2] + (by - a[2]) * t,
      a[3] + (bz - a[3]) * t,
    ]);
  }
  const theta = Math.acos(dot);
  const s = Math.sin(theta);
  const wa = Math.sin((1 - t) * theta) / s;
  const wb = Math.sin(t * theta) / s;
  return [
    a[0] * wa + bw * wb,
    a[1] * wa + bx * wb,
    a[2] * wa + by * wb,
    a[3] * wa + bz * wb,
  ];
}

/** Row-major 3×3 rotation matrix. */
export function quatToMat3(q, out = new Float64Array(9)) {
  const [w, x, y, z] = q;
  out[0] = 1 - 2 * (y * y + z * z);
  out[1] = 2 * (x * y - w * z);
  out[2] = 2 * (x * z + w * y);
  out[3] = 2 * (x * y + w * z);
  out[4] = 1 - 2 * (x * x + z * z);
  out[5] = 2 * (y * z - w * x);
  out[6] = 2 * (x * z - w * y);
  out[7] = 2 * (y * z + w * x);
  out[8] = 1 - 2 * (x * x + y * y);
  return out;
}

export function quatRotate(q, v) {
  const m = quatToMat3(q);
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}

/** Shortest-arc rotation taking unit vector u onto unit vector v. */
export function quatFromTo(u, v) {
  const dot = u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
  if (dot < -0.999999) {
    const axis = Math.abs(u[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
    const c = [
      u[1] * axis[2] - u[2] * axis[1],
      u[2] * axis[0] - u[0] * axis[2],
      u[0] * axis[1] - u[1] * axis[0],
    ];
    return quatFromAxisAngle(c[0], c[1], c[2], Math.PI);
  }
  return quatNormalize([
    1 + dot,
    u[1] * v[2] - u[2] * v[1],
    u[2] * v[0] - u[0] * v[2],
    u[0] * v[1] - u[1] * v[0],
  ]);
}

// ---- easing -------------------------------------------------------------------

/** CSS cubic-bezier timing function, solved for x with Newton + bisection. */
export function cubicBezier(x1, y1, x2, y2) {
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const sampleX = (t) => ((ax * t + bx) * t + cx) * t;
  const sampleY = (t) => ((ay * t + by) * t + cy) * t;
  const slopeX = (t) => (3 * ax * t + 2 * bx) * t + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i += 1) {
      const err = sampleX(t) - x;
      if (Math.abs(err) < 1e-6) return sampleY(t);
      const d = slopeX(t);
      if (Math.abs(d) < 1e-6) break;
      t -= err / d;
    }
    let lo = 0;
    let hi = 1;
    t = x;
    for (let i = 0; i < 30; i += 1) {
      const v = sampleX(t);
      if (Math.abs(v - x) < 1e-6) break;
      if (v < x) lo = t;
      else hi = t;
      t = (lo + hi) / 2;
    }
    return sampleY(t);
  };
}

/** Spec easings: out, in, in-out (easeInOutCubic). */
export const easeOut = cubicBezier(0.2, 0, 0, 1);
export const easeIn = cubicBezier(0.4, 0, 1, 1);
export function easeInOutCubic(t) {
  if (t <= 0) return 0;
  if (t >= 1) return 1;
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

// ---- geometry helpers ------------------------------------------------------------

export function vecToLatLon(v) {
  const len = Math.hypot(v[0], v[1], v[2]) || 1;
  return {
    lat: Math.asin(Math.max(-1, Math.min(1, v[1] / len))) / DEG,
    lon: Math.atan2(v[0], v[2]) / DEG,
  };
}

/**
 * Roll-free orientation that puts the orb-local unit vector `p` at the centre
 * front, north kept up: X(lat)·Y(−lon).
 */
export function orientationFacing(p) {
  const { lat, lon } = vecToLatLon(p);
  return quatNormalize(
    quatMul(
      quatFromAxisAngle(1, 0, 0, lat * DEG),
      quatFromAxisAngle(0, 1, 0, -lon * DEG),
    ),
  );
}

/**
 * Roll-free orientation that shows the orb-local unit vector `p` slightly
 * above the centre of the view (`elevationDeg` of view elevation), north
 * kept up: X(lat − elevation)·Y(−lon). With elevation 0 this is
 * orientationFacing(p).
 */
export function orientationFraming(p, elevationDeg = FRAME_ELEVATION_DEG) {
  const { lat, lon } = vecToLatLon(p);
  return quatNormalize(
    quatMul(
      quatFromAxisAngle(1, 0, 0, (lat - elevationDeg) * DEG),
      quatFromAxisAngle(0, 1, 0, -lon * DEG),
    ),
  );
}

/**
 * Where a set of weighted lat/lon points sits on the orb, as one lat/lon:
 * latitude is the weighted mean latitude; longitude is the weighted circular
 * mean, each point counted by cos(latitude) (a point near a pole says little
 * about longitude). On this categorical sphere a plain vector centroid would
 * not do: contacts spread over several sectors cancel out horizontally and
 * the centroid collapses toward the pole. Non-finite or tiny weights count as
 * `floor`. Null for no points.
 * @param {Array<{lat:number, lon:number, weight?:number}>} points degrees
 * @param {number} [floor]
 * @returns {{lat:number, lon:number}|null}
 */
export function meanLatLon(points, floor = 0.05) {
  let wSum = 0;
  let latSum = 0;
  let sx = 0;
  let sz = 0;
  let best = null;
  for (const point of points || []) {
    const lat = Number(point?.lat);
    const lon = Number(point?.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const raw = Number(point.weight);
    const w = Number.isFinite(raw) && raw > floor ? raw : floor;
    wSum += w;
    latSum += w * lat;
    const c = w * Math.cos(lat * DEG);
    sx += c * Math.sin(lon * DEG);
    sz += c * Math.cos(lon * DEG);
    if (!best || c > best.c) best = { c, lon };
  }
  if (!wSum) return null;
  const lon =
    Math.hypot(sx, sz) > 1e-6 ? Math.atan2(sx, sz) / DEG : (best?.lon ?? 0);
  return { lat: latSum / wSum, lon };
}

/**
 * Unit vector `c` moved along the great circle toward unit vector `p` just
 * far enough that `p` is at most `maxDeg` away from it (unchanged when it
 * already is).
 */
export function pullWithin(c, p, maxDeg) {
  const dot = Math.max(
    -1,
    Math.min(1, c[0] * p[0] + c[1] * p[1] + c[2] * p[2]),
  );
  const angle = Math.acos(dot);
  const limit = maxDeg * DEG;
  if (angle <= limit || angle < 1e-9) return c.slice();
  const s = Math.sin(angle);
  if (s < 1e-9) return c.slice();
  const t = (angle - limit) / angle;
  const wa = Math.sin((1 - t) * angle) / s;
  const wb = Math.sin(t * angle) / s;
  const v = [0, 1, 2].map((k) => wa * c[k] + wb * p[k]);
  const len = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / len, v[1] / len, v[2] / len];
}

/** Normalised centroid of unit vectors and the largest angle (deg) from it. */
export function centroid(vectors) {
  let x = 0;
  let y = 0;
  let z = 0;
  for (const v of vectors) {
    x += v[0];
    y += v[1];
    z += v[2];
  }
  const len = Math.hypot(x, y, z);
  if (!vectors.length || len < 1e-9) return null;
  const c = [x / len, y / len, z / len];
  let spread = 0;
  for (const v of vectors) {
    const d = Math.max(
      -1,
      Math.min(1, v[0] * c[0] + v[1] * c[1] + v[2] * c[2]),
    );
    spread = Math.max(spread, Math.acos(d) / DEG);
  }
  return { vector: c, spread };
}

/**
 * The camera: orientation, zoom and the screen viewport the orb occupies.
 * @param {{distance?: number, tiltDeg?: number}} [options]
 */
export function createCamera(options = {}) {
  const distance = options.distance ?? CAMERA_DISTANCE;
  const tilt = (options.tiltDeg ?? TILT_DEG) * DEG;
  const rest = () => quatFromAxisAngle(1, 0, 0, tilt);
  const focalK = Math.sqrt(distance * distance - 1);
  let q = rest();
  let zoom = 1;
  let viewport = { cx: 0, cy: 0, r: 1 };
  let version = 0;
  const m = new Float64Array(9);
  let mVersion = -1;

  const changed = () => {
    version += 1;
  };
  const matrix = () => {
    if (mVersion !== version) {
      quatToMat3(q, m);
      mVersion = version;
    }
    return m;
  };

  const camera = {
    distance,
    get version() {
      return version;
    },
    get q() {
      return q.slice();
    },
    get zoom() {
      return zoom;
    },
    get viewport() {
      return { ...viewport };
    },
    /** Radius of the limb on screen, in CSS px. */
    radius() {
      return viewport.r * zoom;
    },
    rest,
    setViewport({ cx, cy, r }) {
      if (![cx, cy, r].every(Number.isFinite) || r <= 0) return false;
      if (cx === viewport.cx && cy === viewport.cy && r === viewport.r)
        return false;
      viewport = { cx, cy, r };
      changed();
      return true;
    },
    setOrientation(next) {
      q = quatNormalize(next);
      changed();
    },
    setZoom(value) {
      const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Number(value) || 1));
      if (next !== zoom) {
        zoom = next;
        changed();
      }
      return zoom;
    },
    zoomBy(factor) {
      return camera.setZoom(zoom * factor);
    },
    reset() {
      q = rest();
      zoom = 1;
      changed();
    },
    /** Rotate about the orb's own (tilted) polar axis. */
    spin(rad) {
      q = quatNormalize(quatMul(q, quatFromAxisAngle(0, 1, 0, rad)));
      changed();
    },
    /** Tilt about the screen's horizontal axis. */
    tilt(rad) {
      q = quatNormalize(quatMul(quatFromAxisAngle(1, 0, 0, rad), q));
      changed();
    },
    /** Virtual trackball drag from (x0, y0) to (x1, y1) in CSS px. */
    trackball(x0, y0, x1, y1) {
      const a = camera.toBall(x0, y0);
      const b = camera.toBall(x1, y1);
      const dq = quatFromTo(a, b);
      q = quatNormalize(quatMul(dq, q));
      changed();
    },
    /** Screen point → point on the virtual trackball (Bell's sheet). */
    toBall(x, y) {
      const r = camera.radius();
      const px = (x - viewport.cx) / r;
      const py = -(y - viewport.cy) / r;
      const d2 = px * px + py * py;
      const pz = d2 <= 0.5 ? Math.sqrt(1 - d2) : 0.5 / Math.sqrt(d2);
      const len = Math.hypot(px, py, pz);
      return [px / len, py / len, pz / len];
    },
    /** Orb-local vector → view space. */
    toView(p) {
      const M = matrix();
      return [
        M[0] * p[0] + M[1] * p[1] + M[2] * p[2],
        M[3] * p[0] + M[4] * p[1] + M[5] * p[2],
        M[6] * p[0] + M[7] * p[1] + M[8] * p[2],
      ];
    },
    /**
     * Project an orb-local point into `out[o..o+3]` = (x, y, z, scale):
     * screen x/y in CSS px, view-space depth (1 = facing, < 0 = back), and a
     * mild size factor for depth cueing.
     */
    projectXYZ(x, y, z, out, o = 0) {
      const M = matrix();
      const vx = M[0] * x + M[1] * y + M[2] * z;
      const vy = M[3] * x + M[4] * y + M[5] * z;
      const vz = M[6] * x + M[7] * y + M[8] * z;
      const f = viewport.r * zoom * focalK;
      const d = distance - vz;
      out[o] = viewport.cx + (f * vx) / d;
      out[o + 1] = viewport.cy - (f * vy) / d;
      out[o + 2] = vz;
      out[o + 3] = 1 + 0.12 * vz;
      return out;
    },
    /** Project one point; returns {x, y, z, scale, front}. */
    project(p) {
      const out = [0, 0, 0, 0];
      camera.projectXYZ(p[0], p[1], p[2], out);
      return {
        x: out[0],
        y: out[1],
        z: out[2],
        scale: out[3],
        front: out[2] >= 0,
      };
    },
    /** Project n packed xyz points into a (x, y, z, scale) Float32Array. */
    projectAll(pos, n, out = new Float32Array(n * 4)) {
      for (let i = 0; i < n; i += 1) {
        camera.projectXYZ(
          pos[i * 3],
          pos[i * 3 + 1],
          pos[i * 3 + 2],
          out,
          i * 4,
        );
      }
      return out;
    },
    /** Angle in degrees between the view axis and the point. */
    angleFromFront(p) {
      const v = camera.toView(p);
      const len = Math.hypot(v[0], v[1], v[2]) || 1;
      return Math.acos(Math.max(-1, Math.min(1, v[2] / len))) / DEG;
    },
    /** Screen angle (radians, canvas convention) from the centre toward p. */
    limbAngle(p) {
      const v = camera.toView(p);
      return Math.atan2(-v[1], v[0]);
    },
  };
  return camera;
}

/**
 * Screen-space hash grid for picking: nearest eligible node within
 * max(8, r + 4) px, or max(14, r + 4) px for touch.
 */
export function createPickGrid(cellPx = PICK_CELL_PX) {
  const cells = new Map();
  let xs = new Float32Array(0);
  let ys = new Float32Array(0);
  let rs = new Float32Array(0);
  const key = (cx, cy) => (cx + 32768) * 65536 + (cy + 32768);
  return {
    /**
     * @param {Float32Array} proj (x, y, z, scale) per node
     * @param {number} n node count
     * @param {(i:number)=>number} radiusOf pick radius base (0 = not pickable)
     */
    build(proj, n, radiusOf) {
      cells.clear();
      if (xs.length < n) {
        xs = new Float32Array(n);
        ys = new Float32Array(n);
        rs = new Float32Array(n);
      }
      for (let i = 0; i < n; i += 1) {
        const r = radiusOf(i);
        rs[i] = r;
        if (!(r > 0)) continue;
        const x = proj[i * 4];
        const y = proj[i * 4 + 1];
        xs[i] = x;
        ys[i] = y;
        const k = key(Math.floor(x / cellPx), Math.floor(y / cellPx));
        const list = cells.get(k);
        if (list) list.push(i);
        else cells.set(k, [i]);
      }
    },
    pick(x, y, { touch = false } = {}) {
      const gx = Math.floor(x / cellPx);
      const gy = Math.floor(y / cellPx);
      let best = -1;
      let bestD = Infinity;
      for (let dx = -1; dx <= 1; dx += 1) {
        for (let dy = -1; dy <= 1; dy += 1) {
          const list = cells.get(key(gx + dx, gy + dy));
          if (!list) continue;
          for (const i of list) {
            const reach = touch
              ? Math.max(14, rs[i] + 4)
              : Math.max(8, rs[i] + 4);
            const d = Math.hypot(xs[i] - x, ys[i] - y);
            if (d <= reach && d < bestD) {
              bestD = d;
              best = i;
            }
          }
        }
      }
      return best;
    },
    get size() {
      return cells.size;
    },
  };
}
