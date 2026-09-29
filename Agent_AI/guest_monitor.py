"""
guest_monitor.py — fleet-wide Proxmox guest health (status · memory · CPU).

Before this, the agent could only ask about ONE guest at a time
(check_proxmox_status(node, vmid)), so "how are all my VMs doing?" meant
seven serial tool calls and a small model doing arithmetic on the results.
This sweeps every catalogued guest in parallel and hands back a digest.

The memory story is the whole point, and it is deliberately conservative:

    mem_pct_source == 'guest'        real /proc/meminfo reading. Trust it.
    mem_pct_source == 'host_cgroup'  accurate for LXC. Trust it.
    mem_pct_source == 'host_balloon' Proxmox's view of a QEMU VM. It counts
                                     page cache, so healthy VMs read 85-95%.
                                     NEVER flagged as high memory — flagged
                                     as UNRELIABLE instead, which is a
                                     different problem with a different fix
                                     (install qemu-guest-agent).

That distinction came from a real incident: OPNSense reported 89.6% from
host_balloon while being completely healthy. Treating that as "high memory"
is how an agent talks itself into restarting a router.

CLI:
    uv run python guest_monitor.py                 # table + LLM digest
    uv run python guest_monitor.py --json
    uv run python guest_monitor.py --fast          # host-side only, no SSH
    uv run python guest_monitor.py --alert         # Telegram if anything is wrong
    uv run python guest_monitor.py --no-summary

Imported (wired as the check_all_guests tool):
    from guest_monitor import scan_guests
    data = scan_guests()

Exit codes follow the monitor contract: 0 healthy, 2 findings (already
alerted), anything else = the check itself broke.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from catalog import load_catalog
from tools import check_proxmox_status

# A guest is "high memory" only when the reading can be trusted.
MEM_WARN_PCT = float(os.getenv("GUEST_MEM_WARN_PCT", "85"))
TRUSTED_SOURCES = ("guest", "host_cgroup")
# Criticality levels where a stopped guest is a finding rather than a note.
ALERT_CRITICALITIES = {"critical", "high"}


def _classify(svc, status: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one raw status into a row with an explicit verdict."""
    row = {
        "vmid": svc.vmid,
        "name": svc.name,
        "kind": svc.kind,
        "criticality": svc.criticality,
        "restart_policy": svc.restart_policy,
    }
    if "error" in status:
        err = str(status["error"])
        # Proxmox answers a deleted guest with 500 "Configuration file ...
        # does not exist". That is catalog drift, not a monitoring failure,
        # and it deserves a different word and a different fix.
        if "does not exist" in err or status.get("error") == "not_found":
            return {**row, "verdict": "missing",
                    "error": "guest no longer exists on Proxmox",
                    "hint": f"remove vmid {svc.vmid} from catalog.yaml"}
        return {**row, "verdict": "unknown", "error": err}

    source = status.get("mem_pct_source", "")
    mem = status.get("mem_pct")
    row.update({
        "status": status.get("status"),
        "cpu_pct": status.get("cpu_pct"),
        "mem_pct": mem,
        "mem_pct_source": source,
        "maxmem_mb": status.get("maxmem_mb"),
        "uptime_h": status.get("uptime_h"),
    })
    if status.get("mem_pct_warning"):
        row["mem_pct_warning"] = status["mem_pct_warning"]

    if status.get("status") != "running":
        row["verdict"] = "stopped"
    elif source in TRUSTED_SOURCES and mem is not None and mem >= MEM_WARN_PCT:
        row["verdict"] = "high_mem"
    elif source not in TRUSTED_SOURCES or status.get("mem_pct_warning"):
        # Reading exists but cannot be trusted — a monitoring gap, NOT a
        # memory problem. Do not let this become a restart argument.
        row["verdict"] = "mem_unreliable"
    else:
        row["verdict"] = "ok"
    return row


