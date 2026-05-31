"""
Real tool implementations for HomelabSentinel.

Step 1  : Proxmox via API token (live HTTP).
Step 1B : Hybrid model layer (see models.py).
Step 2  : Home Assistant (read) + Telegram (send).
Step 3  : Approval gate + destructive tools wired (THIS COMMIT).
          - request_telegram_approval(): send a Telegram message with
            Approve/Deny inline keyboard, poll for the response.
          - restart_lxc(): now ACTUALLY restarts via Proxmox API, but
            calls request_telegram_approval first.
          - call_ha_service(): new HA write tool, also gated.
          - audit.log: every approval request + decision appended.

Design notes:
- All credentials come from .env (see .env.example). Never hardcode.
- Each tool returns a plain dict so it serializes cleanly into the
  agent's message history.
- READ tools return data directly. WRITE tools gate themselves through
  request_telegram_approval before doing anything destructive.
- AUDIT_LOG_PATH is appended to on EVERY approval ask — review this
  file regularly to see what the agent has been asking permission for.
"""

import json
import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()


# -------------------------------------------------------------------
# Audit log
# -------------------------------------------------------------------
AUDIT_LOG_PATH = Path(__file__).parent / "audit.log"


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
        pass  # auditing must never break the agent


# -------------------------------------------------------------------
# Proxmox HTTP client
# -------------------------------------------------------------------
_PROXMOX_HOST = os.getenv("PROXMOX_HOST", "")
_PROXMOX_PORT = os.getenv("PROXMOX_PORT", "8006")
_PROXMOX_TOKEN_ID = os.getenv("PROXMOX_TOKEN_ID", "")
_PROXMOX_TOKEN_SECRET = os.getenv("PROXMOX_TOKEN_SECRET", "")
_PROXMOX_DEFAULT_NODE = os.getenv("PROXMOX_DEFAULT_NODE", "pve")
_PROXMOX_VERIFY_TLS = os.getenv("PROXMOX_VERIFY_TLS", "false").lower() == "true"
_PROXMOX_SSH_USER = os.getenv("PROXMOX_SSH_USER", "root")
_PROXMOX_SSH_HOST = os.getenv("PROXMOX_SSH_HOST", _PROXMOX_HOST)
_PROXMOX_SSH_PORT = os.getenv("PROXMOX_SSH_PORT", "22")

_PROXMOX_BASE = f"https://{_PROXMOX_HOST}:{_PROXMOX_PORT}/api2/json"
_PROXMOX_AUTH = f"PVEAPIToken={_PROXMOX_TOKEN_ID}={_PROXMOX_TOKEN_SECRET}"


def _proxmox_request(method: str, path: str) -> Dict[str, Any]:
    """HTTP request against Proxmox. Returns {"data": ...} or {"error": "..."}."""
    if not (_PROXMOX_HOST and _PROXMOX_TOKEN_ID and _PROXMOX_TOKEN_SECRET):
        return {"error": "Proxmox env vars not set. See .env.example."}
    try:
        r = httpx.request(
            method,
            f"{_PROXMOX_BASE}{path}",
            headers={"Authorization": _PROXMOX_AUTH},
            verify=_PROXMOX_VERIFY_TLS,
            timeout=15.0,
        )
        if r.status_code == 401:
            return {"error": "Proxmox auth failed — check PROXMOX_TOKEN_ID / SECRET."}
        if r.status_code == 403:
            return {"error": "Proxmox forbidden — token lacks required role/privileges."}
        if r.status_code == 404:
            return {"error": "not_found", "path": path}
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError as e:
        return {"error": f"cannot reach Proxmox at {_PROXMOX_HOST}:{_PROXMOX_PORT} — {e}"}
    except httpx.HTTPError as e:
        return {"error": f"proxmox http error: {e}"}


def _proxmox_get(path: str) -> Dict[str, Any]:
    return _proxmox_request("GET", path)


