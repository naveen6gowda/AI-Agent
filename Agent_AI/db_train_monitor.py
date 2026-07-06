"""
Commute Monitor — S2 S-Bahn + MVV RegionalBus 700 from YourVillage.

Watches the weekday-morning commute (Mon-Fri, 06:00-09:00 by default):
  * S2 S-Bahn departures at YourVillage toward the city/Munich
    (trains arriving from LineTerminus)
  * RegionalBus 700 toward City (S) from the same station stop

Cancellations and delays >= TRANSIT_MIN_DELAY_ANNOUNCE minutes are announced
on the Echo via Home Assistant (voice.speak_on_alexa, "announce" chime).
Each disruption is announced ONCE; it is re-announced only if it worsens
(delay grows by >= TRANSIT_REANNOUNCE_DELTA minutes, or turns into a
cancellation). Announcement state lives in TRANSIT_STATE_FILE.

Data sources (all free, no API key — verified 2026-07-02):
  * Primary: MVV EFA departure monitor (efa.mvv-muenchen.de, rapidJSON).
    One call covers BOTH the S2 and bus 700 at the same stop. Timestamps
    are true UTC ("...Z") and must be converted to Europe/Berlin.
  * Fallback: MVG v3 departures API (www.mvg.de) — S-Bahn only; the bus
    is unmonitored while EFA is down (logged).
  * v6.db.transport.rest (community DB HAFAS proxy) is NOT used: requests
    from this network hang/503 (confirmed with curl across HTTP/1.1 and
    HTTP/2, multiple queries, two vantage points).

CLI usage:
    uv run python db_train_monitor.py                    # single check (print only)
    uv run python db_train_monitor.py --alert            # announce on Alexa (dedup applies)
    uv run python db_train_monitor.py --daemon           # continuous polling every 5 min
    uv run python db_train_monitor.py --force            # ignore the commute time window

Imported usage (wired as @tool in agent_v5_approval.py and as the "train"
voice intent in voice.py):
    from db_train_monitor import check_commute, next_departures
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration — read from .env or use defaults
# ---------------------------------------------------------------------------

def _env_list(name: str, default: str) -> List[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


# YourVillage (Kr the city) station stop — serves both S2 and bus 700.
# NearbyVillage (bus only) would be de:00000:0003; LineTerminus is de:00000:0001.
STOP_ID = os.getenv("TRANSIT_STOP_ID", "de:00000:0002")
STOP_SPOKEN_NAME = os.getenv("TRANSIT_STOP_NAME", "YourVillage")

TRAIN_LINE = os.getenv("TRANSIT_TRAIN_LINE", "S2")
TRAIN_DESTINATIONS = _env_list("TRANSIT_TRAIN_DESTINATIONS", "City-Terminus,City Central")
BUS_LINE = os.getenv("TRANSIT_BUS_LINE", "700")
BUS_DESTINATIONS = _env_list("TRANSIT_BUS_DESTINATIONS", "City (S)")

MIN_DELAY_ANNOUNCE = int(os.getenv("TRANSIT_MIN_DELAY_ANNOUNCE", "5"))
REANNOUNCE_DELTA = int(os.getenv("TRANSIT_REANNOUNCE_DELTA", "5"))
STATE_FILE = os.getenv("TRANSIT_STATE_FILE", "/opt/sentinel/var/transit_state.json")
STATE_MAX_AGE_HOURS = 6

POLL_INTERVAL_MINUTES = 5

# Only alert during the actual morning commute — avoids spurious Alexa
# announcements about evening/weekend service that isn't relevant.
COMMUTE_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri
COMMUTE_WINDOW_START = os.getenv("TRANSIT_WINDOW_START", "06:00")
COMMUTE_WINDOW_END = os.getenv("TRANSIT_WINDOW_END", "09:00")

LOCAL_TZ = ZoneInfo("Europe/Berlin")


def _in_commute_window(now: datetime) -> bool:
    """True if `now` falls within the configured weekday morning commute window."""
    if now.weekday() not in COMMUTE_DAYS:
        return False
    start = datetime.strptime(COMMUTE_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(COMMUTE_WINDOW_END, "%H:%M").time()
    return start <= now.time() <= end


def _spoken_time(dt_local: datetime) -> str:
    """7:13 rather than 07:13 — reads more naturally on the Echo."""
    return f"{dt_local.hour}:{dt_local.minute:02d}"


# ---------------------------------------------------------------------------
# MVV EFA departure monitor (primary) — no key needed, covers S-Bahn + bus
# ---------------------------------------------------------------------------

EFA_URL = "https://efa.mvv-muenchen.de/ng/XML_DM_REQUEST"


def get_efa_departures(stop_id: str = STOP_ID, limit: int = 40) -> List[Dict[str, Any]]:
    """Fetch raw stopEvents from the MVV EFA departure monitor.

    Returns [] on any failure — the caller falls back to MVG for the train."""
    params = {
        "outputFormat": "rapidJSON",
        "type_dm": "stop",
        "name_dm": stop_id,
        "mode": "direct",
        "useRealtime": 1,
        "limit": limit,
    }
    try:
        r = httpx.get(EFA_URL, params=params, timeout=15)
        if r.status_code != 200:
            return []
        events = r.json().get("stopEvents", [])
        return events if isinstance(events, list) else []
    except Exception:
        return []


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str, max_len: int = 120) -> str:
    """EFA 'infos' content is raw HTML — reduce to a short spoken clause."""
    text = _TAG_RE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    i = cut.rfind(" ")
    return (cut[:i] if i > max_len * 0.5 else cut).rstrip(".,;: ") + "."


def _parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    """EFA timestamps look like '2026-07-03T04:13:00Z' and are true UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_efa_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an EFA stopEvent to the shared departure-info shape."""
    tl = ev.get("transportation") or {}
    line = str(tl.get("number") or tl.get("disassembledName") or "")
    destination = str((tl.get("destination") or {}).get("name") or "")
    product = str((tl.get("product") or {}).get("name") or "")

    planned = _parse_iso_utc(ev.get("departureTimePlanned"))
    estimated = _parse_iso_utc(ev.get("departureTimeEstimated"))
    delay_minutes = (
        round((estimated - planned).total_seconds() / 60)
        if planned and estimated else None
    )

    info_text = ""
    for info in ev.get("infos") or []:
        if info.get("priority") not in ("veryHigh", "high"):
            continue
        links = info.get("infoLinks") or []
        raw = (links[0].get("content") if links else "") or info.get("urlText") or ""
        info_text = _strip_html(raw)
        if info_text:
            break

    if line == TRAIN_LINE or product.startswith("S-Bahn"):
        mode = "train"
    elif line == BUS_LINE or "Bus" in product:
        mode = "bus"
    else:
        mode = "other"

    return {
        "line": line,
        "destination": destination,
        "mode": mode,
        "planned_iso_utc": planned.isoformat() if planned else "",
        "planned_time": _spoken_time(planned.astimezone(LOCAL_TZ)) if planned else "",
        "actual_time": _spoken_time(estimated.astimezone(LOCAL_TZ)) if estimated else "",
        "delay_minutes": delay_minutes,
        "is_cancelled": bool(ev.get("isCancelled")),
        "is_replacement_bus": False,  # EFA has no direct SEV flag on stopEvents
        "info_text": info_text,
    }


# ---------------------------------------------------------------------------
# MVG v3 API (fallback, S-Bahn only) — https://www.mvg.de, no key needed
# ---------------------------------------------------------------------------

API_BASE = "https://www.mvg.de/api/bgw-pt/v3"


def get_departures(global_id: str = STOP_ID) -> List[Dict[str, Any]]:
    """Get upcoming S-Bahn departures from MVG, with realtime delay/cancellation."""
    url = f"{API_BASE}/departures"
    params = {"globalId": global_id, "limit": 20, "transportTypes": "SBAHN"}

    try:
        r = httpx.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_departure(dep: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an MVG departure object to the shared departure-info shape."""
    planned_ms = dep.get("plannedDepartureTime")
    realtime_ms = dep.get("realtimeDepartureTime")
    has_realtime = bool(dep.get("realtime")) and planned_ms is not None and realtime_ms is not None

    planned = (
        datetime.fromtimestamp(planned_ms / 1000, tz=timezone.utc) if planned_ms else None
    )
    estimated = (
        datetime.fromtimestamp(realtime_ms / 1000, tz=timezone.utc) if has_realtime else None
    )

    return {
        "line": dep.get("label", ""),
        "destination": dep.get("destination", ""),
        "mode": "train",
        "planned_iso_utc": planned.isoformat() if planned else "",
        "planned_time": _spoken_time(planned.astimezone(LOCAL_TZ)) if planned else "",
        "actual_time": _spoken_time(estimated.astimezone(LOCAL_TZ)) if estimated else "",
        "delay_minutes": (
            round((estimated - planned).total_seconds() / 60) if planned and estimated else None
        ),
        "is_cancelled": bool(dep.get("cancelled")),
        "is_replacement_bus": bool(dep.get("sev")),
        "info_text": "",
    }


