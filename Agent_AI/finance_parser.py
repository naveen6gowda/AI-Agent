"""
finance_parser.py — N26 phone-notification text → Firefly transaction dict.

The HA companion app captures N26 app notifications on the phone
(sensor.navis24_last_notification); an HA automation forwards the text (and
the notification's post_time) to finance_server.py, which calls parse() and
hands the result to firefly_client.create_transaction().

Recognized formats (all real N26 English notification texts):
    "€81.00 will be debited from your account on 06 Jul 2026 to pay VATTENFALL"
    "Your payment of €6.99 will be debited on 25 Oct 2025"
    "Your payment of €79.00 to Test Store A has been successfully processed"
    "€14.28 was debited from your account to pay klarmobil GmbH"
    "You received a MoneyBeam for €286,00 from mani"          → deposit
    "You received €100.00 from John Doe"                      → deposit
    "You sent a MoneyBeam of €20.00 to Jane"
    "You just paid €12.50 to REWE Markt" / "You spent €5 at Aldi"

Samsung Wallet card payments (notification title = the card's name, e.g.
"TF Mastercard Gold"; message is just merchant + amount, € AFTER the number,
often with a non-breaking space):
    "Parkgarage Maximilians 9,00 €"
The whole-string wallet pattern is tried LAST so it can never shadow an N26
format. When it matches and a title was sent, the title is passed through as
`asset_account` so the payment books to that card's asset account in Firefly
instead of the default (N26).

Anything else returns {"parsed": False, "reason": ...} — the server logs it
to finance_unparsed.jsonl so no capture is silently lost.

Dedupe: external_id = sha256 of the normalized text + the notification's
post_time (when HA sends it) or the transaction date (fallback). Same
notification re-delivered → same external_id → firefly_client skips it.
Caveat (fallback path only): two genuinely identical purchases on the same
day would collide — always send post_time from HA.

Double-count caveat: a "will be debited on <date>" advance notice is stored
dated <date> and tagged "scheduled". If N26 later also notifies when the
debit executes, that second text parses differently and would insert again —
delete one in Firefly if you see a pair (watch the "scheduled" tag).

Self-test (no network, no Firefly writes):
    uv run python finance_parser.py --selftest
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("AGENT_TZ", "Europe/Berlin"))

# 12 / 12.50 / 1.234,56 / 1,234.56 (bare number, no € sign)
_AMT_NUM = r"(?P<amount>\d{1,3}(?:[.,]\d{3})*[.,]\d{2}|\d+(?:[.,]\d{1,2})?)"
# €12 / €12.50 / €1.234,56 / €1,234.56
_AMT = r"€\s?" + _AMT_NUM
_DATE = r"(?P<date>\d{1,2}\.? [A-Za-z]+\.? \d{4})"

# (kind, direction, compiled regex) — first match wins.
_PATTERNS = [
    ("scheduled_debit", "withdrawal", re.compile(
        rf"{_AMT} will be debited from your account on {_DATE} to pay (?P<who>.+?)\.?$", re.I)),
    ("scheduled_debit", "withdrawal", re.compile(
        rf"payment of {_AMT} will be debited(?: from your account)?(?: on {_DATE})?", re.I)),
    ("card_payment", "withdrawal", re.compile(
        rf"payment of {_AMT} to (?P<who>.+?) (?:has been|was) successfully processed", re.I)),
    ("direct_debit", "withdrawal", re.compile(
        rf"{_AMT} (?:has been|was) debited from your account to pay (?P<who>.+?)\.?$", re.I)),
    ("moneybeam_in", "deposit", re.compile(
        rf"received a MoneyBeam (?:of|for) {_AMT} from (?P<who>.+?)\.?$", re.I)),
    ("transfer_in", "deposit", re.compile(
        rf"(?:just )?received {_AMT} from (?P<who>.+?)\.?$", re.I)),
    ("moneybeam_out", "withdrawal", re.compile(
        rf"sent (?:a MoneyBeam (?:of|for) )?{_AMT} to (?P<who>.+?)\.?$", re.I)),
    ("card_payment", "withdrawal", re.compile(
        rf"(?:just )?(?:paid|spent) {_AMT} (?:to|at) (?P<who>.+?)\.?$", re.I)),
    # Samsung Wallet: the whole message is "<merchant> <amount> €". Anchored
    # ^…$ so it only fires when nothing above matched AND the text has exactly
    # this shape (N26 texts never end in "<amount> €").
    ("wallet_payment", "withdrawal", re.compile(
        rf"^(?P<who>.+?)\s+{_AMT_NUM}\s*€$")),
]


def _norm_amount(raw: str) -> str:
    """'286,00' → '286.00', '1.234,56' → '1234.56', '1,234.56' → '1234.56'."""
    s = raw.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):        # 1.234,56 — comma is decimal
            s = s.replace(".", "").replace(",", ".")
        else:                                   # 1,234.56 — comma is thousands
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if len(tail) == 2:                      # 286,00 — comma is decimal
            s = head.replace(",", "") + "." + tail
        else:                                   # 1,234 — comma is thousands
            s = s.replace(",", "")
    return s


def _parse_date(raw: str) -> Optional[datetime]:
    """'06 Jul 2026' / '6 July 2026' → aware datetime at midnight local."""
    cleaned = raw.replace(".", "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def make_external_id(text: str, posted: Optional[str], date_str: str,
                     prefix: str = "n26-") -> str:
    basis = " ".join(text.split()).lower()
    key = f"{basis}|{posted}" if posted else f"{basis}|{date_str}"
    return prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def parse(text: str, posted: Optional[str] = None,
          title: Optional[str] = None) -> Dict[str, Any]:
    """Turn one notification text into the dict create_transaction() expects.

    posted: the notification's post_time from HA (any string/number) — used
    only to build a collision-proof external_id.
    title: the notification's title from HA. For Samsung Wallet payments the
    title is the card's name (e.g. "TF Mastercard Gold") and is passed through
    as asset_account so the payment books to that card in Firefly.
    Returns {"parsed": False, "reason": ..., "raw": ...} when unrecognized.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return {"parsed": False, "reason": "empty_message", "raw": text}
    if "€" not in raw:
        return {"parsed": False, "reason": "no_eur_amount", "raw": raw}

    for kind, direction, rx in _PATTERNS:
        m = rx.search(raw)
        if not m:
            continue
        groups = m.groupdict()
        amount = _norm_amount(groups["amount"])
        who = (groups.get("who") or "").strip() or None

        scheduled = kind == "scheduled_debit"
        wallet = kind == "wallet_payment"
        when = _parse_date(groups["date"]) if groups.get("date") else None
        if when is None:
            when = datetime.now(TZ)
        date_iso = when.isoformat(timespec="seconds")

        source = "Samsung Wallet" if wallet else "N26"
        out = {
            "parsed": True,
            "kind": kind,
            "direction": direction,
            "amount": amount,
            "currency": "EUR",
            "merchant": who,
            "category": None,           # left to Firefly's rules
            "description": who or kind.replace("_", " "),
            "date": date_iso,
            "external_id": make_external_id(
                raw, posted, date_iso[:10],
                prefix="wallet-" if wallet else "n26-"),
            "notes": f"Captured from {source} notification via HA.\n{raw}",
            "tags": ["scheduled"] if scheduled else (["wallet"] if wallet else []),
        }
        if wallet and (title or "").strip():
            out["asset_account"] = title.strip()   # book to the card, not N26
        return out

    return {"parsed": False, "reason": "unrecognized_format", "raw": raw}


