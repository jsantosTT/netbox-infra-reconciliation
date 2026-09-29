"""IPT ticket linking and, behind a flag, creation.

Creation order from the ownership matrix, which is also the duplicate guard:

1. JQL on the SN custom field.
2. Fallback text search on hostname or serial.
3. Only if *both* return nothing, and only with ``--create-ipt``, create one.

Backfills are fill-if-empty. A field already holding a different value is
flagged, never overwritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..collect.jira import JiraClient
from ..models import DevicePair, JiraIssue

LOG = logging.getLogger("nbrecon.jira.apply")


@dataclass
class JiraOutcome:
    device_id: int
    device_name: str | None
    serial: str | None
    issue_key: str | None = None
    issue_url: str | None = None
    created: bool = False
    fields_written: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class JiraResult:
    outcomes: list[JiraOutcome] = field(default_factory=list)

    @property
    def created_count(self) -> int:
        return sum(1 for o in self.outcomes if o.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created_count,
            "devices": [
                {
                    "device_id": o.device_id,
                    "device_name": o.device_name,
                    "serial": o.serial,
                    "issue_key": o.issue_key,
                    "issue_url": o.issue_url,
                    "created": o.created,
                    "fields_written": o.fields_written,
                    "flags": o.flags,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


def lookup(client: JiraClient, serial: str | None, hostname: str | None) -> tuple[
    JiraIssue | None, list[JiraIssue]
]:
    """Find the IPT ticket for a device.

    Returns ``(confirmed, candidates)``. A single hit on the SN field is
    treated as confirmed. Anything found only by text search is a candidate
    that a human confirms; the tool never promotes it on its own.
    """
    if serial:
        exact = client.find_by_serial(serial)
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact

    fallback = client.find_by_text(serial, hostname)
    if fallback:
        return None, fallback
    return None, []


class JiraApplier:
    def __init__(self, client: JiraClient, create_enabled: bool = False) -> None:
        self.client = client
        self.create_enabled = create_enabled

    def process(self, pair: DevicePair, netbox_url: str) -> JiraOutcome:
        device = pair.netbox
        serial = (pair.host.serial if pair.host else None) or device.serial
        outcome = JiraOutcome(
            device_id=device.id,
            device_name=device.name,
            serial=serial,
        )

        issue = pair.jira
        candidates = pair.jira_candidates

        if issue is None and candidates:
            outcome.flags.append(
                "possible IPT match not confirmed: "
                + ", ".join(c.key for c in candidates)
                + " - a human confirms; no ticket created"
            )
            return outcome

        if issue is None:
            if not self.create_enabled:
                outcome.flags.append(
                    "no IPT ticket found; creation is disabled (pass --create-ipt once the "
                    "service account has permission)"
                )
                return outcome
            if not serial:
                outcome.flags.append("no serial; refusing to create an IPT ticket")
                return outcome
            try:
                issue = self.client.create_issue(
                    summary=f"{device.name or 'device'} ({serial})",
                    description=(
                        "Created by nbrecon reconciliation.\n"
                        f"Serial: {serial}\nNetBox: {netbox_url}"
                    ),
                    serial=serial,
                    netbox_url=netbox_url,
                )
            except Exception as exc:  # noqa: BLE001 - reported per device
                outcome.error = f"IPT creation failed: {exc}"
                return outcome
            outcome.created = True
            outcome.fields_written = ["summary", "description"]

        outcome.issue_key = issue.key
        outcome.issue_url = issue.url

        if not outcome.created:
            try:
                outcome.fields_written += self._backfill(issue, serial, netbox_url, outcome)
            except Exception as exc:  # noqa: BLE001 - reported per device
                outcome.error = f"IPT backfill failed: {exc}"
        return outcome

    def _backfill(
        self, issue: JiraIssue, serial: str | None, netbox_url: str, outcome: JiraOutcome
    ) -> list[str]:
        """Fill only empty SN / NetBox URL fields; flag differing values."""
        serial_to_write = None
        url_to_write = None

        if serial:
            if not issue.serial_field:
                serial_to_write = serial
            elif issue.serial_field.strip().upper() != serial.strip().upper():
                outcome.flags.append(
                    f"IPT {issue.key} SN field is {issue.serial_field!r}, device serial is "
                    f"{serial!r} - flagged, not overwritten"
                )

        if not issue.netbox_url_field:
            url_to_write = netbox_url
        elif issue.netbox_url_field.strip() != netbox_url.strip():
            outcome.flags.append(
                f"IPT {issue.key} NetBox URL field points elsewhere "
                f"({issue.netbox_url_field!r}) - flagged, not overwritten"
            )

        if not serial_to_write and not url_to_write:
            return []
        return self.client.set_issue_fields(issue.key, serial_to_write, url_to_write)
