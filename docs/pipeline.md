# Pipeline

Seven stages. Each writes an artifact the next one reads, so a run can be
inspected, archived and re-reviewed without re-querying any source system.

```
1. scope -> 2. collect -> 3. correlate -> 4. diff -> 5. dry-run report
                                                          |
                                      6. approve <--------+
                                            |
                                      7. apply -> verify -> audit
```

Everything before `apply` is read-only.

## 1. Scope

A run must be scoped. An unscoped invocation is refused so a stray command can
never target the whole fleet. Selectors: site, tenant, tags, explicit device
names, a host list file, or an Ansible group. They combine with AND.

`max_devices` in the scope file is additionally capped by `NBRECON_MAX_BATCH`
(default 20). Small batches during the pilot are deliberate.

## 2. Collect (read-only)

| Source | What it provides | Notes |
| --- | --- | --- |
| NetBox | Device snapshot including `last_updated` | The `last_updated` value anchors the staleness check at apply time |
| Redfish | Serial, model/SKU, asset tag, BMC IP/MAC, chassis and tray serials, power state | Primary source. Service tree is discovered, not assumed |
| Ansible export | tt-smi Flash/FW/KMD/SMI, topology, tray serials | Read from an exported file. The tool never opens an SSH session |
| Jira | IPT tickets by SN field, then by text | Optional; absent configuration disables IPT handling entirely |
| Prometheus | Node names only | Optional. A metrics outage degrades to an empty set and never fails a run |

BMC addresses come from NetBox: the OOB IP first, then the primary IP. A device
with neither is reported as `no_bmc_ip` because Redfish cannot reach it.

Anything a collector could not determine is `None`, which is distinct from an
empty string. `None` can never produce a write.

## 3. Correlate

Identity is the serial number and nothing else.

- A single serial match on both sides is an authoritative match.
- A NetBox device with an **empty** serial may be matched by hostname or BMC IP,
  but only as a *suggestion*. Every write on a suggested pair is marked as
  requiring explicit per-field approval and can never be bulk approved.
- A NetBox device that **has** a serial which does not match is an identity
  problem, not a candidate for a weaker match. It is reported and not diffed.
- A missing or duplicated serial, on either side, means no automatic match and
  no writes. Both records go to the unmatched report.

The unmatched report uses the categories from the flow diagram: in NetBox but
not found on host, on host but not in NetBox, no BMC IP, missing IPT, possible
IPT match, and collection failures.

## 4. Diff

The ownership matrix in `config/ownership.yaml` decides everything. See
[field-ownership.md](field-ownership.md).

Invariants enforced in code regardless of configuration:

- A `None` collected value never produces a write. A failed or partial
  collection can therefore never clear a NetBox field.
- `fill_if_empty` fields are written only into an empty value. A differing
  value is flagged for a human, never overwritten.
- Human-only and report-only fields have no write path at all.
- A field whose NetBox custom field name is unmapped is skipped, never guessed.
- Device types and manufacturers are never created. An unmapped or unknown SKU
  is reported.

## 5. Dry-run report

`plan.json` is the machine-readable artifact; `report.md` is the human one. The
console rendering groups proposed writes by device and separates flags,
suggestions and report-only rows.

## 6. Approve

Interactive CLI review, per device and per field. Options per device are
`all`, `none`, `each` and `quit`. Fields marked with `*` are always asked
individually even inside a device the reviewer accepted wholesale.

The approval file records the approver, timestamp, every decision including
rejections, and a SHA-256 digest of the exact plan that was reviewed. A plan
regenerated or edited after approval cannot be applied with a stale sign-off.

## 7. Apply, verify, audit

Apply, in order:

1. The approval digest must match the plan.
2. Only approved fields are considered.
3. Each field is re-checked against the ownership matrix. A hand-edited plan
   cannot smuggle a human-only field through.
4. The batch cap limits how many devices one run may touch.
5. Each device is re-read; if `last_updated` moved since the snapshot the
   device is skipped and re-diffed on the next run.
6. The PATCH body contains only the approved fields.

Targets that must already exist, because the tool does not create them: device
types, manufacturers, custom fields, IPAM IP address objects, and device
interfaces. If one is missing the field is skipped with an explanation.

Jira runs after NetBox, and only for devices that actually changed. Ticket
creation requires `--create-ipt` *and* both searches returning nothing. The SN
and NetBox URL fields are fill-if-empty; a differing value is flagged.

Verify re-reads NetBox and Jira and confirms every applied value is present.

Audit lands in three places: the run directory on disk, the local run-history
database, and a NetBox journal entry on each changed device carrying the run
ID.

## Run history

The one rule that spans runs is the status suggestion: after three consecutive
unreachable runs the report suggests considering Offline. The tool never writes
status. History lives in a local SQLite file, `var/run-history.db` by default.
