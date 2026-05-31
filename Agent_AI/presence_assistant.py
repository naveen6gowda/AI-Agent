"""
Phase 3 / Feature #7 — Presence + HVAC + Lights.

Read-only state readers that feed the agent. The agent decides what
ACTIONS to take (and asks for approval via call_ha_service, which is
already gated). This module deliberately has NO rule engine — the LLM
plus the catalog policy ARE the rules.

What's here:
  discover_home_entities()  — find person, device_tracker, motion,
                              door/window, climate, light, lock entities
  check_presence_state()    — who's home / away / mixed
  check_climate_state()     — current climate entities + temps + mode
  check_light_state()       — light entities + on/off + brightness

CLI:
    uv run python presence_assistant.py --discover
    uv run python presence_assistant.py                  # state snapshot
    uv run python presence_assistant.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from tools import ha_all_states


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------
def _attr(state: dict, key: str, default=None):
    return (state.get("attributes") or {}).get(key, default)


def discover_home_entities() -> Dict[str, Any]:
    """Find every HA entity relevant to presence + HVAC + lights.

    Buckets:
      persons       — HA person.* (the authoritative "is X home?" entity)
      device_trackers — phones/wearables/etc; persons.* derive from these
      motion        — binary_sensor with device_class=motion
      door_window   — binary_sensor with device_class in (door,window,opening,garage)
      occupancy     — binary_sensor with device_class=occupancy
      climate       — climate.* (thermostats, HVAC, mini-splits)
      light         — light.* (groups + individual)
      switch        — switch.* (often used for fans, plug strips)
      lock          — lock.*
    """
    states = ha_all_states()
    if not states:
        return {"error": "HA returned no entities (check HA_URL / HA_TOKEN)"}

    def _row(s: dict) -> dict:
        return {
            "entity_id": s["entity_id"],
            "state": s.get("state"),
            "friendly_name": _attr(s, "friendly_name", ""),
            "device_class": _attr(s, "device_class", ""),
        }

    persons, trackers = [], []
    motion, door_window, occupancy = [], [], []
    climate, light, switch, lock = [], [], [], []

    for s in states:
        eid = s.get("entity_id", "")
        dc = _attr(s, "device_class", "") or ""

        if eid.startswith("person."):
            persons.append(_row(s))
        elif eid.startswith("device_tracker."):
            trackers.append(_row(s))
        elif eid.startswith("binary_sensor."):
            if dc == "motion":
                motion.append(_row(s))
            elif dc in ("door", "window", "opening", "garage_door"):
                door_window.append(_row(s))
            elif dc == "occupancy":
                occupancy.append(_row(s))
        elif eid.startswith("climate."):
            climate.append(_row(s))
        elif eid.startswith("light."):
            light.append(_row(s))
        elif eid.startswith("switch."):
            switch.append(_row(s))
        elif eid.startswith("lock."):
            lock.append(_row(s))

    for lst in (persons, trackers, motion, door_window, occupancy,
                climate, light, switch, lock):
        lst.sort(key=lambda e: e["entity_id"])

    return {
        "persons": persons,
        "device_trackers": trackers,
        "motion": motion,
        "door_window": door_window,
        "occupancy": occupancy,
        "climate": climate,
        "light": light,
        "switch": switch,
        "lock": lock,
        "counts": {
            "persons": len(persons),
            "device_trackers": len(trackers),
            "motion": len(motion),
            "door_window": len(door_window),
            "occupancy": len(occupancy),
            "climate": len(climate),
            "light": len(light),
            "switch": len(switch),
            "lock": len(lock),
        },
    }


# ----------------------------------------------------------------------
# Presence
# ----------------------------------------------------------------------
def check_presence_state() -> Dict[str, Any]:
    """Determine current home presence from person.* entities.

    `person.*` is HA's authoritative "is X home?" — it combines all of
    that person's device_trackers (GPS, router, etc.) into one judgement.
    Any state == "home" → person is home. Anything else (zone names like
    "Work", "not_home", "unknown") → not home.

    Returns:
        {
          "presence": "home" | "away" | "mixed" | "unknown",
          "home": ["Naveen", ...],
          "away": [{"name": "Chaitra", "state": "Work"}, ...],
          "details": [...]   # raw entity data for the agent to inspect
        }
    """
    states = ha_all_states()
    persons = [s for s in states if s.get("entity_id", "").startswith("person.")]

    if not persons:
        return {
            "presence": "unknown",
            "home": [],
            "away": [],
            "details": [],
            "note": "no person.* entities in HA — set up Person entries "
                    "under Settings → People to get presence detection",
        }

    home, away, details = [], [], []
    for p in persons:
        state = p.get("state", "unknown")
        name = _attr(p, "friendly_name", "") or p["entity_id"]
        details.append({
            "entity_id": p["entity_id"],
            "state": state,
            "friendly_name": name,
            "last_changed": p.get("last_changed"),
        })
        if state == "home":
            home.append(name)
        else:
            away.append({"name": name, "state": state})

    if not home and away:
        presence = "away"
    elif home and not away:
        presence = "home"
    else:
        presence = "mixed"

    return {
        "presence": presence,
        "home": home,
        "away": away,
        "details": details,
    }


# ----------------------------------------------------------------------
# Climate
# ----------------------------------------------------------------------
def check_climate_state() -> Dict[str, Any]:
    """Snapshot of every climate.* entity — current temp, target, mode, action."""
    states = ha_all_states()
    climates = [s for s in states if s.get("entity_id", "").startswith("climate.")]

    rows = []
    for c in climates:
        rows.append({
            "entity_id": c["entity_id"],
            "friendly_name": _attr(c, "friendly_name", ""),
            "state": c.get("state"),          # off / heat / cool / heat_cool / auto / fan_only / dry
            "current_temp": _attr(c, "current_temperature"),
            "target_temp": _attr(c, "temperature"),
            "target_temp_low": _attr(c, "target_temp_low"),
            "target_temp_high": _attr(c, "target_temp_high"),
            "hvac_action": _attr(c, "hvac_action"),  # idle / heating / cooling / off
            "preset_mode": _attr(c, "preset_mode"),
            "fan_mode": _attr(c, "fan_mode"),
            "current_humidity": _attr(c, "current_humidity"),
        })

    rows.sort(key=lambda r: r["entity_id"])
    return {"count": len(rows), "climates": rows}


# ----------------------------------------------------------------------
# Lights / switches
# ----------------------------------------------------------------------
def check_light_state(include_off: bool = True) -> Dict[str, Any]:
    """Snapshot of every light.* entity. With include_off=False, only return lights currently on."""
    states = ha_all_states()
    lights = [s for s in states if s.get("entity_id", "").startswith("light.")]
    rows = []
    for l in lights:
        st = l.get("state")
        if not include_off and st != "on":
            continue
        bright = _attr(l, "brightness")
        rows.append({
            "entity_id": l["entity_id"],
            "friendly_name": _attr(l, "friendly_name", ""),
            "state": st,
            "brightness": bright,
            "brightness_pct": round(bright / 2.55, 0) if bright else None,
        })
    rows.sort(key=lambda r: (r["state"] != "on", r["entity_id"]))
    return {"count": len(rows), "on_count": sum(1 for r in rows if r["state"] == "on"),
            "lights": rows}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print_discover(d: Dict[str, Any]) -> None:
    if "error" in d:
        print(f"ERROR: {d['error']}")
        return
    counts = d["counts"]
    print(f"\nDiscovered home entities:")
    for k, v in counts.items():
        print(f"  {k:<16} {v}")

    for label in ("persons", "climate", "light", "lock"):
        rows = d[label]
        if not rows:
            continue
        print(f"\n{label.upper()} ({len(rows)}):")
        print(f"  {'ENTITY_ID':<40} {'STATE':<12} {'CLASS':<10} FRIENDLY")
        print("  " + "-" * 90)
        for r in rows[:15]:
            print(f"  {r['entity_id']:<40} {str(r['state'])[:12]:<12} "
                  f"{r['device_class']:<10} {r['friendly_name']}")
        if len(rows) > 15:
            print(f"  ... {len(rows) - 15} more")


def _print_state(presence, climates, lights) -> None:
    print("\nPresence")
    print("=" * 60)
    if presence.get("note"):
        print(f"  ({presence['note']})")
    print(f"  Status   : {presence['presence']}")
    if presence["home"]:
        print(f"  Home     : {', '.join(presence['home'])}")
    if presence["away"]:
        away_strs = [f"{a['name']} ({a['state']})" for a in presence["away"]]
        print(f"  Away     : {', '.join(away_strs)}")

    print("\nClimate")
    print("=" * 60)
    if climates["count"] == 0:
        print("  (no climate entities — no thermostats / HVAC / mini-splits in HA)")
    else:
        for c in climates["climates"]:
            tgt = c["target_temp"]
            cur = c["current_temp"]
            label = c["friendly_name"] or c["entity_id"]
            print(f"  {label[:30]:<30} mode={c['state']:<10} "
                  f"cur={cur}°C target={tgt}°C action={c['hvac_action']}")

    print("\nLights")
    print("=" * 60)
    if lights["count"] == 0:
        print("  (no light entities)")
    else:
        on_lights = [l for l in lights["lights"] if l["state"] == "on"]
        print(f"  ON: {lights['on_count']} of {lights['count']}")
        for l in on_lights[:10]:
            bp = f" @ {int(l['brightness_pct'])}%" if l['brightness_pct'] else ""
            print(f"    {l['entity_id']:<40} {l['friendly_name']}{bp}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel presence/HVAC/lights")
    parser.add_argument("--discover", action="store_true",
                        help="list every relevant HA entity and exit")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    if args.discover:
        d = discover_home_entities()
        if args.json:
            print(json.dumps(d, indent=2, default=str))
            return 0
        _print_discover(d)
        return 0

    presence = check_presence_state()
    climates = check_climate_state()
    lights = check_light_state()

    if args.json:
        print(json.dumps({
            "presence": presence, "climate": climates, "lights": lights,
        }, indent=2, default=str))
        return 0

    _print_state(presence, climates, lights)
    return 0


if __name__ == "__main__":
    sys.exit(main())
