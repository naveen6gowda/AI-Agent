"""
speedtest_monitor.py — Internet speed watcher.

Runs a speed test (download / upload / ping) against Cloudflare's anycast
speed endpoint, classifies download against a threshold, and (optionally)
sends a Telegram alert when it's too slow. Like the other monitors it
summarizes on the the LOCAL LLM — ZERO cloud tokens.

WHY NOT `speedtest-cli` (replaced 2026-07-27): the pure-Python library is
abandoned (2.1.3, 2021) and Ookla deprecated the `speedtest-servers-*.php`
endpoints it discovers servers through. The returned pool had degraded to a
handful of arbitrary hosts, so across 30 runs / 14 days it NEVER picked one
of the 10 servers Ookla lists within 167 km — it tested against Nairobi,
Libreville, Johannesburg, Cape Town, Manama… and reported the resulting
intercontinental congestion as "your internet is slow". Nine false pages.
Worse, `get_best_server()` scores a failed latency probe as 3600 and averages
`sum(cum)/6`, so a server it could not reach AT ALL surfaced as a literal
`ping 1800000.0 ms` — and it then measured throughput to that host anyway.

Cloudflare is anycast: every request lands on the nearest PoP (FRA/MUC from
here), so there is no server selection to get wrong. `cf-meta-colo` tells us
which PoP answered, which is the honest analogue of Ookla's "server" field.

NB: a speed test SATURATES the link for ~30s and uses real bandwidth, so
don't run it on a tight schedule — every few hours is plenty.

CLI:
    uv run python speedtest_monitor.py
    uv run python speedtest_monitor.py --json
    uv run python speedtest_monitor.py --alert            # Telegram if too slow
    uv run python speedtest_monitor.py --min-download 50  # override threshold
    uv run python speedtest_monitor.py --no-summary

Imported (wired as @tool in agent_v5_approval.py):
    from speedtest_monitor import run_speedtest
    data = run_speedtest()
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()


# Config via env so you can tune the threshold without editing code —
# same pattern as APPROVAL_TIMEOUT_S etc. Default: 20 Mbps download.
MIN_DOWNLOAD_MBPS = float(os.getenv("SPEEDTEST_MIN_DOWNLOAD_MBPS", "20"))
# Transient DNS/TLS/PoP hiccups happen BEFORE any bandwidth is used, so
# retrying is cheap and keeps a blip from failing the systemd unit and
# paging the operator.
ATTEMPTS = max(1, int(os.getenv("SPEEDTEST_ATTEMPTS", "3")))
RETRY_WAIT_S = float(os.getenv("SPEEDTEST_RETRY_WAIT_S", "30"))

CF_DOWN_URL = os.getenv("SPEEDTEST_CF_DOWN", "https://speed.cloudflare.com/__down")
CF_UP_URL = os.getenv("SPEEDTEST_CF_UP", "https://speed.cloudflare.com/__up")
# Sized for a ~57 Mbit line: 50 MB ≈ 7s down, 20 MB ≈ 8s up. Enough to get
# past TCP slow-start without hogging the link for long.
DOWNLOAD_BYTES = int(os.getenv("SPEEDTEST_DOWNLOAD_BYTES", "50000000"))
UPLOAD_BYTES = int(os.getenv("SPEEDTEST_UPLOAD_BYTES", "20000000"))
LATENCY_SAMPLES = int(os.getenv("SPEEDTEST_LATENCY_SAMPLES", "5"))
HTTP_TIMEOUT_S = float(os.getenv("SPEEDTEST_HTTP_TIMEOUT_S", "60"))

# Sanity ceiling/floor. A reading outside these is not a slow link, it is a
# broken measurement — report it as an ERROR (exit 1, pages via OnFailure)
# rather than quietly alerting "internet slow" on a number that cannot be
# real. This is exactly what the old speedtest-cli path got wrong: it
# published ping=1800000 and download=0.0 as if they were speeds.
MAX_PLAUSIBLE_PING_MS = float(os.getenv("SPEEDTEST_MAX_PING_MS", "1000"))


def _server_time_ms(resp) -> float:
    """Server-side processing time from the `Server-Timing` header, in ms.

    speed.cloudflare.com reports e.g. `cfSpeedEdge;dur=8, cfSpeedWorker;dur=14`.
    Subtracting it is what Cloudflare's own client does — without it the
    "ping" is really RTT + ~22 ms of edge work, which reads as a much worse
    link than you have (measured 34 ms vs 7 ms actual ICMP here).
    """
    total = 0.0
    for part in resp.headers.get("Server-Timing", "").split(","):
        if "dur=" in part:
            try:
                total += float(part.split("dur=")[1].split(";")[0].strip(' "'))
            except (ValueError, IndexError):
                pass  # unparseable segment → count it as zero, never crash
    return total


def _measure_latency(sess) -> tuple[float, str]:
    """Median network RTT in ms to the nearest Cloudflare PoP, plus its colo.

    Deliberately NO sentinel value for a failed probe: if the endpoint can't
    be reached we let the exception escape so the caller records an ERROR.
    Inventing a number here is precisely how the old implementation ended up
    reporting `ping 1800000.0 ms` as though it were a measurement.
    """
    samples: list[float] = []
    colo = ""
    # +1 sample, first discarded: it carries DNS + TCP + TLS setup cost that
    # the later ones (same keep-alive connection) don't.
    for i in range(LATENCY_SAMPLES + 1):
        t0 = time.perf_counter()
        # Not streamed, so requests has already drained the (empty) body by
        # the time this returns — the elapsed span is a true round trip.
        resp = sess.get(f"{CF_DOWN_URL}?bytes=0", timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if not colo:
            # `colo` is the documented header; CF-RAY's `-XXX` suffix is the
            # fallback (both said MUC when this was written).
            colo = resp.headers.get("colo", "") or \
                resp.headers.get("CF-RAY", "").rpartition("-")[2]
        if i > 0:
            samples.append(max(0.0, elapsed_ms - _server_time_ms(resp)))
    return round(statistics.median(samples), 1), colo


def _measure_download(sess) -> float:
    """Download throughput in BITS per second."""
    resp = sess.get(f"{CF_DOWN_URL}?bytes={DOWNLOAD_BYTES}",
                    stream=True, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    # Clock starts AFTER headers land, so DNS/TLS/TTFB don't depress the
    # number — we want link throughput, not time-to-first-byte.
    t0 = time.perf_counter()
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
    elapsed = time.perf_counter() - t0
    if elapsed <= 0 or total == 0:
        raise RuntimeError(f"download probe returned {total} bytes in {elapsed:.4f}s")
    return (total * 8) / elapsed


def _measure_upload(sess) -> float:
    """Upload throughput in BITS per second."""
    payload = b"\0" * UPLOAD_BYTES
    t0 = time.perf_counter()
    resp = sess.post(CF_UP_URL, data=payload, timeout=HTTP_TIMEOUT_S,
                     headers={"Content-Type": "application/octet-stream"})
    resp.raise_for_status()
    elapsed = time.perf_counter() - t0
    if elapsed <= 0:
        raise RuntimeError("upload probe completed in zero time")
    return (UPLOAD_BYTES * 8) / elapsed


def run_speedtest(min_download_mbps: float | None = None) -> Dict[str, Any]:
    """Run ONE speed test; return a structured dict. Never raises.

    Success: {"status": "ok"|"slow", "download_mbps": .., "upload_mbps": ..,
              "ping_ms": .., "server": "..", "threshold_mbps": ..}
    Failure: {"status": "error", "error": ".."}
    """
    threshold = MIN_DOWNLOAD_MBPS if min_download_mbps is None else min_download_mbps

    # Lazy import so the module still loads (and the agent still starts) even
    # if the dependency is missing — we just return a clean error.
    try:
        import requests
    except ImportError:
        return {"status": "error",
                "error": "requests not installed — run `uv add requests`"}

    last_err = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with requests.Session() as sess:
                ping_ms, colo = _measure_latency(sess)
                download_mbps = round(_measure_download(sess) / 1_000_000, 1)
                upload_mbps = round(_measure_upload(sess) / 1_000_000, 1)
            break
        except Exception as e:
            # Any network hiccup → structured error, never a crash.
            last_err = f"{type(e).__name__}: {e}"
            if attempt < ATTEMPTS:
                print(f"speedtest attempt {attempt}/{ATTEMPTS} failed "
                      f"({last_err}) — retrying in {RETRY_WAIT_S:.0f}s",
                      file=sys.stderr)
                time.sleep(RETRY_WAIT_S)
    else:
        return {"status": "error", "attempts": ATTEMPTS, "error": last_err}

    server = f"Cloudflare — {colo}" if colo else "Cloudflare — ?"

    # Sanity gate BEFORE classification. An impossible reading must never be
    # dressed up as "internet slow" — that is a broken probe, and the
    # operator needs to know the CHECK failed, not that the LINK is bad.
    implausible = None
    if ping_ms > MAX_PLAUSIBLE_PING_MS:
        implausible = f"ping {ping_ms} ms exceeds {MAX_PLAUSIBLE_PING_MS:.0f} ms"
    elif download_mbps <= 0:
        implausible = f"download measured {download_mbps} Mbps"
    elif upload_mbps <= 0:
        implausible = f"upload measured {upload_mbps} Mbps"
    if implausible:
        return {
            "status": "error",
            "error": (f"implausible measurement ({implausible}) — treating as a "
                      f"failed probe, not a slow link. "
                      f"down={download_mbps} up={upload_mbps} ping={ping_ms} "
                      f"via {server}"),
            "download_mbps": download_mbps,
            "upload_mbps": upload_mbps,
            "ping_ms": ping_ms,
            "server": server,
            "threshold_mbps": threshold,
        }

    return {
        "status": "slow" if download_mbps < threshold else "ok",
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": ping_ms,
        "server": server,
        "threshold_mbps": threshold,
    }


def summarize_speedtest(data: Dict[str, Any]) -> str:
    """One-sentence operator digest via the local LLM. Deterministic fallback
    if the local LLM is down — so this NEVER fails the way the other monitors don't."""
    if data.get("status") == "error":
        return f"Speed test failed: {data.get('error')}"

    def _fallback(reason: str) -> str:
        verdict = "BELOW threshold" if data["status"] == "slow" else "OK"
        return (f"[{reason}] Download {data['download_mbps']} Mbps ({verdict}, "
                f"min {data['threshold_mbps']}), upload {data['upload_mbps']} Mbps, "
                f"ping {data['ping_ms']} ms via {data['server']}.")

    prompt = (
        "You are summarizing a home internet speed test for the operator.\n"
        "Write ONE short sentence. Lead with whether download is below the "
        "threshold. Mention the numbers. No preamble, no bullet points.\n\n"
        f"download_mbps={data['download_mbps']} (threshold {data['threshold_mbps']})\n"
        f"upload_mbps={data['upload_mbps']}  ping_ms={data['ping_ms']}\n"
        f"server={data['server']}  status={data['status']}\n"
    )
    try:
        from models import helper_llm
        llm = helper_llm(temperature=0.2, max_tokens=120)
        resp = llm.invoke(prompt)
        text = (resp.content if isinstance(resp.content, str) else str(resp.content)).strip()
        return text or _fallback("helper_llm returned empty")
    except Exception as e:
        return _fallback(f"helper_llm unavailable: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="HomelabSentinel internet speed test")
    parser.add_argument("--alert", action="store_true",
                        help="send a Telegram alert if download is below threshold")
    parser.add_argument("--min-download", type=float, default=None,
                        help=f"download threshold in Mbps (default {MIN_DOWNLOAD_MBPS})")
    parser.add_argument("--no-summary", action="store_true", help="skip the LLM digest")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    data = run_speedtest(min_download_mbps=args.min_download)

    if args.json:
        import json
        print(json.dumps(data, indent=2))
        # 1 = the check itself broke (pages via OnFailure); 2 = slow,
        # already self-alerted (units declare SuccessExitStatus=2).
        if data.get("status") == "error":
            return 1
        return 2 if data.get("status") == "slow" else 0

    if data.get("status") == "error":
        print(f"ERROR: {data['error']}")
        return 1

    print(f"\nInternet speed — download {data['download_mbps']} Mbps / "
          f"upload {data['upload_mbps']} Mbps / ping {data['ping_ms']} ms")
    print(f"  server={data['server']}  threshold={data['threshold_mbps']} Mbps  "
          f"status={data['status'].upper()}")

    if not args.no_summary:
        print("\n--- LLM digest ---")
        print(summarize_speedtest(data))

    if args.alert and data["status"] == "slow":
        from tools import send_telegram_alert
        msg = (f"🐢 Internet slow: download {data['download_mbps']} Mbps "
               f"(below {data['threshold_mbps']} Mbps).\n\n{summarize_speedtest(data)}")
        result = send_telegram_alert(msg)
        print(f"\nAlert sent: {result}")

    # Exit code 2 = finding (already self-alerted above); the units declare
    # SuccessExitStatus=2 so only real breakage (exit 1) pages via OnFailure.
    # Matches the other monitors (reachability.py, smart_monitor.py).
    return 2 if data["status"] == "slow" else 0


if __name__ == "__main__":
    sys.exit(main())
