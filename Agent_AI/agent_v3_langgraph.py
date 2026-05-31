"""
LangGraph version of the homelab agent.

In v2 we used `langchain.agents.create_agent(...)` — a one-liner that
returns a *prebuilt* LangGraph under the hood.

In v3 we build the same graph BY HAND so every piece (state, nodes,
edges, routing) is visible. This is the form you'd extend in
production: add new nodes (human approval, retry, parallel branches),
swap the state schema, plug in a checkpointer, etc.

Install:
    pip install "langgraph>=1.0" langchain-anthropic
"""

import sys
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools import (LXC
    check_proxmox_status as _check_proxmox_status,
    get_ha_entity as _get_ha_entity,
    restart_lxc as _restart_lxc,
    send_telegram_alert as _send_telegram_alert,
)

sys.stdout.reconfigure(encoding="utf-8")   # Windows console safety for emoji
load_dotenv()


# -------------------------------------------------------------------
# 1.  Tools  (identical to v2)
# -------------------------------------------------------------------
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
# 2.  System prompt
# -------------------------------------------------------------------
SYSTEM = """You are HomelabSentinel, an SRE agent for Naveen's homelab.
Your job: investigate the user's question, use tools to gather data, decide if action is needed and also take approval from user via telegram alert before taking any action.
Rules:
- Always check status before restarting anything.
- If mem_pct > 85, recommend restart but send a Telegram alert first.
- If a container is stopped, restart it AND alert.
- Explain your reasoning briefly at the end."""


# -------------------------------------------------------------------
# 3.  Graph STATE
# -------------------------------------------------------------------
# The graph's state is just one field: a list of messages.
# `add_messages` is a "reducer" — when a node returns new messages,
# they are APPENDED to the existing list (not overwritten). This is
# the LangGraph equivalent of `messages.append(...)` from v1.
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# -------------------------------------------------------------------
# 4.  LLM with tools bound
# -------------------------------------------------------------------
# .bind_tools tells the model what tools are available. From this
# point on, `llm` can emit tool_call blocks.
llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=2048)
llm_with_tools = llm.bind_tools(tools)


# -------------------------------------------------------------------
# 5.  Graph NODES
# -------------------------------------------------------------------
# A node is just a function: state -> partial state update.

def agent_node(state: AgentState) -> dict:
    """Call the LLM. Returns one new message (AIMessage), possibly
    containing tool_calls. The reducer appends it to state.messages."""
    response = llm_with_tools.invoke(
        [SystemMessage(content=SYSTEM)] + state["messages"]
    )
    return {"messages": [response]}


# `ToolNode` is a prebuilt node that:
#   1. Looks at the last AIMessage in state.messages
#   2. Reads its .tool_calls
#   3. Dispatches each one to the matching @tool function
#   4. Returns a list of ToolMessage results
# This is the LangGraph equivalent of the `for block in resp.content:
# if block.type == "tool_use": ...` loop in v1.
tool_node = ToolNode(tools)


# -------------------------------------------------------------------
# 6.  Build the GRAPH
# -------------------------------------------------------------------
#
#                ┌─────────┐
#       START ──►│  agent  │◄──────┐
#                └────┬────┘       │
#                     │            │
#         tools_condition routes:  │
#         ── tool_calls present? ──┤
#                     │            │
#                     ▼            │
#                ┌─────────┐       │
#                │  tools  │───────┘
#                └────┬────┘
#                     │ (no tool_calls)
#                     ▼
#                    END
#
graph = StateGraph(AgentState)

graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)

graph.add_edge(START, "agent")

# Conditional edge: after the agent runs, decide where to go next.
# `tools_condition` is a prebuilt helper: it returns "tools" if the
# last AIMessage has tool_calls, else END.
graph.add_conditional_edges("agent", tools_condition)

# After tools run, always go back to the agent to interpret results.
graph.add_edge("tools", "agent")

app = graph.compile()


# -------------------------------------------------------------------
# 7.  Run it
# -------------------------------------------------------------------
if __name__ == "__main__":
    user_msg = "Check on the openclaw container (vmid 100) and mqtt broker (vmid 102). Fix anything broken."

    result = app.invoke({
        "messages": [{"role": "user", "content": user_msg}]
    })

    print("\n=== FINAL ===")
    print(result["messages"][-1].content)

    # To see the full trace, uncomment:
    # for m in result["messages"]:
    #     print(f"\n--- {type(m).__name__} ---")
    #     print(m)
