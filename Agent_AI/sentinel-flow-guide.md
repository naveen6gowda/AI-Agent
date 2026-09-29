# HomelabSentinel — The Complete Flow Guide

*Written 2026-07-10, after Phase 5 completion. A teacher's walk through the
whole system: what it is, how a message flows, where the gates are, which
node does what — with the exact file and line for every claim.
Companion to the interactive web guide (clickable graph + playable example).*

---

## 1. The 30-second mental model

Sentinel is an **SRE agent for a Proxmox homelab**. It is built from three
layers that deliberately do not trust each other:

- **The hands — tools & monitors.** Plain Python functions that talk to
  Proxmox, Home Assistant, Portainer, the MVG transit API and Firefly III.
  Monitors run on systemd timers **without any LLM** — detection never
  depends on a model being awake.
- **The brain — one local LLM.** A single MLX model on a MacBook (LM Studio,
  OpenAI-compatible API). Every component obtains it from `models.py`, the
  only file allowed to instantiate a model. Zero marginal token cost.
- **The gate — the policy node.** A LangGraph node inspects every tool call
  the LLM proposes. Destructive ones freeze the whole graph with
  `interrupt()` until a human taps Approve or Deny on a Telegram card.
  The default is **deny**.

Three frontends drive the same agent: the **Telegram bot** (conversation and
approval cards), the **Alexa voice bridge** (read-only), and an **MCP
server** so Claude Code / Claude Desktop can be clients too.

The core design insight: **safety is a property of the graph, not of the
prompt.** You can beg a model in its system prompt to "always ask before
restarting things" — one day it won't. Sentinel instead routes every tool
call through a policy node that matches the tool *name* against a
config-defined destructive set. Even a prompt-injected or hallucinating
model cannot reach `restart_lxc` without the graph pausing first.

## 2. The system map

```
   Telegram bot            Alexa voice bridge          systemd timers
 (conversation +          (HA -> Sentinel -> Echo)   (reachability, SMART,
  approval cards)                  |                  backups, energy, docker,
        |                          |                  speedtest, commute)
        +------------+-------------+                          |
                     v                                        v
          +-------------------------+              direct checks + alerts
          |   LangGraph agent (v5)  |              (no LLM in the hot path)
          |  agent -> policy -> tools|
          |         loop            |
          +------------+------------+
                       |
          policy node: destructive tool call?
              -> interrupt() -> Telegram approval card
              -> resume from SQLite checkpoint
                       |
                       v
   ~29 tools: Proxmox API · Home Assistant · Portainer/Docker ·
   MVG commute · Firefly III finance · BM25 RAG over docs/ ·
   Alexa TTS · Telegram · service catalog (catalog.yaml)

   MCP clients (Claude Code/Desktop) --> mcp_server.py :8765
       same registry, same gate, enforced SERVER-side
```

Everything runs on LXC 106 (sentinel.lan). Config comes from `.env`
(secrets, endpoints) and `catalog.yaml` (topology + policy). Runtime state
lives in `var/` (checkpoints, audit log, approvals, active model).

## 3. The agent graph, node by node

The live agent is `agent_v5_approval.py`. Four nodes, one loop, one escape
hatch. Graph state is just `{messages: [...], denied_ids: [...]}`.

```
 START ──► agent ──► tools_condition ──► END        (plain-text answer)
              ▲            │
              │            │ tool calls
              │            ▼
              │         policy ───────► gated tools
              │            │ ▲               │
              │ interrupt()│ │ Command(      │ ToolMessages
              │            ▼ │  resume=...)  │ (results or refusals)
              │      ⚠ operator approval     │
              │        (Telegram card,       │
              │         120 s, default-deny) │
              └──────────────────────────────┘
```

### START — a message arrives (agent_v5_approval.py:571–575)

Every turn begins as fresh state: the user's text plus an empty
`denied_ids` list. `app.invoke()` pushes it into the graph; the SQLite
checkpointer loads any prior conversation for this `thread_id`, so context
carries across turns.

Detail worth knowing: the model has no clock, so every user turn is
prefixed by `_stamped()` (line 501) with the live local time —
`[now: Thursday 2026-07-10 09:12 CEST]`. That is the model's only source of
truth for date/time questions.

