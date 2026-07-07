"""
HomelabSentinel v5 — LangGraph interrupt() + checkpointer approval gate.

What v5 adds over v3:

  1. A `policy_node` that inspects pending tool_calls. If any call is
     destructive, it `interrupt()`s — pausing the graph and surfacing
     the request to the CALLER (this file's run()).

  2. A `gated_tool_node` (replaces the prebuilt ToolNode) that
     dispatches tool calls but skips any whose id is in `denied_ids`,
     emitting a synthetic refusal ToolMessage instead. This means the
     LLM sees the refusal on its next turn and can reason about it.

  3. A SqliteSaver checkpointer persists graph state across the
     interrupt. You can kill the process during a pending approval,
     restart later, and resume the same thread_id where it left off
     (state lives in checkpoints.sqlite next to this file).

  ┌─────────┐    ┌─────────┐         ┌────────┐      ┌────────────┐
  │  START  │──► │  agent  │────────►│ policy │─────►│  gated_    │
  └─────────┘    └─────────┘         └────┬───┘      │  tools     │
                     ▲                    │          └────┬───────┘
                     │             destructive?           │
                     │                    ▼               │
                     │              [interrupt()  ◄──── caller
                     │                  resumes  ◄──── feeds decisions]
                     └────────────────────┴──────────────┘

The destructive tools are imported from tools.py as their *_raw
variants, which do NOT call request_telegram_approval themselves —
that's the gate's job, not the tool's. Avoids double-prompting.
"""

import os
import sys
import time
from datetime import datetime
from typing import Annotated, List, TypedDict
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, ToolMessage, trim_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import tools_condition
from langgraph.types import Command, interrupt

from catalog import KNOWN_DESTRUCTIVE, destructive_tools
from models import agent_llm, agent_provider
from models import get_active_model as _get_active_model
from registry import _TOOLS, _TOOLS_BY_NAME
from tools import _audit, request_telegram_approval

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

# --- Langfuse tracing via @observe (optional; safe no-op if unavailable) ---
import os as _os

try:
    from langfuse.decorators import langfuse_context as _lf_ctx
    from langfuse.decorators import observe as _observe
    _LF_ON = bool(_os.getenv("LANGFUSE_PUBLIC_KEY") and _os.getenv("LANGFUSE_SECRET_KEY"))
except Exception:
    _LF_ON = False
    _lf_ctx = None
    def _observe(*_a, **_k):
        if _a and len(_a) == 1 and callable(_a[0]) and not _k:
            return _a[0]
        def _decorate(_fn):
            return _fn
        return _decorate
print("[agent] Langfuse @observe tracing:", "on" if _LF_ON else "off")


# Tools live in registry.py (Phase 3) — this file is graph logic only.

# Which tools require approval comes from catalog.yaml (policy block) —
# safety policy is data, reviewed like any other config. The fallback is
# the full known destructive set, so a missing/broken catalog can only
# make the gate STRICTER, never more permissive.
# (accessor lives in catalog.py so the MCP server shares the exact same gate)
_FALLBACK_DESTRUCTIVE = KNOWN_DESTRUCTIVE

DESTRUCTIVE_TOOLS = destructive_tools()


