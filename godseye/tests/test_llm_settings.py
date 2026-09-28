"""BYOK provider settings: catalog, child env, storage, secrets, validators,
redaction, quick probe and the settings routes (BYOK spec section 11.1).

No SDK, no network beyond a loopback stub, fake keys only, never the login
keychain (the opt-in keychain test uses a throwaway keychain file).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from godseye_uav import llm_providers as catalog
from godseye_uav import llm_settings as L
from support.stub_anthropic import THINKING_REJECTION, StubAnthropic

KEY = "test-key-123"
KEY2 = "test-key-456"
HTTPS_ONLY = re.compile(r"^https://[^\s]+$")


def make(tmp_path: pathlib.Path, llm_env: dict | None = None, *, environ: dict | None = None,
         secrets=None, **kw) -> L.LlmSettings:
    return L.LlmSettings(tmp_path, llm_env or {}, store_dir=tmp_path / "store",
                         secrets=secrets if secrets is not None else L.MemorySecrets(),
                         environ=environ if environ is not None else {}, **kw)


@pytest.fixture(autouse=True)
def _fresh_secret_registry(monkeypatch):
    """Each test starts with no live secrets (the registry is process-wide)."""
    monkeypatch.setattr(L, "LIVE_SECRETS", L.SecretRegistry())


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
                 "all_proxy"):
        monkeypatch.delenv(name, raising=False)


PROBES: list[str] = []


@pytest.fixture(autouse=True)
def _loopback_only_redirect_probe(monkeypatch):
    """The full check's redirect probe is real HTTP: here it may only reach a
    loopback stub. A catalog endpoint (api.minimax.io, ...) is never called;
    the probe answers "no redirect" for it and is recorded in ``PROBES``."""
    real = getattr(L, "redirect_probe", None)
    PROBES.clear()

    def guarded(base_url, env, **kw):
        PROBES.append(base_url)
        if real is None or not L.is_loopback_host(L.urllib.parse.urlsplit(base_url).hostname):
            return None
        return real(base_url, env, **kw)
    monkeypatch.setattr(L, "redirect_probe", guarded, raising=False)


# ------------------------------------------------------------------ catalog --

def test_catalog_ids_kinds_and_enums():
    ids = [p["id"] for p in catalog.PROVIDERS.values()]
    assert len(ids) == len(set(ids)) == len(catalog.PROVIDER_IDS)
    for p in catalog.PROVIDERS.values():
        assert re.fullmatch(r"[a-z0-9_]+", p["id"])
        assert p["kind"] in catalog.KINDS
        assert p["group"] in catalog.GROUPS
        assert p["model_family"] in catalog.FAMILIES
        assert p["thinking"] in catalog.THINKING_MODES
        assert p["auth"]["scheme"] in catalog.AUTH_SCHEMES
        assert set(p["auth"]["schemes"]) <= set(catalog.AUTH_SCHEMES)
        assert p["cost_basis"] in catalog.COST_BASES
        assert p["effort"] == (p["kind"] in catalog.EFFORT_KINDS)
        if p["quick_check"] is not None:
            assert p["quick_check"]["kind"] in catalog.QUICK_CHECKS


def test_catalog_docs_are_https_and_notes_are_sentences():
    for p in catalog.PROVIDERS.values():
        assert HTTPS_ONLY.fullmatch(p["docs_url"]), p["id"]
        assert p["docs"] and all(HTTPS_ONLY.fullmatch(d["url"]) for d in p["docs"])
        assert all(isinstance(n, str) and n == n.strip() and n.endswith(".")
                   for n in p["notes"]), p["id"]
        if p["model_family"] in ("non_claude", "mixed"):
            assert catalog.NON_CLAUDE_NOTE in p["notes"], p["id"]


def test_catalog_env_names_are_allowlisted():
    for p in catalog.PROVIDERS.values():
        assert set(p["extra_env"]) <= catalog.EXTRA_ENV_ALLOWLIST, p["id"]
        assert set(p["auth"]["env_override"]) <= set(L.CAPTURED_VARS), p["id"]
        for f in p["fields"]:
            assert f["env"] is None or f["env"] in catalog.FIELD_ENV_ALLOWLIST
            if f["pattern"]:
                re.compile(f["pattern"])
    assert catalog.key_env_names() <= set(L.CAPTURED_VARS)


def test_catalog_models_and_presets_validate():
    for p in catalog.PROVIDERS.values():
        m = p["models"]
        for model in (m["default"], m["small_default"], *m["suggestions"]):
            if model is not None:
                assert L.validate_model(model) == model, (p["id"], model)
        base = p["base_url"]
        if base is None:
            continue
        for url in (base["default"], *(x["url"] for x in base["presets"])):
            if url:
                filled = L.fill_placeholders(url, {"workspace_id": "ws-abc123"})
                assert L.validate_base_url(filled) == filled.rstrip("/"), (p["id"], url)


def test_catalog_url_kinds_have_a_way_to_authenticate():
    for p in catalog.PROVIDERS.values():
        if p["kind"] in catalog.URL_KINDS:
            assert p["auth"]["scheme"] in ("bearer", "x-api-key")
            if p["auth"]["key_optional"]:
                assert p["auth"]["default_token"], p["id"]


# ---------------------------------------------------------- build_child_env --

CLOUD_FIELDS = {"bedrock": {"region": "us-east-1"},
                "vertex": {"project": "my-project-123", "region": "global"},
                "foundry": {"resource": "my-resource"}}
URLS = {"alibaba": "https://dashscope.aliyuncs.com/apps/anthropic",
        "custom": "https://gw.example.com/api"}
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
                   "ANTHROPIC_FOUNDRY_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
SWITCHES = {"bedrock": "CLAUDE_CODE_USE_BEDROCK", "vertex": "CLAUDE_CODE_USE_VERTEX",
            "foundry": "CLAUDE_CODE_USE_FOUNDRY"}


def cfg_for(pid: str, **kw) -> L.ProviderConfig:
    spec = catalog.get(pid)
    model = spec["models"]["default"] or (
        "claude-opus-5" if spec["model_family"] == "claude" else "local-model")
    base = None
    if spec["kind"] in catalog.URL_KINDS:
        base = URLS.get(pid) or spec["base_url"]["default"]
    args = {"provider": pid, "model": model, "small_model": spec["models"]["small_default"],
            "base_url": base, "fields": CLOUD_FIELDS.get(pid, {}),
            "auth_scheme": spec["auth"]["scheme"], "thinking": "adaptive",
            "model_family": spec["model_family"]}
    args.update(kw)
    return L.ProviderConfig(**args)


def designated(pid: str) -> str | None:
    spec = catalog.get(pid)
    kind = spec["kind"]
    if kind == "anthropic_key":
        return "ANTHROPIC_API_KEY"
    if kind in catalog.URL_KINDS:
        return "ANTHROPIC_API_KEY" if spec["auth"]["scheme"] == "x-api-key" \
            else "ANTHROPIC_AUTH_TOKEN"
    return {"bedrock": "AWS_BEARER_TOKEN_BEDROCK",
            "foundry": "ANTHROPIC_FOUNDRY_API_KEY"}.get(kind)


def build(pid: str, key: str | None = KEY, tmp: pathlib.Path | None = None, **kw):
    cfg_dir = (tmp or pathlib.Path("/tmp/x")) / "store" / "analyst" / "claude-home"
    return L.build_child_env(catalog.get(pid), cfg_for(pid, **kw), key,
                             config_dir=cfg_dir, captured={})


@pytest.mark.parametrize("pid", catalog.PROVIDER_IDS)
def test_child_env_rules_per_kind(pid):
    spec = catalog.get(pid)
    kind = spec["kind"]
    env, reason = build(pid)
    assert reason is None
    assert set(env) <= L.CHILD_ENV_ALLOWLIST
    assert set(L.BLANK) <= set(env)
    target = designated(pid)
    holders = [k for k, v in env.items() if v == KEY]
    assert holders == ([target] if target else [])
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        assert env[var] == (KEY if var == target else ""), var
    assert ("ANTHROPIC_BASE_URL" in env) == (kind in catalog.URL_KINDS)
    if kind == "anthropic_login":
        assert not set(L.HARDEN_FLAGS) & set(env)
        assert "CLAUDE_CONFIG_DIR" not in env
    else:
        assert {k: env[k] for k in L.HARDEN_FLAGS} == dict(L.HARDEN_FLAGS)
        assert env["CLAUDE_CONFIG_DIR"].endswith("/store/analyst/claude-home")
    for k, switch in SWITCHES.items():
        assert env[switch] == ("1" if kind == k else "")
    pins = set(L.PIN_VARS) & set(env)
    if kind in catalog.URL_KINDS or kind in catalog.CLOUD_KINDS:
        cfg = cfg_for(pid)
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == env["ANTHROPIC_DEFAULT_SONNET_MODEL"] \
            == cfg.model
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == (cfg.small_model or cfg.model)
    else:
        assert not pins
    if kind in catalog.URL_KINDS:
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert "CLAUDE_CODE_DISABLE_THINKING" not in env
    assert not any(L.has_ctl(k) or L.has_ctl(v) for k, v in env.items())
    off, _ = build(pid, thinking="off")
    assert off["CLAUDE_CODE_DISABLE_THINKING"] == "1"


def test_child_env_exact_key_sets():
    base, harden = set(L.BLANK), set(L.HARDEN_FLAGS) | {"CLAUDE_CONFIG_DIR"}
    url = {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", *L.PIN_VARS}
    assert set(build("anthropic_login")[0]) == base
    assert set(build("anthropic_api")[0]) == base | harden
    assert set(build("openrouter")[0]) == base | harden | url
    assert set(build("minimax")[0]) == base | harden | url | {"CLAUDE_CODE_AUTO_COMPACT_WINDOW"}
    assert set(build("bedrock")[0]) == base | harden | set(L.PIN_VARS) | {
        "AWS_REGION", "AWS_BEARER_TOKEN_BEDROCK"}
    assert set(build("vertex", key=None)[0]) == base | harden | set(L.PIN_VARS) | {
        "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION"}


def test_login_env_carries_only_captured_login_values():
    spec = catalog.get("anthropic_login")
    env, reason = L.build_child_env(
        spec, cfg_for("anthropic_login"), None, config_dir=None,
        captured={"CLAUDE_CODE_OAUTH_TOKEN": "tok-abcdefgh1234", "CLAUDE_CONFIG_DIR": "/x/cfg",
                  "ANTHROPIC_BASE_URL": "https://evil.example"})
    assert reason is None
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-abcdefgh1234"
    assert env["CLAUDE_CONFIG_DIR"] == "/x/cfg"
    assert "ANTHROPIC_BASE_URL" not in env


@pytest.mark.parametrize("pid", ["anthropic_api", "openrouter", "minimax", "deepseek",
                                 "moonshot", "zai", "zhipu", "alibaba", "custom"])
@pytest.mark.parametrize("key", [None, "", "   "])
def test_byok_refuses_without_a_credential(pid, key):
    env, reason = build(pid, key=key)
    assert (env, reason) == ({}, L.REASON_KEY_MISSING)


def test_keyless_local_providers_send_a_placeholder_token_never_blank():
    env, _ = build("ollama", key=None)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama" and env["ANTHROPIC_API_KEY"] == ""
    env, _ = build("lmstudio", key=None)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "lmstudio"
    env, _ = build("custom", key=None, auth_scheme="none")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "unused"
    env, reason = build("ollama", key=None, base_url="https://ollama.com")
    assert (env, reason) == ({}, L.REASON_KEY_MISSING)


def test_refusals_for_missing_or_bad_config(tmp_path):
    assert build("custom", base_url=None)[1] == L.REASON_NOT_CONFIGURED
    assert build("ollama", model=None)[1] == L.REASON_NOT_CONFIGURED
    assert build("bedrock", fields={})[1] == L.REASON_NOT_CONFIGURED
    assert build("custom", model="bad\nmodel")[1] == L.REASON_SETTINGS_ERROR
    assert build("custom", base_url="https://x.example/\r\nHost: y")[1] == \
        L.REASON_SETTINGS_ERROR
    env, reason = L.build_child_env(catalog.get("minimax"), cfg_for("minimax"), KEY,
                                    config_dir=None)
    assert (env, reason) == ({}, L.REASON_SETTINGS_ERROR)


def test_switch_back_to_login_clears_the_byok_endpoint(tmp_path):
    s = make(tmp_path)
    s.put({"provider": "openrouter", "key": {"action": "set", "value": KEY}}, 0)
    rp = s._resolve("openrouter", s._doc)
    assert rp.ready and rp.env["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert rp.env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    login = s.resolve()
    assert login.kind == "anthropic_login" and login.ready
    assert "ANTHROPIC_BASE_URL" not in login.env
    assert login.env["ANTHROPIC_AUTH_TOKEN"] == "" and login.env["ANTHROPIC_API_KEY"] == ""
    assert "CLAUDE_CONFIG_DIR" not in login.env
    assert KEY not in repr(rp) and "***" in repr(rp)


# ------------------------------------------------------- launch environment --

def test_capture_pops_every_listed_variable_and_keeps_cloud_chains():
    environ = {name: f"value-for-{name.lower()}" for name in L.CAPTURED_VARS}
    keep = {"AWS_PROFILE": "p", "AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "a",
            "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t",
            "GOOGLE_APPLICATION_CREDENTIALS": "/x.json", "CLOUD_ML_REGION": "global",
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj", "HTTPS_PROXY": "http://p:1",
            "NODE_EXTRA_CA_CERTS": "/ca.pem", "PATH": "/usr/bin"}
    environ.update(keep)
    environ["ANTHROPIC_MODEL"] = ""
    captured = L.capture_llm_env(environ)
    assert environ == keep
    assert set(captured) == set(L.CAPTURED_VARS) - {"ANTHROPIC_MODEL"}


@pytest.mark.parametrize("captured,expected", [
    ({"GODSEYE_LLM_PROVIDER": "minimax", "CLAUDE_CODE_USE_BEDROCK": "1"},
     ("minimax", True, "GODSEYE_LLM_PROVIDER")),
    ({"CLAUDE_CODE_USE_VERTEX": "true", "ANTHROPIC_API_KEY": KEY}, ("vertex", False, None)),
    ({"CLAUDE_CODE_USE_FOUNDRY": "1"}, ("foundry", False, None)),
    ({"ANTHROPIC_BASE_URL": "https://gw.example", "ANTHROPIC_AUTH_TOKEN": KEY},
     ("custom", False, "bearer")),
    ({"ANTHROPIC_BASE_URL": "https://gw.example", "ANTHROPIC_API_KEY": KEY},
     ("custom", False, "x-api-key")),
    ({"ANTHROPIC_API_KEY": KEY}, ("anthropic_api", False, None)),
    ({}, ("anthropic_login", False, None)),
    ({"GODSEYE_LLM_PROVIDER": "nope"}, ("anthropic_login", False, None)),
])
def test_first_run_inference(captured, expected):
    choice = L.infer_env_provider(captured)
    assert choice.provider == expected[0]
    assert choice.locked is expected[1]
    if expected[0] == "custom":
        assert choice.custom_scheme == expected[2]


def test_base_url_without_a_key_is_discarded(caplog, tmp_path):
    with caplog.at_level(logging.WARNING, logger="godseye_uav.llm_settings"):
        choice = L.infer_env_provider({"ANTHROPIC_BASE_URL": "https://gw.example"})
    assert choice.provider == "anthropic_login" and choice.custom_base_url is None
    assert "Ignored ANTHROPIC_BASE_URL without a key" in caplog.text
    s = make(tmp_path, {"ANTHROPIC_BASE_URL": "https://gw.example"})
    rp = s.resolve()
    assert rp.kind == "anthropic_login" and "ANTHROPIC_BASE_URL" not in rp.env


def test_environment_custom_endpoint_is_locked_and_used(tmp_path):
    s = make(tmp_path, {"ANTHROPIC_BASE_URL": "https://gw.example/api",
                        "ANTHROPIC_AUTH_TOKEN": KEY, "ANTHROPIC_MODEL": "gw-model"})
    rp = s.resolve()
    assert (rp.id, rp.ready, rp.key_source) == ("custom", True, "environment")
    assert rp.env["ANTHROPIC_BASE_URL"] == "https://gw.example/api"
    assert rp.env["ANTHROPIC_AUTH_TOKEN"] == KEY and rp.env["ANTHROPIC_API_KEY"] == ""
    view = next(p for p in s.view()["providers"] if p["id"] == "custom")
    assert view["base_url"]["editable"] is False and view["key"]["env"] == "ANTHROPIC_AUTH_TOKEN"
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "custom", "key": {"action": "set", "value": KEY2}}, 0)
    assert err.value.status == 409 and err.value.body()["error"] == "locked_by_environment"


def test_environment_key_wins_over_the_store(tmp_path):
    mem = L.MemorySecrets()
    s = make(tmp_path, secrets=mem)
    s.put({"provider": "minimax", "key": {"action": "set", "value": KEY2}}, 0)
    s2 = make(tmp_path, {"MINIMAX_API_KEY": KEY}, secrets=mem)
    rp = s2._resolve("minimax", s2._doc)
    assert rp.env["ANTHROPIC_AUTH_TOKEN"] == KEY and rp.key_source == "environment"


# --------------------------------------------------------------- validators --

@pytest.mark.parametrize("model", ["claude-opus-5-5", "MiniMax-M3[1m]", "~anthropic/claude-x",
                                   "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                                   "claude-haiku-4-5@20251001", "a"])
def test_model_accepts(model):
    assert L.validate_model(model) == model


@pytest.mark.parametrize("model", ["-rf", "--model", "has space", "x\ny", "a;b", "é", "x" * 201])
def test_model_rejects(model):
    with pytest.raises(L.SettingsError):
        L.validate_model(model)


SELF = [("127.0.0.1", 8780), ("localhost", 8780), ("::1", 8780)]


@pytest.mark.parametrize("url,kw,expected", [
    ("https://api.minimax.io/anthropic/", {}, "https://api.minimax.io/anthropic"),
    ("http://localhost:11434", {}, "http://localhost:11434"),
    ("http://127.0.0.1:1234/", {}, "http://127.0.0.1:1234"),
    ("http://[::1]:1234", {}, "http://[::1]:1234"),
    ("http://192.168.1.20:8000", {"allow_insecure_http": True}, "http://192.168.1.20:8000"),
    ("http://[fd00::5]:8000", {"allow_insecure_http": True}, "http://[fd00::5]:8000"),
    ("https://例え.jp/anthropic", {}, "https://xn--r8jz45g.jp/anthropic"),
    ("https://{workspace_id}.us-east-1.maas.aliyuncs.com/apps/anthropic",
     {"fields": {"workspace_id": "ws-1"}}, "https://ws-1.us-east-1.maas.aliyuncs.com/apps/anthropic"),
    ("http://127.0.0.1:8781", {"self_hostports": SELF}, "http://127.0.0.1:8781"),
])
def test_base_url_accepts(url, kw, expected):
    assert L.validate_base_url(url, **kw) == expected


@pytest.mark.parametrize("url,kw", [
    ("http://api.example.com", {}),
    ("http://192.168.1.20:8000", {}),
    ("http://10.0.0.1", {}),
    ("https://169.254.169.254/latest", {}),
    ("http://[fe80::1]:80", {"allow_insecure_http": True}),
    ("https://0.0.0.0", {}),
    ("https://user:pw@api.example.com", {}),
    ("https://api.example.com/?x=1", {}),
    ("https://api.example.com/#frag", {}),
    ("ftp://api.example.com", {}),
    ("file:///etc/passwd", {}),
    ("https://", {}),
    ("https://{workspace_id}.maas.aliyuncs.com", {}),
    ("http://127.0.0.1:8780", {"self_hostports": SELF}),
    ("http://localhost:8780/api", {"self_hostports": SELF}),
    ("https://a b.example", {}),
    ("https://x.example:99999", {}),
])
def test_base_url_rejects(url, kw):
    with pytest.raises(L.SettingsError) as err:
        L.validate_base_url(url, **kw)
    assert err.value.status == 422


def test_key_validator():
    assert L.validate_key(f"  {KEY}\t") == KEY
    for bad in ("short", "x" * 513, "has space in it", "tab\tinside!", "ключ-ключ-ключ", 12345,
                "line\nbreak-123"):
        with pytest.raises(L.SettingsError):
            L.validate_key(bad)


def test_field_validators(tmp_path):
    region = catalog.get("bedrock")["fields"][0]
    assert L.validate_field(region, "us-gov-west-1") == "us-gov-west-1"
    with pytest.raises(L.SettingsError):
        L.validate_field(region, "US-EAST-1")
    project = catalog.get("vertex")["fields"][0]
    with pytest.raises(L.SettingsError):
        L.validate_field(project, "ab")
    creds = catalog.get("vertex")["fields"][2]
    f = tmp_path / "creds.json"
    f.write_text("{}")
    assert L.validate_field(creds, str(f)) == str(f)
    for bad in ("creds.json", str(tmp_path), str(tmp_path / "missing.json")):
        with pytest.raises(L.SettingsError):
            L.validate_field(creds, bad)


# ---------------------------------------------------------------- redaction --

SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"


def test_redact_whole_key_fragment_and_json():
    L.register_secret(SECRET)
    assert L.redact(f"bad key {SECRET}!") == "bad key [redacted key]!"
    fragment = SECRET[5:17]
    assert len(fragment) == 12
    assert L.redact(f"...{fragment}...") == "...[redacted key]..."
    assert L.redact(SECRET[:11]) == SECRET[:11]  # shorter than a run: not a match
    blob = json.dumps({"error": {"message": f"key {SECRET} rejected"}})
    assert SECRET not in L.redact(blob) and "[redacted key]" in L.redact(blob)
    nested = L.redact_value({"a": [f"x{SECRET}", {"b": SECRET}], "n": 3})
    assert SECRET not in json.dumps(nested) and nested["n"] == 3
    assert L.redact("the short key test-key-9 here", ["test-key-9"]) == \
        "the short key [redacted key] here"
    assert L.redact("placeholder ollama stays", ["ollama"]) == "placeholder ollama stays"


def test_log_records_in_the_godseye_tree_are_redacted(caplog):
    L.install_log_redaction()
    L.register_secret(SECRET)
    child = logging.getLogger("godseye_uav.some.child")
    with caplog.at_level(logging.INFO):
        child.info("provider said %s", f"invalid key {SECRET}")
        try:
            raise ValueError(f"boom {SECRET}")
        except ValueError:
            child.exception("failed")
    assert SECRET not in caplog.text
    assert "[redacted key]" in caplog.text
    for rec in caplog.records:
        assert SECRET not in rec.getMessage() and SECRET not in (rec.exc_text or "")


def test_redacting_filter_on_a_handler():
    L.register_secret(SECRET)
    rec = logging.LogRecord("other", logging.INFO, __file__, 1, "k=%s", (SECRET,), None)
    assert L.RedactingFilter().filter(rec)
    assert SECRET not in rec.getMessage()


# ----------------------------------------------------------- settings store --

def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_modes_and_revisions(tmp_path):
    s = make(tmp_path / "data")
    view = s.put({"provider": "minimax", "model": "MiniMax-M2.7"}, 0)
    assert view["rev"] == 1
    path = tmp_path / "data" / L.SETTINGS_FILE
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    doc = json.loads(path.read_text())
    assert doc["schema"] == L.SCHEMA and doc["rev"] == 1
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax", "model": "MiniMax-M3[1m]"}, 0)
    assert err.value.status == 409 and err.value.body() == {"error": "settings_conflict",
                                                           "rev": 1}
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax"}, None)
    assert err.value.status == 428
    assert s.put({"provider": "minimax"}, '"1"')["rev"] == 2
    assert s.put({"provider": "minimax"}, 'W/"2"')["rev"] == 3


def test_future_schema_is_read_only(tmp_path):
    path = tmp_path / L.SETTINGS_FILE
    raw = json.dumps({"schema": "eye-in-the-sky.llm-settings/9", "rev": 4, "active": "minimax",
                      "providers": {"minimax": {"model": "MiniMax-M2.7"}}, "future": 1})
    path.write_text(raw)
    s = make(tmp_path)
    view = s.view()
    assert view["read_only"] is True and view["rev"] == 4 and "newer version" in view["notice"]
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax"}, 4)
    assert err.value.status == 409 and err.value.error == "read_only"
    assert path.read_text() == raw


def test_corrupt_file_is_moved_aside_without_logging_its_contents(tmp_path, caplog):
    path = tmp_path / L.SETTINGS_FILE
    path.write_text('{"schema": "eye-in-the-sky.llm-settings/1", "rev": ' + SECRET)
    with caplog.at_level(logging.WARNING):
        s = make(tmp_path)
    assert s.view()["rev"] == 0
    backups = list(tmp_path.glob(f"{L.SETTINGS_FILE}.corrupt-*"))
    assert len(backups) == 1 and not path.exists()
    assert "corrupt" in caplog.text and SECRET[5:17] not in caplog.text


def test_file_secrets_roundtrip_and_chmod_repair(tmp_path, caplog):
    fs = L.FileSecrets(tmp_path / "d" / L.SECRETS_FILE)
    fs.set("minimax", KEY)
    path = tmp_path / "d" / L.SECRETS_FILE
    assert mode(path) == 0o600
    assert json.loads(path.read_text())["schema"] == L.SECRETS_SCHEMA
    os.chmod(path, 0o644)
    with caplog.at_level(logging.WARNING):
        assert fs.get("minimax") == KEY
    assert mode(path) == 0o600 and "readable by others" in caplog.text
    fs.delete("minimax")
    assert fs.get("minimax") is None


def test_settings_file_never_holds_the_key(tmp_path):
    fs = L.FileSecrets(tmp_path / L.SECRETS_FILE)
    s = make(tmp_path, secrets=fs)
    s.put({"provider": "deepseek", "key": {"action": "set", "value": KEY}}, 0)
    text = (tmp_path / L.SETTINGS_FILE).read_text()
    assert KEY not in text and KEY[:12] not in text
    assert json.loads(text)["providers"]["deepseek"]["secret"]["last4"] == "-123"


# ---------------------------------------------------------------- keychain --

class FakeSecurity:
    """Stands in for /usr/bin/security; records argv and stdin, keeps items."""

    def __init__(self, fail_add: bool = False):
        self.calls: list[tuple[list[str], str | None]] = []
        self.items: dict[str, str] = {}
        self.fail_add = fail_add

    def __call__(self, argv, stdin, timeout):
        self.calls.append((list(argv), stdin))
        if argv[1] == "-i":
            if self.fail_add:
                return 2, "", "add-generic-password: returned 2"
            acct = re.search(r'-a "([^"]+)"', stdin).group(1)
            hexed = re.search(r"-X ([0-9a-f]+)", stdin).group(1)
            self.items[acct] = bytes.fromhex(hexed).decode()
            return 0, "", ""
        acct = argv[argv.index("-a") + 1]
        if argv[1] == "find-generic-password":
            if acct in self.items:
                return 0, self.items[acct] + "\n", ""
            return 44, "", ("security: SecKeychainSearchCopyNext: The specified item "
                            "could not be found in the keychain.")
        if argv[1] == "delete-generic-password":
            return (0, "", "") if self.items.pop(acct, None) else (44, "", "could not be found")
        raise AssertionError(argv)


def with_fake_keychain(tmp_path: pathlib.Path, fake: FakeSecurity) -> L.LlmSettings:
    """Default backends for macOS (keychain, then file), the keychain faked."""
    s = L.LlmSettings(tmp_path, {}, store_dir=tmp_path / "store", environ={}, platform="darwin")
    s._backends = {"keychain": L.KeychainSecrets(s._keychain_scope, runner=fake),
                   "file": L.FileSecrets(tmp_path / L.SECRETS_FILE)}
    s._primary = s._backends["keychain"]
    return s


def test_keychain_secret_never_on_argv_and_hex_on_stdin():
    fake = FakeSecurity()
    kc = L.KeychainSecrets(lambda: "abcd1234", runner=fake)
    kc.set("minimax", KEY)
    assert kc.get("minimax") == KEY
    for argv, stdin in fake.calls:
        assert all(KEY not in a for a in argv)
        if stdin is not None:
            assert KEY not in stdin and KEY.encode().hex() in stdin
            assert '-s "eye-in-the-sky.llm"' in stdin and '-a "abcd1234:minimax"' in stdin
    assert kc.get("deepseek") is None
    kc.delete("minimax")
    kc.delete("minimax")  # not found is fine
    assert kc.get("minimax") is None


def test_keychain_errors_and_file_fallback(tmp_path, caplog):
    s = with_fake_keychain(tmp_path, FakeSecurity(fail_add=True))
    with caplog.at_level(logging.WARNING):
        view = s.put({"provider": "zai", "key": {"action": "set", "value": KEY}}, 0)
    zai = next(p for p in view["providers"] if p["id"] == "zai")
    assert zai["key"]["source"] == "file"
    assert json.loads((tmp_path / L.SECRETS_FILE).read_text())["keys"]["zai"] == KEY
    assert "keychain write failed" in caplog.text and KEY not in caplog.text
    with pytest.raises(L.KeychainError):
        L.KeychainSecrets(lambda: "abcd1234", runner=lambda *a: (1, "", "boom")).get("zai")


def test_status_and_view_never_touch_the_keychain_and_resolve_caches(tmp_path):
    fake = FakeSecurity()
    s = with_fake_keychain(tmp_path, fake)
    s.put({"provider": "deepseek", "key": {"action": "set", "value": KEY}}, 0)
    s._key_cache.clear()
    fake.calls.clear()
    s.view()
    s.status()
    s.resolve()
    assert fake.calls == []
    rp = s._resolve("deepseek", s._doc)
    assert rp.env["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert len(fake.calls) == 1
    s._resolve("deepseek", s._doc)
    assert len(fake.calls) == 1


def _security(*args: str, pw: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True,
                          check=False, timeout=30)


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("GODSEYE_TEST_KEYCHAIN") != "1",
                    reason="opt-in: GODSEYE_TEST_KEYCHAIN=1 (throwaway keychain, macOS)")
def test_real_keychain_roundtrip_on_a_throwaway_keychain(tmp_path):
    root = pathlib.Path("/private/tmp/claude-501/intel-console-scratch/byok-build")
    base = pathlib.Path(tempfile.mkdtemp(dir=root if root.is_dir() else tmp_path))
    path = str(base / "throwaway.keychain-db")
    assert "login.keychain" not in path
    before = _security("list-keychains", "-d", "user").stdout
    password = os.urandom(12).hex()
    assert _security("create-keychain", "-p", password, path).returncode == 0
    try:
        _security("set-keychain-settings", path)
        assert _security("unlock-keychain", "-p", password, path).returncode == 0
        kc = L.KeychainSecrets(lambda: "t3st5c0p", keychain=path)
        assert kc.get("minimax") is None
        kc.set("minimax", KEY)
        assert kc.get("minimax") == KEY
        kc.set("minimax", KEY2)  # -U updates in place
        assert kc.get("minimax") == KEY2
        kc.delete("minimax")
        assert kc.get("minimax") is None
    finally:
        _security("delete-keychain", path)
        shutil.rmtree(base, ignore_errors=True)
    assert _security("list-keychains", "-d", "user").stdout == before


# -------------------------------------------------------------- quick probe --

@pytest.fixture
def stub():
    with StubAnthropic() as s:
        yield s


def custom_cfg(base: str, **kw) -> L.ProviderConfig:
    args = {"provider": "custom", "model": "MiniMax-M3[1m]", "base_url": base,
            "auth_scheme": "bearer", "model_family": "non_claude"}
    args.update(kw)
    return L.ProviderConfig(**args)


def probe(stub_base: str, key: str | None = KEY, **kw) -> dict:
    return L.quick_probe(catalog.get("custom"), custom_cfg(stub_base, **kw), key, timeout=5)


def test_quick_probe_ok_sends_one_tiny_request_with_the_right_header(stub):
    res = probe(stub.url)
    assert res["ok"] is True and res["status"] == 200 and res["depth"] == "quick"
    assert res["host"] == f"127.0.0.1:{stub.port}"
    (req,) = stub.requests
    assert req["auth"] == {"authorization": {"scheme": "Bearer", "kind": f"test:{KEY}"},
                           "x_api_key": None}
    assert req["body"]["max_tokens"] == 1 and req["body"]["model"] == "MiniMax-M3"
    assert req["body"]["stream"] is False
    stub.reset()
    assert probe(stub.url, auth_scheme="x-api-key")["ok"]
    assert stub.requests[0]["auth"] == {"authorization": None, "x_api_key": f"test:{KEY}"}


def test_quick_probe_errors_are_classified(stub):
    assert probe(stub.base("401"))["code"] == "auth"
    res = probe(stub.base("403"))
    assert (res["code"], res["status"]) == ("auth", 403) and "refused access" in res["message"]
    assert probe(stub.base("429"))["code"] == "rate_limit"
    assert probe(stub.base("529"))["code"] == "server"
    assert probe(stub.base("404"))["code"] == "model"
    res = probe(stub.url + "/nothing")
    assert res["code"] == "endpoint" and "without /v1" in res["message"]
    res = probe(stub.base("html"))
    assert res["code"] == "malformed" and "Anthropic Messages API" in res["message"]
    res = probe(stub.base("reject_thinking"))  # a probe sends no thinking: accepted
    assert res["ok"] is True


def test_quick_probe_never_follows_a_redirect(stub):
    res = probe(stub.base("redirect"))
    assert res["code"] == "redirect" and res["status"] == 307
    assert "wasn't sent on" in res["message"]
    assert [r for r in stub.requests if r["path"].startswith("/landing")] == []


def test_quick_probe_redacts_an_echoed_key(stub):
    res = probe(stub.base("echo_key"))
    assert res["code"] == "auth"
    assert KEY not in json.dumps(res)
    res = L._fail(L._result("quick", host="h", thinking="adaptive", model="m"),
                  "invalid_request", label="X", status=400, detail=f"bad key {KEY}",
                  secrets=[KEY])
    assert KEY not in json.dumps(res) and "[redacted key]" in res["message"]


def test_quick_probe_network_failure():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    res = probe(f"http://127.0.0.1:{port}")
    assert res["code"] == "network" and res["retryable"] is True
    assert res["message"] == f"Couldn't reach 127.0.0.1:{port}."


def test_probe_requests_for_fixed_endpoints_are_built_not_sent():
    method, url, headers, body = L.probe_request(
        catalog.get("anthropic_api"), L.ProviderConfig("anthropic_api", "claude-opus-5"), KEY)
    assert (method, url, body) == ("GET", "https://api.anthropic.com/v1/models?limit=1", None)
    assert headers["x-api-key"] == KEY and "authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    method, url, headers, body = L.probe_request(
        catalog.get("openrouter"),
        L.ProviderConfig("openrouter", "anthropic/claude-opus-5.5",
                         base_url="https://openrouter.ai/api"), KEY)
    assert (method, url) == ("GET", "https://openrouter.ai/api/v1/key")
    assert headers["authorization"] == f"Bearer {KEY}" and "x-api-key" not in headers
    with pytest.raises(L.SettingsError):
        L.probe_request(catalog.get("bedrock"), L.ProviderConfig("bedrock", "m"), KEY)


# ------------------------------------------------------------ fake SDK (full) --

class _Text:
    def __init__(self, text: str):
        self.text = text


class AssistantMessage:
    def __init__(self, text: str, error: str | None = None):
        self.content = [_Text(text)]
        self.error = error


class ResultMessage:
    def __init__(self, is_error: bool, status: int | None = None, result: str = ""):
        self.is_error = is_error
        self.api_error_status = status
        self.result = result
        self.errors = None


class FakeSDK:
    """Just enough of claude_agent_sdk for full_check; ``outcomes`` is one
    ``(is_error, status, text)`` per check turn."""

    def __init__(self, *outcomes: tuple[bool, int | None, str]):
        self.outcomes = list(outcomes) or [(False, None, "OK")]
        self.options: list[dict] = []
        self.PermissionResultDeny = lambda **kw: ("deny", kw)

    def tool(self, name, description, schema):
        return lambda fn: fn

    def create_sdk_mcp_server(self, name, version, tools):
        return {"name": name, "tools": tools}

    def ClaudeAgentOptions(self, **kw):
        self.options.append(kw)
        return kw

    def ClaudeSDKClient(self, options):
        sdk = self

        class Client:
            async def connect(self):
                return None

            async def query(self, prompt):
                self.prompt = prompt

            async def receive_response(self):
                is_error, status, text = sdk.outcomes.pop(0)
                yield AssistantMessage(text, "invalid_request" if is_error else None)
                yield ResultMessage(is_error, status, text)

            async def disconnect(self):
                return None

        return Client()


def full(s: L.LlmSettings, body: dict) -> dict:
    s._last_full = 0.0
    return asyncio.run(s.test({**body, "depth": "full"}))


def test_full_check_uses_fixed_options_and_its_own_config_dir(tmp_path):
    sdk = FakeSDK()
    s = make(tmp_path, sdk=sdk)
    s.put({"provider": "minimax", "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "minimax"})
    assert res["ok"] is True and res["check_token"] and res["thinking_detected"] == "adaptive"
    (opts,) = sdk.options
    env = opts["env"]
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "0" and env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "store" / "analyst" / "claude-check")
    assert env["ANTHROPIC_AUTH_TOKEN"] == KEY and env["ANTHROPIC_API_KEY"] == ""
    assert opts["tools"] == [] and opts["setting_sources"] == []
    assert opts["permission_mode"] == "default" and opts["strict_mcp_config"] is True
    assert opts["max_turns"] == 1 and opts["allowed_tools"] == []
    assert opts["mcp_servers"]["check"]["name"] == "check"
    assert opts["thinking"] == dict(L.ADAPTIVE_THINKING) and opts["model"] == "MiniMax-M3[1m]"
    outside_env = {k: v for k, v in opts.items()
                   if k not in ("env", "can_use_tool", "mcp_servers", "stderr")}
    assert KEY not in json.dumps(outside_env, default=str)
    assert KEY not in json.dumps(res)


def test_full_check_downgrades_thinking_once_for_auto_providers(tmp_path):
    sdk = FakeSDK((True, 400, THINKING_REJECTION), (False, None, "OK"))
    s = make(tmp_path, sdk=sdk)
    s.put({"provider": "deepseek", "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "deepseek"})
    assert res["ok"] and res["thinking_detected"] == "off" and res["thinking"] == "off"
    assert "doesn't accept extended thinking" in res["message"]
    assert sdk.options[1]["thinking"] == {"type": "disabled"}
    assert sdk.options[1]["env"]["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    view = s.put({"provider": "deepseek", "check_token": res["check_token"], "activate": True,
                  "acknowledge_non_claude": True}, 1)
    assert view["active"] == "deepseek"
    rp = s.resolve()
    assert rp.thinking == {"type": "disabled"} and rp.env["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    # A provider whose catalog pins adaptive is never downgraded.
    sdk2 = FakeSDK((True, 400, THINKING_REJECTION))
    s2 = make(tmp_path / "b", sdk=sdk2)
    s2.put({"provider": "minimax", "key": {"action": "set", "value": KEY}}, 0)
    res2 = full(s2, {"provider": "minimax"})
    assert res2["ok"] is False and res2["code"] == "invalid_request" and len(sdk2.options) == 1
    assert "Turn off extended thinking" in res2["hint"]


def test_full_check_failure_codes_and_redaction(tmp_path):
    for outcome, code in (((True, 401, f"invalid key {KEY}"), "auth"),
                          ((True, 404, "model: nope"), "model"),
                          ((True, 429, "slow down"), "rate_limit"),
                          ((True, 529, "Overloaded"), "server")):
        sdk = FakeSDK(outcome)
        s = make(tmp_path / code, sdk=sdk)
        s.put({"provider": "zai", "key": {"action": "set", "value": KEY}}, 0)
        res = full(s, {"provider": "zai"})
        assert (res["ok"], res["code"]) == (False, code)
        assert res["check_token"] is None and KEY not in json.dumps(res)


def test_full_check_uses_the_analysts_option_builder_when_wired(tmp_path):
    """Spec 6.2: with the chat service wired into the router, the check's
    options come from the analyst's own builder (ChatService.check_options)."""
    sdk = FakeSDK((True, 400, THINKING_REJECTION), (False, None, "OK"))
    calls: list[tuple] = []

    class Chat:
        def sdk_module(self):
            return sdk

        def check_options(self, rp, *, env_extra, mcp_servers, allowed_tools, max_turns,
                          stderr):
            calls.append((rp, dict(env_extra), mcp_servers, list(allowed_tools), max_turns))
            return sdk.ClaudeAgentOptions(model=rp.model, thinking=dict(rp.thinking),
                                          env={**rp.env, **env_extra}, system_prompt="ANALYST",
                                          mcp_servers=mcp_servers, max_turns=max_turns)

    s = make(tmp_path)                      # no sdk of its own: the chat's is used
    L.llm_settings_router(s, lambda: True, Chat())
    s.put({"provider": "deepseek", "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "deepseek"})
    assert res["ok"] and res["thinking_detected"] == "off"
    assert [dict(c[0].thinking) for c in calls] == [dict(L.ADAPTIVE_THINKING),
                                                    {"type": "disabled"}]
    assert calls[1][1]["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    for _rp, env, servers, allowed, turns in calls:
        assert env["CLAUDE_CODE_MAX_RETRIES"] == "0" and env["ANTHROPIC_AUTH_TOKEN"] == KEY
        assert env["CLAUDE_CONFIG_DIR"].endswith("claude-check")
        assert "check" in servers and allowed == [] and turns == 1
    assert [o["system_prompt"] for o in sdk.options] == ["ANALYST", "ANALYST"]
    assert KEY not in json.dumps(res)


class ToolUseBlock:
    def __init__(self, name: str):
        self.id, self.name, self.input = "toolu_1", name, {"word": "OK"}


def test_full_check_passes_when_the_model_calls_the_dummy_tool(tmp_path):
    """A model that answers with a well-formed call of check_echo proves the
    engine works; max_turns=1 and the refusal end the turn there."""
    class ToolSDK(FakeSDK):
        def ClaudeSDKClient(self, options):
            client = super().ClaudeSDKClient(options)

            async def receive_response():
                msg = AssistantMessage("Calling a tool.")
                msg.content.append(ToolUseBlock(L.CHECK_TOOL))
                yield msg
                res = ResultMessage(True, None, "Reached maximum number of turns (1)")
                res.subtype = "error_max_turns"
                yield res
            client.receive_response = receive_response
            return client

    s = make(tmp_path, sdk=ToolSDK())
    s.put({"provider": "custom", "base_url": "http://127.0.0.1:9/x", "model": "m-1",
           "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "custom"})
    assert res["ok"] is True and res["check_token"]
    # The same cut-off with an API error is still a failure.
    assert L._check_outcome({"result": ResultMessage(True, 401, "bad key"), "exc": None,
                             "tool_use": True, "assistant_error": None, "text": ["bad key"],
                             "status": 401, "stderr": []})[0] is False


def test_temp_stores_never_default_to_the_login_keychain(tmp_path, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("the keychain was touched")
    monkeypatch.setattr(L, "run_security", never)
    assert L.is_temp_path(tmp_path) and not L.is_temp_path("/Users/someone/Library")
    s = L.LlmSettings(tmp_path, {}, store_dir=tmp_path / "store", environ={},
                      platform="darwin")
    assert s.key_store["kind"] == "file"
    forced = L.LlmSettings(tmp_path / "k", {}, store_dir=tmp_path / "k" / "store",
                           environ={L.SECRET_STORE_ENV: "keychain"}, platform="darwin")
    assert forced.key_store["kind"] == ("keychain" if os.path.exists(L.SECURITY_BIN)
                                        else "file")


def test_test_endpoint_rules(tmp_path):
    s = make(tmp_path, sdk=FakeSDK())
    res = asyncio.run(s.test({"provider": "minimax", "depth": "quick"}))
    assert res["ok"] is False and res["code"] == "config" and res["reason"] == \
        L.REASON_KEY_MISSING
    with pytest.raises(L.SettingsError) as err:
        asyncio.run(s.test({"provider": "bedrock", "depth": "quick"}))
    assert err.value.status == 422
    s._testing = True
    with pytest.raises(L.SettingsError) as err:
        asyncio.run(s.test({"provider": "minimax", "key": KEY}))
    assert (err.value.status, err.value.error) == (429, "test_busy")
    s._testing = False
    asyncio.run(s.test({"provider": "minimax", "key": KEY, "depth": "full"}))
    with pytest.raises(L.SettingsError) as err:
        asyncio.run(s.test({"provider": "minimax", "key": KEY, "depth": "full"}))
    assert err.value.status == 429 and err.value.body()["retry_after_s"] > 0


# -------------------------------------------------------- activation / gen --

def test_activation_needs_a_matching_check_and_the_acknowledgement(tmp_path):
    s = make(tmp_path, sdk=FakeSDK(*[(False, None, "OK")] * 4))
    gen0 = s.generation
    s.put({"provider": "minimax", "key": {"action": "set", "value": KEY}}, 0)
    assert s.generation == gen0  # the active provider (login) did not change
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax", "activate": True, "acknowledge_non_claude": True}, 1)
    assert err.value.body()["error"] == "needs_check"
    token = full(s, {"provider": "minimax"})["check_token"]
    with pytest.raises(L.SettingsError) as err:  # the check vouched for another model
        s.put({"provider": "minimax", "model": "MiniMax-M2.7", "check_token": token,
               "activate": True, "acknowledge_non_claude": True}, 1)
    assert err.value.body()["error"] == "needs_check"
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax", "check_token": token, "activate": True}, 1)
    assert err.value.body()["error"] == "needs_ack"
    view = s.put({"provider": "minimax", "check_token": token, "activate": True,
                  "acknowledge_non_claude": True}, 1)
    mm = next(p for p in view["providers"] if p["id"] == "minimax")
    assert view["active"] == "minimax" and mm["status"] == "active" and mm["tested"]["current"]
    assert s.generation == gen0 + 1
    rp = s.resolve()
    assert (rp.id, rp.ready, rp.generation) == ("minimax", True, gen0 + 1)
    identity = rp.identity
    # Model change on the active provider (checked): new generation, same identity.
    token = full(s, {"provider": "minimax", "model": "MiniMax-M2.7"})["check_token"]
    s.put({"provider": "minimax", "model": "MiniMax-M2.7", "check_token": token}, 2)
    rp2 = s.resolve()
    assert rp2.generation == gen0 + 2 and rp2.identity == identity
    assert rp2.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "MiniMax-M2.7"
    # Endpoint change: new identity.
    cn = "https://api.minimax.cn/anthropic"
    token = full(s, {"provider": "minimax", "base_url": cn})["check_token"]
    s.put({"provider": "minimax", "base_url": cn, "check_token": token}, 3)
    rp3 = s.resolve()
    assert rp3.identity != identity and rp3.env["ANTHROPIC_BASE_URL"].endswith(".cn/anthropic")
    # Removing the active provider's key: not ready, new generation.
    s.delete_key("minimax", 4)
    rp4 = s.resolve()
    assert (rp4.ready, rp4.reason) == (False, L.REASON_KEY_MISSING)
    assert rp4.generation == gen0 + 4 and rp4.env == {}
    assert s.status()["reason"] == L.REASON_KEY_MISSING


def test_login_activation_needs_no_check_and_mixed_ack_depends_on_model(tmp_path):
    s = make(tmp_path, sdk=FakeSDK(*[(False, None, "OK")] * 2))
    s.put({"provider": "openrouter", "key": {"action": "set", "value": KEY}}, 0)
    token = full(s, {"provider": "openrouter"})["check_token"]
    view = s.put({"provider": "openrouter", "check_token": token, "activate": True}, 1)
    assert view["active"] == "openrouter"  # anthropic/ model: no acknowledgement needed
    token = full(s, {"provider": "openrouter", "model": "moonshotai/kimi-k3"})["check_token"]
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "openrouter", "model": "moonshotai/kimi-k3", "check_token": token,
               "activate": True}, 2)
    assert err.value.body()["error"] == "needs_ack"
    view = s.put({"provider": "anthropic_login", "activate": True}, 2)
    assert view["active"] == "anthropic_login"


def test_locks_from_the_environment(tmp_path):
    s = make(tmp_path, {"GODSEYE_LLM_PROVIDER": "anthropic_login"}, model="claude-sonnet-5",
             environ={"GODSEYE_CHAT_EFFORT": "high"})
    view = s.view()
    assert view["locks"]["provider"] == "anthropic_login"
    assert view["locks"]["model"] == "claude-sonnet-5"
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "minimax", "activate": True}, 0)
    assert err.value.body() == {"error": "locked_by_environment", "field": "provider",
                                "env": "GODSEYE_LLM_PROVIDER"}
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "anthropic_login", "model": "claude-haiku-4-5"}, 0)
    assert err.value.body()["field"] == "model"
    rp = s.resolve()
    assert (rp.model, rp.effort) == ("claude-sonnet-5", "high")
    s2 = make(tmp_path / "k", {"DEEPSEEK_API_KEY": KEY})
    with pytest.raises(L.SettingsError) as err:
        s2.delete_key("deepseek", 0)
    assert err.value.body() == {"error": "locked_by_environment", "field": "key",
                                "env": "DEEPSEEK_API_KEY"}


