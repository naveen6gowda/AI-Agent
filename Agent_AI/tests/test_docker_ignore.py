"""The catalog `docker.expected_down` allowlist (2026-08-06).

Dify's one-shot `init_permissions` container chowns a volume, exits 0 and
is declared `restart: "no"` — so it sits in `exited` forever and the
10-minute monitor paged about it every 10 minutes.

What matters here is that the suppression stays NARROW. A plain
`docker stop jellyfin` also leaves "Exited (0)" behind, so an exit-code
heuristic would have silently killed the very alert the operator asked for in
July 2026 — hence test_ordinary_stopped_container_still_alerts_and_asks.
"""

import json
from types import SimpleNamespace

import pytest

import catalog
import docker_tools as dt

INIT = "docker-init_permissions-1"


def _raw(name, state, status, labels=None):
    """One entry shaped like Docker's /containers/json?all=1 payload."""
    return {
        "Names": [f"/{name}"],
        "Id": "deadbeefcafe0123456789",
        "Image": "busybox:latest",
        "State": state,
        "Status": status,
        "Labels": labels or {},
    }


RUNNING = _raw("adguard", "running", "Up 3 days (healthy)")


@pytest.fixture
def catalog_cfg(monkeypatch):
    """Catalog with the real production allowlist shape, no YAML on disk."""
    cfg = catalog.DockerConfig(
        expected_down=[catalog.ExpectedDown(name=INIT, reason="Dify one-shot init")],
        expected_down_labels={"sentinel.expected_down": "true"},
    )
    monkeypatch.setattr(catalog, "load_catalog",
                        lambda *a, **k: SimpleNamespace(docker=cfg))
    return cfg


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Fake card/restart transport + isolated ask state (as test_docker_ask)."""
    calls = {"cards": [], "restarts": []}
    monkeypatch.setattr(dt, "ASK_STATE_FILE", tmp_path / "asks.json")
    monkeypatch.setattr(dt, "APPROVALS_DIR", tmp_path / "approvals")
    monkeypatch.setattr(dt, "ASK_TIMEOUT_S", 0.0)
    monkeypatch.setattr(dt, "_send_restart_card",
                        lambda name, status: (calls["cards"].append(name)
                                              or f"rid-{name}", "chat"))
    monkeypatch.setattr(dt, "restart_container_raw",
                        lambda name: (calls["restarts"].append(name)
                                      or {"ok": True, "restarted": name}))
    return calls


def _scan(monkeypatch, *raws):
    monkeypatch.setattr(dt, "_docker", lambda method, path, **kw: list(raws))
    return dt.list_containers(all_containers=True)


def test_expected_down_oneshot_is_not_a_finding(catalog_cfg, flow, monkeypatch):
    data = _scan(monkeypatch, RUNNING, _raw(INIT, "exited", "Exited (0) 2 hours ago"))

    assert data["stopped_containers"] == []
    assert data["stopped"] == 0
    assert data["expected_down_containers"] == [INIT]
    # Still visible to the operator, just flagged.
    assert [c["expected_down"] for c in data["containers"] if c["name"] == INIT] == [True]
    # main() returns 2 only on a real finding; nothing to alert about here.
    assert not (data["stopped_containers"] or data["unhealthy_containers"])

    dt.handle_restart_approvals(data)
    assert flow["cards"] == []          # no "Restart docker-init_permissions-1?"


def test_nonzero_exit_still_alerts(catalog_cfg, flow, monkeypatch):
    """The allowlist covers a clean exit only — a failed chown must page."""
    data = _scan(monkeypatch, RUNNING, _raw(INIT, "exited", "Exited (1) 2 hours ago"))

    assert data["stopped_containers"] == [INIT]
    assert data["expected_down_containers"] == []

    dt.handle_restart_approvals(data)
    assert flow["cards"] == [INIT]


def test_ordinary_stopped_container_still_alerts_and_asks(catalog_cfg, flow, monkeypatch):
    """Regression guard for the 2026-07-22 jellyfin behaviour: `docker stop`
    also yields "Exited (0)" and is NOT in the allowlist, so it must still
    alert and still send a restart card."""
    data = _scan(monkeypatch, RUNNING, _raw("jellyfin", "exited", "Exited (0) 20 hours ago"))

    assert data["stopped_containers"] == ["jellyfin"]
    assert data["expected_down_containers"] == []

    dt.handle_restart_approvals(data)
    assert flow["cards"] == ["jellyfin"]
    assert json.loads(dt.ASK_STATE_FILE.read_text())["jellyfin"]["decision"] == "pending"


def test_label_opt_out(catalog_cfg, flow, monkeypatch):
    """A stack can declare itself ignorable without a Sentinel edit."""
    labelled = _raw("some-future-init-1", "exited", "Exited (0) 5 minutes ago",
                    labels={"sentinel.expected_down": "true"})
    data = _scan(monkeypatch, RUNNING, labelled)

    assert data["stopped_containers"] == []
    assert data["expected_down_containers"] == ["some-future-init-1"]

    dt.handle_restart_approvals(data)
    assert flow["cards"] == []


def test_unhealthy_is_never_suppressed(catalog_cfg, flow, monkeypatch):
    """An allowlisted name that is RUNNING but unhealthy has no exit code,
    so it can't qualify — health problems always surface."""
    data = _scan(monkeypatch, _raw(INIT, "running", "Up 2 minutes (unhealthy)"))

    assert data["unhealthy_containers"] == [INIT]
    assert data["expected_down_containers"] == []
