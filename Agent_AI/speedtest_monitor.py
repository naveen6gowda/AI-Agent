"""
speedtest_monitor.py — Internet speed watcher.

Runs an Ookla speed test (download / upload / ping) via the pure-Python
`speedtest-cli` library, classifies download against a threshold, and
(optionally) sends a Telegram alert when it's too slow. Like the other
monitors it summarizes on the the LOCAL LLM — ZERO cloud tokens.

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
import sys
from typing import Any, Dict

from dotenv import load_dotenv

load_dotenv()


# Config via env so you can tune the threshold without editing code —
# same pattern as APPROVAL_TIMEOUT_S etc. Default: 20 Mbps download.
MIN_DOWNLOAD_MBPS = float(os.getenv("SPEEDTEST_MIN_DOWNLOAD_MBPS", "20"))


def run_speedtest(min_download_mbps: float | None = None) -> Dict[str, Any]:
    """Run ONE speed test; return a structured dict. Never raises.

    Success: {"status": "ok"|"slow", "download_mbps": .., "upload_mbps": ..,
              "ping_ms": .., "server": "..", "threshold_mbps": ..}
    Failure: {"status": "error", "error": ".."}
    """
    threshold = MIN_DOWNLOAD_MBPS if min_download_mbps is None else min_download_mbps

    # Lazy import: the module still loads (and the agent still starts) even
    # if the library is missing — we just return a clean error.
    try:
        import speedtest
    except ImportError:
        return {"status": "error",
                "error": "speedtest-cli not installed — run `uv add speedtest-cli`"}

    try:
        st = speedtest.Speedtest(secure=True)
        st.get_best_server()
        # .download()/.upload() return BITS per second → /1e6 = Mbps.
        download_mbps = round(st.download() / 1_000_000, 1)
        upload_mbps = round(st.upload(pre_allocate=False) / 1_000_000, 1)
        res = st.results.dict()
    except Exception as e:
        # Any network/Ookla hiccup → structured error, never a crash.
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    srv = res.get("server", {}) or {}
    return {
        "status": "slow" if download_mbps < threshold else "ok",
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": round(res.get("ping", 0), 1),
        "server": f"{srv.get('sponsor', '?')} — {srv.get('name', '?')}",
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
        return 2 if data.get("status") in ("slow", "error") else 0

    if data.get("status") == "error":
        print(f"ERROR: {data['error']}")
        return 2

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

    # Exit code 2 on a problem → systemd/cron can detect it. Matches the
    # other monitors (reachability.py, smart_monitor.py).
    return 2 if data["status"] == "slow" else 0


if __name__ == "__main__":
    sys.exit(main())
