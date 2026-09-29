"""Safety invariants of the diff engine."""

from __future__ import annotations

from nbrecon.diff import DeviceContext, DiffEngine
from nbrecon.models import Action, Category, MatchMethod, UnmatchedReason

from .conftest import make_device, make_host, make_pair


def engine(matrix, custom_fields, sku_map, **kwargs):
    return DiffEngine(matrix, custom_fields, sku_map, **kwargs)


def writes_for(result, key):
    return [p for p in result.proposals if p.field_key == key and p.action is Action.WRITE]


def props_for(result, key):
    return [p for p in result.proposals if p.field_key == key]


# --- unknown never clears -------------------------------------------------
def test_none_from_host_never_produces_a_write(matrix, custom_fields, sku_map):
    """A field the collector could not determine must not touch NetBox."""
    host = make_host(topology=None, chassis_revision=None, asset_tag=None)
    device = make_device(
        custom_fields={"topology": "mesh", "chassis_revision": "RevB"}, asset_tag="ASSET-1"
    )
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device, host)])

    assert writes_for(result, "topology") == []
    assert writes_for(result, "chassis_revision") == []
    assert writes_for(result, "asset_tag") == []


def test_totally_failed_collection_produces_no_writes(matrix, custom_fields, sku_map):
    host = make_host(
        serial=None, model=None, part_number=None, asset_tag=None, bmc_ipv4=None,
        bmc_mac=None, chassis_revision=None, flash_version=None, fw_version=None,
        kmd_version=None, smi_version=None, topology=None, hostname=None,
    )
    device = make_device(custom_fields={"topology": "mesh"})
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device, host)])

    assert [p for p in result.proposals if p.action is Action.WRITE] == []


# --- fill-if-empty --------------------------------------------------------
def test_fill_if_empty_writes_into_an_empty_value(matrix, custom_fields, sku_map):
    device = make_device(asset_tag=None)
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    writes = writes_for(result, "asset_tag")
    assert len(writes) == 1
    assert writes[0].proposed == "ASSET-1"


def test_fill_if_empty_flags_rather_than_overwrites(matrix, custom_fields, sku_map):
    device = make_device(asset_tag="EXISTING-TAG")
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    assert writes_for(result, "asset_tag") == []
    flags = [p for p in props_for(result, "asset_tag") if p.action is Action.FLAG]
    assert len(flags) == 1
    assert flags[0].current == "EXISTING-TAG"


def test_serial_mismatch_is_an_identity_problem_not_a_diff(matrix, custom_fields, sku_map):
    device = make_device(serial="SN-OLD-000")
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    assert writes_for(result, "serial") == []
    flags = [p for p in props_for(result, "serial") if p.action is Action.FLAG]
    assert len(flags) == 1
    assert "identity mismatch" in flags[0].reason
    assert any(u.reason is UnmatchedReason.IN_NETBOX_NOT_ON_HOST for u in result.unmatched)


# --- host wins ------------------------------------------------------------
def test_host_owned_write_policy_overwrites(matrix, custom_fields, sku_map):
    device = make_device(custom_fields={"topology": "old-topology"})
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    writes = writes_for(result, "topology")
    assert len(writes) == 1
    assert writes[0].current == "old-topology"
    assert writes[0].proposed == "mesh"


def test_identical_values_produce_nothing(matrix, custom_fields, sku_map):
    device = make_device(
        asset_tag="ASSET-1",
        custom_fields={
            "topology": "mesh",
            "chassis_revision": "RevB",
            "flash_version": "1.2.3",
            "fw_version": "4.5.6",
            "kmd_version": "7.8.9",
            "smi_version": "2.0.0",
        },
    )
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    for key in ("asset_tag", "topology", "chassis_revision", "flash_version"):
        assert writes_for(result, key) == []


