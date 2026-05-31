"""
LangGraph + LangSmith instrumented version of the homelab agent.

LangSmith is LangChain's observability platform. Once a few env vars
are set, EVERY LLM call, tool call, and graph step is automatically
traced to the LangSmith UI — no code changes required.

This file shows three things:
  1. The minimal env setup (auto-tracing — the only thing 90% of
     teams need).
  2. Adding tags / metadata / run_name so traces are searchable.
  3. The @traceable decorator for instrumenting arbitrary Python
     functions outside the graph.

Setup:
  pip install langsmith
  Then either set these env vars in your shell or put them in .env:
      LANGSMITH_TRACING=true
      LANGSMITH_API_KEY=ls__...               # from smith.langchain.com
      LANGSMITH_PROJECT=homelab-sentinel       # any name; LangSmith creates on first run
      LANGSMITH_ENDPOINT=https://api.smith.langchain.com   # default; omit unless self-hosted
"""

import os
import sys
import uuid
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langsmith import traceable, Client

from tools import (
    check_proxmox_status as _check_proxmox_status,
    get_ha_entity as _get_ha_entity,
    restart_lxc as _restart_lxc,
    send_telegram_alert as _send_telegram_alert,
)

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()


# -------------------------------------------------------------------
# 1.  LangSmith activation
# -------------------------------------------------------------------
# Setting LANGSMITH_TRACING=true is the master switch. Once on, every
# LangChain / LangGraph component auto-emits trace events. The other
# vars say WHERE to send them and under what project name.
#
# We set them defensively here so the file is self-contained, but
# in real projects you put them in .env or your secrets manager.
os.environ.setdefault("LANGSMITH_TRACING", "true")
os.environ.setdefault("LANGSMITH_PROJECT", "homelab-sentinel")
# LANGSMITH_API_KEY must come from your environment — never hardcode.

if not os.getenv("LANGSMITH_API_KEY"):
    print("[WARN] LANGSMITH_API_KEY not set — traces will not be uploaded.")
    print("       Get one at https://smith.langchain.com/ -> Settings -> API Keys")


# -------------------------------------------------------------------
# 2.  Tools (identical to v3 — but note the @traceable on the wrapper
#     to record extra metadata in LangSmith)
# -------------------------------------------------------------------
# @tool already makes these traceable via LangChain's callback system.
# We DON'T need to add @traceable — it would double-log.
# But for any *non-tool* helper you wrote yourself, @traceable is how
# you get it into the trace. See section 6 below.

@tool
def check_proxmox_status(node: str, vmid: int) -> dict:
    """Get status (running/stopped, memory %, uptime) of a Proxmox LXC or VM.

    Args:
        node: Proxmox node name, e.g. 'pve'
        vmid: VM/LXC ID, e.g. 100
    """
    return _check_proxmox_status(node, vmid)


@tool
def get_ha_entity(entity_id: str) -> dict:
    """Get current state of a Home Assistant entity."""
    return _get_ha_entity(entity_id)


@tool
def restart_lxc(node: str, vmid: int) -> dict:
    """Restart an LXC container. Use only after confirming the container is unhealthy."""
    return _restart_lxc(node, vmid)


@tool
def send_telegram_alert(message: str) -> dict:
    """Send a Telegram message to the operator."""
    return _send_telegram_alert(message)


tools = [check_proxmox_status, get_ha_entity, restart_lxc, send_telegram_alert]


# -------------------------------------------------------------------
# 3.  System prompt
# -------------------------------------------------------------------
SYSTEM = """You are HomelabSentinel, an SRE agent for Naveen's homelab.
Your job: investigate the user's question, use tools to gather data, decide if action is needed and also take approval from user via telegram alert before taking any action.
Rules:
- Always check status before restarting anything.
- If mem_pct > 85, recommend restart but send a Telegram alert first.
- If a container is stopped, restart it AND alert.
- Explain your reasoning briefly at the end."""


# -------------------------------------------------------------------
# 4.  Graph (same shape as v3)
# -------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=2048)
llm_with_tools = llm.bind_tools(tools)


def agent_node(state: AgentState) -> dict:
    response = llm_with_tools.invoke(
        [SystemMessage(content=SYSTEM)] + state["messages"]
    )
    return {"messages": [response]}


tool_node = ToolNode(tools)

graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")

app = graph.compile()


# -------------------------------------------------------------------
# 5.  Pre-processing helper instrumented with @traceable
# -------------------------------------------------------------------
# @traceable wraps a normal Python function so it appears as a span
# in the LangSmith trace tree. Use it for ANY custom logic you want
# visibility into: input validation, preprocessing, post-processing,
# external API calls, scoring, etc.

@traceable(name="validate_user_request", run_type="chain")
def validate_user_request(msg: str) -> str:
    """Light pre-flight check. Shows up as its own span in LangSmith."""
    if not msg or not msg.strip():
        raise ValueError("Empty user message")
    if len(msg) > 4000:
        msg = msg[:4000] + " ... [truncated]"
    return msg


# -------------------------------------------------------------------
# 6.  Run with explicit run config (tags, metadata, run_name)
# -------------------------------------------------------------------
if __name__ == "__main__":
    raw_msg = "Check on the openclaw container (vmid 100) and mqtt broker (vmid 102). Fix anything broken."
    user_msg = validate_user_request(raw_msg)

    # Generate a thread/session id so you can group related runs in
    # the LangSmith UI (useful for multi-turn conversations).
    session_id = str(uuid.uuid4())

    # `config` is passed through LangGraph to every callback/tracer.
    # - tags: free-form strings, filterable in the UI
    # - metadata: arbitrary key/value, indexed and searchable
    # - run_name: human-readable label for this run in the trace list
    config = {
        "run_name": "homelab-sentinel-check",
        "tags": ["env:dev", "agent:v4", "user:naveen"],
        "metadata": {
            "session_id": session_id,
            "version": "v4-langsmith",
            "operator": "naveen",
        },
    }

    result = app.invoke(
        {"messages": [{"role": "user", "content": user_msg}]},
        config=config,
    )

    print("\n=== FINAL ===")
    print(result["messages"][-1].content)

    # -----------------------------------------------------------------
    # 7.  Optional: programmatic access to the trace via langsmith.Client
    # -----------------------------------------------------------------
    # The Client lets you fetch runs, add feedback (thumbs up/down),
    # build evaluation datasets, etc. Most teams don't need this on
    # day 1, but it's the foundation of LLM evals.
    if os.getenv("LANGSMITH_API_KEY"):
        client = Client()
        # Find recent runs for this session_id (eventually consistent;
        # may take a few seconds for traces to be indexed).
        try:
            recent = list(client.list_runs(
                project_name=os.environ["LANGSMITH_PROJECT"],
                filter=f'has(tags, "agent:v4")',
                limit=3,
            ))
            print(f"\n[LangSmith] Last {len(recent)} runs in project "
                  f"'{os.environ['LANGSMITH_PROJECT']}':")
            for r in recent:
                print(f"  - {r.name}  status={r.status}  "
                      f"latency={r.total_time:.2f}s  id={r.id}")
        except Exception as e:
            print(f"[LangSmith] Client query failed (expected on first run): {e}")
