"""chat.ChatService + chat_router against a scripted FAKE Agent SDK (no network, no CLI).

The fake mirrors the parts of claude_agent_sdk 0.2.160 the service uses: the
message/block dataclasses, PermissionResultAllow/Deny, ClaudeAgentOptions and a
ClaudeSDKClient whose turns are async generators written per test.  A script
can ask for a tool permission exactly the way the SDK does it -- by running the
``can_use_tool`` callback in a separate task, which ``interrupt()`` cancels.

One integration test at the end drives a real GodseyeUavServer (FakeAirSim on
ports 52200-52299) through the real toolbelt with the fake client.
"""
import asyncio
import contextlib
import dataclasses
import itertools
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import airsim
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from godseye_uav import chat as chat_mod
from godseye_uav.analyst_policy import STATIC_AUTO_TOOLS, TOOL_PREFIX
from godseye_uav.chat import (
    DEFAULT_MODEL,
    EVENT_LOG_SIZE,
    SIGN_IN_HINT,
    Busy,
    ChatService,
    NotAllowed,
    NotFound,
    Unavailable,
    chat_router,
)
from godseye_uav.fake_airsim import FakeAirSim
from godseye_uav.geo import GeoPoint
from godseye_uav.host import honour_msgpack_bind_host
from godseye_uav.safety import SafetyEnvelope
from godseye_uav.server import GodseyeUavServer, UavBackend
from godseye_uav.store import Store

T = 5.0  # seconds; every wait in this file is bounded


# ======================================================================
# Fake claude_agent_sdk
# ======================================================================

@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclass
class AssistantMessage:
    content: list
    model: str = "claude-opus-5"
    parent_tool_use_id: str | None = None
    error: str | None = None


@dataclass
class UserMessage:
    content: Any
    parent_tool_use_id: str | None = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = "claude-sess-1"
    total_cost_usd: float | None = 0.01
    usage: dict | None = None
    errors: list | None = None
    result: str | None = None
    terminal_reason: str | None = "completed"
    api_error_status: int | None = None
    duration_ms: int = 1
    duration_api_ms: int = 1
    num_turns: int = 1


@dataclass
class StreamEvent:
    event: dict
    uuid: str = "u"
    session_id: str = "claude-sess-1"
    parent_tool_use_id: str | None = None


@dataclass
class RateLimitInfo:
    status: str
    resets_at: int | None = None
    rate_limit_type: str | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    uuid: str = "u"
    session_id: str = "claude-sess-1"


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"
    updated_input: dict | None = None
    updated_permissions: list | None = None


@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


@dataclass
class ToolPermissionContext:
    tool_use_id: str | None = None
    suggestions: list = field(default_factory=list)
    signal: Any = None


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.kw = kw
        self.__dict__.update(kw)


class CLINotFoundError(Exception):
    pass


class CanUseToolShadowedWarning(UserWarning):
    pass


class FakeTool:
    def __init__(self, name, description, input_schema, handler):
        self.name, self.description = name, description
        self.input_schema, self.handler = input_schema, handler


class FakeClient:
    sdk: "FakeSdk"

    def __init__(self, options=None):
        self.options = options
        self.prompts: list[str] = []
        self.tasks: dict[str, Any] = {}
        self.interrupts = 0
        self.connected = False
        self.disconnected = False
        self._perm_tasks: set = set()
        self._gen = None
        self.sdk.clients.append(self)

    async def connect(self):
        self.tasks["connect"] = asyncio.current_task()
        if self.sdk.connect_errors:
            raise self.sdk.connect_errors.pop(0)
        self.connected = True

    async def query(self, prompt):
        self.tasks["query"] = asyncio.current_task()
        self.prompts.append(prompt)
        self._gen = self.sdk.turns.pop(0)(self, prompt)

    async def receive_response(self):
        self.tasks["receive"] = asyncio.current_task()
        async for msg in self._gen:
            yield msg

    async def interrupt(self):
        self.interrupts += 1
        for task in list(self._perm_tasks):
            task.cancel()

    async def disconnect(self):
        self.tasks["disconnect"] = asyncio.current_task()
        self.disconnected = True

    # ---- script helpers -------------------------------------------------
    async def ask(self, tool: str, tool_input: dict, tool_use_id: str):
        """Run can_use_tool the way the SDK does: in its own task. None = cancelled."""
        ctx = ToolPermissionContext(tool_use_id=tool_use_id)
        task = asyncio.create_task(self.options.can_use_tool(tool, tool_input, ctx))
        self._perm_tasks.add(task)
        try:
            await asyncio.wait({task})
        finally:
            self._perm_tasks.discard(task)
        return None if task.cancelled() else task.result()

    async def run_tool(self, tool: str, tool_input: dict) -> dict:
        name = tool.removeprefix(TOOL_PREFIX)
        for t in self.options.mcp_servers["godseye"]["tools"]:
            if t.name == name:
                return await t.handler(tool_input)
        raise KeyError(name)

    def cancel_permissions(self):
        for task in list(self._perm_tasks):
            task.cancel()


class FakeSdk:
    """Stands in for the claude_agent_sdk module (attributes, not a real module)."""

    TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock = (
        TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock)
    AssistantMessage, UserMessage, SystemMessage, ResultMessage = (
        AssistantMessage, UserMessage, SystemMessage, ResultMessage)
    StreamEvent, RateLimitEvent = StreamEvent, RateLimitEvent
    PermissionResultAllow, PermissionResultDeny = PermissionResultAllow, PermissionResultDeny
    ToolPermissionContext, ClaudeAgentOptions = ToolPermissionContext, ClaudeAgentOptions
    CLINotFoundError, CanUseToolShadowedWarning = CLINotFoundError, CanUseToolShadowedWarning

    def __init__(self, *turns):
        self.turns = list(turns)
        self.clients: list[FakeClient] = []
        self.connect_errors: list[BaseException] = []
        sdk = self

        class _Client(FakeClient):
            pass

        _Client.sdk = sdk
        self.ClaudeSDKClient = _Client

    @staticmethod
    def tool(name, description, input_schema, annotations=None):
        def deco(fn):
            return FakeTool(name, description, input_schema, fn)
        return deco

    @staticmethod
    def create_sdk_mcp_server(name, version="1.0.0", tools=None):
        return {"type": "sdk", "name": name, "tools": list(tools or [])}


# ---- message builders ------------------------------------------------------

def init(status="connected", session_id="claude-sess-1"):
    return SystemMessage("init", {"mcp_servers": [{"name": "godseye", "status": status}],
                                  "session_id": session_id, "model": "claude-opus-5"})


