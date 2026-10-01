"""The scope-only NetBox read.

The summary exists to answer "which of these devices could the tool actually
reach", so the probeable/unprobeable split is what these mostly pin.
"""

from __future__ import annotations

import csv
import io

import pytest

from nbrecon.config import Scope
from nbrecon.errors import ScopeError
from nbrecon.snapshot import custom_field_names, summarise, to_csv, to_payload, to_rows

from .conftest import make_device


def test_a_device_with_neither_address_is_not_probeable():
    devices = [
        make_device(id=1, name="has-oob", oob_ip="10.0.0.1/24", primary_ip4=None),
        make_device(id=2, name="has-primary", oob_ip=None, primary_ip4="10.0.1.2/24"),
        make_device(id=3, name="has-neither", oob_ip=None, primary_ip4=None),
    ]
    summary = summarise(devices)

    assert summary.probeable == 2
    assert summary.unprobeable == ["has-neither"]
    assert summary.with_oob_ip == 1
    assert summary.with_primary_ip4 == 1


def test_oob_wins_over_primary_for_the_probe_address():
    device = make_device(oob_ip="10.0.0.1/24", primary_ip4="10.0.1.2/24")
    assert to_rows([device])[0]["probe_address"] == "10.0.0.1"


def test_an_unnamed_device_is_identified_by_id():
    summary = summarise([make_device(id=42, name=None, oob_ip=None, primary_ip4=None)])
    assert summary.unprobeable == ["id-42"]


def test_duplicate_serials_are_surfaced_case_insensitively():
    devices = [
        make_device(id=1, name="a", serial="ABC123"),
        make_device(id=2, name="b", serial="abc123"),
        make_device(id=3, name="c", serial="OTHER"),
    ]
    summary = summarise(devices)

    assert summary.duplicate_serials == ["abc123"]
    assert summary.with_serial == 3


def test_missing_serials_are_counted():
    devices = [make_device(id=1, serial=None), make_device(id=2, serial="S1")]
    summary = summarise(devices)

    assert summary.without_serial == 1
    assert summary.duplicate_serials == []


def test_tallies_label_absent_values_rather_than_dropping_them():
    devices = [
        make_device(id=1, status="active", site_slug="aus"),
        make_device(id=2, status=None, site_slug=None),
    ]
    summary = summarise(devices)

    assert summary.by_status == {"(none)": 1, "active": 1}
    assert summary.by_site == {"(none)": 1, "aus": 1}


def test_tallies_are_ordered_by_count():
    devices = [make_device(id=i, site_slug="aus") for i in range(3)]
    devices += [make_device(id=9, site_slug="tor")]

    assert list(summarise(devices).by_site) == ["aus", "tor"]


# --- CSV -------------------------------------------------------------------


def test_csv_has_a_stable_column_for_every_custom_field_seen():
    devices = [
        make_device(id=1, custom_fields={"owner": "infra"}),
        make_device(id=2, custom_fields={"flash_version": "1.2.0"}),
    ]
    assert custom_field_names(devices) == ["flash_version", "owner"]

    rows = list(csv.DictReader(io.StringIO(to_csv(devices))))
    assert rows[0]["cf_owner"] == "infra"
    assert rows[0]["cf_flash_version"] == "", "a field this device lacks is blank, not absent"
    assert rows[1]["cf_flash_version"] == "1.2.0"


def test_csv_renders_none_as_empty_not_the_word_none():
    devices = [make_device(custom_fields={"owner": None}, serial=None, asset_tag=None)]
    row = list(csv.DictReader(io.StringIO(to_csv(devices))))[0]

    assert row["cf_owner"] == ""
    assert row["serial"] == ""
    assert row["asset_tag"] == ""


def test_csv_joins_tags():
    row = list(csv.DictReader(io.StringIO(to_csv([make_device(tags=["pilot", "galaxy"])]))))[0]
    assert row["tags"] == "pilot galaxy"


def test_payload_carries_the_scope_and_the_summary():
    payload = to_payload({"site": "aus"}, "https://netbox.example", [make_device()], "T")

    assert payload["scope"] == {"site": "aus"}
    assert payload["taken_at"] == "T"
    assert payload["summary"]["total"] == 1
    assert payload["devices"][0]["name"] == "gx-lab-01"


def test_an_empty_scope_produces_an_empty_but_valid_payload():
    payload = to_payload({}, "https://netbox.example", [], "T")

    assert payload["devices"] == []
    assert payload["summary"]["total"] == 0
    assert to_csv([]).strip().startswith("id,name,serial")


# --- the selector / batch-cap split ----------------------------------------


def test_a_read_needs_a_selector_but_not_a_batch_cap():
    scope = Scope(site="aus", max_devices=500)

    scope.require_selector()  # a read of 500 devices writes nothing

    with pytest.raises(ScopeError, match="exceeds the configured batch cap"):
        scope.validate(max_batch=20)


def test_an_unscoped_read_is_still_refused():
    with pytest.raises(ScopeError, match="refusing to run without a scope"):
        Scope().require_selector()


def test_rack_alone_satisfies_the_selector_check():
    Scope(rack="RACK-42").require_selector()
