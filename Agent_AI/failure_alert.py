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

Exit-code convention across the monitors (docker_tools, speedtest_monitor,
reachability, smart_monitor): 0 = all fine, 2 = found a problem and already
sent its own detailed alert (units declare SuccessExitStatus=2, so no page),
anything else = the monitor itself broke → this pager fires.

Manual test:
    systemctl start sentinel-failure-alert@MANUAL-TEST.service
"""

import html
import os
import re
import subprocess
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

# Journal noise that never explains a failure: container status tables,
# separator rules, table headers, systemd's own start/stop chatter.
_NOISE = re.compile(
    r"^(?:running|exited|created|paused|restarting|dead)\s"
    r"|^-{10,}"
    r"|^STATE\s+HEALTH"
    r"|^(?:Starting|Started|Finished|Stopping|Stopped) "
)


def _systemctl_show(unit: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p",
             "Description,Result,ExecMainStatus,ExecMainCode"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def journal_tail(unit: str, lines: int = 40, keep: int = 10) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    relevant = [line for line in out.splitlines() if line.strip() and not _NOISE.match(line)]
    return "\n".join(relevant[-keep:])[-1500:] or "(no journal output)"


def _explain(unit: str, info: dict) -> str:
    """One plain-English sentence about what actually happened."""
    result = info.get("Result", "")
    status = info.get("ExecMainStatus", "")
    if result == "timeout":
        return "The task ran too long and systemd killed it (timeout)."
    if result == "signal":
        return "The task was killed by a signal (crash or out-of-memory)."
    if status and status not in ("0", ""):
        return (f"The task crashed with exit code {status} before it could "
                f"finish its check.")
    return f"The task failed (result: {result or 'unknown'})."


def main() -> int:
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown-unit"
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    # TELEGRAM_CHAT_ID may be a comma-separated list (bot supports multiple
    # authorized chats) — alert the primary operator, the first entry.
    chat_id = os.environ["TELEGRAM_CHAT_ID"].split(",")[0].strip()

    info = _systemctl_show(unit)
    # "HomelabSentinel internet speed test" → "Internet speed test"
    friendly = re.sub(r"^HomelabSentinel\s*[—-]?\s*", "",
                      info.get("Description", unit)).strip() or unit
    friendly = friendly[0].upper() + friendly[1:]

    text = (
        f"🛠 <b>Sentinel monitor broke: {html.escape(friendly)}</b>\n\n"
        f"{html.escape(_explain(unit, info))}\n\n"
        f"⚠️ This means the <b>check itself</b> stopped working — it is "
        f"NOT saying that what it watches (internet, containers, disks…) "
        f"is down. Until it is fixed, Sentinel is blind on this check.\n\n"
        f"<b>Most relevant log lines:</b>\n"
        f"<pre>{html.escape(journal_tail(unit))}</pre>\n\n"
        f"To investigate, run on the sentinel container:\n"
        f"<code>journalctl -u {html.escape(unit)} -e</code>"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = httpx.post(url, json={"chat_id": chat_id, "text": text,
                              "parse_mode": "HTML"}, timeout=15)
    if r.status_code == 400:
        # HTML parse rejected (odd journal content) — the page must still
        # go out, so fall back to plain text.
        plain = re.sub(r"</?(b|pre|code)>", "", text)
        r = httpx.post(url, json={"chat_id": chat_id, "text": plain}, timeout=15)
    r.raise_for_status()
    print(f"failure alert sent for {unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