### agent — the LLM thinks (agent_v5_approval.py:381–385)

The only node that calls the model:

```
def agent_node(state: AgentState) -> dict:
    history = _cache_tail(_bounded_history(state["messages"]))
    response = _invoke_resilient(_active_llm_with_tools(), [_SYSTEM_MSG] + history)
    _log_usage(response)          # token counts -> var/audit.log
    return {"messages": [response]}
```

- `_bounded_history` (line 330) caps replayed history at 40 messages,
  keeping tool call/result pairs intact, so a long-lived chat can't grow
  per-turn cost without bound.
- `_invoke_resilient` (line 261) is the Phase 5 fallback chain: primary
  call → if the error looks like an LM Studio model reload, wait 6 s and
  retry once → if the connection is dead, try the optional
  `MLX_FALLBACK_*` endpoint → otherwise raise a typed `LLMUnavailable`.
- The response either contains `tool_calls` (the model wants to act) or
  plain text (the final answer).

### tools_condition — answer or act? (agent_v5_approval.py:472–476)

A prebuilt LangGraph router: if the last message carries tool calls, route
onward; otherwise route to END. The crucial edit versus a vanilla ReAct
graph is *where* tool calls go:

```
graph.add_conditional_edges(
    "agent",
    tools_condition,
    {"tools": "policy", END: END},   # tool calls go to policy FIRST
)
```

### policy — the inspection point (agent_v5_approval.py:388–420)

Pure Python, no LLM. It checks each pending tool call's *name* against
`destructive_tools()` from `catalog.py`. Nothing destructive → pass
through. Anything destructive → `interrupt()`: the graph checkpoints and
freezes, surfacing the calls to whoever is driving the graph:

```
destructive = [tc for tc in tool_calls if tc["name"] in DESTRUCTIVE_TOOLS]
if not destructive:
    return {"denied_ids": []}

decisions = interrupt({                  # graph STOPS here
    "kind": "approval_request",
    "destructive_calls": [
        {"tool_call_id": tc["id"], "name": tc["name"], "args": tc["args"]}
        for tc in destructive
    ],
})
# DEFAULT-DENY on resume:
approved_ids = {d.get("tool_call_id") for d in (decisions or [])
                if isinstance(d, dict) and d.get("decision") == "approved"}
denied_ids = [tc["id"] for tc in destructive if tc["id"] not in approved_ids]
```

### ⚠ the human — the Telegram approval card (sentinel_bot.py:212–277)

The interrupt bubbles out of `app.invoke()` as `result["__interrupt__"]`.
The caller loop (`_execute`, agent_v5_approval.py:577–607) formats each
destructive call into a Telegram card with inline Approve/Deny buttons and blocks
on a `threading.Event`. The bot's poll loop receives the tap
(`_handle_callback`, sentinel_bot.py:382), records who decided, edits the
card into an audit line ("-> APPROVED by @operator"), and sets the Event.
The loop then re-invokes the graph:

```
while result.get("__interrupt__"):
    payload = result["__interrupt__"][0].value
    decisions = [ ...one per destructive call, via approval_fn... ]
    result = app.invoke(Command(resume=decisions), config=config)
```

Timeout (120 s, `APPROVAL_TIMEOUT_S`) counts as denied.

### gated tools — execute or refuse (agent_v5_approval.py:423–460)

A replacement for the prebuilt ToolNode that respects `denied_ids`:

```
for tc in tool_calls:
    if tc["id"] in denied_ids:
        out_messages.append(ToolMessage(
            content="REFUSED by operator. Do not retry without new investigation.",
            tool_call_id=tc["id"], name=tc["name"]))
        continue
    tool_fn = _TOOLS_BY_NAME.get(tc["name"])
    try:
        result = tool_fn.invoke(tc["args"])
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    out_messages.append(ToolMessage(content=str(result), ...))
return {"messages": out_messages, "denied_ids": []}
```

Approved and read-only calls dispatch through the registry; denied ids get
a synthetic refusal ToolMessage the LLM will see and reason about on the
next lap; tool exceptions become `{"error": ...}` results instead of
crashes; `denied_ids` is cleared so the next lap starts fresh.