def test_mac_and_ip_compare_in_their_own_shape(matrix, custom_fields, sku_map):
    """Formatting differences are not changes."""
    device = make_device(oob_ip="10.100.1.50/24")
    host = make_host(bmc_mac="AA-BB-CC-DD-EE-FF")
    result = engine(matrix, custom_fields, sku_map).run(
        [make_pair(device, host)],
        {device.id: DeviceContext(bmc_interface={"mac_address": "aa:bb:cc:dd:ee:ff"})},
    )

    assert writes_for(result, "bmc_ip") == []
    assert writes_for(result, "bmc_mac") == []


# --- human-only and report-only ------------------------------------------
def test_human_only_fields_never_produce_writes(matrix, custom_fields, sku_map):
    device = make_device(site_slug="somewhere-else", status="offline")
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    for p in result.proposals:
        if p.category in (Category.HUMAN_ONLY, Category.REPORT_ONLY):
            assert p.action is not Action.WRITE


def test_status_suggestion_only_after_threshold(matrix, custom_fields, sku_map):
    device = make_device()
    below = engine(matrix, custom_fields, sku_map).run(
        [make_pair(device)], {device.id: DeviceContext(unreachable_streak=2)}
    )
    assert props_for(below, "status") == []

    at = engine(matrix, custom_fields, sku_map).run(
        [make_pair(device)], {device.id: DeviceContext(unreachable_streak=3)}
    )
    suggestions = props_for(at, "status")
    assert len(suggestions) == 1
    assert suggestions[0].action is Action.SUGGEST
    assert "never writes status" in suggestions[0].reason


def test_hostname_disagreement_is_flagged_not_fixed(matrix, custom_fields, sku_map):
    device = make_device(name="gx-lab-01")
    host = make_host(hostname="gx-lab-1-renamed")
    pair = make_pair(device, host)
    pair.prometheus_node = "gx-lab-01"
    result = engine(matrix, custom_fields, sku_map).run([pair])

    flags = props_for(result, "hostname")
    assert len(flags) == 1
    assert flags[0].action is Action.FLAG
    assert "a human decides" in flags[0].reason


def test_matching_hostnames_produce_no_flag(matrix, custom_fields, sku_map):
    pair = make_pair()
    pair.prometheus_node = "gx-lab-01"
    result = engine(matrix, custom_fields, sku_map).run([pair])
    assert props_for(result, "hostname") == []


def test_power_state_is_reported_never_stored(matrix, custom_fields, sku_map):
    result = engine(matrix, custom_fields, sku_map).run([make_pair()])
    entries = props_for(result, "power_state")
    assert len(entries) == 1
    assert entries[0].action is Action.REPORT


# --- trays ----------------------------------------------------------------
def test_trays_are_reported_never_written(matrix, custom_fields, sku_map):
    from nbrecon.models import TrayFacts

    host = make_host(trays=[TrayFacts(tray_id="0", serial="TRAY-1")])
    result = engine(matrix, custom_fields, sku_map).run([make_pair(host=host)])

    entries = props_for(result, "tray_serials")
    assert len(entries) == 1
    assert entries[0].action is Action.REPORT
    assert "not modelled in NetBox" in entries[0].reason


# --- SKU mapping ----------------------------------------------------------
def test_unmapped_sku_reports_and_never_writes(matrix, empty_custom_fields, empty_sku_map):
    device = make_device(device_type_slug="something-else")
    result = engine(matrix, empty_custom_fields, empty_sku_map).run([make_pair(device)])

    assert writes_for(result, "device_type") == []
    reports = [p for p in props_for(result, "device_type") if p.action is Action.REPORT]
    assert len(reports) == 1
    assert "never creates device types" in reports[0].reason
    assert any(u.reason is UnmatchedReason.UNMAPPED_SKU for u in result.unmatched)


def test_mapped_sku_proposes_the_device_type(matrix, custom_fields, sku_map):
    device = make_device(device_type_slug="galaxy-blackhole")
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    writes = writes_for(result, "device_type")
    assert len(writes) == 1
    assert writes[0].proposed == "galaxy-wormhole"


def test_device_type_not_present_in_netbox_is_reported(matrix, custom_fields, sku_map):
    device = make_device(device_type_slug="galaxy-blackhole")
    result = engine(matrix, custom_fields, sku_map, known_device_type_slugs=set()).run(
        [make_pair(device)]
    )

    assert writes_for(result, "device_type") == []
    assert any("an admin must create it" in p.reason for p in props_for(result, "device_type"))