def streamed_text(*chunks):
    out = [StreamEvent({"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}})]
    out += [StreamEvent({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": c}}) for c in chunks]
    out.append(AssistantMessage([TextBlock("".join(chunks))]))
    out.append(StreamEvent({"type": "content_block_stop", "index": 0}))
    return out


def tool_use(call_id, tool, args):
    return AssistantMessage([ToolUseBlock(call_id, f"{TOOL_PREFIX}{tool}", args)])


def tool_result(call_id, payload, is_error=None):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return UserMessage([ToolResultBlock(call_id, [{"type": "text", "text": text}], is_error)])


def deny_result(call_id, perm):
    return UserMessage([ToolResultBlock(call_id, perm.message, True)])


# ---- event recorder --------------------------------------------------------

class Recorder:
    def __init__(self, svc: ChatService, sid: str, last: int | None = None):
        self.events: list[tuple[int, str, dict]] = []
        self._changed = asyncio.Event()
        self.done = False
        self.task = asyncio.create_task(self._run(svc.subscribe(sid, last)))

    async def _run(self, it):
        async for item in it:
            self.events.append(item)
            self._changed.set()
        self.done = True
        self._changed.set()

    async def wait(self, pred, timeout=T):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._changed.clear()
            for e in self.events:
                if pred(e):
                    return e
            remaining = deadline - loop.time()
            if remaining <= 0 or self.done:
                raise AssertionError(f"timed out; saw {self.names()}")
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                pass

    async def wait_name(self, name, timeout=T):
        return (await self.wait(lambda e: e[1] == name, timeout))[2]

    def names(self):
        return [e[1] for e in self.events]

    def of(self, name):
        return [e[2] for e in self.events if e[1] == name]

    def stop(self):
        self.task.cancel()


def make_service(tmp_path, sdk, **kw) -> ChatService:
    kw.setdefault("server", None)
    kw.setdefault("intel", None)
    return ChatService(store_dir=tmp_path / "store", sdk=sdk, **kw)


async def start_turn(svc, text="hello", context=None):
    sid = await svc.create_session()
    rec = Recorder(svc, sid)
    turn_id = await svc.post_message(sid, text, context)
    return sid, rec, turn_id


# ======================================================================
# status / configuration
# ======================================================================

def test_status_disabled(tmp_path):
    svc = make_service(tmp_path, FakeSdk(), enabled=False)
    st = svc.status()
    assert st["available"] is False and st["reason"] == "disabled" and st["model"]


def test_status_sdk_missing_without_importing_at_module_load(tmp_path, monkeypatch):
    real = chat_mod.importlib.import_module

    def fake_import(name, *a, **k):
        if name == "claude_agent_sdk":
            raise ImportError("No module named 'claude_agent_sdk'")
        return real(name, *a, **k)

    monkeypatch.setattr(chat_mod.importlib, "import_module", fake_import)
    svc = ChatService(server=None, intel=None, store_dir=tmp_path)
    st = svc.status()
    assert st == {"available": False, "reason": "sdk_missing", "hint": chat_mod.SDK_MISSING_HINT,
                  "model": DEFAULT_MODEL}

    async def main():
        sid = await svc.create_session()
        with pytest.raises(Unavailable) as exc:
            await svc.post_message(sid, "hi")
        assert exc.value.reason == "sdk_missing"
    asyncio.run(main())


def test_status_cli_missing(tmp_path):
    svc = make_service(tmp_path, FakeSdk(), cli_path=str(tmp_path / "nope" / "claude"))
    st = svc.status()
    assert st["available"] is False and st["reason"] == "cli_missing" and st["hint"]
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    assert make_service(tmp_path, FakeSdk(), cli_path=str(cli)).status()["available"] is True


def test_real_sdk_status_is_available_when_installed(tmp_path):
    pytest.importorskip("claude_agent_sdk")
    svc = ChatService(server=None, intel=None, store_dir=tmp_path)
    assert svc.status()["available"] is True  # the wheel bundles the CLI


def test_model_and_effort_come_from_args_then_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GODSEYE_CHAT_MODEL", raising=False)
    monkeypatch.delenv("GODSEYE_CHAT_EFFORT", raising=False)
    svc = make_service(tmp_path, FakeSdk())
    assert svc.model == DEFAULT_MODEL == "claude-opus-5" and svc.effort is None
    assert "effort" not in svc.status()
    monkeypatch.setenv("GODSEYE_CHAT_MODEL", "claude-test-model")
    monkeypatch.setenv("GODSEYE_CHAT_EFFORT", "high")
    svc = make_service(tmp_path, FakeSdk())
    assert svc.model == "claude-test-model" and svc.effort == "high"
    assert svc.status()["effort"] == "high"
    svc = make_service(tmp_path, FakeSdk(), model="explicit", effort="low")
    assert svc.model == "explicit" and svc.effort == "low"
    monkeypatch.setenv("GODSEYE_CHAT_EFFORT", "turbo")
    assert make_service(tmp_path, FakeSdk()).effort is None


# ======================================================================
# a plain turn
# ======================================================================

def test_plain_turn_streams_text_and_uses_the_mandated_options(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield RateLimitEvent(RateLimitInfo("allowed", 1_700_000_000, "five_hour"))
        for m in streamed_text("Hello ", "operator."):
            yield m
        yield ResultMessage(usage={"input_tokens": 3, "cache_read_input_tokens": 10,
                                   "cache_creation_input_tokens": 2, "output_tokens": 5},
                            total_cost_usd=0.02)

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, turn_id = await start_turn(svc, "What is up?")
        end = await rec.wait_name("turn_end")
        assert end == {"turn_id": turn_id, "stop": "end"}
        assert rec.names()[:2] == ["session", "turn_start"]
        assert rec.of("turn_start") == [{"turn_id": turn_id, "text": "What is up?"}]
        assert [d["text"] for d in rec.of("text_delta")] == ["Hello ", "operator."]
        usage = rec.of("usage")[-1]
        assert usage["cost_usd"] == 0.02 and usage["session_cost_usd"] == 0.02
        assert usage["input_tokens"] == 15
        assert usage["output_tokens"] == 5
        assert usage["rate_limit"] == {"status": "allowed", "resets_at": 1_700_000_000,
                                       "type": "five_hour"}
        session = rec.events[0]
        assert session[0] == 0 and session[2]["session_id"] == sid
        assert session[2]["model"] == DEFAULT_MODEL and session[2]["available"] is True
        seqs = [e[0] for e in rec.events[1:]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        client = sdk.clients[0]
        # one actor task owns the client: connect, query and receive run in it
        assert client.tasks["connect"] is client.tasks["query"] is client.tasks["receive"]
        assert client.tasks["connect"] is not asyncio.current_task()
        assert client.prompts == ["What is up?"]
        await svc.shutdown()
        assert client.disconnected and client.tasks["disconnect"] is client.tasks["connect"]
        rec.stop()
    asyncio.run(main())

    kw = sdk.clients[0].options.kw
    assert kw["tools"] == [] and kw["setting_sources"] == []
    assert kw["strict_mcp_config"] is True and kw["verbatim_prompts"] is True
    assert kw["permission_mode"] == "default" and kw["include_partial_messages"] is True
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kw["max_turns"] == 40 and kw["model"] == DEFAULT_MODEL
    assert kw["cwd"] == str(tmp_path / "store" / "analyst")
    assert (tmp_path / "store" / "analyst").is_dir()
    assert list(kw["mcp_servers"]) == ["godseye"]
    assert callable(kw["can_use_tool"])
    assert kw["allowed_tools"] and all(
        t.startswith(TOOL_PREFIX) and t[len(TOOL_PREFIX):] in STATIC_AUTO_TOOLS
        for t in kw["allowed_tools"])
    assert f"{TOOL_PREFIX}uav_list_tracks" in kw["disallowed_tools"]
    assert f"{TOOL_PREFIX}sim_set_environment" in kw["disallowed_tools"]
    assert "ISR only" in kw["system_prompt"] and "[[type:id|label]]" in kw["system_prompt"]
    for absent in ("cli_path", "effort", "resume", "env"):
        assert absent not in kw
    for forbidden in ("bypassPermissions", "dontAsk"):
        assert kw["permission_mode"] != forbidden


def test_cli_path_and_effort_pass_through(tmp_path):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)

    async def turn(client, prompt):
        yield init()
        yield ResultMessage()

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk, cli_path=str(cli), effort="max")

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        await svc.shutdown()
    asyncio.run(main())
    kw = sdk.clients[0].options.kw
    assert kw["cli_path"] == str(cli) and kw["effort"] == "max"


def test_text_and_thinking_fall_back_to_blocks_without_stream_events(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield AssistantMessage([ThinkingBlock("Checking fuel first.")])
        yield AssistantMessage([TextBlock("First paragraph.")])
        yield AssistantMessage([TextBlock("Second paragraph.")])
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        assert [d["text"] for d in rec.of("text_delta")] == ["First paragraph.",
                                                            "\n\nSecond paragraph."]
        assert [d["text"] for d in rec.of("thinking")] == ["Checking fuel first."]
        await svc.shutdown()
    asyncio.run(main())


def test_streamed_text_blocks_are_kept_apart_as_paragraphs(tmp_path):
    async def turn(client, prompt):
        yield init()
        for m in streamed_text("I'll check."):
            yield m
        for m in streamed_text("Drone1 ", "is idle."):
            yield m
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        texts = [d["text"] for d in rec.of("text_delta")]
        assert texts == ["I'll check.", "\n\n", "Drone1 ", "is idle."]
        await svc.shutdown()
    asyncio.run(main())


def test_cost_is_reported_per_turn_from_the_cumulative_total(tmp_path):
    def turn_costing(total):
        async def turn(client, prompt):
            yield init()
            yield ResultMessage(total_cost_usd=total)
        return turn

    async def crash(client, prompt):
        yield init()
        raise RuntimeError("CLI died")
        yield  # pragma: no cover

    sdk = FakeSdk(turn_costing(0.30), turn_costing(0.33), crash, turn_costing(0.05))
    svc = make_service(tmp_path, sdk)

    async def main():
        sid = await svc.create_session()
        rec = Recorder(svc, sid)
        for _ in range(4):
            tid = await svc.post_message(sid, "go")
            await rec.wait(lambda e, tid=tid: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        usage = [u for u in rec.of("usage") if "cost_usd" in u]
        assert [u["cost_usd"] for u in usage] == [0.3, 0.03, 0.05]
        assert [u["session_cost_usd"] for u in usage] == [0.3, 0.33, 0.38]
        await svc.shutdown()
    asyncio.run(main())


def test_thinking_deltas_are_forwarded(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield StreamEvent({"type": "content_block_start",
                           "content_block": {"type": "thinking", "thinking": ""}})
        yield StreamEvent({"type": "content_block_delta",
                           "delta": {"type": "thinking_delta", "thinking": "Plan: dry run."}})
        yield AssistantMessage([ThinkingBlock("Plan: dry run.")])
        yield StreamEvent({"type": "content_block_delta", "parent_tool_use_id": None,
                           "delta": {"type": "signature_delta", "signature": "x"}})
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        assert [d["text"] for d in rec.of("thinking")] == ["Plan: dry run."]
        await svc.shutdown()
    asyncio.run(main())


def test_focused_ids_are_prefixed_to_the_prompt(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield ResultMessage()

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        _, rec, _ = await start_turn(svc, "Tell me about these.", {
            "focused_ids": ["trk:TRK-a-0001", "not an id", "veh:Drone1", "x]]y", 7,
                            "trk:TRK-a-0001", "poi:default:North Field", "veh:a\nb",
                            "trk:x]]"]})
        await rec.wait_name("turn_end")
        await svc.shutdown()
    asyncio.run(main())
    assert sdk.clients[0].prompts == [
        ("[Console context: the operator has these entities focused: [[trk:TRK-a-0001]], "
         "[[veh:Drone1]], [[poi:default:North Field]]]\n\nTell me about these.")]


# ======================================================================
# tools and approvals
# ======================================================================

DRY_RUN_PAYLOAD = {
    "executed": False, "dry_run": True, "vehicle": "Drone1", "kind": "grid_search",
    "gate": {"ok": True, "required_pct": 30.0, "available_pct": 90.0, "plan_fuel_pct": 25.0,
             "reserve_pct": 20.0, "bingo_latched": False, "envelope_violations": [],
             "warnings": [], "start": [1, 2]},
    "est_time_s": 600.0, "est_fuel_pct": 25.0, "fuel_pct": 90.0,
    "waypoints": [{"lat": 1, "lon": 2}] * 5 + [{"_truncated": True, "_omitted": 7, "_note": "x"}],
}
AO = [[47.63, -122.16], [47.63, -122.12], [47.66, -122.12], [47.66, -122.16]]


def test_plan_then_approved_command_with_dry_run_summary_and_auto_track(tmp_path):
    seen: dict = {}

    async def turn(client, prompt):
        yield init()
        args = {"vehicle": "Drone1", "kind": "grid_search", "polygon": AO, "alt_agl_m": 60}
        yield tool_use("toolu_1", "mission_dry_run", args)
        seen["plan"] = await client.ask(f"{TOOL_PREFIX}mission_dry_run", args, "toolu_1")
        yield tool_result("toolu_1", DRY_RUN_PAYLOAD)
        live = {"vehicle": "Drone1", "polygon": AO, "alt_agl_m": 60}
        yield tool_use("toolu_2", "mission_grid_search", live)
        seen["live"] = await client.ask(f"{TOOL_PREFIX}mission_grid_search", live, "toolu_2")
        yield tool_result("toolu_2", {"mission_id": "MSN-1a2b3c4d", "task_id": "abc123",
                                      "status": "accepted", "vehicle": "Drone1"})
        for m in streamed_text("Launched."):
            yield m
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, turn_id = await start_turn(svc, "Search the AO")
        req = await rec.wait_name("approval_request")
        assert req["call_id"] == "toolu_2" and req["tool"] == "mission_grid_search"
        assert req["class"] == "command" and req["allow_session"] is False
        assert req["title"] == "Grid search"
        assert req["summary"] == "Drone1 · polygon of 4 points · 60 m AGL"
        assert any("Takes off" in c for c in req["consequences"])
        assert req["args"]["vehicle"] == "Drone1"
        assert req["expires_at_ms"] > 0
        dry = req["dry_run"]
        assert dry["ok"] is True and dry["eta_s"] == 600.0 and dry["fuel_pct_after"] == 65.0
        assert dry["waypoints"] == 12 and dry["gate"]["required_pct"] == 30.0
        assert "start" not in dry["gate"]
        assert dry["matches_args"] is True  # same polygon/altitude as the dry run
        assert dry["tool"] == "mission_dry_run" and dry["at_ms"] > 0
        assert "envelope" not in dry["gate"]  # no server in this test
        # contract v1.1 §10.1
        assert req["vehicle"] == "Drone1" and req["dry_runnable"] is True
        assert req["grant_scope"] is None
        # the plan call before it ran without any approval card
        assert len(rec.of("approval_request")) == 1
        calls = rec.of("tool_call")
        assert calls[0] == {"turn_id": turn_id, "call_id": "toolu_1", "tool": "mission_dry_run",
                            "title": "Plan grid search", "class": "plan",
                            "args": {"vehicle": "Drone1", "kind": "grid_search",
                                     "polygon": AO, "alt_agl_m": 60},
                            "summary": "Drone1 · grid search · polygon of 4 points · "
                                       "60 m AGL · dry run"}
        with pytest.raises(NotAllowed):
            await svc.resolve_approval(sid, req["approval_id"], "approve_session")
        await svc.resolve_approval(sid, req["approval_id"], "approve")
        with pytest.raises(NotFound):
            await svc.resolve_approval(sid, req["approval_id"], "approve")
        await rec.wait_name("turn_end")
        resolved = rec.of("approval_resolved")
        assert resolved == [{"approval_id": req["approval_id"], "call_id": "toolu_2",
                             "decision": "approved", "tool": "mission_grid_search",
                             "scope": "once"}]
        results = {d["call_id"]: d for d in rec.of("tool_result")}
        assert results["toolu_1"]["ok"] is True and "Gate passed" in results["toolu_1"]["summary"]
        live = results["toolu_2"]
        assert live["ok"] is True and "rejected" not in live and live["truncated"] is False
        assert live["outcome"] == "ok" and "busy_with" not in live
        assert live["entities"] == ["msn:MSN-1a2b3c4d", "veh:Drone1"]
        assert live["bytes"] > 0 and "MSN-1a2b3c4d" in live["summary"]
        assert rec.of("ui") == [{"action": "track", "vehicle": "Drone1",
                                 "reason": "mission launched"}]
        names = rec.names()
        assert names.index("approval_resolved") < names.index("ui") < names.index("turn_end")
        await svc.shutdown()
    asyncio.run(main())
    assert isinstance(seen["plan"], PermissionResultAllow)
    assert isinstance(seen["live"], PermissionResultAllow)
    assert seen["live"].updated_input == {"vehicle": "Drone1", "polygon": AO, "alt_agl_m": 60}
    assert seen["live"].updated_permissions is None  # never echo ctx.suggestions


class _StubMcp:
    async def list_tools(self):
        return []


class _StubServer:
    """Just enough server for the toolbelt (no tools) plus the safety envelope."""

    def __init__(self):
        self.mcp = _StubMcp()
        self.envelope = SafetyEnvelope(geofence=AO_T, home=(47.64, -122.14, 93.0))


def test_dry_run_summary_says_when_the_live_args_differ_and_carries_the_envelope(tmp_path):
    async def turn(client, prompt):
        yield init()
        plan = {"vehicle": "Drone1", "kind": "grid_search", "params": {"polygon": AO},
                "alt_agl_m": 60, "lost_link_plan": None}
        yield tool_use("toolu_1", "mission_dry_run", plan)
        await client.ask(f"{TOOL_PREFIX}mission_dry_run", plan, "toolu_1")
        yield tool_result("toolu_1", DRY_RUN_PAYLOAD)
        for cid, alt in (("toolu_2", 60), ("toolu_3", 90)):
            live = {"vehicle": "Drone1", "polygon": AO, "alt_agl_m": alt,
                    "idempotency_key": cid}
            yield tool_use(cid, "mission_grid_search", live)
            perm = await client.ask(f"{TOOL_PREFIX}mission_grid_search", live, cid)
            yield deny_result(cid, perm)
        # a different vehicle has no dry run at all
        other = {"vehicle": "Drone2", "polygon": AO}
        yield tool_use("toolu_4", "mission_grid_search", other)
        perm = await client.ask(f"{TOOL_PREFIX}mission_grid_search", other, "toolu_4")
        yield deny_result("toolu_4", perm)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn), server=_StubServer())

    async def main():
        sid, rec, _ = await start_turn(svc)
        seen = {}
        for cid in ("toolu_2", "toolu_3", "toolu_4"):
            req = (await rec.wait(lambda e, cid=cid: e[1] == "approval_request"
                                  and e[2]["call_id"] == cid))[2]
            seen[cid] = req
            await svc.resolve_approval(sid, req["approval_id"], "deny")
        await rec.wait_name("turn_end")
        same, moved, none = seen["toolu_2"], seen["toolu_3"], seen["toolu_4"]
        assert same["dry_run"]["matches_args"] is True  # params flattened, keys ignored
        assert moved["dry_run"]["matches_args"] is False  # 90 m is not what was planned
        assert "dry_run" not in none and none["vehicle"] == "Drone2"
        assert same["dry_run"]["gate"]["envelope"] == {
            "ceiling_m_agl": 120.0, "min_agl_m": 3.0, "max_speed_mps": 20.0,
            "geofence": "enforced"}
        assert [r["outcome"] for r in rec.of("tool_result")] == [
            "ok", "not_run", "not_run", "not_run"]
        await svc.shutdown()
    asyncio.run(main())


def test_rejected_or_failed_commands_do_not_auto_track(tmp_path):
    outcomes = [
        {"rejected": True, "gate": {"ok": False, "required_pct": 80, "available_pct": 20,
                                    "envelope_violations": ["geofence"]}},
        {"error": {"code": "invalid_mission_params", "message": "bad polygon"}},
        {"status": "busy", "vehicle": "Drone1", "rejected_tool": "mission_grid_search",
         "current": {"task_id": "t-1", "tool": "uav_fly_route", "mission_id": "MSN-0000aaaa",
                     "state": "executing"}},
        {"cancelled": False, "refused": True, "reason": "uav_return_to_home is an "
                                                       "un-cancellable safety transition"},
    ]

    async def turn(client, prompt):
        yield init()
        for i, payload in enumerate(outcomes):
            cid = f"toolu_{i}"
            yield tool_use(cid, "mission_grid_search", {"vehicle": "Drone1", "polygon": AO})
            await client.ask(f"{TOOL_PREFIX}mission_grid_search",
                             {"vehicle": "Drone1", "polygon": AO}, cid)
            yield tool_result(cid, payload)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, _ = await start_turn(svc)
        for i in range(4):
            req = await rec.wait(lambda e, i=i: e[1] == "approval_request"
                                 and e[2]["call_id"] == f"toolu_{i}")
            await svc.resolve_approval(sid, req[2]["approval_id"], "approve")
        await rec.wait_name("turn_end")
        results = rec.of("tool_result")
        assert results[0]["ok"] is False and results[0]["rejected"] is True
        assert "needs 80% fuel, has 20%" in results[0]["summary"]
        assert "geofence" in results[0]["summary"]
        assert results[1]["ok"] is False and results[1]["error"] == "bad polygon"
        assert results[2]["ok"] is False and "busy" in results[2]["summary"]
        assert "rejected" not in results[2] and "error" not in results[2]
        assert [r["outcome"] for r in results] == ["rejected", "error", "busy", "rejected"]
        assert results[2]["busy_with"] == {"task_id": "t-1", "mission_id": "MSN-0000aaaa",
                                           "tool": "uav_fly_route"}
        assert results[3]["summary"].startswith("Refused:")
        assert rec.of("ui") == []
        await svc.shutdown()
    asyncio.run(main())


def test_deny_sends_the_operator_note_and_the_model_sees_an_error(tmp_path):
    seen: dict = {}

    async def turn(client, prompt):
        yield init()
        args = {"vehicle": "Drone1"}
        yield tool_use("toolu_9", "uav_takeoff", args)
        seen["perm"] = perm = await client.ask(f"{TOOL_PREFIX}uav_takeoff", args, "toolu_9")
        yield deny_result("toolu_9", perm)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, _ = await start_turn(svc)
        req = await rec.wait_name("approval_request")
        await svc.resolve_approval(sid, req["approval_id"], "deny", "Wait for the weather.")
        await rec.wait_name("turn_end")
        assert rec.of("approval_resolved")[0]["decision"] == "denied"
        assert rec.of("approval_resolved")[0]["note"] == "Wait for the weather."
        res = rec.of("tool_result")[0]
        assert res["ok"] is False and "denied" in res["error"]
        assert res["outcome"] == "not_run"
        assert rec.of("ui") == []
        await svc.shutdown()
    asyncio.run(main())
    perm = seen["perm"]
    assert isinstance(perm, PermissionResultDeny) and perm.interrupt is False
    assert "denied" in perm.message and "Do not retry" in perm.message
    assert "Wait for the weather." in perm.message


def test_allow_for_session_is_per_sensor_tool_and_revocable(tmp_path):
    perms: list = []

    async def turn(client, prompt):
        yield init()
        steps = [("toolu_a", "uav_scan_targets"), ("toolu_b", "uav_scan_targets"),
                 ("toolu_c", "uav_get_detections"), ("toolu_d", "sim_set_time"),
                 ("toolu_e", "uav_takeoff")]
        for cid, name in steps:
            args = {"vehicle": "Drone1"}
            yield tool_use(cid, name, args)
            perms.append((cid, await client.ask(f"{TOOL_PREFIX}{name}", args, cid)))
            yield tool_result(cid, {"ok": True, "vehicle": "Drone1"})
        yield ResultMessage()

    async def again(client, prompt):
        yield init()
        yield tool_use("toolu_f", "uav_scan_targets", {"vehicle": "Drone1"})
        perms.append(("toolu_f", await client.ask(f"{TOOL_PREFIX}uav_scan_targets",
                                                  {"vehicle": "Drone1"}, "toolu_f")))
        yield tool_result("toolu_f", {"ok": True})
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn, again))

    def request_for(call_id):
        return lambda e: e[1] == "approval_request" and e[2]["call_id"] == call_id

    async def main():
        sid, rec, _ = await start_turn(svc)
        first = (await rec.wait(request_for("toolu_a")))[2]
        assert first["class"] == "sensor" and first["allow_session"] is True
        assert first["grant_scope"] == ["uav_scan_targets"]
        await svc.resolve_approval(sid, first["approval_id"], "approve_session")
        other = (await rec.wait(request_for("toolu_c")))[2]  # another sensor TOOL still asks
        assert other["tool"] == "uav_get_detections"
        await svc.resolve_approval(sid, other["approval_id"], "approve")
        sim = (await rec.wait(request_for("toolu_d")))[2]
        assert sim["class"] == "sim" and sim["allow_session"] is False
        assert sim["grant_scope"] is None
        with pytest.raises(NotAllowed):
            await svc.resolve_approval(sid, sim["approval_id"], "approve_session")
        await svc.resolve_approval(sid, sim["approval_id"], "approve")
        cmd = (await rec.wait(request_for("toolu_e")))[2]
        with pytest.raises(NotAllowed):
            await svc.resolve_approval(sid, cmd["approval_id"], "approve_session")
        await svc.resolve_approval(sid, cmd["approval_id"], "approve")
        await rec.wait_name("turn_end")
        asked = [d["call_id"] for d in rec.of("approval_request")]
        assert asked == ["toolu_a", "toolu_c", "toolu_d", "toolu_e"]  # toolu_b ran on the grant
        resolved = rec.of("approval_resolved")
        assert resolved[0]["scope"] == "session" and resolved[0]["tool"] == "uav_scan_targets"
        assert all(r["scope"] == "once" for r in resolved[1:])
        grants = svc.grants(sid)
        assert [g["tool"] for g in grants] == ["uav_scan_targets"] and grants[0]["since_ms"] > 0
        # the takeoff was approved call by call and succeeded, so the console tracks it;
        # the session-granted sensor calls never do
        assert rec.of("ui") == [{"action": "track", "vehicle": "Drone1",
                                 "reason": "mission launched"}]
        await svc.revoke_grant(sid, "uav_scan_targets")
        await svc.revoke_grant(sid, "uav_scan_targets")  # idempotent
        assert svc.grants(sid) == []
        tid = await svc.post_message(sid, "scan again")
        again_req = (await rec.wait(request_for("toolu_f")))[2]  # asks again after revoke
        await svc.resolve_approval(sid, again_req["approval_id"], "approve")
        await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        await svc.shutdown()
    asyncio.run(main())
    assert all(isinstance(p, PermissionResultAllow) for _, p in perms)


def test_unknown_tool_fails_closed_to_an_approval(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield AssistantMessage([ToolUseBlock("toolu_x", "Bash", {"command": "ls"})])
        perm = await client.ask("Bash", {"command": "ls"}, "toolu_x")
        yield deny_result("toolu_x", perm)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, _ = await start_turn(svc)
        req = await rec.wait_name("approval_request")
        assert req["class"] == "command" and req["tool"] == "Bash"
        assert req["allow_session"] is False
        await svc.resolve_approval(sid, req["approval_id"], "deny")
        await rec.wait_name("turn_end")
        await svc.shutdown()
    asyncio.run(main())


def test_interrupt_during_pending_approval_cancels_it(tmp_path):
    seen: dict = {}

    async def turn(client, prompt):
        yield init()
        args = {"vehicle": "Drone1", "polygon": AO}
        yield tool_use("toolu_1", "mission_grid_search", args)
        seen["perm"] = await client.ask(f"{TOOL_PREFIX}mission_grid_search", args, "toolu_1")
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            terminal_reason="aborted_tools")

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, turn_id = await start_turn(svc)
        req = await rec.wait_name("approval_request")
        await svc.interrupt(sid)
        end = await rec.wait_name("turn_end")
        assert end == {"turn_id": turn_id, "stop": "interrupted"}
        assert rec.of("approval_resolved") == [{"approval_id": req["approval_id"],
                                                "call_id": "toolu_1", "decision": "cancelled",
                                                "tool": "mission_grid_search",
                                                "scope": "once"}]
        assert not svc._sessions[sid].pending
        with pytest.raises(NotFound):
            await svc.resolve_approval(sid, req["approval_id"], "approve")
        assert sdk.clients[0].interrupts == 1
        await svc.interrupt(sid)  # idle: no-op
        await svc.shutdown()
    asyncio.run(main())
    perm = seen["perm"]
    assert perm is None or (isinstance(perm, PermissionResultDeny) and perm.interrupt is True)


def test_sdk_cancelling_a_parked_callback_resolves_it_as_cancelled(tmp_path):
    async def turn(client, prompt):
        yield init()
        args = {"vehicle": "Drone1"}
        yield tool_use("toolu_1", "uav_land", args)
        asking = asyncio.create_task(client.ask(f"{TOOL_PREFIX}uav_land", args, "toolu_1"))
        while not client._perm_tasks:
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        client.cancel_permissions()  # what control_cancel_request does
        assert await asking is None
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            terminal_reason="aborted_tools")

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        assert rec.of("approval_resolved")[0]["decision"] == "cancelled"
        assert rec.of("turn_end")[0]["stop"] == "interrupted"
        assert not svc._sessions[sid].pending
        await svc.shutdown()
    asyncio.run(main())


def test_approval_times_out_as_expired_and_denies(tmp_path):
    seen: dict = {}

    async def turn(client, prompt):
        yield init()
        args = {"vehicle": "Drone1", "fuel_pct": 100}
        yield tool_use("toolu_1", "sim_set_fuel", args)
        seen["perm"] = perm = await client.ask(f"{TOOL_PREFIX}sim_set_fuel", args, "toolu_1")
        yield deny_result("toolu_1", perm)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn), approval_timeout_s=0.05)

    async def main():
        sid, rec, _ = await start_turn(svc)
        req = await rec.wait_name("approval_request")
        assert req["class"] == "safety_override" and req["allow_session"] is False
        assert any("BINGO latch" in c for c in req["consequences"])
        await rec.wait_name("turn_end")
        assert rec.of("approval_resolved")[0]["decision"] == "expired"
        assert rec.of("tool_result")[0]["outcome"] == "not_run"
        with pytest.raises(NotFound):
            await svc.resolve_approval(sid, req["approval_id"], "approve")
        await svc.shutdown()
    asyncio.run(main())
    assert isinstance(seen["perm"], PermissionResultDeny)
    assert "did not answer" in seen["perm"].message and seen["perm"].interrupt is False


# ======================================================================
# failures
# ======================================================================

def test_auth_failure_emits_a_sign_in_hint_once(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield AssistantMessage([TextBlock("Invalid API key · Please run /login")],
                               error="authentication_failed")
        yield ResultMessage(is_error=True, result="Invalid API key · Please run /login")

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        end = await rec.wait_name("turn_end")
        assert end["stop"] == "error"
        errors = rec.of("error")
        # BYOK spec §5: every error names its code and provider.
        assert errors == [{"message": "The analyst could not sign in to Claude.",
                           "hint": SIGN_IN_HINT, "retryable": False, "code": "auth",
                           "provider": {"id": "anthropic_login",
                                        "label": "Claude login (this Mac)"}}]
        assert rec.of("text_delta") == []  # the CLI's error text is replaced, not echoed
        await svc.shutdown()
    asyncio.run(main())


def test_result_errors_that_look_like_auth_get_the_hint(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            errors=["API Error: 401 authentication_error: OAuth token expired"])

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        end = await rec.wait_name("turn_end")
        assert end["stop"] == "error" and "401" in end["error"]
        assert rec.of("error")[0]["hint"] == SIGN_IN_HINT
        await svc.shutdown()
    asyncio.run(main())


def test_connect_failure_is_reported_and_the_next_turn_reconnects(tmp_path):
    async def ok_turn(client, prompt):
        yield init(session_id="claude-sess-9")
        yield ResultMessage(session_id="claude-sess-9")

    async def crash_turn(client, prompt):
        yield init(session_id="claude-sess-9")
        raise RuntimeError("CLI process exited with code 1")
        yield  # pragma: no cover

    sdk = FakeSdk(ok_turn, crash_turn, ok_turn)
    sdk.connect_errors = [RuntimeError("Not logged in · Please run /login")]
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        end = await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == t1)
        assert end[2]["stop"] == "error"
        assert rec.of("error")[0]["hint"] == SIGN_IN_HINT
        for _ in range(3):
            tid = await svc.post_message(sid, "again")
            await rec.wait(lambda e, tid=tid: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        ends = rec.of("turn_end")
        assert [e["stop"] for e in ends] == ["error", "end", "error", "end"]
        assert "exited with code 1" in rec.of("error")[-1]["message"]
        await svc.shutdown()
    asyncio.run(main())
    # connect failed (client 0), turn 2 on client 1, crash drops it, turn 4 on client 2
    assert len(sdk.clients) == 3
    assert sdk.clients[1].disconnected
    assert "resume" not in sdk.clients[1].options.kw
    assert sdk.clients[2].options.kw["resume"] == "claude-sess-9"


def test_cli_not_found_error_maps_to_the_cli_hint(tmp_path):
    sdk = FakeSdk()
    sdk.connect_errors = [CLINotFoundError("Claude Code not found")]
    svc = make_service(tmp_path, sdk)

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        err = rec.of("error")[0]
        assert err["hint"] == chat_mod.CLI_MISSING_HINT and err["retryable"] is False
        await svc.shutdown()
    asyncio.run(main())


def test_tool_server_not_connected_stops_the_turn(tmp_path):
    async def turn(client, prompt):
        yield init(status="failed")
        await asyncio.sleep(0.05)  # let the interrupt watcher run
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            terminal_reason="aborted_streaming")

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        _, rec, _ = await start_turn(svc)
        end = await rec.wait_name("turn_end")
        assert end["stop"] == "error" and end["error"] == "mcp_unavailable"
        err = rec.of("error")[0]
        assert "tool server" in err["message"] and err["retryable"] is True
        assert sdk.clients[0].interrupts == 1
        await svc.shutdown()
    asyncio.run(main())


def test_max_turns_is_its_own_stop_reason(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield ResultMessage(subtype="error_max_turns", is_error=True, terminal_reason="max_turns")

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        assert (await rec.wait_name("turn_end"))["stop"] == "max_turns"
        await svc.shutdown()
    asyncio.run(main())


# ======================================================================
# sessions, busy, replay
# ======================================================================

def test_busy_while_a_turn_runs(tmp_path):
    gate = {}

    async def slow(client, prompt):
        yield init()
        await gate["release"].wait()
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(slow))

    async def main():
        gate["release"] = asyncio.Event()
        sid, rec, turn_id = await start_turn(svc)
        with pytest.raises(Busy) as exc:
            await svc.post_message(sid, "second")
        assert exc.value.turn_id == turn_id
        with pytest.raises(ValueError):
            await svc.post_message(sid, "   ")
        with pytest.raises(ValueError):
            await svc.post_message(sid, "x" * 8001)
        with pytest.raises(NotFound):
            await svc.post_message("nope", "hi")
        gate["release"].set()
        await rec.wait_name("turn_end")
        await svc.shutdown()
    asyncio.run(main())


def test_replay_by_last_event_id(tmp_path):
    async def turn(client, prompt):
        yield init()
        for m in streamed_text("a", "b", "c"):
            yield m
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        sid, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        full = [e for e in rec.events if e[0] > 0]
        cut = full[2][0]
        replay = Recorder(svc, sid, last=cut)
        await replay.wait(lambda e: e[1] == "turn_end")
        assert replay.events[0][1] == "session" and replay.events[0][0] == 0
        assert replay.events[0][2]["history_truncated"] is False
        assert replay.events[0][2]["last_seq"] == full[-1][0]
        assert replay.events[1:] == [e for e in full if e[0] > cut]
        again = Recorder(svc, sid)  # no Last-Event-ID: the whole log
        await again.wait(lambda e: e[1] == "turn_end")
        assert again.events[1:] == full
        await svc.close_session(sid)
        for r in (rec, replay, again):
            await asyncio.wait_for(r.task, T)  # streams end when the session closes
            assert r.done
    asyncio.run(main())


def test_event_log_is_bounded_and_replay_reports_the_gap(tmp_path):
    svc = make_service(tmp_path, FakeSdk())

    async def main():
        sid = await svc.create_session()
        s = svc._sessions[sid]
        for i in range(EVENT_LOG_SIZE + 100):
            s.emit("text_delta", {"turn_id": "t", "text": str(i)})
        assert len(s.log) == EVENT_LOG_SIZE and s.log[0][0] == 101
        items = []
        async for item in svc.subscribe(sid, 5):
            items.append(item)
            if len(items) == 3:
                break
        assert items[0][2]["history_truncated"] is True
        assert items[1][0] == 101
        with pytest.raises(NotFound):
            svc.subscribe("nope")
        await svc.shutdown()
    asyncio.run(main())


def test_close_session_cancels_approvals_and_disconnects(tmp_path):
    async def turn(client, prompt):
        yield init()
        yield tool_use("toolu_1", "uav_hover", {"vehicle": "Drone1"})
        await client.ask(f"{TOOL_PREFIX}uav_hover", {"vehicle": "Drone1"}, "toolu_1")
        await asyncio.sleep(30)
        yield ResultMessage()  # pragma: no cover

    sdk = FakeSdk(turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, turn_id = await start_turn(svc)
        await rec.wait_name("approval_request")
        await svc.close_session(sid)
        await asyncio.wait_for(rec.task, T)
        assert rec.of("approval_resolved")[0]["decision"] == "cancelled"
        assert rec.of("turn_end") == [{"turn_id": turn_id, "stop": "interrupted"}]
        assert sdk.clients[0].disconnected
        assert sdk.clients[0].tasks["disconnect"] is sdk.clients[0].tasks["connect"]
        with pytest.raises(NotFound):
            await svc.close_session(sid)
        with pytest.raises(NotFound):
            await svc.interrupt(sid)
    asyncio.run(main())


def test_sessions_are_capped_by_evicting_the_idlest(tmp_path):
    svc = make_service(tmp_path, FakeSdk())

    async def main():
        sids = [await svc.create_session() for _ in range(chat_mod.MAX_SESSIONS)]
        extra = await svc.create_session()
        assert sids[0] not in svc._sessions and extra in svc._sessions
        assert len(svc._sessions) == chat_mod.MAX_SESSIONS
        for s in svc._sessions.values():
            s.turn = chat_mod._Turn("busy")
        with pytest.raises(Busy):
            await svc.create_session()
        for s in svc._sessions.values():
            s.turn = None
        await svc.shutdown()
        assert not svc._sessions
    asyncio.run(main())


def test_sse_frames_have_ids_names_json_and_heartbeats(tmp_path):
    svc = make_service(tmp_path, FakeSdk())

    async def main():
        sid = await svc.create_session()
        s = svc._sessions[sid]
        s.emit("text_delta", {"turn_id": "t", "text": "line one\nline two"})
        frames = svc.sse_frames(sid, None, heartbeat_s=0.05)
        assert await anext(frames) == "retry: 3000\n\n"
        session = await anext(frames)
        assert session.startswith("event: session\ndata: {") and "id:" not in session
        delta = await anext(frames)
        assert delta == ('id: 1\nevent: text_delta\ndata: {"turn_id":"t","text":'
                         '"line one\\nline two"}\n\n')
        ping = await asyncio.wait_for(anext(frames), T)
        assert ping.startswith(": ping ") and ping.endswith("\n\n")
        await frames.aclose()
        await svc.shutdown()
    asyncio.run(main())


# ======================================================================
# HTTP router
# ======================================================================

TOKEN = "test-token"


def _auth(request: Request):
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


def _sse_auth(request: Request):
    if (request.headers.get("authorization") == f"Bearer {TOKEN}"
            or request.query_params.get("token") == TOKEN):
        return True
    raise HTTPException(status_code=401, detail="unauthorized")


def _app(svc):
    app = FastAPI()
    app.include_router(chat_router(svc, _auth, _sse_auth))
    return app


H = {"Authorization": f"Bearer {TOKEN}"}


async def _sse_get(app, path, query="", headers=None, stop=lambda body: False, timeout=T):
    """Drive a streaming GET through raw ASGI; disconnect once `stop(body)` is true."""
    sent: list = []
    disconnect = asyncio.Event()
    body = bytearray()
    started = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start":
            started.set()
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if stop(body.decode()):
                disconnect.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query.encode(), "root_path": "",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout)
        start = next(m for m in sent if m["type"] == "http.response.start")
        if start["status"] != 200:
            await asyncio.wait_for(task, timeout)
        else:
            await asyncio.wait_for(disconnect.wait(), timeout)
            await asyncio.wait_for(task, timeout)
    finally:
        disconnect.set()
        if not task.done():
            task.cancel()
    headers_out = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers_out, body.decode()


def test_router_status_codes(tmp_path):
    gate = {}

    async def slow(client, prompt):
        yield init()
        yield tool_use("toolu_1", "uav_takeoff", {"vehicle": "Drone1"})
        perm = await client.ask(f"{TOOL_PREFIX}uav_takeoff", {"vehicle": "Drone1"}, "toolu_1")
        yield deny_result("toolu_1", perm) if isinstance(perm, PermissionResultDeny) else \
            tool_result("toolu_1", {"task_id": "x", "vehicle": "Drone1"})
        await gate["release"].wait()
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(slow))
    app = _app(svc)

    async def main():
        gate["release"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/chat/status")).status_code == 401
            st = await c.get("/chat/status", headers=H)
            assert st.status_code == 200 and st.json()["available"] is True
            sid = (await c.post("/chat/sessions", headers=H)).json()["session_id"]
            url = f"/chat/sessions/{sid}/messages"
            assert (await c.post(url, headers=H, json={"text": ""})).status_code == 422
            assert (await c.post(url, headers=H, json={"text": "x" * 8001})).status_code == 422
            assert (await c.post(url, json={"text": "hi"})).status_code == 401
            assert (await c.post("/chat/sessions/nope/messages", headers=H,
                                 json={"text": "hi"})).status_code == 404
            r = await c.post(url, headers=H, json={"text": "take off",
                                                   "context": {"focused_ids": ["veh:Drone1"]}})
            assert r.status_code == 202
            turn_id = r.json()["turn_id"]
            busy = await c.post(url, headers=H, json={"text": "again"})
            assert busy.status_code == 409
            assert busy.json() == {"error": "busy", "turn_id": turn_id}
            s = svc._sessions[sid]
            for _ in range(200):
                if s.pending:
                    break
                await asyncio.sleep(0.01)
            (approval_id,) = list(s.pending)
            base = f"/chat/sessions/{sid}/approvals"
            assert (await c.post(f"{base}/nope", headers=H,
                                 json={"decision": "approve"})).status_code == 404
            assert (await c.post(f"{base}/{approval_id}", headers=H,
                                 json={"decision": "maybe"})).status_code == 422
            nope = await c.post(f"{base}/{approval_id}", headers=H,
                                json={"decision": "approve_session"})
            assert nope.status_code == 422 and nope.json()["error"] == "not_allowed"
            ok = await c.post(f"{base}/{approval_id}", headers=H,
                              json={"decision": "approve", "note": "go"})
            assert ok.status_code == 200 and ok.json() == {"ok": True}
            assert (await c.post(f"{base}/{approval_id}", headers=H,
                                 json={"decision": "approve"})).status_code == 404
            grants = await c.get(f"/chat/sessions/{sid}/grants", headers=H)
            assert grants.status_code == 200 and grants.json() == {"grants": []}
            s.grants["uav_scan_targets"] = 123
            assert (await c.get(f"/chat/sessions/{sid}/grants", headers=H)).json() == {
                "grants": [{"tool": "uav_scan_targets", "since_ms": 123}]}
            rm = await c.delete(f"/chat/sessions/{sid}/grants/uav_scan_targets", headers=H)
            assert rm.status_code == 200 and rm.json() == {"ok": True} and not s.grants
            unknown = await c.get("/chat/sessions/nope/grants", headers=H)
            assert unknown.status_code == 404 and unknown.json()["error"] == "unknown_session"
            gone = await c.delete("/chat/sessions/nope/grants/uav_scan_targets", headers=H)
            assert gone.status_code == 404 and gone.json()["error"] == "unknown_session"
            assert (await c.get(f"/chat/sessions/{sid}/grants")).status_code == 401
            assert (await c.delete(f"/chat/sessions/{sid}/grants/uav_scan_targets")
                    ).status_code == 401
            intr = await c.post(f"/chat/sessions/{sid}/interrupt", headers=H)
            assert intr.status_code == 200 and intr.json() == {"ok": True}
            assert (await c.post("/chat/sessions/nope/interrupt", headers=H)).status_code == 404
            gate["release"].set()
            for _ in range(200):
                if s.turn is None:
                    break
                await asyncio.sleep(0.01)
            assert (await c.delete(f"/chat/sessions/{sid}", headers=H)).json() == {"ok": True}
            assert (await c.delete(f"/chat/sessions/{sid}", headers=H)).status_code == 404
            assert (await c.post(url, headers=H, json={"text": "hi"})).status_code == 404
        await svc.shutdown()
    asyncio.run(main())


def test_router_unavailable_is_503_with_the_reason(tmp_path):
    svc = make_service(tmp_path, FakeSdk(), enabled=False)
    app = _app(svc)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            st = (await c.get("/chat/status", headers=H)).json()
            assert st["available"] is False and st["reason"] == "disabled"
            sid = (await c.post("/chat/sessions", headers=H)).json()["session_id"]
            r = await c.post(f"/chat/sessions/{sid}/messages", headers=H, json={"text": "hi"})
            assert r.status_code == 503 and r.json()["reason"] == "disabled"
    asyncio.run(main())


def test_router_sse_stream_framing_auth_and_last_event_id(tmp_path):
    async def turn(client, prompt):
        yield init()
        for m in streamed_text("Hi ", "there."):
            yield m
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))
    app = _app(svc)

    async def main():
        sid = await svc.create_session()
        rec = Recorder(svc, sid)
        await svc.post_message(sid, "hello")
        await rec.wait_name("turn_end")
        path = f"/chat/sessions/{sid}/stream"
        status, _, _ = await _sse_get(app, path)
        assert status == 401
        status, _, _ = await _sse_get(app, "/chat/sessions/nope/stream", f"token={TOKEN}")
        assert status == 404
        status, headers, body = await _sse_get(
            app, path, f"token={TOKEN}", stop=lambda b: "event: turn_end" in b)
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert "no-cache" in headers["cache-control"]
        assert headers["x-accel-buffering"] == "no"
        frames = [f for f in body.split("\n\n") if f]
        assert frames[0] == "retry: 3000"
        assert frames[1].startswith("event: session\ndata: ")
        parsed = []
        for f in frames[2:]:
            lines = f.split("\n")
            assert lines[0].startswith("id: ") and lines[1].startswith("event: ")
            assert lines[2].startswith("data: ") and len(lines) == 3
            parsed.append((int(lines[0][4:]), lines[1][7:], json.loads(lines[2][6:])))
        assert [p[1] for p in parsed] == ["turn_start", "text_delta", "text_delta", "usage",
                                          "turn_end"]
        cut = parsed[1][0]
        status, _, body = await _sse_get(
            app, path, headers={**H, "Last-Event-ID": str(cut)},
            stop=lambda b: "event: turn_end" in b)
        assert status == 200
        ids = [int(line[4:]) for line in body.split("\n") if line.startswith("id: ")]
        assert ids == [p[0] for p in parsed if p[0] > cut]
        rec.stop()
        await svc.shutdown()
    asyncio.run(main())


# ======================================================================
# end to end: real server + real toolbelt + fake CLI
# ======================================================================

HOME = GeoPoint(47.641468, -122.140165, 93.0)
AO_T = [(47.63, -122.16), (47.63, -122.12), (47.66, -122.12), (47.66, -122.16)]
_PORTS = list(range(52200, 52300))
_PORT = itertools.cycle(_PORTS[os.getpid() % len(_PORTS):] + _PORTS[:os.getpid() % len(_PORTS)])


def _nothing_listens(port: int) -> bool:
    """No loopback listener on ``port``. These ranges are shared with live
    checks, and a wildcard bind can SHADOW-share a port someone else holds on
    127.0.0.1 (macOS): the test's clients would then fly the OTHER sim."""
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


@contextmanager
def real_server(tmp_path):
    sim = None
    honour_msgpack_bind_host()   # the sim binds 127.0.0.1, so a held port fails
    for _ in range(len(_PORTS)):
        port = next(_PORT)
        if not _nothing_listens(port):
            continue
        sim = FakeAirSim(home=HOME, port=port)
        try:
            sim.start()
            break
        except OSError:
            sim.stop()
            sim = None
    assert sim is not None, "no free port in 52200-52299"
    store = Store(tmp_path / "srv")
    srv = None
    try:
        client = airsim.MultirotorClient(port=sim.port)
        client.confirmConnection()
        backend = UavBackend(client, HOME, sim=sim)
        envelope = SafetyEnvelope(geofence=AO_T,
                                  home=(HOME.latitude, HOME.longitude, HOME.altitude))
        srv = GodseyeUavServer(backend, store, envelope=envelope, watchdog_s=30.0)
        yield srv
    finally:
        if srv is not None:
            srv.stop_monitor()
            with contextlib.suppress(Exception):
                srv.tasking.shutdown()
        store.close()
        sim.stop()


def test_end_to_end_read_then_approved_takeoff_against_the_real_server(tmp_path):
    async def turn(client, prompt):
        yield init()
        read = {"vehicle": "Drone1"}
        yield tool_use("toolu_r", "uav_get_telemetry", read)
        perm = await client.ask(f"{TOOL_PREFIX}uav_get_telemetry", read, "toolu_r")
        assert isinstance(perm, PermissionResultAllow)
        out = await client.run_tool(f"{TOOL_PREFIX}uav_get_telemetry", perm.updated_input)
        yield UserMessage([ToolResultBlock("toolu_r", out["content"], out.get("is_error"))])
        cmd = {"vehicle": "Drone1", "alt_agl_m": 20}
        yield tool_use("toolu_c", "uav_takeoff", cmd)
        perm = await client.ask(f"{TOOL_PREFIX}uav_takeoff", cmd, "toolu_c")
        assert isinstance(perm, PermissionResultAllow)
        out = await client.run_tool(f"{TOOL_PREFIX}uav_takeoff", perm.updated_input)
        yield UserMessage([ToolResultBlock("toolu_c", out["content"], out.get("is_error"))])
        yield ResultMessage()

    with real_server(tmp_path) as srv:
        svc = ChatService(server=srv, intel=None, store_dir=tmp_path / "store",
                          sdk=FakeSdk(turn))

        async def main():
            sid, rec, _ = await start_turn(svc, "Take Drone1 up to 20 m")
            req = await rec.wait_name("approval_request")
            assert req["tool"] == "uav_takeoff" and req["call_id"] == "toolu_c"
            assert len(rec.of("approval_request")) == 1  # telemetry ran without asking
            await svc.resolve_approval(sid, req["approval_id"], "approve")
            await rec.wait_name("turn_end")
            results = {d["call_id"]: d for d in rec.of("tool_result")}
            assert results["toolu_r"]["ok"] is True
            assert results["toolu_r"]["entities"][0] == "veh:Drone1"
            assert results["toolu_c"]["ok"] is True, results["toolu_c"]
            assert rec.of("ui") == [{"action": "track", "vehicle": "Drone1",
                                     "reason": "mission launched"}]
            await svc.shutdown()
        asyncio.run(main())


def test_prompt_chip_prefixes_are_the_graph_prefixes():
    """The system prompt's [[type:id]] vocabulary is exactly the intel graph's id
    prefixes (seen live: `[[theater:default]]`, a chip nothing resolves, while
    the prompt listed no theater prefix at all)."""
    import re

    from godseye_uav.intel_graph import TYPE_PREFIX

    text = chat_mod._load_prompt()
    used = set(re.findall(r"\[\[([a-z]+):", text)) - {"type"}  # the `[[type:id]]` pattern
    assert used <= set(TYPE_PREFIX.values()), used - set(TYPE_PREFIX.values())
    assert used == set(TYPE_PREFIX.values())
    for prefix in TYPE_PREFIX.values():
        assert chat_mod._FOCUS_ID_RX.match(f"{prefix}:x"), prefix


# ======================================================================
# review-finding regressions
# ======================================================================

class _ListedTool:
    def __init__(self, name, schema):
        self.name, self.input_schema, self.description = name, schema, name


class _DivergentMcp:
    async def list_tools(self):
        return [_ListedTool("uav_orbit_poi", {"type": "object", "properties": {
            "vehicle": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"},
            "radius_m": {"type": "number", "default": 150.0},
            "alt_agl_m": {"type": "number", "default": 60.0},
            "dry_run": {"type": "boolean", "default": False}}})]


class _DivergentServer:
    """The dispatcher (mission_dry_run) and the discrete tool (uav_orbit_poi)
    default the orbit radius differently -- the shape of the live finding."""

    def __init__(self):
        self.mcp = _DivergentMcp()
        self.MISSION_PARAM_DEFAULTS = {"orbit_poi": {"radius_m": 80.0, "alt_agl_m": 60.0}}


def test_matches_args_compares_effective_plans_not_raw_arguments(tmp_path):
    """Review finding: the dry run (mission_dry_run kind=orbit_poi, radius
    defaulted to 80 m by the dispatcher) and the live uav_orbit_poi (radius
    defaulted to 150 m) both omitted radius_m, so the RAW args matched and the
    card claimed the preview matched a larger, costlier orbit."""
    point = {"lat": 47.645, "lon": -122.14}

    async def turn(client, prompt):
        yield init()
        plan = {"vehicle": "Drone1", "kind": "orbit_poi", "params": dict(point)}
        yield tool_use("toolu_1", "mission_dry_run", plan)
        await client.ask(f"{TOOL_PREFIX}mission_dry_run", plan, "toolu_1")
        yield tool_result("toolu_1", {**DRY_RUN_PAYLOAD, "kind": "orbit_poi"})
        for cid, extra in (("toolu_2", {}), ("toolu_3", {"radius_m": 80})):
            live = {"vehicle": "Drone1", **point, **extra}
            yield tool_use(cid, "uav_orbit_poi", live)
            perm = await client.ask(f"{TOOL_PREFIX}uav_orbit_poi", live, cid)
            yield deny_result(cid, perm)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn), server=_DivergentServer())

    async def main():
        sid, rec, _ = await start_turn(svc)
        seen = {}
        for cid in ("toolu_2", "toolu_3"):
            req = (await rec.wait(lambda e, cid=cid: e[1] == "approval_request"
                                  and e[2]["call_id"] == cid))[2]
            seen[cid] = req
            await svc.resolve_approval(sid, req["approval_id"], "deny")
        await rec.wait_name("turn_end")
        # same raw args, different effective radius (80 vs 150): NOT a match
        assert seen["toolu_2"]["dry_run"]["matches_args"] is False
        # different raw args, same effective radius (80 = 80): a match
        assert seen["toolu_3"]["dry_run"]["matches_args"] is True
        await svc.shutdown()
    asyncio.run(main())


