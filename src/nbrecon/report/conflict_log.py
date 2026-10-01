"""A CSV of everything in the export that only its maintainer can fix.

The audit's other outputs are written for whoever runs the tool. This one is
written for whoever owns the spreadsheet, who may never run it: one row per
affected host, naming the cell, what it says, what NetBox says, and what to do
about it. No rich markup, no prose, nothing that needs the console to read.

Only conflicts between rows go here. A cell that merely needs a decision --
``DHCP`` in an address column, two addresses in one cell -- is reported in the
audit itself, because it is ambiguous rather than contradictory.
"""

from __future__ import annotations

import csv
import io

from ..inventory import DuplicateGroup
from ..inventory_audit import (
    NETBOX_AGREES,
    SHEET_ERROR,
    UNDECIDABLE,
    AuditResult,
    DuplicateResolution,
)

HEADER = (
    "kind",
    "value",
    "line",
    "hostname",
    "status",
    "sheet_value",
    "netbox_value",
    "netbox_device",
    "verdict",
    "action",
)

# Ordered so the rows needing hardware come first: they are the only ones the
# spreadsheet owner cannot resolve alone.
_VERDICT_ORDER = {NETBOX_AGREES: 0, UNDECIDABLE: 1, SHEET_ERROR: 2}


def _serial_rows(resolutions: list[DuplicateResolution]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for res in sorted(resolutions, key=lambda r: (_VERDICT_ORDER.get(r.verdict, 9), r.value)):
        for member in res.members:
            rows.append(
                {
                    "kind": "duplicate-serial",
                    "value": res.value,
                    "line": str(member.line or ""),
                    "hostname": member.hostname,
                    "status": member.status,
                    "sheet_value": member.sheet_serial,
                    "netbox_value": member.netbox_serial or "",
                    "netbox_device": member.netbox_url,
                    "verdict": res.verdict,
                    "action": res.action,
                }
            )
    return rows


def _group_rows(
    groups: list[DuplicateGroup], kind: str, field: str, action: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for group in groups:
        for row in group.rows:
            rows.append(
                {
                    "kind": kind,
                    "value": group.value,
                    "line": str(row.line),
                    "hostname": row.hostname,
                    "status": row.status,
                    "sheet_value": row.value(field) or "",
                    "netbox_value": "",
                    "netbox_device": "",
                    "verdict": "",
                    "action": action,
                }
            )
    return rows


def build_rows(
    dup_hostnames: list[DuplicateGroup],
    dup_bmc: list[DuplicateGroup],
    dup_serials: list[DuplicateGroup],
    audit: AuditResult | None,
) -> list[dict[str, str]]:
    """Every row-against-row contradiction, flattened one host per line."""
    rows: list[dict[str, str]] = []

    if audit is not None:
        rows += _serial_rows(audit.duplicate_serials)
    else:
        # Offline: the duplicate is known but unresolvable, so say exactly that
        # rather than leaving the verdict column misleadingly blank.
        rows += [
            {**r, "verdict": UNDECIDABLE, "action": "Re-run without --offline to resolve."}
            for r in _group_rows(
                [g for g in dup_serials if not g.historical],
                "duplicate-serial",
                "serial",
                "",
            )
        ]

    rows += _group_rows(
        [g for g in dup_bmc if not g.historical],
        "duplicate-bmc-ip",
        "bmc_ip",
        "Two hosts still in service cannot share one BMC address; one row is wrong.",
    )
    rows += _group_rows(
        dup_hostnames,
        "duplicate-hostname",
        "hostname",
        "The same hostname appears on more than one row; merge or retire one.",
    )
    return rows


def render_conflict_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=HEADER, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