# ---------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------
_SAMPLES = [
    # (text, expect_parsed, expect_direction, expect_amount, expect_merchant)
    ("€81.00 will be debited from your account on 06 Jul 2026 to pay VATTENFALL EUROPE SALES",
     True, "withdrawal", "81.00", "VATTENFALL EUROPE SALES"),
    ("Your payment of €6.99 will be debited on 25 Oct 2025",
     True, "withdrawal", "6.99", None),
    ("Your payment of €79.00 to Test Store A has been successfully processed",
     True, "withdrawal", "79.00", "Test Store A"),
    ("You received a MoneyBeam for €286,00 from mani",
     True, "deposit", "286.00", "mani"),
    ("€14.28 was debited from your account to pay klarmobil GmbH",
     True, "withdrawal", "14.28", "klarmobil GmbH"),
    ("You just paid €12.50 to REWE Markt",
     True, "withdrawal", "12.50", "REWE Markt"),
    ("You received €1.050,00 from Chaitra Kallimani",
     True, "deposit", "1050.00", "Chaitra Kallimani"),
    ("You sent a MoneyBeam of €20.00 to Jane",
     True, "withdrawal", "20.00", "Jane"),
    ("Your monthly statement is ready", False, None, None, None),
    ("", False, None, None, None),
]

