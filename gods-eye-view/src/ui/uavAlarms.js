/**
 * UAV alarm presentation (PLAN §3.1 acceptance 5, "≥3 alarm types
 * demonstrated"): safety events arriving on the bridge's SSE `/events`
 * channel become toasts plus a persistent HUD banner, styled by the
 * severity the contract assigns each kind.
 *
 * The bridge ships `/events` after this surface does, so an absent channel is
 * the NORMAL case: the stream reports its own state and this surface shows it
 * as a banner line. Nothing is logged to the console and nothing throws — the
 * poll loop and the rest of the panel are unaffected either way.
 */
import { alarmSeverityRank } from '../sources/live/uav.js';
import { formatAge, h, label, replaceKids, setHidden } from './uavDom.js';

/** How long a toast of each severity stays up; null means until dismissed. */
export const ALARM_DWELL_MS = Object.freeze({
  info: 6000,
  warning: 15000,
  critical: null,
});

const MAX_TOASTS = 5;

const ALARM_CSS = `
.uav-alarm-stack{position:fixed;right:16px;bottom:16px;z-index:9000;
  display:flex;flex-direction:column;gap:6px;max-width:340px;
  font:11px/1.45 "SF Mono",Menlo,monospace;pointer-events:none}
.uav-alarm-toast{pointer-events:auto;display:flex;gap:8px;align-items:flex-start;
  padding:8px 10px;border-radius:6px;border:1px solid #234b40;
  background:rgba(10,14,18,.94);color:#d7f5ec;
  box-shadow:0 6px 20px rgba(0,0,0,.55)}
.uav-alarm-toast .uav-alarm-kind{font-weight:700;letter-spacing:.1em;
  white-space:nowrap}
.uav-alarm-toast .uav-alarm-msg{flex:1}
.uav-alarm-toast .uav-alarm-meta{color:#5f8c80}
.uav-alarm-toast .uav-alarm-close{background:transparent;border:0;cursor:pointer;
  color:#5f8c80;font:inherit;line-height:1;padding:0 2px}
.uav-alarm-toast[data-severity="info"]{border-color:#1de9b6}
.uav-alarm-toast[data-severity="info"] .uav-alarm-kind{color:#1de9b6}
.uav-alarm-toast[data-severity="warning"]{border-color:#ffd166}
.uav-alarm-toast[data-severity="warning"] .uav-alarm-kind{color:#ffd166}
.uav-alarm-toast[data-severity="critical"]{border-color:#ff7a7a}
.uav-alarm-toast[data-severity="critical"] .uav-alarm-kind{color:#ff7a7a}
.uav-alarm-banner{margin-top:8px;padding:5px 8px;border-radius:4px;
  border:1px solid #234b40;background:#0d1512;color:#9fd9c8;
  font:10px/1.4 "SF Mono",Menlo,monospace;display:flex;gap:6px;
  align-items:center;letter-spacing:.06em}
.uav-alarm-banner .uav-alarm-kind{font-weight:700}
.uav-alarm-banner[data-severity="info"]{border-color:#1de9b6;color:#8fe9cf}
.uav-alarm-banner[data-severity="warning"]{border-color:#ffd166;color:#ffd166}
.uav-alarm-banner[data-severity="critical"]{border-color:#ff7a7a;color:#ff7a7a}
.uav-alarm-banner[data-severity="feed"]{border-color:#234b40;color:#5f8c80}
.uav-alarm-stack .is-hidden,.uav-alarm-banner.is-hidden{display:none}
`;

/**
 * Banner text for a stream that is not delivering.
 * @param {string} status stream status from `createUavEventStream`
 * @param {object} [detail] `{retryInMs}` when reconnecting
 * @returns {{kind: string, text: string}|null} null when nothing to say
 */
export function streamStatusNotice(status, detail = {}) {
  if (status === 'unsupported')
    return {
      kind: 'ALARM FEED',
      text: 'unavailable — this bridge serves no /events channel yet',
    };
  if (status === 'offline') {
    const retry = Number.isFinite(detail.retryInMs)
      ? ` — retrying in ${Math.round(detail.retryInMs / 1000)}s`
      : '';
    return { kind: 'ALARM FEED', text: `offline${retry}` };
  }
  if (status === 'connecting')
    return { kind: 'ALARM FEED', text: 'connecting…' };
  return null;
}

/**
 * Choose what the persistent HUD banner shows: the worst live alarm, else
 * the alarm feed's own state. Pure, so the precedence is testable.
 * @param {Array<object>} entries live toast entries `{alarm, expiresAtMs}`
 * @param {string} status stream status
 * @param {object} detail stream status detail
 * @param {number} nowMs current time
 * @returns {object|null} `{severity, kind, text, atMs}` or null when idle
 */
export function alarmBannerModel(entries, status, detail, nowMs) {
  const live = entries.filter(
    (entry) => entry.expiresAtMs == null || entry.expiresAtMs > nowMs,
  );
  let worst = null;
  for (const entry of live) {
    if (
      !worst ||
      alarmSeverityRank(entry.alarm.severity) >
        alarmSeverityRank(worst.alarm.severity) ||
      (alarmSeverityRank(entry.alarm.severity) ===
        alarmSeverityRank(worst.alarm.severity) &&
        entry.alarm.atMs >= worst.alarm.atMs)
    )
      worst = entry;
  }
  if (worst)
    return {
      severity: worst.alarm.severity,
      kind: label(worst.alarm.kind),
      text: worst.alarm.message,
      atMs: worst.alarm.atMs,
    };
  const notice = streamStatusNotice(status, detail);
  return notice
    ? { severity: 'feed', kind: notice.kind, text: notice.text, atMs: null }
    : null;
}

