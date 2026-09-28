# HomelabSentinel — source (`Agent_AI/`)

This folder holds the **complete source** of HomelabSentinel, an agentic-AI SRE
that watches a Proxmox homelab and acts on it through a human-approval gate.

> 📐 For the architecture, diagrams, and the design story, see the
> [repository README](../README.md).
> 📚 For a deep, file-by-file walkthrough, read
> [`sentinel-learning-guide.md`](sentinel-learning-guide.md).
> 🗺️ For the assessment + phased roadmap, read
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## ▶️ Run it

**Prerequisites:** Python **3.14**, [`uv`](https://github.com/astral-sh/uv), an
**OpenAI-compatible local LLM server** (e.g. LM Studio with a tool-calling
model), and a homelab to point it at (Proxmox API token, Home Assistant token,
a Telegram bot).

```bash
# 1. configure
cp .env.example .env                 # fill in your tokens + LLM endpoint
cp catalog.example.yaml catalog.yaml # describe your VMs/LXCs

# 2. install
uv sync

# 3a. the production agent (interactive, with approval gate)
uv run python agent_v5_approval.py

# 3b. the always-on Telegram bot
uv run python sentinel_bot.py

# 3c. run a monitor by hand
uv run python reachability.py --json | jq .
uv run python speedtest_monitor.py          # WAN download / upload / latency
uv run python rag.py "how do I restart the bot?"   # offline doc search
uv run python checkpoint_maintenance.py --dry-run  # what would be pruned?

# 4. checks (what CI runs)
uv run ruff check . && uv run pytest -q
uv run python evals/run_evals.py --limit 3   # golden scenarios — needs a live model
```

`systemd/` contains the units used for the live 24/7 deployment: **4
long-running services** (Telegram bot, Alexa voice bridge, finance bridge,
MCP server), **8 timers** (monitors + nightly maintenance), and the
`sentinel-failure-alert@.service` template that pages the operator whenever
any unit fails.

---

## 🗺️ Module map

| Layer | File | What it does |
|------|------|--------------|
| **Brain** | `agent_v5_approval.py` | **Production agent** — LangGraph `interrupt()` approval gate, SQLite checkpointer, Langfuse tracing, resilient LLM call (reload-retry → fallback endpoint → `LLMUnavailable`) |
| | `registry.py` | All 29 `@tool` definitions in one catalog (`TOOLS` / `TOOLS_BY_NAME`), shared by agent, MCP server and tests |
| | `legacy/` | The 5-lesson course that led here (v1 raw loop → v4 tracing) — archived, not live |
| **Front-ends** | `sentinel_bot.py` | Long-running Telegram bot (single-poller + event-based approvals, `/model` switcher) |
| | `voice_server.py` / `voice.py` | FastAPI Alexa bridge — read-only by construction |
| | `mcp_server.py` | MCP over streamable HTTP with bearer auth; destructive calls become Telegram approval cards, default deny on timeout |
| **Life automations** | `db_train_monitor.py` | Commute guard: MVG departures → delay/cancellation alerts on the Echo |
| | `finance_server.py` / `finance_parser.py` / `firefly_client.py` | Bank-app push → HA → parsed → draft transaction in Firefly III |
| **Foundation** | `models.py` | **Single-LLM factory**: one local OpenAI-compatible model for agent + helpers; runtime switch with probe-before-switch |
| | `catalog.py` | Pydantic-validated loader for `catalog.yaml` (policy as data) |
| | `tools.py` | The integration layer — Proxmox / HA / Telegram + the approval primitive |
| **Monitors** | `reachability.py` | Parallel TCP/HTTP endpoint sweep |
| | `smart_monitor.py` | SMART disk health over SSH (`smartctl`) |
| | `speedtest_monitor.py` | Internet speed — download / upload / latency |
| | `backup_verifier.py` | Backup freshness vs `max_backup_age_h` |
| | `docker_tools.py` | Container health + gated restart via Portainer REST |
| | `presence_assistant.py` | Read-only Home Assistant presence/light/climate |
| | `energy_assistant.py` | Reset-aware energy deltas + tariff costing |
| **Reliability** | `checkpoint_maintenance.py` | Nightly checkpoint-DB retention: prune idle threads, cap history, VACUUM |
| | `failure_alert.py` | `OnFailure=` hook → Telegram page with the journal tail; independent of the LLM stack |
| **Knowledge** | `rag.py` | BM25 lexical RAG over `docs/*.md` (local, no embeddings) |
| **Quality** | `tests/` | pytest suite — policy-gate invariants, clients, MCP, eval mechanics, retention |
| | `evals/` | `golden.yaml` scenarios + `run_evals.py`: the real graph against the live model, deny-all interrupts |
| **Docs** | `docs/` | Runbook · service notes · voice setup · **ARCHITECTURE.md** (also the RAG corpus) |

---

## 🔑 Configuration files

| File | Tracked? | Purpose |
|------|----------|---------|
| `.env.example` | ✅ committed | Template — every key documented, no values |
| `.env` | 🚫 gitignored | Your real secrets |
| `catalog.example.yaml` | ✅ committed | Schema + placeholder homelab |
| `catalog.yaml` | 🚫 gitignored | Your real infrastructure inventory |

Nothing with a real credential or your real topology is committed — see the
repo `.gitignore`.
