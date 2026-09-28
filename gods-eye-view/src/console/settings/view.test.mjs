import test from 'node:test';
import assert from 'node:assert/strict';

import { createSettingsSheet, focusables } from './view.js';
import { COPY } from './model.js';

// ---- stub DOM (uavDom's h() reads globalThis.document) ---------------------------

function stubDom() {
  const doc = { activeElement: null };
  const mk = (tag) => {
    const el = {
      tag,
      tagName: String(tag).toUpperCase(),
      children: [],
      attrs: {},
      listeners: {},
      className: '',
      _text: '',
      get textContent() {
        return this._text;
      },
      set textContent(v) {
        for (const c of this.children)
          if (c && typeof c === 'object') c.parent = null;
        this.children = [];
        this._text = String(v ?? '');
      },
      value: '',
      checked: false,
      type: '',
      parent: null,
      isConnected: true,
      append(...kids) {
        for (const k of kids) {
          if (k && typeof k === 'object') {
            k.parent?.detach?.(k);
            k.parent = this;
          }
          this.children.push(k);
        }
      },
      detach(k) {
        this.children = this.children.filter((c) => c !== k);
      },
      replaceChildren(...kids) {
        for (const c of this.children)
          if (c && typeof c === 'object') c.parent = null;
        this.children = [];
        this.append(...kids);
      },
      remove() {
        this.parent?.detach(this);
        this.parent = null;
      },
      setAttribute(k, v) {
        this.attrs[k] = String(v);
        if (k === 'type') this.type = String(v);
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
      removeEventListener(t, f) {
        this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== f);
      },
      fire(t, ev = {}) {
        const event = {
          target: this,
          defaultPrevented: false,
          preventDefault() {
            this.defaultPrevented = true;
          },
          stopPropagation() {},
          ...ev,
        };
        for (let n = this; n; n = n.parent)
          for (const f of [...(n.listeners[t] || [])]) f(event);
        return event;
      },
      click() {
        return this.fire('click');
      },
      focus() {
        doc.activeElement = this;
      },
      contains(x) {
        for (let n = x; n; n = n.parent) if (n === this) return true;
        return false;
      },
    };
    return el;
  };
  doc.createElement = mk;
  doc.body = mk('body');
  return doc;
}

globalThis.document = stubDom();
const doc = globalThis.document;

// ---- tree helpers ---------------------------------------------------------------------

function all(root, pred, out = []) {
  if (!root || typeof root !== 'object') return out;
  if (pred(root)) out.push(root);
  for (const kid of root.children || []) all(kid, pred, out);
  return out;
}
const find = (root, pred) => all(root, pred)[0] || null;
const byFk = (root, key) => find(root, (el) => el.attrs?.['data-fk'] === key);
const byClass = (root, cls) =>
  find(root, (el) =>
    String(el.className || '')
      .split(/\s+/)
      .includes(cls),
  );
const hidden = (el) => {
  for (let n = el; n; n = n.parent)
    if (n.attrs && 'hidden' in n.attrs) return true;
  return false;
};
function textOf(el) {
  if (el == null || el === false) return '';
  if (typeof el !== 'object') return String(el);
  if (el.attrs && 'hidden' in el.attrs) return '';
  return [el.textContent || '', ...(el.children || []).map(textOf)]
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}
/** Every string the sheet would put on screen, hidden or not. */
function allText(el) {
  if (el == null || typeof el !== 'object') return String(el ?? '');
  return [
    el.textContent || '',
    el.value || '',
    ...(el.children || []).map(allText),
  ].join(' ');
}
const flush = () => new Promise((r) => setTimeout(r, 0));

// ---- fixture: GET /settings/llm (BYOK spec §7.1) ---------------------------------------

function provider(over) {
  return {
    kind: 'anthropic_compatible',
    group: 'compatible',
    model_family: 'non_claude',
    base_url: null,
    host: null,
    model: '',
    small_model: null,
    thinking: 'auto',
    models: { default: null, small_default: null, suggestions: [] },
    fields: [],
    values: {},
    auth: { scheme: 'bearer', key_label: null, key_optional: false },
    key: { configured: false, source: 'none', masked: null, env: null },
    status: 'not_configured',
    tested: null,
    notes: [],
    docs_url: null,
    quick_check: false,
    ...over,
  };
}

function fixture() {
  return {
    schema: 'eye-in-the-sky.llm-settings/1',
    rev: 7,
    active: 'anthropic_login',
    read_only: false,
    locks: { provider: null, model: null },
    key_store: { kind: 'keychain', label: 'macOS Keychain', path: null },
    providers: [
      provider({
        id: 'anthropic_login',
        label: 'Claude login (this Mac)',
        kind: 'anthropic_login',
        group: 'claude',
        model_family: 'claude',
        model: 'claude-opus-5-5',
        auth: { scheme: 'login' },
        key: { configured: true, source: 'login' },
        status: 'active',
        thinking: 'adaptive',
      }),
      provider({
        id: 'anthropic_api',
        label: 'Anthropic API key',
        kind: 'anthropic_key',
        group: 'claude',
        model_family: 'claude',
        model: 'claude-opus-5-5',
        host: 'api.anthropic.com',
        models: {
          default: 'claude-opus-5-5',
          suggestions: ['claude-sonnet-5'],
        },
        auth: { scheme: 'x-api-key', key_label: 'Anthropic API key' },
        quick_check: true,
      }),
      provider({
        id: 'bedrock',
        label: 'Amazon Bedrock',
        kind: 'bedrock',
        group: 'cloud',
        model_family: 'claude',
        model: 'us.anthropic.claude-opus-5-5',
        host: 'us-east-1',
        auth: {
          scheme: 'cloud',
          key_label: 'Bedrock API key',
          key_optional: true,
        },
        fields: [
          {
            id: 'region',
            label: 'AWS region',
            required: true,
            pattern: '^[a-z]{2}(-gov)?-[a-z]+-\\d$',
          },
          { id: 'profile', label: 'AWS profile', required: false },
        ],
        values: { region: 'us-east-1' },
      }),
      provider({
        id: 'vertex',
        label: 'Google Cloud Agent Platform (Vertex AI)',
        kind: 'vertex',
        group: 'cloud',
        model_family: 'claude',
        model: 'claude-opus-5-5',
        auth: { scheme: 'cloud', key_optional: true },
        fields: [
          { id: 'project', label: 'Google Cloud project', required: true },
          { id: 'region', label: 'Region', required: false, default: 'global' },
          {
            id: 'credentials_file',
            label: 'Credentials file',
            required: false,
            type: 'file',
          },
        ],
      }),
      provider({
        id: 'openrouter',
        label: 'OpenRouter',
        model_family: 'mixed',
        model: 'anthropic/claude-opus-5.5',
        host: 'openrouter.ai',
        base_url: {
          value: 'https://openrouter.ai/api',
          editable: false,
          presets: [],
        },
        models: {
          default: 'anthropic/claude-opus-5.5',
          suggestions: ['minimax/minimax-m3', 'openai/gpt-6-sol'],
        },
        auth: { scheme: 'bearer', key_label: 'OpenRouter API key' },
        key: {
          configured: true,
          source: 'keychain',
          masked: '…9f2a',
          env: null,
        },
        status: 'key_saved',
        notes: [
          "Anthropic doesn't support routing Claude Code to non-Claude models through any gateway.",
        ],
        docs_url:
          'https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration',
        quick_check: { kind: 'key_info', billable: false },
      }),
      provider({
        id: 'minimax',
        label: 'MiniMax',
        model: 'MiniMax-M3[1m]',
        host: 'api.minimax.io',
        base_url: {
          value: 'https://api.minimax.io/anthropic',
          editable: false,
          presets: [
            { label: 'International', url: 'https://api.minimax.io/anthropic' },
            { label: 'China', url: 'https://api.minimax.cn/anthropic' },
          ],
        },
        quick_check: { kind: 'messages', billable: true },
      }),
      provider({
        id: 'alibaba',
        label: 'Alibaba Model Studio (Qwen)',
        model: 'qwen3.7-max',
        base_url: {
          value: null,
          editable: true,
          presets: [
            {
              label: 'Singapore',
              url: 'https://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/apps/anthropic',
            },
            {
              label: 'Pay-as-you-go (Beijing)',
              url: 'https://dashscope.aliyuncs.com/apps/anthropic',
            },
          ],
        },
        fields: [
          { id: 'workspace_id', label: 'Workspace ID', required: false },
        ],
        quick_check: { kind: 'messages', billable: true },
      }),
      provider({
        id: 'lmstudio',
        label: 'LM Studio',
        model: 'openai/gpt-oss-20b',
        base_url: {
          value: 'http://localhost:1234',
          editable: true,
          presets: [],
        },
        auth: { scheme: 'bearer', key_optional: true },
        models: {
          default: null,
          suggestions: [],
          placeholder: 'openai/gpt-oss-20b',
        },
        quick_check: { kind: 'messages', billable: true },
      }),
      provider({
        id: 'custom',
        label: 'Custom Anthropic-compatible endpoint',
        kind: 'custom',
        group: 'custom',
        model: '',
        base_url: { value: '', editable: true, presets: [] },
        auth: { scheme: 'bearer', key_label: 'API key' },
        quick_check: { kind: 'messages', billable: true },
      }),
    ],
  };
}

