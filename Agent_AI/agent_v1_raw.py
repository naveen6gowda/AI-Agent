import os, json
from anthropic import Anthropic
from dotenv import load_dotenv
from tools import check_proxmox_status, get_ha_entity, restart_lxc, send_telegram_alert

load_dotenv()
client = Anthropic()

TOOLS = [
    {
        "name": "check_proxmox_status",
        "description": "Get status (running/stopped, memory %, uptime) of a Proxmox LXC or VM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Proxmox node name, e.g. 'pve'"},
                "vmid": {"type": "integer", "description": "VM/LXC ID, e.g. 100"},
            },
            "required": ["node", "vmid"],
        },
    },
    {
        "name": "get_ha_entity",
        "description": "Get current state of a Home Assistant entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "restart_lxc",
        "description": "Restart an LXC container. Use only after confirming the container is unhealthy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "node": {"type": "string"},
                "vmid": {"type": "integer"},
            },
            "required": ["node", "vmid"],
        },
    },
    {
        "name": "send_telegram_alert",
        "description": "Send a Telegram message to the operator.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]

TOOL_FUNCS = {
    "check_proxmox_status": check_proxmox_status,
    "get_ha_entity": get_ha_entity,
    "restart_lxc": restart_lxc,
    "send_telegram_alert": send_telegram_alert,
}

SYSTEM = """You are HomelabSentinel, an SRE agent for Naveen's homelab.
Your job: investigate the user's question, use tools to gather data, decide if action is needed.
Rules:
- Always check status before restarting anything.
- If mem_pct > 85, recommend restart but send a Telegram alert first.
- If a container is stopped, restart it AND alert.
- Explain your reasoning briefly at the end."""

def run_agent(user_msg: str, max_iters: int = 10):
    messages = [{"role": "user", "content": user_msg}]

    for i in range(max_iters):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            # Final text answer
            for block in resp.content:
                if block.type == "text":
                    print(f"\n=== FINAL ===\n{block.text}")
            return

        # Process tool calls
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                print(f"[TOOL CALL] {block.name}({block.input})")
                result = TOOL_FUNCS[block.name](**block.input)
                print(f"[TOOL RESULT] {result}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

    print("Hit max iterations")

if __name__ == "__main__":
    run_agent("Check on the openclaw container (vmid 100) and mqtt broker (vmid 102). Fix anything broken.")