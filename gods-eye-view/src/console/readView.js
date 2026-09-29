/**
 * The read view (WG spec §5.3.11): a report's full Markdown in the stage,
 * where the orb or the list was. The after-action review opens here from
 * the report inspector's Read in full.
 *
 * It renders `fields.markdown` through chat/markdown.js and nothing else:
 * the Markdown is untrusted, every character of it lands in a text node,
 * links are text, and no other field of the report is drawn. The section
 * carries `data-view="read"`; Esc (the shell's layer stack) and Back return
 * to the view it covered.
 */
import { h, replaceKids, setHidden } from '../ui/uavDom.js';
import { renderDom } from './chat/markdown.js';
import { safeText } from './orb/placeText.js';

/** Copy deck (Appendix B, §5.3.11). */
export const READ_COPY = Object.freeze({
  aarTitle: 'After-action review (simulated)',
  reportTitle: 'Report',
  back: 'Back',
  simulated: 'Simulated',
  empty: 'This report has no text to read in full.',
});

/** The renderer's own source cap; the AAR itself is at most 40 KB. */
export const READ_MAX_CHARS = 200_000;

function str(v) {
  return typeof v === 'string' ? v : '';
}

/** Whether a report node or entity is an after-action review. */
export function isAar({ node = null, entity = null } = {}) {
  const f = entity?.fields || {};
  const header = f.header && typeof f.header === 'object' ? f.header : {};
  return (
    f.report_type === 'AAR' ||
    header.format === 'AAR' ||
    node?.attrs?.format === 'AAR' ||
    entity?.attrs?.format === 'AAR'
  );
}

/**
 * A document to read from a request, or null when there is nothing to show.
 * Accepts `{id?, title?, markdown?, simulated?}` or `{id?, node?, entity?}`
 * (the entity's `fields.markdown`). The id and title are untrusted text:
 * bidi- and control-stripped and cut short. An AAR is always titled
 * "After-action review (simulated)" and marked simulated.
 * @returns {{id:string|null, title:string, markdown:string, simulated:boolean}|null}
 */
export function readDocOf(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const entity =
    src.entity && typeof src.entity === 'object' ? src.entity : null;
  const node = src.node && typeof src.node === 'object' ? src.node : null;
  const markdown = str(src.markdown) || str(entity?.fields?.markdown);
  if (!markdown.trim()) return null;
  const aar = isAar({ node, entity }) || src.aar === true;
  const id =
    safeText(str(src.id) || str(entity?.id) || str(node?.id), 120) || null;
  const title = aar
    ? READ_COPY.aarTitle
    : safeText(str(src.title) || str(node?.label) || str(entity?.label), 120) ||
      READ_COPY.reportTitle;
  const simulated =
    aar ||
    src.simulated === true ||
    entity?.fields?.simulated === true ||
    node?.attrs?.simulated === true;
  return {
    id,
    title,
    markdown: markdown.slice(0, READ_MAX_CHARS),
    simulated,
  };
}

let readSeq = 0;

/**
 * Build the read view inside `host` (hidden until `open`).
 * @param {object} host the stage's read host
 * @param {object} [options]
 * @param {() => void} [options.onBack] Back pressed (the shell closes)
 * @returns {{element:object, open(doc:object):boolean, close():void,
 *   isOpen():boolean, current():object|null, focus():void, destroy():void}}
 */
export function createReadView(host, { onBack = () => {} } = {}) {
  readSeq += 1;
  const titleId = `ic-read-title-${readSeq}`;
  let doc = null;

  const back = h(
    'button',
    {
      type: 'button',
      class: 'ic-btn ic-read__back',
      'data-variant': 'quiet',
      'aria-keyshortcuts': 'Escape',
    },
    h(
      'span',
      { class: 'ic-icon material-symbols-outlined', 'aria-hidden': 'true' },
      'arrow_back',
    ),
    h('span', { class: 'ic-btn__label' }, READ_COPY.back),
  );
  const title = h('h2', { class: 'ic-read__title', id: titleId });
  const meta = h('p', { class: 'ic-read__meta' });
  const body = h('div', { class: 'ic-md ic-read__body' });
  const element = h(
    'section',
    {
      class: 'ic-read',
      'data-view': 'read',
      'aria-labelledby': titleId,
      tabindex: '-1',
      hidden: true,
    },
    h('header', { class: 'ic-read__head' }, back, title, meta),
    body,
  );
  host?.append?.(element);
  back.addEventListener('click', () => onBack());

  function renderMeta() {
    const kids = [];
    if (doc.simulated) {
      kids.push(
        h('span', { class: 'ic-reg-tag ic-read__tag' }, READ_COPY.simulated),
      );
    }
    if (doc.id) kids.push(h('span', { class: 'ic-read__id' }, doc.id));
    replaceKids(meta, kids);
    setHidden(meta, kids.length === 0);
  }

  function renderBody() {
    let nodes = [];
    try {
      nodes = renderDom(doc.markdown) || [];
    } catch (error) {
      globalThis.console?.error?.(error);
      nodes = [];
    }
    replaceKids(body, nodes.length ? nodes : [h('p', {}, READ_COPY.empty)]);
  }

  return {
    element,
    /** Show `raw` (see readDocOf). Returns false when it has no text. */
    open(raw) {
      const next = readDocOf(raw);
      if (!next) return false;
      doc = next;
      replaceKids(title, [doc.title]);
      renderMeta();
      renderBody();
      setHidden(element, false);
      return true;
    },
    close() {
      doc = null;
      setHidden(element, true);
      replaceKids(body, []);
    },
    isOpen: () => doc != null,
    current: () => (doc ? { ...doc } : null),
    /** Put focus on the view itself, so Esc and the screen reader start here. */
    focus() {
      element.focus?.();
    },
    destroy() {
      doc = null;
      element.remove?.();
    },
  };
}