def list_proxmox_nodes() -> dict:
    """List all Proxmox nodes visible to the API token.

    Use this when you don't know which node name to pass to
    check_proxmox_status or restart_lxc. In a single-node homelab there
    will be exactly one entry — its 'node' field is the canonical name.
    """
    resp = _proxmox_get("/nodes")
    if "error" in resp:
        return resp
    nodes = resp.get("data", [])
    return {
        "count": len(nodes),
        "nodes": [
            {
                "node": n.get("node"),
                "status": n.get("status"),
                "uptime_h": round(n.get("uptime", 0) / 3600, 1),
                "cpu_pct": round(n.get("cpu", 0) * 100, 1),
                "mem_pct": round(
                    n.get("mem", 0) / (n.get("maxmem", 1) or 1) * 100, 1
                ),
            }
            for n in nodes
        ],
    }


def list_proxmox_guests(node: Optional[str] = None) -> dict:
    """List all LXCs and VMs on a Proxmox node.

    Use this when you don't know which vmid to inspect. Defaults to
    PROXMOX_DEFAULT_NODE. Returns kind ('lxc'|'qemu'), vmid, name, status.
    """
    node = node or _PROXMOX_DEFAULT_NODE
    out = []
    for kind, path in (("lxc", "lxc"), ("qemu", "qemu")):
        resp = _proxmox_get(f"/nodes/{node}/{path}")
        if "error" in resp:
            # If the node name is wrong this is where we find out — bubble up.
            return resp
        for g in resp.get("data", []):
            out.append({
                "kind": kind,
                "vmid": g.get("vmid"),
                "name": g.get("name"),
                "status": g.get("status"),
            })
    return {"node": node, "count": len(out), "guests": out}


