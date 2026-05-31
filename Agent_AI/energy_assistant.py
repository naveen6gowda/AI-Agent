"""
Phase 3 / Feature #6 — Energy Assistant.

Two modes:

  --discover    Find every HA sensor that looks energy-related (by
                device_class or unit_of_measurement). Use this FIRST to
                see what's available, then fill in catalog.yaml -> energy.

  default       Use catalog.yaml -> energy to compute totals + cost for a
                configurable period (default 24h). Reads main meter delta
                and per-device deltas via /api/history/period. Hands the
                summary to local Gemma for a 2-3 sentence operator digest.

CLI usage:
    uv run python energy_assistant.py --discover
    uv run python energy_assistant.py                  # last 24h
    uv run python energy_assistant.py --hours 168      # last week
    uv run python energy_assistant.py --alert-over 50  # Telegram if > 50 kWh
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from catalog import EnergyConfig, load_catalog
from tools import ha_all_states, get_ha_history


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------
# What counts as "energy-related." HA convention is the device_class
# attribute — but lots of integrations report units without setting it,
# so we also look at unit_of_measurement.
_ENERGY_UNITS = {"Wh", "kWh", "MWh"}
_POWER_UNITS = {"W", "kW", "MW"}


def discover_energy_entities() -> Dict[str, Any]:
    """Categorize HA entities by whether they look like energy or power sensors.

    Energy (accumulating, kWh): suitable for delta-over-period totals.
    Power  (instantaneous, W) : useful for current draw but needs integration
                                for energy totals — we list but don't sum.
    """
    states = ha_all_states()
    if not states:
        return {"error": "HA returned no entities (check HA_URL + HA_TOKEN)"}

    energy: List[Dict[str, Any]] = []
    power: List[Dict[str, Any]] = []

    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith("sensor."):
            continue
        attrs = s.get("attributes", {}) or {}
        unit = attrs.get("unit_of_measurement", "") or ""
        dc = attrs.get("device_class", "") or ""

        item = {
            "entity_id": eid,
            "friendly_name": attrs.get("friendly_name", ""),
            "unit": unit,
            "device_class": dc,
            "state": s.get("state"),
            "state_class": attrs.get("state_class", ""),
        }
        if dc == "energy" or unit in _ENERGY_UNITS:
            energy.append(item)
        elif dc == "power" or unit in _POWER_UNITS:
            power.append(item)

    energy.sort(key=lambda e: e["entity_id"])
    power.sort(key=lambda e: e["entity_id"])

    return {
        "energy_count": len(energy),
        "power_count": len(power),
        "energy": energy,
        "power": power,
    }


# ----------------------------------------------------------------------
# Period summary
# ----------------------------------------------------------------------
def _delta_for_entity(entity_id: str, hours: int) -> Optional[float]:
    """Reset-aware kWh delta over the last `hours` from an accumulator.

    Tuya/cloud energy sensors reset their lifetime totals on a schedule
    (often ~daily). A naive last-first delta straddles those resets and
    silently under-reports — a fridge that did 1.8 kWh across a reset
    would look like 0.4 kWh of "post-reset" usage.

    Standard rate-counter pattern: walk the history, sum upward steps
    only. A downward step = reset; we don't try to recover the lost
    value (the reset point's "true" value isn't in the data), we just
    pick up counting again from the new low.

    Returns:
        - None if HA has no history for the entity (sensor offline,
          entity didn't exist, no parseable readings)
        - 0.0 if history exists but the value never moved (device was
          off the whole period) — distinct from None
        - >0.0 sum of all positive increments observed
    """
    history = get_ha_history(entity_id, hours)
    if not history:
        return None

    total = 0.0
    prev_val: Optional[float] = None
    saw_numeric = False
    for entry in history:
        try:
            v = float(entry.get("state", ""))
        except (TypeError, ValueError):
            continue
        saw_numeric = True
        if prev_val is None:
            prev_val = v
            continue
        if v >= prev_val:
            total += v - prev_val
        # else: counter reset — don't credit or debit the gap; resume
        # counting from the post-reset baseline on the next iteration.
        prev_val = v

    if not saw_numeric:
        return None  # had entries but none were numeric
    return round(total, 3)


def _estimate_cost(kwh: float, hours: int, energy_cfg: EnergyConfig) -> Optional[Dict[str, Any]]:
    """Apply tariff to a kWh figure. Coarse: weighs by fraction of the
    measurement window that falls in off-peak hours, vs everything else.

    Good enough for daily/weekly digests. Not good enough for billing.
    """
    if not energy_cfg.tariff:
        return None
    t = energy_cfg.tariff
    if t.off_peak_per_kwh is None or not t.off_peak_hours:
        # Flat tariff — easy
        return {
            "currency": t.currency,
            "cost": round(kwh * t.price_per_kwh, 2),
            "tariff_mode": "flat",
            "price_per_kwh": t.price_per_kwh,
        }

    # Estimate fraction of measurement window that was off-peak.
    now = dt.datetime.now()
    off_peak_set = set(t.off_peak_hours)
    off_peak_count = 0
    for i in range(hours):
        h = (now - dt.timedelta(hours=i)).hour
        if h in off_peak_set:
            off_peak_count += 1
    off_peak_frac = off_peak_count / hours if hours else 0.0
    peak_frac = 1 - off_peak_frac

    cost = kwh * (peak_frac * t.price_per_kwh + off_peak_frac * t.off_peak_per_kwh)
    return {
        "currency": t.currency,
        "cost": round(cost, 2),
        "tariff_mode": "tou",  # time-of-use
        "off_peak_fraction": round(off_peak_frac, 2),
        "price_per_kwh_peak": t.price_per_kwh,
        "price_per_kwh_off_peak": t.off_peak_per_kwh,
    }


def read_energy_summary(hours: int = 24) -> Dict[str, Any]:
    """Compute totals + per-device breakdown over the last `hours`.

    Returns one of:
      {"error": "..."}            — catalog has no `energy:` section
      {"unconfigured": True, ...} — section present but no main meter
                                    AND no watch list (just discovery info)
      full summary                — totals, devices[], optional cost, gemma-ready
    """
    cat = load_catalog()
    if cat.energy is None:
        return {
            "error": "no energy section in catalog.yaml — run with --discover "
                     "to see candidates, then fill in catalog.yaml -> energy"
        }

    cfg = cat.energy

    if not cfg.main_meter and not cfg.watch:
        return {
            "unconfigured": True,
            "message": "energy section exists but has no main_meter and "
                       "no watch entries — nothing to measure"
        }

    main_kwh = _delta_for_entity(cfg.main_meter, hours) if cfg.main_meter else None
    devices: List[Dict[str, Any]] = []
    for w in cfg.watch:
        d = _delta_for_entity(w.entity_id, hours)
        devices.append({
            "entity_id": w.entity_id,
            "name": w.name or w.entity_id,
            "kwh": d,
        })

    # Top consumers (sorted desc, missing values last)
    devices_sorted = sorted(
        devices,
        key=lambda x: (x["kwh"] is None, -(x["kwh"] or 0)),
    )

    watched_total = sum(d["kwh"] for d in devices if d["kwh"] is not None)

    # Effective total for digest/cost. Prefer the true whole-house reading;
    # fall back to sum-of-watched when no main meter is configured (a very
    # common homelab pattern — smart plugs, no whole-house clamp).
    if main_kwh is not None:
        effective_total = main_kwh
        total_source = "main_meter"
    elif watched_total > 0:
        effective_total = watched_total
        total_source = "sum_of_watched"
    else:
        effective_total = None
        total_source = None

    cost_info = (
        _estimate_cost(effective_total, hours, cfg)
        if effective_total is not None else None
    )
    if cost_info:
        cost_info["basis"] = total_source

    # Coverage % only makes sense if there's an independent main meter to
    # compare against. With sum-of-watched as the total it would be 100%
    # tautologically, so we suppress it.
    coverage = (
        round(watched_total / main_kwh * 100, 1)
        if main_kwh and main_kwh > 0 else None
    )

    return {
        "period_hours": hours,
        "main_meter": cfg.main_meter,
        "main_kwh": main_kwh,
        "effective_total_kwh": (round(effective_total, 3)
                                 if effective_total is not None else None),
        "total_source": total_source,
        "devices": devices_sorted,
        "watched_total_kwh": round(watched_total, 3),
        "watched_coverage_pct": coverage,
        "cost": cost_info,
    }


# ----------------------------------------------------------------------
# Gemma digest
# ----------------------------------------------------------------------
def summarize_energy(data: Dict[str, Any]) -> str:
    """One-paragraph digest. Deterministic fallback if LLM empty/down."""

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        bits = []
        if data.get("main_kwh") is not None:
            bits.append(f"main meter used {data['main_kwh']} kWh "
                        f"over the last {data['period_hours']}h")
        elif data.get("effective_total_kwh") is not None:
            bits.append(f"tracked devices used {data['effective_total_kwh']} kWh "
                        f"over the last {data['period_hours']}h "
                        f"(no whole-house meter)")
        if data.get("cost") and data["cost"].get("cost") is not None:
            bits.append(f"~{data['cost']['cost']} {data['cost']['currency']}")
        top = next((d for d in data.get("devices", []) if d.get("kwh") is not None), None)
        if top and top["kwh"] > 0:
            bits.append(f"top tracked device: {top['name']} at {top['kwh']} kWh")
        if not bits:
            return prefix + "no energy readings available (check sensors)."
        return prefix + "; ".join(bits) + "."

    lines = [f"period_hours={data['period_hours']}"]
    if data.get("main_meter") and data.get("main_kwh") is not None:
        lines.append(f"main_meter={data['main_meter']}  kwh={data['main_kwh']}")
    elif data.get("effective_total_kwh") is not None:
        lines.append(f"total (sum-of-watched, no whole-house meter): "
                     f"{data['effective_total_kwh']} kWh")
    if data.get("cost"):
        c = data["cost"]
        lines.append(f"cost ~ {c.get('cost')} {c.get('currency')} "
                     f"(basis={c.get('basis')}, mode={c.get('tariff_mode')})")
    if data.get("watched_coverage_pct") is not None:
        lines.append(f"watched_coverage={data['watched_coverage_pct']}% of main")
    for d in data.get("devices", []):
        lines.append(f"  - {d['name']:<20} {d.get('kwh')} kWh ({d['entity_id']})")

    block = "\n".join(lines)
    prompt = (
        "You are summarizing a homelab energy report for the operator.\n"
        "Write ONE short paragraph (2-3 sentences). Mention specific numbers "
        "and device names. If a top consumer stands out, point it out. "
        "Do not invent details. No preamble, no headings, no bullet points.\n\n"
        f"{block}\n"
    )
    try:
        from models import helper_llm
        llm = helper_llm(temperature=0.2, max_tokens=220)
        resp = llm.invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = text.strip()
        if not text:
            return _fallback("helper_llm returned empty")
        return text
    except Exception as e:
        return _fallback(f"helper_llm unavailable: {e}")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print_discover(data: Dict[str, Any]) -> None:
    if "error" in data:
        print(f"ERROR: {data['error']}")
        return
    print(f"\nDiscovered {data['energy_count']} energy sensor(s) "
          f"and {data['power_count']} power sensor(s).\n")

    if data["energy"]:
        print("ENERGY (kWh-accumulating — usable for daily totals):")
        print(f"  {'ENTITY_ID':<45} {'UNIT':<6} {'STATE':<12} FRIENDLY NAME")
        print("  " + "-" * 90)
        for e in data["energy"]:
            print(f"  {e['entity_id']:<45} {e['unit']:<6} "
                  f"{str(e['state'])[:12]:<12} {e['friendly_name']}")
    else:
        print("ENERGY: none found.")

    print()
    if data["power"]:
        print(f"POWER (instantaneous W — informational only):")
        print(f"  {'ENTITY_ID':<45} {'UNIT':<6} {'STATE':<12} FRIENDLY NAME")
        print("  " + "-" * 90)
        for p in data["power"][:20]:  # cap — power sensors are often many
            print(f"  {p['entity_id']:<45} {p['unit']:<6} "
                  f"{str(p['state'])[:12]:<12} {p['friendly_name']}")
        if len(data["power"]) > 20:
            print(f"  ... {len(data['power']) - 20} more")

    print("\nNext:  pick a main meter from the ENERGY list (kWh-accumulating),")
    print("       and any per-device entries you want broken out, then add")
    print("       them to catalog.yaml under  energy:")


def _print_summary(data: Dict[str, Any]) -> None:
    if "error" in data:
        print(f"NOTE: {data['error']}")
        return
    if data.get("unconfigured"):
        print(f"NOTE: {data['message']}")
        return

    print(f"\nEnergy summary — last {data['period_hours']}h")
    print("=" * 60)
    if data.get("main_meter"):
        main = data.get("main_kwh")
        if main is None:
            print(f"  Main meter ({data['main_meter']}): no reading "
                  f"(sensor missing / reset / no history)")
        else:
            print(f"  Main meter: {main} kWh ({data['main_meter']})")
    elif data.get("effective_total_kwh") is not None:
        # No main meter configured — show sum-of-watched as the effective total
        print(f"  Effective total: {data['effective_total_kwh']} kWh "
              f"(sum of watched — no whole-house meter configured)")
    if data.get("cost"):
        c = data["cost"]
        if c.get("cost") is not None:
            mode = c.get("tariff_mode", "?")
            basis = c.get("basis", "?")
            extra = (f"  (off-peak {int(c.get('off_peak_fraction',0)*100)}% of period)"
                     if mode == "tou" else "")
            note = " [partial — watched devices only]" if basis == "sum_of_watched" else ""
            print(f"  Cost      : ~{c['cost']} {c['currency']}  [{mode}]{extra}{note}")
    if data.get("watched_coverage_pct") is not None:
        print(f"  Coverage  : watched devices = "
              f"{data['watched_coverage_pct']}% of main meter")
    print(f"\n  {'DEVICE':<22} {'kWh':<10} ENTITY_ID")
    print("  " + "-" * 70)
    for d in data["devices"]:
        kwh = "-" if d["kwh"] is None else f"{d['kwh']:.3f}"
        print(f"  {d['name'][:22]:<22} {kwh:<10} {d['entity_id']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel energy assistant")
    parser.add_argument("--discover", action="store_true",
                        help="list HA energy/power sensors and exit")
    parser.add_argument("--hours", type=int, default=24,
                        help="summary period in hours (default 24)")
    parser.add_argument("--alert-over", type=float, default=None,
                        help="Telegram alert if main-meter kWh in period exceeds this")
    parser.add_argument("--digest", action="store_true",
                        help="send Gemma digest to Telegram (daily-digest mode)")
    parser.add_argument("--no-summary", action="store_true",
                        help="skip helper_llm digest")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    if args.discover:
        data = discover_energy_entities()
        if args.json:
            print(json.dumps(data, indent=2, default=str))
            return 0
        _print_discover(data)
        return 0

    data = read_energy_summary(hours=args.hours)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    _print_summary(data)

    if "error" in data or data.get("unconfigured"):
        return 1

    if not args.no_summary:
        print("\n--- Gemma digest ---")
        print(summarize_energy(data))

    if args.digest:
        from tools import send_telegram_alert
        digest = summarize_energy(data)
        kwh = (data.get("main_kwh") if data.get("main_kwh") is not None
               else data.get("effective_total_kwh"))
        header = (f"⚡ Energy digest — last {data['period_hours']}h "
                  f"({kwh} kWh)")
        result = send_telegram_alert(f"{header}\n\n{digest}")
        print(f"\nDigest sent: {result}")

    # Alerting
    if (args.alert_over is not None
            and data.get("main_kwh") is not None
            and data["main_kwh"] > args.alert_over):
        from tools import send_telegram_alert
        msg = (f"⚡ Energy alert: {data['main_kwh']} kWh in last "
               f"{data['period_hours']}h exceeds threshold "
               f"{args.alert_over} kWh.\n\n{summarize_energy(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
