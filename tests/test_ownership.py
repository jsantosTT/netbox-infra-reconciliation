"""The shipped matrix must match the agreed field ownership decisions."""

from __future__ import annotations

import pytest
import yaml

from nbrecon.errors import ConfigError
from nbrecon.models import Category, Policy
from nbrecon.ownership import OwnershipMatrix


def test_shipped_matrix_loads(matrix):
    assert matrix.rules
    assert matrix.unknown_never_clears is True
    assert matrix.unreachable_runs_threshold == 3


@pytest.mark.parametrize(
    "key,category",
    [
        ("hostname", Category.REPORT_ONLY),
        ("serial", Category.HOST_OWNED),
        ("device_type", Category.HOST_OWNED),
        ("asset_tag", Category.HOST_OWNED),
        ("bmc_ip", Category.HOST_OWNED),
        ("chassis_revision", Category.HOST_OWNED),
        ("topology", Category.HOST_OWNED),
        ("ipt_link", Category.TOOL_OWNED),
        ("last_reconciled", Category.TOOL_OWNED),
        ("status", Category.HUMAN_ONLY),
        ("owner", Category.HUMAN_ONLY),
        ("site", Category.HUMAN_ONLY),
        ("rack", Category.HUMAN_ONLY),
        ("power_state", Category.REPORT_ONLY),
    ],
)
def test_categories_match_agreed_matrix(matrix, key, category):
    rule = matrix.by_key(key)
    assert rule is not None, f"{key} missing from the matrix"
    assert rule.category is category


def test_human_only_and_report_only_are_never_writable(matrix):
    for rule in matrix.rules:
        if rule.category in (Category.HUMAN_ONLY, Category.REPORT_ONLY):
            assert rule.policy is Policy.NEVER
            assert not rule.writable


def test_serial_and_asset_tag_are_fill_if_empty(matrix):
    assert matrix.by_key("serial").policy is Policy.FILL_IF_EMPTY
    assert matrix.by_key("asset_tag").policy is Policy.FILL_IF_EMPTY
    assert matrix.by_key("ipt_link").policy is Policy.FILL_IF_EMPTY


def test_serial_requires_explicit_approval(matrix):
    assert matrix.by_key("serial").requires_explicit_approval is True


def test_trays_are_disabled_for_v1(matrix):
    rule = matrix.by_key("tray_serials")
    assert rule.enabled is False
    assert not rule.writable


def test_matrix_rejects_writable_human_only_field(tmp_path):
    bad = {
        "version": 1,
        "fields": [
            {
                "key": "site",
                "label": "Site",
                "category": "human_only",
                "policy": "write",
                "target": {"kind": "attribute", "name": "site"},
            }
        ],
    }
    (tmp_path / "ownership.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ConfigError, match="must be 'never'"):
        OwnershipMatrix.load(tmp_path)


def test_matrix_rejects_disabling_unknown_never_clears(tmp_path):
    bad = {
        "version": 1,
        "fields": [
            {
                "key": "serial",
                "label": "Serial",
                "category": "host_owned",
                "policy": "fill_if_empty",
                "target": {"kind": "attribute", "name": "serial"},
            }
        ],
        "rules": {"unknown_never_clears": False},
    }
    (tmp_path / "ownership.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ConfigError, match="unknown_never_clears"):
        OwnershipMatrix.load(tmp_path)


def test_matrix_rejects_write_policy_without_target(tmp_path):
    bad = {
        "version": 1,
        "fields": [
            {
                "key": "topology",
                "label": "Topology",
                "category": "host_owned",
                "policy": "write",
                "target": {"kind": "none", "name": None},
            }
        ],
    }
    (tmp_path / "ownership.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ConfigError, match="no target"):
        OwnershipMatrix.load(tmp_path)
