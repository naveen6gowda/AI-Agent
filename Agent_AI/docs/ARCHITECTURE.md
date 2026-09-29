# Architecture Assessment & Roadmap

*Written 2026-07-05, when Sentinel was designated the main agentic-AI platform
of the homelab. This is the honest engineering review: what the current
architecture gets right, where it will hurt as the system grows, and the
phased plan to production quality.*

## 1. Verdict

**The foundations are sound — no rewrite is needed.** The four core patterns
are exactly what you would choose again for a long-lived agent platform:

1. **Graph-with-a-policy-gate** (agent → policy → gated tools): safety is a
   *structural* property of the graph, not a prompt instruction. Denials
   surface back to the LLM as tool messages it can reason about.
2. **Checkpointer-backed interrupts**: approvals survive process restarts;
   state is durable by construction.
3. **Single-model factory** (`models.py`): the whole system repoints to a new
   LLM with one env change; probe-before-switch prevents bricking.
4. **Declarative topology** (`catalog.yaml` with criticality + restart
   policy): the agent's world model is data, reviewable and diffable.

What needs work is not the architecture but its **packaging**: flat module
layout, a 37 KB `tools.py`, policy defined in code instead of data, no tests,
and a single point of failure on the Mac LLM. All fixable incrementally —
that is the roadmap below.

## 2. Current state (post-cleanup, 2026-07-05)

```
interfaces   sentinel_bot.py · voice_server.py · finance_server.py
agent        agent_v5_approval.py  (graph, policy gate, prompts, Langfuse)
tools/integr tools.py (Proxmox/HA/Telegram) · docker_tools.py (Portainer)
             + monitor modules doubling as tool backends
knowledge    catalog.py/yaml · rag.py (BM25 over docs/)
model        models.py → LM Studio @ llm.lan (single LLM)
state        bot_checkpoints.sqlite · checkpoints.sqlite · audit.log ·
             transit_state.json · active_model.txt (all in repo root, gitignored)
scheduling   17 systemd units (3 long-running, 8 timers + maintenance)
```

Done in Phase 0 (2026-07-05): git repository with full history; dead code and
`.bak` files removed; v1–v4 archived in `legacy/`; LangSmith fully removed
(Langfuse is the single observability stack); stale Anthropic/llama.cpp
config stripped; checkpoint retention (`checkpoint_maintenance.py`, nightly
03:30) after the DB hit 274 MB; LXC disk 8 G → 16 G.

## 3. Known weaknesses (ranked by how much they will hurt)

| # | Weakness | Why it matters as the "main agent" |
|---|----------|-------------------------------------|
| 1 | **No tests, no CI** | Every refactor below is risky until this exists. Blocks everything. |
| 2 | **Mac LLM is a SPOF** | Laptop asleep/away ⇒ the whole brain is down. Monitors survive (by design), but agent/voice/bot die. |
| 3 | **Monitors fail silently** | `Failed to start` on network blips, nobody alerted. The watcher needs a watcher. |
| 4 | **Policy lives in code** | The destructive-tool set is a Python constant in the agent file; it belongs next to `restart_policy` in `catalog.yaml`. |
| 5 | **Flat layout, monolith tools.py** | 30+ root modules; adding integration #10 makes it worse. Needs `src/sentinel/` packaging. |
| 6 | **Tool wrappers duplicated** | Tools are re-wrapped inside the agent file; an MCP server would need a third copy. Needs one registry with metadata. |
| 7 | **Everything runs as root** | One prompt-injected or buggy tool call away from the container; cheap to harden. |
| 8 | **State files scattered in repo root** | sqlite/json/log mixed with code; should live in `var/`. |

## 4. Phased roadmap

Each phase is independently shippable and leaves the system running.

### Phase 1 — Reliability (the watcher gets a watcher) — ✅ shipped 2026-07-05
- `sentinel-failure-alert@.service`: sends Telegram on any unit failure;
  add `OnFailure=sentinel-failure-alert@%n.service` to every sentinel unit.
- `Persistent=true` on the docker/speedtest/reachability timers (missed runs
  fire after outages).
