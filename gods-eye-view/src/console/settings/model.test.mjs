import test from 'node:test';
import assert from 'node:assert/strict';

import * as M from './model.js';

const {
  COPY,
  validateModel,
  validateKey,
  validateField,
  validateBaseUrl,
  hostScope,
  fillPlaceholders,
} = M;

function strings(value, out = []) {
  if (typeof value === 'string') out.push(value);
  else if (typeof value === 'function') {
    const v = value('X', 'Y');
    if (typeof v === 'string') out.push(v);
  } else if (value && typeof value === 'object')
    for (const v of Object.values(value)) strings(v, out);
  return out;
}

test('copy: sentence case, never a middle dot, never shouting', () => {
  const all = [
    ...strings(COPY),
    ...strings(M.GROUPS),
    ...strings(M.FAMILY_TAG),
    ...strings(M.STATUS_WORD),
    ...strings(M.ROW_NOTE),
  ];
  assert.ok(all.length > 100);
  for (const s of all) {
    assert.doesNotMatch(s, /·/, s);
    assert.doesNotMatch(s, /\b[A-Z]{2,}\s[A-Z]{2,}\b/, s);
  }
  assert.equal(COPY.use('OpenRouter'), 'Use OpenRouter');
  assert.equal(
    COPY.requestsGo('openrouter.ai'),
    'Requests go to openrouter.ai',
  );
  assert.equal(
    COPY.egress('api.minimax.io'),
    'Your messages, the intel picture and tool results are sent to api.minimax.io. Only use a provider you trust with this data.',
  );
});