SYSTEM = """You are HomelabSentinel, an SRE agent for the operator's homelab.

Every user message starts with a "[now: ...]" stamp — the live local
time (Europe/Berlin) at the moment the message arrived. That stamp is
the ONLY source of truth for the current date and time; never answer
date/time questions from your internal knowledge, which is stale.

Investigate the user's question with read tools first, then call a
destructive tool only when justified. The runtime will pause and ask
the operator for approval before any destructive call actually runs;
if denied, you will see a refusal ToolMessage — reason about it and
respond, do not blindly retry.

Each tool's own description (provided to you with its schema) explains its
arguments and when to use it — don't expect a tool list here. Read tools are
safe to call freely; restart_lxc and call_ha_service are DESTRUCTIVE and are
gated by operator approval; send_telegram_alert is informational.

Discovery rules (in order of preference):
1. CHECK THE CATALOG FIRST. get_service_catalog tells you what the
   operator considers important and what their restart policies are.
   Trust the catalog's criticality and restart_policy fields.
2. For "is everything OK?" questions, call check_reachability FIRST —
   it probes every catalogued endpoint in parallel and tells you which
   are down in one tool call. Only drill into check_proxmox_status /
   get_ha_entity for services that came back down or degraded.
3. If a service has restart_policy="never", DO NOT call restart_lxc —
   alert via send_telegram_alert instead.
4. If you need live inventory (a new VM not in the catalog),
   list_proxmox_nodes + list_proxmox_guests.
5. For procedural / config / policy / "how do I …" questions about the
   homelab itself (runbooks, conventions, network layout, retention), call
   search_docs — it retrieves from the operator's own notes in docs/. Cite
   the source file in your answer. It costs no cloud tokens to call.
6. NEVER ask the user for a value you can discover with a tool.

Memory-pressure rules (READ CAREFULLY):
- Look at mem_pct_source on every check_proxmox_status result:
  - 'guest'         → real reading from inside the guest. Trust it.
  - 'host_cgroup'   → accurate for LXC. Trust it.
  - 'host_balloon'  → Proxmox's view of a QEMU VM. UNRELIABLE — it
                      counts page cache. Healthy VMs look 85%+ used.
- If mem_pct_source is 'host_balloon' OR mem_pct_warning is present,
  call get_guest_mem_pct(vmid) to get a real reading before making any
  restart decision. If guest exec still fails, do NOT recommend restart
  based on memory alone — alert the operator instead and ask them to
  enable qemu-guest-agent on that VM.

Decision rules:
- ALWAYS check status before recommending a destructive change.
- If mem_pct > 85 AND mem_pct_source in ('guest','host_cgroup')
  AND restart_policy != "never", recommend restart_lxc.
- If a container/VM is stopped AND it's critical/high AND
  restart_policy != "never", restart it AND alert.
- Explain your reasoning briefly at the end.

Docker rules (Debian13 host, via Portainer):
- list_docker_containers / check_docker_container are read-only — use them to
  confirm a container is exited or unhealthy BEFORE proposing a restart.
- restart_docker_container is DESTRUCTIVE and gated. Restart only a container
  that is exited/unhealthy or that the user explicitly named, one at a time;
  don't restart a healthy running container.
- AdGuard runs here and is the LAN's DNS resolver — warn that restarting it
  briefly interrupts DNS for the whole network.

Home automation rules (for call_ha_service decisions):
- BEFORE proposing a climate / light / switch change, call
  check_presence_state. Match the change to who is home:
    - presence == "away" → it's safe to suggest turning off
      HVAC, lights, etc.
    - presence == "home" or "mixed" → only act if the user
      explicitly asked. Don't silently turn things off on people.
- Use check_climate_state / check_light_state to know the CURRENT
  state. Never propose an action that's already in effect (don't
  "turn off" a light that's already off).
- When proposing a setpoint change, prefer a small adjustment over
  a big jump (e.g. lower target_temp by 2°C, not by 8°C).
- call_ha_service is gated — the operator sees a Telegram approval
  prompt for each proposed action. Bundle one logical change per
  call; don't fire 10 service calls in parallel.
"""


# -------------------------------------------------------------------
# 2.  Graph state
# -------------------------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    # IDs of tool_calls the operator denied in the most recent policy
    # pass. The gated tool node skips these and emits a refusal.
    denied_ids: List[str]


# -------------------------------------------------------------------
# 3.  Nodes
# -------------------------------------------------------------------
llm = agent_llm()
llm_with_tools = llm.bind_tools(_TOOLS)