# Samsung Wallet: (text, title, expect_amount, expect_merchant, expect_account)
# First one is the real notification (with the real non-breaking space).
_WALLET_SAMPLES = [
    ("Parkgarage Maximilians 9,00 €", "TF Mastercard Gold",
     "9.00", "Parkgarage Maximilians", "TF Mastercard Gold"),
    ("REWE SAGT DANKE. 44315 23,45 €", "TF Mastercard Gold",
     "23.45", "REWE SAGT DANKE. 44315", "TF Mastercard Gold"),
    ("Aral Station 24 15 €", "TF Mastercard Gold",
     "15", "Aral Station 24", "TF Mastercard Gold"),
]


def _selftest() -> int:
    failures = 0
    for text, want_ok, want_dir, want_amt, want_who in _SAMPLES:
        r = parse(text, posted="1783067814431")
        ok = r.get("parsed") == want_ok
        if want_ok and ok:
            ok = (r["direction"] == want_dir and r["amount"] == want_amt
                  and r["merchant"] == want_who)
        status = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"[{status}] {text[:60]!r}")
        print(f"        -> {({k: r[k] for k in ('direction','amount','merchant','date','tags','external_id')} if r.get('parsed') else r)}")

    for text, title, want_amt, want_who, want_acct in _WALLET_SAMPLES:
        r = parse(text, posted="1783185092606", title=title)
        ok = (r.get("parsed") and r["kind"] == "wallet_payment"
              and r["direction"] == "withdrawal" and r["amount"] == want_amt
              and r["merchant"] == want_who
              and r.get("asset_account") == want_acct
              and r["tags"] == ["wallet"]
              and r["external_id"].startswith("wallet-"))
        status = "PASS" if ok else "FAIL"
        failures += 0 if ok else 1
        print(f"[{status}] wallet: {text[:60]!r}")
        print(f"        -> {({k: r.get(k) for k in ('amount','merchant','asset_account','tags')} if r.get('parsed') else r)}")

    # an N26 text must NOT fall into the wallet pattern even with a title set
    r = parse("You just paid €12.50 to REWE Markt", posted="1", title="N26")
    ok = r.get("parsed") and r["kind"] == "card_payment" and "asset_account" not in r
    print(f"[{'PASS' if ok else 'FAIL'}] N26 text unaffected by title (kind={r.get('kind')})")
    failures += 0 if ok else 1

    # same text + same posted must give the same id; different posted differs
    a = parse(_SAMPLES[2][0], posted="111")
    b = parse(_SAMPLES[2][0], posted="111")
    c = parse(_SAMPLES[2][0], posted="222")
    dedupe_ok = a["external_id"] == b["external_id"] != c["external_id"]
    print(f"[{'PASS' if dedupe_ok else 'FAIL'}] external_id stable per posted, distinct across posted")
    failures += 0 if dedupe_ok else 1

    print(f"\n{'ALL PASS ✅' if failures == 0 else f'{failures} FAILURES ❌'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(parse(" ".join(sys.argv[1:])))
