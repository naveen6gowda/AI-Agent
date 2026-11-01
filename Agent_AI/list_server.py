"""
list_server.py — the Indian List: Alexa custom skill + phone/laptop page.

    "Alexa, tell Indian List to add milk"     (also: "remove milk",
     "milk is done", "what's on the list", "clear done items")
        → Alexa skill endpoint  https://list.example.com/alexa/<LIST_ALEXA_SECRET>
    https://list.example.com/?k=<LIST_TOKEN>    iPhone / laptop, anywhere
    http://sentinel.lan:8097/?k=<LIST_TOKEN>      LAN fallback when the internet is down

Public through the dedicated cloudflared container in list-tunnel/ (only this
port; voice/mcp/finance stay LAN-only). Fails closed: refuses to start
without LIST_TOKEN and LIST_ALEXA_SECRET. Data + Excel copy live in
sentinel_list.py (var/list/). Alexa interaction model: alexa/sentinel-list-model.json.
"""

from __future__ import annotations

import hmac
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

load_dotenv()

import sentinel_list as sl

TOKEN = os.getenv("LIST_TOKEN", "")
ALEXA_SECRET = os.getenv("LIST_ALEXA_SECRET", "")
SKILL_ID = os.getenv("LIST_ALEXA_SKILL_ID", "")   # optional extra check
PORT = int(os.getenv("LIST_SERVER_PORT", "8097"))
PAGE = (Path(__file__).parent / "list_page.html").read_text(encoding="utf-8")

app = FastAPI(title="Indian List", docs_url=None, redoc_url=None, openapi_url=None)


def _ok(supplied: str | None) -> bool:
    return bool(TOKEN and supplied) and hmac.compare_digest(supplied.encode(), TOKEN.encode())


def _auth(request: Request) -> None:
    if not _ok(request.cookies.get("k")):
        raise HTTPException(401, "open your Indian List link (with ?k=) again")


@app.get("/", response_class=HTMLResponse)
def page(request: Request, k: str | None = None):
    if not (_ok(k) or _ok(request.cookies.get("k"))):
        return HTMLResponse("<h3>Indian List: open the full link (with ?k=…)</h3>", 401)
    resp = HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})
    if k:   # iOS home-screen apps keep their own cookie jar: the ?k= link re-seeds it
        resp.set_cookie("k", k, max_age=400 * 86400, httponly=True, samesite="lax")
    return resp


@app.get("/sw.js")
def service_worker():
    """Offline shell: network first, last good page when there is no signal."""
    return Response(
        "self.addEventListener('install',()=>self.skipWaiting());"
        "self.addEventListener('activate',e=>e.waitUntil(clients.claim()));"
        "self.addEventListener('fetch',e=>{if(e.request.mode!=='navigate')return;"
        "e.respondWith(fetch(e.request).then(r=>{if(r.ok){const c=r.clone();"
        "caches.open('sl1').then(x=>x.put('/',c))}return r}).catch(()=>caches.match('/')))});",
        media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/api/items")
def list_items(request: Request):
    _auth(request)
    return {"items": sl.items()}


@app.post("/api/items")
def add_item(request: Request, item: str = Body(..., embed=True)):
    _auth(request)
    added, already = sl.add(item)
    return {"added": added, "already": already}


@app.patch("/api/items/{item_id}")
def tick_item(item_id: str, request: Request,
              done: bool | None = Body(None), item: str | None = Body(None)):
    """Tick/untick ({"done": …}) or fix the spelling ({"item": …})."""
    _auth(request)
    if item is not None:
        name = sl.rename(item_id, item)
        hits = [name] if name else []
    elif done is not None:
        hits, _ = sl.set_done(item_id, done)
    else:
        raise HTTPException(422, "send done or item")
    if not hits:
        raise HTTPException(404, "no such item")
    return {"updated": hits}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, request: Request):
    _auth(request)
    hits, _ = sl.remove(item_id)
    if not hits:
        raise HTTPException(404, "no such item")
    return {"removed": hits}


