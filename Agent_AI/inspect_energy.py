"""One-shot inspector for catalog.yaml energy section.

Pulls state_class / last_reset / friendly_name for every candidate
entity and prints history-derived monotonic-vs-resetting evidence.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

from tools import get_ha_entity, get_ha_history  # noqa: E402

CANDIDATES = [
    "sensor.refrigerator_total_energy",
    "sensor.refrigerator_electricity",
    "sensor.dishwasher_total_energy",
    "sensor.dishwasher_total_energy_cloud",
    "sensor.washing_machine_total_energy",
    "sensor.washing_machine_electricity",
    "sensor.laptop_total_energy",
    "sensor.laptop_electricity",
    "sensor.second_laptop_total_energy",
    "sensor.second_laptop_electricity",
    "sensor.smart_plug_10_total_energy",   # Backlight
    "sensor.backlight_electricity",
    "sensor.smart_plug_12_total_energy",   # Decor lights
    "sensor.decor_lights_electricity",
    "sensor.smart_plug_13_total_energy",   # TV Stand
    "sensor.ryzen_7_electricity",
]


def _attrs(eid: str) -> Dict[str, Any]:
    r = get_ha_entity(eid)
    if "error" in r:
        return {"error": r["error"]}
    a = r.get("attributes", {}) or {}
    return {
        "state": r.get("state"),
        "friendly_name": a.get("friendly_name", ""),
        "state_class": a.get("state_class", ""),
        "device_class": a.get("device_class", ""),
        "unit": a.get("unit_of_measurement", ""),
        "last_reset": a.get("last_reset"),
        "last_updated": r.get("last_updated"),
    }


def _history_trend(eid: str, hours: int = 168) -> Dict[str, Any]:
    """Pull N-hour history and characterize: any resets? min/max?"""
    h = get_ha_history(eid, hours)
    if not h:
        return {"points": 0}
    vals: List[float] = []
    resets = 0
    prev: Optional[float] = None
    for e in h:
        try:
            v = float(e.get("state", ""))
        except (TypeError, ValueError):
            continue
        vals.append(v)
        if prev is not None and v < prev - 0.001:
            resets += 1
        prev = v
    if not vals:
        return {"points": len(h), "numeric_points": 0}
    return {
        "points": len(h),
        "numeric_points": len(vals),
        "min": min(vals),
        "max": max(vals),
        "first": vals[0],
        "last": vals[-1],
        "resets_seen": resets,
        "monotonic": resets == 0 and vals[-1] >= vals[0],
    }


def main() -> None:
    print("\n=== ATTRIBUTES ===\n")
    for eid in CANDIDATES:
        a = _attrs(eid)
        if "error" in a:
            print(f"{eid:<45}  ERROR: {a['error']}")
            continue
        print(f"{eid}")
        print(f"  state={a['state']}  unit={a['unit']}  "
              f"state_class={a['state_class']!r}  device_class={a['device_class']!r}")
        print(f"  friendly_name={a['friendly_name']!r}  last_reset={a['last_reset']}")

    print("\n=== 7-DAY HISTORY TREND (dishwasher pair + a few controls) ===\n")
    for eid in (
        "sensor.dishwasher_total_energy",
        "sensor.dishwasher_total_energy_cloud",
        "sensor.refrigerator_total_energy",
        "sensor.refrigerator_electricity",
        "sensor.ryzen_7_electricity",
    ):
        t = _history_trend(eid)
        print(f"{eid}")
        print(f"  {json.dumps(t)}")


if __name__ == "__main__":
    main()
