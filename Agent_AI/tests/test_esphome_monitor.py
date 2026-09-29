"""ESPHome online/offline watch: availability, grace period, transitions.

What matters here is mostly what must NOT page: a node that was already
offline when monitoring started, a reboot/OTA shorter than the grace
period, a parked device, and HA itself being down.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

import esphome_monitor as em
import tools
from catalog import EspDevice, EspHomeConfig, load_catalog

NOW = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)


def _ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def _raw(*devices):
    """devices: (id, name, mac, [(state, seconds_ago), ...])"""
    return {
        "devices": [{"id": i, "name": n, "model": "esp32-c3-devkitm-1",
                     "sw": "2026.9.0 (2026-09-27 09:06:27 +0200)",
                     "conn": [["mac", m]]} for i, n, m, _ in devices],
        "entities": [[i, st, _ago(s)] for i, _, _, ents in devices for st, s in ents],
    }


def _use(monkeypatch, raw, *entries, grace=180):
    cfg = EspHomeConfig(offline_grace_s=grace, devices=list(entries))
    monkeypatch.setattr(em, "load_catalog", lambda: SimpleNamespace(esphome=cfg))
    monkeypatch.setattr(em, "_fetch_raw", lambda: raw)


# ── availability ─────────────────────────────────────────────────────


def test_one_live_entity_means_online():
    """A node with a permanently-unavailable sensor (S3 Dashboard has two)
    is still online — the node's connection, not each sensor, decides."""
    rows = em._ha_devices(_raw(("d1", "S3 Dashboard", "AA:01",
                                [("unavailable", 900), ("unavailable", 900), ("21.4", 30)])), NOW)
    assert rows[0]["ha_state"] == "online"
    assert rows[0]["firmware"] == "2026.9.0"
    assert rows[0]["mac"] == "aa:01"


def test_all_unavailable_dates_from_the_newest_change():
    rows = em._ha_devices(_raw(("d1", "Hall Clock", "aa:02",
                                [("unavailable", 600), ("unavailable", 400)])), NOW)
    assert rows[0]["ha_state"] == "unavailable"
    assert rows[0]["unavailable_s"] == 400


def test_short_drop_is_dropping_not_offline():
    """An OTA flash or reboot must never count as offline."""
    assert em._status({"ha_state": "unavailable", "unavailable_s": 60}, 180) == "dropping"
    assert em._status({"ha_state": "unavailable", "unavailable_s": 181}, 180) == "offline"


# ── catalog matching ────────────────────────────────────────────────


def test_match_by_mac_survives_rename_in_ha(monkeypatch):
    _use(monkeypatch, _raw(("d1", "ESP32 S3 Display Hub", "02:00:00:aa:bb:01", [("on", 10)])),
         EspDevice(name="BedRoom Monitoring Display", mac="02:00:00:AA:BB:01"))
    data = em.scan_esphome(NOW)
    assert data["devices"][0]["status"] == "online"
    assert data["devices"][0]["name"] == "BedRoom Monitoring Display"
    assert data["untracked"] == []


def test_untracked_and_missing_are_reported_not_problems_unless_monitored(monkeypatch):
    _use(monkeypatch, _raw(("d1", "New Node", "aa:09", [("on", 10)])),
         EspDevice(name="Gone Node", mac="aa:08", monitor=False))
    data = em.scan_esphome(NOW)
    assert data["untracked"][0]["ha_name"] == "New Node"
    assert data["devices"][0]["status"] == "missing"
    assert data["problems"] == []          # parked, so not a finding


def test_parked_device_is_never_a_problem(monkeypatch):
    _use(monkeypatch, _raw(("d1", "TV Remote", "aa:03", [("unavailable", 86400)])),
         EspDevice(name="TV Remote", mac="aa:03", monitor=False))
    data = em.scan_esphome(NOW)
    assert data["devices"][0]["status"] == "offline"
    assert data["problems"] == [] and data["parked"] == ["TV Remote"]


