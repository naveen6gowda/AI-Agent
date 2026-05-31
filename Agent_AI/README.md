# HomelabSentinel — source (`Agent_AI/`)

This folder holds the **complete source** of HomelabSentinel, an agentic-AI SRE
that watches a Proxmox homelab and acts on it through a human-approval gate.

> 📐 For the architecture, diagrams, and the design story, see the
> [repository README](../README.md).
> 📚 For a deep, file-by-file walkthrough, read
> [`sentinel-learning-guide.md`](sentinel-learning-guide.md).

---

## ▶️ Run it

**Prerequisites:** Python **3.14**, [`uv`](https://github.com/astral-sh/uv), and a
homelab to point it at (Proxmox API token, Home Assistant token, a Telegram bot).

```bash
# 1. configure
cp .env.example .env                 # fill in your tokens
cp catalog.example.yaml catalog.yaml # describe your VMs/LXCs

# 2. install
uv sync

# 3a. the production agent (interactive, with approval gate)
uv run python agent_v5_approval.py

# 3b. the always-on Telegram bot
uv run python sentinel_bot.py

# 3c. run a monitor by hand
uv run python reachability.py --json | jq .
uv run python rag.py "how do I restart the bot?"   # zero Claude tokens
```

`systemd/` contains the unit + timer files used for the live 24/7 deployment
(one long-running bot service + five scheduled monitors).

---

## 🗺️ Module map

| Layer | File | What it does |
|------|------|--------------|
| **Agent course** | `agent_v1_raw.py` | ReAct loop with **zero frameworks** — raw Anthropic tool-calling |
| | `agent_v2_langchain.py` | Same task, `@tool` decorator + `create_agent` |
| | `agent_v3_langgraph.py` | The loop rebuilt as an explicit `StateGraph` |
| | `agent_v4_langsmith.py` | v3 + LangSmith tracing (observability) |
| | `agent_v5_approval.py` | **Production brain** — `interrupt()` approval gate, checkpointer, token economy |
| **Front-ends** | `sentinel_bot.py` | Long-running Telegram bot (single-poller + event-based approvals) |
| | `voice_server.py` / `voice.py` | FastAPI Alexa bridge — read-only, zero-token |
| **Foundation** | `models.py` | Two-brain factory: Claude (reason) + local Gemma (summarize) |
| | `catalog.py` | Pydantic-validated loader for `catalog.yaml` (policy as data) |
| | `tools.py` | The integration layer — Proxmox / HA / Telegram + the approval primitive |
| **Monitors** | `reachability.py` | Parallel TCP/HTTP endpoint sweep |
| | `smart_monitor.py` | SMART disk health over SSH (`smartctl`) |
| | `backup_verifier.py` | Backup freshness vs `max_backup_age_h` |
| | `docker_tools.py` | Container health + gated restart via Portainer REST |
| | `presence_assistant.py` | Read-only Home Assistant presence/light/climate |
| | `energy_assistant.py` | Reset-aware energy deltas + tariff costing |
| **Knowledge** | `rag.py` | BM25 lexical RAG over `docs/*.md` (local, no embeddings) |
| **Docs** | `docs/` | Operator runbook, service notes, voice setup (also the RAG corpus) |

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
