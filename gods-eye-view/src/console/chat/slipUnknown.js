/**
 * The unknown-class fail-safe slip body (WG spec §4.2.1, D7 #8).
 *
 * A server that is newer than this console can send an approval class the
 * console doesn't know (M14a adds `engagement`). The console must not guess
 * what it does, so the slip shows only what the server sent, as text (title,
 * tool in mono, summary, consequences), one fixed line, and a Deny-only
 * button whose note tells the analyst why. There is no Approve element in the
 * DOM (slip.js builds the buttons from `denyOnly`).
 */

import { stripBidi, truncate } from './format.js';

/** The class word as shown: bidi-free, trimmed, at most 40 characters. */
export function classWord(klass) {
  const text =
    typeof klass === 'string' ? stripBidi(klass).replace(/\s+/g, ' ') : '';
  const word = truncate(text.trim(), 40);
  return word || 'none';
}

/** The fixed line (copy deck, Phase A: unrecognised). */
export function unknownLine(klass) {
  return `This console doesn't recognise the approval class "${classWord(klass)}", so it can't show what this does or approve it. Update the app, or deny it.`;
}

/** The note the Deny button sends to the analyst. */
export function unknownDenyNote(klass) {
  return `The console can't approve the "${classWord(klass)}" class.`;
}

/**
 * Info nodes for an unknown-class slip.
 * @param {object} approval reducer approval (`rawClass`, `tool`, `summary`,
 *   `consequences`)
 * @param {{h: Function, segmentNodes: Function}} ui element factory (bidi-safe)
 *   and the summary splitter from slip.js
 * @returns {Array<object>} nodes
 */
export function unknownInfoNodes(approval, ui) {
  const a = approval || {};
  const { h, segmentNodes } = ui;
  const nodes = [];
  if (a.tool) nodes.push(h('p', { class: 'ic-mono ic-slip__tool' }, a.tool));
  const summary = segmentNodes(a.summary);
  if (summary.length) {
    nodes.push(h('p', { class: 'ic-slip__summary' }, ...summary));
  }
  const consequences = Array.isArray(a.consequences) ? a.consequences : [];
  nodes.push(h('h4', { class: 'ic-slip__head' }, 'What the server says'));
  if (consequences.length) {
    nodes.push(
      h(
        'ul',
        { class: 'ic-slip__list' },
        ...consequences.map((c) => h('li', {}, String(c))),
      ),
    );
  } else {
    nodes.push(
      h(
        'p',
        { class: 'ic-slip__muted' },
        'The server sent no description of the effects.',
      ),
    );
  }
  return nodes;
}