# The operator can switch models live via the Telegram /model command
# (persisted by models.set_active_model). Rebuild the tool-bound client only
# when the active model actually changes, so normal turns pay nothing.
_ACTIVE = {"model": _get_active_model(), "bound": llm_with_tools}


def _active_llm_with_tools():
    current = _get_active_model()
    if current != _ACTIVE["model"] or _ACTIVE["bound"] is None:
        _ACTIVE["model"] = current
        _ACTIVE["bound"] = agent_llm(model=current).bind_tools(_TOOLS)
        print(f"[agent] active model -> {current}")
    return _ACTIVE["bound"]


# LM Studio JIT-loads the selected model; right after a /model switch it may
# still hold the previous (large) model in RAM and crash loading the new one
# ("The model has crashed ... Exit code: null", surfaced as a 400). The crash
# evicts the old model, so the *next* request reloads cleanly — retry once.
# This node only asks the LLM for the next message (no tool side effects), so
# re-invoking is safe.
_RELOAD_MARKERS = ("model has crashed", "model loading", "failed to load",
                   "exit code", "no models loaded")


def _usage_details(response) -> dict:
    um = getattr(response, "usage_metadata", None) or {}
    usage = {}
    input_tokens = um.get("input_tokens")
    output_tokens = um.get("output_tokens")
    total_tokens = um.get("total_tokens")
    if isinstance(input_tokens, int):
        usage["input"] = input_tokens
    if isinstance(output_tokens, int):
        usage["output"] = output_tokens
    if isinstance(total_tokens, int):
        usage["total"] = total_tokens
    elif "input" in usage or "output" in usage:
        usage["total"] = usage.get("input", 0) + usage.get("output", 0)
    return usage


def _record_llm_observation(response, model: str) -> None:
    if not _LF_ON or _lf_ctx is None:
        return
    usage_details = _usage_details(response)
    usage = ({**usage_details, "unit": "TOKENS"} if usage_details else None)
    try:
        _lf_ctx.update_current_observation(
            name="agent-llm",
            model=model,
            model_parameters={
                "provider": agent_provider(),
                "temperature": "0.0",
                "max_tokens": 2048,
            },
            usage=usage,
            usage_details=usage_details or None,
            metadata={
                "active_model": model,
                "provider": agent_provider(),
            },
            output=getattr(response, "content", None),
        )
    except Exception as e:
        print(f"[agent] Langfuse observation update failed: {type(e).__name__}: {e}")


@_observe(name="agent-llm", as_type="generation",
          capture_input=False, capture_output=False)
def _invoke_resilient(bound, messages):
    model = _ACTIVE.get("model") or _get_active_model()
    try:
        response = bound.invoke(messages)
    except Exception as e:
        if any(m in str(e).lower() for m in _RELOAD_MARKERS):
            print(f"[agent] LLM transient ({type(e).__name__}); "
                  f"waiting for model reload, retrying once")
            time.sleep(6)
            response = bound.invoke(messages)
        else:
            raise
    _record_llm_observation(response, model)
    return response

# --- Token economy -------------------------------------------------------
# The tools + system block is byte-identical on every call, so we mark it
# with one Anthropic prompt-cache breakpoint: re-reads cost ~10% of normal
# input tokens, and during a multi-tool investigation the whole prefix is
# served from cache on every inner-loop iteration. A second breakpoint on
# the conversation tail lets each iteration (and each follow-up turn) reuse
# the prior messages from cache too.
#
# NB: for the *direct* Anthropic API the `cache_control` invoke kwarg is a
# no-op — langchain only expands it for Bedrock/Vertex — so we place both
# breakpoints directly on message content blocks ourselves.
# Anthropic prompt-caching only helps (and is only understood) on the
# Anthropic API. On a local MLX/OpenAI-compatible server the cache_control
# blocks are meaningless and the list-of-blocks shape is just overhead, so
# fall back to a plain-string system message there.
_CACHE = agent_provider() == "anthropic"
if _CACHE:
    _SYSTEM_MSG = SystemMessage(content=[
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ])
else:
    _SYSTEM_MSG = SystemMessage(content=SYSTEM)

