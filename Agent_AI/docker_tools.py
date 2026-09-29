"""
docker_tools.py — Docker container monitoring + restart via the Portainer API.

Sentinel reaches the Debian13 Docker host (~25 containers: AdGuard DNS,
Immich, Vaultwarden, Jellyfin, …) through Portainer's REST API, which is
already running on the host. No SSH, no root login — a Portainer access
token, scoped and revocable, does it all.

Config in .env:
    PORTAINER_URL=https://docker.lan:9443   # or http://docker.lan:9000
    PORTAINER_API_KEY=ptr_xxxxx                # Portainer ▸ My account ▸ Access tokens
    PORTAINER_ENDPOINT_ID=                      # blank = auto-detect first Docker env
    PORTAINER_VERIFY_TLS=false                  # 9443 ships a self-signed cert
    PORTAINER_TIMEOUT_S=15

Containers that are SUPPOSED to be stopped (one-shot init containers that
chown a volume and exit 0) are declared in catalog.yaml under `docker:` —
see catalog.docker_expected_down(). They stay visible in the inventory but
never produce an alert or a restart card.

Public functions (wired into the agent in agent_v5_approval.py):
    list_containers(all_containers=True)  → inventory + per-container state/health
    get_container(name)                   → one container's status (name match)
    restart_container_raw(name)           → DESTRUCTIVE; gated at the graph level
    scan_containers()                     → digest dict for monitors / voice
    summarize_containers(data)            → local-the local LLM one-paragraph digest

Like the other modules, the restart entry point is *_raw: it does NOT ask for
approval itself — the agent's policy gate owns that, so we don't double-prompt.

CLI:
    uv run python docker_tools.py                 # table + LLM digest
    uv run python docker_tools.py --json
    uv run python docker_tools.py --restart immich_server
    uv run python docker_tools.py --alert         # Telegram if anything is down/unhealthy
                                                  # + per-container restart approval card
                                                  # (catalog restart_policy: ask)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import httpx
from dotenv import load_dotenv

load_dotenv()

from catalog import docker_expected_down  # "supposed to be stopped" allowlist
from tools import _audit  # reuse the audit log

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
_URL = os.getenv("PORTAINER_URL", "").rstrip("/")
_KEY = os.getenv("PORTAINER_API_KEY", "")
_ENDPOINT_ID = os.getenv("PORTAINER_ENDPOINT_ID", "").strip()
_VERIFY = os.getenv("PORTAINER_VERIFY_TLS", "false").lower() in ("1", "true", "yes")
_TIMEOUT = float(os.getenv("PORTAINER_TIMEOUT_S", "15"))

_Result = Union[Dict[str, Any], list]


def _configured() -> bool:
    return bool(_URL and _KEY)


def _request(method: str, path: str, **kw) -> _Result:
    if not _configured():
        return {"error": "Portainer not configured — set PORTAINER_URL and "
                         "PORTAINER_API_KEY in .env"}
    try:
        with httpx.Client(verify=_VERIFY, timeout=_TIMEOUT) as c:
            r = c.request(method, f"{_URL}{path}", headers={"X-API-Key": _KEY}, **kw)
            if r.status_code in (401, 403):
                return {"error": "Portainer auth failed — check PORTAINER_API_KEY"}
            if r.status_code == 404:
                return {"error": "not_found", "path": path}
            r.raise_for_status()
            if not r.content:
                return {"ok": True}
            try:
                return r.json()
            except Exception:
                return {"ok": True}
    except httpx.ConnectError as e:
        return {"error": f"cannot reach Portainer at {_URL} — {e}"}
    except httpx.HTTPError as e:
        return {"error": f"Portainer http error: {e}"}


_ENDPOINT_CACHE: Dict[str, Any] = {"id": None}


def _resolve_endpoint_id() -> Union[int, Dict[str, Any]]:
    """The numeric Portainer environment id whose Docker API we proxy. Uses
    PORTAINER_ENDPOINT_ID if set, else auto-detects the first Docker
    environment and caches it."""
    if _ENDPOINT_ID:
        try:
            return int(_ENDPOINT_ID)
        except ValueError:
            return {"error": f"PORTAINER_ENDPOINT_ID not an int: {_ENDPOINT_ID!r}"}
    if _ENDPOINT_CACHE["id"] is not None:
        return _ENDPOINT_CACHE["id"]
    eps = _request("GET", "/api/endpoints")
    if isinstance(eps, dict):
        return eps if eps.get("error") else {"error": "unexpected /api/endpoints response"}
    if not eps:
        return {"error": "no Portainer environments found"}
    # Type 1 = Docker, 2 = Docker (agent). Fall back to the first listed.
    docker_eps = [e for e in eps if e.get("Type") in (1, 2)] or eps
    eid = docker_eps[0].get("Id")
    _ENDPOINT_CACHE["id"] = eid
    return eid


def _docker(method: str, subpath: str, **kw) -> _Result:
    eid = _resolve_endpoint_id()
    if isinstance(eid, dict):       # error dict
        return eid
    return _request(method, f"/api/endpoints/{eid}/docker{subpath}", **kw)


# ---------------------------------------------------------------------
# Normalization + listing
# ---------------------------------------------------------------------
def _health_from_status(status: str) -> str:
    s = (status or "").lower()
    if "(healthy)" in s:
        return "healthy"
    if "(unhealthy)" in s:
        return "unhealthy"
    if "health: starting" in s:
        return "starting"
    return ""


_EXIT_RE = re.compile(r"Exited \((\d+)\)")


def _exit_code(status: str) -> Optional[int]:
    """Exit code out of the human status string ("Exited (0) 3 days ago").
    None while running, or for a container that never ran to completion."""
    m = _EXIT_RE.search(status or "")
    return int(m.group(1)) if m else None


def _norm(c: Dict[str, Any]) -> Dict[str, Any]:
    name = (c.get("Names") or ["/?"])[0].lstrip("/")
    status = c.get("Status", "")
    code = _exit_code(status)
    return {
        "name": name,
        "image": c.get("Image", ""),
        "state": c.get("State", ""),     # running/exited/paused/created/restarting/dead
        "status": status,                # human string, e.g. "Up 3 days (healthy)"
        "health": _health_from_status(status),
        "exit_code": code,               # None unless it exited
        # True = catalog says this one is MEANT to sit there stopped.
        # Labels are consumed here and deliberately not returned — the
        # agent's tool output shouldn't carry 11 compose labels per row.
        "expected_down": docker_expected_down(name, c.get("Labels") or {}, code),
        "id": (c.get("Id") or "")[:12],  # short id; Docker accepts it as a prefix
    }


def list_containers(all_containers: bool = True) -> Dict[str, Any]:
    """Inventory of containers with per-container state + health, plus a
    rolled-up digest. all_containers=False lists only running ones."""
    res = _docker("GET", f"/containers/json?all={1 if all_containers else 0}")
    if isinstance(res, dict):
        return res  # error
    items = [_norm(c) for c in res]
    running = sum(1 for c in items if c["state"] == "running")
    # expected-down containers stay in `containers` (nothing is hidden from
    # the operator) but are kept out of every list that drives an alert.
    expected = [c for c in items if c["expected_down"]]
    stopped = [c for c in items
               if c["state"] in ("exited", "dead", "created")
               and not c["expected_down"]]
    unhealthy = [c for c in items
                 if c["health"] == "unhealthy" and not c["expected_down"]]
    # problems first: stopped, then unhealthy, then the fine ones
    # (running or expected-down); alpha within.
    items.sort(key=lambda c: (c["expected_down"]
                              or (c["state"] == "running"
                                  and c["health"] != "unhealthy"),
                              c["name"].lower()))
    return {
        "total": len(items),
        "running": running,
        "stopped": len(stopped),
        "unhealthy": len(unhealthy),
        "expected_down": len(expected),
        "stopped_containers": [c["name"] for c in stopped],
        "unhealthy_containers": [c["name"] for c in unhealthy],
        "expected_down_containers": [c["name"] for c in expected],
        "containers": items,
    }


def get_container(name: str) -> Dict[str, Any]:
    """One container by name (exact, case-insensitive; falls back to a unique
    substring match)."""
    data = list_containers(all_containers=True)
    if data.get("error"):
        return data
    want = (name or "").strip().lstrip("/").lower()
    if not want:
        return {"error": "no container name given"}
    for c in data["containers"]:
        if c["name"].lower() == want:
            return c
    partial = [c for c in data["containers"] if want in c["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        return {"error": "ambiguous name", "matches": [c["name"] for c in partial]}
    return {"error": f"container not found: {name!r}"}


def restart_container_raw(name: str) -> Dict[str, Any]:
    """Restart a container. DESTRUCTIVE — the agent's approval gate owns the
    confirmation, so this does NOT prompt. Resolves the name first so we
    restart exactly one known container."""
    c = get_container(name)
    if c.get("error"):
        _audit("docker_restart_failed", {"name": name, "error": c.get("error")})
        return c
    cid, cname = c["id"], c["name"]
    res = _docker("POST", f"/containers/{cid}/restart")
    if isinstance(res, dict) and res.get("error"):
        _audit("docker_restart_failed", {"name": cname, "id": cid, "error": res["error"]})
        return {"error": res["error"], "container": cname}
    _audit("docker_restart_executed", {"name": cname, "id": cid})
    return {"ok": True, "restarted": cname, "id": cid}


# ---------------------------------------------------------------------
# Restart-approval flow for the monitor (catalog restart_policy: ask)
#
# The chat agent and the MCP server already gate restarts behind a
# Telegram approval card — but the 10-min monitor only *told* the
# operator a container was down and left the restart to a manual chat.
# Now the monitor itself asks: one card per problem container, reusing
# the bot's mcpapp:<rid>:<decision> callback → var/approvals/<rid>.json
# file IPC (the bot owns the single getUpdates stream, see
# sentinel_bot._handle_mcp_approval).
#
# Semantics: wait ASK_TIMEOUT_S inline for a tap; an unanswered card
# stays live and a later tap is honored on the next scheduled run.
# A denial (or any answered ask) is remembered in var/docker_restart_asks
# .json until the container is healthy again, with ASK_COOLDOWN_S before
# re-asking — so a deliberately stopped container doesn't nag every 10
# minutes. Default-deny throughout: only an explicit Approve restarts.
# ---------------------------------------------------------------------
VAR_DIR = Path(__file__).parent / "var"
APPROVALS_DIR = VAR_DIR / "approvals"
ASK_STATE_FILE = VAR_DIR / "docker_restart_asks.json"
# Inline wait after sending a card. Since the bot applies a tap the moment
# it arrives (apply_tapped_decision), this is only a short race window for
# an immediate tap — not the mechanism that makes approvals work. Keeping
# it short also stops one slow card from delaying the next one.
ASK_TIMEOUT_S = float(os.getenv("DOCKER_ASK_TIMEOUT_S", "20"))
ASK_COOLDOWN_S = float(os.getenv("DOCKER_ASK_COOLDOWN_S", "21600"))  # 6 h
ASK_MAX_CARDS = int(os.getenv("DOCKER_ASK_MAX_CARDS", "3"))


def _load_asks() -> Dict[str, Any]:
    try:
        return json.loads(ASK_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_asks(state: Dict[str, Any]) -> None:
    ASK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ASK_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(ASK_STATE_FILE)


def _merged_with_disk(state: Dict[str, Any]) -> Dict[str, Any]:
    """Never overwrite a decision another process recorded while we ran.

    The bot's instant-apply path writes this same file, so a blind save of
    our in-memory snapshot could downgrade an "approved" it just stored back
    to "pending" — and a pending entry is never re-asked, so the incident
    would go silent forever.
    """
    disk = _load_asks()
    for name, entry in state.items():
        theirs = disk.get(name)
        if (theirs and theirs.get("rid") == entry.get("rid")
                and entry.get("decision") == "pending"
                and theirs.get("decision") in ("approved", "denied")):
            state[name] = theirs
    return state


def _read_decision(rid: str) -> Optional[str]:
    """Atomically consume var/approvals/<rid>.json if the bot has written it.

    Two processes can race for the same tap: this monitor's inline wait and
    the bot's instant-apply path (apply_tapped_decision). os.replace() is
    atomic, so exactly one of them renames the file and acts on it; the loser
    gets FileNotFoundError and does nothing. Without the claim both would
    read the same decision and restart the container twice.
    """
    path = APPROVALS_DIR / f"{rid}.json"
    claim = APPROVALS_DIR / f"{rid}.claim"
    try:
        os.replace(path, claim)
    except OSError:
        return None
    try:
        decision = json.loads(claim.read_text()).get("decision")
    except (OSError, ValueError):
        decision = None
    claim.unlink(missing_ok=True)
    return decision if decision in ("approved", "denied") else None


def apply_tapped_decision(rid: str, decision: str, by: str = "") -> Optional[dict]:
    """Act on a restart card the instant the operator taps it.

    Called by sentinel_bot (the single Telegram consumer) from a worker
    thread. Before this existed, a tap that arrived after the monitor's
    inline wait had expired sat unapplied until the next scheduled run —
    up to ~10 minutes of silence between "Restart" and the confirmation.

    Returns None when the rid is not one of our restart cards (e.g. an MCP
    server approval — those stay untouched for their own watcher) or when
    the monitor's inline wait won the claim and is already acting.
    """
    state = _load_asks()
    name = next((n for n, e in state.items() if e.get("rid") == rid), None)
    if name is None:
        return None                     # not a docker restart card
    claimed = _read_decision(rid)
    if claimed is None:
        return None                     # monitor won the race, or already consumed

    # Re-read: the monitor may have saved between our load and the claim.
    state = _load_asks()
    entry = state.get(name) or {"rid": rid}
    entry.update({"decision": claimed, "asked_ts": time.time(), "by": by})
    state[name] = entry
    _save_asks(state)
    _audit("docker_tap_applied", {"rid": rid, "container": name,
                                  "decision": claimed, "by": by})
    if claimed == "approved":
        _restart_and_notify(name)
    return {"container": name, "decision": claimed}


def _send_restart_card(name: str, status: str) -> Tuple[Optional[str], Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").split(",")[0].strip()
    if not (token and chat_id):
        return None, None
    rid = uuid.uuid4().hex[:12]
    text = (
        f"🐳 Container needs attention: {name}\n\n"
        f"Current state: {status}\n\n"
        f"Restart it via Portainer?\n"
        f"(The buttons stay active — a tap is applied straight away, whenever "
        f"you get to it. I won't ask again for ~{int(ASK_COOLDOWN_S / 3600)} h.)"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Restart", "callback_data": f"mcpapp:{rid}:approved"},
        {"text": "⛔ Leave it", "callback_data": f"mcpapp:{rid}:denied"},
    ]]}
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return None, None
    return rid, chat_id


def _await_decision(rid: str, timeout_s: float) -> Optional[str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        decision = _read_decision(rid)
        if decision:
            return decision
        time.sleep(2)
    return None


def _restart_and_notify(name: str) -> None:
    from tools import send_telegram_alert
    res = restart_container_raw(name)
    if res.get("ok"):
        send_telegram_alert(f"✅ Docker: {name} restarted on your approval.")
    else:
        send_telegram_alert(f"❌ Docker: restart of {name} FAILED — "
                            f"{res.get('error', 'unknown error')}")


def handle_restart_approvals(data: Dict[str, Any]) -> None:
    """Monitor-side 'ask' flow: propose a restart for each stopped or
    unhealthy container and act on the operator's decision."""
    problems = {
        c["name"]: c["status"]
        for c in data.get("containers", [])
        if not c.get("expected_down")
        and (c["state"] in ("exited", "dead", "created")
             or c["health"] == "unhealthy")
    }
    state = _load_asks()
    now = time.time()

    # Container healthy again (or gone) → forget the incident entirely.
    for name in [n for n in state if n not in problems]:
        del state[name]

    # A card from an earlier run answered while we weren't looking?
    for name, entry in state.items():
        if entry.get("decision") == "pending":
            late = _read_decision(entry.get("rid", ""))
            if late:
                entry["decision"] = late
                entry["asked_ts"] = now  # cooldown restarts at the decision
                if late == "approved":
                    _restart_and_notify(name)

    cards = 0
    for name, status in problems.items():
        entry = state.get(name)
        if entry and (entry.get("decision") == "pending"
                      or now - entry.get("asked_ts", 0) < ASK_COOLDOWN_S):
            continue
        if cards >= ASK_MAX_CARDS:
            break
        rid, _chat = _send_restart_card(name, status)
        if rid is None:  # Telegram unreachable/unconfigured — retry next run
            break
        cards += 1
        # Persist BEFORE waiting: apply_tapped_decision resolves a tap by
        # looking the rid up here, so the entry has to be on disk from the
        # moment the card exists — otherwise an instant tap finds nothing.
        state[name] = {"rid": rid, "asked_ts": now, "decision": "pending"}
        _save_asks(state)
        decision = _await_decision(rid, ASK_TIMEOUT_S)
        if decision:
            state[name] = {"rid": rid, "asked_ts": time.time(),
                           "decision": decision}
            if decision == "approved":
                _restart_and_notify(name)

    _save_asks(_merged_with_disk(state))


