"""Correlate NetBox devices with collected host facts.

Identity is the serial number and nothing else. Hostname and BMC IP can and do
change, so they only ever produce a *suggested* match, which is marked as
requiring explicit per-device approval and can never be bulk-approved.

A device whose serial is missing or duplicated is never matched automatically
and never written to; it goes to the unmatched report for a human to fix.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .models import (
    CollectionStatus,
    DevicePair,
    HostFacts,
    MatchMethod,
    NetBoxDevice,
    UnmatchedEntry,
    UnmatchedReason,
)

LOG = logging.getLogger("nbrecon.correlate")


def normalise_serial(serial: str | None) -> str | None:
    """Comparison form of a serial. The original is preserved for writes."""
    if serial is None:
        return None
    cleaned = serial.strip().upper()
    return cleaned or None


def _short(hostname: str | None) -> str | None:
    if not hostname:
        return None
    return hostname.split(".", 1)[0].strip().lower() or None


@dataclass
class CorrelationResult:
    pairs: list[DevicePair] = field(default_factory=list)
    unmatched: list[UnmatchedEntry] = field(default_factory=list)
    collection_failures: list[UnmatchedEntry] = field(default_factory=list)

    @property
    def matched_device_ids(self) -> set[int]:
        return {p.netbox.id for p in self.pairs}


def correlate(
    devices: list[NetBoxDevice],
    host_by_ip: dict[str, HostFacts],
    probe_ip_by_device: dict[int, str],
) -> CorrelationResult:
    """Build device pairs and the unmatched report.

    ``probe_ip_by_device`` records which BMC address was tried for each NetBox
    device, so a device with no reachable address can be reported distinctly
    from one that answered but did not match.
    """
    result = CorrelationResult()

    devices_by_id = {d.id: d for d in devices}
    claimed_devices: set[int] = set()
    consumed_hosts: set[str] = set()

    # --- index NetBox by serial, flagging duplicates -----------------------
    netbox_by_serial: dict[str, list[NetBoxDevice]] = defaultdict(list)
    for device in devices:
        key = normalise_serial(device.serial)
        if key:
            netbox_by_serial[key].append(device)

    duplicate_netbox_serials = {k for k, v in netbox_by_serial.items() if len(v) > 1}
    for serial in sorted(duplicate_netbox_serials):
        dupes = netbox_by_serial[serial]
        for device in dupes:
            claimed_devices.add(device.id)
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.DUPLICATE_SERIAL,
                    identifier=device.name or f"device-{device.id}",
                    detail=(
                        f"serial {serial!r} is shared by NetBox devices "
                        f"{', '.join(str(d.id) for d in dupes)}; no automatic match, no writes"
                    ),
                    device_id=device.id,
                    netbox_url=device.url,
                )
            )

    # --- index collected hosts by serial, flagging duplicates --------------
    hosts_by_serial: dict[str, list[tuple[str, HostFacts]]] = defaultdict(list)
    for ip, facts in host_by_ip.items():
        if not facts.usable:
            continue
        key = normalise_serial(facts.serial)
        if key:
            hosts_by_serial[key].append((ip, facts))

    for serial, entries in sorted(hosts_by_serial.items()):
        if len(entries) > 1:
            for ip, _ in entries:
                consumed_hosts.add(ip)
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.DUPLICATE_SERIAL,
                    identifier=serial,
                    detail=(
                        "serial reported by more than one BMC: "
                        f"{', '.join(ip for ip, _ in entries)}; no automatic match, no writes"
                    ),
                )
            )

    # --- pass 1: authoritative match on serial -----------------------------
    for serial, entries in hosts_by_serial.items():
        if len(entries) > 1 or serial in duplicate_netbox_serials:
            continue
        candidates = netbox_by_serial.get(serial) or []
        if len(candidates) != 1:
            continue
        device = candidates[0]
        if device.id in claimed_devices:
            continue
        ip, facts = entries[0]
        result.pairs.append(
            DevicePair(
                netbox=device,
                host=facts,
                match_method=MatchMethod.SERIAL,
                match_evidence=f"serial {serial} matched NetBox device {device.id}",
            )
        )
        claimed_devices.add(device.id)
        consumed_hosts.add(ip)

    # --- pass 2: suggestion match for NetBox devices with no serial --------
    # Only devices whose NetBox serial is empty are eligible. A device that has
    # a serial but did not match one is an identity problem, not a candidate
    # for a weaker match.
    unclaimed_devices = [
        d
        for d in devices
        if d.id not in claimed_devices and normalise_serial(d.serial) is None
    ]
    by_hostname = {_short(d.name): d for d in unclaimed_devices if _short(d.name)}

    for ip, facts in host_by_ip.items():
        if ip in consumed_hosts or not facts.usable:
            continue
        if normalise_serial(facts.serial) is None:
            # The host has no serial either, so a suggestion would establish
            # nothing and there is no serial to fill. Leave it unmatched.
            continue

        device = None
        method = MatchMethod.UNMATCHED
        evidence = ""

        host_short = _short(facts.hostname)
        if host_short and host_short in by_hostname:
            candidate = by_hostname[host_short]
            if candidate.id not in claimed_devices:
                device = candidate
                method = MatchMethod.HOSTNAME_SUGGESTED
                evidence = f"hostname {facts.hostname!r} matched NetBox name {candidate.name!r}"

        if device is None:
            for candidate in unclaimed_devices:
                if candidate.id in claimed_devices:
                    continue
                if probe_ip_by_device.get(candidate.id) == ip:
                    device = candidate
                    method = MatchMethod.BMC_IP_SUGGESTED
                    evidence = f"BMC IP {ip} is the OOB address of NetBox device {candidate.id}"
                    break

        if device is None:
            continue

        result.pairs.append(
            DevicePair(
                netbox=device,
                host=facts,
                match_method=method,
                match_evidence=evidence,
            )
        )
        claimed_devices.add(device.id)
        consumed_hosts.add(ip)
        LOG.info(
            "suggested match for device %s via %s - requires explicit approval",
            device.id,
            method.value,
        )

    # --- leftovers: hosts seen but not represented in NetBox ---------------
    for ip, facts in host_by_ip.items():
        if ip in consumed_hosts:
            continue
        if not facts.usable:
            result.collection_failures.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.COLLECTION_FAILED,
                    identifier=ip,
                    detail="; ".join(facts.errors) or facts.status.value,
                )
            )
            continue
        if normalise_serial(facts.serial) is None:
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.MISSING_SERIAL,
                    identifier=ip,
                    detail="BMC responded but reported no serial; no automatic match, no writes",
                )
            )
        else:
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.ON_HOST_NOT_IN_NETBOX,
                    identifier=str(facts.serial),
                    detail=f"serial reported by BMC {ip} has no NetBox device",
                )
            )

    # --- leftovers: NetBox devices with nothing collected ------------------
    for device in devices:
        if device.id in claimed_devices:
            continue
        probe_ip = probe_ip_by_device.get(device.id)
        if not probe_ip:
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.NO_BMC_IP,
                    identifier=device.name or f"device-{device.id}",
                    detail="no OOB or primary IP recorded in NetBox; Redfish cannot be reached",
                    device_id=device.id,
                    netbox_url=device.url,
                )
            )
            continue

        facts = host_by_ip.get(probe_ip)
        if facts is None or not facts.usable:
            detail = "; ".join(facts.errors) if facts else "no data collected"
            status = facts.status if facts else CollectionStatus.FAILED
            result.collection_failures.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.COLLECTION_FAILED,
                    identifier=device.name or f"device-{device.id}",
                    detail=f"{status.value} at {probe_ip}: {detail}",
                    device_id=device.id,
                    netbox_url=device.url,
                )
            )
        else:
            result.unmatched.append(
                UnmatchedEntry(
                    reason=UnmatchedReason.IN_NETBOX_NOT_ON_HOST,
                    identifier=device.name or f"device-{device.id}",
                    detail=(
                        f"BMC {probe_ip} responded with serial {facts.serial!r} which does not "
                        f"match NetBox serial {device.serial!r}"
                    ),
                    device_id=device.id,
                    netbox_url=device.url,
                )
            )

    # Devices quarantined for duplicate serials are already reported above;
    # make sure they never leak into the pair list.
    result.pairs = [p for p in result.pairs if p.netbox.id in devices_by_id]
    return result


def probe_address(device: NetBoxDevice) -> str | None:
    """The address to try Redfish on: OOB first, then the primary IP."""
    for candidate in (device.oob_ip, device.primary_ip4):
        if candidate:
            return candidate.split("/", 1)[0].strip() or None
    return None
