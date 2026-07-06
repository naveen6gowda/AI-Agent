"""
Phase 2 / Feature #4 — Service reachability sweep.

For every endpoint listed in catalog.yaml, probe it (HTTP/HTTPS GET or
TCP connect) in parallel, classify results, and (optionally) ask the
the local LLM LLM to write a one-paragraph operator digest.

CLI usage:
    uv run python reachability.py                    # all services
    uv run python reachability.py --criticality critical
    uv run python reachability.py --alert            # send Telegram if anything critical is down
    uv run python reachability.py --no-summary       # skip helper_llm call

Imported usage (also wired as @tool in agent_v5_approval.py):
    from reachability import sweep_services
    data = sweep_services(criticality="critical")
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

from catalog import Endpoint, Service, load_catalog

# Unlike the other monitors, reachability imports only from catalog — not from
# tools — so nothing else pulls in the .env. When run as a bare CLI
# (`python reachability.py`) that left auth_env probes (HA_TOKEN,
# LLAMACPP_API_KEY) looking "auth_missing". Load it here so the CLI matches the
# systemd EnvironmentFile path and the agent's in-process tool call.
load_dotenv()


# ----------------------------------------------------------------------
# Single-endpoint probe
# ----------------------------------------------------------------------
def probe_endpoint(svc: Service, ep: Endpoint) -> Dict[str, Any]:
    """Probe one endpoint. Always returns a structured dict, never raises.

    Status values:
      "up"        — got expected status (HTTP) or socket connected (TCP)
      "wrong_code" — HTTP responded but with unexpected status
      "auth_missing" — endpoint requires auth_env that isn't set
      "down"      — connection refused, timed out, or any other error
    """
    started = time.perf_counter()
    timeout = svc.reachability_timeout_s
    base = {
        "service": svc.name,
        "vmid": svc.vmid,
        "criticality": svc.criticality,
        "endpoint": ep.name,
        "proto": ep.proto,
        "host": ep.host,
        "port": ep.port,
    }

    if ep.proto == "tcp":
        try:
            with socket.create_connection((ep.host, ep.port), timeout=timeout):
                return {**base, "status": "up",
                        "latency_ms": int((time.perf_counter() - started) * 1000)}
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            return {**base, "status": "down", "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.perf_counter() - started) * 1000)}

    # HTTP / HTTPS
    url = f"{ep.proto}://{ep.host}:{ep.port}{ep.path}"
    headers: Dict[str, str] = {}
    if ep.auth_env:
        token = os.getenv(ep.auth_env)
        if not token:
            return {**base, "url": url, "status": "auth_missing",
                    "error": f"env var {ep.auth_env} not set"}
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = httpx.get(url, headers=headers, verify=ep.verify_tls, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if r.status_code == ep.expected_status:
            return {**base, "url": url, "status": "up",
                    "code": r.status_code, "latency_ms": elapsed_ms}
        return {**base, "url": url, "status": "wrong_code",
                "code": r.status_code, "expected": ep.expected_status,
                "latency_ms": elapsed_ms}
    except httpx.TimeoutException:
        return {**base, "url": url, "status": "down", "error": "timeout",
                "latency_ms": int((time.perf_counter() - started) * 1000)}
    except httpx.HTTPError as e:
        return {**base, "url": url, "status": "down",
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.perf_counter() - started) * 1000)}


# ----------------------------------------------------------------------
# Parallel sweep across all services
# ----------------------------------------------------------------------
def sweep_services(criticality: Optional[str] = None,
                   max_workers: int = 16) -> Dict[str, Any]:
    """Probe every endpoint of every service (optionally filtered).

    Returns a structured digest:
        {
          "total":        9,
          "up":           7,
          "down":         1,
          "wrong_code":   1,
          "auth_missing": 0,
          "critical_down": [...],     # subset where service.criticality == critical
          "results":      [...]       # all per-endpoint dicts
        }
    """
    cat = load_catalog()
    services = cat.filter(criticality=criticality or None, with_endpoints=True)

    probes: List = []
    for svc in services:
        for ep in svc.endpoints:
            probes.append((svc, ep))

    if not probes:
        return {"total": 0, "results": [], "note": "no endpoints in catalog"}

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(probe_endpoint, s, e): (s, e) for s, e in probes}
        for fut in as_completed(futures):
            results.append(fut.result())

    counts = {"up": 0, "down": 0, "wrong_code": 0, "auth_missing": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    critical_down = [
        r for r in results
        if r["criticality"] == "critical" and r["status"] != "up"
    ]

    return {
        "total": len(results),
        **counts,
        "critical_down": critical_down,
        "results": sorted(results, key=lambda r: (r["criticality"] != "critical",
                                                   r["status"] == "up",
                                                   r["service"])),
    }


# ----------------------------------------------------------------------
# the local LLM summarizer — uses helper_llm, never crashes the sweep
# ----------------------------------------------------------------------
def summarize_sweep(data: Dict[str, Any]) -> str:
    """Ask the local LLM helper to write a 2-3 sentence operator digest.

    If helper_llm() is unreachable (llama-server down), returns a
    plain-text fallback so the sweep is still useful.
    """
    # Build a compact representation for the prompt. We don't dump the
    # entire results list — a small local model has limited context and gets confused
    # by big JSON. Hand it a per-line summary.
    lines = []
    for r in data["results"]:
        icon = "OK" if r["status"] == "up" else "FAIL"
        loc = f"{r['service']}/{r['endpoint']}"
        lat = f"{r.get('latency_ms', '-')}ms"
        detail = (
            f"code={r.get('code')}" if r["status"] in ("up", "wrong_code")
            else r.get("error", "?")
        )
        lines.append(f"  [{icon}] {loc:<35} {r['criticality']:<8} {lat:<7} {detail}")

    block = "\n".join(lines)
    counts_line = (
        f"total={data['total']}  up={data.get('up',0)}  "
        f"down={data.get('down',0)}  wrong_code={data.get('wrong_code',0)}  "
        f"auth_missing={data.get('auth_missing',0)}"
    )

    prompt = (
        "You are summarizing a homelab service reachability check for the operator.\n"
        "Write ONE short paragraph (2-3 sentences max). Lead with whether anything "
        "critical is down. Mention specific service names. Do not invent details. "
        "No preamble, no bullet points, no headings.\n\n"
        f"Counts: {counts_line}\n\n"
        f"Per-endpoint:\n{block}\n"
    )

    def _fallback(reason: str) -> str:
        prefix = f"[{reason}] "
        if data.get("critical_down"):
            names = ", ".join({r["service"] for r in data["critical_down"]})
            return (prefix + f"CRITICAL DOWN: {names}. "
                    f"{data.get('up',0)}/{data['total']} endpoints up.")
        if data.get("down", 0) + data.get("wrong_code", 0) > 0:
            return (prefix + f"Some non-critical failures. "
                    f"{data.get('up',0)}/{data['total']} endpoints up.")
        return prefix + f"All {data['total']} endpoints up."

    try:
        from models import helper_llm  # local import — keep sweep usable if models fails
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
    print(f"{'STATUS':<14} {'CRIT':<9} {'SERVICE':<18} "
          f"{'ENDPOINT':<22} {'LAT':<7} DETAIL")
    print("-" * 95)
    for r in data["results"]:
        detail = (
            f"code={r.get('code')}" if r.get("code") is not None
            else r.get("error", "")
        )
        print(f"{r['status']:<14} {r['criticality']:<9} "
              f"{r['service']:<18} {r['endpoint']:<22} "
              f"{r.get('latency_ms','-'):<7} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel reachability sweep")
    parser.add_argument("--criticality", choices=["critical", "high", "medium", "lab"],
                        help="filter to one criticality level")
    parser.add_argument("--alert", action="store_true",
                        help="send Telegram alert when any critical endpoint is down")
    parser.add_argument("--no-summary", action="store_true",
                        help="skip the helper_llm summary call")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw structured result as JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    data = sweep_services(criticality=args.criticality)

    if args.json:
        import json
        print(json.dumps(data, indent=2))
        return 0 if not data.get("critical_down") else 2

    print(f"\nReachability sweep — {data['total']} endpoints "
          f"({data.get('up',0)} up / {data.get('down',0)} down / "
          f"{data.get('wrong_code',0)} wrong_code / "
          f"{data.get('auth_missing',0)} auth_missing)\n")
    _print_table(data)

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_sweep(data))

    if args.alert and data.get("critical_down"):
        from tools import send_telegram_alert
        names = ", ".join({r["service"] for r in data["critical_down"]})
        msg = (f"⚠️ Reachability: CRITICAL services unreachable — {names}\n\n"
               f"{summarize_sweep(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")

    # Exit code: 0 healthy, 2 critical down (useful for cron/systemd).
    return 2 if data.get("critical_down") else 0


if __name__ == "__main__":
    sys.exit(main())