def _ssh_exec(remote_command: str, timeout: int = 15) -> Dict[str, Any]:
    """Run a command on the Proxmox host via SSH. Returns {"stdout": "..."} or error.

    Requires:
      - OpenSSH client on PATH (Windows 10+ has it; install
        `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0`)
      - Key-based auth set up (BatchMode prevents any password prompt
        from blocking the agent — we'd rather fail fast).
    """
    if not _PROXMOX_SSH_HOST:
        return {"error": "PROXMOX_SSH_HOST not set"}
    target = f"{_PROXMOX_SSH_USER}@{_PROXMOX_SSH_HOST}"
    try:
        proc = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new",
             "-p", str(_PROXMOX_SSH_PORT),
             target, remote_command],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            # Include stdout — some tools (notably `smartctl -j`) emit
            # valid JSON even when exiting non-zero. Callers that need the
            # data can still parse it; callers that just want pass/fail
            # check for "error".
            return {
                "error": f"ssh exit {proc.returncode}",
                "stderr": proc.stderr.strip()[:500],
                "stdout": proc.stdout,
                "exit_code": proc.returncode,
            }
        return {"stdout": proc.stdout, "exit_code": 0}
    except FileNotFoundError:
        return {"error": "ssh client not found on PATH. "
                          "On Windows: Settings → Apps → Optional features → "
                          "Add → OpenSSH Client."}
    except subprocess.TimeoutExpired:
        return {"error": f"ssh timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"ssh failed: {type(e).__name__}: {e}"}


def _parse_meminfo(text: str) -> Optional[Dict[str, int]]:
    """Extract MemTotal and MemAvailable (kB) from /proc/meminfo text."""
    total = available = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            try:
                total = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("MemAvailable:"):
            try:
                available = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        if total is not None and available is not None:
            break
    if total is None or available is None:
        return None
    return {"total_kb": total, "available_kb": available}


def get_guest_mem_pct(vmid: int, kind: Optional[str] = None) -> dict:
    """Read REAL memory utilization from inside the guest via /proc/meminfo.

    Why this exists: Proxmox's host-side mem/maxmem for QEMU is the
    balloon driver's view — it counts allocated/touched pages including
    Linux page cache. A healthy VM looks 85%+ used when it's actually
    fine. The only meaningful "is this VM under memory pressure?" number
    is `1 - MemAvailable/MemTotal` from inside the guest.

    LXC reports accurate cgroup numbers from the host already, but we
    use the same code path for consistency (so the agent's logic doesn't
    have to branch on kind).

    Args:
        vmid: target vmid
        kind: "lxc" or "qemu". Auto-detected from catalog if omitted.

    Returns on success:
        {"vmid": 100, "kind": "qemu",
         "mem_total_mb": 4096, "mem_available_mb": 2890,
         "mem_used_pct": 29.4,
         "source": "guest_/proc/meminfo"}
    On failure:
        {"error": "...", "fallback_hint": "use Proxmox host-side mem"}
    """
    if kind not in ("lxc", "qemu"):
        kind = _kind_from_catalog(vmid)
    if kind not in ("lxc", "qemu"):
        return {"error": f"unknown kind for vmid {vmid}"}

    if kind == "qemu":
        # qm guest exec requires qemu-guest-agent in the VM.
        # --timeout-seconds caps the exec; we wrap in JSON output mode
        # so we can parse exitcode/out-data cleanly.
        ssh_cmd = (f"qm guest exec {vmid} --timeout 5 -- cat /proc/meminfo")
    else:
        ssh_cmd = f"pct exec {vmid} -- cat /proc/meminfo"

    res = _ssh_exec(ssh_cmd, timeout=15)
    if "error" in res:
        return {**res, "fallback_hint": "guest agent missing or SSH broken; "
                                         "host-side mem from Proxmox API is "
                                         "unreliable for QEMU but available"}

    stdout = res["stdout"]
    meminfo_text = stdout
    if kind == "qemu":
        # qm guest exec emits JSON to stdout: {"exitcode": 0, "exited": 1,
        # "out-data": "MemTotal: ...\n..."}
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {"error": f"unexpected qm output (no JSON): {stdout[:200]}",
                    "fallback_hint": "verify qm guest exec works manually"}
        if payload.get("exitcode", 1) != 0:
            return {"error": f"guest exec exitcode {payload.get('exitcode')}: "
                              f"{payload.get('err-data', '')[:300]}",
                    "fallback_hint": "qemu-guest-agent likely missing"}
        meminfo_text = payload.get("out-data", "")

    parsed = _parse_meminfo(meminfo_text)
    if not parsed:
        return {"error": "MemTotal/MemAvailable not found in /proc/meminfo output"}

    used_pct = round((1 - parsed["available_kb"] / parsed["total_kb"]) * 100, 1)
    return {
        "vmid": vmid,
        "kind": kind,
        "mem_total_mb": round(parsed["total_kb"] / 1024, 0),
        "mem_available_mb": round(parsed["available_kb"] / 1024, 0),
        "mem_used_pct": used_pct,
        "source": "guest_/proc/meminfo",
    }


def _kind_from_catalog(vmid: int) -> Optional[str]:
    """Lazy peek at catalog.yaml to learn 'lxc' or 'qemu' for a vmid.

    Lazy import — tools.py must not hard-depend on catalog.py since
    catalog.py imports nothing from here. Returns None on any failure
    (catalog missing, vmid not listed, etc.) and the caller probes.
    """
    try:
        from catalog import load_catalog  # local import = no module-load cost
        svc = load_catalog().by_vmid(vmid)
        return svc.kind if svc else None
    except Exception:
        return None


def _format_status(resp: dict, kind: str, vmid: int) -> dict:
    """Shape a /status/current response into our flat dict.

    `mem_pct` here is the HOST-side view (Proxmox API). For QEMU this is
    the balloon driver's reading, which counts page cache and is wildly
    misleading. The caller (check_proxmox_status) overrides this with
    the guest-side reading from /proc/meminfo when possible.
    """
    data = resp.get("data", {})
    mem = data.get("mem", 0)
    maxmem = data.get("maxmem", 0) or 1
    uptime = data.get("uptime", 0)
    return {
        "kind": kind,
        "name": data.get("name", f"vmid-{vmid}"),
        "status": data.get("status", "unknown"),
        "mem_pct": round(mem / maxmem * 100, 1),
        "mem_pct_source": "host_balloon" if kind == "qemu" else "host_cgroup",
        "maxmem_mb": round(maxmem / 1024 / 1024, 0),
        "uptime_h": round(uptime / 3600, 1),
        "cpu_pct": round(data.get("cpu", 0) * 100, 1),
    }


def _enrich_with_guest_mem(status: dict, vmid: int) -> dict:
    """Replace host-side mem_pct with the guest's /proc/meminfo reading.

    If the guest call fails (qemu-guest-agent missing, SSH broken, VM
    stopped), keep the host-side number and add a warning so the agent
    knows the data is unreliable.
    """
    if status.get("status") != "running":
        # No point asking a stopped VM for its memory state.
        return status
    g = get_guest_mem_pct(vmid, kind=status.get("kind"))
    if "error" in g:
        status["mem_pct_warning"] = g["error"]
        return status
    status["mem_pct"] = g["mem_used_pct"]
    status["mem_pct_source"] = "guest"
    status["mem_total_mb"] = g["mem_total_mb"]
    status["mem_available_mb"] = g["mem_available_mb"]
    return status


def check_proxmox_status(node: str, vmid: int) -> dict:
    """Get live status of an LXC or VM on Proxmox.

    Resolution order:
      1. If catalog.yaml knows this vmid, use the declared kind (1 API call).
      2. Otherwise try LXC; if any error, fall back to QEMU. Some Proxmox
         versions return 500 (not 404) when you ask the wrong endpoint.

    After fetching host-side status, attempts to override `mem_pct` with
    the guest's /proc/meminfo reading (the only meaningful number — see
    get_guest_mem_pct docstring). Costs one SSH round-trip; gracefully
    falls back to host data with a warning if guest exec fails.

    Returns: {kind, name, status, mem_pct, mem_pct_source, maxmem_mb,
              uptime_h, cpu_pct, ...optional guest fields}.
    """
    node = node or _PROXMOX_DEFAULT_NODE

    catalog_kind = _kind_from_catalog(vmid)
    if catalog_kind in ("lxc", "qemu"):
        resp = _proxmox_get(f"/nodes/{node}/{catalog_kind}/{vmid}/status/current")
        if "error" in resp:
            return resp
        return _enrich_with_guest_mem(_format_status(resp, catalog_kind, vmid),
                                       vmid)

    # Catalog miss — probe both kinds.
    lxc_resp = _proxmox_get(f"/nodes/{node}/lxc/{vmid}/status/current")
    if "error" not in lxc_resp:
        return _enrich_with_guest_mem(_format_status(lxc_resp, "lxc", vmid),
                                       vmid)

    qemu_resp = _proxmox_get(f"/nodes/{node}/qemu/{vmid}/status/current")
    if "error" not in qemu_resp:
        return _enrich_with_guest_mem(_format_status(qemu_resp, "qemu", vmid),
                                       vmid)

    if qemu_resp.get("error") == "not_found":
        return {"error": f"vmid {vmid} not found on node {node!r} "
                          f"(neither lxc nor qemu)"}
    return qemu_resp


# -------------------------------------------------------------------
# Home Assistant client
# -------------------------------------------------------------------
_HA_URL = os.getenv("HA_URL", "").rstrip("/")
_HA_TOKEN = os.getenv("HA_TOKEN", "")


def _ha_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_HA_TOKEN}",
        "Content-Type": "application/json",
    }


