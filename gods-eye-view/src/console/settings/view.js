/**
 * The analyst settings sheet (BYOK spec §10): pick the analyst's model
 * provider, store a key for it, test the connection and put it in use.
 *
 * `createSettingsSheet(host, ctx, opts?)` → { element, open({provider,
 *   invoker}?), close({restore}?), isOpen(), escape(), setLayout(layout),
 *   reload(), destroy() }
 *
 * - A sheet over the analyst column (same width, raised, layer 30); at
 *   narrow a full-screen sheet with Back. The shell mounts it and makes the
 *   column under it inert; the pending-approval banner stays pinned on top.
 * - Keyboard-complete: focus goes to the checked provider on open, Tab stays
 *   inside the sheet, Esc closes the innermost layer (the remove
 *   confirmation, then Replace key, then the sheet) and focus returns to the
 *   invoker. Arrow keys move through providers; selecting never activates.
 * - A key is write-only. The field is a password input that is cleared after
 *   every save and is never filled from anything but the operator's typing;
 *   the sheet shows only the host's last four characters. Nothing is written
 *   to localStorage, sessionStorage or the URL.
 *
 * ctx = { api, bus, chat, config } (index.js). opts (tests): { doc,
 *   client, clipboard }
 */

import { h, replaceKids, setHidden } from '../../ui/uavDom.js';
import { createSettingsClient } from './client.js';
import {
  COPY,
  approvalLine,
  draftFrom,
  draftHost,
  endpointMode,
  endpointTarget,
  familyOf,
  familyTag,
  fieldsOf,
  groupProviders,
  inUseSummary,
  keyLabel,
  keyMode,
  last4,
  modelPlaceholder,
  modelSuggestions,
  needsAck,
  presetsOf,
  needsCheck,
  putBody,
  quickCheck,
  resultCopy,
  rowNote,
  rowNoteIsHost,
  saveNeedsCheck,
  sendsDataOut,
  settingsErrorCopy,
  statusWord,
  testBody,
  thinkingChoice,
  thinkingValue,
  useBlocker,
  validateBaseUrl,
  validateDraft,
} from './model.js';

/** Glyph names reach the DOM only through this map (UX spec §11.2). */
export const ICON = Object.freeze({
  back: 'arrow_back',
  check: 'check',
  close: 'close',
  copy: 'content_copy',
  error: 'error',
  expand: 'expand_more',
  settings: 'settings',
  warning: 'warning',
});

/** The picker's status glyphs; the word always goes with them. */
const STATE_GLYPH = Object.freeze({
  connected: ICON.check,
  failed: ICON.error,
  no_key: ICON.warning,
  not_set_up: ICON.warning,
  needs_check: ICON.warning,
});

let sheetCounter = 0;

function iconEl(name, cls = '') {
  return h(
    'span',
    {
      class: `ic-icon material-symbols-outlined ${cls}`.trim(),
      'aria-hidden': 'true',
    },
    name,
  );
}

function button(label, { variant = 'quiet', icon, fk, aria, cls } = {}) {
  return h(
    'button',
    {
      type: 'button',
      class: `ic-btn ${cls || ''}`.trim(),
      'data-variant': variant,
      'data-fk': fk,
      'aria-label': aria,
    },
    icon ? iconEl(icon) : null,
    label ? h('span', { class: 'ic-btn__label' }, label) : null,
  );
}

function setText(el, value) {
  if (el) el.textContent = String(value ?? '');
}

function setAttr(el, name, value) {
  if (!el) return;
  if (value == null || value === false) el.removeAttribute?.(name);
  else el.setAttribute(name, value === true ? '' : String(value));
}

function isHiddenEl(el) {
  return Boolean(el?.getAttribute?.('hidden') != null);
}

function kidsOf(el) {
  return Array.isArray(el?.children) ? el.children : [...(el?.children || [])];
}

const FOCUSABLE_TAGS = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A']);

/** Tab stops under `root` in DOM order (skips hidden subtrees, disabled
 *  controls and tabindex=-1), on a real DOM or a stub tree. */
export function focusables(root) {
  const out = [];
  const walk = (el) => {
    if (!el || typeof el !== 'object' || isHiddenEl(el)) return;
    if (el.getAttribute?.('inert') != null) return;
    // A real DOM also hides by CSS (display:none renders no boxes).
    if (typeof el.getClientRects === 'function' && !el.getClientRects().length)
      return;
    const tag = String(el.tagName || '').toUpperCase();
    const tabindex = el.getAttribute?.('tabindex');
    const disabled =
      el.disabled === true || el.getAttribute?.('disabled') != null;
    if (
      !disabled &&
      tabindex !== '-1' &&
      (FOCUSABLE_TAGS.has(tag) || tabindex === '0')
    )
      out.push(el);
    for (const kid of kidsOf(el)) walk(kid);
  };
  walk(root);
  return out;
}

/** Whether `a` contains `b` (real DOM or stub tree). */
function contains(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  if (typeof a.contains === 'function') {
    try {
      if (a.contains(b)) return true;
    } catch {
      /* fall through to the walk */
    }
  }
  for (const kid of kidsOf(a)) if (contains(kid, b)) return true;
  return false;
}

/** The first element under `root` whose `data-fk` is `key`. */
function byFocusKey(root, key) {
  if (!root || typeof root !== 'object' || !key) return null;
  if (root.getAttribute?.('data-fk') === key) return root;
  for (const kid of kidsOf(root)) {
    const hit = byFocusKey(kid, key);
    if (hit) return hit;
  }
  return null;
}

function inputEl(attrs) {
  return h('input', {
    autocomplete: 'off',
    spellcheck: 'false',
    autocapitalize: 'off',
    ...attrs,
  });
}

function fieldRow(label, control, { id, help, error } = {}) {
  const errorEl = h('p', {
    class: 'ic-settings__error',
    id: `${id}-error`,
    hidden: true,
  });
  const helpEl = help
    ? h('p', { class: 'ic-settings__help', id: `${id}-help` }, help)
    : null;
  setAttr(control, 'id', id);
  setAttr(
    control,
    'aria-describedby',
    [helpEl ? `${id}-help` : null, `${id}-error`].filter(Boolean).join(' '),
  );
  const row = h(
    'div',
    { class: 'ic-settings__field' },
    h('label', { class: 'ic-settings__label', for: id }, label),
    control,
    helpEl,
    errorEl,
  );
  if (error) {
    setText(errorEl, error);
    setHidden(errorEl, false);
  }
  return { row, errorEl, helpEl };
}

/** The settings client, or null when this api can't reach the routes. */
function clientFor(api) {
  try {
    return api ? createSettingsClient(api) : null;
  } catch {
    return null;
  }
}

/**
 * Mount the settings sheet (hidden) into `host`.
 * @param {object} host the shell's sheet host
 * @param {object} ctx {api, bus, chat, config}
 * @param {object} [opts] {doc, client, clipboard, selfHost}
 */
