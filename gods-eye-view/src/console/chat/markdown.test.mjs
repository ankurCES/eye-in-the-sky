import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_HEADING,
  chipRefs,
  escapeHtml,
  holdBackPartialChip,
  parseInline,
  parseMarkdown,
  renderDom,
  renderHtml,
} from './markdown.js';

// ---- a stub document: elements record tag, attrs and children only ----------

function stubDoc() {
  const mk = (tag) => ({
    tag,
    children: [],
    attrs: {},
    className: '',
    append(...kids) {
      this.children.push(...kids);
    },
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    getAttribute(k) {
      return this.attrs[k];
    },
  });
  return { createElement: mk };
}

function withDoc(fn) {
  const prev = globalThis.document;
  globalThis.document = stubDoc();
  try {
    return fn();
  } finally {
    globalThis.document = prev;
  }
}

function walk(nodes, visit) {
  for (const node of nodes) {
    visit(node);
    if (node && typeof node === 'object') walk(node.children || [], visit);
  }
}

const SAFE_TAGS = new Set([
  'p',
  'h1',
  'h2',
  'h3',
  'h4',
  'ul',
  'ol',
  'li',
  'pre',
  'code',
  'strong',
  'em',
  'br',
  'hr',
  'blockquote',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
  'div',
  'span',
]);
const SAFE_ATTRS = new Set([
  'data-id',
  'data-lang',
  'data-align',
  'start',
  'class',
]);

/** Every node is a known tag with known attributes; all text is text. */
function assertSafeDom(nodes) {
  walk(nodes, (node) => {
    if (typeof node === 'string') return;
    assert.ok(SAFE_TAGS.has(node.tag), `unexpected tag <${node.tag}>`);
    assert.equal(node.innerHTML, undefined, 'innerHTML is never used');
    for (const key of Object.keys(node.attrs)) {
      assert.ok(SAFE_ATTRS.has(key), `unexpected attribute ${key}`);
      assert.doesNotMatch(key, /^on/i);
    }
  });
}

function textOf(nodes) {
  let out = '';
  walk(nodes, (node) => {
    if (typeof node === 'string') out += node;
  });
  return out;
}