# ---------------------------------------------------------------------------
# Filtering — which departures do we actually care about?
# ---------------------------------------------------------------------------

def _dest_matches(destination: str, wanted: List[str]) -> bool:
    d = destination.lower()
    return any(w.lower() in d for w in wanted)


def _is_watched(info: Dict[str, Any]) -> bool:
    """The operator's services only: S2 toward the city, bus 700 toward the city."""
    if info["line"] == TRAIN_LINE:
        return _dest_matches(info["destination"], TRAIN_DESTINATIONS)
    if info["line"] == BUS_LINE:
        return _dest_matches(info["destination"], BUS_DESTINATIONS)
    return False


def _fetch_watched(stop_id: str = STOP_ID) -> tuple[List[Dict[str, Any]], str]:
    """All watched departures at the stop, soonest first.

    Returns (departures, source) where source is "efa" or "mvg"."""
    events = get_efa_departures(stop_id)
    if events:
        infos = [parse_efa_event(ev) for ev in events]
        source = "efa"
    else:
        print(f"WARNING: EFA returned nothing — falling back to MVG; bus {BUS_LINE} is unmonitored this cycle")
        infos = [parse_departure(dep) for dep in get_departures(stop_id)]
        source = "mvg"
    watched = [i for i in infos if _is_watched(i)]
    watched.sort(key=lambda i: i["planned_iso_utc"])
    return watched, source


