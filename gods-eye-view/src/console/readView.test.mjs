import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  READ_COPY,
  READ_MAX_CHARS,
  createReadView,
  isAar,
  readDocOf,
} from './readView.js';

// ---- a stub document (uavDom's h() needs only createElement) ----------------------

function stubDom() {
  const doc = { activeElement: null, created: [] };
  const mk = (tag) => {
    const el = {
      tag,
      tagName: String(tag).toUpperCase(),
      children: [],
      attrs: {},
      listeners: {},
      className: '',
      textContent: '',
      parent: null,
      append(...kids) {
        for (const k of kids) {
          if (k && typeof k === 'object') k.parent = this;
          this.children.push(k);
        }
      },
      replaceChildren(...kids) {
        this.children = [];
        this.append(...kids);
      },
      remove() {
        if (this.parent)
          this.parent.children = this.parent.children.filter((c) => c !== this);
        this.parent = null;
      },
      setAttribute(k, v) {
        this.attrs[k] = String(v);
      },
      removeAttribute(k) {
        delete this.attrs[k];
      },
      getAttribute(k) {
        return k in this.attrs ? this.attrs[k] : null;
      },
      addEventListener(t, f) {
        (this.listeners[t] ||= []).push(f);
      },
      fire(t) {
        for (const f of this.listeners[t] || []) f({ target: this });
      },
      focus() {
        doc.activeElement = this;
      },
    };
    // innerHTML must never be used: a setter that fails the test if it is.
    Object.defineProperty(el, 'innerHTML', {
      set() {
        throw new Error('innerHTML used');
      },
      get() {
        return '';
      },
    });
    doc.created.push(el);
    return el;
  };
  doc.createElement = mk;
  return doc;
}

function withDom(fn) {
  return async () => {
    const saved = globalThis.document;
    const doc = stubDom();
    globalThis.document = doc;
    try {
      await fn(doc);
    } finally {
      globalThis.document = saved;
    }
  };
}

function textOf(el) {
  if (el == null || el === false) return '';
  if (typeof el !== 'object') return String(el);
  if (el.attrs && 'hidden' in el.attrs) return '';
  return [el.textContent || '', ...(el.children || []).map(textOf)]
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function all(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const kid of root.children || []) all(kid, pred, out);
  return out;
}

const byClass = (root, cls) =>
  all(root, (el) =>
    String(el.className || '')
      .split(/\s+/)
      .includes(cls),
  )[0] || null;
const isHidden = (el) => Boolean(el && 'hidden' in el.attrs);

const AAR_MD = [
  '# After-action review (simulated)',
  '',
  '## Summary',
  '',
  'Session WG-1a2b3c, seed 4417. **Loss exchange** 1 : 0.',
  '',
  '| Time (Z) | Event | Side | Outcome | Register |',
  '| --- | --- | --- | --- | --- |',
  '| 14:05:12Z | Red SAM 2 engaged Drone1 | red | missed | Scenario |',
].join('\n');

const AAR_ENTITY = {
  id: 'rpt:aar-WG-1a2b3c',
  type: 'report',
  label: 'Report aar-WG-1a2b3c',
  attrs: { format: 'AAR' },
  fields: {
    report_type: 'AAR',
    markdown: AAR_MD,
    // Fields the read view must never draw.
    timeline: [{ text: 'NOT-IN-THE-READ-VIEW' }],
    note: 'NOT-IN-THE-READ-VIEW',
  },
};

// ---- the model -------------------------------------------------------------------

test('isAar reads the report type, the header format or the node format', () => {
  assert.equal(isAar({ entity: AAR_ENTITY }), true);
  assert.equal(
    isAar({ entity: { fields: { header: { format: 'AAR' } } } }),
    true,
  );
  assert.equal(isAar({ node: { attrs: { format: 'AAR' } } }), true);
  assert.equal(isAar({ entity: { fields: { report_type: 'SALUTE' } } }), false);
  assert.equal(isAar(), false);
});

test('readDocOf takes fields.markdown only, and titles an AAR as simulated', () => {
  assert.deepEqual(readDocOf({ id: 'rpt:aar-WG-1a2b3c', entity: AAR_ENTITY }), {
    id: 'rpt:aar-WG-1a2b3c',
    title: 'After-action review (simulated)',
    markdown: AAR_MD,
    simulated: true,
  });
  // A plain report keeps its own (safe) title and is not marked simulated.
  const plain = readDocOf({
    id: 'rpt:latest',
    title: 'Target report ‮evil',
    markdown: 'Text.',
  });
  assert.equal(plain.title, 'Target report evil');
  assert.equal(plain.simulated, false);
  assert.equal(readDocOf({ markdown: 'x' }).title, READ_COPY.reportTitle);
  // Nothing to read: no document.
  assert.equal(readDocOf({ id: 'rpt:x' }), null);
  assert.equal(readDocOf({ markdown: '   ' }), null);
  assert.equal(readDocOf({ entity: { fields: { markdown: 42 } } }), null);
  assert.equal(readDocOf(null), null);
  // The source is capped at the renderer's own limit.
  assert.equal(
    readDocOf({ markdown: 'a'.repeat(READ_MAX_CHARS + 50) }).markdown.length,
    READ_MAX_CHARS,
  );
});

