# Homelab docs (RAG corpus)

Drop any homelab notes, runbooks, or wiki pages in here as `.md` / `.txt`
files. After adding or editing files, rebuild the search index:

```bash
cd /opt/sentinel
uv run python rag.py --ingest
```

Then the agent can answer "how do I…" / config / policy questions about the
homelab from these docs (via the `search_docs` tool), and you can query them
directly from the CLI:

```bash
uv run python rag.py "how do I restart the bot?"     # Gemma-generated answer + sources
uv run python rag.py --search "backup storage"       # raw matching chunks, no LLM
```

## How retrieval works

Search is **BM25 lexical** retrieval — pure Python, fully local, no embedding
model. It matches on shared vocabulary (service names, commands, IPs, config
keys), which is what homelab questions are made of. Generation runs on the
local Gemma helper (`models.helper_llm`), so querying the docs costs **zero**
Claude tokens. The index lives in `rag_index.json` and is rebuilt by
`--ingest`; it is safe to delete (just re-ingest).

Keep headings meaningful — they're used as citation breadcrumbs and are part
of what gets matched.
