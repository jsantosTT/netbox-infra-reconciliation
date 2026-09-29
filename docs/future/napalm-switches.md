# Future phase: network device reconciliation

Not implemented. `nbrecon` v1 covers servers only. This file preserves the
collection research for the network phase so it is not lost; nothing here is
wired into the tool yet.

The same pipeline shape applies when this phase starts: collect read-only,
correlate on a stable identity, diff against a field ownership matrix, dry-run,
approve, apply, verify, audit. Collectors change; the safety model does not.

## Collection approach

Avoid scraping SSH shells or KVM consoles. Use NAPALM for network
infrastructure and standard Redfish REST APIs for OpenBMC servers.

```
+-------------------------------------------------------------------------+
|                  Collector Container / VM (Python)                      |
|                                                                         |
|      [NAPALM Drivers]                      [Redfish REST / requests]    |
+--------------|-----------------------------------------|----------------+
               | (NETCONF / eAPI / SSH)                  | (HTTPS JSON)
               v                                         v
   +-----------------------+                 +-----------------------+
   | Network Switches      |                 | OpenBMC / Servers     |
   | (Arista, Cisco, etc.) |                 | (Chassis, Trays, BMC) |
   +-----------------------+                 +-----------------------+
```

## Network devices: NAPALM

NAPALM pulls structured facts without parsing terminal regex.

- `get_facts()`: device model, vendor OS, serial number, hostname, FQDN.
- `get_interfaces()`: physical and logical ports, MAC addresses, enabled and
  operational status, speed, MTU.
- `get_interfaces_ip()`: configured interface IP addresses and subnet masks.
- `get_lldp_neighbors()`: peer hostnames and interface names, which maps
  switch-to-switch and switch-to-server cabling.

NAPALM abstracts vendor syntax differences across Cisco IOS-XE/NX-OS, Arista
EOS and Junos, returning identical Python dictionaries.

## FS switches

FSOS has no native NAPALM driver, but the CLI is IOS-like so the `ios` driver
often works for basic getters in lab testing. Do not rely on it for production
until validated against the exact FSOS/PicOS firmware.

- Test the NAPALM `ios` driver for `get_facts`, `get_interfaces`,
  `get_interfaces_ip`, `get_lldp_neighbors`.
- Use SNMPv3 with ENTITY-MIB, IF-MIB, IP-MIB and LLDP-MIB for model, serial,
  ports, MACs, IPs and neighbours.
- Use Netmiko with ntc-templates as a fallback where SNMP MIBs are incomplete,
  to keep parsing structured.

For FS, SNMP provides the same structured inventory without depending on CLI
compatibility.

## Domain summary

| Domain | Tool / protocol | Target endpoints | Output |
| --- | --- | --- | --- |
| Network switches | NAPALM (`get_facts`, `get_interfaces_ip`, `get_lldp_neighbors`) | Management IP over eAPI/NETCONF/SSH | Standardised Python dict |
| Servers / compute | Redfish REST API (`/redfish/v1/...`) | BMC management IP over HTTPS:443 | DMTF standard JSON |

## Server collection reference (implemented in v1)

Kept here because the original notes covered both domains together.

| Redfish endpoint | Data extracted | Target NetBox model |
| --- | --- | --- |
| `/redfish/v1/Systems/system` | Model, serial, asset tag, host UUID, power state | `dcim.devices` |
| `/redfish/v1/Chassis/chassis` | Physical enclosure, height/dimensions, part number | `dcim.device_types`, `dcim.devices` |
| `/redfish/v1/Managers/bmc/EthernetInterfaces` | BMC MAC, management IP, subnet mask, gateway | `ipam.ip_addresses`, `dcim.interfaces` |
| `/redfish/v1/Chassis/chassis/Power` | PSU part numbers, serials, input wattage | `dcim.power_ports`, `dcim.power_outlets` |

For dense compute topologies with multiple accelerator trays in a single
multi-U frame:

1. **Chassis and management node**: queried via OpenBMC Redfish on the chassis
   or host BMC for the main system serial, management IP and power feeds.
2. **UBB / accelerator trays**: queried through subordinate Redfish chassis
   endpoints (`/redfish/v1/Chassis/<tray_id>`) or via a host execution pass
   (`tt-smi`, `dmidecode`) run by Ansible, to extract individual tray serials.

## Superseded guidance

The original notes ended with "pass structured fields directly into
`pynetbox`". That is superseded. Collection never writes to NetBox. Everything
goes through the diff, dry-run, approval and audit pipeline described in
[../pipeline.md](../pipeline.md).
