"""Offline end-to-end BYOK checks: the real bundled Claude CLI (through the
Agent SDK) against a local stub endpoint (BYOK spec section 11.3).

Every run: fake keys only, the child environment scrubbed to
``HOME USER LOGNAME PATH TMPDIR LANG``, ``HTTPS_PROXY``/``HTTP_PROXY`` pointed
at a deny-all logging proxy with ``NO_PROXY=127.0.0.1,localhost``. Nothing
can reach a third party: the stub is loopback and the proxy refuses the rest.

Skipped without ``claude_agent_sdk`` or its bundled CLI, or with
``GODSEYE_LIVE_CLI=0``. ``GODSEYE_LIVE_IDLE_S`` (default 5) holds the analyst
session open after its turn to catch late egress (the spec's E1 gate uses 200).

The ChatService scenarios (E12-E14: hostile model, approvals, provider switch,
tool round trip) belong to tests/test_chat.py once ChatService takes ``llm=``.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from godseye_uav import llm_settings as L
from support.deny_proxy import DenyProxy
from support.stub_anthropic import StubAnthropic


def _cli() -> pathlib.Path | None:
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or not spec.origin:
        return None
    cli = pathlib.Path(spec.origin).parent / "_bundled" / "claude"
    return cli if cli.is_file() and os.access(cli, os.X_OK) else None


CLI = _cli()
pytestmark = pytest.mark.skipif(
    CLI is None or os.environ.get("GODSEYE_LIVE_CLI") == "0",
    reason="needs claude_agent_sdk with its bundled CLI (GODSEYE_LIVE_CLI=0 skips)")

KEY = "test-key-123"
OTHER = "test-key-456"
IDLE_S = float(os.environ.get("GODSEYE_LIVE_IDLE_S", "5"))
KEEP = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG")
_LOOPBACK = re.compile(r"^(127\.\d+\.\d+\.\d+|\[?::1\]?|localhost)(:\d+)?$")


@pytest.fixture
def net(monkeypatch):
    """(stub, proxy) with the process environment scrubbed for the child."""
    with StubAnthropic() as stub, DenyProxy() as proxy:
        for name in list(os.environ):
            if name not in KEEP:
                monkeypatch.delenv(name)
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
        yield stub, proxy


@pytest.fixture(autouse=True)
def _fresh_secret_registry(monkeypatch):
    monkeypatch.setattr(L, "LIVE_SECRETS", L.SecretRegistry())


def settings(tmp_path: pathlib.Path, llm_env: dict | None = None) -> L.LlmSettings:
    return L.LlmSettings(tmp_path, llm_env or {}, store_dir=tmp_path / "store",
                         secrets=L.MemorySecrets(), environ={}, cli_path=str(CLI))


def use_custom(s: L.LlmSettings, base: str, *, scheme: str = "bearer", key: str | None = KEY,
               model: str = "stub-model") -> None:
    body = {"provider": "custom", "base_url": base, "model": model, "auth_scheme": scheme}
    if key:
        body["key"] = {"action": "set", "value": key}
    s.put(body, s.rev)


def check(s: L.LlmSettings, pid: str = "custom", **body) -> dict:
    s._last_full = 0.0
    seen: dict = {}

    def on_connect(client) -> None:
        seen["argv"] = child_argv(client)

    res = asyncio.run(s.test({"provider": pid, "depth": "full", **body}, on_connect=on_connect))
    res["_argv"] = seen.get("argv", "")
    return res


def child_argv(client) -> str:
    proc = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(proc, "pid", None)
    if not pid:
        return ""
    return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                          text=True, check=False).stdout


def remote_sockets(pid: int) -> set[str]:
    """Non-loopback peers of ``pid``'s sockets (lsof), for late-egress checks."""
    out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-i"], capture_output=True,
                         text=True, check=False).stdout
    peers = set()
    for line in out.splitlines()[1:]:
        m = re.search(r"->(\S+)", line)
        if m and not _LOOPBACK.match(m.group(1)):
            peers.add(m.group(1))
    return peers


def credentials_seen(stub: StubAnthropic) -> set[str]:
    kinds = set()
    for r in stub.requests:
        a = r["auth"]
        if a["authorization"]:
            kinds.add(f"authorization:{a['authorization']['scheme']}:"
                      f"{a['authorization']['kind']}")
        if a["x_api_key"]:
            kinds.add(f"x-api-key:{a['x_api_key']}")
    return kinds


def assert_only_the_test_key(stub: StubAnthropic, header: str = "bearer") -> None:
    assert stub.messages(), "the stub saw no /v1/messages request"
    expected = ({f"authorization:Bearer:test:{KEY}"} if header == "bearer"
                else {f"x-api-key:test:{KEY}"})
    assert credentials_seen(stub) == expected
    for r in stub.requests:
        assert r["auth"]["authorization"] is None or header == "bearer"
        assert r["auth"]["x_api_key"] is None or header == "x-api-key"
        assert not r["body"].get("account_uuid_present"), "an account id reached the stub"


