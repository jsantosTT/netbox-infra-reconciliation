"""Apply-stage guard rails."""

from __future__ import annotations

from typing import Any

import pytest

from nbrecon.apply.netbox_apply import NetBoxApplier
from nbrecon.approve import approve_from_decisions
from nbrecon.audit import digest
from nbrecon.errors import ApprovalError, SafetyViolation
from nbrecon.models import Action, Category, Plan, Proposal, utcnow

from .conftest import make_device


class FakeNetBox:
    """Records calls instead of issuing them."""

    def __init__(self, device=None, device_types=None, ip_addresses=None, interfaces=None):
        self.device = device or make_device()
        self.device_types = device_types or {"galaxy-wormhole": {"id": 7}}
        self.ip_addresses = ip_addresses or {}
        self.interfaces = interfaces or {}
        self.patches: list[tuple[int, dict[str, Any]]] = []
        self.interface_patches: list[tuple[int, dict[str, Any]]] = []
        self.journals: list[tuple[int, str]] = []

    def fetch_device(self, device_id: int):
        return self.device

    def patch_device(self, device_id: int, payload: dict[str, Any]):
        self.patches.append((device_id, payload))
        return {"id": device_id, **payload}

    def patch_interface(self, interface_id: int, payload: dict[str, Any]):
        self.interface_patches.append((interface_id, payload))
        return {"id": interface_id, **payload}

    def create_journal_entry(self, device_id: int, comment: str):
        self.journals.append((device_id, comment))
        return {"id": 1}

    def find_device_type(self, slug: str):
        return self.device_types.get(slug)

    def find_ip_address(self, address: str):
        return self.ip_addresses.get(address)

    def find_interface(self, device_id: int, name: str):
        return self.interfaces.get(name)


def build_plan(proposals: list[Proposal], device=None, last_updated="2026-09-01T00:00:00Z"):
    device = device or make_device()
    return Plan(
        run_id="20260929T000000Z-abc123",
        created_at=utcnow(),
        scope={"site": "lab"},
        netbox_url="https://netbox.example",
        snapshot_last_updated={str(device.id): last_updated},
        proposals=proposals,
    )


def proposal(field_key="topology", **kwargs) -> Proposal:
    defaults: dict[str, Any] = {
        "device_id": 1,
        "device_name": "gx-lab-01",
        "field_key": field_key,
        "field_label": field_key,
        "category": Category.HOST_OWNED,
        "action": Action.WRITE,
        "current": "old",
        "proposed": "new",
        "reason": "test",
        "target_kind": "custom_field",
        "target_name": field_key,
        "requires_explicit_approval": False,
    }
    defaults.update(kwargs)
    return Proposal(**defaults)


def approve_all(plan: Plan):
    return approve_from_decisions(
        plan, "tester", {(p.device_id, p.field_key) for p in plan.writes}
    )


