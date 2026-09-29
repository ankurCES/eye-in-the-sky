/**
 * The simulated wargame's session strip (WG spec §5.3.2).
 *
 * While `meta.wargame.active` is true, `.ic-root[data-wargame="on"]` gets a
 * 28 px strip above everything, in orb, map and tracking alike:
 *
 *   Simulated wargame   Session {id}   {theater label}   Started {Z}
 *   Sim time ×4   {view word}   [End wargame]
 *
 * With no active session the strip is hidden and builds nothing, so ISR
 * mode is unchanged. End wargame opens a one-step popover ([Keep playing]
 * [End wargame]); its End calls `POST /wargame/session/end` through the
 * injected `endWargame`, and a refusal reads "Couldn't end the wargame:
 * {error}." with Retry.
 *
 * Every string from the host (session id, theater label) is untrusted: it
 * is bidi-stripped, cut short and rendered as a text node only.
 */
import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { ERROR_WORDS, errorCode, formatZulu } from './api.js';
import { safeText } from './orb/placeText.js';

/** The strip's height in px (`--ic-strip-h`); GEV's map is inset by it. */
export const STRIP_H = 28;

/** Copy deck (Appendix B, §5.3.2), sentence case, never "·". */
export const STRIP_COPY = Object.freeze({
  mark: 'Simulated wargame',
  session: (id) => `Session ${id}`,
  started: (z) => `Started ${z}`,
  simTime: (x) => `Sim time ${x}`,
  umpireView: 'Umpire view',
  blueView: 'Blue view',
  end: 'End wargame',
  keepPlaying: 'Keep playing',
  confirm:
    'End the wargame? Scenario units and waiting engagements are cleared. The after-action review is kept. Aircraft keep their current tasks.',
  ending: 'Ending the wargame…',
  failed: (error) => `Couldn't end the wargame: ${error}.`,
  retry: 'Retry',
  endedByYou: (z) => `Wargame ended by you at ${z}.`,
  honesty: 'Simulated wargame.',
});

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/** `graph.meta.wargame` while a session is active, else null. */
export function activeWargame(graph) {
  const wg = graph?.meta?.wargame;
  return wg && typeof wg === 'object' && wg.active === true ? wg : null;
}

/** "×4", "×2.5" for a sim speed other than 1, else null. */
export function simScaleText(scale) {
  if (!finite(scale) || scale <= 0 || Math.abs(scale - 1) < 1e-9) return null;
  return `×${Number(scale.toFixed(2))}`;
}

/**
 * The strip's model from the picture, or null when no session is active.
 * `endedSession` is the id the operator just ended: the strip goes at once,
 * before the next poll says so.
 * @returns {{sessionId:string|null, theater:string|null, started:string|null,
 *   scale:string|null, view:string|null}|null}
 */
export function stripModel(graph, { endedSession = null } = {}) {
  const wg = activeWargame(graph);
  if (!wg) return null;
  const sessionId = safeText(wg.session_id ?? '', 40) || null;
  if (endedSession && sessionId === endedSession) return null;
  const scale = finite(wg.time_scale)
    ? wg.time_scale
    : graph?.theater?.time_scale;
  return {
    sessionId,
    theater: safeText(graph?.theater?.label ?? '', 60) || null,
    started: formatZulu(wg.started_at_ms, { seconds: true }) || null,
    scale: simScaleText(scale),
    view:
      wg.truth_view === true
        ? STRIP_COPY.umpireView
        : wg.truth_view === false
          ? STRIP_COPY.blueView
          : null,
  };
}

