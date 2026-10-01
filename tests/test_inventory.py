"""Parsing the inventory export.

The cases here are taken from the real spreadsheet, because every one of them
is a shape that a naive parser would silently get wrong.
"""

from __future__ import annotations

import pytest

from nbrecon.errors import ConfigError
from nbrecon.inventory import NetworkMap, duplicates, iter_rows, parse_inventory

HEADER = (
    "Hostname,Status,Site,Rack,RU Location,Serial #,BMC IP,BMC MAC,IP (s),"
    "MAC Address,Model #\n"
)


def write_csv(tmp_path, *rows: str):
    path = tmp_path / "servers.csv"
    path.write_text(HEADER + "".join(r if r.endswith("\n") else r + "\n" for r in rows))
    return path


def test_rows_without_a_hostname_are_padding(tmp_path):
    path = write_csv(
        tmp_path,
        "g02glx01,In Use,Austin DRT,G02,15,SER1,172.27.24.5,,172.27.28.5,,Model",
        ",,,,,,,,,,",
        ",,,,,,,,,,",
    )
    result = parse_inventory(path)

    assert result.total_lines == 3
    assert result.skipped_blank == 2
    assert [r.hostname for r in result.rows] == ["g02glx01"]


def test_a_cell_with_two_addresses_yields_none_and_a_finding(tmp_path):
    path = write_csv(
        tmp_path,
        'e07cs06,In Use,,,,SER1,172.27.24.5,,"172.27.107.11, 172.27.28.81",,Model',
    )
    row = parse_inventory(path).rows[0]

    assert row.primary_ip is None, "picking one of two addresses is a guess"
    assert len(row.issues) == 1
    assert "172.27.107.11" in row.issues[0].detail
    assert "172.27.28.81" in row.issues[0].detail


def test_an_annotated_address_is_read_but_reported(tmp_path):
    path = write_csv(
        tmp_path,
        "aus-glx-01,In Use,,,,SER1,172.27.24.5,,172.27.29.12 (pre-IT IP),,Model",
    )
    row = parse_inventory(path).rows[0]

    assert row.primary_ip == "172.27.29.12"
    assert row.issues[0].kind == "has extra text around the address"


@pytest.mark.parametrize("placeholder", ["DHCP", "N/A", "-", "tbd"])
def test_placeholders_are_not_addresses(tmp_path, placeholder):
    path = write_csv(tmp_path, f"h1,In Use,,,,SER1,{placeholder},,,,Model")
    row = parse_inventory(path).rows[0]

    assert row.bmc_ip is None
    assert row.issues[0].kind == "holds no IPv4 address"


def test_macs_are_normalised_for_comparison(tmp_path):
    path = write_csv(
        tmp_path,
        "h1,In Use,,,,SER1,172.27.24.5,00:30:D6:2D:E7:F2,,,Model",
        "h2,In Use,,,,SER2,172.27.24.6,0030d62de7f3,,,Model",
        "h3,In Use,,,,SER3,172.27.24.7,not-a-mac,,,Model",
    )
    rows = parse_inventory(path).rows

    assert rows[0].bmc_mac == "00:30:d6:2d:e7:f2"
    assert rows[1].bmc_mac == "00:30:d6:2d:e7:f3"
    assert rows[2].bmc_mac is None


def test_trailing_whitespace_is_counted_not_listed_per_row(tmp_path):
    path = write_csv(
        tmp_path,
        "h1,Decommissioned ,,,,SER1,172.27.24.5,,,,Model",
        "h2,Decommissioned ,,,,SER2,172.27.24.6,,,,Model",
    )
    result = parse_inventory(path)

    assert result.issues == []
    assert result.normalised == {"Status: surrounding whitespace trimmed": 2}
    assert all(r.status == "Decommissioned" for r in result.rows)


def test_a_missing_column_is_rejected_rather_than_read_as_empty(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("Hostname,Status\nh1,In Use\n")

    with pytest.raises(ConfigError, match="Serial #"):
        parse_inventory(path)


# --- duplicates ------------------------------------------------------------


def test_a_reused_bmc_ip_is_historical_when_only_one_row_is_live(tmp_path):
    path = write_csv(
        tmp_path,
        "e01cs06,In Use,,,,SER1,172.27.25.6,,,,Model",
        "g03cs01,Decommissioned,,,,SER1,172.27.25.6,,,,Model",
    )
    groups = duplicates(parse_inventory(path).rows, "bmc_ip")

    assert len(groups) == 1
    assert groups[0].historical, "a rename keeps the old row; that is not a conflict"


def test_a_reused_bmc_ip_is_a_collision_when_both_rows_are_live(tmp_path):
    path = write_csv(
        tmp_path,
        "f08cs02,In Use,,,,SER1,172.27.25.135,,,,Model",
        "f11-hypervisor-01,In Use,,,,,172.27.25.135,,,,Model",
    )
    groups = duplicates(parse_inventory(path).rows, "bmc_ip")

    assert not groups[0].historical
    assert "f08cs02" in groups[0].describe()


def test_duplicate_detection_folds_case_but_reports_the_original(tmp_path):
    path = write_csv(
        tmp_path,
        "h1,In Use,,,,abc123,172.27.24.5,,,,Model",
        "h2,In Use,,,,ABC123,172.27.24.6,,,,Model",
    )
    groups = duplicates(parse_inventory(path).rows, "serial")

    assert len(groups) == 1
    assert groups[0].value == "abc123", "show the serial as the sheet writes it"


def test_status_filter_is_case_insensitive(tmp_path):
    path = write_csv(
        tmp_path,
        "h1,In Use,,,,SER1,172.27.24.5,,,,Model",
        "h2,Decommissioned,,,,SER2,172.27.24.6,,,,Model",
    )
    rows = parse_inventory(path).rows

    assert [r.hostname for r in iter_rows(rows, ["in use"])] == ["h1"]
    assert len(list(iter_rows(rows, []))) == 2


# --- networks --------------------------------------------------------------


def test_the_most_specific_network_wins(tmp_path):
    path = tmp_path / "nets.yaml"
    path.write_text(
        "bmc:\n"
        "  - { name: BMC net, cidr: 172.27.24.0/22 }\n"
        "other:\n"
        "  - { name: Cloud, cidr: 172.16.0.0/12 }\n"
    )
    networks = NetworkMap.load(path)

    hit = networks.classify("172.27.24.5")
    assert hit is not None and hit.name == "BMC net" and hit.is_bmc

    miss = networks.classify("172.27.28.5")
    assert miss is not None and miss.name == "Cloud" and not miss.is_bmc

    assert networks.classify("10.0.0.1") is None


def test_an_unconfigured_network_map_claims_nothing():
    assert NetworkMap().configured is False
    assert NetworkMap().classify("172.27.24.5") is None
