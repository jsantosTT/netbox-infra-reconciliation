# Running nbrecon against one server

A walkthrough for the first real run: install, configure, rehearse against a
mock lab, then reconcile a single production server.

Do the rehearsal first. It takes a couple of minutes, needs no credentials, and
it is the only way to see the safety behaviour without risking a real record.

---

## 1. Install

On the management host:

```bash
git clone https://github.com/jsantosTT/netbox-infra-reconciliation.git
cd netbox-infra-reconciliation

python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
```

Python 3.10 or newer. Confirm it landed:

```bash
./.venv/bin/nbrecon --version
./.venv/bin/pytest          # 96 passed
```

The test suite is worth running once. It pins the safety invariants, so a green
run tells you the ownership rules in `config/ownership.yaml` are internally
consistent before you point the tool at anything.

### Container

The image never contains secrets; `.env` is mounted at runtime.

```bash
docker build -t nbrecon .
docker run --rm --env-file .env -v "$PWD/var:/var/lib/nbrecon" nbrecon --help
```

---

## 2. Rehearse against the mock lab

`tools/mock_lab.py` is a fake NetBox and a fake Redfish BMC holding one
deliberately drifted server. `tools/rehearse.sh` drives the whole pipeline
against them.

```bash
./tools/rehearse.sh
```

It asks for `sudo` once. Redfish is addressed as `https://<bmc-ip>/redfish/v1`
with no port component, so the mock BMC has to hold port 443.

The mock server is drifted in several different ways at once, so one rehearsal
covers most branches of the ownership matrix:

| Field | NetBox | Host | What should happen |
| --- | --- | --- | --- |
| serial | `TT-GX-0001` | `TT-GX-0001` | correlates, no write proposed |
| device type | `tt-galaxy-wormhole` | Blackhole part number | write, but only via `sku_map.yaml` |
| asset tag | empty | `TT-ASSET-9911` | filled |
| BMC IP | `127.0.0.1/32` | `127.0.0.1` | compared as an address, no write |
| BMC MAC | empty interface | `aa:bb:cc:dd:ee:01` | written on the interface, not the device |
| Flash version | `1.2.0` | `1.5.3` | overwritten, host wins |
| FW/KMD/SMI/topology | empty | from tt-smi | filled |
| owner | `dev-infra` | n/a | never touched |
| server function | empty | n/a | reported, never filled |
| power state, trays | n/a | from Redfish | reported, no NetBox target exists |

Expected result: **9 proposed writes, 9 applied, 9 verified.**

To poke at it by hand, leave the mocks running:

```bash
./tools/rehearse.sh --keep

NB=".venv/bin/nbrecon --env-file var/rehearsal/env --config-dir var/rehearsal/config"
$NB plan --show-all
```

### Rehearsals worth doing before you trust it

These are the four behaviours that matter most. Each one has been run against
the mock lab and behaves as described.

**A second run proposes nothing.** Reconciliation is idempotent; a run that
finds NetBox already correct produces an empty plan.

```
Correlated 1 pair(s); 0 unmatched, 0 collection failure(s)
│ Proposed writes     │     0 │
No changes. Nothing to approve.
```

**A failed collection never clears anything.** Stop the mock BMC, then collect
and plan again. The device drops out of correlation entirely and every
populated NetBox value survives untouched.

```
  0 reachable, 1 failed
│ Proposed writes     │     0 │
│ Collection failures │     1 │
```

```markdown
### Collection failures (unknown, never cleared)

- `galaxy-lab-01` - unreachable at 127.0.0.1: Redfish service root unreachable
```

**An edited plan cannot be applied.** Approve a plan, change a proposed value
in `plan.json`, then apply. The approval carries a SHA-256 digest of the plan
it was given, so apply refuses outright:

```
Error: approval does not match this plan: the plan was regenerated or edited
after it was approved. Re-run 'nbrecon approve'.
```

**A human editing NetBox mid-run wins.** Approve a plan, change the device in
NetBox, then apply. `last_updated` no longer matches the snapshot, so the
device is skipped whole rather than partially written:

