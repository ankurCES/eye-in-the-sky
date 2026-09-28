/**
 * Analyst settings: the pure model behind the settings sheet (BYOK spec §7,
 * §10). No DOM, no network.
 *
 * - Copy: every string the sheet writes, sentence case, never a `·`.
 * - Grouping, family tags and status words for the provider picker.
 * - Validators that mirror the host's rules (§7.3), so the operator sees a
 *   problem before a round trip. The host stays the authority: it runs the
 *   same checks again and its 422 `{field, message}` wins.
 * - Request bodies for PUT /settings/llm and POST /settings/llm/test. A key
 *   the operator typed is passed straight through and never stored here.
 * - What "Use {label}" is waiting for, the In use summary, and the words for
 *   a connection check's result.
 */

// ---- copy ----------------------------------------------------------------------

export const COPY = Object.freeze({
  title: 'Analyst settings',
  close: 'Close analyst settings',
  back: 'Back',
  loading: 'Loading analyst settings…',
  loadFailed: (message) => `Couldn't load analyst settings: ${message}`,
  retry: 'Try again',
  readOnly:
    'These settings were written by a newer version of Eye in the Sky. They are shown read-only.',
  crossOrigin: 'Open the console from the app host to change analyst settings.',
  unavailable: "Analyst settings aren't available in this app.",
  token:
    "The console's access token was rejected. Quit and reopen the app to get a new one.",
  offline:
    "Can't reach Eye in the Sky's local service. Check that the app is still running.",
  inUseHeading: 'In use',
  inUse: 'In use:',
  inUseNone: 'No model provider is in use yet.',
  requestsGo: (host) => `Requests go to ${host}`,
  providerHeading: 'Provider',
  keyHeading: 'Key',
  keyPlaceholder: 'Paste your key',
  keyOptional: (label) => `${label} (optional)`,
  show: 'Show',
  hide: 'Hide',
  showKey: 'Show the key',
  hideKey: 'Hide the key',
  savedKey: 'Saved key ending in',
  storedKeychain: 'Stored in your macOS Keychain',
  storedFile: 'Stored in a file only you can read:',
  storedUnknown: 'Stored on this Mac',
  replaceKey: 'Replace key',
  removeKey: 'Remove key',
  keepKey: 'Keep key',
  cancel: 'Cancel',
  fromEnvA: 'Using',
  fromEnvB:
    'from the environment. To manage the key here, quit the app and unset it.',
  keyless: 'No key needed for a local server.',
  login:
    'Uses the Claude sign-in on this Mac. To sign in, run claude in a terminal, then /login.',
  cloudKey: Object.freeze({
    bedrock:
      'Optional. Leave it empty to use your AWS credentials: the profile below, or the default chain.',
    vertex:
      'Uses your Google Cloud credentials: gcloud application-default login, or the credentials file below.',
    foundry: 'Optional. Leave it empty to sign in with Microsoft Entra ID.',
  }),
  removeConfirm: (label) =>
    `Remove the saved ${label} key? The analyst stops working with ${label} until you add a key.`,
  modelHeading: 'Model',
  modelLabel: 'Model',
  modelHelp: (label) => `Sent to ${label} exactly as typed.`,
  advanced: 'Advanced',
  smallModel: 'Model for background tasks',
  smallHelp: 'Used for short side requests. Leave empty to use the main model.',
  thinking: 'Extended thinking',
  thinkingAuto: 'Auto',
  thinkingOff: 'Off',
  thinkingPinned: 'Always on for this provider.',
  authScheme: 'Authentication',
  schemeBearer: 'Bearer token',
  schemeXApiKey: 'x-api-key header',
  schemeNone: 'None',
  servesLabel: 'Models it serves',
  servesClaude: 'Claude models',
  servesOther: 'Not Claude',
  endpointHeading: 'Endpoint',
  endpointLabel: 'Endpoint URL',
  endpointPreset: 'Endpoint',
  customUrl: 'Another URL',
  endpointHelp: (target) =>
    `The Anthropic Messages API root. Requests go to ${target}.`,
  endpointHelpEmpty: 'The Anthropic Messages API root, without /v1.',
  endpointV1: 'Remove /v1 from the end: the analyst adds /v1/messages itself.',
  loopback: 'Local endpoint. Traffic stays on this Mac.',
  lanWarning:
    "This endpoint isn't encrypted. Your messages, the intel picture and your key would cross the network in the clear.",
  lanAllow: 'Allow an unencrypted endpoint on my network',
  fieldsHeading: 'Account',
  notesHeading: 'Notes',
  docs: 'Provider docs',
  copy: 'Copy',
  copyDocs: 'Copy the provider docs link',
  copied: 'Copied the docs link.',
  copyFailed: "Couldn't copy. Select the link and copy it.",
  safetyHeading: 'Safety and data',
  safetyTitle: "This isn't a Claude model",
  safetyBody:
    'The analyst is built and tested with Claude. Other models may misuse tools, skip parts of the ISR doctrine or misread the picture. Every command, sensor tasking and simulation change still waits for your approval, whatever the model.',
  egress: (host) =>
    `Your messages, the intel picture and tool results are sent to ${host}. Only use a provider you trust with this data.`,
  egressPending:
    'Your messages, the intel picture and tool results are sent to the endpoint you enter above. Only use a provider you trust with this data.',
  ack: 'I understand; use it anyway',
  test: 'Test connection',
  save: 'Save',
  saveAndCheck: 'Save and check',
  use: (label) => `Use ${label}`,
  inUseButton: 'In use',
  paidProbe: (host) =>
    `Sends one tiny request to ${host}. Your provider may bill it.`,
  fullOnly:
    "This provider has no quick check: Test connection runs the analyst's engine once.",
  fullCheck: 'Run a full check',
  testing: (label) => `Testing ${label}…`,
  checking: "Checking with the analyst's engine…",
  quickOk: (model, seconds) => `Connected. ${model} answered in ${seconds} s.`,
  keyOk: (label) => `Connected. ${label} accepted the key.`,
  failed: (message) => `Couldn't connect: ${message}`,
  fullOk: (label) => `The analyst's engine works with ${label}.`,
  fullFailed: (label, message) =>
    `The analyst's engine couldn't use ${label}: ${message}`,
  downgraded: (label) =>
    `${label} doesn't accept extended thinking, so it's turned off for this model.`,
  busyWait: 'Another check just ran. Waiting a few seconds…',
  saved: 'Saved.',
  savedActive: 'Saved. The analyst uses it from your next message.',
  savedInactive: (active, label) =>
    `Saved. The analyst keeps using ${active} until you choose Use ${label}.`,
  activated: (label) =>
    `The analyst now uses ${label}. The change takes effect with your next message.`,
  removed: (label) => `Removed the saved ${label} key.`,
  blockReadOnly: 'Settings were written by a newer version',
  blockLocked: 'Settings are set by the environment',
  blockKey: 'Add a key first',
  blockInvalid: 'Fix the highlighted fields first',
  blockAck: 'Check the acknowledgement first',
  blockActive: 'Already in use',
  blockBusy: 'Wait for the check to finish',
  lockedModel: (env) =>
    `Set by ${env} in the environment. To change it here, quit the app and unset it.`,
  approvalOne: (what) => `Approval waiting: ${what}.`,
  approvalBare: 'Approval waiting.',
  approvalMany: (n, what) =>
    `${n} approvals waiting.${what ? ` Oldest: ${what}.` : ''}`,
  review: 'Review',
  http: (status) => `HTTP ${status}`,
});