def test_effective_plan_applies_each_paths_defaults_and_aliases():
    kd = {"grid_search": {"alt_agl_m": 60.0, "overlap_pct": 20.0, "max_lanes": None}}
    sd = {"mission_grid_search": {"alt_agl_m": 60.0, "overlap_pct": 20.0, "dry_run": False,
                                  "idempotency_key": None}}
    dry = chat_mod._effective_plan(
        "mcp__godseye__mission_dry_run",
        {"vehicle": "D", "kind": "grid_search", "params": json.dumps({"polygon": AO, "alt_m": 60,
                                                                     "overlap": 20})},
        schema_defaults=sd, kind_defaults=kd)
    live = chat_mod._effective_plan("mission_grid_search", {"vehicle": "D", "polygon": AO,
                                                            "dry_run": False},
                                    schema_defaults=sd, kind_defaults=kd)
    assert dry == live == {"vehicle": "D", "polygon": AO, "alt_agl_m": 60, "overlap_pct": 20}
    # one side's defaults unknown: never claims a match it cannot check
    assert chat_mod._effective_plan("mission_grid_search", {"vehicle": "D", "polygon": AO},
                                    schema_defaults={}, kind_defaults=kd) != dry


def test_the_real_servers_dry_run_and_live_orbit_are_compared_on_the_same_plan(tmp_path):
    """End to end on the real server + real toolbelt: a mission_dry_run orbit
    and a live uav_orbit_poi that both omit radius_m now plan the SAME orbit
    (the dispatcher's default was aligned to the tool's), and the card says so;
    a live call with a different radius is not a match."""
    point = {"lat": 47.645, "lon": -122.14}
    out: dict = {}

    async def turn(client, prompt):
        yield init()
        plan = {"vehicle": "Drone1", "kind": "orbit_poi", "params": dict(point)}
        yield tool_use("toolu_1", "mission_dry_run", plan)
        perm = await client.ask(f"{TOOL_PREFIX}mission_dry_run", plan, "toolu_1")
        res = await client.run_tool(f"{TOOL_PREFIX}mission_dry_run", perm.updated_input)
        out["dry"] = json.loads(res["content"][0]["text"])
        yield UserMessage([ToolResultBlock("toolu_1", res["content"], res.get("is_error"))])
        live = {"vehicle": "Drone1", **point, "dry_run": True}
        res = await client.run_tool(f"{TOOL_PREFIX}uav_orbit_poi", live)  # the live plan's product
        out["live_plan"] = json.loads(res["content"][0]["text"])
        for cid, extra in (("toolu_2", {}), ("toolu_3", {"radius_m": 80})):
            call = {"vehicle": "Drone1", **point, **extra}
            yield tool_use(cid, "uav_orbit_poi", call)
            perm = await client.ask(f"{TOOL_PREFIX}uav_orbit_poi", call, cid)
            yield deny_result(cid, perm)
        yield ResultMessage()

    with real_server(tmp_path) as srv:
        svc = ChatService(server=srv, intel=None, store_dir=tmp_path / "store",
                          sdk=FakeSdk(turn))

        async def main():
            sid, rec, _ = await start_turn(svc)
            seen = {}
            for cid in ("toolu_2", "toolu_3"):
                req = (await rec.wait(lambda e, cid=cid: e[1] == "approval_request"
                                      and e[2]["call_id"] == cid))[2]
                seen[cid] = req
                await svc.resolve_approval(sid, req["approval_id"], "deny")
            await rec.wait_name("turn_end")
            await svc.shutdown()
            return seen
        seen = asyncio.run(main())
    assert out["dry"]["gate"]["ok"] is True, out["dry"]
    # the two server paths really plan the same orbit ...
    assert out["dry"]["est_time_s"] == out["live_plan"]["est_time_s"]
    # ... so the card may say it matches; a different radius may not
    same, other = seen["toolu_2"]["dry_run"], seen["toolu_3"]["dry_run"]
    assert same["matches_args"] is True and same["eta_s"] == out["dry"]["est_time_s"]
    assert other["matches_args"] is False


