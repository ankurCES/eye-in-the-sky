/**
 * Shared plain-DOM helpers for the UAV command-center surfaces (mission
 * panel, mission HUD, alarm surface, contact roster). No framework, no
 * dependencies: every surface builds elements the same way so the panel and
 * its children can be mounted into one another or tested against a stub
 * document that implements only createElement/append/setAttribute.
 */

/**
 * Create an element with attributes and children.
 * @param {string} tag element tag name
 * @param {object} [attrs] attribute map; `class` sets className
 * @param {...(Node|string)} kids appended children
 * @returns {object} the created element
 */
export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const kid of kids) if (kid != null && kid !== false) el.append(kid);
  return el;
}

/**
 * Toggle a class without assuming `classList` exists (test stubs do not).
 * @param {object} el element
 * @param {string} name class name
 * @param {boolean} on whether the class should be present
 */
export function setClass(el, name, on) {
  if (!el) return;
  if (el.classList?.toggle) {
    el.classList.toggle(name, Boolean(on));
    return;
  }
  const parts = String(el.className || '')
    .split(/\s+/)
    .filter(Boolean);
  const index = parts.indexOf(name);
  if (on && index < 0) parts.push(name);
  if (!on && index >= 0) parts.splice(index, 1);
  el.className = parts.join(' ');
}

/** Whether an element carries a class, `classList` or not. */
export function hasClass(el, name) {
  if (!el) return false;
  if (el.classList?.contains) return el.classList.contains(name);
  return String(el.className || '')
    .split(/\s+/)
    .includes(name);
}

/** Replace an element's children, falling back to append on a stub. */
export function replaceKids(el, kids) {
  if (!el) return;
  if (el.replaceChildren) el.replaceChildren(...kids);
  else {
    el.children = [];
    for (const kid of kids) el.append(kid);
  }
}

/** Show or hide an element without relying on a `style` object. */
export function setHidden(el, hidden) {
  if (!el) return;
  if (hidden) el.setAttribute('hidden', '');
  else el.removeAttribute?.('hidden');
  setClass(el, 'is-hidden', hidden);
}

/** Percentage for display: `null` is unknown, never a silent zero. */
export function formatPct(value, digits = 0) {
  return Number.isFinite(value) ? `${value.toFixed(digits)}%` : '—';
}

/** Seconds as `m:ss` (or `h:mm:ss`); `null` is unknown. */
export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const total = Math.round(seconds);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const hrs = Math.floor(total / 3600);
  const pad = (n) => String(n).padStart(2, '0');
  return hrs ? `${hrs}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Elapsed wall time as a compact "12s" / "4m" / "2h" age label. */
export function formatAge(atMs, nowMs = Date.now()) {
  if (!Number.isFinite(atMs)) return '—';
  const seconds = Math.max(0, Math.round((nowMs - atMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/** Clamp a number into a range, passing `null` through as `fallback`. */
export function clamp(value, min, max, fallback = null) {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

/** Uppercase a snake_case identifier for operator display. */
export function label(value, fallback = '—') {
  const text = typeof value === 'string' ? value.trim() : '';
  return text ? text.replace(/_/g, ' ').toUpperCase() : fallback;
}
