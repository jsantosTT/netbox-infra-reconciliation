"""The audit's matching and seed decisions.

Each test here is a way the audit could send BMC credentials to the wrong
machine, pinned so it cannot start doing so quietly.
"""

from __future__ import annotations

import ipaddress

import pytest

from nbrecon.errors import SafetyViolation
from nbrecon.inventory import InventoryRow, Network, NetworkMap
from nbrecon.inventory_audit import (
    AMBIGUOUS,
    BY_HOSTNAME,
    BY_SERIAL,
    DIFFER,
    UNMATCHED,
    ReadOnlyNetBox,
    audit_rows,
)

from .conftest import make_device


def row(hostname="g02glx01", serial="SER1", bmc_ip="172.27.24.5", **kw):
    return InventoryRow(
        line=2, hostname=hostname, status="In Use", serial=serial, bmc_ip=bmc_ip, **kw
    )


def device(device_id=1, name="g02glx01", serial="SER1", oob_ip=None, **kw):
    return make_device(
        id=device_id,
        name=name,
        serial=serial,
        oob_ip=oob_ip,
        url=f"https://netbox.example/dcim/devices/{device_id}/",
        **kw,
    )


def networks():
    return NetworkMap(
        networks=[
            Network("BMC net", ipaddress.IPv4Network("172.27.24.0/22"), is_bmc=True),
            Network("Host Mgmt net", ipaddress.IPv4Network("172.27.28.0/22"), is_bmc=False),
        ]
    )


class FakeClient:
    """Stands in for the NetBox IPAM lookup."""

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.calls: list[str] = []

    def find_ip_address(self, address):
        self.calls.append(address)
        return self.entries.get(address)


def run(rows, devices, client=None, collisions=None, nets=None):
    return audit_rows(
        rows, devices, nets or networks(), collisions or set(), client or FakeClient()
    )


# --- matching --------------------------------------------------------------


def test_a_row_matches_on_serial():
    result = run([row()], [device()])
    assert result.audits[0].match == BY_SERIAL


def test_a_hostname_match_is_a_suggestion_and_is_never_seeded():
    result = run([row(serial=None)], [device(serial="OTHER")])
    audit = result.audits[0]

    assert audit.match == BY_HOSTNAME
    assert audit.seed is None
    assert "serial is the tool's identity" in " ".join(audit.blockers)


def test_a_serial_on_two_devices_matches_neither():
    result = run([row()], [device(1, "a"), device(2, "b")])
    audit = result.audits[0]

    assert audit.match == AMBIGUOUS
    assert audit.device is None
    assert audit.seed is None


def test_a_row_netbox_has_never_heard_of_is_reported():
    result = run([row(hostname="ghost", serial="NOPE")], [device()])
    assert result.audits[0].match == UNMATCHED


def test_devices_with_no_sheet_row_are_reported_back():
    result = run([row()], [device(), device(2, "unlisted", "SER2")])
    assert [d.name for d in result.unmatched_devices] == ["unlisted"]


# --- comparison ------------------------------------------------------------


def test_the_mask_is_ignored_when_comparing_addresses():
    result = run([row(bmc_ip="172.27.24.5")], [device(oob_ip="172.27.24.5/22")])
    assert result.audits[0].verdict("bmc_ip") == "agree"


def test_a_real_disagreement_is_reported_and_not_seeded():
    result = run([row(bmc_ip="172.27.24.5")], [device(oob_ip="172.27.24.9/22")])
    audit = result.audits[0]

    assert audit.verdict("bmc_ip") == DIFFER
    assert audit.seed is None, "the sheet does not get to overrule NetBox"


# --- the seed decision -----------------------------------------------------


def test_an_empty_netbox_field_with_a_free_ipam_entry_is_ready():
    client = FakeClient({"172.27.24.5": {"display": "172.27.24.5/22"}})
    result = run([row()], [device()], client)
    seed = result.audits[0].seed

    assert seed is not None and seed.ready
    assert seed.network == "BMC net"
    assert result.ready_seeds == [seed]


def test_an_absent_ipam_entry_blocks_because_apply_will_not_create_one():
    result = run([row()], [device()], FakeClient({}))
    audit = result.audits[0]

    assert audit.seed is not None and not audit.seed.ready
    assert audit.seed.ipam_state == "absent"
    assert "never creates IPAM objects" in " ".join(audit.blockers)


def test_an_ipam_entry_owned_by_another_device_blocks():
    client = FakeClient(
        {"172.27.24.5": {"assigned_object": {"device": {"id": 99, "name": "someone-else"}}}}
    )
    result = run([row()], [device(device_id=1)], client)
    audit = result.audits[0]

    assert audit.seed is not None and not audit.seed.ready
    assert "already assigned to someone-else" in " ".join(audit.blockers)


def test_an_ipam_entry_already_on_this_device_is_not_a_conflict():
    client = FakeClient(
        {"172.27.24.5": {"assigned_object": {"device": {"id": 1, "name": "g02glx01"}}}}
    )
    result = run([row()], [device(device_id=1)], client)

    assert result.audits[0].seed.ready


def test_a_live_collision_blocks_even_when_everything_else_is_clean():
    client = FakeClient({"172.27.24.5": {"display": "172.27.24.5/22"}})
    result = run([row()], [device()], client, collisions={"172.27.24.5"})
    audit = result.audits[0]

    assert audit.seed is None
    assert "more than one host" in " ".join(audit.blockers)


def test_an_address_in_a_non_bmc_network_blocks():
    result = run([row(bmc_ip="172.27.28.5")], [device()])
    audit = result.audits[0]

    assert audit.seed is None
    assert "not a BMC network" in " ".join(audit.blockers)


def test_an_address_in_no_known_network_blocks():
    result = run([row(bmc_ip="10.220.16.203")], [device()])
    assert "not inside any known network" in " ".join(result.audits[0].blockers)


def test_the_network_check_is_skipped_when_unconfigured():
    client = FakeClient({"10.220.16.203": {"display": "10.220.16.203/32"}})
    result = run([row(bmc_ip="10.220.16.203")], [device()], client, nets=NetworkMap())
    seed = result.audits[0].seed

    assert seed is not None and seed.ready
    assert seed.network == "unchecked"


def test_the_audit_looks_up_the_same_addresses_apply_would():
    client = FakeClient({})
    run([row(bmc_ip="172.27.24.5")], [device()], client)

    assert client.calls == ["172.27.24.5", "172.27.24.5/32"]


# --- the read-only guarantee -----------------------------------------------


class Spy:
    def __init__(self):
        self.read = False

    def fetch_devices_by(self, *_args, **_kw):
        self.read = True
        return []

    def patch_device(self, *_args, **_kw):  # pragma: no cover - must never run
        raise AssertionError("the audit wrote to NetBox")


def test_reads_pass_through():
    client = ReadOnlyNetBox(Spy())
    assert client.fetch_devices_by("serial", []) == []


@pytest.mark.parametrize(
    "method", ["patch_device", "patch_interface", "create_journal_entry", "session"]
)
def test_writes_are_refused_by_the_wrapper(method):
    client = ReadOnlyNetBox(Spy())
    with pytest.raises(SafetyViolation, match="read-only"):
        getattr(client, method)


def test_the_wrapped_client_cannot_be_swapped_out():
    client = ReadOnlyNetBox(Spy())
    with pytest.raises(SafetyViolation):
        client._client = Spy()