def _ha_request(method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
    if not (_HA_URL and _HA_TOKEN):
        return {"error": "HA env vars not set (HA_URL, HA_TOKEN)."}
    try:
        r = httpx.request(
            method,
            f"{_HA_URL}{path}",
            headers=_ha_headers(),
            json=body,
            timeout=10.0,
        )
        if r.status_code == 401:
            return {"error": "HA auth failed — token invalid or revoked."}
        if r.status_code == 404:
            return {"error": "not_found", "path": path}
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}
    except httpx.ConnectError as e:
        return {"error": f"cannot reach HA at {_HA_URL} — {e}"}
    except httpx.HTTPError as e:
        return {"error": f"HA http error: {e}"}


def _ha_get(path: str) -> Dict[str, Any]:
    return _ha_request("GET", path)


def get_ha_entity(entity_id: str) -> dict:
    """Get current state of a Home Assistant entity."""
    resp = _ha_get(f"/api/states/{entity_id}")
    if isinstance(resp, dict) and "error" in resp:
        return resp
    if isinstance(resp, dict):
        resp.pop("context", None)
    return resp


def ha_all_states() -> List[Dict[str, Any]]:
    """Internal: full /api/states dump (every entity with attributes).

    Public list_ha_entities trims attributes to keep responses small;
    discovery features that need attributes (device_class, unit_of_measurement)
    should use this directly. Returns [] on error so callers can iterate.
    """
    resp = _ha_get("/api/states")
    if isinstance(resp, dict) and "error" in resp:
        return []
    return resp if isinstance(resp, list) else []


