"""
HomelabSentinel — Telegram conversational bot (Phase 4 / Feature #10).

Long-running. Polls Telegram, dispatches each user message to the agent
(with per-chat memory via LangGraph SqliteSaver), routes callback_query
approvals to whichever agent run is waiting on them.

KEY DESIGN — SINGLE POLLER:
  When the bot is running, ONLY the bot polls Telegram. The original
  request_telegram_approval (which has its own getUpdates loop) is
  replaced INSIDE the agent by request_approval_via_bot, which sends
  the inline-keyboard message but waits on a threading.Event the bot
  sets when the callback arrives. This avoids the contention you'd
  get with two pollers fighting for the same callback_query updates.

  Standalone scripts (`smart_monitor.py --alert`, etc.) still work —
  they only SEND messages (one-way) and don't request approval.
  Just don't run `agent_v5_approval.py` at the same time as the bot.

Run:
    uv run python sentinel_bot.py

Authorization:
    Only chat IDs in TELEGRAM_CHAT_ID env var (comma-separated) are
    answered. Other messages are silently ignored.

Persistence:
    Per-chat conversation state lives in bot_checkpoints.sqlite.
    Use /reset in a chat to start a fresh thread (old state remains
    in the DB but is no longer referenced).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

# Import the refactored agent runner. We bring in `run_one` (returns a
# string) and the underlying graph. We do NOT import the old polling
# request_telegram_approval — the bot supplies its own.
from agent_v5_approval import run_one  # noqa: E402
from models import LLMUnavailable as _LLMUnavailable
from models import (
    get_active_model as _get_active_model,
)
from models import (  # noqa: E402
    list_models as _list_models,
)
from models import (
    probe_model as _probe_model,
)
from models import (
    set_active_model as _set_active_model,
)
from tools import _audit  # reuse the audit log helper  # noqa: E402

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_RAW_CHAT_IDS = os.getenv("TELEGRAM_CHAT_ID", "")

if not _TG_TOKEN:
    print("FATAL: TELEGRAM_BOT_TOKEN must be set in .env")
    sys.exit(1)
if not _RAW_CHAT_IDS:
    print("FATAL: TELEGRAM_CHAT_ID must be set in .env (your numeric chat id)")
    sys.exit(1)

_TG_BASE = f"https://api.telegram.org/bot{_TG_TOKEN}"
# Allow multiple authorized chats by comma-separating in env. Defaults
# to single-user (just the operator's chat id).
AUTHORIZED_CHAT_IDS = {c.strip() for c in _RAW_CHAT_IDS.split(",") if c.strip()}

APPROVAL_TIMEOUT_S = int(os.getenv("APPROVAL_TIMEOUT_S", "120"))
POLL_TIMEOUT_S = int(os.getenv("BOT_POLL_TIMEOUT_S", "25"))


# ----------------------------------------------------------------------
# Telegram I/O helpers
# ----------------------------------------------------------------------
def _send_message(chat_id: str, text: str,
                  reply_markup: Optional[dict] = None) -> Optional[int]:
    """Send text. Splits >4000 chars at the last newline to stay under
    Telegram's 4096 limit. Returns the message_id of the last chunk,
    or None on failure."""
    if not text:
        return None
    chunks = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, 4000)
        if split < 200:  # no newline near the cutoff — hard cut
            split = 4000
        chunks.append(text[:split])
        text = text[split:].lstrip()

    last_id = None
    for i, chunk in enumerate(chunks):
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            r = httpx.post(f"{_TG_BASE}/sendMessage", json=payload, timeout=10.0)
            body = r.json()
            if body.get("ok"):
                last_id = body["result"]["message_id"]
                print(f"[bot] sent {len(chunk)} chars → chat {chat_id} "
                      f"(msg_id={last_id})")
            else:
                print(f"[bot] sendMessage NOT OK: {body.get('description')}  "
                      f"chat={chat_id}  payload_len={len(chunk)}")
        except httpx.HTTPError as e:
            print(f"[bot] send failed: {e}")
    return last_id


def _send_chat_action(chat_id: str, action: str = "typing") -> None:
    """Fire-and-forget typing indicator (Telegram shows it for ~5s)."""
    try:
        httpx.post(f"{_TG_BASE}/sendChatAction",
                   json={"chat_id": chat_id, "action": action},
                   timeout=5.0)
    except httpx.HTTPError:
        pass


def _get_updates(offset: Optional[int]) -> list:
    """Long-poll for new updates. Returns the list (possibly empty)."""
    params: Dict[str, Any] = {
        "timeout": POLL_TIMEOUT_S,
        "allowed_updates": '["message", "callback_query"]',
    }
    if offset is not None:
        params["offset"] = offset
    try:
        r = httpx.get(f"{_TG_BASE}/getUpdates", params=params,
                      timeout=POLL_TIMEOUT_S + 10)
        body = r.json()
        if not body.get("ok"):
            print(f"[bot] getUpdates NOT OK: {body.get('description')}  "
                  f"error_code={body.get('error_code')}")
            return []
        result = body.get("result", [])
        if result:
            print(f"[bot] getUpdates returned {len(result)} update(s)  "
                  f"(used offset={offset})")
        return result
    except httpx.HTTPError as e:
        print(f"[bot] poll error: {e}")
        time.sleep(2)
        return []


def _latest_update_id() -> Optional[int]:
    """One-shot peek at the most recent update_id, used to skip history
    when the bot starts."""
    try:
        r = httpx.get(f"{_TG_BASE}/getUpdates",
                      params={"offset": -1, "limit": 1}, timeout=5.0)
        results = r.json().get("result", [])
        return results[0]["update_id"] if results else None
    except Exception:
        return None


def _answer_callback(callback_id: str, text: str = "") -> None:
    try:
        httpx.post(f"{_TG_BASE}/answerCallbackQuery", json={
            "callback_query_id": callback_id, "text": text,
        }, timeout=5.0)
    except httpx.HTTPError:
        pass


def _edit_message(chat_id: str, message_id: int, text: str) -> None:
    try:
        httpx.post(f"{_TG_BASE}/editMessageText", json={
            "chat_id": chat_id, "message_id": message_id, "text": text,
        }, timeout=5.0)
    except httpx.HTTPError:
        pass


# ----------------------------------------------------------------------
# Pending-approvals registry (shared between request_approval_via_bot
# and _handle_callback). Tokens are short uuids included in
# callback_data so the bot can correlate clicks to waiting threads.
# ----------------------------------------------------------------------
_pending: Dict[str, threading.Event] = {}
_results: Dict[str, dict] = {}
_msg_ids: Dict[str, int] = {}      # token → message_id (for edit-on-resolve)
_msg_texts: Dict[str, str] = {}    # token → original text
_lock = threading.Lock()


def request_approval_via_bot(action: str, details: str,
                              timeout_s: Optional[int] = None) -> dict:
    """Replaces request_telegram_approval inside the bot.

    Sends the inline-keyboard prompt, registers a threading.Event under
    a fresh token, blocks until the bot's main loop resolves it (or the
    timeout fires). Same return shape as the polling version so the
    rest of the agent code path doesn't care which one ran.
    """
    timeout = timeout_s if timeout_s is not None else APPROVAL_TIMEOUT_S
    token = uuid.uuid4().hex[:10]

    text = (f"⚠️  APPROVAL NEEDED\n\n"
            f"Action: {action}\n\n"
            f"{details}\n\n"
            f"You have {timeout}s to respond.")
    payload = {
        "chat_id": next(iter(AUTHORIZED_CHAT_IDS)),  # primary operator
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"a:{token}"},
                {"text": "❌ Deny",    "callback_data": f"d:{token}"},
            ]],
        },
    }
    try:
        r = httpx.post(f"{_TG_BASE}/sendMessage", json=payload, timeout=10.0)
        body = r.json()
        if not body.get("ok"):
            return {"decision": "error", "error": str(body.get("description"))}
        message_id = body["result"]["message_id"]
    except httpx.HTTPError as e:
        return {"decision": "error", "error": f"send failed: {e}"}

    event = threading.Event()
    with _lock:
        _pending[token] = event
        _msg_ids[token] = message_id
        _msg_texts[token] = text

    _audit("approval_requested_bot", {"token": token, "action": action,
                                       "message_id": message_id})

    start = time.time()
    fired = event.wait(timeout=timeout)
    elapsed = round(time.time() - start, 1)

    with _lock:
        result = _results.pop(token, None)
        _pending.pop(token, None)
        _msg_ids.pop(token, None)
        _msg_texts.pop(token, None)

    if not fired or result is None:
        _edit_message(payload["chat_id"], message_id,
                      text + f"\n\n→ ⏱ TIMEOUT ({elapsed}s)")
        _audit("approval_decision_bot", {"token": token, "action": action,
                                          "decision": "timeout",
                                          "elapsed_s": elapsed})
        return {"decision": "timeout", "elapsed_s": elapsed}

    result["elapsed_s"] = elapsed
    _audit("approval_decision_bot",
           {**result, "token": token, "action": action})
    return result


# ----------------------------------------------------------------------
# Model picker (/model) — list LM Studio models, switch on tap. The active
# model is global (shared by agent + monitors) and persisted by models.py.
# callback_data is "m:<index>" into this list (Telegram caps callback_data
# at 64 bytes, so we send an index rather than the full model id).
# ----------------------------------------------------------------------
_model_choices: list = []


def _send_model_picker(chat_id: str) -> None:
    global _model_choices
    models = _list_models()
    if not models:
        _send_message(chat_id,
                      "❌ Couldn't list models — the LLM server didn't answer. "
                      "Check LM Studio is up and the API key is right.")
        return
    _model_choices = models
    current = _get_active_model()
    keyboard = [
        [{"text": ("✅ " if m == current else "") + m, "callback_data": f"m:{i}"}]
        for i, m in enumerate(models)
    ]
    _send_message(chat_id,
                  f"Current model: {current}\nPick a model for your next tasks:",
                  reply_markup={"inline_keyboard": keyboard})


def _handle_model_pick(cb: dict, idx_str: str) -> None:
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id"))
    message_id = cb.get("message", {}).get("message_id")
    user = (cb.get("from", {}).get("username")
            or cb.get("from", {}).get("first_name", "?"))
    if chat_id not in AUTHORIZED_CHAT_IDS:
        _answer_callback(cb["id"], "not authorized")
        return
    try:
        model = _model_choices[int(idx_str)]
    except (ValueError, IndexError):
        _answer_callback(cb["id"], "list expired — send /model again")
        return
    # Don't persist blindly: LM Studio lists models this machine can't run
    # (they JIT-load, then every request dies with "Compute error"), and the
    # choice is global — a bad pick bricks the agent AND all monitors. Probe
    # in a worker thread; a cold JIT load is slow and this callback runs on
    # the bot's single polling thread.
    _answer_callback(cb["id"], f"Testing {model} …")
    if message_id:
        _edit_message(chat_id, message_id,
                      f"⏳ Testing model (loads it if cold, may take a "
                      f"minute):\n{model}")

    def _probe_and_set() -> None:
        error = _probe_model(model)
        if error:
            if message_id:
                _edit_message(
                    chat_id, message_id,
                    f"❌ NOT switched — {model} fails on the LLM server:\n"
                    f"{error}\n\nStill on: {_get_active_model()}")
            print(f"[bot] model probe FAILED for {model}: {error}")
            return
        _set_active_model(model)
        if message_id:
            _edit_message(chat_id, message_id,
                          f"✅ Active model set to:\n{model}")
        _audit("model_switch", {"model": model, "by": user})
        print(f"[bot] model switched -> {model} (by @{user})")

    threading.Thread(target=_probe_and_set, daemon=True).start()


def _handle_mcp_approval(cb: dict, payload: str) -> None:
    """Decision tap for a pending MCP-server approval (mcpapp:<rid>:<d>).

    The MCP server cannot poll Telegram — this bot is the single
    getUpdates consumer — so it parks the request and watches
    var/approvals/<rid>.json. We own the button tap; we write the file
    (atomically, so the watcher never reads a half-write). See
    mcp_server.request_operator_approval.
    """
    rid, _, decision = payload.partition(":")
    if decision not in ("approved", "denied") or not rid.isalnum():
        _answer_callback(cb["id"], "malformed MCP callback")
        return
    user = (cb.get("from", {}).get("username") or
            cb.get("from", {}).get("first_name", "?"))
    path = Path(__file__).parent / "var" / "approvals" / f"{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"decision": decision, "by": user, "ts": time.time()}))
    tmp.replace(path)
    _answer_callback(cb["id"], f"{decision} \u2713")
    msg = cb.get("message") or {}
    if msg.get("message_id"):
        icon = "\u2705" if decision == "approved" else "\u26d4"
        _edit_message(str(msg["chat"]["id"]), msg["message_id"],
                      f"{icon} MCP request {rid}: {decision} by {user}")
    _audit("mcp_approval_tap", {"rid": rid, "decision": decision, "by": user})


def _handle_callback(cb: dict) -> None:
    """Main loop calls this when a callback_query update arrives.

    Parses the token from callback_data, looks up the matching pending
    approval, sets its Event with the decision. Edits the original
    message so the chat history is the audit trail.
    """
    data = cb.get("data", "")
    parts = data.split(":", 1)
    if len(parts) != 2:
        _answer_callback(cb["id"], "malformed callback")
        return
    prefix, token = parts
    if prefix == "m":           # model picker, not an approval
        _handle_model_pick(cb, token)
        return
    if prefix == "mcpapp":      # MCP server approval (file IPC)
        _handle_mcp_approval(cb, token)
        return
    decision = "approved" if prefix == "a" else "denied"
    user = (cb.get("from", {}).get("username") or
            cb.get("from", {}).get("first_name", "?"))

    with _lock:
        event = _pending.get(token)
        message_id = _msg_ids.get(token)
        text = _msg_texts.get(token, "")

    if event is None:
        _answer_callback(cb["id"], "expired or unknown")
        return

    _answer_callback(cb["id"], f"Recorded: {decision}")
    marker = "✅ APPROVED" if decision == "approved" else "❌ DENIED"
    _edit_message(next(iter(AUTHORIZED_CHAT_IDS)), message_id,
                  text + f"\n\n→ {marker} by @{user}")

    with _lock:
        _results[token] = {"decision": decision, "by": user,
                            "message_id": message_id}
    event.set()


# ----------------------------------------------------------------------
# Per-chat message dispatch
# ----------------------------------------------------------------------
_chat_locks: Dict[str, threading.Lock] = {}
_chat_sessions: Dict[str, str] = {}  # chat_id → current thread_id

HELP_TEXT = """HomelabSentinel — what I can help with:

