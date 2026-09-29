/**
 * @module uav/contextOverlay
 * @description The map's context overlay (WG v2 §4.2.7): mapped sites from
 * `GET /intel/overlay`, drawn into their OWN `Cesium.CustomDataSource`
 * ("uav-context-overlay"), never the vehicle snapshot source and never the
 * mission overlay.
 *
 * It fetches only while it is ACTIVE (the console's map overview turns it on
 * through the tracking port): once at once, then every `CONTEXT_POLL_MS` on the
 * layer's own poll tick, sending the last `rev` so an unchanged picture costs
 * a tiny `{rev, unchanged: true}` body. A failure backs off
 * `CONTEXT_ERROR_BACKOFF_MS`. A theater change (`resetForTheater`) drops what
 * was drawn for the old theater and refetches without a `rev`.
 *
 * Sites are context only: a click reports the site's id to `onPick` listeners
 * (the console opens the inspector); nothing here can task an aircraft.
 *
 * Phase B (WG v2 §5.3.12) draws the simulated wargame from the same body:
 * forces, envelopes, vectors and engagements. `truth` is the console's
 * Umpire view (it asks `truth=1`, and red shows only then); switching to
 * Blue view drops red at once, from the body already held, before the
 * refetch lands. Clicking a force selects it, which shows its detection
 * ring. Every wargame item is simulated and none of it is a target either:
 * a click only reports the item's id.
 */
import * as Cesium from 'cesium';
import { TRAIL_PREFIX, VEHICLE_PREFIX } from './policy.js';
import {
  CONTEXT_DATA_SOURCE,
  CONTEXT_ERROR_BACKOFF_MS,
  CONTEXT_POLL_MS,
  DEFAULT_VISIBILITY,
  WARGAME_KINDS,
  contextOverlayUrl,
  kindVisible,
  mergeVisibility,
} from './contextPolicy.js';
import {
  contextEntityList,
  emptyWargameCounts,
  readContextFeatures,
} from './contextEntities.js';
import { createContextIcons } from './contextIcons.js';

function readConfigValue(value) {
  return typeof value === 'function' ? value() : value;
}

const EMPTY_COUNTS = Object.freeze({
  site: Object.freeze({ served: 0, drawn: 0, capped: 0 }),
  unknown: Object.freeze({ served: 0, drawn: 0 }),
  invalid: 0,
  ...emptyWargameCounts(),
});

const EMPTY_META = Object.freeze({
  theater: null,
  sites: null,
  attribution: Object.freeze([]),
  omitted: Object.freeze({}),
  wargameError: null,
});

/** The server's per-kind `omitted` counts for the wargame kinds. */
function wargameOmitted(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  for (const kind of WARGAME_KINDS) {
    const n = raw[kind];
    if (Number.isInteger(n) && n > 0) out[kind] = n;
  }
  return out;
}

/**
 * Create the context-overlay owner for one layer.
 * @param {{state: object, services: object, options: object}} context Layer
 *   context. `options.contextOverlay` ({baseUrl, token, fetchImpl}) names the
 *   host; without it the mission overlay's origin is used (the in-app host
 *   serves `/intel/*` beside the bridge, with the same bearer).
 * @returns {object} Context overlay methods.
 */