# ------------------------------------------------------------------- routes --

TOKEN = "dev-token-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JSON_AUTH = {**AUTH, "Content-Type": "application/json"}
DEV_ORIGIN = "http://localhost:5173"


def app_for(settings: L.LlmSettings):
    from fastapi import FastAPI
    from godseye_uav.host import LoopbackHostMiddleware, bearer_auth
    from starlette.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.include_router(L.llm_settings_router(settings, bearer_auth(TOKEN)))

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    # Same order as the host: the bridge's CORS innermost, then the guards.
    app.add_middleware(CORSMiddleware, allow_origins=[DEV_ORIGIN], allow_methods=["*"],
                       allow_headers=["*"])
    app.add_middleware(L.SettingsGuardMiddleware)
    app.add_middleware(LoopbackHostMiddleware)
    return app


@pytest.fixture
def api(tmp_path):
    from fastapi.testclient import TestClient
    s = make(tmp_path, sdk=FakeSDK(*[(False, None, "OK")] * 4))
    with TestClient(app_for(s), base_url="http://127.0.0.1:8780") as client:
        client.settings = s
        yield client


def no_secret(resp, secret: str = KEY) -> None:
    assert secret not in resp.text and secret[:12] not in resp.text
    for k, v in resp.headers.items():
        assert secret not in k and secret not in v


