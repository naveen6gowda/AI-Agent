"""Bot, agent and HTTP-surface hardening from the 2026-09-28 review."""

import httpx
import respx
from fastapi import HTTPException

import agent_v5_approval as agent
import models
import sentinel_bot as bot
import voice_server


def test_reset_survives_a_bot_restart(tmp_path, monkeypatch):
    """The 03:30 maintenance restarts the bot nightly; /reset must stick."""
    monkeypatch.setattr(bot, "_SESSIONS_FILE", tmp_path / "chat_sessions.json")
    monkeypatch.setattr(bot, "_chat_sessions", {})
    bot._reset_thread("42")
    fresh = bot._chat_sessions["42"]
    assert fresh.startswith("chat-42-")
    assert bot._load_sessions() == {"42": fresh}


@respx.mock
def test_non_json_poll_reply_does_not_crash_the_bot(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    respx.get(f"{bot._TG_BASE}/getUpdates").mock(
        return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))
    assert bot._get_updates(None) == []


@respx.mock
def test_not_ok_poll_reply_backs_off(monkeypatch):
    slept = []
    monkeypatch.setattr(bot.time, "sleep", slept.append)
    respx.get(f"{bot._TG_BASE}/getUpdates").mock(return_value=httpx.Response(
        409, json={"ok": False, "error_code": 409, "description": "Conflict"}))
    assert bot._get_updates(5) == [] and slept == [5]


def test_oversized_tool_result_is_capped():
    big = "x" * (agent.MAX_TOOL_RESULT_CHARS + 500)
    out = agent._cap_tool_result(big)
    assert len(out) < len(big) and "truncated: 500 of" in out
    assert agent._cap_tool_result("small") == "small"


def test_voice_token_check(monkeypatch):
    monkeypatch.setattr(voice_server, "TOKEN", "s3cret")
    voice_server._check_auth("Bearer s3cret", None)
    for bad in ("Bearer nope", None, "Basic s3cret"):
        try:
            voice_server._check_auth(bad, None)
        except HTTPException as e:
            assert e.status_code == 401
        else:
            raise AssertionError(f"{bad!r} was accepted")


def test_active_model_write_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "active_model.txt"
    monkeypatch.setattr(models, "_ACTIVE_MODEL_FILE", str(target))
    models.set_active_model("  qwen-x  ")
    assert target.read_text() == "qwen-x"
    assert not (tmp_path / "active_model.txt.tmp").exists()