```
Applied 0 field(s)
Skipped galaxy-lab-01: edited since the snapshot; it will be re-diffed next run
```

---

## 3. Configure for real

### 3.1 Credentials

```bash
cp .env.example .env
chmod 600 .env
```

`.env` is gitignored and must never be baked into an image.

Start with NetBox alone. Leave Jira and Grafana blank: unconfigured sources are
skipped and reported as skipped, which is safer than half-configured ones.

```ini
NBRECON_NETBOX_URL=https://netbox.it.aws.tenstorrent.com
NBRECON_NETBOX_TOKEN=<token>

NBRECON_BMC_USER=<bmc user>
NBRECON_BMC_PASSWORD=<bmc password>
NBRECON_BMC_VERIFY_TLS=false      # lab sites only; warns on every run

NBRECON_MAX_BATCH=5               # keep this small during the pilot
```

The NetBox token only needs write access to the fields you intend to reconcile.
A read-only token is enough for everything up to and including `plan`, which is
a reasonable way to start.

TLS verification for NetBox, Jira and Grafana is hard-coded on and has no
setting. Only BMC verification can be turned off, because lab BMCs ship
self-signed certificates.

### 3.2 Custom field names

The tool never creates custom fields, and it never guesses their names. Ask
NetBox what exists:

```bash
./.venv/bin/nbrecon preflight --show-custom-fields
```

```
───────────────────── Custom fields on dcim.device ─────────────────────
  chassis_revision  type=text label=Chassis revision
  flash_version     type=text label=Flash version
  ipt_url           type=text label=IPT URL
  ...
```

Paste the matching names into `config/netbox_fields.yaml`. **A field left blank
is skipped entirely** — not guessed, not created. That is the intended way to
disable a field you are not ready to reconcile.

Reconcile a small number of fields first. Leaving most of the file blank on the
first run is a feature, not an incomplete setup.

### 3.3 SKU map

`config/sku_map.yaml` ships empty, so every model reports as unmapped and no
device type is ever written. Fill it only once you know which NetBox device
type slug corresponds to which Redfish part number:

```yaml
by_part_number:
  TT-BH-GALAXY-4U: tt-galaxy-blackhole
```

Part number is matched before model. An unmapped SKU is reported in the
unmatched section and never written; the tool does not create device types or
manufacturers.

### 3.4 Scope

An unscoped run is refused outright, so a stray command cannot reach the fleet.
For the first real run, name exactly one device:

```bash
cp config/scope.example.yaml config/scope.lab.yaml
```

```yaml
devices:
  - galaxy-lab-01
max_devices: 1
```

`max_devices` is capped again by `NBRECON_MAX_BATCH` at runtime, so the scope
file cannot raise its own ceiling.

---

## 4. Run it

### Preflight

```bash
./.venv/bin/nbrecon preflight
```

Checks connectivity and reports what is configured. Exits non-zero if NetBox or
the BMC credentials are unusable. Unconfigured Jira and metrics are warnings,
not failures.

### Collect — read-only

```bash
./.venv/bin/nbrecon collect \
  --scope-file config/scope.lab.yaml \
  --ansible-facts exports/tt-smi.json
```

Reads NetBox, walks Redfish on each BMC, overlays the tt-smi export, and writes
`collection.json`. It never opens an SSH session; the export is produced
separately by an Ansible playbook (see
[ansible-facts-schema.md](ansible-facts-schema.md)). Omit `--ansible-facts` and
the firmware fields simply stay unknown, which means they are never written.

Note the run ID it prints. Every later command defaults to the most recent run,
or takes `--run-id`.

### Plan — the dry run

```bash
./.venv/bin/nbrecon plan --show-all
```

This is where the real review happens. Read the whole report before approving
anything.

- **Proposed writes** — what would change, and why.
- **Flags** — conflicts a human must resolve. A `fill_if_empty` field that
  already holds a different value lands here rather than being overwritten.
