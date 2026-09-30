#!/usr/bin/env python3
"""A fake NetBox and a fake Redfish BMC, for rehearsing a run end to end.

This exists so an operator can watch collect -> plan -> approve -> apply ->
verify behave on a single server before pointing nbrecon at real inventory.
Nothing here is used by the tool at runtime; it is a test fixture with a
network interface.

The fake NetBox is stateful: a PATCH really does change the device and bump
last_updated, so the staleness guard and the verify stage exercise the same
code paths they would in production.

    ./tools/mock_lab.py --serve-netbox --port 8000
    sudo ./tools/mock_lab.py --serve-bmc --port 443

Redfish is addressed as https://<bmc-ip>/redfish/v1, so the BMC side has to
hold port 443 and therefore needs root. Run it with NBRECON_BMC_VERIFY_TLS
set to false; the certificate is self-signed on startup.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

NETBOX_TOKEN = "rehearsal-netbox-token"  # noqa: S105 - fixture credential
BMC_USER = "rehearsal"
BMC_PASSWORD = "rehearsal"  # noqa: S105 - fixture credential

DEVICE_ID = 101
DEVICE_NAME = "galaxy-lab-01"
SERIAL = "TT-GX-0001"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --- the scenario ---------------------------------------------------------
# One server whose NetBox record has drifted in several different ways at
# once, so a single rehearsal covers every branch of the ownership matrix:
#
#   serial      matches          -> correlates cleanly, no write proposed
#   asset_tag   empty in NetBox  -> fill_if_empty writes
#   device_type stale generation -> host wins, but only via the SKU map
#   bmc_ip      equal modulo /32 -> compared as an address, no write
#   bmc_mac     empty interface  -> written on the interface, not the device
#   fw_flash    stale value      -> host wins, overwritten
#   kmd/smi/... empty            -> filled from the tt-smi export
#   owner       set by a human   -> human_only, never touched
#   server_function empty        -> reported for a human, never filled
#   power_state / trays          -> reported only, no NetBox target exists


def initial_state() -> dict[str, Any]:
    return {
        "device": {
            "id": DEVICE_ID,
            "name": DEVICE_NAME,
            "serial": SERIAL,
            "asset_tag": None,
            "device_type": {
                "id": 7,
                "slug": "tt-galaxy-wormhole",
                "model": "TT Galaxy (Wormhole)",
            },
            "site": {"id": 1, "slug": "lab-aus", "name": "Lab AUS"},
            "rack": {"id": 12, "name": "RACK-42"},
            "status": {"value": "active", "label": "Active"},
            "primary_ip4": None,
            "oob_ip": {"id": 55, "address": "127.0.0.1/32"},
            "display_url": f"http://127.0.0.1:8000/dcim/devices/{DEVICE_ID}/",
            "last_updated": "2026-09-20T10:00:00.123456Z",
            "tags": [{"id": 3, "slug": "pilot", "name": "pilot"}],
            "custom_fields": {
                "chassis_revision": None,
                "flash_version": "1.2.0",
                "fw_version": None,
                "kmd_version": None,
                "smi_version": None,
                "topology": None,
                "ipt_url": None,
                "last_reconciled": None,
                "owner": "dev-infra",
                "assignment": None,
                "support": None,
                "server_function": None,
                "exabox": None,
            },
        },
        "interfaces": [
            {"id": 31, "device": {"id": DEVICE_ID}, "name": "bmc", "mac_address": None},
        ],
        "ip_addresses": [
            {"id": 55, "address": "127.0.0.1/32"},
        ],
        "device_types": [
            {"id": 7, "slug": "tt-galaxy-wormhole", "model": "TT Galaxy (Wormhole)"},
            {"id": 9, "slug": "tt-galaxy-blackhole", "model": "TT Galaxy (Blackhole)"},
        ],
        # Two sites share a rack name, so scoping by an ambiguous rack can be
        # rehearsed as well as the happy path.
        "racks": [
            {"id": 12, "name": "RACK-42", "site": {"id": 1, "slug": "lab-aus"}},
            {"id": 13, "name": "RACK-42", "site": {"id": 2, "slug": "lab-tor"}},
            {"id": 14, "name": "RACK-07", "site": {"id": 1, "slug": "lab-aus"}},
        ],
        "journal": [],
    }


CUSTOM_FIELD_DEFS = [
    ("chassis_revision", "Chassis revision"),
    ("flash_version", "Flash version"),
    ("fw_version", "FW version"),
    ("kmd_version", "KMD version"),
    ("smi_version", "SMI version"),
    ("topology", "Topology"),
    ("ipt_url", "IPT URL"),
    ("last_reconciled", "Last reconciled"),
    ("owner", "Owner"),
    ("assignment", "Assignment"),
    ("support", "Support"),
    ("server_function", "Server function"),
    ("exabox", "Exabox"),
]


REDFISH: dict[str, dict[str, Any]] = {
    "/redfish/v1/": {
        "@odata.id": "/redfish/v1/",
        "Id": "RootService",
        "Name": "Root Service",
        "RedfishVersion": "1.13.0",
        "Systems": {"@odata.id": "/redfish/v1/Systems"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"},
    },
    "/redfish/v1/Systems": {
        "Members@odata.count": 1,
        "Members": [{"@odata.id": "/redfish/v1/Systems/system"}],
    },
    "/redfish/v1/Systems/system": {
        "@odata.id": "/redfish/v1/Systems/system",
        "Id": "system",
        "Name": "System",
        "SerialNumber": SERIAL,
        "Model": "Galaxy Blackhole",
        "PartNumber": "TT-BH-GALAXY-4U",
        "SKU": "TT-BH-GALAXY-4U",
        "AssetTag": "TT-ASSET-9911",
        "PowerState": "On",
        "HostName": DEVICE_NAME,
        "Status": {"State": "Enabled", "Health": "OK"},
    },
    "/redfish/v1/Chassis": {
        "Members@odata.count": 5,
        "Members": [
            {"@odata.id": "/redfish/v1/Chassis/chassis"},
            {"@odata.id": "/redfish/v1/Chassis/Tray1"},
            {"@odata.id": "/redfish/v1/Chassis/Tray2"},
            {"@odata.id": "/redfish/v1/Chassis/Tray3"},
            {"@odata.id": "/redfish/v1/Chassis/Tray4"},
        ],
    },
    "/redfish/v1/Chassis/chassis": {
        "@odata.id": "/redfish/v1/Chassis/chassis",
        "Id": "chassis",
        "Name": "Galaxy Enclosure",
        "ChassisType": "RackMount",
        "SerialNumber": SERIAL,
        "Model": "Galaxy Blackhole",
        "PartNumber": "TT-BH-GALAXY-4U",
        "Version": "Rev C",
    },
    "/redfish/v1/Managers": {
        "Members@odata.count": 1,
        "Members": [{"@odata.id": "/redfish/v1/Managers/bmc"}],
    },
    "/redfish/v1/Managers/bmc": {
        "@odata.id": "/redfish/v1/Managers/bmc",
        "Id": "bmc",
        "Name": "OpenBMC Manager",
        "ManagerType": "BMC",
        "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces"},
    },
    "/redfish/v1/Managers/bmc/EthernetInterfaces": {
        "Members@odata.count": 1,
        "Members": [{"@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0"}],
    },
    "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0": {
        "@odata.id": "/redfish/v1/Managers/bmc/EthernetInterfaces/eth0",
        "Id": "eth0",
        "Name": "Manager Ethernet Interface",
        "MACAddress": "aa:bb:cc:dd:ee:01",
        "IPv4Addresses": [
            {"Address": "127.0.0.1", "SubnetMask": "255.255.255.255", "AddressOrigin": "Static"}
        ],
    },
}

for _i in range(1, 5):
    REDFISH[f"/redfish/v1/Chassis/Tray{_i}"] = {
        "@odata.id": f"/redfish/v1/Chassis/Tray{_i}",
        "Id": f"Tray{_i}",
        "Name": f"Accelerator Tray {_i}",
        "ChassisType": "Enclosure",
        "SerialNumber": f"TT-TRAY-000{_i}",
        "Model": "Blackhole Tray",
        "PartNumber": "TT-BH-TRAY",
    }


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: dict[str, Any] = {}
    mode = "netbox"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"  [{self.mode}] {fmt % args}\n")

    # --- helpers ----------------------------------------------------------
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, results: list[dict[str, Any]]) -> None:
        self._send(200, {"count": len(results), "next": None, "previous": None,
                         "results": results})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if self.mode == "netbox":
            return header == f"Token {NETBOX_TOKEN}"
        if not header.startswith("Basic "):
            return False
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        return decoded == f"{BMC_USER}:{BMC_PASSWORD}"

    # --- routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorised():
            self._send(403, {"detail": "Invalid credentials."})
            return
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        handler = self._netbox_get if self.mode == "netbox" else self._redfish_get
        handler(path, query)

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorised() or self.mode != "netbox":
            self._send(403, {"detail": "Invalid credentials."})
            return
        path = urlparse(self.path).path
        device = re.fullmatch(r"/api/dcim/devices/(\d+)/", path)
        if device:
            self._patch_device(int(device.group(1)), self._body())
            return
        iface = re.fullmatch(r"/api/dcim/interfaces/(\d+)/", path)
        if iface:
            self._patch_interface(int(iface.group(1)), self._body())
            return
        self._send(404, {"detail": "Not found."})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._authorised() or self.mode != "netbox":
            self._send(403, {"detail": "Invalid credentials."})
            return
        if urlparse(self.path).path == "/api/extras/journal-entries/":
            entry = {"id": len(self.state["journal"]) + 1, **self._body(),
                     "created": _now()}
            self.state["journal"].append(entry)
            self._send(201, entry)
            return
        self._send(404, {"detail": "Not found."})

    # --- fake NetBox ------------------------------------------------------
    def _netbox_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/status/":
            self._send(200, {"netbox-version": "4.1.3", "python-version": "3.12.3"})
            return

        if path == "/api/extras/custom-fields/":
            self._page([
                {
                    "id": i,
                    "name": name,
                    "label": label,
                    "type": {"value": "text", "label": "Text"},
                    "content_types": ["dcim.device"],
                }
                for i, (name, label) in enumerate(CUSTOM_FIELD_DEFS, start=1)
            ])
            return

        if path == "/api/dcim/devices/":
            self._page(self._filter_devices(query))
            return

        device = re.fullmatch(r"/api/dcim/devices/(\d+)/", path)
        if device:
            if int(device.group(1)) != DEVICE_ID:
                self._send(404, {"detail": "Not found."})
            else:
                self._send(200, self.state["device"])
            return

        if path == "/api/ipam/ip-addresses/":
            wanted = {a.split("/", 1)[0] for a in query.get("address", [])}
            self._page([
                ip for ip in self.state["ip_addresses"]
                if not wanted or ip["address"].split("/", 1)[0] in wanted
            ])
            return

        if path == "/api/dcim/interfaces/":
            device_ids = {int(v) for v in query.get("device_id", [])}
            names = set(query.get("name", []))
            self._page([
                i for i in self.state["interfaces"]
                if (not device_ids or i["device"]["id"] in device_ids)
                and (not names or i["name"] in names)
            ])
            return

        if path == "/api/dcim/racks/":
            names = set(query.get("name", []))
            sites = set(query.get("site", []))
            self._page([
                r for r in self.state["racks"]
                if (not names or r["name"] in names)
                and (not sites or r["site"]["slug"] in sites)
            ])
            return

        if path == "/api/dcim/device-types/":
            slugs = set(query.get("slug", []))
            self._page([
                dt for dt in self.state["device_types"]
                if not slugs or dt["slug"] in slugs
            ])
            return

        self._send(404, {"detail": "Not found."})

    def _filter_devices(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        device = self.state["device"]
        names = query.get("name")
        if names and device["name"] not in names:
            return []
        sites = query.get("site")
        if sites and device["site"]["slug"] not in sites:
            return []
        tags = query.get("tag")
        if tags and not set(tags).issubset({t["slug"] for t in device["tags"]}):
            return []
        rack_ids = query.get("rack_id")
        if rack_ids and str((device.get("rack") or {}).get("id")) not in rack_ids:
            return []
        return [device]

    def _patch_device(self, device_id: int, payload: dict[str, Any]) -> None:
        if device_id != DEVICE_ID:
            self._send(404, {"detail": "Not found."})
            return
        device = self.state["device"]
        for key, value in payload.items():
            if key == "custom_fields":
                device["custom_fields"].update(value)
            elif key == "device_type":
                match = next(
                    (dt for dt in self.state["device_types"] if dt["id"] == value), None
                )
                if not match:
                    self._send(400, {"device_type": ["Invalid pk."]})
                    return
                device["device_type"] = match
            elif key == "oob_ip":
                match = next(
                    (ip for ip in self.state["ip_addresses"] if ip["id"] == value), None
                )
                device["oob_ip"] = match
            else:
                device[key] = value
        device["last_updated"] = _now()
        self._send(200, device)

    def _patch_interface(self, interface_id: int, payload: dict[str, Any]) -> None:
        match = next(
            (i for i in self.state["interfaces"] if i["id"] == interface_id), None
        )
        if not match:
            self._send(404, {"detail": "Not found."})
            return
        match.update(payload)
        self._send(200, match)

    # --- fake Redfish -----------------------------------------------------
    def _redfish_get(self, path: str, _query: dict[str, list[str]]) -> None:
        payload = REDFISH.get(path) or REDFISH.get(path.rstrip("/")) or REDFISH.get(path + "/")
        if payload is None:
            self._send(404, {"error": {"code": "Base.1.0.ResourceMissing"}})
            return
        self._send(200, payload)


def _self_signed_cert() -> tuple[str, str]:
    """Generate a throwaway certificate for the mock BMC.

    No subjectAltName: the mock is only ever reached with
    NBRECON_BMC_VERIFY_TLS=false, and `-addext` does not exist in LibreSSL,
    which is what macOS ships as `openssl`.
    """
    if not shutil.which("openssl"):
        raise SystemExit(
            "openssl not found on PATH; it is needed to generate the mock BMC "
            "certificate. Install it, or run only the NetBox half with "
            "--serve-netbox."
        )

    tmp = Path(tempfile.mkdtemp(prefix="nbrecon-mock-bmc-"))
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    result = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=mock-bmc"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"openssl could not generate a certificate:\n{result.stderr}")
    return str(cert), str(key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve-netbox", action="store_true")
    group.add_argument("--serve-bmc", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-file", help="Persist fake NetBox state here")
    parser.add_argument(
        "--pid-file",
        help="Write the listener PID here. Shutting down by PID avoids pkill "
             "patterns that also match the shell invoking them.",
    )
    args = parser.parse_args()

    mode = "netbox" if args.serve_netbox else "bmc"
    port = args.port or (8000 if mode == "netbox" else 443)

    handler = type("Handler", (_Handler,), {"mode": mode, "state": initial_state()})
    try:
        server = ThreadingHTTPServer((args.host, port), handler)
    except PermissionError:
        raise SystemExit(
            f"not allowed to bind {args.host}:{port}. Ports below 1024 need "
            f"root; run the mock {mode} under sudo."
        ) from None
    except OSError as exc:
        raise SystemExit(
            f"could not bind {args.host}:{port}: {exc}. Something else is "
            "probably already listening there."
        ) from None

    if mode == "bmc":
        cert, key = _self_signed_cert()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)

    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()))

    scheme = "http" if mode == "netbox" else "https"
    print(f"mock {mode} listening on {scheme}://{args.host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if args.state_file and mode == "netbox":
            Path(args.state_file).write_text(json.dumps(handler.state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
