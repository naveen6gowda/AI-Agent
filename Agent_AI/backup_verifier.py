"""
Phase 2 / Feature #2 — backup freshness verifier.

For every service in catalog.yaml, look up its most recent backup in
each storage listed under catalog.proxmox_host.backup_storage. Compare
the backup's age to service.max_backup_age_h. Classify as fresh / stale
/ missing / skipped.

API used:
    GET /nodes/{node}/storage/{storage}/content?content=backup

Returns volids like:
    general-storage:backup/vzdump-qemu-100-2026_05_29-03_00_00.vma.zst
plus ctime (unix seconds), size, vmid.

CLI usage:
    uv run python backup_verifier.py
    uv run python backup_verifier.py --alert
    uv run python backup_verifier.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from catalog import load_catalog
from tools import _proxmox_get


# ----------------------------------------------------------------------
# Storage queries
# ----------------------------------------------------------------------
def _list_storage_backups(node: str, storage: str) -> Optional[List[Dict[str, Any]]]:
    """List every backup entry in a storage. None when the storage can't be
    read — which is NOT the same as "no backups": treating it as an empty
    list used to report every guest's backup as missing."""
    resp = _proxmox_get(f"/nodes/{node}/storage/{storage}/content?content=backup")
    if "error" in resp:
        return None
    return resp.get("data", []) or []


def _backups_by_vmid(node: str, storages: List[str],
                     unreadable: Optional[List[str]] = None) -> Dict[int, List[Dict]]:
    """Aggregate backups across multiple storages, keyed by vmid.

    Same vmid backed up to two different storages? Both kept in the list;
    we'll pick the most recent across all of them.
    """
    by_vmid: Dict[int, List[Dict]] = {}
    for storage in storages:
        entries = _list_storage_backups(node, storage)
        if entries is None:
            if unreadable is not None:
                unreadable.append(storage)
            continue
        for entry in entries:
            vmid = entry.get("vmid")
            if vmid is None:
                continue
            entry["_storage"] = storage
            by_vmid.setdefault(int(vmid), []).append(entry)
    return by_vmid


