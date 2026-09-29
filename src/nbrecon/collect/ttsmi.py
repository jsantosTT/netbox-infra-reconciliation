"""tt-smi facts, read from an Ansible export rather than collected over SSH.

The tool never opens an SSH session. An Ansible playbook (owner TBD) runs
tt-smi on the hosts and writes the result out; this module only reads that
export. If the export is absent or stale for a host, every field stays None
and nothing is written.

Two layouts are accepted:

1. A single JSON file::

       {"hosts": {"<hostname>": {"groups": [...], "tt_smi": {...}}}}

2. A directory in Ansible fact-cache style, one ``<hostname>.json`` per host
   containing either the inner object or ``{"tt_smi": {...}}``.

Recognised keys inside ``tt_smi``: flash_version, fw_version, kmd_version,
smi_version, topology, chassis_revision, serial, trays.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import CollectionError
from ..models import HostFacts, TrayFacts

LOG = logging.getLogger("nbrecon.ttsmi")

_FIELD_ALIASES = {
    "flash_version": ("flash_version", "flash", "fw_flash"),
    "fw_version": ("fw_version", "firmware_version", "fw"),
    "kmd_version": ("kmd_version", "kmd"),
    "smi_version": ("smi_version", "smi", "tt_smi_version"),
    "topology": ("topology", "topo"),
    "chassis_revision": ("chassis_revision", "board_revision", "revision"),
    "serial": ("serial", "serial_number", "board_serial"),
}


@dataclass
class AnsibleFacts:
    """Parsed tt-smi export, indexed by hostname."""

    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    source: str = ""

    @classmethod
    def empty(cls) -> AnsibleFacts:
        return cls()

    @classmethod
    def load(cls, path: str | Path) -> AnsibleFacts:
        p = Path(path)
        if not p.exists():
            raise CollectionError(f"Ansible facts export not found: {p}")
        return cls._load_dir(p) if p.is_dir() else cls._load_file(p)

    @classmethod
    def _load_file(cls, path: Path) -> AnsibleFacts:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CollectionError(f"could not read Ansible facts {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CollectionError(f"{path}: expected a JSON object at the top level")

        raw_hosts = data.get("hosts")
        if not isinstance(raw_hosts, dict):
            raise CollectionError(f"{path}: missing a 'hosts' object")

        facts = cls(source=str(path))
        for hostname, entry in raw_hosts.items():
            if not isinstance(entry, dict):
                continue
            facts.hosts[str(hostname)] = entry.get("tt_smi") or entry
            for group in entry.get("groups") or []:
                facts.groups.setdefault(str(group), []).append(str(hostname))
        return facts

    @classmethod
    def _load_dir(cls, path: Path) -> AnsibleFacts:
        facts = cls(source=str(path))
        for child in sorted(path.glob("*.json")):
            try:
                data = json.loads(child.read_text())
            except (OSError, ValueError) as exc:
                LOG.warning("skipping unreadable fact file %s: %s", child, exc)
                continue
            if not isinstance(data, dict):
                continue
            hostname = str(data.get("hostname") or child.stem)
            facts.hosts[hostname] = data.get("tt_smi") or data
            for group in data.get("groups") or []:
                facts.groups.setdefault(str(group), []).append(hostname)
        return facts

    def hosts_in_group(self, group: str) -> list[str]:
        return sorted(set(self.groups.get(group, [])))

    def for_host(self, hostname: str | None) -> dict[str, Any] | None:
        if not hostname:
            return None
        if hostname in self.hosts:
            return self.hosts[hostname]
        # Tolerate FQDN vs short-name mismatches between Ansible and NetBox.
        short = hostname.split(".", 1)[0].lower()
        for key, value in self.hosts.items():
            if key.split(".", 1)[0].lower() == short:
                return value
        return None


def _pick(payload: dict[str, Any], canonical: str) -> str | None:
    for alias in _FIELD_ALIASES[canonical]:
        if alias in payload and payload[alias] not in (None, ""):
            return str(payload[alias]).strip()
    return None


def enrich(facts: HostFacts, export: AnsibleFacts, hostname: str | None) -> HostFacts:
    """Overlay tt-smi values onto Redfish facts.

    Redfish stays authoritative for anything it already returned; tt-smi only
    fills gaps and supplies the fields Redfish does not expose at all.
    """
    payload = export.for_host(hostname)
    if not payload:
        return facts

    facts.sources.append("ttsmi")

    for canonical in ("flash_version", "fw_version", "kmd_version", "smi_version", "topology"):
        value = _pick(payload, canonical)
        if value is not None:
            setattr(facts, canonical, value)

    if facts.chassis_revision is None:
        facts.chassis_revision = _pick(payload, "chassis_revision")
    if facts.serial is None:
        facts.serial = _pick(payload, "serial")

    if not facts.trays:
        for entry in payload.get("trays") or []:
            if not isinstance(entry, dict):
                continue
            facts.trays.append(
                TrayFacts(
                    tray_id=str(entry.get("tray_id") or entry.get("id") or "?"),
                    serial=(str(entry["serial"]).strip() if entry.get("serial") else None),
                    model=(str(entry["model"]).strip() if entry.get("model") else None),
                    part_number=(
                        str(entry["part_number"]).strip() if entry.get("part_number") else None
                    ),
                    source="ttsmi",
                )
            )
    return facts
