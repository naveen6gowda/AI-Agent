"""The MCP server enforces the same gate contract as the in-process
agent — server-side, so no client (human or AI) can bypass it."""

import pytest

import mcp_server
import registry


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def invoke(self, args):
        self.calls.append(args)
        return {"ok": True}


@pytest.fixture
def fake_restart(monkeypatch):
    fake = FakeTool("restart_lxc")
    monkeypatch.setitem(registry.TOOLS_BY_NAME, "restart_lxc", fake)
    return fake


def test_unknown_tool_is_an_error():
    out = mcp_server.dispatch("frobnicate", {})
    assert "error" in out and "unknown tool" in out["error"]


def test_safe_tool_runs_without_approval(monkeypatch):
    fake = FakeTool("check_disk_health")
    monkeypatch.setitem(registry.TOOLS_BY_NAME, "check_disk_health", fake)
    out = mcp_server.dispatch(
        "check_disk_health", {},
        approval_fn=lambda *_: pytest.fail("read tools must not ask"))
    assert out == {"ok": True}
    assert fake.calls == [{}]


def test_destructive_denied_never_executes(fake_restart):
    out = mcp_server.dispatch("restart_lxc", {"vmid": 1},
                              approval_fn=lambda *_: "denied")
    assert fake_restart.calls == []
    assert out["error"] == "REFUSED by operator"


@pytest.mark.parametrize("verdict", ["timeout", "", None, "yes"])
def test_destructive_default_deny(fake_restart, verdict):
    out = mcp_server.dispatch("restart_lxc", {"vmid": 1},
                              approval_fn=lambda *_: verdict)
    assert fake_restart.calls == [], f"ran despite verdict={verdict!r}"
    assert out["error"] == "REFUSED by operator"


def test_destructive_approved_executes_once(fake_restart):
    out = mcp_server.dispatch("restart_lxc", {"vmid": 1},
                              approval_fn=lambda *_: "approved")
    assert fake_restart.calls == [{"vmid": 1}]
    assert out == {"ok": True}


def test_input_schema_is_object_schema():
    schema = mcp_server._input_schema(registry.TOOLS_BY_NAME["check_proxmox_status"])
    assert schema.get("type") == "object"
    assert "vmid" in schema.get("properties", {})


def test_every_registry_tool_gets_a_valid_schema():
    for t in registry.TOOLS:
        schema = mcp_server._input_schema(t)
        assert schema.get("type") == "object", t.name


# ── LLM fallback chain (Phase 5) ────────────────────────────────────


class _DeadLLM:
    def invoke(self, _msgs):
        raise RuntimeError("Connection refused by llm.test")


def test_dead_llm_without_fallback_raises_llm_unavailable(monkeypatch):
    import agent_v5_approval as agent
    from models import LLMUnavailable
    monkeypatch.setattr(agent, "has_fallback", lambda: False)
    with pytest.raises(LLMUnavailable):
        agent._invoke_resilient(_DeadLLM(), [])


def test_dead_llm_uses_fallback_when_configured(monkeypatch):
    import agent_v5_approval as agent

    class _FallbackLLM:
        def bind_tools(self, _tools):
            return self

        def invoke(self, _msgs):
            return "fallback-answer"

    monkeypatch.setattr(agent, "has_fallback", lambda: True)
    monkeypatch.setattr(agent, "fallback_llm", lambda: _FallbackLLM())
    monkeypatch.setattr(agent, "_record_llm_observation", lambda *a: None)
    assert agent._invoke_resilient(_DeadLLM(), []) == "fallback-answer"