@app.post("/api/clear-done")
def clear(request: Request):
    _auth(request)
    return {"cleared": sl.clear_done()}


@app.get("/list.xlsx")
def excel(request: Request):
    _auth(request)
    return Response(sl.xlsx_bytes(sl.items()), headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="Indian List.xlsx"'},
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------
# Alexa custom skill
# ---------------------------------------------------------------------
_CLEAR_WORDS = {"done items", "the done items", "done", "completed items", "ticked items"}


def _join(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _summary() -> str:
    todo = [r["item"] for r in sl.items() if not r["done"]]
    return f"You need {_join(todo)}." if todo else "Nothing left to get."


def alexa_turn(req: dict) -> tuple[str, bool]:
    """One Alexa request → (speech, end_session)."""
    if req.get("type") == "LaunchRequest":
        return f"Indian list. {_summary()} What should I add?", False
    if req.get("type") != "IntentRequest":
        return "", True   # SessionEndedRequest etc.
    intent = req["intent"]["name"]
    item = (((req["intent"].get("slots") or {}).get("item") or {}).get("value") or "").strip()
    if intent == "RemoveItemIntent" and sl._norm(item) in _CLEAR_WORDS:
        intent = "ClearDoneIntent"   # "remove done items" is a clear, not an item
    if intent == "AddItemIntent":
        if not item:
            return "What should I add?", False
        added, already = sl.add(item)
        said = [f"Added {_join(added)}."] if added else []
        if already:
            said.append(f"{_join(already)} {'is' if len(already) == 1 else 'are'} already on the list.")
        return " ".join(said), True
    if intent in ("RemoveItemIntent", "DoneItemIntent"):
        if not item:
            return "Which item?", False
        removing = intent == "RemoveItemIntent"
        hits, misses = sl.remove(item) if removing else sl.set_done(item)
        said = [f"{'Removed' if removing else 'Ticked off'} {_join(hits)}."] if hits else []
        if misses:
            said.append(f"I couldn't find {_join(misses)} on the list.")
        return " ".join(said), True
    if intent == "ReadListIntent":
        return _summary(), True
    if intent == "ClearDoneIntent":
        n = sl.clear_done()
        return (f"Cleared {n} done item{'' if n == 1 else 's'}." if n else "Nothing is ticked off yet."), True
    if intent == "AMAZON.HelpIntent":
        return "Say add milk, milk is done, remove milk, what's on the list, or clear done items.", False
    if intent == "AMAZON.FallbackIntent":
        return "Sorry, I didn't catch that. Say add, remove or done, then the item.", False
    return "OK.", True   # Stop / Cancel / NavigateHome


@app.post("/alexa/{secret}")
def alexa(secret: str, body: dict):
    # ponytail: secret URL + skill id + 150 s replay window. Amazon's signature-
    # cert check is only mandatory for published skills; add it if this ever is.
    if not hmac.compare_digest(secret.encode(), ALEXA_SECRET.encode()):
        raise HTTPException(404)
    app_id = (body.get("context", {}).get("System", {}).get("application", {})
              .get("applicationId"))
    if SKILL_ID and app_id != SKILL_ID:
        raise HTTPException(403, "wrong skill")
    req = body.get("request", {})
    ts = datetime.fromisoformat(req.get("timestamp", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    if abs((datetime.now(timezone.utc) - ts).total_seconds()) > 150:
        raise HTTPException(400, "stale request")
    speech, end = alexa_turn(req)
    if not speech:
        return {"version": "1.0", "response": {}}
    out = {"outputSpeech": {"type": "PlainText", "text": speech}, "shouldEndSession": end}
    if not end:
        out["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": "What should I do with the list?"}}
    return {"version": "1.0", "response": out}


def main() -> int:
    import uvicorn
    if not (TOKEN and ALEXA_SECRET):
        print("[list] LIST_TOKEN and LIST_ALEXA_SECRET must be set in .env — refusing to start.")
        return 1
    print(f"[list] Indian List on 0.0.0.0:{PORT}  skill-id check={'on' if SKILL_ID else 'off'}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", proxy_headers=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