def test_a_truncation_report_dict_counts_as_truncated(tmp_path):
    """Review finding: IntelService marks a cut entity with
    ``_truncated: {"dropped": [...]}``; tool_result said truncated:false."""
    entity = {"id": "rpt:latest", "type": "report", "fields": {"contacts": 10},
              "_truncated": {"dropped": ["raw"]}}

    async def turn(client, prompt):
        yield init()
        yield tool_use("toolu_1", "intel_entity", {"id": "rpt:latest"})
        yield tool_result("toolu_1", entity)
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(turn))

    async def main():
        _, rec, _ = await start_turn(svc)
        await rec.wait_name("turn_end")
        res = rec.of("tool_result")[0]
        assert res["truncated"] is True and res["ok"] is True
        assert "truncated" in res["summary"]
        await svc.shutdown()
    asyncio.run(main())


def test_a_cli_that_dies_right_after_init_is_not_resumed(tmp_path):
    """Review finding: the session id was taken from system:init, before the
    CLI writes its transcript; a CLI killed in between left every later turn
    resuming a conversation that does not exist."""
    async def crash_after_init(client, prompt):
        yield init(session_id="claude-sess-dead")
        raise RuntimeError("CLI process exited with code 137")
        yield  # pragma: no cover

    async def ok_turn(client, prompt):
        yield init(session_id="claude-sess-new")
        yield ResultMessage(session_id="claude-sess-new")

    sdk = FakeSdk(crash_after_init, ok_turn, ok_turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == t1)
        for _ in range(2):
            tid = await svc.post_message(sid, "again")
            await rec.wait(lambda e, tid=tid: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        assert [e["stop"] for e in rec.of("turn_end")] == ["error", "end", "end"]
        await svc.shutdown()
    asyncio.run(main())
    assert "resume" not in sdk.clients[1].options.kw
    assert len(sdk.clients) == 2


def test_a_resume_whose_transcript_is_gone_starts_fresh_and_says_so(tmp_path):
    """The CLI answers --resume=<id> with 'No conversation found' and exit 1
    (real CLI 2.1.283). The session must stop resuming that id."""
    async def ok_turn(client, prompt):
        yield init(session_id="claude-sess-7")
        yield ResultMessage(session_id="claude-sess-7")

    async def crash_turn(client, prompt):
        yield init(session_id="claude-sess-7")
        raise RuntimeError("CLI process exited with code 1")
        yield  # pragma: no cover

    async def resume_fails(client, prompt):
        client.options.stderr("Error: No conversation found with session ID: claude-sess-7")
        raise RuntimeError("Command failed with exit code 1. Check stderr output for details")
        yield  # pragma: no cover

    sdk = FakeSdk(ok_turn, crash_turn, resume_fails, ok_turn)
    svc = make_service(tmp_path, sdk)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == t1)
        for _ in range(3):
            tid = await svc.post_message(sid, "again")
            await rec.wait(lambda e, tid=tid: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        assert [e["stop"] for e in rec.of("turn_end")] == ["end", "error", "error", "end"]
        lost = rec.of("error")[-1]
        assert lost["message"] == chat_mod.RESUME_LOST_MESSAGE and lost["retryable"] is True
        await svc.shutdown()
    asyncio.run(main())
    assert sdk.clients[1].options.kw["resume"] == "claude-sess-7"   # it did try
    assert "resume" not in sdk.clients[2].options.kw                 # and then stopped


def test_a_resume_rejected_in_the_result_is_forgotten(tmp_path):
    async def rejected(client, prompt):
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            session_id="claude-sess-7",
                            errors=["No conversation found with session ID: claude-sess-7"])

    svc = make_service(tmp_path, FakeSdk(rejected))

    async def main():
        sid, rec, _ = await start_turn(svc)
        svc._sessions[sid].claude_session_id = "claude-sess-7"
        end = await rec.wait_name("turn_end")
        assert end["stop"] == "error"
        assert rec.of("error")[-1]["message"] == chat_mod.RESUME_LOST_MESSAGE
        assert svc._sessions[sid].claude_session_id is None
        await svc.shutdown()
    asyncio.run(main())