# Cap replayed history so a long-lived Telegram chat doesn't grow the
# per-message token cost without bound. We count messages (not tokens) for a
# cheap, deterministic window, and start_on="human" keeps tool_call /
# tool_result pairs intact so we never send an orphaned tool result.
MAX_HISTORY_MESSAGES = int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "40"))


def _bounded_history(messages: list) -> list:
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    return trim_messages(
        messages,
        token_counter=len,
        max_tokens=MAX_HISTORY_MESSAGES,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )


def _cache_tail(messages: list) -> list:
    """Put an ephemeral cache breakpoint on the last message so the whole
    conversation prefix is reusable. Touches only a fresh copy of the last
    message (state is left intact) and only when its content is a plain
    string — always the case here, since the tail is a HumanMessage or a
    ToolMessage. langchain hoists the breakpoint to the tool_result block.

    No-op on non-Anthropic backends (the local MLX server ignores
    cache_control and we don't want to reshape its message content)."""
    if not _CACHE or not messages:
        return messages
    last = messages[-1]
    if not isinstance(last.content, str):
        return messages
    tail = last.model_copy(update={"content": [{
        "type": "text",
        "text": last.content,
        "cache_control": {"type": "ephemeral"},
    }]})
    return messages[:-1] + [tail]


def _log_usage(response) -> None:
    """Record token usage (incl. cache hits) so the savings are visible:
    `tail -f audit.log | jq 'select(.event=="llm_usage")'`."""
    um = getattr(response, "usage_metadata", None)
    if not um:
        return
    details = um.get("input_token_details") or {}
    _audit("llm_usage", {
        "input": um.get("input_tokens"),
        "output": um.get("output_tokens"),
        "cache_read": details.get("cache_read"),
        "cache_creation": details.get("cache_creation"),
    })


def agent_node(state: AgentState) -> dict:
    history = _cache_tail(_bounded_history(state["messages"]))
    response = _invoke_resilient(_active_llm_with_tools(), [_SYSTEM_MSG] + history)
    _log_usage(response)
    return {"messages": [response]}


def policy_node(state: AgentState) -> dict:
    """Pause before destructive tool_calls; resume with operator decisions.

    Resume value (from Command(resume=...)) must be a list of dicts:
        [{"tool_call_id": "...", "decision": "approved"|"denied"|"timeout",
          "by": "<who>"}]
    """
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    destructive = [tc for tc in tool_calls if tc["name"] in DESTRUCTIVE_TOOLS]

    if not destructive:
        return {"denied_ids": []}

    decisions = interrupt({
        "kind": "approval_request",
        "destructive_calls": [
            {"tool_call_id": tc["id"], "name": tc["name"], "args": tc["args"]}
            for tc in destructive
        ],
    })

    # DEFAULT-DENY: a destructive call runs only if its id was EXPLICITLY
    # approved. Anything else — empty payload, timeout, unknown/missing
    # ids, malformed dicts — leaves it denied. (A test-suite catch: the
    # old code derived denials from the decision list, so a call absent
    # from a malformed payload would silently run.)
    approved_ids = {
        d.get("tool_call_id") for d in (decisions or [])
        if isinstance(d, dict) and d.get("decision") == "approved"
    }
    denied_ids = [tc["id"] for tc in destructive if tc["id"] not in approved_ids]
    return {"denied_ids": denied_ids}


