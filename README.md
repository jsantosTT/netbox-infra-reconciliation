# netbox-infra-reconciliation

`nbrecon` reconciles physical server inventory against NetBox so NetBox can be
trusted as the source of truth.

It reads servers over Redfish and a tt-smi facts export, compares what it finds
against NetBox using an explicit field ownership matrix, and produces a dry-run
plan. Nothing is written until a human approves that plan. Every applied run is
verified and audited.

**Scope of v1: servers only** (Galaxy chassis and trays). Network devices are a
later phase; the collection research is preserved in
[docs/future/napalm-switches.md](docs/future/napalm-switches.md).

## Safety model

These properties are enforced in code and covered by tests, not left to
convention.

- **Dry-run by default.** `apply` refuses to run without an approval file whose
  SHA-256 digest matches the plan being applied.
- **Unknown is not empty.** A value the collectors could not determine is
  `None`, and `None` never produces a write. A failed or partial collection can
  never clear a NetBox field.
- **Serial is the only identity.** Hostname and BMC IP produce suggestions
  only, and every write on a suggested match needs explicit per-field approval.
  A missing or duplicated serial means no automatic match and no writes.
- **Human-only fields have no write path.** Status, owner, assignment, support,
  server function, exabox, site, rack and U position are never written.
- **Fill-if-empty never overwrites.** Serial, asset tag and the IPT link are
  written only into an empty value; a differing value is flagged for a human.
- **Nothing is created.** Device types, manufacturers, custom fields, IPAM
  objects and interfaces must already exist. Missing targets are reported.
- **Staleness check.** A device edited in NetBox after the snapshot is skipped
  and re-diffed on the next run.
- **Small batches.** A run is capped at 20 devices by default and an unscoped
  run is refused outright.
- **TLS.** Verification is always on for NetBox, Jira and Grafana, and cannot
  be disabled. Only BMC verification is configurable, for lab self-signed
  certificates, and it warns on every run.
- **No SSH, no KVM.** Server facts come from Redfish and from an Ansible
  export that the tool reads as a file.

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

Or build the container, which never contains secrets:

```bash
docker build -t nbrecon .
docker run --rm --env-file .env -v "$PWD/var:/var/lib/nbrecon" nbrecon --help
```

## Configure

```bash
cp .env.example .env
chmod 600 .env
```

`.env` is gitignored and must never be baked into an image. Mount it at runtime
with `--env-file`.

Then discover the NetBox custom field names, which this tool never creates:

```bash
nbrecon preflight --show-custom-fields
```

Copy the relevant names into `config/netbox_fields.yaml`. Any field left blank
is skipped entirely rather than guessed.

| File | Purpose | Ships as |
| --- | --- | --- |
| `config/ownership.yaml` | The field ownership matrix the diff engine executes | Complete |
| `config/netbox_fields.yaml` | Logical field key to real NetBox custom field name | Blank; fill after preflight |
| `config/sku_map.yaml` | Redfish Model/PartNumber to NetBox device type slug | Empty; every SKU reports as unmapped |
| `config/scope.example.yaml` | Template for a run scope | Copy and edit per site |

## Rehearse

Before pointing the tool at real inventory, run the whole pipeline against a
mock NetBox and a mock Redfish BMC holding one deliberately drifted server. No
credentials, nothing real is touched.

```bash
./tools/rehearse.sh            # the full pipeline, stage by stage
./tools/rehearse.sh --safety   # the same lab, as pass/fail assertions
```

It asks for `sudo` once, because Redfish is addressed as
`https://<bmc-ip>/redfish/v1` and the mock BMC therefore has to hold port 443.

`--safety` asserts the invariants above and exits non-zero on any failure. It
loads `config/ownership.yaml` from the repo rather than a fixture copy, so it
is the check to run after editing the ownership matrix. See
[docs/running-one-server.md](docs/running-one-server.md) for the walkthrough.

## Run

```bash
cp config/scope.example.yaml config/scope.lab.yaml   # edit: site, max_devices

nbrecon preflight
nbrecon collect --scope-file config/scope.lab.yaml --ansible-facts exports/tt-smi.json
nbrecon plan
nbrecon approve --approver ericson
nbrecon apply
nbrecon verify
```

Each command defaults to the most recent run; pass `--run-id` to target a
specific one. Artifacts land in `var/runs/<run-id>/`:

| File | Stage |
| --- | --- |
| `collection.json` | Raw reads from every source |
| `pairs.json` | Correlated device pairs |
| `plan.json` | The dry-run plan |
| `report.md` | Human-readable report |
| `approval.json` | Decisions, approver, timestamp, plan digest |
| `apply.json` | What was written, skipped or failed |
| `jira.json` | IPT linking and creation outcomes |
| `verify.json` | Post-apply confirmation |
| `run.log` | Full log for the run |

### Jira IPT tickets

Ticket creation is off by default. With `--create-ipt`, a ticket is created
only when the SN-field search *and* the text fallback both return nothing.
Anything found by text search is a candidate that a human confirms. Keep the
flag off until the service account's permissions are confirmed; duplicate
tickets are the expected failure mode.

### Pilot sequence

1. One lab site, a handful of devices, `nbrecon plan` only.
2. Approve one device and apply it. Verify.
3. Widen to a full small batch with `--create-ipt` still off.
4. Enable `--create-ipt` once Jira permissions and field IDs are confirmed.
5. Move to production only after several clean runs.

## Development

```bash
./.venv/bin/python -m pytest
./.venv/bin/ruff check src tests tools
```

The test suite pins the safety invariants: unknown never clears, fill-if-empty
never overwrites, human-only fields are unwritable, suggested matches always
require explicit approval, the batch cap and staleness check hold, and a
hand-edited plan cannot smuggle a forbidden field past the apply stage.

## Documentation

- [docs/running-one-server.md](docs/running-one-server.md) - start here: install, configure, rehearse, then reconcile one server
- [docs/pipeline.md](docs/pipeline.md) - the seven stages and what each guarantees
- [docs/field-ownership.md](docs/field-ownership.md) - the matrix and its open items
- [docs/ansible-facts-schema.md](docs/ansible-facts-schema.md) - the tt-smi export contract
- [docs/future/napalm-switches.md](docs/future/napalm-switches.md) - network phase research

## Open items

- NetBox custom field names are unmapped until `preflight` output is pasted into
  `config/netbox_fields.yaml`.
- `config/sku_map.yaml` is empty, so device type writes do not happen yet.
- Jira SN and NetBox URL custom field IDs, and service-account create
  permission, are unconfirmed.
- The tray model in NetBox is undecided, so trays are collect-and-report only.
- Whether LoudBox exposes Redfish at all is unconfirmed.