# --- approval binding -----------------------------------------------------
def test_apply_refuses_when_the_plan_changed_after_approval(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_all(plan)
    client = FakeNetBox()
    applier = NetBoxApplier(client, matrix, custom_fields)

    with pytest.raises(ApprovalError, match="does not match this plan"):
        applier.apply(plan, approval, expected_digest=digest("tampered"), max_batch=20)
    assert client.patches == []


def test_apply_refuses_an_approval_from_another_run(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_all(plan)
    approval.run_id = "some-other-run"

    with pytest.raises(ApprovalError, match="approval is for run"):
        NetBoxApplier(FakeNetBox(), matrix, custom_fields).apply(
            plan, approval, expected_digest=approval.plan_digest, max_batch=20
        )


def test_only_approved_fields_are_written(matrix, custom_fields):
    plan = build_plan([proposal("topology"), proposal("chassis_revision")])
    approval = approve_from_decisions(plan, "tester", {(1, "topology")})
    client = FakeNetBox()

    NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert len(client.patches) == 1
    custom = client.patches[0][1]["custom_fields"]
    assert custom["topology"] == "new"
    assert "chassis_revision" not in custom


def test_rejecting_everything_writes_nothing(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_from_decisions(plan, "tester", set())
    client = FakeNetBox()

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches == []
    assert result.applied_count == 0


# --- batch cap ------------------------------------------------------------
def test_batch_cap_blocks_an_oversized_run(matrix, custom_fields):
    proposals = [proposal(device_id=i, device_name=f"gx-{i}") for i in range(1, 6)]
    plan = build_plan(proposals)
    approval = approve_all(plan)

    with pytest.raises(ApprovalError, match="above the batch cap"):
        NetBoxApplier(FakeNetBox(), matrix, custom_fields).apply(
            plan, approval, expected_digest=approval.plan_digest, max_batch=3
        )


# --- staleness ------------------------------------------------------------
def test_device_edited_after_the_snapshot_is_skipped(matrix, custom_fields):
    device = make_device(last_updated="2026-09-02T10:00:00Z")
    plan = build_plan([proposal()], device=device, last_updated="2026-09-01T00:00:00Z")
    approval = approve_all(plan)
    client = FakeNetBox(device=device)

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches == []
    assert result.stale_devices
    assert "re-diff" in result.stale_devices[0].skipped[0][1]


def test_unchanged_device_is_applied(matrix, custom_fields):
    device = make_device(last_updated="2026-09-01T00:00:00Z")
    plan = build_plan([proposal()], device=device, last_updated="2026-09-01T00:00:00Z")
    approval = approve_all(plan)
    client = FakeNetBox(device=device)

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert len(client.patches) == 1
    assert result.applied_count == 1


# --- writability re-check -------------------------------------------------
def test_apply_rejects_a_field_the_matrix_forbids(matrix, custom_fields):
    """A hand-edited plan cannot smuggle a human-only field past the engine."""
    plan = build_plan([proposal("site", target_kind="attribute", target_name="site")])
    approval = approve_all(plan)

    with pytest.raises(SafetyViolation, match="not writable"):
        NetBoxApplier(FakeNetBox(), matrix, custom_fields).apply(
            plan, approval, expected_digest=approval.plan_digest, max_batch=20
        )


def test_apply_refuses_a_none_value(matrix, custom_fields):
    plan = build_plan([proposal(proposed=None)])
    approval = approve_all(plan)
    client = FakeNetBox()

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches == []
    assert "unknown never clears" in result.outcomes[0].skipped[0][1]


# --- targets that must already exist --------------------------------------
def test_missing_device_type_is_skipped_never_created(matrix, custom_fields):
    plan = build_plan(
        [proposal("device_type", target_kind="attribute", target_name="device_type",
                  proposed="galaxy-blackhole")]
    )
    approval = approve_all(plan)
    client = FakeNetBox(device_types={})

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches == []
    assert "does not exist in NetBox" in result.outcomes[0].skipped[0][1]


def test_missing_ipam_entry_is_skipped_never_created(matrix, custom_fields):
    plan = build_plan(
        [proposal("bmc_ip", target_kind="oob_ip", target_name="primary_ip4_oob",
                  proposed="10.100.1.50")]
    )
    approval = approve_all(plan)
    client = FakeNetBox(ip_addresses={})

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches == []
    assert "does not create IPAM objects" in result.outcomes[0].skipped[0][1]


def test_existing_ipam_entry_is_linked(matrix, custom_fields):
    plan = build_plan(
        [proposal("bmc_ip", target_kind="oob_ip", target_name="primary_ip4_oob",
                  proposed="10.100.1.50")]
    )
    approval = approve_all(plan)
    client = FakeNetBox(ip_addresses={"10.100.1.50": {"id": 42}})

    NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.patches[0][1]["oob_ip"] == 42


def test_missing_bmc_interface_is_skipped_never_created(matrix, custom_fields):
    plan = build_plan(
        [proposal("bmc_mac", target_kind="interface_mac", target_name="bmc",
                  proposed="aa:bb:cc:dd:ee:ff")]
    )
    approval = approve_all(plan)
    client = FakeNetBox(interfaces={})

    result = NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert client.interface_patches == []
    assert "does not create interfaces" in result.outcomes[0].skipped[0][1]


# --- bookkeeping and audit ------------------------------------------------
def test_last_reconciled_is_stamped_only_alongside_a_real_change(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_all(plan)
    client = FakeNetBox()

    NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    stamp = client.patches[0][1]["custom_fields"]["last_reconciled"]
    assert plan.run_id in stamp


def test_last_reconciled_is_not_written_when_unmapped(matrix, empty_custom_fields):
    """A blank mapping disables the stamp rather than inventing a field name."""
    plan = build_plan([proposal("serial", target_kind="attribute", target_name="serial",
                                proposed="SN-NEW")])
    approval = approve_all(plan)
    client = FakeNetBox()

    NetBoxApplier(client, matrix, empty_custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert "custom_fields" not in client.patches[0][1]


def test_journal_entry_records_the_run_id(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_all(plan)
    client = FakeNetBox()

    NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20
    )

    assert len(client.journals) == 1
    assert plan.run_id in client.journals[0][1]


def test_journal_can_be_disabled(matrix, custom_fields):
    plan = build_plan([proposal()])
    approval = approve_all(plan)
    client = FakeNetBox()

    NetBoxApplier(client, matrix, custom_fields).apply(
        plan, approval, expected_digest=approval.plan_digest, max_batch=20, journal=False
    )

    assert client.journals == []
