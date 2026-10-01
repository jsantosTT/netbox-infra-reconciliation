"""Compare the inventory export against live NetBox. Reads only.

This is a reporting stage, not a pipeline stage: it produces no plan, no
approval and no run history, and it cannot write. The point is to turn a
spreadsheet into a reviewed worklist, so the output is deliberately split into
what is safe to act on and what a human has to resolve first.

Two deliberate restrictions:

* **Serial is still the only identity.** A hostname match is reported as a
  suggestion and never reaches the seed worklist, exactly as in the pipeline.
* **The sheet is never treated as authoritative.** Where the two disagree, the
  disagreement is reported. Nothing here decides who is right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .collect.netbox import NetBoxClient
from .errors import SafetyViolation
from .inventory import InventoryRow, NetworkMap
from .models import NetBoxDevice

# Verdicts for one field on one row.
AGREE = "agree"
NETBOX_EMPTY = "netbox-empty"
SHEET_EMPTY = "sheet-empty"
DIFFER = "differ"

# How a sheet row was tied to a NetBox device.
BY_SERIAL = "serial"
BY_HOSTNAME = "hostname"
UNMATCHED = "none"
AMBIGUOUS = "ambiguous"

DeviceIndex = dict[str, list["NetBoxDevice"]]


class ReadOnlyNetBox:
    """A NetBox client with its write methods removed.

    The audit only ever calls read methods, so this wrapper changes nothing in
    practice. It exists so that "this command cannot write to NetBox" is
    enforced by the object rather than asserted in a docstring, and so a future
    edit that reaches for a write fails immediately and loudly.
    """

    BLOCKED = frozenset({"patch_device", "patch_interface", "create_journal_entry", "session"})

    def __init__(self, client: NetBoxClient) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in ReadOnlyNetBox.BLOCKED or name.startswith(("patch", "post", "create", "delete")):
            raise SafetyViolation(
                f"the inventory audit is read-only; it must not call {name!r} on NetBox"
            )
        return getattr(object.__getattribute__(self, "_client"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise SafetyViolation("the inventory audit must not mutate its NetBox client")


@dataclass(frozen=True)
class FieldComparison:
    key: str
    label: str
    sheet: str | None
    netbox: str | None
    verdict: str


@dataclass
class SeedCandidate:
    """One BMC address the sheet could contribute to NetBox."""

    hostname: str
    device_id: int
    device_url: str
    serial: str
    address: str
    network: str
    ipam_state: str  # absent | unassigned | assigned-elsewhere
    ipam_detail: str = ""

    @property
    def ready(self) -> bool:
        """Can ``nbrecon apply`` use this address today?

        Only when the IPAM object already exists and belongs to nobody else.
        ``apply`` refuses to create IPAM objects, so an absent entry is a task
        for a NetBox admin, not something this tool can resolve.
        """
        return self.ipam_state == "unassigned"


@dataclass
class RowAudit:
    row: InventoryRow
    match: str
    device: NetBoxDevice | None = None
    candidates: tuple[str, ...] = ()
    comparisons: list[FieldComparison] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    seed: SeedCandidate | None = None

    def verdict(self, key: str) -> str | None:
        for comparison in self.comparisons:
            if comparison.key == key:
                return comparison.verdict
        return None


@dataclass
class AuditResult:
    audits: list[RowAudit] = field(default_factory=list)
    unmatched_devices: list[NetBoxDevice] = field(default_factory=list)
    netbox_read: int = 0

    @property
    def matched(self) -> list[RowAudit]:
        return [a for a in self.audits if a.match == BY_SERIAL]

    @property
    def seeds(self) -> list[SeedCandidate]:
        return [a.seed for a in self.audits if a.seed is not None]

    @property
    def ready_seeds(self) -> list[SeedCandidate]:
        return [s for s in self.seeds if s.ready]


def _bare(address: str | None) -> str | None:
    """NetBox stores ``172.27.24.5/22``; the sheet stores ``172.27.24.5``."""
    if not address:
        return None
    return address.split("/", 1)[0].strip() or None


def _compare(key: str, label: str, sheet: str | None, netbox: str | None) -> FieldComparison:
    left = (sheet or "").strip()
    right = (netbox or "").strip()
    if left and not right:
        verdict = NETBOX_EMPTY
    elif right and not left:
        verdict = SHEET_EMPTY
    elif left.lower() == right.lower():
        verdict = AGREE
    else:
        verdict = DIFFER
    return FieldComparison(key=key, label=label, sheet=sheet, netbox=netbox, verdict=verdict)


def index_devices(devices: list[NetBoxDevice]) -> tuple[DeviceIndex, DeviceIndex]:
    by_serial: DeviceIndex = {}
    by_name: DeviceIndex = {}
    for device in devices:
        if device.serial:
            by_serial.setdefault(device.serial.strip().lower(), []).append(device)
        if device.name:
            by_name.setdefault(device.name.strip().lower(), []).append(device)
    return by_serial, by_name


def audit_rows(
    rows: list[InventoryRow],
    devices: list[NetBoxDevice],
    networks: NetworkMap,
    live_collisions: set[str],
    client: ReadOnlyNetBox | None = None,
) -> AuditResult:
    """Match every row, compare the fields, and decide what is seedable."""
    by_serial, by_name = index_devices(devices)
    result = AuditResult(netbox_read=len(devices))
    seen_device_ids: set[int] = set()

    for row in rows:
        audit = _audit_row(row, by_serial, by_name)
        if audit.device is not None:
            seen_device_ids.add(audit.device.id)
        _decide_seed(audit, networks, live_collisions, client)
        result.audits.append(audit)

    result.unmatched_devices = [d for d in devices if d.id not in seen_device_ids]
    return result


def _audit_row(
    row: InventoryRow,
    by_serial: dict[str, list[NetBoxDevice]],
    by_name: dict[str, list[NetBoxDevice]],
) -> RowAudit:
    if row.serial:
        matches = by_serial.get(row.serial.strip().lower(), [])
        if len(matches) == 1:
            return _with_comparisons(RowAudit(row=row, match=BY_SERIAL, device=matches[0]))
        if len(matches) > 1:
            audit = RowAudit(
                row=row,
                match=AMBIGUOUS,
                candidates=tuple(d.name or str(d.id) for d in matches),
            )
            audit.blockers.append(
                f"serial {row.serial} matches {len(matches)} NetBox devices "
                f"({', '.join(audit.candidates)})"
            )
            return audit

    matches = by_name.get(row.hostname.strip().lower(), [])
    if len(matches) == 1:
        audit = _with_comparisons(RowAudit(row=row, match=BY_HOSTNAME, device=matches[0]))
        audit.blockers.append(
            "matched on hostname only; serial is the tool's identity, so this is a "
            "suggestion for a human and is never seeded automatically"
        )
        return audit

    audit = RowAudit(row=row, match=UNMATCHED)
    audit.blockers.append("no NetBox device with this serial or hostname")
    return audit


def _with_comparisons(audit: RowAudit) -> RowAudit:
    device = audit.device
    row = audit.row
    assert device is not None
    audit.comparisons = [
        _compare("serial", "Serial", row.serial, device.serial),
        _compare("bmc_ip", "BMC / OOB IP", row.bmc_ip, _bare(device.oob_ip)),
        _compare("primary_ip", "Primary IPv4", row.primary_ip, _bare(device.primary_ip4)),
    ]
    return audit


def _decide_seed(
    audit: RowAudit,
    networks: NetworkMap,
    live_collisions: set[str],
    client: ReadOnlyNetBox | None,
) -> None:
    """Work out whether this row's BMC address is safe to put into NetBox.

    Every gate here exists because failing it would point the tool, holding
    real BMC credentials, at a machine that is not the one NetBox names.
    """
    row = audit.row
    device = audit.device
    if device is None or audit.match != BY_SERIAL or not row.bmc_ip:
        return

    if audit.verdict("bmc_ip") != NETBOX_EMPTY:
        return

    if row.bmc_ip in live_collisions:
        audit.blockers.append(
            f"BMC IP {row.bmc_ip} is claimed by more than one host that is still in "
            "service; a human decides which one owns it"
        )
        return

    network = networks.classify(row.bmc_ip) if networks.configured else None
    if networks.configured:
        if network is None:
            audit.blockers.append(
                f"BMC IP {row.bmc_ip} is not inside any known network"
            )
            return
        if not network.is_bmc:
            audit.blockers.append(
                f"BMC IP {row.bmc_ip} is in {network.name!r}, which is not a BMC network; "
                "connecting there with BMC credentials would hand them to something else"
            )
            return

    state, detail = _ipam_state(client, row.bmc_ip, device.id)
    audit.seed = SeedCandidate(
        hostname=row.hostname,
        device_id=device.id,
        device_url=device.url or "",
        serial=row.serial or "",
        address=row.bmc_ip,
        network=network.name if network else "unchecked",
        ipam_state=state,
        ipam_detail=detail,
    )
    if state == "assigned-elsewhere":
        audit.blockers.append(f"IPAM entry for {row.bmc_ip} is already assigned to {detail}")
    elif state == "absent":
        audit.blockers.append(
            f"no IPAM entry for {row.bmc_ip}; nbrecon never creates IPAM objects, so a "
            "NetBox admin has to add it before this can be applied"
        )


def _ipam_state(client: ReadOnlyNetBox | None, address: str, device_id: int) -> tuple[str, str]:
    """Mirror the lookup ``apply`` performs, so the audit predicts its outcome."""
    if client is None:
        return "unchecked", ""
    entry = client.find_ip_address(address) or client.find_ip_address(f"{address}/32")
    if not entry:
        return "absent", ""

    assigned = entry.get("assigned_object") or {}
    if not assigned:
        return "unassigned", str(entry.get("display") or address)

    owner = assigned.get("device") or {}
    owner_id = owner.get("id") if isinstance(owner, dict) else None
    if owner_id and int(owner_id) == device_id:
        return "unassigned", "already on this device"
    label = owner.get("name") if isinstance(owner, dict) else None
    return "assigned-elsewhere", str(label or assigned.get("display") or "another object")
