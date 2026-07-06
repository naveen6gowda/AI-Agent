"""Unit tests: finance parser, catalog validation, checkpoint retention."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from langgraph.checkpoint.sqlite import SqliteSaver

import catalog as catalog_mod
import checkpoint_maintenance as cm
import finance_parser

# ── finance parser ──────────────────────────────────────────────────


def test_finance_parser_selftest_corpus():
    """The module ships its own sample corpus — it must stay green."""
    assert finance_parser._selftest() == 0


def test_card_payment_parses():
    r = finance_parser.parse("You just paid €12.50 to REWE Markt", posted="1")
    assert r["parsed"] and r["kind"] == "card_payment"
    assert r["direction"] == "withdrawal"
    assert r["amount"] == "12.50"


def test_external_id_is_stable_per_posted():
    text = "You just paid €12.50 to REWE Markt"
    a = finance_parser.parse(text, posted="111")
    b = finance_parser.parse(text, posted="111")
    c = finance_parser.parse(text, posted="222")
    assert a["external_id"] == b["external_id"] != c["external_id"]


# ── catalog (policy as data) ────────────────────────────────────────


def _write_catalog(tmp_path, services_yaml):
    p = tmp_path / "catalog.yaml"
    p.write_text(yaml.safe_dump({
        "defaults": {"max_backup_age_h": 24, "restart_policy": "never"},
        "proxmox_host": {"node": "pve", "ssh_host": "pve.test"},
        "services": services_yaml,
    }))
    return p


def test_catalog_defaults_are_merged(tmp_path):
    p = _write_catalog(tmp_path, [{
        "vmid": 100, "name": "router", "kind": "qemu",
        "node": "pve", "criticality": "critical",
    }])
    catalog_mod.clear_cache()
    cat = catalog_mod.load_catalog(p)
    svc = cat.services[0]
    assert svc.max_backup_age_h == 24
    assert svc.restart_policy == "never"


def test_catalog_rejects_invalid_criticality(tmp_path):
    p = _write_catalog(tmp_path, [{
        "vmid": 100, "name": "x", "kind": "lxc",
        "node": "pve", "criticality": "banana",
    }])
    catalog_mod.clear_cache()
    with pytest.raises(ValueError):
        catalog_mod.load_catalog(p)


# ── checkpoint retention ────────────────────────────────────────────


def _seed(saver, thread_id, n, ts):
    """Write n checkpoints with ascending ids and a fixed timestamp."""
    for i in range(n):
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        checkpoint = {
            "v": 1,
            "id": f"00000000-0000-0000-0000-{i:012d}",
            "ts": ts.isoformat(),
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
        }
        saver.put(config, checkpoint, {"source": "test", "step": i}, {})


def test_prune_deletes_stale_threads_and_caps_live_ones(tmp_path):
    db = str(tmp_path / "ckpt.sqlite")
    now = datetime.now(timezone.utc)
    with SqliteSaver.from_conn_string(db) as saver:
        _seed(saver, "stale-thread", 3, now - timedelta(days=30))
        _seed(saver, "live-thread", 10, now)

    cm.prune(db, keep_days=14, keep_per_thread=4, dry_run=False)

    conn = sqlite3.connect(db)
    try:
        per_thread = dict(conn.execute(
            "SELECT thread_id, COUNT(*) FROM checkpoints GROUP BY thread_id"
        ).fetchall())
    finally:
        conn.close()
    assert "stale-thread" not in per_thread, "idle threads must be deleted"
    assert per_thread["live-thread"] == 4, "live threads keep only the newest N"


def test_dry_run_deletes_nothing(tmp_path):
    db = str(tmp_path / "ckpt.sqlite")
    now = datetime.now(timezone.utc)
    with SqliteSaver.from_conn_string(db) as saver:
        _seed(saver, "live-thread", 10, now)

    cm.prune(db, keep_days=14, keep_per_thread=2, dry_run=True)

    conn = sqlite3.connect(db)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    finally:
        conn.close()
    assert count == 10
