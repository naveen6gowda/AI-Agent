"""
Internet speed (WAN throughput) monitor.

Measures the homelab's live internet speed — download, upload, and latency —
by transferring sized payloads against Cloudflare's public speed endpoints
(speed.cloudflare.com). Pure httpx: no speedtest-cli, no Ookla binary, no extra
dependency (httpx is already used by reachability.py / tools.py). It runs from
wherever the agent runs, so it reflects that host's view of the WAN.

Results are classified healthy / warning / critical against configurable floors
(download/upload) and a ceiling (latency); "unknown" means the test couldn't
reach Cloudflare at all — treat that as WAN-down.

CLI usage:
    uv run python speedtest_monitor.py
    uv run python speedtest_monitor.py --json
    uv run python speedtest_monitor.py --alert        # Telegram if critical/down
    uv run python speedtest_monitor.py --no-summary
    uv run python speedtest_monitor.py --download-mb 25 --upload-mb 10

Imported usage (also wired as @tool check_internet_speed in agent_v5_approval.py):
    from speedtest_monitor import run_speedtest
    data = run_speedtest()
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

# Like reachability.py, this monitor imports only stdlib + httpx, so nothing
# else pulls in the .env. Load it here so the bare CLI matches the systemd
# EnvironmentFile path and the agent's in-process tool call.
load_dotenv()


# ----------------------------------------------------------------------
# Cloudflare speed endpoints (public, no auth). Override the base only if you
# run your own equivalent (e.g. a LibreSpeed backend on the LAN).
# ----------------------------------------------------------------------
_CF_BASE = os.getenv("SPEEDTEST_BASE_URL", "https://speed.cloudflare.com").rstrip("/")
_DOWN_URL = f"{_CF_BASE}/__down"
_UP_URL = f"{_CF_BASE}/__up"
_META_URL = f"{_CF_BASE}/meta"
_TRACE_URL = f"{_CF_BASE}/cdn-cgi/trace"


# ----------------------------------------------------------------------
# Thresholds — homelab defaults; tune to your plan via .env. Floors for
# throughput (below = bad), ceiling for latency (above = bad).
# ----------------------------------------------------------------------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


DOWNLOAD_WARN_MBPS = _envf("SPEEDTEST_DOWNLOAD_WARN_MBPS", 100.0)
DOWNLOAD_CRIT_MBPS = _envf("SPEEDTEST_DOWNLOAD_CRIT_MBPS", 50.0)
UPLOAD_WARN_MBPS   = _envf("SPEEDTEST_UPLOAD_WARN_MBPS", 20.0)
UPLOAD_CRIT_MBPS   = _envf("SPEEDTEST_UPLOAD_CRIT_MBPS", 10.0)
LATENCY_WARN_MS    = _envf("SPEEDTEST_LATENCY_WARN_MS", 60.0)
LATENCY_CRIT_MS    = _envf("SPEEDTEST_LATENCY_CRIT_MS", 150.0)

# Payload sizes (bytes) and per-request timeout. Smaller = faster but noisier;
# bump down on a slow link so the transfer still finishes inside the timeout.
DOWNLOAD_BYTES = int(_envf("SPEEDTEST_DOWNLOAD_BYTES", 10_000_000))
UPLOAD_BYTES   = int(_envf("SPEEDTEST_UPLOAD_BYTES", 5_000_000))
TIMEOUT_S      = _envf("SPEEDTEST_TIMEOUT_S", 30.0)

_LATENCY_SAMPLES = 5  # the first is discarded as TLS/connection warm-up


@dataclass
class Alert:
    severity: str   # "critical" | "warning"
    field: str
    value: Any
    note: str = ""


def _mbps(num_bytes: int, seconds: float) -> float:
    """Bytes over seconds → megabits-per-second (decimal, as ISPs quote)."""
    if seconds <= 0:
        return 0.0
    return round((num_bytes * 8) / (seconds * 1_000_000), 1)


# ----------------------------------------------------------------------
# Individual measurements — each returns a dict and never raises. They share
# one httpx.Client so the TLS connection (opened by the latency warm-up) is
# reused for the throughput tests, keeping handshake cost out of the numbers.
# ----------------------------------------------------------------------
def _measure_latency(client: httpx.Client) -> Dict[str, Any]:
    """Idle latency to the speed edge: small GETs, drop the warm-up sample.

    Returns {latency_ms, jitter_ms, samples} or {error}.
    """
    times: List[float] = []
    for i in range(_LATENCY_SAMPLES):
        t0 = time.perf_counter()
        try:
            client.get(_DOWN_URL, params={"bytes": 0})
        except httpx.HTTPError as e:
            if i == 0:
                return {"error": f"{type(e).__name__}: {e}"}
            continue
        times.append((time.perf_counter() - t0) * 1000)
    samples = times[1:] or times  # discard warm-up when we have more than one
    if not samples:
        return {"error": "no latency samples"}
    return {
        "latency_ms": round(min(samples), 1),
        "jitter_ms": round(statistics.pstdev(samples), 1) if len(samples) > 1 else 0.0,
        "samples": len(samples),
    }


def _measure_download(client: httpx.Client, num_bytes: int) -> Dict[str, Any]:
    """Stream `num_bytes` from Cloudflare; return {download_mbps, ...} or {error}."""
    try:
        t0 = time.perf_counter()
        got = 0
        with client.stream("GET", _DOWN_URL, params={"bytes": num_bytes}) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                got += len(chunk)
        elapsed = time.perf_counter() - t0
        return {"download_mbps": _mbps(got, elapsed),
                "bytes_down": got, "down_s": round(elapsed, 2)}
    except httpx.HTTPError as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _measure_upload(client: httpx.Client, num_bytes: int) -> Dict[str, Any]:
    """POST `num_bytes` to Cloudflare; return {upload_mbps, ...} or {error}."""
    payload = b"\x00" * num_bytes  # zeros: cheap to build, not compressed on the wire
    try:
        t0 = time.perf_counter()
        r = client.post(_UP_URL, content=payload,
                        headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()
        elapsed = time.perf_counter() - t0
        return {"upload_mbps": _mbps(num_bytes, elapsed),
                "bytes_up": num_bytes, "up_s": round(elapsed, 2)}
    except httpx.HTTPError as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_meta(client: httpx.Client) -> Dict[str, Any]:
    """Best-effort context: Cloudflare edge colo + client IP/ISP. Never fatal.

    cdn-cgi/trace is served by every Cloudflare host and reliably returns the
    edge 'colo' + client 'ip'. The richer /meta endpoint also gives the client's
    ISP (asOrganization) but commonly 403s for non-browser clients, so ISP is
    treated as optional.
    """
    meta: Dict[str, Any] = {}
    try:
        r = client.get(_TRACE_URL)
        r.raise_for_status()
        trace = dict(line.split("=", 1)
                     for line in r.text.splitlines() if "=" in line)
        if trace.get("colo"):
            meta["server"] = trace["colo"]
        if trace.get("ip"):
            meta["client_ip"] = trace["ip"]
    except (httpx.HTTPError, ValueError):
        pass
    try:
        r = client.get(_META_URL)
        if r.status_code == 200:
            m = r.json()
            if m.get("asOrganization"):
                meta["isp"] = m["asOrganization"]
            meta.setdefault("server", m.get("colo"))
    except (httpx.HTTPError, ValueError):
        pass
    return meta


# ----------------------------------------------------------------------
# Classify a finished measurement into alerts + a rolled-up status.
# ----------------------------------------------------------------------
def _classify(down: Optional[float], up: Optional[float],
              latency: Optional[float]) -> List[Alert]:
    alerts: List[Alert] = []
    if down is not None:
        if down < DOWNLOAD_CRIT_MBPS:
            alerts.append(Alert("critical", "download_mbps", down,
                                f"< {DOWNLOAD_CRIT_MBPS} Mbps floor"))
        elif down < DOWNLOAD_WARN_MBPS:
            alerts.append(Alert("warning", "download_mbps", down,
                                f"< {DOWNLOAD_WARN_MBPS} Mbps"))
    if up is not None:
        if up < UPLOAD_CRIT_MBPS:
            alerts.append(Alert("critical", "upload_mbps", up,
                                f"< {UPLOAD_CRIT_MBPS} Mbps floor"))
        elif up < UPLOAD_WARN_MBPS:
            alerts.append(Alert("warning", "upload_mbps", up,
                                f"< {UPLOAD_WARN_MBPS} Mbps"))
    if latency is not None:
        if latency >= LATENCY_CRIT_MS:
            alerts.append(Alert("critical", "latency_ms", latency,
                                f">= {LATENCY_CRIT_MS} ms"))
        elif latency >= LATENCY_WARN_MS:
            alerts.append(Alert("warning", "latency_ms", latency,
                                f">= {LATENCY_WARN_MS} ms"))
    return alerts


def run_speedtest(download_bytes: Optional[int] = None,
                  upload_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Measure WAN download/upload/latency. Always returns a dict, never raises.

    Status:
      "healthy"  — all metrics within thresholds
      "warning"  — at least one metric in the warning band
      "critical" — a metric below its critical floor / above the latency ceiling
      "unknown"  — couldn't reach the speed endpoint at all (WAN likely down)

    Returns:
        {status, download_mbps, upload_mbps, latency_ms, jitter_ms,
         server, isp, alerts: [...], bytes_down, bytes_up, elapsed_s}
    """
    dbytes = download_bytes or DOWNLOAD_BYTES
    ubytes = upload_bytes or UPLOAD_BYTES
    started = time.perf_counter()
    result: Dict[str, Any] = {"endpoint": _CF_BASE}

    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True,
                      headers={"User-Agent": "HomelabSentinel-speedtest"}) as client:
        result.update(_fetch_meta(client))
        lat = _measure_latency(client)
        dn = _measure_download(client, dbytes)
        up = _measure_upload(client, ubytes)

    errors = [d["error"] for d in (lat, dn, up) if d.get("error")]
    for d in (lat, dn, up):
        result.update({k: v for k, v in d.items() if k != "error"})

    # No usable metric at all → the WAN is effectively down.
    if (dn.get("download_mbps") is None and up.get("upload_mbps") is None
            and lat.get("latency_ms") is None):
        result.update({
            "status": "unknown",
            "alerts": [{"severity": "critical", "field": "connectivity",
                        "value": "unreachable",
                        "note": "; ".join(errors) or "no metrics collected"}],
            "elapsed_s": round(time.perf_counter() - started, 2),
        })
        return result

    alerts = _classify(dn.get("download_mbps"), up.get("upload_mbps"),
                       lat.get("latency_ms"))
    if errors:
        failed = ", ".join(name for name, d in
                           (("latency", lat), ("download", dn), ("upload", up))
                           if d.get("error"))
        alerts.append(Alert("warning", "partial", failed,
                            "some sub-tests failed; see errors"))
        result["errors"] = errors

    if any(a.severity == "critical" for a in alerts):
        status = "critical"
    elif any(a.severity == "warning" for a in alerts):
        status = "warning"
    else:
        status = "healthy"

    result["status"] = status
    result["alerts"] = [{"severity": a.severity, "field": a.field,
                         "value": a.value, "note": a.note} for a in alerts]
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    return result


