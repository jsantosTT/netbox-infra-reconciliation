"""A plain read of NetBox for a scope.

No Redfish, no Prometheus, no Jira, no run directory, no run-history entry.
This exists to answer "what does NetBox currently say about these devices"
without the side effects of a pipeline run, and it needs no credentials beyond
the NetBox URL and token.

The summary is built around one question the console table cannot answer at a
glance: how many of these devices could the tool actually reach. A device with
neither an OOB address nor a primary IPv4 is never probed, so it is invisible
to reconciliation no matter how correct the rest of its record is.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import asdict, dataclass, field
from io import StringIO
from typing import Any

from .correlate import probe_address
from .models import NetBoxDevice

BASE_COLUMNS = [
    "id",
    "name",
    "serial",
    "asset_tag",
    "device_type_slug",
    "site_slug",
    "rack",
    "status",
    "primary_ip4",
    "oob_ip",
    "probe_address",
    "last_updated",
    "url",
    "tags",
]


@dataclass
class Summary:
    total: int = 0
    with_serial: int = 0
    without_serial: int = 0
    duplicate_serials: list[str] = field(default_factory=list)
    with_oob_ip: int = 0
    with_primary_ip4: int = 0
    probeable: int = 0
    unprobeable: list[str] = field(default_factory=list)
    by_status: dict[str, int] = field(default_factory=dict)
    by_site: dict[str, int] = field(default_factory=dict)
    by_device_type: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarise(devices: list[NetBoxDevice]) -> Summary:
    serials = Counter(d.serial.strip().lower() for d in devices if d.serial)
    unprobeable = sorted(
        (d.name or f"id-{d.id}") for d in devices if probe_address(d) is None
    )
    return Summary(
        total=len(devices),
        with_serial=sum(1 for d in devices if d.serial),
        without_serial=sum(1 for d in devices if not d.serial),
        duplicate_serials=sorted(s for s, n in serials.items() if n > 1),
        with_oob_ip=sum(1 for d in devices if d.oob_ip),
        with_primary_ip4=sum(1 for d in devices if d.primary_ip4),
        probeable=sum(1 for d in devices if probe_address(d) is not None),
        unprobeable=unprobeable,
        by_status=_tally(d.status for d in devices),
        by_site=_tally(d.site_slug for d in devices),
        by_device_type=_tally(d.device_type_slug for d in devices),
    )


def _tally(values: Any) -> dict[str, int]:
    counted = Counter(v or "(none)" for v in values)
    return dict(sorted(counted.items(), key=lambda kv: (-kv[1], kv[0])))


def custom_field_names(devices: list[NetBoxDevice]) -> list[str]:
    """Every custom field seen, so the CSV has a stable column per field."""
    names: set[str] = set()
    for device in devices:
        names.update(device.custom_fields or {})
    return sorted(names)


def to_rows(devices: list[NetBoxDevice]) -> list[dict[str, Any]]:
    """Flatten for CSV: tags joined, custom fields prefixed."""
    rows: list[dict[str, Any]] = []
    for device in devices:
        row: dict[str, Any] = {
            "id": device.id,
            "name": device.name or "",
            "serial": device.serial or "",
            "asset_tag": device.asset_tag or "",
            "device_type_slug": device.device_type_slug or "",
            "site_slug": device.site_slug or "",
            "rack": device.rack or "",
            "status": device.status or "",
            "primary_ip4": device.primary_ip4 or "",
            "oob_ip": device.oob_ip or "",
            # What Redfish would be pointed at, so the CSV answers "would this
            # device be probed" without the reader reapplying the rule.
            "probe_address": probe_address(device) or "",
            "last_updated": device.last_updated or "",
            "url": device.url or "",
            "tags": " ".join(device.tags or []),
        }
        for name, value in (device.custom_fields or {}).items():
            row[f"cf_{name}"] = "" if value is None else value
        rows.append(row)
    return rows


def to_csv(devices: list[NetBoxDevice]) -> str:
    columns = BASE_COLUMNS + [f"cf_{n}" for n in custom_field_names(devices)]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in to_rows(devices):
        writer.writerow({c: row.get(c, "") for c in columns})
    return buffer.getvalue()


def to_payload(
    scope: dict[str, Any], netbox_url: str, devices: list[NetBoxDevice], taken_at: str
) -> dict[str, Any]:
    return {
        "taken_at": taken_at,
        "netbox_url": netbox_url,
        "scope": scope,
        "summary": summarise(devices).as_dict(),
        "devices": [asdict(d) for d in devices],
    }