/** Group headings, in display order (§10 section 2). */
export const GROUPS = Object.freeze([
  Object.freeze({ id: 'anthropic', label: 'Claude from Anthropic' }),
  Object.freeze({ id: 'cloud', label: 'Claude on your cloud' }),
  Object.freeze({ id: 'compatible', label: 'Other providers' }),
  Object.freeze({ id: 'custom', label: 'Custom endpoint' }),
]);

/** Family tags: words in Pencil, never a colour. */
export const FAMILY_TAG = Object.freeze({
  claude: 'Claude models',
  non_claude: 'Not Claude',
  mixed: 'Many models',
});

/** Provider status words (§7.1 `status`). */
export const STATUS_WORD = Object.freeze({
  active: 'In use',
  ready: 'Ready',
  key_saved: 'Key saved',
  not_configured: 'Not set up',
  from_environment: 'Set by the environment',
  needs_check: 'Needs a check',
});

/** One-line notes for the picker rows, by catalog id. */
export const ROW_NOTE = Object.freeze({
  anthropic_login: 'Your Claude sign-in on this Mac',
  anthropic_api: 'Your Anthropic API key',
  bedrock: 'Claude in your AWS account',
  vertex: 'Claude in your Google Cloud project',
  foundry: 'Claude in your Azure resource',
  openrouter: 'One key for Claude and many other models',
  minimax: 'MiniMax M-series models',
  deepseek: 'DeepSeek models',
  moonshot: 'Kimi models from Moonshot AI',
  zai: 'GLM models from Z.ai',
  zhipu: 'GLM models from Zhipu in China',
  alibaba: 'Qwen models in your Model Studio workspace',
  ollama: 'Models on this Mac or Ollama Cloud',
  lmstudio: 'Models on this Mac',
  custom: 'Any Anthropic Messages API',
});

/** Field labels (§10 section 6) when the host sends none. */
export const FIELD_LABEL = Object.freeze({
  'bedrock.region': 'AWS region',
  'bedrock.profile': 'AWS profile',
  'vertex.project': 'Google Cloud project',
  'vertex.region': 'Region',
  'vertex.credentials_file': 'Credentials file',
  'foundry.resource': 'Foundry resource name',
  'alibaba.workspace_id': 'Workspace ID',
});

// ---- validation (mirrors the host, §7.3) ---------------------------------------

/** `--model <v>` goes on the CLI's argv: never a leading `-`. */
export const MODEL_RE = /^[A-Za-z0-9~][A-Za-z0-9._:/@[\]~+-]{0,199}$/;
/** Printable ASCII without spaces, 8–512 characters, after trimming. */
export const KEY_RE = /^[\x21-\x7E]{8,512}$/;
const CONTROL_RE = /[\r\n\0]/;

/** Field patterns by `provider.field` (the host's own pattern wins if sent). */
export const FIELD_PATTERN = Object.freeze({
  'bedrock.region': /^[a-z]{2}(-gov)?-[a-z]+-\d$/,
  'bedrock.profile': /^[A-Za-z0-9_.+-]{1,64}$/,
  'vertex.project': /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/,
  'vertex.region': /^(global|us|eu|[a-z]+-[a-z]+\d)$/,
  'foundry.resource': /^[A-Za-z0-9][A-Za-z0-9-]{1,62}$/,
  'alibaba.workspace_id': /^[A-Za-z0-9-]{1,64}$/,
});

const FIELD_HINT = Object.freeze({
  'bedrock.region': 'Use an AWS region such as us-east-1.',
  'bedrock.profile':
    'Use a profile name: letters, digits and . _ + - only, up to 64 characters.',
  'vertex.project':
    'Use a Google Cloud project ID: 6 to 30 lowercase letters, digits or hyphens, starting with a letter.',
  'vertex.region': 'Use global, us, eu or a region such as us-east5.',
  'foundry.resource':
    'Use the resource name: letters, digits and hyphens, up to 63 characters.',
  'alibaba.workspace_id':
    'Use the workspace ID: letters, digits and hyphens, up to 64 characters.',
});

/** Whether any string in `value` carries CR, LF or NUL. */
export function hasControl(value) {
  return typeof value === 'string' && CONTROL_RE.test(value);
}

/** '' → required error; bad → pattern error; else null. */
export function validateModel(raw, { required = true } = {}) {
  const value = String(raw ?? '').trim();
  if (!value) return required ? 'Enter a model.' : null;
  if (hasControl(value)) return "A model name can't contain line breaks.";
  if (value.startsWith('-')) return "A model name can't start with a hyphen.";
  if (!MODEL_RE.test(value))
    return 'Use letters, digits and . _ : / @ [ ] ~ + - only, up to 200 characters.';
  return null;
}