// ---- fakes ------------------------------------------------------------------------------

function fakeBus() {
  const handlers = {};
  const emitted = [];
  return {
    emitted,
    on(name, fn) {
      (handlers[name] ||= []).push(fn);
      return () => {
        handlers[name] = handlers[name].filter((f) => f !== fn);
      };
    },
    emit(name, payload) {
      emitted.push([name, payload]);
      for (const fn of handlers[name] || []) fn(payload);
    },
  };
}

function httpError(status, body) {
  const e = new Error(body?.message || body?.error || `HTTP ${status}`);
  e.name = status === 401 ? 'AuthError' : 'HttpError';
  e.status = status;
  e.body = body;
  return e;
}

/** A host in memory: PUT stores only the last four of a key, like the host. */
function fakeClient(settings = fixture()) {
  const calls = [];
  const state = {
    settings,
    testReply: { ok: true, latency_ms: 812, check_token: 'tok-1' },
    errors: {},
    hold: null,
  };
  const clone = () => JSON.parse(JSON.stringify(state.settings));
  const find = (id) => state.settings.providers.find((p) => p.id === id);
  return {
    calls,
    state,
    async load() {
      calls.push(['load']);
      if (state.errors.load) throw state.errors.load;
      return clone();
    },
    async save(body, rev) {
      calls.push(['save', JSON.parse(JSON.stringify(body)), rev]);
      if (state.errors.save) throw state.errors.save;
      const p = find(body.provider);
      p.model = body.model;
      if (body.key?.action === 'set') {
        p.key = {
          configured: true,
          source: 'keychain',
          masked: `…${body.key.value.slice(-4)}`,
          env: null,
        };
        p.status = 'key_saved';
      }
      if (body.acknowledge_non_claude) p.ack_non_claude_at_ms = 1;
      if (body.activate) {
        for (const x of state.settings.providers)
          if (x.status === 'active') x.status = 'ready';
        state.settings.active = p.id;
        p.status = 'active';
      }
      state.settings.rev += 1;
      return clone();
    },
    async test(body) {
      calls.push(['test', JSON.parse(JSON.stringify(body))]);
      if (state.hold) await state.hold;
      if (state.errors.test) throw state.errors.test;
      const reply =
        typeof state.testReply === 'function'
          ? state.testReply(body)
          : state.testReply;
      return { depth: body.depth, host: 'example', ...reply };
    },
    async removeKey(id, rev) {
      calls.push(['remove', id, rev]);
      const p = find(id);
      p.key = { configured: false, source: 'none', masked: null, env: null };
      p.status = 'not_configured';
      state.settings.rev += 1;
      return clone();
    },
  };
}

async function mount({
  client = fakeClient(),
  layout = 'wide',
  bus = fakeBus(),
  open = true,
} = {}) {
  const host = doc.createElement('div');
  const invoker = doc.createElement('button');
  let statusCalls = 0;
  const chat = {
    status() {
      statusCalls += 1;
      return Promise.resolve({ available: true });
    },
  };
  const copied = [];
  const sheet = createSettingsSheet(
    host,
    { bus, chat },
    {
      doc,
      client,
      selfHost: '127.0.0.1:54300',
      clipboard: { writeText: async (t) => copied.push(t) },
    },
  );
  sheet.setLayout(layout);
  if (open) {
    invoker.focus();
    sheet.open({ invoker });
    await flush();
  }
  const el = sheet.element;
  const choose = async (id) => {
    find(
      el,
      (x) => x.attrs?.['data-provider'] === id && x.attrs?.role === 'radio',
    ).click();
    await flush();
  };
  const type = (fk, value) => {
    const input = byFk(el, fk);
    input.value = value;
    input.fire('input');
    return input;
  };
  return {
    host,
    sheet,
    el,
    client,
    bus,
    invoker,
    choose,
    type,
    copied,
    get statusCalls() {
      return statusCalls;
    },
  };
}

const detail = (el) => byClass(el, 'ic-settings__detail');
const resultText = (el) => textOf(byClass(el, 'ic-settings__result'));
const why = (el) => textOf(byClass(el, 'ic-settings__why'));

// ---- opening, layout, picker ---------------------------------------------------------------

test('opens as a dialog, groups the providers and focuses the checked one', async () => {
  const m = await mount();
  assert.equal(m.el.attrs.role, 'dialog');
  assert.ok(!hidden(m.el));
  assert.equal(m.el.attrs['aria-modal'], 'false');
  assert.ok(m.el.attrs['aria-labelledby']);
  const heads = all(m.el, (x) => x.className === 'ic-settings__grouphead').map(
    textOf,
  );
  assert.deepEqual(heads, [
    'Claude from Anthropic',
    'Claude on your cloud',
    'Other providers',
    'Custom endpoint',
  ]);
  assert.equal(find(m.el, (x) => x.attrs?.role === 'radiogroup') != null, true);
  const checked = all(m.el, (x) => x.attrs?.['aria-checked'] === 'true');
  assert.equal(checked.length, 1);
  assert.equal(checked[0].attrs['data-provider'], 'anthropic_login');
  assert.equal(doc.activeElement, checked[0]);
  // In use: label, model, status word with its glyph.
  const inUse = textOf(byClass(m.el, 'ic-settings__inuse'));
  assert.match(inUse, /In use: Claude login \(this Mac\), claude-opus-5-5/);
  assert.match(inUse, /Connected/);
  assert.equal(m.client.calls[0][0], 'load');
});

test('narrow: a full-screen modal sheet with Back instead of Close', async () => {
  const m = await mount({ layout: 'narrow' });
  assert.equal(m.el.attrs['aria-modal'], 'true');
  assert.equal(m.el.attrs['data-layout'], 'narrow');
  assert.ok(!hidden(byFk(m.el, 'back')));
  assert.ok(hidden(byFk(m.el, 'close')));
  m.sheet.setLayout('compact');
  assert.equal(m.el.attrs['aria-modal'], 'false');
  assert.ok(hidden(byFk(m.el, 'back')));
});

