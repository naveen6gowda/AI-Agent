"""HTTP integration clients against mocked APIs (respx — no network).

URLs are built from the modules' own config constants, so the same tests
run on the dev box and in CI regardless of environment.
"""

import httpx
import respx

import docker_tools
import firefly_client
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


# ── Firefly III ─────────────────────────────────────────────────────


_COLLECTED_TXN = {
    "kind": "direct_debit_collected", "direction": "withdrawal",
    "amount": "81.00", "currency": "EUR",
    "merchant": "VATTENFALL EUROPE SALES",
    "description": "VATTENFALL EUROPE SALES",
    "date": "2026-07-06T17:14:26+02:00",
    "external_id": "n26-test-collected", "notes": "test",
    "tags": ["collected"],
}


def _mock_external_id_search(found=False):
    respx.get(
        f"{firefly_client._URL}/api/v1/search/transactions",
        params={"query": 'external_id_is:"n26-test-collected"'},
    ).mock(return_value=httpx.Response(
        200, json={"data": [{"id": "440"}] if found else []}))


def _mock_scheduled_search(data):
    respx.get(
        f"{firefly_client._URL}/api/v1/search/transactions",
        params={"query": 'tag_is:scheduled amount_is:"81.00"'},
    ).mock(return_value=httpx.Response(200, json={"data": data}))


@respx.mock
def test_collected_debit_pairs_with_scheduled_twin():
    """The 'has been collected' text must NOT double-book a direct debit we
    already stored from its 'will be debited on <date>' advance notice."""
    _mock_external_id_search()
    _mock_scheduled_search([{
        "id": "440",
        "attributes": {"transactions": [{
            "amount": "81.000000000000",
            "destination_name": "VATTENFALL EUROPE SALES",
            "date": "2026-07-06T00:00:00+02:00",
            "tags": ["sentinel", "scheduled"],
        }]},
    }])
    # no POST route mocked: an insert attempt would fail the test loudly
    out = firefly_client.create_transaction(_COLLECTED_TXN)
    assert out["result"] == "duplicate_of_scheduled"
    assert out["firefly_id"] == "440"


@respx.mock
def test_collected_debit_without_notice_is_stored():
    _mock_external_id_search()
    _mock_scheduled_search([])   # no advance notice booked
    respx.get(f"{firefly_client._URL}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": "7", "attributes": {"name": "N26"}}]}))
    post = respx.post(f"{firefly_client._URL}/api/v1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"id": "999"}}))
    out = firefly_client.create_transaction(_COLLECTED_TXN)
    assert out["result"] == "stored" and out["firefly_id"] == "999"
    assert post.called


@respx.mock
def test_scheduled_match_ignores_other_merchants():
    _mock_external_id_search()
    _mock_scheduled_search([{
        "id": "555",
        "attributes": {"transactions": [{
            "amount": "81.000000000000",
            "destination_name": "SOMEONE ELSE ENTIRELY",
            "date": "2026-07-06T00:00:00+02:00",
            "tags": ["sentinel", "scheduled"],
        }]},
    }])
    respx.get(f"{firefly_client._URL}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json={"data": [
            {"id": "7", "attributes": {"name": "N26"}}]}))
    respx.post(f"{firefly_client._URL}/api/v1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"id": "1000"}}))
    out = firefly_client.create_transaction(_COLLECTED_TXN)
    assert out["result"] == "stored"
