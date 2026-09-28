"""The analyst's model-provider settings (BYOK spec v1, sections 2-7 and 9).

One ``LlmSettings`` per host. It owns:

* the settings file ``<store_dir.parent>/llm-settings.json`` (schema version,
  ``rev`` as an ETag, atomic 0600 writes, never a secret);
* the secret store: launch-environment overrides, then the macOS Keychain
  (``/usr/bin/security``, the secret on stdin, never argv), then a 0600 file;
* ``resolve()``: the one place the CLI child environment is built
  (``build_child_env``). The SDK cannot unset a variable, so every credential
  variable and provider switch is written blank on every build, and every
  kind but the Claude login runs with the ``HARDEN`` set and an isolated
  ``CLAUDE_CONFIG_DIR``. A kind that needs a key refuses to build without a
  non-empty one: with a base URL and no key the CLI sends the owner's Claude
  OAuth token to that URL (spec section 0.5);
* ``redact`` / ``RedactingFilter``: no live key reaches a log, an event or an
  HTTP answer;
* ``quick_probe`` (direct HTTP, redirects refused) and ``full_check`` (the
  analyst's real engine, one tiny turn). The engine follows a redirect and
  re-sends ``x-api-key`` to the new host, so a URL-kind endpoint that
  redirects fails the full check before the engine runs (``redirect_probe``),
  and ``preflight`` repeats that probe before the analyst spawns a CLI;
* ``put``: Save never activates, so a change to the provider in use (config,
  family or key) needs a matching ``check_token`` and, for a non-Claude
  model, the acknowledgement; ``resolve`` fails closed without it;
* ``llm_settings_router`` (``/settings/llm*``) and ``SettingsGuardMiddleware``.

Nothing here mutates ``os.environ`` except ``capture_llm_env``, which the app
calls once at launch, before anything spawns the CLI.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import http.client
import importlib
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets as _secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import llm_providers as catalog

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ constants --

SCHEMA = "eye-in-the-sky.llm-settings/1"
SCHEMA_PREFIX = "eye-in-the-sky.llm-settings/"
SECRETS_SCHEMA = "eye-in-the-sky.llm-secrets/1"
SETTINGS_FILE = "llm-settings.json"
SECRETS_FILE = "llm-secrets.json"
KEYCHAIN_SERVICE = "eye-in-the-sky.llm"
KEYCHAIN_LABEL = "Eye in the Sky LLM key"
SECURITY_BIN = "/usr/bin/security"
#: Forces the secret backend: ``keychain`` | ``file`` | ``memory`` (dev and test
#: runs use ``file`` so they never touch the login keychain).
SECRET_STORE_ENV = "GODSEYE_LLM_SECRET_STORE"
PROVIDER_ENV = "GODSEYE_LLM_PROVIDER"
MODEL_ENV = "GODSEYE_CHAT_MODEL"
EFFORT_ENV = "GODSEYE_CHAT_EFFORT"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: ``chat.DEFAULT_MODEL``; read from chat lazily, this is only the fallback.
FALLBACK_MODEL = "claude-opus-5"

#: Launch-environment variables captured and popped before anything spawns the
#: CLI (spec section 3.3). Order matters only for readability.
CAPTURED_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_AWS_API_KEY",
    "OPENROUTER_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
    "ZAI_API_KEY", "ZHIPU_API_KEY", "DASHSCOPE_API_KEY", "OLLAMA_API_KEY",
    "GODSEYE_LLM_PROVIDER",
    # Also read by the bundled CLI (2.1.283 ``strings``): more provider
    # switches and their routing and credentials. An inherited
    # CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD overrode a BYOK endpoint and sent
    # the CLI to Google's metadata server (secrets review).
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
    "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID", "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_GATEWAY_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "ANTHROPIC_UNIX_SOCKET",
)
#: Captured names whose values are credentials (redacted, never shown).
SECRET_VARS = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_GATEWAY_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    *(n for n in CAPTURED_VARS if n.endswith("_API_KEY")),
})

#: Written on every build, for every kind (spec section 4.1). ``""`` counts as
#: absent for the credentials and the switches. Every ``CLAUDE_CODE_USE_*``
#: provider switch the bundled CLI reads is here (a test runs ``strings``).
BLANK = MappingProxyType({
    "ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "",
    "CLAUDE_CODE_USE_BEDROCK": "", "CLAUDE_CODE_USE_VERTEX": "", "CLAUDE_CODE_USE_FOUNDRY": "",
    "CLAUDE_CODE_USE_MANTLE": "", "CLAUDE_CODE_USE_ANTHROPIC_AWS": "",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "", "CLAUDE_CODE_USE_GATEWAY": "",
})
#: Added for every kind except the Claude login (spec section 4.2), together
#: with ``CLAUDE_CONFIG_DIR=<store>/analyst/claude-home``.
HARDEN_FLAGS = MappingProxyType({
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_MAX_RETRIES": "2",
})
PIN_VARS = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL")
#: Every name ``build_child_env`` may emit (the catalog tests check against it).
CHILD_ENV_ALLOWLIST = frozenset({
    *BLANK, *HARDEN_FLAGS, *PIN_VARS, *catalog.EXTRA_ENV_ALLOWLIST,
    *catalog.FIELD_ENV_ALLOWLIST, "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_DISABLE_THINKING", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY",
})
#: Child-env names whose value is a credential.
CHILD_SECRET_VARS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                               "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_FOUNDRY_API_KEY",
                               "CLAUDE_CODE_OAUTH_TOKEN"})

ADAPTIVE_THINKING = MappingProxyType({"type": "adaptive", "display": "summarized"})
DISABLED_THINKING = MappingProxyType({"type": "disabled"})
THINKING_SETTINGS = ("auto", "adaptive", "off")

REDACTED = "[redacted key]"
REDACT_MIN_RUN = 12

REASON_NOT_CONFIGURED = "provider_not_configured"
REASON_KEY_MISSING = "provider_key_missing"
REASON_SETTINGS_ERROR = "settings_error"

QUICK_TIMEOUT_S = 20.0
FULL_TIMEOUT_S = 45.0
FULL_COOLDOWN_S = 10.0
CHECK_TOKEN_TTL_S = 600.0
PROBE_READ_MAX = 64 * 1024
ANTHROPIC_VERSION = "2023-06-01"
_THINKING_RX = re.compile(r"(?i)thinking|adaptive")

# ------------------------------------------------------------------- errors --


class SettingsError(Exception):
    """A request the settings API refuses; ``body()`` is the JSON answer."""

    def __init__(self, status: int, error: str, *, field: str | None = None,
                 message: str | None = None, **extra: Any):
        super().__init__(message or error)
        self.status = status
        self.error = error
        self.field = field
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        out: dict[str, Any] = {"error": self.error}
        if self.field is not None:
            out["field"] = self.field
        if self.message is not None:
            out["message"] = self.message
        out.update(self.extra)
        return out


class KeychainError(Exception):
    """``security`` failed for a reason other than "not found"."""


def _invalid(field_name: str | None, message: str) -> SettingsError:
    return SettingsError(422, "invalid_settings", field=field_name, message=message)


# --------------------------------------------------------------- validators --

MODEL_RX = re.compile(r"^[A-Za-z0-9~][A-Za-z0-9._:/@\[\]~+-]{0,199}$")
_PLACEHOLDER_RX = re.compile(r"\{([A-Za-z0-9_]+)\}")
_CTL = ("\r", "\n", "\x00")


def has_ctl(value: Any) -> bool:
    """True when ``value`` (a string, or strings nested in dicts/lists) holds
    CR, LF or NUL."""
    if isinstance(value, str):
        return any(c in value for c in _CTL)
    if isinstance(value, Mapping):
        return any(has_ctl(k) or has_ctl(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_ctl(v) for v in value)
    return False


def _text(value: Any, field_name: str) -> str | None:
    """A stripped string or None; refuses other types and control characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(field_name, "Must be text.")
    if has_ctl(value):
        raise _invalid(field_name, "Must not contain line breaks.")
    value = value.strip()
    return value or None


def validate_model(value: Any, field_name: str = "model") -> str | None:
    """A model id the SDK can pass as ``--model <v>`` (never a leading ``-``)."""
    value = _text(value, field_name)
    if value is None:
        return None
    if not MODEL_RX.fullmatch(value):
        raise _invalid(field_name, "Use letters, digits and . _ : / @ [ ] ~ + - only, "
                                   "starting with a letter or digit.")
    return value


def validate_key(value: Any) -> str:
    """A key as pasted: stripped, 8-512 printable ASCII characters."""
    if not isinstance(value, str) or has_ctl(value):
        raise _invalid("key", "Paste the key as one line of text.")
    value = value.strip()
    if not 8 <= len(value) <= 512:
        raise _invalid("key", "A key is 8 to 512 characters long.")
    if any(not 0x21 <= ord(c) <= 0x7E for c in value):
        raise _invalid("key", "A key has no spaces or special characters.")
    return value


def validate_field(spec_field: Mapping, value: Any) -> str | None:
    """One catalog field (AWS region, GCP project, credentials file, ...)."""
    fid = f"fields.{spec_field['id']}"
    value = _text(value, fid)
    if value is None:
        return None
    if spec_field.get("type") == "file":
        p = pathlib.Path(value)
        if not p.is_absolute() or not p.is_file():
            raise _invalid(fid, "Use the absolute path of an existing file.")
        return value
    pattern = spec_field.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        raise _invalid(fid, f"This doesn't look like a valid {spec_field['label'].lower()}.")
    return value


_LINK_LOCAL_V4 = ipaddress.ip_network("169.254.0.0/16")
_LINK_LOCAL_V6 = ipaddress.ip_network("fe80::/10")
_ULA = ipaddress.ip_network("fc00::/7")
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12",
                                                   "192.168.0.0/16"))


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def is_loopback_host(host: str | None) -> bool:
    """``localhost``, ``127.0.0.0/8`` or ``::1``."""
    if not host:
        return False
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    ip = _ip(host)
    return bool(ip and ip.is_loopback)


def _is_private_lan(host: str) -> bool:
    ip = _ip(host)
    if ip is None:
        return False
    if ip.version == 4:
        return any(ip in n for n in _RFC1918)
    return ip in _ULA


def url_host(url: str | None) -> str | None:
    """The host of ``url`` for display (``openrouter.ai``, ``127.0.0.1:8080``)."""
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def fill_placeholders(url: str, fields: Mapping[str, Any] | None) -> str:
    """``{field}`` placeholders in a preset URL, filled from ``fields``."""
    fields = fields or {}

    def sub(m: re.Match) -> str:
        v = fields.get(m.group(1))
        return v if isinstance(v, str) and v else m.group(0)

    return _PLACEHOLDER_RX.sub(sub, url)


