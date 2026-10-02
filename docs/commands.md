# Command reference

Every command, every option, and which of them can change anything.

[docs/running-one-server.md](running-one-server.md) is the walkthrough; this is
the flat reference to come back to.

## Global options

These go *before* the subcommand.

```bash
nbrecon [--env-file FILE] [--config-dir DIR] [-v] COMMAND [ARGS]
```

| Option | Effect |
| --- | --- |
| `--env-file FILE` | Path to `.env` (default `./.env`) |
| `--config-dir DIR` | Override the config directory |
| `-v`, `--verbose` | Debug logging. Reach for this when a command fails and the message is not enough |
| `--version` | Print the version |

## Which commands can write

The division that matters most. Everything except `apply` is read-only against
NetBox.

| Command | NetBox | BMCs | Writes anywhere |
| --- | --- | --- | --- |
| `preflight` | read | read | no |
| `snapshot` | read | no | local files only |
| `audit-inventory` | read | no | local files only |
| `collect` | read | read | run directory |
| `plan` | no | no | run directory |
| `approve` | no | no | run directory |
| `apply` | **write** | no | NetBox, Jira |
| `verify` | read | no | run directory |
| `history` | no | no | no |

`audit-inventory` is the only one that enforces its own read-only promise
structurally: the NetBox client it holds has had its write methods removed, so
a later edit that reaches for one fails immediately rather than writing.

## Credentials each command needs

Every credential defaults to empty and is checked by the command that needs it,
so most of the tool is usable with only the NetBox pair configured.

| Command | Needs |
| --- | --- |
| `audit-inventory --offline` | nothing at all |
| `snapshot` | `NBRECON_NETBOX_URL`, `NBRECON_NETBOX_TOKEN` |
| `audit-inventory` | NetBox URL and token |
| `preflight` | NetBox URL and token; also BMC user and password to pass fully |
| `collect` onwards | NetBox, BMC, and optionally Jira and Prometheus or Grafana |

`preflight` exits non-zero if *any* check fails, so it reports failure on
missing BMC credentials even when NetBox is perfectly healthy. That is not a
NetBox problem; read the NetBox line specifically.

## Standalone commands

### `preflight`

```bash
nbrecon preflight [--show-custom-fields]
```

Checks connectivity and configuration before a real run.

`--show-custom-fields` lists the custom fields that actually exist on
`dcim.device`, so real names can be copied into `config/netbox_fields.yaml`
instead of guessed at. The tool never creates custom fields.

### `snapshot`

```bash
nbrecon snapshot --scope-file FILE [--json FILE] [--csv FILE] [--limit N] [--quiet]
```

Resolves a scope, reads those devices, prints them. No Redfish, no Prometheus,
no Jira, no run directory and no run-history entry.

The cheapest way to prove the NetBox half of a configuration works, which makes
it the right first command on a new machine.

| Option | Effect |
| --- | --- |
| `--json FILE` | Write the devices as JSON |
| `--csv FILE` | Write the devices as CSV |
| `--limit N` | Stop after N devices (`0` means no limit) |
| `--quiet` | Summary only; do not list every device |

### `audit-inventory`

```bash
nbrecon audit-inventory CSV [--status TEXT] [--all-statuses] [--offline]
                            [--out FILE] [--seed-file FILE]
                            [--conflict-log FILE] [--strict]
```

Compares the Cloud Resources export against NetBox. Answers three questions:
what is wrong with the spreadsheet, what NetBox does not know that the sheet
does, and which of those gaps could be filled safely.

| Option | Effect |
| --- | --- |
| `--status TEXT` | Spreadsheet statuses to audit; repeat for more. Default `In Use` |
| `--all-statuses` | Every row, including decommissioned ones |
| `--offline` | Spreadsheet checks only; never contacts NetBox |
| `--out FILE` | Markdown report, with per-row detail |
| `--seed-file FILE` | Seed worklist as JSON. Nothing consumes this yet |
| `--conflict-log FILE` | Row-against-row contradictions as CSV, for the sheet's owner |
| `--strict` | Exit non-zero if anything needs a human |