# --- unmapped custom fields ----------------------------------------------
def test_unmapped_custom_fields_are_skipped_not_guessed(matrix, empty_custom_fields, sku_map):
    """The shipped netbox_fields.yaml is blank, so those fields do nothing."""
    result = engine(matrix, empty_custom_fields, sku_map).run([make_pair()])

    assert writes_for(result, "topology") == []
    assert writes_for(result, "chassis_revision") == []
    assert "topology" in result.skipped_unmapped


# --- suggestion matches ---------------------------------------------------
def test_suggested_match_marks_every_write_explicit(matrix, custom_fields, sku_map):
    device = make_device(serial=None, asset_tag=None)
    pair = make_pair(device, method=MatchMethod.HOSTNAME_SUGGESTED)
    result = engine(matrix, custom_fields, sku_map).run([pair])

    writes = [p for p in result.proposals if p.action is Action.WRITE]
    assert writes
    assert all(p.requires_explicit_approval for p in writes)


def test_serial_fill_always_requires_explicit_approval(matrix, custom_fields, sku_map):
    device = make_device(serial=None)
    result = engine(matrix, custom_fields, sku_map).run([make_pair(device)])

    writes = writes_for(result, "serial")
    assert len(writes) == 1
    assert writes[0].requires_explicit_approval is True


# --- IPT ------------------------------------------------------------------
def test_ipt_is_skipped_entirely_when_jira_is_not_configured(matrix, custom_fields, sku_map):
    result = engine(matrix, custom_fields, sku_map, jira_enabled=False).run([make_pair()])

    assert props_for(result, "ipt_link") == []
    assert not any(u.reason is UnmatchedReason.MISSING_IPT for u in result.unmatched)


def test_missing_ipt_is_reported_when_jira_is_configured(matrix, custom_fields, sku_map):
    result = engine(matrix, custom_fields, sku_map).run([make_pair()])
    assert any(u.reason is UnmatchedReason.MISSING_IPT for u in result.unmatched)


def test_possible_ipt_match_is_flagged_not_linked(matrix, custom_fields, sku_map):
    from nbrecon.models import JiraIssue

    pair = make_pair()
    pair.jira_candidates = [JiraIssue(key="IPT-12345", url="https://jira/browse/IPT-12345")]
    result = engine(matrix, custom_fields, sku_map).run([pair])

    assert writes_for(result, "ipt_link") == []
    flags = [p for p in props_for(result, "ipt_link") if p.action is Action.FLAG]
    assert len(flags) == 1
    assert "a human confirms" in flags[0].reason


def test_confirmed_ipt_fills_an_empty_link(matrix, custom_fields, sku_map):
    from nbrecon.models import JiraIssue

    pair = make_pair()
    pair.jira = JiraIssue(key="IPT-1", url="https://jira/browse/IPT-1")
    result = engine(matrix, custom_fields, sku_map).run([pair])

    writes = writes_for(result, "ipt_link")
    assert len(writes) == 1
    assert writes[0].proposed == "https://jira/browse/IPT-1"


def test_existing_different_ipt_link_is_flagged(matrix, custom_fields, sku_map):
    from nbrecon.models import JiraIssue

    device = make_device(custom_fields={"ipt_url": "https://jira/browse/IPT-999"})
    pair = make_pair(device)
    pair.jira = JiraIssue(key="IPT-1", url="https://jira/browse/IPT-1")
    result = engine(matrix, custom_fields, sku_map).run([pair])

    assert writes_for(result, "ipt_link") == []
    assert any(p.action is Action.FLAG for p in props_for(result, "ipt_link"))


# --- bookkeeping ----------------------------------------------------------
def test_last_reconciled_is_not_offered_for_approval(matrix, custom_fields, sku_map):
    result = engine(matrix, custom_fields, sku_map).run([make_pair()])
    assert props_for(result, "last_reconciled") == []
