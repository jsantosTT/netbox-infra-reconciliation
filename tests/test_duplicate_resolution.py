"""Resolving a serial that several live sheet rows claim.

The case these pin is the one that looked harmless: the sheet duplicates a
serial, NetBox holds it on exactly one device, and so the lookup succeeds for
both rows and quietly binds one of them to its neighbour's device.
"""

from __future__ import annotations

import ipaddress

from nbrecon.inventory import DuplicateGroup, InventoryRow, Network, NetworkMap, duplicates
from nbrecon.inventory_audit import (
    AMBIGUOUS,
    BY_SERIAL,
    NETBOX_AGREES,
    SHEET_ERROR,
    UNDECIDABLE,
    audit_rows,
    index_devices,
    resolve_duplicate_serials,
)
from nbrecon.report.conflict_log import build_rows, render_conflict_csv

from .conftest import make_device


def row(hostname, serial, status="In Use", bmc_ip=None, line=2):
    return InventoryRow(
        line=line, hostname=hostname, status=status, serial=serial, bmc_ip=bmc_ip
    )


def device(device_id, name, serial):
    return make_device(
        id=device_id,
        name=name,
        serial=serial,
        oob_ip=None,
        url=f"https://netbox.example/dcim/devices/{device_id}/",
    )


def networks():
    return NetworkMap(
        networks=[Network("BMC net", ipaddress.IPv4Network("172.27.24.0/22"), is_bmc=True)]
    )


def group(value, rows):
    return DuplicateGroup(value=value, rows=tuple(rows))


# --- the verdicts ----------------------------------------------------------


def test_netbox_holding_distinct_serials_makes_it_a_sheet_error():
    rows = [row("f08cs08", "SHARED"), row("f08cs09", "SHARED")]
    _, by_name = index_devices(
        [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "OTHER")]
    )

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert res.verdict == SHEET_ERROR
    assert "f08cs08=SHARED" in res.detail and "f08cs09=OTHER" in res.detail
    assert "Correct the spreadsheet" in res.action


def test_netbox_carrying_the_same_duplicate_needs_hardware():
    rows = [row("f08cs08", "SHARED"), row("f08cs09", "SHARED")]
    _, by_name = index_devices(
        [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "SHARED")]
    )

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert res.verdict == NETBOX_AGREES
    assert "f08cs08, f08cs09" in res.detail
    assert "hardware" in res.action


def test_a_host_missing_from_netbox_is_undecidable():
    rows = [row("f08cs08", "SHARED"), row("f08cs09", "SHARED")]
    _, by_name = index_devices([device(1, "f08cs08", "SHARED")])

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert res.verdict == UNDECIDABLE
    assert "f08cs09" in res.detail
    assert [m.lookup for m in res.members] == ["found", "absent"]


def test_an_empty_netbox_serial_does_not_count_as_distinct():
    """Blank is not a value. Two blanks are not two different serials."""
    rows = [row("a", "SHARED"), row("b", "SHARED")]
    _, by_name = index_devices([device(1, "a", "SHARED"), device(2, "b", "")])

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert res.verdict == UNDECIDABLE


def test_three_hosts_where_two_still_clash_is_a_conflict():
    rows = [row("a", "SHARED"), row("b", "SHARED"), row("c", "SHARED")]
    _, by_name = index_devices(
        [device(1, "a", "X"), device(2, "b", "Y"), device(3, "c", "Y")]
    )

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert res.verdict == NETBOX_AGREES


def test_a_serial_kept_after_a_replacement_is_not_examined():
    rows = [row("old", "SHARED", status="Decommissioned"), row("new", "SHARED")]
    _, by_name = index_devices([device(1, "new", "SHARED")])

    assert resolve_duplicate_serials([group("SHARED", rows)], by_name) == []


def test_hosts_are_resolved_by_name_not_by_the_disputed_serial():
    """Looking the group up by serial would assume the answer it is testing."""
    rows = [row("f08cs08", "SHARED"), row("f08cs09", "SHARED")]
    _, by_name = index_devices(
        [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "OTHER")]
    )

    [res] = resolve_duplicate_serials([group("SHARED", rows)], by_name)

    assert [m.netbox_serial for m in res.members] == ["SHARED", "OTHER"]


# --- the matching guard ----------------------------------------------------


def test_a_contested_serial_never_binds_a_row_to_a_device():
    rows = [row("f08cs08", "SHARED", bmc_ip="172.27.25.173"),
            row("f08cs09", "SHARED", bmc_ip="172.27.25.174")]
    devices = [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "OTHER")]

    result = audit_rows(
        rows, devices, networks(), set(), None, dup_serials=duplicates(rows, "serial")
    )

    assert [a.match for a in result.audits] == [AMBIGUOUS, AMBIGUOUS]
    assert all(a.device is None for a in result.audits)
    assert all("more than one live row" in " ".join(a.blockers) for a in result.audits)