test('open({provider}) selects that provider; rows show family tags and status words', async () => {
  const m = await mount({ open: false });
  m.sheet.open({ provider: 'openrouter' });
  await flush();
  const row = find(
    m.el,
    (x) =>
      x.attrs?.['data-provider'] === 'openrouter' && x.attrs?.role === 'radio',
  );
  assert.equal(row.attrs['aria-checked'], 'true');
  assert.match(textOf(row), /OpenRouter/);
  assert.match(textOf(row), /Many models/);
  assert.match(textOf(row), /Key saved/);
  const mm = find(
    m.el,
    (x) =>
      x.attrs?.['data-provider'] === 'minimax' && x.attrs?.role === 'radio',
  );
  assert.match(textOf(mm), /Not Claude/);
  assert.match(textOf(mm), /Not set up/);
});

// ---- rendering per provider kind ----------------------------------------------------------

test('Claude login: sign-in copy, no key field, no test, In use', async () => {
  const m = await mount();
  const d = detail(m.el);
  assert.match(textOf(d), /Uses the Claude sign-in on this Mac/);
  assert.equal(byFk(d, 'key'), null);
  assert.equal(byFk(d, 'base-url'), null);
  assert.ok(hidden(byFk(d, 'test')));
  assert.equal(textOf(byFk(d, 'use')), 'In use');
  assert.equal(byFk(d, 'use').attrs['aria-disabled'], 'true');
  // The login sends nothing to a third party: no egress line, no caveat.
  assert.doesNotMatch(textOf(d), /are sent to/);
  assert.doesNotMatch(textOf(d), /This isn't a Claude model/);
});

test('Anthropic API key: a write-only password field, Use waits for a key', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  const d = detail(m.el);
  const key = byFk(d, 'key');
  assert.equal(key.attrs.type, 'password');
  assert.equal(key.attrs.autocomplete, 'off');
  assert.equal(key.attrs.spellcheck, 'false');
  assert.equal(key.attrs.autocapitalize, 'off');
  assert.equal(key.attrs.placeholder, 'Paste your key');
  const label = find(
    d,
    (x) => x.tag === 'label' && x.attrs.for === key.attrs.id,
  );
  assert.equal(textOf(label), 'Anthropic API key');
  assert.equal(byFk(d, 'use').attrs['aria-disabled'], 'true');
  assert.equal(why(m.el), 'Add a key first');
  assert.equal(textOf(byFk(d, 'use')), 'Use Anthropic API key');
  // Show / Hide flips the input and its words.
  const show = byFk(d, 'show');
  show.click();
  assert.equal(key.type, 'text');
  assert.equal(show.attrs['aria-label'], 'Hide the key');
  show.click();
  assert.equal(key.type, 'password');
  // The key-only quick check is free: no paid-probe note.
  assert.doesNotMatch(textOf(d), /Sends one tiny request/);
  assert.match(textOf(d), /are sent to api\.anthropic\.com/);
});

test('Bedrock: optional key, AWS fields, full check only', async () => {
  const m = await mount();
  await m.choose('bedrock');
  const d = detail(m.el);
  assert.match(textOf(d), /Leave it empty to use your AWS credentials/);
  assert.equal(byFk(d, 'field:region').value, 'us-east-1');
  assert.ok(byFk(d, 'field:profile'));
  assert.match(textOf(d), /AWS profile \(optional\)/);
  assert.match(textOf(d), /Bedrock API key \(optional\)/);
  assert.match(textOf(d), /This provider has no quick check/);
  assert.equal(why(m.el), '');
  assert.equal(byFk(d, 'use').attrs['aria-disabled'], 'false');
});

test('Vertex: no key option, the project field is required', async () => {
  const m = await mount();
  await m.choose('vertex');
  const d = detail(m.el);
  assert.equal(byFk(d, 'key'), null);
  assert.match(textOf(d), /Uses your Google Cloud credentials/);
  assert.equal(byFk(d, 'field:region').value, 'global');
  byFk(d, 'use').click();
  await flush();
  assert.match(textOf(d), /Fill this in\./);
  assert.equal(doc.activeElement, byFk(d, 'field:project'));
  assert.equal(m.client.calls.filter((c) => c[0] !== 'load').length, 0);
});