def gated_tool_node(state: AgentState) -> dict:
    """Replacement for prebuilt ToolNode that respects state['denied_ids'].

    Executes approved tool_calls normally; emits a synthetic refusal
    ToolMessage for any tool_call_id in denied_ids.
    """
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    denied_ids = set(state.get("denied_ids", []) or [])

    out_messages = []
    for tc in tool_calls:
        if tc["id"] in denied_ids:
            out_messages.append(ToolMessage(
                content="REFUSED by operator. Do not retry without new investigation.",
                tool_call_id=tc["id"],
                name=tc["name"],
            ))
            continue
        tool_fn = _TOOLS_BY_NAME.get(tc["name"])
        if tool_fn is None:
            out_messages.append(ToolMessage(
                content=f"unknown tool: {tc['name']}",
                tool_call_id=tc["id"], name=tc["name"],
            ))
            continue
        try:
            result = tool_fn.invoke(tc["args"])
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        out_messages.append(ToolMessage(
            content=str(result),
            tool_call_id=tc["id"],
            name=tc["name"],
        ))

    # Clear denied_ids so the NEXT iteration's policy_node starts fresh.
    return {"messages": out_messages, "denied_ids": []}


# -------------------------------------------------------------------
# 4.  Build graph
# -------------------------------------------------------------------
graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("policy", policy_node)
graph.add_node("tools", gated_tool_node)

graph.add_edge(START, "agent")
graph.add_conditional_edges(
    "agent",
    tools_condition,
    {"tools": "policy", END: END},
)
graph.add_edge("policy", "tools")
graph.add_edge("tools", "agent")


# -------------------------------------------------------------------
# 5.  Caller loop — handles interrupts, resolves via Telegram, resumes
# -------------------------------------------------------------------
# Compiling the graph isn't free, and the bot drives it on every inbound
# message with the SAME long-lived checkpointer. Cache the compiled app keyed
# by checkpointer identity so the bot compiles once for its lifetime. The
# transient CLI checkpointer (a fresh SqliteSaver per call) passes reuse=False,
# so we never hold a strong ref to a checkpointer that's about to be closed.
_BOT_APP: dict = {"checkpointer": None, "app": None}


# The model has no clock: nothing in the prompt carried the current time,
# so it answered time questions from training data. Stamp each user turn
# with the live local time instead. The stamp rides on the user message —
# the conversation tail changes every turn anyway — so the byte-identical
# system+tools cache prefix is untouched. The LXC clock is inherited from
# the NTP-synced Proxmox host, so no network call (HA or otherwise) needed.
_AGENT_TZ = ZoneInfo(os.getenv("AGENT_TZ", "Europe/Berlin"))


def _stamped(user_msg: str) -> str:
    now = datetime.now(_AGENT_TZ)
    return f"[now: {now.strftime('%A %Y-%m-%d %H:%M %Z')}]\n{user_msg}"

def _trace_surface(thread_id: str) -> str:
    tid = str(thread_id)
    if tid.startswith("chat-"):
        return "telegram"
    if tid.startswith("voice-"):
        return "voice"
    return "cli"


def _trace_metadata(thread_id: str) -> dict:
    active_model = _get_active_model()
    provider = agent_provider()
    return {
        "thread_id": str(thread_id),
        "surface": _trace_surface(thread_id),
        "provider": provider,
        "active_model": active_model,
        "model": active_model,
    }


def _trace_tags(meta: dict) -> list:
    return [
        "sentinel",
        f"surface:{meta['surface']}",
        f"provider:{meta['provider']}",
        f"model:{meta['active_model']}",
    ]


def _langfuse_callbacks() -> list:
    """Return Langfuse callbacks only when the optional integration is present."""
    if not _LF_ON or _lf_ctx is None:
        return []
    try:
        import langchain.callbacks.base  # noqa: F401
    except Exception:
        return []
    try:
        handler = _lf_ctx.get_current_langchain_handler()
    except Exception as e:
        print(f"[agent] Langfuse callback unavailable: {type(e).__name__}: {e}")
        return []
    return [handler] if handler is not None else []


