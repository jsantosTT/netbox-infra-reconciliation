"""Parse the Cloud Resources inventory export.

The spreadsheet is maintained by hand across many teams, so every cell is
treated as a claim to be checked rather than a fact. Two rules shape this
module:

* **Never guess.** A cell that does not yield exactly one unambiguous value is
  recorded as an issue and the field is left unset. ``IP (s)`` holding
  ``"172.27.107.11, 172.27.28.81"`` produces no address and one finding, not a
  coin flip between the two.
* **Never repair in place.** Normalisation is limited to whitespace and MAC
  formatting. The original text is kept alongside so the report can quote what
  the sheet actually says.

Nothing here touches the network. Parsing and quality analysis run with no
credentials, which is what makes ``--offline`` possible.
"""

from __future__ import annotations

import csv
import ipaddress
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

# Spreadsheet column headers. Listed here rather than inlined so a renamed
# column fails loudly at load time instead of silently reading as empty.
COLUMNS = {
    "hostname": "Hostname",
    "status": "Status",
    "site": "Site",
    "rack": "Rack",
    "position": "RU Location",
    "serial": "Serial #",
    "bmc_ip": "BMC IP",
    "bmc_mac": "BMC MAC",
    "primary_ip": "IP (s)",
    "primary_mac": "MAC Address",
    "model": "Model #",
}

REQUIRED_COLUMNS = ("Hostname", "Status", "Serial #", "BMC IP")

DECOMMISSIONED = "decommissioned"

_MAC_CHARS = re.compile(r"^[0-9a-f]{12}$")
_SPLIT = re.compile(r"[\s,;/]+")


@dataclass(frozen=True)
class Issue:
    """A cell that could not be read as one unambiguous value.

    Structured rather than free text so the report can group identical
    problems: twenty hosts whose IP cell says ``DHCP`` is one decision to make,
    not twenty.
    """

    column: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.column} {self.detail}"


@dataclass(frozen=True)
class InventoryRow:
    """One server as the spreadsheet describes it."""

    line: int
    hostname: str
    status: str
    serial: str | None = None
    bmc_ip: str | None = None
    bmc_mac: str | None = None
    primary_ip: str | None = None
    model: str | None = None
    site: str | None = None
    rack: str | None = None
    position: str | None = None
    raw: dict[str, str] = field(default_factory=dict)
    issues: tuple[Issue, ...] = ()

    @property
    def decommissioned(self) -> bool:
        return self.status.strip().lower() == DECOMMISSIONED

    def value(self, key: str) -> str | None:
        return getattr(self, key, None)


@dataclass
class ParseResult:
    rows: list[InventoryRow] = field(default_factory=list)
    skipped_blank: int = 0
    total_lines: int = 0
    # Tidying that was applied silently and identically to many rows. Counted
    # rather than listed per row, because 104 copies of "this cell has a
    # trailing space" buries the handful of findings that need a decision.
    normalised: dict[str, int] = field(default_factory=dict)

    @property
    def issues(self) -> list[tuple[InventoryRow, Issue]]:
        return [(row, issue) for row in self.rows for issue in row.issues]


