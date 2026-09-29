"""
firefly_client.py — thin REST wrapper around Firefly III.

Same shape as docker_tools.py ↔ Portainer: one `_request` helper does bearer
auth and turns every failure into an {"error": ...} dict (never raises), so
callers can treat a failure as data. Firefly III owns the database, the
dashboard, currency handling, categorization and reconciliation — this module
just lets Sentinel insert captured transactions and read exact aggregates back.

Config in .env:
    FIREFLY_URL=http://docker.lan:8212
    FIREFLY_TOKEN=<Firefly Personal Access Token>      # Options ▸ Profile ▸ OAuth
    FINANCE_ASSET_ACCOUNT=TF Mastercard Gold           # the card behind Samsung Wallet
    FINANCE_DEFAULT_CURRENCY=EUR

Self-test (creates ONE clearly-labelled €0.01 test transaction, then proves
dedupe by trying to insert it again):
    uv run python firefly_client.py --selftest
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

_URL = os.getenv("FIREFLY_URL", "").rstrip("/")
_TOKEN = os.getenv("FIREFLY_TOKEN", "")
_ASSET_ACCOUNT = os.getenv("FINANCE_ASSET_ACCOUNT", "Samsung Wallet")
_DEFAULT_CCY = os.getenv("FINANCE_DEFAULT_CURRENCY", "EUR")
_TIMEOUT = float(os.getenv("FIREFLY_TIMEOUT_S", "20"))


def _configured() -> bool:
    return bool(_URL and _TOKEN)


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, **kw) -> Dict[str, Any]:
    """One Firefly API call. Returns the parsed JSON dict, or {"error": ...}."""
    if not _configured():
        return {"error": "Firefly not configured — set FIREFLY_URL and FIREFLY_TOKEN in .env"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.request(method, f"{_URL}{path}", headers=_headers(), **kw)
            if r.status_code in (401, 403):
                return {"error": "Firefly auth failed — check FIREFLY_TOKEN"}
            if r.status_code == 404:
                return {"error": "not_found", "path": path}
            if r.status_code == 422:
                # validation error — surface Firefly's message so we can see why
                try:
                    body = r.json()
                except Exception:
                    body = {"message": r.text[:300]}
                return {"error": "validation", "detail": body}
            r.raise_for_status()
            if not r.content:
                return {"ok": True}
            return r.json()
    except httpx.ConnectError as e:
        return {"error": f"cannot reach Firefly at {_URL} — {e}"}
    except httpx.HTTPError as e:
        return {"error": f"Firefly http error: {e}"}


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
def about() -> Dict[str, Any]:
    """Firefly version / connectivity check."""
    res = _request("GET", "/api/v1/about")
    if "error" in res:
        return res
    d = res.get("data", {})
    return {"version": d.get("version"), "api_version": d.get("api_version")}


# ---------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------
def _find_account(name: str, acc_type: str) -> Optional[Dict[str, Any]]:
    """Return the account dict whose name matches (case-insensitive), or None."""
    res = _request("GET", "/api/v1/accounts", params={"type": acc_type})
    if "error" in res:
        return None
    want = (name or "").strip().lower()
    for a in res.get("data", []):
        if (a.get("attributes", {}).get("name", "") or "").strip().lower() == want:
            return a
    return None


def ensure_asset_account(name: Optional[str] = None) -> Dict[str, Any]:
    """Find the asset account by name; create it (as an asset account) if missing.

    Returns {"id": ..., "name": ..., "created": bool} or {"error": ...}.
    """
    name = name or _ASSET_ACCOUNT
    existing = _find_account(name, "asset")
    if existing:
        return {"id": existing["id"], "name": name, "created": False}

    body = {
        "name": name,
        "type": "asset",
        "account_role": "defaultAsset",
        "currency_code": _DEFAULT_CCY,
    }
    res = _request("POST", "/api/v1/accounts", json=body)
    if "error" in res:
        return res
    return {"id": res.get("data", {}).get("id"), "name": name, "created": True}


# ---------------------------------------------------------------------
# Transactions — write
# ---------------------------------------------------------------------
def find_by_external_id(external_id: str) -> Optional[Dict[str, Any]]:
    """Dedupe lookup: return the existing transaction with this external_id, or
    None. Uses Firefly's search with the external_id_is operator."""
    if not external_id:
        return None
    res = _request("GET", "/api/v1/search/transactions",
                   params={"query": f'external_id_is:"{external_id}"'})
    if "error" in res:
        return None
    data = res.get("data", [])
    return data[0] if data else None


