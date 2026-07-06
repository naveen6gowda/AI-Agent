"""
Phase 2 / Feature #1 — SMART disk health watcher.

For every disk in catalog.proxmox_host.disks_to_monitor, SSH to the
Proxmox host and run `smartctl -aj <disk>`. Parse the JSON, evaluate
against thresholds, classify as healthy / warning / critical, and
(optionally) ask the local LLM to write an operator digest.

Why SSH and not Proxmox API: smartctl is a host-level tool, and Proxmox
has no API endpoint for it. We already SSH for guest memory readings,
so we reuse `_ssh_exec` from tools.py.

CLI usage:
    uv run python smart_monitor.py
    uv run python smart_monitor.py --json
    uv run python smart_monitor.py --alert        # Telegram if any critical
    uv run python smart_monitor.py --no-summary
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from catalog import load_catalog
from tools import _ssh_exec

# ----------------------------------------------------------------------
# Thresholds — sensible homelab defaults. Override via env later if needed.
# ----------------------------------------------------------------------
TEMP_WARN_C       = 50
TEMP_CRIT_C       = 60

NVME_SPARE_WARN   = 20   # % — vendor "low spare" trigger is usually 10
NVME_SPARE_CRIT   = 10
NVME_USED_WARN    = 80   # % wear indicator
NVME_USED_CRIT    = 95


# ----------------------------------------------------------------------
# Per-disk reader
# ----------------------------------------------------------------------
@dataclass
class Alert:
    severity: str            # "critical" | "warning"
    field: str
    value: Any
    note: str = ""


def _alerts_to_dicts(alerts: List[Alert]) -> List[Dict[str, Any]]:
    return [{"severity": a.severity, "field": a.field, "value": a.value,
             "note": a.note} for a in alerts]


def _parse_sata(report: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Alert]]:
    """Extract the metrics we care about from a SATA smartctl JSON report."""
    alerts: List[Alert] = []
    attrs = {
        a["id"]: a["raw"]["value"]
        for a in (report.get("ata_smart_attributes", {}).get("table") or [])
        if "id" in a and "raw" in a
    }

    reallocated = attrs.get(5, 0)
    pending     = attrs.get(197, 0)
    offline_unc = attrs.get(198, 0)
    reported_unc = attrs.get(187, 0)
    poh = report.get("power_on_time", {}).get("hours")
    temp = report.get("temperature", {}).get("current")

    if reallocated > 0:
        alerts.append(Alert("critical", "reallocated_sectors", reallocated,
                            "drive is silently remapping bad sectors"))
    if pending > 0:
        alerts.append(Alert("critical", "current_pending_sector", pending,
                            "data may be unreadable; backup NOW"))
    if offline_unc > 0:
        alerts.append(Alert("critical", "offline_uncorrectable", offline_unc,
                            "sectors permanently failed"))
    if reported_unc > 0:
        alerts.append(Alert("warning", "reported_uncorrect", reported_unc))
    if temp is not None and temp >= TEMP_CRIT_C:
        alerts.append(Alert("critical", "temperature_c", temp,
                            f">= {TEMP_CRIT_C}C"))
    elif temp is not None and temp >= TEMP_WARN_C:
        alerts.append(Alert("warning", "temperature_c", temp,
                            f">= {TEMP_WARN_C}C"))

    return {
        "kind": "sata",
        "temperature_c": temp,
        "power_on_hours": poh,
        "reallocated_sectors": reallocated,
        "current_pending_sector": pending,
        "offline_uncorrectable": offline_unc,
    }, alerts


def _parse_nvme(report: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Alert]]:
    """Extract metrics from an NVMe smartctl JSON report."""
    alerts: List[Alert] = []
    log = report.get("nvme_smart_health_information_log", {}) or {}

    crit_warn = log.get("critical_warning", 0)
    temp = log.get("temperature")
    spare = log.get("available_spare")
    spare_thresh = log.get("available_spare_threshold", 10)
    used_pct = log.get("percentage_used")
    media_err = log.get("media_errors", 0)
    err_log = log.get("num_err_log_entries", 0)
    poh = log.get("power_on_hours") or report.get("power_on_time", {}).get("hours")

    if crit_warn:
        alerts.append(Alert("critical", "critical_warning", crit_warn,
                            "any non-zero value is bad — see NVMe spec"))
    if media_err > 0:
        alerts.append(Alert("critical", "media_errors", media_err))
    if spare is not None and spare <= NVME_SPARE_CRIT:
        alerts.append(Alert("critical", "available_spare_pct", spare,
                            f"<= {NVME_SPARE_CRIT}% — drive end-of-life"))
    elif spare is not None and (spare <= NVME_SPARE_WARN
                                 or spare <= spare_thresh + 5):
        alerts.append(Alert("warning", "available_spare_pct", spare))
    if used_pct is not None and used_pct >= NVME_USED_CRIT:
        alerts.append(Alert("critical", "percentage_used", used_pct,
                            "wear indicator near spec limit"))
    elif used_pct is not None and used_pct >= NVME_USED_WARN:
        alerts.append(Alert("warning", "percentage_used", used_pct))
    if temp is not None and temp >= TEMP_CRIT_C:
        alerts.append(Alert("critical", "temperature_c", temp,
                            f">= {TEMP_CRIT_C}C"))
    elif temp is not None and temp >= TEMP_WARN_C:
        alerts.append(Alert("warning", "temperature_c", temp,
                            f">= {TEMP_WARN_C}C"))
    if err_log > 0:
        alerts.append(Alert("warning", "num_err_log_entries", err_log))

    return {
        "kind": "nvme",
        "temperature_c": temp,
        "power_on_hours": poh,
        "available_spare_pct": spare,
        "percentage_used": used_pct,
        "media_errors": media_err,
        "critical_warning": crit_warn,
    }, alerts


def read_smart(disk: str) -> Dict[str, Any]:
    """Run smartctl on the Proxmox host for one disk; return structured result.

    Always returns a dict (never raises). On unreachable disk / parse
    failure, status is "unknown" and the error is in `error`.

    NOTE on exit codes: smartctl's exit code is a bitfield reporting
    HEALTH issues, not whether the command worked. With `-j` it always
    emits valid JSON on stdout regardless. So we parse stdout first and
    only fall back to the SSH error message if there's no JSON at all.
    """
    res = _ssh_exec(f"smartctl -aj {disk}", timeout=20)
    stdout = res.get("stdout", "") or ""

    report = None
    if stdout.strip():
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            report = None

    if report is None:
        # No parseable JSON — fall back to the SSH-level error if present,
        # else report whatever exit info we have.
        msg = res.get("error") or "smartctl produced no JSON output"
        if "stderr" in res and res["stderr"]:
            msg += f" — stderr: {res['stderr'][:200]}"
        return {"disk": disk, "status": "unknown", "error": msg, "alerts": []}

    # smartctl_exit_status tells us bit-by-bit what's wrong. We don't
    # need to interpret it ourselves — smart_status.passed is the
    # rolled-up answer, and individual attribute checks below catch the
    # specifics (reallocated, pending, etc.).
    health_passed = report.get("smart_status", {}).get("passed", True)
    device_type = report.get("device", {}).get("type") or report.get("device", {}).get("protocol", "").lower()

    if device_type == "nvme":
        details, alerts = _parse_nvme(report)
    else:
        details, alerts = _parse_sata(report)

    if not health_passed:
        alerts.insert(0, Alert("critical", "smart_health", "FAILED",
                                "overall SMART self-assessment failed"))

    # Roll up severity
    if any(a.severity == "critical" for a in alerts):
        status = "critical"
    elif any(a.severity == "warning" for a in alerts):
        status = "warning"
    else:
        status = "healthy"

    return {
        "disk": disk,
        "model": report.get("model_name") or report.get("model_family", ""),
        "serial": report.get("serial_number", ""),
        "health_passed": health_passed,
        "status": status,
        "alerts": _alerts_to_dicts(alerts),
        **details,
    }


# ----------------------------------------------------------------------
# Scan all disks
# ----------------------------------------------------------------------
def scan_disks(max_workers: int = 4) -> Dict[str, Any]:
    """Run SMART read across every disk in catalog.proxmox_host.disks_to_monitor.

    Parallel reads — smartctl is fast but SSH setup adds latency. Cap at
    4 workers to avoid overwhelming sshd on the Proxmox host.
    """
    cat = load_catalog()
    disks = cat.proxmox_host.disks_to_monitor
    if not disks:
        return {"total": 0, "results": [], "note":
                "no disks_to_monitor in catalog.yaml"}

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(read_smart, d): d for d in disks}
        for fut in as_completed(futures):
            results.append(fut.result())

    counts = {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    critical_disks = [r["disk"] for r in results if r["status"] == "critical"]
    return {
        "total": len(results),
        **counts,
        "critical_disks": critical_disks,
        "results": sorted(results, key=lambda r: (
            r["status"] != "critical", r["status"] != "warning", r["disk"])),
    }


# ----------------------------------------------------------------------
# LLM digest
# ----------------------------------------------------------------------
def _fallback_summary(data: Dict[str, Any], reason: str) -> str:
    """Deterministic summary — used when helper_llm is unreachable OR returns
    empty text. Always tells the operator the essential info."""
    prefix = f"[{reason}] "
    if data.get("critical"):
        return (prefix + f"CRITICAL: {', '.join(data['critical_disks'])}. "
                f"Healthy {data.get('healthy',0)}/{data['total']}.")
    if data.get("unknown"):
        return (prefix + f"{data.get('unknown',0)} disk(s) UNREADABLE — "
                f"likely wrong device name in catalog or missing smartmontools. "
                f"{data.get('healthy',0)} healthy / {data['total']} total.")
    if data.get("warning"):
        return (prefix + f"{data.get('warning',0)} disk(s) with warnings, "
                f"{data.get('healthy',0)} healthy / {data['total']} total.")
    return prefix + f"All {data['total']} disks healthy."


def summarize_scan(data: Dict[str, Any]) -> str:
    """One-paragraph digest from the local LLM. Falls back deterministically."""
    lines = []
    for r in data["results"]:
        tag = {"critical": "[CRIT]", "warning": "[WARN]",
               "healthy": "[OK]", "unknown": "[?]"}[r["status"]]
        bits = [
            r["disk"], (r.get("model") or "")[:25],
            f"temp={r.get('temperature_c')}C",
        ]
        if r.get("kind") == "nvme":
            bits.append(f"spare={r.get('available_spare_pct')}%")
            bits.append(f"used={r.get('percentage_used')}%")
        elif r.get("kind") == "sata":
            bits.append(f"realloc={r.get('reallocated_sectors')}")
            bits.append(f"pending={r.get('current_pending_sector')}")
        if r["alerts"]:
            bits.append("alerts=" + ",".join(a["field"] for a in r["alerts"]))
        if r.get("error"):
            bits.append(f"err={r['error'][:60]}")
        lines.append(f"  {tag:<7} " + "  ".join(b for b in bits if b))

    block = "\n".join(lines)
    prompt = (
        "You are summarizing a homelab disk health (SMART) scan for the operator.\n"
        "Write ONE short paragraph (2-3 sentences). Lead with whether any disk "
        "is critical or unreadable. Mention specific device names. Do not invent "
        "details. No preamble, no headings, no bullet points.\n\n"
        f"Disks ({data['total']} total: healthy={data.get('healthy',0)}, "
        f"warning={data.get('warning',0)}, critical={data.get('critical',0)}, "
        f"unknown={data.get('unknown',0)}):\n{block}\n"
    )
    try:
        from models import helper_llm
        # Slight temperature lift — a small local model at temp 0 sometimes generates
        # empty completions when a prompt has a clear "everything's fine"
        # answer. 0.2 keeps it deterministic-ish but unsticks it.
        llm = helper_llm(temperature=0.2, max_tokens=220)
        resp = llm.invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = text.strip()
        if not text:
            return _fallback_summary(data, "helper_llm returned empty")
        return text
    except Exception as e:
        return _fallback_summary(data, f"helper_llm unavailable: {e}")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'STATUS':<10} {'DISK':<18} {'KIND':<6} {'TEMP':<6} {'KEY':<25} MODEL")
    print("-" * 95)
    for r in data["results"]:
        key = ""
        if r.get("kind") == "nvme":
            key = f"spare={r.get('available_spare_pct')}% used={r.get('percentage_used')}%"
        elif r.get("kind") == "sata":
            key = (f"realloc={r.get('reallocated_sectors')} "
                   f"pending={r.get('current_pending_sector')}")
        print(f"{r['status']:<10} {r['disk']:<18} "
              f"{r.get('kind','?'):<6} "
              f"{str(r.get('temperature_c','-')):<6} "
              f"{key:<25} {(r.get('model') or '')[:30]}")
        for a in r["alerts"]:
            note = f" — {a['note']}" if a['note'] else ""
            print(f"           ↳ {a['severity']:<8} {a['field']}={a['value']}{note}")
        if r.get("error"):
            print(f"           ↳ ERROR: {r['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel SMART scan")
    parser.add_argument("--alert", action="store_true",
                        help="Telegram alert if any disk is critical")
    parser.add_argument("--no-summary", action="store_true",
                        help="skip helper_llm digest")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    data = scan_disks()

    if args.json:
        print(json.dumps(data, indent=2))
        return 2 if data.get("critical_disks") else 0

    print(f"\nSMART scan — {data['total']} disks "
          f"(healthy={data.get('healthy',0)} warning={data.get('warning',0)} "
          f"critical={data.get('critical',0)} unknown={data.get('unknown',0)})\n")
    _print_table(data)

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_scan(data))

    if args.alert and data.get("critical_disks"):
        from tools import send_telegram_alert
        msg = (f"⚠️ SMART: critical disk(s) — "
               f"{', '.join(data['critical_disks'])}\n\n"
               f"{summarize_scan(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")

    return 2 if data.get("critical_disks") else 0


if __name__ == "__main__":
    sys.exit(main())
