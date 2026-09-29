# tt-smi facts export schema

`nbrecon` never opens an SSH session. An Ansible playbook runs `tt-smi` on the
hosts and writes the result to a file; the tool only reads that file. This
document is the contract between the playbook and the tool.

Pass the export with `--ansible-facts <path>`, pointing at either a file or a
directory.

## Layout 1: single file

```json
{
  "hosts": {
    "gx-lab-01": {
      "groups": ["galaxy", "lab"],
      "tt_smi": {
        "flash_version": "1.2.3",
        "fw_version": "4.5.6",
        "kmd_version": "7.8.9",
        "smi_version": "2.0.0",
        "topology": "mesh",
        "chassis_revision": "RevB",
        "serial": "SN-AAA-111",
        "trays": [
          { "tray_id": "0", "serial": "TRAY-0001", "part_number": "TG-00002" }
        ]
      }
    }
  }
}
```

## Layout 2: directory, one file per host

Ansible fact-cache style. One `<hostname>.json` per host, containing either the
inner object or a `tt_smi` key:

```json
{
  "hostname": "gx-lab-01",
  "groups": ["galaxy"],
  "tt_smi": { "fw_version": "4.5.6" }
}
```

## Field aliases

The reader accepts common variants so the playbook does not have to match
exactly:

| Canonical | Also accepted |
| --- | --- |
| `flash_version` | `flash`, `fw_flash` |
| `fw_version` | `firmware_version`, `fw` |
| `kmd_version` | `kmd` |
| `smi_version` | `smi`, `tt_smi_version` |
| `topology` | `topo` |
| `chassis_revision` | `board_revision`, `revision` |
| `serial` | `serial_number`, `board_serial` |

## Precedence

Redfish stays authoritative for anything it already returned. The export fills
gaps and supplies the fields Redfish does not expose at all: Flash, FW, KMD, SMI
and topology.

A missing host, a missing key, or an empty value all mean "not determined". The
field stays `None` and nothing is written for it. A stale or absent export can
never clear a NetBox value.

Hostnames are matched on the short name, so an FQDN in the export still matches
a short name in NetBox.

## Groups

`groups` is what `scope.ansible_group` filters against. It is only needed if a
scope selects devices by Ansible group; a scope doing so without
`--ansible-facts` is rejected rather than falling back to SSH.
