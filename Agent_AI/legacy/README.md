# Legacy — the evolution of the Sentinel agent

These files are **not part of the running system**. They are kept as the
learning progression that led to the current agent (`../agent_v5_approval.py`):

| Version | File | What it added |
|---------|------|---------------|
| v1 | `agent_v1_raw.py` | Raw Anthropic SDK tool-use loop, no framework |
| v2 | `agent_v2_langchain.py` | LangChain tool abstractions |
| v3 | `agent_v3_langgraph.py` | Explicit LangGraph state machine |
| v4 | `agent_v4_langsmith.py` | Tracing/observability experiments (LangSmith — since replaced by self-hosted Langfuse) |
| v5 | *(live, in repo root)* | `interrupt()` human-in-the-loop approval gate + SQLite checkpointer |

Their dependencies (`anthropic`, `langchain`, `langsmith`) were removed from
`pyproject.toml`, so these scripts no longer run without reinstalling them.
`_render_pdf.py` generated `sentinel-learning-guide.pdf` from the markdown guide.