# ---------------------------------------------------------------------
# Digest + the local LLM summary (for monitors / the voice 'docker' intent)
# ---------------------------------------------------------------------
def scan_containers() -> Dict[str, Any]:
    return list_containers(all_containers=True)


def summarize_containers(data: Dict[str, Any]) -> str:
    """local-LLM 2-3 sentence operator digest. Falls back to a
    deterministic line if the local LLM is unreachable, so it never crashes."""
    if data.get("error"):
        return f"Docker check failed: {data['error']}"

    total = data.get("total", 0)
    running = data.get("running", 0)
    stopped = data.get("stopped_containers", [])
    unhealthy = data.get("unhealthy_containers", [])
    expected = data.get("expected_down", 0)

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        if stopped or unhealthy:
            bits = []
            if stopped:
                bits.append(f"stopped: {', '.join(stopped)}")
            if unhealthy:
                bits.append(f"unhealthy: {', '.join(unhealthy)}")
            return prefix + (f"{running}/{total} containers running. " + "; ".join(bits) + ".")
        if expected:
            return (prefix + f"All {running} running containers are healthy "
                    f"({expected} of {total} are one-shot containers that are "
                    f"meant to stay stopped).")
        return prefix + f"All {total} containers running and healthy."

    lines = []
    for c in data.get("containers", []):
        if c.get("expected_down"):
            flag = "EXPECTED"
        elif c["state"] == "running" and c["health"] != "unhealthy":
            flag = "OK"
        else:
            flag = "PROBLEM"
        lines.append(f"  [{flag}] {c['name']:<28} {c['state']:<10} {c['status']}")
    block = "\n".join(lines)
    prompt = (
        "You are summarizing the Docker container status of a homelab host for "
        "the operator. Write ONE short paragraph (2-3 sentences). Lead with "
        "anything stopped or unhealthy, naming the containers. If everything is "
        "running, say so briefly. No preamble, no bullet points.\n"
        "Containers flagged EXPECTED are one-shot init containers that are "
        "SUPPOSED to be stopped — never report them as a problem and do not "
        "name them unless nothing else is wrong.\n\n"
        f"Counts: total={total} running={running} stopped={len(stopped)} "
        f"unhealthy={len(unhealthy)} expected_down={expected}\n\n"
        f"Containers:\n{block}\n"
    )
    try:
        from models import helper_llm
        llm = helper_llm(temperature=0.2, max_tokens=220)
        resp = llm.invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = text.strip()
        return text or _fallback("helper_llm returned empty")
    except Exception as e:
        return _fallback(f"helper_llm unavailable: {e}")


