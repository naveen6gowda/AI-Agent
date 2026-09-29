"""
alert_state.py — page on the change, not on every run.

The monitors run every 5-15 minutes and used to send their alert on EVERY
run while something stayed broken: the 2026-09-02 network outage produced a
reachability alert every 5 minutes and a "Docker check broke" page every
10 minutes for the same incident. This module turns a monitor's list of
current findings into transitions against the last recorded state:

    new finding       -> one "down" message (after `confirm_s`, if set)
    still broken      -> silence, plus a reminder every remind_after_h
    gone              -> one "recovered" message with the outage length

Criticality rides on each finding and follows catalog.yaml's definitions:
critical pages any time; a message that carries only HIGH events is sent
silently (no notification sound) inside alerting.quiet_hours.

Delivery is at-least-once: state is saved only after Telegram accepted
the message, so a failed send is retried on the next run instead of lost
(a WAN outage can't swallow the alert).

Used by reachability.py, guest_monitor.py and docker_tools.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

STATE_DIR = Path(__file__).parent / "var" / "alerts"


@dataclass(frozen=True)
class Finding:
    """Something broken right now. `key` must be stable across runs."""
    key: str
    label: str
    severity: str = "high"          # "critical" | "high"
    detail: str = ""


@dataclass(frozen=True)
class Event:
    kind: str                       # "down" | "still_down" | "recovered"
    label: str
    severity: str
    detail: str
    since: datetime


def in_quiet_hours(now: datetime, quiet_hours: Sequence[int]) -> bool:
    if len(quiet_hours) != 2 or quiet_hours[0] == quiet_hours[1]:
        return False
    start, end = quiet_hours
    h = now.astimezone().hour
    return start <= h < end if start < end else (h >= start or h < end)


def evaluate(state: Dict[str, Any], findings: List[Finding], now: datetime,
             remind_after_h: float = 0.0, confirm_s: float = 0.0,
             keep: Optional[Callable[[str], bool]] = None,
             ) -> Tuple[List[Event], Dict[str, Any]]:
    """Diff the current findings against the recorded state. Pure.

    confirm_s: a finding must have been present this long before it pages
        (a single failed probe that heals by the next run stays silent).
    keep: keys absent from `findings` for which keep(key) is True are
        carried forward untouched instead of counting as recovered — for a
        run that could only see part of its world (Portainer unreachable
        says nothing about the containers behind it).
    """
    events: List[Event] = []
    new_state: Dict[str, Any] = {}
    for f in findings:
        entry = dict(state.get(f.key) or {"since": now.isoformat(), "notified": None})
        entry.update(label=f.label, severity=f.severity, detail=f.detail)
        since = datetime.fromisoformat(entry["since"])
        notified = entry.get("notified")
        if notified is None:
            if (now - since).total_seconds() >= confirm_s:
                events.append(Event("down", f.label, f.severity, f.detail, since))
                entry["notified"] = now.isoformat()
        elif remind_after_h and now - datetime.fromisoformat(notified) >= timedelta(hours=remind_after_h):
            events.append(Event("still_down", f.label, f.severity, f.detail, since))
            entry["notified"] = now.isoformat()
        new_state[f.key] = entry

    for key, entry in state.items():
        if key in new_state:
            continue
        if keep and keep(key):
            new_state[key] = entry
        elif entry.get("notified"):
            events.append(Event("recovered", entry.get("label", key),
                                entry.get("severity", "high"), entry.get("detail", ""),
                                datetime.fromisoformat(entry["since"])))
        # never notified (unconfirmed blip, or healed before it paged): drop quietly
    return events, new_state


def human_duration(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{h // 24} days"


def render(header: str, events: List[Event], now: datetime) -> str:
    lines = [header]
    for e in events:
        took = human_duration((now - e.since).total_seconds())
        why = f" — {e.detail}" if e.detail else ""
        if e.kind == "down":
            icon = "🔴" if e.severity == "critical" else "🟠"
            lines.append(f"{icon} {e.label} DOWN{why}")
        elif e.kind == "still_down":
            lines.append(f"⏳ {e.label} still down after {took}{why}")
        else:
            lines.append(f"🟢 {e.label} recovered after {took}")
    return "\n".join(lines)


def _load(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def notify(monitor: str, header: str, findings: List[Finding],
           now: Optional[datetime] = None, confirm_s: float = 0.0,
           keep: Optional[Callable[[str], bool]] = None) -> Dict[str, Any]:
    """Evaluate, send ONE Telegram message for all transitions, persist.

    Returns {"events": n, "sent": bool, "silent": bool} or, when Telegram
    refused the message, {"events": n, "sent": False, "error": ...} with
    the old state kept so the next run retries.
    """
    now = now or datetime.now(timezone.utc)
    try:
        from catalog import load_catalog
        cfg = load_catalog().alerting
        quiet, remind = cfg.quiet_hours, cfg.remind_after_h
    except Exception:
        quiet, remind = [23, 7], 6.0      # catalog broken: keep paging sanely

    path = STATE_DIR / f"{monitor}.json"
    events, new_state = evaluate(_load(path), findings, now,
                                 remind_after_h=remind, confirm_s=confirm_s, keep=keep)
    silent = False
    if events:
        silent = (in_quiet_hours(now, quiet)
                  and all(e.severity != "critical" for e in events))
        from tools import send_telegram_alert
        res = send_telegram_alert(render(header, events, now), silent=silent)
        if "error" in res:
            return {"events": len(events), "sent": False, "error": res["error"]}
    _save(path, new_state)
    return {"events": len(events), "sent": bool(events), "silent": silent}
