"""
LangChain port of agent_v1_raw.py.

Same behavior, same tools, same system prompt — but the manual
while-loop and tool-dispatch logic are replaced by LangChain's
create_agent (which returns a LangGraph runnable under the hood).

This file is written for LangChain >= 1.0 (the unified langchain +
langgraph API). In 1.x, AgentExecutor was retired in favor of
`langchain.agents.create_agent`.

Install:
    pip install "langchain>=1.0" langchain-anthropic
"""

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langchain.agents import create_agent

from tools import (
    check_proxmox_status as _check_proxmox_status,
    get_ha_entity as _get_ha_entity,
    restart_lxc as _restart_lxc,
    send_telegram_alert as _send_telegram_alert,
)

load_dotenv()


# -------------------------------------------------------------------
# 1.  Wrap plain Python functions as LangChain "Tools"
# -------------------------------------------------------------------
# @tool reads the function name, type hints, and docstring and
# auto-generates the JSON schema sent to Claude. No manual TOOLS list.

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
# 2.  System prompt (identical to v1)
# -------------------------------------------------------------------
SYSTEM = """You are HomelabSentinel, an SRE agent for Naveen's homelab.
Your job: investigate the user's question, use tools to gather data, decide if action is needed and also take approval from user via telegram alert before taking any action.
Rules:
- Always check status before restarting anything.
- If mem_pct > 85, recommend restart but send a Telegram alert first.
- If a container is stopped, restart it AND alert.
- Explain your reasoning briefly at the end."""


# -------------------------------------------------------------------
# 3.  Build the agent
# -------------------------------------------------------------------
# create_agent returns a CompiledStateGraph (LangGraph) that internally
# runs: model_node -> tool_node -> model_node -> ... until the model
# stops requesting tools. Same ReAct loop as v1, just precompiled.
llm = ChatAnthropic(model="claude-sonnet-4-6", max_tokens=2048)

agent = create_agent(
    model=llm,
    tools=tools,
    system_prompt=SYSTEM,
)


# -------------------------------------------------------------------
# 4.  Run it
# -------------------------------------------------------------------
if __name__ == "__main__":
    user_msg = "Check on the openclaw container (vmid 100) and mqtt broker (vmid 102). Fix anything broken."

    # The agent expects a LangGraph state dict: {"messages": [...]}
    result = agent.invoke({
        "messages": [{"role": "user", "content": user_msg}]
    })

    # result["messages"] contains the full conversation:
    #   HumanMessage -> AIMessage(tool_calls) -> ToolMessage -> ... -> AIMessage(final)
    # The last message is Claude's final answer.
    final = result["messages"][-1]
    print("\n=== FINAL ===")
    print(final.content)

    # Uncomment to inspect the full trace:
    # for m in result["messages"]:
    #     print(f"\n--- {type(m).__name__} ---")
    #     print(m)