Duplicates are derived from *every* row, not just the selected statuses: a
retired row is what distinguishes a reused BMC address from a live collision.

### `history`

```bash
nbrecon history [--limit N] [--device TEXT]
```

Inspects the local run history. `--device` shows the unreachable streak for one
device identifier, which is how a host that has been quietly failing to answer
for weeks becomes visible.

## The pipeline

Each stage reads the previous stage's artifacts from the run directory.
`--run-id` defaults to the most recent run throughout, so it is normally
omitted.

```bash
nbrecon collect --scope-file FILE [--ansible-facts PATH] [--run-id TEXT]
nbrecon plan [--run-id TEXT] [--show-all]
nbrecon approve --approver NAME [--run-id TEXT]
nbrecon apply [--run-id TEXT] [--create-ipt] [--no-journal] [--yes]
nbrecon verify [--run-id TEXT]
```

**`collect`** reads NetBox, Redfish, the tt-smi export, Jira and metrics.
`--ansible-facts` takes a file or a directory, and is required when the scope
names an `ansible_group`.

**`plan`** correlates and diffs, producing the dry-run plan. `--show-all`
includes report-only rows on the console.

**`approve`** is interactive, per device and per field. `--approver` is required
and is recorded in the audit trail.

**`apply`** is the only command that writes to NetBox.

| Option | Effect |
| --- | --- |
| `--create-ipt` | Allow creating IPT tickets. Off by default, and stays off until the Jira service account's permissions are confirmed |
| `--no-journal` | Skip the NetBox journal entry |
| `--yes` | Skip the final confirmation prompt. Leave it off while building trust |

**`verify`** re-reads NetBox and Jira and confirms the applied values are
actually present.

See [docs/pipeline.md](pipeline.md) for what each stage guarantees.

## Scope files

Every scoped command refuses to run without at least one selector, so an
unscoped query cannot happen by accident.

```yaml
# config/scope-f08.yml
rack: F08
max_devices: 20
```

| Key | Type | Notes |
| --- | --- | --- |
| `site` | string | NetBox site slug |
| `tenant` | string | NetBox tenant |
| `rack` | string | Rack name; must match NetBox exactly |
| `tags` | list | NetBox tags |
| `devices` | list | Explicit device names |
| `host_list` | path | File of device names, one per line; `#` starts a comment |
| `ansible_group` | string | Restricts to hosts in that group; requires `--ansible-facts` |
| `max_devices` | int | Default 20 |

Combining selectors narrows rather than widens: `site` with `rack` means that
rack at that site.

`max_devices` bounds how much a single run may **write**, so it applies to
`collect` and the pipeline but not to `snapshot`. A pure read is not limited by
a write cap; use `--limit` there instead.

A rack name that resolves to nothing is an error, not an empty result.
Returning zero devices silently is how a typo gets mistaken for an empty rack.

## A first session

```bash
# 1. Does NetBox work at all? Two environment variables, nothing else.
nbrecon snapshot --scope-file config/scope-f08.yml

# 2. Spreadsheet quality, no credentials needed.
nbrecon audit-inventory ~/Downloads/servers.csv --offline

# 3. Spreadsheet against live NetBox, with a log for whoever owns the sheet.
nbrecon audit-inventory ~/Downloads/servers.csv \
  --out var/audit.md --conflict-log var/conflicts.csv

# 4. Only once BMC credentials exist.
nbrecon preflight
nbrecon collect --scope-file config/scope-f08.yml
nbrecon plan
```

## When a command cannot reach NetBox

A NetBox behind an SSO proxy answers API requests with a redirect to an
identity provider rather than with data. The client refuses that redirect and
says so by name, because the alternative is parsing a login page as a device
list:

```
NetBox GET https://netbox.example/api/dcim/racks/ was redirected to
login.microsoftonline.com. An SSO proxy is intercepting API requests, so the
API token never reaches NetBox. Run from inside the network, or ask for the API
path to be exempted from the proxy's login rule.
```

No credential resolves that. The proxy is answering before NetBox is consulted,
so the fix is a change to the proxy or a host on the inside of it.