def get_ha_history(entity_id: str, hours_back: int) -> Optional[List[Dict[str, Any]]]:
    """Fetch state history for ONE entity over the last `hours_back` hours.

    Uses `/api/history/period/<start>?filter_entity_id=<id>&minimal_response`.
    minimal_response strips intermediate state changes — we get just the
    first and last for each interval, which is all we need for energy
    delta calculation against accumulating kWh sensors.

    Returns a list of state objects in time order, or None on error /
    no data. Each state object has at least: {state, last_changed}.
    """
    import datetime as _dt

    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(hours=hours_back)
    start_iso = start.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_iso = end.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # significant_changes_only=false → get EVERY state change, including
    # the rapid drop-to-zero at a counter reset. The default behavior
    # collapses "uninteresting" transitions and can hide the reset event
    # entirely, which would silently break our reset-aware delta logic.
    path = (f"/api/history/period/{start_iso}"
            f"?filter_entity_id={entity_id}"
            f"&minimal_response"
            f"&significant_changes_only=false"
            f"&end_time={end_iso}")
    resp = _ha_get(path)
    if isinstance(resp, dict) and "error" in resp:
        return None
    # Response shape: [[state1, state2, ...]] — list-of-lists, one inner
    # list per entity. We requested one entity so we take [0].
    if not isinstance(resp, list) or not resp or not resp[0]:
        return None
    return resp[0]


def list_ha_entities(domain: Optional[str] = None, limit: int = 50) -> dict:
    """List HA entities, optionally filtered to one domain."""
    resp = _ha_get("/api/states")
    if isinstance(resp, dict) and "error" in resp:
        return resp
    items = resp if isinstance(resp, list) else []
    if domain:
        items = [e for e in items if e.get("entity_id", "").startswith(f"{domain}.")]
    items = items[:limit]
    return {
        "domain": domain or "all",
        "count": len(items),
        "entities": [
            {
                "entity_id": e["entity_id"],
                "state": e.get("state"),
                "friendly_name": e.get("attributes", {}).get("friendly_name"),
            }
            for e in items
        ],
    }


# -------------------------------------------------------------------
# Telegram client
# -------------------------------------------------------------------
_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_TG_BASE = f"https://api.telegram.org/bot{_TG_TOKEN}" if _TG_TOKEN else ""


