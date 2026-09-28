"""The Anthropic stub's script mode (WG §4.5 "Stub support", A14).

``StubAnthropic(mode="script")`` + ``configure(script=[[step, ...], ...])``:
one step list per operator message, the step picked by the tool_use turns
since that message, ``$ref`` resolved against the latest ``tool_result`` of a
tool, "Done (scripted)." when a list runs out.

No network: the stub binds loopback inside A14's port range (54090-54099) and
every request is a plain loopback POST with proxies disabled.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from support.stub_anthropic import (
    SCRIPT_DONE,
    SCRIPT_TOOL_PREFIX,
    ScriptError,
    StubAnthropic,
    resolve_path,
    resolve_refs,
    script_position,
)

TOOLS = [{"name": SCRIPT_TOOL_PREFIX + n, "input_schema": {"type": "object"}}
         for n in ("geo_lookup", "theater_propose", "sim_set_theater", "ui_show_map")]
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

SCRIPT = [
    [
        {"tool": "geo_lookup", "input": {"query": "12.97160, 77.59460"}},
        {"tool": "theater_propose",
         "input": {"lat": {"$ref": "geo_lookup.candidates.0.lat"},
                   "lon": {"$ref": "geo_lookup.candidates[id=c2].lon"},
                   "label": "Bengaluru centre"}},
        {"tool": "sim_set_theater", "input": {"$ref": "theater_propose.set_args"}},
    ],
    [
        {"tool": "ui_show_map", "text": "Opening the map.",
         "input": {"ids": [{"$ref": "sim_set_theater.theater.id"}], "reason": "Watch"}},
    ],
]
GEO = {"candidates": [{"id": "c1", "lat": 12.9716, "lon": 77.5946},
                      {"id": "c2", "lat": 1.0, "lon": 2.5}]}
PROPOSAL = {"proposal_id": "p1", "set_args": {"proposal_id": "p1", "theater_id": "dyn-x",
                                              "airframe": "quad_suas_electric"}}


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("GODSEYE_TEST_PORTS", "54090-54099")
    with StubAnthropic(mode="script") as s:
        s.configure(script=SCRIPT)
        yield s


def post(stub: StubAnthropic, messages: list, *, tools=TOOLS, stream: bool = False):
    body = {"model": "stub-model", "max_tokens": 64, "messages": messages,
            "tools": tools, "stream": stream}
    req = urllib.request.Request(f"{stub.url}/v1/messages", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          "x-api-key": "test-key-123"}, method="POST")
    with _OPENER.open(req, timeout=10) as r:
        raw = r.read().decode()
    return raw if stream else json.loads(raw)


def user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def reminder() -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>context</system-reminder>"}]}


def turn(reply: dict, result) -> list[dict]:
    """The assistant turn the stub gave, plus the tool_result the CLI returns."""
    use = next(b for b in reply["content"] if b["type"] == "tool_use")
    text = result if isinstance(result, str) else json.dumps(result)
    return [{"role": "assistant", "content": reply["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                          "content": [{"type": "text", "text": text}]}]}]


def tool_use(reply: dict) -> dict:
    return next(b for b in reply["content"] if b["type"] == "tool_use")


def test_one_operator_message_runs_its_steps_in_order_with_refs(stub):
    convo = [user("Fly a recce over 12.97160, 77.59460")]
    r1 = post(stub, convo)
    assert r1["stop_reason"] == "tool_use"
    assert tool_use(r1)["name"] == "mcp__godseye__geo_lookup"
    assert tool_use(r1)["input"] == {"query": "12.97160, 77.59460"}
    convo += turn(r1, GEO)
    r2 = post(stub, convo)
    assert tool_use(r2)["name"] == "mcp__godseye__theater_propose"
    # numeric index and a [key=value] filter
    assert tool_use(r2)["input"] == {"lat": 12.9716, "lon": 2.5, "label": "Bengaluru centre"}
    convo += turn(r2, PROPOSAL)
    r3 = post(stub, convo)
    # a whole-input ref
    assert tool_use(r3)["input"] == PROPOSAL["set_args"]
    convo += turn(r3, {"status": "accepted", "theater": {"id": "dyn-x", "epoch": 1}})
    r4 = post(stub, convo)
    assert r4["stop_reason"] == "end_turn"
    assert r4["content"] == [{"type": "text", "text": SCRIPT_DONE}]
    log = stub.script_log()
    assert [e.get("tool") for e in log] == ["geo_lookup", "theater_propose",
                                            "sim_set_theater", None]
    assert log[-1]["done"] is True


def test_the_second_operator_message_picks_the_second_list(stub):
    convo = [user("first")]
    for result in (GEO, PROPOSAL, {"theater": {"id": "dyn-x"}}):
        convo += turn(post(stub, convo), result)
    convo += [{"role": "assistant", "content": [{"type": "text", "text": SCRIPT_DONE}]},
              user("Show me")]
    r = post(stub, convo)
    assert r["content"][0] == {"type": "text", "text": "Opening the map."}
    assert tool_use(r)["input"] == {"ids": ["dyn-x"], "reason": "Watch"}
    convo += turn(r, {"ok": True})
    assert post(stub, convo)["content"][0]["text"] == SCRIPT_DONE
    # a third operator message has no list: done at once
    convo += [{"role": "assistant", "content": [{"type": "text", "text": SCRIPT_DONE}]},
              user("and again")]
    assert post(stub, convo)["content"][0]["text"] == SCRIPT_DONE


def test_denied_calls_still_advance_the_step(stub):
    convo = [user("go")]
    r1 = post(stub, convo)
    convo += turn(r1, "The operator denied this call.")
    r2 = post(stub, convo)
    # the ref into geo_lookup cannot resolve against a plain-text denial
    assert r2["stop_reason"] == "end_turn"
    assert r2["content"][0]["text"].startswith("Script error:")
    assert "error" in stub.script_log()[-1]


def test_reminder_only_messages_and_side_calls_do_not_count(stub):
    convo = [reminder(), user("go")]
    assert tool_use(post(stub, convo))["name"].endswith("geo_lookup")
    # a CLI side call (no mcp tools offered) gets OK and consumes nothing
    side = post(stub, [user("Summarise this conversation")], tools=[])
    assert side["content"] == [{"type": "text", "text": "OK"}]
    convo += turn(post(stub, convo), GEO) + [reminder()]
    assert tool_use(post(stub, convo))["name"].endswith("theater_propose")
    assert {"side_call": True} in stub.script_log()


def test_streaming_replies_carry_the_resolved_input(stub):
    raw = post(stub, [user("go")], stream=True)
    deltas = [json.loads(line[6:]) for line in raw.splitlines()
              if line.startswith("data: ") and "input_json_delta" in line]
    assert len(deltas) == 1
    assert json.loads(deltas[0]["delta"]["partial_json"]) == {"query": "12.97160, 77.59460"}
    assert '"name": "mcp__godseye__geo_lookup"' in raw


def test_configure_rejects_a_script_that_is_not_a_list_of_lists(stub):
    with pytest.raises(TypeError):
        stub.configure(script=[{"tool": "geo_lookup"}])
    with pytest.raises(AttributeError):
        stub.configure(script_entries=[])


def test_resolve_path_grammar():
    doc = {"a": [{"k": "x", "v": [10, 20]}, {"k": "y", "v": [30]}], "b": {"c": None}}
    assert resolve_path(doc, "a.0.k") == "x"
    assert resolve_path(doc, "a[1].v.0") == 30
    assert resolve_path(doc, "a[k=y].v[0]") == 30
    assert resolve_path(doc, "a.-1.k") == "y"
    assert resolve_path(doc, "b.c") is None
    for bad in ("a.9", "a[k=z]", "b.missing", "a.k", "b.c.d"):
        with pytest.raises(ScriptError):
            resolve_path(doc, bad)


def test_resolve_refs_walks_nested_values_and_names_a_missing_tool():
    results = {"geo_lookup": GEO}
    out = resolve_refs({"p": [{"$ref": "geo_lookup.candidates.1.id"}, 3],
                        "whole": {"$ref": "geo_lookup"}}, results)
    assert out == {"p": ["c2", 3], "whole": GEO}
    # a dict with other keys next to $ref is data, not a ref
    assert resolve_refs({"$ref": "x", "y": 1}, results) == {"$ref": "x", "y": 1}
    with pytest.raises(ScriptError, match="no result from theater_propose"):
        resolve_refs({"$ref": "theater_propose.set_args"}, results)


def test_script_position_counts_operator_messages_and_steps():
    convo = [reminder(), user("one")]
    assert script_position(convo)[:2] == (0, 0)
    convo += [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__godseye__geo_lookup", "input": {}}]},
              {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "t1", "content": '{"a": 1}'}]}]
    op, steps, results = script_position(convo)
    assert (op, steps) == (0, 1) and results == {"geo_lookup": {"a": 1}}
    convo += [{"role": "assistant", "content": [{"type": "text", "text": "done"}]},
              user("two")]
    assert script_position(convo)[:2] == (1, 0)
    assert script_position([])[:2] == (-1, 0)
