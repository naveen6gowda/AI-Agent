#!/usr/bin/env python3
"""
failure_alert.py — Telegram alert for a failed systemd unit.

Wired via `OnFailure=sentinel-failure-alert@%n.service` in every sentinel
unit: when a unit enters the failed state, systemd starts the template
instance and the full unit name arrives as argv[1]. Before this existed,
monitors died silently on network blips and nobody noticed — the watcher
needs a watcher.

Deliberately independent of the agent, the bot, and the LLM: if those are
what broke, this must still work. One HTTP call, no imports from the app.

Manual test:
    systemctl start sentinel-failure-alert@MANUAL-TEST.service
"""

import os
import subprocess
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def journal_tail(unit: str, lines: int = 8) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    return out[-1500:] or "(no journal output)"


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown-unit"
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    text = (
        f"🔴 UNIT FAILED: {unit}\n"
        f"host: sentinel\n\n"
        f"last log lines:\n{journal_tail(unit)}\n\n"
        f"inspect: journalctl -u {unit} -e"
    )
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    r.raise_for_status()
    print(f"failure alert sent for {unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