### END — reply to the human

When the model answers in plain text, `run_one()` returns the final
message content to the frontend. The bot chunks it under Telegram's
4096-char limit (sentinel_bot.py:95); voice trims it to ~700 chars for the
Echo (voice.py:75). The whole turn — every lap, every tool call, every
token count — is one Langfuse trace.

One user message can drive many laps around the loop: read a tool, see the
result, read another, then either answer (→ END) or propose an action
(→ gate). A typical "is everything OK?" turn is 2–4 laps.

## 4. A live example — "AdGuard feels flaky — restart it"

The complete flow, both futures.

1. **You message the bot.** The single `getUpdates` poll loop
   (sentinel_bot.py:576) sees the update, checks your chat id against
   `TELEGRAM_CHAT_ID` (anyone else is silently ignored, line 532), and
   dispatches to a per-chat-locked worker thread (line 565) while a
   background thread keeps "typing…" alive.
2. **Into the graph.** `run_one()` drives the compiled graph on thread
   `chat-<your id>`. Your text gets the `[now: ...]` stamp. The agent node
   sends system prompt + history + 29 tool schemas to the local model.
3. **Lap 1 — investigate (no gate).** The prompt demands investigation
   before action, so the model calls `check_docker_container("adguard")` —
   read-only, passes policy untouched, runs via the Portainer API.
   Result: `state=running, health=unhealthy`.
4. **Lap 2 — propose.** Seeing "unhealthy", the model emits a destructive
   call: `restart_docker_container(name="adguard")`. Its prompt also tells
   it AdGuard is the LAN's DNS resolver (agent_v5_approval.py:154), so the
   answer will carry a DNS-blip warning.
5. **The gate.** `policy_node` matches the name against the destructive
   set and calls `interrupt()`. State is checkpointed; the graph freezes.
   Your phone shows:

```
⚠  APPROVAL NEEDED

Action: restart_docker_container(name=adguard)

The agent wants to run:
  restart_docker_container(name=adguard)

Tool-call id: call_9f2c...
You have 120s to respond.

        [ Approve ]      [ Deny ]
```

6a. **You tap Approve.** The tap travels: Telegram → bot poll loop →
    `_handle_callback` matches the token → sets the Event with
    `{decision: "approved", by: "operator"}` → `Command(resume=[...])`
    re-enters the graph → `denied_ids=[]` → the gated tool node POSTs the
    restart to Portainer. The card is edited to "-> APPROVED by @operator".
7a. **Final lap.** The success ToolMessage loops back; the model answers:
    *"Done — I restarted the adguard container. It was unhealthy, so the
    restart was justified. Heads-up: AdGuard is your DNS resolver, so the
    network had a few seconds of DNS blip while it came back."*
    Meanwhile `var/audit.log` gained `approval_requested_bot`,
    `approval_decision_bot` (with who + elapsed seconds) and `llm_usage`
    lines for every lap.

6b. **You tap Deny.** Same path, decision "denied". The call's id lands in
    `denied_ids`; the gated tool node **skips execution** and fabricates
    the refusal ToolMessage. Nothing touched Portainer.
7b. **The model reasons about the refusal.** *"Understood — I won't
    restart it. For the record: adguard is running but its healthcheck
    reports unhealthy. I can re-check later, or you can look at its logs
    in Portainer."* No blind retry — that's partly prompt (lines 99–101)
    and partly structure: a retry would hit the same gate.

**The durability trick:** kill the bot process while the card is pending —
power cut, deploy, crash. The checkpoint was written *before* the
interrupt, so restarting the bot and tapping the same card resumes the
graph on the same `thread_id` from the exact frozen point
(SqliteSaver, `var/bot_checkpoints.sqlite`).

## 5. The safety system — four locks on one door

### Lock 1 — policy is data, and it can only get stricter

Which tools count as destructive lives in `catalog.yaml` under `policy:` —
reviewable, diffable config. The code takes the **union** with a
hard-coded known set, so no config edit, typo or failed YAML load can ever
un-gate a known destructive tool (catalog.py:225–236):

