"""Fleet-wide guest health: verdicts, catalog drift, and the memory rule.

The rule that matters most here is negative: a QEMU balloon reading must
NEVER be classified as high memory. OPNSense reported 89.6% from
host_balloon while being perfectly healthy (2026-09-01) — treating that as
a memory problem is how an agent talks itself into restarting a router.
"""

from types import SimpleNamespace

import pytest

import guest_monitor as gm
import tools


def _svc(vmid, name, kind="qemu", crit="high", policy="ask"):
    return SimpleNamespace(vmid=vmid, name=name, kind=kind, criticality=crit,
                           restart_policy=policy)


def _status(state="running", mem=50.0, source="guest", **extra):
    return {"status": state, "mem_pct": mem, "mem_pct_source": source,
            "cpu_pct": 1.0, "maxmem_mb": 4096, "uptime_h": 10.0, **extra}


# ── the memory rule ──────────────────────────────────────────────────


def test_balloon_reading_is_never_high_mem():
    """The OPNSense case: 89.6% from host_balloon is a MONITORING gap."""
    row = gm._classify(_svc(105, "OPNSense", crit="critical", policy="never"),
                       _status(mem=89.6, source="host_balloon"))
    assert row["verdict"] == "mem_unreliable"
    assert row["verdict"] != "high_mem", "a balloon reading must not justify a restart"


def test_trusted_high_memory_is_flagged():
    row = gm._classify(_svc(106, "sentinel", kind="lxc"),
                       _status(mem=91.0, source="host_cgroup"))
    assert row["verdict"] == "high_mem"


def test_guest_reading_below_threshold_is_ok():
    row = gm._classify(_svc(101, "Hermes", kind="lxc"),
                       _status(mem=31.2, source="host_cgroup"))
    assert row["verdict"] == "ok"


def test_failed_guest_agent_downgrades_to_unreliable():
    row = gm._classify(_svc(100, "HomeAssistant"),
                       _status(mem=40.0, source="guest",
                               mem_pct_warning="ssh exit 255"))
    assert row["verdict"] == "mem_unreliable"


def test_stopped_guest_is_stopped_whatever_the_memory_says():
    row = gm._classify(_svc(102, "Windows10", crit="lab"),
                       _status(state="stopped", mem=99.0, source="host_balloon"))
    assert row["verdict"] == "stopped"


# ── catalog drift ────────────────────────────────────────────────────


def test_deleted_guest_reads_as_catalog_drift():
    row = gm._classify(_svc(104, "HACK-SEQUOIA", crit="lab"), {
        "error": "proxmox http error: Server error '500 Configuration file "
                 "'nodes/Proxmox/qemu-server/104.conf' does not exist'"})
    assert row["verdict"] == "missing"
    assert "catalog.yaml" in row["hint"]


def test_other_api_errors_stay_unknown():
    row = gm._classify(_svc(103, "Debian13"), {"error": "cannot reach Proxmox"})
    assert row["verdict"] == "unknown"


# ── the rollup ───────────────────────────────────────────────────────


@pytest.fixture
def fleet(monkeypatch):
    services = [_svc(105, "OPNSense", crit="critical", policy="never"),
                _svc(106, "sentinel", kind="lxc"),
                _svc(100, "HomeAssistant", crit="critical")]
    monkeypatch.setattr(gm, "load_catalog", lambda *a, **k: SimpleNamespace(
        services=services, proxmox_host=SimpleNamespace(node="Proxmox")))
    by_vmid = {
        105: _status(mem=89.6, source="host_balloon"),      # unreliable
        106: _status(mem=91.0, source="host_cgroup"),       # genuinely high
        100: _status(state="stopped", mem=0, source="host_balloon"),
    }
    monkeypatch.setattr(gm, "check_proxmox_status",
                        lambda node, vmid: by_vmid[vmid])
    monkeypatch.setattr(tools, "list_proxmox_guests", lambda node: {"guests": [
        {"vmid": 105, "name": "OPNSense", "kind": "qemu", "status": "running"},
        {"vmid": 107, "name": "aegis-lab", "kind": "qemu", "status": "stopped"},
    ]})
    return services


def test_rollup_separates_real_problems_from_monitoring_gaps(fleet):
    data = gm.scan_guests()
    assert data["total"] == 3
    assert data["high_mem"] == 1 and data["mem_unreliable"] == 1
    names = {p["name"] for p in data["problems"]}
    assert names == {"sentinel", "HomeAssistant"}, \
        "a balloon reading must not become a finding; a stopped critical must"


def test_rollup_reports_untracked_guests(fleet):
    data = gm.scan_guests()
    assert [g["vmid"] for g in data["untracked_guests"]] == [107]


def test_digest_fallback_never_calls_restart_language(fleet, monkeypatch):
    monkeypatch.setattr("models.helper_llm",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("offline")))
    text = gm.summarize_guests(gm.scan_guests())
    assert "helper_llm unavailable" in text and "sentinel" in text