def test_a_contested_serial_produces_no_seed():
    """The failure this exists for: seeding .174 onto its neighbour's device."""
    rows = [row("f08cs08", "SHARED", bmc_ip="172.27.25.173"),
            row("f08cs09", "SHARED", bmc_ip="172.27.25.174")]
    devices = [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "OTHER")]

    result = audit_rows(
        rows, devices, networks(), set(), None, dup_serials=duplicates(rows, "serial")
    )

    assert result.seeds == []


def test_an_uncontested_serial_still_matches_normally():
    rows = [row("g02glx01", "SER1", bmc_ip="172.27.24.5")]
    devices = [device(1, "g02glx01", "SER1")]

    result = audit_rows(
        rows, devices, networks(), set(), None, dup_serials=duplicates(rows, "serial")
    )

    assert result.audits[0].match == BY_SERIAL


def test_omitting_the_duplicate_groups_keeps_the_old_behaviour():
    """The parameter is optional, so existing callers are unaffected."""
    rows = [row("g02glx01", "SER1", bmc_ip="172.27.24.5")]
    result = audit_rows(rows, [device(1, "g02glx01", "SER1")], networks(), set(), None)

    assert result.audits[0].match == BY_SERIAL
    assert result.duplicate_serials == []


# --- the conflict log ------------------------------------------------------


def _audit_with_conflict():
    rows = [row("f08cs08", "SHARED", line=2), row("f08cs09", "SHARED", line=3)]
    devices = [device(1, "f08cs08", "SHARED"), device(2, "f08cs09", "OTHER")]
    return rows, audit_rows(
        rows, devices, networks(), set(), None, dup_serials=duplicates(rows, "serial")
    )


def test_the_conflict_log_names_both_hosts_and_what_netbox_holds():
    rows, audit = _audit_with_conflict()

    entries = build_rows([], [], duplicates(rows, "serial"), audit)

    assert [e["hostname"] for e in entries] == ["f08cs08", "f08cs09"]
    assert [e["netbox_value"] for e in entries] == ["SHARED", "OTHER"]
    assert {e["verdict"] for e in entries} == {SHEET_ERROR}
    # The sheet's owner edits by line number, so it has to survive the join.
    assert [e["line"] for e in entries] == ["2", "3"]


def test_hardware_conflicts_are_listed_before_sheet_errors():
    rows = [row("a", "S1"), row("b", "S1"), row("c", "S2"), row("d", "S2")]
    devices = [
        device(1, "a", "S1"), device(2, "b", "S1"),  # NetBox agrees: hardware
        device(3, "c", "X"), device(4, "d", "Y"),    # NetBox disagrees: sheet
    ]
    audit = audit_rows(
        rows, devices, networks(), set(), None, dup_serials=duplicates(rows, "serial")
    )

    entries = build_rows([], [], duplicates(rows, "serial"), audit)

    assert [e["verdict"] for e in entries] == [NETBOX_AGREES] * 2 + [SHEET_ERROR] * 2


def test_offline_says_unresolved_rather_than_leaving_the_verdict_blank():
    rows = [row("f08cs08", "SHARED"), row("f08cs09", "SHARED")]

    entries = build_rows([], [], duplicates(rows, "serial"), None)

    assert {e["verdict"] for e in entries} == {UNDECIDABLE}
    assert all("--offline" in e["action"] for e in entries)


def test_bmc_and_hostname_duplicates_reach_the_log_too():
    rows = [
        row("dup", "S1", bmc_ip="172.27.24.5", line=2),
        row("dup", "S2", bmc_ip="172.27.24.5", line=3),
    ]

    entries = build_rows(duplicates(rows, "hostname"), duplicates(rows, "bmc_ip"), [], None)

    kinds = {e["kind"] for e in entries}
    assert kinds == {"duplicate-bmc-ip", "duplicate-hostname"}
    assert {e["line"] for e in entries} == {"2", "3"}


def test_the_csv_round_trips_with_a_stable_header():
    rows, audit = _audit_with_conflict()
    text = render_conflict_csv(build_rows([], [], duplicates(rows, "serial"), audit))

    header, first = text.splitlines()[0], text.splitlines()[1]
    assert header.startswith("kind,value,line,hostname,status")
    assert first.startswith("duplicate-serial,SHARED,2,f08cs08,In Use,SHARED,SHARED")


def test_no_conflicts_still_produces_a_readable_file():
    assert render_conflict_csv([]).strip() == ",".join(
        ["kind", "value", "line", "hostname", "status", "sheet_value",
         "netbox_value", "netbox_device", "verdict", "action"]
    )
