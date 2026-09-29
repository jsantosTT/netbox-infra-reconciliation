"""Redfish collector - the primary source for server facts.

Discovery walks /redfish/v1 rather than assuming fixed paths, because the
Galaxy generations and LoudBox are not confirmed to expose identical service
trees. Anything not found stays None, which can never produce a write.

No SSH, no KVM, no shell scraping.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

import requests

from ..config import BmcSettings
from ..models import CollectionStatus, HostFacts, TrayFacts

LOG = logging.getLogger("nbrecon.redfish")

ROOT = "/redfish/v1"

# Chassis entries matching these hints are treated as accelerator trays rather
# than the enclosing chassis.
TRAY_HINTS = ("tray", "ubb", "baseboard", "accel")


class RedfishCollector:
    def __init__(self, settings: BmcSettings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        if not settings.verify_tls:
            # Lab-only. Warn loudly and suppress the per-request urllib3 noise
            # so the single warning below is actually visible in the log.
            LOG.warning(
                "BMC TLS verification is DISABLED (NBRECON_BMC_VERIFY_TLS=false). "
                "This is permitted for lab sites only; NetBox, Jira and Grafana "
                "are always verified."
            )
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    # --- plumbing ---------------------------------------------------------
    def _get(self, bmc_ip: str, path: str) -> dict[str, Any] | None:
        url = f"https://{bmc_ip}{path}"
        try:
            resp = self.session.get(
                url,
                auth=(self.settings.user, self.settings.password),
                headers={"Accept": "application/json"},
                timeout=self.settings.timeout,
                verify=self.settings.verify_tls,
            )
        except requests.RequestException as exc:
            LOG.debug("redfish GET %s failed: %s", url, exc)
            return None
        if resp.status_code >= 400:
            LOG.debug("redfish GET %s returned %s", url, resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            LOG.debug("redfish GET %s returned non-JSON", url)
            return None

    def _members(self, bmc_ip: str, collection_path: str) -> list[str]:
        payload = self._get(bmc_ip, collection_path)
        if not payload:
            return []
        return [
            m["@odata.id"]
            for m in payload.get("Members", [])
            if isinstance(m, dict) and m.get("@odata.id")
        ]

    # --- collection -------------------------------------------------------
    def collect(self, bmc_ip: str) -> HostFacts:
        """Read one BMC. Never raises; failures are recorded on the result."""
        facts = HostFacts(bmc_ip=bmc_ip, sources=["redfish"])

        if not self.settings.configured:
            facts.status = CollectionStatus.FAILED
            facts.errors.append("BMC credentials not configured")
            return facts

        root = self._get(bmc_ip, ROOT + "/")
        if root is None:
            facts.status = CollectionStatus.UNREACHABLE
            facts.errors.append(f"Redfish service root unreachable at {bmc_ip}")
            return facts

        self._collect_system(bmc_ip, facts)
        self._collect_chassis(bmc_ip, facts)
        self._collect_manager_network(bmc_ip, facts)

        if facts.serial is None and facts.model is None:
            facts.status = CollectionStatus.FAILED
            facts.errors.append("Redfish reachable but returned no system identity")
        elif facts.errors:
            facts.status = CollectionStatus.PARTIAL
        return facts

    def _collect_system(self, bmc_ip: str, facts: HostFacts) -> None:
        members = self._members(bmc_ip, f"{ROOT}/Systems")
        if not members:
            facts.errors.append("no /redfish/v1/Systems members")
            return
        payload = self._get(bmc_ip, members[0])
        if not payload:
            facts.errors.append(f"could not read system {members[0]}")
            return
        facts.serial = _clean(payload.get("SerialNumber"))
        facts.model = _clean(payload.get("Model"))
        facts.part_number = _clean(payload.get("PartNumber")) or _clean(payload.get("SKU"))
        facts.asset_tag = _clean(payload.get("AssetTag"))
        facts.power_state = _clean(payload.get("PowerState"))
        facts.hostname = _clean(payload.get("HostName"))

    def _collect_chassis(self, bmc_ip: str, facts: HostFacts) -> None:
        members = self._members(bmc_ip, f"{ROOT}/Chassis")
        if not members:
            facts.errors.append("no /redfish/v1/Chassis members")
            return
        for path in members:
            payload = self._get(bmc_ip, path)
            if not payload:
                continue
            chassis_id = str(payload.get("Id") or path.rsplit("/", 1)[-1])
            if _looks_like_tray(chassis_id, payload):
                facts.trays.append(
                    TrayFacts(
                        tray_id=chassis_id,
                        serial=_clean(payload.get("SerialNumber")),
                        model=_clean(payload.get("Model")),
                        part_number=_clean(payload.get("PartNumber")),
                        source="redfish",
                    )
                )
                continue
            # Enclosing chassis: fills gaps the system resource left behind.
            if facts.chassis_revision is None:
                facts.chassis_revision = _clean(payload.get("Version")) or _clean(
                    payload.get("PartNumber")
                )
            if facts.serial is None:
                facts.serial = _clean(payload.get("SerialNumber"))
            if facts.model is None:
                facts.model = _clean(payload.get("Model"))

    def _collect_manager_network(self, bmc_ip: str, facts: HostFacts) -> None:
        managers = self._members(bmc_ip, f"{ROOT}/Managers")
        if not managers:
            facts.errors.append("no /redfish/v1/Managers members")
            return
        for manager_path in managers:
            interfaces = self._members(bmc_ip, f"{manager_path}/EthernetInterfaces")
            for iface_path in interfaces:
                payload = self._get(bmc_ip, iface_path)
                if not payload:
                    continue
                addresses = payload.get("IPv4Addresses") or []
                addr = None
                if isinstance(addresses, list) and addresses:
                    first = addresses[0]
                    if isinstance(first, dict):
                        addr = _clean(first.get("Address"))
                mac = _clean(payload.get("MACAddress"))
                # Prefer the interface the tool actually reached the BMC on.
                if addr == bmc_ip:
                    facts.bmc_ipv4 = addr
                    facts.bmc_mac = mac
                    return
                if facts.bmc_ipv4 is None and addr:
                    facts.bmc_ipv4 = addr
                    facts.bmc_mac = mac
        if facts.bmc_ipv4 is None:
            facts.errors.append("no BMC IPv4 address found on any manager interface")


def _clean(value: Any) -> str | None:
    """Normalise a Redfish value.

    Redfish commonly returns placeholders for unpopulated fields. Those mean
    "not determined", so they become None and are therefore never written.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in ("n/a", "na", "none", "null", "unknown", "not available", "to be filled"):
        return None
    if set(text) == {"0"} or text.lower().startswith("to be filled by o.e.m"):
        return None
    return text


def _looks_like_tray(chassis_id: str, payload: dict[str, Any]) -> bool:
    haystack = f"{chassis_id} {payload.get('ChassisType', '')} {payload.get('Name', '')}".lower()
    return any(hint in haystack for hint in TRAY_HINTS)
