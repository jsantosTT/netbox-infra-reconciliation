# Field ownership

`config/ownership.yaml` is the executable form of the agreed field ownership
matrix. The diff engine has no per-field logic of its own, so changing a
category in that file changes what the tool is allowed to write.

## Categories

| Category | Meaning |
| --- | --- |
| `host_owned` | Tool writes the value read from the server, after approval |
| `tool_owned` | Tool creates and maintains the value itself |
| `human_only` | Tool never writes; may suggest in the report |
| `report_only` | Tool compares and flags; never writes or suggests |

Only `host_owned` and `tool_owned` have a write path. A `human_only` or
`report_only` field with a write policy is rejected when the config loads.

## Policies

| Policy | Behaviour |
| --- | --- |
| `write` | Propose whenever the collected value differs; the host wins |
| `fill_if_empty` | Propose only into an empty NetBox value; a differing value is flagged |
| `never` | No write path exists |

## Current matrix

### Mandatory

| Field | Category | Source | Action | On conflict |
| --- | --- | --- | --- | --- |
| Hostname | report_only | NetBox name vs Redfish HostName vs Prometheus node | Flag if sources disagree | Human decides, never auto-fixed |
| Serial | host_owned | Redfish `SerialNumber` | Fill only if empty | Identity problem, unmatched report, no diff |
| Device type (TT SKU) | host_owned | Redfish Model / PartNumber via SKU map | Set after approval | Unmapped SKU reported; never creates device types |
| IPT link in NetBox | tool_owned | Jira issue URL | Set if empty | Different ticket, flagged, not overwritten |

### Non-mandatory

| Field | Category | Source | Action | On conflict |
| --- | --- | --- | --- | --- |
| Asset tag | host_owned | Redfish `AssetTag` | Fill only if empty | Flagged, not overwritten |

### Host details (signed off as host-owned for v1)

| Field | Category | Source |
| --- | --- | --- |
| BMC / OOB IP | host_owned | Redfish `Managers/*/EthernetInterfaces` |
| BMC MAC | host_owned | Redfish `Managers/*/EthernetInterfaces` |
| Chassis revision | host_owned | Redfish chassis, tt-smi fallback |
| Flash / FW / KMD / SMI version | host_owned | tt-smi via the Ansible export |
| Topology | host_owned | tt-smi via the Ansible export |

These are the rows the source matrix starred as pending team sign-off. They are
configured as host-owned. To hold any of them back, change its `category` to
`report_only` and its `policy` to `never`.

### Tool-owned bookkeeping

| Field | Behaviour |
| --- | --- |
| Last reconciled | Timestamp plus run ID, written only alongside another approved change, and only if the custom field is mapped |

It is never offered for approval on its own, so a device with no approved
changes is not touched just to update a timestamp.

### Human-only

Status, Owner, Assignment, Support, Server Function, Exabox, Site, Rack, and U
position. Never written.

Status is the one human-only field with a suggestion rule: after three
consecutive unreachable runs the report suggests considering Offline.

### Report-only

Power state is shown as a liveness hint and never stored.

### Trays

Tray serials are collected and reported but never written. Modules, device bays
and inventory items are not modelled in NetBox yet, and the tool does not create
them. The rule ships with `enabled: false`. Once the tray model is agreed, give
it a real target and enable it.

## Rules applied to every field

1. **Unknown is not empty.** A failed or partial collection never clears a
   NetBox field.
2. Host-owned fields are written only after approval of a reviewed plan, and
   only if the device was not edited after the snapshot. Otherwise the device is
   skipped and re-diffed next run.
3. Human-only and report-only fields are never written.
4. Fill-if-empty fields are never overwritten when already set to a different
   value; they are flagged for a human decision.
5. Pilot guard rails: staging or one lab site first, small batches, IPT
   creation behind `--create-ipt`, every applied run tagged with a run ID in
   the NetBox changelog.

## Still open

- Exact NetBox custom field names. Run `nbrecon preflight --show-custom-fields`
  and fill `config/netbox_fields.yaml`. Unmapped fields are skipped entirely.
- The SKU mapping table in `config/sku_map.yaml` is empty, so every model
  currently reports as unmapped.
- Jira IPT custom field IDs for SN and NetBox URL, plus service-account create
  permission.
- Tray model in NetBox: modules, device bays, or child devices.
- Whether LoudBox exposes Redfish at all.