def send_telegram_alert(
    message: str,
    parse_mode: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> dict:
    """Send a Telegram message via the operator's bot."""
    if not _TG_TOKEN:
        return {"error": "TELEGRAM_BOT_TOKEN not set."}
    target = chat_id or _TG_CHAT_ID
    if not target:
        return {"error": "no TELEGRAM_CHAT_ID and no chat_id arg given."}
    if len(message) > 4000:
        message = message[:4000] + "\n…[truncated]"

    payload: Dict[str, Any] = {"chat_id": target, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = httpx.post(f"{_TG_BASE}/sendMessage", json=payload, timeout=10.0)
        body = r.json()
        if not body.get("ok"):
            return {"error": f"telegram api: {body.get('description', body)}"}
        return {
            "sent": True,
            "message_id": body["result"]["message_id"],
            "chat_id": target,
        }
    except httpx.HTTPError as e:
        return {"error": f"telegram http error: {e}"}


# -------------------------------------------------------------------
# Approval gate (Step 3)
# -------------------------------------------------------------------
APPROVAL_TIMEOUT_S = int(os.getenv("APPROVAL_TIMEOUT_S", "120"))


def _tg_latest_update_id() -> Optional[int]:
    """Get the most recent update_id without consuming history.
    Used to skip historical updates when starting a fresh approval wait."""
    try:
        r = httpx.get(f"{_TG_BASE}/getUpdates", params={"offset": -1, "limit": 1},
                      timeout=5.0)
        results = r.json().get("result", [])
        return results[0]["update_id"] if results else None
    except Exception:
        return None


def request_telegram_approval(
    action: str,
    details: str,
    timeout_s: Optional[int] = None,
) -> dict:
    """Ask the operator via Telegram before performing a destructive action.

    Sends a message with Approve/Deny buttons, polls long-poll getUpdates
    until the user clicks one or `timeout_s` elapses. Edits the original
    message to reflect the final decision (so the chat history is the
    audit trail).

    Args:
        action: short label, e.g. "restart_lxc(node=pve, vmid=102)"
        details: human-readable rationale and current state. The text shown
            to you in Telegram. Keep under ~2000 chars.
        timeout_s: override APPROVAL_TIMEOUT_S env default.

    Returns:
        {"decision": "approved" | "denied" | "timeout" | "error",
         "by": "<telegram-username>",
         "message_id": <int>,
         "elapsed_s": <float>}
    """
    if not (_TG_TOKEN and _TG_CHAT_ID):
        return {"decision": "error", "error": "Telegram not configured."}

    timeout = timeout_s if timeout_s is not None else APPROVAL_TIMEOUT_S
    token = uuid.uuid4().hex[:10]  # correlate callback to THIS request

    text = (
        f"⚠️  APPROVAL NEEDED\n"
        f"\n"
        f"Action: {action}\n"
        f"\n"
        f"{details}\n"
        f"\n"
        f"You have {timeout}s to respond."
    )
    payload = {
        "chat_id": _TG_CHAT_ID,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"a:{token}"},
                {"text": "❌ Deny",    "callback_data": f"d:{token}"},
            ]],
        },
    }

    # Step BEFORE sending: snapshot the latest update_id so we ignore
    # historical callbacks (e.g. from prior approval requests left unclicked).
    skip_before = _tg_latest_update_id()
    offset = (skip_before + 1) if skip_before is not None else None

    try:
        r = httpx.post(f"{_TG_BASE}/sendMessage", json=payload, timeout=10.0)
        body = r.json()
        if not body.get("ok"):
            return {"decision": "error", "error": str(body.get("description"))}
        message_id = body["result"]["message_id"]
    except httpx.HTTPError as e:
        return {"decision": "error", "error": f"send failed: {e}"}

    _audit("approval_requested", {
        "token": token, "action": action, "message_id": message_id,
        "timeout_s": timeout,
    })

    # Long-poll for the callback
    start = time.time()
    decision: Optional[str] = None
    user: Optional[str] = None

    while time.time() - start < timeout:
        remaining = max(1, int(timeout - (time.time() - start)))
        long_poll = min(remaining, 25)  # Telegram caps at 50; 25 is conservative
        params: Dict[str, Any] = {
            "timeout": long_poll,
            "allowed_updates": '["callback_query"]',
        }
        if offset is not None:
            params["offset"] = offset
        try:
            r = httpx.get(f"{_TG_BASE}/getUpdates", params=params,
                          timeout=long_poll + 10)
            updates = r.json().get("result", [])
        except httpx.HTTPError:
            continue  # transient network blip — keep polling

        for u in updates:
            offset = u["update_id"] + 1
            cb = u.get("callback_query")
            if not cb:
                continue
            data = cb.get("data", "")
            if not data.endswith(f":{token}"):
                # Some other approval's callback — leave it for that handler
                continue
            decision = "approved" if data.startswith("a:") else "denied"
            user = cb.get("from", {}).get("username") \
                or cb.get("from", {}).get("first_name", "unknown")

            # Acknowledge — removes the loading spinner on the button
            try:
                httpx.post(f"{_TG_BASE}/answerCallbackQuery", json={
                    "callback_query_id": cb["id"],
                    "text": f"Recorded: {decision}",
                }, timeout=5.0)
            except httpx.HTTPError:
                pass
            break

        if decision is not None:
            break

    elapsed = round(time.time() - start, 1)

    if decision is None:
        decision = "timeout"
        user = None

    # Edit the original message so the chat is the audit trail
    marker = {"approved": "✅ APPROVED", "denied": "❌ DENIED",
              "timeout": "⏱ TIMEOUT"}[decision]
    suffix = f"\n\n→ {marker}"
    if user:
        suffix += f" by @{user}"
    suffix += f" ({elapsed}s)"
    try:
        httpx.post(f"{_TG_BASE}/editMessageText", json={
            "chat_id": _TG_CHAT_ID,
            "message_id": message_id,
            "text": text + suffix,
        }, timeout=5.0)
    except httpx.HTTPError:
        pass

    _audit("approval_decision", {
        "token": token, "action": action, "decision": decision,
        "by": user, "elapsed_s": elapsed, "message_id": message_id,
    })

    return {
        "decision": decision,
        "by": user,
        "message_id": message_id,
        "elapsed_s": elapsed,
    }


