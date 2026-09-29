"""Monitor-side restart-approval flow (docker_tools.handle_restart_approvals).

The card/IPC transport is faked; what's under test is the incident state
machine: ask once, remember denials, honor late taps, forget resolved
incidents. Default-deny — only an explicit approval may restart.
"""

import json

import pytest

import docker_tools as dt


def _data(*containers):
    return {"containers": [
        {"name": n, "state": s, "status": "Exited (0) 2 minutes ago", "health": h}
        for (n, s, h) in containers
    ]}


STOPPED = ("jellyfin", "exited", "")
HEALTHY = ("jellyfin", "running", "healthy")


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Fake transport + isolated state; returns the call recorder."""
    calls = {"cards": [], "restarts": [], "alerts": []}
    monkeypatch.setattr(dt, "ASK_STATE_FILE", tmp_path / "asks.json")
    monkeypatch.setattr(dt, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(dt, "ASK_TIMEOUT_S", 0.0)   # no inline wait in tests
    monkeypatch.setattr(dt, "_send_restart_card",
                        lambda name, status: (calls["cards"].append(name)
                                              or f"rid-{name}-{len(calls['cards'])}",
                                              "chat"))
    monkeypatch.setattr(dt, "restart_container_raw",
                        lambda name: (calls["restarts"].append(name)
                                      or {"ok": True, "restarted": name}))
    monkeypatch.setattr("tools.send_telegram_alert",
                        lambda msg: calls["alerts"].append(msg) or {"ok": True})
    return calls


def _tap(rid, decision):
    dt.APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    (dt.APPROVALS_DIR / f"{rid}.json").write_text(
        json.dumps({"decision": decision, "by": "test", "ts": 0}))


def test_asks_once_and_waits(flow):
    dt.handle_restart_approvals(_data(STOPPED))
    assert flow["cards"] == ["jellyfin"]
    assert flow["restarts"] == []          # no tap → no restart
    # Still stopped 10 min later: the pending card must not be re-sent.
    dt.handle_restart_approvals(_data(STOPPED))
    assert flow["cards"] == ["jellyfin"]


def test_late_approval_restarts_on_next_run(flow):
    dt.handle_restart_approvals(_data(STOPPED))
    rid = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["rid"]
    _tap(rid, "approved")
    dt.handle_restart_approvals(_data(STOPPED))
    assert flow["restarts"] == ["jellyfin"]
    assert any("restarted" in a for a in flow["alerts"])


def test_denial_is_remembered(flow):
    dt.handle_restart_approvals(_data(STOPPED))
    rid = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["rid"]
    _tap(rid, "denied")
    dt.handle_restart_approvals(_data(STOPPED))
    assert flow["restarts"] == []
    assert flow["cards"] == ["jellyfin"]   # cooldown: no second card


def test_resolved_incident_is_forgotten(flow):
    dt.handle_restart_approvals(_data(STOPPED))
    dt.handle_restart_approvals(_data(HEALTHY))
    assert json.loads(dt.ASK_STATE_FILE.read_text()) == {}
    # A fresh incident after recovery asks again immediately.
    dt.handle_restart_approvals(_data(STOPPED))
    assert flow["cards"] == ["jellyfin", "jellyfin"]


# ── instant apply: a tap must not wait for the next scheduled run ────


def test_tap_is_applied_immediately(flow):
    """The bot's path restarts on the tap itself, not 10 minutes later."""
    dt.handle_restart_approvals(_data(STOPPED))
    rid = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["rid"]
    _tap(rid, "approved")

    applied = dt.apply_tapped_decision(rid, "approved", by="op")

    assert applied == {"container": "jellyfin", "decision": "approved"}
    assert flow["restarts"] == ["jellyfin"], "restart must happen on the tap"
    assert any("restarted" in a for a in flow["alerts"])
    entry = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]
    assert entry["decision"] == "approved", "cooldown must start at the decision"


def test_tap_restarts_exactly_once_under_a_race(flow):
    """Monitor and bot both see the tap; the atomic claim picks one."""
    dt.handle_restart_approvals(_data(STOPPED))
    rid = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["rid"]
    _tap(rid, "approved")

    first = dt.apply_tapped_decision(rid, "approved", by="op")
    second = dt.apply_tapped_decision(rid, "approved", by="op")

    assert first and second is None, "the loser of the claim must do nothing"
    assert flow["restarts"] == ["jellyfin"], "no double restart"


def test_applied_decision_survives_the_monitors_next_save(flow):
    """A monitor run that started before the tap must not downgrade it."""
    dt.handle_restart_approvals(_data(STOPPED))
    rid = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["rid"]
    _tap(rid, "denied")
    dt.apply_tapped_decision(rid, "denied", by="op")

    dt.handle_restart_approvals(_data(STOPPED))   # still stopped, runs again

    entry = json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]
    assert entry["decision"] == "denied", "must not revert to pending"
    assert flow["restarts"] == []
    assert flow["cards"] == ["jellyfin"], "cooldown holds: no second card"


def test_unknown_rid_is_left_for_its_own_watcher(flow):
    """MCP-server approvals are not ours — don't consume their file."""
    _tap("mcp-rid-1234", "approved")

    assert dt.apply_tapped_decision("mcp-rid-1234", "approved") is None
    assert (dt.APPROVALS_DIR / "mcp-rid-1234.json").exists()
    assert flow["restarts"] == []