def test_get_is_masked_and_never_cached(api):
    r = api.get("/settings/llm", headers=AUTH)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["schema"] == L.SCHEMA and body["active"] == "anthropic_login"
    assert [p["id"] for p in body["providers"]] == list(catalog.PROVIDER_IDS)
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "0"},
                json={"provider": "minimax", "key": {"action": "set", "value": KEY}})
    assert r.status_code == 200
    no_secret(r)
    r = api.get("/settings/llm", headers=AUTH)
    no_secret(r)
    mm = next(p for p in r.json()["providers"] if p["id"] == "minimax")
    assert mm["key"] == {"configured": True, "source": "memory", "masked": "…-123",
                         "env": None}
    assert mm["status"] == "key_saved"


def test_put_delete_and_conflicts(api):
    r = api.put("/settings/llm", headers=JSON_AUTH, json={"provider": "minimax"})
    assert r.status_code == 428
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "0"},
                json={"provider": "minimax", "key": {"action": "set", "value": KEY}})
    assert r.json()["rev"] == 1
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "0"},
                json={"provider": "minimax"})
    assert r.status_code == 409 and r.json() == {"error": "settings_conflict", "rev": 1}
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "1"},
                json={"provider": "minimax", "model": "-rf"})
    assert r.status_code == 422 and r.json()["field"] == "model"
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "1"},
                json={"provider": "minimax", "activate": True, "acknowledge_non_claude": True})
    assert r.status_code == 409 and r.json()["error"] == "needs_check"
    r = api.delete("/settings/llm/providers/minimax/key", headers={**AUTH, "If-Match": "1"})
    assert r.status_code == 200
    mm = next(p for p in r.json()["providers"] if p["id"] == "minimax")
    assert mm["key"]["configured"] is False and mm["status"] == "not_configured"
    r = api.delete("/settings/llm/providers/nope/key", headers={**AUTH, "If-Match": "2"})
    assert r.status_code == 404
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "2"}, content=b"{nope")
    assert r.status_code == 422


