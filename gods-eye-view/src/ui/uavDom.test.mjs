import test from 'node:test';
import assert from 'node:assert/strict';

import {
  clamp,
  formatAge,
  formatDuration,
  formatPct,
  h,
  hasClass,
  label,
  replaceKids,
  setClass,
  setHidden,
} from './uavDom.js';

function stubDocument() {
  return {
    createElement: (tag) => ({
      tag,
      children: [],
      attrs: {},
      className: '',
      textContent: '',
      append(...kids) {
        this.children.push(...kids);
      },
      setAttribute(key, value) {
        this.attrs[key] = value;
      },
      removeAttribute(key) {
        delete this.attrs[key];
      },
    }),
  };
}

test('unknown readouts render as an em dash, never as zero', () => {
  assert.equal(formatPct(null), '—');
  assert.equal(formatPct(undefined), '—');
  assert.equal(formatPct(0), '0%');
  assert.equal(formatPct(43.52, 1), '43.5%');
  assert.equal(formatDuration(null), '—');
  assert.equal(formatDuration(-5), '—');
  assert.equal(formatDuration(0), '0:00');
  assert.equal(formatDuration(930), '15:30');
  assert.equal(formatDuration(3725), '1:02:05');
  assert.equal(formatAge(null), '—');
});

test('ages compact from seconds to days', () => {
  const now = 1_000_000_000;
  assert.equal(formatAge(now - 5_000, now), '5s');
  assert.equal(formatAge(now - 300_000, now), '5m');
  assert.equal(formatAge(now - 7_200_000, now), '2h');
  assert.equal(formatAge(now - 172_800_000, now), '2d');
  assert.equal(formatAge(now + 5_000, now), '0s'); // clock skew is not negative age
});

test('clamp passes unknowns through instead of inventing a bound', () => {
  assert.equal(clamp(150, 0, 100), 100);
  assert.equal(clamp(-5, 0, 100), 0);
  assert.equal(clamp(null, 0, 100), null);
  assert.equal(clamp(NaN, 0, 100, 42), 42);
});

test('labels upcase snake_case identifiers and keep a fallback', () => {
  assert.equal(label('geofence_proximity'), 'GEOFENCE PROXIMITY');
  assert.equal(label(''), '—');
  assert.equal(label(null, 'NONE'), 'NONE');
});

test('class and visibility helpers work without a classList', () => {
  globalThis.document = stubDocument();
  const el = h('div', { class: 'a b' }, 'x');
  assert.equal(el.className, 'a b');
  assert.equal(hasClass(el, 'b'), true);
  setClass(el, 'b', false);
  assert.equal(hasClass(el, 'b'), false);
  setClass(el, 'c', true);
  assert.equal(el.className, 'a c');
  setHidden(el, true);
  assert.equal(el.attrs.hidden, '');
  setHidden(el, false);
  assert.equal(el.attrs.hidden, undefined);
});

test('h skips null attributes and children so optional slots stay empty', () => {
  globalThis.document = stubDocument();
  const el = h('div', { title: null, 'data-x': 1, hidden: false }, null, 'kid');
  assert.equal(el.attrs.title, undefined);
  assert.equal(el.attrs['data-x'], '1');
  assert.equal(el.attrs.hidden, undefined);
  assert.deepEqual(el.children, ['kid']);
  replaceKids(el, ['other']);
  assert.deepEqual(el.children, ['other']);
});
