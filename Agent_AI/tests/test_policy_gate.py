"""The policy-gate invariants — the safety contract of the whole system.

These tests drive the REAL policy_node + gated_tool_node through a real
compiled LangGraph, with a scripted agent node instead of an LLM. The
invariants under test:

  1. A destructive tool call ALWAYS interrupts the graph.
  2. Safe (read) tool calls never interrupt.
  3. A denied call NEVER executes and yields a refusal ToolMessage.
  4. An approved call executes exactly once.
  5. DEFAULT-DENY: a destructive call that is not EXPLICITLY approved in
     the resume payload (empty/partial/timeout decisions) must not run.
  6. In a mixed batch, only destructive calls are surfaced for approval,
     and safe calls still run when a destructive one is denied.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import Command

import agent_v5_approval as agent

CFG = {"configurable": {"thread_id": "t-test"}}


class FakeTool:
    """Stands in for a real tool in _TOOLS_BY_NAME; records invocations."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def invoke(self, args):
        self.calls.append(args)
        return {"ok": True, "tool": self.name}


def tc(name, cid, **args):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def make_app(scripted_responses):
    """The real graph wiring with the agent node replaced by a script."""
    queue = list(scripted_responses)

    def scripted_agent(state):
        return {"messages": [queue.pop(0)]}

    g = StateGraph(agent.AgentState)
    g.add_node("agent", scripted_agent)
    g.add_node("policy", agent.policy_node)
    g.add_node("tools", agent.gated_tool_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition, {"tools": "policy", END: END})
    g.add_edge("policy", "tools")
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=MemorySaver())


@pytest.fixture
def fake_restart(monkeypatch):
    fake = FakeTool("restart_lxc")
    monkeypatch.setitem(agent._TOOLS_BY_NAME, "restart_lxc", fake)
    return fake


@pytest.fixture
def fake_check(monkeypatch):
    fake = FakeTool("check_disk_health")
    monkeypatch.setitem(agent._TOOLS_BY_NAME, "check_disk_health", fake)
    return fake


def start(app, *first_turn_calls):
    return app.invoke(
        {"messages": [HumanMessage("do it")], "denied_ids": []}, CFG
    )


def tool_messages(state):
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


# ── invariant 1 & payload shape ─────────────────────────────────────


def test_destructive_call_always_interrupts(fake_restart):
    app = make_app([
        AIMessage("", tool_calls=[tc("restart_lxc", "c1", node="pve", vmid=101)]),
        AIMessage("done"),
    ])
    result = start(app)
    assert "__interrupt__" in result, "graph must pause on a destructive call"
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "approval_request"
    assert [c["tool_call_id"] for c in payload["destructive_calls"]] == ["c1"]
    assert fake_restart.calls == [], "nothing may run before the operator decides"


def test_safe_tools_never_interrupt(fake_check):
    app = make_app([
        AIMessage("", tool_calls=[tc("check_disk_health", "c1")]),
        AIMessage("done"),
    ])
    result = start(app)
    assert "__interrupt__" not in result
    assert fake_check.calls == [{}]


# ── invariants 3 & 4: deny blocks, approve runs ─────────────────────


def test_denied_call_never_executes_and_emits_refusal(fake_restart):
    app = make_app([
        AIMessage("", tool_calls=[tc("restart_lxc", "c1", vmid=101)]),
        AIMessage("done"),
    ])
    start(app)
    result = app.invoke(
        Command(resume=[{"tool_call_id": "c1", "decision": "denied", "by": "op"}]),
        CFG,
    )
    assert fake_restart.calls == [], "a denied tool call must never execute"
    refusals = [m for m in tool_messages(result) if "REFUSED" in m.content]
    assert len(refusals) == 1 and refusals[0].tool_call_id == "c1"
    assert result["denied_ids"] == [], "denied_ids must reset for the next turn"


def test_approved_call_executes_exactly_once(fake_restart):
    app = make_app([
        AIMessage("", tool_calls=[tc("restart_lxc", "c1", vmid=101)]),
        AIMessage("done"),
    ])
    start(app)
    result = app.invoke(
        Command(resume=[{"tool_call_id": "c1", "decision": "approved", "by": "op"}]),
        CFG,
    )
    assert fake_restart.calls == [{"vmid": 101}]
    assert not [m for m in tool_messages(result) if "REFUSED" in m.content]


# ── invariant 5: DEFAULT-DENY on anything short of explicit approval ─


@pytest.mark.parametrize(
    "decisions",
    [
        [],                                                      # empty payload
        [{"tool_call_id": "c1", "decision": "timeout"}],         # timeout
        [{"tool_call_id": "other", "decision": "approved"}],     # wrong id approved
        [{"decision": "approved"}],                              # id missing
    ],
    ids=["empty", "timeout", "wrong-id", "missing-id"],
)
def test_default_deny_without_explicit_approval(fake_restart, decisions):
    app = make_app([
        AIMessage("", tool_calls=[tc("restart_lxc", "c1", vmid=101)]),
        AIMessage("done"),
    ])
    start(app)
    result = app.invoke(Command(resume=decisions), CFG)
    assert fake_restart.calls == [], (
        "a destructive call NOT explicitly approved must never run "
        f"(decisions={decisions!r})"
    )
    assert [m for m in tool_messages(result) if "REFUSED" in m.content]


# ── invariant 6: mixed batches ──────────────────────────────────────


def test_mixed_batch_gates_only_destructive(fake_restart, fake_check):
    app = make_app([
        AIMessage("", tool_calls=[
            tc("check_disk_health", "c-safe"),
            tc("restart_lxc", "c-danger", vmid=101),
        ]),
        AIMessage("done"),
    ])
    result = start(app)
    payload = result["__interrupt__"][0].value
    assert [c["tool_call_id"] for c in payload["destructive_calls"]] == ["c-danger"]

    result = app.invoke(
        Command(resume=[{"tool_call_id": "c-danger", "decision": "denied"}]), CFG
    )
    assert fake_check.calls == [{}], "safe calls still run when a peer is denied"
    assert fake_restart.calls == []
    by_id = {m.tool_call_id: m for m in tool_messages(result)}
    assert "REFUSED" in by_id["c-danger"].content
    assert "REFUSED" not in by_id["c-safe"].content


# ── restart_vm is gated exactly like restart_lxc ─────────────────────


@pytest.fixture
def fake_restart_vm(monkeypatch):
    fake = FakeTool("restart_vm")
    monkeypatch.setitem(agent._TOOLS_BY_NAME, "restart_vm", fake)
    return fake


def test_restart_vm_is_gated(fake_restart_vm):
    """Most of this homelab is QEMU — the VM tool must gate like the LXC one."""
    app = make_app([
        AIMessage("", tool_calls=[tc("restart_vm", "v1", node="Proxmox", vmid=100)]),
        AIMessage("done"),
    ])
    result = start(app)
    assert "__interrupt__" in result
    assert [c["tool_call_id"]
            for c in result["__interrupt__"][0].value["destructive_calls"]] == ["v1"]
    assert fake_restart_vm.calls == []

    result = app.invoke(
        Command(resume=[{"tool_call_id": "v1", "decision": "denied"}]), CFG)
    assert fake_restart_vm.calls == []
    assert [m for m in tool_messages(result) if "REFUSED" in m.content]
