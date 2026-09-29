"""
esphome_monitor.py — online/offline watch for the ESPHome fleet.

Sentinel can't reach the ESP boards itself: they sit on the IoT VLAN
(a separate VLAN), which is not routable from this LXC. Home Assistant
can — it holds a native-API session to every adopted node — so HA is the
probe. When a node drops, the ESPHome integration flips ALL of that
device's entities to `unavailable` at the same instant; while it is
connected, at least one entity has a real state. That is the same
ONLINE/OFFLINE the ESPHome dashboard shows, read over the REST API
Sentinel already uses.

Which devices alert is catalog data (catalog.yaml `esphome:`):
    monitor: true    a transition online -> offline (after the grace
                     period) or offline -> online sends ONE Telegram
                     message. A device that stays offline stays quiet.
    monitor: false   parked — listed in the digest, never alerts. Its
                     state is not tracked, so re-enabling it starts from a
                     fresh baseline instead of firing a stale transition.
A node HA knows but the catalog doesn't is reported as `untracked`
(drift), never alerted on.

The two false-alarm traps this is built around:
  - an OTA flash or reboot drops a node for 10-60 s, and an HA restart
    marks every node unavailable until the integration reconnects — so a
    node only counts as offline after `offline_grace_s` of continuous
    unavailability, measured from HA's own last_changed (not our polls);
  - HA being down says nothing about the ESPs. That run is skipped and
    nothing is recorded (the reachability sweep already pages for HA),
    so HA coming back doesn't produce a burst of fake "back online".

Deep-sleep nodes can't be watched this way: HA keeps their entities
"available" with stale values between wake-ups. Keep them parked.

CLI:
    uv run python esphome_monitor.py            # table, read-only
    uv run python esphome_monitor.py --json
    uv run python esphome_monitor.py --alert    # timer mode: track + notify

Imported (wired as the check_esphome_devices tool):
    from esphome_monitor import scan_esphome

Exit codes follow the monitor contract: 0 every monitored node online,
2 at least one monitored node offline/missing (it was alerted on its
transition), 1 = the check itself broke.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

import tools
from catalog import load_catalog

VAR_DIR = Path(__file__).parent / "var"
STATE_FILE = VAR_DIR / "esphome_state.json"

# States a monitored device can be *recorded* in. "dropping" (unavailable,
# but not for the full grace period yet) and "unknown" (no entities with a
# state) are undecided: they never start or end an incident.
_SETTLED = ("online", "offline", "missing")


class HAUnavailable(Exception):
    """HA could not be asked (down, restarting, timed out) — this says
    nothing about the ESP devices, so the run is skipped, not failed."""


# One /api/template round-trip: every ESPHome entity's state, plus the
# registry attributes of every ESPHome device. Entities without a state
# object (disabled in HA) are skipped — states('x') would call them
# "unknown" and they'd count as a live entity.
_TEMPLATE = (
    "{%- set ns = namespace(ents=[], devs=[]) -%}"
    "{%- for e in integration_entities('esphome') -%}"
    "{%- set s = states[e] -%}{%- set did = device_id(e) -%}"
    "{%- if s and did -%}"
    "{%- set ns.ents = ns.ents + [[did, s.state, s.last_changed.isoformat()]] -%}"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{%- for did in integration_entities('esphome') | map('device_id')"
    " | reject('none') | unique -%}"
    "{%- set ns.devs = ns.devs + [{'id': did,"
    " 'name': device_attr(did,'name_by_user') or device_attr(did,'name'),"
    " 'model': device_attr(did,'model'),"
    " 'sw': device_attr(did,'sw_version'),"
    " 'conn': device_attr(did,'connections') | map('list') | list}] -%}"
    "{%- endfor -%}"
    "{{ {'entities': ns.ents, 'devices': ns.devs} | tojson }}"
)


def _fetch_raw() -> Dict[str, Any]:
    """Ask HA for the ESPHome entities + devices.

    Not tools._ha_template(): that folds every failure into one error
    string, and this monitor has to tell "HA is down" (skip quietly) apart
    from "our token or query is broken" (page — the check is blind).
    """
    if not (tools._HA_URL and tools._HA_TOKEN):
        raise RuntimeError("HA env vars not set (HA_URL, HA_TOKEN).")
    try:
        r = httpx.post(f"{tools._HA_URL}/api/template",
                       headers=tools._ha_headers(),
                       json={"template": _TEMPLATE}, timeout=20.0)
    except httpx.TransportError as e:
        raise HAUnavailable(f"{type(e).__name__}: {e}") from e
    if r.status_code in (502, 503, 504):
        raise HAUnavailable(f"HA answered {r.status_code}")
    if r.status_code == 401:
        raise RuntimeError("HA auth failed — token invalid or revoked.")
    r.raise_for_status()
    return json.loads(r.text)


def _mac_of(connections: Any) -> Optional[str]:
    for pair in connections or []:
        if len(pair) == 2 and pair[0] == "mac":
            return str(pair[1]).lower()
    return None


def _ha_devices(raw: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """Collapse per-entity states into one availability row per device."""
    by_dev: Dict[str, List[Tuple[str, datetime]]] = {}
    for did, state, changed in raw.get("entities", []):
        by_dev.setdefault(did, []).append((state, datetime.fromisoformat(changed)))

    rows = []
    for d in raw.get("devices", []):
        ents = by_dev.get(d["id"], [])
        up = sum(1 for state, _ in ents if state != "unavailable")
        row: Dict[str, Any] = {
            "device_id": d["id"],
            "ha_name": d.get("name") or d["id"],
            "mac": _mac_of(d.get("conn")),
            "model": d.get("model"),
            # "2026.9.0 (2026-09-27 09:06:27 +0200)" -> "2026.9.0"
            "firmware": (d.get("sw") or "").split(" ")[0] or None,
            "entities": len(ents),
            "entities_up": up,
        }
        if up:
            row["ha_state"] = "online"
        elif ents:
            # The node took every entity down at once; the newest
            # last_changed is the moment it dropped.
            since = max(t for _, t in ents)
            row["ha_state"] = "unavailable"
            row["unavailable_since"] = since.isoformat()
            row["unavailable_s"] = max(0, int((now - since).total_seconds()))
        else:
            row["ha_state"] = "no_entities"
        rows.append(row)
    return rows


def _status(row: Dict[str, Any], grace_s: int) -> str:
    if row["ha_state"] == "online":
        return "online"
    if row["ha_state"] == "unavailable":
        return "offline" if row["unavailable_s"] >= grace_s else "dropping"
    return "unknown"


def scan_esphome(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Online/offline for every catalogued ESPHome device, plus drift.

    Raises HAUnavailable when HA can't be asked. Returns a digest:
    {grace_s, total, monitored, online, problems, parked, untracked, devices}.
    """
    now = now or datetime.now(timezone.utc)
    cfg = load_catalog().esphome
    ha_rows = _ha_devices(_fetch_raw(), now)
    by_mac = {r["mac"]: r for r in ha_rows if r["mac"]}
    by_name = {r["ha_name"].lower(): r for r in ha_rows}

    devices: List[Dict[str, Any]] = []
    matched = set()
    for entry in cfg.devices:
        mac = entry.mac.lower() if entry.mac else None
        row = (by_mac.get(mac) if mac else None) or by_name.get(entry.name.lower())
        base = {"name": entry.name, "key": mac or entry.name.lower(),
                "monitor": entry.monitor, "ip": entry.ip, "mac": mac}
        if entry.note:
            base["note"] = entry.note
        if row is None:
            devices.append({**base, "status": "missing",
                            "hint": "in catalog.yaml but not in Home Assistant's "
                                    "ESPHome integration (deleted from HA?)"})
            continue
        matched.add(row["device_id"])
        devices.append({**row, **base, "mac": mac or row["mac"],
                        "status": _status(row, cfg.offline_grace_s)})

    untracked = [
        {"ha_name": r["ha_name"], "mac": r["mac"], "status": _status(r, cfg.offline_grace_s),
         "hint": "add it to catalog.yaml esphome.devices to watch it"}
        for r in ha_rows if r["device_id"] not in matched
    ]
    monitored = [d for d in devices if d["monitor"]]
    return {
        "grace_s": cfg.offline_grace_s,
        "total": len(devices),
        "monitored": len(monitored),
        "online": sum(1 for d in monitored if d["status"] == "online"),
        "problems": [d["name"] for d in monitored if d["status"] in ("offline", "missing")],
        "parked": [d["name"] for d in devices if not d["monitor"]],
        "untracked": untracked,
        "devices": devices,
    }


