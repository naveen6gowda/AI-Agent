"""
finance_server.py — HTTP bridge: HA phone notification → Firefly III.

Sibling of voice_server.py (same auth + systemd pattern). The HA companion
app captures N26 notifications (sensor.navis24_last_notification); an HA
automation forwards each one here:

    POST http://sentinel.lan:8098/transaction
    Authorization: Bearer <FINANCE_SERVER_TOKEN>
    {"message": "<android.text>", "posted": "<post_time>", "title": "..."}

Flow: finance_parser.parse() → firefly_client.create_transaction() (dedupes
on external_id). Unparseable texts are appended to finance_unparsed.jsonl
and every request is audit-logged, so no capture is silently lost.

Config in .env:
    FINANCE_SERVER_TOKEN=<shared secret; empty = auth DISABLED (warned)>
    FINANCE_SERVER_PORT=8098
    (plus the FIREFLY_* / FINANCE_* vars — see firefly_client.py)

Run:
    uv run python finance_server.py
    (or as the sentinel-finance.service systemd unit)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

import firefly_client
from finance_parser import parse

TOKEN = os.getenv("FINANCE_SERVER_TOKEN", "")
HOST = os.getenv("FINANCE_SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("FINANCE_SERVER_PORT", "8098"))

AUDIT_LOG_PATH = Path(__file__).parent / "var" / "audit.log"
AUDIT_LOG_PATH.parent.mkdir(exist_ok=True)
UNPARSED_PATH = Path(__file__).parent / "var" / "finance_unparsed.jsonl"

app = FastAPI(title="HomelabSentinel Finance Bridge", version="1.0")


class TxnReq(BaseModel):
    message: str
    posted: Optional[Union[int, str]] = None   # notification post_time from HA
    title: Optional[str] = None                # notification title; for Samsung
                                               # Wallet it's the card name and
                                               # routes the asset account


def _audit(event: str, payload: Dict[str, Any]) -> None:
    """Append one structured line to audit.log. Best-effort — never raises."""
    try:
        line = json.dumps(
            {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
             "event": event, **payload},
            default=str,
        )
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_unparsed(req: TxnReq, reason: str) -> None:
    try:
        line = json.dumps(
            {"ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
             "reason": reason, "title": req.title, "message": req.message,
             "posted": req.posted},
            ensure_ascii=False,
        )
        with UNPARSED_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
    return {"ok": True, "auth_enabled": bool(TOKEN),
            "firefly": firefly_client.about(),
            "asset_account": os.getenv("FINANCE_ASSET_ACCOUNT", "")}


@app.post("/transaction")
def transaction(req: TxnReq, authorization: Optional[str] = Header(None),
                token: Optional[str] = Query(None)):
    """Parse one forwarded notification and store it in Firefly.

    result: "stored" | "duplicate" | "skipped" (unparseable) | "error"
    """
    _check_auth(authorization, token)

    posted = str(req.posted) if req.posted not in (None, "") else None
    txn = parse(req.message, posted=posted, title=req.title)
    if not txn.get("parsed"):
        _log_unparsed(req, txn.get("reason", "unknown"))
        _audit("finance_capture_skipped",
               {"reason": txn.get("reason"), "message": req.message[:200]})
        return {"result": "skipped", "reason": txn.get("reason")}

    res = firefly_client.create_transaction(txn)
    _audit("finance_capture",
           {"result": res.get("result"), "kind": txn.get("kind"),
            "direction": txn.get("direction"), "amount": txn.get("amount"),
            "merchant": txn.get("merchant"),
            "asset_account": txn.get("asset_account"),
            "external_id": txn.get("external_id"),
            "firefly_id": res.get("firefly_id")})
    return res


def main() -> int:
    import uvicorn
    if not TOKEN:
        print("[finance] WARNING: FINANCE_SERVER_TOKEN is empty — auth is "
              "DISABLED. Set it in .env before exposing beyond a trusted LAN.")
    print(f"[finance] starting on {HOST}:{PORT}  "
          f"firefly={os.getenv('FIREFLY_URL', '?')}  "
          f"asset_account={os.getenv('FINANCE_ASSET_ACCOUNT', '?')}  "
          f"auth={'on' if TOKEN else 'OFF'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
