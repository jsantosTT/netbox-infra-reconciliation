"""NetBox REST client.

Reads are used for the snapshot and the staleness re-check; writes are narrow
and deliberate. The tool talks to the REST API directly rather than through an
ORM wrapper so that every PATCH body contains exactly the approved fields and
nothing else.

TLS verification is hard-coded on and there is no setting to disable it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urljoin

import requests

from ..config import NetBoxSettings, Scope
from ..errors import ApplyError, CollectionError, ScopeError
from ..models import NetBoxDevice

LOG = logging.getLogger("nbrecon.netbox")

PAGE_SIZE = 100
TIMEOUT = 30


class NetBoxClient:
    def __init__(self, settings: NetBoxSettings, session: requests.Session | None = None) -> None:
        if not settings.configured:
            raise CollectionError(
                "NetBox is not configured: set NBRECON_NETBOX_URL and NBRECON_NETBOX_TOKEN"
            )
        self.settings = settings
        self.base = settings.url.rstrip("/") + "/"
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {settings.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # --- plumbing ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(path)
        try:
            resp = self.session.get(url, params=params, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise CollectionError(f"NetBox GET {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise CollectionError(
                f"NetBox GET {url} returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        params = {**params, "limit": PAGE_SIZE}
        results: list[dict[str, Any]] = []
        payload = self._get(path, params)
        results.extend(payload.get("results", []))
        next_url = payload.get("next")
        while next_url:
            try:
                resp = self.session.get(next_url, timeout=TIMEOUT, verify=True)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise CollectionError(f"NetBox pagination failed at {next_url}: {exc}") from exc
            payload = resp.json()
            results.extend(payload.get("results", []))
            next_url = payload.get("next")
        return results

    # --- reads ------------------------------------------------------------
    def check(self) -> dict[str, Any]:
        """Verify connectivity and token validity."""
        return self._get("api/status/")

    def custom_field_definitions(self) -> list[dict[str, Any]]:
        """Custom fields defined on dcim.device.

        Used by ``preflight`` so the operator can fill netbox_fields.yaml with
        real names instead of guessing. The tool never creates custom fields.
        """
        for param in ("dcim.device", "device"):
            try:
                fields = self._paginate("api/extras/custom-fields/", {"content_types": param})
            except CollectionError:
                continue
            if fields:
                return fields
        # Fall back to listing everything and filtering client side, since the
        # content_types filter key has changed across NetBox versions.
        everything = self._paginate("api/extras/custom-fields/", {})
        return [
            f
            for f in everything
            if any("device" in str(ct) for ct in (f.get("content_types") or []))
        ]

    def device_url(self, device_id: int) -> str:
        return urljoin(self.base, f"dcim/devices/{device_id}/")

    def fetch_devices(self, scope: Scope) -> list[NetBoxDevice]:
        """Snapshot the devices in scope, including ``last_updated``."""
        explicit = scope.resolved_devices()
        raw: list[dict[str, Any]] = []

        if explicit:
            # NetBox accepts repeated name= parameters; send them in one query.
            raw = self._paginate("api/dcim/devices/", {"name": explicit})
            found = {d.get("name") for d in raw}
            for missing in explicit:
                if missing not in found:
                    LOG.warning("device %s from scope not found in NetBox", missing)
        else:
            params: dict[str, Any] = {}
            if scope.site:
                params["site"] = scope.site
            if scope.tenant:
                params["tenant"] = scope.tenant
            if scope.tags:
                params["tag"] = scope.tags
            if scope.rack:
                params["rack_id"] = self.resolve_rack_ids(scope.rack, scope.site)
            raw = self._paginate("api/dcim/devices/", params)

        devices = [self._to_device(d) for d in raw]
        devices.sort(key=lambda d: (d.name or "", d.id))
        return devices

    def fetch_device(self, device_id: int) -> NetBoxDevice:
        return self._to_device(self._get(f"api/dcim/devices/{device_id}/"))

    def resolve_rack_ids(self, name: str, site: str = "") -> list[int]:
        """Turn a rack name into IDs the device filter can use.

        Racks have no slug and their names are unique only within a site, so
        the name is resolved here instead of being handed to the device filter.
        An ID means the same thing on every NetBox version.
        """
        params: dict[str, Any] = {"name": name}
        if site:
            params["site"] = site
        racks = self._paginate("api/dcim/racks/", params)

        if not racks:
            where = f" at site {site!r}" if site else ""
            raise ScopeError(
                f"rack {name!r} not found in NetBox{where}; "
                "nothing was read and no devices were selected"
            )
        if len(racks) > 1:
            sites = sorted(
                str((r.get("site") or {}).get("slug") or "?") for r in racks
            )
            raise ScopeError(
                f"rack name {name!r} matches {len(racks)} racks across sites "
                f"({', '.join(sites)}); set 'site' in the scope file to choose one"
            )
        return [int(racks[0]["id"])]

    def find_ip_address(self, address: str) -> dict[str, Any] | None:
        """Look up an existing IPAM entry. The tool never creates IP objects."""
        results = self._paginate("api/ipam/ip-addresses/", {"address": address})
        return results[0] if results else None

    def find_interface(self, device_id: int, name: str) -> dict[str, Any] | None:
        results = self._paginate(
            "api/dcim/interfaces/", {"device_id": device_id, "name": name}
        )
        return results[0] if results else None

    def find_device_type(self, slug: str) -> dict[str, Any] | None:
        results = self._paginate("api/dcim/device-types/", {"slug": slug})
        return results[0] if results else None

    # --- writes -----------------------------------------------------------
    def patch_device(self, device_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload:
            raise ApplyError("refusing to PATCH a device with an empty payload")
        url = self._url(f"api/dcim/devices/{device_id}/")
        try:
            resp = self.session.patch(url, json=payload, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise ApplyError(f"NetBox PATCH {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ApplyError(f"NetBox PATCH {url} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def patch_interface(self, interface_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._url(f"api/dcim/interfaces/{interface_id}/")
        try:
            resp = self.session.patch(url, json=payload, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise ApplyError(f"NetBox PATCH {url} failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ApplyError(f"NetBox PATCH {url} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def create_journal_entry(self, device_id: int, comment: str) -> dict[str, Any]:
        url = self._url("api/extras/journal-entries/")
        payload = {
            "assigned_object_type": "dcim.device",
            "assigned_object_id": device_id,
            "kind": "info",
            "comments": comment,
        }
        try:
            resp = self.session.post(url, json=payload, timeout=TIMEOUT, verify=True)
        except requests.RequestException as exc:
            raise ApplyError(f"NetBox journal entry failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ApplyError(
                f"NetBox journal entry returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    # --- mapping ----------------------------------------------------------
    def _to_device(self, raw: dict[str, Any]) -> NetBoxDevice:
        device_type = raw.get("device_type") or {}
        site = raw.get("site") or {}
        status = raw.get("status") or {}
        primary_ip4 = raw.get("primary_ip4") or {}
        oob = raw.get("oob_ip") or {}
        rack = raw.get("rack") or {}
        return NetBoxDevice(
            id=int(raw["id"]),
            name=raw.get("name"),
            serial=(raw.get("serial") or None),
            asset_tag=raw.get("asset_tag"),
            device_type_slug=device_type.get("slug"),
            device_type_id=device_type.get("id"),
            site_slug=site.get("slug"),
            status=status.get("value") if isinstance(status, dict) else status,
            primary_ip4=primary_ip4.get("address") if isinstance(primary_ip4, dict) else None,
            oob_ip=oob.get("address") if isinstance(oob, dict) else None,
            url=raw.get("display_url") or self.device_url(int(raw["id"])),
            last_updated=raw.get("last_updated"),
            rack=rack.get("name") if isinstance(rack, dict) else None,
            custom_fields=raw.get("custom_fields") or {},
            tags=[t.get("slug", "") for t in (raw.get("tags") or []) if isinstance(t, dict)],
        )


def quote_name(name: str) -> str:
    return quote(name, safe="")