def scan_guests(deep: bool = True, max_workers: int = 8) -> Dict[str, Any]:
    """Status + memory + CPU for every catalogued guest, in parallel.

    Args:
        deep: ask each running guest for its real /proc/meminfo reading
            (one SSH round-trip each). False = host-side numbers only,
            much faster, memory marked unreliable for QEMU.

    Returns a digest: {total, running, stopped, high_mem, mem_unreliable,
    unknown, problems: [...], guests: [...]}.
    """
    cat = load_catalog()
    services = cat.services
    node = cat.proxmox_host.node
    if not services:
        return {"total": 0, "guests": [], "note": "no services in catalog"}

    def probe(svc):
        if deep:
            status = check_proxmox_status(node, svc.vmid)
        else:
            from tools import _format_status, _proxmox_get
            resp = _proxmox_get(
                f"/nodes/{node}/{svc.kind}/{svc.vmid}/status/current")
            status = resp if "error" in resp else _format_status(
                resp, svc.kind, svc.vmid)
        return _classify(svc, status)

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(probe, s): s for s in services}
        for fut in as_completed(futures):
            rows.append(fut.result())

    counts = {"ok": 0, "stopped": 0, "high_mem": 0,
              "mem_unreliable": 0, "missing": 0, "unknown": 0}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    # A finding is something an operator should act on tonight: an important
    # guest that is down, or a trustworthy high-memory reading anywhere.
    problems = [
        r for r in rows
        if (r["verdict"] == "stopped"
            and r["criticality"] in ALERT_CRITICALITIES)
        or r["verdict"] == "high_mem"
    ]

    order = {"stopped": 0, "high_mem": 1, "unknown": 2, "missing": 3,
             "mem_unreliable": 4, "ok": 5}
    crit = {"critical": 0, "high": 1, "medium": 2, "lab": 3}
    # Drift runs both ways: a guest can be deleted (verdict "missing") or
    # created without anyone updating catalog.yaml. The second kind is how
    # a service ends up with no backup SLA and no reachability probe, so
    # surface it — informational, never a page.
    untracked: List[Dict[str, Any]] = []
    try:
        from tools import list_proxmox_guests
        live = list_proxmox_guests(node)
        known = {s.vmid for s in services}
        untracked = [{"vmid": g["vmid"], "name": g.get("name"),
                      "kind": g.get("kind"), "status": g.get("status")}
                     for g in live.get("guests", []) if g.get("vmid") not in known]
    except Exception:
        pass

    return {
        "total": len(rows),
        "running": sum(1 for r in rows if r.get("status") == "running"),
        **counts,
        "mem_warn_pct": MEM_WARN_PCT,
        "untracked_guests": untracked,
        "problems": problems,
        "guests": sorted(rows, key=lambda r: (order[r["verdict"]],
                                              crit[r["criticality"]],
                                              r["name"])),
    }


def summarize_guests(data: Dict[str, Any]) -> str:
    """One-paragraph operator digest, with a deterministic fallback."""

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        if data.get("problems"):
            names = ", ".join(f"{p['name']} ({p['verdict']})"
                              for p in data["problems"])
            return prefix + (f"ATTENTION: {names}. "
                             f"{data.get('running', 0)}/{data['total']} guests running.")
        if data.get("mem_unreliable"):
            return prefix + (f"All {data['total']} guests look fine; "
                             f"{data['mem_unreliable']} have unreliable memory "
                             f"readings (install qemu-guest-agent).")
        if data.get("missing") or data.get("untracked_guests"):
            return prefix + (f"All live guests healthy, but catalog.yaml has drifted: "
                             f"{data.get('missing', 0)} cataloged guest(s) no longer exist, "
                             f"{len(data.get('untracked_guests') or [])} live guest(s) "
                             f"are not cataloged.")
        return prefix + f"All {data['total']} guests healthy."

    lines = []
    for g in data.get("guests", []):
        mem = (f"{g.get('mem_pct')}% ({g.get('mem_pct_source')})"
               if g.get("mem_pct") is not None else "-")
        lines.append(f"  [{g['verdict']:<14}] {g['name']:<15} {g['kind']:<5} "
                     f"{g['criticality']:<8} state={g.get('status', '?'):<8} "
                     f"mem={mem:<24} cpu={g.get('cpu_pct', '-')}%")
    prompt = (
        "You are summarizing Proxmox guest health for the operator. Write ONE "
        "short paragraph (2-3 sentences). Lead with anything stopped or with "
        "genuinely high memory, naming the guests. Treat 'mem_unreliable' as a "
        "MONITORING gap (no guest agent), never as a memory problem — do not "
        "suggest restarting for it. No preamble, no bullet points.\n\n"
        f"Counts: total={data['total']} running={data.get('running', 0)} "
        f"stopped={data.get('stopped', 0)} high_mem={data.get('high_mem', 0)} "
        f"mem_unreliable={data.get('mem_unreliable', 0)} "
        f"unknown={data.get('unknown', 0)}\n\n"
        f"Guests:\n" + "\n".join(lines) + "\n"
    )
    try:
        from models import helper_llm
        resp = helper_llm(temperature=0.2, max_tokens=220).invoke(prompt)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        return text.strip() or _fallback("helper_llm returned empty")
    except Exception as e:
        return _fallback(f"helper_llm unavailable: {e}")