# ---------------------------------------------------------------------
# Alerting — once per transition (alert_state.py)
# ---------------------------------------------------------------------
_PORTAINER_KEY = "portainer"


def portainer_unreachable(error: str) -> bool:
    """Connectivity failures (host down, timeout, 5xx) — as opposed to a
    revoked key or missing config, which really do mean the check is broken."""
    e = (error or "").lower()
    return e.startswith("cannot reach portainer") or "http error" in e


def alert_transitions(data: Dict[str, Any]) -> Dict[str, Any]:
    """Stopped/unhealthy containers and an unreachable Portainer each page
    once and once more on recovery. The restart cards
    (handle_restart_approvals) are separate and keep their own cooldown."""
    from alert_state import Finding, notify
    if data.get("error"):
        where = _URL or "Portainer"
        finding = Finding(_PORTAINER_KEY, "Docker host (Portainer)", "high",
                          f"{data['error']} — Debian13 down? Container checks "
                          f"are paused until {where} answers again")
        # Can't see the containers: leave their incidents exactly as they were.
        return notify("docker", "🐳 Docker", [finding],
                      keep=lambda key: key != _PORTAINER_KEY)
    findings = [
        Finding(f"container:{c['name']}", c["name"], "high", c["status"])
        for c in data.get("containers", [])
        if not c.get("expected_down")
        and (c["state"] in ("exited", "dead", "created") or c["health"] == "unhealthy")
    ]
    return notify("docker", "🐳 Docker", findings)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'STATE':<11} {'HEALTH':<10} {'EXPECT':<7} {'NAME':<28} STATUS")
    print("-" * 90)
    for c in data.get("containers", []):
        expect = "yes" if c.get("expected_down") else "-"
        print(f"{c['state']:<11} {c['health'] or '-':<10} {expect:<7} "
              f"{c['name']:<28} {c['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel Docker (Portainer) tools")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    parser.add_argument("--no-summary", action="store_true", help="skip the LLM digest")
    parser.add_argument("--restart", metavar="NAME", help="restart one container by name")
    parser.add_argument("--alert", action="store_true",
                        help="send a Telegram alert if any container is stopped/unhealthy")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.restart:
        res = restart_container_raw(args.restart)
        print(res)
        return 0 if res.get("ok") else 1

    data = scan_containers()
    if args.json:
        import json
        print(json.dumps(data, indent=2))
        # 1 = the check itself broke (pages via OnFailure); 2 = findings,
        # already self-alerted (units declare SuccessExitStatus=2).
        return 0 if not data.get("error") else 1
    if data.get("error"):
        print(f"ERROR: {data['error']}")
        if args.alert and portainer_unreachable(data["error"]):
            # Debian13 (or just Portainer) is down: a FINDING about the
            # host, not a broken check — one message, then one on recovery.
            print(f"alerting: {alert_transitions(data)}")
            return 2
        return 1

    print(f"\nDocker — {data['total']} containers "
          f"({data['running']} running / {data['stopped']} stopped / "
          f"{data['unhealthy']} unhealthy / "
          f"{data.get('expected_down', 0)} expected-down)\n")
    _print_table(data)

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_containers(data))

    if args.alert:
        print(f"alerting: {alert_transitions(data)}")
        # Also prunes resolved incidents and applies late taps, so it
        # runs even when everything is healthy right now.
        handle_restart_approvals(data)

    return 0 if not (data["stopped_containers"] or data["unhealthy_containers"]) else 2


if __name__ == "__main__":
    sys.exit(main())