```
KNOWN_DESTRUCTIVE = frozenset(
    {"restart_lxc", "restart_docker_container", "call_ha_service"})

def destructive_tools() -> frozenset:
    try:
        return frozenset(load_catalog().policy.destructive_tools) | KNOWN_DESTRUCTIVE
    except Exception:
        return KNOWN_DESTRUCTIVE      # broken catalog -> strictest gate
```

### Lock 2 — default-deny on anything unclear

A destructive call runs only if its exact `tool_call_id` was explicitly
approved. Empty payload, timeout, malformed dict, unknown id — all denied.
This was not always true: the Phase-2 test suite caught a real hole where
a call missing from a malformed resume payload silently ran; the fix
inverted the logic to build `approved_ids` first
(agent_v5_approval.py:410–420, tested in tests/test_policy_gate.py).

### Lock 3 — denials the model can reason about

A denied call doesn't crash the run; it becomes a ToolMessage the LLM
reads on the next lap. The agent acknowledges, offers alternatives, and
does not retry.

### Lock 4 — tools that can't self-approve

Destructive functions exist in two flavors in `tools.py`: `restart_lxc`
(asks for its own approval — for standalone script use, line 887) and
`restart_lxc_raw` (just does it, line 872). The registry binds the **raw**
variants (registry.py:353–364): gating is the graph's job, which
guarantees exactly one approval prompt, never two.

**The audit trail:** every approval request, decision (who + elapsed),
MCP call, model switch and LLM token count is appended as a JSON line to
`var/audit.log` by `_audit()` (tools.py:47).

## 6. The brain — one model, honestly managed

After the pay-as-you-go Anthropic account ran dry (May 2026), the system
was collapsed onto **one local MLX model** served by LM Studio on a
MacBook. `models.py` is the only file allowed to build a model client;
everything imports `agent_llm()` (tool-calling loops, temp 0, 2048 tokens)
or `helper_llm()` (one-shot summaries, 512 tokens). Repointing the entire
platform is a one-line `.env` change (`MLX_BASE_URL`, `MLX_MODEL`).

- **/model — switch live, but probe first.** The Telegram `/model` command
  lists the server's models as buttons (sentinel_bot.py:289). Before
  persisting a pick, `probe_model()` (models.py:107) forces a real 2-token
  completion: LM Studio happily *lists* models the Mac cannot *run*
  (they load, then every request dies with "Compute error"), and since the
  active model is global — agent AND monitors — a bad pick would brick
  everything. Failures leave the old model active. The choice persists in
  `var/active_model.txt` across restarts.
- **The fallback chain.** `_invoke_resilient` (agent_v5_approval.py:261):
  primary → one patient retry on reload markers → optional
  `MLX_FALLBACK_*` endpoint → typed `LLMUnavailable`.
- **Honest degradation.** The bot catches `LLMUnavailable` and says:
  *"The local LLM server is unreachable (is the Mac awake and LM Studio
  running?). Monitors and alerts keep working — only chat needs the
  model."* (sentinel_bot.py:501). No stack traces at the operator.
- **Token economy.** History capped at 40 messages; per-turn usage
  audited; Anthropic-style prompt-cache breakpoints auto-disabled on the
  local backend where they would be noise (agent_v5_approval.py:300–378).

## 7. Memory — three layers of remembering

1. **Seconds → hours: the checkpoint.** Every node transition writes graph
   state to `var/bot_checkpoints.sqlite` (SqliteSaver). Each Telegram chat
   is a `thread_id` (`chat-<id>`); `/reset` just points the chat at a
   fresh thread.
2. **Days: bounded history.** Only the last 40 messages are replayed to
   the LLM (`_bounded_history`), so cost stays flat.
3. **Weeks → forever: the prune becomes memory.** Nightly at 03:30,
   `checkpoint_maintenance.py` prunes stale threads — but first
   **summarizes each one into `docs/memory/`** (LLM bullets via
   `helper_llm`, or a deterministic digest if the Mac is asleep) and
   re-ingests the RAG index (summarize_thread_to_memory :135, prune :194).
   Conversations outlive their checkpoints: ask "what did we decide about
   X last month?" and `search_docs` finds it.