Service health:
  • Are all my services reachable?
  • What's the status of <service>?

Disk + backups:
  • How are my disks doing?
  • Are my backups up to date?

Energy:
  • What used the most power today?
  • Energy report for the last week

Home presence + lights:
  • Is anyone home?
  • What lights are on?
  • Suggest devices I could turn off

Homelab docs (runbooks/notes in docs/):
  • How do I restart the bot?
  • Which storage holds backups?
  • What's OPNSense's restart policy?

Destructive actions (always asks approval):
  • Restart <vm>
  • Turn off <light or switch>

Commands:
  /start  — greeting
  /help   — this message
  /reset  — clear conversation memory in this chat
  /model  — list LM Studio models and switch the active one
"""


def _thread_id_for(chat_id: str) -> str:
    return _chat_sessions.get(chat_id, f"chat-{chat_id}")


def _reset_thread(chat_id: str) -> None:
    """Switch this chat to a fresh thread_id. Old state stays in sqlite
    but isn't referenced — cleanup is a future maintenance task."""
    _chat_sessions[chat_id] = f"chat-{chat_id}-{uuid.uuid4().hex[:6]}"


def _typing_loop(chat_id: str, stop: threading.Event) -> None:
    """Background pinger: keeps 'typing...' shown while the agent runs."""
    while not stop.is_set():
        _send_chat_action(chat_id)
        stop.wait(4)