async def analyst_turn(rp: L.ResolvedProvider, cwd: pathlib.Path, *,
                       idle_s: float = 0.0) -> dict:
    """One turn with the analyst's option shape: the provider fields from
    ``provider_options`` and the fixed, provider-independent rest."""
    import claude_agent_sdk as sdk

    stderr: list[str] = []

    async def deny(name, tool_input, ctx):
        return sdk.PermissionResultDeny(message="denied in test", interrupt=False)

    cwd.mkdir(parents=True, exist_ok=True)
    kw = {**L.provider_options(rp), "system_prompt": "You are a test analyst. Reply OK.",
          "tools": [], "allowed_tools": [], "setting_sources": [], "strict_mcp_config": True,
          "verbatim_prompts": True, "permission_mode": "default", "can_use_tool": deny,
          "max_turns": 2, "cwd": str(cwd), "stderr": stderr.append, "cli_path": str(CLI)}
    out: dict = {"result": None, "argv": "", "peers": set(), "stderr": stderr}
    client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**kw))
    await client.connect()
    try:
        out["argv"] = child_argv(client)
        pid = getattr(getattr(client._transport, "_process", None), "pid", None)
        await client.query("Say OK.")
        async for msg in client.receive_response():
            if type(msg).__name__ == "ResultMessage":
                out["result"] = msg
        deadline = time.monotonic() + idle_s
        while pid and time.monotonic() < deadline:
            out["peers"] |= remote_sockets(pid)
            await asyncio.sleep(1.0)
        if pid:
            out["peers"] |= remote_sockets(pid)
    finally:
        await client.disconnect()
    return out


def files_containing(root: pathlib.Path, needle: str) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if needle in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(root)))
            except OSError:
                continue
    return hits


# ------------------------------------------------------------------ E1 - E3 --

def test_e1_bearer_run_sends_only_the_test_key_and_nothing_leaves(tmp_path, net):
    stub, proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.url)
    res = check(s)
    assert res["ok"] is True, res
    assert KEY not in res["_argv"] and res["_argv"]
    assert_only_the_test_key(stub)
    assert [r["body"]["thinking"]["type"] for r in stub.messages()] == ["adaptive"]
    token = res["check_token"]
    s.put({"provider": "custom", "check_token": token, "activate": True,
           "acknowledge_non_claude": True}, s.rev)
    rp = s.resolve()
    assert rp.id == "custom" and rp.ready
    stub.reset()
    out = asyncio.run(analyst_turn(rp, tmp_path / "store" / "analyst", idle_s=IDLE_S))
    assert out["result"] is not None and out["result"].is_error is False
    assert KEY not in out["argv"]
    assert_only_the_test_key(stub)
    msgs = stub.messages()
    assert any(r["body"]["thinking"] and r["body"]["thinking"].get("type") == "adaptive"
               for r in msgs)
    assert proxy.attempts == [], proxy.attempts
    assert out["peers"] == set(), out["peers"]
    home = tmp_path / "store" / "analyst" / "claude-home"
    assert list((home / "projects").rglob("*.jsonl")), "no transcript under claude-home"
    assert files_containing(home, KEY) == []
    assert files_containing(home, KEY[-12:]) == []


def test_e2_x_api_key_run_sends_only_x_api_key(tmp_path, net):
    stub, proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.url, scheme="x-api-key")
    res = check(s)
    assert res["ok"] is True, res
    assert_only_the_test_key(stub, header="x-api-key")
    assert proxy.attempts == [], proxy.attempts
    check_home = tmp_path / "store" / "analyst" / "claude-check"
    assert files_containing(check_home, KEY) == []


def test_e3_launch_key_is_neither_inherited_nor_sent(tmp_path, net, monkeypatch):
    stub, proxy = net
    environ = {"ANTHROPIC_API_KEY": OTHER}
    captured = L.capture_llm_env(environ)
    assert environ == {} and captured == {"ANTHROPIC_API_KEY": OTHER}
    # Worst case: the launch key was never popped from the process environment.
    monkeypatch.setenv("ANTHROPIC_API_KEY", OTHER)
    s = settings(tmp_path, captured)
    use_custom(s, stub.url)
    res = check(s)
    assert res["ok"] is True, res
    assert_only_the_test_key(stub)
    assert OTHER not in json.dumps(stub.requests)
    assert proxy.attempts == []


# ------------------------------------------------------------------ E4 - E11 --