/** The escaped HTML has no live markup beyond the renderer's own tags. */
function assertSafeHtml(html) {
  const tags = [...html.matchAll(/<\/?([a-zA-Z0-9]+)([^>]*)>/g)];
  for (const [, tag, attrs] of tags) {
    assert.ok(
      SAFE_TAGS.has(tag.toLowerCase()),
      `unexpected tag <${tag}> in ${html}`,
    );
    // Attributes are name="value" with no raw quote inside the value.
    const rest = attrs.replace(/\s([a-zA-Z-]+)="([^"<>]*)"/g, (_, name) => {
      assert.ok(
        SAFE_ATTRS.has(name),
        `unexpected attribute ${name} in ${html}`,
      );
      return '';
    });
    assert.equal(rest.trim(), '', `unparsed attribute text in ${html}`);
  }
  assert.doesNotMatch(html, /<script/i);
  // Event handlers, URLs and styles may appear only as escaped TEXT, never
  // inside a real tag (checked above): text has no raw '<' or '"'.
  const text = html.replace(/<\/?[a-zA-Z0-9]+(?:\s[^>]*)?>/g, '');
  assert.doesNotMatch(text, /[<>"]/, `raw markup characters in text: ${html}`);
}

// ---- features ------------------------------------------------------------------

test('paragraphs, bold, italic, inline code and line breaks', () => {
  assert.equal(
    renderHtml('Hello **bold** and *it* and `code`\nnext line\n\nSecond'),
    '<p>Hello <strong>bold</strong> and <em>it</em> and <code>code</code><br>next line</p><p>Second</p>',
  );
  assert.equal(
    renderHtml('__strong__ _em_'),
    '<p><strong>strong</strong> <em>em</em></p>',
  );
});

test('snake_case identifiers are never italicized', () => {
  assert.equal(
    renderHtml('Call uav_scan_targets then mission_grid_search.'),
    '<p>Call uav_scan_targets then mission_grid_search.</p>',
  );
  assert.equal(renderHtml('a_b_c and 2*3*4'), '<p>a_b_c and 2<em>3</em>4</p>');
});

test('headings are clamped to h4', () => {
  assert.equal(MAX_HEADING, 4);
  assert.equal(renderHtml('# One'), '<h1>One</h1>');
  assert.equal(renderHtml('#### Four'), '<h4>Four</h4>');
  assert.equal(renderHtml('###### Six ##'), '<h4>Six</h4>');
});

test('lists: bullets, numbers, a start number and one nested level', () => {
  assert.equal(
    renderHtml('- one\n- two\n  - nested\n- three'),
    '<ul><li>one</li><li>two<ul><li>nested</li></ul></li><li>three</li></ul>',
  );
  assert.equal(renderHtml('1. a\n2. b'), '<ol><li>a</li><li>b</li></ol>');
  assert.equal(
    renderHtml('3) c\n4) d'),
    '<ol start="3"><li>c</li><li>d</li></ol>',
  );
  assert.equal(
    renderHtml('- a\ncontinued'),
    '<ul><li>a<br>continued</li></ul>',
    'a lazy continuation line stays in the item',
  );
});

test('fenced code keeps its text verbatim and escaped', () => {
  assert.equal(
    renderHtml('```json\n{"a": "<b>"}\n```\nafter'),
    '<pre><code data-lang="json">{&quot;a&quot;: &quot;&lt;b&gt;&quot;}</code></pre><p>after</p>',
  );
  // Unterminated while streaming: everything after the fence is code.
  assert.equal(renderHtml('```\npartial'), '<pre><code>partial</code></pre>');
});

test('simple tables with alignment', () => {
  assert.equal(
    renderHtml('| Contact | Threat |\n|:---|---:|\n| SA-6 | High |\n| Truck |'),
    '<table><thead><tr><th data-align="left">Contact</th><th data-align="right">Threat</th></tr></thead>' +
      '<tbody><tr><td data-align="left">SA-6</td><td data-align="right">High</td></tr>' +
      '<tr><td data-align="left">Truck</td><td data-align="right"></td></tr></tbody></table>',
  );
});

test('block quotes and rules', () => {
  assert.equal(
    renderHtml('> quoted **x**'),
    '<blockquote><p>quoted <strong>x</strong></p></blockquote>',
  );
  assert.equal(renderHtml('a\n\n---\n\nb'), '<p>a</p><hr><p>b</p>');
});

test('links render as text only: never an href', () => {
  assert.equal(
    renderHtml('See [the report](https://example.com/r?a=1) now'),
    '<p>See <span class="ic-md-link">the report</span> now</p>',
  );
  assert.equal(
    renderHtml('<https://example.com>'),
    '<p>https://example.com</p>',
  );
  const ast = parseInline('[x](https://a.b)');
  assert.equal(ast[0].type, 'link');
});

test('backslash escapes produce the literal character', () => {
  assert.equal(
    renderHtml('\\*not italic\\* and \\[\\[veh:X\\]\\]'),
    '<p>*not italic* and [[veh:X]]</p>',
  );
});

test('entity references become chip tokens', () => {
  assert.equal(
    renderHtml(
      'Near [[poi:default:North Field|North Field]], see [[trk:T-3fa9c1|SA-6 battery]] and [[veh:Drone1]].',
    ),
    '<p>Near <span class="ic-chip-token" data-id="poi:default:North Field">North Field</span>, see ' +
      '<span class="ic-chip-token" data-id="trk:T-3fa9c1">SA-6 battery</span> and ' +
      '<span class="ic-chip-token" data-id="veh:Drone1">veh:Drone1</span>.</p>',
  );
  const nodes = parseInline('[[msn:MSN-1a2b3c4d]]');
  assert.deepEqual(nodes, [
    { type: 'chip', prefix: 'msn', id: 'msn:MSN-1a2b3c4d', label: null },
  ]);
});

test('chipRefs lists references in order without duplicates', () => {
  assert.deepEqual(
    chipRefs('[[veh:Drone1]] then [[trk:T-1|SA-6]] and [[veh:Drone1|Lead]]'),
    [
      { prefix: 'veh', id: 'veh:Drone1', label: null },
      { prefix: 'trk', id: 'trk:T-1', label: 'SA-6' },
    ],
  );
  assert.deepEqual(chipRefs(null), []);
});

test('a partial reference is held back while streaming', () => {
  assert.equal(holdBackPartialChip('Look at [[trk:T-3fa'), 'Look at ');
  assert.equal(
    holdBackPartialChip('Look at [[trk:T-3fa9c1|SA-6]] and [[veh'),
    'Look at [[trk:T-3fa9c1|SA-6]] and ',
  );
  assert.equal(
    holdBackPartialChip('Done [[veh:Drone1]].'),
    'Done [[veh:Drone1]].',
  );
  withDoc(() => {
    const nodes = renderDom('Look at [[trk:T-3f', { streaming: true });
    assert.equal(textOf(nodes), 'Look at');
    const full = renderDom('Look at [[trk:T-3f');
    assert.equal(
      textOf(full),
      'Look at [[trk:T-3f',
      'not streaming: shown as typed',
    );
  });
});

test('renderDom builds elements with text nodes and calls the chip factory', () => {
  withDoc(() => {
    const seen = [];
    const nodes = renderDom('**Two** contacts: [[trk:T-1|SA-6]]\n\n- `x`', {
      chip: (ref) => {
        seen.push(ref);
        return document.createElement('button');
      },
    });
    assert.equal(nodes.length, 2);
    assert.equal(nodes[0].tag, 'p');
    assert.equal(nodes[0].children[0].tag, 'strong');
    assert.deepEqual(nodes[0].children[0].children, ['Two']);
    assert.equal(nodes[1].tag, 'ul');
    assert.equal(seen.length, 1);
    assert.equal(seen[0].id, 'trk:T-1');
    assert.equal(seen[0].label, 'SA-6');
  });
});

// ---- XSS payloads ------------------------------------------------------------------

const PAYLOADS = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<a href="javascript:alert(1)">click</a>',
  '[click](javascript:alert(1))',
  '[click](javascript:void)',
  '<javascript:alert(1)>',
  '&lt;script&gt;alert(1)&lt;/script&gt;',
  '&#60;script&#62;alert(1)&#60;/script&#62;',
  '&#x6A;avascript:alert(1)',
  '<svg/onload=alert(1)>',
  '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
  '**<b onclick="x()">bold</b>**',
  '*<i>em</i>*',
  '`<script>code</script>`',
  '```html\n<script>alert(1)</script>\n```',
  '| a | <img src=x onerror=1> |\n|---|---|\n| <script> | "q" |',
  '# <h1 onmouseover=alert(1)>',
  '- <li onclick=x>',
  '> <blockquote style="x">',
  '[[trk:x" onmouseover="alert(1)|<img src=x onerror=alert(1)>]]',
  '[[veh:"><script>alert(1)</script>]]',
  '[**[x](javascript:alert(1))**](javascript:y)',
  '<<script>script>alert(1)<</script>/script>',
  '"\'><svg onload=alert(1)>',
  '\u0000<script>',
];