def _run_for_chat(checkpointer, chat_id: str, text: str,
                  lock: threading.Lock) -> None:
    """Worker thread: serialize per-chat agent runs, show typing
    indicator, send the answer back."""
    with lock:
        stop_typing = threading.Event()
        typer = threading.Thread(target=_typing_loop,
                                  args=(chat_id, stop_typing), daemon=True)
        typer.start()

        try:
            answer = run_one(
                user_msg=text,
                thread_id=_thread_id_for(chat_id),
                approval_fn=request_approval_via_bot,
                checkpointer=checkpointer,
            )
        except _LLMUnavailable as e:
            answer = ("⚠️ The local LLM server is unreachable "
                      "(is the Mac awake and LM Studio running?). "
                      "Monitors and alerts keep working — only chat needs "
                      f"the model.\n\ndetail: {e}")
        except Exception as e:
            answer = f"❌ Internal error: {type(e).__name__}: {e}"
            # LM Studio's MLX engine reports a model it can't execute as
            # "Compute error" on the predict stream — that's the model, not
            # the question. Tell the operator the one action that fixes it.
            if "Compute error" in str(e) or "predict stream" in str(e):
                answer += (f"\n\n⚠️ The active model ({_get_active_model()}) "
                           "can't run on the LLM server. Send /model and "
                           "pick a different one.")
            print(f"[bot] agent error in chat {chat_id}: {type(e).__name__}: {e}")
        finally:
            stop_typing.set()

        _send_message(chat_id, answer)


