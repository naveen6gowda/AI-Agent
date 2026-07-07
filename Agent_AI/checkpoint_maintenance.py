#!/usr/bin/env python3
"""
checkpoint_maintenance.py — prune LangGraph checkpoint databases.

Why this exists (2026-07-05): every agent turn appends a full state
snapshot to the checkpointer. The Telegram bot's default thread id is
stable (`chat-<chat_id>`), so its history grows forever, and /reset
spawns orphan threads that are never referenced again after a bot
restart. Unpruned, bot_checkpoints.sqlite reached 274 MB and pushed
the LXC root disk to 94%.

Retention policy (all configurable via flags):
  1. Threads whose NEWEST checkpoint is older than --keep-days are
     deleted entirely (covers orphaned /reset threads).
  2. Surviving threads keep only the newest --keep-per-thread rows per
     checkpoint namespace. LangGraph only needs the latest checkpoint
     to resume a thread — older rows are time-travel history we never
     use.
  3. Orphaned rows in `writes` are removed.
  4. WAL truncate + VACUUM returns the space to the filesystem.
     VACUUM needs exclusive access, so --stop-bot stops sentinel-bot
     around the prune and always restarts it (try/finally).

Run manually:
    uv run python checkpoint_maintenance.py --dry-run
    uv run python checkpoint_maintenance.py --stop-bot
Scheduled by systemd/sentinel-maintenance.timer (daily 03:30).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

DEFAULT_DBS = ["var/bot_checkpoints.sqlite", "var/checkpoints.sqlite"]
BOT_UNIT = "sentinel-bot.service"


def log(msg: str) -> None:
    print(msg, flush=True)


def size_mb(db: str) -> float:
    return sum(
        os.path.getsize(db + sfx)
        for sfx in ("", "-shm", "-wal")
        if os.path.exists(db + sfx)
    ) / 1_048_576


def bot_is_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", BOT_UNIT])
    return r.returncode == 0


def systemctl(action: str) -> None:
    subprocess.run(["systemctl", action, BOT_UNIT], check=True)


def stale_threads(db: str, cutoff: datetime) -> list[str]:
    """Thread ids whose newest checkpoint is older than the cutoff.

    Uses the official SqliteSaver reader so the msgpack checkpoint blob
    (which carries the "ts" ISO timestamp) is decoded for us. A thread
    whose timestamp cannot be read is treated as fresh — never delete
    on uncertainty.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    stale: list[str] = []
    with SqliteSaver.from_conn_string(db) as saver:
        rows = saver.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ).fetchall()
        for (thread_id,) in rows:
            tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            ts_raw = (tup.checkpoint or {}).get("ts") if tup else None
            try:
                ts = datetime.fromisoformat(ts_raw)
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                stale.append(thread_id)
    return stale


MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "docs", "memory")


def _thread_transcript(saver, thread_id: str):
    """(condensed transcript, tool names) for a thread, or None if it
    holds no human conversation worth remembering."""
    tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    msgs = ((tup.checkpoint or {}).get("channel_values") or {}).get("messages") \
        if tup else None
    if not msgs:
        return None
    lines, tools_used = [], set()
    for m in msgs:
        for tc in (getattr(m, "tool_calls", None) or []):
            tools_used.add(tc.get("name", "?"))
        mtype = getattr(m, "type", "")
        content = str(getattr(m, "content", "")).strip()
        if mtype in ("human", "ai") and content:
            lines.append(f"{mtype}: {content[:400]}")
    if not any(ln.startswith("human:") for ln in lines):
        return None
    return "\n".join(lines[-60:]), sorted(tools_used)


def _llm_summary(transcript: str):
    """5–8 durable bullet points via the local helper LLM, or None.
    The nightly run happens at 03:30 — the LLM host may well be asleep,
    so failure here is EXPECTED and must never block the prune."""
    try:
        from models import helper_llm
        resp = helper_llm(max_tokens=400).invoke(
            "Summarize this operator/agent conversation as 5-8 short "
            "bullet points. Keep only durable facts, decisions, and "
            "outcomes — drop greetings and dead ends.\n\n" + transcript[-8000:])
        text = str(getattr(resp, "content", "")).strip()
        return text or None
    except Exception:
        return None


