"""Transition-based paging (alert_state.py) and the monitors that use it.

The regression this guards: on 2026-09-02 one network outage produced a
reachability alert every 5 minutes plus a "Docker check broke" page every
10 minutes. An incident should page once, remind rarely, and say when it
is over — and a delivery failure must never swallow it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

import alert_state as al
import backup_verifier as bv
import docker_tools as dt
import guest_monitor as gm
import reachability as rc
import tools

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)   # 12:00 CEST
F = al.Finding("svc/ep", "Svc (ep)", "critical", "timeout")


def _after(minutes):
    return T0 + timedelta(minutes=minutes)


# ── evaluate(): the state machine ────────────────────────────────────


def test_new_finding_pages_once_then_stays_quiet():
    ev, st = al.evaluate({}, [F], T0)
    assert [e.kind for e in ev] == ["down"]
    ev, st = al.evaluate(st, [F], _after(5))
    assert ev == []


def test_recovery_reports_the_outage_length():
    _, st = al.evaluate({}, [F], T0)
    ev, st = al.evaluate(st, [], _after(25))
    assert [e.kind for e in ev] == ["recovered"] and st == {}
    assert "recovered after 25 min" in al.render("H", ev, _after(25))


def test_reminder_after_remind_interval_only():
    _, st = al.evaluate({}, [F], T0, remind_after_h=6)
    assert al.evaluate(st, [F], _after(300), remind_after_h=6)[0] == []
    ev, _ = al.evaluate(st, [F], _after(361), remind_after_h=6)
    assert [e.kind for e in ev] == ["still_down"]


def test_confirm_window_swallows_a_single_blip():
    ev, st = al.evaluate({}, [F], T0, confirm_s=240)
    assert ev == [] and st["svc/ep"]["notified"] is None
    ev, st = al.evaluate(st, [], _after(5), confirm_s=240)
    assert ev == [] and st == {}, "a blip that healed must vanish silently"


def test_confirmed_on_the_next_run():
    _, st = al.evaluate({}, [F], T0, confirm_s=240)
    ev, _ = al.evaluate(st, [F], _after(5), confirm_s=240)
    assert [e.kind for e in ev] == ["down"]


def test_keep_carries_unseen_keys_forward():
    _, st = al.evaluate({}, [al.Finding("container:x", "x")], T0)
    ev, st = al.evaluate(st, [al.Finding("portainer", "P")], _after(10),
                         keep=lambda k: k != "portainer")
    assert [e.label for e in ev] == ["P"], "x must not read as recovered"
    assert "container:x" in st


@pytest.mark.parametrize("hour,quiet", [(23, True), (2, True), (6, True),
                                        (7, False), (12, False), (22, False)])
def test_quiet_hours_wrap_midnight(hour, quiet, monkeypatch):
    now = datetime(2026, 9, 28, hour, 30).astimezone()
    assert al.in_quiet_hours(now, [23, 7]) is quiet


# ── notify(): delivery ───────────────────────────────────────────────


@pytest.fixture
def sent(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "STATE_DIR", tmp_path)
    box = []
    monkeypatch.setattr(tools, "send_telegram_alert",
                        lambda msg, silent=False, **k: box.append((msg, silent)) or {"sent": True})
    return box


def test_failed_send_is_retried_next_run(sent, monkeypatch):
    monkeypatch.setattr(tools, "send_telegram_alert", lambda *a, **k: {"error": "no route"})
    assert al.notify("m", "H", [F], now=T0)["sent"] is False
    monkeypatch.setattr(tools, "send_telegram_alert",
                        lambda msg, silent=False, **k: sent.append((msg, silent)) or {"sent": True})
    al.notify("m", "H", [F], now=_after(5))
    assert len(sent) == 1 and "DOWN" in sent[0][0]


def test_high_only_news_is_silent_at_night(sent, monkeypatch):
    monkeypatch.setattr(al, "in_quiet_hours", lambda now, q: True)
    al.notify("m", "H", [al.Finding("a", "A", "high")], now=T0)
    al.notify("m2", "H", [al.Finding("b", "B", "critical")], now=T0)
    assert [s for _, s in sent] == [True, False], "critical always rings"


# ── reachability ─────────────────────────────────────────────────────


def _probe(service, crit, status="down", **extra):
    return {"service": service, "endpoint": "ep", "criticality": crit,
            "status": status, **extra}


def test_reachability_pages_high_but_not_medium(sent):
    data = {"results": [_probe("Debian13", "high", error="timeout"),
                        _probe("Lab", "medium"), _probe("HA", "critical", "up")]}
    rc.alert_transitions(data)                      # first sighting: confirm window
    al_state = al._load(al.STATE_DIR / "reachability.json")
    assert set(al_state) == {"Debian13/ep"}


# ── guests ───────────────────────────────────────────────────────────


def test_guest_check_that_reads_nothing_is_blind(monkeypatch):
    monkeypatch.setattr(gm, "scan_guests", lambda deep=True: {
        "total": 2, "unknown": 2, "problems": [], "guests": []})
    monkeypatch.setattr("sys.argv", ["guest_monitor.py", "--alert", "--no-summary"])
    assert gm.main() == 1


def test_stopped_guest_pages_with_its_criticality(sent):
    gm.alert_transitions({"problems": [
        {"vmid": 101, "name": "Hermes", "kind": "lxc", "verdict": "stopped",
         "criticality": "high", "status": "stopped"}]})
    assert "Hermes (lxc 101) DOWN" in sent[0][0] and "🟠" in sent[0][0]


# ── docker ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("err,unreachable", [
    ("cannot reach Portainer at https://x — No route to host", True),
    ("Portainer http error: timed out", True),
    ("Portainer auth failed — check PORTAINER_API_KEY", False),
    ("Portainer not configured — set PORTAINER_URL", False),
])
def test_portainer_error_classification(err, unreachable):
    assert dt.portainer_unreachable(err) is unreachable


def test_portainer_down_is_a_finding_not_a_broken_check(sent, monkeypatch):
    monkeypatch.setattr(dt, "scan_containers",
                        lambda: {"error": "cannot reach Portainer at x — No route"})
    monkeypatch.setattr("sys.argv", ["docker_tools.py", "--alert", "--no-summary"])
    assert dt.main() == 2
    assert "Docker host (Portainer) DOWN" in sent[0][0]


# ── backups ──────────────────────────────────────────────────────────


def _backup_catalog(monkeypatch):
    svc = [SimpleNamespace(vmid=100, name="HomeAssistant", kind="qemu",
                           criticality="critical", max_backup_age_h=24),
           SimpleNamespace(vmid=103, name="Debian13", kind="qemu",
                           criticality="high", max_backup_age_h=168)]
    monkeypatch.setattr(bv, "load_catalog", lambda: SimpleNamespace(
        services=svc, proxmox_host=SimpleNamespace(node="pve", backup_storage=["gs"])))


def test_unreadable_storage_is_unknown_not_missing(monkeypatch):
    _backup_catalog(monkeypatch)
    monkeypatch.setattr(bv, "_proxmox_get", lambda path: {"error": "403"})
    data = bv.verify_backups()
    assert "error" in data and data["results"] == []


def test_stale_high_backup_is_a_problem(monkeypatch):
    _backup_catalog(monkeypatch)
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr(bv, "_proxmox_get", lambda path: {"data": [
        {"vmid": 100, "ctime": now - 3600, "volid": "a"},
        {"vmid": 103, "ctime": now - 30 * 86400, "volid": "b"}]})
    data = bv.verify_backups()
    assert [p["name"] for p in data["problems"]] == ["Debian13"]
    assert data["critical_problems"] == []


# ── restart_policy: never ────────────────────────────────────────────


@respx.mock
def test_never_policy_guest_is_refused_without_calling_proxmox(monkeypatch):
    monkeypatch.setattr("catalog.load_catalog", lambda *a, **k: SimpleNamespace(
        by_vmid=lambda v: SimpleNamespace(name="OPNSense", restart_policy="never", kind="qemu")))
    route = respx.post(url__regex=r".*/status/reboot").mock(return_value=httpx.Response(200))
    res = tools.restart_vm_raw("Proxmox", 105)
    assert res["executed"] is False and res["reason"] == "restart_policy_never"
    assert not route.called


def test_real_catalog_router_is_never_restartable():
    from catalog import restart_forbidden
    assert restart_forbidden(105) == "OPNSense"
    assert restart_forbidden(100) is None