export function createContextOverlay({ state, services, options }) {
  const config = options.contextOverlay || options.missionOverlay || {};
  const fetchImpl =
    config.fetchImpl || ((...args) => globalThis.fetch(...args));
  const icons = options.contextIcons || createContextIcons();
  const pollMs = options.contextPollMs ?? CONTEXT_POLL_MS;
  const backoffMs = options.contextBackoffMs ?? CONTEXT_ERROR_BACKOFF_MS;

  let collection = null;
  let active = false;
  let layerShown = true;
  let visibility = { ...DEFAULT_VISIBILITY };
  let truth = false;
  let rev = null;
  let status = 'idle';
  let lastError = null;
  let fetchedAtMs = null;
  let nextAtMs = 0;
  let inflight = null;
  let generation = 0;
  let counts = EMPTY_COUNTS;
  let meta = EMPTY_META;
  let lastBody = null;
  let selected = null;
  let clickHandler = null;
  /** entity id -> {id, kind} for everything drawn now. */
  const drawn = new Map();
  const pickListeners = new Set();

  function ensureCollection() {
    if (collection || !state.viewer?.dataSources) return collection;
    collection = new Cesium.CustomDataSource(CONTEXT_DATA_SOURCE);
    collection.show = active && layerShown;
    state.viewer.dataSources.add(collection);
    return collection;
  }

  function applyShow() {
    if (collection) collection.show = active && layerShown;
    requestRender();
  }

  function requestRender() {
    state.viewer?.scene?.requestRender?.();
    services.render.governorRequestRender('uav-context');
  }

  /** Remove every drawn entity and release its pick ownership. */
  function clearEntities() {
    collection?.entities.removeAll();
    for (const entityId of drawn.keys()) state.ownedIds.delete(entityId);
    drawn.clear();
  }

  /**
   * Replace the drawn picture with one overlay body.
   * @param {object} body `/intel/overlay` FeatureCollection.
   * @returns {number} Entities drawn.
   */
  function apply(body) {
    const target = ensureCollection();
    if (!target) return 0;
    clearEntities();
    lastBody = body ?? null;
    const read = readContextFeatures(body, { truth, selected });
    for (const item of read.items) {
      try {
        for (const options of contextEntityList(item, { icons })) {
          const entity = target.entities.add(options);
          entity.show = kindVisible(item.kind, visibility);
          // A corridor leg, an outcome ring or an envelope reports the id
          // the console knows (the vector, the engagement, the force).
          drawn.set(options.id, {
            id: item.pickId ?? item.id,
            kind: item.kind,
          });
          state.ownedIds.add(options.id);
        }
      } catch {
        // One malformed feature never costs the rest of the picture.
      }
    }
    counts = read.counts;
    meta = {
      theater:
        body?.theater && typeof body.theater === 'object'
          ? { id: body.theater.id ?? null, epoch: body.theater.epoch ?? null }
          : null,
      sites: body?.sites && typeof body.sites === 'object' ? body.sites : null,
      attribution: Array.isArray(body?.attribution)
        ? body.attribution.filter((line) => typeof line === 'string')
        : [],
      omitted: wargameOmitted(body?.omitted),
      wargameError:
        typeof body?.wargame?.error === 'string'
          ? body.wargame.error.slice(0, 200)
          : null,
    };
    requestRender();
    return drawn.size;
  }

  /** Draw the held body again (the view or the selection changed). */
  function redraw() {
    if (lastBody && collection) apply(lastBody);
  }

  /**
   * Fetch one body. Null when no host is configured (unsupported).
   * @returns {Promise<object|null>} Body.
   */
  async function fetchOverlay() {
    const base = readConfigValue(config.baseUrl);
    if (base === undefined || base === null) return null;
    const token = readConfigValue(config.token);
    const response = await fetchImpl(contextOverlayUrl(base, { truth, rev }), {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!response?.ok)
      throw new Error(`Context overlay HTTP ${response?.status ?? 0}`);
    return response.json();
  }

  /**
   * Fetch now. `force` drops the held `rev` (a full body comes back) and
   * supersedes a fetch already in flight, whose answer is then ignored.
   * Never throws: a missing route or a failing host degrades to status.
   * @param {{force?: boolean}} [opts] Options.
   * @returns {Promise<string>} Resulting status.
   */
  function refresh({ force = false } = {}) {
    if (force) {
      generation += 1;
      rev = null;
      nextAtMs = 0;
    } else if (inflight) {
      return inflight.promise;
    }
    if (!ensureCollection()) return Promise.resolve(status);
    const gen = generation;
    const run = { promise: null };
    run.promise = (async () => {
      try {
        const body = await fetchOverlay();
        if (gen !== generation) return status;
        const nowMs = state.now();
        if (body === null) {
          status = 'unsupported';
          nextAtMs = nowMs + pollMs;
          return status;
        }
        let again = false;
        if (body?.unchanged === true) {
          // An `unchanged` for a rev we do not hold asks again, in full.
          if (typeof body.rev !== 'string' || body.rev !== rev) {
            rev = null;
            again = true;
          }
        } else {
          apply(body);
          rev = typeof body?.rev === 'string' ? body.rev : null;
        }
        status = 'ok';
        lastError = null;
        fetchedAtMs = nowMs;
        nextAtMs = again ? nowMs : nowMs + pollMs;
      } catch (error) {
        if (gen !== generation) return status;
        status = 'error';
        lastError = error?.message ?? 'context overlay unavailable';
        nextAtMs = state.now() + backoffMs;
      } finally {
        if (inflight === run) inflight = null;
      }
      return status;
    })();
    inflight = run;
    return run.promise;
  }

  /**
   * The layer's poll tick: fetch when active and due. Never awaited by the
   * tick, so a slow host never delays the vehicles.
   * @returns {boolean} Whether a fetch started.
   */
  function tick() {
    if (!active) return false;
    ensureCollection();
    installClickHandler();
    if (inflight || state.now() < nextAtMs) return false;
    refresh().catch(() => {});
    return true;
  }

  /**
   * Turn the overlay on (show and poll) or off (hide, stop polling and stop
   * reporting picks).
   * @param {boolean} on Active.
   * @returns {boolean} The new state.
   */
  function setActive(on) {
    const next = Boolean(on);
    if (next === active) return active;
    active = next;
    ensureCollection();
    applyShow();
    if (active) {
      installClickHandler();
      nextAtMs = 0;
      tick();
    } else {
      removeClickHandler();
    }
    return active;
  }

  /**
   * Per-kind show (`{sites, forces, engagements, vectors}`; missing keys keep
   * their current value).
   * @param {object} next Switches.
   * @returns {object} The switches now in force.
   */
  function setVisibility(next) {
    visibility = mergeVisibility(visibility, next);
    // The console's view may ride along (`{truth}`): Umpire view is a
    // presentation choice like the per-kind switches.
    if (typeof next?.truth === 'boolean') setTruth(next.truth);
    for (const [entityId, { kind }] of drawn) {
      const entity = collection?.entities.getById(entityId);
      if (entity) entity.show = kindVisible(kind, visibility);
    }
    requestRender();
    return { ...visibility };
  }

  /**
   * Ask for red truth (the console's Umpire view, §5.3.3) or not (Blue
   * view). A change redraws the held body under the new view at once (so
   * Blue view drops red before anything is fetched) and refetches in full.
   * @param {boolean} on Umpire view.
   * @returns {boolean} The view now in force.
   */
  function setTruth(on) {
    const next = on === true;
    if (next === truth) return truth;
    truth = next;
    generation += 1;
    rev = null;
    nextAtMs = 0;
    redraw();
    if (active) refresh({ force: true }).catch(() => {});
    return truth;
  }

  /**
   * Select a force (`frc:…`, its detection ring shows) or clear (null).
   * @param {string|null} id Force id.
   * @returns {string|null} The selection now in force.
   */
  function select(id) {
    const next = typeof id === 'string' && id.startsWith('frc:') ? id : null;
    if (next === selected) return selected;
    selected = next;
    redraw();
    return selected;
  }

  /**
   * The running theater changed (§4.2.8): what was drawn belongs to the old
   * one, so it goes at once, and the next fetch asks for a full body (now,
   * when active).
   * @returns {void}
   */
  function resetForTheater() {
    generation += 1;
    rev = null;
    nextAtMs = 0;
    counts = EMPTY_COUNTS;
    meta = EMPTY_META;
    lastBody = null;
    selected = null;
    clearEntities();
    requestRender();
    if (active) refresh({ force: true }).catch(() => {});
  }

  // ---- picking ---------------------------------------------------------------

  /**
   * The console id behind a picked entity id: a context feature's own id, or
   * `veh:{name}` for a vehicle or its trail. Null for anything else.
   * @param {string|null} pickedId Canonical pick id.
   * @returns {string|null} Console id.
   */
  function pickTarget(pickedId) {
    if (pickedId === null || pickedId === undefined) return null;
    const id = String(pickedId);
    const hit = drawn.get(id);
    if (hit) return hit.id;
    for (const prefix of [VEHICLE_PREFIX, TRAIL_PREFIX]) {
      if (!id.startsWith(prefix)) continue;
      const reference = id.slice(prefix.length);
      if (state.entities.has(reference)) return `veh:${reference}`;
    }
    return null;
  }

  function emitPick(id) {
    for (const cb of [...pickListeners]) {
      try {
        cb({ id });
      } catch {
        /* a listener must never break the map's input handling */
      }
    }
  }

  /**
   * Resolve a click at a screen position and report it while active.
   * @param {object} position Screen position (Cartesian2).
   * @returns {string|null} The id reported, or null.
   */
  function handleClick(position) {
    if (!active || !state.viewer?.scene?.pick) return null;
    let picked;
    try {
      picked = state.viewer.scene.pick(position);
    } catch {
      return null;
    }
    const target = picked
      ? pickTarget(services.picking.resolvePickId(picked))
      : null;
    // A force (or its envelope) picked selects it; empty ground clears.
    if (target?.startsWith('frc:')) select(target);
    else if (!picked) select(null);
    if (target) emitPick(target);
    return target;
  }

  function installClickHandler() {
    const viewer = state.viewer;
    if (clickHandler || !active || !viewer) return;
    const factory = options.createContextClickHandler;
    if (!factory && !viewer.scene?.canvas) return;
    clickHandler = factory
      ? factory(viewer)
      : new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    clickHandler?.setInputAction?.(
      (click) => handleClick(click?.position),
      Cesium.ScreenSpaceEventType.LEFT_CLICK,
    );
  }

  function removeClickHandler() {
    if (!clickHandler) return;
    clickHandler.destroy?.();
    clickHandler = null;
  }

  /**
   * Subscribe to picks: `cb({id})` with a context id or `veh:{name}`.
   * @param {Function} cb Listener.
   * @returns {Function} Unsubscribe.
   */
  function onPick(cb) {
    if (typeof cb !== 'function') return () => {};
    pickListeners.add(cb);
    return () => pickListeners.delete(cb);
  }

  // ---- status and teardown ---------------------------------------------------

  /**
   * Overlay status for the layer's stats and the console's dock.
   * @returns {object} Status.
   */
  function getStatus() {
    const served = counts.site.served;
    const drawnSites = counts.site.drawn;
    const total = Number.isFinite(meta.sites?.total)
      ? Math.max(meta.sites.total, served)
      : served;
    return {
      status,
      active,
      visibility: { ...visibility },
      truth,
      rev,
      fetchedAtMs,
      lastError,
      retryAtMs: status === 'error' ? nextAtMs : 0,
      theater: meta.theater ? { ...meta.theater } : null,
      attribution: [...meta.attribution],
      sites: {
        total,
        served,
        drawn: drawnSites,
        capped: counts.site.capped,
        notDrawn: Math.max(0, total - drawnSites),
        degraded: meta.sites?.degraded === true,
        reason:
          typeof meta.sites?.reason === 'string' ? meta.sites.reason : null,
        fetchedAtMs: Number.isFinite(meta.sites?.fetched_at_ms)
          ? meta.sites.fetched_at_ms
          : null,
      },
      unknown: { ...counts.unknown },
      // The simulated wargame (§5.3.12): per kind {served, drawn, capped,
      // hidden (red in Blue view)}, the server's omitted counts, the force
      // whose detection ring shows, and a wargame read error, if any.
      wargame: {
        ...Object.fromEntries(
          WARGAME_KINDS.map((kind) => [kind, { ...counts[kind] }]),
        ),
        omitted: { ...meta.omitted },
        selected,
        error: meta.wargameError,
      },
    };
  }

  /** The layer was enabled or disabled: follow it. */
  function setLayerShown(on) {
    layerShown = on !== false;
    applyShow();
  }

  /**
   * Remove the data source, the click handler and all state.
   * @param {Cesium.Viewer} [viewer] Owning viewer.
   * @returns {void}
   */
  function destroy(viewer) {
    removeClickHandler();
    clearEntities();
    const owner = viewer || state.viewer;
    if (collection && owner?.dataSources)
      owner.dataSources.remove(collection, true);
    collection = null;
    generation += 1;
    inflight = null;
    active = false;
    rev = null;
    status = 'idle';
    lastError = null;
    fetchedAtMs = null;
    nextAtMs = 0;
    counts = EMPTY_COUNTS;
    meta = EMPTY_META;
    lastBody = null;
    selected = null;
    // Pick listeners belong to their subscribers (the tracking port); a layer
    // rebuilt after destroy() keeps reporting to them.
    icons.clear?.();
  }

  return {
    ensureCollection,
    apply,
    refresh,
    tick,
    setActive,
    isActive: () => active,
    setVisibility,
    setTruth,
    isTruth: () => truth,
    select,
    resetForTheater,
    pickTarget,
    handleClick,
    onPick,
    getStatus,
    setLayerShown,
    destroy,
    /** The data source (tests and the layer's compatibility handle). */
    collection: () => collection,
  };
}
