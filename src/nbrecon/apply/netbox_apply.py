"""Apply approved changes to NetBox.

Guard rails, in the order they are enforced:

1. The approval digest must match the plan being applied.
2. Only fields the approver said yes to are considered.
3. Only fields the ownership matrix marks writable are considered, re-checked
   here rather than trusted from the plan.
4. The batch cap limits how many devices a single run may touch.
5. Each device is re-read and skipped if NetBox changed since the snapshot.
6. The PATCH body contains only the approved fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..audit import journal_comment
from ..collect.netbox import NetBoxClient
from ..config import CustomFieldMap
from ..errors import ApprovalError, SafetyViolation
from ..models import ApprovedPlan, Plan, Proposal, utcnow
from ..ownership import OwnershipMatrix
from ..runstore import RunStore

LOG = logging.getLogger("nbrecon.apply")


@dataclass
class DeviceOutcome:
    device_id: int
    device_name: str | None
    applied: list[tuple[str, Any, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None
    stale: bool = False


@dataclass
class ApplyResult:
    run_id: str
    outcomes: list[DeviceOutcome] = field(default_factory=list)

    @property
    def applied_count(self) -> int:
        return sum(len(o.applied) for o in self.outcomes)

    @property
    def stale_devices(self) -> list[DeviceOutcome]:
        return [o for o in self.outcomes if o.stale]

    @property
    def failed_devices(self) -> list[DeviceOutcome]:
        return [o for o in self.outcomes if o.error]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "applied_count": self.applied_count,
            "devices": [
                {
                    "device_id": o.device_id,
                    "device_name": o.device_name,
                    "applied": [
                        {"field": f, "old": old, "new": new} for f, old, new in o.applied
                    ],
                    "skipped": [{"field": f, "reason": r} for f, r in o.skipped],
                    "stale": o.stale,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


class NetBoxApplier:
    def __init__(
        self,
        client: NetBoxClient,
        matrix: OwnershipMatrix,
        custom_fields: CustomFieldMap,
        store: RunStore | None = None,
    ) -> None:
        self.client = client
        self.matrix = matrix
        self.custom_fields = custom_fields
        self.store = store

    def apply(
        self,
        plan: Plan,
        approval: ApprovedPlan,
        expected_digest: str,
        max_batch: int,
        journal: bool = True,
    ) -> ApplyResult:
        if approval.plan_digest != expected_digest:
            raise ApprovalError(
                "approval does not match this plan: the plan was regenerated or edited "
                "after it was approved. Re-run 'nbrecon approve'."
            )
        if approval.run_id != plan.run_id:
            raise ApprovalError(
                f"approval is for run {approval.run_id}, plan is run {plan.run_id}"
            )

        approved_keys = approval.approved_keys()
        writes = [p for p in plan.writes if (p.device_id, p.field_key) in approved_keys]

        by_device: dict[int, list[Proposal]] = {}
        for p in writes:
            by_device.setdefault(p.device_id, []).append(p)

        if len(by_device) > max_batch:
            raise ApprovalError(
                f"approved plan touches {len(by_device)} devices, above the batch cap "
                f"of {max_batch}. Split the run or raise NBRECON_MAX_BATCH deliberately."
            )

        result = ApplyResult(run_id=plan.run_id)
        for device_id, proposals in by_device.items():
            result.outcomes.append(self._apply_device(plan, device_id, proposals, journal))
        return result

    # --- per device -------------------------------------------------------
    def _apply_device(
        self, plan: Plan, device_id: int, proposals: list[Proposal], journal: bool
    ) -> DeviceOutcome:
        name = proposals[0].device_name if proposals else None
        outcome = DeviceOutcome(device_id=device_id, device_name=name)

        try:
            current = self.client.fetch_device(device_id)
        except Exception as exc:  # noqa: BLE001 - reported per device, run continues
            outcome.error = f"could not re-read device: {exc}"
            return outcome

        if self.matrix.staleness_check:
            snapshot_ts = plan.snapshot_last_updated.get(str(device_id))
            if snapshot_ts and current.last_updated != snapshot_ts:
                outcome.stale = True
                outcome.skipped = [
                    (p.field_key, "NetBox changed after the snapshot; re-diff on the next run")
                    for p in proposals
                ]
                LOG.warning("device %s edited since snapshot; skipping", device_id)
                return outcome

        payload: dict[str, Any] = {}
        custom_payload: dict[str, Any] = {}
        interface_writes: list[tuple[Proposal, int]] = []

        for proposal in proposals:
            rule = self.matrix.by_key(proposal.field_key)
            if rule is None or not rule.writable:
                # The plan is not trusted as the authority on writability.
                raise SafetyViolation(
                    f"refusing to write {proposal.field_key!r}: not writable in the "
                    "ownership matrix"
                )
            try:
                self._stage(proposal, rule.target_kind, rule.target_name, current,
                            payload, custom_payload, interface_writes, outcome)
            except Exception as exc:  # noqa: BLE001 - one field must not sink the device
                outcome.skipped.append((proposal.field_key, str(exc)))

        if custom_payload:
            payload["custom_fields"] = custom_payload

        if payload:
            stamped = self._stamp_last_reconciled(plan.run_id, payload)
            try:
                self.client.patch_device(device_id, stamped)
            except Exception as exc:  # noqa: BLE001 - reported per device
                outcome.error = str(exc)
                return outcome

        for proposal, interface_id in interface_writes:
            try:
                self.client.patch_interface(interface_id, {"mac_address": proposal.proposed})
                outcome.applied.append(
                    (proposal.field_key, proposal.current, proposal.proposed)
                )
            except Exception as exc:  # noqa: BLE001 - reported per field
                outcome.skipped.append((proposal.field_key, str(exc)))

        if self.store:
            for field_key, old, new in outcome.applied:
                self.store.record_applied_change(
                    plan.run_id, device_id, field_key, old, new, "applied"
                )
            for field_key, reason in outcome.skipped:
                self.store.record_applied_change(
                    plan.run_id, device_id, field_key, None, None, f"skipped: {reason}"
                )

        if journal and outcome.applied:
            try:
                self.client.create_journal_entry(
                    device_id, journal_comment(plan.run_id, outcome.applied)
                )
            except Exception as exc:  # noqa: BLE001 - audit gap is reported, not fatal
                LOG.warning("journal entry failed for device %s: %s", device_id, exc)
                outcome.skipped.append(("_journal", str(exc)))

        return outcome

    # --- field staging ----------------------------------------------------
    def _stage(
        self,
        proposal: Proposal,
        target_kind: str,
        target_name: str | None,
        current: Any,
        payload: dict[str, Any],
        custom_payload: dict[str, Any],
        interface_writes: list[tuple[Proposal, int]],
        outcome: DeviceOutcome,
    ) -> None:
        if proposal.proposed is None:
            raise SafetyViolation(
                f"refusing to write None to {proposal.field_key!r}; unknown never clears"
            )

        if target_kind == "attribute":
            name = target_name or proposal.field_key
            if name == "device_type":
                device_type = self.client.find_device_type(str(proposal.proposed))
                if not device_type:
                    raise ValueError(
                        f"device type {proposal.proposed!r} does not exist in NetBox; "
                        "an admin must create it"
                    )
                payload["device_type"] = device_type["id"]
            else:
                payload[name] = proposal.proposed
            outcome.applied.append((proposal.field_key, proposal.current, proposal.proposed))
            return

        if target_kind == "custom_field":
            real = self.custom_fields.resolve(target_name or proposal.field_key)
            if not real:
                raise ValueError("no NetBox custom field mapped; nothing written")
            custom_payload[real] = proposal.proposed
            outcome.applied.append((proposal.field_key, proposal.current, proposal.proposed))
            return

        if target_kind == "oob_ip":
            address = str(proposal.proposed)
            existing = self.client.find_ip_address(address) or self.client.find_ip_address(
                f"{address}/32"
            )
            if not existing:
                raise ValueError(
                    f"no IPAM entry for {address}; create the IP address in NetBox first "
                    "(nbrecon does not create IPAM objects)"
                )
            payload["oob_ip"] = existing["id"]
            outcome.applied.append((proposal.field_key, proposal.current, address))
            return

        if target_kind == "interface_mac":
            iface_name = target_name or "bmc"
            iface = self.client.find_interface(current.id, iface_name)
            if not iface:
                raise ValueError(
                    f"interface {iface_name!r} not found on the device; create it in NetBox "
                    "first (nbrecon does not create interfaces)"
                )
            interface_writes.append((proposal, iface["id"]))
            return

        raise SafetyViolation(f"unsupported target kind {target_kind!r}")

    def _stamp_last_reconciled(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Attach the tool-owned bookkeeping stamp to a device already changing.

        Never written on its own, so a device with no approved changes is not
        touched just to update a timestamp.
        """
        rule = self.matrix.by_key("last_reconciled")
        if rule is None or not rule.writable:
            return payload
        real = self.custom_fields.resolve(rule.target_name or "last_reconciled")
        if not real:
            return payload
        custom = dict(payload.get("custom_fields") or {})
        custom[real] = f"{utcnow().isoformat()} run={run_id}"
        return {**payload, "custom_fields": custom}