test('OpenRouter: saved key shows only the last four, fixed endpoint, notes and docs', async () => {
  const m = await mount();
  await m.choose('openrouter');
  const d = detail(m.el);
  assert.match(textOf(d), /Saved key ending in 9f2a/);
  const last = find(d, (x) => x.attrs?.['data-last4'] === 'true');
  assert.equal(textOf(last), '9f2a');
  assert.match(last.className, /ic-mono/);
  assert.match(textOf(d), /Stored in your macOS Keychain/);
  assert.ok(byFk(d, 'replace'));
  assert.ok(byFk(d, 'remove'));
  assert.equal(byFk(d, 'key'), null);
  assert.match(textOf(d), /Requests go to openrouter\.ai/);
  assert.match(textOf(d), /Anthropic doesn't support routing Claude Code/);
  assert.match(textOf(d), /openrouter\.ai\/docs\/cookbook/);
  byFk(d, 'copy-docs').click();
  await flush();
  assert.deepEqual(m.copied, [
    'https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration',
  ]);
  // A Claude model through OpenRouter needs no acknowledgement...
  assert.match(textOf(d), /are sent to openrouter\.ai/);
  assert.doesNotMatch(textOf(d), /This isn't a Claude model/);
  assert.equal(why(m.el), '');
  // ...another model does, and Use says so.
  m.type('model', 'minimax/minimax-m3');
  assert.match(textOf(d), /This isn't a Claude model/);
  assert.equal(why(m.el), 'Check the acknowledgement first');
  const ack = byFk(d, 'ack');
  ack.checked = true;
  ack.fire('change');
  assert.equal(why(m.el), '');
  assert.equal(byFk(d, 'use').attrs['aria-disabled'], 'false');
});

test('MiniMax: region presets, paid probe note naming the host', async () => {
  const m = await mount();
  await m.choose('minimax');
  const d = detail(m.el);
  const preset = byFk(d, 'preset');
  assert.deepEqual(
    preset.children.map((o) => textOf(o)),
    ['International', 'China'],
  );
  assert.equal(byFk(d, 'base-url'), null);
  assert.match(
    textOf(d),
    /Sends one tiny request to api\.minimax\.io\. Your provider may bill it\./,
  );
  preset.value = '1';
  preset.fire('change');
  assert.match(textOf(d), /Requests go to api\.minimax\.cn/);
  assert.match(textOf(d), /are sent to api\.minimax\.cn/);
  assert.match(textOf(d), /This isn't a Claude model/);
});

test('Alibaba: presets plus Another URL, the workspace fills the placeholder', async () => {
  const m = await mount();
  await m.choose('alibaba');
  const d = detail(m.el);
  const preset = byFk(d, 'preset');
  assert.equal(textOf(preset.children.at(-1)), 'Another URL');
  assert.equal(preset.value, 'custom');
  preset.value = '0';
  preset.fire('change');
  assert.match(byFk(d, 'base-url').value, /\{workspace_id\}/);
  m.type('key', 'test-key-123');
  m.type('model', 'qwen3.7-max');
  byFk(d, 'save').click();
  await flush();
  assert.match(textOf(d), /Fill in the Workspace ID first\./);
  m.type('field:workspace_id', 'ws-42');
  assert.match(
    textOf(d),
    /ws-42\.ap-southeast-1\.maas\.aliyuncs\.com\/apps\/anthropic\/v1\/messages/,
  );
  assert.doesNotMatch(textOf(d), /Fill in the Workspace ID first/);
});

test('LM Studio: keyless local server, LAN http needs the explicit allowance', async () => {
  const m = await mount();
  await m.choose('lmstudio');
  const d = detail(m.el);
  assert.match(textOf(d), /No key needed for a local server\./);
  assert.match(textOf(d), /Local endpoint\. Traffic stays on this Mac\./);
  assert.equal(byFk(d, 'model').attrs.placeholder, 'openai/gpt-oss-20b');
  m.type('base-url', 'http://192.168.1.5:1234');
  assert.match(textOf(d), /This endpoint isn't encrypted/);
  byFk(d, 'use').click();
  await flush();
  assert.match(textOf(d), /to use http:\/\/ on your network/);
  const lan = byFk(d, 'lan');
  lan.checked = true;
  lan.fire('change');
  assert.doesNotMatch(textOf(d), /to use http:\/\/ on your network/);
});

test('Custom: authentication choice; None makes the key optional', async () => {
  const m = await mount();
  await m.choose('custom');
  const d = detail(m.el);
  byFk(d, 'advanced').click();
  const scheme = byFk(d, 'scheme');
  assert.deepEqual(scheme.children.map(textOf), [
    'Bearer token',
    'x-api-key header',
    'None',
  ]);
  assert.equal(why(m.el), 'Add a key first');
  scheme.value = 'none';
  scheme.fire('change');
  assert.match(textOf(detail(m.el)), /No key needed for a local server\./);
  assert.notEqual(why(m.el), 'Add a key first');
});

// ---- the key is write-only -------------------------------------------------------------------

function storageSpy() {
  const writes = [];
  const make = (name) => ({
    getItem: () => null,
    setItem: (k, v) => writes.push([name, k, v]),
    removeItem: () => {},
  });
  const before = {
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    sessionStorage: Object.getOwnPropertyDescriptor(
      globalThis,
      'sessionStorage',
    ),
  };
  for (const name of ['localStorage', 'sessionStorage'])
    Object.defineProperty(globalThis, name, {
      value: make(name),
      configurable: true,
      writable: true,
    });
  return {
    writes,
    restore() {
      for (const [name, desc] of Object.entries(before)) {
        if (desc) Object.defineProperty(globalThis, name, desc);
        else delete globalThis[name];
      }
    },
  };
}

test('a saved key is never shown again: the field clears, only the last four remain', async () => {
  const spy = storageSpy();
  try {
    const m = await mount();
    await m.choose('anthropic_api');
    const key = m.type('key', '  test-key-123  ');
    assert.equal(m.sheet._typedCount, 1);
    byFk(detail(m.el), 'save').click();
    await flush();
    await flush();
    const [, body, rev] = m.client.calls.find((c) => c[0] === 'save');
    assert.deepEqual(body.key, { action: 'set', value: 'test-key-123' });
    assert.equal(body.activate, false);
    assert.equal(rev, 7);
    // The old field is emptied; the new panel has no field, only the tail.
    assert.equal(key.value, '');
    assert.equal(m.sheet._typedCount, 0);
    assert.equal(byFk(detail(m.el), 'key'), null);
    assert.match(textOf(detail(m.el)), /Saved key ending in -123/);
    assert.doesNotMatch(allText(m.el), /test-key-123/);
    assert.match(
      resultText(m.el),
      /Saved\. The analyst keeps using Claude login \(this Mac\) until you choose Use Anthropic API key\./,
    );
    assert.deepEqual(spy.writes, []);
    // Replace key: a fresh empty field, Cancel goes back to the tail.
    byFk(detail(m.el), 'replace').click();
    assert.equal(byFk(detail(m.el), 'key').value, '');
    assert.equal(doc.activeElement, byFk(detail(m.el), 'key'));
    byFk(detail(m.el), 'cancel-replace').click();
    assert.equal(doc.activeElement, byFk(detail(m.el), 'replace'));
    // Replace and save: the panel leaves Replace and shows the new tail.
    byFk(detail(m.el), 'replace').click();
    m.type('key', 'test-key-456');
    byFk(detail(m.el), 'save').click();
    await flush();
    await flush();
    assert.equal(byFk(detail(m.el), 'key'), null);
    assert.match(textOf(detail(m.el)), /Saved key ending in -456/);
    assert.equal(m.sheet._typedCount, 0);
  } finally {
    spy.restore();
  }
});

test('a typed key is dropped when the operator leaves the provider or closes', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  m.type('key', 'test-key-456');
  await m.choose('bedrock');
  assert.equal(m.sheet._typedCount, 0);
  await m.choose('anthropic_api');
  assert.equal(byFk(detail(m.el), 'key').value, '');
  const key = m.type('key', 'test-key-456');
  m.sheet.close();
  assert.equal(key.value, '');
  assert.equal(m.sheet._typedCount, 0);
});

test('validation mirrors the host before any round trip', async () => {
  const m = await mount();
  await m.choose('custom');
  const d = detail(m.el);
  m.type('key', 'short');
  m.type('model', '-rm');
  m.type('base-url', 'http://example.com');
  byFk(d, 'save').click();
  await flush();
  const text = textOf(detail(m.el));
  assert.match(text, /That key is too short\./);
  assert.match(text, /A model name can't start with a hyphen\./);
  assert.match(text, /Use https:\/\/ for an endpoint on the internet\./);
  assert.equal(doc.activeElement, byFk(detail(m.el), 'key'));
  assert.equal(byFk(detail(m.el), 'model').attrs['aria-invalid'], 'true');
  assert.equal(resultText(m.el), 'Fix the highlighted fields first');
  m.type('base-url', 'http://127.0.0.1:54300');
  assert.match(textOf(detail(m.el)), /That's this app's own address/);
  m.type('base-url', 'https://gw.example.com/anthropic?x=1');
  assert.match(textOf(detail(m.el)), /Remove the \?query or #fragment/);
  assert.equal(m.client.calls.filter((c) => c[0] !== 'load').length, 0);
});

// ---- connection checks -------------------------------------------------------------------------

test('Test connection: running, then the key-only answer; a full check is offered', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  m.type('key', 'test-key-123');
  let release;
  m.client.state.hold = new Promise((r) => (release = r));
  byFk(detail(m.el), 'test').click();
  await flush();
  assert.equal(resultText(m.el), 'Testing Anthropic API key…');
  assert.equal(detail(m.el).attrs['data-state'], 'testing');
  assert.equal(byFk(detail(m.el), 'use').attrs['aria-disabled'], 'true');
  const [, body] = m.client.calls.find((c) => c[0] === 'test');
  assert.equal(body.depth, 'quick');
  assert.equal(body.key, 'test-key-123');
  assert.equal(body.provider, 'anthropic_api');
  release();
  await flush();
  await flush();
  assert.equal(
    resultText(m.el).split(' Run a full check')[0],
    'Connected. Anthropic API key accepted the key.',
  );
  assert.ok(!hidden(byFk(detail(m.el), 'full')));
  // The typed key is still in its field for Save or Use.
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
});

test('a failed check shows the provider message, the HTTP code and the hint', async () => {
  const m = await mount();
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  m.client.state.testReply = {
    ok: false,
    status: 401,
    code: 'auth',
    message: 'MiniMax rejected the key.',
    hint: 'Open analyst settings to replace the key.',
  };
  byFk(detail(m.el), 'test').click();
  await flush();
  await flush();
  const r = resultText(m.el);
  assert.match(r, /^Couldn't connect: MiniMax rejected the key\./);
  assert.match(r, /HTTP 401/);
  // The host's chat hint, worded for the sheet the operator is in.
  assert.match(r, /Replace the key above\./);
  assert.doesNotMatch(r, /analyst settings/);
  assert.equal(
    byClass(m.el, 'ic-settings__result').attrs['data-tone'],
    'critical',
  );
  assert.equal(detail(m.el).attrs['data-state'], 'failed');
  assert.match(
    textOf(byClass(detail(m.el), 'ic-settings__detailhead')),
    /Couldn't connect/,
  );
});

test('a busy host and a dropped connection read as words, never codes', async () => {
  const m = await mount();
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  m.client.state.errors.test = httpError(429, { error: 'test_busy' });
  byFk(detail(m.el), 'test').click();
  await flush();
  await flush();
  assert.match(
    resultText(m.el),
    /Another check is running\. Try again in a moment\./,
  );
  const offline = new Error('x');
  offline.name = 'OfflineError';
  m.client.state.errors.test = offline;
  byFk(detail(m.el), 'test').click();
  await flush();
  await flush();
  assert.match(resultText(m.el), /Can't reach Eye in the Sky's local service/);
});

// ---- Use {label}: full check, then PUT with its token ---------------------------------------

test('Use runs the full check, then activates with the check token and the acknowledgement', async () => {
  const bus = fakeBus();
  const m = await mount({ bus });
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  const ack = byFk(detail(m.el), 'ack');
  ack.checked = true;
  ack.fire('change');
  m.client.state.testReply = {
    ok: true,
    check_token: 'tok-9',
    thinking: 'adaptive',
  };
  byFk(detail(m.el), 'use').click();
  await flush();
  await flush();
  await flush();
  const test = m.client.calls.find((c) => c[0] === 'test');
  assert.equal(test[1].depth, 'full');
  assert.equal(test[1].key, 'test-key-123');
  const [, body, rev] = m.client.calls.find((c) => c[0] === 'save');
  assert.equal(body.activate, true);
  assert.equal(body.check_token, 'tok-9');
  assert.equal(body.acknowledge_non_claude, true);
  assert.deepEqual(body.key, { action: 'set', value: 'test-key-123' });
  assert.equal(body.base_url, 'https://api.minimax.io/anthropic');
  assert.equal(rev, 7);
  assert.match(
    resultText(m.el),
    /The analyst now uses MiniMax\. The change takes effect with your next message\./,
  );
  assert.equal(textOf(byFk(detail(m.el), 'use')), 'In use');
  assert.match(textOf(byClass(m.el, 'ic-settings__inuse')), /In use: MiniMax/);
  assert.ok(
    bus.emitted.some(
      ([n, p]) => n === 'settings:changed' && p.active === 'minimax',
    ),
  );
  assert.equal(m.statusCalls, 1);
  assert.doesNotMatch(allText(m.el), /test-key-123/);
});

test('a failed full check never saves, and says what the engine said', async () => {
  const m = await mount();
  await m.choose('bedrock');
  m.client.state.testReply = {
    ok: false,
    depth: 'full',
    status: 403,
    message: 'Amazon Bedrock refused access for this key.',
  };
  byFk(detail(m.el), 'use').click();
  await flush();
  await flush();
  assert.equal(m.client.calls.filter((c) => c[0] === 'save').length, 0);
  assert.match(
    resultText(m.el),
    /The analyst's engine couldn't use Amazon Bedrock: Amazon Bedrock refused access for this key\./,
  );
});

test('a thinking downgrade is reported with the activation', async () => {
  const m = await mount();
  await m.choose('bedrock');
  m.client.state.testReply = { ok: true, check_token: 't', thinking: 'off' };
  byFk(detail(m.el), 'use').click();
  await flush();
  await flush();
  await flush();
  assert.match(
    resultText(m.el),
    /doesn't accept extended thinking, so it's turned off for this model\./,
  );
});

test('the Claude login activates without a check; a stale token reads as needs a check', async () => {
  const m = await mount();
  m.client.state.settings.active = 'openrouter';
  m.client.state.settings.providers[0].status = 'ready';
  await m.sheet.reload();
  await m.choose('anthropic_login');
  byFk(detail(m.el), 'use').click();
  await flush();
  await flush();
  assert.equal(m.client.calls.filter((c) => c[0] === 'test').length, 0);
  const [, body] = m.client.calls.find((c) => c[0] === 'save');
  assert.equal(body.activate, true);
  assert.equal(body.check_token, undefined);
  // The host refuses a stale check: the words say what to do.
  await m.choose('bedrock');
  m.client.state.errors.save = httpError(409, { error: 'needs_check' });
  byFk(detail(m.el), 'use').click();
  await flush();
  await flush();
  await flush();
  assert.match(
    resultText(m.el),
    /The settings changed after the check\. Run it again, then choose Use\./,
  );
});

test('a conflicting write re-reads the settings', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  m.type('key', 'test-key-123');
  m.client.state.errors.save = httpError(409, {
    error: 'settings_conflict',
    rev: 9,
  });
  byFk(detail(m.el), 'save').click();
  await flush();
  await flush();
  assert.match(resultText(m.el), /These settings changed in another window/);
  assert.equal(m.client.calls.filter((c) => c[0] === 'load').length, 2);
});

// ---- Remove key ---------------------------------------------------------------------------------

test('Remove key asks inline, Keep key first; Esc backs out one layer at a time', async () => {
  const m = await mount();
  await m.choose('openrouter');
  byFk(detail(m.el), 'remove').click();
  const d = detail(m.el);
  assert.match(
    textOf(d),
    /Remove the saved OpenRouter key\? The analyst stops working with OpenRouter until you add a key\./,
  );
  const keep = byFk(d, 'keep');
  const confirm = byFk(d, 'confirm-remove');
  const order = focusables(d);
  assert.ok(order.indexOf(keep) < order.indexOf(confirm));
  assert.equal(doc.activeElement, keep);
  // Esc closes the confirmation, not the sheet.
  keep.fire('keydown', { key: 'Escape' });
  assert.ok(m.sheet.isOpen());
  assert.equal(doc.activeElement, byFk(detail(m.el), 'remove'));
  byFk(detail(m.el), 'remove').click();
  byFk(detail(m.el), 'confirm-remove').click();
  await flush();
  await flush();
  assert.deepEqual(
    m.client.calls.find((c) => c[0] === 'remove'),
    ['remove', 'openrouter', 7],
  );
  assert.match(resultText(m.el), /Removed the saved OpenRouter key\./);
  assert.equal(byFk(detail(m.el), 'key').value, '');
  assert.equal(doc.activeElement, byFk(detail(m.el), 'key'));
});

// ---- keyboard ------------------------------------------------------------------------------------

test('arrow keys move the selection and focus; neither they nor Enter activate', async () => {
  const m = await mount();
  const radio = (id) =>
    find(
      m.el,
      (x) => x.attrs?.role === 'radio' && x.attrs['data-provider'] === id,
    );
  const start = radio('anthropic_login');
  assert.equal(start.attrs.tabindex, '0');
  start.fire('keydown', { key: 'ArrowDown' });
  assert.equal(doc.activeElement, radio('anthropic_api'));
  assert.equal(radio('anthropic_api').attrs['aria-checked'], 'true');
  assert.equal(radio('anthropic_api').attrs.tabindex, '0');
  assert.equal(start.attrs.tabindex, '-1');
  assert.equal(detail(m.el).attrs['data-provider'], 'anthropic_api');
  radio('anthropic_api').fire('keydown', { key: 'End' });
  assert.equal(doc.activeElement, radio('custom'));
  radio('custom').fire('keydown', { key: 'ArrowRight' });
  assert.equal(doc.activeElement, radio('anthropic_login'));
  radio('anthropic_login').fire('keydown', { key: 'ArrowUp' });
  assert.equal(doc.activeElement, radio('custom'));
  const ev = radio('custom').fire('keydown', { key: 'Enter' });
  assert.equal(ev.defaultPrevented, true);
  assert.equal(m.client.calls.filter((c) => c[0] !== 'load').length, 0);
  assert.equal(m.client.state.settings.active, 'anthropic_login');
});

test('Tab stays inside the sheet; Esc closes it and focus returns to the invoker', async () => {
  const m = await mount();
  const stops = focusables(m.el);
  assert.ok(stops.length > 4);
  // Only the checked radio is a Tab stop in the group.
  assert.equal(stops.filter((x) => x.attrs?.role === 'radio').length, 1);
  stops.at(-1).focus();
  const fwd = stops.at(-1).fire('keydown', { key: 'Tab' });
  assert.equal(fwd.defaultPrevented, true);
  assert.equal(doc.activeElement, stops[0]);
  const back = stops[0].fire('keydown', { key: 'Tab', shiftKey: true });
  assert.equal(back.defaultPrevented, true);
  assert.equal(doc.activeElement, stops.at(-1));
  const esc = stops[0].fire('keydown', { key: 'Escape' });
  assert.equal(esc.defaultPrevented, true);
  assert.equal(m.sheet.isOpen(), false);
  assert.ok(hidden(m.el));
  assert.equal(doc.activeElement, m.invoker);
  assert.equal(m.sheet.escape(), false);
});

test('the shell learns open and closed through settings:state', async () => {
  const bus = fakeBus();
  const m = await mount({ bus });
  byFk(m.el, 'close').click();
  const states = bus.emitted
    .filter(([n]) => n === 'settings:state')
    .map(([, p]) => p.open);
  assert.deepEqual(states, [true, false]);
});

// ---- pinned approval, read-only, cross-origin ----------------------------------------------------

test('a pending approval stays pinned above the sheet; Review goes to it', async () => {
  const bus = fakeBus();
  const m = await mount({ bus });
  const banner = byClass(m.el, 'ic-settings__approval');
  assert.ok(hidden(banner));
  bus.emit('approval:pending', {
    count: 1,
    oldest: { title: 'Grid search', vehicle: 'Drone1' },
  });
  assert.ok(!hidden(banner));
  assert.equal(
    textOf(byClass(banner, 'ic-settings__approvaltext')),
    'Approval waiting: Grid search, Drone1.',
  );
  assert.equal(m.el.children[0], banner);
  byFk(m.el, 'review').click();
  assert.equal(m.sheet.isOpen(), false);
  assert.ok(bus.emitted.some(([n]) => n === 'approval:review'));
});

test('settings from a newer version are read-only', async () => {
  const client = fakeClient();
  client.state.settings.read_only = true;
  const m = await mount({ client });
  assert.match(textOf(m.el), /written by a newer version of Eye in the Sky/);
  await m.choose('anthropic_api');
  assert.equal(why(m.el), 'Settings were written by a newer version');
  assert.equal(byFk(detail(m.el), 'key').attrs.readonly, '');
  byFk(detail(m.el), 'save').click();
  await flush();
  assert.equal(m.client.calls.filter((c) => c[0] === 'save').length, 0);
});

test('opened from vite (another origin): the host refuses and the sheet says why', async () => {
  const client = fakeClient();
  client.state.errors.load = httpError(403, { error: 'cross_origin' });
  const m = await mount({ client });
  assert.match(
    textOf(m.el),
    /Open the console from the app host to change analyst settings\./,
  );
  assert.ok(hidden(find(m.el, (x) => x.attrs?.role === 'radiogroup')));
  assert.equal(byFk(m.el, 'retry'), null);
});

test('a missing settings service shows a retry, and Retry reloads', async () => {
  const client = fakeClient();
  client.state.errors.load = Object.assign(new Error('x'), {
    name: 'OfflineError',
  });
  const m = await mount({ client });
  assert.match(textOf(m.el), /Couldn't load analyst settings/);
  client.state.errors.load = null;
  byFk(m.el, 'retry').click();
  await flush();
  await flush();
  assert.match(textOf(m.el), /In use: Claude login/);
});

test('Custom: declaring Claude models drops the acknowledgement, and is sent', async () => {
  const m = await mount();
  await m.choose('custom');
  const d = detail(m.el);
  assert.match(textOf(d), /This isn't a Claude model/);
  byFk(d, 'advanced').click();
  const fam = byFk(d, 'family');
  fam.value = 'claude';
  fam.fire('change');
  assert.doesNotMatch(textOf(d), /This isn't a Claude model/);
  m.type('key', 'test-key-123');
  m.type('model', 'claude-opus-5-5');
  m.type('base-url', 'https://gw.example.com');
  byFk(d, 'save').click();
  await flush();
  await flush();
  const [, body] = m.client.calls.find((c) => c[0] === 'save');
  assert.equal(body.model_family, 'claude');
  assert.equal(body.auth_scheme, 'bearer');
  assert.equal(body.base_url, 'https://gw.example.com');
});

// ---- review fixes (BYOK UX review) ---------------------------------------------------------

const settle = async (n = 4) => {
  for (let i = 0; i < n; i += 1) await flush();
};

async function customReady(m) {
  await m.choose('custom');
  m.type('key', 'test-key-123');
  m.type('model', 'stub-model-1');
  m.type('base-url', 'https://gw.example.com');
  const ack = byFk(detail(m.el), 'ack');
  ack.checked = true;
  ack.fire('change');
}

test('review: Use after a passing full check reuses it instead of checking again', async () => {
  const m = await mount();
  await customReady(m);
  byFk(detail(m.el), 'test').click();
  await settle();
  m.client.state.testReply = { ok: true, check_token: 'tok-full' };
  byFk(detail(m.el), 'full').click();
  await settle();
  assert.match(resultText(m.el), /The analyst's engine works with Custom/);
  // The host refuses a second full check within 10 s: Use must not need one.
  m.client.state.errors.test = httpError(429, {
    error: 'test_busy',
    message: 'Wait a few seconds between checks.',
    retry_after_s: 30,
  });
  byFk(detail(m.el), 'use').click();
  await settle();
  assert.equal(m.client.calls.filter((c) => c[0] === 'test').length, 2);
  const [, body] = m.client.calls.find((c) => c[0] === 'save');
  assert.equal(body.activate, true);
  assert.equal(body.check_token, 'tok-full');
  assert.match(resultText(m.el), /The analyst now uses Custom/);
});

test('review: a changed config after the full check is checked again', async () => {
  const m = await mount();
  await customReady(m);
  m.client.state.testReply = { ok: true, check_token: 'tok-a' };
  byFk(detail(m.el), 'test').click();
  await settle();
  byFk(detail(m.el), 'full').click();
  await settle();
  m.type('model', 'stub-model-2');
  m.client.state.testReply = { ok: true, check_token: 'tok-b' };
  byFk(detail(m.el), 'use').click();
  await settle();
  const tests = m.client.calls.filter((c) => c[0] === 'test');
  assert.equal(tests.length, 3);
  assert.equal(tests[2][1].model, 'stub-model-2');
  const [, body] = m.client.calls.find((c) => c[0] === 'save');
  assert.equal(body.check_token, 'tok-b');
});

test('review: a busy host is a wait, not a failure, and Use retries after it', async () => {
  const m = await mount();
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  const ack = byFk(detail(m.el), 'ack');
  ack.checked = true;
  ack.fire('change');
  m.client.state.errors.test = httpError(429, {
    error: 'test_busy',
    message: 'Wait a few seconds between checks.',
    retry_after_s: 30,
  });
  byFk(detail(m.el), 'use').click();
  await settle();
  assert.equal(resultText(m.el), 'Wait a few seconds between checks.');
  assert.equal(byClass(m.el, 'ic-settings__result').attrs['data-tone'], 'warn');
  assert.notEqual(detail(m.el).attrs['data-state'], 'failed');
  assert.doesNotMatch(
    textOf(byClass(detail(m.el), 'ic-settings__detailhead')),
    /Couldn't connect/,
  );
  // A short cooldown: Use waits it out and carries on.
  let n = 0;
  const real = m.client.test;
  m.client.test = async (b) => {
    n += 1;
    if (n === 1)
      throw httpError(429, {
        error: 'test_busy',
        message: 'Wait a few seconds between checks.',
        retry_after_s: 0.01,
      });
    return real.call(m.client, b);
  };
  m.client.state.errors.test = null;
  byFk(detail(m.el), 'use').click();
  await flush();
  assert.equal(
    resultText(m.el),
    'Another check just ran. Waiting a few seconds…',
  );
  await new Promise((r) => setTimeout(r, 400));
  await settle();
  assert.equal(n, 2);
  assert.ok(m.client.calls.some((c) => c[0] === 'save'));
  assert.match(resultText(m.el), /The analyst now uses MiniMax/);
});

test('review: a failed Use or Save keeps the key the operator pasted', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  const key = m.type('key', 'test-key-123');
  m.client.state.testReply = {
    ok: false,
    status: 400,
    code: 'invalid_request',
    message: 'The engine timed out.',
  };
  byFk(detail(m.el), 'use').click();
  await settle();
  assert.equal(m.client.calls.filter((c) => c[0] === 'save').length, 0);
  assert.equal(byFk(detail(m.el), 'key'), key);
  assert.equal(key.value, 'test-key-123');
  assert.equal(m.sheet._typedCount, 1);
  assert.notEqual(why(m.el), 'Add a key first');
  // The next Use sends the same key.
  m.client.state.testReply = { ok: true, check_token: 't2' };
  m.client.state.errors.save = httpError(422, {
    error: 'invalid_settings',
    field: 'model',
    message: 'Type a model name.',
  });
  byFk(detail(m.el), 'use').click();
  await settle();
  const tests = m.client.calls.filter((c) => c[0] === 'test');
  assert.equal(tests.at(-1)[1].key, 'test-key-123');
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
  // Save fails the same way: still there.
  byFk(detail(m.el), 'save').click();
  await settle();
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
  assert.equal(m.sheet._typedCount, 1);
});

test('review: changing Authentication keeps the typed key', async () => {
  const m = await mount();
  await m.choose('custom');
  m.type('key', 'test-key-123');
  byFk(detail(m.el), 'advanced').click();
  const scheme = byFk(detail(m.el), 'scheme');
  scheme.focus();
  scheme.value = 'x-api-key';
  scheme.fire('change');
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
  assert.equal(m.sheet._typedCount, 1);
  scheme.value = 'none';
  scheme.fire('change');
  assert.match(textOf(detail(m.el)), /No key needed for a local server\./);
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
  // Advanced stays open and focus stays on the select.
  assert.equal(doc.activeElement, byFk(detail(m.el), 'scheme'));
});

test('review: Save on the provider in use checks the edit first', async () => {
  const m = await mount();
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  const ack = byFk(detail(m.el), 'ack');
  ack.checked = true;
  ack.fire('change');
  m.client.state.testReply = { ok: true, check_token: 'tok-use' };
  byFk(detail(m.el), 'use').click();
  await settle();
  assert.equal(textOf(byFk(detail(m.el), 'use')), 'In use');
  assert.equal(textOf(byFk(detail(m.el), 'save')), 'Save');
  // Edit the model of the provider in use: Save runs the full check.
  m.type('model', 'MiniMax-M2.7');
  assert.equal(textOf(byFk(detail(m.el), 'save')), 'Save and check');
  m.client.state.testReply = {
    ok: false,
    depth: 'full',
    status: 404,
    code: 'model',
    message: "MiniMax doesn't recognize the model MiniMax-M2.7.",
  };
  const saves = () => m.client.calls.filter((c) => c[0] === 'save');
  byFk(detail(m.el), 'save').click();
  await settle();
  assert.equal(saves().length, 1, 'a failed check saves nothing');
  const tests = m.client.calls.filter((c) => c[0] === 'test');
  assert.equal(tests.at(-1)[1].depth, 'full');
  assert.equal(tests.at(-1)[1].model, 'MiniMax-M2.7');
  assert.match(resultText(m.el), /couldn't use MiniMax/);
  m.client.state.testReply = { ok: true, check_token: 'tok-save' };
  byFk(detail(m.el), 'save').click();
  await settle();
  assert.equal(saves().length, 2);
  const body = saves().at(-1)[1];
  assert.equal(body.activate, true);
  assert.equal(body.check_token, 'tok-save');
  assert.equal(body.model, 'MiniMax-M2.7');
  assert.match(
    resultText(m.el),
    /Saved\. The analyst uses it from your next message\./,
  );
});

test('review: the endpoint help shows the whole destination; a trailing /v1 is refused', async () => {
  const m = await mount();
  await m.choose('alibaba');
  const preset = byFk(detail(m.el), 'preset');
  preset.value = '1';
  preset.fire('change');
  assert.match(
    textOf(detail(m.el)),
    /Requests go to dashscope\.aliyuncs\.com\/apps\/anthropic\/v1\/messages\./,
  );
  await m.choose('custom');
  assert.doesNotMatch(allText(m.el), /\{URL\}/);
  m.type('key', 'test-key-123');
  m.type('model', 'stub-model-1');
  m.type('base-url', 'https://api.example.com/v1/');
  assert.doesNotMatch(textOf(detail(m.el)), /api\.example\.com\/v1\/messages/);
  byFk(detail(m.el), 'save').click();
  await settle();
  assert.match(textOf(detail(m.el)), /Remove \/v1 from the end/);
  assert.equal(m.client.calls.filter((c) => c[0] === 'save').length, 0);
});

test('review: the key section follows the endpoint (Ollama Cloud needs a key)', async () => {
  const client = fakeClient();
  client.state.settings.providers.splice(
    -1,
    0,
    provider({
      id: 'ollama',
      label: 'Ollama',
      base_url: {
        value: 'http://localhost:11434',
        editable: true,
        presets: [
          { label: 'Local', url: 'http://localhost:11434' },
          { label: 'Ollama Cloud', url: 'https://ollama.com' },
        ],
      },
      auth: {
        scheme: 'bearer',
        key_label: 'Ollama API key',
        key_optional: 'loopback',
      },
      quick_check: { kind: 'messages', billable: true },
    }),
  );
  const m = await mount({ client });
  await m.choose('ollama');
  assert.match(textOf(detail(m.el)), /No key needed for a local server\./);
  assert.match(textOf(detail(m.el)), /Ollama API key \(optional\)/);
  m.type('key', 'test-key-123');
  const preset = byFk(detail(m.el), 'preset');
  preset.value = '1';
  preset.fire('change');
  const d = textOf(detail(m.el));
  assert.doesNotMatch(d, /No key needed/);
  assert.doesNotMatch(d, /\(optional\)/);
  assert.match(d, /Ollama API key/);
  assert.equal(byFk(detail(m.el), 'key').value, 'test-key-123');
  // LM Studio on a public host: the same.
  await m.choose('lmstudio');
  m.type('base-url', 'https://lm.example.com');
  assert.doesNotMatch(textOf(detail(m.el)), /No key needed/);
  assert.equal(why(m.el), 'Add a key first');
});

test('review: a 400 from the quick check offers the full check its hint names', async () => {
  const m = await mount();
  await m.choose('minimax');
  m.type('key', 'test-key-123');
  m.client.state.testReply = {
    ok: false,
    status: 400,
    code: 'invalid_request',
    message: 'MiniMax rejected the request: bad param.',
    hint: 'Run a full check in analyst settings.',
  };
  byFk(detail(m.el), 'test').click();
  await settle();
  const r = resultText(m.el);
  assert.match(
    r,
    /Run a full check to see whether the analyst's engine works\./,
  );
  assert.doesNotMatch(r, /analyst settings/);
  assert.ok(!hidden(byFk(detail(m.el), 'full')));
});

test('review: after Run a full check, focus lands on the next action, never the body', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  m.type('key', 'test-key-123');
  byFk(detail(m.el), 'test').click();
  await settle();
  const full = byFk(detail(m.el), 'full');
  full.focus();
  let release;
  m.client.state.hold = new Promise((r) => (release = r));
  full.click();
  await flush();
  assert.ok(hidden(full));
  assert.equal(doc.activeElement, byFk(detail(m.el), 'test'));
  release();
  await settle();
  assert.equal(doc.activeElement, byFk(detail(m.el), 'use'));
});

test("review: the host's 422 names a field: it is marked and focused", async () => {
  const m = await mount();
  await m.choose('vertex');
  m.type('field:project', 'my-project-1');
  m.type('field:credentials_file', '/nonexistent/key.json');
  m.client.state.errors.save = httpError(422, {
    error: 'invalid_settings',
    field: 'fields.credentials_file',
    message: 'Use the absolute path of an existing file.',
  });
  byFk(detail(m.el), 'save').click();
  await settle();
  const input = byFk(detail(m.el), 'field:credentials_file');
  assert.equal(input.attrs['aria-invalid'], 'true');
  assert.equal(doc.activeElement, input);
  const err = find(
    detail(m.el),
    (x) => x.attrs?.id === `${input.attrs.id}-error`,
  );
  assert.ok(!hidden(err));
  assert.equal(textOf(err), 'Use the absolute path of an existing file.');
  // Editing the field clears the host's complaint.
  m.type('field:credentials_file', '/tmp/key.json');
  assert.ok(hidden(err));
  assert.equal(input.attrs['aria-invalid'], undefined);
});

test('review: the egress and billing lines name a host, never a label', async () => {
  const m = await mount();
  await m.choose('custom');
  let d = textOf(detail(m.el));
  assert.match(d, /are sent to the endpoint you enter above\./);
  assert.doesNotMatch(d, /sent to Custom/);
  assert.doesNotMatch(d, /Sends one tiny request/);
  m.type('base-url', 'https://gw.example.com');
  d = textOf(detail(m.el));
  assert.match(d, /are sent to gw\.example\.com\./);
  assert.match(d, /Sends one tiny request to gw\.example\.com\./);
  await m.choose('bedrock');
  assert.match(
    textOf(detail(m.el)),
    /are sent to bedrock-runtime\.us-east-1\.amazonaws\.com\./,
  );
  m.type('field:region', '');
  assert.doesNotMatch(textOf(detail(m.el)), /sent to Amazon Bedrock/);
});

test('review: a result is announced once, through the sheet live region', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  m.type('key', 'test-key-123');
  byFk(detail(m.el), 'save').click();
  await settle();
  const res = byClass(m.el, 'ic-settings__result');
  assert.equal(res.attrs.role, undefined);
  assert.equal(res.attrs['aria-live'], undefined);
  const live = find(m.el, (x) => x.attrs?.['aria-live'] === 'polite');
  assert.match(live.textContent, /^Saved\./);
  assert.equal(all(m.el, (x) => x.attrs?.['aria-live']).length, 1);
});

test('review: Show/Hide swaps its name only, without a pressed state', async () => {
  const m = await mount();
  await m.choose('anthropic_api');
  const show = byFk(detail(m.el), 'show');
  assert.equal(show.attrs['aria-pressed'], undefined);
  assert.equal(show.attrs['aria-label'], 'Show the key');
  show.click();
  assert.equal(show.attrs['aria-pressed'], undefined);
  assert.equal(show.attrs['aria-label'], 'Hide the key');
  assert.equal(textOf(show), 'Hide');
  assert.equal(byFk(detail(m.el), 'key').type, 'text');
});

test('review: closing the sheet drops the last result', async () => {
  const m = await mount();
  await m.choose('bedrock');
  m.client.state.testReply = { ok: true, check_token: 't' };
  byFk(detail(m.el), 'use').click();
  await settle();
  assert.match(resultText(m.el), /The analyst now uses Amazon Bedrock/);
  m.sheet.close();
  m.sheet.open();
  await settle();
  assert.equal(resultText(m.el), '');
});

test('review: only the button row is sticky; result, why and notes sit above it', async () => {
  const m = await mount();
  await m.choose('minimax');
  const actions = byClass(detail(m.el), 'ic-settings__actions');
  const inActions = (cls) => Boolean(byClass(actions, cls));
  assert.ok(byFk(actions, 'use'));
  assert.ok(!inActions('ic-settings__result'));
  assert.ok(!inActions('ic-settings__why'));
  assert.ok(!inActions('ic-settings__help'));
  const kids = detail(m.el).children;
  const status = byClass(detail(m.el), 'ic-settings__outcome');
  assert.ok(
    kids.indexOf(status) >= 0 && kids.indexOf(status) < kids.indexOf(actions),
  );
});

test('review: every row has a sentence note; a bare host is mono', async () => {
  const client = fakeClient();
  client.state.settings.providers.splice(
    -1,
    0,
    provider({ id: 'future', label: 'Future', host: 'api.future.example' }),
  );
  const m = await mount({ client });
  const row = (id) =>
    find(
      m.el,
      (x) => x.attrs?.role === 'radio' && x.attrs['data-provider'] === id,
    );
  assert.match(textOf(row('minimax')), /MiniMax M-series models/);
  assert.match(textOf(row('alibaba')), /Qwen models/);
  const note = byClass(row('future'), 'ic-settings__rownote');
  assert.equal(textOf(note), 'api.future.example');
  assert.match(note.className, /ic-mono/);
  assert.doesNotMatch(
    byClass(row('minimax'), 'ic-settings__rownote').className,
    /ic-mono/,
  );
});

test('review: In use says No key, with a warning glyph, when its key is gone', async () => {
  const bus = fakeBus();
  const m = await mount({ bus });
  bus.emit('analyst:availability', {
    available: false,
    reason: 'provider_key_missing',
  });
  const inUse = byClass(m.el, 'ic-settings__inuse');
  assert.match(textOf(inUse), /No key/);
  assert.doesNotMatch(textOf(inUse), /Needs a check/);
  const state = byClass(inUse, 'ic-settings__state');
  assert.equal(state.attrs['data-state'], 'no_key');
  assert.match(textOf(state), /^warning/);
});
