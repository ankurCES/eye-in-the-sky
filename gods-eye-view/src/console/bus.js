/**
 * The console's event bus: one tiny emitter shared by every component.
 *
 * Events and payloads (shared frontend interface):
 * - `focus:entities` {ids, by:'operator'|'analyst', note?}
 * - `inspect` {id}
 * - `track:request` {vehicle, source:'operator'|'analyst'|'launch', reason?}
 * - `track:exit` {source?}
 * - `abort:request` {vehicle}
 * - `ask` {text, focused_ids?, draft?}
 * - `search:filter` {ids|null, query, source?}
 * - `mode` {mode:'orb'|'entering_tracking'|'tracking'|'entering_map'|'map'|'exiting',
 *   vehicle?}
 * - `map:request` {ids, bbox?:[s,w,n,e]|null, label?, reason?,
 *   source:'operator'|'analyst', countdown?:boolean}: open (or move) the map
 *   overview (WG §4.2.3, §4.2.7). mode.js opens it at once for the operator,
 *   and for the analyst shows the 3 s notice, or a static toast when
 *   `countdown` is false (the view's gate failed).
 * - `alarm:viewed` {id}
 * - `layout` {layout:'wide'|'compact'|'narrow'}
 * - `analyst:availability` {available, reason?, hint?}
 * - `approval:pending` {count, oldest?}
 *
 * A listener that throws never stops the others: the error is reported and
 * the emit carries on, because one broken panel must not blind the rest.
 */

/** Event names the console uses. Documentation, not an allowlist. */
export const BUS_EVENTS = Object.freeze([
  'focus:entities',
  'inspect',
  'track:request',
  'track:exit',
  'abort:request',
  'ask',
  'search:filter',
  'mode',
  'alarm:viewed',
  'layout',
  'analyst:availability',
  'approval:pending',
  'map:request',
  // Additive, between console owners and GEV:
  'inspector:state', // {open, id} from the inspector (plate viewport)
  'gev:status', // {state, phase, message?} from src/main.js (map start-up)
  'approval:review', // {} a sheet's Review: show the oldest waiting slip
]);

function defaultReport(error) {
  if (typeof globalThis.reportError === 'function')
    globalThis.reportError(error);
  else globalThis.console?.error?.(error);
}

/**
 * Create a bus.
 * @param {{onError?: (error: unknown, event: string) => void}} [options]
 * @returns {{on: Function, off: Function, emit: Function, clear: Function}}
 */
export function createBus({ onError } = {}) {
  const listeners = new Map();
  const report = typeof onError === 'function' ? onError : defaultReport;

  /**
   * Subscribe. The same callback may be added twice; each subscription is
   * removed by its own unsubscribe function.
   * @returns {() => void} unsubscribe (idempotent)
   */
  function on(event, cb) {
    if (typeof cb !== 'function') return () => {};
    let set = listeners.get(event);
    if (!set) {
      set = new Set();
      listeners.set(event, set);
    }
    const entry = { cb };
    set.add(entry);
    return () => {
      set.delete(entry);
      if (!set.size && listeners.get(event) === set) listeners.delete(event);
    };
  }

  /** Remove every subscription of `cb` to `event`. */
  function off(event, cb) {
    const set = listeners.get(event);
    if (!set) return;
    for (const entry of [...set]) if (entry.cb === cb) set.delete(entry);
    if (!set.size) listeners.delete(event);
  }

  /**
   * Deliver `payload` to every current listener of `event`, in subscription
   * order. A listener removed during the emit is skipped.
   * @returns {number} listeners called
   */
  function emit(event, payload) {
    const set = listeners.get(event);
    if (!set) return 0;
    let called = 0;
    for (const entry of [...set]) {
      if (!set.has(entry)) continue;
      called += 1;
      try {
        entry.cb(payload);
      } catch (error) {
        report(error, event);
      }
    }
    return called;
  }

  function clear() {
    listeners.clear();
  }

  return { on, off, emit, clear };
}
