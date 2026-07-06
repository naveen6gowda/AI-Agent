"""
rag.py — Phase 4b: RAG over homelab docs (#15).

Lexical (BM25) retrieval over markdown / text docs in docs/. NO torch, NO
ONNX, NO model downloads — pure Python, so it runs on this LXC's Python 3.14
with zero wheel-compatibility risk. Two reasons we went lexical here:

  1. The local llama-server (models.py helper_llm) serves a CHAT model only
     — it returns HTTP 501 on /v1/embeddings unless restarted with
     `--embeddings`, which would knock out the chat model the agent + the
     four monitors depend on. Not worth it.
  2. Python 3.14 has no wheels yet for torch / onnxruntime / sentence-
     transformers, so a semantic embedder would mean building from source on
     a RAM-limited unprivileged LXC.

BM25 is a strong fit for homelab docs anyway: queries share vocabulary with
the docs (service names, commands, IPs, config keys). The retriever lives
behind a tiny interface (search()), so the day Python catches up you can swap
in embeddings without touching callers.

Two entry points:
  - search(query, k)  → top-k chunks with source + heading + score. Wired
                        into the agent as the search_docs tool: the model reads
                        the chunks and synthesizes the answer (best quality,
                        composes with the other tools).
  - answer(query)     → retrieval + the LOCAL LLM generation. Fully offline,
                        spends ZERO cloud tokens. Used by the CLI and any
                        future "ask the docs" path that must not cost money.

CLI:
    uv run python rag.py --ingest                  # (re)build the index
    uv run python rag.py "how do I restart the bot?"   # retrieve + the local LLM answer
    uv run python rag.py --search "backup storage"     # retrieval only, no LLM
    uv run python rag.py --json "..."                  # raw structured output

Add docs by dropping .md / .txt files into docs/ and re-running --ingest.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------
# Config (env-overridable so paths can move without code edits)
# ---------------------------------------------------------------------
_HERE = Path(__file__).parent
DOCS_DIR = Path(os.getenv("RAG_DOCS_DIR", str(_HERE / "docs")))
INDEX_PATH = Path(os.getenv("RAG_INDEX_PATH", str(_HERE / "rag_index.json")))

DOC_GLOBS = ("*.md", "*.markdown", "*.txt", "*.rst")
CHUNK_CHARS = 900               # target chunk size before starting a new chunk
DEFAULT_K = 4
MAX_CHUNK_RETURN_CHARS = 1200   # truncate per-chunk text handed back (token economy)

# Okapi BM25 parameters (standard defaults).
_K1 = 1.5
_B = 0.75

# Tiny stopword set — drops question filler ("how do I…") so the signal terms
# dominate. BM25's idf handles the rest, so we keep this deliberately small.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in", "on", "for",
    "with", "how", "do", "does", "i", "my", "it", "this", "that", "you", "your",
    "at", "be", "as", "if", "can", "what", "when", "where", "which", "me",
}

_TOKEN_RE = re.compile(r"[a-z0-9_.]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase word/number tokens, keeping internal dots so IPs and
    dotted entity ids ('router.lan', 'sensor.refrigerator_total_energy')
    survive as single tokens. Trailing/leading dots are stripped."""
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        tok = raw.strip(".")
        if len(tok) > 1 and tok not in _STOPWORDS:
            out.append(tok)
    return out


# ---------------------------------------------------------------------
# Chunking — markdown-aware: group paragraphs under their heading, pack to
# ~CHUNK_CHARS. The nearest heading becomes a citation breadcrumb.
# ---------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _chunk_text(text: str, source: str) -> List[Dict[str, Any]]:
    # 1) split into (heading, paragraph) pairs
    paras: List[tuple] = []
    heading = ""
    buf: List[str] = []

    def _flush_para() -> None:
        nonlocal buf
        para = "\n".join(buf).strip()
        if para:
            paras.append((heading, para))
        buf = []

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            _flush_para()
            heading = m.group(2).strip()
        elif line.strip() == "":
            _flush_para()
        else:
            buf.append(line)
    _flush_para()

    # 2) hard-split any paragraph that's much larger than a chunk
    expanded: List[tuple] = []
    for h, para in paras:
        if len(para) > CHUNK_CHARS * 1.5:
            for i in range(0, len(para), CHUNK_CHARS):
                expanded.append((h, para[i:i + CHUNK_CHARS]))
        else:
            expanded.append((h, para))

    # 3) pack paragraphs into chunks up to CHUNK_CHARS, not crossing headings
    chunks: List[Dict[str, Any]] = []
    cur: List[str] = []
    cur_len = 0
    cur_heading = ""

    def _flush_chunk() -> None:
        nonlocal cur, cur_len
        body = "\n\n".join(cur).strip()
        if body:
            chunks.append({"source": source, "heading": cur_heading, "text": body})
        cur = []
        cur_len = 0

    for h, para in expanded:
        if not cur:
            cur_heading = h
        elif h != cur_heading or cur_len + len(para) > CHUNK_CHARS:
            _flush_chunk()
            cur_heading = h
        cur.append(para)
        cur_len += len(para) + 2
    _flush_chunk()
    return chunks


