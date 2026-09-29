"""Correlation: serial is the only identity."""

from __future__ import annotations

from nbrecon.correlate import correlate, probe_address
from nbrecon.models import CollectionStatus, MatchMethod, UnmatchedReason

from .conftest import make_device, make_host


def reasons(entries):
    return {e.reason for e in entries}


def test_serial_match_is_authoritative():
    device = make_device(serial="SN-AAA-111")
    host = make_host(serial="SN-AAA-111")
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert len(result.pairs) == 1
    assert result.pairs[0].match_method is MatchMethod.SERIAL
    assert not result.pairs[0].requires_explicit_approval


def test_serial_comparison_ignores_case_and_whitespace():
    device = make_device(serial=" sn-aaa-111 ")
    host = make_host(serial="SN-AAA-111")
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert len(result.pairs) == 1


def test_hostname_only_produces_a_suggested_match():
    device = make_device(serial=None, name="gx-lab-01")
    host = make_host(serial="SN-NEW-999", hostname="gx-lab-01")
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.match_method is MatchMethod.HOSTNAME_SUGGESTED
    assert pair.requires_explicit_approval


def test_bmc_ip_produces_a_suggested_match_when_hostname_differs():
    device = make_device(serial=None, name="gx-lab-01")
    host = make_host(serial="SN-NEW-999", hostname="completely-different")
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert len(result.pairs) == 1
    assert result.pairs[0].match_method is MatchMethod.BMC_IP_SUGGESTED
    assert result.pairs[0].requires_explicit_approval


def test_device_with_a_serial_is_never_matched_by_suggestion():
    """A populated but different serial is an identity problem, not a weak match."""
    device = make_device(serial="SN-OLD-000", name="gx-lab-01")
    host = make_host(serial="SN-NEW-999", hostname="gx-lab-01")
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert result.pairs == []
    assert UnmatchedReason.IN_NETBOX_NOT_ON_HOST in reasons(result.unmatched)


def test_duplicate_netbox_serial_quarantines_both():
    a = make_device(id=1, name="gx-1", serial="SN-DUP")
    b = make_device(id=2, name="gx-2", serial="SN-DUP")
    host = make_host(serial="SN-DUP")
    result = correlate([a, b], {"10.0.0.1": host}, {1: "10.0.0.1"})

    assert result.pairs == []
    dupes = [e for e in result.unmatched if e.reason is UnmatchedReason.DUPLICATE_SERIAL]
    assert {e.device_id for e in dupes} == {1, 2}


def test_duplicate_host_serial_quarantines_both_hosts():
    device = make_device(serial="SN-DUP")
    hosts = {
        "10.0.0.1": make_host(serial="SN-DUP", bmc_ip="10.0.0.1"),
        "10.0.0.2": make_host(serial="SN-DUP", bmc_ip="10.0.0.2"),
    }
    result = correlate([device], hosts, {device.id: "10.0.0.1"})

    assert result.pairs == []
    assert UnmatchedReason.DUPLICATE_SERIAL in reasons(result.unmatched)


def test_host_without_a_serial_is_never_matched():
    device = make_device(serial=None)
    host = make_host(serial=None, hostname=None)
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert result.pairs == []
    assert UnmatchedReason.MISSING_SERIAL in reasons(result.unmatched)


def test_device_without_a_bmc_ip_is_reported():
    device = make_device(oob_ip=None, primary_ip4=None)
    result = correlate([device], {}, {})

    assert UnmatchedReason.NO_BMC_IP in reasons(result.unmatched)


def test_unreachable_bmc_becomes_a_collection_failure():
    device = make_device()
    host = make_host(status=CollectionStatus.UNREACHABLE, errors=["timeout"])
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert result.pairs == []
    assert result.collection_failures
    assert result.collection_failures[0].reason is UnmatchedReason.COLLECTION_FAILED


def test_a_failed_device_is_reported_once_not_twice():
    """The device loop names the device; the host loop must not repeat it."""
    device = make_device()
    host = make_host(status=CollectionStatus.UNREACHABLE, errors=["timeout"])
    result = correlate([device], {"10.100.1.50": host}, {device.id: "10.100.1.50"})

    assert len(result.collection_failures) == 1
    assert result.collection_failures[0].device_id == device.id


def test_host_not_in_netbox_is_reported():
    device = make_device(id=1, serial="SN-KNOWN", oob_ip="10.0.0.1/24")
    hosts = {
        "10.0.0.1": make_host(serial="SN-KNOWN"),
        "10.0.0.2": make_host(serial="SN-STRANGER", hostname="stranger"),
    }
    result = correlate([device], hosts, {1: "10.0.0.1"})

    assert len(result.pairs) == 1
    assert UnmatchedReason.ON_HOST_NOT_IN_NETBOX in reasons(result.unmatched)


def test_probe_address_prefers_oob_and_strips_the_mask():
    assert probe_address(make_device(oob_ip="10.1.2.3/24")) == "10.1.2.3"
    assert probe_address(make_device(oob_ip=None, primary_ip4="10.9.9.9/26")) == "10.9.9.9"
    assert probe_address(make_device(oob_ip=None, primary_ip4=None)) is None
