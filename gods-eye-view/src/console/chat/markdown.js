/**
 * Tiny, safe Markdown for the analyst transcript (contract §7, spec §6.2).
 *
 * The analyst's text is untrusted. It is parsed into a small AST of known
 * node types and rendered either
 *   - as DOM through `h()` with every untrusted character in a TEXT node
 *     (`renderDom`, what the view uses; no innerHTML anywhere), or
 *   - as an HTML string where every text leaf and attribute value is escaped
 *     before it lands in markup (`renderHtml`, for tests and previews).
 *
 * Supported: paragraphs (single newlines become line breaks), **bold**,
 * *italic*, `inline code`, fenced code, bullet/numbered lists (one nesting
 * level per indent), headings clamped to h4, simple pipe tables, block quotes,
 * rules, and links rendered as TEXT (never an href). Entity references
 * `[[type:id|label]]` / `[[type:id]]` become chip tokens.
 *
 * Nothing is ever decoded: `&lt;script&gt;` stays those nine characters.
 * Bidi embedding/override/isolate controls are removed before parsing, so an
 * RLO in the analyst's text cannot reverse the words around it.
 */

import { h as domH } from '../../ui/uavDom.js';
import { bidiSafe, stripBidi } from './format.js';

const h = bidiSafe(domH);

export const MAX_HEADING = 4;
const MAX_DEPTH = 6;
const MAX_SOURCE = 200_000;

// ---- escaping ------------------------------------------------------------------

const ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

// ---- entity chips ------------------------------------------------------------------

/** `[[type:id|label]]` or `[[type:id]]`. Ids may contain spaces (POI names). */
const CHIP_SOURCE =
  '\\[\\[([a-z]{2,8}):([^\\]|\\n]{1,200}?)(?:\\|([^\\]\\n]{1,200}?))?\\]\\]';

function chipRef(prefix, rest, label) {
  const id = `${prefix}:${rest.trim()}`;
  const text = (label ?? '').trim();
  return { prefix, id, label: text || null };
}

/** Every entity reference `{prefix, id, label}` in a text, in order, de-duplicated by id. */
export function chipRefs(text) {
  const seen = new Set();
  const out = [];
  const rx = new RegExp(CHIP_SOURCE, 'g');
  for (const m of String(text ?? '').matchAll(rx)) {
    const ref = chipRef(m[1], m[2], m[3]);
    if (seen.has(ref.id)) continue;
    seen.add(ref.id);
    out.push(ref);
  }
  return out;
}

/**
 * While text streams, hold back a trailing `[[…` until its `]]` arrives so a
 * half-typed reference never flashes as raw markup (spec §6.2).
 */
export function holdBackPartialChip(text) {
  const s = String(text ?? '');
  const open = s.lastIndexOf('[[');
  if (open < 0) return s;
  if (s.indexOf(']]', open) >= 0) return s;
  return s.slice(0, open);
}

// ---- inline parsing ----------------------------------------------------------------

