"""The ownership engine: turn correlated pairs into proposals.

Safety properties this module is responsible for:

* A collected value of ``None`` means "not determined" and never produces a
  write. Partial collection can therefore never clear a NetBox field.
* ``fill_if_empty`` fields are written only into an empty NetBox value. A
  differing value is flagged for a human.
* Human-only and report-only fields never produce a write, whatever the rest of
  the configuration says.
* A field whose NetBox custom field name is unmapped is skipped, never guessed.
* Suggestion-matched devices mark every proposal as needing explicit approval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import CustomFieldMap, SkuMap
from .models import (
    Action,
    Category,
    DevicePair,
    HostFacts,
    NetBoxDevice,
    Policy,
    Proposal,
    UnmatchedEntry,
    UnmatchedReason,
)
from .ownership import FieldRule, OwnershipMatrix

LOG = logging.getLogger("nbrecon.diff")

# Written by the apply stage alongside other approved changes rather than
# offered for approval on its own.
BOOKKEEPING_FIELDS = {"last_reconciled"}

HOST_GETTERS: dict[str, Callable[[HostFacts], Any]] = {
    "hostname": lambda h: h.hostname,
    "serial": lambda h: h.serial,
    "asset_tag": lambda h: h.asset_tag,
    "bmc_ip": lambda h: h.bmc_ipv4,
    "bmc_mac": lambda h: h.bmc_mac,
    "power_state": lambda h: h.power_state,
    "chassis_revision": lambda h: h.chassis_revision,
    "fw_flash": lambda h: h.flash_version,
    "fw_firmware": lambda h: h.fw_version,
    "fw_kmd": lambda h: h.kmd_version,
    "fw_smi": lambda h: h.smi_version,
    "topology": lambda h: h.topology,
}


@dataclass
class DeviceContext:
    """Facts about the NetBox side that are not on the device record itself."""

    bmc_interface: dict[str, Any] | None = None
    unreachable_streak: int = 0


@dataclass
class DiffResult:
    proposals: list[Proposal] = field(default_factory=list)
    unmatched: list[UnmatchedEntry] = field(default_factory=list)
    skipped_unmapped: dict[str, str] = field(default_factory=dict)


class DiffEngine:
    def __init__(
        self,
        matrix: OwnershipMatrix,
        custom_fields: CustomFieldMap,
        sku_map: SkuMap,
        known_device_type_slugs: set[str] | None = None,
        jira_enabled: bool = True,
    ) -> None:
        self.matrix = matrix
        self.custom_fields = custom_fields
        self.sku_map = sku_map
        self.known_device_type_slugs = known_device_type_slugs
        self.jira_enabled = jira_enabled

    def run(
        self,
        pairs: list[DevicePair],
        contexts: dict[int, DeviceContext] | None = None,
    ) -> DiffResult:
        contexts = contexts or {}
        result = DiffResult()
        for pair in pairs:
            ctx = contexts.get(pair.netbox.id, DeviceContext())
            for rule in self.matrix.rules:
                if rule.key in BOOKKEEPING_FIELDS:
                    continue
                self._evaluate(pair, rule, ctx, result)
        return result

    # --- dispatch ---------------------------------------------------------
    def _evaluate(
        self, pair: DevicePair, rule: FieldRule, ctx: DeviceContext, out: DiffResult
    ) -> None:
        if rule.category is Category.REPORT_ONLY or not rule.enabled:
            self._report_only(pair, rule, out)
            return
        if rule.category is Category.HUMAN_ONLY:
            self._human_only(pair, rule, ctx, out)
            return
        self._writable(pair, rule, ctx, out)

    # --- report-only ------------------------------------------------------
    def _report_only(self, pair: DevicePair, rule: FieldRule, out: DiffResult) -> None:
        device, host = pair.netbox, pair.host

        if rule.key == "hostname":
            sources = {
                "netbox": device.name,
                "redfish": host.hostname if host else None,
                "prometheus": pair.prometheus_node,
            }
            present = {k: v for k, v in sources.items() if v}
            distinct = {_norm_text(v) for v in present.values()}
            if len(distinct) > 1:
                out.proposals.append(
                    self._make(
                        pair,
                        rule,
                        Action.FLAG,
                        current=device.name,
                        proposed=None,
                        reason=(
                            "hostname disagrees across sources: "
                            + ", ".join(f"{k}={v!r}" for k, v in sorted(present.items()))
                            + " - other systems key on this, so a human decides"
                        ),
                    )
                )
            return

        if rule.key == "tray_serials":
            trays = host.trays if host else []
            if trays:
                summary = ", ".join(
                    f"{t.tray_id}={t.serial or 'unknown'}" for t in sorted(trays, key=_tray_sort)
                )
                out.proposals.append(
                    self._make(
                        pair,
                        rule,
                        Action.REPORT,
                        current=None,
                        proposed=summary,
                        reason=(
                            "trays collected but not written: modules, device bays and "
                            "inventory items are not modelled in NetBox yet"
                        ),
                    )
                )
            return

        value = HOST_GETTERS.get(rule.key, lambda _h: None)(host) if host else None
        if value is not None:
            out.proposals.append(
                self._make(
                    pair, rule, Action.REPORT, current=None, proposed=value, reason="liveness hint"
                )
            )

    # --- human-only -------------------------------------------------------
    def _human_only(
        self, pair: DevicePair, rule: FieldRule, ctx: DeviceContext, out: DiffResult
    ) -> None:
        if rule.suggest_rule == "unreachable_runs_threshold":
            threshold = self.matrix.unreachable_runs_threshold
            if ctx.unreachable_streak >= threshold:
                out.proposals.append(
                    self._make(
                        pair,
                        rule,
                        Action.SUGGEST,
                        current=pair.netbox.status,
                        proposed=None,
                        reason=(
                            f"unreachable for {ctx.unreachable_streak} consecutive runs "
                            f"(threshold {threshold}); consider Offline. "
                            "The tool never writes status."
                        ),
                    )
                )
            return

        if rule.report_if_empty:
            current = self._netbox_value(pair.netbox, rule, ctx)
            if current in (None, ""):
                out.proposals.append(
                    self._make(
                        pair,
                        rule,
                        Action.REPORT,
                        current=None,
                        proposed=None,
                        reason="empty in NetBox; human-only field, a person must fill it",
                    )
                )

    # --- writable ---------------------------------------------------------
    def _writable(
        self, pair: DevicePair, rule: FieldRule, ctx: DeviceContext, out: DiffResult
    ) -> None:
        if rule.target_kind == "custom_field":
            real_name = self.custom_fields.resolve(rule.target_name or rule.key)
            if not real_name:
                out.skipped_unmapped[rule.key] = (
                    f"no NetBox custom field mapped for {rule.target_name or rule.key!r}"
                )
                return

        collected = self._collected_value(pair, rule, out)
        if collected is _SKIP:
            return

        current = self._netbox_value(pair.netbox, rule, ctx)

        # Unknown is not empty. A value we could not determine never clears or
        # overwrites anything, and never becomes a proposal.
        if collected is None:
            if rule.mandatory and _is_empty(current):
                out.proposals.append(
                    self._make(
                        pair,
                        rule,
                        Action.REPORT,
                        current=current,
                        proposed=None,
                        reason="mandatory field empty in NetBox and not reported by the host",
                    )
                )
            return

        if _equivalent(current, collected, rule):
            return

        if _is_empty(current):
            out.proposals.append(
                self._make(
                    pair,
                    rule,
                    Action.WRITE,
                    current=current,
                    proposed=collected,
                    reason="empty in NetBox; filling from the host",
                )
            )
            return

        # Present in NetBox and different.
        if rule.policy is Policy.FILL_IF_EMPTY:
            reason = (
                "identity mismatch: NetBox and the host disagree on the serial, "
                "so this device is not diffed further"
                if rule.on_conflict == "identity_mismatch"
                else "already set to a different value; fill-if-empty fields are never overwritten"
            )
            out.proposals.append(
                self._make(pair, rule, Action.FLAG, current=current, proposed=collected,
                           reason=reason)
            )
            if rule.on_conflict == "identity_mismatch":
                out.unmatched.append(
                    UnmatchedEntry(
                        reason=UnmatchedReason.IN_NETBOX_NOT_ON_HOST,
                        identifier=pair.netbox.name or f"device-{pair.netbox.id}",
                        detail=f"NetBox serial {current!r} vs host serial {collected!r}",
                        device_id=pair.netbox.id,
                        netbox_url=pair.netbox.url,
                    )
                )
            return

        out.proposals.append(
            self._make(
                pair,
                rule,
                Action.WRITE,
                current=current,
                proposed=collected,
                reason="host is authoritative for this field",
            )
        )

    # --- value resolution -------------------------------------------------
    def _collected_value(self, pair: DevicePair, rule: FieldRule, out: DiffResult) -> Any:
        if rule.key == "device_type":
            return self._device_type_value(pair, rule, out)
        if rule.key == "ipt_link":
            return self._ipt_value(pair, rule, out)
        host = pair.host
        if host is None:
            return None
        return HOST_GETTERS.get(rule.key, lambda _h: None)(host)

    def _device_type_value(self, pair: DevicePair, rule: FieldRule, out: DiffResult) -> Any:
        host = pair.host
        if host is None:
            return None
        if host.part_number is None and host.model is None:
            return None

        slug = self.sku_map.resolve(host.part_number, host.model)
        if not slug:
            out.proposals.append(
                self._make(
                    pair,
                    rule,
                    Action.REPORT,
                    current=pair.netbox.device_type_slug,
                    proposed=None,
                    reason=(
                        f"unmapped SKU: model={host.model!r} part_number={host.part_number!r}; "
                        "add it to config/sku_map.yaml. The tool never creates device types."
                    ),
                )
            )
            out.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.UNMAPPED_SKU,
                    identifier=str(host.part_number or host.model),
                    detail=f"device {pair.netbox.name or pair.netbox.id}",
                    device_id=pair.netbox.id,
                    netbox_url=pair.netbox.url,
                )
            )
            return _SKIP

        if self.known_device_type_slugs is not None and slug not in self.known_device_type_slugs:
            out.proposals.append(
                self._make(
                    pair,
                    rule,
                    Action.REPORT,
                    current=pair.netbox.device_type_slug,
                    proposed=slug,
                    reason=(
                        f"device type {slug!r} does not exist in NetBox; an admin must create it. "
                        "The tool never creates device types or manufacturers."
                    ),
                )
            )
            return _SKIP
        return slug

    def _ipt_value(self, pair: DevicePair, rule: FieldRule, out: DiffResult) -> Any:
        if not self.jira_enabled:
            # Jira was never queried, so absence of a ticket proves nothing.
            return _SKIP
        if pair.jira:
            return pair.jira.url
        if pair.jira_candidates:
            candidates = ", ".join(f"{c.key}" for c in pair.jira_candidates)
            out.proposals.append(
                self._make(
                    pair,
                    rule,
                    Action.FLAG,
                    current=self._netbox_value(pair.netbox, rule, DeviceContext()),
                    proposed=None,
                    reason=(
                        f"possible IPT match found ({candidates}) but not confirmed; "
                        "a human confirms, nothing is created automatically"
                    ),
                )
            )
            out.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.POSSIBLE_IPT_MATCH,
                    identifier=pair.netbox.name or f"device-{pair.netbox.id}",
                    detail=candidates,
                    device_id=pair.netbox.id,
                    netbox_url=pair.netbox.url,
                )
            )
            return _SKIP
        out.unmatched.append(
            UnmatchedEntry(
                reason=UnmatchedReason.MISSING_IPT,
                identifier=pair.netbox.name or f"device-{pair.netbox.id}",
                detail="no IPT ticket found by serial or text search",
                device_id=pair.netbox.id,
                netbox_url=pair.netbox.url,
            )
        )
        return _SKIP

    def _netbox_value(self, device: NetBoxDevice, rule: FieldRule, ctx: DeviceContext) -> Any:
        kind = rule.target_kind
        if kind == "attribute":
            name = rule.target_name or rule.key
            if name == "device_type":
                return device.device_type_slug
            if name == "site":
                return device.site_slug
            return getattr(device, name, None)
        if kind == "custom_field":
            real = self.custom_fields.resolve(rule.target_name or rule.key)
            return device.custom_fields.get(real) if real else None
        if kind == "oob_ip":
            return device.oob_ip
        if kind == "interface_mac":
            iface = ctx.bmc_interface
            return iface.get("mac_address") if iface else None
        return None

    def _make(
        self,
        pair: DevicePair,
        rule: FieldRule,
        action: Action,
        current: Any,
        proposed: Any,
        reason: str,
    ) -> Proposal:
        explicit = action is Action.WRITE and (
            rule.requires_explicit_approval or pair.requires_explicit_approval
        )
        if pair.requires_explicit_approval and action is Action.WRITE:
            reason = f"{reason} (matched by suggestion: {pair.match_evidence})"
        return Proposal(
            device_id=pair.netbox.id,
            device_name=pair.netbox.name,
            field_key=rule.key,
            field_label=rule.label,
            category=rule.category,
            action=action,
            current=current,
            proposed=proposed,
            reason=reason,
            target_kind=rule.target_kind,
            target_name=rule.target_name,
            requires_explicit_approval=explicit,
        )


class _Skip:
    """Sentinel: this field produced its own report entry and needs no diff."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<SKIP>"


_SKIP = _Skip()


def _norm_text(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _normalise_mac(value: Any) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def _normalise_ip(value: Any) -> str:
    return str(value).split("/", 1)[0].strip().lower()


def _equivalent(current: Any, collected: Any, rule: FieldRule) -> bool:
    """Compare in the shape appropriate to the field, not byte for byte."""
    if current is None or collected is None:
        return False
    if rule.target_kind == "interface_mac":
        return _normalise_mac(current) == _normalise_mac(collected)
    if rule.target_kind == "oob_ip":
        return _normalise_ip(current) == _normalise_ip(collected)
    if rule.key == "serial":
        # Serials are compared case-insensitively for matching, but a
        # difference in case alone is not worth a write.
        return str(current).strip().upper() == str(collected).strip().upper()
    return _norm_text(current) == _norm_text(collected)


def _tray_sort(tray: Any) -> tuple[int, str]:
    raw = str(getattr(tray, "tray_id", ""))
    return (0, raw.zfill(4)) if raw.isdigit() else (1, raw)