/** A pasted key: trimmed, 8–512 printable ASCII characters. */
export function validateKey(raw) {
  const value = String(raw ?? '').trim();
  if (!value) return 'Paste your key.';
  if (hasControl(value) || /\s/.test(value))
    return "A key can't contain spaces or line breaks.";
  if (value.length < 8) return 'That key is too short.';
  if (value.length > 512) return 'That key is too long.';
  if (!KEY_RE.test(value))
    return 'A key uses printable characters only (no accents or symbols outside ASCII).';
  return null;
}

function fieldKey(providerId, fieldId) {
  return `${providerId}.${fieldId}`;
}

function serverPattern(field) {
  if (typeof field?.pattern !== 'string' || !field.pattern) return null;
  try {
    return new RegExp(field.pattern);
  } catch {
    return null;
  }
}

/** One provider field (§7.3 table): the error, or null. */
export function validateField(providerId, field, raw) {
  const id = String(field?.id ?? '');
  const value = String(raw ?? '').trim();
  if (!value) return field?.required ? 'Fill this in.' : null;
  if (hasControl(value)) return "This can't contain line breaks.";
  const key = fieldKey(providerId, id);
  if (id === 'credentials_file' || field?.kind === 'path') {
    return value.startsWith('/')
      ? null
      : 'Use the full path to the file, starting with /.';
  }
  const pattern = serverPattern(field) || FIELD_PATTERN[key] || null;
  if (pattern && !pattern.test(value))
    return FIELD_HINT[key] || "That doesn't look right.";
  return null;
}

// Address classes (§7.3).
function ipv4(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  return parts.every((n) => n <= 255) ? parts : null;
}

/** 'loopback' | 'private' | 'link_local' | 'unspecified' | 'public'. */
export function hostScope(hostname) {
  const host = String(hostname ?? '')
    .toLowerCase()
    .replace(/^\[|\]$/g, '');
  if (!host) return 'public';
  if (host === 'localhost' || host.endsWith('.localhost')) return 'loopback';
  const v4 = ipv4(host);
  if (v4) {
    const [a, b] = v4;
    if (a === 0 && v4.every((n) => n === 0)) return 'unspecified';
    if (a === 127) return 'loopback';
    if (a === 169 && b === 254) return 'link_local';
    if (a === 10) return 'private';
    if (a === 172 && b >= 16 && b <= 31) return 'private';
    if (a === 192 && b === 168) return 'private';
    return 'public';
  }
  if (host.includes(':')) {
    if (host === '::1') return 'loopback';
    if (host === '::') return 'unspecified';
    const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/.exec(host);
    if (mapped) return hostScope(mapped[1]);
    const first = parseInt(host.split(':')[0] || '0', 16);
    if ((first & 0xffc0) === 0xfe80) return 'link_local';
    if ((first & 0xfe00) === 0xfc00) return 'private';
    return 'public';
  }
  return 'public';
}

function defaultPort(protocol) {
  return protocol === 'https:' ? '443' : '80';
}

function isSelf(url, selfHost) {
  if (!selfHost) return false;
  let self;
  try {
    self = new URL(`http://${selfHost}`);
  } catch {
    return false;
  }
  const port = url.port || defaultPort(url.protocol);
  const selfPort = self.port || '80';
  if (port !== selfPort) return false;
  const a = url.hostname.toLowerCase();
  const b = self.hostname.toLowerCase();
  if (a === b) return true;
  return hostScope(a) === 'loopback' && hostScope(b) === 'loopback';
}

/**
 * Fill `{field}` placeholders from `fields`.
 * @returns {{url?:string, error?:string}}
 */
export function fillPlaceholders(raw, fields = {}, labels = {}) {
  let missing = null;
  const url = String(raw ?? '').replace(
    /\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
    (m, name) => {
      const value = String(fields?.[name] ?? '').trim();
      if (!value) {
        missing ||= name;
        return m;
      }
      return value;
    },
  );
  if (missing)
    return { error: `Fill in the ${labels[missing] || missing} first.` };
  if (/[{}]/.test(url))
    return { error: 'The URL still has a {placeholder} to fill in.' };
  return { url };
}

/**
 * Validate and normalize an endpoint URL (§7.3).
 * @returns {{ok:boolean, url:string|null, host:string|null,
 *   scope:string|null, error:string|null, needsInsecureAck:boolean}}
 */