def test_full_check_then_activate_over_http(api):
    api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "0"},
            json={"provider": "minimax", "key": {"action": "set", "value": KEY}})
    r = api.post("/settings/llm/test", headers=JSON_AUTH,
                 json={"provider": "minimax", "depth": "full"})
    assert r.status_code == 200 and r.json()["ok"] is True
    token = r.json()["check_token"]
    r = api.put("/settings/llm", headers={**JSON_AUTH, "If-Match": "1"},
                json={"provider": "minimax", "check_token": token, "activate": True,
                      "acknowledge_non_claude": True})
    assert r.status_code == 200 and r.json()["active"] == "minimax"
    assert api.settings.resolve().id == "minimax"


def test_auth_is_header_only(api):
    assert api.get("/settings/llm").status_code == 401
    assert api.get("/settings/llm", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = api.get(f"/settings/llm?token={TOKEN}")
    assert r.status_code in (400, 401)
    r = api.get(f"/settings/llm?token={TOKEN}", headers=AUTH)
    assert r.status_code == 400 and r.json()["error"] == "token_in_url"


def test_cross_origin_is_refused_and_gets_no_cors_headers(api):
    for origin in ("http://evil.example", DEV_ORIGIN, "null", "http://127.0.0.1:9999"):
        r = api.get("/settings/llm", headers={**AUTH, "Origin": origin})
        assert r.status_code == 403 and r.json() == {"error": "cross_origin"}, origin
        assert not any(k.lower().startswith("access-control-") for k in r.headers)
    pre = api.options("/settings/llm", headers={
        "Origin": DEV_ORIGIN, "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert pre.status_code == 403 and "access-control-allow-origin" not in pre.headers
    # The same dev origin still gets CORS elsewhere: the guard is path-scoped.
    ping = api.get("/api/ping", headers={"Origin": DEV_ORIGIN})
    assert ping.headers.get("access-control-allow-origin") == DEV_ORIGIN
    r = api.get("/settings/llm", headers={**AUTH, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    for origin in ("http://127.0.0.1:8780", "http://localhost:8780"):
        r = api.get("/settings/llm", headers={**AUTH, "Origin": origin,
                                              "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


def test_foreign_host_and_non_json_writes_are_refused(api):
    r = api.get("/settings/llm", headers={**AUTH, "Host": "evil.example:8780"})
    assert r.status_code == 400
    r = api.put("/settings/llm", headers={**AUTH, "If-Match": "0",
                                          "Content-Type": "text/plain"},
                content=json.dumps({"provider": "minimax"}))
    assert r.status_code == 415 and r.json() == {"error": "json_required"}
    r = api.post("/settings/llm/test", headers={**AUTH, "Content-Type":
                                                "application/x-www-form-urlencoded"},
                 content="provider=minimax")
    assert r.status_code == 415


def test_test_route_never_echoes_the_key(api, stub):
    r = api.post("/settings/llm/test", headers=JSON_AUTH, json={
        "provider": "custom", "base_url": stub.base("echo_key"), "model": "m1",
        "key": KEY, "depth": "quick"})
    assert r.status_code == 200 and r.json()["code"] == "auth"
    no_secret(r)
    r = api.post("/settings/llm/test", headers=JSON_AUTH, json={
        "provider": "custom", "base_url": stub.url, "model": "m1", "key": KEY})
    assert r.json()["ok"] is True
    no_secret(r)
    assert stub.requests[-1]["auth"]["authorization"]["kind"] == f"test:{KEY}"


def test_classify_refused_connection_is_network_not_server():
    text = ("API Error: Connection refused — a firewall or proxy may be blocking it "
            "(ECONNREFUSED)")
    assert L.classify(None, "server_error", text) == "network"
    assert L.classify(529, "server_error", "Overloaded") == "server"
    assert L.classify(None, "server_error", "API Error: 500 upstream timed out") == "server"
    assert L.classify(401, "invalid_request", "whatever") == "auth"
    assert L.classify(None, "model_not_found", "") == "model"


def test_key_rules_for_providers_without_a_saved_key(tmp_path):
    fake = FakeSecurity()
    s = with_fake_keychain(tmp_path, fake)
    for pid in ("vertex", "anthropic_login"):
        with pytest.raises(L.SettingsError) as err:
            s.put({"provider": pid, "key": {"action": "set", "value": KEY}}, 0)
        assert err.value.status == 422 and err.value.field == "key"
    view = s.delete_key("minimax", 0)  # nothing stored: no keychain call, no error
    assert view["rev"] == 1 and fake.calls == []
    view = s.put({"provider": "bedrock", "fields": {"region": "eu-west-1"},
                  "key": {"action": "set", "value": KEY}}, 1)
    bedrock = next(p for p in view["providers"] if p["id"] == "bedrock")
    assert bedrock["key"]["source"] == "keychain" and bedrock["host"] == \
        "bedrock-runtime.eu-west-1.amazonaws.com"
    rp = s._resolve("bedrock", s._doc)
    assert rp.env["AWS_BEARER_TOKEN_BEDROCK"] == KEY and rp.env["AWS_REGION"] == "eu-west-1"


# ------------------------------------------------- review fixes (secrets lens) --

@pytest.fixture
def redirector():
    """(redirecting stub, target stub on another origin)."""
    with StubAnthropic() as target, StubAnthropic() as red:
        red.configure(mode="redirect", redirect_to=target.url)
        yield red, target


@pytest.mark.parametrize("scheme", ["x-api-key", "bearer"])
def test_full_check_refuses_an_endpoint_that_redirects_before_the_engine_runs(
        tmp_path, redirector, scheme):
    """The CLI follows a 307 and re-sends x-api-key to the new origin: the full
    check must fail like the quick check, mint no token, never run the engine."""
    red, target = redirector
    sdk = FakeSDK()
    s = make(tmp_path, sdk=sdk)
    s.put({"provider": "custom", "base_url": red.url, "model": "stub-model",
           "auth_scheme": scheme, "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "custom"})
    assert (res["ok"], res["code"], res["status"]) == (False, "redirect", 307), res
    assert res["check_token"] is None and "wasn't sent on" in res["message"]
    assert sdk.options == [] and target.requests == []
    (probe_req,) = red.requests
    assert probe_req["path"] == "/v1/messages" and probe_req["body"]["messages"] == 0
    assert (probe_req["auth"]["x_api_key"] if scheme == "x-api-key"
            else probe_req["auth"]["authorization"]["kind"]) == f"test:{KEY}"
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "custom", "activate": True, "acknowledge_non_claude": True,
               "check_token": res["check_token"]}, s.rev)
    assert err.value.error == "needs_check"
    assert KEY not in json.dumps(res)


def test_full_check_probes_url_kinds_only_and_passes_a_plain_endpoint(tmp_path, stub):
    sdk = FakeSDK(*[(False, None, "OK")] * 2)
    s = make(tmp_path, sdk=sdk)
    s.put({"provider": "custom", "base_url": stub.url, "model": "stub-model",
           "key": {"action": "set", "value": KEY}}, 0)
    res = full(s, {"provider": "custom"})
    assert res["ok"] is True and res["check_token"] and len(sdk.options) == 1
    assert [r["user_agent"] for r in stub.probes()] == ["eye-in-the-sky-redirect-check/1"]
    PROBES.clear()
    s.put({"provider": "anthropic_api", "key": {"action": "set", "value": KEY}}, s.rev)
    assert full(s, {"provider": "anthropic_api"})["ok"] is True
    assert PROBES == []                      # api.anthropic.com: nothing to probe


def test_preflight_refuses_a_redirecting_endpoint_before_the_analyst_spawns(
        tmp_path, redirector, stub):
    _red, target = redirector
    s = make(tmp_path, sdk=FakeSDK())
    s.put({"provider": "custom", "base_url": stub.url, "model": "stub-model",
           "auth_scheme": "x-api-key", "key": {"action": "set", "value": KEY}}, 0)
    token = full(s, {"provider": "custom"})["check_token"]
    s.put({"provider": "custom", "check_token": token, "activate": True,
           "acknowledge_non_claude": True}, s.rev)
    assert asyncio.run(s.preflight(s.resolve())) is None
    # The endpoint starts redirecting after it was checked.
    stub.configure(mode="redirect", redirect_to=target.url)
    refusal = asyncio.run(s.preflight(s.resolve()))
    assert refusal["code"] == "config" and refusal["status"] == 307
    assert refusal["message"] == (f"127.0.0.1:{stub.port} tried to redirect the request. "
                                  "The key wasn't sent on.")
    assert target.requests == [] and KEY not in json.dumps(refusal)
    login = s._resolve("anthropic_login", s._doc)
    PROBES.clear()
    assert asyncio.run(s.preflight(login)) is None and PROBES == []


def _in_use(tmp_path, pid: str, model: str, **extra) -> L.LlmSettings:
    """``pid`` checked and in use, acknowledged only if its model needs it
    (what the UI sends)."""
    s = make(tmp_path, sdk=FakeSDK(*[(False, None, "OK")] * 6))
    s.put({"provider": pid, "model": model, "key": {"action": "set", "value": KEY}, **extra},
          0)
    token = full(s, {"provider": pid})["check_token"]
    s.put({"provider": pid, "check_token": token, "activate": True}, s.rev)
    assert s.resolve().id == pid
    return s


def test_save_on_the_provider_in_use_cannot_skip_the_check_or_the_ack(tmp_path):
    """Spec 9.11 / 10: Save never activates. An unchecked config (or a new key)
    for the provider in use is refused; the analyst keeps the checked one."""
    s = _in_use(tmp_path, "openrouter", "anthropic/claude-opus-5.5")
    gen = s.generation
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "openrouter", "model": "openai/gpt-6-sol", "activate": False},
              s.rev)
    assert (err.value.status, err.value.error) == (409, "needs_check")
    rp = s.resolve()
    assert (rp.model, rp.ready, s.generation) == ("anthropic/claude-opus-5.5", True, gen)
    token = full(s, {"provider": "openrouter", "model": "openai/gpt-6-sol"})["check_token"]
    with pytest.raises(L.SettingsError) as err:  # checked, but a non-Claude model: ack again
        s.put({"provider": "openrouter", "model": "openai/gpt-6-sol", "check_token": token},
              s.rev)
    assert err.value.error == "needs_ack"
    view = s.put({"provider": "openrouter", "model": "openai/gpt-6-sol", "check_token": token,
                  "acknowledge_non_claude": True}, s.rev)
    assert view["active"] == "openrouter" and s.resolve().model == "openai/gpt-6-sol"
    assert s.generation == gen + 1
    # Saving what is already in use (the UI sends the whole form) is fine.
    s.put({"provider": "openrouter", "model": "openai/gpt-6-sol", "small_model": None,
           "thinking": "auto", "allow_insecure_http": False, "fields": {}}, s.rev)
    # A new key for the provider in use needs a check too; clearing it just stops it.
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "openrouter", "key": {"action": "set", "value": KEY2}}, s.rev)
    assert err.value.error == "needs_check"
    assert s.resolve().env["ANTHROPIC_AUTH_TOKEN"] == KEY
    s.put({"provider": "openrouter", "key": {"action": "clear"}}, s.rev)
    assert (s.resolve().ready, s.resolve().reason) == (False, L.REASON_KEY_MISSING)


def test_save_on_the_custom_endpoint_in_use_needs_a_check(tmp_path):
    s = _in_use(tmp_path, "custom", "stub-model", base_url="http://127.0.0.1:9",
                model_family="claude")
    for change in ({"base_url": "http://127.0.0.1:10"}, {"model_family": "non_claude"},
                   {"auth_scheme": "x-api-key"}, {"thinking": "off"}):
        with pytest.raises(L.SettingsError) as err:
            s.put({"provider": "custom", **change}, s.rev)
        assert err.value.error == "needs_check", change
    rp = s.resolve()
    assert rp.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9" and rp.model_family == "claude"
    # Another provider's draft is saved without a check, as before.
    s.put({"provider": "minimax", "model": "MiniMax-M2.7"}, s.rev)
    assert s.resolve().id == "custom"


def test_resolve_fails_closed_when_the_ack_does_not_match_the_family(tmp_path):
    s = _in_use(tmp_path, "openrouter", "anthropic/claude-opus-5.5")
    doc = json.loads(json.dumps(s._doc))       # a hand-edited or stale settings file
    doc["providers"]["openrouter"]["model"] = "openai/gpt-6-sol"
    s._doc = doc
    rp = s.resolve()
    assert (rp.ready, rp.reason, rp.env) == (False, L.REASON_NOT_CONFIGURED, {})
    assert s.status()["ready"] is False
    doc["providers"]["openrouter"].update(ack_non_claude_at_ms=1, ack_family="mixed")
    assert s.resolve().ready is True


def test_an_ack_sent_for_a_claude_model_does_not_cover_a_later_non_claude_one(tmp_path):
    s = make(tmp_path, sdk=FakeSDK(*[(False, None, "OK")] * 2))
    s.put({"provider": "openrouter", "key": {"action": "set", "value": KEY}}, 0)
    token = full(s, {"provider": "openrouter"})["check_token"]
    s.put({"provider": "openrouter", "check_token": token, "activate": True,
           "acknowledge_non_claude": True}, s.rev)   # not needed: not recorded
    token = full(s, {"provider": "openrouter", "model": "moonshotai/kimi-k3"})["check_token"]
    with pytest.raises(L.SettingsError) as err:
        s.put({"provider": "openrouter", "model": "moonshotai/kimi-k3", "check_token": token},
              s.rev)
    assert err.value.error == "needs_ack"


def _key_everywhere(tmp_path, fake: FakeSecurity, pid: str = "minimax") -> list[str]:
    where = [acct for acct in fake.items if acct.endswith(f":{pid}")]
    path = tmp_path / L.SECRETS_FILE
    if path.exists() and pid in json.loads(path.read_text())["keys"]:
        where.append("file")
    return where


def test_replace_and_remove_leave_no_copy_in_any_backend(tmp_path):
    """A key that fell back to the file (keychain locked) must not survive a
    later keychain save or "Remove key", and the other way round."""
    fake = FakeSecurity(fail_add=True)
    s = with_fake_keychain(tmp_path, fake)
    body = {"provider": "minimax", "key": {"action": "set", "value": KEY}}
    s.put(body, s.rev)
    assert _key_everywhere(tmp_path, fake) == ["file"]
    fake.fail_add = False
    view = s.put(body, s.rev)                                  # "Replace key"
    mm = next(p for p in view["providers"] if p["id"] == "minimax")
    assert mm["key"]["source"] == "keychain"
    assert len(_key_everywhere(tmp_path, fake)) == 1 and "file" not in _key_everywhere(
        tmp_path, fake)
    s.delete_key("minimax", s.rev)                             # "Remove key"
    assert _key_everywhere(tmp_path, fake) == [] and fake.items == {}
    # Keychain first, then a fallback save: the keychain copy goes too.
    s.put({"provider": "minimax", "key": {"action": "set", "value": KEY2}}, s.rev)
    fake.fail_add = True
    s.put(body, s.rev)
    assert _key_everywhere(tmp_path, fake) == ["file"]
    s.put({"provider": "minimax", "key": {"action": "clear"}}, s.rev)
    assert _key_everywhere(tmp_path, fake) == []
    assert KEY not in (tmp_path / L.SECRETS_FILE).read_text()


def test_remove_key_reports_a_copy_it_could_not_delete(tmp_path):
    fake = FakeSecurity(fail_add=True)
    s = with_fake_keychain(tmp_path, fake)
    s.put({"provider": "minimax", "key": {"action": "set", "value": KEY}}, 0)
    fake.items[f"{s._keychain_scope()}:minimax"] = KEY        # a copy the sweep can't reach
    real = fake.__call__

    def broken(argv, stdin, timeout):
        if argv[1] == "delete-generic-password":
            return 51, "", "security: SecKeychainItemDelete: User interaction is not allowed."
        return real(argv, stdin, timeout)
    s._backends["keychain"]._run = broken
    with pytest.raises(L.SettingsError) as err:
        s.delete_key("minimax", s.rev)
    assert (err.value.status, err.value.error) == (500, "key_store_failed")
    mm = next(p for p in s.view()["providers"] if p["id"] == "minimax")
    assert mm["key"]["configured"] is True                     # not reported as gone


def test_delete_key_without_a_stored_key_still_sweeps_the_file(tmp_path):
    fake = FakeSecurity()
    s = with_fake_keychain(tmp_path, fake)
    L.FileSecrets(tmp_path / L.SECRETS_FILE).set("zai", KEY)   # residue of an older version
    s.put({"provider": "minimax", "model": "MiniMax-M2.7"}, 0)
    s.delete_key("zai", s.rev)
    assert KEY not in (tmp_path / L.SECRETS_FILE).read_text()


#: CLAUDE_CODE_USE_* names in the bundled CLI that pick no model provider.
NOT_PROVIDER_SWITCHES = frozenset({
    "CLAUDE_CODE_USE_CCR_V2", "CLAUDE_CODE_USE_COWORK_PLUGINS",
    "CLAUDE_CODE_USE_NATIVE_FILE_SEARCH", "CLAUDE_CODE_USE_POWERSHELL_TOOL"})
ROUTING_VARS = ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
                "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID", "ANTHROPIC_AWS_BASE_URL",
                "ANTHROPIC_BEDROCK_MANTLE_BASE_URL", "CLAUDE_CODE_USE_GATEWAY",
                "CLAUDE_CODE_GATEWAY_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                "ANTHROPIC_UNIX_SOCKET")


def test_every_routing_variable_is_captured_and_every_switch_blanked(tmp_path):
    """An inherited CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD sent a BYOK run to
    Google's metadata server and oauth2.googleapis.com instead of the endpoint."""
    assert set(ROUTING_VARS) <= set(L.CAPTURED_VARS)
    assert {"CLAUDE_CODE_GATEWAY_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN"} <= L.SECRET_VARS
    for switch in ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "CLAUDE_CODE_USE_GATEWAY"):
        assert L.BLANK[switch] == ""
    environ = {name: "1" for name in ROUTING_VARS}
    environ["CLAUDE_CODE_GATEWAY_TOKEN"] = "test-key-gateway-1"
    captured = L.capture_llm_env(environ)
    assert environ == {} and set(captured) == set(ROUTING_VARS)
    assert "test-key-gateway-1" not in L.redact("x test-key-gateway-1 y")
    for pid in ("anthropic_login", "custom"):
        env = build(pid, tmp=tmp_path)[0]
        assert env["CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD"] == ""


def _cli_switches() -> set[str] | None:
    import importlib.util
    spec = importlib.util.find_spec("claude_agent_sdk")
    cli = pathlib.Path(spec.origin).parent / "_bundled" / "claude" if spec and spec.origin \
        else None
    strings = shutil.which("strings")
    if cli is None or not cli.is_file() or strings is None:
        return None
    out = subprocess.run([strings, str(cli)], capture_output=True, check=False).stdout
    return {m.decode() for m in re.findall(rb"CLAUDE_CODE_USE_[A-Z0-9_]*[A-Z0-9]", out)}


def test_every_provider_switch_in_the_bundled_cli_is_blanked_and_captured():
    found = _cli_switches()
    if not found:
        pytest.skip("needs the bundled CLI and strings(1)")
    switches = found - NOT_PROVIDER_SWITCHES
    assert switches - set(L.BLANK) == set(), "a new provider switch: add it to BLANK"
    assert switches - set(L.CAPTURED_VARS) == set()