def alert_transitions(data: Dict[str, Any]) -> Dict[str, Any]:
    """One Telegram message per change (alert_state.py): a guest that
    stops pages once, and once more when it is running again."""
    from alert_state import Finding, notify
    findings = []
    for p in data.get("problems", []):
        if p["verdict"] == "stopped":
            detail = f"state: {p.get('status', '?')}"
        else:
            detail = f"memory {p.get('mem_pct')}% (≥ {MEM_WARN_PCT:g}%, {p.get('mem_pct_source')})"
        findings.append(Finding(
            key=f"{p['vmid']}:{p['verdict']}",
            label=f"{p['name']} ({p['kind']} {p['vmid']})"
                  + (" high memory" if p["verdict"] == "high_mem" else ""),
            severity="critical" if p["criticality"] == "critical" else "high",
            detail=detail))
    return notify("guests", "🖥 Proxmox guests", findings)


def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'VERDICT':<15} {'NAME':<15} {'KIND':<5} {'CRIT':<9} "
          f"{'STATE':<9} {'MEM':<10} {'SOURCE':<13} {'CPU':<7} UPTIME_H")
    print("-" * 108)
    for g in data.get("guests", []):
        mem = f"{g.get('mem_pct')}%" if g.get("mem_pct") is not None else "-"
        print(f"{g['verdict']:<15} {g['name']:<15} {g['kind']:<5} "
              f"{g['criticality']:<9} {str(g.get('status', '?')):<9} {mem:<10} "
              f"{str(g.get('mem_pct_source', '-')):<13} "
              f"{str(g.get('cpu_pct', '-')):<7} {g.get('uptime_h', '-')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="HomelabSentinel Proxmox guest health")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--fast", action="store_true",
                    help="host-side numbers only (no per-guest SSH)")
    ap.add_argument("--alert", action="store_true",
                    help="Telegram on transitions: a guest stops/recovers or goes truly high on memory")
    ap.add_argument("--no-summary", action="store_true", help="skip the LLM digest")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    data = scan_guests(deep=not args.fast)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 2 if data.get("problems") else 0

    print(f"\nProxmox guests — {data['total']} total "
          f"({data.get('running', 0)} running / {data.get('stopped', 0)} stopped / "
          f"{data.get('high_mem', 0)} high-mem / "
          f"{data.get('mem_unreliable', 0)} unreliable-mem)\n")
    _print_table(data)

    if data.get("untracked_guests"):
        print("\nNot in catalog.yaml (drift):")
        for g in data["untracked_guests"]:
            print(f"  vmid {g['vmid']:<5} {str(g['name']):<15} {g['kind']:<5} {g['status']}")

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_guests(data))

    if data.get("total") and data.get("unknown") == data["total"]:
        # Every probe failed (API down, token revoked): the check is blind.
        # Exit 1 pages via OnFailure — silence here would read as "all fine".
        print("\nERROR: no guest could be read from Proxmox — check is blind.")
        return 1

    if args.alert:
        print(f"\nalerting: {alert_transitions(data)}")

    return 2 if data.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