/** The refusal line: the host's words (or the error's), one full stop. */
export function endFailedText(error) {
  let text = safeText(
    error?.message ?? (typeof error === 'string' ? error : ''),
    200,
  )
    .replace(/[.\s]+$/, '')
    .trim();
  if (!text) text = 'no answer from the local service';
  // "Can't reach…" reads as a clause after the colon; "HTTP 500" keeps case.
  if (/^[A-Z][a-z']/.test(text)) text = text[0].toLowerCase() + text.slice(1);
  return STRIP_COPY.failed(text);
}

let popSeq = 0;

function button(label, cls, variant = 'quiet') {
  const text = h('span', { class: 'ic-btn__label' }, label);
  const el = h(
    'button',
    { type: 'button', class: `ic-btn ${cls}`, 'data-variant': variant },
    text,
  );
  return { el, text };
}

/**
 * Build the strip. It starts hidden; `update(stripModel(graph))` shows it.
 * @param {object} [options]
 * @param {() => Promise<{ok?:boolean, aar_id?:string}>} [options.endWargame]
 * @param {(info:{session_id:string|null, aar_id:string|null, ended_at_ms:number}) => void} [options.onEnded]
 * @param {(text:string, politeness?:string) => void} [options.announce]
 * @param {() => number} [options.now]
 * @returns {{element:object, update(model:object|null):void, open():void,
 *   close(opts?:{restore?:boolean}):void, isOpen():boolean, escape():boolean,
 *   readonly ending:boolean, readonly model:object|null, destroy():void}}
 */
export function createWargameStrip({
  endWargame = null,
  onEnded = () => {},
  announce = () => {},
  now = () => Date.now(),
} = {}) {
  popSeq += 1;
  const popId = `ic-wgstrip-pop-${popSeq}`;
  let model = null;
  let open = false;
  let ending = false;
  let failure = null;
  let destroyed = false;

  const details = h('span', { class: 'ic-wgstrip__details' });
  const view = h('span', { class: 'ic-wgstrip__view' });
  const endBtn = h(
    'button',
    {
      type: 'button',
      class: 'ic-wgstrip__end',
      'aria-haspopup': 'dialog',
      'aria-expanded': 'false',
      'aria-controls': popId,
    },
    STRIP_COPY.end,
  );
  const body = h('p', { class: 'ic-popover__body' }, STRIP_COPY.confirm);
  const status = h('p', {
    class: 'ic-wgstrip__status',
    role: 'status',
    hidden: true,
  });
  const keep = button(STRIP_COPY.keepPlaying, 'ic-wgstrip__keep');
  const confirm = button(STRIP_COPY.end, 'ic-wgstrip__confirm', 'danger');
  const pop = h(
    'div',
    {
      class: 'ic-popover ic-wgstrip__pop',
      id: popId,
      role: 'dialog',
      'aria-label': STRIP_COPY.end,
      hidden: true,
    },
    body,
    status,
    h('div', { class: 'ic-popover__actions' }, keep.el, confirm.el),
  );
  const element = h(
    'section',
    {
      class: 'ic-wgstrip',
      'aria-label': STRIP_COPY.mark,
      hidden: true,
    },
    h('span', { class: 'ic-wgstrip__bar', 'aria-hidden': 'true' }),
    h('span', { class: 'ic-wgstrip__mark' }, STRIP_COPY.mark),
    details,
    view,
    endBtn,
    pop,
  );

  function item(kids) {
    return h('span', { class: 'ic-wgstrip__item' }, ...kids);
  }

  function renderDetails() {
    const kids = [];
    if (model.sessionId) {
      kids.push(
        item([
          'Session ',
          h('span', { class: 'ic-wgstrip__mono' }, model.sessionId),
        ]),
      );
    }
    if (model.theater) kids.push(item([model.theater]));
    if (model.started) kids.push(item([STRIP_COPY.started(model.started)]));
    if (model.scale) kids.push(item([STRIP_COPY.simTime(model.scale)]));
    // A space between items, so a screen reader never runs them together.
    replaceKids(
      details,
      kids.flatMap((kid, i) => (i ? [' ', kid] : [kid])),
    );
    const words = [
      model.sessionId ? STRIP_COPY.session(model.sessionId) : '',
      model.theater || '',
      model.started ? STRIP_COPY.started(model.started) : '',
      model.scale ? STRIP_COPY.simTime(model.scale) : '',
    ].filter(Boolean);
    // The full line for a pointer when a narrow strip cuts it short.
    if (words.length) details.setAttribute('title', words.join('   '));
    else details.removeAttribute?.('title');
    view.textContent = model.view || '';
    setHidden(view, !model.view);
  }

  function renderPop() {
    setHidden(pop, !open);
    endBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    const line = ending ? STRIP_COPY.ending : failure;
    status.textContent = line || '';
    setHidden(status, !line);
    replaceKids(confirm.text, [
      failure && !ending ? STRIP_COPY.retry : STRIP_COPY.end,
    ]);
    if (ending) confirm.el.setAttribute('aria-disabled', 'true');
    else confirm.el.removeAttribute?.('aria-disabled');
    confirm.el.setAttribute('aria-busy', ending ? 'true' : 'false');
  }

  function openPop() {
    if (destroyed || !model || open) return;
    open = true;
    failure = null;
    renderPop();
    // The safe choice takes focus: Enter never ends the wargame by accident.
    keep.el.focus?.();
  }

  function closePop({ restore = true } = {}) {
    if (!open) return;
    open = false;
    failure = null;
    renderPop();
    if (restore && element.getAttribute?.('hidden') == null) endBtn.focus?.();
  }

  async function confirmEnd() {
    if (destroyed || ending || !open) return;
    if (typeof endWargame !== 'function') {
      failure = endFailedText({ message: 'this console has no end route' });
      renderPop();
      return;
    }
    const sessionId = model?.sessionId ?? null;
    ending = true;
    failure = null;
    renderPop();
    announce(STRIP_COPY.ending, 'polite');
    let result;
    try {
      result = await endWargame();
      if (result && typeof result === 'object' && result.ok === false) {
        const code = errorCode(result);
        const message =
          (typeof result.message === 'string' && result.message) ||
          (code && (ERROR_WORDS[code] || code.replace(/_/g, ' '))) ||
          'the host refused';
        throw Object.assign(new Error(String(message)), { body: result });
      }
    } catch (error) {
      if (destroyed) return;
      ending = false;
      failure = endFailedText(error);
      renderPop();
      announce(failure, 'assertive');
      confirm.el.focus?.();
      return;
    }
    if (destroyed) return;
    ending = false;
    open = false;
    failure = null;
    renderPop();
    const endedAt = Number(now()) || Date.now();
    const aar =
      result && typeof result === 'object' && typeof result.aar_id === 'string'
        ? safeText(result.aar_id, 80) || null
        : null;
    try {
      onEnded({ session_id: sessionId, aar_id: aar, ended_at_ms: endedAt });
    } catch (error) {
      globalThis.console?.error?.(error);
    }
  }

  endBtn.addEventListener('click', () => {
    if (ending) return;
    if (open) closePop();
    else openPop();
  });
  keep.el.addEventListener('click', () => closePop());
  confirm.el.addEventListener('click', () => {
    confirmEnd();
  });
  pop.addEventListener('keydown', (event) => {
    if (event?.key !== 'Escape') return;
    event.preventDefault?.();
    event.stopPropagation?.();
    escape();
  });

  /** Esc: close the popover (not while an End is in flight, so its answer
   *  stays in view). Returns whether Esc was used. */
  function escape() {
    if (!open) return false;
    if (!ending) closePop();
    return true;
  }

  /** Show the strip for `next` (a stripModel), or hide it for null. */
  function update(next) {
    if (destroyed) return;
    model = next && typeof next === 'object' ? next : null;
    if (!model) {
      // A session that ends elsewhere (the analyst, a restart) takes the
      // popover with it; a pending End keeps running to its answer.
      if (!ending) {
        open = false;
        failure = null;
      }
      renderPop();
      replaceKids(details, []);
      setHidden(element, true);
      return;
    }
    renderDetails();
    setHidden(element, false);
    renderPop();
  }

  return {
    element,
    update,
    open: openPop,
    close: closePop,
    isOpen: () => open,
    escape,
    get ending() {
      return ending;
    },
    get model() {
      return model ? { ...model } : null;
    },
    /** The End wargame button, for focus handling in the shell. */
    endButton: endBtn,
    destroy() {
      destroyed = true;
      open = false;
      element.remove?.();
    },
  };
}