**RAG — deliberately boring.** `rag.py` is BM25 lexical search over
markdown in `docs/` — chunked by headings, indexed to disk, no embeddings.
Chosen on purpose: Python 3.14 had no torch wheels, homelab queries share
exact vocabulary with the runbooks, and it costs zero tokens. Everything
hides behind `search(query, k)` (rag.py:279), so an embedding backend is a
drop-in swap if it ever earns its complexity.

## 8. Surfaces and monitors

### Telegram — the primary console (sentinel_bot.py)

A long-poll loop that is the **single Telegram consumer** in the whole
system (Telegram allows one `getUpdates` poller per bot token). Messages
from authorized chats dispatch to the agent in per-chat-locked worker
threads — two chats run in parallel, one chat stays ordered. Slash
commands: `/start`, `/help`, `/reset`, `/model`.

### Alexa — voice, deliberately read-only (voice.py + voice_server.py)

```
"Alexa, <phrase>" -> Alexa Routine -> HA automation
    -> POST http://sentinel.lan:8099/voice  {"intent": "status"}
    -> intent runs the actual monitor code (zero LLM calls)
    -> speak_on_alexa() -> Echo says the summary
```

Amazon does the speech-to-text — no Whisper anywhere. Canned intents
(status/backups/energy/disks/presence/docker/commute) call monitor code
directly. The free-form `ask` intent runs the real agent but with an
approval function that always denies (voice.py:150):

```
def _voice_deny(action, details, timeout_s=None):
    return {"decision": "denied", "by": "voice (read-only)"}
```

Hands-free destructive actions are a bad idea — and voice must never fight
the bot for Telegram polling.

### MCP — Sentinel as a platform (mcp_server.py)

The same 29-tool registry served over streamable HTTP (`:8765`, bearer
token) to any MCP client. The gate is enforced **server-side** — a
destructive call from an AI client still sends the Telegram card and
blocks up to 120 s, default-deny (dispatch :115). Because the bot owns
Telegram polling, the tap travels by file IPC: the bot writes
`var/approvals/<rid>.json` atomically (sentinel_bot.py:352); the MCP
server watches for the file (mcp_server.py:98). Resources exposed:
`sentinel://catalog` and the audit-log tail.

### Finance — HA → Firefly III (finance_server.py :8098)

Phone notifications from N26 and Samsung Wallet, relayed by HA, are parsed
(finance_parser.py) and booked into Firefly III (firefly_client.py). No
LLM in the pipeline.

### The monitor fleet — no LLM in the hot path

```
sentinel-bot / -voice / -finance / -mcp     long-running services
sentinel-reachability    every 5 min        endpoint sweep from catalog.yaml
sentinel-docker          every 10 min       container health via Portainer
sentinel-db-train        5 min, Mon-Fri 6-9 S2 + bus 700 commute alerts
sentinel-speedtest       every 4 h          internet speed vs. threshold
sentinel-smart           nightly 02:30      SMART disk scan over SSH
sentinel-backups         daily 09:00        backup freshness per service
sentinel-energy          daily 21:00        HA energy digest (+ tariff)
sentinel-maintenance     daily 03:30        prune -> memory -> RAG re-ingest
```

Every unit carries `OnFailure=sentinel-failure-alert@%n.service` — the
watcher has a watcher: a dying monitor pages Telegram within a minute.
The same monitor functions double as the agent's read tools, so detection
code and diagnosis code are literally the same functions.

## 9. How it got here — five agents, five phases

The `legacy/` folder keeps the learning path (not live):

- v1 `agent_v1_raw.py` — raw Anthropic SDK tool-use loop, no framework
- v2 `agent_v2_langchain.py` — LangChain tool abstractions
- v3 `agent_v3_langgraph.py` — explicit LangGraph state machine
- v4 `agent_v4_langsmith.py` — tracing experiments (LangSmith → Langfuse)
- v5 `agent_v5_approval.py` — **live**: interrupt() gate + checkpointer

Then five hardening phases (docs/ARCHITECTURE.md):

1. **Reliability** (07-05): OnFailure pager on every unit; persistent
   timers; logrotate.
2. **Quality gates** (07-05): pytest + respx + ruff + mypy + CI. The
   policy-gate tests found a real default-allow hole — fixed.