def _execute(user_msg, thread_id, approval_fn, checkpointer, verbose, reuse=False):
    """Inner loop. Drives the compiled graph through any number of
    interrupt/resume cycles and returns the final assistant message."""
    if reuse:
        if _BOT_APP["checkpointer"] is not checkpointer:
            _BOT_APP["checkpointer"] = checkpointer
            _BOT_APP["app"] = graph.compile(checkpointer=checkpointer)
        app = _BOT_APP["app"]
    else:
        app = graph.compile(checkpointer=checkpointer)
    meta = _trace_metadata(thread_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": meta,
        "tags": _trace_tags(meta),
    }
    callbacks = _langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks

    state = {
        "messages": [{"role": "user", "content": _stamped(user_msg)}],
        "denied_ids": [],
    }
    result = app.invoke(state, config=config)

    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        if verbose:
            print(f"\n--- INTERRUPT: {payload['kind']} ---")

        decisions = []
        for call in payload["destructive_calls"]:
            pretty_args = ", ".join(
                f"{k}={v}" for k, v in call["args"].items()
            )
            d = approval_fn(
                action=f"{call['name']}({pretty_args})",
                details=(
                    f"The agent wants to run:\n"
                    f"  {call['name']}({pretty_args})\n\n"
                    f"Tool-call id: {call['tool_call_id']}"
                ),
            )
            decisions.append({
                "tool_call_id": call["tool_call_id"],
                "name": call["name"],
                "decision": d["decision"],
                "by": d.get("by"),
            })
            if verbose:
                print(
                    f"  {call['name']}: {d['decision']} "
                    f"by {d.get('by') or '-'} in {d.get('elapsed_s')}s"
                )

        result = app.invoke(Command(resume=decisions), config=config)

    return result["messages"][-1].content


@_observe(name="sentinel-agent", capture_input=False)
def run_one(user_msg: str, thread_id: str = "default",
            approval_fn=None, checkpointer=None, verbose: bool = False) -> str:
    """Run the agent for ONE user message. Returns the final answer string.

    Designed for the Telegram bot, which wants:
      - Its own approval_fn (Event-based; the bot's single poller resolves it)
      - A long-lived checkpointer opened ONCE for the bot's lifetime, so
        per-chat thread_ids share the same sqlite file

    CLI use: leave defaults — opens a fresh SqliteSaver for this call and
    uses the polling-based request_telegram_approval. Same behavior as
    the old run() body.
    """
    if _LF_ON and _lf_ctx is not None:
        try:
            meta = _trace_metadata(thread_id)
            _lf_ctx.update_current_trace(
                name="sentinel-agent",
                session_id=thread_id,
                input=user_msg,
                metadata=meta,
                tags=_trace_tags(meta),
            )
        except Exception:
            pass
    if approval_fn is None:
        approval_fn = request_telegram_approval

    if checkpointer is None:
        with SqliteSaver.from_conn_string("var/checkpoints.sqlite") as cp:
            return _execute(user_msg, thread_id, approval_fn, cp, verbose)
    return _execute(user_msg, thread_id, approval_fn, checkpointer, verbose,
                    reuse=True)


def run(user_msg: str, thread_id: str = "default") -> None:
    """CLI wrapper — preserves the old print-the-answer behavior."""
    answer = run_one(user_msg=user_msg, thread_id=thread_id, verbose=True)
    print("\n=== FINAL ===")
    print(answer)


if __name__ == "__main__":
    # Real-inventory demo. Adjust as you like.
    user_msg = (
        "Read the service catalog first. Then check the health of every "
        "service whose criticality is 'critical' or 'high'. Recommend "
        "(but don't execute) any restart that looks justified, respecting "
        "each service's restart_policy. Summarize findings at the end."
        "Is anyone home right now? Summarize the household state in one paragraph."
        "List every light that's currently on and tell me whether anyone is in that room (use occupancy sensors)."
    )
    # Fresh thread per run so we don't replay old conversations.
    import uuid
    run(user_msg, thread_id=f"run-{uuid.uuid4().hex[:8]}")