# ---------------------------------------------------------------------------
# Dedup state — announce each disruption once, re-announce only on worsening
# ---------------------------------------------------------------------------

def _state_key(info: Dict[str, Any], stop_id: str = STOP_ID) -> str:
    return f"{info['line']}|{stop_id}|{info['planned_iso_utc']}"


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"WARNING: could not save state file {STATE_FILE}: {e}")


def _prune_state(state: Dict[str, Any], now_utc: datetime) -> None:
    cutoff = now_utc - timedelta(hours=STATE_MAX_AGE_HOURS)
    for key in list(state.keys()):
        planned = _parse_iso_utc(state[key].get("planned_utc")) if isinstance(state[key], dict) else None
        if planned is None or planned < cutoff:
            del state[key]


def _should_announce(info: Dict[str, Any], state: Dict[str, Any]) -> bool:
    entry = state.get(_state_key(info)) or {}
    if info["is_cancelled"] or info["is_replacement_bus"]:
        return not entry.get("announced_cancelled")
    delay = info["delay_minutes"]
    if delay is None or delay < MIN_DELAY_ANNOUNCE:
        return False
    if not entry:
        return True
    # a train we already reported cancelled never downgrades to delay chatter
    if entry.get("announced_cancelled"):
        return False
    return delay >= entry.get("last_announced_delay", 0) + REANNOUNCE_DELTA


def _record_announcement(info: Dict[str, Any], state: Dict[str, Any],
                         now_utc: datetime) -> None:
    key = _state_key(info)
    entry = state.get(key) or {}
    entry["last_announced_delay"] = max(
        entry.get("last_announced_delay", 0), info["delay_minutes"] or 0
    )
    entry["announced_cancelled"] = bool(
        entry.get("announced_cancelled") or info["is_cancelled"] or info["is_replacement_bus"]
    )
    entry["planned_utc"] = info["planned_iso_utc"]
    entry["last_announced_at"] = now_utc.isoformat()
    state[key] = entry


# ---------------------------------------------------------------------------
# Messages — short spoken English, local times
# ---------------------------------------------------------------------------

def _service_phrase(info: Dict[str, Any]) -> str:
    if info["mode"] == "bus":
        return f"The {info['planned_time']} bus {info['line']} to the city"
    dest = "Munich City-Terminus" if "City-Terminus" in info["destination"] else "the city"
    return f"The {info['planned_time']} {info['line']} to {dest}"


def format_message(info: Dict[str, Any]) -> Optional[str]:
    """Format a departure into an Alexa-friendly spoken message. None if fine."""
    service = _service_phrase(info)

    if info["is_cancelled"]:
        msg = f"Attention. {service} from {STOP_SPOKEN_NAME} is cancelled."
        if info.get("info_text"):
            msg += f" {info['info_text']}"
        return msg

    if info["is_replacement_bus"]:
        return f"Attention. {service} from {STOP_SPOKEN_NAME} is replaced by a rail replacement bus."

    delay = info["delay_minutes"]
    if delay is not None and delay >= MIN_DELAY_ANNOUNCE:
        msg = f"Attention. {service} is delayed by {delay} minutes."
        if info["actual_time"]:
            msg += f" New departure {info['actual_time']}."
        return msg

    return None


# ---------------------------------------------------------------------------
# Main logic — importable check_commute() / next_departures() + CLI
# ---------------------------------------------------------------------------