- Fix `systemd-networkd-wait-online` timeouts in the LXC (it waits on an
  interface it does not manage).
- `logrotate` for `audit.log`; journald size cap.
- **Done when:** killing any monitor produces a Telegram alert within a minute.

### Phase 2 — Quality gates (unlocks all refactoring) — ✅ shipped 2026-07-05
- `pytest` + `respx`: unit tests for Proxmox/HA/Portainer clients (mocked
  HTTP) and — critically — **policy-gate tests**: a destructive tool call
  always interrupts; a denied call never executes; a denial produces a
  refusal ToolMessage.
- `ruff` + `mypy`; GitHub **private** repo; Actions CI (uv, lint, tests).
- **Done when:** CI is green and the policy invariants are executable.
- **Outcome:** 23 tests; the default-deny suite caught a real hole (a
  destructive call missing from a malformed resume payload was treated as
  approved) — fixed in policy_node. ruff clean; mypy advisory (30-error
  baseline to burn down); stale two-brain wording removed from live code.

### Phase 3 — Structure (policy as data, one tool registry) — ✅ core shipped 2026-07-06
- `src/sentinel/` package: `integrations/` (proxmox, ha, portainer, telegram,
  mvg, firefly), `monitors/`, `agent/`, `channels/`, `registry.py`.
- **Tool registry**: every tool declared once with metadata
  (`destructive: bool`, criticality hints, docs). The agent graph *and* the
  future MCP server consume the same registry.
- Move the destructive set into `catalog.yaml` (`policy:` block) — approval
  rules become reviewable data, like `restart_policy` already is.
- Runtime state → `var/`; config via `pydantic-settings`.
- **Done when:** `tools.py` is gone and the agent file contains only graph logic.
- **Outcome:** approval policy lives in catalog.yaml (union with the
  built-in set — config can only tighten the gate); all 29 tools are
  declared once in registry.py (agent file 1080 -> 650 lines, graph
  logic only); runtime state moved to var/. DEFERRED by choice: the
  src/sentinel package split — with the registry in place it is
  cosmetic at this scale, and Phase 4 (MCP) does not depend on it.
  Revisit if the module count keeps growing.

### Phase 4 — MCP server (Sentinel becomes a platform) — ✅ shipped 2026-07-07
- `sentinel-mcp`: FastMCP, **streamable HTTP + bearer token** on the LXC, so
  Claude Code/Desktop (and other agents) become additional frontends.
- Tools come from the Phase-3 registry. **Destructive MCP tools still route
  through the Telegram approval gate — policy is enforced server-side**, so
  no client, human or AI, bypasses it.
- Resources: `sentinel://catalog`, latest monitor reports, audit tail.
- Optional second, public repo: a standalone MVG transit MCP (no secrets).
- **Done when:** Claude on the Mac can triage the homelab, and a restart
  request from it still lands as an approval card on the phone.
- **Outcome:** mcp_server.py — low-level MCP Server bridging all 29
  registry tools (schemas derived from the LangChain tools), stateless
  streamable HTTP + bearer auth on :8765, systemd-managed with the
  OnFailure pager. Destructive calls block on a Telegram approval card,
  default-deny on timeout. Because the bot is Telegram's single
  getUpdates consumer, the tap travels bot → var/approvals/<id>.json →
  server (atomic file IPC). 10 gate tests mirror the in-process suite;
  resources: sentinel://catalog, sentinel://audit-log.

### Phase 5 — Agentic maturity (the interview differentiators) — ✅ shipped 2026-07-07
- **Eval harness**: golden set of ~30 prompts with expected tool-call
  sequences; runs in CI against the local model; results tracked in Langfuse.
  Model swaps become measurable regressions, not vibes.
- **LLM fallback chain** in `models.py`: LM Studio → (optional) cloud API →
  degraded "monitors-only" mode with an honest Telegram notice.
- **Long-term memory**: before the nightly prune deletes old checkpoints,
  summarize them into `docs/memory/` — the RAG index becomes the agent's
  durable memory.
- ~~Prometheus textfile metrics from monitors + a Grafana dashboard~~ —
  skipped by operator choice (2026-07-07).