# -------------------------------------------------------------------
# Destructive tools — TWO variants each:
#
#   *_raw : performs the action with NO gate. Use only when approval
#           is enforced by the caller (e.g. agent_v5's policy_node).
#   <name>: wraps *_raw with request_telegram_approval first. Use in
#           simple agents (v3, v4) that don't have a graph-level gate.
#
# This split avoids double-prompting in agents that gate at the graph
# layer. Pick ONE variant per agent — never mix them on the same call.
# -------------------------------------------------------------------
def restart_lxc_raw(node: str, vmid: int) -> dict:
    """Restart an LXC container on Proxmox. NO APPROVAL CHECK.

    Caller is responsible for gating. Audited regardless.
    """
    node = node or _PROXMOX_DEFAULT_NODE
    resp = _proxmox_request("POST", f"/nodes/{node}/lxc/{vmid}/status/reboot")
    if "error" in resp:
        _audit("restart_lxc_failed", {"vmid": vmid, "error": resp["error"]})
        return {"executed": False, "reason": "proxmox_error", **resp}
    upid = resp.get("data")
    _audit("restart_lxc_executed", {"vmid": vmid, "upid": upid})
    return {"executed": True, "vmid": vmid, "upid": upid}


def restart_lxc(node: str, vmid: int) -> dict:
    """Restart an LXC container with built-in Telegram approval gate."""
    node = node or _PROXMOX_DEFAULT_NODE
    status = check_proxmox_status(node, vmid)
    details = (
        f"Node:  {node}\n"
        f"VMID:  {vmid}\n"
        f"Kind:  {status.get('kind', '?')}\n"
        f"Name:  {status.get('name', '?')}\n"
        f"Current state: {status.get('status', '?')}, "
        f"mem={status.get('mem_pct', '?')}%, "
        f"uptime={status.get('uptime_h', '?')}h"
    )
    decision = request_telegram_approval(
        action=f"restart_lxc(node={node}, vmid={vmid})",
        details=details,
    )
    if decision["decision"] != "approved":
        return {
            "executed": False,
            "reason": decision["decision"],
            "by": decision.get("by"),
            "vmid": vmid,
        }
    result = restart_lxc_raw(node, vmid)
    if result.get("executed"):
        result["approved_by"] = decision.get("by")
    return result


def call_ha_service_raw(
    domain: str,
    service: str,
    entity_id: Optional[str] = None,
    service_data: Optional[dict] = None,
) -> dict:
    """Call a Home Assistant service. NO APPROVAL CHECK.

    Caller is responsible for gating.
    """
    body: Dict[str, Any] = {}
    if entity_id:
        body["entity_id"] = entity_id
    if service_data:
        body.update(service_data)
    resp = _ha_request("POST", f"/api/services/{domain}/{service}", body=body)
    if isinstance(resp, dict) and "error" in resp:
        _audit("call_ha_service_failed",
               {"service": f"{domain}.{service}", "error": resp["error"]})
        return {"executed": False, "reason": "ha_error", **resp}
    _audit("call_ha_service_executed",
           {"service": f"{domain}.{service}", "args": body})
    return {
        "executed": True,
        "service": f"{domain}.{service}",
        "args": body,
    }


def call_ha_service(
    domain: str,
    service: str,
    entity_id: Optional[str] = None,
    service_data: Optional[dict] = None,
) -> dict:
    """Call a HA service with built-in Telegram approval gate.

    Examples:
        call_ha_service("light", "turn_off", "light.kitchen")
        call_ha_service("climate", "set_temperature",
                        "climate.living_room", {"temperature": 21})
    """
    body_preview: Dict[str, Any] = {}
    if entity_id:
        body_preview["entity_id"] = entity_id
    if service_data:
        body_preview.update(service_data)
    pretty_args = json.dumps(body_preview, ensure_ascii=False) \
        if body_preview else "(no args)"
    details = f"HA Service: {domain}.{service}\nArgs: {pretty_args}"
    decision = request_telegram_approval(
        action=f"call_ha_service({domain}.{service})",
        details=details,
    )
    if decision["decision"] != "approved":
        return {
            "executed": False,
            "reason": decision["decision"],
            "by": decision.get("by"),
            "service": f"{domain}.{service}",
        }
    result = call_ha_service_raw(domain, service, entity_id, service_data)
    if result.get("executed"):
        result["approved_by"] = decision.get("by")
    return result
