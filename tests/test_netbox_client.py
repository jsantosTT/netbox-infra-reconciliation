"""NetBox client: the queries a scope actually turns into.

These exercise the real client against mocked HTTP, because the thing worth
pinning is the request it builds, not that a fake returned what it was told to.
"""

from __future__ import annotations

import pytest
import responses

from nbrecon.collect.netbox import NetBoxClient
from nbrecon.config import NetBoxSettings, Scope
from nbrecon.errors import ScopeError

BASE = "https://netbox.example"
DEVICES = f"{BASE}/api/dcim/devices/"
RACKS = f"{BASE}/api/dcim/racks/"


@pytest.fixture
def client() -> NetBoxClient:
    return NetBoxClient(NetBoxSettings(url=BASE, token="t"))


def page(*results: dict) -> dict:
    return {"count": len(results), "next": None, "previous": None, "results": list(results)}


def device_payload(**overrides) -> dict:
    payload = {
        "id": 1,
        "name": "gx-lab-01",
        "serial": "SN-1",
        "device_type": {"id": 7, "slug": "galaxy-wormhole"},
        "site": {"slug": "lab"},
        "status": {"value": "active"},
        "rack": {"id": 12, "name": "RACK-42"},
        "last_updated": "2026-09-01T00:00:00Z",
        "custom_fields": {},
        "tags": [],
    }
    payload.update(overrides)
    return payload


def query_of(call) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(call.request.url).query)


# --- rack scope -----------------------------------------------------------
@responses.activate
def test_rack_is_resolved_to_an_id_before_querying_devices(client):
    """A name is ambiguous across NetBox versions; an ID is not."""
    responses.add(responses.GET, RACKS, json=page({"id": 12, "site": {"slug": "lab"}}))
    responses.add(responses.GET, DEVICES, json=page(device_payload()))

    devices = client.fetch_devices(Scope(rack="RACK-42"))

    assert [d.id for d in devices] == [1]
    assert query_of(responses.calls[0])["name"] == ["RACK-42"]
    assert query_of(responses.calls[1])["rack_id"] == ["12"]


@responses.activate
def test_rack_and_site_are_both_applied(client):
    responses.add(responses.GET, RACKS, json=page({"id": 12, "site": {"slug": "lab"}}))
    responses.add(responses.GET, DEVICES, json=page(device_payload()))

    client.fetch_devices(Scope(site="lab", rack="RACK-42"))

    assert query_of(responses.calls[0])["site"] == ["lab"]
    device_query = query_of(responses.calls[1])
    assert device_query["site"] == ["lab"]
    assert device_query["rack_id"] == ["12"]


@responses.activate
def test_unknown_rack_stops_the_run_instead_of_widening_it(client):
    """Selecting nothing must not fall back to every device in the site."""
    responses.add(responses.GET, RACKS, json=page())

    with pytest.raises(ScopeError, match="not found"):
        client.fetch_devices(Scope(site="lab", rack="NO-SUCH-RACK"))

    assert len(responses.calls) == 1  # devices were never queried


@responses.activate
def test_ambiguous_rack_name_names_the_sites(client):
    responses.add(
        responses.GET,
        RACKS,
        json=page(
            {"id": 12, "site": {"slug": "lab-aus"}},
            {"id": 13, "site": {"slug": "lab-tor"}},
        ),
    )

    with pytest.raises(ScopeError) as excinfo:
        client.fetch_devices(Scope(rack="RACK-42"))

    assert "lab-aus" in str(excinfo.value)
    assert "lab-tor" in str(excinfo.value)
    assert len(responses.calls) == 1


@responses.activate
def test_no_rack_means_no_rack_lookup(client):
    responses.add(responses.GET, DEVICES, json=page(device_payload()))

    client.fetch_devices(Scope(site="lab"))

    assert len(responses.calls) == 1
    assert "rack_id" not in query_of(responses.calls[0])


# --- snapshot -------------------------------------------------------------
@responses.activate
def test_snapshot_carries_the_rack_name(client):
    responses.add(responses.GET, DEVICES, json=page(device_payload()))

    assert client.fetch_devices(Scope(site="lab"))[0].rack == "RACK-42"


@responses.activate
def test_a_device_with_no_rack_is_not_a_failure(client):
    responses.add(responses.GET, DEVICES, json=page(device_payload(rack=None)))

    assert client.fetch_devices(Scope(site="lab"))[0].rack is None


@responses.activate
def test_explicit_device_names_skip_the_rack_lookup(client):
    """Named devices take precedence; rack is not used to widen the query."""
    responses.add(responses.GET, DEVICES, json=page(device_payload()))

    client.fetch_devices(Scope(rack="RACK-42", devices=["gx-lab-01"]))

    assert len(responses.calls) == 1
    assert query_of(responses.calls[0])["name"] == ["gx-lab-01"]