const INLINE_RULES = [
  // Backslash escape of ASCII punctuation: literal character.
  { type: 'escape', rx: /\\([!-/:-@[-`{-~])/y },
  { type: 'code', rx: /(`+)([^`]|[^`][\s\S]{0,4000}?[^`])\1(?!`)/y },
  { type: 'chip', rx: new RegExp(CHIP_SOURCE, 'y') },
  { type: 'autolink', rx: /<((?:https?|mailto):[^\s<>]{1,500})>/y },
  {
    type: 'link',
    rx: /\[([^\]\n]{1,500})\]\(([^()\s]{0,1000})(?:\s+"[^"\n]*")?\)/y,
  },
  { type: 'strong', rx: /\*\*(?=\S)([\s\S]{0,2000}?\S)\*\*/y },
  { type: 'strong_u', rx: /__(?=\S)([\s\S]{0,2000}?\S)__(?![A-Za-z0-9])/y },
  { type: 'em', rx: /\*(?=[^\s*])([\s\S]{0,2000}?[^\s*])\*(?!\*)/y },
  { type: 'em_u', rx: /_(?=\S)([\s\S]{0,2000}?\S)_(?![A-Za-z0-9])/y },
  { type: 'br', rx: /[ \t]*\n[ \t]*/y },
];

const STARTERS = /[\\`[<*_\n]/g;

function isWordChar(ch) {
  return /[A-Za-z0-9]/.test(ch || '');
}

/** Parse inline markup into nodes: text, code, chip, strong, em, br. */
export function parseInline(text, depth = 0) {
  const src = String(text ?? '');
  const out = [];
  let buffer = '';
  const flush = () => {
    if (buffer) out.push({ type: 'text', text: buffer });
    buffer = '';
  };
  let i = 0;
  while (i < src.length) {
    STARTERS.lastIndex = i;
    const found = STARTERS.exec(src);
    if (!found) {
      buffer += src.slice(i);
      break;
    }
    if (found.index > i) buffer += src.slice(i, found.index);
    i = found.index;
    let matched = null;
    for (const rule of INLINE_RULES) {
      if (rule.type === 'em_u' || rule.type === 'strong_u') {
        // Underscore emphasis only at word boundaries: snake_case stays text.
        if (isWordChar(src[i - 1])) continue;
      }
      rule.rx.lastIndex = i;
      const m = rule.rx.exec(src);
      if (m) {
        matched = { rule, m };
        break;
      }
    }
    if (!matched) {
      buffer += src[i];
      i += 1;
      continue;
    }
    const { rule, m } = matched;
    i += m[0].length;
    const deeper = depth + 1 < MAX_DEPTH;
    switch (rule.type) {
      case 'escape':
        buffer += m[1];
        break;
      case 'code': {
        let code = m[2];
        if (/^ .* $/.test(code) && code.trim()) code = code.slice(1, -1);
        flush();
        out.push({ type: 'code', text: code });
        break;
      }
      case 'chip':
        flush();
        out.push({ type: 'chip', ...chipRef(m[1], m[2], m[3]) });
        break;
      case 'autolink':
        buffer += m[1];
        break;
      case 'link': {
        flush();
        const inner = deeper
          ? parseInline(m[1], depth + 1)
          : [{ type: 'text', text: m[1] }];
        out.push({ type: 'link', children: inner, href: m[2] });
        break;
      }
      case 'strong':
      case 'strong_u':
        flush();
        out.push({
          type: 'strong',
          children: deeper
            ? parseInline(m[1], depth + 1)
            : [{ type: 'text', text: m[1] }],
        });
        break;
      case 'em':
      case 'em_u':
        flush();
        out.push({
          type: 'em',
          children: deeper
            ? parseInline(m[1], depth + 1)
            : [{ type: 'text', text: m[1] }],
        });
        break;
      case 'br':
        flush();
        if (i < src.length) out.push({ type: 'br' });
        break;
      default:
        buffer += m[0];
    }
  }
  flush();
  return out;
}

// ---- block parsing ----------------------------------------------------------------

const FENCE = /^ {0,3}(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]{0,32})[^\n`]*$/;
const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/;
const QUOTE = /^ {0,3}>[ \t]?(.*)$/;
const LIST_ITEM = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const TABLE_SEP =
  /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

function indentOf(text) {
  let n = 0;
  for (const ch of text) {
    if (ch === ' ') n += 1;
    else if (ch === '\t') n += 4;
    else break;
  }
  return n;
}

function splitCells(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells = [];
  let cur = '';
  for (let i = 0; i < s.length; i += 1) {
    if (s[i] === '\\' && s[i + 1] === '|') {
      cur += '|';
      i += 1;
    } else if (s[i] === '|') {
      cells.push(cur.trim());
      cur = '';
    } else cur += s[i];
  }
  cells.push(cur.trim());
  return cells;
}

function isTableStart(lines, i) {
  return (
    i + 1 < lines.length &&
    lines[i].includes('|') &&
    TABLE_SEP.test(lines[i + 1]) &&
    lines[i + 1].includes('-')
  );
}

function startsBlock(lines, i) {
  const line = lines[i];
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    LIST_ITEM.test(line) ||
    isTableStart(lines, i)
  );
}

function parseList(lines, start, depth) {
  const first = LIST_ITEM.exec(lines[start]);
  const base = indentOf(first[1]);
  const ordered = /\d/.test(first[2]);
  const startNumber = ordered ? parseInt(first[2], 10) : null;
  const items = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // A blank line ends the list unless the next line continues it.
      const next = lines[i + 1];
      const m = next != null ? LIST_ITEM.exec(next) : null;
      if (m && indentOf(m[1]) >= base && /\d/.test(m[2]) === ordered) {
        i += 1;
        continue;
      }
      break;
    }
    const m = LIST_ITEM.exec(line);
    if (m && indentOf(m[1]) === base) {
      if (/\d/.test(m[2]) !== ordered) break;
      items.push({ text: m[3], children: [] });
      i += 1;
      continue;
    }
    if (m && indentOf(m[1]) > base && items.length && depth < 3) {
      const nested = parseList(lines, i, depth + 1);
      items[items.length - 1].children.push(nested.block);
      i = nested.next;
      continue;
    }
    if (!m && items.length && indentOf(line) > base && !startsBlock(lines, i)) {
      items[items.length - 1].text += `\n${line.trim()}`;
      i += 1;
      continue;
    }
    if (
      !m &&
      items.length &&
      !startsBlock(lines, i) &&
      indentOf(line) <= base
    ) {
      // Lazy continuation line.
      items[items.length - 1].text += `\n${line.trim()}`;
      i += 1;
      continue;
    }
    break;
  }
  return {
    block: {
      type: ordered ? 'ol' : 'ul',
      start: startNumber,
      items: items.map((item) => ({
        inlines: parseInline(item.text),
        children: item.children,
      })),
    },
    next: i,
  };
}