def parse_inventory(path: Path) -> ParseResult:
    """Read the export. Rows with no hostname are padding and are skipped."""
    try:
        handle = path.open(newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"cannot read inventory export {path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ConfigError(f"{path} is empty; expected a header row")
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ConfigError(
                f"{path} is missing required column(s): {', '.join(missing)}. "
                "Export the 'Servers' sheet with its header row intact."
            )
        result = ParseResult()
        for offset, raw in enumerate(reader, start=2):
            result.total_lines += 1
            row = _build_row(offset, raw, result.normalised)
            if row is None:
                result.skipped_blank += 1
                continue
            result.rows.append(row)
    return result


def _build_row(
    line: int, raw: dict[str, Any], normalised: dict[str, int]
) -> InventoryRow | None:
    text = {k: _clean(v) for k, v in raw.items() if isinstance(k, str)}
    hostname = text.get(COLUMNS["hostname"], "")
    if not hostname:
        return None

    issues: list[Issue] = []

    bmc_ip = _one_address(text.get(COLUMNS["bmc_ip"], ""), "BMC IP", issues)
    primary_ip = _one_address(text.get(COLUMNS["primary_ip"], ""), "IP (s)", issues)
    bmc_mac = _mac(text.get(COLUMNS["bmc_mac"], ""), "BMC MAC", issues)

    for column in (COLUMNS["status"], COLUMNS["serial"], COLUMNS["hostname"]):
        value = raw.get(column) or ""
        if value.strip() and value != value.strip():
            key = f"{column}: surrounding whitespace trimmed"
            normalised[key] = normalised.get(key, 0) + 1

    return InventoryRow(
        line=line,
        hostname=hostname,
        status=text.get(COLUMNS["status"], ""),
        serial=text.get(COLUMNS["serial"]) or None,
        bmc_ip=bmc_ip,
        bmc_mac=bmc_mac,
        primary_ip=primary_ip,
        model=text.get(COLUMNS["model"]) or None,
        site=text.get(COLUMNS["site"]) or None,
        rack=text.get(COLUMNS["rack"]) or None,
        position=text.get(COLUMNS["position"]) or None,
        raw=text,
        issues=tuple(issues),
    )


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _one_address(raw: str, label: str, issues: list[Issue]) -> str | None:
    """Exactly one IPv4 address, or nothing at all.

    The sheet carries annotated cells (``172.27.29.12 (pre-IT IP)``), dual
    homed hosts (``172.27.107.11, 172.27.28.81``) and placeholders (``DHCP``,
    ``N/A``). Only the unambiguous single-address case is usable; the rest are
    reported so a human can decide, because picking one silently is exactly how
    a tool ends up authenticating against the wrong machine.
    """
    if not raw:
        return None
    found: list[str] = []
    for token in _SPLIT.split(raw):
        candidate = token.strip("()[],;")
        if not candidate:
            continue
        try:
            ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if candidate not in found:
            found.append(candidate)

    if not found:
        issues.append(
            Issue(label, "holds no IPv4 address", f"{raw!r} is not an IPv4 address")
        )
        return None
    if len(found) > 1:
        issues.append(
            Issue(
                label,
                "holds more than one address, so a human picks",
                f"{raw!r} contains {len(found)}: {', '.join(found)}",
            )
        )
        return None
    if found[0] != raw:
        issues.append(
            Issue(label, "has extra text around the address", f"{raw!r} reads as {found[0]}")
        )
    return found[0]


def _mac(raw: str, label: str, issues: list[Issue]) -> str | None:
    """Normalise to lowercase colon-separated form for comparison."""
    if not raw:
        return None
    stripped = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if not _MAC_CHARS.match(stripped):
        issues.append(Issue(label, "is not a MAC address", f"{raw!r} is not a MAC address"))
        return None
    return ":".join(stripped[i : i + 2] for i in range(0, 12, 2))


# --- duplicate analysis ----------------------------------------------------


@dataclass(frozen=True)
class DuplicateGroup:
    value: str
    rows: tuple[InventoryRow, ...]

    @property
    def live_rows(self) -> tuple[InventoryRow, ...]:
        return tuple(r for r in self.rows if not r.decommissioned)

    @property
    def serials(self) -> tuple[str, ...]:
        return tuple(sorted({r.serial for r in self.rows if r.serial}))

    @property
    def historical(self) -> bool:
        """One live row and the rest retired: a rename, not a conflict.

        The sheet keeps the old row when a machine is re-racked and renamed, so
        the same BMC address legitimately appears twice. That is only benign
        while exactly one of the rows is still in service.
        """
        return len(self.live_rows) <= 1

    def describe(self) -> str:
        # Parentheses, not square brackets: this string is printed through rich,
        # which would read "[no serial, In Use]" as markup and swallow it.
        return ", ".join(
            f"{r.hostname} ({r.serial or 'no serial'}, {r.status or 'no status'})"
            for r in self.rows
        )


def duplicates(rows: Iterable[InventoryRow], key: str) -> list[DuplicateGroup]:
    """Group rows that share a value, comparing case-insensitively.

    The group keeps the first spelling it saw rather than the folded key, so
    the report shows the serial as the sheet writes it.
    """
    grouped: dict[str, list[InventoryRow]] = defaultdict(list)
    display: dict[str, str] = {}
    for row in rows:
        value = row.value(key)
        if not value:
            continue
        folded = value.strip().lower()
        grouped[folded].append(row)
        display.setdefault(folded, value.strip())
    return [
        DuplicateGroup(value=display[k], rows=tuple(rs))
        for k, rs in sorted(grouped.items())
        if len(rs) > 1
    ]


# --- network classification ------------------------------------------------


@dataclass(frozen=True)
class Network:
    name: str
    cidr: ipaddress.IPv4Network
    is_bmc: bool


@dataclass
class NetworkMap:
    """Which address ranges are BMC ranges, from the workbook's Networks sheet.

    Used to catch a 'BMC IP' that is really a host address. Connecting to one
    with BMC credentials would hand them to the host OS, so the check exists
    even though the current export passes it.
    """

    networks: list[Network] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.networks)

    @classmethod
    def load(cls, path: Path) -> NetworkMap:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

        networks: list[Network] = []
        for is_bmc, section in ((True, "bmc"), (False, "other")):
            entries = data.get(section) or []
            if not isinstance(entries, list):
                raise ConfigError(f"{path}: '{section}' must be a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ConfigError(f"{path}: every entry under '{section}' must be a mapping")
                cidr = str(entry.get("cidr") or "").strip()
                try:
                    network = ipaddress.IPv4Network(cidr)
                except ValueError as exc:
                    raise ConfigError(f"{path}: {cidr!r} is not an IPv4 network: {exc}") from exc
                networks.append(
                    Network(
                        name=str(entry.get("name") or cidr).strip(),
                        cidr=network,
                        is_bmc=is_bmc,
                    )
                )
        # Most specific wins, so an inner /24 is reported instead of the /12
        # that contains it.
        networks.sort(key=lambda n: n.cidr.prefixlen, reverse=True)
        return cls(networks=networks)

    def classify(self, address: str) -> Network | None:
        try:
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            return None
        for network in self.networks:
            if ip in network.cidr:
                return network
        return None


def iter_rows(rows: Iterable[InventoryRow], statuses: Iterable[str]) -> Iterator[InventoryRow]:
    wanted = {s.strip().lower() for s in statuses if s.strip()}
    for row in rows:
        if not wanted or row.status.strip().lower() in wanted:
            yield row
