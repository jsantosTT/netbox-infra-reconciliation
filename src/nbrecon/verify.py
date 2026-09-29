"""Post-apply verification.

Re-reads NetBox, and Jira where relevant, and confirms the values that were
written are actually present. Read-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .collect.jira import JiraClient
from .collect.netbox import NetBoxClient
from .config import CustomFieldMap
from .diff import _equivalent
from .ownership import OwnershipMatrix

LOG = logging.getLogger("nbrecon.verify")


@dataclass
class VerifyEntry:
    device_id: int
    field_key: str
    expected: Any
    observed: Any
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    run_id: str
    entries: list[VerifyEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(e.ok for e in self.entries)

    @property
    def failures(self) -> list[VerifyEntry]:
        return [e for e in self.entries if not e.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "checked": len(self.entries),
            "failures": len(self.failures),
            "entries": [
                {
                    "device_id": e.device_id,
                    "field": e.field_key,
                    "expected": e.expected,
                    "observed": e.observed,
                    "ok": e.ok,
                    "detail": e.detail,
                }
                for e in self.entries
            ],
        }


def verify_apply(
    client: NetBoxClient,
    matrix: OwnershipMatrix,
    custom_fields: CustomFieldMap,
    apply_payload: dict[str, Any],
    jira_payload: dict[str, Any] | None = None,
    jira_client: JiraClient | None = None,
) -> VerifyResult:
    """Confirm each applied field now holds the value that was written."""
    result = VerifyResult(run_id=str(apply_payload.get("run_id", "")))

    for device in apply_payload.get("devices", []):
        applied = device.get("applied") or []
        if not applied:
            continue
        device_id = int(device["device_id"])
        try:
            fresh = client.fetch_device(device_id)
        except Exception as exc:  # noqa: BLE001 - recorded as a failure, run continues
            for item in applied:
                result.entries.append(
                    VerifyEntry(device_id, item["field"], item["new"], None, False, str(exc))
                )
            continue

        for item in applied:
            field_key = item["field"]
            if field_key.startswith("_"):
                continue
            expected = item["new"]
            rule = matrix.by_key(field_key)
            if rule is None:
                continue

            observed: Any = None
            detail = ""
            if rule.target_kind == "attribute":
                name = rule.target_name or field_key
                observed = (
                    fresh.device_type_slug if name == "device_type" else getattr(fresh, name, None)
                )
            elif rule.target_kind == "custom_field":
                real = custom_fields.resolve(rule.target_name or field_key)
                observed = fresh.custom_fields.get(real) if real else None
            elif rule.target_kind == "oob_ip":
                observed = fresh.oob_ip
            elif rule.target_kind == "interface_mac":
                iface = client.find_interface(device_id, rule.target_name or "bmc")
                observed = iface.get("mac_address") if iface else None
                detail = "" if iface else "BMC interface not found"

            ok = _equivalent(observed, expected, rule)
            result.entries.append(
                VerifyEntry(device_id, field_key, expected, observed, ok, detail)
            )

    if jira_payload and jira_client:
        for entry in jira_payload.get("devices", []):
            key = entry.get("issue_key")
            if not key:
                continue
            try:
                issue = jira_client.get_issue(key)
            except Exception as exc:  # noqa: BLE001 - recorded as a failure
                result.entries.append(
                    VerifyEntry(int(entry["device_id"]), "ipt_ticket", key, None, False, str(exc))
                )
                continue
            result.entries.append(
                VerifyEntry(
                    device_id=int(entry["device_id"]),
                    field_key="ipt_ticket",
                    expected=key,
                    observed=issue.key if issue else None,
                    ok=bool(issue),
                    detail="" if issue else "issue not readable after write",
                )
            )

    return result