- **Outcome:** evals/ golden set (12 cases incl. two safety cases) drives
  the real graph with deny-all interrupts; first run: 11/12 on
  qwen3.6-35b-a3b@iq3_s in ~5 min. The failure is a real finding — the
  model refuses an explicit operator restart order for a healthy-looking
  container through two prompt iterations; tracked as known_fail (XFAIL)
  for cross-model comparison in Langfuse. LLM fallback chain:
  primary → reload-retry → optional MLX_FALLBACK_* endpoint → typed
  LLMUnavailable with an honest bot message. Long-term memory: the
  nightly prune summarizes threads into docs/memory/ (LLM bullets, or a
  deterministic digest when the LLM host sleeps) and re-ingests the RAG
  index — conversations outlive their checkpoints.

## 5. What this demonstrates (interview mapping)

| Competency | Where it shows |
|------------|----------------|
| Agent safety / HITL | policy gate, interrupts, audit log, policy-as-data |
| LLMOps | Langfuse traces, eval harness, probe-before-switch, fallback chain |
| Platform thinking | one tool registry → LangGraph + MCP + Telegram frontends |
| Ops discipline | systemd fleet, OnFailure alerting, retention jobs, runbooks |
| Cost engineering | local-first single LLM, zero marginal token cost |
| Honest tradeoffs | BM25-vs-embeddings decision, single-model constraint |

## 6. Review 2026-09-28 — state after the roadmap

**Verdict unchanged: the four core patterns hold.** Twelve weeks on, the
graph-with-a-policy-gate, the checkpointer, the single-model factory and the
declarative catalog have all survived real use without a redesign. What the
review found was in the layer the roadmap never covered: **how monitors turn
findings into messages.**

Current shape: 32 tools in one registry (agent + MCP), 4 long-running
services, 11 timers, a haproxy LLM front door on the LXC (Mac LAN primary,
Tailscale backup), 125 tests (the public mirror omits one private exporter).

What was wrong, and what changed:

| Finding | Evidence | Change |
|---------|----------|--------|
| Monitors alerted on **every run** while a problem lasted | 2026-09-02 outage: a reachability alert every 5 min + 12 "Docker check broke" pages | `alert_state.py`: one message per transition (down / recovered, 6 h reminder); reachability needs 2 failed probes |
| `high` criticality **never alerted** from reachability or backups, contradicting the catalog's own definition | Debian13 (DNS, Vaultwarden, Immich) and Hermes are `high` | reachability + backups page `high`; silent delivery 23:00–07:00 |
| Nothing watched **VM/LXC state** — `sentinel-guests` was never installed | a stopped Hermes would have gone unnoticed | timer installed, transition alerts |
| Portainer unreachable paged as **"monitor broke"** | 09-02 | now a finding about the host ("Debian13 down?"); container incidents are kept, not closed |
| Unreadable backup storage reported as **"backups missing"** | code path | now an error (check blind → pager) |
| `restart_policy: never` lived **only in the prompt** | one mistaken tap could reboot OPNSense | enforced inside the restart tools |
| Tool results were **unbounded** | `list_ha_entities` takes a model-chosen `limit` | capped at 24k chars with a "narrow the query" note |
| `/reset` **undone nightly** (sessions in memory, bot restarts 03:30) | code path | sessions persisted in `var/` |
| docs the RAG answers from were **stale** (Claude/Gemma, destroyed VMs) | `docs/operations-runbook.md` | rewritten |

Still open (operator decisions, not bugs):

- **Services run as root** (roadmap weakness #7). Cheap next step:
  `ProtectSystem=strict` + `ReadWritePaths=/opt/sentinel/var` on the
  monitor units; a dedicated user needs the Proxmox SSH key and
  `systemctl` access for maintenance.
- **Backups live on the host they protect** (`general-storage` on the
  Proxmox box) — an offsite copy is the one data-loss risk left.
- **`restart_policy: auto`** is not implemented (everything asks). Fine as
  long as nobody expects it to work.
- **A bot restart mid-approval** leaves that tool call unanswered in the
  thread (never observed in the checkpoint DB; the approval times out
  normally in every other case).