export function createSettingsSheet(host, ctx = {}, opts = {}) {
  const doc = opts.doc ?? globalThis.document;
  const bus = ctx.bus ?? null;
  const client = opts.client ?? clientFor(ctx.api);
  const uid = (sheetCounter += 1);
  const titleId = `ic-settings-title-${uid}`;
  const selfHost =
    opts.selfHost ??
    (() => {
      try {
        return globalThis.location?.host || null;
      } catch {
        return null;
      }
    })();

  // ---- state (never holds a saved key; typed keys live only until sent) ----
  let open = false;
  let destroyed = false;
  let layout = 'wide';
  let invoker = null;
  let settings = null;
  let loadError = null;
  let loading = null;
  let selectedId = null;
  let pending = { count: 0, oldest: null };
  let availability = null;
  let busy = null; // 'test' | 'use' | 'save' | 'remove'
  let replacing = null; // provider id whose saved key is being replaced
  let confirmRemove = null; // provider id awaiting "Remove key"
  let advancedOpen = false;
  let showKey = false;
  let result = null; // {id, tone, text, status, hint, full?:boolean}
  let offerFull = null; // provider id with a passed quick check
  let attempted = false; // show field errors after the first Save/Test/Use
  // A passing full check that Use or Save can redeem instead of running the
  // engine again (the host allows one full check per 10 s): {id, sig, token,
  // downgraded, at}. `sig` never holds a key, only whether one was typed.
  let lastFull = null;
  let hostError = null; // the host's 422 for one field: {id, fk, text, sig}
  let keyEdits = 0; // bumps on every keystroke in a key field
  let waiting = false; // a check waits out the host's cooldown
  const drafts = new Map();
  const typed = new Map(); // provider id → a key the operator typed, unsent
  const offs = [];
  let d = {}; // the detail panel's live elements

  // ---- skeleton ----
  const approvalText = h('p', { class: 'ic-settings__approvaltext' });
  const reviewBtn = button(COPY.review, { variant: 'primary', fk: 'review' });
  const approvalEl = h(
    'div',
    { class: 'ic-settings__approval', role: 'status', hidden: true },
    approvalText,
    reviewBtn,
  );
  const backBtn = button(COPY.back, {
    variant: 'quiet',
    icon: ICON.back,
    fk: 'back',
    cls: 'ic-settings__back',
  });
  const closeBtn = button('', {
    variant: 'icon',
    icon: ICON.close,
    fk: 'close',
    aria: COPY.close,
    cls: 'ic-settings__close',
  });
  const headEl = h(
    'header',
    { class: 'ic-settings__head' },
    backBtn,
    iconEl(ICON.settings, 'ic-settings__glyph'),
    h('h2', { class: 'ic-settings__title', id: titleId }, COPY.title),
    closeBtn,
  );
  const noticeEl = h('div', {
    class: 'ic-settings__notice',
    role: 'status',
    hidden: true,
  });
  const inUseEl = h('section', {
    class: 'ic-settings__inuse',
    'aria-label': COPY.inUseHeading,
  });
  const pickerEl = h('div', {
    class: 'ic-settings__picker',
    role: 'radiogroup',
    'aria-labelledby': `${titleId}-provider`,
  });
  const pickerSection = h(
    'section',
    { class: 'ic-settings__section' },
    h(
      'h3',
      { class: 'ic-settings__heading', id: `${titleId}-provider` },
      COPY.providerHeading,
    ),
    pickerEl,
  );
  const detailEl = h('div', { class: 'ic-settings__detail' });
  const scrollEl = h(
    'div',
    { class: 'ic-settings__scroll' },
    noticeEl,
    inUseEl,
    pickerSection,
    detailEl,
  );
  const liveEl = h('div', { class: 'ic-vh', 'aria-live': 'polite' });
  const rootEl = h(
    'section',
    {
      class: 'ic-settings',
      role: 'dialog',
      'aria-labelledby': titleId,
      'aria-modal': 'false',
      'data-layout': layout,
      hidden: true,
    },
    approvalEl,
    headEl,
    scrollEl,
    liveEl,
  );
  host?.append?.(rootEl);

  function announce(text) {
    const t = String(text ?? '').trim();
    if (!t) return;
    // Re-announcing the same words needs a change a screen reader notices.
    setText(liveEl, liveEl.textContent === t ? `${t} ` : t);
  }

  function providers() {
    return Array.isArray(settings?.providers)
      ? settings.providers.filter((p) => p && p.id)
      : [];
  }

  function current() {
    return providers().find((p) => p.id === selectedId) || null;
  }

  function draftOf(p) {
    if (!p) return null;
    if (!drafts.has(p.id)) drafts.set(p.id, draftFrom(p));
    return drafts.get(p.id);
  }

  function keyTextOf(p) {
    return p ? typed.get(p.id) || '' : '';
  }

  function locked() {
    return Boolean(settings?.read_only) || loadError?.code === 'cross_origin';
  }

  // ---- notice, In use, approval banner ----

  function renderNotice() {
    let text = null;
    let tone = 'info';
    let retry = false;
    if (loading && !settings) text = COPY.loading;
    else if (loadError) {
      tone = 'critical';
      text = settings ? loadError.text : COPY.loadFailed(loadError.text);
      retry = !settings && !['cross_origin', 'token'].includes(loadError.code);
      if (['cross_origin', 'settings_unavailable'].includes(loadError.code))
        text = loadError.text;
    } else if (settings?.read_only) {
      tone = 'warn';
      text = COPY.readOnly;
    }
    setHidden(noticeEl, !text);
    noticeEl.setAttribute('data-tone', tone);
    const kids = text
      ? [h('p', { class: 'ic-settings__noticetext' }, text)]
      : [];
    if (retry) {
      const b = button(COPY.retry, { variant: 'quiet', fk: 'retry' });
      b.addEventListener('click', () => reload());
      kids.push(b);
    }
    replaceKids(noticeEl, kids);
  }

  function renderInUse() {
    const s = settings ? inUseSummary(settings, availability) : null;
    setHidden(inUseEl, !settings);
    if (!s) {
      replaceKids(inUseEl, [
        h('p', { class: 'ic-settings__inuseline' }, COPY.inUseNone),
      ]);
      return;
    }
    const line = h(
      'p',
      { class: 'ic-settings__inuseline' },
      h('span', { class: 'ic-settings__inuselabel' }, `${COPY.inUse} `),
      `${s.label}, `,
      h('span', { class: 'ic-mono' }, s.model),
    );
    const kids = [line];
    if (s.host)
      kids.push(
        h('p', { class: 'ic-settings__host ic-mono' }, COPY.requestsGo(s.host)),
      );
    kids.push(
      h(
        'p',
        { class: 'ic-settings__state', 'data-state': s.state },
        iconEl(STATE_GLYPH[s.state] || ICON.warning),
        h('span', {}, s.word),
      ),
    );
    replaceKids(inUseEl, kids);
  }

  function renderApproval() {
    const text = approvalLine(pending);
    setHidden(approvalEl, !text);
    setText(approvalText, text);
  }

  // ---- provider picker (radiogroup; selecting never activates) ----

  function rowFor(p) {
    const checked = p.id === selectedId;
    const status = statusWord(p);
    const tag = familyTag(p);
    const note = rowNote(p);
    const row = h(
      'button',
      {
        type: 'button',
        class: 'ic-settings__row',
        role: 'radio',
        'aria-checked': checked ? 'true' : 'false',
        tabindex: checked ? '0' : '-1',
        'data-provider': p.id,
        'data-status': p.status || 'not_configured',
        'data-fk': `row:${p.id}`,
      },
      h(
        'span',
        { class: 'ic-settings__rowmain' },
        h('span', { class: 'ic-settings__rowlabel' }, p.label || p.id),
        note
          ? h(
              'span',
              {
                class: rowNoteIsHost(p)
                  ? 'ic-settings__rownote ic-mono'
                  : 'ic-settings__rownote',
              },
              note,
            )
          : null,
      ),
      h(
        'span',
        { class: 'ic-settings__rowmeta' },
        tag ? h('span', { class: 'ic-settings__tag' }, tag) : null,
        status
          ? h(
              'span',
              {
                class: 'ic-settings__rowstatus',
                'data-status': p.status || '',
              },
              status,
            )
          : null,
      ),
    );
    row.addEventListener('click', () => select(p.id));
    row.addEventListener('keydown', (event) => onRowKey(event, p.id));
    return row;
  }

  function renderPicker() {
    const groups = groupProviders(providers());
    const kids = [];
    for (const g of groups) {
      const headId = `${titleId}-g-${g.id}`;
      kids.push(
        h(
          'div',
          {
            class: 'ic-settings__group',
            role: 'group',
            'aria-labelledby': headId,
          },
          h('p', { class: 'ic-settings__grouphead', id: headId }, g.label),
          ...g.providers.map(rowFor),
        ),
      );
    }
    replaceKids(pickerEl, kids);
  }

  function rows() {
    const out = [];
    const walk = (el) => {
      if (!el || typeof el !== 'object') return;
      if (el.getAttribute?.('role') === 'radio') out.push(el);
      for (const kid of kidsOf(el)) walk(kid);
    };
    walk(pickerEl);
    return out;
  }

  function syncRows() {
    for (const r of rows()) {
      const on = r.getAttribute('data-provider') === selectedId;
      r.setAttribute('aria-checked', on ? 'true' : 'false');
      r.setAttribute('tabindex', on ? '0' : '-1');
    }
  }

  function onRowKey(event, id) {
    const list = rows();
    const at = list.findIndex((r) => r.getAttribute('data-provider') === id);
    if (at < 0) return;
    const key = event?.key;
    let next = null;
    if (key === 'ArrowDown' || key === 'ArrowRight')
      next = (at + 1) % list.length;
    else if (key === 'ArrowUp' || key === 'ArrowLeft')
      next = (at - 1 + list.length) % list.length;
    else if (key === 'Home') next = 0;
    else if (key === 'End') next = list.length - 1;
    else if (key === 'Enter') {
      // Enter never activates a provider: it only selects, like a click.
      event.preventDefault?.();
      select(id);
      return;
    }
    if (next == null) return;
    event.preventDefault?.();
    const target = list[next];
    select(target.getAttribute('data-provider'));
    target.focus?.();
  }

  function select(id) {
    if (!id || id === selectedId) return;
    selectedId = id;
    replacing = null;
    confirmRemove = null;
    showKey = false;
    attempted = false;
    offerFull = null;
    if (result && result.id !== id) result = null;
    syncRows();
    renderDetail();
  }

  // ---- the selected provider ----

  const DETAIL_WORD = Object.freeze({
    testing: 'Testing…',
    failed: "Couldn't connect",
    active: 'In use',
    needs_check: 'In use, needs a check',
    not_configured: 'Not set up',
  });

  /** Busy states that run a connection check. */
  const RUNNING = new Set(['test', 'use', 'savecheck']);

  /**
   * 'testing' | 'failed' | 'active' | 'needs_check' | 'configured' |
   * 'not_configured'. The provider in use reads 'needs_check' when its
   * saved config hasn't passed a check (the same test as In use).
   */
  function detailState(p) {
    if (RUNNING.has(busy) && p?.id === selectedId) return 'testing';
    if (result?.id === p?.id && result.tone === 'critical') return 'failed';
    if (p?.status === 'active') return needsCheck(p) ? 'needs_check' : 'active';
    if (
      p?.key?.configured ||
      ['ready', 'key_saved', 'from_environment', 'needs_check'].includes(
        p?.status,
      )
    )
      return 'configured';
    return 'not_configured';
  }

  function detailWord(p, st) {
    if (st === 'configured') return statusWord(p) || 'Ready';
    return DETAIL_WORD[st] || '';
  }

  function sectionEl(heading, ...kids) {
    return h(
      'section',
      { class: 'ic-settings__section' },
      heading ? h('h3', { class: 'ic-settings__heading' }, heading) : null,
      ...kids,
    );
  }

  function keySection(p, draft) {
    const mode = keyMode(p, draft);
    d.keyMode = mode;
    d.keyInput = null;
    d.keyError = null;
    const label = keyLabel(p);
    const kids = [];
    if (mode === 'login') {
      kids.push(h('p', { class: 'ic-settings__text' }, COPY.login));
      return sectionEl(COPY.keyHeading, ...kids);
    }
    if (mode === 'environment') {
      const env = p.key?.env || 'a key variable';
      kids.push(
        h(
          'p',
          { class: 'ic-settings__text' },
          `${COPY.fromEnvA} `,
          h('span', { class: 'ic-mono' }, env),
          ` ${COPY.fromEnvB}`,
        ),
      );
      return sectionEl(COPY.keyHeading, ...kids);
    }
    if (mode === 'none_needed') {
      kids.push(h('p', { class: 'ic-settings__text' }, COPY.cloudKey.vertex));
      return sectionEl(COPY.keyHeading, ...kids);
    }
    if (mode === 'saved' && replacing !== p.id) {
      kids.push(
        h(
          'p',
          { class: 'ic-settings__text' },
          `${COPY.savedKey} `,
          h('span', { class: 'ic-mono', 'data-last4': 'true' }, last4(p)),
        ),
      );
      const store = p.key?.source;
      const path = settings?.key_store?.path;
      kids.push(
        store === 'keychain'
          ? h('p', { class: 'ic-settings__help' }, COPY.storedKeychain)
          : store === 'file' && path
            ? h(
                'p',
                { class: 'ic-settings__help' },
                `${COPY.storedFile} `,
                h('span', { class: 'ic-mono' }, path),
              )
            : h('p', { class: 'ic-settings__help' }, COPY.storedUnknown),
      );
      if (confirmRemove === p.id) {
        const keep = button(COPY.keepKey, { variant: 'quiet', fk: 'keep' });
        const remove = button(COPY.removeKey, {
          variant: 'danger',
          fk: 'confirm-remove',
        });
        keep.addEventListener('click', () => cancelRemove());
        remove.addEventListener('click', () => removeKey(p));
        kids.push(
          h(
            'div',
            { class: 'ic-settings__confirm', role: 'group' },
            h('p', { class: 'ic-settings__text' }, COPY.removeConfirm(p.label)),
            h('div', { class: 'ic-settings__buttons' }, keep, remove),
          ),
        );
      } else {
        const replace = button(COPY.replaceKey, { fk: 'replace' });
        const remove = button(COPY.removeKey, { fk: 'remove' });
        replace.addEventListener('click', () => startReplace(p));
        remove.addEventListener('click', () => askRemove(p));
        const locks = locked();
        setAttr(replace, 'aria-disabled', locks ? 'true' : null);
        setAttr(remove, 'aria-disabled', locks ? 'true' : null);
        kids.push(h('div', { class: 'ic-settings__buttons' }, replace, remove));
      }
      return sectionEl(COPY.keyHeading, ...kids);
    }
    // A field to paste into: nothing saved, a keyless local server, an
    // optional cloud key, or Replace key.
    if (mode === 'keyless')
      kids.push(h('p', { class: 'ic-settings__text' }, COPY.keyless));
    if (mode === 'optional' && COPY.cloudKey[p.kind])
      kids.push(h('p', { class: 'ic-settings__text' }, COPY.cloudKey[p.kind]));
    const optional = mode === 'keyless' || mode === 'optional';
    const input = inputEl({
      class: 'ic-settings__input ic-settings__key ic-mono',
      type: 'password',
      placeholder: COPY.keyPlaceholder,
      'data-fk': 'key',
    });
    input.value = '';
    setAttr(input, 'readonly', locked() ? true : null);
    input.addEventListener('input', () => {
      const v = String(input.value ?? '');
      if (v) typed.set(p.id, v);
      else typed.delete(p.id);
      keyEdits += 1;
      refresh();
    });
    // One name at a time ("Show the key" / "Hide the key"), no pressed
    // state: a toggle whose name flips would read "Hide the key, pressed".
    const toggle = button(COPY.show, {
      variant: 'quiet',
      fk: 'show',
      aria: COPY.showKey,
      cls: 'ic-settings__show',
    });
    const paint = () => {
      input.type = showKey ? 'text' : 'password';
      input.setAttribute('type', showKey ? 'text' : 'password');
      toggle.setAttribute('aria-label', showKey ? COPY.hideKey : COPY.showKey);
      setText(toggle.children?.[0] || toggle, showKey ? COPY.hide : COPY.show);
    };
    paint();
    toggle.addEventListener('click', () => {
      showKey = !showKey;
      paint();
    });
    const f = fieldRow(optional ? COPY.keyOptional(label) : label, input, {
      id: `${titleId}-key`,
    });
    // The label points at the input; the row holds input + Show. Built after
    // fieldRow, which appends (so moves) the input into its own row.
    const control = h('div', { class: 'ic-settings__keyrow' }, input, toggle);
    replaceKids(f.row, [kidsOf(f.row)[0], control, f.errorEl]);
    kids.push(f.row);
    d.keyInput = input;
    d.keyError = f.errorEl;
    if (replacing === p.id) {
      const cancel = button(COPY.cancel, { fk: 'cancel-replace' });
      cancel.addEventListener('click', () => cancelReplace());
      kids.push(h('div', { class: 'ic-settings__buttons' }, cancel));
    }
    return sectionEl(COPY.keyHeading, ...kids);
  }

  /** The flag or variable that fixes this provider's model, or null. */
  function lockedModelEnv(p) {
    const lock = settings?.locks?.model;
    const locked = p?.locked?.model === true || Boolean(lock);
    if (!locked) return null;
    if (p.kind !== 'anthropic_login' && p.kind !== 'anthropic_key') return null;
    const source = settings?.locks?.model_source;
    return typeof source === 'string' && source ? source : 'GODSEYE_CHAT_MODEL';
  }

  function textInput(p, draft, key, { fk, mono = true, list, placeholder }) {
    const input = inputEl({
      class: `ic-settings__input${mono ? ' ic-mono' : ''}`,
      type: 'text',
      list,
      placeholder: placeholder || null,
      'data-fk': fk,
    });
    input.value = String(draft[key] ?? '');
    input.addEventListener('input', () => {
      draft[key] = String(input.value ?? '');
      refresh();
    });
    if (locked()) setAttr(input, 'readonly', true);
    return input;
  }

  function modelSection(p, draft) {
    const listId = `${titleId}-models-${p.id}`;
    const suggestions = modelSuggestions(p);
    const datalist = h(
      'datalist',
      { id: listId },
      ...suggestions.map((m) => h('option', { value: m })),
    );
    const input = textInput(p, draft, 'model', {
      fk: 'model',
      list: suggestions.length ? listId : null,
      placeholder: modelPlaceholder(p),
    });
    const env = lockedModelEnv(p);
    if (env) setAttr(input, 'readonly', true);
    const f = fieldRow(COPY.modelLabel, input, {
      id: `${titleId}-model`,
      help: env ? COPY.lockedModel(env) : COPY.modelHelp(p.label),
    });
    d.modelError = f.errorEl;

    // Advanced: the background model, thinking, and (custom) authentication.
    const panelId = `${titleId}-advanced`;
    const toggle = button(COPY.advanced, {
      variant: 'quiet',
      icon: ICON.expand,
      fk: 'advanced',
      cls: 'ic-settings__disclosure',
    });
    setAttr(toggle, 'aria-expanded', advancedOpen ? 'true' : 'false');
    setAttr(toggle, 'aria-controls', panelId);
    const small = textInput(p, draft, 'small_model', {
      fk: 'small-model',
      list: suggestions.length ? listId : null,
    });
    const sf = fieldRow(COPY.smallModel, small, {
      id: `${titleId}-small`,
      help: COPY.smallHelp,
    });
    d.smallError = sf.errorEl;
    const thinking = h(
      'select',
      { class: 'ic-settings__select', 'data-fk': 'thinking' },
      h('option', { value: 'auto' }, COPY.thinkingAuto),
      h('option', { value: 'off' }, COPY.thinkingOff),
    );
    thinking.value = thinkingChoice(p, draft);
    thinking.addEventListener('change', () => {
      draft.thinking = thinkingValue(p, thinking.value);
      refresh();
    });
    if (locked()) setAttr(thinking, 'disabled', true);
    const tf = fieldRow(COPY.thinking, thinking, { id: `${titleId}-thinking` });
    const panelKids = [sf.row, tf.row];
    if (p.id === 'custom' || p.kind === 'custom') {
      const scheme = h(
        'select',
        { class: 'ic-settings__select', 'data-fk': 'scheme' },
        h('option', { value: 'bearer' }, COPY.schemeBearer),
        h('option', { value: 'x-api-key' }, COPY.schemeXApiKey),
        h('option', { value: 'none' }, COPY.schemeNone),
      );
      scheme.value = draft.auth_scheme || 'bearer';
      scheme.addEventListener('change', () => {
        draft.auth_scheme = String(scheme.value || 'bearer');
        // The key section reads differently with authentication None:
        // refresh() rebuilds just that section and keeps the typed key.
        refresh();
      });
      if (locked()) setAttr(scheme, 'disabled', true);
      panelKids.push(
        fieldRow(COPY.authScheme, scheme, { id: `${titleId}-scheme` }).row,
      );

      // What the endpoint serves: Claude models skip the acknowledgement.
      const serves = h(
        'select',
        { class: 'ic-settings__select', 'data-fk': 'family' },
        h('option', { value: 'non_claude' }, COPY.servesOther),
        h('option', { value: 'claude' }, COPY.servesClaude),
      );
      serves.value = draft.model_family === 'claude' ? 'claude' : 'non_claude';
      serves.addEventListener('change', () => {
        draft.model_family =
          serves.value === 'claude' ? 'claude' : 'non_claude';
        refresh();
      });
      if (locked()) setAttr(serves, 'disabled', true);
      panelKids.push(
        fieldRow(COPY.servesLabel, serves, { id: `${titleId}-family` }).row,
      );
    }
    const panel = h(
      'div',
      { class: 'ic-settings__advanced', id: panelId, hidden: !advancedOpen },
      ...panelKids,
    );
    toggle.addEventListener('click', () => {
      advancedOpen = !advancedOpen;
      toggle.setAttribute('aria-expanded', advancedOpen ? 'true' : 'false');
      setHidden(panel, !advancedOpen);
    });
    return sectionEl(COPY.modelHeading, f.row, datalist, toggle, panel);
  }

  function endpointSection(p, draft) {
    const mode = endpointMode(p);
    if (mode === 'none') return null;
    const kids = [];
    if (mode === 'fixed') {
      d.endpointHost = h('p', { class: 'ic-settings__host ic-mono' });
      kids.push(d.endpointHost);
      const env = p.base_url?.env;
      if (typeof env === 'string' && env)
        kids.push(
          h('p', { class: 'ic-settings__help' }, COPY.lockedModel(env)),
        );
      return sectionEl(COPY.endpointHeading, ...kids);
    }
    const presets = presetsOf(p);
    let urlInput = null;
    if (presets.length) {
      const select = h(
        'select',
        { class: 'ic-settings__select', 'data-fk': 'preset' },
        ...presets.map((x, i) => h('option', { value: String(i) }, x.label)),
        mode === 'editable'
          ? h('option', { value: 'custom' }, COPY.customUrl)
          : null,
      );
      const at = presets.findIndex((x) => x.url === draft.base_url);
      select.value =
        at >= 0 ? String(at) : mode === 'editable' ? 'custom' : '0';
      if (at < 0 && mode === 'presets') draft.base_url = presets[0].url;
      select.addEventListener('change', () => {
        const x = presets[Number(select.value)];
        if (x) {
          draft.base_url = x.url;
          if (urlInput) urlInput.value = x.url;
        }
        refresh();
      });
      if (locked()) setAttr(select, 'disabled', true);
      kids.push(
        fieldRow(COPY.endpointPreset, select, { id: `${titleId}-preset` }).row,
      );
    }
    if (mode === 'editable') {
      urlInput = textInput(p, draft, 'base_url', {
        fk: 'base-url',
        placeholder: 'https://',
      });
      setAttr(urlInput, 'inputmode', 'url');
      const f = fieldRow(COPY.endpointLabel, urlInput, {
        id: `${titleId}-url`,
        help: COPY.endpointHelpEmpty,
      });
      d.urlError = f.errorEl;
      d.endpointHelp = f.helpEl;
      kids.push(f.row);
    }
    d.scopeNote = h('p', { class: 'ic-settings__help', hidden: true });
    kids.push(d.scopeNote);
    const lan = h('input', { type: 'checkbox', 'data-fk': 'lan' });
    lan.checked = Boolean(draft.allow_insecure_http);
    lan.addEventListener('change', () => {
      draft.allow_insecure_http = Boolean(lan.checked);
      refresh();
    });
    d.lanBox = h(
      'div',
      { class: 'ic-settings__lan', hidden: true },
      h(
        'p',
        { class: 'ic-settings__warn' },
        iconEl(ICON.warning),
        COPY.lanWarning,
      ),
      h(
        'label',
        { class: 'ic-settings__check' },
        lan,
        h('span', {}, COPY.lanAllow),
      ),
    );
    kids.push(d.lanBox);
    if (mode === 'presets') {
      d.endpointHost = h('p', { class: 'ic-settings__host ic-mono' });
      kids.push(d.endpointHost);
    }
    return sectionEl(COPY.endpointHeading, ...kids);
  }

  function fieldsSection(p, draft) {
    const list = fieldsOf(p);
    if (!list.length) return null;
    d.fieldErrors = {};
    const rowsOut = list.map((f) => {
      if (draft.fields[f.id] == null) draft.fields[f.id] = '';
      const input = inputEl({
        class: 'ic-settings__input ic-mono',
        type: 'text',
        placeholder: f.placeholder || null,
        'data-fk': `field:${f.id}`,
      });
      input.value = String(draft.fields[f.id] ?? '');
      input.addEventListener('input', () => {
        draft.fields[f.id] = String(input.value ?? '');
        refresh();
      });
      if (locked()) setAttr(input, 'readonly', true);
      const label = f.required ? f.label : `${f.label} (optional)`;
      const row = fieldRow(label, input, { id: `${titleId}-f-${f.id}` });
      d.fieldErrors[f.id] = row.errorEl;
      return row.row;
    });
    return sectionEl(COPY.fieldsHeading, ...rowsOut);
  }

  function notesSection(p) {
    const notes = (Array.isArray(p.notes) ? p.notes : []).filter(
      (n) => typeof n === 'string' && n.trim(),
    );
    const docs = typeof p.docs_url === 'string' ? p.docs_url : '';
    if (!notes.length && !docs) return null;
    const kids = notes.map((n) => h('p', { class: 'ic-settings__note' }, n));
    if (docs) {
      const copyBtn = button(COPY.copy, {
        variant: 'quiet',
        icon: ICON.copy,
        fk: 'copy-docs',
        aria: COPY.copyDocs,
      });
      copyBtn.addEventListener('click', () => copyDocs(docs));
      kids.push(
        h(
          'div',
          { class: 'ic-settings__docs' },
          h('span', { class: 'ic-settings__docslabel' }, COPY.docs),
          h('span', { class: 'ic-settings__docsurl ic-mono' }, docs),
          copyBtn,
        ),
      );
    }
    return sectionEl(COPY.notesHeading, ...kids);
  }

  function safetySection(p, draft) {
    if (!sendsDataOut(p) && !needsAck(p, draft.model, familyOf(p, draft)))
      return null;
    const ack = h('input', { type: 'checkbox', 'data-fk': 'ack' });
    ack.checked = Boolean(draft.ack);
    ack.addEventListener('change', () => {
      draft.ack = Boolean(ack.checked);
      refresh();
    });
    if (locked()) setAttr(ack, 'disabled', true);
    d.ackBox = h(
      'div',
      { class: 'ic-settings__caveat', hidden: true },
      h('p', { class: 'ic-settings__caveattitle' }, COPY.safetyTitle),
      h('p', { class: 'ic-settings__text' }, COPY.safetyBody),
      h('label', { class: 'ic-settings__check' }, ack, h('span', {}, COPY.ack)),
    );
    d.egress = h('p', { class: 'ic-settings__text', hidden: true });
    return sectionEl(COPY.safetyHeading, d.ackBox, d.egress);
  }

  function actionsSection(p) {
    const test = button(COPY.test, { variant: 'quiet', fk: 'test' });
    const save = button(COPY.save, { variant: 'quiet', fk: 'save' });
    const use = button(COPY.use(p.label), { variant: 'primary', fk: 'use' });
    const whyId = `${titleId}-why`;
    const why = h('p', { class: 'ic-settings__why', id: whyId, hidden: true });
    setAttr(use, 'aria-describedby', whyId);
    const testNote = h('p', { class: 'ic-settings__help', hidden: true });
    const full = button(COPY.fullCheck, {
      variant: 'link',
      fk: 'full',
      cls: 'ic-settings__full',
    });
    setHidden(full, true);
    const resultText = h('p', { class: 'ic-settings__resulttext' });
    const resultCode = h('span', {
      class: 'ic-settings__code ic-mono',
      hidden: true,
    });
    const resultHint = h('p', { class: 'ic-settings__help', hidden: true });
    // Not a live region: announce() speaks each result once through the
    // sheet's own live region, which also survives a rebuilt panel.
    const resultEl = h(
      'div',
      { class: 'ic-settings__result' },
      resultText,
      resultCode,
      resultHint,
      full,
    );
    test.addEventListener('click', () => runTest(p));
    save.addEventListener('click', () => saveDraft(p));
    use.addEventListener('click', () => useProvider(p));
    full.addEventListener('click', () => runTest(p, 'full'));
    Object.assign(d, {
      test,
      save,
      use,
      why,
      testNote,
      full,
      resultEl,
      resultText,
      resultCode,
      resultHint,
    });
    // Only the button row is sticky; what the buttons say sits just above
    // it and scrolls with the form.
    d.outcome = h(
      'div',
      { class: 'ic-settings__outcome' },
      why,
      testNote,
      resultEl,
    );
    d.actions = h(
      'div',
      { class: 'ic-settings__actions' },
      h('div', { class: 'ic-settings__buttons' }, test, save, use),
    );
    return [d.outcome, d.actions];
  }

  /**
   * Build the selected provider's panel. A typed key survives a rebuild only
   * with `keepKey` (a failed write, a re-read of the settings) and only for
   * the provider already on screen; otherwise the field starts empty.
   */
  function renderDetail({ keepFocus = null, keepKey = false } = {}) {
    const p = current();
    const active = doc?.activeElement;
    const focusKey =
      keepFocus ||
      (contains(detailEl, active) ? active?.getAttribute?.('data-fk') : null);
    const same = p && detailEl.getAttribute?.('data-provider') === p.id;
    const carry = keepKey && same ? typed.get(p.id) || null : null;
    // The old key field goes blank before it is dropped: a typed key never
    // outlives the field that shows it.
    if (d.keyInput) d.keyInput.value = '';
    typed.clear();
    if (!carry) showKey = false;
    d = {};
    if (!p) {
      replaceKids(detailEl, []);
      return;
    }
    const draft = draftOf(p);
    const tag = familyTag(p);
    d.state = h('p', { class: 'ic-settings__state' });
    const head = h(
      'div',
      { class: 'ic-settings__detailhead' },
      h('h3', { class: 'ic-settings__detailtitle' }, p.label || p.id),
      tag ? h('span', { class: 'ic-settings__tag' }, tag) : null,
      d.state,
    );
    d.keyHost = h('div', { class: 'ic-settings__keyhost' });
    const parts = [
      head,
      d.keyHost,
      modelSection(p, draft),
      endpointSection(p, draft),
      fieldsSection(p, draft),
      notesSection(p),
      safetySection(p, draft),
      ...actionsSection(p),
    ].filter(Boolean);
    placeKey(p, draft, carry);
    replaceKids(detailEl, parts);
    detailEl.setAttribute('data-provider', p.id);
    refresh();
    if (focusKey) byFocusKey(detailEl, focusKey)?.focus?.();
  }

  /**
   * Fill the key section for the draft's key mode. `carry` is a key the
   * operator typed for this provider; it goes back into the new field, or is
   * dropped when the new section has none.
   */
  function placeKey(p, draft, carry = null) {
    if (!d.keyHost) return;
    // The field being replaced goes blank first (it may still be referenced).
    if (d.keyInput) d.keyInput.value = '';
    replaceKids(d.keyHost, [keySection(p, draft)]);
    if (carry && d.keyInput) {
      d.keyInput.value = carry;
      typed.set(p.id, carry);
    } else typed.delete(p.id);
  }

  // ---- live updates (validation, blockers, notes, results) ----

  function validation(p, draft) {
    return validateDraft(p, draft, { keyText: keyTextOf(p), selfHost });
  }

  function showError(el, message, input) {
    if (!el) return;
    setText(el, message || '');
    setHidden(el, !message);
    if (input) setAttr(input, 'aria-invalid', message ? 'true' : null);
  }

  function scopeOf(p, draft) {
    const mode = endpointMode(p);
    if (mode !== 'editable' && mode !== 'presets') return null;
    return validateBaseUrl(draft.base_url, {
      fields: draft.fields,
      allowInsecureHttp: true,
      selfHost,
    });
  }

  /** What a check ran on, without the key: only whether one was typed. */
  function checkSig(p, draft) {
    const typedKey = keyTextOf(p) ? 'typed' : 'saved';
    return `${JSON.stringify(testBody(p, draft, { depth: 'full' }))}|${typedKey}|${keyEdits}`;
  }

  function refresh() {
    const p = current();
    if (!p || !d.use) return;
    const draft = draftOf(p);
    // The key section follows the endpoint and authentication (Ollama Cloud
    // needs a key, a local server doesn't); a typed key stays in its field.
    if (d.keyHost && keyMode(p, draft) !== d.keyMode)
      placeKey(p, draft, typed.get(p.id) || null);
    if (
      hostError &&
      (hostError.id !== p.id || hostError.sig !== checkSig(p, draft))
    )
      hostError = null;
    const v = validation(p, draft);
    const e = attempted ? v.errors : {};
    const fk = (key) => byFocusKey(detailEl, key);
    // The host's 422 marks its field until the operator edits something.
    const withHost = (key, message) =>
      message || (hostError?.fk === key ? hostError.text : null);
    showError(d.keyError, withHost('key', e.key), d.keyInput);
    showError(d.modelError, withHost('model', e.model), fk('model'));
    showError(
      d.smallError,
      withHost('small-model', e.small_model),
      fk('small-model'),
    );
    showError(d.urlError, withHost('base-url', e.base_url), fk('base-url'));
    for (const [id, el] of Object.entries(d.fieldErrors || {}))
      showError(el, withHost(`field:${id}`, e.fields?.[id]), fk(`field:${id}`));

    // Where requests go: the endpoint help, the local and LAN notes.
    const host = draftHost(p, draft, selfHost);
    const target = endpointTarget(scopeOf(p, draft));
    if (d.endpointHelp)
      setText(
        d.endpointHelp,
        target ? COPY.endpointHelp(target) : COPY.endpointHelpEmpty,
      );
    if (d.endpointHost) {
      setText(d.endpointHost, host ? COPY.requestsGo(host) : '');
      setHidden(d.endpointHost, !host);
    }
    const scope = scopeOf(p, draft);
    const http = /^http:/i.test(String(draft.base_url ?? '').trim());
    if (d.scopeNote) {
      const local = scope?.scope === 'loopback';
      setText(d.scopeNote, local ? COPY.loopback : '');
      setHidden(d.scopeNote, !local);
    }
    if (d.lanBox) setHidden(d.lanBox, !(http && scope?.scope === 'private'));
    if (d.egress) {
      const out = sendsDataOut(p);
      // Always a host (§9.7): until one is known, the endpoint to be entered.
      setText(
        d.egress,
        out ? (host ? COPY.egress(host) : COPY.egressPending) : '',
      );
      setHidden(d.egress, !out);
    }
    if (d.ackBox)
      setHidden(d.ackBox, !needsAck(p, draft.model, familyOf(p, draft)));

    // What each action is waiting for.
    const typedKey = keyTextOf(p);
    const blocker = useBlocker(p, draft, {
      settings,
      keyText: typedKey,
      validation: attempted ? v : null,
      busy: Boolean(busy),
    });
    const inUse = blocker === COPY.blockActive;
    setText(
      d.use.children?.[0] || d.use,
      inUse ? COPY.inUseButton : COPY.use(p.label),
    );
    setAttr(d.use, 'aria-disabled', blocker ? 'true' : 'false');
    // The same words already in the result line are not repeated.
    const said = result?.id === p.id ? result.text : '';
    const why =
      blocker && !inUse && blocker !== COPY.blockBusy && blocker !== said
        ? blocker
        : '';
    setText(d.why, why);
    setHidden(d.why, !why);
    const keyMissing =
      keyMode(p, draft) === 'input' &&
      !typedKey &&
      p.kind !== 'anthropic_login';
    setAttr(
      d.test,
      'aria-disabled',
      busy || locked() || keyMissing ? 'true' : 'false',
    );
    setAttr(d.save, 'aria-disabled', busy || locked() ? 'true' : 'false');
    // An edit to the provider in use runs the full check before it saves.
    setText(
      d.save.children?.[0] || d.save,
      saveNeedsCheck(p, draft, { settings, keyText: typedKey })
        ? COPY.saveAndCheck
        : COPY.save,
    );
    const qc = quickCheck(p);
    const note =
      p.kind === 'anthropic_login'
        ? ''
        : !qc.available
          ? COPY.fullOnly
          : qc.billable && host
            ? COPY.paidProbe(host)
            : '';
    setText(d.testNote, note);
    setHidden(d.testNote, !note);
    if (p.kind === 'anthropic_login') setHidden(d.test, true);

    // The check: running, or its result for this provider.
    const running = RUNNING.has(busy);
    const r = result?.id === p.id ? result : null;
    let text = '';
    let tone = 'info';
    if (running)
      text = waiting
        ? COPY.busyWait
        : busy === 'test'
          ? COPY.testing(p.label)
          : COPY.checking;
    else if (r) {
      text = r.text;
      tone = r.tone || 'info';
    }
    setText(d.resultText, text);
    d.resultEl.setAttribute('data-tone', tone);
    setHidden(d.resultEl, !text);
    const code =
      !running && Number.isFinite(r?.status) ? COPY.http(r.status) : '';
    setText(d.resultCode, code);
    setHidden(d.resultCode, !code);
    const hint = !running && r?.hint ? r.hint : '';
    setText(d.resultHint, hint);
    setHidden(d.resultHint, !hint);
    setHidden(d.full, !(offerFull === p.id && !busy));

    const st = detailState(p);
    detailEl.setAttribute('data-state', st);
    const glyph =
      st === 'failed'
        ? ICON.error
        : st === 'active'
          ? ICON.check
          : st === 'needs_check' ||
              (p.status === 'needs_check' && st === 'configured')
            ? ICON.warning
            : null;
    replaceKids(
      d.state,
      [glyph ? iconEl(glyph) : null, h('span', {}, detailWord(p, st))].filter(
        Boolean,
      ),
    );
    d.state.setAttribute('data-state', st);
  }

  // ---- actions ----

  const INVALID_ORDER = [
    ['key', 'key'],
    ['model', 'model'],
    ['small_model', 'small-model'],
    ['base_url', 'base-url'],
  ];

  function focusInvalid(v) {
    for (const [key, fk] of INVALID_ORDER) {
      if (!v.errors[key]) continue;
      if (key === 'small_model' && !advancedOpen)
        byFocusKey(detailEl, 'advanced')?.click?.();
      byFocusKey(detailEl, fk)?.focus?.();
      return;
    }
    const id = Object.keys(v.errors.fields || {})[0];
    if (id) byFocusKey(detailEl, `field:${id}`)?.focus?.();
  }

  function note(p, tone, text, extra = {}) {
    result = { id: p.id, tone, text, status: null, hint: null, ...extra };
  }

  /** Validate before a round trip; shows the errors and focuses the first. */
  function guard(p, draft) {
    attempted = true;
    const v = validation(p, draft);
    refresh();
    if (v.ok) return true;
    note(p, 'warn', COPY.blockInvalid);
    refresh();
    reveal();
    announce(COPY.blockInvalid);
    focusInvalid(v);
    return false;
  }

  /** The host's 422 `field` → the input's focus key. */
  const HOST_FIELD = Object.freeze({
    model: 'model',
    small_model: 'small-model',
    base_url: 'base-url',
    key: 'key',
  });

  function hostFieldKey(field) {
    if (typeof field !== 'string' || !field) return null;
    if (field.startsWith('fields.')) return `field:${field.slice(7)}`;
    return HOST_FIELD[field] || null;
  }

  /** A busy host (one check at a time, full checks 10 s apart) is a wait,
   *  never a failed connection. */
  function busyNote(p, c) {
    note(p, 'warn', c.text);
  }

  function writeFailed(p, error) {
    const c = settingsErrorCopy(error);
    const fk = c.code === 'invalid_settings' ? hostFieldKey(c.field) : null;
    if (fk && byFocusKey(detailEl, fk)) {
      // The host's words go on the field it names; the result says where.
      attempted = true;
      hostError = { id: p.id, fk, text: c.text, sig: checkSig(p, draftOf(p)) };
      if (fk === 'small-model' && !advancedOpen)
        byFocusKey(detailEl, 'advanced')?.click?.();
      note(p, 'warn', COPY.blockInvalid);
    } else if (c.code === 'test_busy') busyNote(p, c);
    else note(p, 'critical', c.text);
    if (c.code === 'cross_origin' || c.code === 'token') loadError = c;
    if (c.code === 'settings_conflict') reload();
    return c;
  }

  /** Bring what the buttons said into view above the sticky button row. */
  function reveal() {
    try {
      // Clear of the sticky row, however many lines its buttons wrap to.
      const foot = d.actions?.getBoundingClientRect?.().height;
      if (foot && d.outcome?.style)
        d.outcome.style.scrollMarginBottom = `${Math.ceil(foot) + 12}px`;
      d.outcome?.scrollIntoView?.({ block: 'nearest' });
    } catch {
      /* a stub DOM has no layout */
    }
  }

  const AUTO_WAIT_MS = 12000;

  /**
   * POST /settings/llm/test. A full check within the host's cooldown comes
   * back 429 `test_busy` with `retry_after_s`: wait it out once, saying so.
   */
  async function testWithWait(body) {
    try {
      return await client.test(body);
    } catch (error) {
      const c = settingsErrorCopy(error);
      const ms = c.retryAfterMs;
      if (c.code !== 'test_busy' || ms == null || ms > AUTO_WAIT_MS)
        throw error;
      waiting = true;
      refresh();
      announce(COPY.busyWait);
      try {
        await new Promise((r) => setTimeout(r, ms + 200));
      } finally {
        waiting = false;
      }
      if (destroyed || !open) throw error;
      refresh();
      return client.test(body);
    }
  }

  const REUSE_MS = 9 * 60 * 1000; // the host's check token lasts 10 min

  /** A passing full check of exactly this config, still redeemable. */
  function reusableFull(p, draft) {
    const f = lastFull;
    if (!f || f.id !== p.id || Date.now() - f.at > REUSE_MS) return null;
    return f.sig === checkSig(p, draft) ? f : null;
  }

  /**
   * The analyst's own engine on this draft: a fresh passing full check, or
   * the one that just passed.
   * @returns {Promise<{ok:true, token, downgraded} | {ok:false, copy}>}
   */
  async function engineCheck(p, draft, keyText) {
    const reuse = reusableFull(p, draft);
    if (reuse)
      return { ok: true, token: reuse.token, downgraded: reuse.downgraded };
    const sig = checkSig(p, draft);
    const r = await testWithWait(
      testBody(p, draft, { keyText, depth: 'full' }),
    );
    const copy = resultCopy(p, r, {
      model: draft.model,
      requestedThinking: draft.thinking,
    });
    if (!r?.ok) return { ok: false, copy };
    const token = typeof r.check_token === 'string' ? r.check_token : null;
    remember(p, sig, token, copy.downgraded);
    return { ok: true, token, downgraded: copy.downgraded };
  }

  function remember(p, sig, token, downgraded) {
    lastFull = token
      ? { id: p.id, sig, token, downgraded, at: Date.now() }
      : null;
  }

  /**
   * After a write. Success rebuilds the panel (the saved key shows only its
   * last four); a failure keeps it as it is, typed key included.
   */
  function finishWrite(ok, focusKey) {
    if (destroyed) return;
    if (ok) {
      renderAll({ keepFocus: focusKey });
    } else {
      renderNotice();
      renderInUse();
      refresh();
      if (hostError) byFocusKey(detailEl, hostError.fk)?.focus?.();
    }
    reveal();
    announce(result?.text);
  }

  async function runTest(p, depth = null) {
    if (busy || locked() || !client) return;
    const draft = draftOf(p);
    if (keyMode(p, draft) === 'input' && !keyTextOf(p)) {
      note(p, 'warn', COPY.blockKey);
      refresh();
      announce(COPY.blockKey);
      d.keyInput?.focus?.();
      return;
    }
    if (!guard(p, draft)) return;
    const qc = quickCheck(p);
    const dep = depth || (qc.available ? 'quick' : 'full');
    // "Run a full check" hides while it runs: keep keyboard focus in place.
    const fromFull = doc?.activeElement === d.full;
    if (fromFull) d.test?.focus?.();
    busy = 'test';
    result = null;
    offerFull = null;
    refresh();
    announce(dep === 'full' ? COPY.checking : COPY.testing(p.label));
    const sig = checkSig(p, draft);
    let passed = false;
    try {
      const r = await testWithWait(
        testBody(p, draft, { keyText: keyTextOf(p), depth: dep }),
      );
      const copy = resultCopy(p, r, {
        model: draft.model,
        requestedThinking: draft.thinking,
      });
      result = { id: p.id, ...copy };
      passed = Boolean(r?.ok);
      if ((r?.ok && dep === 'quick') || copy.offerFull) offerFull = p.id;
      // A passing full check is what Use (or Save and check) redeems next.
      if (r?.ok && dep === 'full')
        remember(p, sig, r.check_token || null, copy.downgraded);
    } catch (error) {
      const c = settingsErrorCopy(error);
      if (c.code === 'test_busy') busyNote(p, c);
      else note(p, 'critical', COPY.failed(c.text));
      if (c.code === 'cross_origin' || c.code === 'token') loadError = c;
    } finally {
      busy = null;
    }
    if (destroyed) return;
    renderNotice();
    refresh();
    if (fromFull) {
      const at = doc?.activeElement;
      if (at === d.test || !contains(rootEl, at))
        (passed ? d.use : d.test)?.focus?.();
    }
    reveal();
    announce(result?.text);
  }

  /** Why Use (or Save and check) can't run yet: shown, announced, focused. */
  function blocked(p, blocker, v) {
    attempted = true;
    note(p, 'warn', blocker);
    refresh();
    reveal();
    announce(blocker);
    if (blocker === COPY.blockInvalid) focusInvalid(v);
    else if (blocker === COPY.blockKey) d.keyInput?.focus?.();
    else if (blocker === COPY.blockAck) byFocusKey(detailEl, 'ack')?.focus?.();
  }

  async function useProvider(p) {
    if (busy || !client) return;
    const draft = draftOf(p);
    const v = validation(p, draft);
    const blocker = useBlocker(p, draft, {
      settings,
      keyText: keyTextOf(p),
      validation: v,
      busy: false,
    });
    if (blocker) {
      if (blocker !== COPY.blockActive) blocked(p, blocker, v);
      return;
    }
    busy = 'use';
    result = null;
    offerFull = null;
    refresh();
    announce(COPY.checking);
    const keyText = keyTextOf(p);
    let ok = false;
    try {
      // The Claude login needs no check (§7.2); every other provider passes
      // the analyst's own engine first, and the PUT carries its token.
      let token = null;
      let downgraded = false;
      if (p.kind !== 'anthropic_login') {
        const c = await engineCheck(p, draft, keyText);
        if (!c.ok) {
          result = { id: p.id, ...c.copy };
          return;
        }
        ({ token, downgraded } = c);
      }
      const next = await client.save(
        putBody(p, draft, { keyText, activate: true, checkToken: token }),
        settings?.rev,
      );
      if (token) lastFull = null; // redeemed
      applySettings(next, p.id);
      // A saved key is shown only by its last four: leave Replace key.
      replacing = null;
      note(
        p,
        'ok',
        downgraded
          ? `${COPY.activated(p.label)} ${COPY.downgraded(p.label)}`
          : COPY.activated(p.label),
      );
      ok = true;
      changed();
    } catch (error) {
      writeFailed(p, error);
    } finally {
      busy = null;
      finishWrite(ok, 'use');
    }
  }

  async function saveDraft(p) {
    if (busy || locked() || !client) return;
    const draft = draftOf(p);
    if (!guard(p, draft)) return;
    const keyText = keyTextOf(p);
    // An edit to the provider in use applies from the next message: it
    // passes the same full check as Use, and the PUT carries its token.
    const check = saveNeedsCheck(p, draft, { settings, keyText });
    if (check && needsAck(p, draft.model, familyOf(p, draft)) && !draft.ack) {
      blocked(p, COPY.blockAck, validation(p, draft));
      return;
    }
    busy = check ? 'savecheck' : 'save';
    if (check) {
      result = null;
      offerFull = null;
    }
    refresh();
    if (check) announce(COPY.checking);
    let ok = false;
    try {
      let token = null;
      let downgraded = false;
      if (check) {
        const c = await engineCheck(p, draft, keyText);
        if (!c.ok) {
          result = { id: p.id, ...c.copy };
          return;
        }
        ({ token, downgraded } = c);
      }
      const next = await client.save(
        putBody(p, draft, { keyText, activate: check, checkToken: token }),
        settings?.rev,
      );
      if (token) lastFull = null; // redeemed
      applySettings(next, p.id);
      // A saved key is shown only by its last four: leave Replace key.
      replacing = null;
      const active = providers().find((x) => x.id === settings?.active);
      const words =
        settings?.active === p.id
          ? COPY.savedActive
          : COPY.savedInactive(
              active?.label || 'its current provider',
              p.label,
            );
      note(
        p,
        'ok',
        downgraded ? `${words} ${COPY.downgraded(p.label)}` : words,
      );
      ok = true;
      changed();
    } catch (error) {
      writeFailed(p, error);
    } finally {
      busy = null;
      finishWrite(ok, 'save');
    }
  }

  async function removeKey(p) {
    if (busy || !client) return;
    busy = 'remove';
    try {
      const next = await client.removeKey(p.id, settings?.rev);
      applySettings(next, p.id);
      // A saved key is shown only by its last four: leave Replace key.
      replacing = null;
      note(p, 'ok', COPY.removed(p.label));
      changed();
    } catch (error) {
      writeFailed(p, error);
    } finally {
      busy = null;
      confirmRemove = null;
      replacing = null;
      if (!destroyed) {
        renderAll({ keepFocus: 'key' });
        announce(result?.text);
      }
    }
  }

  function askRemove(p) {
    if (locked() || busy) return;
    confirmRemove = p.id;
    renderDetail({ keepFocus: 'keep' });
  }

  function cancelRemove() {
    confirmRemove = null;
    renderDetail({ keepFocus: 'remove' });
  }

  function startReplace(p) {
    if (locked()) return;
    replacing = p.id;
    renderDetail({ keepFocus: 'key' });
  }

  function cancelReplace() {
    replacing = null;
    renderDetail({ keepFocus: 'replace' });
  }

  async function copyDocs(url) {
    const clip = opts.clipboard ?? globalThis.navigator?.clipboard;
    try {
      if (typeof clip?.writeText !== 'function')
        throw new Error('no clipboard');
      await clip.writeText(url);
      announce(COPY.copied);
    } catch {
      announce(COPY.copyFailed);
    }
  }

  /** The analyst's provider or key changed: the shell and header re-read status. */
  function changed() {
    bus?.emit?.('settings:changed', { active: settings?.active ?? null });
    try {
      const p = ctx.chat?.status?.();
      p?.catch?.(() => {});
    } catch {
      /* the header catches up on its next status check */
    }
  }

  // ---- loading and rendering ----

  let wanted = null; // a provider asked for by `open({provider})`

  function applySettings(next, resetId = null) {
    if (!next || typeof next !== 'object' || !Array.isArray(next.providers))
      return;
    settings = next;
    loadError = null;
    if (resetId) drafts.delete(resetId);
    const ids = providers().map((p) => p.id);
    for (const id of [...drafts.keys()])
      if (!ids.includes(id)) drafts.delete(id);
    if (wanted && ids.includes(wanted)) selectedId = wanted;
    wanted = null;
    if (!ids.includes(selectedId))
      selectedId = ids.includes(settings.active)
        ? settings.active
        : ids[0] || null;
  }

  async function reload() {
    if (!client) {
      loadError = { code: 'settings_unavailable', text: COPY.unavailable };
      renderAll();
      return;
    }
    const token = {};
    loading = token;
    renderNotice();
    let same = false;
    try {
      const next = await client.load();
      if (loading !== token || destroyed) return;
      // Unchanged settings keep the panel as it is: a key being pasted while
      // the sheet re-reads the host is never wiped by a rebuild.
      same = Boolean(settings) && !loadError && sameSettings(settings, next);
      applySettings(next);
      if (!settings)
        loadError = { code: 'error', text: 'no settings came back' };
    } catch (error) {
      if (loading !== token || destroyed) return;
      loadError = settingsErrorCopy(error);
    } finally {
      if (loading === token) loading = null;
    }
    if (destroyed) return;
    if (same) {
      renderNotice();
      renderInUse();
    } else renderAll({ keepKey: true });
  }

  function sameSettings(a, b) {
    try {
      return JSON.stringify(a) === JSON.stringify(b);
    } catch {
      return false;
    }
  }

  function renderAll({ keepFocus = null, keepKey = false } = {}) {
    const active = doc?.activeElement;
    const inPicker = contains(pickerEl, active);
    const pickerKey = inPicker ? active?.getAttribute?.('data-fk') : null;
    renderNotice();
    renderInUse();
    renderApproval();
    setHidden(pickerSection, !settings);
    renderPicker();
    renderDetail({ keepFocus, keepKey });
    rootEl.setAttribute('data-readonly', locked() ? 'true' : 'false');
    if (pickerKey) byFocusKey(pickerEl, pickerKey)?.focus?.();
  }

  function focusStart() {
    const row = selectedId ? byFocusKey(pickerEl, `row:${selectedId}`) : null;
    (row || (layout === 'narrow' ? backBtn : closeBtn))?.focus?.();
  }

  // ---- open, close, Esc, focus trap ----

  function openSheet({ provider = null, invoker: from = null } = {}) {
    if (destroyed) return;
    const was = open;
    open = true;
    if (!was) invoker = from ?? doc?.activeElement ?? null;
    if (provider) {
      if (providers().some((p) => p.id === provider)) {
        wanted = null;
        if (provider !== selectedId) {
          selectedId = provider;
          replacing = null;
          confirmRemove = null;
          result = null;
        }
      } else wanted = provider;
    }
    setHidden(rootEl, false);
    applyLayout();
    bus?.emit?.('settings:state', { open: true });
    renderAll();
    focusStart();
    const first = !settings;
    reload().then(() => {
      if (first && open) focusStart();
    });
  }

  function closeSheet({ restore = true } = {}) {
    if (!open) return;
    open = false;
    if (d.keyInput) d.keyInput.value = '';
    typed.clear();
    drafts.clear();
    replacing = null;
    confirmRemove = null;
    showKey = false;
    attempted = false;
    // What the sheet last said is stale by the time it opens again.
    result = null;
    offerFull = null;
    lastFull = null;
    hostError = null;
    setHidden(rootEl, true);
    bus?.emit?.('settings:state', { open: false });
    const target = invoker;
    invoker = null;
    if (restore && target && target.isConnected !== false) target.focus?.();
  }

  /** Close the innermost layer; false when the sheet is closed. */
  function escape() {
    if (!open) return false;
    if (confirmRemove) cancelRemove();
    else if (replacing) cancelReplace();
    else closeSheet();
    return true;
  }

  function onKey(event) {
    if (!open || !event) return;
    if (event.key === 'Escape') {
      event.preventDefault?.();
      event.stopPropagation?.();
      escape();
      return;
    }
    if (event.key !== 'Tab') return;
    const list = focusables(rootEl);
    if (!list.length) return;
    const at = list.indexOf(doc?.activeElement);
    if (event.shiftKey && at <= 0) {
      event.preventDefault?.();
      list[list.length - 1].focus?.();
    } else if (!event.shiftKey && (at === list.length - 1 || at < 0)) {
      event.preventDefault?.();
      list[0].focus?.();
    }
  }

  function applyLayout() {
    const narrow = layout === 'narrow';
    rootEl.setAttribute('data-layout', layout);
    rootEl.setAttribute('aria-modal', narrow ? 'true' : 'false');
    setHidden(backBtn, !narrow);
    setHidden(closeBtn, narrow);
  }

  rootEl.addEventListener('keydown', onKey);
  backBtn.addEventListener('click', () => closeSheet());
  closeBtn.addEventListener('click', () => closeSheet());
  reviewBtn.addEventListener('click', () => {
    closeSheet({ restore: false });
    bus?.emit?.('approval:review', {});
  });
  if (bus?.on) {
    offs.push(
      bus.on('approval:pending', (p) => {
        const n = Number(p?.count);
        pending = {
          count: Number.isFinite(n) && n > 0 ? n : 0,
          oldest: p?.oldest || null,
        };
        renderApproval();
      }),
      bus.on('analyst:availability', (p) => {
        availability = p && typeof p === 'object' ? { ...p } : null;
        if (open) renderInUse();
      }),
    );
  }
  applyLayout();

  return {
    element: rootEl,
    open: openSheet,
    close: closeSheet,
    escape,
    isOpen: () => open,
    reload,
    setLayout(next) {
      layout = ['wide', 'compact', 'narrow'].includes(next) ? next : 'wide';
      applyLayout();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      for (const off of offs.splice(0)) {
        try {
          off?.();
        } catch {
          /* keep tearing down */
        }
      }
      typed.clear();
      rootEl.remove?.();
    },
    /** Tests: the typed-key map is empty whenever no field shows a key. */
    get _typedCount() {
      return typed.size;
    },
  };
}
