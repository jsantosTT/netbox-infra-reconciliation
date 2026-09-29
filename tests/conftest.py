from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nbrecon.config import CustomFieldMap, SkuMap
from nbrecon.models import (
    CollectionStatus,
    DevicePair,
    HostFacts,
    MatchMethod,
    NetBoxDevice,
)
from nbrecon.ownership import OwnershipMatrix

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def matrix() -> OwnershipMatrix:
    """The real shipped matrix, so the tests exercise production rules."""
    return OwnershipMatrix.load(CONFIG_DIR)


@pytest.fixture
def custom_fields() -> CustomFieldMap:
    """A fully mapped field set, as a site would have after preflight."""
    return CustomFieldMap(
        mapping={
            "ipt_url": "ipt_url",
            "last_reconciled": "last_reconciled",
            "chassis_revision": "chassis_revision",
            "flash_version": "flash_version",
            "fw_version": "fw_version",
            "kmd_version": "kmd_version",
            "smi_version": "smi_version",
            "topology": "topology",
            "owner": "owner",
            "assignment": "assignment",
            "support": "support",
            "server_function": "server_function",
            "exabox": "exabox",
        }
    )


@pytest.fixture
def empty_custom_fields() -> CustomFieldMap:
    """The shipped default: nothing mapped, so nothing is written."""
    return CustomFieldMap.load(CONFIG_DIR)


@pytest.fixture
def sku_map() -> SkuMap:
    return SkuMap(by_part_number={"tg-00002": "galaxy-wormhole"}, by_model={})


@pytest.fixture
def empty_sku_map() -> SkuMap:
    return SkuMap()


def make_device(**overrides: Any) -> NetBoxDevice:
    defaults: dict[str, Any] = {
        "id": 1,
        "name": "gx-lab-01",
        "serial": "SN-AAA-111",
        "asset_tag": None,
        "device_type_slug": "galaxy-wormhole",
        "device_type_id": 7,
        "site_slug": "lab",
        "status": "active",
        "primary_ip4": None,
        "oob_ip": "10.100.1.50/24",
        "url": "https://netbox.example/dcim/devices/1/",
        "last_updated": "2026-09-01T00:00:00Z",
        "custom_fields": {},
        "tags": [],
    }
    defaults.update(overrides)
    return NetBoxDevice(**defaults)


def make_host(**overrides: Any) -> HostFacts:
    defaults: dict[str, Any] = {
        "bmc_ip": "10.100.1.50",
        "hostname": "gx-lab-01",
        "serial": "SN-AAA-111",
        "model": "Galaxy Wormhole",
        "part_number": "TG-00002",
        "asset_tag": "ASSET-1",
        "bmc_ipv4": "10.100.1.50",
        "bmc_mac": "aa:bb:cc:dd:ee:ff",
        "power_state": "On",
        "chassis_revision": "RevB",
        "flash_version": "1.2.3",
        "fw_version": "4.5.6",
        "kmd_version": "7.8.9",
        "smi_version": "2.0.0",
        "topology": "mesh",
        "trays": [],
        "status": CollectionStatus.OK,
        "sources": ["redfish"],
        "errors": [],
    }
    defaults.update(overrides)
    return HostFacts(**defaults)


def make_pair(
    device: NetBoxDevice | None = None,
    host: HostFacts | None = None,
    method: MatchMethod = MatchMethod.SERIAL,
) -> DevicePair:
    return DevicePair(
        netbox=device or make_device(),
        host=host if host is not None else make_host(),
        match_method=method,
        match_evidence="test",
    )