def test_e4_switch_back_to_login_sends_nothing_to_the_old_endpoint(tmp_path, net):
    stub, _proxy = net
    empty_config = tmp_path / "empty-claude-config"  # no keychain login is keyed to it
    empty_config.mkdir()
    s = settings(tmp_path, {"CLAUDE_CONFIG_DIR": str(empty_config)})
    use_custom(s, stub.url)
    token = check(s)["check_token"]
    s.put({"provider": "custom", "check_token": token, "activate": True,
           "acknowledge_non_claude": True}, s.rev)
    stub.reset()
    s.put({"provider": "anthropic_login", "activate": True}, s.rev)
    rp = s.resolve()
    assert rp.kind == "anthropic_login" and "ANTHROPIC_BASE_URL" not in rp.env
    assert rp.env["CLAUDE_CONFIG_DIR"] == str(empty_config)
    res = check(s, "anthropic_login")
    assert res["ok"] is False, res
    assert stub.requests == []


def test_e5_empty_key_never_spawns(tmp_path, net):
    stub, _proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.url, key=None)
    res = check(s)
    assert (res["ok"], res["code"], res["reason"]) == (False, "config", L.REASON_KEY_MISSING)
    assert res["_argv"] == "" and stub.requests == []
    assert s._resolve("custom", s._doc).env == {}


@pytest.mark.parametrize("mode,code,retryable", [("401", "auth", False),
                                                 ("404", "model", False),
                                                 ("429", "rate_limit", True)])
def test_e6_e8_provider_errors_are_classified_fast(tmp_path, net, mode, code, retryable):
    stub, proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.base(mode))
    t0 = time.monotonic()
    res = check(s)
    assert time.monotonic() - t0 < 15
    assert (res["ok"], res["code"], res["retryable"]) == (False, code, retryable), res
    assert KEY not in json.dumps(res)
    assert proxy.attempts == []


def test_e9_connection_refused_is_a_network_error(tmp_path, net):
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    s = settings(tmp_path)
    use_custom(s, f"http://127.0.0.1:{port}")
    t0 = time.monotonic()
    res = check(s)
    assert time.monotonic() - t0 < 15
    assert (res["ok"], res["code"]) == (False, "network"), res
    assert res["message"] == f"Couldn't reach 127.0.0.1:{port}."


def test_e10_thinking_rejection_downgrades_and_the_next_turn_sends_none(tmp_path, net):
    stub, _proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.base("reject_thinking"))
    res = check(s)
    assert res["ok"] is True and res["thinking_detected"] == "off", res
    s.put({"provider": "custom", "check_token": res["check_token"], "activate": True,
           "acknowledge_non_claude": True}, s.rev)
    rp = s.resolve()
    assert rp.thinking == {"type": "disabled"}
    stub.reset()
    out = asyncio.run(analyst_turn(rp, tmp_path / "store" / "analyst"))
    assert out["result"] is not None and out["result"].is_error is False
    assert stub.messages() and all(not r["body"]["has_thinking"] for r in stub.messages())


def test_e11_an_echoed_key_is_redacted_everywhere(tmp_path, net, caplog):
    stub, _proxy = net
    s = settings(tmp_path)
    use_custom(s, stub.base("echo_key"))
    with caplog.at_level("DEBUG"):
        res = check(s)
    assert (res["ok"], res["code"]) == (False, "auth"), res
    assert KEY not in json.dumps(res) and KEY not in caplog.text
    home = tmp_path / "store" / "analyst"
    assert files_containing(home, KEY) == []
    view = json.dumps(s.view())
    assert KEY not in view


# ---------------------------------------------------------------------- E12 --

@pytest.mark.parametrize("scheme", ["x-api-key", "bearer"])
def test_e12_a_cross_origin_redirect_never_passes_the_full_check(tmp_path, net, scheme):
    """The CLI follows a 307 and re-sends x-api-key to the new origin (secrets
    review): the full check refuses the endpoint before the engine runs, and
    the redirect target never sees a request."""
    stub, proxy = net
    with StubAnthropic() as target:
        stub.configure(mode="redirect", redirect_to=target.url)
        s = settings(tmp_path)
        use_custom(s, stub.url, scheme=scheme)
        res = check(s)
        assert (res["ok"], res["code"], res["status"]) == (False, "redirect", 307), res
        assert res["check_token"] is None and res["_argv"] == ""   # no CLI was spawned
        assert target.requests == []
        assert stub.messages() == [] and len(stub.probes()) == 1
        assert KEY not in json.dumps(res) and proxy.attempts == []
        # An endpoint that starts redirecting after its check: the analyst's
        # preflight refuses it before a CLI is spawned.
        stub.configure(mode="ok")
        token = check(s)["check_token"]
        s.put({"provider": "custom", "check_token": token, "activate": True,
               "acknowledge_non_claude": True}, s.rev)
        stub.configure(mode="redirect")
        refusal = asyncio.run(s.preflight(s.resolve()))
        assert refusal and refusal["status"] == 307 and target.requests == []