3. **Structure** (07-06): all 29 tools declared once in `registry.py`
   (agent file 1080 → ~650 lines); destructive set moved to
   `catalog.yaml`; state into `var/`.
4. **MCP server** (07-07): same registry over HTTP, gate server-side,
   file-IPC approvals.
5. **Agentic maturity** (07-07): golden-set eval harness (12 cases incl.
   two safety cases; 11/12 with one honest known_fail — the local model
   refuses an explicit operator restart order, tracked as XFAIL for
   cross-model comparison); LLM fallback chain; prune-to-memory.

## 10. File map

```
agent_v5_approval.py   the brain: graph, gate, resume loop, prompt
                       policy_node :388 · gated_tool_node :423
                       wiring :466 · resume loop :577 · run_one :613
sentinel_bot.py        Telegram frontend: single poller, cards, /model
                       card :212 · callback router :382 · main :576
registry.py            all 29 tools declared once   _TOOLS :456
catalog.py / .yaml     typed topology + policy-as-data
                       Policy :94 · destructive_tools() :229
models.py              single-LLM factory  agent_llm :155 · probe :107
tools.py               Proxmox/HA/Telegram + audit  _audit :47
docker_tools.py        Portainer client
mcp_server.py          MCP frontend  approval :61 · dispatch :115
voice.py / voice_server.py  Alexa bridge  _voice_deny :150 · INTENTS :180
rag.py                 BM25 over docs/  _BM25 :198 · search :279
checkpoint_maintenance.py  prune + memory  summarize :135 · prune :194
reachability.py, smart_monitor.py, backup_verifier.py,
speedtest_monitor.py, energy_assistant.py, presence_assistant.py,
db_train_monitor.py    the monitor fleet (timers + agent tools)
finance_server.py, finance_parser.py, firefly_client.py   finance bridge
evals/                 golden set (12 cases) + runner
tests/                 policy-gate + client + unit tests
systemd/               unit files (copied to /etc/systemd/system/)
var/                   checkpoints.sqlite · audit.log · approvals/ ·
                       active_model.txt
docs/                  runbooks + ARCHITECTURE.md + memory/
legacy/                v1–v4 agents (history, not live)
```

## 11. Check yourself

1. **Why does the registry bind `restart_lxc_raw` instead of
   `restart_lxc`?** The non-raw variants request their own Telegram
   approval (for standalone use). In the agent, gating is the graph's job
   — binding raw variants means exactly one prompt, never two, and keeps
   policy out of the tools.
2. **The resume payload arrives empty or malformed. What runs?** Nothing
   destructive. `approved_ids` is built only from well-formed dicts
   explicitly marked "approved"; everything else is denied and gets a
   refusal ToolMessage. Default-deny, with a test asserting it.
3. **The bot dies while an approval card is pending. Then what?** State
   (including the pending interrupt) is already in
   `var/bot_checkpoints.sqlite`. Restart the bot, tap the card — the run
   resumes on the same thread from the exact frozen point.
4. **Why can't the MCP server poll Telegram for its approvals?** Telegram
   allows one getUpdates consumer per token, and sentinel_bot owns it. The
   MCP server only sends the card; the bot writes the tapped decision to
   `var/approvals/<rid>.json`; the server watches the file.
5. **Why does /model run a 2-token completion before switching?**
   LM Studio lists models the Mac can't execute. The active model is
   global (agent + monitors), so persisting a broken pick would brick the
   whole system. The probe forces the server to actually run the model;
   failure leaves the old model active.
6. **Nobody's home. May the agent turn off the heating on its own?** It
   may propose it (the prompt requires `check_presence_state` first) —
   but `call_ha_service` is destructive, so the actual change still
   freezes the graph and waits for a human tap. Suggesting is free;
   acting never is.

---

*Sources: agent_v5_approval.py, sentinel_bot.py, registry.py, catalog.py,
models.py, tools.py, mcp_server.py, voice.py, voice_server.py, rag.py,
checkpoint_maintenance.py, docs/ARCHITECTURE.md, README.md — as of commit
6dd14bb (Phase 5 complete).*