# ----------------------------------------------------------------------
# Per-service verification
# ----------------------------------------------------------------------
def _classify(svc, latest: Optional[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """Build the per-service result dict."""
    base = {
        "vmid": svc.vmid,
        "name": svc.name,
        "kind": svc.kind,
        "criticality": svc.criticality,
        "max_age_h": svc.max_backup_age_h,
    }

    # max_age_h == 0 means "I don't care about this VM's backups"
    if svc.max_backup_age_h == 0:
        return {**base, "status": "skipped"}

    if latest is None:
        return {**base, "status": "missing", "latest_backup": None}

    age_s = now - latest.get("ctime", 0)
    age_h = age_s / 3600.0
    info = {
        "volid": latest.get("volid"),
        "storage": latest.get("_storage"),
        "ctime": latest.get("ctime"),
        "age_h": round(age_h, 1),
        "size_mb": round(latest.get("size", 0) / 1024 / 1024, 0),
    }
    status = "fresh" if age_h <= svc.max_backup_age_h else "stale"
    return {**base, "status": status, "latest_backup": info}


def verify_backups() -> Dict[str, Any]:
    """Run the verification pass across every catalogued service.

    Returns:
        {
          "total":   N,
          "fresh":   ...,
          "stale":   ...,
          "missing": ...,
          "skipped": ...,
          "critical_problems": [...],   # critical services NOT fresh
          "results": [...]
        }
    """
    cat = load_catalog()
    node = cat.proxmox_host.node
    storages = cat.proxmox_host.backup_storage
    if not storages:
        return {"total": 0, "results": [],
                "note": "no backup_storage in catalog.proxmox_host"}

    unreadable: List[str] = []
    by_vmid = _backups_by_vmid(node, storages, unreadable)
    if len(unreadable) == len(storages):
        return {"error": f"could not read backup storage {', '.join(unreadable)} "
                         f"from Proxmox — backup freshness is unknown",
                "total": 0, "results": [], "critical_problems": [], "problems": [],
                "storages_checked": storages, "node": node}
    now = time.time()

    results: List[Dict[str, Any]] = []
    for svc in cat.services:
        candidates = by_vmid.get(svc.vmid, [])
        latest = max(candidates, key=lambda e: e.get("ctime", 0)) if candidates else None
        results.append(_classify(svc, latest, now))

    counts = {"fresh": 0, "stale": 0, "missing": 0, "skipped": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    critical_problems = [
        r for r in results
        if r["criticality"] == "critical" and r["status"] in ("stale", "missing")
    ]
    # What pages: critical AND high (catalog: high = alert in waking hours;
    # the check runs at 09:00). Debian13 holds Vaultwarden and Immich and is
    # "high" — its stale backup used to never reach the operator.
    problems = [
        r for r in results
        if r["criticality"] in ("critical", "high") and r["status"] in ("stale", "missing")
    ]

    return {
        "total": len(results),
        **counts,
        "critical_problems": critical_problems,
        "problems": problems,
        "storages_unreadable": unreadable,
        "storages_checked": storages,
        "node": node,
        # Sort: problems first, then by criticality desc, then by name.
        "results": sorted(
            results,
            key=lambda r: (
                r["status"] not in ("missing", "stale"),
                {"critical": 0, "high": 1, "medium": 2, "lab": 3}[r["criticality"]],
                r["name"],
            ),
        ),
    }


# ----------------------------------------------------------------------
# LLM digest (with same fallback pattern as reachability + smart)
# ----------------------------------------------------------------------
def summarize_verification(data: Dict[str, Any]) -> str:
    """One-paragraph digest. Falls back deterministically."""

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        if data.get("critical_problems"):
            names = ", ".join(r["name"] for r in data["critical_problems"])
            return prefix + (f"CRITICAL backup issue: {names}. "
                             f"Fresh {data.get('fresh',0)}/{data['total']}, "
                             f"stale {data.get('stale',0)}, "
                             f"missing {data.get('missing',0)}.")
        if data.get("stale") or data.get("missing"):
            return prefix + (f"{data.get('stale',0)} stale, "
                             f"{data.get('missing',0)} missing, "
                             f"{data.get('fresh',0)} fresh / "
                             f"{data['total']} total.")
        return prefix + f"All required backups fresh ({data.get('fresh',0)} of {data['total']})."

    lines = []
    for r in data["results"]:
        tag = {"fresh": "[OK]", "stale": "[STALE]", "missing": "[MISS]",
               "skipped": "[skip]"}[r["status"]]
        b = r.get("latest_backup") or {}
        age = f"age={b.get('age_h', '-')}h" if b else "no_backup"
        lines.append(f"  {tag:<8} {r['name']:<16} {r['criticality']:<9} "
                     f"max={r['max_age_h']}h  {age}")
    block = "\n".join(lines)

    prompt = (
        "You are summarizing a homelab backup-freshness check for the operator.\n"
        "Write ONE short paragraph (2-3 sentences). Lead with whether any critical "
        "service has a stale or missing backup. Mention specific service names. "
        "Do not invent details. No preamble, no headings, no bullet points.\n\n"
        f"Services ({data['total']} total: fresh={data.get('fresh',0)}, "
        f"stale={data.get('stale',0)}, missing={data.get('missing',0)}, "
        f"skipped={data.get('skipped',0)}):\n{block}\n"
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
def _print_table(data: Dict[str, Any]) -> None:
    print(f"{'STATUS':<8} {'NAME':<16} {'CRIT':<9} {'MAX_H':<7} "
          f"{'AGE_H':<8} {'SIZE_MB':<9} STORAGE / VOLID")
    print("-" * 100)
    for r in data["results"]:
        b = r.get("latest_backup") or {}
        age = b.get("age_h", "-")
        size = b.get("size_mb", "-")
        loc = f"{b.get('storage', '-')} / {b.get('volid', '')[:60]}" if b else "(no backup found)"
        print(f"{r['status']:<8} {r['name']:<16} {r['criticality']:<9} "
              f"{r['max_age_h']:<7} {str(age):<8} {str(size):<9} {loc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel backup verifier")
    parser.add_argument("--alert", action="store_true",
                        help="Telegram alert if any critical/high service is stale/missing")
    parser.add_argument("--no-summary", action="store_true",
                        help="skip helper_llm digest")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    data = verify_backups()

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        if data.get("error"):
            return 1
        return 2 if data.get("problems") else 0
    if data.get("error"):
        # The check is blind — exit 1 pages via OnFailure with this line.
        print(f"ERROR: {data['error']}")
        return 1

    print(f"\nBackup verification — {data['total']} services, "
          f"storages={data.get('storages_checked')}")
    print(f"  fresh={data.get('fresh',0)}  stale={data.get('stale',0)}  "
          f"missing={data.get('missing',0)}  skipped={data.get('skipped',0)}\n")
    _print_table(data)

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_verification(data))

    if args.alert and data.get("problems"):
        from tools import send_telegram_alert
        names = ", ".join(f"{r['name']} ({r['criticality']}, {r['status']})"
                          for r in data["problems"])
        msg = (f"⚠️ Backups behind: {names}\n\n"
               f"{summarize_verification(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")

    return 2 if data.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