// ---- the view --------------------------------------------------------------------

test(
  'the read view renders the Markdown through chat/markdown.js, and nothing else',
  withDom(() => {
    const host = globalThis.document.createElement('div');
    const view = createReadView(host);
    assert.equal(host.children[0], view.element);
    assert.equal(view.element.attrs['data-view'], 'read');
    assert.ok(isHidden(view.element));
    assert.equal(view.open({ id: AAR_ENTITY.id, entity: AAR_ENTITY }), true);
    assert.ok(!isHidden(view.element));
    assert.equal(view.isOpen(), true);
    const title = byClass(view.element, 'ic-read__title');
    assert.equal(textOf(title), 'After-action review (simulated)');
    assert.equal(view.element.attrs['aria-labelledby'], title.attrs.id);
    assert.equal(
      textOf(byClass(view.element, 'ic-read__meta')),
      'Simulated rpt:aar-WG-1a2b3c',
    );
    const body = byClass(view.element, 'ic-read__body');
    assert.match(body.className, /\bic-md\b/);
    // Markdown structure: headings (clamped), a paragraph with bold, a table.
    assert.ok(all(body, (el) => el.tag === 'h1').length === 1);
    assert.ok(all(body, (el) => el.tag === 'strong').length === 1);
    assert.ok(all(body, (el) => el.tag === 'table').length === 1);
    assert.match(textOf(body), /Red SAM 2 engaged Drone1/);
    // No other field of the report is drawn.
    assert.doesNotMatch(textOf(view.element), /NOT-IN-THE-READ-VIEW/);
  }),
);

test(
  'untrusted Markdown stays text: no img, no onerror, no href, no bidi',
  withDom((doc) => {
    const host = doc.createElement('div');
    const view = createReadView(host);
    view.open({
      id: 'rpt:aar-WG-x',
      aar: true,
      markdown:
        "<img src=x onerror=alert(1)>\n\n[click](javascript:alert(1)) ‮evil‬\n\n<script>alert('x')</script>",
    });
    const tags = doc.created.map((el) => el.tag);
    assert.ok(!tags.includes('img'), 'no img element');
    assert.ok(!tags.includes('script'), 'no script element');
    assert.ok(!tags.includes('a'), 'links are text');
    for (const el of doc.created) {
      assert.ok(!('onerror' in el.attrs), 'no onerror attribute');
      assert.ok(!('href' in el.attrs), 'no URL attribute');
      assert.ok(!('src' in el.attrs), 'no URL attribute');
    }
    const text = textOf(view.element);
    assert.match(text, /<img src=x onerror=alert\(1\)>/);
    assert.match(text, /<script>alert\('x'\)<\/script>/);
    assert.doesNotMatch(text, /[‪-‮⁦-⁩]/);
  }),
);

test(
  'Back calls the shell; close hides and empties; focus lands on the view',
  withDom((doc) => {
    const host = doc.createElement('div');
    let backs = 0;
    const view = createReadView(host, { onBack: () => (backs += 1) });
    assert.equal(view.open({ id: 'rpt:x' }), false, 'nothing to read');
    assert.ok(isHidden(view.element));
    view.open({ markdown: 'Some text.', title: 'Report rpt:x' });
    view.focus();
    assert.equal(doc.activeElement, view.element);
    assert.equal(view.element.attrs.tabindex, '-1');
    const back = byClass(view.element, 'ic-read__back');
    assert.equal(back.attrs['aria-keyshortcuts'], 'Escape');
    assert.match(textOf(back), /Back$/);
    back.fire('click');
    assert.equal(backs, 1);
    view.close();
    assert.ok(isHidden(view.element));
    assert.equal(view.isOpen(), false);
    assert.equal(view.current(), null);
    assert.equal(
      textOf(byClass(view.element, 'ic-read__body')),
      '',
      'the text is dropped on close',
    );
  }),
);

test('readView.js never touches innerHTML and renders only via renderDom', () => {
  const src = readFileSync(new URL('./readView.js', import.meta.url), 'utf8');
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert.doesNotMatch(code, /innerHTML|outerHTML|insertAdjacentHTML/);
  assert.match(code, /import \{ renderDom \} from '\.\/chat\/markdown\.js';/);
  assert.doesNotMatch(code, /renderHtml/);
});
