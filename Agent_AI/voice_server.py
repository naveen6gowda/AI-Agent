"""
voice_server.py — Phase 4c HTTP bridge: HA → Sentinel → Alexa.

A tiny FastAPI service so a Home Assistant automation can hand a voice
command to Sentinel and have the Echo speak the answer. Runs on this LXC
(LXC 106, 192.168.178.106) alongside the bot; HA reaches it over the LAN.

    "Alexa, <phrase>" → Alexa Routine → HA automation
        → POST http://192.168.178.106:8099/voice
           {"intent": "status", "text": "", "speak": true}
        → this server runs the intent (Gemma for the canned checks; Claude,
          read-only, for free-form "ask") and speaks the result on the Echo
        → also returns the answer text in the HTTP response.

The common intents (status/backups/energy/disks/presence) spend ZERO Claude
tokens (see voice.py), so this works even with an empty Anthropic balance.

Auth: set VOICE_SERVER_TOKEN in .env and send it as `Authorization: Bearer
<token>` (or `?token=`). If the env var is empty, auth is DISABLED and a
loud warning is printed at startup — fine on a trusted LAN, not for exposure.

Run:
    uv run python voice_server.py            # binds VOICE_SERVER_HOST:PORT
    (or as the sentinel-voice.service systemd unit)

See docs/voice-setup.md for the Home Assistant + Alexa configuration.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

from voice import ALEXA_TARGET, INTENTS, respond, speak_on_alexa

TOKEN = os.getenv("VOICE_SERVER_TOKEN", "")
HOST = os.getenv("VOICE_SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("VOICE_SERVER_PORT", "8099"))

app = FastAPI(title="HomelabSentinel Voice Bridge", version="1.0")


class VoiceReq(BaseModel):
    intent: str = "ask"
    text: str = ""
    speak: bool = True
    device: Optional[str] = None   # override the Echo target (notify.<device>)


class SpeakReq(BaseModel):
    text: str
    device: Optional[str] = None


def _check_auth(authorization: Optional[str], token_q: Optional[str]) -> None:
    if not TOKEN:
        return  # auth disabled (warned at startup)
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied:
        supplied = token_q
    if supplied != TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


@app.get("/health")
def health():
    return {"ok": True, "alexa_target": ALEXA_TARGET, "auth_enabled": bool(TOKEN),
            "intents": sorted(set(INTENTS))}


@app.post("/voice")
def voice(req: VoiceReq, authorization: Optional[str] = Header(None),
          token: Optional[str] = Query(None)):
    """Run an intent and speak the answer on the Echo. Returns the text too."""
    _check_auth(authorization, token)
    return respond(req.intent, req.text, speak=req.speak, target=req.device)


@app.post("/speak")
def speak(req: SpeakReq, authorization: Optional[str] = Header(None),
          token: Optional[str] = Query(None)):
    """Make the Echo say arbitrary text (no intent logic). Handy for testing
    and for HA automations that already have the text to speak."""
    _check_auth(authorization, token)
    return {"spoke_result": speak_on_alexa(req.text, target=req.device)}


def main() -> int:
    import uvicorn
    if not TOKEN:
        print("[voice] WARNING: VOICE_SERVER_TOKEN is empty — auth is DISABLED. "
              "Set it in .env before exposing this beyond a trusted LAN.")
    print(f"[voice] starting on {HOST}:{PORT}  alexa_target=notify.{ALEXA_TARGET}  "
          f"auth={'on' if TOKEN else 'OFF'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
