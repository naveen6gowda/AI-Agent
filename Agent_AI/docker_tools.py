"""
docker_tools.py — Docker container monitoring + restart via the Portainer API.

Sentinel reaches the Debian13 Docker host (~25 containers: AdGuard DNS,
Immich, Vaultwarden, Jellyfin, …) through Portainer's REST API, which is
already running on the host. No SSH, no root login — a Portainer access
token, scoped and revocable, does it all.

Config in .env:
    PORTAINER_URL=https://docker.lan   # or http://docker.lan
    PORTAINER_API_KEY=ptr_xxxxx                # Portainer ▸ My account ▸ Access tokens
    PORTAINER_ENDPOINT_ID=                      # blank = auto-detect first Docker env
    PORTAINER_VERIFY_TLS=false                  # 9443 ships a self-signed cert
    PORTAINER_TIMEOUT_S=15

Public functions (wired into the agent in agent_v5_approval.py):
    list_containers(all_containers=True)  → inventory + per-container state/health
    get_container(name)                   → one container's status (name match)
    restart_container_raw(name)           → DESTRUCTIVE; gated at the graph level
    scan_containers()                     → digest dict for monitors / voice
    summarize_containers(data)            → local-Gemma one-paragraph digest

Like the other modules, the restart entry point is *_raw: it does NOT ask for
approval itself — the agent's policy gate owns that, so we don't double-prompt.

CLI:
    uv run python docker_tools.py                 # table + Gemma digest
    uv run python docker_tools.py --json
    uv run python docker_tools.py --restart immich_server
    uv run python docker_tools.py --alert         # Telegram if anything is down/unhealthy
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Union

import httpx
from dotenv import load_dotenv

load_dotenv()

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


def _norm(c: Dict[str, Any]) -> Dict[str, Any]:
    name = (c.get("Names") or ["/?"])[0].lstrip("/")
    status = c.get("Status", "")
    return {
        "name": name,
        "image": c.get("Image", ""),
        "state": c.get("State", ""),     # running/exited/paused/created/restarting/dead
        "status": status,                # human string, e.g. "Up 3 days (healthy)"
        "health": _health_from_status(status),
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
    stopped = [c for c in items if c["state"] in ("exited", "dead", "created")]
    unhealthy = [c for c in items if c["health"] == "unhealthy"]
    # problems first: stopped, then unhealthy, then running; alpha within.
    items.sort(key=lambda c: (c["state"] == "running" and c["health"] != "unhealthy",
                              c["name"].lower()))
    return {
        "total": len(items),
        "running": running,
        "stopped": len(stopped),
        "unhealthy": len(unhealthy),
        "stopped_containers": [c["name"] for c in stopped],
        "unhealthy_containers": [c["name"] for c in unhealthy],
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
# Digest + Gemma summary (for monitors / the voice 'docker' intent)
# ---------------------------------------------------------------------
def scan_containers() -> Dict[str, Any]:
    return list_containers(all_containers=True)


def summarize_containers(data: Dict[str, Any]) -> str:
    """Local-Gemma 2-3 sentence operator digest. Falls back to a
    deterministic line if Gemma is unreachable, so it never crashes."""
    if data.get("error"):
        return f"Docker check failed: {data['error']}"

    total = data.get("total", 0)
    running = data.get("running", 0)
    stopped = data.get("stopped_containers", [])
    unhealthy = data.get("unhealthy_containers", [])

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        if stopped or unhealthy:
            bits = []
            if stopped:
                bits.append(f"stopped: {', '.join(stopped)}")
            if unhealthy:
                bits.append(f"unhealthy: {', '.join(unhealthy)}")
            return prefix + (f"{running}/{total} containers running. " + "; ".join(bits) + ".")
        return prefix + f"All {total} containers running and healthy."

    lines = []
    for c in data.get("containers", []):
        flag = "OK" if (c["state"] == "running" and c["health"] != "unhealthy") else "PROBLEM"
        lines.append(f"  [{flag}] {c['name']:<28} {c['state']:<10} {c['status']}")
    block = "\n".join(lines)
    prompt = (
        "You are summarizing the Docker container status of a homelab host for "
        "the operator. Write ONE short paragraph (2-3 sentences). Lead with "
        "anything stopped or unhealthy, naming the containers. If everything is "
        "running, say so briefly. No preamble, no bullet points.\n\n"
        f"Counts: total={total} running={running} stopped={len(stopped)} "
        f"unhealthy={len(unhealthy)}\n\n"
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
# CLI
# ---------------------------------------------------------------------
def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'STATE':<11} {'HEALTH':<10} {'NAME':<28} STATUS")
    print("-" * 80)
    for c in data.get("containers", []):
        print(f"{c['state']:<11} {c['health'] or '-':<10} {c['name']:<28} {c['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel Docker (Portainer) tools")
    parser.add_argument("--json", action="store_true", help="emit raw JSON")
    parser.add_argument("--no-summary", action="store_true", help="skip the Gemma digest")
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
        return 0 if not data.get("error") else 2
    if data.get("error"):
        print(f"ERROR: {data['error']}")
        return 2

    print(f"\nDocker — {data['total']} containers "
          f"({data['running']} running / {data['stopped']} stopped / "
          f"{data['unhealthy']} unhealthy)\n")
    _print_table(data)

    if not args.no_summary:
        print("\n--- Gemma digest ---")
        print(summarize_containers(data))

    if args.alert and (data["stopped_containers"] or data["unhealthy_containers"]):
        from tools import send_telegram_alert
        send_telegram_alert("🐳 Docker: " + summarize_containers(data))

    return 0 if not (data["stopped_containers"] or data["unhealthy_containers"]) else 2


if __name__ == "__main__":
    sys.exit(main())
