"""
voice.py — Phase 4c: Voice (#8), Alexa-native core.

The operator talks to Alexa exactly like they already do for lights
("Alexa, switch off the passage light" → HA → Zigbee). For Sentinel the
chain is:

    "Alexa, <trigger phrase>"
        → an Alexa Routine flips a virtual helper exposed to Alexa
        → an HA automation POSTs to this LXC's voice server (voice_server.py)
        → handle_intent() does the work and returns a short answer
        → speak_on_alexa() makes the Echo say it

Amazon does the speech-to-text on this path, so there is NO Whisper here —
Whisper would only apply to a Telegram-voice path, which we deliberately did
not build (the operator wants everything through Alexa).

COST: the common intents (status / backups / energy / disks / presence) run
the existing monitors and summarize on LOCAL Gemma — ZERO Claude tokens — so
voice works even when the Anthropic balance is empty. Only the free-form
"ask" intent uses the Claude agent, and it is forced READ-ONLY: any
destructive tool the agent proposes is auto-denied. (Voice must never poll
Telegram for approval, and hands-free destructive actions are a bad idea.)

This module is pure logic + an HA REST call, so it's importable and testable
without the HTTP server.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

from tools import _ha_request, _audit  # reuse HA REST helper + audit log

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
# notify.<this> is the Alexa Media Player TTS service for the target Echo.
ALEXA_TARGET = os.getenv("VOICE_ALEXA_TARGET", "alexa_media_your_echo_dot")
# "tts" speaks immediately; "announce" prepends the Alexa chime.
ALEXA_TYPE = os.getenv("VOICE_ALEXA_TYPE", "tts")
# Keep spoken answers short — an Echo reading a 2000-char essay is painful.
SPEAK_MAX_CHARS = int(os.getenv("VOICE_SPEAK_MAX_CHARS", "700"))


# ---------------------------------------------------------------------
# Output — make the Echo speak
# ---------------------------------------------------------------------
def speak_on_alexa(text: str, target: Optional[str] = None,
                   kind: Optional[str] = None) -> Dict[str, Any]:
    """Make an Echo speak `text` via the Alexa Media Player notify service.

    Calls HA: notify.<target> with {"message": ..., "data": {"type": ...}}.
    target defaults to VOICE_ALEXA_TARGET, kind to VOICE_ALEXA_TYPE."""
    target = target or ALEXA_TARGET
    kind = kind or ALEXA_TYPE
    text = (text or "").strip()
    if not text:
        return {"error": "empty text"}
    body = {"message": text, "data": {"type": kind}}
    res = _ha_request("POST", f"/api/services/notify/{target}", body=body)
    if isinstance(res, list):
        res = {"changed_states": res}
    _audit("voice_speak", {"target": target, "type": kind, "chars": len(text),
                           "ok": "error" not in res, "error": res.get("error")})
    return res


def _for_speech(text: str) -> str:
    """Trim a long answer to something an Echo can read aloud, preferring a
    sentence boundary."""
    text = (text or "").strip()
    if len(text) <= SPEAK_MAX_CHARS:
        return text
    cut = text[:SPEAK_MAX_CHARS]
    for sep in (". ", "! ", "? ", "\n"):
        i = cut.rfind(sep)
        if i > SPEAK_MAX_CHARS * 0.5:
            return cut[:i + 1].strip()
    return cut.strip() + "…"


# ---------------------------------------------------------------------
# Intents — each returns a short answer string (spoken back on the Echo)
# ---------------------------------------------------------------------
def _intent_status(text: str = "") -> str:
    from reachability import sweep_services, summarize_sweep
    return summarize_sweep(sweep_services())


def _intent_backups(text: str = "") -> str:
    from backup_verifier import verify_backups, summarize_verification
    return summarize_verification(verify_backups())


def _intent_energy(text: str = "") -> str:
    from energy_assistant import read_energy_summary, summarize_energy
    data = read_energy_summary(24)
    if data.get("error"):
        return "Energy monitoring isn't configured yet."
    if data.get("unconfigured"):
        return "Energy monitoring has no devices configured yet."
    return summarize_energy(data)


def _intent_disks(text: str = "") -> str:
    from smart_monitor import scan_disks, summarize_scan
    return summarize_scan(scan_disks())

def _intent_speed(text: str = "") -> str:
    from speedtest_monitor import run_speedtest, summarize_speedtest
    return summarize_speedtest(run_speedtest())


def _intent_presence(text: str = "") -> str:
    from presence_assistant import check_presence_state
    d = check_presence_state()
    p = d.get("presence", "unknown")
    if p == "unknown":
        return "I couldn't determine who's home — presence isn't set up in Home Assistant."
    home = d.get("home") or []          # list of names (str)
    away = d.get("away") or []          # list of {"name","state"}
    parts = []
    parts.append("Home: " + ", ".join(home) + "." if home else "Nobody appears to be home.")
    if away:
        names = [a.get("name", "someone") if isinstance(a, dict) else str(a) for a in away]
        parts.append("Away: " + ", ".join(names) + ".")
    return " ".join(parts)


def _intent_docker(text: str = "") -> str:
    from docker_tools import scan_containers, summarize_containers
    return summarize_containers(scan_containers())


def _intent_train(text: str = "") -> str:
    from db_train_monitor import next_departures
    try:
        return next_departures().get("summary") or "I couldn't reach the departure service."
    except Exception as e:
        return f"I couldn't check the trains: {type(e).__name__}."


def _voice_deny(action: str, details: str, timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """Approval fn for the voice 'ask' path: never approve. Keeps voice
    read-only and avoids fighting the bot for Telegram callbacks."""
    return {"decision": "denied", "by": "voice (read-only)"}


def _intent_ask(text: str = "") -> str:
    """Free-form question → Claude agent, READ-ONLY. Reachable today via
    curl or a future Alexa custom skill (Alexa Routines can't capture free
    text). Costs Claude tokens, so it degrades gracefully when out of
    credits."""
    text = (text or "").strip()
    if not text:
        return "What would you like to ask Sentinel?"
    try:
        from agent_v5_approval import run_one
    except Exception as e:
        return f"The reasoning agent is unavailable ({type(e).__name__})."
    try:
        return run_one(user_msg=text,
                       thread_id=f"voice-{uuid.uuid4().hex[:8]}",
                       approval_fn=_voice_deny)
    except Exception as e:
        if "credit balance" in str(e).lower():
            return ("The Claude account is out of credits, so I can only run the "
                    "built-in checks right now — try asking for status, backups, "
                    "energy, disks, or who's home.")
        return f"Sorry, I hit an error: {type(e).__name__}."


INTENTS: Dict[str, Callable[[str], str]] = {
    "status": _intent_status, "health": _intent_status, "reachability": _intent_status,
    "backups": _intent_backups, "backup": _intent_backups,
    "energy": _intent_energy, "power": _intent_energy,
    "disks": _intent_disks, "disk": _intent_disks, "smart": _intent_disks,
    "speed": _intent_speed, "internet": _intent_speed, "network": _intent_speed,
    "presence": _intent_presence, "home": _intent_presence, "who": _intent_presence,
    "docker": _intent_docker, "containers": _intent_docker, "container": _intent_docker,
    "train": _intent_train, "commute": _intent_train, "bus": _intent_train, "sbahn": _intent_train,
    "ask": _intent_ask,
}


def handle_intent(intent: str, text: str = "") -> str:
    """Route an intent name to its handler. Unknown intents fall through to
    the free-form 'ask' agent using whatever text we got."""
    fn = INTENTS.get((intent or "").strip().lower())
    if fn is None:
        return _intent_ask(text or intent)
    return fn(text)


def respond(intent: str, text: str = "", speak: bool = True,
            target: Optional[str] = None) -> Dict[str, Any]:
    """Run an intent and (optionally) speak the answer on the Echo.

    Returns {intent, answer (full), spoken (trimmed), spoke_result}."""
    try:
        answer = handle_intent(intent, text) or "No result."
    except Exception as e:
        answer = f"Sorry, that check failed: {type(e).__name__}."
    spoken = _for_speech(answer)
    spoke_result = speak_on_alexa(spoken, target=target) if speak else None
    _audit("voice_intent", {"intent": intent, "text": (text or "")[:200],
                            "answer_chars": len(answer), "spoke": bool(speak)})
    return {"intent": intent, "answer": answer, "spoken": spoken,
            "spoke_result": spoke_result}