def find_scheduled_match(amount: Any, merchant: Optional[str],
                         date_iso: Optional[str],
                         window_days: int = 7) -> Optional[str]:
    """The Firefly id of an existing 'scheduled'-tagged withdrawal with the
    same amount and counterparty within ±window_days, else None.

    Used to pair N26's "has been collected by" notification with the advance
    "will be debited on <date>" notice we already booked — storing both
    would double-count the debit. Any lookup failure returns None (fail
    open: better a visible duplicate in Firefly than a silently dropped
    transaction)."""
    try:
        want_amt = float(amount)
    except (TypeError, ValueError):
        return None
    res = _request("GET", "/api/v1/search/transactions",
                   params={"query": f'tag_is:scheduled amount_is:"{amount}"'})
    if "error" in res:
        return None
    try:
        center = datetime.fromisoformat(date_iso) if date_iso else None
    except ValueError:
        center = None
    want_merchant = (merchant or "").strip().lower()

    for t in res.get("data", []):
        for split in t.get("attributes", {}).get("transactions", []):
            try:
                if abs(float(split.get("amount")) - want_amt) > 0.005:
                    continue
            except (TypeError, ValueError):
                continue
            dest = (split.get("destination_name") or "").strip().lower()
            if want_merchant and dest and not (
                    want_merchant in dest or dest in want_merchant):
                continue
            if center is not None and split.get("date"):
                try:
                    delta = datetime.fromisoformat(split["date"]) - center
                except ValueError:
                    delta = None
                if delta is not None and abs(delta.days) > window_days:
                    continue
            return t.get("id")
    return None


def create_transaction(txn: Dict[str, Any]) -> Dict[str, Any]:
    """Insert ONE withdrawal into Firefly. Idempotent: if a transaction with the
    same external_id already exists, returns {"result": "duplicate"} WITHOUT
    inserting.

    Expects a dict shaped like finance_parser output:
        {amount: "56.11", currency: "EUR", merchant: "Aldi",
         category: "Groceries"|None, description: "...", date: "2026-06-06T..",
         external_id: "<hash>", notes: "...", direction: "withdrawal",
         asset_account: "TF Mastercard Gold"|absent → FINANCE_ASSET_ACCOUNT}
    """
    external_id = txn.get("external_id", "")
    dup = find_by_external_id(external_id)
    if dup is not None:
        return {"result": "duplicate", "external_id": external_id,
                "firefly_id": dup.get("id")}

    # N26 sends a "has been collected by" notification for a direct debit it
    # already announced with a "will be debited on <date>" notice — which we
    # booked (tagged "scheduled"). Pair them instead of double-counting; a
    # collected debit with NO prior notice stores normally below.
    if txn.get("kind") == "direct_debit_collected":
        sched_id = find_scheduled_match(txn.get("amount"), txn.get("merchant"),
                                        txn.get("date"))
        if sched_id is not None:
            return {"result": "duplicate_of_scheduled",
                    "external_id": external_id, "firefly_id": sched_id,
                    "merchant": txn.get("merchant"),
                    "amount": str(txn.get("amount"))}

    # Samsung Wallet payments carry the card's name from the notification
    # title; N26 texts don't set it and fall back to the .env default.
    asset = ensure_asset_account(txn.get("asset_account"))
    if "error" in asset:
        return asset

    ttype = txn.get("direction") or "withdrawal"
    counterparty = txn.get("merchant") or (
        "Unknown payer" if ttype == "deposit" else "Unknown merchant")
    inner: Dict[str, Any] = {
        "type": ttype,
        "date": txn.get("date"),
        "amount": str(txn.get("amount")),
        "currency_code": txn.get("currency") or _DEFAULT_CCY,
        "description": txn.get("description") or counterparty,
        "external_id": external_id,
        "notes": txn.get("notes") or "",
        "tags": ["sentinel"] + list(txn.get("tags") or []),
    }
    # Firefly requires the asset account on the money's receiving side for
    # deposits, sending side for withdrawals.
    if ttype == "deposit":
        inner["source_name"] = counterparty
        inner["destination_name"] = asset["name"]
    else:
        inner["source_name"] = asset["name"]
        inner["destination_name"] = counterparty
    if txn.get("category"):
        inner["category_name"] = txn["category"]

    body = {"apply_rules": True, "fire_webhooks": False, "transactions": [inner]}
    res = _request("POST", "/api/v1/transactions", json=body)
    if "error" in res:
        return {"result": "error", **res}
    return {"result": "stored",
            "firefly_id": res.get("data", {}).get("id"),
            "external_id": external_id,
            "type": ttype,
            "merchant": counterparty,
            "amount": inner["amount"],
            "currency": inner["currency_code"]}