- **Report-only** — power state, trays, and human-only fields that are empty.
- **Unmatched** — missing or duplicate serials, unmapped SKUs, unconfirmed IPT
  candidates. None of these can produce a write.
- **Collection failures** — hosts that could not be read. Their NetBox values
  are left alone.

On the very first run, expect the unmatched and report-only sections to be
large. That is the tool telling you what it refuses to guess at.

`report.md` in the run directory is the same content in a form you can paste
into a ticket or a review.

### Approve

```bash
./.venv/bin/nbrecon approve --approver <your-name>
```

Per device, choose `all`, `none`, `each`, or `quit`. Fields marked `*` require
an individual decision and are excluded from `all` — that covers serial writes
and every field on a device matched by suggestion rather than serial.

For a first run, use `each` and answer deliberately.

The approval records who approved, when, and a SHA-256 digest of the exact plan
they saw. Re-planning invalidates it.

### Apply — the only stage that writes

```bash
./.venv/bin/nbrecon apply
```

Prompts once before writing. Before each device it re-reads the record and
skips it if `last_updated` moved since the snapshot. Only approved fields are
sent, the ownership matrix is re-checked rather than trusted from the plan, and
a journal entry is written to the device recording the run.

Jira linking is skipped unless Jira is configured. Ticket creation additionally
requires `--create-ipt`; leave it off until the service account's permissions
are confirmed, because duplicate tickets are the expected failure mode.

### Verify

```bash
./.venv/bin/nbrecon verify
```

Re-reads NetBox and confirms each applied value is actually present. Exits
non-zero if anything disagrees.

### Afterwards

```bash
./.venv/bin/nbrecon history
./.venv/bin/nbrecon history --device galaxy-lab-01
```

The run history is a local SQLite database. It tracks consecutive unreachable
runs so the report can suggest a device might be Offline. The tool never writes
status.

Everything is kept under `var/runs/<run-id>/`, which is gitignored. See the
artifact table in the [README](../README.md#run).

---

## 5. Widening out

1. One device, `plan` only. Read every section.
2. Same device, approve one field, apply, verify.
3. A handful of devices, `--create-ipt` still off.
4. A full batch at `NBRECON_MAX_BATCH`, still one site.
5. Enable `--create-ipt` once the Jira field IDs and permissions are confirmed.
6. Production only after several clean runs.

Re-run `./tools/rehearse.sh` after editing `config/ownership.yaml`. It exercises
the real matrix, so it will catch a rule change that makes a field writable
that should not be.

---

## Troubleshooting

**`refusing to run without a scope`** — the scope file has no selector set. This
is deliberate; name a site, tenant, tag, or device.

**`scope max_devices (N) exceeds the configured batch cap`** — lower
`max_devices` or raise `NBRECON_MAX_BATCH` on purpose.

**Every field reports as unmapped** — `config/netbox_fields.yaml` is still
blank. Run `preflight --show-custom-fields` and paste the real names in.

**Every model reports as an unmapped SKU** — `config/sku_map.yaml` is empty.
That is how it ships; device type writes stay off until you fill it.

**`0 reachable, 1 failed`** — Redfish could not be read. Check the BMC IP in
NetBox (`oob_ip`, falling back to `primary_ip4`), the credentials, and whether
the BMC needs `NBRECON_BMC_VERIFY_TLS=false`. Nothing is written either way.

**`no BMC IP in NetBox`** — the device has neither `oob_ip` nor `primary_ip4`.
The tool does not create IPAM objects; a human adds the address.

**`no IPAM entry for <address>`** at apply time — the BMC answered on an
address NetBox does not know. Create the IP address in NetBox first.

**`interface 'bmc' not found on the device`** — the MAC has nowhere to go. The
tool does not create interfaces. Create it, or blank the `bmc_mac` mapping.

**`device type '<slug>' does not exist in NetBox`** — an admin must create it.
The tool never creates device types or manufacturers.

**`approval does not match this plan`** — the plan changed after approval.
Re-run `approve`.

**`edited since the snapshot`** — someone changed the device mid-run. Expected
behaviour; re-run `collect` and `plan`.
