"""Scope guards, config mapping behaviour, and the run-history store."""

from __future__ import annotations

import pytest
import yaml

from nbrecon.config import CustomFieldMap, Scope, SkuMap
from nbrecon.errors import ScopeError
from nbrecon.models import CollectionStatus
from nbrecon.runstore import RunStore
from nbrecon.state import dump_collection, load_collection

from .conftest import CONFIG_DIR, make_device


# --- scope ----------------------------------------------------------------
def test_unscoped_run_is_refused():
    with pytest.raises(ScopeError, match="without a scope"):
        Scope().validate(max_batch=20)


def test_scope_cannot_exceed_the_batch_cap():
    with pytest.raises(ScopeError, match="exceeds the configured batch cap"):
        Scope(site="lab", max_devices=50).validate(max_batch=20)


def test_minimal_scope_is_accepted():
    Scope(site="lab", max_devices=10).validate(max_batch=20)


def test_host_list_is_merged_and_deduplicated(tmp_path):
    listing = tmp_path / "hosts.txt"
    listing.write_text("gx-1\n# a comment\ngx-2\ngx-1\n\n")
    scope = Scope(devices=["gx-3"], host_list=str(listing))

    assert scope.resolved_devices() == ["gx-3", "gx-1", "gx-2"]


def test_missing_host_list_is_an_error():
    with pytest.raises(ScopeError, match="host_list file not found"):
        Scope(host_list="/nonexistent/hosts.txt").resolved_devices()


def test_example_scope_file_parses():
    scope = Scope.from_file(CONFIG_DIR / "scope.example.yaml")
    assert scope.max_devices == 20


# --- custom field mapping -------------------------------------------------
def test_shipped_mapping_is_blank_so_nothing_is_written():
    mapping = CustomFieldMap.load(CONFIG_DIR)
    assert mapping.resolve("ipt_url") is None
    assert "ipt_url" in mapping.unmapped()


def test_blank_mapping_resolves_to_none():
    mapping = CustomFieldMap(mapping={"topology": "  "})
    assert mapping.resolve("topology") is None


def test_unknown_key_resolves_to_none():
    assert CustomFieldMap(mapping={}).resolve("whatever") is None


# --- SKU map --------------------------------------------------------------
def test_shipped_sku_map_is_empty():
    assert SkuMap.load(CONFIG_DIR).is_empty


def test_part_number_wins_over_model():
    sku = SkuMap(by_part_number={"tg-00002": "by-part"}, by_model={"galaxy": "by-model"})
    assert sku.resolve("TG-00002", "Galaxy") == "by-part"
    assert sku.resolve(None, "Galaxy") == "by-model"
    assert sku.resolve("UNKNOWN", "UNKNOWN") is None


def test_sku_lookup_ignores_case_and_padding(tmp_path):
    (tmp_path / "sku_map.yaml").write_text(
        yaml.safe_dump({"by_part_number": {"  TG-00003 ": "galaxy-blackhole"}})
    )
    assert SkuMap.load(tmp_path).resolve("tg-00003", None) == "galaxy-blackhole"


# --- run history ----------------------------------------------------------
def test_unreachable_streak_counts_consecutive_runs(tmp_path):
    store = RunStore(tmp_path / "history.db")
    for i in range(3):
        store.record_observation(f"run-{i}", "gx-1", CollectionStatus.UNREACHABLE)

    assert store.consecutive_unreachable("gx-1") == 3


def test_a_successful_run_resets_the_streak(tmp_path):
    store = RunStore(tmp_path / "history.db")
    store.record_observation("run-0", "gx-1", CollectionStatus.UNREACHABLE)
    store.record_observation("run-1", "gx-1", CollectionStatus.UNREACHABLE)
    store.record_observation("run-2", "gx-1", CollectionStatus.OK)
    store.record_observation("run-3", "gx-1", CollectionStatus.UNREACHABLE)

    assert store.consecutive_unreachable("gx-1") == 1


def test_unknown_device_has_no_streak(tmp_path):
    assert RunStore(tmp_path / "history.db").consecutive_unreachable("nobody") == 0


def test_runs_are_recorded_and_listed(tmp_path):
    store = RunStore(tmp_path / "history.db")
    store.record_run("run-1", "collect", "site=lab", 5)
    store.record_run("run-1", "plan", "site=lab", 5)

    runs = store.recent_runs()
    assert len(runs) == 1
    assert runs[0]["stage"] == "plan"


# --- collection artifact --------------------------------------------------
def test_collection_carries_the_bmc_interface():
    """The interface holding the MAC has to survive into the plan stage.

    Without it the NetBox side of bmc_mac reads as empty on every run, so a
    MAC that already matches is proposed as a write over and over.
    """
    device = make_device()
    payload = dump_collection(
        "run-1", {}, "https://netbox.example", [device], {}, {}, [],
        {device.id: {"id": 31, "name": "bmc", "mac_address": "aa:bb:cc:dd:ee:ff"}},
    )

    _, _, _, _, interfaces = load_collection(payload)

    assert interfaces[device.id]["mac_address"] == "aa:bb:cc:dd:ee:ff"


def test_collection_without_interfaces_still_loads():
    """Older artifacts, and devices with no BMC interface, must not break."""
    payload = dump_collection("run-1", {}, "https://netbox.example", [], {}, {}, [])
    payload.pop("bmc_interfaces")

    assert load_collection(payload)[4] == {}