def test_an_idle_session_releases_its_cli_and_resumes_on_the_next_message(tmp_path):
    """Review finding: nothing closed an idle session's CLI (about 186 MB each)."""
    async def ok_turn(client, prompt):
        yield init()
        yield ResultMessage(session_id="claude-sess-1")

    sdk = FakeSdk(ok_turn, ok_turn)
    svc = make_service(tmp_path, sdk)
    svc.idle_disconnect_s = 0.2

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == t1)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + T
        while not sdk.clients[0].disconnected:
            assert loop.time() < deadline, "the idle CLI was never disconnected"
            await asyncio.sleep(0.05)
        assert sid in svc._sessions                     # the session itself stays
        tid = await svc.post_message(sid, "still there?")
        end = await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        assert end[2]["stop"] == "end"
        await svc.shutdown()
    asyncio.run(main())
    assert len(sdk.clients) == 2
    assert sdk.clients[1].options.kw["resume"] == "claude-sess-1"


def test_shutdown_closes_sessions_in_parallel(tmp_path):
    """A busy CLI takes seconds to go away; the host gives the analyst a fixed
    shutdown budget, which serial closes used up after two sessions."""
    async def ok_turn(client, prompt):
        yield init()
        yield ResultMessage()

    sdk = FakeSdk(ok_turn, ok_turn, ok_turn)

    async def slow_disconnect(self):
        await asyncio.sleep(0.6)
        self.disconnected = True

    sdk.ClaudeSDKClient.disconnect = slow_disconnect
    svc = make_service(tmp_path, sdk)

    async def main():
        for _ in range(3):
            _, rec, tid = await start_turn(svc)
            await rec.wait(lambda e, tid=tid: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await svc.shutdown()
        return loop.time() - t0
    took = asyncio.run(main())
    assert all(c.disconnected for c in sdk.clients)
    assert took < 1.4, f"shutdown took {took:.2f}s: the three 0.6 s closes ran one after another"


# ======================================================================
# BYOK model providers (spec §4.4, §5, §8, §9): a fake llm_settings
# ======================================================================
# Fake keys only (spec §11): nothing here reaches a network or a keychain.

KEY_A, KEY_B = "test-key-123", "test-key-456"
BLANK = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_USE_BEDROCK": "",
         "CLAUDE_CODE_USE_VERTEX": "", "CLAUDE_CODE_USE_FOUNDRY": "",
         "CLAUDE_CODE_USE_MANTLE": "", "CLAUDE_CODE_USE_ANTHROPIC_AWS": ""}
