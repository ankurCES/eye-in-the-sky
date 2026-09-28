/**
 * Analyst settings: the host's `/settings/llm*` routes (BYOK spec §6, §7).
 *
 * `createSettingsClient(api)` → { load(), save(body, rev), test(body),
 *   removeKey(providerId, rev) }
 *
 * - Every write carries `If-Match: <rev>` (the settings file's ETag) and
 *   `Content-Type: application/json`, which the host's SettingsGuard requires
 *   of every write, a body-less DELETE included.
 * - A typed key travels only in a request body (`key.value` on PUT, `key` on
 *   the test), never in the URL, and nothing here keeps it.
 * - Connection checks get their own timeouts, a little over the host's own
 *   (quick 20 s, full 45 s), so the host's answer arrives first.
 * - Failures are api.js's typed errors; settings/model.js words them.
 */

export const SETTINGS_PATH = '/settings/llm';
/** Client-side limits, just above the host's check timeouts (§6). */
export const TEST_TIMEOUT_MS = Object.freeze({ quick: 25000, full: 50000 });
export const WRITE_TIMEOUT_MS = 15000;

function writeHeaders(rev) {
  const headers = { 'Content-Type': 'application/json' };
  if (rev != null && rev !== '') headers['If-Match'] = String(rev);
  return headers;
}

/**
 * @param {object} api createApi() result: get, post, put, del
 */
export function createSettingsClient(api) {
  for (const name of ['get', 'post', 'put', 'del']) {
    if (typeof api?.[name] !== 'function')
      throw new TypeError(`The settings client needs api.${name}()`);
  }
  return {
    /** GET /settings/llm: the catalog with saved values, keys masked. */
    load(opts = {}) {
      return api.get(SETTINGS_PATH, { signal: opts.signal });
    },
    /** PUT /settings/llm (If-Match rev). Resolves with the GET shape. */
    save(body, rev) {
      return api.put(SETTINGS_PATH, body, {
        headers: writeHeaders(rev),
        timeoutMs: WRITE_TIMEOUT_MS,
      });
    },
    /** POST /settings/llm/test. Resolves with the check result (ok or not). */
    test(body) {
      const depth = body?.depth === 'full' ? 'full' : 'quick';
      return api.post(
        `${SETTINGS_PATH}/test`,
        { ...(body || {}), depth },
        { timeoutMs: TEST_TIMEOUT_MS[depth] },
      );
    },
    /** DELETE /settings/llm/providers/{id}/key. Resolves with the GET shape. */
    removeKey(providerId, rev) {
      const id = encodeURIComponent(String(providerId ?? ''));
      return api.del(`${SETTINGS_PATH}/providers/${id}/key`, {
        headers: writeHeaders(rev),
        timeoutMs: WRITE_TIMEOUT_MS,
      });
    },
  };
}