/**
 * Create the alarm toast stack plus the HUD banner element.
 * @param {object} [options]
 * @param {() => number} [options.now] clock, for tests
 * @param {object} [options.dwellMs] per-severity toast dwell, null = sticky
 * @param {number} [options.tickMs] prune/repaint interval
 * @param {Function} [options.startTimer] interval starter, for tests
 * @param {Function} [options.stopTimer] interval canceller, for tests
 * @param {boolean} [options.withStyle] emit the scoped stylesheet element
 * @returns {object} `{element, banner, style, push, setStreamStatus, ...}`
 */
export function createUavAlarmSurface({
  now = () => Date.now(),
  dwellMs = ALARM_DWELL_MS,
  maxToasts = MAX_TOASTS,
  tickMs = 1000,
  startTimer = (fn, ms) => setInterval(fn, ms),
  stopTimer = (handle) => clearInterval(handle),
  withStyle = true,
} = {}) {
  const style = withStyle ? h('style') : null;
  if (style) style.textContent = ALARM_CSS;

  const element = h('div', { class: 'uav-alarm-stack' });
  const bannerKind = h('span', { class: 'uav-alarm-kind' }, '');
  const bannerText = h('span', { class: 'uav-alarm-msg' }, '');
  const banner = h(
    'div',
    { class: 'uav-alarm-banner', 'data-severity': 'feed' },
    bannerKind,
    bannerText,
  );
  setHidden(banner, true);

  /** @type {Array<{id:string, alarm:object, expiresAtMs:number|null}>} */
  let entries = [];
  let streamStatus = 'idle';
  let streamDetail = {};
  let sequence = 0;
  let timer = null;

  function toast(entry) {
    const close = h(
      'button',
      { class: 'uav-alarm-close', title: 'Dismiss' },
      '×',
    );
    close.addEventListener('click', () => dismiss(entry.id));
    return h(
      'div',
      { class: 'uav-alarm-toast', 'data-severity': entry.alarm.severity },
      h('span', { class: 'uav-alarm-kind' }, label(entry.alarm.kind)),
      h(
        'span',
        { class: 'uav-alarm-msg' },
        entry.alarm.message,
        h(
          'div',
          { class: 'uav-alarm-meta' },
          [
            entry.alarm.vehicle,
            entry.alarm.trackId,
            formatAge(entry.alarm.atMs, now()),
          ]
            .filter(Boolean)
            .join(' · '),
        ),
      ),
      close,
    );
  }

  function render() {
    const nowMs = now();
    replaceKids(element, entries.map(toast));
    setHidden(element, entries.length === 0);
    const model = alarmBannerModel(entries, streamStatus, streamDetail, nowMs);
    if (!model) {
      setHidden(banner, true);
      return null;
    }
    banner.setAttribute('data-severity', model.severity);
    bannerKind.textContent = model.kind;
    bannerText.textContent = model.text;
    setHidden(banner, false);
    return model;
  }

  function prune(nowMs = now()) {
    const before = entries.length;
    entries = entries.filter(
      (entry) => entry.expiresAtMs == null || entry.expiresAtMs > nowMs,
    );
    if (entries.length !== before) render();
    return entries.length;
  }

  function dismiss(id) {
    const before = entries.length;
    entries = entries.filter((entry) => entry.id !== id);
    if (entries.length !== before) render();
  }

  function arm() {
    if (timer != null || !tickMs) return;
    timer = startTimer(() => prune(), tickMs);
  }

  return {
    element,
    banner,
    style,
    /**
     * Raise one normalized alarm.
     * @param {object} alarm from `normalizeAlarm`
     * @returns {object|null} the entry, or null when the alarm is unusable
     */
    push(alarm) {
      if (!alarm?.kind) return null;
      const dwell = Object.hasOwn(dwellMs, alarm.severity)
        ? dwellMs[alarm.severity]
        : ALARM_DWELL_MS.info;
      sequence += 1;
      const entry = {
        id: `uav-alarm-${sequence}`,
        alarm,
        expiresAtMs: dwell == null ? null : now() + dwell,
      };
      // Newest first, oldest evicted: a critical alarm outlives chatter.
      entries = [entry, ...entries].slice(0, maxToasts);
      arm();
      render();
      return entry;
    },
    /**
     * Report the alarm channel's own state (absent, reconnecting, live).
     * @param {string} status
     * @param {object} [detail]
     */
    setStreamStatus(status, detail = {}) {
      streamStatus = status;
      streamDetail = detail || {};
      arm();
      return render();
    },
    dismiss,
    prune,
    /** Live entries, newest first — the surface's state for tests. */
    active() {
      return entries.slice();
    },
    /** Drop every toast without touching the stream status. */
    clear() {
      entries = [];
      render();
    },
    destroy() {
      if (timer != null) stopTimer(timer);
      timer = null;
      entries = [];
      replaceKids(element, []);
      element.remove?.();
      banner.remove?.();
      style?.remove?.();
    },
  };
}