ADAPTIVE = {"type": "adaptive", "display": "summarized"}
LOGIN_REF = {"id": "anthropic_login", "label": "Claude login (this Mac)"}


@dataclass(frozen=True)
class FakeRP:
    """Shaped like llm_settings.ResolvedProvider (spec §2)."""
    id: str = "anthropic_login"
    label: str = "Claude login (this Mac)"
    kind: str = "anthropic_login"
    model_family: str = "claude"
    host: str | None = None
    model: str = "claude-opus-5"
    thinking: dict = field(default_factory=lambda: dict(ADAPTIVE))
    effort: str | None = None
    env: dict = field(default_factory=lambda: dict(BLANK), repr=False)
    cost_basis: str = "anthropic_list"
    identity: str = "ident-login"
    generation: int = 1
    ready: bool = True
    reason: str | None = None


def _harden(tmp_path) -> dict:
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "store" / "analyst" / "claude-home"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_MAX_RETRIES": "2"}


def provider(name: str, tmp_path, key: str = KEY_A, **kw) -> FakeRP:
    """One ResolvedProvider per catalog kind, env shaped per spec §4.3."""
    h = _harden(tmp_path)
    table = {
        "login": FakeRP(),
        "anthropic_api": FakeRP(id="anthropic_api", label="Anthropic API key",
                                kind="anthropic_key", identity="ident-api",
                                env={**BLANK, **h, "ANTHROPIC_API_KEY": key}),
        "openrouter": FakeRP(
            id="openrouter", label="OpenRouter", kind="anthropic_compatible",
            model_family="mixed", host="openrouter.ai", model="anthropic/claude-opus-5.5",
            cost_basis="unreliable", identity="ident-openrouter",
            env={**BLANK, **h, "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
                 "ANTHROPIC_AUTH_TOKEN": key, "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL": "anthropic/claude-opus-5.5"}),
        "custom": FakeRP(
            id="custom", label="Custom Anthropic-compatible endpoint", kind="custom",
            model_family="non_claude", host="127.0.0.1", model="stub-model",
            thinking={"type": "disabled"}, cost_basis="unreliable", identity="ident-custom",
            env={**BLANK, **h, "ANTHROPIC_BASE_URL": "http://127.0.0.1:53999",
                 "ANTHROPIC_AUTH_TOKEN": key, "CLAUDE_CODE_DISABLE_THINKING": "1"}),
        "bedrock": FakeRP(id="bedrock", label="Amazon Bedrock", kind="bedrock",
                          host="us-east-1", model="us.anthropic.claude-opus-5-5",
                          identity="ident-bedrock",
                          env={**BLANK, **h, "CLAUDE_CODE_USE_BEDROCK": "1",
                               "AWS_REGION": "us-east-1", "AWS_BEARER_TOKEN_BEDROCK": key}),
        "vertex": FakeRP(id="vertex", label="Google Cloud Agent Platform (Vertex AI)",
                         kind="vertex", host="global", model="claude-opus-5-5",
                         identity="ident-vertex",
                         env={**BLANK, **h, "CLAUDE_CODE_USE_VERTEX": "1",
                              "ANTHROPIC_VERTEX_PROJECT_ID": "proj-12345"}),
        "foundry": FakeRP(id="foundry", label="Microsoft Foundry", kind="foundry",
                          host="res-1", model="claude-opus-5-5", identity="ident-foundry",
                          env={**BLANK, **h, "CLAUDE_CODE_USE_FOUNDRY": "1",
                               "ANTHROPIC_FOUNDRY_RESOURCE": "res-1",
                               "ANTHROPIC_FOUNDRY_API_KEY": key}),
    }
    rp = table[name]
    return dataclasses.replace(rp, **kw) if kw else rp


PROVIDER_NAMES = ("login", "anthropic_api", "openrouter", "custom", "bedrock", "vertex",
                  "foundry")


class FakeLlm:
    """Stands in for llm_settings.LlmSettings: ``aresolve()``/``resolve()``, the
    keychain-free ``status()`` (same shape as the real one) and
    ``generation``. Tests swap ``rp`` to simulate a settings change."""

    def __init__(self, rp: FakeRP, *, key_source: str = "keychain", rev: int = 3):
        self.rp = rp
        self.key_source = key_source
        self.rev = rev
        self.resolves = 0

    @property
    def generation(self):
        return self.rp.generation

    def resolve(self):
        self.resolves += 1
        return self.rp

    async def aresolve(self):
        return self.resolve()

    def status(self):
        rp = self.rp
        out = {"provider": {"id": rp.id, "label": rp.label, "kind": rp.kind,
                            "model_family": rp.model_family, "host": rp.host,
                            "key_source": "login" if rp.kind == "anthropic_login"
                            else self.key_source, "configured": rp.ready},
               "cost_basis": rp.cost_basis, "settings_rev": self.rev, "ready": rp.ready,
               "model": rp.model}
        if rp.reason:
            out["reason"] = rp.reason
        return out


def ok_turn_factory(session_id="claude-sess-1", text="OK."):
    async def turn(client, prompt):
        yield init(session_id=session_id)
        for m in streamed_text(text):
            yield m
        yield ResultMessage(session_id=session_id, total_cost_usd=0.02,
                            usage={"input_tokens": 3, "output_tokens": 2})
    return turn


def run_one_turn(tmp_path, llm=None, turn=None, **kw):
    """One turn on a fresh service; returns (sdk, events, svc)."""
    sdk = FakeSdk(turn or ok_turn_factory())
    svc = make_service(tmp_path, sdk, llm=llm, **kw)

    async def main():
        _, rec, tid = await start_turn(svc)
        await rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == tid)
        await svc.shutdown()
        rec.stop()
        return list(rec.events)
    return sdk, asyncio.run(main()), svc


#: The option set ``_options()`` built before the BYOK spec (Claude login, no
#: effort, no cli_path, no resume). Callables are compared by kind.
def _pre_byok_options(tmp_path) -> dict:
    return {"model": DEFAULT_MODEL, "tools": [], "strict_mcp_config": True,
            "setting_sources": [], "verbatim_prompts": True, "permission_mode": "default",
            "include_partial_messages": True, "thinking": dict(ADAPTIVE), "max_turns": 40,
            "cwd": str(tmp_path / "store" / "analyst")}


def _comparable(kw: dict) -> dict:
    out = {}
    for k, v in kw.items():
        if callable(v):
            out[k] = "<callable>"
        elif k == "mcp_servers":
            out[k] = sorted(v)
        elif k == "system_prompt":
            out[k] = len(v)
        else:
            out[k] = v
    return out


def test_the_login_provider_keeps_the_pre_byok_options_and_adds_only_env(tmp_path, monkeypatch):
    """Spec §4.4: the default path is today's option set; the only addition is
    ``env`` (credentials and switches blanked, spec §4.1)."""
    monkeypatch.delenv("GODSEYE_CHAT_MODEL", raising=False)
    monkeypatch.delenv("GODSEYE_CHAT_EFFORT", raising=False)
    legacy_sdk, _, _ = run_one_turn(tmp_path / "a")
    login_sdk, _, _ = run_one_turn(tmp_path / "b", llm=FakeLlm(provider("login", tmp_path)))
    legacy = legacy_sdk.clients[0].options.kw
    login = login_sdk.clients[0].options.kw
    for key, value in _pre_byok_options(tmp_path / "a").items():
        assert legacy[key] == value, key
    assert set(legacy) == {*_pre_byok_options(tmp_path), "system_prompt", "allowed_tools",
                           "disallowed_tools", "mcp_servers", "can_use_tool", "stderr"}
    assert "env" not in legacy
    assert login["env"] == BLANK                       # no base URL, no config dir, no pins
    assert {k: v for k, v in _comparable(login).items() if k not in ("env", "cwd")} == {
        k: v for k, v in _comparable(legacy).items() if k != "cwd"}
    assert login["cwd"] == str(tmp_path / "b" / "store" / "analyst")


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_options_differ_across_providers_only_in_model_thinking_effort_and_env(
        tmp_path, monkeypatch, name):
    """Spec §9.9: the invariant, over every catalog kind."""
    monkeypatch.delenv("GODSEYE_CHAT_MODEL", raising=False)
    monkeypatch.delenv("GODSEYE_CHAT_EFFORT", raising=False)
    base_sdk, _, _ = run_one_turn(tmp_path, llm=FakeLlm(provider("login", tmp_path)),
                                  effort="high")
    rp = provider(name, tmp_path)
    sdk, _, _ = run_one_turn(tmp_path, llm=FakeLlm(rp), effort="high")
    base = _comparable(base_sdk.clients[0].options.kw)
    kw = sdk.clients[0].options.kw
    varying = {"model", "thinking", "effort", "env"}
    assert {k: v for k, v in _comparable(kw).items() if k not in varying} == {
        k: v for k, v in base.items() if k not in varying}
    for fixed in ("permission_mode", "allowed_tools", "tools", "setting_sources",
                  "verbatim_prompts", "strict_mcp_config", "disallowed_tools"):
        assert kw[fixed] == base_sdk.clients[0].options.kw[fixed], fixed
    assert callable(kw["can_use_tool"])
    assert kw["model"] == rp.model and kw["thinking"] == rp.thinking
    assert kw["env"] == rp.env                        # the key travels only here
    assert ("effort" in kw) == (rp.kind in chat_mod.EFFORT_KINDS)
    for key, value in kw.items():
        if key != "env" and isinstance(value, str):
            assert KEY_A not in value, key            # never argv-bound options


def test_provider_options_precedence():
    """Spec §3.4: model flag/env win only for the first-party Claude kinds;
    effort only where the kind allows it; thinking and env pass through."""
    po = chat_mod.provider_options
    login = FakeRP(model="")
    assert po(login)["model"] == DEFAULT_MODEL
    assert po(login, model_override="claude-x")["model"] == "claude-x"
    assert po(FakeRP(kind="anthropic_key", model="m"), model_override="o")["model"] == "o"
    compat = FakeRP(kind="anthropic_compatible", model="MiniMax-M3[1m]")
    assert po(compat, model_override="claude-x")["model"] == "MiniMax-M3[1m]"
    assert "effort" not in po(compat, effort_override="high")
    assert po(FakeRP(kind="bedrock"), effort_override="high")["effort"] == "high"
    assert po(FakeRP(effort="low"))["effort"] == "low"
    assert "effort" not in po(FakeRP(effort="turbo"))
    off = po(FakeRP(kind="custom", thinking={"type": "disabled"}))
    assert off["thinking"] == {"type": "disabled"}
    assert "env" not in po(chat_mod._LegacyProvider(model="m", effort=None))
    env = po(FakeRP(env={"A": "1"}))["env"]
    assert env == {"A": "1"}