export function validateBaseUrl(
  raw,
  { fields = {}, labels = {}, allowInsecureHttp = false, selfHost = null } = {},
) {
  const fail = (error, extra = {}) => ({
    ok: false,
    url: null,
    host: null,
    scope: null,
    error,
    needsInsecureAck: false,
    ...extra,
  });
  const text = String(raw ?? '').trim();
  if (!text) return fail('Enter the endpoint URL.');
  if (hasControl(text)) return fail("The URL can't contain line breaks.");
  const filled = fillPlaceholders(text, fields, labels);
  if (filled.error) return fail(filled.error);
  let url;
  try {
    url = new URL(filled.url);
  } catch {
    return fail("That isn't a complete URL. Start it with https://.");
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:')
    return fail('Use an https:// URL.');
  if (url.username || url.password)
    return fail('Remove the user name or password from the URL.');
  if (url.search || url.hash || /[?#]/.test(filled.url))
    return fail('Remove the ?query or #fragment from the URL.');
  const scope = hostScope(url.hostname);
  if (scope === 'unspecified')
    return fail("That address can't be used as an endpoint.");
  if (scope === 'link_local')
    return fail("Link-local addresses can't be used as an endpoint.");
  if (isSelf(url, selfHost))
    return fail("That's this app's own address, not a model provider.");
  const host = url.port ? `${url.hostname}:${url.port}` : url.hostname;
  if (url.protocol === 'http:') {
    if (scope === 'public')
      return fail('Use https:// for an endpoint on the internet.', { host });
    if (scope === 'private' && !allowInsecureHttp)
      return fail(`Tick "${COPY.lanAllow}" to use http:// on your network.`, {
        host,
        scope,
        needsInsecureAck: true,
      });
  }
  const path = url.pathname.replace(/\/+$/, '');
  // The analyst appends /v1/messages: a root ending in /v1 would double it.
  if (/\/v1$/i.test(path)) return fail(COPY.endpointV1, { host, scope });
  return {
    ok: true,
    url: `${url.protocol}//${url.host}${path}`,
    host,
    scope,
    error: null,
    needsInsecureAck: false,
  };
}

/** Where a validated endpoint's requests go ("host/path/v1/messages"), or null. */
export function endpointTarget(v) {
  if (!v?.ok || !v.url) return null;
  return `${v.url.replace(/^https?:\/\//i, '')}/v1/messages`;
}

// ---- provider helpers -------------------------------------------------------------

const CLOUD_KINDS = new Set(['bedrock', 'vertex', 'foundry']);

/** The catalog's group ids (llm_providers.GROUPS) → picker groups. */
const GROUP_ALIAS = Object.freeze({ claude: 'anthropic' });

/** Picker group for a catalog entry. */
export function groupOf(p) {
  if (p?.id === 'custom' || p?.group === 'custom') return 'custom';
  const group = GROUP_ALIAS[p?.group] || p?.group;
  if (GROUPS.some((g) => g.id === group)) return group;
  if (p?.kind === 'anthropic_login' || p?.kind === 'anthropic_key')
    return 'anthropic';
  if (CLOUD_KINDS.has(p?.kind)) return 'cloud';
  return 'compatible';
}

/** Providers grouped for the picker, headings in order, empty groups dropped. */
export function groupProviders(providers) {
  const list = Array.isArray(providers) ? providers.filter((p) => p?.id) : [];
  return GROUPS.map((g) => ({
    ...g,
    providers: list.filter((p) => groupOf(p) === g.id),
  })).filter((g) => g.providers.length);
}

export function rowNote(p) {
  return ROW_NOTE[p?.id] || p?.host || '';
}

/** Whether the row's note is a bare host (shown in mono). */
export function rowNoteIsHost(p) {
  return !ROW_NOTE[p?.id] && Boolean(p?.host);
}

export function familyTag(p) {
  return FAMILY_TAG[p?.model_family] || '';
}

export function statusWord(p) {
  return STATUS_WORD[p?.status] || '';
}

/** The provider's key label ("OpenRouter API key"). */
export function keyLabel(p) {
  const label = p?.auth?.key_label;
  return typeof label === 'string' && label
    ? label
    : `${p?.label || 'Provider'} API key`;
}

/** Last four characters of the masked key ("…9f2a" → "9f2a"). */
export function last4(p) {
  const masked = String(p?.key?.masked ?? '');
  const tail = masked.replace(/[^\x21-\x7E]/g, '').replace(/^[.*•]+/, '');
  return tail.slice(-4);
}

/** Address class of the endpoint the draft points at, or null. */
function draftScope(p, draft) {
  const mode = endpointMode(p);
  const raw =
    mode === 'editable' || mode === 'presets'
      ? draft?.base_url
      : p?.base_url?.value;
  if (!raw) return null;
  const v = validateBaseUrl(raw, {
    fields: draft?.fields,
    allowInsecureHttp: true,
  });
  return v.scope;
}

/**
 * How the key section reads for this provider and draft:
 * 'login' | 'environment' | 'saved' | 'keyless' | 'optional' | 'none_needed' | 'input'.
 * - keyless: a local server (loopback or LAN) that needs no key, or a custom
 *   endpoint with authentication None; an optional key can still be added.
 * - optional: a cloud kind that falls back to the cloud's own credentials.
 * - none_needed: Vertex, which has no key option at all.
 */
export function keyMode(p, draft = null) {
  const scheme = p?.auth?.scheme ?? null;
  const source = p?.key?.source ?? null;
  if (p?.kind === 'anthropic_login' || scheme === 'login') return 'login';
  if (source === 'environment') return 'environment';
  if (p?.key?.configured && (source === 'keychain' || source === 'file'))
    return 'saved';
  if (p?.kind === 'vertex') return 'none_needed';
  if (p?.id === 'custom' && (draft?.auth_scheme ?? scheme) === 'none')
    return 'keyless';
  const optional = p?.auth?.key_optional;
  if (optional) {
    if (CLOUD_KINDS.has(p?.kind)) return 'optional';
    const scope = draftScope(p, draft);
    // "loopback": optional only for an endpoint on this Mac (Ollama Cloud
    // needs a key); true: optional for any local or LAN server.
    if (optional === 'loopback')
      return scope === 'loopback' ? 'keyless' : 'input';
    return scope === 'loopback' || scope === 'private' ? 'keyless' : 'input';
  }
  return 'input';
}

/** Whether "Use" needs a key the provider doesn't have yet. */
export function needsKey(p, draft = null) {
  return keyMode(p, draft) === 'input';
}

/** Whether the non-Claude acknowledgement is required for this model (§7.2). */
export function needsAck(p, model, family = p?.model_family) {
  if (family === 'non_claude') return true;
  if (family === 'mixed') {
    const m = String(model ?? '').trim();
    // The host's own list of Claude prefixes wins (OpenRouter: anthropic/).
    const prefixes = Array.isArray(p?.claude_prefixes)
      ? p.claude_prefixes.filter((x) => typeof x === 'string' && x)
      : [];
    if (prefixes.length) return !prefixes.some((x) => m.startsWith(x));
    return !/^~?anthropic\//.test(m);
  }
  return false;
}

/** Whether the provider shows the data-egress line (every non-login provider). */
export function sendsDataOut(p) {
  return keyMode(p) !== 'login';
}

/** Whether a quick check exists, and whether it is a paid probe. */
export function quickCheck(p) {
  // GET sends `quick_check: true|false`; the catalog's own shape is
  // `{kind: models_list|key_info|messages, billable}`.
  const qc = p?.quick_check;
  const obj = qc && typeof qc === 'object' ? qc : null;
  const available = qc === true || Boolean(obj?.kind);
  const keyOnly = obj
    ? obj.kind === 'models_list' || obj.kind === 'key_info'
    : p?.id === 'anthropic_api' || p?.id === 'openrouter';
  const billable =
    typeof obj?.billable === 'boolean'
      ? obj.billable
      : typeof p?.quick_check_billable === 'boolean'
        ? p.quick_check_billable
        : available && !keyOnly;
  return { available, keyOnly, billable };
}

/** Thinking select value: 'auto' | 'off'. */
export function thinkingChoice(p, draft) {
  const v = draft?.thinking ?? p?.thinking ?? 'auto';
  return v === 'off' ? 'off' : 'auto';
}

/** The draft's thinking value for a select choice: Auto keeps the catalog's
 *  own mode ('auto' or 'adaptive'), Off is 'off'. */
export function thinkingValue(p, choice) {
  if (choice === 'off') return 'off';
  const own = p?.thinking;
  return own && own !== 'off' ? String(own) : 'auto';
}

/** Base-URL presets as `{label, url}`. */
export function presetsOf(p) {
  const raw = Array.isArray(p?.base_url?.presets) ? p.base_url.presets : [];
  return raw
    .map((x) =>
      typeof x === 'string'
        ? { label: x, url: x }
        : {
            label: String(x?.label ?? x?.name ?? x?.url ?? x?.value ?? ''),
            url: String(x?.url ?? x?.value ?? ''),
          },
    )
    .filter((x) => x.url);
}

/** Field descriptors with labels filled in. */
export function fieldsOf(p) {
  const raw = Array.isArray(p?.fields) ? p.fields : [];
  return raw
    .filter((f) => f && typeof f.id === 'string' && f.id)
    .map((f) => ({
      ...f,
      label:
        typeof f.label === 'string' && f.label
          ? f.label
          : FIELD_LABEL[fieldKey(p.id, f.id)] || f.id,
    }));
}

/** The editable, non-secret draft of a provider (never holds a key). */
export function draftFrom(p) {
  const values = p?.values && typeof p.values === 'object' ? p.values : {};
  const fields = {};
  for (const f of fieldsOf(p)) {
    const v = values[f.id] ?? f.default ?? '';
    fields[f.id] = v == null ? '' : String(v);
  }
  return {
    model: String(p?.model ?? p?.models?.default ?? ''),
    small_model: String(p?.small_model ?? ''),
    base_url: String(p?.base_url?.value ?? ''),
    fields,
    auth_scheme:
      p?.id === 'custom'
        ? String(p?.auth_scheme ?? p?.auth?.scheme ?? 'bearer')
        : null,
    thinking: p?.thinking === 'off' ? 'off' : String(p?.thinking ?? 'auto'),
    allow_insecure_http: Boolean(p?.allow_insecure_http),
    ack: Boolean(p?.ack_non_claude_at_ms || p?.acknowledged === true),
    // Only a custom endpoint may declare what it serves (§1.3).
    model_family:
      p?.id === 'custom'
        ? p?.model_family === 'claude'
          ? 'claude'
          : 'non_claude'
        : null,
  };
}

/** The model family a draft would run with (custom: as declared). */
export function familyOf(p, draft) {
  if (p?.id === 'custom' && draft?.model_family) return draft.model_family;
  return p?.model_family;
}

/** Whether the draft differs from what the host has saved. */
export function draftChanged(p, draft) {
  const base = draftFrom(p);
  const keys = [
    'model',
    'small_model',
    'base_url',
    'auth_scheme',
    'thinking',
    'allow_insecure_http',
    'model_family',
  ];
  if (keys.some((k) => String(base[k] ?? '') !== String(draft?.[k] ?? '')))
    return true;
  const ids = new Set([
    ...Object.keys(base.fields),
    ...Object.keys(draft?.fields || {}),
  ]);
  for (const id of ids)
    if ((base.fields[id] ?? '') !== (draft?.fields?.[id] ?? '')) return true;
  return false;
}

/** Whether the endpoint is editable, fixed, or picked from presets. */
export function endpointMode(p) {
  if (!p?.base_url) return 'none';
  if (p.base_url.editable) return 'editable';
  if (presetsOf(p).length) return 'presets';
  return 'fixed';
}

/**
 * Validate a draft (and a typed key) for this provider.
 * @returns {{ok:boolean, errors:object, endpoint:object|null}}
 */
export function validateDraft(
  p,
  draft,
  { keyText = '', selfHost = null } = {},
) {
  const errors = {};
  const m = validateModel(draft?.model);
  if (m) errors.model = m;
  const sm = validateModel(draft?.small_model, { required: false });
  if (sm) errors.small_model = sm;
  const fieldErrors = {};
  const labels = {};
  for (const f of fieldsOf(p)) {
    labels[f.id] = f.label;
    const e = validateField(p.id, f, draft?.fields?.[f.id]);
    if (e) fieldErrors[f.id] = e;
  }
  if (Object.keys(fieldErrors).length) errors.fields = fieldErrors;
  let endpoint = null;
  const mode = endpointMode(p);
  if (mode === 'editable' || mode === 'presets') {
    endpoint = validateBaseUrl(draft?.base_url, {
      fields: draft?.fields,
      labels,
      allowInsecureHttp: Boolean(draft?.allow_insecure_http),
      selfHost,
    });
    if (!endpoint.ok) errors.base_url = endpoint.error;
  }
  if (String(keyText ?? '').trim()) {
    const k = validateKey(keyText);
    if (k) errors.key = k;
  }
  return { ok: Object.keys(errors).length === 0, errors, endpoint };
}

/** The endpoint host a draft would send to, for the egress and help lines. */
export function draftHost(p, draft, selfHost = null) {
  const mode = endpointMode(p);
  if (mode === 'editable' || mode === 'presets') {
    const v = validateBaseUrl(draft?.base_url, {
      fields: draft?.fields,
      allowInsecureHttp: true,
      selfHost,
    });
    // What the operator typed decides; a URL still being typed goes nowhere
    // yet, so the saved host is never shown in its place.
    return v.host || null;
  }
  if (CLOUD_KINDS.has(p?.kind)) return cloudHost(p, draft?.fields);
  return p?.host || null;
}

/** A field value that passes its own check, or null. */
function goodField(p, id, fields) {
  const value = String(fields?.[id] ?? '').trim();
  if (!value) return null;
  const f = fieldsOf(p).find((x) => x.id === id) || { id };
  return validateField(p.id, f, value) ? null : value;
}

/** The host a cloud kind sends to, as the host derives it (llm_settings). */
export function cloudHost(p, fields = {}) {
  if (p?.kind === 'bedrock') {
    const region = goodField(p, 'region', fields);
    return region ? `bedrock-runtime.${region}.amazonaws.com` : null;
  }
  if (p?.kind === 'vertex') {
    const raw = String(fields?.region ?? '').trim();
    const region = raw ? goodField(p, 'region', fields) : 'global';
    if (!region) return null;
    return region === 'global'
      ? 'aiplatform.googleapis.com'
      : `${region}-aiplatform.googleapis.com`;
  }
  if (p?.kind === 'foundry') {
    const resource = goodField(p, 'resource', fields);
    return resource ? `${resource}.services.ai.azure.com` : null;
  }
  return null;
}

function stripOrNull(value) {
  const s = String(value ?? '').trim();
  return s ? s : null;
}

/** Non-secret part of a PUT or test body. */
function configBody(p, draft) {
  const mode = endpointMode(p);
  const body = {
    provider: p.id,
    model: String(draft?.model ?? '').trim(),
    small_model: stripOrNull(draft?.small_model),
    base_url:
      mode === 'editable' || mode === 'presets'
        ? stripOrNull(draft?.base_url)
        : null,
    fields: {},
    auth_scheme: p.id === 'custom' ? draft?.auth_scheme || 'bearer' : null,
    allow_insecure_http: Boolean(draft?.allow_insecure_http),
    thinking: draft?.thinking === 'off' ? 'off' : draft?.thinking || 'auto',
  };
  if (p.id === 'custom')
    body.model_family =
      draft?.model_family === 'claude' ? 'claude' : 'non_claude';
  for (const f of fieldsOf(p)) {
    const v = stripOrNull(draft?.fields?.[f.id]);
    if (v != null) body.fields[f.id] = v;
  }
  return body;
}

/**
 * PUT /settings/llm body. `keyText` (typed, not yet saved) is sent once as
 * `key.value` and never kept.
 */
export function putBody(
  p,
  draft,
  { keyText = '', activate = false, checkToken = null } = {},
) {
  const key = String(keyText ?? '').trim();
  const body = {
    ...configBody(p, draft),
    key: key ? { action: 'set', value: key } : { action: 'keep' },
    activate: Boolean(activate),
  };
  if (activate) {
    if (checkToken) body.check_token = checkToken;
    if (needsAck(p, body.model, familyOf(p, draft)))
      body.acknowledge_non_claude = Boolean(draft?.ack);
  }
  return body;
}

/** POST /settings/llm/test body. */
export function testBody(p, draft, { keyText = '', depth = 'quick' } = {}) {
  const body = { ...configBody(p, draft), depth };
  const key = String(keyText ?? '').trim();
  if (key) body.key = key;
  return body;
}

/**
 * Why "Use {label}" can't run yet, or null.
 * @param {object} ctx {settings, keyText, validation, busy, active}
 */
export function useBlocker(p, draft, ctx = {}) {
  const s = ctx.settings || {};
  if (ctx.busy) return COPY.blockBusy;
  if (s.read_only) return COPY.blockReadOnly;
  if (s.locks?.provider && s.locks.provider !== p?.id) return COPY.blockLocked;
  const typed = String(ctx.keyText ?? '').trim();
  // Already in use, checked and unchanged: there is nothing to put in use.
  // One whose saved config hasn't passed a check can run it through Use.
  if (
    s.active === p?.id &&
    p?.status === 'active' &&
    !needsCheck(p) &&
    !typed &&
    !draftChanged(p, draft)
  )
    return COPY.blockActive;
  if (needsKey(p, draft) && !typed) return COPY.blockKey;
  if (ctx.validation && !ctx.validation.ok) return COPY.blockInvalid;
  if (needsAck(p, draft?.model, familyOf(p, draft)) && !draft?.ack)
    return COPY.blockAck;
  return null;
}

/**
 * Whether Save must pass the full check first: an edit to the provider in
 * use (its config or its key) applies from the next message, so it gets the
 * same check as "Use" (the Claude login needs none).
 */
export function saveNeedsCheck(p, draft, { settings, keyText = '' } = {}) {
  if (!p || settings?.active !== p.id || p.kind === 'anthropic_login')
    return false;
  return Boolean(String(keyText ?? '').trim()) || draftChanged(p, draft);
}

/**
 * Whether the provider's saved config hasn't passed a check: the host says
 * so, or its last check was for a config that has since changed.
 */
export function needsCheck(p) {
  return p?.status === 'needs_check' || p?.tested?.current === false;
}

const IN_USE_WORDS = Object.freeze({
  connected: ['Connected', 'check'],
  failed: ["Couldn't connect", 'error'],
  no_key: ['No key', 'warning'],
  not_set_up: ['Not set up', 'warning'],
  needs_check: ['Needs a check', 'warning'],
});

/**
 * The In use summary.
 * @returns {null | {label, model, host,
 *   state:'connected'|'failed'|'no_key'|'not_set_up'|'needs_check', word, glyph}}
 */
export function inUseSummary(settings, availability = null) {
  const list = Array.isArray(settings?.providers) ? settings.providers : [];
  const p = list.find((x) => x?.id === settings?.active);
  if (!p) return null;
  const reason = availability?.reason ?? null;
  let state = 'connected';
  if (
    reason === 'provider_auth' ||
    (reason === 'auth' && p.kind === 'anthropic_login') ||
    (p.tested?.ok === false && p.tested?.current !== false)
  )
    state = 'failed';
  // A check can't help without a key: say what is missing instead.
  else if (reason === 'provider_key_missing') state = 'no_key';
  else if (reason === 'provider_not_configured') state = 'not_set_up';
  else if (
    needsCheck(p) ||
    (p.kind !== 'anthropic_login' && p.tested?.ok !== true)
  )
    state = 'needs_check';
  const words = IN_USE_WORDS[state];
  return {
    id: p.id,
    label: p.label || p.id,
    model: p.model || p.models?.default || '',
    host: draftHost(p, draftFrom(p)),
    state,
    word: words[0],
    glyph: words[1],
  };
}

// ---- connection-check results -------------------------------------------------------

/** Local words for a result code when the host sent no message (§5, §6.1). */
export function codeMessage(
  code,
  { label = 'The provider', host, model } = {},
) {
  const where = host || 'the endpoint';
  switch (code) {
    case 'auth':
      return `${label} rejected the key.`;
    case 'billing':
      return `${label} reports a billing problem.`;
    case 'model':
      return `${label} doesn't recognize the model ${model || ''}.`.replace(
        ' .',
        '.',
      );
    case 'rate_limit':
      return `${label} is rate-limiting this key.`;
    case 'server':
      return `${label} had a server error.`;
    case 'network':
      return `Couldn't reach ${where}.`;
    case 'redirect':
      return `${where} tried to redirect the request. The key wasn't sent on.`;
    case 'malformed':
      return `${where} didn't answer like an Anthropic Messages API.`;
    case 'endpoint':
      return `Nothing answered at ${where}/v1/messages. The URL should be the API root, without /v1.`;
    default:
      return `${label} returned an error.`;
  }
}

function seconds(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return null;
  // A local server can answer in a few ms: never "answered in 0.0 s".
  return Math.max(0.1, Math.round(n / 100) / 10).toFixed(1);
}

const IN_SETTINGS = /\s+in analyst settings(?=\.?$)/i;

/**
 * The host's hint, worded for the sheet: its chat-context hints say "in
 * analyst settings", which the operator is already in.
 * @returns {{hint:string|null, offerFull:boolean}}
 */
export function sheetHint(code, hint, depth = 'quick') {
  const h = typeof hint === 'string' ? hint.trim() : '';
  if (!h || /^open analyst settings\.?$/i.test(h))
    return { hint: null, offerFull: false };
  const contextual = /analyst settings/i.test(h);
  if (code === 'auth' && contextual)
    return { hint: 'Replace the key above.', offerFull: false };
  if (code === 'model' && contextual)
    return { hint: 'Pick another model above.', offerFull: false };
  if (/extended thinking/i.test(h) && contextual)
    return {
      hint: 'Turn off extended thinking for this model under Advanced.',
      offerFull: false,
    };
  if (code === 'invalid_request' && /full check/i.test(h))
    return depth === 'full'
      ? { hint: null, offerFull: false }
      : {
          hint: "Run a full check to see whether the analyst's engine works.",
          offerFull: true,
        };
  return { hint: h.replace(IN_SETTINGS, '') || null, offerFull: false };
}

/**
 * Words for a finished check.
 * @returns {{tone:'ok'|'critical'|'warn', text, status:number|null,
 *   hint:string|null, downgraded:boolean, offerFull:boolean}}
 */
export function resultCopy(p, result, { model, requestedThinking } = {}) {
  const label = p?.label || 'The provider';
  const depth = result?.depth === 'full' ? 'full' : 'quick';
  const status = Number.isFinite(result?.status) ? result.status : null;
  if (result?.ok) {
    const downgraded =
      depth === 'full' &&
      result.thinking === 'off' &&
      requestedThinking !== 'off';
    if (downgraded)
      return {
        tone: 'warn',
        text: `${COPY.fullOk(label)} ${COPY.downgraded(label)}`,
        status: null,
        hint: null,
        downgraded: true,
      };
    if (depth === 'full')
      return {
        tone: 'ok',
        text: COPY.fullOk(label),
        status: null,
        hint: null,
        downgraded: false,
      };
    const { keyOnly } = quickCheck(p);
    const s = seconds(result.latency_ms);
    const shown = String(model ?? p?.model ?? '').trim();
    return {
      tone: 'ok',
      text:
        keyOnly || s == null || !shown
          ? COPY.keyOk(label)
          : COPY.quickOk(shown, s),
      status: null,
      hint: null,
      downgraded: false,
    };
  }
  const message =
    typeof result?.message === 'string' && result.message.trim()
      ? result.message.trim()
      : codeMessage(result?.code, { label, host: result?.host, model });
  const { hint, offerFull } = sheetHint(result?.code, result?.hint, depth);
  return {
    tone: 'critical',
    text:
      depth === 'full' ? COPY.fullFailed(label, message) : COPY.failed(message),
    status,
    hint,
    downgraded: false,
    offerFull,
  };
}

// ---- settings-route errors -----------------------------------------------------------

/**
 * Words for a failed settings call (api.js typed errors; the host's
 * `{error, field, message, env}` bodies).
 * @returns {{code:string, text:string, field:string|null}}
 */
export function settingsErrorCopy(error) {
  const name = error?.name || '';
  const body = error?.body && typeof error.body === 'object' ? error.body : {};
  const code =
    typeof body.error === 'string'
      ? body.error
      : typeof error?.code === 'string'
        ? error.code
        : null;
  const field = typeof body.field === 'string' ? body.field : null;
  const said =
    typeof body.message === 'string' && body.message.trim()
      ? body.message.trim()
      : null;
  if (name === 'AuthError' || error?.status === 401)
    return { code: 'token', text: COPY.token, field: null };
  if (name === 'OfflineError')
    return { code: 'offline', text: COPY.offline, field: null };
  if (name === 'TimeoutError')
    return {
      code: 'timeout',
      text: 'The check took too long. The provider may be slow or unreachable.',
      field: null,
    };
  switch (code) {
    case 'invalid_settings':
      return {
        code,
        text: said || 'The host did not accept these settings.',
        field,
      };
    case 'settings_conflict':
      return {
        code,
        text: 'These settings changed in another window. They were reloaded; check them and try again.',
        field: null,
      };
    case 'locked_by_environment':
      return {
        code,
        text:
          typeof body.env === 'string' && body.env
            ? COPY.lockedModel(body.env)
            : 'That setting comes from the environment. To change it here, quit the app and unset it.',
        field,
      };
    case 'needs_check':
      return {
        code,
        text: 'The settings changed after the check. Run it again, then choose Use.',
        field: null,
      };
    case 'needs_ack':
      return {
        code,
        text: 'Tick the acknowledgement first: this model is not Claude.',
        field: null,
      };
    case 'cross_origin':
      return { code, text: COPY.crossOrigin, field: null };
    case 'test_busy': {
      // Not a failure: one check at a time, full checks 10 s apart.
      const wait = Number(body.retry_after_s);
      return {
        code,
        text: said || 'Another check is running. Try again in a moment.',
        field: null,
        retryAfterMs:
          Number.isFinite(wait) && wait >= 0 ? Math.round(wait * 1000) : null,
      };
    }
    case 'settings_unavailable':
      return { code, text: COPY.unavailable, field: null };
    case 'read_only':
      return { code, text: COPY.readOnly, field: null };
    case 'if_match_required':
      return {
        code,
        text: 'The settings were reloaded. Check them and try again.',
        field: null,
      };
    default:
      break;
  }
  if (error?.status === 404)
    return {
      code: 'settings_unavailable',
      text: COPY.unavailable,
      field: null,
    };
  return {
    code: code || 'error',
    text: said || error?.message || 'Something went wrong.',
    field,
  };
}

// ---- approval banner ------------------------------------------------------------------

/** The pinned approval line (same words as the inspector's). */
export function approvalLine(pending) {
  const n = Number(pending?.count) || 0;
  if (n <= 0) return '';
  const o = pending?.oldest || null;
  const what = o?.title ? [o.title, o.vehicle].filter(Boolean).join(', ') : '';
  if (n > 1) return COPY.approvalMany(n, what);
  return what ? COPY.approvalOne(what) : COPY.approvalBare;
}

// ---- analyst surfaces: header, divider, unavailable panel, cost ---------------------

/** `/chat/status.reason` values the settings sheet can fix (BYOK spec §8). */
export const PROVIDER_REASONS = Object.freeze([
  'provider_not_configured',
  'provider_key_missing',
  'provider_auth',
  'settings_error',
]);

export function isProviderReason(reason) {
  return PROVIDER_REASONS.includes(reason);
}

/** Opens the sheet from the unavailable panel and the spine popover. */
export const OPEN_SETTINGS = 'Open analyst settings';
/** Secondary action under the Claude-login sign-in failure. */
export const USE_API_KEY = 'Use an API key instead';

/** The analyst header's model line: "{model} via {label}", never a `·`. */
export function modelVia(model, provider) {
  const m = String(model ?? '').trim();
  const label = String(provider?.label ?? '').trim();
  if (m && label) return `${m} via ${label}`;
  return m || label;
}

/** The transcript divider for a `provider_changed` event (§10). */
export function providerChangedText(data) {
  const to = data?.to && typeof data.to === 'object' ? data.to : {};
  const label = String(to.label || to.id || 'another provider');
  const model = to.model ? ` (${to.model})` : '';
  const base = `The analyst now uses ${label}${model}.`;
  return data?.memory === 'cleared'
    ? `${base} It doesn't remember the conversation above.`
    : base;
}

/**
 * Unavailable copy for a provider reason, with the button that opens the
 * sheet, or null for any other reason.
 * @returns {null|{title, body, action, provider:string|null}}
 */
export function providerUnavailableCopy(reason, provider = null) {
  const label = typeof provider?.label === 'string' ? provider.label : '';
  const id = typeof provider?.id === 'string' ? provider.id : null;
  switch (reason) {
    case 'provider_not_configured':
      return {
        title: 'The analyst has no model provider set up.',
        body: 'Add a key for Claude or another provider.',
        action: OPEN_SETTINGS,
        provider: id,
      };
    case 'provider_key_missing':
      return {
        title: label
          ? `The analyst has no key for ${label}.`
          : 'The analyst has no key for its model provider.',
        body: 'Add a key, or choose another provider.',
        action: OPEN_SETTINGS,
        provider: id,
      };
    case 'provider_auth':
      return {
        title: label
          ? `${label} rejected the analyst's key.`
          : "The model provider rejected the analyst's key.",
        body: 'Replace the key, or choose another provider.',
        action: OPEN_SETTINGS,
        provider: id,
      };
    case 'settings_error':
      return {
        title: "The analyst's model settings couldn't be read.",
        body: 'Choose a provider again in analyst settings.',
        action: OPEN_SETTINGS,
        provider: null,
      };
    default:
      return null;
  }
}

/** The host's hint for a provider reason, unless it only repeats the button. */
export function providerHint(hint) {
  const h = typeof hint === 'string' ? hint.trim() : '';
  return /^open analyst settings\b/i.test(h) ? '' : h;
}

/** Whether usage dollars are invented for this provider (§8 `cost_basis`). */
export function costUnreliable(basis) {
  return basis === 'unreliable';
}

/** The usage footer's line when costs aren't shown (§10). */
export function costNote(provider) {
  const label = String(provider?.label ?? '').trim() || 'This provider';
  return `Cost isn't shown: ${label} bills you directly.`;
}

/** The model input's placeholder for free-text kinds. */
export function modelPlaceholder(p) {
  const v = p?.models?.placeholder;
  return typeof v === 'string' && v ? v : '';
}

/** Model suggestions, the current model first when it isn't listed. */
export function modelSuggestions(p) {
  const raw = Array.isArray(p?.models?.suggestions) ? p.models.suggestions : [];
  const out = [];
  for (const m of [p?.models?.default, ...raw]) {
    const v = typeof m === 'string' ? m.trim() : '';
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}