# ---------------------------------------------------------------------
# Ingestion — read docs/, chunk, persist to a human-readable JSON index
# ---------------------------------------------------------------------
def ingest(docs_dir: Path = DOCS_DIR, index_path: Path = INDEX_PATH) -> Dict[str, Any]:
    files: List[Path] = []
    if docs_dir.exists():
        for pattern in DOC_GLOBS:
            files.extend(docs_dir.rglob(pattern))
    files = sorted({f for f in files if f.is_file()})

    chunks: List[Dict[str, Any]] = []
    used_files: List[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = str(f.relative_to(docs_dir))
        file_chunks = _chunk_text(text, rel)
        for c in file_chunks:
            c["id"] = len(chunks)
            chunks.append(c)
        if file_chunks:
            used_files.append(rel)

    index = {
        "version": 1,
        "docs_dir": str(docs_dir),
        "chunk_count": len(chunks),
        "files": used_files,
        "chunks": chunks,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    # bust the in-process cache so a search right after --ingest sees new data
    _CACHE["mtime"] = None
    _CACHE["bm25"] = None
    return index


# ---------------------------------------------------------------------
# BM25 retriever (in-memory, rebuilt from the JSON index, mtime-cached)
# ---------------------------------------------------------------------
class _BM25:
    def __init__(self, chunks: List[Dict[str, Any]]):
        self.chunks = chunks
        # index the body + heading + source so a service name in the filename
        # or heading still matches.
        self.tokens = [
            _tokenize(f"{c.get('text','')} {c.get('heading','')} {c.get('source','')}")
            for c in chunks
        ]
        self.N = len(self.tokens)
        self.dl = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0

        df: Dict[str, int] = {}
        self.tf: List[Dict[str, int]] = []
        for toks in self.tokens:
            freq: Dict[str, int] = {}
            for t in toks:
                freq[t] = freq.get(t, 0) + 1
            self.tf.append(freq)
            for term in freq:
                df[term] = df.get(term, 0) + 1
        self.df = df

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        # BM25 idf with +1 inside the log so it never goes negative.
        return math.log((self.N - n + 0.5) / (n + 0.5) + 1.0)

    def search(self, query: str, k: int) -> List[Dict[str, Any]]:
        q = _tokenize(query)
        if not q or self.N == 0:
            return []
        scored: List[tuple] = []
        for i in range(self.N):
            freq = self.tf[i]
            dl = self.dl[i]
            score = 0.0
            for term in q:
                f = freq.get(term, 0)
                if not f:
                    continue
                denom = f + _K1 * (1 - _B + _B * dl / (self.avgdl or 1.0))
                score += self._idf(term) * (f * (_K1 + 1)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, i in scored[:k]:
            c = self.chunks[i]
            out.append({
                "source": c.get("source", ""),
                "heading": c.get("heading", ""),
                "score": round(score, 3),
                "text": c.get("text", ""),
            })
        return out


_CACHE: Dict[str, Any] = {"mtime": None, "bm25": None}


def _load_index() -> "_BM25 | None":
    if not INDEX_PATH.exists():
        return None
    mtime = INDEX_PATH.stat().st_mtime
    if _CACHE["bm25"] is not None and _CACHE["mtime"] == mtime:
        return _CACHE["bm25"]
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    bm25 = _BM25(data.get("chunks", []))
    _CACHE["mtime"] = mtime
    _CACHE["bm25"] = bm25
    return bm25


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------
def search(query: str, k: int = DEFAULT_K) -> Dict[str, Any]:
    """Retrieve the top-k doc chunks for a query. No LLM involved."""
    bm25 = _load_index()
    if bm25 is None:
        return {"query": query, "count": 0, "chunks": [],
                "error": "no index — add docs to docs/ then run "
                         "`uv run python rag.py --ingest`"}
    if bm25.N == 0:
        return {"query": query, "count": 0, "chunks": [],
                "note": "index is empty — add .md files to docs/ and re-ingest"}
    hits = bm25.search(query, max(1, k))
    for h in hits:
        if len(h["text"]) > MAX_CHUNK_RETURN_CHARS:
            h["text"] = h["text"][:MAX_CHUNK_RETURN_CHARS] + " …[truncated]"
    return {"query": query, "count": len(hits), "chunks": hits}


def answer(query: str, k: int = DEFAULT_K) -> Dict[str, Any]:
    """Retrieve + generate an answer with the LOCAL LLM (zero cloud tokens).

    Falls back to returning the top chunk verbatim if the llama-server is
    unreachable, so the docs are still useful when the local LLM is down."""
    res = search(query, k=k)
    chunks = res.get("chunks", [])
    sources = [{"source": c["source"], "heading": c["heading"]} for c in chunks]

    if not chunks:
        return {"query": query, "answer": res.get("error") or res.get("note")
                or "No relevant docs found.", "sources": [], "chunks": []}

    context = "\n\n".join(
        f"[{i + 1}] (source: {c['source']}"
        + (f" › {c['heading']}" if c["heading"] else "") + f")\n{c['text']}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "You are answering a question about the operator's homelab using ONLY the "
        "context below. If the answer is not in the context, say you don't "
        "know — do not invent details. Be concise (a few sentences). Cite the "
        "sources you used by their [n] tag.\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {query}\n\nAnswer:"
    )

    def _fallback(reason: str) -> Dict[str, Any]:
        top = chunks[0]
        return {"query": query,
                "answer": f"[{reason}] Closest match — {top['source']}"
                          + (f" › {top['heading']}" if top["heading"] else "")
                          + f":\n{top['text']}",
                "sources": sources, "chunks": chunks}

    try:
        from models import helper_llm  # local import: keep search() usable if models fails
        llm = helper_llm(temperature=0.2, max_tokens=300)
        resp = llm.invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = text.strip()
        if not text:
            return _fallback("helper_llm returned empty")
        return {"query": query, "answer": text, "sources": sources, "chunks": chunks}
    except Exception as e:
        return _fallback(f"helper_llm unavailable: {e}")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel RAG over docs/")
    parser.add_argument("query", nargs="*", help="the question to ask")
    parser.add_argument("--ingest", action="store_true",
                        help="(re)build the index from docs/ and exit")
    parser.add_argument("--search", action="store_true",
                        help="retrieval only — print matching chunks, no LLM")
    parser.add_argument("-k", type=int, default=DEFAULT_K,
                        help=f"number of chunks to retrieve (default {DEFAULT_K})")
    parser.add_argument("--json", action="store_true",
                        help="emit raw structured JSON")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.ingest:
        idx = ingest()
        print(f"Ingested {idx['chunk_count']} chunk(s) from "
              f"{len(idx['files'])} file(s) in {DOCS_DIR}")
        for f in idx["files"]:
            print(f"  - {f}")
        if not idx["files"]:
            print(f"  (no docs found — drop .md/.txt files into {DOCS_DIR})")
        return 0

    query = " ".join(args.query).strip()
    if not query:
        parser.error("provide a query, or use --ingest to build the index")

    if args.search:
        res = search(query, k=args.k)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0
        if res.get("error") or res.get("note"):
            print(res.get("error") or res.get("note"))
            return 0
        print(f"\nTop {res['count']} match(es) for: {query!r}\n")
        for i, c in enumerate(res["chunks"], 1):
            loc = c["source"] + (f" › {c['heading']}" if c["heading"] else "")
            print(f"[{i}] score={c['score']}  {loc}")
            print("    " + c["text"].replace("\n", "\n    ")[:600])
            print()
        return 0

    res = answer(query, k=args.k)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    print(res["answer"])
    if res.get("sources"):
        seen = set()
        print("\nSources:")
        for s in res["sources"]:
            key = (s["source"], s["heading"])
            if key in seen:
                continue
            seen.add(key)
            print(f"  - {s['source']}" + (f" › {s['heading']}" if s["heading"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
