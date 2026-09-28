"""Direct HTTP plumbing for the geodata providers (WG v2 §4.1.5, D6, C9).

`geocode.py`, `sites.py` and `realdata.py`'s direct mode talk to the public
upstreams (Photon, Nominatim, Overpass, Re:Earth, Open-Meteo) themselves rather
than through God's Eye View's dev-server proxies. Everything those proxies did
for politeness therefore lives here, once:

* one identifying `User-Agent` (`GEO_USER_AGENT`), as the Nominatim and
  Overpass usage policies ask;
* one process-wide `realdata.RateGate` per upstream (`gate()`), spaced by
  `MIN_SPACING_S`;
* a bounded body (16 MB) and a hard egress switch: with `GODSEYE_NO_EGRESS=1`
  the default client refuses before opening a socket (the test suite's egress
  guard sets it, §4.1.10);
* `DiskCache` (memory plus atomic JSON files) and `CallBudget` (a sliding
  window the tools use to refuse a flood of uncached calls).

An injected `fetch` is a test double: it bypasses the egress switch and the
gate, because it never reaches the upstream the gate protects. Every failure is
raised as `realdata.RealDataUnavailable`, which the providers turn into data
(`real=False` plus a reason) — never into silence.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .realdata import HttpResponse, RateGate, RealDataUnavailable

_LOG = logging.getLogger(__name__)

#: Env switch: "1" makes the default client refuse every request (§4.1.10).
NO_EGRESS_ENV = "GODSEYE_NO_EGRESS"
#: Env override for the contact named in the User-Agent.
CONTACT_ENV = "GODSEYE_GEO_CONTACT"
#: The reason every refused request carries.
EGRESS_DISABLED = "egress disabled"

#: Response body cap, bytes (same as realdata's default client).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
#: The reason a body over the cap carries (sites.fetch_exclusion reads it).
CAP_REASON = "response exceeded the 16 MB cap"

#: Minimum spacing between two requests to one upstream, seconds (§4.1.5).
#: `overpass-kumi` is the one-shot fallback mirror; it gets its own gate so a
#: retry after a failure is not held behind the main instance's spacing.
MIN_SPACING_S: Mapping[str, float] = {
    "photon": 1.0,
    "nominatim": 1.1,
    "overpass": 5.0,
    "overpass-kumi": 5.0,
    "reearth": 0.25,
    "open-meteo": 0.5,
}

#: The only upstream URLs this module is ever pointed at (§4.1.5).
UPSTREAM_URLS: Mapping[str, str] = {
    "photon": "https://photon.komoot.io/api/",
    "nominatim": "https://nominatim.openstreetmap.org/search",
    "overpass": "https://overpass-api.de/api/interpreter",
    "overpass-kumi": "https://overpass.kumi.systems/api/interpreter",
    "reearth": "https://terrain.reearth.land/heights.json",
    "open-meteo": "https://api.open-meteo.com/v1/forecast",
    "open-meteo-elevation": "https://api.open-meteo.com/v1/elevation",
}

#: Bidi controls stripped from untrusted text (§0.2, §3.11) plus the
#: ALM/LRM/RLM marks that steer the same algorithm.
_BIDI = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)]
    + [chr(0x061C), chr(0x200E), chr(0x200F)])

#: `fetch(url, timeout_s)` for a GET, `fetch(url, timeout_s, data=bytes)` for a
#: POST, returning a `realdata.HttpResponse`. Injected in tests.
GeoFetch = Callable[..., HttpResponse]


@functools.cache
def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("godseye-uav")
    except Exception:  # noqa: BLE001 — a source checkout has no metadata
        return "0.1.0"


def user_agent() -> str:
    """The identifying User-Agent, read now (the contact env may change)."""
    contact = os.environ.get(CONTACT_ENV) or "unset"
    return f"EyeInTheSky/{_package_version()} (+godseye; contact: {contact})"


#: The User-Agent at import time (`user_agent()` re-reads the contact env).
GEO_USER_AGENT = user_agent()


def egress_disabled() -> bool:
    """True when `GODSEYE_NO_EGRESS` is set to a truthy value."""
    return os.environ.get(NO_EGRESS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def clean_text(value: Any, max_len: int = 160) -> str:
    """Untrusted text made safe to carry: bidi and control characters removed,
    whitespace collapsed, truncated to `max_len` characters (§0.2)."""
    if value is None:
        return ""
    out = []
    for ch in str(value):
        if ch in _BIDI:
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" or cat in ("Zl", "Zp"):
            out.append(" ")  # a newline or tab separates words; keep the gap
            continue
        out.append(ch)
    text = " ".join("".join(out).split())
    return text[:max(0, int(max_len))]


_GATES: dict[str, RateGate] = {}
_GATES_LOCK = threading.Lock()


def gate(upstream: str) -> RateGate:
    """The process-wide `RateGate` for one upstream (created on first use)."""
    if upstream not in MIN_SPACING_S:
        raise ValueError(f"unknown upstream {upstream!r}; known: {sorted(MIN_SPACING_S)}")
    with _GATES_LOCK:
        found = _GATES.get(upstream)
        if found is None:
            found = _GATES[upstream] = RateGate(MIN_SPACING_S[upstream])
        return found


def http_fetch(url: str, timeout_s: float, data: bytes | None = None) -> HttpResponse:
    """Default client: stdlib, identifying UA, 16 MB cap, honours the egress
    switch. GET without `data`, form POST with it. Raises RealDataUnavailable
    for transport failures; returns any HTTP status as a response."""
    if egress_disabled():
        raise RealDataUnavailable("http", EGRESS_DISABLED)
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RealDataUnavailable("http", CAP_REASON)
            lowered = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResponse(int(resp.status), raw.decode("utf-8", "replace"), lowered)
    except urllib.error.HTTPError as exc:  # a status, not a transport failure
        raw = exc.read(64 * 1024) if hasattr(exc, "read") else b""
        lowered = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return HttpResponse(int(exc.code), raw.decode("utf-8", "replace"), lowered)
    except RealDataUnavailable:
        raise
    except Exception as exc:  # reported as the reason, never swallowed
        raise RealDataUnavailable("http", f"{type(exc).__name__}: {exc}") from exc


def _encode(data: Mapping[str, str] | bytes | None) -> bytes | None:
    if data is None or isinstance(data, bytes):
        return data
    return urllib.parse.urlencode(dict(data)).encode("utf-8")


def fetch_json(upstream: str, url: str, *, timeout_s: float,
               data: Mapping[str, str] | bytes | None = None,
               fetch: GeoFetch | None = None) -> Any:
    """One blocking JSON request to `upstream`. Raises RealDataUnavailable.

    Without `fetch`: refuses under `GODSEYE_NO_EGRESS=1`, then waits on
    `gate(upstream)`, then uses `http_fetch`. `data` (a mapping is
    form-encoded) makes it a POST.
    """
    if upstream not in MIN_SPACING_S:
        raise ValueError(f"unknown upstream {upstream!r}")
    body = _encode(data)
    try:
        if fetch is None:
            if egress_disabled():
                raise RealDataUnavailable(upstream, EGRESS_DISABLED)
            gate(upstream).wait()
            resp = http_fetch(url, timeout_s, body)
        elif body is None:
            resp = fetch(url, timeout_s)
        else:
            resp = fetch(url, timeout_s, data=body)
    except RealDataUnavailable as exc:
        if exc.feed == upstream:
            raise
        raise RealDataUnavailable(upstream, exc.reason) from exc
    except Exception as exc:  # a double's OSError is a failure too
        raise RealDataUnavailable(upstream, f"{type(exc).__name__}: {exc}") from exc
    if resp.status != 200:
        detail = " (rate limited)" if resp.status == 429 else ""
        raise RealDataUnavailable(upstream, f"HTTP {resp.status}{detail}")
    if len(resp.body) > MAX_RESPONSE_BYTES:
        raise RealDataUnavailable(upstream, CAP_REASON)
    try:
        return json.loads(resp.body)
    except ValueError as exc:
        raise RealDataUnavailable(upstream, f"unparsable response: {exc}") from exc


class DiskCache:
    """Memory plus atomic JSON files under `root/namespace`; `root=None` is
    memory only. Values must be JSON-serialisable. Never raises on I/O: a
    cache that cannot write degrades to memory and logs once."""

    def __init__(self, root: str | os.PathLike | None, namespace: str, ttl_s: float, *,
                 now: Callable[[], float] = time.time, max_memory: int = 512) -> None:
        self.namespace = str(namespace)
        self.dir: Path | None = (None if root is None
                                 else Path(root) / self.namespace)
        self.ttl_s = float(ttl_s)
        self._now = now
        self._max = max(1, int(max_memory))
        self._mem: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._disk_warned = False

    def path_for(self, key: str) -> Path | None:
        if self.dir is None:
            return None
        return self.dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def _remember(self, key: str, entry: tuple[float, Any]) -> None:
        with self._lock:
            self._mem[key] = entry
            self._mem.move_to_end(key)
            while len(self._mem) > self._max:
                self._mem.popitem(last=False)

    def _load(self, key: str) -> tuple[float, Any] | None:
        path = self.path_for(key)
        if path is None:
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # A hash collision or a hand-edited file must not answer for this key.
        if not isinstance(doc, dict) or doc.get("key") != key:
            return None
        at = doc.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
            return None
        entry = (float(at), doc.get("value"))
        self._remember(key, entry)
        return entry

    def get_entry(self, key: str, *, ttl_s: float | None = None) -> tuple[float, Any] | None:
        """`(stored_at_s, value)` under the same TTL rule as `get`."""
        with self._lock:
            entry = self._mem.get(key)
        if entry is None:
            entry = self._load(key)
        if entry is None:
            return None
        ttl = self.ttl_s if ttl_s is None else float(ttl_s)
        if self._now() - entry[0] >= ttl:
            return None
        return entry

    def get(self, key: str, *, ttl_s: float | None = None) -> Any | None:
        """The value if stored less than `ttl_s` (default: the cache's) ago."""
        entry = self.get_entry(key, ttl_s=ttl_s)
        return None if entry is None else entry[1]

    def put(self, key: str, value: Any) -> None:
        """Store `value` in memory and, with a root, atomically on disk."""
        at = float(self._now())
        text = json.dumps({"key": key, "at": at, "value": value}, separators=(",", ":"))
        self._remember(key, (at, json.loads(text)["value"]))
        path = self.path_for(key)
        if path is None:
            return
        tmp: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)  # atomic: a reader sees the old file or the new one
            tmp = None
        except OSError as exc:
            if not self._disk_warned:
                self._disk_warned = True
                _LOG.warning("geodata cache %s: disk write failed (%s); memory only",
                             self.dir, exc)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def default_cache(root: str | os.PathLike | None) -> DiskCache:
    """The shared geodata cache (§4.1.5 wiring): namespace `geodata`, 30-day
    default TTL; each provider passes its own TTL on read."""
    return DiskCache(root, "geodata", 30 * 86_400.0)


class CallBudget:
    """At most `calls` takes in any sliding `window_s`. Thread-safe."""

    def __init__(self, calls: int, window_s: float, *,
                 now: Callable[[], float] = time.monotonic) -> None:
        if int(calls) < 1 or float(window_s) <= 0:
            raise ValueError("CallBudget needs calls >= 1 and window_s > 0")
        self.calls = int(calls)
        self.window_s = float(window_s)
        self._now = now
        self._taken: deque[float] = deque()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._taken and now - self._taken[0] >= self.window_s:
            self._taken.popleft()

    def take(self) -> bool:
        """Spend one call; False (and nothing spent) when the window is full."""
        with self._lock:
            now = self._now()
            self._expire(now)
            if len(self._taken) >= self.calls:
                return False
            self._taken.append(now)
            return True

    def remaining(self) -> int:
        with self._lock:
            self._expire(self._now())
            return self.calls - len(self._taken)