# ----------------------------------------------------------------------
# Gemma digest — falls back deterministically if the local LLM is down.
# ----------------------------------------------------------------------
def _fallback_summary(data: Dict[str, Any], reason: str) -> str:
    prefix = f"[{reason}] "
    if data.get("status") == "unknown":
        return prefix + "Internet appears DOWN — speed endpoint unreachable."
    head = (f"{data.get('download_mbps', '?')} Mbps down / "
            f"{data.get('upload_mbps', '?')} Mbps up / "
            f"{data.get('latency_ms', '?')} ms.")
    crit = [a["field"] for a in data.get("alerts", []) if a["severity"] == "critical"]
    if crit:
        return prefix + f"DEGRADED ({', '.join(crit)} below floor). " + head
    if data.get("alerts"):
        return prefix + "Slower than target but usable. " + head
    return prefix + "Internet healthy. " + head


def summarize_speedtest(data: Dict[str, Any]) -> str:
    """One-sentence operator digest from local Gemma; deterministic fallback."""
    alert_str = "; ".join(
        f"{a['severity']} {a['field']}={a['value']} ({a['note']})"
        for a in data.get("alerts", [])
    ) or "none"
    prompt = (
        "You are summarizing a homelab internet-speed check for the operator.\n"
        "Write ONE short sentence. Lead with healthy/slow/down, then the numbers. "
        "Do not invent details. No preamble, no bullet points, no headings.\n\n"
        f"status={data.get('status')}  download={data.get('download_mbps')} Mbps  "
        f"upload={data.get('upload_mbps')} Mbps  latency={data.get('latency_ms')} ms  "
        f"jitter={data.get('jitter_ms')} ms  via={data.get('server')}  "
        f"isp={data.get('isp')}\nalerts: {alert_str}\n"
    )
    try:
        from models import helper_llm  # local import — keep usable if models fails
        llm = helper_llm(temperature=0.2, max_tokens=160)
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
def _print_report(data: Dict[str, Any]) -> None:
    tag = {"healthy": "[OK]", "warning": "[WARN]",
           "critical": "[CRIT]", "unknown": "[DOWN]"}.get(data.get("status"), "[?]")
    print(f"\nInternet speed {tag}  (via {data.get('server') or '?'}"
          f" · {data.get('isp') or 'unknown ISP'})\n")
    print(f"  Download : {data.get('download_mbps', '-')} Mbps")
    print(f"  Upload   : {data.get('upload_mbps', '-')} Mbps")
    print(f"  Latency  : {data.get('latency_ms', '-')} ms "
          f"(jitter {data.get('jitter_ms', '-')} ms)")
    print(f"  Took     : {data.get('elapsed_s', '-')} s")
    for a in data.get("alerts", []):
        note = f" — {a['note']}" if a.get("note") else ""
        print(f"    ↳ {a['severity']:<8} {a['field']}={a['value']}{note}")
    for e in data.get("errors", []):
        print(f"    ↳ error: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel internet speed test")
    parser.add_argument("--alert", action="store_true",
                        help="send Telegram alert if status is critical/down")
    parser.add_argument("--no-summary", action="store_true",
                        help="skip the helper_llm summary call")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw structured result as JSON only")
    parser.add_argument("--download-mb", type=float, default=None,
                        help="override download payload size (MB)")
    parser.add_argument("--upload-mb", type=float, default=None,
                        help="override upload payload size (MB)")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    dbytes = int(args.download_mb * 1_000_000) if args.download_mb else None
    ubytes = int(args.upload_mb * 1_000_000) if args.upload_mb else None
    data = run_speedtest(download_bytes=dbytes, upload_bytes=ubytes)

    if args.json:
        print(json.dumps(data, indent=2))
        return 2 if data.get("status") in ("critical", "unknown") else 0

    _print_report(data)

    if not args.no_summary:
        print("\n--- Gemma digest ---")
        print(summarize_speedtest(data))

    if args.alert and data.get("status") in ("critical", "unknown"):
        from tools import send_telegram_alert
        msg = (f"⚠️ Internet speed {data.get('status').upper()} — "
               f"{data.get('download_mbps', '?')}↓ / "
               f"{data.get('upload_mbps', '?')}↑ Mbps, "
               f"{data.get('latency_ms', '?')}ms\n\n{summarize_speedtest(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")

    # Exit code: 0 healthy/warning, 2 critical or down (useful for systemd).
    return 2 if data.get("status") in ("critical", "unknown") else 0


if __name__ == "__main__":
    sys.exit(main())