def check_commute(stop_id: str = STOP_ID, force: bool = False) -> List[Dict[str, Any]]:
    """Current disruptions on the watched services (no dedup — pure view).

    Returns a list of {"type": "alert", "message": ..., "details": info};
    empty if all clear or outside the commute window (unless force)."""
    if not force and not _in_commute_window(datetime.now()):
        return []

    watched, _source = _fetch_watched(stop_id)
    alerts = []
    for info in watched:
        msg = format_message(info)
        if msg:
            alerts.append({"type": "alert", "message": msg, "details": info})
    return alerts


# Back-compat alias — older callers/docs use check_trains().
check_trains = check_commute


def next_departures(limit: int = 4, stop_id: str = STOP_ID) -> Dict[str, Any]:
    """Next watched departures regardless of the commute window.

    The "summary" string is spoken by the voice intent."""
    watched, source = _fetch_watched(stop_id)
    now_utc = datetime.now(timezone.utc)
    upcoming = [
        i for i in watched
        if i["planned_iso_utc"] and _parse_iso_utc(i["planned_iso_utc"]) >= now_utc - timedelta(minutes=1)
    ][:limit]

    parts = []
    for mode, _label in (("train", "train"), ("bus", "bus")):
        nxt = next((i for i in upcoming if i["mode"] == mode), None)
        if not nxt:
            continue
        status = "on time"
        if nxt["is_cancelled"]:
            status = "cancelled"
        elif nxt["delay_minutes"]:
            n = nxt["delay_minutes"]
            status = f"{n} minute{'s' if n != 1 else ''} late"
        name = f"{nxt['line']} to {nxt['destination']}" if mode == "train" else f"bus {nxt['line']} to the city"
        parts.append(f"Next {name} at {nxt['planned_time']}, {status}.")

    summary = " ".join(parts) if parts else (
        f"I found no upcoming {TRAIN_LINE} or bus {BUS_LINE} departures from {STOP_SPOKEN_NAME}."
    )
    return {
        "stop": STOP_SPOKEN_NAME,
        "stop_id": stop_id,
        "source": source,
        "departures": [
            {k: i[k] for k in ("line", "destination", "mode", "planned_time",
                               "actual_time", "delay_minutes", "is_cancelled")}
            for i in upcoming
        ],
        "summary": summary,
    }


def run_once(force: bool = False, alert: bool = False) -> List[Dict[str, Any]]:
    """One check cycle: fetch, dedup against state, optionally announce."""
    now_local = datetime.now()
    print(f"[{now_local.strftime('%H:%M')}] Checking {STOP_SPOKEN_NAME} (id={STOP_ID}) "
          f"for {TRAIN_LINE} -> {'/'.join(TRAIN_DESTINATIONS)} and bus {BUS_LINE}")

    if not force and not _in_commute_window(now_local):
        print(f"Outside commute window (weekday {COMMUTE_WINDOW_START}-{COMMUTE_WINDOW_END}) "
              "— skipping check. Use --force to override.")
        return []

    alerts = check_commute(force=force)
    now_utc = datetime.now(timezone.utc)
    state = _load_state()
    _prune_state(state, now_utc)

    to_announce = []
    for a in alerts:
        if _should_announce(a["details"], state):
            to_announce.append(a)
            print(f"  ALERT: {a['message']}")
        else:
            print(f"  suppressed (already announced): {a['message']}")

    if not alerts:
        print("No delays or cancellations detected. All clear!")

    if alert and to_announce:
        from voice import speak_on_alexa
        for a in to_announce:
            res = speak_on_alexa(a["message"], kind="announce")
            if "error" in res:
                # don't record — let the next cycle retry the announcement
                print(f"  ERROR: Alexa announce failed: {res['error']}")
            else:
                _record_announcement(a["details"], state, now_utc)

    _save_state(state)
    return to_announce


def run_daemon():
    """Run continuous monitoring loop."""
    print(f"[{datetime.now().strftime('%H:%M')}] Starting commute monitor daemon "
          f"(poll every {POLL_INTERVAL_MINUTES} min)")

    while True:
        try:
            run_once(alert=True)
        except Exception as e:
            print(f"Error in daemon loop: {e}")

        time.sleep(POLL_INTERVAL_MINUTES * 60)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Commute Monitor — {TRAIN_LINE} + bus {BUS_LINE} from {STOP_SPOKEN_NAME}")
    parser.add_argument("--alert", action="store_true", help="Send Alexa announcements (default: print only)")
    parser.add_argument("--daemon", action="store_true", help="Continuous polling mode")
    parser.add_argument("--force", action="store_true", help="Ignore the commute time window (for manual testing)")
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
    else:
        run_once(force=args.force, alert=args.alert)