function parseBlocks(lines, depth = 0) {
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length) {
        const close = lines[i].trim();
        if (
          close.startsWith(marker[0].repeat(marker.length)) &&
          /^([`~])\1*$/.test(close)
        )
          break;
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or past the end while streaming)
      blocks.push({
        type: 'code',
        lang: fence[2] || null,
        text: body.join('\n'),
      });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({
        type: 'heading',
        level: Math.min(heading[1].length, MAX_HEADING),
        inlines: parseInline(heading[2]),
      });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      blocks.push({ type: 'hr' });
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(QUOTE.exec(lines[i])[1]);
        i += 1;
      }
      blocks.push(
        depth < 3
          ? { type: 'quote', blocks: parseBlocks(body, depth + 1) }
          : { type: 'paragraph', inlines: parseInline(body.join('\n')) },
      );
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const list = parseList(lines, i, 0);
      blocks.push(list.block);
      i = list.next;
      continue;
    }
    if (isTableStart(lines, i)) {
      const header = splitCells(lines[i]);
      const align = splitCells(lines[i + 1]).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        if (left) return 'left';
        return null;
      });
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        rows.push(splitCells(lines[i]));
        i += 1;
      }
      const width = header.length;
      blocks.push({
        type: 'table',
        align: header.map((_, c) => align[c] ?? null),
        header: header.map((cell) => parseInline(cell)),
        rows: rows.map((row) =>
          Array.from({ length: width }, (_, c) => parseInline(row[c] ?? '')),
        ),
      });
      continue;
    }
    const para = [line.trim()];
    i += 1;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
      para.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ type: 'paragraph', inlines: parseInline(para.join('\n')) });
  }
  return blocks;
}

/** Parse Markdown into a block AST. */
export function parseMarkdown(source) {
  const text = String(source ?? '')
    .slice(0, MAX_SOURCE)
    .replace(/\r\n?/g, '\n')
    .replace(/\u0000/g, '�');
  return parseBlocks(text.split('\n'));
}

// ---- HTML string rendering (escaped) -------------------------------------------------

function inlineHtml(nodes) {
  let out = '';
  for (const node of nodes) {
    switch (node.type) {
      case 'text':
        out += escapeHtml(node.text);
        break;
      case 'code':
        out += `<code>${escapeHtml(node.text)}</code>`;
        break;
      case 'chip':
        out +=
          `<span class="ic-chip-token" data-id="${escapeHtml(node.id)}">` +
          `${escapeHtml(node.label || node.id)}</span>`;
        break;
      case 'link':
        out += `<span class="ic-md-link">${inlineHtml(node.children)}</span>`;
        break;
      case 'strong':
        out += `<strong>${inlineHtml(node.children)}</strong>`;
        break;
      case 'em':
        out += `<em>${inlineHtml(node.children)}</em>`;
        break;
      case 'br':
        out += '<br>';
        break;
      default:
        break;
    }
  }
  return out;
}

function blocksHtml(blocks) {
  let out = '';
  for (const block of blocks) {
    switch (block.type) {
      case 'paragraph':
        out += `<p>${inlineHtml(block.inlines)}</p>`;
        break;
      case 'heading':
        out += `<h${block.level}>${inlineHtml(block.inlines)}</h${block.level}>`;
        break;
      case 'hr':
        out += '<hr>';
        break;
      case 'code':
        out += `<pre><code${block.lang ? ` data-lang="${escapeHtml(block.lang)}"` : ''}>${escapeHtml(block.text)}</code></pre>`;
        break;
      case 'quote':
        out += `<blockquote>${blocksHtml(block.blocks)}</blockquote>`;
        break;
      case 'ul':
      case 'ol': {
        const start =
          block.type === 'ol' &&
          Number.isFinite(block.start) &&
          block.start !== 1
            ? ` start="${block.start}"`
            : '';
        out += `<${block.type}${start}>`;
        for (const item of block.items) {
          out += `<li>${inlineHtml(item.inlines)}${blocksHtml(item.children)}</li>`;
        }
        out += `</${block.type}>`;
        break;
      }
      case 'table': {
        const cell = (tag, inlines, c) =>
          `<${tag}${block.align[c] ? ` data-align="${block.align[c]}"` : ''}>${inlineHtml(inlines)}</${tag}>`;
        out += '<table><thead><tr>';
        block.header.forEach((inl, c) => {
          out += cell('th', inl, c);
        });
        out += '</tr></thead><tbody>';
        for (const row of block.rows) {
          out += '<tr>';
          row.forEach((inl, c) => {
            out += cell('td', inl, c);
          });
          out += '</tr>';
        }
        out += '</tbody></table>';
        break;
      }
      default:
        break;
    }
  }
  return out;
}

/** Render Markdown to an escaped HTML string. */
export function renderHtml(source) {
  return blocksHtml(parseMarkdown(stripBidi(String(source ?? ''))));
}

// ---- DOM rendering (text nodes only) -------------------------------------------------

function defaultChip(ref) {
  return h(
    'span',
    { class: 'ic-chip-token', 'data-id': ref.id },
    ref.label || ref.id,
  );
}

function inlineDom(nodes, opts) {
  const out = [];
  for (const node of nodes) {
    switch (node.type) {
      case 'text':
        out.push(node.text);
        break;
      case 'code':
        out.push(h('code', {}, node.text));
        break;
      case 'chip':
        out.push((opts.chip || defaultChip)(node));
        break;
      case 'link':
        out.push(
          h('span', { class: 'ic-md-link' }, ...inlineDom(node.children, opts)),
        );
        break;
      case 'strong':
        out.push(h('strong', {}, ...inlineDom(node.children, opts)));
        break;
      case 'em':
        out.push(h('em', {}, ...inlineDom(node.children, opts)));
        break;
      case 'br':
        out.push(h('br'));
        break;
      default:
        break;
    }
  }
  return out;
}

function blocksDom(blocks, opts) {
  const out = [];
  for (const block of blocks) {
    switch (block.type) {
      case 'paragraph':
        out.push(h('p', {}, ...inlineDom(block.inlines, opts)));
        break;
      case 'heading':
        out.push(h(`h${block.level}`, {}, ...inlineDom(block.inlines, opts)));
        break;
      case 'hr':
        out.push(h('hr'));
        break;
      case 'code':
        out.push(
          h(
            'pre',
            {},
            h(
              'code',
              block.lang ? { 'data-lang': block.lang } : {},
              block.text,
            ),
          ),
        );
        break;
      case 'quote':
        out.push(h('blockquote', {}, ...blocksDom(block.blocks, opts)));
        break;
      case 'ul':
      case 'ol': {
        const attrs =
          block.type === 'ol' &&
          Number.isFinite(block.start) &&
          block.start !== 1
            ? { start: block.start }
            : {};
        out.push(
          h(
            block.type,
            attrs,
            ...block.items.map((item) =>
              h(
                'li',
                {},
                ...inlineDom(item.inlines, opts),
                ...blocksDom(item.children, opts),
              ),
            ),
          ),
        );
        break;
      }
      case 'table': {
        const cell = (tag, inlines, c) =>
          h(
            tag,
            block.align[c] ? { 'data-align': block.align[c] } : {},
            ...inlineDom(inlines, opts),
          );
        out.push(
          h(
            'div',
            { class: 'ic-md-table' },
            h(
              'table',
              {},
              h(
                'thead',
                {},
                h(
                  'tr',
                  {},
                  ...block.header.map((inl, c) => cell('th', inl, c)),
                ),
              ),
              h(
                'tbody',
                {},
                ...block.rows.map((row) =>
                  h('tr', {}, ...row.map((inl, c) => cell('td', inl, c))),
                ),
              ),
            ),
          ),
        );
        break;
      }
      default:
        break;
    }
  }
  return out;
}

/**
 * Render Markdown to DOM nodes. `opts.chip(ref)` builds a chip for an entity
 * reference `{type:'chip', prefix, id, label}`; `opts.streaming` holds back a partial `[[`.
 */
export function renderDom(source, opts = {}) {
  const clean = stripBidi(String(source ?? ''));
  const text = opts.streaming ? holdBackPartialChip(clean) : clean;
  return blocksDom(parseMarkdown(text), opts);
}
