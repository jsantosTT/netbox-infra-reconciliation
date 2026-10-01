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
from .inventory import DuplicateGroup, InventoryRow, NetworkMap
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

# What a serial shared by several live sheet rows turns out to mean once NetBox
# has been asked about each host separately.
SHEET_ERROR = "sheet-error"
NETBOX_AGREES = "netbox-agrees"
UNDECIDABLE = "undecidable"

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


@dataclass(frozen=True)
class DuplicateMember:
    """One host from a duplicated-serial group, with what NetBox says about it."""

    hostname: str
    status: str
    sheet_serial: str
    line: int = 0
    netbox_serial: str | None = None
    netbox_name: str | None = None
    netbox_url: str = ""
    netbox_id: int | None = None
    lookup: str = "found"  # found | absent | several

    @property
    def usable(self) -> bool:
        """Did NetBox give one device carrying a serial we can compare?"""
        return self.lookup == "found" and bool((self.netbox_serial or "").strip())


@dataclass(frozen=True)
class DuplicateResolution:
    """What a serial shared by several live sheet rows actually means.

    The sheet says two machines are one. NetBox is asked about each host
    independently, and the answer decides whether this is a typing mistake or
    something that needs a hand on the hardware.
    """

    value: str
    verdict: str
    detail: str
    members: tuple[DuplicateMember, ...] = ()

    @property
    def action(self) -> str:
        if self.verdict == SHEET_ERROR:
            return "Correct the spreadsheet; NetBox already holds distinct serials."
        if self.verdict == NETBOX_AGREES:
            return "Read the serial from the hardware; both sources carry the duplicate."
        return "Cannot be settled from NetBox; see the detail."


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
    duplicate_serials: list[DuplicateResolution] = field(default_factory=list)

    @property
    def sheet_errors(self) -> list[DuplicateResolution]:
        return [d for d in self.duplicate_serials if d.verdict == SHEET_ERROR]

    @property
    def hardware_conflicts(self) -> list[DuplicateResolution]:
        return [d for d in self.duplicate_serials if d.verdict == NETBOX_AGREES]

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


def resolve_duplicate_serials(
    groups: list[DuplicateGroup], by_name: DeviceIndex
) -> list[DuplicateResolution]:
    """Ask NetBox what each host in a duplicated-serial group really is.

    Hosts are looked up by name, deliberately. The serial is the field under
    dispute, so using it to find the device would assume the answer: the
    duplicated value would simply lead back to whichever single device happens
    to carry it, and both rows would appear to agree.

    Only groups with more than one row still in service are considered. A
    serial repeated across one live row and several retired ones is a
    replacement record, which ``DuplicateGroup.historical`` already covers.
    """
    resolutions: list[DuplicateResolution] = []
    for group in groups:
        if group.historical:
            continue
        members = tuple(_describe_member(row, by_name) for row in group.live_rows)
        resolutions.append(_verdict(group.value, members))
    return resolutions


def _describe_member(row: InventoryRow, by_name: DeviceIndex) -> DuplicateMember:
    found = by_name.get(row.hostname.strip().lower(), [])
    if len(found) == 1:
        device = found[0]
        return DuplicateMember(
            hostname=row.hostname,
            status=row.status,
            sheet_serial=row.serial or "",
            line=row.line,
            netbox_serial=device.serial or None,
            netbox_name=device.name,
            netbox_url=device.url or "",
            netbox_id=device.id,
            lookup="found",
        )
    return DuplicateMember(
        hostname=row.hostname,
        status=row.status,
        sheet_serial=row.serial or "",
        line=row.line,
        lookup="absent" if not found else "several",
    )


def _verdict(value: str, members: tuple[DuplicateMember, ...]) -> DuplicateResolution:
    usable = [m for m in members if m.usable]
    if len(usable) < 2:
        missing = [m.hostname for m in members if not m.usable]
        return DuplicateResolution(
            value=value,
            verdict=UNDECIDABLE,
            detail=(
                "NetBox has a serial for fewer than two of these hosts; no serial on "
                f"record for {', '.join(missing)}"
            ),
            members=members,
        )

    seen: dict[str, list[str]] = {}
    for member in usable:
        seen.setdefault((member.netbox_serial or "").strip().lower(), []).append(member.hostname)

    shared = {k: v for k, v in seen.items() if len(v) > 1}
    if shared:
        pairs = "; ".join(f"{', '.join(hosts)} both hold {k}" for k, hosts in shared.items())
        return DuplicateResolution(
            value=value,
            verdict=NETBOX_AGREES,
            detail=f"NetBox carries the duplicate too ({pairs})",
            members=members,
        )

    return DuplicateResolution(
        value=value,
        verdict=SHEET_ERROR,
        detail=(
            "NetBox holds a distinct serial for each host ("
            + ", ".join(f"{m.hostname}={m.netbox_serial}" for m in usable)
            + ")"
        ),
        members=members,
    )


def audit_rows(
    rows: list[InventoryRow],
    devices: list[NetBoxDevice],
    networks: NetworkMap,
    live_collisions: set[str],
    client: ReadOnlyNetBox | None = None,
    dup_serials: list[DuplicateGroup] | None = None,
) -> AuditResult:
    """Match every row, compare the fields, and decide what is seedable."""
    by_serial, by_name = index_devices(devices)
    groups = dup_serials or []
    result = AuditResult(netbox_read=len(devices))
    result.duplicate_serials = resolve_duplicate_serials(groups, by_name)
    # Folded once here so every row lookup is a set membership test.
    contested = {g.value.strip().lower() for g in groups if not g.historical}
    seen_device_ids: set[int] = set()

    for row in rows:
        audit = _audit_row(row, by_serial, by_name, contested)
        if audit.device is not None:
            seen_device_ids.add(audit.device.id)
        _decide_seed(audit, networks, live_collisions, client)
        result.audits.append(audit)

    # A contested row is deliberately left unmatched, which would otherwise push
    # its device into "NetBox devices with no sheet row" -- the opposite of true,
    # since the sheet names it twice.
    seen_device_ids |= {
        m.netbox_id
        for res in result.duplicate_serials
        for m in res.members
        if m.netbox_id is not None
    }
    result.unmatched_devices = [d for d in devices if d.id not in seen_device_ids]
    return result


def _audit_row(
    row: InventoryRow,
    by_serial: dict[str, list[NetBoxDevice]],
    by_name: dict[str, list[NetBoxDevice]],
    contested: set[str] | None = None,
) -> RowAudit:
    contested = contested or set()
    if row.serial:
        folded = row.serial.strip().lower()
        # A serial two live rows both claim identifies neither of them. Without
        # this the duplicate resolves to the one device that genuinely carries
        # it, and the other row is bound, silently and confidently, to its
        # neighbour's device.
        if folded in contested:
            audit = RowAudit(row=row, match=AMBIGUOUS)
            audit.blockers.append(
                f"serial {row.serial} is claimed by more than one live row in the sheet, "
                "so it cannot identify a device; fix the export first"
            )
            return audit
        matches = by_serial.get(folded, [])
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