def validate_base_url(value: Any, *, fields: Mapping[str, Any] | None = None,
                      allow_insecure_http: bool = False,
                      self_hostports: Iterable[tuple[str, int]] = ()) -> str | None:
    """An Anthropic Messages API root (spec section 7.3), normalised.

    https anywhere; http only for loopback, or for an RFC 1918 / ULA address
    with ``allow_insecure_http``. No userinfo, query or fragment; no
    link-local or unspecified address; never the app's own ``host:port``.
    Placeholders are filled from ``fields``; the trailing slash is dropped.
    """
    raw = _text(value, "base_url")
    if raw is None:
        return None
    raw = fill_placeholders(raw, fields)
    left = _PLACEHOLDER_RX.search(raw)
    if left or "{" in raw or "}" in raw:
        name = left.group(1) if left else None
        raise _invalid(f"fields.{name}" if name else "base_url",
                       f"Fill in the {name.replace('_', ' ')} for this endpoint."
                       if name else "Remove the { } from the URL.")
    if any(c.isspace() for c in raw):
        raise _invalid("base_url", "The URL must not contain spaces.")
    if "?" in raw or "#" in raw:
        raise _invalid("base_url", "Use the API root, without ? or # parts.")
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _invalid("base_url", "This isn't a valid URL.") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise _invalid("base_url", "Use an http:// or https:// URL.")
    if "@" in parts.netloc:
        raise _invalid("base_url", "Remove the user name or password from the URL.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise _invalid("base_url", "The URL needs a host name.")
    ip = _ip(host)
    if ip is None:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise _invalid("base_url", "This host name isn't valid.") from None
        if not re.fullmatch(r"[a-z0-9.-]{1,253}", host) or ".." in host:
            raise _invalid("base_url", "This host name isn't valid.")
    elif ip.is_unspecified or ip in (_LINK_LOCAL_V4 if ip.version == 4 else _LINK_LOCAL_V6):
        raise _invalid("base_url", "This address can't be used as an endpoint.")
    if scheme == "http" and not is_loopback_host(host):
        if not _is_private_lan(host):
            raise _invalid("base_url", "Use https:// for an endpoint that isn't on this Mac.")
        if not allow_insecure_http:
            raise _invalid("base_url", "Allow an unencrypted endpoint on your network first.")
    eff_port = port or (443 if scheme == "https" else 80)
    for own_host, own_port in self_hostports:
        same = (host == own_host.strip("[]").lower()
                or (is_loopback_host(host) and is_loopback_host(own_host)))
        if same and eff_port == own_port:
            raise _invalid("base_url", "That's this app's own address.")
    netloc = f"[{ip.compressed}]" if ip is not None and ip.version == 6 else host
    if port:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


# ---------------------------------------------------------------- redaction --

#: Placeholder tokens sent in place of a key to a keyless local server.
PLACEHOLDER_TOKENS = frozenset({"ollama", "lmstudio", "unused"})


class SecretRegistry:
    """Every live secret; ``text`` replaces each run of ``REDACT_MIN_RUN`` or
    more of a secret's characters with ``[redacted key]`` (spec section 9.5).

    A secret shorter than the run length is redacted where it appears whole.
    Each secret is also matched in its JSON-escaped form. Secrets are only
    ever added (a replaced key may still be echoed by a turn that used it).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._grams: frozenset[str] = frozenset()
        self._whole: tuple[str, ...] = ()

    def add(self, secret: Any) -> None:
        if (not isinstance(secret, str) or len(secret) < 8 or secret in PLACEHOLDER_TOKENS
                or secret in self._secrets):
            return
        with self._lock:
            self._secrets.add(secret)
            grams, whole = set(self._grams), set(self._whole)
            for form in {secret, json.dumps(secret)[1:-1], secret.replace("/", "\\/")}:
                if len(form) >= REDACT_MIN_RUN:
                    grams.update(form[i:i + REDACT_MIN_RUN]
                                 for i in range(len(form) - REDACT_MIN_RUN + 1))
                else:
                    whole.add(form)
            self._grams, self._whole = frozenset(grams), tuple(sorted(whole, key=len))

    def __contains__(self, secret: object) -> bool:
        return secret in self._secrets

    @property
    def active(self) -> bool:
        return bool(self._secrets)

    def snapshot(self) -> tuple[str, ...]:
        """The live secrets (for a caller keeping its own redactor)."""
        return tuple(self._secrets)

    def text(self, value: str, extra: Iterable[str] = ()) -> str:
        grams, whole = self._grams, self._whole
        extra = [s for s in extra if isinstance(s, str) and len(s) >= 8
                 and s not in PLACEHOLDER_TOKENS]
        if extra:
            reg = SecretRegistry()
            for s in (*self._secrets, *extra):
                reg.add(s)
            grams, whole = reg._grams, reg._whole
        if not isinstance(value, str) or (not grams and not whole) or len(value) < 8:
            return value
        spans: list[tuple[int, int]] = []
        n = REDACT_MIN_RUN
        if grams:
            for i in range(len(value) - n + 1):
                if value[i:i + n] in grams:
                    spans.append((i, i + n))
        for needle in whole:
            start = value.find(needle)
            while start >= 0:
                spans.append((start, start + len(needle)))
                start = value.find(needle, start + 1)
        if not spans:
            return value
        spans.sort()
        out, pos = [], 0
        cur_a, cur_b = spans[0]
        for a, b in spans[1:]:
            if a <= cur_b:
                cur_b = max(cur_b, b)
                continue
            out += [value[pos:cur_a], REDACTED]
            pos, cur_a, cur_b = cur_b, a, b
        out += [value[pos:cur_a], REDACTED, value[cur_b:]]
        return "".join(out)

    def value(self, value: Any, extra: Iterable[str] = ()) -> Any:
        """``value`` with every string inside it redacted (dicts, lists, tuples)."""
        if isinstance(value, str):
            return self.text(value, extra)
        if isinstance(value, Mapping):
            return {k: self.value(v, extra) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.value(v, extra) for v in value)
        return value


#: One registry per process: every key the settings have read, been sent, or
#: found in the launch environment.
LIVE_SECRETS = SecretRegistry()


def register_secret(secret: Any) -> None:
    LIVE_SECRETS.add(secret)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every live secret (and ``secrets``) redacted."""
    return LIVE_SECRETS.text(text, secrets)


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    return LIVE_SECRETS.value(value, secrets)


class RedactingFilter(logging.Filter):
    """Redacts a record's message, arguments and traceback text.

    Works on a logger (records created on it) or a handler (every record it
    emits). ``install_log_redaction`` puts it on the whole ``godseye_uav``
    tree through the record factory, since a logger's own filters never see
    its children's records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


def redact_record(record: logging.LogRecord) -> None:
    if not LIVE_SECRETS.active or getattr(record, "_godseye_redacted", False):
        return
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a bad format string: leave it to logging
        return
    clean = LIVE_SECRETS.text(message)
    if clean != message:
        record.msg, record.args = clean, None
    if record.exc_info and not record.exc_text:
        with contextlib.suppress(Exception):
            record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = LIVE_SECRETS.text(record.exc_text)
    if record.stack_info:
        record.stack_info = LIVE_SECRETS.text(record.stack_info)
    record._godseye_redacted = True


_REDACTED_TREES = ("godseye_uav", "claude_agent_sdk")
_factory_installed = False
_factory_lock = threading.Lock()


def install_log_redaction(trees: Iterable[str] = _REDACTED_TREES) -> None:
    """Redact every record from the ``godseye_uav`` (and SDK) logger trees.

    Idempotent. Wraps the log-record factory so a record is clean from the
    moment it exists, whichever logger or handler sees it next.
    """
    global _factory_installed
    names = tuple(trees)
    with _factory_lock:
        if _factory_installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            name = record.name or ""
            if LIVE_SECRETS.active and any(name == t or name.startswith(t + ".")
                                           for t in names):
                redact_record(record)
            return record

        logging.setLogRecordFactory(factory)
        _factory_installed = True
    for t in names:
        lg = logging.getLogger(t)
        if not any(isinstance(f, RedactingFilter) for f in lg.filters):
            lg.addFilter(RedactingFilter())


# ------------------------------------------------------------ atomic writes --

def ensure_private_dir(path: pathlib.Path) -> pathlib.Path:
    """``mkdir -p`` then 0700 on the leaf."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def write_private(path: pathlib.Path, text: str) -> None:
    """Atomic 0600 write (``app.write_harness_config`` pattern)."""
    path = pathlib.Path(path)
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if fd != -1:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.replace(tmp, path)


# ------------------------------------------------------------ secret stores --

class MemorySecrets:
    """In-process secrets (tests, or ``GODSEYE_LLM_SECRET_STORE=memory``)."""

    kind = "memory"
    label = "Memory (this run only)"

    def __init__(self) -> None:
        self.path: str | None = None
        self._keys: dict[str, str] = {}

    def get(self, provider_id: str) -> str | None:
        return self._keys.get(provider_id)

    def set(self, provider_id: str, secret: str) -> None:
        self._keys[provider_id] = secret

    def delete(self, provider_id: str) -> None:
        self._keys.pop(provider_id, None)


class FileSecrets:
    """``<dir>/llm-secrets.json``, mode 0600 (the fallback off macOS)."""

    kind = "file"
    label = "A file only you can read"

    def __init__(self, path: pathlib.Path):
        self._path = pathlib.Path(path)
        self.path = str(self._path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return {}
        if st.st_mode & 0o077:
            log.warning("llm secrets file was readable by others; set it to 0600: %s",
                        self._path)
            os.chmod(self._path, 0o600)
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("llm secrets file unreadable; treating it as empty: %s", self._path)
            return {}
        keys = doc.get("keys") if isinstance(doc, dict) else None
        if not isinstance(keys, dict):
            return {}
        return {k: v for k, v in keys.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, keys: dict[str, str]) -> None:
        write_private(self._path, json.dumps({"schema": SECRETS_SCHEMA, "keys": keys},
                                             indent=1) + "\n")

    def get(self, provider_id: str) -> str | None:
        with self._lock:
            return self._load().get(provider_id)

    def set(self, provider_id: str, secret: str) -> None:
        with self._lock:
            keys = self._load()
            keys[provider_id] = secret
            self._save(keys)

    def delete(self, provider_id: str) -> None:
        with self._lock:
            keys = self._load()
            if keys.pop(provider_id, None) is not None:
                self._save(keys)


#: ``runner(argv, stdin_text, timeout_s) -> (returncode, stdout, stderr)``
Runner = Callable[[list[str], str | None, float], tuple[int, str, str]]


def run_security(argv: list[str], stdin: str | None = None,
                 timeout: float = 15.0) -> tuple[int, str, str]:
    """Run ``/usr/bin/security``; the secret travels only on stdin / stdout."""
    p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                       check=False)
    return p.returncode, p.stdout, p.stderr


_NOT_FOUND_RX = re.compile(r"(?i)could not be found|SecKeychainSearchCopyNext")


class KeychainSecrets:
    """macOS Keychain generic passwords through ``/usr/bin/security``.

    Service ``eye-in-the-sky.llm``, account ``<keychain_scope>:<provider id>``.
    Writes go through ``security -i`` with the command on stdin and the
    secret hex-encoded (``-X``), so it is never on any process's argv. Reads
    get the secret on stdout. ``keychain`` pins every command to one keychain
    file (tests use a throwaway one); None means the default search list.
    """

    kind = "keychain"
    label = "macOS Keychain"

    def __init__(self, scope: Callable[[], str], *, runner: Runner | None = None,
                 keychain: str | None = None, security: str = SECURITY_BIN):
        if keychain is not None and (has_ctl(keychain) or any(c in keychain for c in '"\\')):
            raise ValueError("keychain path must not contain quotes or backslashes")
        self._scope = scope
        self._run = runner or run_security
        self._keychain = keychain
        self._security = security
        self.path: str | None = None

    def _account(self, provider_id: str) -> str:
        scope = self._scope()
        if not re.fullmatch(r"[A-Za-z0-9]{4,32}", scope or "") or not re.fullmatch(
                r"[a-z0-9_]+", provider_id):
            raise KeychainError("bad keychain account")
        return f"{scope}:{provider_id}"

    def _tail(self) -> list[str]:
        return [self._keychain] if self._keychain else []

    def get(self, provider_id: str) -> str | None:
        argv = [self._security, "find-generic-password", "-a", self._account(provider_id),
                "-s", KEYCHAIN_SERVICE, "-w", *self._tail()]
        rc, out, err = self._run(argv, None, 15.0)
        if rc == 0:
            value = out.rstrip("\n")
            return value or None
        if rc == 44 or _NOT_FOUND_RX.search(err or ""):
            return None
        raise KeychainError(f"security find-generic-password exited {rc}")

    def set(self, provider_id: str, secret: str) -> None:
        if has_ctl(secret) or not secret:
            raise KeychainError("refusing to store an empty or multi-line secret")
        kc = f' "{self._keychain}"' if self._keychain else ""
        line = (f'add-generic-password -U -a "{self._account(provider_id)}" '
                f'-s "{KEYCHAIN_SERVICE}" -l "{KEYCHAIN_LABEL}" '
                f'-X {secret.encode("utf-8").hex()}{kc}\n')
        rc, _out, _err = self._run([self._security, "-i"], line, 15.0)
        if rc != 0:
            raise KeychainError(f"security add-generic-password exited {rc}")
        # ``security -i`` can exit 0 after a failed command: read it back.
        if self.get(provider_id) != secret:
            raise KeychainError("the keychain did not keep the key")

    def delete(self, provider_id: str) -> None:
        argv = [self._security, "delete-generic-password", "-a", self._account(provider_id),
                "-s", KEYCHAIN_SERVICE, *self._tail()]
        rc, _out, err = self._run(argv, None, 15.0)
        if rc != 0 and rc != 44 and not _NOT_FOUND_RX.search(err or ""):
            raise KeychainError(f"security delete-generic-password exited {rc}")


# ------------------------------------------------------------ settings file --

def _now_ms() -> int:
    return int(time.time() * 1000)


def default_doc() -> dict:
    return {"schema": SCHEMA, "rev": 0, "updated_at_ms": 0, "active": None,
            "keychain_scope": None, "providers": {}}


class SettingsStore:
    """``llm-settings.json``: 0700 directory, 0600 file, atomic writes.

    ``rev`` is the ETag: ``save`` refuses a stale ``expected_rev``. A file
    with a newer schema is opened read-only and never overwritten; a corrupt
    one is renamed ``llm-settings.json.corrupt-<ts>`` and the defaults used.
    """

    def __init__(self, directory: pathlib.Path):
        self.dir = pathlib.Path(directory)
        self.path = self.dir / SETTINGS_FILE
        self.read_only = False
        self.exists = False
        self.error: str | None = None
        self._lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            self.error = None
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self.exists, self.read_only = False, False
                return default_doc()
            except OSError as exc:
                self.exists, self.read_only = True, True
                self.error = f"{type(exc).__name__}"
                log.warning("llm settings unreadable (%s); using defaults read-only",
                            self.error)
                return default_doc()
            self.exists = True
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
            if not isinstance(doc, dict) or not isinstance(doc.get("schema"), str):
                self._quarantine()
                self.exists, self.read_only = False, False
                return default_doc()
            schema = doc["schema"]
            if schema != SCHEMA:
                if schema.startswith(SCHEMA_PREFIX):
                    self.read_only = True
                    log.warning("llm settings were written by a newer version (%s); "
                                "opened read-only", schema[:80])
                    return _normalise(doc)
                self._quarantine()
                self.exists, self.read_only = False, False
                return default_doc()
            self.read_only = False
            return _normalise(doc)

    def _quarantine(self) -> None:
        dest = self.path.with_name(f"{SETTINGS_FILE}.corrupt-{_now_ms()}")
        try:
            os.replace(self.path, dest)
            log.warning("llm settings file was corrupt; moved it aside to %s", dest.name)
        except OSError as exc:
            log.warning("llm settings file was corrupt and could not be moved (%s)",
                        type(exc).__name__)

    def save(self, doc: dict, expected_rev: int) -> dict:
        """Write ``doc`` as ``expected_rev + 1``; 409 when the file moved on."""
        with self._lock:
            current = self.load()
            if self.read_only:
                raise SettingsError(409, "read_only", message=(
                    "These settings were written by a newer version of Eye in the Sky."))
            if int(current.get("rev", 0)) != int(expected_rev):
                raise SettingsError(409, "settings_conflict", rev=int(current.get("rev", 0)))
            out = _normalise(doc)
            out["schema"] = SCHEMA
            out["rev"] = int(expected_rev) + 1
            out["updated_at_ms"] = _now_ms()
            ensure_private_dir(self.dir)
            write_private(self.path, json.dumps(out, indent=1, sort_keys=True) + "\n")
            self.exists = True
            return out


_PROVIDER_KEYS = ("model", "small_model", "base_url", "fields", "auth_scheme",
                  "allow_insecure_http", "thinking", "model_family", "secret", "tested",
                  "ack_non_claude_at_ms", "ack_family")


def _normalise(doc: dict) -> dict:
    """A settings document with the expected types (unknown providers and
    keys dropped, never a secret value)."""
    out = default_doc()
    out["schema"] = doc.get("schema") if isinstance(doc.get("schema"), str) else SCHEMA
    rev = doc.get("rev")
    out["rev"] = rev if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0 else 0
    ts = doc.get("updated_at_ms")
    out["updated_at_ms"] = ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    active = doc.get("active")
    out["active"] = active if active in catalog.PROVIDERS else None
    scope = doc.get("keychain_scope")
    out["keychain_scope"] = (scope if isinstance(scope, str)
                             and re.fullmatch(r"[A-Za-z0-9]{4,32}", scope) else None)
    providers = doc.get("providers")
    if isinstance(providers, dict):
        for pid, cfg in providers.items():
            if pid in catalog.PROVIDERS and isinstance(cfg, dict):
                out["providers"][pid] = {k: cfg[k] for k in _PROVIDER_KEYS if k in cfg}
    return out


# -------------------------------------------------------- launch environment --

def capture_llm_env(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Capture every provider, credential and model variable, then pop it
    (spec section 3.3). Call once at launch, after ``sanitize_env`` and
    before anything spawns the CLI. Blank values are dropped.
    """
    environ = os.environ if environ is None else environ
    captured: dict[str, str] = {}
    for name in CAPTURED_VARS:
        if name in environ:
            value = environ.pop(name)
            if isinstance(value, str) and value.strip():
                captured[name] = value.strip()
                if name in SECRET_VARS:
                    register_secret(captured[name])
    return captured


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EnvChoice:
    """What the launch environment says about the provider (spec section 3.4)."""

    provider: str
    locked: bool = False                 # GODSEYE_LLM_PROVIDER: read-only in the UI
    source: str | None = None            # the variable that decided
    custom_base_url: str | None = None   # ANTHROPIC_BASE_URL with a key: custom, locked
    custom_scheme: str | None = None
    custom_key_var: str | None = None


def infer_env_provider(captured: Mapping[str, str]) -> EnvChoice:
    """Provider from the captured launch environment.

    ``GODSEYE_LLM_PROVIDER``; a truthy ``CLAUDE_CODE_USE_BEDROCK|VERTEX|FOUNDRY``;
    ``ANTHROPIC_BASE_URL`` with a token or key (custom); ``ANTHROPIC_API_KEY``;
    else the Claude login. A base URL without a key is discarded: the CLI
    would send the Claude login to that host.
    """
    base = captured.get("ANTHROPIC_BASE_URL")
    token = captured.get("ANTHROPIC_AUTH_TOKEN")
    api_key = captured.get("ANTHROPIC_API_KEY")
    custom: dict[str, str] = {}
    if base and (token or api_key):
        custom = {"custom_base_url": base,
                  "custom_scheme": "bearer" if token else "x-api-key",
                  "custom_key_var": "ANTHROPIC_AUTH_TOKEN" if token else "ANTHROPIC_API_KEY"}
    elif base:
        log.warning("Ignored ANTHROPIC_BASE_URL without a key: it would send the Claude "
                    "login to that host.")
    named = captured.get(PROVIDER_ENV)
    if named:
        if named in catalog.PROVIDERS:
            return EnvChoice(named, True, PROVIDER_ENV, **custom)
        log.warning("ignoring unknown %s (not a provider id)", PROVIDER_ENV)
    for var, pid in (("CLAUDE_CODE_USE_BEDROCK", "bedrock"),
                     ("CLAUDE_CODE_USE_VERTEX", "vertex"),
                     ("CLAUDE_CODE_USE_FOUNDRY", "foundry")):
        if _truthy(captured.get(var)):
            return EnvChoice(pid, False, var, **custom)
    if custom:
        return EnvChoice("custom", False, "ANTHROPIC_BASE_URL", **custom)
    if api_key:
        return EnvChoice("anthropic_api", False, "ANTHROPIC_API_KEY")
    return EnvChoice(catalog.DEFAULT_PROVIDER, False, None)


# ---------------------------------------------------------- resolved config --

@dataclass(frozen=True)
class ProviderConfig:
    """The effective, non-secret configuration of one provider."""

    provider: str
    model: str | None
    small_model: str | None = None
    base_url: str | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    auth_scheme: str = "bearer"
    thinking: str = "adaptive"            # effective: adaptive | off
    model_family: str = "claude"
    allow_insecure_http: bool = False
    thinking_setting: str = "auto"        # what the operator asked: auto | adaptive | off

    def canonical(self) -> str:
        """The config a check vouches for (the operator's thinking setting,
        not the effective mode a check may have downgraded)."""
        return json.dumps({"provider": self.provider, "model": self.model,
                           "small_model": self.small_model, "base_url": self.base_url,
                           "fields": dict(sorted(self.fields.items())),
                           "auth_scheme": self.auth_scheme, "thinking": self.thinking_setting,
                           "model_family": self.model_family,
                           "allow_insecure_http": self.allow_insecure_http},
                          sort_keys=True, separators=(",", ":"))


def provider_identity(spec: Mapping, base_url: str | None, fields: Mapping[str, str]) -> str:
    """sha256(id|kind|base_url|sorted fields): a change means "fresh session"."""
    blob = "|".join((spec["id"], spec["kind"], base_url or "",
                     json.dumps(sorted(fields.items()), separators=(",", ":"))))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, repr=False)
class ResolvedProvider:
    """Everything ``chat._options()`` takes from the provider (spec section 2).

    ``env`` holds the secret: never log it, never log the options built from
    it. ``repr`` masks every credential value.
    """

    id: str
    label: str
    kind: str
    model_family: str
    host: str | None
    model: str
    thinking: Mapping[str, str]
    effort: str | None
    env: Mapping[str, str]
    cost_basis: str
    identity: str
    generation: int
    ready: bool
    reason: str | None = None
    base_url: str | None = None
    small_model: str | None = None
    key_source: str = "none"
    configured: bool = False
    settings_rev: int = 0

    def __repr__(self) -> str:
        env = {k: ("***" if k in CHILD_SECRET_VARS and v else v) for k, v in self.env.items()}
        return (f"ResolvedProvider(id={self.id!r}, kind={self.kind!r}, model={self.model!r}, "
                f"host={self.host!r}, ready={self.ready}, reason={self.reason!r}, "
                f"generation={self.generation}, env={env!r})")

    __str__ = __repr__

    def summary(self) -> dict:
        """``/chat/status`` ``provider`` block: no key, no key-derived value."""
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "model_family": self.model_family, "host": self.host,
                "key_source": self.key_source, "configured": self.configured}

    def public(self) -> dict:
        """``/app/config`` and the ``session`` event: id and label only."""
        return {"id": self.id, "label": self.label}


def provider_options(rp: ResolvedProvider) -> dict[str, Any]:
    """The only ``ClaudeAgentOptions`` fields that depend on the provider
    (spec section 4.4): ``model``, ``thinking``, ``env`` and, for Claude
    kinds, ``effort``. Every other option must be identical for every provider.
    """
    kw: dict[str, Any] = {"model": rp.model, "thinking": dict(rp.thinking), "env": dict(rp.env)}
    if rp.effort:
        kw["effort"] = rp.effort
    return kw


def key_optional(spec: Mapping, base_url: str | None) -> bool:
    """Whether this provider may run without a stored key."""
    opt = spec["auth"]["key_optional"]
    if opt == "loopback":
        return is_loopback_host(urllib.parse.urlsplit(base_url).hostname if base_url else None)
    return bool(opt)


def _pins(cfg: ProviderConfig) -> dict[str, str]:
    return {"ANTHROPIC_DEFAULT_OPUS_MODEL": cfg.model or "",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": cfg.model or "",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": cfg.small_model or cfg.model or ""}


def _missing_field(spec: Mapping, cfg: ProviderConfig) -> str | None:
    for f in spec["fields"]:
        if f["required"] and not cfg.fields.get(f["id"]):
            return f["id"]
    return None


def build_child_env(spec: Mapping, cfg: ProviderConfig, key: str | None, *,
                    config_dir: pathlib.Path | str | None,
                    captured: Mapping[str, str] | None = None
                    ) -> tuple[dict[str, str], str | None]:
    """The CLI child's provider environment (spec section 4), or a refusal.

    Returns ``(env, None)`` when the provider can run, else ``({}, reason)``
    (``provider_key_missing`` | ``provider_not_configured`` |
    ``settings_error``). Rules, in order: ``BLANK`` for every kind; the Claude
    login adds only a captured ``CLAUDE_CODE_OAUTH_TOKEN`` / ``CLAUDE_CONFIG_DIR``;
    every other kind needs its credential (a key, a keyless-local token, or
    cloud credentials), adds its switch / base URL / pins, then ``HARDEN``
    and ``CLAUDE_CONFIG_DIR=<config_dir>``. ``ANTHROPIC_BASE_URL`` and the
    model pins are never emitted blank.
    """
    captured = captured or {}
    kind = spec["kind"]
    key = key.strip() if isinstance(key, str) and key.strip() else None
    env: dict[str, str] = dict(BLANK)
    if kind == "anthropic_login":
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
            if captured.get(var):
                env[var] = captured[var]
    else:
        if not config_dir:
            return {}, REASON_SETTINGS_ERROR
        if not cfg.model:
            return {}, REASON_NOT_CONFIGURED
        if kind == "anthropic_key":
            if not key:
                return {}, REASON_KEY_MISSING
            env["ANTHROPIC_API_KEY"] = key
        elif kind in catalog.URL_KINDS:
            if not cfg.base_url:
                return {}, REASON_NOT_CONFIGURED
            scheme = cfg.auth_scheme
            token = key if scheme != "none" else None
            if token is None:
                if scheme != "none" and not key_optional(spec, cfg.base_url):
                    return {}, REASON_KEY_MISSING
                token = spec["auth"]["default_token"] or "unused"
            env["ANTHROPIC_API_KEY" if scheme == "x-api-key" else "ANTHROPIC_AUTH_TOKEN"] = token
            env["ANTHROPIC_BASE_URL"] = cfg.base_url
            env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
            env.update(spec["extra_env"])
            env.update(_pins(cfg))
        elif kind in catalog.CLOUD_KINDS:
            if _missing_field(spec, cfg):
                return {}, REASON_NOT_CONFIGURED
            switch = {"bedrock": "CLAUDE_CODE_USE_BEDROCK", "vertex": "CLAUDE_CODE_USE_VERTEX",
                      "foundry": "CLAUDE_CODE_USE_FOUNDRY"}[kind]
            env[switch] = "1"
            for f in spec["fields"]:
                value = cfg.fields.get(f["id"]) or f.get("default")
                if f["env"] and value:
                    env[f["env"]] = value
            if key and kind == "bedrock":
                env["AWS_BEARER_TOKEN_BEDROCK"] = key
            elif key and kind == "foundry":
                env["ANTHROPIC_FOUNDRY_API_KEY"] = key
            env.update(_pins(cfg))
        else:
            return {}, REASON_SETTINGS_ERROR
        env.update(HARDEN_FLAGS)
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if cfg.thinking == "off":
        env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    if any(has_ctl(k) or has_ctl(v) or not isinstance(v, str) for k, v in env.items()):
        return {}, REASON_SETTINGS_ERROR
    for name in ("ANTHROPIC_BASE_URL", *PIN_VARS):
        if name in env and not env[name]:
            return {}, REASON_NOT_CONFIGURED
    return env, None


def display_host(spec: Mapping, cfg: ProviderConfig) -> str | None:
    """Where requests go, for "Requests go to {host}" (never a key)."""
    kind = spec["kind"]
    if kind in ("anthropic_login", "anthropic_key"):
        return "api.anthropic.com"
    if kind in catalog.URL_KINDS:
        return url_host(cfg.base_url)
    if kind == "bedrock":
        region = cfg.fields.get("region")
        return f"bedrock-runtime.{region}.amazonaws.com" if region else "Amazon Bedrock"
    if kind == "vertex":
        region = cfg.fields.get("region") or "global"
        return ("aiplatform.googleapis.com" if region == "global"
                else f"{region}-aiplatform.googleapis.com")
    if kind == "foundry":
        resource = cfg.fields.get("resource")
        return f"{resource}.services.ai.azure.com" if resource else "Microsoft Foundry"
    return None


def thinking_payload(mode: str) -> Mapping[str, str]:
    return DISABLED_THINKING if mode == "off" else ADAPTIVE_THINKING


def _default_claude_model() -> str:
    try:
        from .chat import DEFAULT_MODEL
        return DEFAULT_MODEL
    except Exception:  # noqa: BLE001 - chat is optional for this module
        return FALLBACK_MODEL


_FIRST_PARTY = frozenset({"anthropic_login", "anthropic_key"})
_UNSET = object()
#: Stands in for a stored key when readiness is computed without reading it.
_KEY_PRESENT = "stored-key-not-read"


# ----------------------------------------------------------------- settings --

_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
               "/var/tmp", "/private/var/tmp")


def is_temp_path(path: pathlib.Path | str) -> bool:
    """True for a directory under the system temp roots (``$TMPDIR``,
    ``/tmp``, ``/var/folders``): a throwaway store."""
    try:
        real = os.path.realpath(str(path))
        roots = {os.path.realpath(r) for r in _TEMP_ROOTS}
        roots.add(os.path.realpath(tempfile.gettempdir()))
    except (OSError, ValueError):
        return False
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots)


class LlmSettings:
    """The analyst's provider settings for one host (spec section 2).

    ``settings_dir`` is ``<store_dir.parent>``; ``llm_env`` is what
    ``capture_llm_env`` returned at launch; ``model`` / ``effort`` are the
    ``--model`` / ``--effort`` flags. ``secrets`` forces a secret backend
    (tests); otherwise ``GODSEYE_LLM_SECRET_STORE`` or the platform decides,
    except that a store under the temp roots uses the file backend unless
    ``GODSEYE_LLM_SECRET_STORE=keychain`` asks for the keychain.
    """

    def __init__(self, settings_dir: pathlib.Path | str,
                 llm_env: Mapping[str, str] | None = None, *,
                 store_dir: pathlib.Path | str | None = None,
                 secrets: Any = None, model: str | None = None, effort: str | None = None,
                 environ: Mapping[str, str] | None = None, default_model: str | None = None,
                 app_port: int | None = None, sdk: Any = None, cli_path: str | None = None,
                 platform: str | None = None):
        self.dir = pathlib.Path(settings_dir)
        self.store_dir = pathlib.Path(store_dir) if store_dir else self.dir / "store"
        self.claude_home = self.store_dir / "analyst" / "claude-home"
        self.check_home = self.store_dir / "analyst" / "claude-check"
        self._captured = {k: v for k, v in (llm_env or {}).items()
                          if isinstance(v, str) and v.strip()}
        for name, value in self._captured.items():
            if name in SECRET_VARS:
                register_secret(value)
        self._environ = os.environ if environ is None else environ
        self._model_flag = model or None
        self._effort_flag = effort or None
        self._default_model = default_model
        self.app_port = app_port
        self.sdk = sdk
        self.cli_path = cli_path
        self.store = SettingsStore(self.dir)
        self._lock = threading.RLock()
        self._doc = self.store.load()
        self._env_choice = infer_env_provider(self._captured)
        self._backends, self._primary = self._make_backends(secrets, platform)
        self._key_cache: dict[str, str | None] = {}
        self._generation = 1
        self._check_tokens: dict[str, tuple[str, float, dict]] = {}
        self._testing = False
        self._last_full = 0.0
        self._options_builder: Callable[..., Any] | None = None
        self._options_sdk: Callable[[], Any] | None = None
        install_log_redaction()
        self._fingerprint = self._active_fingerprint()

    # -------------------------------------------------------------- backends --
    def _make_backends(self, forced_backend: Any, platform: str | None
                       ) -> tuple[dict[str, Any], Any]:
        if forced_backend is not None:
            return {forced_backend.kind: forced_backend}, forced_backend
        forced = str(self._environ.get(SECRET_STORE_ENV) or "").strip().lower()
        file_backend = FileSecrets(self.dir / SECRETS_FILE)
        if forced == "memory":
            mem = MemorySecrets()
            return {"memory": mem}, mem
        if forced == "file":
            return {"file": file_backend}, file_backend
        if forced not in ("", "keychain"):
            log.warning("ignoring unknown %s (use keychain, file or memory)", SECRET_STORE_ENV)
        if forced != "keychain" and is_temp_path(self.dir):
            # A throwaway store (tests, harness runs) never writes the login
            # keychain: a headless run would block on its prompt.
            return {"file": file_backend}, file_backend
        if (platform or sys.platform) == "darwin" and os.path.exists(SECURITY_BIN):
            kc = KeychainSecrets(self._keychain_scope)
            return {"keychain": kc, "file": file_backend}, kc
        return {"file": file_backend}, file_backend

    def use_analyst_options(self, chat: Any) -> None:
        """Build the full check's options with the analyst's own builder
        (``ChatService.check_options``, spec section 6.2)."""
        builder = getattr(chat, "check_options", None)
        if callable(builder):
            self._options_builder = builder
            self._options_sdk = getattr(chat, "sdk_module", None)

    def _check_engine(self) -> tuple[Any, Callable[..., Any] | None]:
        """(sdk, options_builder) for a full check."""
        sdk, builder = self.sdk, self._options_builder
        if builder is not None and sdk is None and callable(self._options_sdk):
            try:
                sdk = self._options_sdk()
            except Exception:  # noqa: BLE001 - fall back to a plain import
                sdk = None
            if sdk is None:
                builder = None
        return sdk, builder

    def _keychain_scope(self) -> str:
        return self._doc.get("keychain_scope") or ""

    @property
    def key_store(self) -> dict:
        b = self._primary
        return {"kind": b.kind, "label": b.label, "path": getattr(b, "path", None)}

    # ------------------------------------------------------------ properties --
    @property
    def generation(self) -> int:
        return self._generation

    @property
    def rev(self) -> int:
        return int(self._doc.get("rev", 0))

    def _active_id(self, doc: Mapping) -> str:
        choice = self._env_choice
        if choice.locked:
            return choice.provider
        active = doc.get("active")
        if active in catalog.PROVIDERS:
            return active
        return choice.provider

    def _model_lock(self) -> tuple[str | None, str | None]:
        """(model, source) forced by ``--model`` or ``GODSEYE_CHAT_MODEL``."""
        if self._model_flag:
            return self._model_flag, "--model"
        env_model = str(self._environ.get(MODEL_ENV) or "").strip()
        if env_model:
            return env_model, MODEL_ENV
        return None, None

    def _effort(self) -> str | None:
        raw = self._effort_flag or str(self._environ.get(EFFORT_ENV) or "").strip() or None
        return raw if raw in EFFORT_LEVELS else None

    def _claude_default(self) -> str:
        return self._default_model or _default_claude_model()

    # ------------------------------------------------------ effective config --
    @staticmethod
    def _stored(doc: Mapping, pid: str) -> dict:
        cfg = (doc.get("providers") or {}).get(pid)
        return dict(cfg) if isinstance(cfg, dict) else {}

    def _env_custom(self, pid: str) -> bool:
        return pid == "custom" and bool(self._env_choice.custom_base_url)

    def _field_values(self, spec: Mapping, stored: Mapping) -> dict[str, str]:
        values = stored.get("fields") if isinstance(stored.get("fields"), dict) else {}
        out: dict[str, str] = {}
        for f in spec["fields"]:
            v = values.get(f["id"])
            if not (isinstance(v, str) and v) and f["env"]:
                v = self._captured.get(f["env"]) or self._environ.get(f["env"])
            if not (isinstance(v, str) and v):
                v = f.get("default")
            if isinstance(v, str) and v and not has_ctl(v):
                out[f["id"]] = v
        return out

    def _effective(self, pid: str, doc: Mapping, overrides: Mapping | None = None
                   ) -> ProviderConfig:
        spec = catalog.get(pid)
        stored = self._stored(doc, pid)
        if overrides:
            stored.update(overrides)
        kind = spec["kind"]
        models = spec["models"]
        default_model = models["default"] or (self._claude_default()
                                               if spec["model_family"] == "claude"
                                               and kind in _FIRST_PARTY else None)
        model = stored.get("model") if isinstance(stored.get("model"), str) else None
        if kind in _FIRST_PARTY:
            model = self._model_lock()[0] or model
        if self._env_custom(pid):
            model = model or self._captured.get("ANTHROPIC_MODEL")
        model = model or default_model
        small = stored.get("small_model") if isinstance(stored.get("small_model"), str) else None
        if not small:
            small = models["small_default"] if model == models["default"] else None
        fields = self._field_values(spec, stored)
        base_url = None
        bspec = spec["base_url"]
        if self._env_custom(pid):
            base_url = self._env_base_url()
        elif bspec is not None and kind in catalog.URL_KINDS:
            want = stored.get("base_url") if isinstance(stored.get("base_url"), str) else None
            presets = {fill_placeholders(p["url"], fields) for p in bspec["presets"]}
            if bspec["editable"]:
                base_url = want or bspec["default"]
            elif want and want in presets:
                base_url = want
            else:
                base_url = bspec["default"]
            if base_url and _PLACEHOLDER_RX.search(fill_placeholders(base_url, fields)):
                base_url = None
            elif base_url:
                base_url = fill_placeholders(base_url, fields)
        auth = spec["auth"]
        scheme = auth["scheme"]
        if self._env_custom(pid):
            scheme = self._env_choice.custom_scheme or scheme
        elif stored.get("auth_scheme") in auth["schemes"]:
            scheme = stored["auth_scheme"]
        family = spec["model_family"]
        if pid == "custom" and stored.get("model_family") in ("claude", "non_claude"):
            family = stored["model_family"]
        return ProviderConfig(
            provider=pid, model=model, small_model=small, base_url=base_url,
            fields=MappingProxyType(fields), auth_scheme=scheme,
            thinking=self._thinking_mode(spec, stored, model), model_family=family,
            allow_insecure_http=bool(stored.get("allow_insecure_http")),
            thinking_setting=stored.get("thinking") if stored.get("thinking")
            in THINKING_SETTINGS else "auto")

    def _env_base_url(self) -> str | None:
        try:
            return validate_base_url(self._env_choice.custom_base_url, allow_insecure_http=True)
        except SettingsError:
            log.warning("ignoring the launch ANTHROPIC_BASE_URL: not a usable endpoint")
            return None

    @staticmethod
    def _thinking_mode(spec: Mapping, stored: Mapping, model: str | None) -> str:
        """Effective thinking: ``adaptive`` or ``off`` (spec sections 0.6, 6.2)."""
        want = stored.get("thinking") if stored.get("thinking") in THINKING_SETTINGS else "auto"
        if want == "off" or spec["thinking"] == "off":
            return "off"
        tested = stored.get("tested") if isinstance(stored.get("tested"), dict) else {}
        if (spec["thinking"] == "auto" and tested.get("thinking_detected") == "off"
                and tested.get("model") == model):
            return "off"
        return "adaptive"

    # ------------------------------------------------------------------ keys --
    def _env_key(self, pid: str) -> tuple[str | None, str | None]:
        """(key, variable) when the launch environment supplies this key."""
        choice = self._env_choice
        if self._env_custom(pid) and choice.custom_key_var:
            return self._captured.get(choice.custom_key_var), choice.custom_key_var
        for var in catalog.get(pid)["auth"]["env_override"]:
            if var == choice.custom_key_var and choice.custom_base_url:
                continue  # that key belongs to the environment's custom endpoint
            if self._captured.get(var):
                return self._captured[var], var
        return None, None

    def _secret_meta(self, pid: str, doc: Mapping) -> dict | None:
        meta = self._stored(doc, pid).get("secret")
        if isinstance(meta, dict) and meta.get("store") in self._backends:
            return meta
        return None

    def _key_state(self, pid: str, doc: Mapping) -> dict:
        """``{configured, source, masked, env}`` without touching the keychain."""
        spec = catalog.get(pid)
        kind = spec["kind"]
        value, var = self._env_key(pid)
        if value:
            return {"configured": True, "source": "environment",
                    "masked": "…" + value[-4:] if kind != "anthropic_login" else None,
                    "env": var}
        if kind == "anthropic_login":
            return {"configured": True, "source": "login", "masked": None, "env": None}
        meta = self._secret_meta(pid, doc)
        if meta:
            last4 = meta.get("last4") if isinstance(meta.get("last4"), str) else ""
            return {"configured": True, "source": meta["store"],
                    "masked": "…" + last4[-4:] if last4 else "…", "env": None}
        source = "cloud" if kind in catalog.CLOUD_KINDS else "none"
        return {"configured": False, "source": source, "masked": None, "env": None}

    def _read_key(self, pid: str, doc: Mapping) -> str | None:
        """The key for ``pid`` (may run ``security``; cached after the first read)."""
        value, _var = self._env_key(pid)
        if value:
            return value
        if catalog.get(pid)["kind"] == "anthropic_login":
            return None
        meta = self._secret_meta(pid, doc)
        if not meta:
            return None
        cache_key = f"{meta['store']}:{pid}"
        if cache_key in self._key_cache:
            return self._key_cache[cache_key]
        try:
            key = self._backends[meta["store"]].get(pid)
        except (KeychainError, OSError, subprocess.SubprocessError) as exc:
            log.warning("could not read the %s key from the %s (%s)", pid, meta["store"],
                        type(exc).__name__)
            return None
        if key:
            register_secret(key)
        self._key_cache[cache_key] = key
        return key

    def _forget_key(self, pid: str) -> None:
        for k in [k for k in self._key_cache if k.endswith(f":{pid}")]:
            del self._key_cache[k]

    # --------------------------------------------------------------- resolve --
    def _resolve(self, pid: str, doc: Mapping, *, overrides: Mapping | None = None,
                 key: Any = _UNSET, lookup: bool = True) -> ResolvedProvider:
        """Resolve ``pid``. ``lookup=False`` never reads a stored key: the
        readiness is computed from the key's metadata and ``env`` is empty."""
        spec = catalog.get(pid)
        kind = spec["kind"]
        cfg = self._effective(pid, doc, overrides)
        state = self._key_state(pid, doc)
        stored_config = overrides is None and key is _UNSET  # not a check or an activation
        if key is _UNSET:
            if lookup:
                key = self._read_key(pid, doc)
            else:
                key = self._env_key(pid)[0] or (_KEY_PRESENT if state["configured"] else None)
        env, reason = build_child_env(
            spec, cfg, key, captured=self._captured,
            config_dir=None if kind == "anthropic_login" else self.claude_home)
        if reason is None and stored_config and self._ack_missing(pid, doc, spec, cfg):
            env, reason = {}, REASON_NOT_CONFIGURED
        if reason is None and lookup and kind != "anthropic_login":
            try:
                ensure_private_dir(self.claude_home)
            except OSError:
                env, reason = {}, REASON_SETTINGS_ERROR
        if reason == REASON_KEY_MISSING and state["configured"] and lookup:
            state = {**state, "configured": False}
        return ResolvedProvider(
            id=pid, label=spec["label"], kind=kind, model_family=cfg.model_family,
            host=display_host(spec, cfg), model=cfg.model or "",
            thinking=thinking_payload(cfg.thinking),
            effort=self._effort() if kind in catalog.EFFORT_KINDS else None,
            env=MappingProxyType(dict(env) if lookup else {}),
            cost_basis=spec["cost_basis"],
            identity=provider_identity(spec, cfg.base_url, cfg.fields),
            generation=self._generation, ready=reason is None, reason=reason,
            base_url=cfg.base_url, small_model=cfg.small_model,
            key_source=state["source"], configured=reason is None or state["configured"],
            settings_rev=int(doc.get("rev", 0)))

    def _ack_missing(self, pid: str, doc: Mapping, spec: Mapping, cfg: ProviderConfig) -> bool:
        """The provider put in use from settings runs a model that needs the
        non-Claude acknowledgement and the stored one doesn't cover it (a
        hand-edited or stale settings file): fail closed (spec 9.11)."""
        if pid != doc.get("active") or self._env_choice.locked or not self.needs_ack(spec, cfg):
            return False
        stored = self._stored(doc, pid)
        return not (stored.get("ack_non_claude_at_ms")
                    and stored.get("ack_family") == cfg.model_family)

    def _unresolved(self) -> ResolvedProvider:
        return ResolvedProvider(
            id="unknown", label="the model provider", kind="unknown", model_family="claude",
            host=None, model="", thinking=ADAPTIVE_THINKING, effort=None,
            env=MappingProxyType({}), cost_basis="unreliable", identity="", generation=-1,
            ready=False, reason=REASON_SETTINGS_ERROR, settings_rev=self.rev)

    def resolve(self) -> ResolvedProvider:
        """The active provider, ready to spawn with (or ``ready=False`` and a
        reason). May read the keychain once per key; ``aresolve`` does that
        off the event loop. Never raises."""
        try:
            with self._lock:
                if self.store.error:
                    return self._unresolved()
                return self._resolve(self._active_id(self._doc), self._doc)
        except Exception as exc:  # noqa: BLE001 - a settings bug must not kill the analyst
            log.warning("could not resolve the analyst's provider (%s)", type(exc).__name__)
            return self._unresolved()

    async def aresolve(self) -> ResolvedProvider:
        return await asyncio.to_thread(self.resolve)

    def status(self) -> dict:
        """The ``/chat/status`` additions (spec section 8). Never reads a key."""
        try:
            with self._lock:
                rp = self._resolve(self._active_id(self._doc), self._doc, lookup=False)
        except Exception:  # noqa: BLE001
            rp = self._unresolved()
        out = {"provider": rp.summary(), "cost_basis": rp.cost_basis,
               "settings_rev": rp.settings_rev, "ready": rp.ready, "model": rp.model}
        if rp.reason:
            out["reason"] = rp.reason
        if rp.effort:
            out["effort"] = rp.effort
        return out

    async def preflight(self, rp: ResolvedProvider) -> dict | None:
        """Called by the analyst before it spawns a CLI for ``rp``: a
        ``{code, status, message, hint}`` refusal when the endpoint now
        redirects (the CLI would re-send the key to the new host), else None.
        The full check refuses such an endpoint too; this catches one that
        starts redirecting after it was checked."""
        return await redirect_refusal(rp)

    def public(self) -> dict:
        """``/app/config`` ``chat.provider``: ``{id, label}`` only."""
        pid = self._active_id(self._doc)
        return {"id": pid, "label": catalog.get(pid)["label"]}

    def live_secrets(self) -> tuple[str, ...]:
        """Every key this process knows (for another module's redactor)."""
        return LIVE_SECRETS.snapshot()

    def redact(self, text: str) -> str:
        return redact(text)

    # ------------------------------------------------------------ generation --
    def _active_fingerprint(self) -> str:
        """Changes whenever the active provider's spawn config changes; never
        reads a key (the key's saved time and last four stand in for it)."""
        doc = self._doc
        pid = self._active_id(doc)
        try:
            cfg = self._effective(pid, doc)
        except Exception:  # noqa: BLE001
            return f"error:{pid}"
        meta = self._secret_meta(pid, doc) or {}
        env_key = self._env_key(pid)[1]
        blob = "|".join((pid, cfg.canonical(), str(meta.get("saved_at_ms")),
                         str(meta.get("store")), str(env_key), str(self._effort())))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _refresh_generation(self) -> None:
        fp = self._active_fingerprint()
        if fp != self._fingerprint:
            self._fingerprint = fp
            self._generation += 1

    # ----------------------------------------------------------------- views --
    @staticmethod
    def needs_ack(spec: Mapping, cfg: ProviderConfig) -> bool:
        """Non-Claude acknowledgement needed (spec sections 7.2, 9.11)."""
        if cfg.model_family == "non_claude":
            return True
        if cfg.model_family == "mixed":
            prefixes = tuple(spec.get("claude_prefixes") or ())
            return not (cfg.model or "").startswith(prefixes) if prefixes else True
        return False

    def _config_marker(self, pid: str, doc: Mapping, cfg: ProviderConfig) -> str:
        """Non-secret fingerprint of a tested config: the key's saved time and
        last four stand in for the key itself."""
        meta = self._secret_meta(pid, doc) or {}
        blob = "|".join((cfg.canonical(), str(meta.get("saved_at_ms")),
                         str(meta.get("last4")), str(self._env_key(pid)[1])))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def _tested_view(self, pid: str, doc: Mapping, cfg: ProviderConfig) -> dict | None:
        tested = self._stored(doc, pid).get("tested")
        if not isinstance(tested, dict):
            return None
        out = {k: tested.get(k) for k in ("depth", "ok", "at_ms", "thinking_detected", "code")
               if k in tested}
        out["current"] = tested.get("config") == self._config_marker(pid, doc, cfg)
        return out

    def _provider_view(self, pid: str, doc: Mapping, active: str) -> dict:
        spec = catalog.get(pid)
        kind = spec["kind"]
        stored = self._stored(doc, pid)
        cfg = self._effective(pid, doc)
        state = self._key_state(pid, doc)
        rp = self._resolve(pid, doc, lookup=False)
        tested = self._tested_view(pid, doc, cfg)
        bspec = spec["base_url"]
        base_view = None
        if bspec is not None and kind in catalog.URL_KINDS:
            env_locked = self._env_custom(pid)
            base_view = {
                "value": cfg.base_url or stored.get("base_url") or bspec["default"],
                "default": bspec["default"], "editable": bool(bspec["editable"]) and not env_locked,
                "required": bool(bspec["required"]),
                "presets": [dict(p) for p in bspec["presets"]],
                "env": "ANTHROPIC_BASE_URL" if env_locked else None}
        if pid == active and rp.ready:
            status = "active"
        elif state["source"] == "environment":
            status = "from_environment"
        elif not rp.ready:
            status = "not_configured"
        elif kind == "anthropic_login" or (tested and tested.get("current") and tested.get("ok")):
            status = "ready"
        elif tested:
            status = "needs_check"
        elif state["source"] in ("keychain", "file", "memory"):
            status = "key_saved"
        else:
            status = "needs_check"
        models = spec["models"]
        qc = spec["quick_check"]
        ack_at = stored.get("ack_non_claude_at_ms")
        if stored.get("ack_family") != cfg.model_family:
            ack_at = None
        auth = spec["auth"]
        values = stored.get("fields") if isinstance(stored.get("fields"), dict) else {}
        locked_model = self._model_lock()[0] if kind in _FIRST_PARTY else None
        return {
            "id": pid, "label": spec["label"], "kind": kind, "group": spec["group"],
            "model_family": cfg.model_family, "claude_prefixes": list(
                spec.get("claude_prefixes") or ()),
            "base_url": base_view, "host": rp.host, "model": cfg.model,
            "small_model": stored.get("small_model") if "small_model" in stored
            else None, "small_model_effective": cfg.small_model,
            "thinking": stored.get("thinking") if stored.get("thinking") in THINKING_SETTINGS
            else spec["thinking"], "thinking_effective": cfg.thinking,
            "models": {"default": models["default"] or (
                self._claude_default() if kind in _FIRST_PARTY else None),
                "small_default": models["small_default"],
                "suggestions": list(models["suggestions"]),
                "placeholder": models["placeholder"]},
            "fields": [dict(f) for f in spec["fields"]],
            "values": {k: v for k, v in values.items() if isinstance(v, str)},
            "auth": {"scheme": auth["scheme"], "schemes": list(auth["schemes"]),
                     "key_label": auth["key_label"], "key_optional": auth["key_optional"],
                     "key_needed": kind not in ("anthropic_login", *catalog.CLOUD_KINDS)
                     and cfg.auth_scheme != "none" and not key_optional(spec, cfg.base_url)},
            "auth_scheme": cfg.auth_scheme,
            "allow_insecure_http": cfg.allow_insecure_http,
            "ack_non_claude_at_ms": ack_at, "needs_ack": self.needs_ack(spec, cfg),
            "key": state, "status": status, "ready": rp.ready, "reason": rp.reason,
            "tested": tested, "notes": list(spec["notes"]), "docs_url": spec["docs_url"],
            "docs": [dict(d) for d in spec["docs"]],
            "quick_check": qc is not None,
            "quick_check_billable": bool(qc and qc["billable"]),
            "cost_basis": spec["cost_basis"], "effort": bool(spec["effort"]),
            "locked": {"model": locked_model is not None,
                       "base_url": self._env_custom(pid),
                       "key": state["source"] == "environment"},
        }

    def view(self) -> dict:
        """``GET /settings/llm``: every provider, keys masked (spec section 7.1)."""
        with self._lock:
            doc = self._doc
            active = self._active_id(doc)
            providers = [self._provider_view(pid, doc, active) for pid in catalog.PROVIDER_IDS]
            choice = self._env_choice
            lock_model, lock_source = self._model_lock()
            read_only = self.store.read_only
            return {
                "schema": SCHEMA, "rev": int(doc.get("rev", 0)), "active": active,
                "read_only": read_only,
                "notice": ("These settings were written by a newer version of Eye in the Sky."
                           if read_only and not self.store.error else None),
                "locks": {"provider": choice.provider if choice.locked else None,
                          "provider_env": PROVIDER_ENV if choice.locked else None,
                          "model": lock_model, "model_source": lock_source},
                "key_store": self.key_store,
                "providers": providers,
            }

    # ---------------------------------------------------------------- writes --
    def _self_hostports(self) -> list[tuple[str, int]]:
        if not self.app_port:
            return []
        return [(h, int(self.app_port)) for h in ("127.0.0.1", "localhost", "::1")]

    def _parse_config(self, pid: str, body: Mapping, doc: Mapping) -> dict:
        """Validated provider overrides from a PUT or test body (spec 7.3)."""
        spec = catalog.get(pid)
        stored = self._stored(doc, pid)
        ov: dict[str, Any] = {}
        if "model" in body:
            ov["model"] = validate_model(body.get("model"))
        if "small_model" in body:
            ov["small_model"] = validate_model(body.get("small_model"), "small_model")
        if "allow_insecure_http" in body:
            if not isinstance(body["allow_insecure_http"], bool):
                raise _invalid("allow_insecure_http", "Must be true or false.")
            ov["allow_insecure_http"] = body["allow_insecure_http"]
        if body.get("fields") is not None:
            raw = body["fields"]
            if not isinstance(raw, dict):
                raise _invalid("fields", "Must be an object.")
            by_id = {f["id"]: f for f in spec["fields"]}
            fields = {}
            for fid, value in raw.items():
                if fid not in by_id:
                    raise _invalid(f"fields.{fid}", "Not a setting of this provider.")
                clean = validate_field(by_id[fid], value)
                if clean is not None:
                    fields[fid] = clean
            ov["fields"] = fields
        if body.get("auth_scheme") is not None:
            if body["auth_scheme"] not in spec["auth"]["schemes"]:
                raise _invalid("auth_scheme", "Pick Bearer token, x-api-key header or None.")
            ov["auth_scheme"] = body["auth_scheme"]
        if "thinking" in body and body["thinking"] is not None:
            if body["thinking"] not in THINKING_SETTINGS:
                raise _invalid("thinking", "Pick Auto or Off.")
            ov["thinking"] = body["thinking"]
        if body.get("model_family") is not None:
            if pid != "custom" or body["model_family"] not in ("claude", "non_claude"):
                raise _invalid("model_family", "Only a custom endpoint can declare this.")
            ov["model_family"] = body["model_family"]
        bspec = spec["base_url"]
        if ("base_url" in body and bspec is not None and spec["kind"] in catalog.URL_KINDS
                and not self._env_custom(pid)):
            fields = {**(stored.get("fields") or {}), **ov.get("fields", {})}
            insecure = ov.get("allow_insecure_http", bool(stored.get("allow_insecure_http")))
            url = validate_base_url(body.get("base_url"), fields=fields,
                                    allow_insecure_http=insecure,
                                    self_hostports=self._self_hostports())
            if not bspec["editable"] and url is not None:
                allowed = {validate_base_url(p["url"]) for p in bspec["presets"]
                           if not _PLACEHOLDER_RX.search(p["url"])}
                allowed.add(bspec["default"])
                if url not in allowed:
                    raise _invalid("base_url", "Pick one of the listed endpoints.")
            if bspec["editable"] and bspec["required"] and url is None and bspec["default"] is None:
                raise _invalid("base_url", "Enter the endpoint URL.")
            ov["base_url"] = url
        return ov

    @staticmethod
    def _parse_if_match(value: Any) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        text = text.removeprefix("W/")
        text = text.strip('"')
        return int(text) if text.isdigit() else None

    def _check_hash(self, cfg: ProviderConfig, key: str | None) -> str:
        return hashlib.sha256(f"{cfg.canonical()}|{key or ''}".encode()).hexdigest()

    def _write_key(self, pid: str, key: str) -> str:
        """Store ``key``; returns the backend kind used (keychain, else file).

        Then removes the provider's key from every other backend: a key that
        once fell back to the file (keychain locked) must not outlive a later
        keychain save, nor the other way round (secrets review)."""
        primary = self._primary
        try:
            primary.set(pid, key)
            used = primary
        except (KeychainError, OSError, subprocess.SubprocessError) as exc:
            fallback = self._backends.get("file")
            if fallback is None or fallback is primary:
                raise SettingsError(500, "key_store_failed",
                                    message="The key couldn't be saved.") from exc
            log.warning("keychain write failed (%s); saving the key to %s instead",
                        type(exc).__name__, fallback.path)
            fallback.set(pid, key)
            used = fallback
        failed = self._sweep_key(pid, skip=used.kind)
        if failed:
            # The new key is saved and in use; the old copy is reported and
            # "Remove key" retries it (and says so if it still can't).
            log.warning("an older %s key is still in the %s", pid, ", ".join(failed))
        return used.kind

    def _sweep_key(self, pid: str, *, skip: str | None = None) -> list[str]:
        """Delete ``pid``'s key from every backend but ``skip`` (not found is
        fine). Returns the backends that failed."""
        failed = []
        for kind, backend in self._backends.items():
            if kind == skip or (kind == "keychain" and not self._keychain_scope()):
                continue  # no scope yet: this settings dir never wrote the keychain
            try:
                backend.delete(pid)
            except (KeychainError, OSError, subprocess.SubprocessError) as exc:
                log.warning("could not delete the %s key from the %s (%s)", pid, kind,
                            type(exc).__name__)
                failed.append(kind)
        return failed

    def _delete_stored_key(self, pid: str) -> None:
        """Remove ``pid``'s key from EVERY backend, whatever the metadata
        names (an older fallback copy, or residue from an earlier version).
        Refuses (500) unless every copy is gone: "Remove key" never reports a
        key gone while a copy is left."""
        self._forget_key(pid)
        if self._sweep_key(pid):
            raise SettingsError(500, "key_store_failed", message="The key couldn't be removed.")

    def _begin_write(self, if_match: Any) -> dict:
        doc = self.store.load()
        if self.store.read_only:
            raise SettingsError(409, "read_only", message=(
                "These settings were written by a newer version of Eye in the Sky."))
        expected = self._parse_if_match(if_match)
        rev = int(doc.get("rev", 0))
        if expected is None:
            raise SettingsError(428, "if_match_required", rev=rev)
        if expected != rev:
            raise SettingsError(409, "settings_conflict", rev=rev)
        self._doc = doc
        return json.loads(json.dumps(doc))

    def put(self, body: Any, if_match: Any) -> dict:
        """``PUT /settings/llm`` (spec section 7.2). The key is write-only.

        Everything is validated before anything is stored; activation needs
        a ready provider, a matching ``check_token`` (not for the login) and,
        for a non-Claude model, the acknowledgement.
        """
        if not isinstance(body, dict):
            raise _invalid(None, "The request body must be a JSON object.")
        pid = body.get("provider")
        if pid not in catalog.PROVIDERS:
            raise _invalid("provider", "Pick a provider from the list.")
        spec = catalog.get(pid)
        kind = spec["kind"]
        activate = body.get("activate") is True
        with self._lock:
            doc = self._begin_write(if_match)
            choice = self._env_choice
            if activate and choice.locked and pid != choice.provider:
                raise SettingsError(409, "locked_by_environment", field="provider",
                                    env=PROVIDER_ENV)
            overrides = self._parse_config(pid, body, doc)
            lock_model, lock_source = self._model_lock()
            if (kind in _FIRST_PARTY and lock_model
                    and overrides.get("model") not in (None, lock_model)):
                raise SettingsError(409, "locked_by_environment", field="model",
                                    env=lock_source)
            kb = body.get("key") if body.get("key") is not None else {"action": "keep"}
            if not isinstance(kb, dict) or kb.get("action", "keep") not in ("keep", "set",
                                                                             "clear"):
                raise _invalid("key", "Key action must be keep, set or clear.")
            action = kb.get("action", "keep")
            env_var = self._env_key(pid)[1]
            if action in ("set", "clear") and env_var:
                raise SettingsError(409, "locked_by_environment", field="key", env=env_var)
            if action == "set" and not spec["auth"]["key_label"]:
                raise _invalid("key", f"{spec['label']} doesn't use a key saved here.")
            new_key = validate_key(kb.get("value")) if action == "set" else None
            if new_key:
                register_secret(new_key)
            now = _now_ms()
            entry = self._stored(doc, pid)
            entry.update(overrides)
            if action == "set":
                entry["secret"] = {"store": self._primary.kind, "last4": new_key[-4:],
                                   "saved_at_ms": now}
            elif action == "clear":
                entry.pop("secret", None)
            doc["providers"][pid] = entry
            if not doc.get("keychain_scope"):
                doc["keychain_scope"] = _secrets.token_hex(4)
            cfg = self._effective(pid, doc)
            if body.get("acknowledge_non_claude") is True and self.needs_ack(spec, cfg):
                # Only for a model that needs it: an ack sent with a Claude
                # model must not cover a later non-Claude one (spec 9.11).
                entry["ack_non_claude_at_ms"] = now
                entry["ack_family"] = cfg.model_family
            if action == "set":
                key_now = new_key
            elif action == "clear":
                key_now = None
            else:
                key_now = self._read_key(pid, self._doc)
            token = body.get("check_token")
            record = self._check_tokens.get(token) if isinstance(token, str) else None
            token_ok = bool(record and record[1] > time.monotonic()
                            and record[0] == self._check_hash(cfg, key_now))
            if token_ok:
                entry["tested"] = {**record[2], "config": self._config_marker(pid, doc, cfg)}
                cfg = self._effective(pid, doc)
            acked = bool(entry.get("ack_non_claude_at_ms")
                         and entry.get("ack_family") == cfg.model_family)
            if activate:
                rp = self._resolve(pid, doc, key=key_now)
                if not rp.ready:
                    raise self._not_ready_error(spec, cfg, rp.reason)
                if kind != "anthropic_login" and not token_ok:
                    raise SettingsError(409, "needs_check", message=(
                        f"Run the check with {spec['label']} before using it."))
                if self.needs_ack(spec, cfg) and not acked:
                    raise SettingsError(409, "needs_ack", message=(
                        "Confirm you understand this isn't a Claude model."))
                doc["active"] = pid
            elif kind != "anthropic_login" and self._active_id(self._doc) == pid:
                # Save never activates (spec 10), yet the provider in use
                # reads this same entry: a new endpoint, model, family or key
                # would reach the live analyst unchecked and unacknowledged.
                # Only a checked (and, for a non-Claude model, acknowledged)
                # change is saved; clearing the key just stops the provider.
                before = self._effective(pid, self._doc)
                if action == "set" or before.canonical() != cfg.canonical():
                    if not token_ok:
                        raise SettingsError(409, "needs_check", message=(
                            f"{spec['label']} is in use. Use {spec['label']} to check this "
                            "change and apply it."))
                    if self.needs_ack(spec, cfg) and not acked:
                        raise SettingsError(409, "needs_ack", message=(
                            "Confirm you understand this isn't a Claude model."))
            # Validated: now store the key, then the settings.
            self._doc = {**self._doc, "keychain_scope": doc["keychain_scope"]}
            if action == "set":
                entry["secret"]["store"] = self._write_key(pid, new_key)
                self._forget_key(pid)
                self._key_cache[f"{entry['secret']['store']}:{pid}"] = new_key
            elif action == "clear":
                self._delete_stored_key(pid)
            if token_ok:
                self._check_tokens.pop(token, None)
            self._doc = self.store.save(doc, int(doc.get("rev", 0)))
            self._refresh_generation()
            return self.view()

    def _not_ready_error(self, spec: Mapping, cfg: ProviderConfig,
                         reason: str | None) -> SettingsError:
        label = spec["label"]
        if reason == REASON_KEY_MISSING:
            return _invalid("key", f"Add a key for {label} first.")
        if spec["kind"] in catalog.URL_KINDS and not cfg.base_url:
            return _invalid("base_url", "Enter the endpoint URL.")
        if not cfg.model:
            return _invalid("model", "Type a model name.")
        missing = _missing_field(spec, cfg)
        if missing:
            return _invalid(f"fields.{missing}", "This is required.")
        return _invalid(None, f"{label} isn't set up.")

    def delete_key(self, pid: str, if_match: Any) -> dict:
        """``DELETE /settings/llm/providers/{id}/key`` (spec section 7.4)."""
        if pid not in catalog.PROVIDERS:
            raise SettingsError(404, "unknown_provider")
        with self._lock:
            doc = self._begin_write(if_match)
            env_var = self._env_key(pid)[1]
            if env_var:
                raise SettingsError(409, "locked_by_environment", field="key", env=env_var)
            entry = self._stored(doc, pid)
            self._delete_stored_key(pid)
            entry.pop("secret", None)
            entry.pop("tested", None)
            doc["providers"][pid] = entry
            self._doc = self.store.save(doc, int(doc.get("rev", 0)))
            self._refresh_generation()
            return self.view()

    # ------------------------------------------------------------------ test --
    async def test(self, body: Any, *, on_connect: Callable[[Any], Any] | None = None) -> dict:
        """``POST /settings/llm/test`` (spec section 6). Never writes settings;
        a passing full check returns a ``check_token`` the next PUT redeems.
        One test at a time (429 ``test_busy``); full checks 10 s apart."""
        if not isinstance(body, dict):
            raise _invalid(None, "The request body must be a JSON object.")
        pid = body.get("provider")
        if pid not in catalog.PROVIDERS:
            raise _invalid("provider", "Pick a provider from the list.")
        depth = body.get("depth", "quick")
        if depth not in ("quick", "full"):
            raise _invalid("depth", "Use quick or full.")
        spec = catalog.get(pid)
        if depth == "quick" and spec["quick_check"] is None:
            raise _invalid("depth", f"Only the full check is available for {spec['label']}.")
        if self._testing:
            raise SettingsError(429, "test_busy", message="A check is already running.")
        wait = FULL_COOLDOWN_S - (time.monotonic() - self._last_full)
        if depth == "full" and wait > 0:
            raise SettingsError(429, "test_busy", message="Wait a few seconds between checks.",
                                retry_after_s=round(wait, 1))
        key = validate_key(body["key"]) if body.get("key") not in (None, "") else None
        if key:
            register_secret(key)
        self._testing = True
        try:
            with self._lock:
                doc = self._doc
                overrides = self._parse_config(pid, body, doc)
            if key is None:
                key = await asyncio.to_thread(self._read_key, pid, doc)
            with self._lock:
                cfg = self._effective(pid, doc, overrides)
                rp = self._resolve(pid, doc, overrides=overrides, key=key)
            secrets = [key] if key else []
            if not rp.ready:
                res = _result(depth, host=rp.host, thinking=cfg.thinking, model=cfg.model)
                message, hint = _config_copy(rp.reason, spec["label"])
                res.update(code="config", message=message, hint=hint, reason=rp.reason)
                return res
            if depth == "quick":
                res = await asyncio.wait_for(
                    asyncio.to_thread(quick_probe, spec, cfg, key, timeout=QUICK_TIMEOUT_S),
                    QUICK_TIMEOUT_S + 5)
            else:
                self._last_full = time.monotonic()
                refusal = await redirect_refusal(rp)
                if refusal:
                    # The engine would follow it and send the key on: fail
                    # like the quick check, and mint no check_token.
                    res = _result(depth, host=rp.host, thinking=cfg.thinking, model=cfg.model)
                    return _fail(res, "redirect", label=spec["label"],
                                 status=refusal["status"], detail="", secrets=secrets)
                sdk, builder = self._check_engine()
                res = await full_check(
                    rp, spec=spec, cfg=cfg, check_home=self.check_home, sdk=sdk,
                    cli_path=self.cli_path, key=key, options_builder=builder,
                    allow_downgrade=(spec["thinking"] == "auto"
                                     and cfg.thinking_setting == "auto"),
                    on_connect=on_connect)
                self._last_full = time.monotonic()
                if res.get("ok"):
                    token = _secrets.token_urlsafe(24)
                    now = time.monotonic()
                    self._check_tokens = {t: r for t, r in self._check_tokens.items()
                                          if r[1] > now}
                    tested = {"depth": "full", "ok": True, "at_ms": res["checked_at_ms"],
                              "thinking_detected": res.get("thinking_detected"),
                              "model": cfg.model}
                    self._check_tokens[token] = (self._check_hash(cfg, key),
                                                 now + CHECK_TOKEN_TTL_S, tested)
                    res["check_token"] = token
            return redact_value(res, secrets)
        except TimeoutError:
            res = _result(depth, host=None, thinking="adaptive", model=None)
            return _fail(res, "network", label=spec["label"], status=None,
                         detail="timed out")
        finally:
            self._testing = False


# ------------------------------------------------------------ error copy (§5) --

def _chat_copy(code: str, *, label: str, host: str | None, model: str | None,
               status: int | None, detail: str) -> tuple[str, str | None, bool]:
    from .chat import provider_error_copy  # the single source of the section 5 copy
    return provider_error_copy(code, label=label, host=host, model=model, status=status,
                               detail=detail)


_NETWORK_RX = re.compile(r"(?i)ECONNREFUSED|Connection refused|ENOTFOUND|timed out|certificate")
_STATUS_IN_TEXT_RX = re.compile(r"(?i)\b(?:API Error|status(?: code)?)[:\s]+[1-5]\d\d\b")


def classify(status: int | None, assistant_error: str | None, text: str | None) -> str:
    """``error.code`` for a failed call (spec section 5; chat owns the table).

    One correction on top of chat's table: the CLI reports a refused
    connection as ``server_error`` with no status, and section 5 counts
    ``server_error`` as a server error only with a status; "no status and
    ECONNREFUSED/ENOTFOUND/timed out/certificate" is ``network``.
    """
    from .chat import classify_provider_error
    code = classify_provider_error(status, assistant_error, text)
    has_status = isinstance(status, int) and not isinstance(status, bool)
    if (code == "server" and not has_status and _NETWORK_RX.search(text or "")
            and not _STATUS_IN_TEXT_RX.search(text or "")):
        return "network"
    return code


def _result(depth: str, *, host: str | None, thinking: str, model: str | None) -> dict:
    return {"ok": False, "depth": depth, "host": host, "status": None, "code": None,
            "message": None, "hint": None, "retryable": False, "latency_ms": None,
            "checked_at_ms": _now_ms(), "thinking": thinking, "thinking_detected": None,
            "check_token": None, "model": model}


def _fail(res: dict, code: str, *, label: str, status: int | None, detail: str,
          secrets: Iterable[str] = ()) -> dict:
    detail = redact(detail or "", secrets)[:300]
    host = res.get("host")
    if code == "malformed":
        message = f"{host} didn't answer like an Anthropic Messages API."
        hint, retry = "Check that the URL is the provider's Anthropic API root.", False
    elif code == "redirect":
        message, hint, retry = REDIRECT_MESSAGE.format(host=host), REDIRECT_HINT, False
    elif code == "endpoint":
        message = (f"Nothing answered at {res.get('_base')}/v1/messages. The URL should be "
                   "the API root, without /v1.")
        hint, retry = "Check the endpoint URL.", False
    else:
        message, hint, retry = _chat_copy(code, label=label, host=host,
                                          model=res.get("model"), status=status, detail=detail)
    res.pop("_base", None)
    res.update(ok=False, code=code, status=status, message=redact(message, secrets),
               hint=hint, retryable=retry)
    return res


# -------------------------------------------------------------- quick probe --

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: CPython would forward the key header to the
    new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def strip_1m(model: str | None) -> str:
    return re.sub(r"\[1m\]$", "", model or "")


def probe_request(spec: Mapping, cfg: ProviderConfig, key: str | None
                  ) -> tuple[str, str, dict[str, str], bytes | None]:
    """(method, url, headers, body) for the quick check (spec section 6.1)."""
    qc = spec["quick_check"]
    if qc is None:
        raise _invalid("depth", f"Only the full check is available for {spec['label']}.")
    kind = spec["kind"]
    base = spec["base_url"]["default"] if kind == "anthropic_key" else cfg.base_url
    if not base:
        raise _invalid("base_url", "Enter the endpoint URL.")
    url = base.rstrip("/") + qc["path"]
    headers = {"anthropic-version": ANTHROPIC_VERSION, "accept": "application/json",
               "user-agent": "eye-in-the-sky-settings/1"}
    scheme = cfg.auth_scheme if kind in catalog.URL_KINDS else spec["auth"]["scheme"]
    token = key if scheme != "none" else None
    if not token:
        token = spec["auth"]["default_token"] or "unused"
    if scheme == "x-api-key":
        headers["x-api-key"] = token
    else:
        headers["authorization"] = f"Bearer {token}"
    body = None
    method = "GET"
    if qc["kind"] == "messages":
        method = "POST"
        headers["content-type"] = "application/json"
        body = json.dumps({"model": strip_1m(cfg.model), "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
    return method, url, headers, body


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _error_text(parsed: Any, raw: bytes) -> str:
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
        if isinstance(parsed.get("message"), str):
            return parsed["message"]
    return raw[:600].decode("utf-8", "replace")


def quick_probe(spec: Mapping, cfg: ProviderConfig, key: str | None, *,
                timeout: float = QUICK_TIMEOUT_S, opener: Any = None) -> dict:
    """Direct HTTP check (stdlib, blocking: run it in a thread). Never follows
    a redirect; reads at most 64 KiB; every message is redacted."""
    method, url, headers, body = probe_request(spec, cfg, key)
    label = spec["label"]
    secrets = [key] if key else []
    res = _result("quick", host=url_host(url), thinking=cfg.thinking, model=cfg.model)
    res["_base"] = (spec["base_url"]["default"] if spec["kind"] == "anthropic_key"
                    else cfg.base_url)
    opener = opener or urllib.request.build_opener(_NoRedirect)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read(PROBE_READ_MAX)
        res["latency_ms"] = int((time.monotonic() - t0) * 1000)
        parsed = _json(raw)
        kind = spec["quick_check"]["kind"]
        ok = isinstance(parsed, dict) and (
            (kind == "messages" and parsed.get("type") == "message")
            or (kind == "models_list" and isinstance(parsed.get("data"), list))
            or (kind == "key_info" and isinstance(parsed.get("data"), dict)))
        if not ok:
            return _fail(res, "malformed", label=label, status=status, detail="",
                         secrets=secrets)
        res.pop("_base", None)
        res.update(ok=True, status=status, code=None, message=(
            f"{cfg.model} answered." if kind == "messages" else f"{label} accepted the key."))
        return res
    except urllib.error.HTTPError as exc:
        res["latency_ms"] = int((time.monotonic() - t0) * 1000)
        status = exc.code
        try:
            raw = exc.read(PROBE_READ_MAX) or b""
        except Exception:  # noqa: BLE001
            raw = b""
        finally:
            exc.close()
        if 300 <= status < 400:
            return _fail(res, "redirect", label=label, status=status, detail="",
                         secrets=secrets)
        parsed = _json(raw)
        detail = _error_text(parsed, raw)
        if status == 404:
            names_model = isinstance(parsed, dict) and (
                "model" in detail.lower() or (cfg.model and strip_1m(cfg.model) in detail))
            code = "model" if names_model and spec["quick_check"]["kind"] == "messages" \
                else "endpoint"
        else:
            code = classify(status, None, detail)
        return _fail(res, code, label=label, status=status, detail=detail, secrets=secrets)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        res["latency_ms"] = int((time.monotonic() - t0) * 1000)
        reason = getattr(exc, "reason", None) or exc
        detail = f"{type(reason).__name__}: {reason}"
        if isinstance(reason, TimeoutError) or "timed out" in detail:
            detail = f"timed out: {detail}"
        return _fail(res, "network", label=label, status=None, detail=detail,
                     secrets=secrets)


# ------------------------------------------------------------ redirect probe --

REDIRECT_PROBE_TIMEOUT_S = 10.0
REDIRECT_PROBE_UA = "eye-in-the-sky-redirect-check/1"
REDIRECT_MESSAGE = "{host} tried to redirect the request. The key wasn't sent on."
REDIRECT_HINT = "Use the endpoint URL the provider documents."


def redirect_probe(base_url: str, env: Mapping[str, str], *, model: str | None = None,
                   timeout: float = REDIRECT_PROBE_TIMEOUT_S, opener: Any = None) -> int | None:
    """The 3xx status when ``{base_url}/v1/messages`` redirects, else None.

    The bundled CLI follows a redirect and re-sends ``x-api-key`` to the new
    host, even another origin, plain http or a link-local address (it drops
    ``Authorization`` there, not ``x-api-key``; secrets review). So a URL-kind
    endpoint that redirects is refused before the engine runs: one POST like
    the CLI's first (same path, the credential header the child ``env``
    carries) whose empty ``messages`` a provider rejects without generating.
    A redirect is never followed. Any other answer, and any network error,
    is left to the engine to report. Blocking: run it in a thread.
    """
    url = base_url.rstrip("/") + "/v1/messages?beta=true"
    headers = {"anthropic-version": ANTHROPIC_VERSION, "accept": "application/json",
               "content-type": "application/json", "user-agent": REDIRECT_PROBE_UA}
    if env.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = env["ANTHROPIC_API_KEY"]
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        headers["authorization"] = f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"
    body = json.dumps({"model": strip_1m(model), "max_tokens": 1, "messages": []}).encode()
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            resp.read(PROBE_READ_MAX)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None
    return status if 300 <= status < 400 else None


async def redirect_refusal(rp: ResolvedProvider) -> dict | None:
    """``{code, status, message, hint}`` when ``rp``'s endpoint redirects;
    None for a non-URL kind, a provider that isn't ready, or no redirect."""
    if rp.kind not in catalog.URL_KINDS or not rp.ready or not rp.base_url or not rp.env:
        return None
    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(redirect_probe, rp.base_url, rp.env, model=rp.model),
            REDIRECT_PROBE_TIMEOUT_S + 5)
    except TimeoutError:
        return None
    if not status:
        return None
    return {"code": "config", "status": status, "hint": REDIRECT_HINT,
            "message": REDIRECT_MESSAGE.format(host=rp.host or url_host(rp.base_url))}


# --------------------------------------------------------------- full check --

CHECK_PROMPT = "Reply with OK."
CHECK_SYSTEM_PROMPT = ("You are a connection check for the Eye in the Sky analyst. "
                       "Reply with the single word OK.")
#: On the wire so the provider sees a tool schema; never auto-approved
#: (``can_use_tool`` denies every call).
CHECK_TOOL = "mcp__check__check_echo"


async def _check_turn(sdk: Any, *, rp: ResolvedProvider, env: Mapping[str, str],
                      thinking: Mapping[str, str], cli_path: str | None, cwd: pathlib.Path,
                      on_connect: Callable[[Any], Any] | None,
                      options_builder: Callable[..., Any] | None = None) -> dict:
    """One ``max_turns=1`` turn through the bundled CLI; never raises.

    ``options_builder`` is the analyst's own builder
    (``ChatService.check_options``, spec section 6.2): when the host wires
    it, the check runs with exactly the analyst's fixed options (system
    prompt, partial messages, permission mode, ...) plus the check's changes.
    """
    stderr: list[str] = []

    @sdk.tool("check_echo", "Echo a word back (read-only; used by the connection check).",
              {"word": str})
    async def check_echo(args: dict) -> dict:
        return {"content": [{"type": "text", "text": str(args.get("word", ""))[:40]}]}

    async def deny_all(tool_name: str, tool_input: dict, ctx: Any) -> Any:
        return sdk.PermissionResultDeny(message="The connection check runs no tools.",
                                        interrupt=False)

    kw: dict[str, Any] = {
        "model": rp.model, "thinking": dict(thinking), "env": dict(env),
        "system_prompt": CHECK_SYSTEM_PROMPT, "tools": [], "allowed_tools": [],
        "mcp_servers": {"check": sdk.create_sdk_mcp_server(name="check", version="1.0.0",
                                                           tools=[check_echo])},
        "strict_mcp_config": True, "setting_sources": [], "verbatim_prompts": True,
        "permission_mode": "default", "can_use_tool": deny_all, "max_turns": 1,
        "cwd": str(cwd), "stderr": lambda line: stderr.append(redact(str(line))),
    }
    if rp.effort:
        kw["effort"] = rp.effort
    if cli_path:
        kw["cli_path"] = cli_path
    out: dict[str, Any] = {"result": None, "assistant_error": None, "text": [], "status": None,
                           "exc": None, "stderr": stderr, "tool_use": False}
    try:
        if options_builder is not None:
            from dataclasses import replace
            options = options_builder(
                replace(rp, thinking=MappingProxyType(dict(thinking)), env=dict(env)),
                env_extra=dict(env), mcp_servers=kw["mcp_servers"], allowed_tools=[],
                max_turns=1, stderr=kw["stderr"])
        else:
            options = sdk.ClaudeAgentOptions(**kw)
    except Exception as exc:  # noqa: BLE001 - reported as the check's outcome
        out["exc"] = f"{type(exc).__name__}: {exc}"
        return out
    client = sdk.ClaudeSDKClient(options=options)
    try:
        await client.connect()
        if on_connect is not None:
            maybe = on_connect(client)
            if asyncio.iscoroutine(maybe):
                await maybe
        await client.query(CHECK_PROMPT)
        async for msg in client.receive_response():
            name = type(msg).__name__
            if name == "AssistantMessage":
                if getattr(msg, "error", None):
                    out["assistant_error"] = msg.error
                for block in getattr(msg, "content", None) or []:
                    if isinstance(getattr(block, "text", None), str):
                        out["text"].append(block.text)
                    if type(block).__name__ == "ToolUseBlock":
                        out["tool_use"] = True
            elif name == "ResultMessage":
                out["result"] = msg
                out["status"] = getattr(msg, "api_error_status", None)
                if isinstance(getattr(msg, "result", None), str):
                    out["text"].append(msg.result)
                for err in getattr(msg, "errors", None) or []:
                    out["text"].append(str(err))
    except Exception as exc:  # noqa: BLE001 - reported as the check's outcome
        out["exc"] = f"{type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), 20.0)
    return out


def _check_outcome(out: dict) -> tuple[bool, str, int | None, str]:
    """(ok, code, status, detail) of one check turn."""
    result = out["result"]
    if result is not None and not getattr(result, "is_error", True) and not out["exc"]:
        return True, "", None, ""
    if (result is not None and not out["exc"] and out.get("tool_use")
            and not out["assistant_error"] and getattr(result, "api_error_status", None) is None
            and (getattr(result, "subtype", None) == "error_max_turns"
                 or getattr(result, "terminal_reason", None) == "max_turns")):
        # The provider answered with a well-formed call of the dummy tool; the
        # check's max_turns=1 (and its refusal) end the turn there. The engine
        # works: auth, model, streaming and the tool schema all went through.
        return True, "", None, ""
    text = " ".join(out["text"])
    if out["exc"]:
        text = f"{text} {out['exc']} {' '.join(out['stderr'][-5:])}".strip()
    status = out["status"] if isinstance(out["status"], int) else None
    code = classify(status, out["assistant_error"], text)
    return False, code, status, redact(text)[:300]


async def full_check(rp: ResolvedProvider, *, spec: Mapping, cfg: ProviderConfig,
                     check_home: pathlib.Path, sdk: Any = None, cli_path: str | None = None,
                     key: str | None = None, allow_downgrade: bool = False,
                     timeout: float = FULL_TIMEOUT_S,
                     on_connect: Callable[[Any], Any] | None = None,
                     options_builder: Callable[..., Any] | None = None) -> dict:
    """The analyst's real engine, one tiny turn (spec section 6.2).

    Same fixed options as the analyst (no settings, no built-in tools,
    ``can_use_tool`` denies), one read-only dummy tool so a tool schema is on
    the wire, ``max_turns=1``, ``CLAUDE_CODE_MAX_RETRIES=0``, no prompt
    history, and its own ``CLAUDE_CONFIG_DIR`` (``<store>/analyst/claude-check``).
    With ``allow_downgrade`` a 400 about thinking is retried once with
    thinking off.
    """
    label = spec["label"]
    secrets = [key] if key else []
    res = _result("full", host=rp.host, thinking=cfg.thinking, model=rp.model)
    if not rp.ready:
        message, hint = _config_copy(rp.reason, label)
        res.update(code="config", message=message, hint=hint)
        return res
    if sdk is None:
        try:
            sdk = importlib.import_module("claude_agent_sdk")
        except Exception:  # noqa: BLE001
            res.update(code="config", message="The analyst's engine isn't installed.",
                       hint="Install the analyst extra: pip install 'godseye-uav[app]'.")
            return res
    env = dict(rp.env)
    env.update({"CLAUDE_CODE_MAX_RETRIES": "0", "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1"})
    if rp.kind != "anthropic_login":
        env["CLAUDE_CONFIG_DIR"] = str(ensure_private_dir(check_home))
    cwd = ensure_private_dir(pathlib.Path(check_home).parent / "check-cwd")
    thinking = rp.thinking
    t0 = time.monotonic()

    deadline = t0 + timeout

    async def run(env_now: Mapping[str, str], thinking_now: Mapping[str, str]) -> dict:
        budget = max(1.0, deadline - time.monotonic())  # one budget for both attempts
        try:
            return await asyncio.wait_for(
                _check_turn(sdk, rp=rp, env=env_now, thinking=thinking_now, cli_path=cli_path,
                            cwd=cwd, on_connect=on_connect, options_builder=options_builder),
                budget)
        except TimeoutError:
            return {"result": None, "assistant_error": None, "status": None, "stderr": [],
                    "text": [], "exc": f"timed out after {int(timeout)} s"}

    out = await run(env, thinking)
    ok, code, status, detail = _check_outcome(out)
    if (not ok and allow_downgrade and code == "invalid_request"
            and thinking.get("type") != "disabled" and _THINKING_RX.search(detail)):
        env_off = {**env, "CLAUDE_CODE_DISABLE_THINKING": "1"}
        out = await run(env_off, DISABLED_THINKING)
        ok2, code2, status2, detail2 = _check_outcome(out)
        if ok2:
            res.update(ok=True, thinking="off", thinking_detected="off", message=(
                f"{label} doesn't accept extended thinking, so it's turned off for this "
                "model."), latency_ms=int((time.monotonic() - t0) * 1000))
            return res
        ok, code, status, detail = ok2, code2, status2, detail2
    res["latency_ms"] = int((time.monotonic() - t0) * 1000)
    if ok:
        mode = "off" if thinking.get("type") == "disabled" else "adaptive"
        res.update(ok=True, thinking=mode, thinking_detected=mode,
                   message=f"The analyst's engine works with {label}.")
        return res
    return _fail(res, code, label=label, status=status, detail=detail, secrets=secrets)


def _config_copy(reason: str | None, label: str) -> tuple[str, str]:
    if reason == REASON_KEY_MISSING:
        return f"The analyst has no key for {label}.", "Open analyst settings."
    if reason == REASON_SETTINGS_ERROR:
        return "The analyst's model settings could not be read.", "Open analyst settings."
    return f"The analyst isn't set up to use {label}.", "Open analyst settings."


# ------------------------------------------------------------------ HTTP API --

NO_STORE = {"Cache-Control": "no-store"}
MAX_BODY = 64 * 1024


def llm_settings_router(settings: LlmSettings, auth: Callable[..., bool],
                        chat: Any = None) -> Any:
    """``/settings/llm*`` (spec section 7). ``auth`` must be the header-only
    bearer dependency; a ``?token=`` query is refused outright. Include it
    before the ``/api/*`` catch-all and the static mount, and wrap the app in
    ``SettingsGuardMiddleware``. ``chat`` (the ``ChatService``, optional)
    lends the full check the analyst's own option builder."""
    router = APIRouter()
    if chat is not None:
        settings.use_analyst_options(chat)

    def answer(body: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(redact_value(body), status_code=status, headers=NO_STORE)

    def no_query_token(request: Request) -> bool:
        if "token" in request.query_params:
            raise SettingsError(400, "token_in_url")
        return True

    async def body_of(request: Request) -> Any:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise SettingsError(413, "too_large")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise _invalid(None, "The request body isn't valid JSON.") from None

    async def guarded(request: Request, fn: Callable[[], Any]) -> JSONResponse:
        try:
            no_query_token(request)
            return answer(await fn())
        except SettingsError as exc:
            return answer(exc.body(), exc.status)

    @router.get("/settings/llm", include_in_schema=False)
    async def get_settings(request: Request,
                           _: bool = Depends(auth)) -> JSONResponse:
        return await guarded(request, lambda: asyncio.to_thread(settings.view))

    @router.put("/settings/llm", include_in_schema=False)
    async def put_settings(request: Request,
                           _: bool = Depends(auth)) -> JSONResponse:
        async def run() -> dict:
            body = await body_of(request)
            return await asyncio.to_thread(settings.put, body, request.headers.get("if-match"))
        return await guarded(request, run)

    @router.delete("/settings/llm/providers/{pid}/key", include_in_schema=False)
    async def delete_key(pid: str, request: Request,
                         _: bool = Depends(auth)) -> JSONResponse:
        async def run() -> dict:
            return await asyncio.to_thread(settings.delete_key, pid,
                                           request.headers.get("if-match"))
        return await guarded(request, run)

    @router.post("/settings/llm/test", include_in_schema=False)
    async def test_settings(request: Request,
                            _: bool = Depends(auth)) -> JSONResponse:
        async def run() -> dict:
            return await settings.test(await body_of(request))
        return await guarded(request, run)

    @router.api_route("/settings/{path:path}", methods=["GET", "POST", "PUT", "PATCH",
                                                        "DELETE"], include_in_schema=False)
    async def not_found(path: str, _: bool = Depends(auth)) -> JSONResponse:
        return answer({"error": "not_found"}, 404)

    return router


class SettingsGuardMiddleware:
    """Same-origin only for ``/settings/*`` (spec section 7.5).

    Refuses (403 ``cross_origin``) an ``Origin`` other than
    ``http://{127.0.0.1|localhost|[::1]}:<port>`` and a ``Sec-Fetch-Site``
    other than ``same-origin``/``none``; refuses (400) a ``token`` query and
    (415) a POST/PUT/PATCH that isn't ``application/json``. Strips ``Origin``
    before the bridge's ``CORSMiddleware`` sees it and drops every
    ``Access-Control-*`` response header, so a cross-origin preflight fails.
    Every answer is ``Cache-Control: no-store``. ``port`` (an int or a
    callable) is the app's port; by default the listening socket's.
    """

    def __init__(self, app: Any, *, prefix: str = "/settings",
                 port: int | Callable[[], int | None] | None = None):
        self.app = app
        self.prefix = prefix.rstrip("/")
        self._port = port

    def _allowed(self, scope: Mapping) -> set[str]:
        port = self._port() if callable(self._port) else self._port
        if not port:
            server = scope.get("server") or ()
            port = server[1] if len(server) > 1 else None
        out = set()
        for host in ("127.0.0.1", "localhost", "[::1]"):
            out.add(f"http://{host}:{port}")
            if port == 80:
                out.add(f"http://{host}")
        return out

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if not (path == self.prefix or path.startswith(self.prefix + "/")):
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import Headers, QueryParams

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        site = headers.get("sec-fetch-site")
        refuse: tuple[int, str] | None = None
        if origin is not None and origin.strip().rstrip("/").lower() not in self._allowed(scope) or site is not None and site.strip().lower() not in ("same-origin", "none"):
            refuse = (403, "cross_origin")
        elif "token" in QueryParams(scope.get("query_string", b"")):
            refuse = (400, "token_in_url")
        elif scope.get("method") in ("POST", "PUT", "PATCH"):
            ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                refuse = (415, "json_required")
        if refuse is not None:
            response = JSONResponse({"error": refuse[1]}, status_code=refuse[0],
                                    headers={**NO_STORE, "X-Content-Type-Options": "nosniff"})
            await response(scope, receive, send)
            return
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() != b"origin"]

        async def guarded_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", [])
                        if not k.lower().startswith(b"access-control-")
                        and k.lower() not in (b"cache-control", b"x-content-type-options")]
                kept += [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
                message = {**message, "headers": kept}
            await send(message)

        await self.app(scope, receive, guarded_send)


__all__ = [
    "BLANK", "CAPTURED_VARS", "HARDEN_FLAGS", "LIVE_SECRETS", "SCHEMA", "EnvChoice",
    "FileSecrets", "KeychainError", "KeychainSecrets", "LlmSettings", "MemorySecrets",
    "ProviderConfig", "RedactingFilter", "ResolvedProvider", "SettingsError",
    "SettingsGuardMiddleware", "SettingsStore", "build_child_env", "capture_llm_env",
    "classify", "full_check", "infer_env_provider", "install_log_redaction",
    "llm_settings_router", "provider_options", "quick_probe", "redact", "redact_value",
    "register_secret", "validate_base_url", "validate_key", "validate_model",
]