def _handle_message(checkpointer, msg: dict) -> None:
    """Main loop calls this when a `message` update arrives."""
    chat_id = str(msg.get("chat", {}).get("id"))
    text = (msg.get("text") or "").strip()
    from_user = (msg.get("from", {}).get("username")
                  or msg.get("from", {}).get("first_name", "?"))

    print(f"[bot] message  chat_id={chat_id!r}  from=@{from_user}  "
          f"text={text!r}")

    if chat_id not in AUTHORIZED_CHAT_IDS:
        print(f"[bot]   → IGNORED (chat_id not in {AUTHORIZED_CHAT_IDS!r})")
        return
    if not text:
        print("[bot]   → IGNORED (empty text)")
        return

    # Strip @botusername that Telegram appends to commands in groups
    # ("/help@SentinelBot" → "/help"). Harmless for 1:1 chats.
    cmd = text.split("@", 1)[0] if text.startswith("/") else text

    # Slash commands
    if cmd == "/start":
        print("[bot]   → /start")
        _send_message(chat_id,
                      "HomelabSentinel online. Send /help for examples.")
        return
    if cmd == "/help":
        print(f"[bot]   → /help (sending {len(HELP_TEXT)} chars)")
        _send_message(chat_id, HELP_TEXT)
        return
    if cmd == "/reset":
        print("[bot]   → /reset")
        _reset_thread(chat_id)
        _send_message(chat_id, "Conversation memory reset.")
        return
    if cmd == "/model":
        print("[bot]   → /model")
        _send_model_picker(chat_id)
        return

    # Otherwise: send to agent in a worker thread, locked per chat
    print(f"[bot]   → dispatching to agent (thread_id={_thread_id_for(chat_id)})")
    lock = _chat_locks.setdefault(chat_id, threading.Lock())
    threading.Thread(
        target=_run_for_chat,
        args=(checkpointer, chat_id, text, lock),
        daemon=True,
    ).start()


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------
def main() -> int:
    print(f"[bot] starting. Authorized chat_ids: {AUTHORIZED_CHAT_IDS}")
    print(f"[bot] poll timeout = {POLL_TIMEOUT_S}s.  Ctrl+C to stop.")

    # Skip any updates that were already queued before the bot started —
    # don't want to spam answers to messages from yesterday.
    skip_before = _latest_update_id()
    offset = (skip_before + 1) if skip_before is not None else None
    print(f"[bot] starting offset = {offset}")

    # Hello-on-startup so the operator knows the bot is live.
    for chat in AUTHORIZED_CHAT_IDS:
        _send_message(chat, "✅ HomelabSentinel bot online. "
                             "Send /help for examples.")

    with SqliteSaver.from_conn_string("var/bot_checkpoints.sqlite") as checkpointer:
        while True:
            try:
                updates = _get_updates(offset)
            except KeyboardInterrupt:
                break

            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        _handle_message(checkpointer, u["message"])
                    elif "callback_query" in u:
                        _handle_callback(u["callback_query"])
                    else:
                        print(f"[bot] update {u.get('update_id')} had no "
                              f"message or callback_query: keys={list(u.keys())}")
                except Exception as e:
                    print(f"[bot] handler crashed on update "
                          f"{u.get('update_id')}: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()

    print("\n[bot] stopped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[bot] bye.")
        sys.exit(0)