# ---------------------------------------------------------------------
# Transactions — read / aggregate (Firefly does the math)
# ---------------------------------------------------------------------
def list_transactions(start: str, end: str, limit: int = 50) -> Dict[str, Any]:
    """Withdrawals between start and end (YYYY-MM-DD), newest first."""
    res = _request("GET", "/api/v1/transactions",
                   params={"start": start, "end": end, "type": "withdrawal",
                           "limit": limit})
    if "error" in res:
        return res
    out = []
    for t in res.get("data", []):
        for split in t.get("attributes", {}).get("transactions", []):
            out.append({
                "date": split.get("date", "")[:10],
                "amount": split.get("amount"),
                "currency": split.get("currency_code"),
                "merchant": split.get("destination_name"),
                "category": split.get("category_name"),
                "description": split.get("description"),
            })
    return {"start": start, "end": end, "count": len(out), "transactions": out}


def insight_expense(start: str, end: str) -> List[Dict[str, Any]]:
    """Exact summed expense per merchant (expense account) over the period.
    Returns [{"name": "Aldi", "amount": 56.11, "currency": "EUR"}, ...]."""
    res = _request("GET", "/api/v1/insight/expense/expense",
                   params={"start": start, "end": end})
    if isinstance(res, dict) and "error" in res:
        return []
    rows = res if isinstance(res, list) else res.get("data", res)
    out = []
    for r in (rows or []):
        out.append({
            "name": r.get("name"),
            "amount": abs(float(r.get("difference_float") or 0.0)),
            "currency": r.get("currency_code"),
        })
    return out


def list_categories() -> List[str]:
    res = _request("GET", "/api/v1/categories")
    if "error" in res:
        return []
    return [c.get("attributes", {}).get("name") for c in res.get("data", [])]


# ---------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------
def _selftest() -> int:
    print("1) about():", about())

    print("2) ensure_asset_account():", ensure_asset_account())

    test_txn = {
        "amount": "0.01",
        "currency": _DEFAULT_CCY,
        "merchant": "ZZ Sentinel Selftest",
        "category": None,
        "description": "Sentinel self-test — safe to delete",
        "date": "2026-06-21T12:00:00+02:00",
        "external_id": "sentinel-selftest-fixed-0001",
        "notes": "Created by firefly_client.py --selftest. Delete me.",
        "direction": "withdrawal",
    }

    print("3) create_transaction() first time:")
    r1 = create_transaction(test_txn)
    print("   ->", r1)

    print("4) create_transaction() again (should be duplicate):")
    r2 = create_transaction(test_txn)
    print("   ->", r2)

    ok = r1.get("result") == "stored" and r2.get("result") == "duplicate"
    print("\nDEDUPE:", "PASS ✅" if ok else "FAIL ❌")
    print("(Find 'ZZ Sentinel Selftest' / €0.01 in Firefly and delete it when done.)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(about())