# ── transitions ─────────────────────────────────────────────────────


def _dev(status, monitor=True, key="aa:02", **extra):
    return {"name": "Hall Clock", "key": key, "monitor": monitor,
            "status": status, "ip": "10.0.30.16", **extra}


def _data(*devices):
    mon = [d for d in devices if d["monitor"]]
    return {"devices": list(devices), "monitored": len(mon),
            "online": sum(d["status"] == "online" for d in mon)}


def test_first_sight_is_a_silent_baseline_even_when_offline():
    """The operator's rule: already-offline devices must not page."""
    events, state = em.track_transitions(
        _data(_dev("offline", unavailable_since=_ago(9000))), {}, NOW)
    assert events == []
    assert state["aa:02"]["status"] == "offline"


def test_online_to_offline_fires_once():
    d = _data(_dev("offline", unavailable_since=_ago(300), unavailable_s=300))
    events, state = em.track_transitions(d, {"aa:02": {"status": "online", "since": _ago(9000)}}, NOW)
    assert [(e["from"], e["to"]) for e in events] == [("online", "offline")]
    again, _ = em.track_transitions(d, state, NOW)
    assert again == [], "a device that stays offline must stay quiet"


def test_back_online_reports_downtime():
    events, _ = em.track_transitions(
        _data(_dev("online")), {"aa:02": {"status": "offline", "since": _ago(1500)}}, NOW)
    assert [(e["from"], e["to"]) for e in events] == [("offline", "online")]
    msg = em.format_alert(events, _data(_dev("online")), NOW)
    assert "back ONLINE" in msg and "was offline 25 min" in msg


def test_dropping_keeps_the_previous_record():
    prev = {"aa:02": {"status": "online", "since": _ago(9000)}}
    events, state = em.track_transitions(
        _data(_dev("dropping", unavailable_s=60)), prev, NOW)
    assert events == [] and state == prev


def test_parked_device_is_not_tracked():
    """Re-enabling a parked device must start from a fresh baseline."""
    events, state = em.track_transitions(
        _data(_dev("online", monitor=False)), {"aa:02": {"status": "offline", "since": _ago(99)}}, NOW)
    assert events == [] and state == {}


def test_everything_down_at_once_points_at_the_network():
    a = _dev("offline", key="a", unavailable_since=_ago(300))
    b = {**_dev("offline", key="b", unavailable_since=_ago(300)), "name": "Office Monitor"}
    prev = {"a": {"status": "online"}, "b": {"status": "online"}}
    events, _ = em.track_transitions(_data(a, b), prev, NOW)
    msg = em.format_alert(events, _data(a, b), NOW)
    assert msg.count("OFFLINE") == 2 and "IoT Wi-Fi/VLAN" in msg


# ── HA down / broken ────────────────────────────────────────────────


@respx.mock
def test_ha_down_skips_the_run_and_records_nothing(monkeypatch, tmp_path):
    respx.post(f"{tools._HA_URL}/api/template").mock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr(em, "STATE_FILE", tmp_path / "esphome_state.json")
    monkeypatch.setattr("sys.argv", ["esphome_monitor.py", "--alert"])
    assert em.main() == 0
    assert not (tmp_path / "esphome_state.json").exists()


@respx.mock
def test_revoked_token_is_a_broken_check_not_a_skip():
    """A 401 must page (exit 1) — HAUnavailable would silently skip forever."""
    respx.post(f"{tools._HA_URL}/api/template").mock(return_value=httpx.Response(401))
    with pytest.raises(RuntimeError, match="auth"):
        em._fetch_raw()


# ── the real catalog ────────────────────────────────────────────────


def test_real_catalog_esphome_block_is_consistent():
    cfg = load_catalog().esphome
    macs = [d.mac.lower() for d in cfg.devices if d.mac]
    assert len(macs) == len(set(macs)), "duplicate MAC in catalog esphome.devices"
    assert cfg.offline_grace_s >= 60