# ----------------------------------------------------------------------
# Transition tracking (timer mode)
# ----------------------------------------------------------------------
def track_transitions(data: Dict[str, Any], state: Dict[str, Any],
                      now: datetime) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Compare this sweep with the last recorded state of each MONITORED
    device. Returns (events, new_state); pure, so it is tested directly.

    First sight of a device records a silent baseline — that is what keeps
    an already-offline device quiet. Undecided statuses (dropping/unknown)
    carry the previous record forward untouched.
    """
    events: List[Dict[str, Any]] = []
    new_state: Dict[str, Any] = {}
    for d in data["devices"]:
        if not d["monitor"]:
            continue
        key, cur = d["key"], d["status"]
        prev = state.get(key)
        if cur not in _SETTLED:
            if prev:
                new_state[key] = prev
            continue
        since = d.get("unavailable_since") if cur == "offline" else now.isoformat()
        if prev is None:
            new_state[key] = {"status": cur, "since": since}
        elif prev["status"] == cur:
            new_state[key] = prev
        else:
            new_state[key] = {"status": cur, "since": since}
            events.append({"device": d, "from": prev["status"], "to": cur,
                           "prev_since": prev.get("since")})
    return events, new_state


def _dur(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} days"


def _hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def format_alert(events: List[Dict[str, Any]], data: Dict[str, Any],
                 now: datetime) -> str:
    lines = ["📟 ESPHome devices"]
    for e in events:
        d = e["device"]
        where = f" · {d['ip']}" if d.get("ip") else ""
        if e["to"] == "offline":
            lines.append(f"🔴 {d['name']} went OFFLINE — unavailable in Home "
                         f"Assistant since {_hhmm(d['unavailable_since'])}{where}")
        elif e["to"] == "online":
            gone = ""
            if e.get("prev_since") and e["from"] == "offline":
                secs = (now - datetime.fromisoformat(e["prev_since"])).total_seconds()
                gone = f" — was offline {_dur(secs)}"
            lines.append(f"🟢 {d['name']} is back ONLINE{gone}")
        else:
            lines.append(f"⚪ {d['name']} disappeared from Home Assistant's "
                         f"ESPHome integration (deleted or re-adopted?)")

    offline_now = [d for d in data["devices"] if d["monitor"] and d["status"] == "offline"]
    went_down = any(e["to"] == "offline" for e in events)
    if went_down and data["monitored"] > 1 and len(offline_now) == data["monitored"]:
        lines.append("\nEvery monitored device is offline at once — more likely the "
                     "IoT Wi-Fi/VLAN or HA's ESPHome integration than the boards.")
    lines.append(f"\n{data['online']}/{data['monitored']} monitored devices online.")
    return "\n".join(lines)


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'STATUS':<9} {'WATCH':<7} {'NAME':<28} {'IP':<15} "
          f"{'FIRMWARE':<10} {'UP':<6} DETAIL")
    print("-" * 100)
    for d in data["devices"]:
        detail = ""
        if d.get("unavailable_s") is not None:
            detail = f"unavailable {_dur(d['unavailable_s'])}"
        elif d.get("hint"):
            detail = d["hint"]
        ents = (f"{d['entities_up']}/{d['entities']}"
                if d.get("entities") is not None else "-")
        print(f"{d['status']:<9} {'yes' if d['monitor'] else 'parked':<7} "
              f"{d['name']:<28} {str(d.get('ip') or '-'):<15} "
              f"{str(d.get('firmware') or '-'):<10} {ents:<6} {detail}")
    for u in data["untracked"]:
        print(f"{u['status']:<9} {'-':<7} {u['ha_name']:<28} (untracked — not in catalog.yaml)")


def main() -> int:
    ap = argparse.ArgumentParser(description="HomelabSentinel ESPHome device watch")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--alert", action="store_true",
                    help="track state and send Telegram on online/offline transitions")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now(timezone.utc)
    try:
        data = scan_esphome(now)
    except HAUnavailable as e:
        print(f"Home Assistant unreachable ({e}) — run skipped, nothing recorded. "
              f"The reachability sweep pages for HA itself.")
        return 0

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif args.alert:
        # timer mode runs every 2 min: one journal line, not a table
        print(f"ESPHome: {data['online']}/{data['monitored']} monitored online"
              + (f", problems: {', '.join(data['problems'])}" if data["problems"] else "")
              + (f", parked: {len(data['parked'])}" if data["parked"] else "")
              + (f", untracked: {len(data['untracked'])}" if data["untracked"] else ""))
    else:
        print(f"\nESPHome devices — {data['online']}/{data['monitored']} monitored online, "
              f"{len(data['parked'])} parked, grace {data['grace_s']}s\n")
        _print_table(data)

    if args.alert:
        events, new_state = track_transitions(data, _load_state(), now)
        if events:
            res = tools.send_telegram_alert(format_alert(events, data, now))
            print(f"Alert sent: {res}")
            if "error" in res:
                # keep the old state so the next run retries this transition
                return 1
        _save_state(new_state)

    return 2 if data["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