def summarize_thread_to_memory(saver, thread_id: str,
                               memory_dir: str | None = None):
    """Write a memory note for a thread about to be deleted.

    LLM summary when the model is reachable, else a deterministic
    digest — the prune must never lose a conversation silently just
    because the LLM host is asleep. Returns the path or None."""
    memory_dir = memory_dir or MEMORY_DIR
    data = _thread_transcript(saver, thread_id)
    if data is None:
        return None
    transcript, tools_used = data
    summary = _llm_summary(transcript)
    if summary is None:
        humans = [ln for ln in transcript.splitlines() if ln.startswith("human:")]
        ais = [ln for ln in transcript.splitlines() if ln.startswith("ai:")]
        summary = ("(deterministic digest — LLM unreachable at prune time)\n"
                   f"- first request: {humans[0][7:] if humans else '?'}\n"
                   f"- last request: {humans[-1][7:] if humans else '?'}\n"
                   f"- last answer: {ais[-1][4:] if ais else '?'}")
    os.makedirs(memory_dir, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(memory_dir, f"{day}-{thread_id[:40]}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Conversation memory — {thread_id}\n\n"
                f"Archived {day} by checkpoint retention, before pruning.\n"
                f"Tools used: {', '.join(tools_used) or 'none'}\n\n"
                f"{summary}\n")
    log(f"[memory] {thread_id} -> {path}")
    return path


def cleanup_approvals(max_age_s: float = 86400.0) -> int:
    """Sweep stale var/approvals decision files (MCP requests that
    timed out before the operator tapped)."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "var", "approvals")
    removed = 0
    if os.path.isdir(base):
        cutoff = time.time() - max_age_s
        for name in os.listdir(base):
            fp = os.path.join(base, name)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.unlink(fp)
                    removed += 1
            except OSError:
                continue
    return removed


def delete_threads(db: str, threads: list[str]) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(db) as saver:
        for thread_id in threads:
            saver.delete_thread(thread_id)


def prune(db: str, keep_days: int, keep_per_thread: int, dry_run: bool,
          memory: bool = True) -> None:
    before = size_mb(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

    conn = sqlite3.connect(db, timeout=30)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "checkpoints" not in tables:
            log(f"[{db}] no checkpoints table — skipping")
            return
        n_rows, n_threads = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT thread_id) FROM checkpoints"
        ).fetchone()
        log(f"[{db}] {before:.1f} MB, {n_rows} checkpoints in {n_threads} threads")
    finally:
        conn.close()

    # 1. whole threads idle past the cutoff
    stale = stale_threads(db, cutoff)
    log(f"[{db}] threads idle > {keep_days}d: {len(stale)}")
    if stale and not dry_run:
        if memory:
            from langgraph.checkpoint.sqlite import SqliteSaver
            with SqliteSaver.from_conn_string(db) as saver:
                for tid in stale:
                    try:
                        summarize_thread_to_memory(saver, tid)
                    except Exception as e:
                        log(f"[memory] skipped {tid}: {type(e).__name__}: {e}")
        delete_threads(db, stale)

    # 2. cap history of surviving threads + 3. orphaned writes + 4. vacuum
    conn = sqlite3.connect(db, timeout=30)
    try:
        capped = 0
        if dry_run:
            for _thread_id, _ns, n in conn.execute(
                "SELECT thread_id, checkpoint_ns, COUNT(*) FROM checkpoints"
                " GROUP BY thread_id, checkpoint_ns"
            ).fetchall():
                capped += max(0, n - keep_per_thread)
            log(f"[{db}] dry-run: would delete {capped} rows over the "
                f"per-thread cap of {keep_per_thread}; stopping here")
            return
        for thread_id, ns in conn.execute(
            "SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints"
        ).fetchall():
            cur = conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ?"
                " AND checkpoint_id NOT IN ("
                "   SELECT checkpoint_id FROM checkpoints"
                "   WHERE thread_id = ? AND checkpoint_ns = ?"
                "   ORDER BY checkpoint_id DESC LIMIT ?)",
                (thread_id, ns, thread_id, ns, keep_per_thread),
            )
            capped += cur.rowcount
        if "writes" in tables:
            orphans = conn.execute(
                "DELETE FROM writes WHERE (thread_id, checkpoint_ns, checkpoint_id)"
                " NOT IN (SELECT thread_id, checkpoint_ns, checkpoint_id"
                "         FROM checkpoints)"
            ).rowcount
        else:
            orphans = 0
        conn.commit()
        log(f"[{db}] deleted {capped} over-cap checkpoints, {orphans} orphaned writes")

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()

    after = size_mb(db)
    log(f"[{db}] {before:.1f} MB -> {after:.1f} MB (freed {before - after:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", action="append",
                    help=f"database file (repeatable; default: {DEFAULT_DBS})")
    ap.add_argument("--keep-days", type=int, default=14,
                    help="delete threads idle longer than this (default 14)")
    ap.add_argument("--keep-per-thread", type=int, default=40,
                    help="newest checkpoints kept per thread (default 40)")
    ap.add_argument("--stop-bot", action="store_true",
                    help="stop sentinel-bot during the prune (needed for VACUUM)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; delete nothing")
    ap.add_argument("--no-memory", action="store_true",
                    help="skip writing docs/memory notes for pruned threads")
    args = ap.parse_args()

    dbs = args.db or [d for d in DEFAULT_DBS if os.path.exists(d)]
    restart_bot = False
    failed = False
    try:
        if args.stop_bot and not args.dry_run and bot_is_active():
            log(f"stopping {BOT_UNIT} for exclusive DB access")
            systemctl("stop")
            restart_bot = True
        for db in dbs:
            try:
                prune(db, args.keep_days, args.keep_per_thread, args.dry_run,
                      memory=not args.no_memory)
            except sqlite3.OperationalError as e:
                failed = True
                log(f"[{db}] ERROR: {e} (bot still running? use --stop-bot)")
    finally:
        if restart_bot:
            log(f"restarting {BOT_UNIT}")
            systemctl("start")
    if not args.dry_run:
        removed = cleanup_approvals()
        if removed:
            log(f"[approvals] swept {removed} stale decision files")
        if not args.no_memory:
            try:
                from rag import ingest
                stats = ingest()
                log(f"[memory] RAG re-ingested: {stats}")
            except Exception as e:
                log(f"[memory] RAG ingest skipped: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
