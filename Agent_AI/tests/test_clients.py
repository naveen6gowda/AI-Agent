"""HTTP integration clients against mocked APIs (respx — no network).

URLs are built from the modules' own config constants, so the same tests
run on the dev box and in CI regardless of environment.
"""

import httpx
import respx

import docker_tools
import tools

# ── Proxmox ─────────────────────────────────────────────────────────


@respx.mock
def test_list_proxmox_nodes_parses_and_rounds():
    respx.get(f"{tools._PROXMOX_BASE}/nodes").mock(
        return_value=httpx.Response(200, json={"data": [{
            "node": "pve", "status": "online", "uptime": 7200,
            "cpu": 0.123, "mem": 8 * 2**30, "maxmem": 16 * 2**30,
        }]})
    )
    out = tools.list_proxmox_nodes()
    assert out["count"] == 1
    node = out["nodes"][0]
    assert node["node"] == "pve"
    assert node["uptime_h"] == 2.0
    assert node["cpu_pct"] == 12.3
    assert node["mem_pct"] == 50.0


@respx.mock
def test_proxmox_auth_failure_is_an_error_dict_not_an_exception():
    respx.get(f"{tools._PROXMOX_BASE}/nodes").mock(
        return_value=httpx.Response(401)
    )
    out = tools.list_proxmox_nodes()
    assert "error" in out and "auth" in out["error"].lower()


@respx.mock
def test_proxmox_unreachable_is_an_error_dict():
    respx.get(f"{tools._PROXMOX_BASE}/nodes").mock(
        side_effect=httpx.ConnectError("boom")
    )
    out = tools.list_proxmox_nodes()
    assert "error" in out and "cannot reach" in out["error"]


# ── Home Assistant ──────────────────────────────────────────────────


@respx.mock
def test_get_ha_entity_strips_context():
    respx.get(f"{tools._HA_URL}/api/states/sensor.x").mock(
        return_value=httpx.Response(200, json={
            "entity_id": "sensor.x", "state": "21.5",
            "attributes": {"unit_of_measurement": "°C"},
            "context": {"id": "should-be-dropped"},
        })
    )
    out = tools.get_ha_entity("sensor.x")
    assert out["state"] == "21.5"
    assert "context" not in out


@respx.mock
def test_get_ha_entity_404_is_an_error_dict():
    respx.get(f"{tools._HA_URL}/api/states/sensor.gone").mock(
        return_value=httpx.Response(404)
    )
    out = tools.get_ha_entity("sensor.gone")
    assert out.get("error") == "not_found"


# ── Portainer / Docker ──────────────────────────────────────────────


def _container(name, state, status):
    return {"Id": "x" * 12, "Names": [f"/{name}"], "Image": "img",
            "State": state, "Status": status}


@respx.mock
def test_list_containers_rollup():
    eid = int(docker_tools._ENDPOINT_ID)
    respx.get(
        f"{docker_tools._URL}/api/endpoints/{eid}/docker/containers/json"
    ).mock(
        return_value=httpx.Response(200, json=[
            _container("pihole", "running", "Up 2 days (healthy)"),
            _container("broken", "exited", "Exited (1) 3 hours ago"),
        ])
    )
    out = docker_tools.list_containers()
    assert out["total"] == 2
    assert out["running"] == 1
    assert out["stopped"] == 1
    assert "broken" in out["stopped_containers"]


@respx.mock
def test_portainer_auth_failure_is_an_error_dict():
    eid = int(docker_tools._ENDPOINT_ID)
    respx.get(
        f"{docker_tools._URL}/api/endpoints/{eid}/docker/containers/json"
    ).mock(return_value=httpx.Response(401))
    out = docker_tools.list_containers()
    assert "error" in out and "auth" in out["error"].lower()