test('XSS payloads never become markup in the HTML renderer', () => {
  for (const payload of PAYLOADS) {
    const html = renderHtml(payload);
    assertSafeHtml(html);
  }
  // Entity-encoded tricks are never decoded: they stay literal characters.
  assert.equal(renderHtml('&lt;script&gt;'), '<p>&amp;lt;script&amp;gt;</p>');
  assert.equal(renderHtml('&#106;avascript:x'), '<p>&amp;#106;avascript:x</p>');
  // The chip attribute is quoted and escaped.
  assert.equal(
    renderHtml('[[trk:x" onmouseover="alert(1)|<b>l</b>]]'),
    '<p><span class="ic-chip-token" data-id="trk:x&quot; onmouseover=&quot;alert(1)">&lt;b&gt;l&lt;/b&gt;</span></p>',
  );
});

test('XSS payloads never become markup in the DOM renderer', () => {
  withDoc(() => {
    for (const payload of PAYLOADS) {
      const nodes = renderDom(payload);
      assertSafeDom(nodes);
    }
    const nodes = renderDom(
      '<img src=x onerror=alert(1)> and <script>alert(2)</script>',
    );
    assert.equal(
      textOf(nodes),
      '<img src=x onerror=alert(1)> and <script>alert(2)</script>',
    );
    const chip = renderDom('[[trk:x" onmouseover="y|<b>l</b>]]')[0].children[0];
    assert.equal(chip.attrs['data-id'], 'trk:x" onmouseover="y');
    assert.deepEqual(chip.children, ['<b>l</b>']);
  });
});

test('bidi controls in the analyst text are removed before rendering (review: RLO reversed the words after it)', () => {
  const src =
    'Orbit **Drone1 \u202eynneD** then [[trk:T-1|BMP \u202eYLDNEIRF\u202c]] and `x\u2066y`';
  withDoc(() => {
    const nodes = renderDom(src, { streaming: true });
    const text = textOf(nodes);
    assert.doesNotMatch(text, /[\u202a-\u202e\u2066-\u2069]/);
    assert.ok(text.includes('Drone1 ynneD'));
    assert.ok(text.includes('BMP YLDNEIRF'));
    assert.ok(text.includes('xy'));
    assert.deepEqual(renderDom(null), []);
  });
  assert.doesNotMatch(renderHtml(src), /[\u202a-\u202e\u2066-\u2069]/);
});

test('escapeHtml escapes the five characters', () => {
  assert.equal(
    escapeHtml(`<a href="x" title='y'>&</a>`),
    '&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;',
  );
  assert.equal(escapeHtml(null), '');
});

test('pathological input stays fast', () => {
  const inputs = [
    '*'.repeat(50_000) + 'a',
    '**a '.repeat(10_000),
    '_'.repeat(30_000),
    '`'.repeat(20_000),
    '[['.repeat(20_000),
    '['.repeat(20_000) + ']('.repeat(10_000),
    '- '.repeat(10_000),
    '|'.repeat(10_000) + '\n' + '|---'.repeat(3000),
  ];
  for (const input of inputs) {
    const start = Date.now();
    renderHtml(input);
    assert.ok(Date.now() - start < 2000, `slow on ${input.slice(0, 20)}…`);
  }
});

test('the AST exposes only known block types', () => {
  const blocks = parseMarkdown(
    '# h\n\ntext\n\n- a\n\n1. b\n\n```\nc\n```\n\n> q\n\n---\n\n| a |\n|---|\n| b |',
  );
  assert.deepEqual(
    blocks.map((b) => b.type),
    ['heading', 'paragraph', 'ul', 'ol', 'code', 'quote', 'hr', 'table'],
  );
});