test('model names: the argv-safe pattern the host enforces', () => {
  for (const ok of [
    'claude-opus-5-5',
    'MiniMax-M3[1m]',
    '~anthropic/claude-opus-latest[1m]',
    'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    'claude-haiku-4-5@20251001',
    'qwen3.5',
  ])
    assert.equal(validateModel(ok), null, ok);
  assert.match(validateModel('-rm'), /can't start with a hyphen/);
  assert.match(validateModel('a b'), /Use letters/);
  assert.match(validateModel('a\nb'), /line breaks/);
  assert.match(validateModel('x'.repeat(201)), /Use letters/);
  assert.equal(validateModel(''), 'Enter a model.');
  assert.equal(validateModel('', { required: false }), null);
});

test('keys: trimmed, 8 to 512 printable ASCII characters', () => {
  assert.equal(validateKey('  test-key-123  '), null);
  assert.equal(validateKey('short'), 'That key is too short.');
  assert.equal(validateKey('x'.repeat(513)), 'That key is too long.');
  assert.match(validateKey('test key 123'), /spaces or line breaks/);
  assert.match(validateKey('test-kéy-123'), /printable characters only/);
  assert.equal(validateKey(''), 'Paste your key.');
});

test('fields: the host patterns, required and path rules', () => {
  const region = { id: 'region', required: true };
  assert.equal(validateField('bedrock', region, 'us-east-1'), null);
  assert.equal(validateField('bedrock', region, 'us-gov-west-1'), null);
  assert.match(validateField('bedrock', region, 'US-EAST-1'), /AWS region/);
  assert.equal(validateField('bedrock', region, ''), 'Fill this in.');
  assert.equal(validateField('vertex', { id: 'project' }, 'my-proj-01'), null);
  assert.match(
    validateField('vertex', { id: 'project' }, 'Proj'),
    /project ID/,
  );
  assert.equal(validateField('vertex', { id: 'region' }, 'us-east5'), null);
  assert.equal(validateField('foundry', { id: 'resource' }, 'my-res'), null);
  assert.match(
    validateField('vertex', { id: 'credentials_file' }, 'creds.json'),
    /full path/,
  );
  assert.equal(
    validateField('vertex', { id: 'credentials_file' }, '/Users/me/c.json'),
    null,
  );
  // The host's own pattern wins when it sends one.
  assert.match(
    validateField('x', { id: 'y', pattern: '^[0-9]+$' }, 'abc'),
    /doesn't look right/,
  );
});

test('address classes', () => {
  assert.equal(hostScope('localhost'), 'loopback');
  assert.equal(hostScope('127.0.0.5'), 'loopback');
  assert.equal(hostScope('[::1]'), 'loopback');
  assert.equal(hostScope('10.1.2.3'), 'private');
  assert.equal(hostScope('172.20.0.1'), 'private');
  assert.equal(hostScope('192.168.1.5'), 'private');
  assert.equal(hostScope('fd12::1'), 'private');
  assert.equal(hostScope('169.254.169.254'), 'link_local');
  assert.equal(hostScope('fe80::1'), 'link_local');
  assert.equal(hostScope('0.0.0.0'), 'unspecified');
  assert.equal(hostScope('api.minimax.io'), 'public');
});

test('endpoint URLs: https, loopback http, LAN http only when allowed', () => {
  const ok = validateBaseUrl('https://gw.example.com/anthropic/');
  assert.equal(ok.ok, true);
  assert.equal(ok.url, 'https://gw.example.com/anthropic');
  assert.equal(ok.host, 'gw.example.com');
  assert.equal(validateBaseUrl('http://localhost:11434').scope, 'loopback');
  const lan = validateBaseUrl('http://192.168.1.5:1234');
  assert.equal(lan.ok, false);
  assert.equal(lan.needsInsecureAck, true);
  assert.equal(
    validateBaseUrl('http://192.168.1.5:1234', { allowInsecureHttp: true }).ok,
    true,
  );
  assert.match(validateBaseUrl('http://example.com').error, /Use https:\/\//);
  assert.match(validateBaseUrl('https://u:p@example.com').error, /user name/);
  assert.match(validateBaseUrl('https://example.com/?a=1').error, /query/);
  assert.match(validateBaseUrl('https://example.com/#x').error, /fragment/);
  assert.match(validateBaseUrl('ftp://example.com').error, /https/);
  assert.match(validateBaseUrl('http://169.254.169.254').error, /Link-local/);
  assert.match(validateBaseUrl('http://[fe80::1]:80').error, /Link-local/);
  assert.match(validateBaseUrl('http://0.0.0.0:1').error, /can't be used/);
  assert.match(
    validateBaseUrl('http://localhost:54300', { selfHost: '127.0.0.1:54300' })
      .error,
    /own address/,
  );
  assert.equal(
    validateBaseUrl('http://localhost:54301', { selfHost: '127.0.0.1:54300' })
      .ok,
    true,
  );
  const idn = validateBaseUrl('https://bücher.example/api');
  assert.equal(idn.ok, true);
  assert.equal(idn.host, 'xn--bcher-kva.example');
  // The URL parser would silently drop an inner line break; the check won't.
  assert.match(validateBaseUrl('https://a.example/x\ny').error, /line breaks/);
});

test('placeholders fill from the fields; a leftover one is refused', () => {
  const tpl =
    'https://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/apps/anthropic';
  assert.deepEqual(fillPlaceholders(tpl, { workspace_id: 'ws-1' }), {
    url: 'https://ws-1.ap-southeast-1.maas.aliyuncs.com/apps/anthropic',
  });
  assert.match(
    fillPlaceholders(tpl, {}, { workspace_id: 'Workspace ID' }).error,
    /Fill in the Workspace ID first/,
  );
  assert.equal(
    validateBaseUrl(tpl, { fields: { workspace_id: 'ws-1' } }).host,
    'ws-1.ap-southeast-1.maas.aliyuncs.com',
  );
  assert.match(validateBaseUrl('https://x.example/{a').error, /placeholder/);
});

// ---- providers ---------------------------------------------------------------------

const P = {
  login: {
    id: 'anthropic_login',
    label: 'Claude login (this Mac)',
    kind: 'anthropic_login',
    group: 'claude',
    model_family: 'claude',
    auth: { scheme: 'login' },
    status: 'active',
  },
  api: {
    id: 'anthropic_api',
    label: 'Anthropic API key',
    kind: 'anthropic_key',
    group: 'claude',
    model_family: 'claude',
    auth: { scheme: 'x-api-key' },
    key: { configured: false, source: 'none' },
    quick_check: { kind: 'models_list', billable: false },
  },
  bedrock: {
    id: 'bedrock',
    label: 'Amazon Bedrock',
    kind: 'bedrock',
    group: 'cloud',
    model_family: 'claude',
    auth: { scheme: 'cloud', key_optional: true },
  },
  vertex: {
    id: 'vertex',
    label: 'Vertex',
    kind: 'vertex',
    group: 'cloud',
    model_family: 'claude',
    auth: { scheme: 'cloud' },
  },
  openrouter: {
    id: 'openrouter',
    label: 'OpenRouter',
    kind: 'anthropic_compatible',
    group: 'compatible',
    model_family: 'mixed',
    base_url: {
      value: 'https://openrouter.ai/api',
      editable: false,
      presets: [],
    },
    auth: { scheme: 'bearer' },
    key: { configured: true, source: 'keychain', masked: '…9f2a' },
    quick_check: { kind: 'key_info', billable: false },
  },
  ollama: {
    id: 'ollama',
    label: 'Ollama',
    kind: 'anthropic_compatible',
    group: 'compatible',
    model_family: 'non_claude',
    base_url: {
      value: 'http://localhost:11434',
      editable: false,
      presets: [
        { label: 'Local', url: 'http://localhost:11434' },
        { label: 'Cloud', url: 'https://ollama.com' },
      ],
    },
    auth: { scheme: 'bearer', key_optional: 'loopback' },
    quick_check: { kind: 'messages', billable: true },
  },
  env: {
    id: 'deepseek',
    label: 'DeepSeek',
    kind: 'anthropic_compatible',
    model_family: 'non_claude',
    auth: { scheme: 'bearer' },
    key: { configured: true, source: 'environment', env: 'DEEPSEEK_API_KEY' },
  },
  custom: {
    id: 'custom',
    label: 'Custom',
    kind: 'custom',
    group: 'custom',
    model_family: 'non_claude',
    base_url: { value: '', editable: true, presets: [] },
    auth: { scheme: 'bearer' },
  },
};

test('grouping: catalog groups map to the four headings, in order', () => {
  const groups = M.groupProviders(Object.values(P));
  assert.deepEqual(
    groups.map((g) => g.label),
    [
      'Claude from Anthropic',
      'Claude on your cloud',
      'Other providers',
      'Custom endpoint',
    ],
  );
  assert.deepEqual(
    groups[0].providers.map((p) => p.id),
    ['anthropic_login', 'anthropic_api'],
  );
  assert.equal(M.familyTag(P.openrouter), 'Many models');
  assert.equal(
    M.statusWord({ status: 'from_environment' }),
    'Set by the environment',
  );
});

test('key modes per kind', () => {
  assert.equal(M.keyMode(P.login), 'login');
  assert.equal(M.keyMode(P.api), 'input');
  assert.equal(M.keyMode(P.bedrock), 'optional');
  assert.equal(M.keyMode(P.vertex), 'none_needed');
  assert.equal(M.keyMode(P.openrouter), 'saved');
  assert.equal(M.keyMode(P.env), 'environment');
  assert.equal(M.keyMode(P.custom, { auth_scheme: 'none' }), 'keyless');
  assert.equal(M.keyMode(P.custom, { auth_scheme: 'bearer' }), 'input');
  // Ollama: keyless on this Mac, a key for Ollama Cloud.
  assert.equal(
    M.keyMode(P.ollama, { base_url: 'http://localhost:11434' }),
    'keyless',
  );
  assert.equal(
    M.keyMode(P.ollama, { base_url: 'https://ollama.com' }),
    'input',
  );
  assert.equal(M.last4(P.openrouter), '9f2a');
});

test('the non-Claude acknowledgement: non_claude always, mixed unless anthropic/', () => {
  assert.equal(M.needsAck(P.ollama, 'qwen3.5'), true);
  assert.equal(M.needsAck(P.openrouter, 'anthropic/claude-opus-5.5'), false);
  assert.equal(
    M.needsAck(P.openrouter, '~anthropic/claude-opus-latest[1m]'),
    false,
  );
  assert.equal(M.needsAck(P.openrouter, 'openai/gpt-6-sol'), true);
  assert.equal(M.needsAck(P.api, 'claude-opus-5-5'), false);
  assert.equal(M.sendsDataOut(P.login), false);
  assert.equal(M.sendsDataOut(P.api), true);
});

test('quick checks: free key checks, paid one-token probes, none for cloud kinds', () => {
  assert.deepEqual(M.quickCheck(P.api), {
    available: true,
    keyOnly: true,
    billable: false,
  });
  assert.deepEqual(M.quickCheck(P.ollama), {
    available: true,
    keyOnly: false,
    billable: true,
  });
  assert.equal(M.quickCheck(P.bedrock).available, false);
  assert.equal(M.quickCheck({ id: 'x', quick_check: true }).available, true);
});

test('PUT and test bodies: the key only when typed, never kept; activation extras', () => {
  const draft = {
    ...M.draftFrom(P.openrouter),
    model: 'openai/gpt-6-sol',
    ack: true,
  };
  const keep = M.putBody(P.openrouter, draft);
  assert.deepEqual(keep.key, { action: 'keep' });
  assert.equal(keep.base_url, null);
  assert.equal(keep.activate, false);
  assert.equal('check_token' in keep, false);
  const use = M.putBody(P.openrouter, draft, {
    keyText: ' test-key-123 ',
    activate: true,
    checkToken: 'tok',
  });
  assert.deepEqual(use.key, { action: 'set', value: 'test-key-123' });
  assert.equal(use.check_token, 'tok');
  assert.equal(use.acknowledge_non_claude, true);
  const t = M.testBody(
    P.custom,
    {
      ...M.draftFrom(P.custom),
      model: 'm',
      base_url: ' https://gw.example.com ',
    },
    { depth: 'full' },
  );
  assert.equal(t.depth, 'full');
  assert.equal(t.base_url, 'https://gw.example.com');
  assert.equal(t.auth_scheme, 'bearer');
  assert.equal('key' in t, false);
  assert.equal(
    M.testBody(P.api, M.draftFrom(P.api), { keyText: 'test-key-456' }).key,
    'test-key-456',
  );
});

test('why Use waits, in the order the operator can fix it', () => {
  const s = { active: 'anthropic_login', locks: {} };
  const d = {
    ...M.draftFrom(P.ollama),
    model: 'qwen3.5',
    base_url: 'https://ollama.com',
  };
  assert.equal(M.useBlocker(P.ollama, d, { settings: s }), 'Add a key first');
  assert.equal(
    M.useBlocker(P.ollama, d, { settings: s, keyText: 'test-key-123' }),
    'Check the acknowledgement first',
  );
  assert.equal(
    M.useBlocker(
      P.ollama,
      { ...d, ack: true },
      { settings: s, keyText: 'test-key-123' },
    ),
    null,
  );
  assert.equal(
    M.useBlocker(P.ollama, d, { settings: s, busy: true }),
    'Wait for the check to finish',
  );
  assert.equal(
    M.useBlocker(P.ollama, d, { settings: { ...s, read_only: true } }),
    'Settings were written by a newer version',
  );
  assert.equal(
    M.useBlocker(P.ollama, d, {
      settings: { ...s, locks: { provider: 'bedrock' } },
    }),
    'Settings are set by the environment',
  );
  assert.equal(
    M.useBlocker(P.ollama, d, {
      settings: s,
      keyText: 'test-key-123',
      validation: { ok: false },
    }),
    'Fix the highlighted fields first',
  );
  assert.equal(
    M.useBlocker(P.login, M.draftFrom(P.login), { settings: s }),
    'Already in use',
  );
});

test('In use: connected, failed or needs a check, always with a glyph', () => {
  const settings = {
    active: 'openrouter',
    providers: [
      {
        ...P.openrouter,
        model: 'm',
        host: 'openrouter.ai',
        tested: { ok: true },
      },
    ],
  };
  assert.deepEqual(M.inUseSummary(settings), {
    id: 'openrouter',
    label: 'OpenRouter',
    model: 'm',
    host: 'openrouter.ai',
    state: 'connected',
    word: 'Connected',
    glyph: 'check',
  });
  assert.equal(
    M.inUseSummary(settings, { reason: 'provider_auth' }).word,
    "Couldn't connect",
  );
  settings.providers[0].tested = null;
  assert.equal(M.inUseSummary(settings).glyph, 'warning');
  assert.equal(M.inUseSummary({ active: 'x', providers: [] }), null);
});

test('check results in the spec words (§10 table)', () => {
  const mm = {
    id: 'minimax',
    label: 'MiniMax',
    quick_check: { kind: 'messages', billable: true },
  };
  assert.equal(
    M.resultCopy(mm, { ok: true, latency_ms: 812 }, { model: 'MiniMax-M3[1m]' })
      .text,
    'Connected. MiniMax-M3[1m] answered in 0.8 s.',
  );
  assert.equal(
    M.resultCopy(P.openrouter, { ok: true, latency_ms: 50 }).text,
    'Connected. OpenRouter accepted the key.',
  );
  assert.equal(
    M.resultCopy(mm, { ok: true, depth: 'full' }).text,
    "The analyst's engine works with MiniMax.",
  );
  const down = M.resultCopy(
    mm,
    { ok: true, depth: 'full', thinking: 'off' },
    { requestedThinking: 'auto' },
  );
  assert.equal(down.downgraded, true);
  assert.match(
    down.text,
    /MiniMax doesn't accept extended thinking, so it's turned off for this model\./,
  );
  const fail = M.resultCopy(mm, {
    ok: false,
    status: 404,
    code: 'endpoint',
    host: 'api.minimax.io',
    hint: 'h',
  });
  assert.equal(
    fail.text,
    "Couldn't connect: Nothing answered at api.minimax.io/v1/messages. The URL should be the API root, without /v1.",
  );
  assert.equal(fail.status, 404);
  assert.equal(fail.tone, 'critical');
  assert.equal(
    M.resultCopy(mm, { ok: false, depth: 'full', message: 'x.' }).text,
    "The analyst's engine couldn't use MiniMax: x.",
  );
  assert.equal(
    M.codeMessage('redirect', { host: 'h.example' }),
    "h.example tried to redirect the request. The key wasn't sent on.",
  );
});

test('settings route errors read as words the operator can act on', () => {
  const err = (status, body, name = 'HttpError') => ({ name, status, body });
  assert.equal(M.settingsErrorCopy(err(401, null, 'AuthError')).code, 'token');
  assert.equal(M.settingsErrorCopy({ name: 'OfflineError' }).code, 'offline');
  assert.match(
    M.settingsErrorCopy({ name: 'TimeoutError' }).text,
    /took too long/,
  );
  assert.deepEqual(
    M.settingsErrorCopy(
      err(422, {
        error: 'invalid_settings',
        field: 'model',
        message: 'Bad model.',
      }),
    ),
    { code: 'invalid_settings', text: 'Bad model.', field: 'model' },
  );
  assert.match(
    M.settingsErrorCopy(
      err(409, { error: 'locked_by_environment', env: 'GODSEYE_CHAT_MODEL' }),
    ).text,
    /Set by GODSEYE_CHAT_MODEL in the environment/,
  );
  assert.match(
    M.settingsErrorCopy(err(409, { error: 'needs_ack' })).text,
    /acknowledgement/,
  );
  assert.equal(
    M.settingsErrorCopy(err(403, { error: 'cross_origin' })).text,
    COPY.crossOrigin,
  );
  assert.equal(
    M.settingsErrorCopy(err(404, null)).code,
    'settings_unavailable',
  );
  assert.equal(
    M.settingsErrorCopy(err(503, { error: 'settings_unavailable' })).text,
    COPY.unavailable,
  );
});

test('analyst surfaces: header, divider, unavailable panel, cost', () => {
  assert.equal(
    M.modelVia('MiniMax-M3[1m]', { label: 'MiniMax' }),
    'MiniMax-M3[1m] via MiniMax',
  );
  assert.equal(M.modelVia('claude-opus-5-5', null), 'claude-opus-5-5');
  assert.equal(
    M.providerChangedText({
      to: { label: 'MiniMax', model: 'MiniMax-M3[1m]' },
      memory: 'kept',
    }),
    'The analyst now uses MiniMax (MiniMax-M3[1m]).',
  );
  assert.equal(
    M.providerChangedText({
      to: { label: 'OpenRouter', model: 'm' },
      memory: 'cleared',
    }),
    "The analyst now uses OpenRouter (m). It doesn't remember the conversation above.",
  );
  const nc = M.providerUnavailableCopy('provider_not_configured');
  assert.equal(nc.title, 'The analyst has no model provider set up.');
  assert.equal(nc.body, 'Add a key for Claude or another provider.');
  assert.equal(nc.action, 'Open analyst settings');
  const km = M.providerUnavailableCopy('provider_key_missing', {
    id: 'minimax',
    label: 'MiniMax',
  });
  assert.equal(km.title, 'The analyst has no key for MiniMax.');
  assert.equal(km.action, 'Open analyst settings');
  assert.equal(km.provider, 'minimax');
  assert.equal(
    M.providerUnavailableCopy('provider_auth', { label: 'MiniMax' }).title,
    "MiniMax rejected the analyst's key.",
  );
  assert.equal(
    M.providerUnavailableCopy('provider_auth').action,
    'Open analyst settings',
  );
  assert.equal(M.providerUnavailableCopy('sdk_missing'), null);
  assert.equal(
    M.costNote({ label: 'MiniMax' }),
    "Cost isn't shown: MiniMax bills you directly.",
  );
  assert.equal(M.costUnreliable('unreliable'), true);
  assert.equal(M.costUnreliable('anthropic_list'), false);
  assert.equal(
    M.approvalLine({ count: 2, oldest: { title: 'Grid search' } }),
    '2 approvals waiting. Oldest: Grid search.',
  );
});

test('the host-sent Claude prefixes decide the acknowledgement for a mixed provider', () => {
  const p = {
    model_family: 'mixed',
    claude_prefixes: ['anthropic/', '~anthropic/'],
  };
  assert.equal(M.needsAck(p, 'anthropic/claude-sonnet-5'), false);
  assert.equal(M.needsAck(p, 'x-ai/grok-4.7'), true);
  const custom = { id: 'custom', model_family: 'non_claude' };
  assert.equal(M.familyOf(custom, { model_family: 'claude' }), 'claude');
  assert.equal(
    M.needsAck(custom, 'm', M.familyOf(custom, { model_family: 'claude' })),
    false,
  );
  assert.equal(
    M.putBody(custom, { ...M.draftFrom(custom), model: 'm' }).model_family,
    'non_claude',
  );
  assert.equal('model_family' in M.putBody(P.api, M.draftFrom(P.api)), false);
});

test('a local answer in a few ms never reads as 0.0 s', () => {
  const mm = { id: 'x', label: 'X', quick_check: { kind: 'messages' } };
  assert.equal(
    M.resultCopy(mm, { ok: true, latency_ms: 4 }, { model: 'm' }).text,
    'Connected. m answered in 0.1 s.',
  );
});

// ---- review fixes (BYOK UX review) --------------------------------------------------

test('review: a busy check reads the host words and carries its retry-after', () => {
  const busy = M.settingsErrorCopy({
    name: 'HttpError',
    status: 429,
    body: {
      error: 'test_busy',
      message: 'Wait a few seconds between checks.',
      retry_after_s: 4.2,
    },
  });
  assert.equal(busy.code, 'test_busy');
  assert.equal(busy.text, 'Wait a few seconds between checks.');
  assert.equal(busy.retryAfterMs, 4200);
  const bare = M.settingsErrorCopy({
    status: 429,
    body: { error: 'test_busy' },
  });
  assert.equal(bare.text, 'Another check is running. Try again in a moment.');
  assert.equal(bare.retryAfterMs, null);
});

test('review: In use needs a check after an untested save; no key says so', () => {
  const s = {
    active: 'openrouter',
    providers: [
      {
        ...P.openrouter,
        model: 'm',
        host: 'openrouter.ai',
        status: 'active',
        tested: { ok: true, current: false },
      },
    ],
  };
  assert.equal(M.inUseSummary(s).state, 'needs_check');
  assert.equal(M.inUseSummary(s).word, 'Needs a check');
  assert.equal(M.needsCheck(s.providers[0]), true);
  s.providers[0].tested = { ok: true, current: true };
  assert.equal(M.inUseSummary(s).state, 'connected');
  assert.equal(M.needsCheck(s.providers[0]), false);
  const noKey = M.inUseSummary(s, { reason: 'provider_key_missing' });
  assert.equal(noKey.word, 'No key');
  assert.equal(noKey.glyph, 'warning');
  const none = M.inUseSummary(s, { reason: 'provider_not_configured' });
  assert.equal(none.word, 'Not set up');
  assert.equal(none.glyph, 'warning');
});

test('review: an endpoint ending in /v1 is refused; the help shows the full path', () => {
  const v = validateBaseUrl('https://api.example.com/v1/');
  assert.equal(v.ok, false);
  assert.match(v.error, /Remove \/v1 from the end/);
  assert.equal(validateBaseUrl('https://gw.example.com/v1beta').ok, true);
  assert.equal(
    M.endpointTarget(
      validateBaseUrl('https://dashscope.aliyuncs.com/apps/anthropic/'),
    ),
    'dashscope.aliyuncs.com/apps/anthropic/v1/messages',
  );
  assert.equal(M.endpointTarget(validateBaseUrl('')), null);
  assert.doesNotMatch(COPY.endpointHelpEmpty, /[{}]/);
  assert.equal(
    COPY.endpointHelpEmpty,
    'The Anthropic Messages API root, without /v1.',
  );
});

test('review: check hints are worded for the sheet, never "in analyst settings"', () => {
  const mm = {
    id: 'minimax',
    label: 'MiniMax',
    quick_check: { kind: 'messages', billable: true },
  };
  const fail = (over) => M.resultCopy(mm, { ok: false, ...over });
  assert.equal(
    fail({
      status: 401,
      code: 'auth',
      hint: 'Open analyst settings to replace the key.',
    }).hint,
    'Replace the key above.',
  );
  // A precise hint from the host stays as it is.
  assert.equal(
    fail({
      status: 403,
      code: 'auth',
      hint: "Check the key's permissions or plan in your MiniMax account.",
    }).hint,
    "Check the key's permissions or plan in your MiniMax account.",
  );
  assert.equal(
    fail({
      status: 404,
      code: 'model',
      hint: 'Pick another model in analyst settings.',
    }).hint,
    'Pick another model above.',
  );
  const bad = fail({
    status: 400,
    code: 'invalid_request',
    hint: 'Run a full check in analyst settings.',
  });
  assert.equal(
    bad.hint,
    "Run a full check to see whether the analyst's engine works.",
  );
  assert.equal(bad.offerFull, true);
  const badFull = fail({
    depth: 'full',
    status: 400,
    code: 'invalid_request',
    hint: 'Run a full check in analyst settings.',
  });
  assert.equal(badFull.hint, null);
  assert.equal(badFull.offerFull, false);
  assert.equal(
    fail({
      code: 'invalid_request',
      hint: 'Turn off extended thinking for this model in analyst settings.',
    }).hint,
    'Turn off extended thinking for this model under Advanced.',
  );
  assert.equal(
    fail({ code: 'config', hint: 'Open analyst settings.' }).hint,
    null,
  );
  assert.equal(
    fail({ code: 'x', hint: 'Check the region in analyst settings.' }).hint,
    'Check the region.',
  );
});

test('review: cloud kinds name their real endpoint host; an unknown one is null', () => {
  const bedrock = {
    ...P.bedrock,
    fields: [{ id: 'region', required: true }],
  };
  assert.equal(
    M.draftHost(bedrock, { fields: { region: 'eu-west-1' } }),
    'bedrock-runtime.eu-west-1.amazonaws.com',
  );
  assert.equal(M.draftHost(bedrock, { fields: { region: '' } }), null);
  assert.equal(M.draftHost(bedrock, { fields: { region: 'nope' } }), null);
  const foundry = {
    id: 'foundry',
    kind: 'foundry',
    label: 'Microsoft Foundry',
    fields: [{ id: 'resource', required: true }],
  };
  assert.equal(
    M.draftHost(foundry, { fields: { resource: 'res-1' } }),
    'res-1.services.ai.azure.com',
  );
  assert.equal(M.draftHost(foundry, { fields: {} }), null);
  const vertex = { ...P.vertex, fields: [{ id: 'region' }] };
  assert.equal(
    M.draftHost(vertex, { fields: { region: '' } }),
    'aiplatform.googleapis.com',
  );
  assert.equal(
    M.draftHost(vertex, { fields: { region: 'us-east5' } }),
    'us-east5-aiplatform.googleapis.com',
  );
  // A URL kind with nothing typed yet goes nowhere, never to its label.
  assert.equal(M.draftHost(P.custom, { base_url: '' }), null);
  // In use names the saved cloud host too.
  const s = {
    active: 'bedrock',
    providers: [
      {
        ...bedrock,
        host: 'Amazon Bedrock',
        values: { region: 'us-west-2' },
        tested: { ok: true },
      },
    ],
  };
  assert.equal(
    M.inUseSummary(s).host,
    'bedrock-runtime.us-west-2.amazonaws.com',
  );
  s.providers[0].values = {};
  assert.equal(M.inUseSummary(s).host, null);
});

test('review: provider reasons open analyst settings; rows have a sentence note', () => {
  for (const reason of M.PROVIDER_REASONS)
    assert.equal(
      M.providerUnavailableCopy(reason, { id: 'x', label: 'X' }).action,
      'Open analyst settings',
    );
  for (const id of [
    'minimax',
    'deepseek',
    'moonshot',
    'zai',
    'zhipu',
    'alibaba',
  ])
    assert.match(M.rowNote({ id, host: 'api.example.com' }), /^[A-Z].* /);
  assert.equal(
    M.rowNote({ id: 'other', host: 'api.example.com' }),
    'api.example.com',
  );
  assert.equal(M.rowNoteIsHost({ id: 'other', host: 'api.example.com' }), true);
  assert.equal(
    M.rowNoteIsHost({ id: 'minimax', host: 'api.minimax.io' }),
    false,
  );
});

test('review: the provider in use with an untested config can be checked and used again', () => {
  const p = {
    ...P.openrouter,
    model: 'anthropic/claude-opus-5.5',
    status: 'active',
    tested: { ok: true, current: true },
  };
  const settings = { active: 'openrouter', providers: [p] };
  const draft = M.draftFrom(p);
  assert.equal(M.useBlocker(p, draft, { settings }), COPY.blockActive);
  p.tested = { ok: true, current: false };
  assert.equal(M.useBlocker(p, M.draftFrom(p), { settings }), null);
  // Save on the provider in use checks first, unless nothing changed.
  assert.equal(M.saveNeedsCheck(p, M.draftFrom(p), { settings }), false);
  assert.equal(
    M.saveNeedsCheck(p, { ...M.draftFrom(p), model: 'x/y' }, { settings }),
    true,
  );
  assert.equal(
    M.saveNeedsCheck(p, M.draftFrom(p), { settings, keyText: 'test-key-123' }),
    true,
  );
  assert.equal(
    M.saveNeedsCheck(p, M.draftFrom(p), { settings: { active: 'other' } }),
    false,
  );
});