def test_check_options_reuse_the_analyst_builder_with_the_checks_changes(tmp_path):
    """Spec §6.2: the full check gets the same builder: only env extras, the
    dummy tool server, max_turns=1 and a refuse-all permission callback."""
    sdk = FakeSdk()
    svc = make_service(tmp_path, sdk)
    rp = provider("custom", tmp_path)
    opts = svc.check_options(rp, env_extra={"CLAUDE_CODE_MAX_RETRIES": "0"},
                             mcp_servers={"check": {"type": "sdk"}},
                             allowed_tools=["mcp__check__check_echo"])
    kw = opts.kw
    assert kw["env"] == {**rp.env, "CLAUDE_CODE_MAX_RETRIES": "0"}
    assert kw["max_turns"] == 1 and kw["thinking"] == {"type": "disabled"}
    assert kw["model"] == "stub-model" and kw["tools"] == []
    assert kw["permission_mode"] == "default" and kw["setting_sources"] == []
    assert list(kw["mcp_servers"]) == ["check"]
    assert kw["allowed_tools"] == ["mcp__check__check_echo"]
    denied = asyncio.run(kw["can_use_tool"]("mcp__godseye__uav_takeoff", {}, None))
    assert isinstance(denied, PermissionResultDeny)
    assert rp.env == provider("custom", tmp_path).env   # the provider's env is not mutated


def _ends(rec, tid):
    return rec.wait(lambda e: e[1] == "turn_end" and e[2]["turn_id"] == tid)


def test_an_identity_change_starts_fresh_and_says_so_before_turn_start(tmp_path):
    """Spec §8: a new provider identity disconnects, drops the resume id and
    emits provider_changed{memory:"cleared"} just before the next turn_start."""
    llm = FakeLlm(provider("login", tmp_path))
    sdk = FakeSdk(ok_turn_factory("sess-a"), ok_turn_factory("sess-b"))
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        llm.rp = provider("openrouter", tmp_path, generation=2)
        t2 = await svc.post_message(sid, "again")
        await _ends(rec, t2)
        await svc.shutdown()
        rec.stop()
        return rec
    rec = asyncio.run(main())
    names = rec.names()
    i = names.index("provider_changed")
    assert names[i + 1] == "turn_start" and rec.events[i + 1][2]["text"] == "again"
    changed = rec.of("provider_changed")[0]
    assert changed["memory"] == "cleared" and isinstance(changed["at_ms"], int)
    assert changed["from"] == {**LOGIN_REF, "model": DEFAULT_MODEL}
    assert changed["to"] == {"id": "openrouter", "label": "OpenRouter",
                             "model": "anthropic/claude-opus-5.5"}
    assert len(sdk.clients) == 2 and sdk.clients[0].disconnected
    second = sdk.clients[1].options.kw
    assert "resume" not in second                      # no resume across providers
    assert second["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY_A
    assert second["model"] == "anthropic/claude-opus-5.5"


def test_a_model_only_change_keeps_the_memory_and_resumes(tmp_path):
    llm = FakeLlm(provider("openrouter", tmp_path))
    sdk = FakeSdk(ok_turn_factory("sess-a"), ok_turn_factory("sess-a"))
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        llm.rp = provider("openrouter", tmp_path, generation=2, model="minimax/minimax-m3")
        t2 = await svc.post_message(sid, "again")
        await _ends(rec, t2)
        await svc.shutdown()
        return rec
    rec = asyncio.run(main())
    changed = rec.of("provider_changed")
    assert [c["memory"] for c in changed] == ["kept"]
    assert changed[0]["to"]["model"] == "minimax/minimax-m3"
    assert sdk.clients[0].disconnected
    assert sdk.clients[1].options.kw["resume"] == "sess-a"
    assert sdk.clients[1].options.kw["model"] == "minimax/minimax-m3"


def test_an_unchanged_generation_reuses_the_cli_and_emits_nothing(tmp_path):
    llm = FakeLlm(provider("openrouter", tmp_path))
    sdk = FakeSdk(ok_turn_factory(), ok_turn_factory())
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        t2 = await svc.post_message(sid, "again")
        await _ends(rec, t2)
        await svc.shutdown()
        return rec
    rec = asyncio.run(main())
    assert "provider_changed" not in rec.names()
    assert len(sdk.clients) == 1 and llm.resolves == 2


def test_a_running_turn_finishes_on_the_old_provider(tmp_path):
    """Spec §8: a settings change never interrupts the running turn."""
    gate = {}

    async def slow(client, prompt):
        yield init(session_id="sess-a")
        await gate["release"].wait()
        for m in streamed_text("done"):
            yield m
        yield ResultMessage(session_id="sess-a")

    llm = FakeLlm(provider("openrouter", tmp_path))
    sdk = FakeSdk(slow, ok_turn_factory("sess-b"))
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        gate["release"] = asyncio.Event()
        sid, rec, t1 = await start_turn(svc)
        await rec.wait_name("turn_start")
        await asyncio.sleep(0.05)
        llm.rp = provider("custom", tmp_path, key=KEY_B, generation=5)
        await asyncio.sleep(0.05)
        assert not sdk.clients[0].disconnected and sdk.clients[0].interrupts == 0
        gate["release"].set()
        end = await _ends(rec, t1)
        assert end[2]["stop"] == "end"
        assert "provider_changed" not in rec.names()
        t2 = await svc.post_message(sid, "next")
        await _ends(rec, t2)
        await svc.shutdown()
        return rec
    rec = asyncio.run(main())
    assert sdk.clients[0].interrupts == 0
    assert sdk.clients[0].options.kw["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY_A
    assert sdk.clients[1].options.kw["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY_B
    assert rec.of("provider_changed")[0]["memory"] == "cleared"


@pytest.mark.parametrize(("reason", "message"), [
    ("provider_key_missing", "The analyst has no key for OpenRouter."),
    ("provider_not_configured", "The analyst isn't set up to use OpenRouter."),
])
def test_a_provider_that_is_not_ready_never_spawns_a_cli(tmp_path, reason, message):
    """Spec §4.4/§5: rp.ready False fails the turn with code config; no connect."""
    llm = FakeLlm(provider("openrouter", tmp_path, ready=False, reason=reason, env={}))
    sdk = FakeSdk()
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        st = svc.status()
        assert st["available"] is False and st["reason"] == reason
        assert st["hint"] == "Open analyst settings."
        _, rec, tid = await start_turn(svc)
        end = (await _ends(rec, tid))[2]
        await svc.shutdown()
        return rec, end
    rec, end = asyncio.run(main())
    assert sdk.clients == []
    assert end == {"turn_id": end["turn_id"], "stop": "error", "error": reason}
    err = rec.of("error")[0]
    assert err["code"] == "config" and err["message"] == message
    assert err["provider"] == {"id": "openrouter", "label": "OpenRouter"}
    assert err["retryable"] is False
    assert "provider_changed" not in rec.names()


def test_a_failing_resolve_is_a_settings_error_not_a_crash(tmp_path):
    class Broken(FakeLlm):
        def resolve(self):
            raise RuntimeError("settings file unreadable")

    sdk, events, _svc = run_one_turn(tmp_path, llm=Broken(provider("login", tmp_path)))
    errors = [d for _, n, d in events if n == "error"]
    assert errors[0]["code"] == "config"
    assert errors[0]["message"] == "The analyst's model settings could not be read."
    assert sdk.clients == []


@pytest.mark.parametrize(("status", "error", "text", "code"), [
    (401, "invalid_request", "", "auth"),              # gateway 401: the status decides
    (None, "authentication_failed", "", "auth"),
    (403, None, "", "auth"),
    (402, None, "", "billing"),
    (None, "billing_error", "", "billing"),
    (404, None, "", "model"),
    (None, "model_not_found", "", "model"),            # outside the SDK's Literal
    (429, None, "", "rate_limit"),
    (None, "rate_limit", "", "rate_limit"),
    (400, None, "Input tag 'adaptive' is invalid", "invalid_request"),
    (422, None, "", "invalid_request"),
    (None, "unknown", "API Error: 400 bad thinking", "invalid_request"),
    (500, None, "", "server"), (502, None, "", "server"), (503, None, "", "server"),
    (529, "server_error", "", "server"),
    (None, "server_error", "", "server"),
    # The CLI reports a refused connection as server_error with no status.
    (None, "server_error", "Unable to connect: ECONNREFUSED 127.0.0.1:53999", "network"),
    (None, "server_error", "API Error: 500 upstream timed out", "server"),
    (None, None, "connect ECONNREFUSED 127.0.0.1:53999", "network"),
    (None, None, "getaddrinfo ENOTFOUND api.example", "network"),
    (None, None, "Request timed out", "network"),
    (None, None, "unable to verify the first certificate", "network"),
    (None, None, "something odd", "unknown"),
    (True, None, "", "unknown"),                       # a bool is not a status
])
def test_classify_provider_error_covers_every_row_of_the_table(status, error, text, code):
    assert chat_mod.classify_provider_error(status, error, text) == code


def test_provider_error_copy_is_the_specs_copy():
    copy = chat_mod.provider_error_copy
    assert copy("auth", label="MiniMax", status=401) == (
        "MiniMax rejected the key.", "Open analyst settings to replace the key.", False)
    assert copy("auth", label="MiniMax", status=403)[0] == "MiniMax refused access for this key."
    assert copy("billing", label="X")[0] == "X reports a billing problem."
    assert copy("model", label="X", model="m-1")[0] == "X doesn't recognize the model m-1."
    assert copy("rate_limit", label="X")[2] is True
    msg, hint, retry = copy("invalid_request", label="X", detail="Input tag 'adaptive' bad")
    assert msg == "X rejected the request: Input tag 'adaptive' bad" and retry is False
    assert hint == "Turn off extended thinking for this model in analyst settings."
    assert copy("invalid_request", label="X", detail="nope")[1] == (
        "Run a full check in analyst settings.")
    assert copy("server", label="X")[0] == "X had a server error."
    assert copy("network", label="X", host="api.x.io")[0] == "Couldn't reach api.x.io."
    msg, hint, retry = copy("unknown", label="X", detail="d" * 900)
    assert msg == "The analyst hit an error with X: " + "d" * 300 and hint is None and retry


def _error_turn(*, status=None, error=None, text="", result_text=None):
    async def turn(client, prompt):
        yield init()
        if error:
            yield AssistantMessage([TextBlock(text)], error=error)
        yield ResultMessage(is_error=True, api_error_status=status,
                            result=result_text or text or "failed")
    return turn


def test_a_gateway_401_is_an_auth_error_and_marks_the_provider(tmp_path):
    """PLUMBING §7: api_error_status=401 with error="invalid_request"."""
    llm = FakeLlm(provider("openrouter", tmp_path))
    _sdk, events, svc = run_one_turn(
        tmp_path, llm=llm, turn=_error_turn(status=401, error="invalid_request",
                                            text="API Error: 401 invalid_request"))
    err = [d for _, n, d in events if n == "error"]
    assert err == [{"message": "OpenRouter rejected the key.",
                    "hint": "Open analyst settings to replace the key.", "retryable": False,
                    "code": "auth", "provider": {"id": "openrouter", "label": "OpenRouter"}}]
    st = svc.status()
    assert st["available"] is False and st["reason"] == "provider_auth"
    llm.rp = provider("openrouter", tmp_path, key=KEY_B, generation=2)
    assert svc.status()["available"] is True             # a new key clears it


def test_a_turn_that_works_clears_provider_auth_on_the_same_generation(tmp_path):
    llm = FakeLlm(provider("openrouter", tmp_path))
    sdk = FakeSdk(_error_turn(status=401, error="authentication_failed", text="bad key"),
                  ok_turn_factory())
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        assert svc.status()["reason"] == "provider_auth"
        t2 = await svc.post_message(sid, "again")          # the provider recovered
        assert (await _ends(rec, t2))[2]["stop"] == "end"
        await svc.shutdown()
    asyncio.run(main())
    assert svc.status()["available"] is True and "reason" not in svc.status()


def test_model_not_found_and_403_and_network_get_provider_copy(tmp_path):
    rp = provider("custom", tmp_path)
    _, events, _ = run_one_turn(tmp_path / "m", llm=FakeLlm(rp),
                                turn=_error_turn(error="model_not_found", text="no such model"))
    assert [(d["code"], d["message"]) for _, n, d in events if n == "error"] == [
        ("model", ("Custom Anthropic-compatible endpoint doesn't recognize the model "
                  "stub-model."))]
    _, events, _ = run_one_turn(tmp_path / "f", llm=FakeLlm(rp),
                                turn=_error_turn(status=403, result_text="Forbidden"))
    err = next(d for _, n, d in events if n == "error")
    assert err["code"] == "auth" and "refused access" in err["message"]

    async def refused(client, prompt):
        yield init()
        raise RuntimeError("connect ECONNREFUSED 127.0.0.1:53999")
        yield  # pragma: no cover

    _, events, _ = run_one_turn(tmp_path / "n", llm=FakeLlm(rp), turn=refused)
    err = next(d for _, n, d in events if n == "error")
    assert err["code"] == "network" and err["message"] == "Couldn't reach 127.0.0.1."
    assert err["retryable"] is True


def test_the_claude_login_keeps_its_own_copy_with_a_code(tmp_path):
    _, events, svc = run_one_turn(
        tmp_path, llm=FakeLlm(provider("login", tmp_path)),
        turn=_error_turn(status=429, error="rate_limit", text="limit"))
    err = [d for _, n, d in events if n == "error"]
    assert err == [{"message": "Claude usage limit reached.",
                    "hint": "Wait for the limit to reset, then retry.", "retryable": True,
                    "code": "rate_limit", "provider": LOGIN_REF}]
    assert svc.status()["available"] is True             # login errors keep today's status


LONG_KEY = "sk-or-v1-" + "a1b2c3d4e5f6" * 4                # fake; 57 characters


def test_redactor_catches_the_key_its_fragments_and_its_json_form():
    red = chat_mod._Redactor()
    red.add(LONG_KEY)
    red.add("lmstudio")                                    # a placeholder token: left alone
    assert red.text(f"401: bad key {LONG_KEY}!") == "401: bad key [redacted key]!"
    assert LONG_KEY[20:32] not in red.text(f"tail ...{LONG_KEY[20:32]}...")
    assert red.text(json.dumps({"k": LONG_KEY})) == '{"k": "[redacted key]"}'
    assert red.text("token lmstudio ok") == "token lmstudio ok"
    assert red.value({"a": [LONG_KEY], "b": 3}) == {"a": ["[redacted key]"], "b": 3}


def test_a_key_the_provider_echoes_never_reaches_events_logs_or_the_transcript(
        tmp_path, caplog):
    """Spec §9.5/§9.6 (DESIGN S10/S11): a 401 that echoes the key."""
    rp = provider("custom", tmp_path, key=LONG_KEY)
    config_dir = Path(rp.env["CLAUDE_CONFIG_DIR"])
    transcript = config_dir / "projects" / "-store-analyst" / "sess-x.jsonl"
    echo = f"Invalid bearer token {LONG_KEY}"

    async def echo_turn(client, prompt):
        client.options.stderr(f"[api] 401 for Authorization: Bearer {LONG_KEY}")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"message": {"content": echo}}) + "\n")
        yield init(session_id="sess-x")
        yield AssistantMessage([TextBlock(echo)], error="authentication_failed")
        yield ResultMessage(session_id="sess-x", is_error=True, api_error_status=401,
                            result=echo, errors=[echo])

    sdk = FakeSdk(echo_turn, ok_turn_factory("sess-x"))
    svc = make_service(tmp_path, sdk, llm=FakeLlm(rp))
    caplog.set_level("INFO", logger="godseye_uav.chat")

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        chat_mod.log.warning("provider said: %s", echo)   # the module logger is filtered
        frames = []
        async for frame in svc.sse_frames(sid, heartbeat_s=0.01):
            frames.append(frame)
            if len(frames) > 3 and frame.startswith(": ping"):
                break
        tail = list(svc._sessions[sid].stderr_tail)
        t2 = await svc.post_message(sid, "again")
        await _ends(rec, t2)
        await svc.shutdown()
        return rec, frames, tail
    rec, frames, tail = asyncio.run(main())
    blob = json.dumps([e[2] for e in rec.events]) + "".join(frames) + " ".join(tail)
    fragment = LONG_KEY[-12:]
    assert LONG_KEY not in blob and fragment not in blob
    assert "[redacted key]" in " ".join(tail)
    assert LONG_KEY not in caplog.text and fragment not in caplog.text
    assert rec.of("error")[0]["code"] == "auth"
    # the turn's transcript was scrubbed after the turn, with the CLI stopped
    assert transcript.is_file() and LONG_KEY not in transcript.read_text()
    assert "[redacted key]" in transcript.read_text()
    assert stat.S_IMODE(transcript.stat().st_mode) == 0o600
    assert sdk.clients[0].disconnected
    assert sdk.clients[1].options.kw["resume"] == "sess-x"   # resumes the clean transcript


def test_scrub_transcripts_rewrites_only_the_sessions_own_files(tmp_path):
    """Another session's CLI may be appending to its file in the same folder:
    only this session's transcript (and its side folder) is replaced."""
    cfg = tmp_path / "home"
    folder = cfg / "projects" / "-store-analyst"
    (folder / "s1").mkdir(parents=True)
    line = f'{{"k": "{LONG_KEY}"}}\n'
    for name in ("s1.jsonl", "s2.jsonl", "s1/agent-1.jsonl"):
        (folder / name).write_text(line)
    red = chat_mod._Redactor()
    red.add(LONG_KEY)
    done = chat_mod.scrub_transcripts(cfg, session_ids=["s1", "../x"], redact=red.text)
    assert done == [folder / "s1" / "agent-1.jsonl", folder / "s1.jsonl"]
    assert LONG_KEY not in (folder / "s1.jsonl").read_text()
    assert LONG_KEY in (folder / "s2.jsonl").read_text()   # another session: untouched
    assert chat_mod.scrub_transcripts(tmp_path / "none", session_ids=["s1"],
                                      redact=red.text) == []


@pytest.mark.parametrize(("name", "priced"), [("login", True), ("anthropic_api", True),
                                               ("bedrock", True), ("openrouter", False),
                                               ("custom", False)])
def test_usage_names_its_cost_basis_and_drops_invented_dollars(tmp_path, name, priced):
    """Spec §8: the CLI prices a model it doesn't know from its own table."""
    rp = provider(name, tmp_path)
    _, events, _ = run_one_turn(tmp_path, llm=FakeLlm(rp))
    usage = [d for _, n, d in events if n == "usage"][-1]
    assert usage["cost_basis"] == rp.cost_basis
    assert ("cost_usd" in usage) is priced and ("session_cost_usd" in usage) is priced
    assert usage["input_tokens"] == 3 and usage["output_tokens"] == 2


def test_status_and_the_session_event_name_the_provider_without_secrets(tmp_path):
    rp = provider("openrouter", tmp_path)
    llm = FakeLlm(rp, rev=7)
    svc = make_service(tmp_path, FakeSdk(), llm=llm)
    st = svc.status()
    assert st["available"] is True and st["model"] == "anthropic/claude-opus-5.5"
    assert st["provider"] == {"id": "openrouter", "label": "OpenRouter",
                              "kind": "anthropic_compatible", "model_family": "mixed",
                              "host": "openrouter.ai", "key_source": "keychain",
                              "configured": True}
    assert st["cost_basis"] == "unreliable" and st["settings_rev"] == 7
    assert "effort" not in st and llm.resolves == 0     # status never resolves (no keychain)
    assert KEY_A not in json.dumps(st) and "env" not in json.dumps(st)

    async def main():
        sid = await svc.create_session()
        async for seq, name, data in svc.subscribe(sid):
            assert (seq, name) == (0, "session")
            return data
    session = asyncio.run(main())
    assert session["provider"] == {"id": "openrouter", "label": "OpenRouter"}
    assert session["model"] == "anthropic/claude-opus-5.5"
    login = make_service(tmp_path, FakeSdk(), llm=FakeLlm(provider("login", tmp_path)),
                         model="claude-flag", effort="high")
    st = login.status()
    assert st["model"] == "claude-flag" and st["effort"] == "high"
    assert st["provider"]["key_source"] == "login" and st["cost_basis"] == "anthropic_list"


def test_a_summary_without_provider_status_falls_back_to_the_last_resolve(tmp_path):
    class Minimal:
        def __init__(self, rp):
            self.rp = rp

        def resolve(self):
            return self.rp

    svc = make_service(tmp_path, FakeSdk(), llm=Minimal(provider("custom", tmp_path)))
    assert svc.status()["provider"]["id"] == "unknown"     # nothing resolved yet
    asyncio.run(svc._resolve_provider())
    st = svc.status()
    assert st["provider"]["id"] == "custom" and st["model"] == "stub-model"
    assert KEY_A not in json.dumps(st)


@pytest.mark.parametrize("name", ["login", "openrouter", "custom", "bedrock"])
def test_the_approval_gate_is_the_same_whatever_the_model(tmp_path, name):
    """Spec §0.8/§11.2: a hostile model's command still waits for approval;
    an unknown tool is a command; denying never runs it."""
    seen: dict = {}

    async def hostile(client, prompt):
        yield init()
        for tool, args, call in (("mcp__godseye__uav_takeoff", {"vehicle": "Drone1"}, "t1"),
                                 ("Bash", {"command": "ls"}, "t2")):
            yield AssistantMessage([ToolUseBlock(call, tool, args)])
            seen[call] = perm = await client.ask(tool, args, call)
            yield deny_result(call, perm)
        for m in streamed_text("Takeoff approved and executed"):
            yield m
        yield ResultMessage()

    svc = make_service(tmp_path, FakeSdk(hostile), llm=FakeLlm(provider(name, tmp_path)))

    async def main():
        sid, rec, tid = await start_turn(svc)
        for n in (1, 2):
            req = await rec.wait(lambda e, n=n: e[1] == "approval_request"
                                 and len(rec.of("approval_request")) >= n)
            await svc.resolve_approval(sid, rec.of("approval_request")[n - 1]["approval_id"],
                                       "deny")
        await _ends(rec, tid)
        await svc.shutdown()
        return rec, req
    rec, _ = asyncio.run(main())
    reqs = rec.of("approval_request")
    assert [(r["tool"], r["class"]) for r in reqs] == [("uav_takeoff", "command"),
                                                      ("Bash", "command")]
    assert [r["outcome"] for r in rec.of("tool_result")] == ["not_run", "not_run"]
    assert all(isinstance(p, PermissionResultDeny) for p in seen.values())
    assert rec.of("ui") == []                             # the claim in text changes nothing


# ---- review fixes (secrets lens) --------------------------------------------

def test_the_chat_redactor_covers_every_valid_key_but_the_placeholders():
    """An all-letter key of 8-11 characters is a valid key (validate_key): it
    is redacted; only the keyless-local placeholder tokens are left alone."""
    from godseye_uav import llm_settings
    assert chat_mod.PLACEHOLDER_TOKENS == llm_settings.PLACEHOLDER_TOKENS
    red = chat_mod._Redactor()
    for key in ("abcdefghij", "secretkey"):
        assert llm_settings.validate_key(key) == key
        red.add(key)
        assert key not in red.text(f"invalid api key: {key}")
    for token in chat_mod.PLACEHOLDER_TOKENS:
        red.add(token)
        assert red.text(f"token {token} ok") == f"token {token} ok"


def split_key_turn(key: str, session_id: str = "sess-split"):
    """A provider that answers 200 and quotes the credential in 5-character
    text deltas (no single delta holds 12 characters of it)."""
    text = f"Upstream said: invalid key {key}. Please retry."
    chunks = [text[i:i + 5] for i in range(0, len(text), 5)]

    async def turn(client, prompt):
        yield init(session_id=session_id)
        yield StreamEvent({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "thinking", "thinking": ""}})
        for c in chunks:
            yield StreamEvent({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "thinking_delta", "thinking": c}})
        yield StreamEvent({"type": "content_block_stop", "index": 0})
        for m in streamed_text(*chunks):
            yield m
        yield ResultMessage(session_id=session_id)
    return turn, text


def test_a_key_split_across_deltas_is_redacted_and_the_transcript_scrubbed(tmp_path):
    rp = provider("custom", tmp_path, key=KEY_A)
    config_dir = Path(rp.env["CLAUDE_CONFIG_DIR"])
    transcript = config_dir / "projects" / "-store-analyst" / "sess-split.jsonl"
    turn, text = split_key_turn(KEY_A)

    async def writing_turn(client, prompt):
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"message": {"content": text}}) + "\n")
        async for m in turn(client, prompt):
            yield m

    sdk = FakeSdk(writing_turn)
    svc = make_service(tmp_path, sdk, llm=FakeLlm(rp))

    async def main():
        sid, rec, tid = await start_turn(svc)
        await _ends(rec, tid)
        frames = []
        async for frame in svc.sse_frames(sid, heartbeat_s=0.01):
            frames.append(frame)
            if frame.startswith(": ping"):
                break
        await svc.shutdown()
        return rec, frames
    rec, frames = asyncio.run(main())
    said = "".join(e["text"] for e in rec.of("text_delta"))
    thought = "".join(e["text"] for e in rec.of("thinking"))
    for blob in (said, thought, "".join(frames)):
        assert KEY_A not in blob
    assert said == thought == text.replace(KEY_A, "[redacted key]")
    assert KEY_A not in transcript.read_text() and "[redacted key]" in transcript.read_text()
    assert sdk.clients[0].disconnected


def test_a_session_holds_back_only_what_could_still_be_a_key():
    s = chat_mod._Session("s", chat_mod._Redactor())
    assert s.stream("text", "no key known yet: all of it") == "no key known yet: all of it"
    s._redactor.add(KEY_A)
    out = [s.stream("text", d) for d in ("invalid key tes", "t-key", "-123.", " ok then")]
    out.append(s.flush_stream("text"))
    assert "".join(out) == "invalid key [redacted key]. ok then"
    assert all(KEY_A[:5] not in piece or "[redacted" in piece for piece in out)
    assert s.needs_scrub is True
    assert s.flush_stream("text") == ""


def test_a_full_text_block_with_a_key_flags_the_scrub_even_when_streamed(tmp_path):
    """The final TextBlock is checked even when its deltas were already sent."""
    s = chat_mod._Session("s", chat_mod._Redactor())
    s._redactor.add(KEY_A)
    svc = make_service(tmp_path, FakeSdk())
    turn = chat_mod._Turn("t1", text_chars=10, any_text=True)
    s.turn = turn
    svc._on_assistant(s, turn, AssistantMessage([TextBlock(f"key was {KEY_A}")]))
    assert s.needs_scrub is True and len(s.log) == 0   # nothing re-sent, scrub flagged


class PreflightLlm(FakeLlm):
    def __init__(self, rp, refusal):
        super().__init__(rp)
        self.refusal = refusal
        self.preflights = 0

    async def preflight(self, rp):
        self.preflights += 1
        return self.refusal


def test_a_redirecting_endpoint_never_spawns_a_cli(tmp_path):
    rp = provider("custom", tmp_path)
    refusal = {"code": "config", "status": 307, "hint": "Use the endpoint URL the provider "
               "documents.", "message": "127.0.0.1 tried to redirect the request. "
               "The key wasn't sent on."}
    llm = PreflightLlm(rp, refusal)
    sdk = FakeSdk()
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        _, rec, tid = await start_turn(svc)
        end = (await _ends(rec, tid))[2]
        await svc.shutdown()
        return rec, end
    rec, end = asyncio.run(main())
    assert sdk.clients == [] and llm.preflights == 1
    assert end["stop"] == "error"
    err = rec.of("error")[0]
    assert (err["code"], err["message"], err["hint"]) == (
        "config", refusal["message"], refusal["hint"])
    assert err["retryable"] is False


def test_a_clean_preflight_spawns_once_and_is_not_rerun_on_a_live_cli(tmp_path):
    llm = PreflightLlm(provider("custom", tmp_path), None)
    sdk = FakeSdk(ok_turn_factory(), ok_turn_factory())
    svc = make_service(tmp_path, sdk, llm=llm)

    async def main():
        sid, rec, t1 = await start_turn(svc)
        await _ends(rec, t1)
        t2 = await svc.post_message(sid, "again")
        await _ends(rec, t2)
        await svc.shutdown()
    asyncio.run(main())
    assert len(sdk.clients) == 1 and llm.preflights == 1
